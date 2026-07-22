"""The workspace resolver: the ONE boundary between the config plane (the
hand-owned intent TOML + the machine-owned lockfile) and everything else.

`resolve_workspace(ws)` parses the intent with today's validators, binds each
binding's PLACEHOLDER identity (an explicit `placeholder` in the TOML wins;
otherwise it is read from -- or minted into -- the lockfile, keyed by binding
name), and EXPANDS every `[[pack]]` reference (config-v2): a pack a `[[pack]]`
block names is expanded from its current definition (or reused verbatim from the
lock when the operator's recorded inputs are unchanged), snapshotted in the lock,
and merged into the effective model as ordinary bindings/rules/container-half --
LITERAL entries first, then pack expansions in `[[pack]]` declaration order.

Secrets are NEVER fetched here (that stays in the push/wire path), nothing is
prompted (all prompting happens at `pack add`/`create` time; the resolver fails
closed on an unresolvable ref), and nothing is written: resolution is
side-effect-free. A mutating command persists the returned lock content only when
`lock_dirty`.

Nothing outside `core/model/` may re-derive bindings/rules from the TOML --
engine and porcelain consume `resolve_workspace()`, so placeholder identity and
pack expansion live in exactly one place.
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
from .packs import (
    build_pack,
    expansion_to_lock,
    get_pack,
    lock_expansion_to_model,
    parse_pack_refs,
    pack_ref_inputs,
    resolve_options,
    resolve_pack_credential,
)
from .postgres import (
    Postgres,
    _parse_postgres,
    _require_postgres_names,
    validate as validate_postgres,
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
                  `[[pack]]` container half (mounts/env/setup) merged in.
    - `bindings`: validated bindings (literal + pack-expanded), `name` required
                  and `placeholder` bound. Secret VALUES are NOT fetched -- only
                  the secret refs the TOML/expansion carries.
    - `rules`:    validated rules (literal + pack-expanded).
    - `lock`:     the new/updated lock content (unknown top-level keys preserved).
    - `lock_dirty`: True iff `lock` differs from what is on disk (a caller that
                  mutates persists it; a read-only caller ignores it).
    - `notes`:    non-fatal advisories (e.g. a pack whose definition changed
                  since the lock snapshot) for the CLI to surface.
    """
    config: dict
    bindings: list[Binding]
    rules: list[Rule]
    postgres: list[Postgres]
    lock: dict
    lock_dirty: bool
    notes: list[str] = field(default_factory=list)


