"""The workspace resolver: the ONE boundary between the config plane (the
hand-owned intent TOML + the machine-owned lockfile) and everything else.

`resolve_workspace(ws)` parses the intent with today's validators, binds each
binding's PLACEHOLDER identity (an explicit `placeholder` in the TOML wins;
otherwise it is read from -- or minted into -- the lockfile, keyed by binding
name), and EXPANDS every `[[preset]]` reference (config-v2): a pack a `[[preset]]`
block names is expanded from its current definition (or reused verbatim from the
lock when the operator's recorded inputs are unchanged), snapshotted in the lock,
and merged into the effective model as ordinary bindings/rules/container-half --
LITERAL entries first, then preset expansions in `[[preset]]` declaration order.

Secrets are NEVER fetched here (that stays in the push/wire path), nothing is
prompted (all prompting happens at `preset add`/`create` time; the resolver fails
closed on an unresolvable ref), and nothing is written: resolution is
side-effect-free. A mutating command persists the returned lock content only when
`lock_dirty`.

Nothing outside `core/model/` may re-derive bindings/rules from the TOML --
engine and porcelain consume `resolve_workspace()`, so placeholder identity and
preset expansion live in exactly one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import tomllib

from ..errors import ConfigError, CredproxyError
from . import lock as lock_mod
from .bindings import (
    Binding,
    _parse_bindings,
    _require_binding_names,
    validate as validate_bindings,
)
from .config import (
    _parse_mount,
    load_config_from_text,
    validate_mount_set,
)
from .injectors import find_injector
from .presets import (
    build_preset,
    expansion_to_lock,
    get_preset,
    lock_expansion_to_model,
    parse_preset_refs,
    preset_ref_inputs,
    resolve_options,
    resolve_preset_credential,
)
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

    - `config`:   the normalized container-half dict (`load_config`), with any
                  `[[preset]]` container half (mounts/env/setup) merged in.
    - `bindings`: validated bindings (literal + preset-expanded), `name` required
                  and `placeholder` bound. Secret VALUES are NOT fetched -- only
                  the secret refs the TOML/expansion carries.
    - `rules`:    validated rules (literal + preset-expanded).
    - `lock`:     the new/updated lock content (unknown top-level keys preserved).
    - `lock_dirty`: True iff `lock` differs from what is on disk (a caller that
                  mutates persists it; a read-only caller ignores it).
    - `notes`:    non-fatal advisories (e.g. a preset whose definition changed
                  since the lock snapshot) for the CLI to surface.
    """
    config: dict
    bindings: list[Binding]
    rules: list[Rule]
    lock: dict
    lock_dirty: bool
    notes: list[str] = field(default_factory=list)


def resolve_workspace(ws: "Workspace", *,
                      check_bind_exists: bool = False) -> ResolvedWorkspace:
    """Resolve `ws`'s intent + lock into a `ResolvedWorkspace`. Pure/read-only:
    never fetches a secret, never prompts, never writes. Raises `ConfigError` on
    an invalid config (missing name, failed validation, an unresolvable preset
    ref) -- reusing the existing validators verbatim.

    `check_bind_exists` selects the mount-normalization mode: the default (False)
    keeps host-bind sources literal (side-effect-free -- `binding list`/`inspect`
    work before a bind dir is created); the engine passes True at `start`-time so
    both literal AND preset-expanded binds are `~`-expanded and existence-checked,
    exactly as a hand-written mount would be (so a preset mount feeds the spec hash
    identically to a stamped one)."""
    source = str(ws.config_path)
    if not ws.exists():
        raise ConfigError(
            f"workspace '{ws.name}' not found (no {ws.config_path})")
    text = ws.config_path.read_text()
    lock = lock_mod.load_lock(ws)
    return _resolve_from(text, lock, source, check_bind_exists=check_bind_exists)


def validate_text(text: str, source: str, *,
                  check_bind_exists: bool = False) -> ResolvedWorkspace:
    """Resolve + validate a workspace-config TOML STRING against an EMPTY lock --
    the in-memory, all-or-nothing check `create` runs before writing a config
    (every `[[preset]]` reference must expand, credentials/options resolvable,
    collisions clean). Placeholders are minted ephemerally (discarded); raises
    `ConfigError`/`PresetOptionsError` on any problem so nothing is written."""
    return _resolve_from(text, {"version": 1, "placeholders": {}}, source,
                         check_bind_exists=check_bind_exists)


