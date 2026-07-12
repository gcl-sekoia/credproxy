"""This overlay ships the claude-managed-settings MECHANISM: the rewrite script
(tested in test_claude_code_settings_rewrite.py) and a pack whose `settings_patch`
defaults to a no-op "{}" — a profile supplies the real patch by shadowing the pack
(see the 50-example overlay).

In the full overlay chain 50-example shadows this pack, so `get_preset` /
`build_preset` return the shadow; to check THIS overlay's own neutral default we
parse its own file directly.
"""
from pathlib import Path

from credproxy_cli.core.model.presets import _parse_preset

PACK = Path(__file__).resolve().parent.parent / "presets" / "claude-managed-settings.toml"


def test_pack_parses_and_is_a_neutral_noop():
    spec = _parse_preset(PACK, "claude-managed-settings", "claude-managed-settings")
    assert spec.parts == () and spec.placeholder is None      # pure-rule, no credential
    (pr,) = spec.rules
    assert pr.suffix == "rewrite"
    assert pr.rule.action == "script" and pr.rule.script == "claude-code-settings-rewrite"
    assert pr.rule.hosts == ("api.anthropic.com",)
    assert pr.rule.path == "/api/claude_code/settings"
    # The default imposes nothing; the field is present so it's discoverable.
    assert pr.rule.params["settings_patch"] == "{}"


def test_pack_clears_the_cached_settings():
    spec = _parse_preset(PACK, "claude-managed-settings", "claude-managed-settings")
    (setup,) = spec.setup
    assert setup["order"] == 30 and setup["user"] == "workspace"
    assert "remote-settings.json" in setup["run"]
