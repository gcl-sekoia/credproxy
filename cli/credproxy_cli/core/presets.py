"""Presets: CLI-side generators that emit a coordinated *service setup pack* --
the bindings a credential needs across a service's hosts AND the credential-free
guardrails (rules) that should accompany them.

The binding half packages the multi-binding shape a single credential needs --
e.g. a GitHub PAT is `bearer` on api.github.com but HTTP `basic` on github.com /
ghcr.io, sharing ONE bare-token placeholder. The rule half ships policy: an
overlay's `readonly-guard.star` wired to its hosts/params in one `preset add`.
Either half may be empty: a credential-only preset (`[[part]]` only) or a
pure-rule policy pack (`[[rule]]` only, no `[placeholder]`/provider/secret).

A preset is pure host-side config **expansion, not a link**: it stamps ordinary
`[[binding]]` + `[[rule]]` blocks; the proxy never sees a "preset", and
editing/removing the stamped blocks afterwards is normal.

Presets are *data*, loaded from the layered registry (user > overlays >
builtin, paths.layered_dirs) -- a `<name>.toml` per preset, the name being the
filename stem. So an org adds its own packs by dropping a TOML in an overlay, no
code. See docs/advanced/overlays.md and builtin/presets/github.toml.
"""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, replace

from . import rules as core_rules
from .bindings import Binding
from .errors import ConfigError, CredproxyError, InjectorError
from .injectors import Placeholder, validate_placeholder
from .paths import layered_dirs


@dataclass(frozen=True)
class _Part:
    suffix: str             # appended to the preset's base name
    injector: str           # injector / scheme to use
    hosts: tuple[str, ...]
    env: str | None


@dataclass(frozen=True)
class _PresetRule:
    suffix: str             # appended to the preset's base name (like _Part)
    rule: "core_rules.Rule"  # a validated Rule with name=None (filled at build)


# The prerequisite check kinds a pack may DECLARE. Each is implemented by core
# (host-side, read-only) -- a pack never supplies shell (`core/prereqs.py`).
_REQUIRE_KINDS = ("path", "command", "env", "provider")


@dataclass(frozen=True)
class _Require:
    """One declarative `[[requires]]` host-prerequisite check. `kind` selects the
    check; exactly one of the per-kind payload fields is set (`path`/`command`/
    `var`); `fetch` is provider-only (test-fetch the secret, not just resolve the
    provider). `hint` is the operator remedy shown on failure."""
    kind: str
    path: str | None = None       # kind == "path"
    command: str | None = None    # kind == "command"
    var: str | None = None        # kind == "env"
    fetch: bool = False           # kind == "provider"
    hint: str | None = None


@dataclass(frozen=True)
class _PresetMount:
    """One preset `[[mount]]`, in stamp-ready form. `value` is what gets stamped
    into the workspace TOML for `kind`: a tier-QUALIFIED overlay rel
    (`tier:setup.d/x.sh`, pinned to the pack's owning tier), a volume name, or a
    literal host-bind source (baked v1 default, existence-checked at `start`, not
    here). `readonly` is None when the pack didn't declare it (the stamp omits it,
    load applies the per-kind default)."""
    kind: str                    # "overlay" | "volume" | "bind"
    value: str
    target: str
    readonly: bool | None
    user_owned: bool = False


def mount_table(pm: _PresetMount) -> dict:
    """Reconstruct the raw mount TABLE from a `_PresetMount`, for re-normalizing
    through `config._parse_mount` at add time (the merged-mount validation) and
    for rendering the stamped inline table."""
    t: dict = {pm.kind: pm.value, "target": pm.target}
    if pm.readonly is not None:
        t["readonly"] = pm.readonly
    if pm.user_owned:
        t["user_owned"] = True
    return t


