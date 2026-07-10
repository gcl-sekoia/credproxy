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
from ..errors import ConfigError, CredproxyError, InjectorError
from .injectors import Placeholder, validate_placeholder
from ..paths import layered_dirs


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
    provider). `hint` is the operator remedy shown on failure.

    `path_option` (path-check only) names a pack `[[option]]` supplying the whole
    `path` value via a `{ option = "id" }` marker (#59): `path` is then None on
    the definition spec and filled with the resolved literal by
    `apply_option_values` before the check runs. Requires are NOT stamped into the
    workspace config, so a refresh/doctor recovers this option's value from a
    STAMPED field that shares it (a mount source); an option feeding ONLY a
    requires path is unrecoverable and that check degrades to skip-with-note."""
    kind: str
    path: str | None = None       # kind == "path"
    command: str | None = None    # kind == "command"
    var: str | None = None        # kind == "env"
    fetch: bool = False           # kind == "provider"
    hint: str | None = None
    path_option: str | None = None  # kind == "path", `path = { option = "id" }`


@dataclass(frozen=True)
class _PresetMount:
    """One preset `[[mount]]`, in stamp-ready form. `value` is what gets stamped
    into the workspace TOML for `kind`: a tier-QUALIFIED overlay rel
    (`tier:setup.d/x.sh`, pinned to the pack's owning tier), a volume name, or a
    literal host-bind source (baked v1 default, existence-checked at `start`, not
    here). `readonly` is None when the pack didn't declare it (the stamp omits it,
    load applies the per-kind default).

    `source_option` (#59) names a pack `[[option]]` supplying the whole `value`
    via a `{ option = "id" }` marker on the `bind`/`volume` source. It is set only
    on the DEFINITION spec (where `value` is ""); `apply_option_values` substitutes
    the resolved literal and clears it, producing the literal spec that stamps.
    Overlay sources can't take an option (they're tier-qualified at pack-definition
    time), and container-half fields (`target`) never take one."""
    kind: str                    # "overlay" | "volume" | "bind"
    value: str
    target: str
    readonly: bool | None
    user_owned: bool = False
    source_option: str | None = None


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


_OPTION_TYPES = ("string", "enum", "bool")


@dataclass(frozen=True)
class _Option:
    """One pack `[[option]]` definition (#59): a whole-field parameter an operator
    supplies at expansion time (explicit `--opt id=value` / template
    `[preset.options]` -> prompt on loose+TTY -> `default` -> fail). `type` is
    `string`/`enum`/`bool`; `has_default` distinguishes "no default declared"
    (required) from a falsy default. `choices` is non-empty for `enum` only.
    `description` is the prompt/`preset list` blurb. Options parameterize
    HOST-HALF whole values only (a mount `bind`/`volume` source, a `[[requires]]`
    `path`) via a structural `{ option = "id" }` marker -- never a token inside a
    string (string interpolation is inexpressible by construction)."""
    id: str
    type: str
    has_default: bool
    default: object            # str | bool | None (None only when has_default is False)
    description: str | None
    choices: tuple[str, ...] = ()


def _option_marker(value, where: str) -> str | None:
    """If `value` is a whole-field option marker `{ option = "id" }`, return the
    option id; otherwise None. A dict that has an `option` key but a wrong shape
    (extra keys, or a non-string/empty id) is a definition error -- it was clearly
    MEANT as a marker, so we reject rather than silently treat it as a table."""
    if not isinstance(value, dict) or "option" not in value:
        return None
    if set(value) != {"option"}:
        extra = ", ".join(sorted(set(value) - {"option"}))
        raise ConfigError(
            f"{where}: an option marker is exactly `{{ option = \"id\" }}` -- "
            f"unexpected extra key(s): {extra}")
    oid = value["option"]
    if not isinstance(oid, str) or not oid:
        raise ConfigError(
            f"{where}: an option marker's `option` must be a non-empty string")
    return oid


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
    # Pack `[[option]]` definitions (#59): whole-field parameters resolved at
    # expansion time and substituted into the host-half markers (mount source /
    # requires path) BEFORE stamping. Empty () once resolved (`apply_option_values`
    # clears them on the literal spec).
    options: tuple[_Option, ...] = ()
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
    options_raw = raw.get("option") or []
    if not isinstance(options_raw, list):
        raise ConfigError(f"{src}: [[option]] must be an array of tables")
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

    # Options are parsed FIRST -- the mount/require parsers validate that every
    # `{ option = "id" }` marker names a defined option (and is type-appropriate).
    options = [_parse_preset_option(o, i, src) for i, o in enumerate(options_raw)]
    _reject_dup_join_keys([o.id for o in options], "option", "id", src)
    option_by_id = {o.id: o for o in options}

    rules = [_parse_preset_rule(r, i, src) for i, r in enumerate(rules_raw)]
    mounts = [_parse_preset_mount(m, i, src, tier, option_by_id)
              for i, m in enumerate(mounts_raw)]
    env = _parse_preset_env(env_raw, src)
    setup = [_parse_preset_setup(s, i, src) for i, s in enumerate(setup_raw)]
    requires = [_parse_preset_require(r, i, src, has_parts=bool(parts_raw),
                                      options=option_by_id)
                for i, r in enumerate(requires_raw)]

    # `order` (setup) and `target` (mount) are the JOIN KEYS `preset refresh`
    # re-classifies each stamped element by, so they must be UNIQUE within a pack
    # -- a duplicate would silently mis-join a refresh onto the wrong element.
    # Reject it here, at definition parse (a well-defined join key up front).
    _reject_dup_join_keys(
        [s["order"] for s in setup], "setup", "order", src)
    _reject_dup_join_keys(
        [_norm_mount_target(m.target) for m in mounts], "mount", "target", src)

    return PresetSpec(
        name=name,
        placeholder=placeholder,
        parts=tuple(parts),
        rules=tuple(rules),
        mounts=tuple(mounts),
        env=tuple(env),
        setup=tuple(setup),
        requires=tuple(requires),
        options=tuple(options),
        rev=rev,
        default_provider=raw.get("default_provider"),
        default_secret=raw.get("default_secret"),
    )


def _norm_mount_target(t: str) -> str:
    """Normalize a mount target the same way `preset refresh` joins on it (trailing
    slashes stripped), so `/opt/x` and `/opt/x/` count as the same target."""
    return t.rstrip("/") or "/"


def _reject_dup_join_keys(keys: list, kind: str, field: str, src: str) -> None:
    """Reject a duplicate join key across a pack's elements. `keys` is the ordered
    list of each element's `field` value (already normalized). A duplicate is a
    definition error -- `preset refresh` joins stamped elements to definition
    elements by this key, so it must be unique per pack."""
    seen: set = set()
    dups: list = []
    for k in keys:
        if k in seen and k not in dups:
            dups.append(k)
        seen.add(k)
    if dups:
        shown = ", ".join(repr(d) for d in dups)
        raise ConfigError(
            f"{src}: duplicate [[{kind}]] {field} ({shown}) -- each {kind} needs "
            f"a unique {field} (it is the join key `preset refresh` re-classifies "
            f"by)")


def _parse_preset_option(o, i: int, src: str) -> _Option:
    """One pack `[[option]]` -> a validated `_Option`. `id` required non-empty; `type`
    in {string, enum, bool}; `enum` needs a non-empty `choices` list of strings (and,
    if a default is present, it must be a member); a `bool` default must be a bool;
    a `string` default is optional. `description` is an optional non-empty string.
    Unknown keys are rejected (mirroring the other per-section validators)."""
    where = f"{src} option[{i}]"
    if not isinstance(o, dict):
        raise ConfigError(f"{where}: must be a table")
    oid = o.get("id")
    if not isinstance(oid, str) or not oid:
        raise ConfigError(f"{where}: 'id' must be a non-empty string")
    where = f"{src} option '{oid}'"
    otype = o.get("type")
    if otype not in _OPTION_TYPES:
        raise ConfigError(
            f"{where}: 'type' must be one of {', '.join(_OPTION_TYPES)}, "
            f"got {otype!r}")

    allowed = {"id", "type", "default", "description"}
    choices: tuple[str, ...] = ()
    if otype == "enum":
        allowed.add("choices")
        raw_choices = o.get("choices")
        if not isinstance(raw_choices, list) or not raw_choices \
                or not all(isinstance(c, str) and c for c in raw_choices):
            raise ConfigError(
                f"{where}: an 'enum' option needs a non-empty 'choices' array of "
                f"non-empty strings")
        if len(set(raw_choices)) != len(raw_choices):
            raise ConfigError(f"{where}: 'choices' has duplicate values")
        choices = tuple(raw_choices)

    has_default = "default" in o
    default: object = None
    if has_default:
        default = o["default"]
        if otype == "bool":
            if not isinstance(default, bool):
                raise ConfigError(f"{where}: a 'bool' option's default must be a "
                                  f"boolean, got {default!r}")
        elif otype == "enum":
            if not isinstance(default, str) or default not in choices:
                raise ConfigError(
                    f"{where}: default {default!r} is not one of the choices "
                    f"({', '.join(choices)})")
        else:  # string
            if not isinstance(default, str):
                raise ConfigError(f"{where}: a 'string' option's default must be a "
                                  f"string, got {default!r}")

    description = o.get("description")
    if description is not None and (not isinstance(description, str) or not description):
        raise ConfigError(f"{where}: 'description' must be a non-empty string or absent")

    extra = sorted(set(o) - allowed)
    if extra:
        raise ConfigError(
            f"{where}: unknown key(s): {', '.join(extra)} "
            f"(allowed for type={otype!r}: {', '.join(sorted(allowed))})")

    return _Option(id=oid, type=otype, has_default=has_default, default=default,
                   description=description, choices=choices)


def _require_stringlike_option(opt: _Option, where: str) -> None:
    """A `{ option = "id" }` marker sits only in a STRING-valued host field (a mount
    `bind`/`volume` source, a `[[requires]]` path). A `bool` option supplies no
    sensible whole value there, so referencing one is a definition error (string /
    enum options are fine -- both resolve to a string literal)."""
    if opt.type == "bool":
        raise ConfigError(
            f"{where}: option '{opt.id}' is a 'bool' option, which can't supply a "
            f"host path / source string; use a 'string' or 'enum' option there")


def _parse_preset_require(r, i: int, src: str, *, has_parts: bool,
                          options: dict) -> _Require:
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
    path_option: str | None = None

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
        raw_val = r.get(field)
        # A `path` may be supplied whole by an option (`path = { option = "id" }`);
        # the literal (and its absolute/`~`/`$`-root check) lands at
        # apply_option_values time. `command`/`env` fields take no option marker.
        oid = _option_marker(raw_val, f"{where} {field}")
        if oid is not None:
            if kind != "path":
                raise ConfigError(
                    f"{where}: an option marker is only supported on a 'path' "
                    f"check's 'path', not {kind!r}")
            if oid not in options:
                raise ConfigError(
                    f"{where}: option marker references undefined option {oid!r}")
            _require_stringlike_option(options[oid], where)
            path_option = oid
        else:
            val = raw_val
            if not isinstance(val, str) or not val:
                raise ConfigError(
                    f"{where}: a {kind!r} check needs a non-empty '{field}' string")
            if kind == "path" and not val.startswith(("~", "$")):
                # A bare relative path resolves against the CURRENT DIRECTORY, so
                # the same check would pass or fail depending on where `doctor`
                # runs -- nondeterministic. Require an absolute or `~`/`$VAR`-rooted
                # path (the latter resolves to absolute at check time, portable
                # even if the var is currently unset). Finding 7.
                import os
                if not os.path.isabs(os.path.expanduser(os.path.expandvars(val))):
                    raise ConfigError(
                        f"{where}: a 'path' check must be absolute or "
                        f"`~`/`$VAR`-rooted (got {val!r}, which resolves relative "
                        f"to the current directory -- nondeterministic across "
                        f"`doctor` runs)")
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
                    var=payload["var"], fetch=fetch, hint=hint,
                    path_option=path_option)


# Benign literal stand-ins substituted for an option-marker source so the shared
# `config._parse_mount` shape validation (kind exclusivity, target, allowed keys)
# still runs at definition parse; the real literal lands at apply_option_values.
_MOUNT_OPTION_DUMMY = {"bind": "/__credproxy_option__", "volume": "optplaceholder"}


def _parse_preset_mount(m, i: int, src: str, tier: str, options: dict) -> _PresetMount:
    """One preset `[[mount]]` -> a `_PresetMount`. An unqualified `overlay` source
    is QUALIFIED with the pack's owning `tier` (so it resolves within THIS pack's
    tier, immune to overlay reorder/shadow) before being validated through the
    SHARED `config._parse_mount` (bind sources kept literal -- a baked v1 default
    checked at `start`, not here).

    A `bind`/`volume` source may instead be a whole-field option marker
    `{ option = "id" }` (#59): the option's resolved literal is substituted by
    `apply_option_values` before stamping. An option marker in a container-half
    field (`target`) or on an `overlay` source is rejected -- options parameterize
    the host-half only, and overlay rels are tier-qualified at pack-definition
    time (an unresolved value can't be)."""
    from . import config as core_config
    where = f"{src} mount[{i}]"
    if not isinstance(m, dict):
        raise ConfigError(f"{where} must be a table")
    table = dict(m)

    # Detect + validate option markers. `target` (container half) and `overlay`
    # (tier-qualified) never take one; a `bind`/`volume` source may.
    source_option: str | None = None
    for key in list(table):
        oid = _option_marker(table[key], f"{where} {key}")
        if oid is None:
            continue
        if key == "target":
            raise ConfigError(
                f"{where}: 'target' is a container-half field -- an option marker "
                f"isn't allowed there (options parameterize a host-half mount "
                f"source only)")
        if key == "overlay":
            raise ConfigError(
                f"{where}: an option marker isn't supported on an 'overlay' source "
                f"(overlay rels are tier-qualified at pack-definition time); use a "
                f"'bind' or 'volume' source")
        if key not in ("bind", "volume"):
            raise ConfigError(f"{where}: an option marker isn't allowed on {key!r}")
        if oid not in options:
            raise ConfigError(
                f"{where}: option marker references undefined option {oid!r}")
        _require_stringlike_option(options[oid], where)
        source_option = oid
        # Substitute a benign literal so the shared shape validation still runs.
        table[key] = _MOUNT_OPTION_DUMMY[key]

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
    # An option-sourced mount carries an empty `value` on the DEFINITION spec; the
    # literal is filled by apply_option_values before stamping.
    return _PresetMount(
        kind=kind,
        value="" if source_option else table[kind],
        target=norm["target"],
        readonly=table.get("readonly"),    # None when the pack didn't declare it
        user_owned=bool(norm.get("user_owned")),
        source_option=source_option,
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
                {"kind": m.kind,
                 "source": (f"{{option={m.source_option}}}" if m.source_option
                            else m.value),
                 "target": m.target}
                for m in spec.mounts
            ],
            "env": [{"key": k, "value": v} for k, v in spec.env],
            "setup": [dict(s) for s in spec.setup],
            "requires": [require_summary(rq) for rq in spec.requires],
            "options": [option_summary(o) for o in spec.options],
            # Options no marker references (mount source / requires path). Purely
            # advisory for the pack AUTHOR (N6): such an option is inert -- its
            # value is prompted/defaulted but substituted nowhere. A `bool` option
            # is inherently here (host-half markers are string/path/ref, never a
            # bool), a documented gap: `bool` parses (per the locked spec) but has
            # no marker sink yet.
            "unreferenced_options": _unreferenced_option_ids(spec),
        }
        for spec in sorted(load_presets().values(), key=lambda s: s.name)
    ]


def _unreferenced_option_ids(spec: PresetSpec) -> list[str]:
    """Option ids no `{ option = "id" }` marker references (mount source or
    requires path). An unreferenced option is a likely pack-author mistake (its
    value is resolved but substituted nowhere) -- surfaced as a `preset list` note
    (N6). Declaration order preserved."""
    referenced = {m.source_option for m in spec.mounts if m.source_option}
    referenced |= {rq.path_option for rq in spec.requires if rq.path_option}
    return [o.id for o in spec.options if o.id not in referenced]


def option_summary(o: _Option) -> dict:
    """A JSON-clean summary of one `[[option]]` (for `preset list` + the structured
    missing-options error). `default` is present only when declared; `choices` only
    for an enum."""
    out: dict = {"id": o.id, "type": o.type, "description": o.description}
    if o.has_default:
        out["default"] = o.default
    if o.type == "enum":
        out["choices"] = list(o.choices)
    return out


def require_summary(rq: _Require) -> dict:
    """A JSON-clean summary of one `[[requires]]` check (for `preset list` and
    the requires-result rendering). Only the payload field for its kind is
    carried, plus `fetch` for a provider check."""
    out: dict = {"kind": rq.kind, "hint": rq.hint}
    if rq.kind == "path":
        # An option-fed path (`path = { option = "id" }`) carries no literal until
        # expansion; render the marker `{option=id}` (like a mount source does in
        # `describe_presets`) so `preset list` shows which option feeds it -- never
        # a bare `None` (N2).
        out["path"] = (f"{{option={rq.path_option}}}" if rq.path_option
                       else rq.path)
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
    optional `provider`, an optional single `secret` ref, and an optional
    `[preset.options]` sub-table (`{id = value}`) supplying pack option values
    (#59) -- the explicit-value channel `--opt id=value` is for at create time."""
    name: str
    provider: str | None
    secret: str | None
    options: dict = None  # {id: value}, from the `[preset.options]` sub-table


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
    allowed = {"name", "provider", "secret", "options"}
    out: list[TemplatePreset] = []
    for i, e in enumerate(entries_raw):
        where = f"{source}: preset[{i}]"
        if not isinstance(e, dict):
            raise ConfigError(f"{where} must be a table")
        extra = sorted(set(e) - allowed)
        if extra:
            raise ConfigError(
                f"{where} unknown key(s): {', '.join(extra)} "
                f"(allowed: name, provider, secret, options)")
        name = e.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where} 'name' must be a non-empty string")
        provider = e.get("provider")
        if provider is not None and (not isinstance(provider, str) or not provider):
            raise ConfigError(f"{where} 'provider' must be a non-empty string")
        secret = e.get("secret")
        if secret is not None and (not isinstance(secret, str) or not secret):
            raise ConfigError(f"{where} 'secret' must be a non-empty string")
        opts = e.get("options")
        if opts is not None and not isinstance(opts, dict):
            raise ConfigError(
                f"{where} 'options' must be a `[preset.options]` table of "
                f"`id = value` pairs")
        out.append(TemplatePreset(name=name, provider=provider, secret=secret,
                                  options=dict(opts) if opts else {}))
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


# ---- pack options: coerce / resolve / substitute (#59) -----------------------


def coerce_option_value(opt: _Option, raw, where: str):
    """Coerce+validate one raw option value (a string from `--opt id=value` /
    template `[preset.options]`, or an already-typed TOML value) against `opt`'s
    type. Idempotent for already-typed values (a bool stays a bool, an enum member
    stays). Raises ConfigError on a type/enum mismatch. `where` labels the error."""
    if opt.type == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "false"):
            return s == "true"
        raise ConfigError(
            f"{where}: option '{opt.id}' is a bool -- value must be true/false, "
            f"got {raw!r}")
    if opt.type == "enum":
        if not isinstance(raw, str) or raw not in opt.choices:
            raise ConfigError(
                f"{where}: option '{opt.id}' value {raw!r} is not one of the "
                f"choices ({', '.join(opt.choices)})")
        return raw
    # string
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ConfigError(
            f"{where}: option '{opt.id}' is a string, got {raw!r}")
    return raw


