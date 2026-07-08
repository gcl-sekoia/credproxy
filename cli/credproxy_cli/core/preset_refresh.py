"""`preset refresh`: re-expand a stamped pack against its CURRENT definition and
update the workspace TOML -- the explicit, operator-clock alternative to a live
link. A preset stays **expansion, not a link**: refresh is pure CLI-side tooling
over the stamped file (`preset_stamp`'s markers), and nothing here makes the
load/push path depend on a preset definition or its provenance comments.

Per-block three-way classification via the two provenance hashes #56 pinned:

  - **up-to-date**: the block's would-be re-stamped text is byte-identical to
    what's on disk (regardless of `rev` -- a re-encoded-but-identical definition
    is still up to date).
  - **updated**: the block is unedited since stamping (its recomputed `sha`
    matches the marker's `sha`) but the current definition renders it
    differently -> replace the block, new marker (new rev/sha).
  - **skipped-edited**: the block was hand-edited since stamping (recomputed
    `sha` != marker `sha`) -> NEVER overwrite; print a diff of what refresh WOULD
    write and move on.
  - **added**: the definition gained a block absent from the file -> stamp it
    additively.
  - **prunable/pruned**: a stamped block whose counterpart vanished from the
    definition -> reported; deleted only under `--prune`.

Identity is PRESERVED, not regenerated: the shared placeholder + provider/secret
are read back from the stamped bindings (rotating the placeholder would break
placeholder-consuming state and cross-binding sharing), and a definition-new
`[[part]]` reuses those siblings' credential/placeholder.

The write is all-or-nothing: every classified in-place edit is applied to one
line buffer, additions go through `preset_stamp.compose` (which re-parses and
verifies its own additions), then a final re-classification + full re-validation
of the composed text guards the write -- any surgery bug leaves the file
untouched. NEVER a three-way text merge: a hand-edited block is the operator's to
resolve.
"""
from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, replace

from . import bindings as core_bindings
from . import config as core_config
from . import rules as core_rules
from .bindings import (
    _BLOCK_HEADER_RE,
    _array_depth_delta,
    _block_spans,
    _parse_bindings,
    _render_binding_block,
    _with_auto_names as _bindings_with_auto_names,
)
from .errors import ConfigError
from .preset_stamp import (
    _line_comment,
    _marker,
    _MOUNTS_BLOCK_RE,
    _render_env_line,
    _render_mount_block,
    _render_mount_inline,
    _render_setup_block,
    _render_setup_inline,
    _SETUP_BLOCK_RE,
    _sha12,
    compose,
    is_marker_line,
    multiline_string_line_indices,
)
from .presets import PresetSpec, build_preset
from .rules import (
    _parse_rules,
    _render_rule_block,
    _RULE_CHILD_RE,
    _RULE_HEADER_RE,
    _with_auto_names as _rules_with_auto_names,
)

# Parse the `rev=`/`sha=` off a full provenance marker (name already matched by
# `is_marker_line` / the trailing-comment scan).
_REV_SHA_RE = re.compile(r"rev=([0-9a-f]{12})\s+sha=([0-9a-f]{12})")
# A top-level `mounts = [` / `setup = [` array opener (the inline-element form the
# stamp uses/creates on a normal workspace). Element lines inside it carry a
# trailing provenance comment; block-form `[[mounts]]`/`[[setup]]` is handled
# separately via `_block_spans`.
_ARRAY_OPEN_RE = re.compile(r"^\s*(mounts|setup)\s*=\s*\[")


# ---- results -----------------------------------------------------------------


@dataclass(frozen=True)
class RefreshAction:
    """One classified block/element. `target` is the join identity (binding/rule
    NAME, env KEY, mount TARGET, setup ORDER). `diff` is set only for
    `skipped-edited` (a unified diff of on-disk vs. would-write)."""
    kind: str      # binding | rule | env | mount | setup
    target: str
    # up-to-date | updated | skipped-edited | skipped-divergent |
    # skipped-collision | added | prunable | pruned
    action: str
    diff: str | None = None