@dataclass(frozen=True)
class PresetSpec:
    name: str
    # The shared, service-shaped sentinel -- None for a preset with no bindings
    # (a pure-rule or pure-container pack; nothing to couple).
    placeholder: Placeholder | None
    parts: tuple[_Part, ...]
    rules: tuple[_PresetRule, ...] = ()
    # The container-half a pack may ALSO carry (stamped as ordinary literal
    # config, expansion-not-a-link): managed mounts, env vars, ordered setup
    # steps. Any of the five (parts/rules/mounts/env/setup) may be empty; the
    # whole preset may not be.
    mounts: tuple[_PresetMount, ...] = ()
    env: tuple[tuple[str, str], ...] = ()      # ordered (key, value) pairs
    setup: tuple[dict, ...] = ()               # {"run", "user", "order"} dicts
    # Declarative host-prerequisite checks (#58): NOT stamped into the workspace
    # (host state, not config) -- checked (advisory) at `preset add`/`create` and
    # (authoritative) at `doctor` time. Ordered as declared.
    requires: tuple[_Require, ...] = ()
    # first-12-hex of sha256 over the preset DEFINITION FILE bytes, for the
    # provenance marker (`rev=`); the pack files are pinned to a tier, this pins
    # the stamp to a pack revision.
    rev: str = ""
    # A canonical source so the common case needs no flags. `default_provider`
    # fills an omitted `--provider`. `default_secret` fills an omitted `--secret`
    # but ONLY when the resolved provider is `default_provider` -- a secret ref's
    # meaning is provider-specific (a gh hostname is not an env-var name nor an
    # op:// path), so it can't be defaulted for an arbitrary provider.
    default_provider: str | None = None
    default_secret: str | None = None

    @property
    def needs_credential(self) -> bool:
        """A preset with bindings needs a provider/secret (and a placeholder);
        a pure-rule / pure-container pack needs none."""
        return bool(self.parts)

    @property
    def has_container_half(self) -> bool:
        """True iff the pack stamps any container-half config (mounts/env/setup)
        -- the half an ATTACHED workspace can't accept and that drifts the spec
        hash (triggering a recreate)."""
        return bool(self.mounts or self.env or self.setup)


def _tier_qualifier(source_label: str) -> str:
    """The mount-source TIER qualifier for a `layered_dirs` tier label: an overlay
    label `overlay:<base>` -> `<base>`; the literal tiers `user`/`builtin` stay.
    Mirrors `config._tier_roots`, the resolution side.

    An overlay whose basename is a reserved tier literal (`user`/`builtin`) would
    shadow that tier's qualifier for EVERY pack it holds -- a pack's own
    `overlay="rel"` mount would qualify as `user:rel`/`builtin:rel` and resolve
    against the WRONG root (silently the user config dir / builtin, not the
    overlay). It's unambiguously broken, so it's rejected here (the seam that
    turns a label into a qualifier)."""
    if not source_label.startswith("overlay:"):
        return source_label
    base = source_label.split(":", 1)[1]
    if base in ("user", "builtin"):
        tier_name = "XDG user config" if base == "user" else "builtin"
        raise ConfigError(
            f"overlay directory named {base!r} shadows the reserved {base!r} "
            f"tier qualifier (the {tier_name} tier) -- rename the overlay "
            f"directory to something else")
    return base


