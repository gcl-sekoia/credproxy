"""Workspace `setup` steps: ordered exec, per-step user/env, marker + chown.

Setup commands (config key `setup`) run once per container instance: on a
freshly created/recreated container, and on the next `start` after a failed
attempt (the lock's `applied.setup_container_id` records the container id that
COMPLETED setup, written only on success -- so a failure retries). A plain
`start`/`stop` of an existing container does NOT re-run them (same id, writable
layer intact). Because a recreate re-runs them, setup commands should be
idempotent.

The setup markers ride the lock's `applied` section via containers' shared
`_load_applied`/`_update_applied` helpers.
"""
from __future__ import annotations

import os
import subprocess

from . import containers, docker
from .containers import Notify, _noop
from ..errors import DockerError
from ..model.workspace import Workspace


def _read_setup_marker(ws: Workspace) -> str | None:
    """The container id that last COMPLETED setup (`applied.setup_container_id`),
    or None. Not a docker label -- setup completes AFTER create, and labels are
    immutable post-create."""
    cid = containers._load_applied(ws).get("setup_container_id")
    return cid if isinstance(cid, str) else None


def _write_setup_marker(ws: Workspace, container_id: str) -> None:
    containers._update_applied(ws, setup_container_id=container_id)


def _setup_needed(marker: str | None, container_id: str) -> bool:
    """Setup is needed when this container hasn't recorded a completed run: a
    new/recreated container (different id), or one where a prior attempt failed
    (no marker written). A plain stop/start keeps the id, so setup is skipped."""
    return bool(container_id) and marker != container_id


def _setup_order(entry) -> int:
    """The sort key `order` of a setup entry: 0 for a plain string (today's
    implicit order), the table's normalized `order` otherwise."""
    return 0 if isinstance(entry, str) else entry["order"]


def _setup_execution_order(setup: list) -> list[tuple[int, object]]:
    """`(declaration_index, entry)` pairs in execution order: a STABLE sort by
    `(order, declaration index)`. Strings carry an implicit `order = 0`, so an
    all-string (or all-default) config keeps its declaration order -- exactly
    today's behavior -- and equal orders preserve declaration order. The
    declaration index rides along so notify/error text names the entry by its
    position in the TOML, not its shuffled runtime slot."""
    return sorted(enumerate(setup), key=lambda p: (_setup_order(p[1]), p[0]))