@dataclass(frozen=True)
class RefreshResult:
    preset: str
    actions: tuple[RefreshAction, ...]
    new_text: str      # == the input text when nothing changed (caller skips the write)
    changed: bool
    container_changed: bool   # a mounts/env/setup block changed -> container spec drift


# ---- located stamped items ---------------------------------------------------


@dataclass
class _Stamped:
    kind: str
    identity: str
    rev: str
    sha: str            # the marker's recorded sha (definition-time span digest)
    current_sha: str    # sha recomputed from the on-disk text (== sha iff unedited)
    on_disk: str        # the exact text the sha covers (block body / env code / bare element)
    edit_start: int     # first line index to replace/delete (marker line, or the line itself)
    edit_end: int       # exclusive
    is_block: bool      # standalone-marker block (vs. a trailing-comment line)


def _norm_target(t: str) -> str:
    return t.rstrip("/") or "/"


def _is_blank_or_comment(line: str) -> bool:
    """True for a blank line or a whole-line comment (`# ...`, marker or plain
    hand note) -- the trailing lines a `[[...]]` block span swallows but that the
    stamp's `sha` never covered."""
    s = line.strip()
    return s == "" or s.startswith("#")


def _parse_marker(line: str) -> tuple[str, str] | None:
    m = _REV_SHA_RE.search(line)
    return (m.group(1), m.group(2)) if m else None