def _parse_preset(path, name: str, tier: str = "builtin") -> PresetSpec:
    src = f"preset '{name}' ({path})"
    try:
        data = path.read_bytes()
        raw = tomllib.loads(data.decode())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{src}: unreadable ({e})")
    rev = hashlib.sha256(data).hexdigest()[:12]

    parts_raw = raw.get("part") or []
    rules_raw = raw.get("rule") or []
    mounts_raw = raw.get("mount") or []
    env_raw = raw.get("env") or {}
    setup_raw = raw.get("setup") or []
    requires_raw = raw.get("requires") or []
    if not isinstance(parts_raw, list):
        raise ConfigError(f"{src}: [[part]] must be an array of tables")
    if not isinstance(rules_raw, list):
        raise ConfigError(f"{src}: [[rule]] must be an array of tables")
    if not isinstance(mounts_raw, list):
        raise ConfigError(f"{src}: [[mount]] must be an array of tables")
    if not isinstance(env_raw, dict):
        raise ConfigError(f"{src}: [env] must be a table")
    if not isinstance(setup_raw, list):
        raise ConfigError(f"{src}: [[setup]] must be an array of tables")
    if not isinstance(requires_raw, list):
        raise ConfigError(f"{src}: [[requires]] must be an array of tables")
    if not (parts_raw or rules_raw or mounts_raw or env_raw or setup_raw):
        # `[[requires]]` alone is not a pack -- there'd be nothing to stamp, so
        # the checks would guard config that was never written.
        raise ConfigError(
            f"{src}: needs at least one [[part]], [[rule]], [[mount]], [env], "
            f"or [[setup]]")

    # [placeholder] is the BINDING coupling mechanism, required only when the
    # preset carries bindings; a pure-rule pack has nothing to couple.
    ph = raw.get("placeholder")
    if parts_raw:
        if not isinstance(ph, dict):
            raise ConfigError(f"{src}: missing [placeholder] table "
                              f"(required when the preset has [[part]] bindings)")
        # Validate through the shared injector path so a bad charset or a length
        # <= prefix (zero-entropy, non-unique placeholder) fails HERE, not as a
        # KeyError in generate() or a silently-broken sentinel at build time.
        try:
            placeholder = validate_placeholder(ph, src)
        except InjectorError as e:
            raise ConfigError(str(e)) from e
    else:
        if ph is not None:
            raise ConfigError(f"{src}: [placeholder] is meaningless without "
                              f"[[part]] bindings (rules carry no placeholder)")
        placeholder = None

    parts = []
    for i, pr in enumerate(parts_raw):
        where = f"{src} part[{i}]"
        if not isinstance(pr, dict):
            raise ConfigError(f"{where}: must be a table")
        suffix, injector = pr.get("suffix"), pr.get("injector")
        hosts = pr.get("hosts")
        if not isinstance(suffix, str) or not suffix:
            raise ConfigError(f"{where}: 'suffix' must be a non-empty string")
        if not isinstance(injector, str) or not injector:
            raise ConfigError(f"{where}: 'injector' must be a non-empty string")
        if not isinstance(hosts, list) or not hosts \
                or not all(isinstance(h, str) and h for h in hosts):
            raise ConfigError(f"{where}: 'hosts' must be a non-empty array of strings")
        env = pr.get("env")
        if env is not None and (not isinstance(env, str) or not env):
            raise ConfigError(f"{where}: 'env' must be a non-empty string or absent")
        parts.append(_Part(suffix=suffix, injector=injector,
                           hosts=tuple(hosts), env=env))

    rules = [_parse_preset_rule(r, i, src) for i, r in enumerate(rules_raw)]
    mounts = [_parse_preset_mount(m, i, src, tier)
              for i, m in enumerate(mounts_raw)]
    env = _parse_preset_env(env_raw, src)
    setup = [_parse_preset_setup(s, i, src) for i, s in enumerate(setup_raw)]
    requires = [_parse_preset_require(r, i, src, has_parts=bool(parts_raw))
                for i, r in enumerate(requires_raw)]

    return PresetSpec(
        name=name,
        placeholder=placeholder,
        parts=tuple(parts),
        rules=tuple(rules),
        mounts=tuple(mounts),
        env=tuple(env),
        setup=tuple(setup),
        requires=tuple(requires),
        rev=rev,
        default_provider=raw.get("default_provider"),
        default_secret=raw.get("default_secret"),
    )