def resolve_options(spec: PresetSpec, explicit: dict, prompt=None) -> tuple[dict, list[_Option]]:
    """Resolve every pack option to a concrete value in the settled order
    (#59 decision 2): explicit (`--opt`/template `[preset.options]`) -> `prompt`
    (loose+TTY only; None disables it) -> declared `default` -> unresolved.

    Returns `(values, missing)`: `values` maps each RESOLVED option id to its
    coerced/typed value; `missing` is the ordered list of still-unresolved required
    `_Option`s (no explicit, no prompt, no default). An `explicit` key naming no
    defined option is a hard error (a typo the caller wants surfaced). The caller
    renders the structured missing error and, when `missing` is empty, feeds
    `values` to `build_preset(..., options=values)` / `apply_option_values`.

    `prompt(opt)` returns a coerced/validated value (the porcelain prompt does its
    own coercion), so a prompted value bypasses `coerce_option_value` here."""
    defined = {o.id for o in spec.options}
    unknown = sorted(set(explicit) - defined)
    if unknown:
        raise ConfigError(
            f"preset '{spec.name}': unknown option(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(defined)) or '(none)'})")
    values: dict = {}
    missing: list[_Option] = []
    for opt in spec.options:
        if opt.id in explicit:
            values[opt.id] = coerce_option_value(
                opt, explicit[opt.id], f"preset '{spec.name}' option")
        elif prompt is not None:
            values[opt.id] = prompt(opt)
        elif opt.has_default:
            values[opt.id] = opt.default
        else:
            missing.append(opt)
    return values, missing


