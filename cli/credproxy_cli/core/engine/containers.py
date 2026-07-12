"""Container primitives: docker run/rm/stop argv, spec/drift, volume lifecycle.

This module owns the low-level container operations the rest of the engine
plane builds on: `docker run` argv construction (proxy + workspace), the
hostname/userns/SELinux flag logic, managed-volume lifecycle, spec-drift
detection + the inspect surface, and the SHARED primitives (`Notify`/`_noop`,
the lock's `applied` section I/O) that `startup`/`setup`/`sessions` import.

Progress that the old monolith printed from inside these helpers is now
surfaced via an optional `notify: Callable[[str], None]`. Porcelain wires
it to stderr rendering (the `[credproxy] ` prefix); when omitted, progress
is silently dropped. The core never imports porcelain and never prints.

Applied-state (written into the lock's `applied` section, no side effects on
read -- #65 folded the old per-file artifacts into `lock.json`):
  applied.spec              — the spec dict that fed the last successful
      workspace container creation (image, mounts, env, setup, run_flags,
      map_host_user, user_uid, hostname, proxy_id). inspect/apply itemizable drift.
  applied.bindings          — binding metadata (name, injector, provider, secret,
      hosts, placeholder, env) pushed to the proxy after a successful config
      push. NO secret values. Used by inspect/apply.
  applied.rules             — rule wire metadata last pushed. No secrets.
  applied.config_generation — the generation the proxy returned for the last
      accepted push (groundwork for #66).
  applied.setup_container_id — the container id that last COMPLETED setup.

All `applied` writes go through the model plane's `lock.update("applied", ...)`
(load-modify-write the whole lock), so they preserve the resolver's
`placeholders`/`presets` and vice versa -- sequenced AFTER any resolver
`save_lock` within the same held workspace flock so neither reads a stale file.
"""
from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import Callable

from . import docker
from ..model.config import (
    load_config,
    render_template,
)
from ..errors import (
    ConfigError, CredproxyError, DockerError, ImageError, WorkspaceError,
)
from .imageenv import ImageEnv
from ..model.workspace import Workspace, ensure_token, hostname_for
from ..paths import IMAGE_TAG, PROXY_DIR, atomic_write_text
from .proxy_http import get_config

Notify = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


@dataclass(frozen=True)
class WorkspaceStatus:
    name: str
    running: bool
    image: str


def list_workspaces() -> list[WorkspaceStatus]:
    """Structured status for every workspace: name, running?, image.

    Docker is queried for the running state; image is read from the TOML."""
    from ..model.config import quick_image
    from ..model.workspace import list_names

    out = []
    for name in list_names():
        ws = Workspace(name)
        running = docker.container_status(ws.ws_container) == "running"
        out.append(WorkspaceStatus(name, running, quick_image(ws)))
    return out


# ---- applied-state helpers (the lock's `applied` section) -------------------
#
# The old per-file artifacts (applied-spec.json / applied-bindings.json /
# applied-rules.json / setup_done) are folded into `lock.json`'s `applied`
# section (#65). Every write merges into that section via lock.update, which
# load-modify-writes the WHOLE lock so the resolver's placeholders/presets are
# preserved; every read tolerates a missing lock/section (== "nothing applied").


def _load_applied(ws: Workspace) -> dict:
    """The lock's `applied` section, or {} if the lock/section is absent (== a
    fresh workspace with nothing applied yet -- the old missing-file case)."""
    from ..model.lock import load_lock

    try:
        applied = load_lock(ws).get("applied")
    except (ValueError, OSError):
        return {}
    return applied if isinstance(applied, dict) else {}


def _update_applied(ws: Workspace, **fields) -> None:
    """Merge `fields` into the lock's `applied` section, preserving the resolver's
    placeholders/presets (and any applied keys not being written) by routing
    through lock.update (load-modify-write of the whole lock). Callers hold the
    workspace flock and have already persisted any dirty resolver lock, so this
    reads a fresh file and clobbers nothing."""
    from ..model.lock import update as lock_update

    applied = dict(_load_applied(ws))
    applied.update(fields)
    lock_update(ws, "applied", applied)


def _write_applied_spec(ws: Workspace, cfg: dict, proxy_id: str | None) -> None:
    """Record the workspace launch spec in `applied.spec`.

    Called after creating the workspace container. The spec matches what
    workspace_spec_hash() hashes, so drift can be recomputed exactly."""
    spec = {
        "image": cfg["image"],
        "mounts": cfg["mounts"],
        "env": cfg["env"],
        "setup": cfg["setup"],
        "run_flags": cfg.get("run_flags") or [],
        "map_host_user": bool(cfg.get("map_host_user")),
        "user_uid": cfg.get("user_uid"),
        "hostname": hostname_for(ws.name),
        "proxy_id": proxy_id,
    }
    _update_applied(ws, spec=spec)


def _binding_applied_records(bindings) -> list[dict]:
    """Binding metadata (NO secret values) for the `applied.bindings` record.
    `bindings` is a list of Binding dataclass instances (from bindings.py)."""
    return [
        {
            "name": b.name,
            "injector": b.injector,
            "provider": b.provider,
            "secret": b.secret,
            "hosts": list(b.hosts),
            "placeholder": b.placeholder,
            "env": b.env,
        }
        for b in bindings
    ]


def _write_applied_push(ws: Workspace, bindings, rules,
                        generation: int | None) -> None:
    """Record the metadata of a successful config push into `applied`: binding
    metadata (no secret values), rule wire metadata (rules carry no secret), and
    the `config_generation` the proxy returned (None if the response omitted it).
    Written in ONE lock update so the three move together."""
    from ..model.rules import rule_wire_entries

    fields = {
        "bindings": _binding_applied_records(bindings),
        "rules": rule_wire_entries(rules),
        # Always overwrite: a None generation (the proxy omitted it -- e.g. an
        # attach.admin_url target on a proxy build that doesn't return it) must
        # NOT leave a PREVIOUS push's generation attributed to this one. Stored
        # null reads back as None == "unknown" for #66's reality comparison.
        "config_generation": generation,
    }
    _update_applied(ws, **fields)


def _load_applied_rules(ws: Workspace) -> list[dict] | None:
    """The last recorded applied rules (`applied.rules`), or None if not present."""
    r = _load_applied(ws).get("rules")
    return r if isinstance(r, list) else None


