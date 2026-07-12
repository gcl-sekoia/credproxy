"""The `preset` noun's workspace-scoped verbs (add/refresh/remove) plus the shared
preset-reference helpers: option/credential resolution, block rendering, the
minted-snapshot summary projection, the newly-intercepted advisory, and the
`[[preset]]` block regexes."""
from __future__ import annotations

import argparse
import re
import tomllib

from ..core.engine import docker as core_docker
from . import render
from .render import fail, say
from .common import Ctx, _resolve_ws, _require_exists, _confirm_destructive
from .cmd_binding import _parse_secret_args

# The `[[preset]]` header + `[preset.<child>]` sub-table regexes are the SINGLE
# whitespace-tolerant source in `core.model.presets` (shared with `remove_preset`
# so the two block-span readers can never diverge on a spaced-dot child and
# orphan/corrupt it -- the #62/#63 child-table class). Imported, never duplicated.
from ..core.model.presets import (
    _PRESET_HEADER_RE as _PRESET_BLOCK_RE,
    _PRESET_CHILD_RE,
)
# A `[[preset]]` header OR an inline `preset =` assignment, for detecting preset
# references in a template we can't fully parse.
_PRESET_REF_RE = re.compile(r"^\s*(\[\[\s*preset\s*\]\]|preset\s*=)", re.M)


def _newly_intercepted(existing_hosts, new_hosts) -> list[str]:
    """Hosts the preset newly flips to TLS-intercepted: `new_hosts` not already
    covered by `existing_hosts` (a literal already named, or matched by an
    existing glob). Adding a rule to a previously-passthrough host intercepts it
    (the UNION intercept set), so `preset add` announces this rather than letting
    the operator discover a fresh CA-cert error."""
    from ..core.model import hostmatch
    existing_lowered = {h.lower() for h in existing_hosts}
    globs = [hostmatch.compile_pattern(h.lower())
             for h in existing_hosts if hostmatch.is_pattern(h)]
    out, seen = [], set()
    for h in new_hosts:
        hl = h.lower()
        if hl in seen:
            continue
        seen.add(hl)
        already = hl in existing_lowered or (
            not hostmatch.is_pattern(h) and any(g.fullmatch(hl) for g in globs))
        if not already:
            out.append(h)
    return out


def _render_preset_ref_block(name: str, provider, secret, options: dict) -> str:
    """Render a `[[preset]]` reference block (leading blank line) with the RESOLVED
    provider/secret/options written explicitly -- what `preset add` / `create`
    append. `disable`/`override` are hand-edit-only, so never rendered here. The
    expansion itself is never written to the TOML (it lives in the lock)."""
    from ..core.model.bindings import _toml_key, _toml_str
    lines = ["", "[[preset]]", f"name     = {_toml_str(name)}"]
    if provider is not None:
        lines.append(f"provider = {_toml_str(provider)}")
    if isinstance(secret, dict):
        inner = ", ".join(f"{_toml_key(s)} = {_toml_str(r)}"
                          for s, r in secret.items())
        lines.append(f"secret   = {{ {inner} }}")
    elif secret is not None:
        lines.append(f"secret   = {_toml_str(secret)}")
    if options:
        lines.append("[preset.options]")
        for k, v in options.items():
            rendered = ("true" if v else "false") if isinstance(v, bool) \
                else _toml_str(str(v))
            lines.append(f"{_toml_key(k)} = {rendered}")
    return "\n".join(lines) + "\n"


def _append_preset_ref(text: str, name: str, provider, secret,
                       options: dict) -> str:
    """Append a `[[preset]]` reference block at EOF (array-of-tables merge in file
    order, like `[[binding]]`)."""
    if text and not text.endswith("\n"):
        text += "\n"
    return text + _render_preset_ref_block(name, provider, secret, options)


