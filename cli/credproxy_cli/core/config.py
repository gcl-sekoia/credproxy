"""Workspace config: load/validate <name>.toml, resolve ${secret:} refs,
and compute the workspace launch-spec hash.

Config is stored at $XDG_CONFIG_HOME/credproxy/workspaces/<name>.toml and
parsed with stdlib `tomllib` (Python 3.11+). No external dependencies.

Schema:
  image  = "mcr.microsoft.com/devcontainers/base:ubuntu"  # str, optional (default)
  home   = "/root"                     # str, optional (default applied)
  mounts = ["~/src:/src"]              # list[str] "SRC:DST" or "SRC:DST:ro"
  env    = { KEY = "value" }           # table, optional; passed as -e to ws
  setup  = ["npm ci"]                  # list[str], optional; run once on create
  run_flags = ["--userns=keep-id"]     # list[str], optional; spliced into docker run
  map_host_user = true                 # bool, optional; non-root `user` owns mounts
  user_uid = 1000                      # int, optional; in-container uid of `user`
  user   = "dev"                       # str, optional; user `enter` execs as
  shell  = ["zsh"]                     # list[str], optional; default `enter` command
  workdir = "/code"                    # str, optional; dir `enter` starts in
  enter_prelude = "..."                # str, optional; shell run before exec on enter

  [[binding]]                          # zero or more; see core/bindings.py
  injector = "bearer"
  provider = "env"
  secret   = "GITHUB_TOKEN"
  hosts    = ["api.github.com"]

The `[[binding]]` array is parsed/validated/materialized by core/bindings.py,
not here -- load_config only handles the container-side settings.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path

from .errors import ConfigError
from .paths import atomic_write_text, overlay_dirs, resolve_singleton
from .workspace import Workspace

import tomllib

# A managed-volume name (the `volume`/`home` mount source). Docker-volume-name
# safe; must start alnum.
_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Every top-level key a workspace TOML may carry: the container-side settings
# load_config parses, `auto_stop` (host-side session behavior, read fresh by
# lifecycle._maybe_auto_stop), and the two array-of-tables handled by their own
# modules (`binding` -> core/bindings.py, `rule` -> core/rules.py). load_config
# rejects anything else so a typo (`mount` for `mounts`, `setup_cmd`, `user_id`)
# is a hard error, not a silent no-op -- the TOML is the single source of truth,
# so a misspelled key that parses fine and does nothing is a real footgun.
KNOWN_KEYS = frozenset({
    "image", "home", "directory", "mounts", "env", "setup", "user", "workdir",
    "enter_prelude", "shell", "exec_flags", "run_flags", "map_host_user",
    "user_uid", "auto_stop", "binding", "rule", "attach",
})

# The `attach` selector keys. Exactly one must be present. `compose_project` is
# sugar for a `discover` on the Compose project + service=proxy labels (the ONLY
# Compose-aware bit); it is normalized to `discover` at load.
_ATTACH_SELECTORS = ("admin_url", "container", "discover", "compose_project")

# Every container-lifecycle key an attached workspace may NOT carry -- credproxy
# manages only credentials/config for an attached workspace; its containers are
# run externally (Compose/devcontainers/CI). `directory` (host-side cwd
# resolution) and `[[binding]]`/`[[rule]]` stay valid.
_ATTACH_EXCLUSIVE = frozenset({
    "image", "home", "mounts", "env", "setup", "user", "user_uid",
    "map_host_user", "run_flags", "shell", "workdir", "enter_prelude",
    "exec_flags", "auto_stop",
})


def _parse_attach(raw_attach, source: str) -> dict:
    """Validate + normalize an `attach` table into exactly one selector:
    `{"admin_url": U}` | `{"container": X}` | `{"discover": SPEC}`. Enforces one
    selector, non-empty string values, `discover`/`compose_project` shape, and the
    loopback-only rule on `admin_url` (I8). `compose_project = "P"` normalizes to
    `discover = "com.docker.compose.project=P,com.docker.compose.service=proxy"`."""
    if not isinstance(raw_attach, dict):
        raise ConfigError(f"{source}: `attach` must be a table")
    extra = sorted(set(raw_attach) - set(_ATTACH_SELECTORS))
    if extra:
        raise ConfigError(
            f"{source}: `attach` has unknown key(s): {', '.join(extra)} "
            f"(one of {', '.join(_ATTACH_SELECTORS)})")
    present = [k for k in _ATTACH_SELECTORS if k in raw_attach]
    if len(present) != 1:
        raise ConfigError(
            f"{source}: `attach` needs exactly one of "
            f"{', '.join(_ATTACH_SELECTORS)} (got {len(present)})")
    key = present[0]
    val = raw_attach[key]
    if not isinstance(val, str) or not val:
        raise ConfigError(f"{source}: `attach.{key}` must be a non-empty string")

    if key == "compose_project":
        return {"discover": f"com.docker.compose.project={val},"
                            f"com.docker.compose.service=proxy"}
    if key == "discover":
        from .push import parse_discover
        parse_discover(val)   # validates the k=v,k=v shape (raises ConfigError)
        return {"discover": val}
    if key == "admin_url":
        from .push import normalize_admin_url, require_loopback
        url = normalize_admin_url(val)
        require_loopback(url)
        return {"admin_url": url}
    return {"container": val}


def _attached_config(raw: dict, ws: Workspace) -> dict:
    """The normalized config for an attached workspace: the `attach` selector plus
    every container-lifecycle field defaulted to empty/None, so the shape matches
    the managed path (inspect/resolve read one dict). Rejects any lifecycle key
    (mutual exclusion) before validating the selector."""
    offending = sorted(k for k in raw if k in _ATTACH_EXCLUSIVE)
    if offending:
        raise ConfigError(
            f"{ws.config_path}: `attach` is mutually exclusive with "
            f"container-lifecycle fields -- remove: {', '.join(offending)} "
            f"(an attached workspace's container is managed externally; credproxy "
            f"manages only its credentials/config)")
    attach = _parse_attach(raw["attach"], str(ws.config_path))

    directory = raw.get("directory")
    if directory is not None and (not isinstance(directory, str)
                                  or not directory.startswith("/")):
        raise ConfigError(f"{ws.config_path}: `directory` must be an absolute path")

    return {
        "attach": attach, "directory": directory,
        "image": None, "home": None, "mounts": [], "env": {}, "setup": [],
        "user": None, "workdir": None, "enter_prelude": None, "shell": None,
        "exec_flags": [], "run_flags": [], "map_host_user": False,
        "user_uid": None, "auto_stop": False,
    }


def _bind_source(raw_src: str, where: str) -> str:
    """Resolve + validate a host-bind source: `~` expanded, absolute, exists."""
    src = Path(os.path.expanduser(raw_src))
    if not src.is_absolute():
        raise ConfigError(f"{where} source must be absolute (after ~): {raw_src!r}")
    if not src.exists():
        raise ConfigError(f"{where} source does not exist: {src}")
    return str(src)


def _overlay_source(rel: str, where: str) -> str:
    """Resolve an overlay-relative bind source across the configured overlays,
    searched in declared order (first overlay containing `rel` wins). Confined
    within the winning overlay (no `..`/symlink escape) and required to exist.
    The user tier and builtin do NOT participate -- mounts search the overlays
    only. The not-found error names every overlay searched."""
    searched: list[Path] = []
    for _label, base in overlay_dirs():
        base = base.resolve()
        searched.append(base)
        resolved = (base / rel).resolve()
        # `..`-escape is a property of `rel`, not of a given overlay, so reject
        # it outright rather than falling through to the next overlay.
        if resolved != base and base not in resolved.parents:
            raise ConfigError(f"{where} overlay path {rel!r} escapes the overlay dir")
        if resolved.exists():
            return str(resolved)
    roots = ", ".join(str(b) for b in searched) or "(no overlays configured)"
    raise ConfigError(
        f"{where} overlay source {rel!r} not found (searched: {roots})"
    )


def _parse_mount(m, where: str) -> dict:
    """One `mounts` entry -> a typed record
    `{kind: bind|volume|overlay, source|name, target, readonly}`.

    String form is a host bind (`"SRC:DST[:ro]"`). Table form has exactly one of
    `bind`/`volume`/`overlay`, plus an absolute `target` and optional `readonly`
    (overlay mounts default to read-only -- they ship static assets)."""
    if isinstance(m, str):
        parts = m.split(":")
        if len(parts) < 2 or len(parts) > 3 or (len(parts) == 3 and parts[2] != "ro"):
            raise ConfigError(f'{where}: expected "SRC:DST" or "SRC:DST:ro", got {m!r}')
        target = parts[1]
        if not target.startswith("/"):
            raise ConfigError(f"{where} target must be absolute: {target!r}")
        return {"kind": "bind", "source": _bind_source(parts[0], where),
                "target": target, "readonly": len(parts) == 3}

    if not isinstance(m, dict):
        raise ConfigError(f'{where} must be a string ("SRC:DST[:ro]") or a table')

    kinds = [k for k in ("bind", "volume", "overlay") if k in m]
    if len(kinds) != 1:
        raise ConfigError(f"{where} must have exactly one of bind/volume/overlay")
    kind = kinds[0]
    # `user_owned` is a managed-volume-only flag (chown the volume to the
    # workspace user); it is meaningless on a host bind (the user's own dir,
    # never chowned) or a read-only overlay mount, so it isn't accepted there.
    allowed = {kind, "target", "readonly"}
    if kind == "volume":
        allowed.add("user_owned")
    extra = set(m) - allowed
    if extra:
        raise ConfigError(f"{where} unknown key(s): {', '.join(sorted(extra))}")
    target = m.get("target")
    if not isinstance(target, str) or not target.startswith("/"):
        raise ConfigError(f"{where} target must be an absolute path")
    ro = m.get("readonly")
    if ro is not None and not isinstance(ro, bool):
        raise ConfigError(f"{where} readonly must be a boolean")
    val = m[kind]
    if not isinstance(val, str) or not val:
        raise ConfigError(f"{where} {kind} must be a non-empty string")

    if kind == "bind":
        return {"kind": "bind", "source": _bind_source(val, where),
                "target": target, "readonly": bool(ro)}
    if kind == "volume":
        if not _VOLUME_NAME_RE.match(val):
            raise ConfigError(f"{where} volume name {val!r} is invalid "
                              f"(letters/digits/_.-, starting alnum)")
        uo = m.get("user_owned")
        if uo is not None and not isinstance(uo, bool):
            raise ConfigError(f"{where} user_owned must be a boolean")
        out = {"kind": "volume", "name": val, "target": target,
               "readonly": bool(ro)}
        # Omit when false so the common (unset) case leaves the normalized mount
        # dict -- and thus the spec hash -- byte-identical to before the flag.
        if uo:
            out["user_owned"] = True
        return out
    return {"kind": "overlay", "source": _overlay_source(val, where),
            "target": target, "readonly": True if ro is None else bool(ro)}



def load_config(ws: Workspace) -> dict:
    """Parse and validate the container-side settings of <name>.toml into a
    normalized dict: {image, home, directory, mounts: [{source, target, readonly}],
    env: {}, setup: []}. The `[[binding]]` array is handled separately by
    core/bindings.py."""
    if not ws.exists():
        raise ConfigError(
            f"workspace '{ws.name}' not found (no {ws.config_path})"
        )
    try:
        raw = tomllib.loads(ws.config_path.read_text())
    except Exception as e:
        raise ConfigError(f"{ws.config_path}: TOML parse error: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"{ws.config_path}: top level must be a table")

    # Reject unknown top-level keys (a typo silently no-ops otherwise). Mirror the
    # `_parse_mount` "unknown key(s)" precedent, with a cheap did-you-mean.
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        def _hint(k: str) -> str:
            near = difflib.get_close_matches(k, KNOWN_KEYS, n=1)
            return f"`{k}` (did you mean `{near[0]}`?)" if near else f"`{k}`"
        raise ConfigError(
            f"{ws.config_path}: unknown key(s): {', '.join(_hint(k) for k in unknown)}"
        )

    # Attached workspace: credproxy manages only credentials/config; the
    # container is run externally. Mutually exclusive with every lifecycle field
    # (checked in _attached_config), so it takes a distinct, container-free path.
    if raw.get("attach") is not None:
        return _attached_config(raw, ws)

    # image (mandatory -- the scaffold writes a concrete one; there is no
    # built-in default image to fall back to).
    image = raw.get("image")
    if not isinstance(image, str) or not image:
        raise ConfigError(
            f"{ws.config_path}: `image` is required (a non-empty string) -- "
            f"`credproxy workspace create` writes one for you"
        )

    # home: optional. Sugar for a managed volume named "home" mounted at this
    # path (image-seeded, persistent). Omit it -> no managed home volume; the
    # container's home is the image's, ephemeral (gone on recreate).
    home = raw.get("home")
    if home is not None and (not isinstance(home, str) or not home.startswith("/")):
        raise ConfigError(f"{ws.config_path}: `home` must be an absolute path")

    # directory: optional host path this workspace is "for". Pure resolution
    # metadata for the loose surface -- `credp <verb>` with no NAME, run at or
    # under this path, resolves to this workspace (see core/dirmatch.py). The
    # name stays canonical; this is just another resolver, like the default
    # pointer. Host-side only: never touches the container, so NOT part of the
    # spec hash.
    directory = raw.get("directory")
    if directory is not None and (not isinstance(directory, str)
                                  or not directory.startswith("/")):
        raise ConfigError(f"{ws.config_path}: `directory` must be an absolute path")

    # mounts: typed list. A string is a host bind ("SRC:DST[:ro]"); a table is a
    # bind/volume/overlay mount. The `home` sugar prepends the home volume so it
    # shares the uniqueness checks + emission path.
    raw_mounts = raw.get("mounts") or []
    if not isinstance(raw_mounts, list):
        raise ConfigError(f"{ws.config_path}: `mounts` must be an array")
    mounts = [_parse_mount(m, f"{ws.config_path}: mounts[{i}]")
              for i, m in enumerate(raw_mounts)]
    if home:
        mounts.insert(0, {"kind": "volume", "name": "home", "target": home,
                          "readonly": False})

    # No two mounts on the same target; no two volumes with the same name.
    seen_targets: set[str] = set()
    seen_vols: set[str] = set()
    for m in mounts:
        t = m["target"].rstrip("/") or "/"
        if t in seen_targets:
            raise ConfigError(f"{ws.config_path}: two mounts target {m['target']!r}")
        seen_targets.add(t)
        if m["kind"] == "volume":
            if m["name"] in seen_vols:
                raise ConfigError(
                    f"{ws.config_path}: two volumes named {m['name']!r} "
                    f"('home' names the home volume)"
                )
            seen_vols.add(m["name"])

    # env: inline table of string values
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ConfigError(f"{ws.config_path}: `env` must be a table")
    for k, v in env.items():
        if not isinstance(k, str):
            raise ConfigError(f"{ws.config_path}: `env` keys must be strings")
        if not isinstance(v, str):
            raise ConfigError(
                f"{ws.config_path}: env.{k} must be a string, got {type(v).__name__}"
            )

    # setup: list of shell command strings
    setup = raw.get("setup") or []
    if not isinstance(setup, list):
        raise ConfigError(f"{ws.config_path}: `setup` must be an array")
    for i, cmd in enumerate(setup):
        if not isinstance(cmd, str):
            raise ConfigError(
                f"{ws.config_path}: setup[{i}] must be a string"
            )

    # user: optional user that `enter` execs as (docker exec -u). Exec-only, so
    # NOT part of the spec hash -- changing it never recreates the container; it
    # takes effect on the next `enter`. The user must exist in the image (built
    # in or created by `setup`, which always runs as root).
    user = raw.get("user")
    if user is not None and (not isinstance(user, str) or not user):
        raise ConfigError(f"{ws.config_path}: `user` must be a non-empty string")

    # `user_owned` volumes are chowned to `user`, so they need a non-root one --
    # otherwise the flag is a silent no-op (root already owns everything).
    if any(m["kind"] == "volume" and m.get("user_owned") for m in mounts) and (
            not user or user.split(":", 1)[0] in ("root", "0")):
        raise ConfigError(
            f"{ws.config_path}: a `user_owned` volume requires a non-root `user` "
            f"(the volume is chowned to it)"
        )

    # exec_flags: escape hatch -- extra flags spliced into `docker exec` for
    # `enter` (e.g. ["--workdir", "/srv"], ["--env", "FOO=bar"]). credproxy keeps
    # ownership of the session-control flags (-i/-t/-d), so these can't break
    # session tracking. Exec-only, like `user`; not part of the spec.
    exec_flags = raw.get("exec_flags") or []
    if not isinstance(exec_flags, list) or not all(isinstance(f, str) for f in exec_flags):
        raise ConfigError(f"{ws.config_path}: `exec_flags` must be an array of strings")

    # workdir: directory `enter` starts in (docker exec --workdir), defaulting to
    # `home` at exec time. The workspaceFolder analog -- so `enter` lands in your
    # project (or home) rather than the image's WORKDIR. Exec-only (it's where
    # the exec starts, not a container change), so NOT part of the spec hash; a
    # --workdir in `exec_flags` still overrides it (docker last-wins).
    workdir = raw.get("workdir")
    if workdir is not None and (not isinstance(workdir, str) or not workdir.startswith("/")):
        raise ConfigError(f"{ws.config_path}: `workdir` must be an absolute path")

    # enter_prelude: escape hatch over the `enter` env shim. By default credproxy
    # wraps the enter command in `sh -c '<prelude>; exec "$@"'`, where the prelude
    # sources the proxy's CA-env file -- so the env reaches an interactive shell,
    # `enter -- cmd`, and subprocesses alike (docker exec is a bare execve). This
    # overrides that snippet; set it to "" to skip wrapping (direct execve).
    # Exec-only -> not part of the spec hash.
    enter_prelude = raw.get("enter_prelude")
    if enter_prelude is not None and not isinstance(enter_prelude, str):
        raise ConfigError(f"{ws.config_path}: `enter_prelude` must be a string")

    # shell: the command `enter` runs when no `-- CMD` is given (argv list).
    # Defaults to a LOGIN shell (`["bash", "-l"]`) -- semantically `enter` is
    # "log into the workspace" (the ssh model), so the interactive entry sources
    # the full login environment; `enter -- CMD` stays a bare, non-login command
    # (the ssh `host cmd` model). Exec-only -> not part of the spec hash.
    shell = raw.get("shell")
    if shell is not None and (
        not isinstance(shell, list) or not shell
        or not all(isinstance(s, str) and s for s in shell)
    ):
        raise ConfigError(
            f"{ws.config_path}: `shell` must be a non-empty array of non-empty strings"
        )

    # run_flags: escape hatch -- extra flags spliced into the workspace
    # `docker run` (e.g. ["--userns=keep-id:uid=1000,gid=1000"] for rootless
    # podman, or a custom idmapped mount). Unlike `exec_flags`, these shape the
    # container itself, so they ARE part of the spec hash: changing them
    # recreates the container on the next `start`. credproxy's structural flags
    # (--name, labels, --network, the home volume) are applied AFTER these and
    # win on conflict, so run_flags can't detach the netns or rename the box.
    run_flags = raw.get("run_flags") or []
    if not isinstance(run_flags, list) or not all(isinstance(f, str) for f in run_flags):
        raise ConfigError(f"{ws.config_path}: `run_flags` must be an array of strings")

    # map_host_user: let credproxy make the non-root `user` own the bind mounts
    # without changing host ownership, picking the runtime-appropriate lever
    # (--userns=keep-id on rootless podman; a no-op on Docker, where the matching
    # uid via CREDPROXY_HOST_UID handles it). Shapes the container -> part of the
    # spec hash. Requires a non-root `user` (validated below).
    map_host_user = raw.get("map_host_user", False)
    if not isinstance(map_host_user, bool):
        raise ConfigError(f"{ws.config_path}: `map_host_user` must be a boolean")

    # auto_stop: host-side session behavior (stop the workspace when the last
    # `enter` session exits). Strict bool -- `auto_stop = "false"` is a truthy
    # STRING that would silently ENABLE auto-stop, the exact trap this rejects.
    # NOT part of the spec hash (host-side only, never touches the container).
    # lifecycle._maybe_auto_stop re-reads it fresh (mid-session edits are
    # intentional) but with the same `is True` strictness.
    auto_stop = raw.get("auto_stop", False)
    if not isinstance(auto_stop, bool):
        raise ConfigError(f"{ws.config_path}: `auto_stop` must be a boolean")

    # user_uid: the in-container uid of `user`. map_host_user's keep-id maps
    # host-you onto THIS uid, so it's the side the host must land on for `user`
    # to own the bind mounts (host uid and this need not be equal). Defaults to
    # the host uid (correct for a `setup`-provisioned user made as
    # $CREDPROXY_HOST_UID); set it to a baked user's uid (the default image's
    # `vscode` is 1000). Shapes the container -> part of the spec hash.
    user_uid = raw.get("user_uid")
    if user_uid is not None and (not isinstance(user_uid, int) or isinstance(user_uid, bool)
                                 or user_uid < 0):
        raise ConfigError(f"{ws.config_path}: `user_uid` must be a non-negative integer")

    # map_host_user / user_uid configure how the non-root `user` owns bind
    # mounts, so they're meaningless without one. Reject rather than silently
    # no-op -- a uid (or mapping toggle) for a non-existent user is a config
    # error, not an in-progress state worth tolerating.
    if user is None:
        orphans = [name for name, present in
                   (("map_host_user", map_host_user), ("user_uid", user_uid is not None))
                   if present]
        if orphans:
            joined = " and ".join(f"`{o}`" for o in orphans)
            verb, subj = ("require", "they") if len(orphans) > 1 else ("requires", "it")
            raise ConfigError(
                f"{ws.config_path}: {joined} {verb} `user` to be set "
                f"({subj} configure{'' if len(orphans) > 1 else 's'} how the "
                f"non-root `user` owns bind mounts)"
            )

    return {
        "attach": None,
        "image": image,
        "home": home,
        "directory": directory,
        "mounts": mounts,
        "env": env,
        "setup": setup,
        "user": user,
        "workdir": workdir,
        "enter_prelude": enter_prelude,
        "shell": shell,
        "exec_flags": exec_flags,
        "run_flags": run_flags,
        "map_host_user": map_host_user,
        "user_uid": user_uid,
        "auto_stop": auto_stop,
    }


def declared_config(ws: Workspace) -> dict:
    """The container-side settings literally present in the TOML, before any
    defaults are applied -- the raw declaration, for `config --declared`.
    Excludes the `[[binding]]` array (shown by `binding list`). Raises
    ConfigError on a missing file or parse error."""
    if not ws.exists():
        raise ConfigError(f"workspace '{ws.name}' not found (no {ws.config_path})")
    try:
        raw = tomllib.loads(ws.config_path.read_text())
    except Exception as e:
        raise ConfigError(f"{ws.config_path}: TOML parse error: {e}") from e
    return {k: v for k, v in raw.items() if k != "binding"}


def quick_image(ws: Workspace) -> str:
    """Best-effort `image` read for `list`, without full validation."""
    try:
        raw = tomllib.loads(ws.config_path.read_text())
        return raw.get("image") or "?"
    except Exception:
        return "?"


def quick_attach(ws: Workspace) -> bool:
    """Best-effort "is this an attached workspace?" for verb gating, tolerant of
    an otherwise-invalid config (a half-edited peer must not break the gate).
    True iff a top-level `attach` table is present."""
    try:
        raw = tomllib.loads(ws.config_path.read_text())
        return raw.get("attach") is not None
    except Exception:
        return False


def quick_directory(ws: Workspace) -> str | None:
    """Best-effort `directory` read for cwd-resolution and `list`, tolerant of
    an otherwise-invalid config (a half-edited peer workspace must not break
    resolution for the rest). Returns None when absent or unreadable."""
    try:
        raw = tomllib.loads(ws.config_path.read_text())
        d = raw.get("directory")
        return d if isinstance(d, str) and d else None
    except Exception:
        return None


def set_top_level_key(text: str, key: str, value: str) -> str:
    """Insert or replace a top-level `key = "value"` string assignment (see
    set_top_level_raw); `value` is TOML-string-escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return set_top_level_raw(text, key, f'"{escaped}"')


