"""The 50-example overlay's preset packs: its own `persist`, and its SHADOW of
`claude-managed-settings` (which supplies the opinionated patch the base pack
leaves as a no-op). Resolved off the mounted overlay chain and expanded the way
`preset add` / `create` do.
"""
from credproxy_cli.core.presets import build_preset, load_preset_sources


def test_persist_resolves_from_this_overlay():
    assert load_preset_sources().get("persist") == "overlay:50-example"


def test_claude_managed_settings_shadow_provides_the_aggressive_patch():
    # This overlay sorts first, so it shadows the neutral claude-managed-settings
    # pack; the effective pack is ours, carrying the real org-stripping patch.
    assert load_preset_sources().get("claude-managed-settings") == "overlay:50-example"
    exp = build_preset("claude-managed-settings")
    assert exp.bindings == ()
    (rule,) = exp.rules
    assert rule.name == "claude-managed-settings-rewrite"
    assert rule.script == "claude-code-settings-rewrite"
    patch = rule.params["settings_patch"]
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" in patch and '"enabled": null' in patch


def test_persist_bundles_volume_chown_and_env():
    exp = build_preset("persist")
    assert exp.bindings == () and exp.rules == ()
    (mount,) = exp.mounts
    assert mount.kind == "volume" and mount.value == "persist" and mount.target == "/persist"
    # A root chown runs before the workspace-user setup steps (which write into it).
    (setup,) = exp.setup
    assert setup["user"] == "root" and setup["order"] == 5
    assert "/persist" in setup["run"]
    env = dict(exp.env)
    assert env["CLAUDE_CONFIG_DIR"] == "/persist/claude"
    assert env["HISTFILE"] == "/persist/zsh_history"