def _parse_preset_require(r, i: int, src: str, *, has_parts: bool) -> _Require:
    """One preset `[[requires]]` entry -> a `_Require`. `kind` selects the check
    and dictates which single payload field is required; unknown keys are
    rejected (mirroring the other per-section validators). A `provider` check on
    a pack with no `[[part]]` bindings is a definition error (nothing to fetch),
    and `fetch` is provider-only."""
    where = f"{src} requires[{i}]"
    if not isinstance(r, dict):
        raise ConfigError(f"{where}: must be a table")
    kind = r.get("kind")
    if kind not in _REQUIRE_KINDS:
        raise ConfigError(
            f"{where}: 'kind' must be one of {', '.join(_REQUIRE_KINDS)}, "
            f"got {kind!r}")

    # Per-kind required payload field + the full allowed-key set for this kind.
    field_by_kind = {"path": "path", "command": "command", "env": "var"}
    allowed = {"kind", "hint"}
    payload = {"path": None, "command": None, "var": None}
    fetch = False

    if kind == "provider":
        if not has_parts:
            raise ConfigError(
                f"{where}: a 'provider' check needs the pack to have [[part]] "
                f"bindings (there is nothing to resolve/fetch otherwise)")
        allowed.add("fetch")
        f = r.get("fetch", False)
        if not isinstance(f, bool):
            raise ConfigError(f"{where}: 'fetch' must be a boolean")
        fetch = f
    else:
        field = field_by_kind[kind]
        allowed.add(field)
        # `fetch` is provider-only -- a misplaced `fetch` on another kind is a
        # definition error (it would silently do nothing).
        if "fetch" in r:
            raise ConfigError(
                f"{where}: 'fetch' applies only to a 'provider' check, not "
                f"{kind!r}")
        val = r.get(field)
        if not isinstance(val, str) or not val:
            raise ConfigError(
                f"{where}: a {kind!r} check needs a non-empty '{field}' string")
        payload[field] = val

    hint = r.get("hint")
    if hint is not None and (not isinstance(hint, str) or not hint):
        raise ConfigError(f"{where}: 'hint' must be a non-empty string or absent")

    extra = sorted(set(r) - allowed)
    if extra:
        raise ConfigError(
            f"{where}: unknown key(s): {', '.join(extra)} "
            f"(allowed for kind={kind!r}: {', '.join(sorted(allowed))})")

    return _Require(kind=kind, path=payload["path"], command=payload["command"],
                    var=payload["var"], fetch=fetch, hint=hint)


def _parse_preset_mount(m, i: int, src: str, tier: str) -> _PresetMount:
    """One preset `[[mount]]` -> a `_PresetMount`. An unqualified `overlay` source
    is QUALIFIED with the pack's owning `tier` (so it resolves within THIS pack's
    tier, immune to overlay reorder/shadow) before being validated through the
    SHARED `config._parse_mount` (bind sources kept literal -- a baked v1 default
    checked at `start`, not here)."""
    from . import config as core_config
    where = f"{src} mount[{i}]"
    if not isinstance(m, dict):
        raise ConfigError(f"{where} must be a table")
    table = dict(m)
    ov = table.get("overlay")
    if isinstance(ov, str) and ":" not in ov:
        # A `#`-containing qualifier is a duplicate-basename overlay's dedup
        # label (`base#2`) -- ORDER-DEPENDENT (the suffix follows discovery
        # order), so pinning a pack's shipped file to it would silently break if
        # the overlay order changed. Never a real user/builtin tier (those hold
        # no `#`). Reject here, when we're about to bake the qualifier in.
        if "#" in tier:
            raise ConfigError(
                f"{where}: overlay directory basename {tier!r} yields an "
                f"order-dependent duplicate-basename tier qualifier (the "
                f"'#N' suffix follows overlay discovery order) -- give the "
                f"overlay a unique basename, or pin the source with an explicit "
                f"`tier:rel` qualifier")
        table["overlay"] = f"{tier}:{ov}"
    # Validate shape (exactly one of overlay/volume/bind, absolute target,
    # readonly bool, volume-name/user_owned rules) + resolve the overlay file.
    norm = core_config._parse_mount(table, where, expand_bind=False)
    kind = norm["kind"]
    return _PresetMount(
        kind=kind,
        value=table[kind],                 # qualified overlay rel / name / literal bind
        target=norm["target"],
        readonly=table.get("readonly"),    # None when the pack didn't declare it
        user_owned=bool(norm.get("user_owned")),
    )