def create_workspace_files(ws: Workspace, text: str | None = None) -> None:
    """Scaffold a managed workspace's config + auth token. `text` overrides the
    rendered template (used by `create` after in-memory `[[preset]]` expansion, so
    the write is a single atomic step -- all-or-nothing)."""
    if ws.exists():
        raise WorkspaceError(
            f"workspace '{ws.name}' already exists ({ws.config_path})"
        )
    ws.config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ws.config_path,
                      render_template(ws.name) if text is None else text)
    ensure_token(ws)


def create_attached_workspace_files(ws: Workspace, selector: dict,
                                    text: str | None = None) -> None:
    """Scaffold an ATTACHED workspace: the attach template stamped with the given
    (already-validated, normalized) selector, plus the auth token. The token is
    still host-owned -- it authenticates the config push to the externally-run
    proxy exactly as for a managed workspace. `text` overrides the rendered
    template (post-`[[preset]]`-expansion; single atomic write)."""
    from ..model.config import render_attach_template

    if ws.exists():
        raise WorkspaceError(
            f"workspace '{ws.name}' already exists ({ws.config_path})"
        )
    ws.config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        ws.config_path,
        render_attach_template(ws.name, selector) if text is None else text)
    ensure_token(ws)


def resolve_admin_url(ws: Workspace, notify: Notify = _noop) -> str:
    """The loopback admin base URL for a workspace's proxy, managed or attached.

    Managed: the proxy container's published ephemeral port (resolved live, I6);
    raises WorkspaceError if it isn't running (G4 -- push does not auto-start).
    Attached: the `attach` selector resolved via docker (container/discover) or
    used verbatim (admin_url)."""
    from . import push as core_push

    cfg = load_config(ws)
    attach = cfg.get("attach")
    if attach is not None:
        return core_push.resolve_admin_url(attach, notify)
    meta = ImageEnv.load()
    if docker.container_status(ws.proxy_container) != "running":
        raise WorkspaceError(
            f"workspace '{ws.name}' proxy is not running; start it first "
            f"(`credproxy workspace {ws.name} start`) before pushing")
    port = docker.resolve_host_port(ws.proxy_container, meta.http_port)
    return f"http://127.0.0.1:{port}"


def create_proxy(ws: Workspace, meta: ImageEnv) -> None:
    args = [
        "run", "-d",
        "--name", ws.proxy_container,
        # Name the container's UTS host after the workspace, so the in-container
        # shell prompt reads `user@myproject` not a hex id. On Docker the netns
        # joiner (the workspace) INHERITS this hostname (Docker rejects
        # --hostname on the joiner), so setting it here is the only lever there;
        # on podman it just names the proxy (the workspace carries its own copy,
        # added in create_ws_container). Always safe on both runtimes. Guarded
        # against an empty sanitized name (can't happen given the name charset).
        *(["--hostname", hostname_for(ws.name)] if hostname_for(ws.name) else []),
        "--label", "credproxy.role=proxy",
        "--label", f"credproxy.workspace={ws.name}",
        "--cap-add", "NET_ADMIN",
        # The workspace's own name, exposed to the workspace via /setup (e.g.
        # to customize the shell prompt). Not a secret -- it is the instance's
        # handle. Set at create; inherited by the proxy process and persists
        # across `docker start` / `dev reload`.
        "-e", f"CREDPROXY_WORKSPACE={ws.name}",
        # mode=1777 so the proxy's unprivileged uid can write config.json:
        # the tmpfs dir's default mode is not writable by it, and Docker
        # mounts it differently on `docker run` vs. a later `docker start`.
        "--tmpfs", f"{meta.tmpfs}:size=64k,mode=1777",
        # Bind the host token in read-only. The `:Z` SELinux relabel (private)
        # is required on enforcing-SELinux hosts (Fedora/RHEL) so the proxy can
        # read it -- without it the file keeps its host label and the container
        # is denied. `:Z` is a no-op on non-SELinux hosts and accepted by both
        # Docker and Podman; `-v` (not `--mount`) is used because Docker rejects
        # `relabel=` on `--mount`. The proxy stays SELinux-confined (it is the
        # privileged, secret-holding component); only the workspace disables it.
        "-v", f"{ws.token_path}:{meta.token}:ro,Z",
        # Ephemeral host port: the runtime assigns a free port per proxy
        # container so multiple workspaces run simultaneously without port
        # conflicts. The empty host-port spelling (`ip::container`) means
        # "pick a random port" on both Docker and Podman; Docker also accepts
        # `:0:` but Podman does not, so use `::` for cross-runtime support.
        "-p", f"127.0.0.1::{meta.http_port}",
    ]
    # Dev convenience: bind-mount the proxy source so `dev reload` picks
    # up edits. Skipped if run outside the repo checkout. `:z` (shared SELinux
    # relabel) so the proxy can read it under enforcing SELinux; no-op without.
    if PROXY_DIR.is_dir():
        args += ["-v", f"{PROXY_DIR}:{meta.source}:z"]
    args.append(IMAGE_TAG)
    docker.docker(args)


def _credproxy_owns_user_mapping(cfg: dict) -> bool:
    """True when credproxy owns this workspace's uid mapping: `map_host_user` is
    set and the workspace runs as a non-root `user`.

    The precondition for both levers that assume host-you maps onto the container
    user -- the `--userns=keep-id` flag (rootless podman) and the mount-parent
    chown (every runtime). Outside this mode credproxy must not presume the
    mapped uid is the right owner: a root workspace already owns everything, and
    a user-supplied `run_flags` namespace is theirs to map."""
    user = cfg.get("user")
    return bool(cfg.get("map_host_user") and user and user not in ("root", "0"))


def _mapped_uid(cfg: dict) -> int:
    """The workspace user's in-container uid -- the uid host-you maps onto under
    `map_host_user`, so the owner that keep-id targets AND the mount-parent chown
    must use. `user_uid` if set (a baked user like vscode=1000), else the host
    uid (a user provisioned as $CREDPROXY_HOST_UID in `setup`). Callers guard
    `hasattr(os, "getuid")` before relying on the fallback."""
    uid = cfg.get("user_uid")
    return os.getuid() if uid is None else uid


