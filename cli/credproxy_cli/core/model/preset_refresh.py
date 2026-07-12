"""`preset refresh` (config-v2, #64): re-expand `[[preset]]` references from their
CURRENT definitions and diff the result against the locked snapshots.

This is small by construction. A preset is a durable reference whose expansion is
snapshotted in `lock.json`; a changed definition is inert until the operator asks
for it. Refresh is that ask: for each targeted reference it FORCE-re-expands from
the current definition -- reusing the ONE re-expand implementation the resolver
uses (`resolver._expand_ref`, which reuses the locked shared placeholder and never
rotates it) -- then structurally diffs the old locked `expansion` against the new
one and (unless `--check`) persists the fresh snapshot.

There is no stamped text to hand-edit anymore, so the old sha-forensics /
three-way / skipped-edited machinery is gone: a hand change is expressed through
`disable` / `[preset.override.<suffix>]` in the reference (which are the ref's
INPUTS, so they survive a refresh). A vanished definition part simply disappears
from the new snapshot -- the diff's "removed" case, no `--prune` flag.

Model plane only: no engine/subprocess/porcelain imports (the docker-existence
restart hint and the newly-intercepted advisory are the porcelain caller's).
"""
from __future__ import annotations

import difflib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import tomllib

from ..errors import ConfigError
from . import lock as lock_mod
from . import resolver as resolver_mod
from .presets import get_preset, parse_preset_refs, preset_ref_inputs

if TYPE_CHECKING:
    from .resolver import ResolvedWorkspace
    from .workspace import Workspace


@dataclass(frozen=True)
class EntryDiff:
    """One structural change in a preset's expansion. `kind` is the entry family
    (`binding`/`rule`/`mount`/`env`/`setup`); `name` is its identity within the
    pack (a binding/rule name, a mount target, an env key, a setup `order`);
    `action` is `added`/`removed`/`changed`; `diff` is a unified-diff string of the
    canonically-rendered entry (only for `changed`)."""
    kind: str
    name: str
    action: str
    diff: str | None = None

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "name": self.name, "action": self.action}
        # A mount's identity IS its target (the keying function), but a `--json`
        # consumer keying on `kind:"mount"` looks for `target`, not `name` -- so
        # surface it under both. Targets are unique within a pack, so `name`
        # (never `#n`-disambiguated for mounts) equals the target verbatim.
        if self.kind == "mount":
            d["target"] = self.name
        if self.diff is not None:
            d["diff"] = self.diff
        return d


@dataclass(frozen=True)
class PresetRefresh:
    """The per-preset refresh outcome: whether its expansion changed, the old/new
    `definition_rev`, and the entry-level diff."""
    preset: str
    changed: bool
    old_rev: str | None
    new_rev: str
    entries: tuple[EntryDiff, ...]

    def to_dict(self) -> dict:
        return {
            "preset": self.preset,
            "changed": self.changed,
            "definition_rev": {"old": self.old_rev, "new": self.new_rev},
            "entries": [e.to_dict() for e in self.entries],
        }


@dataclass(frozen=True)
class RefreshResult:
    """The whole refresh computation (no side effects). `new_lock` is the lock
    content to persist (the caller writes it unless `--check` or `not dirty`);
    `resolved` is the re-validated merged model (for the caller's host advisories);
    `container_half_changed` flags a mount/env/setup change (spec-drift hint)."""
    presets: tuple[PresetRefresh, ...]
    new_lock: dict
    resolved: "ResolvedWorkspace"
    changed: bool
    dirty: bool
    container_half_changed: bool