def _finalize_option_values(spec: PresetSpec, values: dict) -> dict:
    """Fill every option from `values` (already resolved) or its declared default,
    coercing each. Raises ConfigError naming any option with neither -- the
    last-resort backstop for `build_preset`/`apply_option_values` (porcelain
    resolves + reports the structured missing error before reaching here)."""
    out: dict = {}
    missing: list[str] = []
    for opt in spec.options:
        if opt.id in values:
            out[opt.id] = coerce_option_value(
                opt, values[opt.id], f"preset '{spec.name}' option")
        elif opt.has_default:
            out[opt.id] = opt.default
        else:
            missing.append(opt.id)
    if missing:
        raise ConfigError(
            f"preset '{spec.name}': unresolved option(s): {', '.join(missing)} "
            f"(supply --opt id=value)")
    return out


def _substitute_mount_options(spec: PresetSpec, values: dict,
                              context: str = "add") -> PresetSpec:
    """Substitute resolved option values into the MOUNT source markers only,
    returning a spec whose mounts are literal. Only options a mount references need
    a value (explicit or default) -- an option feeding ONLY a `[[requires]]` path is
    irrelevant here (requires aren't part of the expansion), so `refresh`, which
    can read back a mount-feeding option but not a requires-only one, still builds.
    Each substituted source is re-validated through the shared `config._parse_mount`.

    `context` (`"add"` | `"refresh"`) flavors the unresolved-option remedy: at
    `add`/`create` an option value comes from `--opt`; at `refresh` there is no
    `--opt` flag and the value is read back from the stamped config (S3)."""
    from . import config as core_config

    new_mounts: list[_PresetMount] = []
    for m in spec.mounts:
        if m.source_option is None:
            new_mounts.append(m)
            continue
        literal = _resolve_one_option(spec, m.source_option, values, context)
        where = f"preset '{spec.name}' mount (option '{m.source_option}')"
        table = mount_table(replace(m, value=literal, source_option=None))
        core_config._parse_mount(table, where, expand_bind=False)
        new_mounts.append(replace(m, value=literal, source_option=None))
    return replace(spec, mounts=tuple(new_mounts))