def _reserved_uid_check(cfg: dict, meta: ImageEnv) -> None:
    """Refuse a workspace that would run egress as the proxy's mitmproxy uid.

    The workspace shares the proxy's netns, where the iptables loop-prevention
    rule RETURNs (un-proxied) every packet from that uid so mitmproxy's own
    outbound isn't re-captured. A workspace process running as the SAME uid
    therefore silently bypasses interception entirely. Covers the config-settable
    vectors -- `user_uid` and a numeric `user` (uid or uid:gid); raw
    `run_flags --user` is the user's own escape hatch and left to them. The
    reserved uid comes from the image (CREDPROXY_MITMPROXY_UID), not a CLI
    constant."""
    reserved = meta.mitmproxy_uid
    uid = cfg.get("user_uid")
    user = cfg.get("user")
    user_uid_part = user.split(":", 1)[0] if isinstance(user, str) else None
    if uid == reserved or user_uid_part == str(reserved):
        raise ConfigError(
            f"workspace uid collides with the proxy's reserved uid {reserved}: a "
            f"workspace process running as uid {reserved} bypasses egress "
            f"interception (the shared-netns loop-prevention rule exempts that "
            f"uid). Use a different user_uid/user."
        )


def _host_user_run_flags(cfg: dict) -> list[str]:
    """The userns flag that makes a non-root `user` own the bind mounts when
    `map_host_user` is set -- runtime-specific, host ownership untouched.

    Only rootless podman needs a lever: there the userns maps host-you to
    container-root, so a non-root user can't write the mounts; --userns=keep-id
    maps your host uid/gid onto the workspace user instead. On Docker (uids 1:1)
    and when map_host_user is off this is a no-op. Returns [] unless the runtime
    actually needs it, so the same config stays portable across runtimes.

    The keep-id `uid` is the workspace user's IN-CONTAINER uid -- that's the side
    of the map host-you must land on for the user to own the mounts. It comes
    from `user_uid` if set (e.g. the default image's `vscode` is uid 1000),
    otherwise it falls back to the host uid (correct for a user provisioned in
    `setup` as $CREDPROXY_HOST_UID). host uid and the user's uid may differ
    freely -- keep-id maps across them; they need not be equal.

    A no-op without credproxy-owned mapping: the default root workspace already
    owns the mounts on every runtime, so there is nothing to map (and we skip the
    runtime probe). Checked before the probe so the common root workspace pays no
    daemon round-trip."""
    if not _credproxy_owns_user_mapping(cfg) or not hasattr(os, "getuid"):
        return []
    from .runtime import is_podman_rootless
    if not is_podman_rootless():
        return []
    return [f"--userns=keep-id:uid={_mapped_uid(cfg)},gid={os.getgid()}"]


def _run_flags_override_userns(cfg: dict) -> bool:
    """True iff the user hand-rolled their own `--userns` in `run_flags`. Those
    are applied AFTER credproxy's keep-id (docker last-wins), so the user's
    choice is what actually takes effect -- meaning credproxy's keep-id is NOT
    in force and the runc `sysfs` gotcha (#50) is the user's to own, not ours."""
    run_flags = cfg.get("run_flags") or []
    return any(f == "--userns" or f.startswith("--userns=") for f in run_flags)


def emits_keep_id(cfg: dict) -> bool:
    """True iff THIS run gets credproxy's `--userns=keep-id` in effect: credproxy
    owns the mapping (`map_host_user` + non-root user) on rootless podman AND the
    user hasn't overridden it with a `--userns` in `run_flags`.

    This is the exact combination that -- together with the always-present netns
    join (`--network container:`) -- trips the runc `sysfs` mount limitation
    (#50). Shared by the post-failure error enrichment and doctor's predictive
    check so they can never disagree on what credproxy emitted."""
    return bool(_host_user_run_flags(cfg)) and not _run_flags_override_userns(cfg)


# The runc `sysfs` failure signature (#50): runc refuses a fresh read-only sysfs
# mount when the mounter's userns (keep-id) doesn't own the joined netns. We
# require BOTH the `sysfs` token and a permission-denied phrase so an unrelated
# docker-run failure is never misattributed to this cause.
_RUNC_SYSFS_HINT = (
    "This is the known runc limitation on rootless podman: mounting a fresh "
    "sysfs at /sys requires the mounter's user namespace to own the network "
    "namespace, but --userns=keep-id (from map_host_user) puts the workspace in "
    "a different userns than the proxy's shared netns. crun handles this case; "
    "runc does not.\n"
    "Fix it either way:\n"
    "  - switch podman to crun (per-user default) by adding to\n"
    "    ~/.config/containers/containers.conf:\n"
    "        [engine]\n"
    '        runtime = "crun"\n'
    "  - or set `map_host_user = false` in this workspace's TOML "
    "(the non-root user then can't own bind mounts on rootless podman).\n"
    "See docs/troubleshooting.md "
    '("Workspace fails to start with a sysfs mount error (rootless podman + runc)").'
)


def _enrich_ws_run_error(e: DockerError, cfg: dict) -> DockerError:
    """Augment a WORKSPACE-container `docker run` failure with an actionable hint
    when it matches the runc `sysfs` signature AND credproxy emitted keep-id for
    this run (#50). Otherwise the original error passes through untouched -- the
    raw OCI text is always preserved (prepended to the hint) so it stays
    greppable/googlable."""
    msg = str(e)
    low = msg.lower()
    sysfs_signature = "sysfs" in low and (
        "operation not permitted" in low or "oci permission denied" in low)
    if sysfs_signature and emits_keep_id(cfg):
        return DockerError(f"{msg}\n\n{_RUNC_SYSFS_HINT}")
    return e