def _expansion_summary(name: str, entry: dict) -> dict:
    """Render-ready announce dict for a minted preset snapshot (`entry` is the
    lock's `presets[name]`). Reads the intent-level `expansion` dicts."""
    exp = entry["expansion"]

    def _mount(m: dict) -> dict:
        for k in ("overlay", "volume", "bind"):
            if k in m:
                return {"kind": k, "source": m[k], "target": m["target"]}
        return {"kind": "?", "source": "", "target": m.get("target", "")}

    return {
        "preset": name,
        "bindings": [{"name": b["name"], "injector": b["injector"],
                      "provider": b.get("provider"), "secret": b.get("secret"),
                      "hosts": list(b.get("hosts", [])),
                      "placeholder": b.get("placeholder"),
                      "env": (None if b.get("env") is False else b.get("env"))}
                     for b in exp["bindings"]],
        "rules": [{"name": r["name"], "hosts": list(r.get("hosts", [])),
                   "action": r.get("action"), "script": r.get("script"),
                   "visible": r.get("visible", r.get("action") in
                              ("block", "respond"))}
                  for r in exp["rules"]],
        "mounts": [_mount(m) for m in exp["mounts"]],
        "env": [{"key": k, "value": v} for k, v in exp["env"].items()],
        "setup": [dict(s) for s in exp["setup"]],
        "has_container_half": bool(exp["mounts"] or exp["env"] or exp["setup"]),
    }


def _parse_opt_flags(opts: list[str] | None) -> dict:
    """Parse repeatable `--opt id=value` flags into `{id: value}` (values are raw
    strings; coercion against each option's type happens in `resolve_options`). A
    later `--opt` for the same id wins. Malformed (`no '='`, empty id) fails."""
    out: dict = {}
    for raw in opts or []:
        if "=" not in raw:
            fail(f"--opt expects id=value, got {raw!r}")
        key, val = raw.split("=", 1)
        key = key.strip()
        if not key:
            fail(f"--opt expects a non-empty id, got {raw!r}")
        out[key] = val
    return out


def _resolve_preset_option_values(ctx: Ctx, spec, explicit: dict) -> dict:
    """Resolve every pack `[[option]]` to a value in the settled order (#59):
    explicit (`--opt`/template `[preset.options]`) -> prompt (loose+TTY only) ->
    default -> structured fail. Returns `{id: value}` (empty for an option-less
    pack). Raises `PresetOptionsError` (structured under `--json`) when a required
    option can't be resolved without prompting."""
    if not spec.options:
        # An explicit --opt for a pack that declares no options is a typo worth
        # surfacing (resolve_options rejects unknown ids), so still route through it.
        if not explicit:
            return {}
    from ..core.errors import PresetOptionsError
    from ..core.model.presets import option_summary, resolve_options
    from . import prompt as prompt_mod

    ask = prompt_mod.ask_option if prompt_mod.prompting_enabled(ctx) else None
    values, missing = resolve_options(spec, explicit, prompt=ask)
    if missing:
        raise PresetOptionsError(spec.name, [option_summary(o) for o in missing])
    return values


def _resolve_preset_credential_interactive(
        ctx: Ctx, spec, provider_arg, secret_arg, *, on_missing):
    """Resolve a pack's provider/secret with the shared defaulting, THEN prompt on
    loose+TTY for anything still missing (decision 4: provider picker + secret with
    a validate-at-prompt loop). Strict / loose-no-TTY don't prompt -- `on_missing`
    (a callable taking the `missing` list) fires instead, rendering the caller's
    own structured/human error. Returns `(provider, secret)`."""
    from ..core.model.presets import preset_slot_set, resolve_preset_credential
    from . import prompt as prompt_mod

    provider, secret, missing = resolve_preset_credential(
        spec, provider_arg, secret_arg)
    if missing and prompt_mod.prompting_enabled(ctx):
        if "provider" in missing:
            provider = prompt_mod.ask_provider(spec.default_provider)
            # Re-apply defaulting now the provider is known (a prompted provider
            # equal to default_provider makes default_secret eligible).
            provider, secret, missing = resolve_preset_credential(
                spec, provider, secret_arg)
        # The single-string secret prompt only makes sense for the single-slot
        # `value` sugar. A multi-slot / named-slot pack (#71) can't be prompted a
        # single value, so it falls closed to explicit `--secret SLOT=REF` flags
        # (multi-slot prompting is punted) -- leave it in `missing`.
        if "secret" in missing and preset_slot_set(spec) == ("value",):
            hint_default = (spec.default_secret
                            if provider == spec.default_provider else None)
            secret = prompt_mod.ask_secret(provider, hint_default)
            missing = [m for m in missing if m != "secret"]
    if missing:
        on_missing(missing)   # renders + exits (fail / raise)
    return provider, secret


