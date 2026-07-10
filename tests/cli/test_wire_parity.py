"""Parity: every wire config the CLI's wire_config() emits must be accepted by
the proxy's load_resolved(). The CLI and proxy are separate deploy units (the
CLI can't import the proxy), so the wire contract can drift silently -- this
feeds REAL CLI output into the REAL proxy validator, per builtin injector.

proxy/config.py + schemes.py import on the host (no mitmproxy/aiohttp dep), the
same way tests/cli/test_scheme_catalog_drift.py reaches the proxy catalog.
Script schemes need the Starlark runtime (proxy image only), so they're covered
by the in-image tests/test_scripted_config.py and skipped here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _proxy_config():
    proxy_dir = str(Path(__file__).resolve().parents[2] / "proxy")
    if proxy_dir not in sys.path:
        sys.path.insert(0, proxy_dir)
    import config as proxy_config
    return proxy_config


def _minimal_binding(inj):
    """A minimal valid Binding using injector `inj`: one host, every slot filled
    with a ref, a placeholder for substitute schemes."""
    from credproxy_cli.core.model.bindings import Binding
    slots = inj.spec.slots
    secret = "ref" if (len(slots) == 1 and slots[0] == "value") \
        else {s: f"ref-{s}" for s in slots}
    placeholder = inj.placeholder.generate() if inj.spec.uses_placeholder else None
    return Binding(name=f"{inj.name}-b", injector=inj.name, provider="env",
                   secret=secret, hosts=("api.example.com",),
                   placeholder=placeholder, env=None)


def test_wire_config_round_trips_through_proxy(xdg):
    """For every builtin built-in injector, CLI wire_config -> proxy
    load_resolved with no error (catches wire-contract drift between units)."""
    from credproxy_cli.core.model.bindings import wire_config
    from credproxy_cli.core.model.injectors import list_injectors
    proxy_config = _proxy_config()

    def fake_fetch(provider, refs):
        return {r: f"val-{r}" for r in refs}

    builtins = [d for d in list_injectors() if d.scheme != "script"]
    # Sanity: the families we expect are present (so this isn't vacuously empty).
    assert {d.name for d in builtins} >= {
        "bearer", "basic", "body", "sigv4", "oauth2-reseal"}
    for inj in builtins:
        wire = wire_config([_minimal_binding(inj)], fetch_many=fake_fetch)
        try:
            proxy_config.load_resolved(wire)  # raises ConfigError on drift
        except Exception as e:  # noqa: BLE001 - surface which injector drifted
            pytest.fail(f"injector {inj.name!r} ({inj.scheme}) wire config rejected "
                        f"by proxy load_resolved: {type(e).__name__}: {e}")


def test_proxy_validator_is_not_a_noop(xdg):
    """The parity assertion is only meaningful if load_resolved actually rejects
    a malformed wire config."""
    proxy_config = _proxy_config()
    with pytest.raises(Exception):
        proxy_config.load_resolved({"bindings": [{"name": "x"}]})  # missing fields


def test_pathmatch_parity():
    """The CLI's pathmatch mirror must translate path globs byte-for-byte like
    the proxy's, or `rule test` disagrees with the real matcher."""
    from credproxy_cli.core.model import pathmatch as cli_pathmatch
    proxy_dir = str(Path(__file__).resolve().parents[2] / "proxy")
    if proxy_dir not in sys.path:
        sys.path.insert(0, proxy_dir)
    import rules as proxy_rules
    for glob in ["/repos/**", "/v1/models", "/users/*/repos", "/a", "/x/**/y",
                 "/p.a-t_h/*", "/"]:
        assert cli_pathmatch.path_to_regex(glob) == proxy_rules.path_to_regex(glob)
        assert cli_pathmatch.validate_path(glob) == proxy_rules.validate_path(glob)


def test_rule_wire_config_round_trips_through_proxy(xdg):
    """Declarative rule wire entries the CLI emits must be accepted by the proxy
    validator (script rules need the Starlark runtime, covered in-image)."""
    from credproxy_cli.core.model.rules import Rule, rule_wire_entries
    proxy_config = _proxy_config()

    rules = [
        Rule(name="blk", hosts=("api.github.com",), action="block",
             methods=("DELETE",), path="/repos/**"),
        Rule(name="stub", hosts=("api.openai.com",), action="respond",
             path="/v1/models", status=200, body="{}",
             headers={"Content-Type": "application/json"}),
        Rule(name="rw", hosts=("api.example.com",), action="rewrite",
             set_headers={"X-Env": "sandbox"}, remove_headers=("X-Id",)),
    ]
    wire = {"bindings": [], "rules": rule_wire_entries(rules)}
    try:
        creds = proxy_config.load_resolved(wire)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"rule wire config rejected by proxy load_resolved: "
                    f"{type(e).__name__}: {e}")
    assert {r.name for r in creds.rule_set().all()} == {"blk", "stub", "rw"}