def _mount_parent_dirs(cfg: dict) -> list[str]:
    """Directories the runtime fabricates (as container-root) for mount targets
    nested under a managed *volume* -- the intermediate path components between
    the enclosing volume and each mount point.

    Generalizes the old home-anchored rule to any volume mount. Re-owning a
    fabricated parent is host-safe only when it lives inside a credproxy-managed
    volume (or the ephemeral writable layer), never inside a **host bind** -- so
    we chown the volume-enclosed case (the common nested mount, e.g. `~/src/app`
    under the home volume) and skip anything under a bind. Writable-layer nesting
    is left to the user (we can't know which ancestors the image already owns).
    A mount point itself is never chowned -- it's owned by its own mount."""
    volume_targets = sorted(
        (m["target"].rstrip("/") or "/" for m in cfg["mounts"] if m["kind"] == "volume"),
        key=len, reverse=True,
    )
    all_targets = {m["target"].rstrip("/") or "/" for m in cfg["mounts"]}
    dirs: set[str] = set()
    for m in cfg["mounts"]:
        target = m["target"].rstrip("/") or "/"
        enclosing = next((v for v in volume_targets if target.startswith(v + "/")), None)
        if enclosing is None:
            continue
        d = posixpath.dirname(target)
        while d != enclosing and d != "/":
            if d not in all_targets:        # never chown another mount point
                dirs.add(d)
            d = posixpath.dirname(d)
    return sorted(dirs)


def chown_mount_parents(ws: Workspace, cfg: dict, notify: Notify) -> None:
    """Re-own the runtime-fabricated parents of nested bind mounts to the
    workspace user, so `map_host_user`'s promise (the non-root user owns the
    mounts) holds for the dirs the runtime invented as container-root too.

    Gated on credproxy owning the uid mapping; the chown target is the mapped uid
    (`user_uid`/host uid -- the same uid keep-id uses, so the parents land on the
    user that runs inside). Runtime-agnostic, unlike the keep-id flag: the
    fabricated parent is container-root on rootless podman AND rootful Docker
    (uid 0 is host root), so a non-root user is locked out on both. Non-recursive
    -- chowns only the intermediate dirs, never a mount point, so host files are
    untouched. Idempotent (everything under the home volume should be user-owned
    anyway), so re-chowning pre-existing ancestors is a harmless no-op."""
    if not _credproxy_owns_user_mapping(cfg) or not hasattr(os, "getuid"):
        return
    parents = _mount_parent_dirs(cfg)
    if not parents:
        return
    notify(f"fixing ownership of {len(parents)} mount parent dir(s)...")
    docker.docker(["exec", "-u", "0", ws.ws_container,
                   "chown", f"{_mapped_uid(cfg)}:{os.getgid()}", *parents])


def _ws_hostname_flag(ws: Workspace, cfg: dict) -> list[str]:
    """`--hostname <name>` for the workspace container, or [] when not wanted.

    Only podman needs (and accepts) it on the netns joiner: a podman netns join
    leaves UTS independent, so the workspace keeps its own hostname and podman
    lets us set it here. Docker rejects --hostname on the joiner (the workspace
    inherits the proxy's, set in create_proxy), so we never add it there.

    run_flags wins: if the user already set --hostname (either `--hostname X` or
    `--hostname=X`), credproxy leaves it alone -- run_flags is the escape hatch,
    mirroring the --userns precedence. Also skipped if the sanitized name is
    empty (can't happen given the name charset, but guarded)."""
    from .runtime import is_podman
    host = hostname_for(ws.name)
    if not host or not is_podman():
        return []
    run_flags = cfg.get("run_flags") or []
    if any(f == "--hostname" or f.startswith("--hostname=") for f in run_flags):
        return []
    return ["--hostname", host]


def create_ws_container(
    ws: Workspace, cfg: dict, spec_hash: str, proxy_id: str | None = None
) -> None:
    args = [
        "run", "-d",
        # credproxy-managed userns mapping (map_host_user) goes FIRST, so a
        # user-supplied --userns in run_flags below overrides it (docker
        # last-wins). run_flags is the escape hatch and beats the convenience
        # knob -- mirroring how exec_flags overrides config user/workdir on
        # `enter`. Safe to let run_flags win here because the user namespace is
        # orthogonal to the shared netns (--network container:..., below).
        *_host_user_run_flags(cfg),
        # Escape hatch: after keep-id (so it can override it) but BEFORE
        # credproxy's structural flags below (--name, labels, --network, home
        # volume), which are applied last and win on conflict -- so a stray
        # --network/--name in run_flags still can't detach the netns or rename
        # the box. Additive flags (extra --mount/-v, --security-opt) just apply.
        *(cfg.get("run_flags") or []),
        "--name", ws.ws_container,
        "--label", "credproxy.role=workspace",
        "--label", f"credproxy.workspace={ws.name}",
        "--label", f"credproxy.spec={spec_hash}",
        # Run the workspace with SELinux labeling disabled (like distrobox /
        # toolbx). On enforcing-SELinux hosts this lets the user's bind mounts
        # be read WITHOUT relabeling -- i.e. without mutating the SELinux
        # context of the user's own project directories. It is a no-op on
        # non-SELinux hosts. The tradeoff is that the workspace container loses
        # SELinux confinement; acceptable because the workspace runs the user's
        # own workload (credproxy is a credential boundary, not a hardened
        # jail) and the privileged proxy stays confined.
        "--security-opt", "label=disable",
        # Share the proxy's netns so all egress is captured.
        "--network", f"container:{ws.proxy_container}",
        # Name the UTS host after the workspace (podman only; Docker inherits the
        # proxy's -- see create_proxy). Suppressed if the user's run_flags already
        # set --hostname. Empty on Docker/absent-daemon.
        *_ws_hostname_flag(ws, cfg),
    ]
    # Create this workspace's managed volumes up front, LABELLED with the
    # workspace name, so delete can enumerate them unambiguously (see
    # _workspace_volumes). Must run before the `docker run` that mounts them.
    _ensure_managed_volumes(ws, cfg)
    # Mounts, by kind. A managed `volume` (incl. the `home` sugar) is a named
    # Docker volume, namespaced per workspace, image-seeded on first run. `bind`
    # and `overlay` (an overlay-relative bind) are host binds.
    for m in cfg["mounts"]:
        if m["kind"] == "volume":
            opt = f"{ws.volume(m['name'])}:{m['target']}"
            if m["readonly"]:
                opt += ":ro"
            args += ["-v", opt]
        else:
            opt = f"type=bind,source={m['source']},target={m['target']}"
            if m["readonly"]:
                opt += ",readonly"
            args += ["--mount", opt]
    # Self-config breadcrumb: a tenant (e.g. an agent) that inspects its
    # environment finds the inward setup surface proactively, without first
    # having to trip a TLS-interception error and investigate it. Points at the
    # agent-facing guidance; /etc/hosts already resolves proxy.local.
    args += ["-e", "CREDPROXY_SETUP=http://proxy.local/llms.txt"]
    # The workspace's own name, also available via /setup. Handy for setup
    # scripts and shell rc (e.g. a prompt label) that would otherwise template
    # the literal name. Stable per workspace, so (like CREDPROXY_SETUP) it's not
    # part of the spec hash -- an existing container picks it up on next recreate.
    args += ["-e", f"CREDPROXY_WORKSPACE={ws.name}"]
    # Host identity breadcrumb: the uid/gid the CLI runs as -- i.e. the owner of
    # the user's bind-mounted project dirs. `setup` can match a non-root user to
    # it (`useradd -u $CREDPROXY_HOST_UID dev`) so that user can read/write the
    # mounts without changing host ownership; the same value feeds a rootless
    # podman `run_flags = ["--userns=keep-id:uid=$CREDPROXY_HOST_UID"]`. Stable
    # per host user, so (like CREDPROXY_SETUP) it's not part of the spec hash.
    host_uid = getattr(os, "getuid", lambda: None)()
    host_gid = getattr(os, "getgid", lambda: None)()
    if host_uid is not None:
        args += ["-e", f"CREDPROXY_HOST_UID={host_uid}",
                 "-e", f"CREDPROXY_HOST_GID={host_gid}"]
    # The configured `user` (exec identity) -- the NAME to pair with the uid
    # breadcrumbs above, so a `setup` script (which runs as root) can provision
    # that user without templating the literal: `useradd $CREDPROXY_USER`,
    # `chown -R $CREDPROXY_USER ...`. Only emitted when `user` is set (else the
    # image default applies and there's no name to expose). This is the
    # *configured default*; a per-session `enter --user NAME` override does not
    # change it. Exec-only + stable, so (like the others) it's not in the spec hash.
    if cfg.get("user"):
        args += ["-e", f"CREDPROXY_USER={cfg['user']}"]
    # env vars from config (after the breadcrumbs, so a user could override them)
    for k, v in cfg.get("env", {}).items():
        args += ["-e", f"{k}={v}"]
    # `tail -f /dev/null` keeps the container alive to `exec` into; the
    # image's own CMD is irrelevant to a credproxy workspace. `tail` is
    # used over `sleep infinity` because it works on busybox/alpine too.
    args += [cfg["image"], "tail", "-f", "/dev/null"]
    docker.docker(args)
    # Record the applied spec for itemizable drift.
    _write_applied_spec(ws, cfg, proxy_id)