def set_top_level_raw(text: str, key: str, raw_value: str) -> str:
    """Insert or replace a top-level `key = <raw_value>` assignment in a TOML
    document, preserving comments and ordering. `raw_value` is emitted verbatim
    (already-rendered TOML -- e.g. an inline table `{ container = "x" }`). Top-level
    keys must precede the first table header, so a new key is inserted before the
    first `[...]`/`[[...]]` line (or appended if there is none); an existing
    top-level assignment is replaced in place. A surgical text edit -- the TOML
    file stays the single source of truth."""
    assignment = f'{key} = {raw_value}'
    lines = text.splitlines(keepends=True)
    header_re = re.compile(r"\s*\[")
    key_re = re.compile(rf"\s*{re.escape(key)}\s*=")
    first_header = next((i for i, ln in enumerate(lines) if header_re.match(ln)),
                        len(lines))
    # Replace an existing top-level assignment (before the first table header).
    for i in range(first_header):
        if key_re.match(lines[i]):
            lines[i] = assignment + ("\n" if lines[i].endswith("\n") else "")
            return "".join(lines)
    # Otherwise insert before the first header (or append at end).
    if first_header > 0 and not lines[first_header - 1].endswith("\n"):
        lines[first_header - 1] += "\n"
    lines.insert(first_header, assignment + "\n")
    return "".join(lines)