def _resolve_one_option(spec: PresetSpec, oid: str, values: dict,
                        context: str = "add"):
    """The final value for option `oid`: `values[oid]` (coerced) else its declared
    default, else a ConfigError. `context` selects the remedy (see
    `_substitute_mount_options`): `add`/`create` point at `--opt`; `refresh` points
    at the read-back path (an option-fed mount was removed or its target changed)."""
    opt = next(o for o in spec.options if o.id == oid)
    if oid in values:
        return coerce_option_value(opt, values[oid], f"preset '{spec.name}' option")
    if opt.has_default:
        return opt.default
    if context == "refresh":
        raise ConfigError(
            f"preset '{spec.name}': could not recover option '{oid}' from the "
            f"stamped config (the option-fed mount was removed or its target "
            f"changed); re-add the pack (`preset add {spec.name} --opt "
            f"{oid}=...`) or restore the stamped mount")
    raise ConfigError(
        f"preset '{spec.name}': unresolved option '{oid}' (supply --opt {oid}=value)")


def apply_option_values(spec: PresetSpec, values: dict) -> PresetSpec:
    """Substitute resolved option `values` into every `{ option = "id" }` marker,
    returning a LITERAL spec (`options=()`, no markers left). Mount sources and
    requires paths become their whole literal value; each substituted value is
    re-validated in its field context (a mount source through the shared
    `config._parse_mount`, a requires path against the absolute/`~`/`$`-root rule).
    `values` must resolve every option (defaults folded via `_finalize_option_values`).

    This is the FULL substitution (mounts AND requires), used by `preset add` /
    `create` to build the literal spec whose `requires` feed the #58 prereq run.
    (`build_preset` uses the mount-only `_substitute_mount_options`, since the
    expansion never carries requires.)"""
    from . import config as core_config

    resolved = _finalize_option_values(spec, values)

    new_mounts: list[_PresetMount] = []
    for m in spec.mounts:
        if m.source_option is None:
            new_mounts.append(m)
            continue
        literal = resolved[m.source_option]  # a string (bind/volume source)
        where = f"preset '{spec.name}' mount (option '{m.source_option}')"
        table = mount_table(replace(m, value=literal, source_option=None))
        # Re-run the shared shape/charset validation now the source is literal.
        core_config._parse_mount(table, where, expand_bind=False)
        new_mounts.append(replace(m, value=literal, source_option=None))

    new_requires: list[_Require] = []
    for rq in spec.requires:
        if rq.path_option is None:
            new_requires.append(rq)
            continue
        literal = resolved[rq.path_option]
        _validate_require_path(literal, spec.name, rq.path_option)
        new_requires.append(replace(rq, path=literal, path_option=None))

    return replace(spec, mounts=tuple(new_mounts), requires=tuple(new_requires),
                   options=())