def _parse_preset_env(env_raw: dict, src: str) -> list[tuple[str, str]]:
    """A preset `[env]` table -> ordered (key, value) pairs. Values must be
    non-empty strings (they stamp as `KEY = "value"`)."""
    out: list[tuple[str, str]] = []
    for k, v in env_raw.items():
        if not isinstance(k, str) or not k:
            raise ConfigError(f"{src}: [env] keys must be non-empty strings")
        if not isinstance(v, str) or not v:
            raise ConfigError(
                f"{src}: env.{k} must be a non-empty string, got {v!r}")
        out.append((k, v))
    return out


def _parse_preset_setup(s, i: int, src: str) -> dict:
    """One preset `[[setup]]` step -> a normalized `{"run", "user", "order"}`
    dict via the SHARED `config._parse_setup_table` -- with the extra pack rules
    that `order` is REQUIRED and a bare command string is REJECTED (the root
    string form is the workspace's escape hatch, never a pack's)."""
    from . import config as core_config
    where = f"{src} setup[{i}]"
    if isinstance(s, str):
        raise ConfigError(
            f"{where} a preset setup step must be a table "
            f'{{ run = "...", order = N }}, not a bare string')
    if not isinstance(s, dict):
        raise ConfigError(f"{where} must be a table")
    return core_config._parse_setup_table(s, where, require_order=True)


def _parse_preset_rule(entry, i: int, src: str) -> _PresetRule:
    """One preset `[[rule]]` -> a _PresetRule. Like `[[part]]`, it carries a
    `suffix` (expanding to `name = <preset>-<suffix>`), NOT a literal `name`;
    the rest is a standard rule table validated through the SAME
    `core.rules._parse_rule_entry` the load path and `rule add` use -- so a bad
    preset rule fails at preset load with the same errors (and inherits the
    CLI<->proxy validator mirror + #36's `[rule.params]` validation)."""
    # `where` is the location fragment; `src` is passed separately as the message
    # source (both to _parse_rule_entry and our own raises), so it must NOT be
    # baked into `where` too -- else _parse_rule_entry's `f"{source}: {where}..."`
    # would print the preset path twice.
    where = f"rule[{i}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{src}: {where} must be a table")
    suffix = entry.get("suffix")
    if not isinstance(suffix, str) or not suffix:
        raise ConfigError(f"{src}: {where} 'suffix' must be a non-empty string")
    if "name" in entry:
        raise ConfigError(f"{src}: {where} a preset rule uses 'suffix' (-> "
                          f"name '<preset>-<suffix>'), not a literal 'name'")
    fields = {k: v for k, v in entry.items() if k != "suffix"}
    try:
        rule = core_rules._parse_rule_entry(fields, src, where)
    except CredproxyError as e:
        raise ConfigError(str(e)) from e
    return _PresetRule(suffix=suffix, rule=rule)


def load_presets() -> dict[str, PresetSpec]:
    """All resolvable presets keyed by name, user shadowing overlays shadowing
    builtin (least-specific first so the most-specific overwrites)."""
    seen: dict[str, PresetSpec] = {}
    for source, base in reversed(layered_dirs("presets")):
        if not base.is_dir():
            continue
        tier = _tier_qualifier(source)
        for path in sorted(base.iterdir()):
            if path.suffix == ".toml" and path.is_file():
                seen[path.stem] = _parse_preset(path, path.stem, tier)
    return seen