def associate_directory(ws: Workspace, directory: str) -> None:
    """Persist the workspace's `directory` field (insert or replace) via an
    atomic write that preserves comments and ordering."""
    new = set_top_level_key(ws.config_path.read_text(), "directory", directory)
    atomic_write_text(ws.config_path, new)


def _toml_basic_str(s: str) -> str:
    """A TOML basic (double-quoted) string literal for `s`."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_volume_mount_inline(name: str, target: str, readonly: bool,
                                user_owned: bool = False) -> str:
    """An inline-table mount entry, e.g. `{ volume = "cache", target = "/c" }`."""
    parts = [f"volume = {_toml_basic_str(name)}",
             f"target = {_toml_basic_str(target)}"]
    if readonly:
        parts.append("readonly = true")
    if user_owned:
        parts.append("user_owned = true")
    return "{ " + ", ".join(parts) + " }"


def _render_volume_mount_block(name: str, target: str, readonly: bool,
                               user_owned: bool = False) -> str:
    """A `[[mounts]]` array-of-tables block for a managed volume."""
    lines = ["[[mounts]]",
             f"volume = {_toml_basic_str(name)}",
             f"target = {_toml_basic_str(target)}"]
    if readonly:
        lines.append("readonly = true")
    if user_owned:
        lines.append("user_owned = true")
    return "\n".join(lines) + "\n"


def _inline_mounts_array_span(text: str) -> tuple[int, int] | None:
    """If the doc has an uncommented top-level `mounts = [ ... ]` inline array,
    return `(open_bracket_index, close_bracket_index)`; else None.

    The bracket scan skips string contents and `#` comments and balances nested
    `[]`, so a path containing a bracket or a multi-line array doesn't fool it.
    Returns None when `mounts` is absent, commented, declared as `[[mounts]]`
    blocks (a header, not a `mounts =` assignment), or not an inline array -- all
    cases where the caller appends a `[[mounts]]` block instead."""
    m = re.search(r"(?m)^[ \t]*mounts[ \t]*=[ \t]*", text)
    if not m:
        return None
    open_idx = m.end()
    if open_idx >= len(text) or text[open_idx] != "[":
        return None
    depth = 0
    in_str = False
    str_ch = ""
    j = open_idx
    while j < len(text):
        c = text[j]
        if in_str:
            if str_ch == '"' and c == "\\":
                j += 2
                continue
            if c == str_ch:
                in_str = False
        elif c in "\"'":
            in_str = True
            str_ch = c
        elif c == "#":
            nl = text.find("\n", j)
            if nl == -1:
                break
            j = nl
            continue
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return (open_idx, j)
        j += 1
    return None  # unbalanced -> let the caller append a block instead


def add_volume_mount(text: str, name: str, target: str,
                     readonly: bool = False, user_owned: bool = False) -> str:
    """Add a managed-volume mount to a workspace TOML, preserving comments.

    If an uncommented inline `mounts = [ ... ]` array exists, the entry is
    prepended inside it (prepending avoids trailing-comma/last-entry guesswork).
    Otherwise a `[[mounts]]` block is appended -- which also composes with
    existing `[[mounts]]` blocks (TOML merges them), but is never mixed with an
    inline array (TOML forbids a key and a table-array of the same name)."""
    span = _inline_mounts_array_span(text)
    if span is None:
        block = _render_volume_mount_block(name, target, readonly, user_owned)
        if text and not text.endswith("\n"):
            text += "\n"
        sep = "\n" if text and not text.endswith("\n\n") else ""
        return text + sep + block
    open_idx, close_idx = span
    entry = _render_volume_mount_inline(name, target, readonly, user_owned)
    inner = text[open_idx + 1:close_idx]
    # Strip comments to decide whether the array already has entries (a trailing
    # comma is then needed before the existing content).
    has_entries = bool(re.sub(r"(?m)#.*$", "", inner).strip())
    if has_entries:
        # Space after the comma only when the next entry follows on the same line
        # (an inline array); a multi-line array already has its own newline.
        sep = "" if inner[:1] in ("\n", " ", "\t", "\r") else " "
        insertion = f" {entry},{sep}"
    else:
        insertion = f" {entry}"
    return text[:open_idx + 1] + insertion + inner + text[close_idx:]


def write_added_mount(ws: Workspace, name: str, target: str,
                      readonly: bool, user_owned: bool = False) -> None:
    """Persist a new managed-volume mount into <name>.toml (atomic, surgical).

    A volume named `home` is the `home = "..."` sugar (a top-level key), not a
    `mounts` entry -- writing it as a mount would collide with the sugar's
    duplicate-volume check at load time. The sugar carries no flags, so a
    `home` volume can't be `user_owned` via this path -- use an explicit
    `[[mounts]]` table with `volume = "home"` for that."""
    text = ws.config_path.read_text()
    if name == "home":
        if user_owned:
            raise ConfigError(
                "the `home = \"...\"` sugar can't carry user_owned; declare home "
                "as a `[[mounts]]` table (volume = \"home\") to make it user-owned"
            )
        new = set_top_level_key(text, "home", target)
    else:
        new = add_volume_mount(text, name, target, readonly, user_owned)
    atomic_write_text(ws.config_path, new)


def workspace_spec_hash(cfg: dict, proxy_id: str | None) -> str:
    """Identity of the workspace container's launch spec. Changing the
    image, mounts (incl. the home volume), env, setup, run_flags, map_host_user,
    user_uid, or the proxy container (netns peer) yields a new hash, which
    `start` uses to decide whether to recreate."""
    spec = json.dumps(
        {
            "image": cfg["image"],
            "mounts": cfg["mounts"],
            "env": cfg["env"],
            "setup": cfg["setup"],
            "run_flags": cfg.get("run_flags") or [],
            "map_host_user": bool(cfg.get("map_host_user")),
            "user_uid": cfg.get("user_uid"),
            "proxy": proxy_id,
        },
        sort_keys=True,
    )
    return hashlib.sha256(spec.encode()).hexdigest()[:16]


def render_template(name: str) -> str:
    """Scaffold a workspace TOML from the resolved template (a user copy if
    present, else an overlay's, else the builtin default; resolve_singleton walks
    the same tiers as the registries). The template is a LITERAL workspace config
    -- every occurrence of the exact token `{name}` is replaced with the
    workspace name, and nothing else (no `str.format`, so literal braces need no
    doubling). To use a different image, edit the scaffolded file (the template's
    comments show what to adjust), or override the template in an overlay (see
    docs/advanced/overlays.md)."""
    path = resolve_singleton("workspace.template.toml")
    if path is None:
        raise ConfigError(
            "no workspace.template.toml found (looked in the user config, the "
            "overlays, and the builtin defaults)"
        )
    return path.read_text().replace("{name}", name)


def render_attach_inline(selector: dict) -> str:
    """Render a normalized attach selector (`_parse_attach`'s output) as an inline
    TOML table, e.g. `{ container = "foo" }`. The value is TOML-escaped."""
    (key, val), = selector.items()
    return f'{{ {key} = {_toml_basic_str(val)} }}'


def render_attach_template(name: str, selector: dict) -> str:
    """Scaffold an ATTACHED workspace TOML from the resolved
    `workspace.attach.template.toml` (user > overlay > builtin, same walk as the
    managed template), with `{name}` filled and the template's placeholder
    `attach = ...` line replaced by the chosen selector."""
    path = resolve_singleton("workspace.attach.template.toml")
    if path is None:
        raise ConfigError(
            "no workspace.attach.template.toml found (looked in the user config, "
            "the overlays, and the builtin defaults)"
        )
    text = path.read_text().replace("{name}", name)
    return set_top_level_raw(text, "attach", render_attach_inline(selector))