def _resolve_from(text: str, lock: dict, source: str, *,
                  check_bind_exists: bool) -> ResolvedWorkspace:
    # Host bind-source existence is deferred by default: resolution is
    # side-effect-free and must not depend on host filesystem state. `start` calls
    # with check_bind_exists=True, where CLAUDE.md says the check belongs.
    cfg = load_config_from_text(text, source, check_bind_exists=check_bind_exists)

    raw = tomllib.loads(text)

    bindings = _parse_bindings(raw, source)
    _require_binding_names(bindings, source)
    rules = _parse_rules(raw, source)
    _require_rule_names(rules, source)

    old_ph: dict = lock.get("placeholders", {})
    new_ph: dict[str, str] = {}
    resolved_bindings: list[Binding] = []
    for b in bindings:
        placeholder = b.placeholder
        if placeholder is not None:
            resolved_bindings.append(b)
            continue
        injector = find_injector(b.injector)
        if injector.spec.uses_placeholder:
            if b.name in old_ph:
                placeholder = old_ph[b.name]
            else:
                placeholder = injector.placeholder.generate()
            new_ph[b.name] = placeholder
            resolved_bindings.append(replace(b, placeholder=placeholder))
        else:
            resolved_bindings.append(b)

    # ---- preset references (config-v2) ----
    refs = parse_preset_refs(raw, source)
    old_presets: dict = lock.get("presets", {})
    new_presets: dict = {}
    notes: list[str] = []
    presets_dirty = False
    preset_bindings: list[Binding] = []
    preset_rules: list[Rule] = []
    # The merged container half accumulates onto the literal cfg.
    for ref in refs:
        inputs = preset_ref_inputs(ref)
        old = old_presets.get(ref.name)
        if isinstance(old, dict) and old.get("inputs") == inputs \
                and isinstance(old.get("expansion"), dict):
            # Inputs unchanged: reuse the locked snapshot verbatim, even if the
            # definition file changed (inert until `preset refresh`). The snapshot
            # is complete and self-contained, so a deleted/unparseable pack is
            # TOLERATED here (the limit case of an inert definition change) -- the
            # pack is needed only for the `definition_rev` advisory. Surface a note
            # when the definition rev differs, or when it no longer resolves.
            entry = old
            new_presets[ref.name] = entry
            try:
                spec = get_preset(ref.name)
            except CredproxyError:
                notes.append(
                    f"preset '{ref.name}' definition no longer resolvable -- "
                    f"reusing lock snapshot")
            else:
                if entry.get("definition_rev") != spec.rev:
                    notes.append(
                        f"preset '{ref.name}' definition changed since lock -- "
                        f"run preset refresh")
        else:
            # Re-expand from the current definition (fresh, or the operator edited
            # the ref's inputs -- the operator's clock). A missing pack HERE is a
            # real error: there is no reusable snapshot for these inputs.
            spec = get_preset(ref.name)      # CredproxyError on an unknown pack
            entry = _expand_ref(spec, ref, inputs, old)
            new_presets[ref.name] = entry
            presets_dirty = True

        b, r, m, e, s = lock_expansion_to_model(
            ref.name, entry["expansion"], source)
        preset_bindings.extend(b)
        preset_rules.extend(r)
        if cfg.get("attach") is not None and (m or e or s):
            # An attached workspace has no credproxy-managed container, so a
            # preset can't contribute a mount/env/setup to it.
            raise ConfigError(
                f"{source}: preset '{ref.name}' carries container-half config "
                f"(mounts/env/setup), but the workspace is attached -- its "
                f"container is managed externally. Only binding/rule-only packs "
                f"apply to an attached workspace.")
        if cfg.get("attach") is None:
            _merge_container_half(
                cfg, ref.name, m, e, s, source,
                check_bind_exists=check_bind_exists)

    # ---- merge literal + preset entries (literal FIRST) ----
    _reject_cross_collisions(resolved_bindings, preset_bindings,
                             preset_rules, rules, source)
    merged_bindings = resolved_bindings + preset_bindings
    merged_rules = rules + preset_rules

    # Validate the merged, placeholder-bound set -- what the proxy receives on the
    # wire.
    validate_bindings(merged_bindings, source)
    validate_rules(merged_rules, source)

    lock_dirty = new_ph != old_ph or presets_dirty or new_presets != old_presets
    new_lock = {**lock, "placeholders": new_ph}
    if new_presets:
        new_lock["presets"] = new_presets
    elif "presets" in new_lock:
        # No refs remain -> drop a now-stale presets section.
        del new_lock["presets"]

    return ResolvedWorkspace(
        config=cfg,
        bindings=merged_bindings,
        rules=merged_rules,
        lock=new_lock,
        lock_dirty=lock_dirty,
        notes=notes,
    )