def _preset_provenance() -> tuple[dict[str, str], dict[str, list[str]]]:
    """One reversed walk mapping each resolvable preset name to (its winning tier
    label, the tier labels it shadows most-specific-first). Presets don't carry
    source on the spec (unlike the other registries), so this is the diagnostics
    seam (`info`'s per-tier counts, `preset list`'s shadow annotations)."""
    src: dict[str, str] = {}
    shadowed: dict[str, list[str]] = {}
    for source, base in reversed(layered_dirs("presets")):
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.suffix == ".toml" and path.is_file():
                if path.stem in src:
                    shadowed.setdefault(path.stem, []).append(src[path.stem])
                src[path.stem] = source
    return src, {n: list(reversed(losers)) for n, losers in shadowed.items()}


def load_preset_sources() -> dict[str, str]:
    """Map each resolvable preset name to the tier label it resolves from."""
    return _preset_provenance()[0]


def get_preset(name: str) -> PresetSpec:
    presets = load_presets()
    spec = presets.get(name)
    if spec is None:
        raise CredproxyError(
            f"unknown preset {name!r}; known presets: "
            f"{', '.join(sorted(presets)) or '(none)'}"
        )
    return spec


def describe_presets() -> list[dict]:
    """Structured description of every known preset, for `preset list`: the
    bindings AND rules it expands to, so an operator sees the full stamp before
    applying. No secret/provider -- those are supplied at `preset add` time. Each
    row carries its resolved tier label (`source`) and the tiers it `shadows`."""
    sources, shadows = _preset_provenance()
    return [
        {
            "name": spec.name,
            "source": sources.get(spec.name, ""),
            "shadows": shadows.get(spec.name, []),
            "needs_credential": spec.needs_credential,
            "bindings": [
                {
                    "name": f"{spec.name}-{part.suffix}",
                    "injector": part.injector,
                    "hosts": list(part.hosts),
                    "env": part.env,
                }
                for part in spec.parts
            ],
            "rules": [
                {
                    "name": f"{spec.name}-{pr.suffix}",
                    "hosts": list(pr.rule.hosts),
                    "action": pr.rule.action,
                    "script": pr.rule.script,
                    "visible": pr.rule.effective_visible,
                }
                for pr in spec.rules
            ],
            "mounts": [
                {"kind": m.kind, "source": m.value, "target": m.target}
                for m in spec.mounts
            ],
            "env": [{"key": k, "value": v} for k, v in spec.env],
            "setup": [dict(s) for s in spec.setup],
            "requires": [require_summary(rq) for rq in spec.requires],
        }
        for spec in sorted(load_presets().values(), key=lambda s: s.name)
    ]


def require_summary(rq: _Require) -> dict:
    """A JSON-clean summary of one `[[requires]]` check (for `preset list` and
    the requires-result rendering). Only the payload field for its kind is
    carried, plus `fetch` for a provider check."""
    out: dict = {"kind": rq.kind, "hint": rq.hint}
    if rq.kind == "path":
        out["path"] = rq.path
    elif rq.kind == "command":
        out["command"] = rq.command
    elif rq.kind == "env":
        out["var"] = rq.var
    elif rq.kind == "provider":
        out["fetch"] = rq.fetch
    return out


@dataclass(frozen=True)
class TemplatePreset:
    """One `[[preset]]` entry declared in a `workspace.template.toml` /
    `workspace.attach.template.toml`, consumed and expanded at `create` time (it
    never survives into the stamped `<name>.toml` -- the loader rejects `preset`
    in a workspace config). Mirrors the `preset add` inputs: the pack `name`, an
    optional `provider`, and an optional single `secret` ref."""
    name: str
    provider: str | None
    secret: str | None