def _locate(text: str, preset_name: str) -> list[_Stamped]:
    """Every block/element in `text` stamped by `preset_name`, located by its
    provenance marker + the shared span machinery. Blocks (`[[binding]]`/
    `[[rule]]`/`[[mounts]]`/`[[setup]]`) carry a standalone marker line above
    them; env keys and inline mounts/setup elements carry a trailing-comment
    marker. Each item's `current_sha` is recomputed from the on-disk text so the
    caller can tell an unedited block from a hand-edited one."""
    lines = text.splitlines(keepends=True)
    # Lines inside a hand-written multiline string (`run = """..."""`) are inert:
    # a marker-shaped / header-shaped line there is a string value, never a real
    # stamp, so every scan below skips them (the per-line comment/blockspan
    # scanners can't see triple-quote state on their own).
    masked = multiline_string_line_indices(text)
    items: list[_Stamped] = []

    # 1) Standalone-marker blocks. Identity is parsed from each block's own text
    #    (not a raw-index map), so a mixed inline/block mounts array can't
    #    misalign the join key.
    block_kinds = [
        ("binding", _BLOCK_HEADER_RE, None, "binding", lambda d: d.get("name")),
        ("rule", _RULE_HEADER_RE, _RULE_CHILD_RE, "rule", lambda d: d.get("name")),
        ("mount", _MOUNTS_BLOCK_RE, None, "mounts", lambda d: _norm_target(d["target"])),
        ("setup", _SETUP_BLOCK_RE, None, "setup", lambda d: str(d["order"])),
    ]
    for kind, header_re, child_re, toml_key, id_fn in block_kinds:
        for start, end in _block_spans(text, header_re, child_re):
            if start == 0 or (start - 1) in masked \
                    or not is_marker_line(lines[start - 1]):
                continue
            marker = lines[start - 1].strip()
            name = _marker_name(marker)
            if name != preset_name:
                continue
            rs = _parse_marker(marker)
            if rs is None:
                continue
            # `_block_spans` runs a block to the NEXT table header, so its span
            # swallows the trailing blank separator, the following block's
            # standalone provenance marker, AND any hand comment a user appended
            # after the last stamped block. Trim all trailing blank/comment lines
            # back off, so `body` is the exact `[[...]]`-header-through-trailing-
            # newline text the stamp's `sha` covered (#56) -- the round-trip
            # anchor. The stamp never writes a comment INSIDE a block body, so a
            # trailing comment is provably not part of the sha'd text; leaving it
            # outside `end` also keeps it in the file across an `updated` splice.
            body_end = end
            while body_end > start and _is_blank_or_comment(lines[body_end - 1]):
                body_end -= 1
            end = body_end
            body = "".join(lines[start:end])
            try:
                elem = tomllib.loads(body)[toml_key][0]
            except (tomllib.TOMLDecodeError, KeyError, IndexError):
                continue
            identity = id_fn(elem)
            if identity is None:
                continue
            items.append(_Stamped(
                kind=kind, identity=str(identity), rev=rs[0], sha=rs[1],
                current_sha=_sha12(body), on_disk=body,
                edit_start=start - 1, edit_end=end, is_block=True))

    # 2) Trailing-comment lines: env keys (depth 0) and inline mounts/setup
    #    elements (inside a `mounts = [` / `setup = [` array). Array membership is
    #    tracked with the same bracket-depth scanner `_block_spans` uses.
    array_key: str | None = None
    depth = 0
    for i, ln in enumerate(lines):
        if i in masked:
            # Inside a multiline string: inert. Skip marker detection AND the
            # bracket-depth update (its brackets are string content, not array
            # structure).
            continue
        comment = _line_comment(ln)
        marker = comment.strip() if comment else ""
        code = ln[:ln.index(comment)].rstrip() if comment else ln.rstrip("\n")
        if depth == 0 and array_key is None:
            mo = _ARRAY_OPEN_RE.match(ln)
            if mo:
                array_key = mo.group(1)
        if marker and code and is_marker_line(marker) \
                and _marker_name(marker) == preset_name:
            rs = _parse_marker(marker)
            if rs is not None:
                if array_key in ("mounts", "setup") and depth > 0:
                    bare = code.strip()
                    if bare.endswith(","):
                        bare = bare[:-1].rstrip()
                    kind = "mount" if array_key == "mounts" else "setup"
                    identity = _element_identity(kind, bare)
                    if identity is not None:
                        items.append(_Stamped(
                            kind=kind, identity=identity, rev=rs[0], sha=rs[1],
                            current_sha=_sha12(bare), on_disk=bare,
                            edit_start=i, edit_end=i + 1, is_block=False))
                elif array_key is None and depth == 0:
                    # Strip the env code to its bare `KEY = "value"` before
                    # hashing/comparing: the stamp writes it unindented, so a
                    # cosmetic re-indent must not flip the block to skipped-edited
                    # (matches how inline mount/setup elements strip to bare). The
                    # round-trip is unaffected (a fresh stamp has no indent).
                    env_code = code.strip()
                    identity = _env_key(env_code)
                    if identity is not None:
                        items.append(_Stamped(
                            kind="env", identity=identity, rev=rs[0], sha=rs[1],
                            current_sha=_sha12(env_code), on_disk=env_code,
                            edit_start=i, edit_end=i + 1, is_block=False))
        depth = max(0, depth + _array_depth_delta(ln))
        if depth == 0:
            array_key = None
    return items


_MARKER_NAME_RE = re.compile(r"name=(\S+)")


def _marker_name(marker: str) -> str | None:
    m = _MARKER_NAME_RE.search(marker)
    return m.group(1) if m else None


def _element_identity(kind: str, bare: str) -> str | None:
    try:
        d = tomllib.loads(f"__x = {bare}")["__x"]
    except tomllib.TOMLDecodeError:
        return None
    if kind == "mount":
        t = d.get("target")
        return _norm_target(t) if isinstance(t, str) else None
    o = d.get("order")
    return str(o) if o is not None else None


def _env_key(code: str) -> str | None:
    try:
        d = tomllib.loads(code)
    except tomllib.TOMLDecodeError:
        return None
    return next(iter(d)) if len(d) == 1 else None


# ---- kept identity (placeholder / provider / secret) -------------------------