def _resolve_container_home(ws: Workspace, user: str) -> str | None:
    """The home directory of `user` as it exists IN the container right now, or
    None if the user doesn't exist. `docker exec -u <user>` does NOT reliably
    set HOME (it inherits the image's env, e.g. /root), so a workspace-user
    setup step must be told its own HOME -- resolved here by name in-container
    (mirroring chown_user_owned_volumes' by-name approach, sidestepping
    host/userns uid math). Resolved PER STEP, not once up front, because an
    earlier root step may `useradd` the user.

    Portable across images: `getent` when present, falling back to a raw
    /etc/passwd scan (busybox/distroless may lack getent). Field 6 of the passwd
    line is the home dir.

    The in-container lookup ALWAYS exits 0 (a trailing `|| true`), so a nonzero
    `docker exec` return code means the exec itself failed -- the container died,
    a daemon hiccup -- NOT that the user is absent. That case raises a distinct
    DockerError (carrying the stderr/returncode) rather than funnelling into the
    caller's "user does not exist, create it earlier" advice, which would be
    misleading. A genuinely absent user is the `returncode == 0` + empty-output
    path, which returns None.

    The fallback matches the NAME (field 1) OR the UID (field 3) via an exact
    awk string compare, so a legal numeric user (`user = "1000"`) resolves on a
    getent-less busybox where uid 1000 exists, and a username containing a `.`
    (regex-significant) matches literally -- unlike the old name-only
    `grep "^$1:"` regex. `user` stays a positional parameter (`sh -c '...' _
    "$user"`) spliced into an awk `-v` binding, never into the awk program body,
    preserving the no-injection property."""
    r = subprocess.run(
        ["docker", "exec", "-u", "0", ws.ws_container, "sh", "-c",
         'getent passwd "$1" 2>/dev/null '
         '|| awk -F: -v u="$1" \'$1==u||$3==u{print;exit}\' /etc/passwd 2>/dev/null '
         '|| true',
         "_", user],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise DockerError(
            f"resolving HOME for setup user {user!r} failed: `docker exec` "
            f"exited {r.returncode} (the workspace container may have died)"
            + (f": {r.stderr.strip()}" if (r.stderr or "").strip() else "")
        )
    lines = (r.stdout or "").strip().splitlines()
    if not lines:
        return None  # user genuinely absent -> caller's "create it earlier" error
    fields = lines[0].split(":")
    if len(fields) < 6 or not fields[5]:
        return None
    return fields[5]


def run_setup(ws: Workspace, cfg: dict, notify: Notify = _noop,
              bindings: list | None = None) -> None:
    """Run the `setup` steps in the workspace container via `docker exec`.
    Called from start_workspace when the container hasn't recorded a completed
    setup (see `_setup_needed`): a fresh container, or a prior attempt that
    failed. Steps run in `(order, declaration index)` order (stable sort). A
    failing step raises DockerError and leaves the container running for
    debugging -- and, since the success marker isn't written, the next `start`
    retries all of setup.

    Two entry shapes (normalized by `config._parse_setup`):

    - A plain STRING runs EXACTLY as before -- root (`-u 0`), `sh -lc`, NO
      injected env. Root is pinned rather than inherited because the container's
      default run-user isn't always root (`map_host_user`'s keep-id runs podman
      as the mapped non-root uid; an image can bake `USER dev`), and setup is
      the place to provision (useradd, apt, chown the home volume). uid 0 is
      root-in-namespace even under keep-id.
    - A TABLE `{run, user, order}` runs as the workspace `user` (`user =
      "workspace"`, the default; falls back to root when config has no `user`)
      or root (`user = "root"`), and additionally receives the BINDING ENV --
      each binding's effective env var set to its placeholder, the same set
      /exports.sh serves (computed host-side, no in-container curl). A
      workspace-user step also gets `-e HOME=<home>` resolved in-container per
      step (see _resolve_container_home).

    `enter` pins its own `-u <user>`, so setup and enter never collide. Setup
    steps should be idempotent: a container recreate re-runs them while the
    persistent home volume survives, so writable-layer work is re-provisioned
    and home-volume work just needs to be cheap to repeat."""
    from ..model.bindings import binding_env_map
    setup = cfg.get("setup") or []
    if not setup:
        return
    # Binding env (VAR=placeholder) injected into TABLE entries only; strings
    # get nothing (their unchanged escape-hatch semantics). Computed once.
    env_map = binding_env_map(bindings or [])
    ws_user = cfg.get("user")  # config user; None -> root

    notify(f"running {len(setup)} setup command(s)...")
    for idx, entry in _setup_execution_order(setup):
        run, argv, user_label, order = _build_setup_exec(
            ws, entry, idx, ws_user, env_map)
        notify(f"  setup[{idx}] (user={user_label}, order={order}): {run}")
        r = subprocess.run(argv, check=False)
        if r.returncode != 0:
            raise DockerError(
                f"setup command failed (exit {r.returncode}): {run!r}\n"
                f"The workspace container is left running for debugging."
            )


def _build_setup_exec(
    ws: Workspace, entry, idx: int, ws_user: str | None,
    env_map: dict[str, str],
) -> tuple[str, list[str], str, int]:
    """Build the `docker exec` argv for one setup entry, returning
    `(run_cmd, argv, user_label, order)` for both execution and the notify line.

    A string is today's argv verbatim: `-u 0`, no env, `sh -lc CMD`. A table
    resolves its `user` (workspace -> config user, or root), gets the binding
    env, and -- when non-root -- resolves + passes `-e HOME=<home>` (failing
    with a precise error if the user doesn't exist yet)."""
    if isinstance(entry, str):
        return (entry,
                ["docker", "exec", "-u", "0", ws.ws_container,
                 "sh", "-lc", entry],
                "root", 0)

    run = entry["run"]
    order = entry["order"]
    # user = "workspace" -> the config `user` (None -> root, per the spec's
    # "unset/root -> run as-is" mirror); user = "root" -> root.
    resolved = ws_user if entry["user"] == "workspace" else None
    is_root = (not resolved) or resolved.split(":", 1)[0] in ("root", "0")

    env_flags: list[str] = []
    if is_root:
        # decision-6-wins: a TABLE entry gets the binding env even when it
        # resolves to root; only plain STRINGS are the env-free escape hatch.
        for k, v in env_map.items():
            env_flags += ["-e", f"{k}={v}"]
        return (run,
                ["docker", "exec", "-u", "0", *env_flags, ws.ws_container,
                 "sh", "-lc", run],
                "root", order)

    # Non-root workspace user: resolve HOME per step (an earlier root step may
    # have created the user), then pass it plus the binding env.
    home = _resolve_container_home(ws, resolved.split(":", 1)[0])
    if home is None:
        raise DockerError(
            f"user {resolved!r} does not exist in the container when "
            f"setup[{idx}] runs -- create it in an earlier root step or set "
            f'user = "root"'
        )
    env_flags = ["-e", f"HOME={home}"]
    for k, v in env_map.items():
        env_flags += ["-e", f"{k}={v}"]
    return (run,
            ["docker", "exec", "-u", resolved, *env_flags, ws.ws_container,
             "sh", "-lc", run],
            resolved, order)


def chown_user_owned_volumes(ws: Workspace, cfg: dict, notify: Notify) -> None:
    """Chown each `user_owned` managed volume to the workspace `user`, so a
    non-root user can write a volume mounted at a path the image doesn't
    populate (Docker creates such a volume root-owned -- there is no image
    content to seed ownership from).

    Opt-in per volume, never blanket: a `chown -R` would clobber a volume that
    WAS image-seeded with intentional ownership, so only volumes that declared
    `user_owned = true` are touched. Runs AFTER `run_setup`, unlike
    chown_mount_parents -- the `user` may be provisioned by setup, so it must
    exist before we chown to it BY NAME (chown resolves the name in the
    container's /etc/passwd, sidestepping host/userns uid arithmetic). Owner
    only (group left as-is) so it works under coreutils and busybox alike, and
    independent of `map_host_user` -- the gap exists on plain Docker too."""
    user = cfg.get("user")
    if not user or user.split(":", 1)[0] in ("root", "0"):
        return
    targets = [m["target"] for m in cfg["mounts"]
               if m["kind"] == "volume" and m.get("user_owned")]
    if not targets:
        return
    notify(f"making '{user}' own {len(targets)} user-owned volume(s)...")
    docker.docker(["exec", "-u", "0", ws.ws_container,
                   "chown", "-R", user, *targets])