def parse_template_presets(raw: dict, source: str) -> list[TemplatePreset]:
    """Extract + validate the `[[preset]]` entries from a rendered template's
    parsed TOML. `source` labels error messages. Returns [] when there are none.
    Fields: `name` (required non-empty string), `provider` / `secret` (optional
    non-empty strings, `secret` a single ref like `preset add`'s one `--secret`).
    Unknown keys are rejected."""
    entries_raw = raw.get("preset")
    if entries_raw is None:
        return []
    if not isinstance(entries_raw, list):
        raise ConfigError(f"{source}: `[[preset]]` must be an array of tables")
    allowed = {"name", "provider", "secret"}
    out: list[TemplatePreset] = []
    for i, e in enumerate(entries_raw):
        where = f"{source}: preset[{i}]"
        if not isinstance(e, dict):
            raise ConfigError(f"{where} must be a table")
        extra = sorted(set(e) - allowed)
        if extra:
            raise ConfigError(
                f"{where} unknown key(s): {', '.join(extra)} "
                f"(allowed: name, provider, secret)")
        name = e.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where} 'name' must be a non-empty string")
        provider = e.get("provider")
        if provider is not None and (not isinstance(provider, str) or not provider):
            raise ConfigError(f"{where} 'provider' must be a non-empty string")
        secret = e.get("secret")
        if secret is not None and (not isinstance(secret, str) or not secret):
            raise ConfigError(f"{where} 'secret' must be a non-empty string")
        out.append(TemplatePreset(name=name, provider=provider, secret=secret))
    return out


def resolve_preset_credential(
    spec: PresetSpec, provider: str | None, secret: str | None,
) -> tuple[str | None, str | None, list[str]]:
    """Apply a preset's provider/secret DEFAULTS -- the single source of truth
    shared by `preset add` and template-declared `[[preset]]` expansion (#57), so
    the two never diverge on how a pack's `default_provider`/`default_secret` fill
    an omitted flag/field.

    Returns `(provider, secret, missing)`: the resolved values plus the list of
    still-unresolved REQUIRED fields (`["provider"]`/`["secret"]`), empty when
    complete or when the pack needs no credential. `default_secret` fills an
    omitted secret ONLY when the resolved provider equals `default_provider` (a
    ref's meaning is provider-specific). The caller renders its own error from
    `missing` (add points at flags; create points at the template entry)."""
    if not spec.needs_credential:
        return None, None, []
    provider = provider or spec.default_provider
    missing: list[str] = []
    if provider is None:
        missing.append("provider")
    if secret is None:
        if provider is not None and provider == spec.default_provider \
                and spec.default_secret is not None:
            secret = spec.default_secret
        else:
            missing.append("secret")
    return provider, secret, missing


@dataclass(frozen=True)
class PresetExpansion:
    """A preset expanded for stamping: the ordinary blocks/config it writes into
    a workspace TOML. `rev` (the definition-file digest) rides every provenance
    marker. Mounts/env/setup are the container half."""
    name: str
    rev: str
    bindings: tuple[Binding, ...]
    rules: tuple["core_rules.Rule", ...]
    mounts: tuple[_PresetMount, ...]
    env: tuple[tuple[str, str], ...]
    setup: tuple[dict, ...]

    @property
    def has_container_half(self) -> bool:
        return bool(self.mounts or self.env or self.setup)


def build_preset(preset: str, provider: str | None = None,
                 secret: str | None = None) -> PresetExpansion:
    """Expand `preset` into a `PresetExpansion`. All bindings share one
    freshly-generated placeholder and resolve the same single-slot `secret` ref
    via `provider` (both None for a pack with no bindings). Each rule's `name` is
    filled to `<preset>-<suffix>`. Mounts/env/setup carry through verbatim.
    Raises CredproxyError on an unknown preset."""
    spec = get_preset(preset)
    placeholder = spec.placeholder.generate() if spec.placeholder else None
    bindings = tuple(
        Binding(
            name=f"{spec.name}-{part.suffix}",
            injector=part.injector,
            provider=provider,
            secret=secret,
            hosts=part.hosts,
            placeholder=placeholder,
            env=part.env,
        )
        for part in spec.parts
    )
    rules = tuple(replace(pr.rule, name=f"{spec.name}-{pr.suffix}")
                  for pr in spec.rules)
    return PresetExpansion(
        name=spec.name, rev=spec.rev, bindings=bindings, rules=rules,
        mounts=spec.mounts, env=spec.env, setup=spec.setup,
    )