def stop_workspace(ws: Workspace) -> None:
    """Stop the workspace, then the proxy (the workspace shares the
    proxy's netns). Best-effort -- absent containers are fine. A short
    timeout: PID 1 in both containers ignores SIGTERM, so the default
    10s grace would just delay the SIGKILL."""
    with ws.lock():
        docker.docker_quiet(["stop", "-t", "1", ws.ws_container])
        docker.docker_quiet(["stop", "-t", "1", ws.proxy_container])


def _proxy_diagnostics(ws: Workspace) -> str:
    """Explain why the proxy didn't become capture-ready, by inspecting its
    container. The common case is a crash on boot (the container has exited);
    surface its exit code and recent log tail so the failure is actionable
    without a second command. (When the container is still up, /health answered
    503 with the specific pending reason -- carried in the ProxyError this
    appends to -- so the tail below explains why it's stuck, not that it's mute.)"""
    status = docker.container_status(ws.proxy_container)
    if status is None:
        return f"  (the proxy container {ws.proxy_container} is gone)"
    if status == "exited":
        code = docker.inspect(ws.proxy_container, "{{.State.ExitCode}}") or "?"
        head = f"  the proxy container exited (code {code}) -- it crashed on startup."
    else:
        head = f"  the proxy container is '{status}' but not yet capture-ready."
    lines = [head]
    tail = [ln for ln in docker.logs_tail(ws.proxy_container, 20).splitlines() if ln.strip()]
    if tail:
        lines.append("  last proxy log lines:")
        lines += [f"    {ln}" for ln in tail[-12:]]
    lines.append(f"  full logs: credproxy workspace {ws.name} logs")
    return "\n".join(lines)


def _should_push(force_push: bool, proxy_fresh: bool,
                 status: dict | None, want_fp: str) -> bool:
    """Decide whether to (re)push config. Push when forced (`enter --push`,
    `start`), when the proxy was just (re)started (its tmpfs config is empty),
    or when we can't confirm it already holds the intended config -- it is
    unreachable/unknown (None), reports no config, or reports a different
    fingerprint. Only a confirmed matching fingerprint skips the push."""
    if force_push or proxy_fresh:
        return True
    if not status or not status.get("loaded"):
        return True
    return status.get("fingerprint") != want_fp


_VOLUME_OWNER_LABEL = "credproxy.workspace"


def _ensure_managed_volumes(ws: Workspace, cfg: dict) -> None:
    """Create this workspace's managed volumes up front, each LABELLED with the
    owning workspace, so they can later be enumerated unambiguously for delete.

    A `docker run -v name:target` would auto-create the volume too, but unlabelled
    -- and ownership can't be inferred from the NAME by prefix, because
    `credproxy-vol-foo-` is also a prefix of `credproxy-vol-foo-bar-home`, so
    deleting `foo` would catch `foo-bar`'s volumes. `docker volume create` is
    idempotent and leaves an existing volume's data intact, so image-seeding on
    first mount is unaffected. Best-effort: if labelling fails the run still
    auto-creates the volume (unlabelled -> a possible orphan on delete, never a
    cross-workspace deletion)."""
    for m in cfg["mounts"]:
        if m["kind"] != "volume":
            continue
        docker.docker_quiet([
            "volume", "create",
            "--label", f"{_VOLUME_OWNER_LABEL}={ws.name}",
            "--label", f"credproxy.volume={m['name']}",
            ws.volume(m["name"]),
        ])


