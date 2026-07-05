"""Repo layout and CLI-only conventions.

These are names the CLI picks for itself (image tag, default workspace
image) -- they don't need to match anything in the proxy image. The
image's own API (ports, mount targets) is read separately from
`docker inspect` in imageenv.py.

Storage follows XDG:
  - Config:  $XDG_CONFIG_HOME/credproxy/   (default ~/.config/credproxy/)
  - State:   $XDG_STATE_HOME/credproxy/    (default ~/.local/state/credproxy/)

Use the XDG env vars to override (works for tests too).
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo layout: this package lives at <repo>/cli/credproxy_cli. The proxy
# source tree is needed only by the `dev` harness commands and for the
# dev-mode source bind-mount into the proxy container.
REPO_ROOT = Path(__file__).resolve().parents[3]
PROXY_DIR = REPO_ROOT / "proxy"
TESTS_DIR = REPO_ROOT / "tests"


def _xdg_config_home() -> Path:
    """XDG_CONFIG_HOME, defaulting to ~/.config. Read at call time so
    tests can override the env var before any call."""
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _xdg_state_home() -> Path:
    """XDG_STATE_HOME, defaulting to ~/.local/state. Read at call time so
    tests can override the env var before any call."""
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def config_dir() -> Path:
    """Root config dir: $XDG_CONFIG_HOME/credproxy/."""
    return _xdg_config_home() / "credproxy"


def _labeled(dirs: list[Path]) -> list[tuple[str, Path]]:
    """Label each overlay dir by its basename, disambiguating duplicate
    basenames with a deterministic numeric suffix (`base`, `base#2`, ...) in
    declared order. Derived only from the dir list, so the same env var always
    yields the same labels (they flow into --json output and doctor check ids)."""
    seen: dict[str, int] = {}
    out: list[tuple[str, Path]] = []
    for d in dirs:
        base = d.name
        seen[base] = seen.get(base, 0) + 1
        label = base if seen[base] == 1 else f"{base}#{seen[base]}"
        out.append((label, d))
    return out


def _discovered_overlays() -> list[Path]:
    """The default (env-unset) overlays: every DIRECTORY under the
    `<repo>/overlay/` container, lexical order by basename. Upstream ships the
    container with only a README, so a fork activates an overlay by just
    `mkdir overlay/<org>/` -- no env var, no engine edit. Files (the README) are
    skipped; a missing or subdir-less container yields no overlays. Lexical
    ordering makes multi-overlay precedence explicit (`10-base/`, `20-team/`)."""
    container = REPO_ROOT / "overlay"
    if not container.is_dir():
        return []
    return sorted((d for d in container.iterdir() if d.is_dir()),
                  key=lambda d: d.name)


def overlay_dirs() -> list[tuple[str, Path]]:
    """The ordered org *overlays*, most specific first -- the middle tier(s)
    between the end-user's XDG config and the in-package `builtin` defaults. Each
    holds an org's customized scaffold and definitions (injectors/providers/
    scripts/presets) -- see docs/overlays.md.

    `CREDPROXY_OVERLAY_PATH` is an `os.pathsep`-separated list of dirs, searched
    leftmost-first (PATH semantics); it REPLACES the default entirely. Unset
    falls back to discovery: each subdirectory of the `<repo>/overlay/` container
    is one overlay (lexical order; upstream ships the container empty except a
    README, so a fork's whole diff is `overlay/<org>/...`). Set-but-empty (`""`)
    means NO overlays (explicit opt-out); empty entries within the list (`a::b`)
    are skipped. Labels are `overlay:<dir basename>`, deduped with a numeric
    suffix. Read at call time so the env var can change per test. Missing
    env-listed dirs are tolerated here (they contribute nothing) -- loud
    reporting is doctor's job."""
    env = os.environ.get("CREDPROXY_OVERLAY_PATH")
    dirs = ([Path(p) for p in env.split(os.pathsep) if p]
            if env is not None else _discovered_overlays())
    return [(f"overlay:{label}", d) for label, d in _labeled(dirs)]


def overlay_roots() -> list[tuple[str, Path]]:
    """The whole resolution order, most specific first:

        user (XDG)  ->  overlays (declared order)  ->  builtin (upstream)

    The single seam every registry search and the singleton walk derive from, so
    the tiers stay in sync across all assets."""
    return [("user", config_dir()), *overlay_dirs(), ("builtin", BUILTIN_DIR)]


def workspaces_config_dir() -> Path:
    """Directory that holds per-workspace TOML files."""
    return config_dir() / "workspaces"


def providers_config_dir() -> Path:
    """User provider registry: $XDG_CONFIG_HOME/credproxy/providers/."""
    return config_dir() / "providers"


def injectors_config_dir() -> Path:
    """User injector registry: $XDG_CONFIG_HOME/credproxy/injectors/."""
    return config_dir() / "injectors"


def scripts_config_dir() -> Path:
    """User Starlark-script registry: $XDG_CONFIG_HOME/credproxy/scripts/.
    Scripts back a scripted injector (scheme = "script"); the CLI reads the
    .star source and pushes it to the proxy."""
    return config_dir() / "scripts"


# Builtin definitions ship in the package; they double as scaffold templates.
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin"


def builtin_providers_dir() -> Path:
    return BUILTIN_DIR / "providers"


def builtin_injectors_dir() -> Path:
    return BUILTIN_DIR / "injectors"


def builtin_scripts_dir() -> Path:
    return BUILTIN_DIR / "scripts"


def builtin_presets_dir() -> Path:
    return BUILTIN_DIR / "presets"


# Singleton distribution assets (one file; a higher tier overrides it).
def builtin_workspace_template_file() -> Path:
    """Built-in workspace scaffold frame."""
    return BUILTIN_DIR / "workspace.template.toml"


def resolve_singleton(filename: str) -> Path | None:
    """A singleton distribution file (only `workspace.template.toml` today): the
    first tier -- user, then each overlay, then builtin -- whose copy exists,
    else None. Rides the same `overlay_roots()` walk as the registries, so a
    user's personal `$XDG_CONFIG_HOME/credproxy/<filename>` shadows every
    overlay's, which shadow the builtin default."""
    for _, root in overlay_roots():
        cand = root / filename
        if cand.is_file():
            return cand
    return None


def layered_dirs(kind: str) -> list[tuple[str, Path]]:
    """The ordered search path for a *registry* asset kind (`injectors`,
    `providers`, `scripts`, `presets`), most specific first:

        user (XDG)  ->  overlays (declared order)  ->  builtin (upstream default)

    First match wins, so an overlay shadows a builtin definition of the same
    name and a user definition shadows both. Just `overlay_roots()` joined to
    the kind subdir, so every `find_*`/`list_*` stays in sync (and treats the
    tier label as opaque, so N overlays need no per-registry logic)."""
    return [(label, root / kind) for label, root in overlay_roots()]


def state_dir() -> Path:
    """Root state dir: $XDG_STATE_HOME/credproxy/."""
    return _xdg_state_home() / "credproxy"


def workspaces_state_dir() -> Path:
    """Directory that holds per-workspace state subdirs."""
    return state_dir() / "workspaces"


# CLI-only conventions. The workspace *image* is mandatory (no default -- the
# scaffold writes a concrete one, `load_config` errors if missing), and `home`
# is optional sugar for a managed volume (no default home path either), so the
# only hardcoded distribution constant left is the proxy image tag.
IMAGE_TAG = "credproxy:dev"          # the proxy image the CLI builds/runs
DEFAULT_WORKSPACE = "default"


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: a same-dir temp file + os.replace, so
    an interrupted or concurrent write never leaves a truncated/partial file --
    the path always holds either the prior complete contents or the new complete
    contents. The shared writer for every file that is itself a source of truth
    or drives drift/setup state (the workspace TOML, applied-spec/-bindings,
    setup_done, the default pointer); a torn write to any of those silently
    corrupts state.

    A new file gets the usual umask-derived mode; an overwrite preserves the
    existing file's permissions. The temp name carries the pid so two processes
    writing the same path don't clobber each other's temp. (Durability across
    power loss -- fsync -- is intentionally not done here.)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
