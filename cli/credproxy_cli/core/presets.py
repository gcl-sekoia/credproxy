"""Presets: CLI-side generators that emit a coordinated *service setup pack* --
the bindings a credential needs across a service's hosts AND the credential-free
guardrails (rules) that should accompany them.

The binding half packages the multi-binding shape a single credential needs --
e.g. a GitHub PAT is `bearer` on api.github.com but HTTP `basic` on github.com /
ghcr.io, sharing ONE bare-token placeholder. The rule half ships policy: an org
overlay's `readonly-guard.star` wired to its hosts/params in one `preset add`.
Either half may be empty: a credential-only preset (`[[part]]` only) or a
pure-rule policy pack (`[[rule]]` only, no `[placeholder]`/provider/secret).

A preset is pure host-side config **expansion, not a link**: it stamps ordinary
`[[binding]]` + `[[rule]]` blocks; the proxy never sees a "preset", and
editing/removing the stamped blocks afterwards is normal.

Presets are *data*, loaded from the layered registry (user > profile overlay >
builtin, paths.layered_dirs) -- a `<name>.toml` per preset, the name being the
filename stem. So an org adds its own packs by dropping a TOML in its profile
overlay, no code. See docs/forking.md and builtin/presets/github.toml.
"""
from __future__ import annotations

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


@dataclass(frozen=True)
class PresetSpec:
    name: str
    # The shared, service-shaped sentinel -- None for a pure-rule preset (nothing
    # to couple: rules carry no placeholder).
    placeholder: Placeholder | None
    parts: tuple[_Part, ...]
    rules: tuple[_PresetRule, ...] = ()
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
        a pure-rule pack needs none."""
        return bool(self.parts)


def _parse_preset(path, name: str) -> PresetSpec:
    src = f"preset '{name}' ({path})"
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{src}: unreadable ({e})")

    parts_raw = raw.get("part") or []
    rules_raw = raw.get("rule") or []
    if not isinstance(parts_raw, list):
        raise ConfigError(f"{src}: [[part]] must be an array of tables")
    if not isinstance(rules_raw, list):
        raise ConfigError(f"{src}: [[rule]] must be an array of tables")
    if not parts_raw and not rules_raw:
        raise ConfigError(f"{src}: needs at least one [[part]] or [[rule]]")

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

    return PresetSpec(
        name=name,
        placeholder=placeholder,
        parts=tuple(parts),
        rules=tuple(rules),
        default_provider=raw.get("default_provider"),
        default_secret=raw.get("default_secret"),
    )


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
    """All resolvable presets keyed by name, user shadowing profile shadowing
    builtin (least-specific first so the most-specific overwrites)."""
    seen: dict[str, PresetSpec] = {}
    for _source, base in reversed(layered_dirs("presets")):
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.suffix == ".toml" and path.is_file():
                seen[path.stem] = _parse_preset(path, path.stem)
    return seen


def load_preset_sources() -> dict[str, str]:
    """Map each resolvable preset name to the tier it resolves from
    ("user"/"profile"/"builtin"), mirroring load_presets' shadowing. Presets
    don't carry source on the spec (unlike the other registries), so this is the
    diagnostics seam (e.g. `info`'s per-tier counts)."""
    src: dict[str, str] = {}
    for source, base in reversed(layered_dirs("presets")):
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.suffix == ".toml" and path.is_file():
                src[path.stem] = source
    return src


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
    applying. No secret/provider -- those are supplied at `preset add` time."""
    return [
        {
            "name": spec.name,
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
        }
        for spec in sorted(load_presets().values(), key=lambda s: s.name)
    ]


def build_preset(preset: str, provider: str | None = None,
                 secret: str | None = None) -> tuple[list[Binding], list["core_rules.Rule"]]:
    """Expand `preset` into its `(bindings, rules)`. All bindings share one
    freshly-generated placeholder and resolve the same single-slot `secret` ref
    via `provider` (both None for a pure-rule preset). Each rule's `name` is
    filled to `<preset>-<suffix>`. Raises CredproxyError on an unknown preset."""
    spec = get_preset(preset)
    placeholder = spec.placeholder.generate() if spec.placeholder else None
    bindings = [
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
    ]
    rules = [replace(pr.rule, name=f"{spec.name}-{pr.suffix}") for pr in spec.rules]
    return bindings, rules