def test_summarize_wire_matches_proxy_get_projection(xdg):
    """#66 field contract: the CLI's summarize_wire projection of a config must
    EQUAL the proxy's GET /admin/config projection (config.sanitized_live_config)
    of that same pushed config. Both deploy units derive the {name,hosts,scheme,
    placeholder,env} / {name,hosts,action,visible} shape independently, so this
    feeds real CLI output through the real proxy loader + projection and asserts
    byte-parity -- a one-sided field change breaks it."""
    from credproxy_cli.core.model.injectors import list_injectors
    from credproxy_cli.core.model.rules import Rule
    from credproxy_cli.core.model.wire import summarize_wire
    proxy_config = _proxy_config()

    def fake_fetch(provider, refs):
        return {r: f"val-{r}" for r in refs}

    # A representative mix: substitute (bearer/basic/body), sign (sigv4), re-seal
    # (oauth2-reseal) -- everything but script (needs Starlark, image only). Each
    # binding gets a DISTINCT host so unconditional (sign-family) writers don't
    # collide on a shared (host, location).
    from credproxy_cli.core.model.bindings import Binding
    injectors = {d.name: d for d in list_injectors() if d.scheme != "script"}

    def _binding_on(inj, host):
        slots = inj.spec.slots
        secret = "ref" if (len(slots) == 1 and slots[0] == "value") \
            else {s: f"ref-{s}" for s in slots}
        placeholder = inj.placeholder.generate() if inj.spec.uses_placeholder else None
        return Binding(name=f"{inj.name}-b", injector=inj.name, provider="env",
                       secret=secret, hosts=(host,),
                       placeholder=placeholder, env=None)

    bindings = [
        _binding_on(injectors[n], f"api{i}.example.com")
        for i, n in enumerate(("bearer", "basic", "body", "sigv4", "oauth2-reseal"))
    ]
    rules = [
        Rule(name="blk", hosts=("api.github.com",), action="block",
             methods=("DELETE",), path="/repos/**"),
        Rule(name="rw", hosts=("api.example.com",), action="rewrite",
             set_headers={"X-Env": "sandbox"}),          # default-hidden
        Rule(name="stub", hosts=("api.openai.com",), action="respond",
             status=200, body="{}", visible=True),
    ]

    cli_projection = summarize_wire(bindings, rules)
    wire = _build_wire(bindings, rules, fake_fetch)
    creds = proxy_config.load_resolved(wire)
    proxy_projection = proxy_config.sanitized_live_config(creds)

    assert cli_projection == proxy_projection
    # Sanity: it isn't vacuously empty, and secret/param keys are absent.
    assert len(cli_projection["bindings"]) == 5
    assert len(cli_projection["rules"]) == 3
    for b in cli_projection["bindings"]:
        assert set(b) == {"name", "hosts", "scheme", "placeholder", "env"}
    for r in cli_projection["rules"]:
        assert set(r) == {"name", "hosts", "action", "visible"}


def test_summarize_wire_script_rule_and_env_projection_parity(xdg):
    """#66 field contract, extended: a SCRIPT-action rule and env-suppressed /
    env-override bindings must project identically on both sides. The lossy
    projection carries only the rule `action` (not the script source/params) and a
    binding's EFFECTIVE env, so a suppressed (`env=false`) binding shows env null
    and an override shows the override -- CLI summarize_wire must match the proxy's
    sanitized_live_config for all of them. A builtin rule script needs no Starlark
    on the CLI side (rule_wire_entries just embeds the registered source), but the
    proxy round-trip compiles it, so skip if the runtime isn't importable."""
    pytest.importorskip("starlark")
    from credproxy_cli.core.model.bindings import Binding
    from credproxy_cli.core.model.injectors import find_injector
    from credproxy_cli.core.model.rules import Rule
    from credproxy_cli.core.model.wire import summarize_wire
    proxy_config = _proxy_config()

    def fake_fetch(provider, refs):
        return {r: f"val-{r}" for r in refs}

    bearer = find_injector("bearer")
    ph1 = bearer.placeholder.generate()
    ph2 = bearer.placeholder.generate()
    bindings = [
        # env = false -> suppressed -> effective env null (not the injector hint).
        Binding(name="suppressed", injector="bearer", provider="env",
                secret="ref1", hosts=("api1.example.com",),
                placeholder=ph1, env=None, env_suppressed=True),
        # explicit env override -> that literal, not the injector hint.
        Binding(name="override", injector="bearer", provider="env",
                secret="ref2", hosts=("api2.example.com",),
                placeholder=ph2, env="MY_OVERRIDE_TOKEN"),
    ]
    # A script rule referencing a builtin rule script (scrub-emails is a response
    # rule); its source + api ride the wire, but the projection shows only action.
    rules = [
        Rule(name="scrub", hosts=("api.example.com",), action="script",
             script="scrub-emails"),
    ]

    cli_projection = summarize_wire(bindings, rules)
    wire = _build_wire(bindings, rules, fake_fetch)
    creds = proxy_config.load_resolved(wire)
    proxy_projection = proxy_config.sanitized_live_config(creds)

    assert cli_projection == proxy_projection
    # The suppressed binding projects env null; the override projects its literal.
    envs = {b["name"]: b["env"] for b in cli_projection["bindings"]}
    assert envs == {"suppressed": None, "override": "MY_OVERRIDE_TOKEN"}
    # The script rule projects action only -- no source/params/api leak.
    assert cli_projection["rules"] == [
        {"name": "scrub", "hosts": ["api.example.com"], "action": "script",
         "visible": False}]