def resolve_requires_for_check(spec: PresetSpec, option_values: dict):
    """Resolve a stamped pack's `[[requires]]` for the authoritative `doctor` re-run
    (#59): substitute each `path = { option = "id" }` marker with its read-back
    value (`option_values`, recovered from stamped mounts) or the option's default.

    Returns `(resolved, skipped)`: `resolved` is the requires list with literal
    paths (option-less requires pass through unchanged); `skipped` is the requires
    whose option feeds ONLY the requires path (no stamped mount to read back) and
    has no default -- unrecoverable, so `doctor` degrades that check to
    skip-with-note rather than crash on a None path."""
    resolved: list[_Require] = []
    skipped: list[_Require] = []
    for rq in spec.requires:
        if rq.path_option is None:
            resolved.append(rq)
            continue
        opt = next((o for o in spec.options if o.id == rq.path_option), None)
        if opt is not None and rq.path_option in option_values:
            val = coerce_option_value(opt, option_values[rq.path_option],
                                      f"preset '{spec.name}' option")
        elif opt is not None and opt.has_default:
            val = opt.default
        else:
            skipped.append(rq)
            continue
        resolved.append(replace(rq, path=val, path_option=None))
    return resolved, skipped


def _validate_require_path(val: str, preset: str, opt_id: str) -> None:
    """A requires `path` supplied by an option must still be absolute or
    `~`/`$VAR`-rooted (the same determinism rule the literal form enforces at
    definition parse)."""
    if not val.startswith(("~", "$")):
        import os
        if not os.path.isabs(os.path.expanduser(os.path.expandvars(val))):
            raise ConfigError(
                f"preset '{preset}': option '{opt_id}' supplies requires path "
                f"{val!r}, which must be absolute or `~`/`$VAR`-rooted "
                f"(a relative path is nondeterministic across `doctor` runs)")


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
                 secret: str | None = None,
                 options: dict | None = None,
                 context: str = "add") -> PresetExpansion:
    """Expand `preset` into a `PresetExpansion`. All bindings share one
    freshly-generated placeholder and resolve the same single-slot `secret` ref
    via `provider` (both None for a pack with no bindings). Each rule's `name` is
    filled to `<preset>-<suffix>`. Mounts/env/setup carry through verbatim.
    Raises CredproxyError on an unknown preset.

    `options` (#59) maps resolved option ids to values; when the pack declares
    `[[option]]`s they are substituted into the host-half markers (mount source)
    BEFORE stamping, so the expansion is entirely literal. A missing value falls
    back to the option's default; an option with neither raises (porcelain resolves
    + reports the structured missing error before reaching here). `context`
    (`"add"` | `"refresh"`) flavors that unresolved-option remedy (S3: refresh has
    no `--opt` flag, so its remedy points at the read-back/re-add path)."""
    spec = get_preset(preset)
    if spec.options:
        # Mount-only substitution: the expansion carries no requires, so a
        # requires-only option (which `refresh` can't read back) is never needed.
        spec = _substitute_mount_options(spec, options or {}, context)
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