def compute_refresh(ws: "Workspace",
                    preset_name: str | None = None) -> RefreshResult:
    """Re-expand `preset_name` (or every `[[preset]]` reference when None) from the
    current definition, diff against the locked snapshots, and RE-VALIDATE the
    merged model -- all without writing. Raises `ConfigError` (naming both sides)
    if the refresh would introduce a collision, or if a named `preset_name` isn't
    referenced in the intent file. The caller persists `new_lock` when `dirty`."""
    if not ws.exists():
        raise ConfigError(
            f"workspace '{ws.name}' not found (no {ws.config_path})")
    source = str(ws.config_path)
    text = ws.config_path.read_text()
    raw = tomllib.loads(text)
    lock = lock_mod.load_lock(ws)

    refs = parse_preset_refs(raw, source)
    by_name = {r.name: r for r in refs}
    if preset_name is not None:
        if preset_name not in by_name:
            referenced = ", ".join(f"'{n}'" for n in by_name) or "(none)"
            raise ConfigError(
                f"preset '{preset_name}' is not referenced in {source} "
                f"-- referenced pack(s): {referenced}")
        targets = [by_name[preset_name]]
    else:
        targets = list(refs)

    old_presets: dict = lock.get("presets", {})
    new_lock = deepcopy(lock)
    new_presets = new_lock.setdefault("presets", {})

    diffs: list[PresetRefresh] = []
    for ref in targets:
        old_entry = old_presets.get(ref.name)
        old_exp = old_entry.get("expansion", {}) \
            if isinstance(old_entry, dict) else {}
        old_rev = old_entry.get("definition_rev") \
            if isinstance(old_entry, dict) else None
        inputs = preset_ref_inputs(ref)
        spec = get_preset(ref.name)          # CredproxyError on an unknown pack
        # FORCE re-expand -- the ONE re-expand implementation the resolver uses,
        # so refresh and resolve can never diverge on what an expansion is (the
        # shared placeholder is reused from `old_entry`, never rotated).
        new_entry = resolver_mod._expand_ref(spec, ref, inputs, old_entry or {})
        new_presets[ref.name] = new_entry
        entries = _diff_expansion(old_exp, new_entry["expansion"])
        diffs.append(PresetRefresh(
            preset=ref.name, changed=bool(entries), old_rev=old_rev,
            new_rev=new_entry["definition_rev"], entries=tuple(entries)))

    # Re-validate the merged model against the refreshed lock. Because the refs'
    # INPUTS are unchanged, the resolver reuses these refreshed snapshots verbatim
    # (never re-expands them again) and validates the whole set: a collision an
    # introduced part causes raises ConfigError HERE, before anything is written.
    resolved = resolver_mod._resolve_from(
        text, new_lock, source, check_bind_exists=False)

    # A NAMED `preset refresh A` still persists any OTHER pack whose ref inputs
    # were edited in the TOML (the final resolve re-expands it, and `dirty` covers
    # the whole lock). Behavior-neutral (any resolve would), but out of the named
    # scope -- so surface a note per non-targeted pack whose lock snapshot changed
    # as a side effect. (An inputs-UNCHANGED pack whose definition merely drifted
    # is reused inert; the resolver already emits its own "run preset refresh"
    # note there, and its snapshot is unchanged, so it isn't caught here.)
    if preset_name is not None:
        final_presets: dict = resolved.lock.get("presets", {})
        for pname, new_e in final_presets.items():
            if pname == preset_name:
                continue
            if old_presets.get(pname) != new_e:
                resolved.notes.append(
                    f"preset '{pname}' inputs changed -- re-expanded "
                    f"(run 'preset refresh {pname}' to review)")

    container_half_changed = any(
        e.kind in ("mount", "env", "setup")
        for d in diffs for e in d.entries)

    return RefreshResult(
        presets=tuple(diffs),
        new_lock=resolved.lock,
        resolved=resolved,
        changed=any(d.changed for d in diffs),
        dirty=(resolved.lock != lock),
        container_half_changed=container_half_changed,
    )


# ---- structural diff ---------------------------------------------------------


def _diff_expansion(old_exp: dict, new_exp: dict) -> list[EntryDiff]:
    """Diff two `expansion` snapshots (`{bindings, rules, mounts, env, setup}`)
    entry-by-entry. Bindings/rules key on `name`, mounts on `target`, env on its
    key, setup on `order` -- each stable within a pack. Returns [] when identical."""
    out: list[EntryDiff] = []
    out += _keyed_diff("binding", old_exp.get("bindings", []),
                       new_exp.get("bindings", []), lambda b: b.get("name", ""))
    out += _keyed_diff("rule", old_exp.get("rules", []),
                       new_exp.get("rules", []), lambda r: r.get("name", ""))
    out += _keyed_diff("mount", old_exp.get("mounts", []),
                       new_exp.get("mounts", []),
                       lambda m: str(m.get("target", "")))
    out += _env_diff(old_exp.get("env", {}), new_exp.get("env", {}))
    out += _keyed_diff("setup", old_exp.get("setup", []),
                       new_exp.get("setup", []),
                       lambda s: str(s.get("order", "")))
    return out


def _ordered_map(items: list, key_of: Callable[[dict], str]) -> dict:
    """Map each item to its identity key, preserving order. A duplicate key (e.g.
    two setup steps sharing an `order`) is disambiguated with a `#n` suffix so it
    still diffs cleanly rather than clobbering."""
    m: dict[str, dict] = {}
    for it in items:
        k = key_of(it)
        if k in m:
            n = 2
            while f"{k}#{n}" in m:
                n += 1
            k = f"{k}#{n}"
        m[k] = it
    return m


def _keyed_diff(kind: str, old_list: list, new_list: list,
                key_of: Callable[[dict], str]) -> list[EntryDiff]:
    old_map = _ordered_map(old_list, key_of)
    new_map = _ordered_map(new_list, key_of)
    out: list[EntryDiff] = []
    for k, v in new_map.items():
        if k not in old_map:
            out.append(EntryDiff(kind, k, "added"))
        elif old_map[k] != v:
            out.append(EntryDiff(kind, k, "changed", _unified(old_map[k], v, k)))
    for k in old_map:
        if k not in new_map:
            out.append(EntryDiff(kind, k, "removed"))
    return out


def _env_diff(old: dict, new: dict) -> list[EntryDiff]:
    out: list[EntryDiff] = []
    for k, v in new.items():
        if k not in old:
            out.append(EntryDiff("env", k, "added"))
        elif old[k] != v:
            out.append(EntryDiff("env", k, "changed",
                                 _unified({k: old[k]}, {k: v}, k)))
    for k in old:
        if k not in new:
            out.append(EntryDiff("env", k, "removed"))
    return out


def _unified(old: dict, new: dict, name: str) -> str:
    """A readable unified diff of two entries, canonically rendered (sorted-key,
    indented JSON) so the diff shows only the fields that actually changed."""
    old_lines = json.dumps(old, indent=2, sort_keys=True).splitlines()
    new_lines = json.dumps(new, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{name} (old)", tofile=f"{name} (new)", lineterm=""))