def _kept_credential(text: str, stamped_names: set[str], source: str):
    """Read the shared placeholder + provider/secret back from the workspace's
    stamped bindings for this preset (identity is preserved across a refresh,
    never regenerated). Returns `(provider, secret, placeholder, divergent)`:
    the first stamped binding's credential in file order, and `divergent=True`
    when the pack's stamped bindings DISAGREE on (provider, secret, placeholder)
    -- someone hand-edited them apart. On divergence there is no single shared
    credential to reuse, so the caller must NOT silently pick the first and
    rotate the siblings (that would break placeholder-consuming state); it reports
    the divergence and rotates nothing. `(None, None, None, False)` when the pack
    stamped no bindings (pure-rule / pure-container)."""
    raw = tomllib.loads(text)
    bindings = _bindings_with_auto_names(_parse_bindings(raw, source))
    stamped = [b for b in bindings if b.name in stamped_names]
    if not stamped:
        return None, None, None, False
    first = stamped[0]
    divergent = len({(b.provider, b.secret, b.placeholder)
                     for b in stamped}) > 1
    return first.provider, first.secret, first.placeholder, divergent


# ---- rendering a definition item's would-be on-disk text ---------------------


def _render_defn(kind: str, obj, *, is_block: bool) -> str:
    """The exact on-disk text a fresh stamp of `obj` produces -- the value the
    marker `sha` covers. Compared byte-for-byte against the located block's
    on-disk text (so a freshly-stamped-then-refreshed block is up-to-date) and
    spliced in on an `updated`. `is_block` selects the block vs. inline-element
    form for a mounts/setup item (matching the located item's form)."""
    if kind == "binding":
        return _render_binding_block(obj)[1:]        # drop the leading blank line
    if kind == "rule":
        return _render_rule_block(obj)[1:]
    if kind == "env":
        return _render_env_line(obj[0], obj[1])
    if kind == "mount":
        return _render_mount_block(obj)[1:] if is_block else _render_mount_inline(obj)
    if kind == "setup":
        return _render_setup_block(obj)[1:] if is_block else _render_setup_inline(obj)
    raise AssertionError(kind)  # pragma: no cover


# ---- classification ----------------------------------------------------------


# Ordered kinds so `[[binding]]` blocks are reported before container-half items.
_KINDS = ("binding", "rule", "env", "mount", "setup")


def _definition_map(exp) -> dict[str, dict[str, object]]:
    """Map each kind to `{identity: definition object}` for the CURRENT
    expansion (built with the KEPT credential/placeholder). Bindings/rules join
    by name, env by key, mounts by target, setup by order."""
    return {
        "binding": {b.name: b for b in exp.bindings},
        "rule": {r.name: r for r in exp.rules},
        "env": {k: (k, v) for k, v in exp.env},
        "mount": {_norm_target(m.target): m for m in exp.mounts},
        "setup": {str(s["order"]): s for s in exp.setup},
    }


# ---- the engine --------------------------------------------------------------


@dataclass
class _Plan:
    """The classification of one refresh pass: the per-block actions plus the
    concrete edits (in-place replacements/deletions) and additions needed to
    realize them. Pure data -- `refresh_preset` turns it into text."""
    actions: list[RefreshAction]
    edits: list[tuple[int, int, list[str], bool]]
    add_bindings: list
    add_rules: list
    add_mounts: list
    add_env: list
    add_setup: list