def _workspace_volumes(ws: Workspace) -> list[str]:
    """This workspace's managed Docker volumes, found by the owner LABEL (exact
    match) -- NOT a name prefix, which would also match another workspace whose
    name extends this one (`foo` vs `foo-bar`). Returns [] if none / docker
    unavailable."""
    try:
        out = docker.docker_output([
            "volume", "ls",
            "--filter", f"label={_VOLUME_OWNER_LABEL}={ws.name}",
            "--format", "{{.Name}}",
        ])
    except DockerError:
        return []
    return [n for n in out.splitlines() if n]


def delete_workspace(ws: Workspace, keep_volumes: bool = False,
                     *, containers: bool = True) -> None:
    """Remove both containers, the workspace's managed volumes (unless
    `keep_volumes`), the config file, and the state dir. Best-effort on the
    Docker objects (absent ones are fine).

    `containers=False` (an ATTACHED workspace) removes only the config file + state
    dir: an attached workspace owns no containers or volumes -- they are managed
    externally -- so there is nothing to `docker rm`/`volume rm`."""
    import shutil

    with ws.lock():
        if containers:
            docker.docker_quiet(["rm", "-f", ws.ws_container])
            docker.docker_quiet(["rm", "-f", ws.proxy_container])
            if not keep_volumes:
                for vol in _workspace_volumes(ws):
                    docker.docker_quiet(["volume", "rm", vol])
        # Remove config file
        if ws.config_path.exists():
            ws.config_path.unlink()
        # Remove state dir (incl. the lock file we hold; our fd stays valid until
        # we release on context exit).
        shutil.rmtree(ws.state_dir, ignore_errors=True)


# ---- drift + inspect --------------------------------------------------------


@dataclass(frozen=True)
class BindingSummary:
    name: str
    injector: str
    provider: str
    secret: str | dict[str, str]
    hosts: tuple[str, ...]
    placeholder: str | None
    env: str | None


@dataclass(frozen=True)
class DriftItem:
    """One detected difference between configured and applied state."""
    kind: str        # "container" or "bindings"
    item: str        # e.g. "image", "binding added: 'x'"
    applied: object  # value in applied state
    configured: object  # value in configured state


@dataclass(frozen=True)
class DriftReport:
    in_sync: bool
    changes: tuple[DriftItem, ...]


@dataclass(frozen=True)
class WorkspaceInspect:
    """A point-in-time view of a workspace: its config path, parsed config,
    container statuses, resolved host port (if running), binding summary,
    and a drift report comparing configured vs applied state."""

    name: str
    config_path: str
    config: dict          # normalized container-side settings (load_config)
    proxy_status: str | None    # docker status or None if absent
    ws_status: str | None
    running: bool
    host_port: int | None       # resolved proxy host port when running
    bindings: tuple[BindingSummary, ...]
    drift: DriftReport
    rules: tuple = ()           # tuple of core.rules.Rule (traffic governance)
    # Attached workspaces: the `attach` selector and, best-effort, the admin URL
    # it currently resolves to (None if unresolvable / docker absent). Both None
    # for a managed workspace.
    attach: dict | None = None
    attach_target: str | None = None
    # Live drift against the RUNNING proxy (what it is actually holding), or None
    # when the proxy is unreachable (the offline lock/applied report stands alone).
    live: "LiveDrift | None" = None


@dataclass(frozen=True)
class LiveDrift:
    """Verdict of the resolved intent against what the proxy is ACTUALLY running,
    read from GET /admin/config.

    ONE verdict, discriminated by two signals -- NEITHER of which is the lossy
    live projection:

      - "reality-drift" -- `applied.config_generation` is None, or the proxy's
                           generation != it. The proxy is NOT holding the push we
                           recorded (a lost tmpfs, a stateless `push --admin`, or a
                           foreign push). Takes PRECEDENCE over content.
      - "config-drift"  -- generation matches AND the OFFLINE, content-complete
                           drift (`_compute_drift`, which sees secret/provider/
                           params and every rule detail) is non-empty: the TOML
                           moved ahead of our push.
      - "in-sync"       -- generation matches AND no offline content drift.

    Correctness NEVER reads `projection` -- the sanitized GET body's LOSSY
    {name,hosts,scheme,placeholder,env} / {name,hosts,action,visible} lists. That
    view omits the secret ref, provider, injector params, and a rule's methods/
    path/status/body/headers/params, so a change to any of those is INVISIBLE in
    it (a `secret = "OLD" -> "NEW"` swap, a tightened `block` path). `projection`
    is carried for DISPLAY only (`inspect` shows what the proxy is running); the
    verdict comes from the generation + the offline content drift.

    Caveat: the generation discriminator can misclassify the rare case where a
    foreign push happens to land the SAME generation number the proxy would have
    reached -- it reads as config-drift (or in-sync) instead of reality-drift.
    Harmless: `apply` pushes on config-drift too, so a real divergence with any
    content difference is still corrected.

    `generation`/`applied_generation` carry the raw counters so the renderer can
    show the "(gen N vs M)" evidence behind a reality-drift verdict."""
    verdict: str                     # "reality-drift" | "config-drift" | "in-sync"
    generation: int | None           # live proxy config generation (None if absent)
    applied_generation: int | None   # lock's applied.config_generation
    projection: dict | None = None   # DISPLAY-only lossy live view {bindings, rules}

    @property
    def in_sync(self) -> bool:
        return self.verdict == "in-sync"