def _expand_ref(spec, ref, inputs: dict, old) -> dict:
    """Expand a `[[preset]]` ref from the current definition into a lock entry.
    Resolves the credential + options non-interactively (fails closed on a
    missing required one), reuses the shared placeholder from a prior snapshot
    (never rotating it), and applies `disable`/`override` via `expansion_to_lock`."""
    from ..errors import PresetOptionsError
    from .presets import option_summary

    provider, secret, missing = resolve_preset_credential(
        spec, ref.provider, ref.secret)
    if missing:
        joined = " and ".join(f"`{m}`" for m in missing)
        raise ConfigError(
            f"preset '{ref.name}': the `[[preset]]` reference is missing {joined} "
            f"(the pack has no default for it) -- add {joined} to the block, or "
            f"re-run `credproxy workspace ... preset add {ref.name}`")

    option_values, missing_opts = resolve_options(spec, ref.options, prompt=None)
    if missing_opts:
        raise PresetOptionsError(
            spec.name, [option_summary(o) for o in missing_opts])

    prior_ph = old.get("placeholder") if isinstance(old, dict) else None
    exp = build_preset(ref.name, provider, secret, options=option_values,
                       placeholder=prior_ph)
    # Record the SHARED placeholder `build_preset` generated/reused -- read off the
    # pre-transform bindings, BEFORE `expansion_to_lock` applies disable/override.
    # This is the pack's stable identity: it must not rotate when a disable drops
    # every placeholder-bearing part (the serialized expansion would then carry
    # none) nor be displaced by an override that sets a part's `placeholder`. When
    # the current expansion carries no placeholder-bearing binding, PRESERVE any
    # prior recorded identity so a disable->enable cycle reuses the same value.
    shared_ph = next(
        (b.placeholder for b in exp.bindings if b.placeholder), None) or prior_ph
    expansion = expansion_to_lock(exp, ref)
    return {
        "definition_rev": spec.rev,
        "inputs": inputs,
        "placeholder": shared_ph,
        "expansion": expansion,
    }


def _merge_container_half(cfg: dict, preset: str, mounts: list, env: dict,
                          setup: list, source: str, *,
                          check_bind_exists: bool) -> None:
    """Merge one preset's container half into the (literal) `cfg` in place:
    mounts appended (re-normalized through the shared `_parse_mount`, so a preset
    mount is held to identical validation and feeds the spec hash exactly like a
    hand-written one), env keys merged (identical value fine, different value an
    error naming both sides), setup steps appended. Target/name collisions error."""
    existing_targets = {m["target"].rstrip("/") or "/" for m in cfg["mounts"]}
    for i, table in enumerate(mounts):
        where = f"{source}: preset '{preset}' mount[{i}]"
        norm = _parse_mount(table, where, expand_bind=check_bind_exists)
        t = norm["target"].rstrip("/") or "/"
        if t in existing_targets:
            raise ConfigError(
                f"{source}: preset '{preset}' mounts {norm['target']!r}, which is "
                f"already mounted by another entry (mount targets must be unique)")
        existing_targets.add(t)
        cfg["mounts"].append(norm)
    validate_mount_set(cfg["mounts"], source, cfg.get("user"))

    for k, v in env.items():
        if k in cfg["env"]:
            if cfg["env"][k] == v:
                continue
            raise ConfigError(
                f"{source}: preset '{preset}' sets env {k}={v!r}, but {k}="
                f"{cfg['env'][k]!r} is already set (different value)")
        cfg["env"][k] = v

    cfg["setup"].extend(setup)


def _reject_cross_collisions(literal_bindings, preset_bindings,
                             preset_rules, literal_rules, source: str) -> None:
    """Fail with a message naming BOTH sides when a preset-expanded binding/rule
    name collides with a literal (or another preset's) entry -- clearer than the
    generic duplicate-name error `validate` would raise on the merged set."""
    lit_b = {b.name for b in literal_bindings}
    lit_r = {r.name for r in literal_rules}
    seen_b: set[str] = set()
    for b in preset_bindings:
        if b.name in lit_b:
            raise ConfigError(
                f"{source}: preset-expanded binding '{b.name}' collides with a "
                f"literal `[[binding]]` of the same name -- rename one, or "
                f"`disable`/`override` the preset part")
        if b.name in seen_b:
            raise ConfigError(
                f"{source}: two presets both expand a binding named '{b.name}'")
        seen_b.add(b.name)
    seen_r: set[str] = set()
    for r in preset_rules:
        if r.name in lit_r:
            raise ConfigError(
                f"{source}: preset-expanded rule '{r.name}' collides with a "
                f"literal `[[rule]]` of the same name -- rename one, or "
                f"`disable`/`override` the preset rule")
        if r.name in seen_r:
            raise ConfigError(
                f"{source}: two presets both expand a rule named '{r.name}'")
        seen_r.add(r.name)