def _classify(text: str, preset_name: str, spec: PresetSpec, *,
              prune: bool, source: str) -> _Plan:
    """Locate `preset_name`'s stamped blocks, re-expand its definition with the
    KEPT credential/placeholder, and classify every block three-ways (+ add /
    prune). Pure; produces a `_Plan`. Shared by `refresh_preset` (which applies
    it) and `_verify` (which re-runs it on the composed text to prove
    convergence), so the two can never disagree on the classification."""
    stamped = _locate(text, preset_name)

    # Fail closed (belt-and-suspenders behind the multiline-aware scan): two
    # located items sharing a (kind, identity) join key mean the scan mis-targeted
    # -- editing either would corrupt foreign bytes. Never write; make the operator
    # look.
    seen_ids: dict[tuple[str, str], _Stamped] = {}
    for s in stamped:
        key = (s.kind, s.identity)
        if key in seen_ids:
            raise ConfigError(
                f"preset '{preset_name}': two stamped {s.kind} items share the "
                f"identity {s.identity!r} (lines "
                f"{seen_ids[key].edit_start + 1} and {s.edit_start + 1}) -- refusing "
                f"to refresh, as an edit could target the wrong one. Resolve the "
                f"duplicate by hand (no changes were written)")
        seen_ids[key] = s

    stamped_binding_names = {s.identity for s in stamped if s.kind == "binding"}

    # `divergent`: the stamped bindings disagree on the shared credential (a hand
    # edit). The binding half is then skipped whole (below) -- never rotated -- but
    # the expansion is still built with the first binding's cred so the
    # credential-independent halves (rules, mounts/env/setup) classify normally.
    provider, secret, placeholder, divergent = _kept_credential(
        text, stamped_binding_names, source)
    if spec.needs_credential and provider is None:
        raise ConfigError(
            f"preset '{preset_name}' now declares bindings but the applied pack "
            f"has none to read a provider/secret from -- re-apply it with "
            f"`preset add {preset_name} --provider P --secret REF` instead of "
            f"refreshing")

    exp = build_preset(preset_name, provider, secret)
    if placeholder is not None:
        # Preserve the SHARED placeholder read back from the file (a
        # definition-new part inherits it too, since build_preset shares one).
        exp = replace(exp, bindings=tuple(
            replace(b, placeholder=placeholder) for b in exp.bindings))
    defn = _definition_map(exp)

    # Existing (current-file) identities, so an `added` item that would collide
    # with UNMANAGED config (a hand-declared mount target / env key) is reported
    # rather than blindly stamped into a duplicate.
    cfg = core_config.load_config_from_text(text, source)
    existing_targets = {_norm_target(m["target"]) for m in cfg["mounts"]}
    existing_env = dict(cfg["env"])

    stamped_by_kind: dict[str, dict[str, _Stamped]] = {k: {} for k in _KINDS}
    for s in stamped:
        stamped_by_kind[s.kind][s.identity] = s

    plan = _Plan([], [], [], [], [], [], [])
    for kind in _KINDS:
        smap = stamped_by_kind[kind]
        dmap = defn[kind]
        # Definition order first (stable, matches `preset list`), then any
        # stamped item without a definition counterpart (prune candidates).
        ordered = list(dmap) + [i for i in smap if i not in dmap]
        for ident in ordered:
            s = smap.get(ident)
            obj = dmap.get(ident)
            # A pack whose stamped bindings were hand-edited apart has no single
            # shared credential to rebuild from: skip the whole binding half (both
            # matched and definition-new bindings) rather than rotate siblings to
            # the first binding's placeholder. A prunable binding (no `obj`) is
            # credential-free to delete, so it falls through to the prune path.
            if kind == "binding" and divergent and obj is not None:
                plan.actions.append(
                    RefreshAction(kind, ident, "skipped-divergent"))
                continue
            if s is not None and obj is not None:
                want = _render_defn(kind, obj, is_block=s.is_block)
                # Check the edit state FIRST: a hand-edited block (recomputed sha
                # != marker sha) is ALWAYS skipped, even if the would-write text
                # happens to match on disk (e.g. the kept credential was read from
                # this very edited binding) -- otherwise the edit would be masked
                # as up-to-date and silently propagated to its siblings.
                if s.current_sha != s.sha:
                    diff = _unified_diff(s.on_disk, want, kind, ident) \
                        if s.on_disk != want else None
                    plan.actions.append(
                        RefreshAction(kind, ident, "skipped-edited", diff))
                elif s.on_disk == want:
                    plan.actions.append(RefreshAction(kind, ident, "up-to-date"))
                else:
                    plan.edits.append(_update_edit(s, want, preset_name, spec.rev))
                    plan.actions.append(RefreshAction(kind, ident, "updated"))
            elif obj is not None:
                # A definition item with no stamped counterpart -> add it, unless
                # it collides with UNMANAGED config (report, don't clobber).
                if kind == "mount" and ident in existing_targets:
                    plan.actions.append(
                        RefreshAction(kind, ident, "skipped-collision"))
                    continue
                if kind == "env":
                    key, val = obj
                    if key in existing_env:
                        act = "up-to-date" if existing_env[key] == val \
                            else "skipped-collision"
                        plan.actions.append(RefreshAction(kind, ident, act))
                        continue
                    plan.add_env.append(obj)
                elif kind == "binding":
                    plan.add_bindings.append(obj)
                elif kind == "rule":
                    plan.add_rules.append(obj)
                elif kind == "mount":
                    plan.add_mounts.append(obj)
                elif kind == "setup":
                    plan.add_setup.append(obj)
                plan.actions.append(RefreshAction(kind, ident, "added"))
            else:
                # Stamped, but the definition dropped it.
                if prune:
                    plan.edits.append((s.edit_start, s.edit_end, [], s.is_block))
                    plan.actions.append(RefreshAction(kind, ident, "pruned"))
                else:
                    plan.actions.append(RefreshAction(kind, ident, "prunable"))
    return plan