def _compute_drift(
    ws: Workspace,
    cfg: dict,
    current_bindings: list,   # list of BindingSummary-like (name, injector, provider, secret, hosts, placeholder, env)
    running: bool,
    current_rules: list | None = None,   # list of core.rules.Rule
) -> DriftReport:
    """Compare current config against the last applied spec + bindings + rules.

    All three baselines come from the lock's `applied` section (#65 folded the
    old applied-*.json files into it): `applied.spec`, `applied.bindings`,
    `applied.rules`. A missing lock/section reads as "nothing applied".
    Returns a DriftReport with all detected changes."""
    changes: list[DriftItem] = []

    applied = _load_applied(ws)
    applied_spec = applied.get("spec") if isinstance(applied.get("spec"), dict) else None
    applied_bindings = applied.get("bindings")
    if not isinstance(applied_bindings, list):
        applied_bindings = None

    # ---- container-spec drift ----
    if applied_spec is None:
        # No applied.spec record. NOT running -> just "never
        # started", genuinely no drift. Running -> we can't confirm the container
        # matches config, so surface it as UNKNOWN rather than silently "in sync"
        # (a restart re-records the spec).
        if running:
            changes.append(DriftItem(
                kind="container", item="spec state unknown",
                applied=None, configured=None,
            ))
    else:
        # Compare fields that feed the spec hash. (`home` is folded into mounts.)
        for field in ("image", "env", "setup"):
            configured_val = cfg[field]
            applied_val = applied_spec.get(field)
            if configured_val != applied_val:
                changes.append(DriftItem(
                    kind="container",
                    item=field,
                    applied=applied_val,
                    configured=configured_val,
                ))
        # mounts: compare list of dicts
        configured_mounts = cfg["mounts"]
        applied_mounts = applied_spec.get("mounts") or []
        if configured_mounts != applied_mounts:
            changes.append(DriftItem(
                kind="container",
                item="mounts",
                applied=applied_mounts,
                configured=configured_mounts,
            ))
        # run_flags: list of strings (missing in pre-run_flags specs -> [])
        configured_run_flags = cfg.get("run_flags") or []
        applied_run_flags = applied_spec.get("run_flags") or []
        if configured_run_flags != applied_run_flags:
            changes.append(DriftItem(
                kind="container",
                item="run_flags",
                applied=applied_run_flags,
                configured=configured_run_flags,
            ))
        # map_host_user: bool (missing in older specs -> False)
        configured_map = bool(cfg.get("map_host_user"))
        applied_map = bool(applied_spec.get("map_host_user"))
        if configured_map != applied_map:
            changes.append(DriftItem(
                kind="container",
                item="map_host_user",
                applied=applied_map,
                configured=configured_map,
            ))
        # user_uid: optional int (missing in older specs -> None)
        if cfg.get("user_uid") != applied_spec.get("user_uid"):
            changes.append(DriftItem(
                kind="container",
                item="user_uid",
                applied=applied_spec.get("user_uid"),
                configured=cfg.get("user_uid"),
            ))

    # ---- bindings drift ----
    if applied_bindings is None:
        # No applied.bindings record. Running WITH configured
        # bindings -> we can't confirm the proxy holds them, so treat as drift so
        # apply re-pushes (never silently assume "in sync"). Not running, or no
        # configured bindings, is genuinely no drift.
        if running and current_bindings:
            changes.append(DriftItem(
                kind="bindings", item="state unknown (re-push)",
                applied=None, configured=None,
            ))
    else:
        # Build lookup dicts keyed by binding name.
        applied_by_name = {b["name"]: b for b in applied_bindings}
        configured_by_name = {b.name: b for b in current_bindings}

        # Bindings added in config but not in applied.
        for name in configured_by_name:
            if name not in applied_by_name:
                changes.append(DriftItem(
                    kind="bindings",
                    item=f"binding added: '{name}'",
                    applied=None,
                    configured=_binding_summary_dict(configured_by_name[name]),
                ))

        # Bindings removed from config but still in applied.
        for name in applied_by_name:
            if name not in configured_by_name:
                changes.append(DriftItem(
                    kind="bindings",
                    item=f"binding removed: '{name}'",
                    applied=applied_by_name[name],
                    configured=None,
                ))

        # Bindings present in both: check for changes.
        for name in configured_by_name:
            if name not in applied_by_name:
                continue
            cb = configured_by_name[name]
            ab = applied_by_name[name]
            diffs = []
            for field in ("injector", "provider", "secret", "placeholder", "env"):
                cv = getattr(cb, field, None) if hasattr(cb, field) else cb.get(field)
                av = ab.get(field)
                if cv != av:
                    diffs.append(f"{field}: {av!r} -> {cv!r}")
            # hosts: compare as sorted lists for order-independent comparison
            cb_hosts = sorted(cb.hosts) if hasattr(cb, "hosts") else sorted(cb.get("hosts", []))
            ab_hosts = sorted(ab.get("hosts", []))
            if cb_hosts != ab_hosts:
                diffs.append(f"hosts: {ab_hosts!r} -> {cb_hosts!r}")
            if diffs:
                changes.append(DriftItem(
                    kind="bindings",
                    item=f"binding changed: '{name}' ({', '.join(diffs)})",
                    applied=ab,
                    configured=_binding_summary_dict(cb),
                ))

    # ---- rules drift ----
    if current_rules is not None:
        changes.extend(_rules_drift(ws, current_rules, running))

    return DriftReport(in_sync=len(changes) == 0, changes=tuple(changes))


def _rules_drift(ws: Workspace, current_rules: list, running: bool) -> list:
    """Rule drift against `applied.rules`, keyed by name. inspect/apply parse
    rules WITHOUT validate() (so they don't crash on a config the push path would
    reject), so validate() HERE and surface any config error -- a duplicate name,
    a bad host/path glob, an unresolved script -- as a single drift item, rather
    than silently mis-computing drift (e.g. a name-keyed dict collapsing two
    duplicate-named rules). Keeps inspect from being more lenient than push."""
    from ..errors import ConfigError, CredproxyError
    from ..model.rules import rule_wire_entries, validate

    out: list[DriftItem] = []
    try:
        validate(current_rules, str(ws.config_path))
        current = {e["name"]: e for e in rule_wire_entries(current_rules)}
    except (ConfigError, CredproxyError) as e:
        return [DriftItem(kind="rules", item=f"rules invalid ({e})",
                          applied=None, configured=None)]

    applied = _load_applied_rules(ws)
    if applied is None:
        if running and current:
            out.append(DriftItem(kind="rules", item="state unknown (re-push)",
                                 applied=None, configured=None))
        return out
    applied_by_name = {e["name"]: e for e in applied}
    for name, entry in current.items():
        if name not in applied_by_name:
            out.append(DriftItem(kind="rules", item=f"rule added: '{name}'",
                                 applied=None, configured=entry))
        elif entry != applied_by_name[name]:
            out.append(DriftItem(kind="rules", item=f"rule changed: '{name}'",
                                 applied=applied_by_name[name], configured=entry))
    for name in applied_by_name:
        if name not in current:
            out.append(DriftItem(kind="rules", item=f"rule removed: '{name}'",
                                 applied=applied_by_name[name], configured=None))
    # Reorder-only change: same rules, different DECLARATION ORDER. Rules
    # evaluate in order (first-terminal-wins), so this is a behavioral change
    # even though every rule is individually unchanged -- surface it so `apply`
    # re-pushes (the name-keyed comparison above would otherwise miss it).
    cur_order, app_order = list(current), [e["name"] for e in applied]
    if cur_order != app_order and set(cur_order) == set(app_order):
        out.append(DriftItem(kind="rules", item="rules reordered",
                             applied=app_order, configured=cur_order))
    return out


