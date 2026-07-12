"""Preset placeholder validation -- now shared with injector parsing, so a bad
charset/length fails at parse instead of crashing later (KeyError in generate())
or silently producing a zero-entropy, non-unique placeholder."""
from __future__ import annotations

import pytest


def _write_preset(name: str, placeholder_toml: str):
    from credproxy_cli.core.paths import config_dir
    d = config_dir() / "presets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(
        placeholder_toml
        + '[[part]]\nsuffix = "api"\ninjector = "bearer"\nhosts = ["h.example"]\n'
    )


def test_preset_rejects_unknown_charset(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("badcs", '[placeholder]\nprefix = "p_"\nlength = 32\ncharset = "nope"\n')
    with pytest.raises(ConfigError, match="charset"):
        load_presets()


def test_preset_rejects_zero_entropy_length(xdg):
    """length <= len(prefix) would generate just the prefix -- identical across
    workspaces, defeating placeholder uniqueness. Must fail at parse."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("badlen", '[placeholder]\nprefix = "p_"\nlength = 2\ncharset = "hex"\n')
    with pytest.raises(ConfigError, match="must exceed"):
        load_presets()


def test_preset_valid_placeholder_builds(xdg):
    from credproxy_cli.core.model.presets import get_preset
    _write_preset("good", '[placeholder]\nprefix = "g_"\nlength = 18\ncharset = "hex"\n')
    ph = get_preset("good").placeholder.generate()
    assert ph.startswith("g_") and len(ph) == 18


def test_preset_placeholder_defaults_missing_fields(xdg):
    """Missing placeholder fields now DEFAULT (consistent with injector parsing)
    rather than raising a KeyError; only present-but-invalid values are rejected."""
    from credproxy_cli.core.model.presets import get_preset
    _write_preset("partial", '[placeholder]\nprefix = "x_"\n')   # no length/charset
    assert get_preset("partial").placeholder.prefix == "x_"


# ---- service setup packs: bindings + rules (#37) ----------------------------


def _write_raw_preset(name: str, toml: str):
    from credproxy_cli.core.paths import config_dir
    d = config_dir() / "presets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(toml)


_MIXED = """
[placeholder]
prefix = "ghp_"
length = 40
charset = "alnumeric"

[[part]]
suffix = "api"
injector = "bearer"
hosts = ["api.github.com"]
env = "GITHUB_TOKEN"

[[rule]]
suffix = "readonly"
hosts = ["api.github.com"]
action = "script"
script = "readonly-guard"
[rule.params]
allow_prefixes = ["/repos/myorg/scratch-"]
message = "scratch only"
"""


def test_preset_mixed_parses_bindings_and_rules(xdg):
    from credproxy_cli.core.model.presets import build_preset, get_preset
    _write_raw_preset("gh-guarded", _MIXED)
    spec = get_preset("gh-guarded")
    assert spec.needs_credential is True
    exp = build_preset("gh-guarded", "env", "GITHUB_TOKEN")
    bindings, rules = list(exp.bindings), list(exp.rules)
    assert [b.name for b in bindings] == ["gh-guarded-api"]
    # suffix -> name; the params survive to the built Rule.
    assert [r.name for r in rules] == ["gh-guarded-readonly"]
    assert rules[0].action == "script" and rules[0].script == "readonly-guard"
    assert rules[0].params == {"allow_prefixes": ["/repos/myorg/scratch-"],
                               "message": "scratch only"}
    # bindings and rules SHARE the freshly-generated placeholder shape.
    assert bindings[0].placeholder.startswith("ghp_")


_RULE_ONLY = """
[[rule]]
suffix = "guard"
hosts = ["api.github.com"]
action = "block"
methods = ["DELETE"]
"""


def test_preset_pure_rule_needs_no_credential(xdg):
    """A pure-rule pack: no [placeholder], no [[part]] -> no provider/secret."""
    from credproxy_cli.core.model.presets import build_preset, get_preset
    _write_raw_preset("policy", _RULE_ONLY)
    spec = get_preset("policy")
    assert spec.needs_credential is False and spec.placeholder is None
    exp = build_preset("policy")                       # zero flags
    assert exp.bindings == ()
    assert [r.name for r in exp.rules] == ["policy-guard"] \
        and exp.rules[0].action == "block"


def test_preset_binding_without_placeholder_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_raw_preset("nop", '[[part]]\nsuffix="api"\ninjector="bearer"\n'
                             'hosts=["h.example"]\n')
    with pytest.raises(ConfigError, match="missing \\[placeholder\\]"):
        load_presets()


def test_preset_pure_rule_with_placeholder_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_raw_preset("bad", '[placeholder]\nprefix="p_"\n' + _RULE_ONLY)
    with pytest.raises(ConfigError, match="meaningless without"):
        load_presets()


def test_preset_empty_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_raw_preset("empty", '[placeholder]\nprefix="p_"\n')  # no parts, no rules
    with pytest.raises(ConfigError, match="at least one"):
        load_presets()


def test_preset_rule_literal_name_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_raw_preset("bad", '[[rule]]\nname="x"\nsuffix="g"\n'
                             'hosts=["h.example"]\naction="block"\n')
    with pytest.raises(ConfigError, match="uses 'suffix'"):
        load_presets()


def test_preset_rule_field_error_surfaces_with_preset_path(xdg):
    """A bad preset rule fails at preset LOAD via the shared _parse_rule_entry,
    with the preset path in the message ONCE (not doubled -- review #38), not a
    deferred crash at apply."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_raw_preset("bad", '[[rule]]\nsuffix="g"\nhosts=["h.example"]\n'
                             'action="nope"\n')
    with pytest.raises(ConfigError, match="action must be one of") as ei:
        load_presets()
    msg = str(ei.value)
    assert "preset 'bad'" in msg and "rule[0]" in msg
    assert msg.count("preset 'bad'") == 1        # not doubled


def test_describe_presets_includes_rules(xdg):
    from credproxy_cli.core.model.presets import describe_presets
    _write_raw_preset("gh-guarded", _MIXED)
    row = next(p for p in describe_presets() if p["name"] == "gh-guarded")
    assert row["needs_credential"] is True
    assert len(row["bindings"]) == 1 and len(row["rules"]) == 1
    assert row["rules"][0] == {"name": "gh-guarded-readonly",
                               "hosts": ["api.github.com"], "action": "script",
                               "script": "readonly-guard", "visible": False}


# ---- #59 v2 review finding 5: unique join keys within a pack ------------------


def test_preset_rejects_duplicate_setup_order(xdg):
    """Two `[[setup]]` steps sharing an `order` are a definition error -- `order`
    is the join key the lock snapshots setup elements by (must be unique)."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_raw_preset(
        "dupsetup",
        '[[setup]]\nrun = "a"\norder = 1\n[[setup]]\nrun = "b"\norder = 1\n')
    with pytest.raises(ConfigError, match=r"duplicate .*setup.* order"):
        load_presets()


def test_preset_rejects_duplicate_mount_target(xdg):
    """Two `[[mount]]` entries with the same target (trailing slash normalized)
    are a definition error -- `target` is the mount join key."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_raw_preset(
        "dupmount",
        '[[mount]]\nvolume = "a"\ntarget = "/x"\n'
        '[[mount]]\nvolume = "b"\ntarget = "/x/"\n')
    with pytest.raises(ConfigError, match=r"duplicate .*mount.* target"):
        load_presets()


def test_preset_distinct_setup_orders_ok(xdg):
    """Distinct orders / targets parse fine (the guard is duplicate-only)."""
    from credproxy_cli.core.model.presets import get_preset
    _write_raw_preset(
        "okpack",
        '[[setup]]\nrun = "a"\norder = 1\n[[setup]]\nrun = "b"\norder = 2\n'
        '[[mount]]\nvolume = "a"\ntarget = "/x"\n'
        '[[mount]]\nvolume = "b"\ntarget = "/y"\n')
    spec = get_preset("okpack")
    assert [s["order"] for s in spec.setup] == [1, 2]
    assert [m.target for m in spec.mounts] == ["/x", "/y"]
