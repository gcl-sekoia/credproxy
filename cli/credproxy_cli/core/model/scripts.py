"""Starlark script registry for scripted injectors.

A *scripted injector* (an injector with `scheme = "script"`) names a `.star`
file that defines `on_request`/`on_response`. The host CLI resolves the file
here and reads its SOURCE; the source is pushed to the proxy in the wire config
(the push model -- the proxy stays stateless and compiles what it is given, so
user scripts work with no mounts or image rebuilds). The proxy sandboxes
execution; see `proxy/starlark_runtime.py`.

Discovery (first match wins, user shadows overlays shadow builtin):
  1. user      $XDG_CONFIG_HOME/credproxy/scripts/<name>.star
  2. overlays  <CREDPROXY_OVERLAY_PATH or repo/overlay/*>/scripts/<name>.star
  3. builtin   cli/credproxy_cli/builtin/scripts/<name>.star
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..errors import InjectorError
from ..paths import layered_dirs


@dataclass(frozen=True)
class Script:
    name: str
    source: str
    # tier label: "user", "overlay:<name>", "builtin" -- diagnostics / `list`
    source_origin: str
    # Tier labels this script shadows (same name, less-specific tier),
    # most-specific-loser first. Populated only by `list_scripts`.
    shadows: tuple[str, ...] = ()


def find_script(name: str) -> Script:
    """Resolve a `.star` script by name and read its source across the layered
    registry (user > overlays > builtin). Raises InjectorError if not found."""
    searched = layered_dirs("scripts")
    for origin, base in searched:
        path = base / f"{name}.star"
        if path.is_file():
            return Script(name=name, source=path.read_text(), source_origin=origin)
    where = ", ".join(str(b) for _, b in searched)
    raise InjectorError(
        f"script '{name}' not found (looked for {name}.star in {where})"
    )


def list_scripts() -> list[Script]:
    """All resolvable scripts, user shadowing overlays shadowing builtin, sorted
    by name. Each winner carries the tier labels it shadows (recorded as the
    least-specific-first walk overwrites, so no extra filesystem walk)."""
    seen: dict[str, Script] = {}
    shadowed: dict[str, list[str]] = {}
    for origin, base in reversed(layered_dirs("scripts")):
        if not base.is_dir():
            continue
        for path in base.iterdir():
            if path.suffix == ".star" and path.is_file():
                name = path.stem
                if name in seen:
                    shadowed.setdefault(name, []).append(seen[name].source_origin)
                seen[name] = Script(name, path.read_text(), origin)
    return [replace(seen[n], shadows=tuple(reversed(shadowed.get(n, []))))
            for n in sorted(seen)]