def do_preset_add(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    """Apply a preset as a service setup pack: append a durable `[[preset]]`
    REFERENCE (with the resolved provider/secret/options written explicitly) to the
    workspace TOML, then resolve to mint the expansion snapshot into the lock. The
    proxy never sees a "preset"; the resolver expands the reference. A pure-rule /
    pure-container preset needs no provider/secret."""
    from ..core.model.presets import (
        apply_option_values, get_preset, parse_preset_refs, preset_slot_set,
    )
    from ..core.providers import find_provider

    spec = get_preset(a.preset)               # CredproxyError -> clean fail on unknown

    # Pack options (#59): resolved before the credential so a bad --opt fails fast.
    option_values = _resolve_preset_option_values(
        ctx, spec, _parse_opt_flags(a.opt))

    provider = secret = None
    if spec.needs_credential:
        # The pack's shared secret slot set (#71): parts all couple one credential,
        # so their injectors must agree on slots. Parse `--secret` with it, so a
        # single `--secret SLOT=REF` for a named/multi slot is recognized (not
        # swallowed as a bare `value` ref).
        slots = preset_slot_set(spec)
        secret_arg = _parse_secret_args(a.secret, slots)

        def _missing(missing):
            if "provider" in missing:
                fail("preset '%s' has bindings but no default provider -- pass "
                     "--provider" % a.preset)
            if slots == ("value",):
                fail("`preset add` needs --secret (its meaning depends on "
                     "--provider)")
            fail("`preset add` needs `--secret SLOT=REF` for each slot: "
                 f"{', '.join(slots)}")

        provider, secret = _resolve_preset_credential_interactive(
            ctx, spec, a.provider, secret_arg, on_missing=_missing)
        find_provider(provider)
    elif a.provider or a.secret:
        shape = "container-only (mounts/env/setup)" if spec.has_container_half \
            else "pure-rule"
        fail(f"preset '{a.preset}' is a {shape} pack with no bindings -- it "
             f"needs no credential, so --provider/--secret don't apply")

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    from ..core.errors import CredproxyError
    from ..core.model import config as core_config
    from ..core.model.lock import save_lock
    from ..core.model.resolver import resolve_workspace
    from ..core.paths import atomic_write_text

    source = str(ws.config_path)
    # Requires (#58) aren't expanded into the model, so substitute their option
    # markers here (the literal spec) for the advisory prereq run below.
    literal_spec = apply_option_values(spec, option_values) if spec.options else spec

    with ws.lock():                          # atomic read-validate-write
        text = ws.config_path.read_text()

        # Double-add guard: a `[[preset]]` reference for this pack already present.
        if a.preset in {r.name for r in parse_preset_refs(tomllib.loads(text), source)}:
            fail(f"preset '{a.preset}' is already referenced here "
                 f"(a `[[preset]]` block names it); edit that block instead")

        # An attached workspace has no credproxy-managed container -> refuse a
        # container-half pack (binding/rule-only packs still apply).
        attached = core_config.load_config_from_text(
            text, source, check_bind_exists=False).get("attach") is not None
        if attached and spec.has_container_half:
            fail(f"preset '{a.preset}' carries container-half config "
                 f"(mounts/env/setup), but the workspace is attached -- its "
                 f"container is managed externally. Only binding/rule-only packs "
                 f"apply to an attached workspace.")

        # Hosts already covered, for the newly-intercepted advisory (resolve the
        # pre-add config).
        before = resolve_workspace(ws)
        before_hosts = [h for b in before.bindings for h in b.hosts] \
            + [h for r in before.rules for h in r.hosts]

        # Append the reference, then resolve to validate + mint the snapshot.
        # Roll back the (hand-owned) TOML on any resolve failure so a bad ref never
        # half-writes it.
        atomic_write_text(
            ws.config_path,
            _append_preset_ref(text, a.preset, provider, secret, option_values))
        try:
            resolved = resolve_workspace(ws)
        except CredproxyError as e:
            atomic_write_text(ws.config_path, text)
            fail(str(e))
        for n in resolved.notes:
            say(f"note: {n}")
        save_lock(ws, resolved.lock)

    entry = resolved.lock["presets"][a.preset]
    announce = _expansion_summary(a.preset, entry)
    exp_hosts = [h for b in announce["bindings"] for h in b["hosts"]] \
        + [h for r in announce["rules"] for h in r["hosts"]]
    newly = _newly_intercepted(before_hosts, exp_hosts)
    has_container_half = announce["has_container_half"]

    # `preset add` is otherwise a pure config edit, so a missing/unreachable
    # docker must not fail it: if we can't check, assume no container.
    container_exists = False
    if not attached and has_container_half:
        try:
            container_exists = \
                core_docker.container_status(ws.ws_container) is not None
        except CredproxyError:
            container_exists = False

    # Host-prerequisite checks (#58): advisory here -- the reference already landed
    # (durable config), so a failing check reports + hints but never fails the add.
    # The `provider` check only needs ONE ref to prove the provider serves the
    # credential, so normalize a multi-slot `{slot: ref}` secret (#71) to its first
    # ref -- matching `doctor`'s `_secret_ref`, and keeping the value hashable for
    # prereqs' `(provider, secret)` dedup key.
    from ..core.model import prereqs
    probe_secret = secret if isinstance(secret, str) or secret is None \
        else next(iter(secret.values()), None)
    requires = [prereqs.summary(r) for r in prereqs.evaluate(
        literal_spec.requires, provider=provider, secret=probe_secret,
        do_fetch=True)]
    render.OUT.preset_applied(
        ws.name, a.preset, announce["bindings"], announce["rules"],
        newly, mounts=announce["mounts"], env=announce["env"],
        setup=announce["setup"],
        recreate=(container_exists and has_container_half),
        attached=attached, requires=requires)


def do_preset_refresh(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    """Re-expand the workspace's `[[preset]]` reference(s) from their CURRENT
    definitions and diff against the locked snapshots. `--check` previews without
    writing. A refresh that would introduce a collision fails atomically (nothing
    written). The shared placeholder is preserved (never rotated)."""
    from ..core.errors import CredproxyError
    from ..core.model import preset_refresh
    from ..core.model.lock import save_lock
    from ..core.model.resolver import resolve_workspace

    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    # Read-modify-write under the workspace lock so the compute and the save are
    # atomic -- a concurrent mutation between them can't be silently clobbered
    # (mirrors do_binding_remove; the gate prompt is held under the lock, which is
    # fine for this single-user tool).
    with ws.lock():
        # "before" model (from the current lock) for the newly-intercepted advisory.
        before = resolve_workspace(ws)
        before_hosts = [h for b in before.bindings for h in b.hosts] \
            + [h for r in before.rules for h in r.hosts]

        result = preset_refresh.compute_refresh(ws, a.preset)  # a.preset may be None

        # Safety gate: a non-`--check` refresh can change the effective config
        # wholesale, so gate it like the destructive set -- but ONLY when there's a
        # real change AND the workspace is implicitly targeted on the loose surface.
        if not a.check and result.changed:
            _confirm_destructive(ctx, ws, implicit, "refresh presets of")

        written = not a.check and result.dirty
        if written:
            save_lock(ws, result.new_lock)

    for n in result.resolved.notes:
        say(f"note: {n}")

    after_hosts = [h for b in result.resolved.bindings for h in b.hosts] \
        + [h for r in result.resolved.rules for h in r.hosts]
    newly = _newly_intercepted(before_hosts, after_hosts)

    attached = result.resolved.config.get("attach") is not None
    # `--check` is a pure preview -- nothing was written, so a "restart to apply"
    # hint would be false (it would apply the OLD snapshot). Suppress the docker
    # probe AND the hint in check mode (recreate stays False when container_exists
    # is).
    container_exists = False
    if not a.check and not attached and result.container_half_changed:
        try:
            container_exists = \
                core_docker.container_status(ws.ws_container) is not None
        except CredproxyError:
            container_exists = False

    render.OUT.preset_refreshed(
        ws.name, [p.to_dict() for p in result.presets], check=a.check,
        newly_intercepted=newly,
        recreate=(container_exists and result.container_half_changed),
        attached=attached, written=written)


def do_preset_remove(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    """Remove a `[[preset]]` reference block (+ its `[preset.options]`/
    `[preset.override.*]` child sub-tables) and drop its lock snapshot. Reports
    what leaves the effective model + whether hosts stop being intercepted."""
    from ..core.errors import CredproxyError
    from ..core.model.lock import load_lock
    from ..core.model.presets import parse_preset_refs, remove_preset
    from ..core.model.resolver import resolve_workspace

    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    # The ONLY hard preconditions are: the pack is referenced (`parse_preset_refs`
    # needs no pack definition, so a dangling ref still parses) + the destructive
    # gate. Everything downstream is best-effort REPORTING -- `preset remove` must
    # succeed exactly when removal is the fix (a literal-vs-preset collision, or a
    # dangling ref whose pack was deleted and inputs since edited), mirroring
    # `remove_binding`/`do_binding_remove`'s resolution-free design.
    refs = {r.name for r in parse_preset_refs(
        tomllib.loads(ws.config_path.read_text()), str(ws.config_path))}
    if a.preset not in refs:
        fail(f"preset '{a.preset}' is not referenced in workspace '{ws.name}'")

    _confirm_destructive(ctx, ws, implicit, "remove preset from")

    # The removed pack's contribution (report + host advisory) comes from its LOCK
    # snapshot, read directly -- NOT via a full resolve, which hard-fails on an
    # unrelated collision or a dangling ref (precisely the states where removal is
    # the fix).
    entry = load_lock(ws).get("presets", {}).get(a.preset)
    summary = _expansion_summary(a.preset, entry) if entry else {
        "bindings": [], "rules": [], "mounts": [], "env": [], "setup": [],
        "has_container_half": False}
    removed_hosts = [h for b in summary["bindings"] for h in b["hosts"]] \
        + [h for r in summary["rules"] for h in r["hosts"]]

    # `attach` (push-hint wording) from a best-effort resolve -- if the model won't
    # resolve, default to managed (the common case) rather than fail.
    attached = False
    try:
        attached = resolve_workspace(ws).config.get("attach") is not None
    except CredproxyError:
        pass

    with ws.lock():
        remove_preset(ws, a.preset)

    # After removal: which removed hosts stop being intercepted? Best-effort -- if
    # the REMAINING model is independently broken, the mutation already landed, so
    # skip the advisory rather than error on a completed remove.
    no_longer: list[str] = []
    try:
        after = resolve_workspace(ws)
        after_hosts = [h for b in after.bindings for h in b.hosts] \
            + [h for r in after.rules for h in r.hosts]
        no_longer = _newly_intercepted(after_hosts, removed_hosts)
    except CredproxyError:
        pass

    container_exists = False
    if not attached and summary["has_container_half"]:
        try:
            container_exists = \
                core_docker.container_status(ws.ws_container) is not None
        except CredproxyError:
            container_exists = False

    render.OUT.preset_removed(
        ws.name, a.preset, summary["bindings"], summary["rules"], no_longer,
        mounts=summary["mounts"], env=summary["env"], setup=summary["setup"],
        recreate=(container_exists and summary["has_container_half"]),
        attached=attached)
