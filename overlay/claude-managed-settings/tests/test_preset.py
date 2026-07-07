"""`build_preset` resolves this overlay's preset off the mounted overlay chain and
expands it -- the same expansion `preset add` runs."""
from credproxy_cli.core.presets import build_preset


def test_claude_managed_settings_preset_expands_to_the_rule():
    bindings, rules = build_preset("claude-managed-settings")
    assert bindings == []
    (rule,) = rules
    assert rule.name == "claude-managed-settings-rewrite"
    assert rule.action == "script" and rule.script == "claude-code-settings-rewrite"
    assert rule.hosts == ("api.anthropic.com",)
    assert rule.path == "/api/claude_code/settings"
    assert rule.params is None