def resolve_workspace(ws: "Workspace", *,
                      check_bind_exists: bool = False) -> ResolvedWorkspace:
    """Resolve `ws`'s intent + lock into a `ResolvedWorkspace`. Pure/read-only:
    never fetches a secret, never prompts, never writes. Raises `ConfigError` on
    an invalid config (missing name, failed validation, an unresolvable pack
    ref) -- reusing the existing validators verbatim.

    `check_bind_exists` selects the mount-normalization mode: the default (False)
    keeps host-bind sources literal (side-effect-free -- `binding list`/`inspect`
    work before a bind dir is created); the engine passes True at `start`-time so
    both literal AND pack-expanded binds are `~`-expanded and existence-checked,
    exactly as a hand-written mount would be (so a pack mount feeds the spec hash
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
    (every `[[pack]]` reference must expand, credentials/options resolvable,
    collisions clean). Placeholders are minted ephemerally (discarded); raises
    `ConfigError`/`PackOptionsError` on any problem so nothing is written."""
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
    postgres = _parse_postgres(raw, source)
    _require_postgres_names(postgres, source)

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

    # ---- pack references (config-v2) ----
    refs = parse_pack_refs(raw, source)
    old_packs: dict = lock.get("packs", {})
    new_packs: dict = {}
    notes: list[str] = []
    packs_dirty = False
    pack_bindings: list[Binding] = []
    pack_rules: list[Rule] = []
    # The merged container half accumulates onto the literal cfg.
    for ref in refs:
        inputs = pack_ref_inputs(ref)
        old = old_packs.get(ref.name)
        if isinstance(old, dict) and old.get("inputs") == inputs \
                and isinstance(old.get("expansion"), dict):
            # Inputs unchanged: reuse the locked snapshot verbatim, even if the
            # definition file changed (inert until `pack refresh`). The snapshot
            # is complete and self-contained, so a deleted/unparseable pack is
            # TOLERATED here (the limit case of an inert definition change) -- the
            # pack is needed only for the `definition_rev` advisory. Surface a note
            # when the definition rev differs, or when it no longer resolves.
            entry = old
            new_packs[ref.name] = entry
            try:
                spec = get_pack(ref.name)
            except CredproxyError:
                notes.append(
                    f"pack '{ref.name}' definition no longer resolvable -- "
                    f"reusing lock snapshot")
            else:
                if entry.get("definition_rev") != spec.rev:
                    notes.append(
                        f"pack '{ref.name}' definition changed since lock -- "
                        f"run pack refresh")
        else:
            # Re-expand from the current definition (fresh, or the operator edited
            # the ref's inputs -- the operator's clock). A missing pack HERE is a
            # real error: there is no reusable snapshot for these inputs.
            spec = get_pack(ref.name)      # CredproxyError on an unknown pack
            entry = _expand_ref(spec, ref, inputs, old)
            new_packs[ref.name] = entry
            packs_dirty = True

        b, r, m, e, s = lock_expansion_to_model(
            ref.name, entry["expansion"], source)
        pack_bindings.extend(b)
        pack_rules.extend(r)
        if cfg.get("attach") is not None and (m or e or s):
            # An attached workspace has no credproxy-managed container, so a
            # pack can't contribute a mount/env/setup to it.
            raise ConfigError(
                f"{source}: pack '{ref.name}' carries container-half config "
                f"(mounts/env/setup), but the workspace is attached -- its "
                f"container is managed externally. Only binding/rule-only packs "
                f"apply to an attached workspace.")
        if cfg.get("attach") is None:
            _merge_container_half(
                cfg, ref.name, m, e, s, source,
                check_bind_exists=check_bind_exists)

    # ---- merge literal + pack entries (literal FIRST) ----
    _reject_cross_collisions(resolved_bindings, pack_bindings,
                             pack_rules, rules, source)
    merged_bindings = resolved_bindings + pack_bindings
    merged_rules = rules + pack_rules

    # Validate the merged, placeholder-bound set -- what the proxy receives on the
    # wire.
    validate_bindings(merged_bindings, source)
    validate_rules(merged_rules, source)
    validate_postgres(postgres, source)
    # pg binding names share the config namespace with bindings/rules (the proxy
    # keys /setup by name across all three, and mirrors this in load_pg's
    # `reserved` check) -- so a pg name must not collide with either.
    _reject_pg_collisions(postgres, merged_bindings, merged_rules, source)

    lock_dirty = new_ph != old_ph or packs_dirty or new_packs != old_packs
    new_lock = {**lock, "placeholders": new_ph}
    if new_packs:
        new_lock["packs"] = new_packs
    elif "packs" in new_lock:
        # No refs remain -> drop a now-stale packs section.
        del new_lock["packs"]

    return ResolvedWorkspace(
        config=cfg,
        bindings=merged_bindings,
        rules=merged_rules,
        postgres=postgres,
        lock=new_lock,
        lock_dirty=lock_dirty,
        notes=notes,
    )


def _reject_pg_collisions(postgres, bindings, rules, source: str) -> None:
    """A `[[postgres]]` name must be unique across the whole config namespace --
    not just among pg bindings (validate_postgres covers that), but against every
    `[[binding]]` and `[[rule]]` too."""
    taken = {b.name for b in bindings} | {r.name for r in rules}
    for p in postgres:
        if p.name in taken:
            raise ConfigError(
                f"{source}: pg binding '{p.name}' collides with a binding/rule of "
                f"the same name -- names are unique across bindings, rules, and "
                f"pg bindings")