def _build_wire(bindings, rules, fake_fetch):
    """build_wire with an injected fetcher (the CLI's wire_config takes fetch_many;
    build_wire calls it internally, so resolve here then hand the proxy the
    literal-value wire the same way the push path does)."""
    from credproxy_cli.core.model.bindings import wire_config
    from credproxy_cli.core.model.rules import rule_wire_entries
    wire = wire_config(bindings, fetch_many=fake_fetch)
    wire["rules"] = rule_wire_entries(rules)
    return wire


def _proxy_module(name):
    proxy_dir = str(Path(__file__).resolve().parents[2] / "proxy")
    if proxy_dir not in sys.path:
        sys.path.insert(0, proxy_dir)
    import importlib
    return importlib.import_module(name)


def test_hostmatch_compile_pattern_parity():
    """The CLI's hostmatch.compile_pattern mirror (used by `rule test` to match
    host globs on the host) must agree with the proxy's over the same inputs."""
    from credproxy_cli.core.model import hostmatch as cli_hm
    proxy_hm = _proxy_module("hostmatch")
    pats = ["*.example.com", "s3.*.amazonaws.com", "*.amazonaws.com"]
    hosts = ["a.example.com", "x.y.example.com", "API.Example.COM",
             "s3.eu-west-1.amazonaws.com", "example.com", "evil.com"]
    for pat in pats:
        for host in hosts:
            assert bool(cli_hm.compile_pattern(pat).fullmatch(host.lower())) == \
                   bool(proxy_hm.compile_pattern(pat).fullmatch(host.lower())), \
                   (pat, host)


def test_rule_constants_parity():
    """The mirrored rule constants must stay identical across the CLI and proxy
    (and, for the forbidden set, the two proxy copies) -- a one-sided edit would
    make `rule add`/`validate` disagree with what the proxy enforces."""
    from credproxy_cli.core.model import rules as cli_rules
    proxy_config = _proxy_module("config")
    proxy_rules = _proxy_module("rules")
    assert cli_rules._VISIBLE_DEFAULT == proxy_config._VISIBLE_DEFAULT
    # _FORBIDDEN_REWRITE_HEADERS lives once on the proxy (rules.py; config.py
    # references it), so only the CLI mirror needs a parity assertion.
    assert cli_rules._FORBIDDEN_REWRITE_HEADERS \
        == proxy_rules._FORBIDDEN_REWRITE_HEADERS


def test_rule_sequencing_parity_declarative():
    """The CLI's offline match_rules and the proxy's RuleSet.dry_run must classify
    a DECLARATIVE rule set identically -- same order, terminal, conditional -- so
    the two hand-written first-terminal-wins walks can't drift. (Script rules
    diverge by design: offline is conservative, dry_run reads the exact phase.)"""
    from credproxy_cli.core.model.rules import Rule, match_rules, rule_wire_entries
    proxy_config = _proxy_config()
    rules = [
        Rule(name="rw", hosts=("api.github.com",), action="rewrite",
             set_headers={"X-Env": "s"}),
        Rule(name="blk", hosts=("api.github.com",), action="block",
             methods=("DELETE",)),
        Rule(name="never", hosts=("api.github.com",), action="block"),
    ]
    cli = [(m.name, m.terminal, m.conditional)
           for m in match_rules(rules, "DELETE", "api.github.com", "/repos/a")]
    creds = proxy_config.load_resolved(
        {"bindings": [], "rules": rule_wire_entries(rules)})
    proxy = [(m["name"], m["terminal"], m["conditional"])
             for m in creds.rule_set().dry_run("DELETE", "api.github.com", "/repos/a")]
    assert cli == proxy == [("rw", False, False), ("blk", True, False)]


def test_rule_params_field_parity_cli_proxy():
    """The `params` field must be allowed on the `script` action in BOTH deploy
    units (and only there) -- else a params rule would pass one validator and be
    rejected by the other (#35). This checks the field SETS directly, since a
    full round-trip needs Starlark (image only)."""
    from credproxy_cli.core.model.rules import _ACTION_FIELDS as cli_fields
    proxy_fields = _proxy_config()._RULE_ACTION_FIELDS
    assert "params" in cli_fields["script"]
    assert "params" in proxy_fields["script"]
    for act in ("block", "respond", "rewrite"):
        assert "params" not in cli_fields[act]
        assert "params" not in proxy_fields[act]
