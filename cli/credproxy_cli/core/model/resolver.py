"""The workspace resolver: the ONE boundary between the config plane (the
hand-owned intent TOML + the machine-owned lockfile) and everything else.

`resolve_workspace(ws)` parses the intent with today's validators, then binds
each binding's PLACEHOLDER identity -- an explicit `placeholder` in the TOML
wins; otherwise the value is read from (or minted into) the lockfile, keyed by
binding name. Secrets are NEVER fetched here (that stays in the push/wire path),
nothing is prompted, and nothing is written: resolution is side-effect-free. A
mutating command persists the returned lock content only when `lock_dirty`.

Nothing outside `core/model/` may re-derive bindings/rules from the TOML --
engine and porcelain consume `resolve_workspace()`, so placeholder identity
lives in exactly one place.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import tomllib

from ..errors import ConfigError
from . import lock as lock_mod
from .bindings import (
    Binding,
    _parse_bindings,
    _require_binding_names,
    validate as validate_bindings,
)
from .config import load_config_from_text
from .injectors import find_injector
from .rules import (
    Rule,
    _parse_rules,
    _require_rule_names,
    validate as validate_rules,
)

if TYPE_CHECKING:
    from .workspace import Workspace


@dataclass(frozen=True)
class ResolvedWorkspace:
    """The fully-resolved config plane for a workspace, secrets excluded.

    - `config`:   the normalized container-half dict (`load_config`).
    - `bindings`: validated bindings with `name` required and `placeholder`
                  bound (from the TOML if explicit, else from the lock). Secret
                  VALUES are NOT fetched -- only the secret refs the TOML carries.
    - `rules`:    validated rules.
    - `lock`:     the new/updated lock content (unknown top-level keys preserved).
    - `lock_dirty`: True iff `lock` differs from what is on disk (a caller that
                  mutates persists it; a read-only caller ignores it).
    """
    config: dict
    bindings: list[Binding]
    rules: list[Rule]
    lock: dict
    lock_dirty: bool


def resolve_workspace(ws: "Workspace") -> ResolvedWorkspace:
    """Resolve `ws`'s intent + lock into a `ResolvedWorkspace`. Pure/read-only:
    never fetches a secret, never prompts, never writes. Raises `ConfigError` on
    an invalid config (missing name, failed validation) -- reusing the existing
    validators verbatim."""
    source = str(ws.config_path)
    if not ws.exists():
        raise ConfigError(
            f"workspace '{ws.name}' not found (no {ws.config_path})")
    text = ws.config_path.read_text()
    # Host bind-source existence is a START-time check, not a resolve-time one --
    # resolution is side-effect-free and must not depend on host filesystem state
    # (so `binding list`/`test` on a config whose bind dir isn't created yet still
    # works). `start`/`apply`/`inspect` run their own existence-checking
    # load_config; deferring here mirrors CLAUDE.md ("existence checked at start").
    cfg = load_config_from_text(text, source, check_bind_exists=False)

    raw = tomllib.loads(text)

    bindings = _parse_bindings(raw, source)
    _require_binding_names(bindings, source)
    rules = _parse_rules(raw, source)
    _require_rule_names(rules, source)

    lock = lock_mod.load_lock(ws)
    old_ph: dict = lock.get("placeholders", {})
    new_ph: dict[str, str] = {}
    resolved_bindings: list[Binding] = []
    for b in bindings:
        placeholder = b.placeholder
        if placeholder is not None:
            # An explicit `placeholder` in the TOML wins and never enters the
            # lock -- the hand-authored value is the source of truth.
            resolved_bindings.append(b)
            continue
        injector = find_injector(b.injector)
        if injector.spec.uses_placeholder:
            # Lock-managed: reuse the recorded placeholder (stable across
            # resolves), else mint one from the injector's pattern. Identity is
            # keyed by binding NAME, so renaming regenerates it. Membership (not
            # `or`) so a stored value that is somehow falsy isn't treated as
            # missing -- that would re-mint every resolve (a permanent dirty-flap).
            if b.name in old_ph:
                placeholder = old_ph[b.name]
            else:
                placeholder = injector.placeholder.generate()
            new_ph[b.name] = placeholder
            resolved_bindings.append(replace(b, placeholder=placeholder))
        else:
            # Sign-family / no-placeholder schemes hold none.
            resolved_bindings.append(b)

    # Validate the RESOLVED bindings (placeholders bound), matching what the
    # proxy receives on the wire.
    validate_bindings(resolved_bindings, source)
    validate_rules(rules, source)

    lock_dirty = new_ph != old_ph
    new_lock = {**lock, "placeholders": new_ph}

    return ResolvedWorkspace(
        config=cfg,
        bindings=resolved_bindings,
        rules=rules,
        lock=new_lock,
        lock_dirty=lock_dirty,
    )