def refresh_preset(text: str, preset_name: str, spec: PresetSpec, *,
                   prune: bool, source: str) -> RefreshResult:
    """Re-expand `preset_name` against `spec` and return the composed new text
    plus the per-block actions. Pure w.r.t. disk. Raises `ConfigError` on a hard
    failure (a needs-credential pack whose stamped bindings vanished, an invalid
    composed result) so nothing is written."""
    plan = _classify(text, preset_name, spec, prune=prune, source=source)

    new_text = _apply(text.splitlines(keepends=True), plan.edits)
    try:
        # Additions ride the shared compose (re-parse + additive verify + one text).
        new_text = compose(
            new_text, preset_name, spec.rev,
            bindings=plan.add_bindings, rules=plan.add_rules,
            mounts=plan.add_mounts, env_items=plan.add_env,
            setup=[dict(s) for s in plan.add_setup])
        changed = new_text != text
        if changed:
            _verify(new_text, preset_name, spec, prune=prune, source=source)
    except ConfigError as e:
        # A pack that RENAMED a block (its old suffix vanished -> prunable, a new
        # suffix appeared -> added) leaves the old block in place without --prune,
        # so the new one collides with it (e.g. two bindings sharing a placeholder
        # on one host). That's a legible situation, not a credproxy bug -- name it.
        if not prune and _looks_like_rename(plan):
            raise ConfigError(
                f"preset '{preset_name}' looks like it renamed a block: a stamped "
                f"block vanished from the definition while a new one appeared, and "
                f"the new block collides with the old (still-present) one. Delete "
                f"the vanished block by hand and re-run, or refresh with --prune to "
                f"drop it. Underlying error: {e}") from e
        raise

    container_changed = any(
        a.kind in ("env", "mount", "setup")
        and a.action in ("updated", "added", "pruned")
        for a in plan.actions)
    return RefreshResult(
        preset=preset_name, actions=tuple(plan.actions), new_text=new_text,
        changed=changed, container_changed=container_changed)


def _looks_like_rename(plan: _Plan) -> bool:
    """True iff the plan both prunes and adds an item of the same kind -- the
    fingerprint of a pack renaming a block (the old suffix goes prunable, the new
    one added), which collides on compose/validate without --prune."""
    prunable = {a.kind for a in plan.actions if a.action == "prunable"}
    added = {a.kind for a in plan.actions if a.action == "added"}
    return bool(prunable & added)