def _expand_ref(spec, ref, inputs: dict, old) -> dict:
    """Expand a `[[pack]]` ref from the current definition into a lock entry.
    Resolves the credential + options non-interactively (fails closed on a
    missing required one), reuses the shared placeholder from a prior snapshot
    (never rotating it), and applies `disable`/`override` via `expansion_to_lock`."""
    from ..errors import PackOptionsError
    from .packs import option_summary

    provider, secret, missing = resolve_pack_credential(
        spec, ref.provider, ref.secret)
    if missing:
        joined = " and ".join(f"`{m}`" for m in missing)
        raise ConfigError(
            f"pack '{ref.name}': the `[[pack]]` reference is missing {joined} "
            f"(the pack has no default for it) -- add {joined} to the block, or "
            f"re-run `credproxy workspace ... pack add {ref.name}`")

    option_values, missing_opts = resolve_options(spec, ref.options, prompt=None)
    if missing_opts:
        raise PackOptionsError(
            spec.name, [option_summary(o) for o in missing_opts])

    prior_ph = old.get("placeholder") if isinstance(old, dict) else None
    exp = build_pack(ref.name, provider, secret, options=option_values,
                       placeholder=prior_ph)
    # Record the SHARED placeholder `build_pack` generated/reused -- read off the
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


def _merge_container_half(cfg: dict, pack: str, mounts: list, env: dict,
                          setup: list, source: str, *,
                          check_bind_exists: bool) -> None:
    """Merge one pack's container half into the (literal) `cfg` in place:
    mounts appended (re-normalized through the shared `_parse_mount`, so a pack
    mount is held to identical validation and feeds the spec hash exactly like a
    hand-written one), env keys merged (identical value fine, different value an
    error naming both sides), setup steps appended. Target/name collisions error."""
    existing_targets = {m["target"].rstrip("/") or "/" for m in cfg["mounts"]}
    for i, table in enumerate(mounts):
        where = f"{source}: pack '{pack}' mount[{i}]"
        norm = _parse_mount(table, where, expand_bind=check_bind_exists)
        t = norm["target"].rstrip("/") or "/"
        if t in existing_targets:
            raise ConfigError(
                f"{source}: pack '{pack}' mounts {norm['target']!r}, which is "
                f"already mounted by another entry (mount targets must be unique)")
        existing_targets.add(t)
        cfg["mounts"].append(norm)
    validate_mount_set(cfg["mounts"], source, cfg.get("user"))

    for k, v in env.items():
        if k in cfg["env"]:
            if cfg["env"][k] == v:
                continue
            raise ConfigError(
                f"{source}: pack '{pack}' sets env {k}={v!r}, but {k}="
                f"{cfg['env'][k]!r} is already set (different value)")
        cfg["env"][k] = v

    cfg["setup"].extend(setup)


def _reject_cross_collisions(literal_bindings, pack_bindings,
                             pack_rules, literal_rules, source: str) -> None:
    """Fail with a message naming BOTH sides when a pack-expanded binding/rule
    name collides with a literal (or another pack's) entry -- clearer than the
    generic duplicate-name error `validate` would raise on the merged set."""
    lit_b = {b.name for b in literal_bindings}
    lit_r = {r.name for r in literal_rules}
    seen_b: set[str] = set()
    for b in pack_bindings:
        if b.name in lit_b:
            raise ConfigError(
                f"{source}: pack-expanded binding '{b.name}' collides with a "
                f"literal `[[binding]]` of the same name -- rename one, or "
                f"`disable`/`override` the pack part")
        if b.name in seen_b:
            raise ConfigError(
                f"{source}: two packs both expand a binding named '{b.name}'")
        seen_b.add(b.name)
    seen_r: set[str] = set()
    for r in pack_rules:
        if r.name in lit_r:
            raise ConfigError(
                f"{source}: pack-expanded rule '{r.name}' collides with a "
                f"literal `[[rule]]` of the same name -- rename one, or "
                f"`disable`/`override` the pack rule")
        if r.name in seen_r:
            raise ConfigError(
                f"{source}: two packs both expand a rule named '{r.name}'")
        seen_r.add(r.name)
