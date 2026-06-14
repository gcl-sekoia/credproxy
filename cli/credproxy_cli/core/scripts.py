"""Starlark script registry for scripted injectors (design-v3 phase 3b).

A *scripted injector* (an injector with `scheme = "script"`) names a `.star`
file that defines `on_request`/`on_response`. The host CLI resolves the file
here and reads its SOURCE; the source is pushed to the proxy in the wire config
(the push model -- the proxy stays stateless and compiles what it is given, so
user scripts work with no mounts or image rebuilds). The proxy sandboxes
execution; see `proxy/starlark_runtime.py`.

Discovery (first match wins, user shadows bundled):
  1. $XDG_CONFIG_HOME/credproxy/scripts/<name>.star
  2. bundled  cli/credproxy_cli/bundled/scripts/<name>.star
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import InjectorError
from .paths import bundled_scripts_dir, scripts_config_dir


@dataclass(frozen=True)
class Script:
    name: str
    source: str
    source_origin: str  # "user" or "bundled" -- for diagnostics / `list`


def find_script(name: str) -> Script:
    """Resolve a `.star` script by name and read its source; user shadows
    bundled. Raises InjectorError if not found."""
    for origin, base in (("user", scripts_config_dir()),
                         ("bundled", bundled_scripts_dir())):
        path = base / f"{name}.star"
        if path.is_file():
            return Script(name=name, source=path.read_text(), source_origin=origin)
    raise InjectorError(
        f"script '{name}' not found (looked for {name}.star in "
        f"{scripts_config_dir()} and {bundled_scripts_dir()})"
    )


def list_scripts() -> list[Script]:
    """All resolvable scripts, user shadowing bundled, sorted by name."""
    seen: dict[str, Script] = {}
    for origin, base in (("bundled", bundled_scripts_dir()),
                         ("user", scripts_config_dir())):
        if not base.is_dir():
            continue
        for path in base.iterdir():
            if path.suffix == ".star" and path.is_file():
                seen[path.stem] = Script(path.stem, path.read_text(), origin)
    return [seen[n] for n in sorted(seen)]