def _update_edit(s: _Stamped, want: str, preset_name: str,
                 rev: str) -> tuple[int, int, list[str], bool]:
    """The in-place replacement for an `updated` item: a fresh provenance marker
    (new rev + recomputed sha over `want`) plus the freshly-rendered on-disk
    text, in the item's existing form. A block keeps its standalone marker line
    above a rebuilt body; an env key / inline element keeps its `code  # marker`
    single-line shape (2-space indent + trailing comma preserved for an
    element)."""
    marker = _marker(preset_name, rev, want)
    if s.is_block:
        return (s.edit_start, s.edit_end, [marker + "\n", want], False)
    if s.kind in ("mount", "setup"):
        return (s.edit_start, s.edit_end, [f"  {want},  {marker}\n"], False)
    return (s.edit_start, s.edit_end, [f"{want}  {marker}\n"], False)


def _apply(lines: list[str],
           edits: list[tuple[int, int, list[str], bool]]) -> str:
    """Apply in-place line-range edits to `lines` (a keepends buffer). Applied
    high-index-first so earlier splices don't shift later ranges. A block
    deletion also drops one preceding blank separator (so repeated refreshes
    don't accumulate blank lines), mirroring `remove_binding`."""
    for start, end, repl, drop_blank in sorted(edits, key=lambda e: e[0],
                                               reverse=True):
        lo = start
        if not repl and drop_blank and lo > 0 and lines[lo - 1].strip() == "":
            lo -= 1
        lines[lo:end] = repl
    return "".join(lines)


def _unified_diff(on_disk: str, want: str, kind: str, ident: str) -> str:
    """A unified diff of the on-disk (hand-edited) block against what refresh
    WOULD write -- shown for a `skipped-edited` block so the operator can
    reconcile by hand (or delete + re-refresh)."""
    label = f"{kind} {ident}"
    a = on_disk if on_disk.endswith("\n") else on_disk + "\n"
    b = want if want.endswith("\n") else want + "\n"
    return "".join(difflib.unified_diff(
        a.splitlines(keepends=True), b.splitlines(keepends=True),
        fromfile=f"{label} (on disk)", tofile=f"{label} (refresh)"))


def _verify(new_text: str, preset_name: str, spec: PresetSpec, *,
            prune: bool, source: str) -> None:
    """Guard the write: the composed text must (1) re-parse + re-validate as a
    full workspace config, and (2) CONVERGE -- classifying the result again must
    surface no further `updated`/`added`/`prunable` work, proving every intended
    edit landed and nothing was corrupted. Any mismatch raises, so the caller
    writes nothing."""
    try:
        raw = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"preset refresh produced invalid TOML ({e}); no changes were "
            f"written")
    # Full re-validation (same parse/validate `start` runs): binding/rule name
    # collisions, script resolution, mount set, env/setup shapes.
    core_config.load_config_from_text(new_text, source)
    core_bindings.validate(
        _bindings_with_auto_names(_parse_bindings(raw, source)), source)
    core_rules.validate(
        _rules_with_auto_names(_parse_rules(raw, source)), source)

    # Convergence: re-classify the just-written text (classification only, no
    # recursion into the composing/verifying path). If we pruned, a lingering
    # `prunable` means a deletion missed; either way, `updated`/`added` residue
    # means an edit didn't land.
    again = _classify(new_text, preset_name, spec, prune=prune, source=source)
    residual = [a for a in again.actions
                if a.action in ("updated", "added")
                or (prune and a.action in ("pruned", "prunable"))]
    if residual:
        detail = ", ".join(f"{a.kind} {a.target}:{a.action}" for a in residual)
        raise ConfigError(
            f"preset refresh did not converge (residual: {detail}); this is a "
            f"credproxy bug -- no changes were written")