def _binding_summary_dict(b) -> dict:
    """Convert a BindingSummary (or dict) to a plain dict for DriftItem."""
    if isinstance(b, dict):
        return b
    return {
        "name": b.name,
        "injector": b.injector,
        "provider": b.provider,
        "secret": b.secret,
        "hosts": list(b.hosts),
        "placeholder": b.placeholder,
        "env": b.env,
    }


def _live_drift(ws: Workspace, admin_url: str, *,
                has_content_drift: bool) -> "LiveDrift | None":
    """Verdict of the resolved intent against what the proxy at `admin_url` is
    actually running (GET /admin/config). Returns None when the proxy is
    unreachable / doesn't answer 200 (or the token is unreadable) -- the caller
    treats None as "live unavailable" and the offline drift report stands alone.

    The verdict is generation-then-offline-content (see LiveDrift): the generation
    from the last recorded push (`applied.config_generation`) is the reality
    discriminator, and `has_content_drift` (the caller's OFFLINE, content-complete
    `_compute_drift` signal) is the config discriminator. The lossy live projection
    the GET body carries is stored for DISPLAY only -- never for the verdict."""
    from ..model.workspace import read_token

    try:
        token = read_token(ws)
    except CredproxyError:
        return None
    live = get_config(admin_url, token)
    if live is None:
        return None

    applied_gen = _load_applied(ws).get("config_generation")
    live_gen = live.get("generation")

    if applied_gen is None or live_gen != applied_gen:
        verdict = "reality-drift"   # proxy is not holding the push we recorded
    elif has_content_drift:
        verdict = "config-drift"    # TOML moved ahead of our push
    else:
        verdict = "in-sync"

    return LiveDrift(
        verdict=verdict,
        generation=live_gen,
        applied_generation=applied_gen,
        projection={"bindings": live.get("bindings"), "rules": live.get("rules")},
    )


def inspect_workspace(ws: Workspace) -> WorkspaceInspect:
    """Gather config + running state + a binding summary + drift report for `ws`.

    Reads through `resolve_workspace` (placeholders bound from the lock) WITHOUT
    fetching secrets or persisting the lock, so inspect is side-effect-free."""
    from ..model.resolver import resolve_workspace

    if not ws.exists():
        raise WorkspaceError(f"workspace '{ws.name}' not found")

    # `resolved.config` folds in any `[[preset]]` container half (config-v2), so
    # drift compares the effective set (binds existence-checked like `start`).
    resolved = resolve_workspace(ws, check_bind_exists=True)
    cfg = resolved.config

    if cfg.get("attach") is not None:
        return _inspect_attached(ws, cfg)

    proxy_status = docker.container_status(ws.proxy_container)
    ws_status = docker.container_status(ws.ws_container)
    running = ws_status == "running"

    host_port: int | None = None
    if proxy_status == "running":
        try:
            meta = ImageEnv.load()
            host_port = docker.resolve_host_port(ws.proxy_container, meta.http_port)
        except (ImageError, DockerError):
            host_port = None

    bindings = tuple(
        BindingSummary(
            name=b.name,
            injector=b.injector,
            provider=b.provider,
            secret=b.secret,
            hosts=b.hosts,
            placeholder=b.placeholder,
            env=b.env,
        )
        for b in resolved.bindings
    )

    current_rules = resolved.rules

    drift = _compute_drift(ws, cfg, bindings, running, current_rules=current_rules)

    # Live drift: when the proxy is reachable (its port resolved), compare the
    # resolved intent against what it is actually running -- surfacing a proxy that
    # lost its tmpfs on restart (which the offline applied cache can't detect).
    live = None
    if host_port is not None:
        content_drift = any(c.kind in ("bindings", "rules") for c in drift.changes)
        live = _live_drift(ws, f"http://127.0.0.1:{host_port}",
                           has_content_drift=content_drift)

    return WorkspaceInspect(
        name=ws.name,
        config_path=str(ws.config_path),
        config=cfg,
        proxy_status=proxy_status,
        ws_status=ws_status,
        running=running,
        host_port=host_port,
        bindings=bindings,
        rules=tuple(current_rules),
        drift=drift,
        live=live,
    )


def _inspect_attached(ws: Workspace, cfg: dict) -> WorkspaceInspect:
    """Inspect an attached workspace: the attach selector + the admin URL it
    currently resolves to (best-effort, tolerating docker absence), plus the
    binding/rule summary and drift against applied state -- but no container
    status (the containers are managed externally)."""
    from ..model.resolver import resolve_workspace

    attach = cfg["attach"]
    attach_target: str | None = None
    try:
        attach_target = resolve_admin_url(ws)
    except CredproxyError:
        attach_target = None

    resolved = resolve_workspace(ws)
    bindings = tuple(
        BindingSummary(name=b.name, injector=b.injector, provider=b.provider,
                       secret=b.secret, hosts=b.hosts, placeholder=b.placeholder,
                       env=b.env)
        for b in resolved.bindings
    )
    current_rules = resolved.rules
    # No managed container, so "running" is not meaningful; drift is computed
    # against applied.bindings/.rules regardless (added/removed/changed).
    drift = _compute_drift(ws, cfg, bindings, running=False,
                           current_rules=current_rules)
    # Live drift rides the SAME resolved attach admin URL push uses (loopback
    # enforced there), so an attached workspace gets reality-drift detection too.
    live = None
    if attach_target is not None:
        content_drift = any(c.kind in ("bindings", "rules") for c in drift.changes)
        live = _live_drift(ws, attach_target, has_content_drift=content_drift)
    return WorkspaceInspect(
        name=ws.name, config_path=str(ws.config_path), config=cfg,
        proxy_status=None, ws_status=None, running=False, host_port=None,
        bindings=bindings, rules=tuple(current_rules), drift=drift,
        attach=attach, attach_target=attach_target, live=live,
    )
