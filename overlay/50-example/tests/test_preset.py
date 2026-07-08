"""The 50-example overlay's preset packs: its own `persist`, and its SHADOW of
`claude-managed-settings` (which supplies the opinionated patch the base pack
leaves as a no-op). Resolved off the mounted overlay chain and expanded the way
`preset add` / `create` do.
"""
import tomllib

from credproxy_cli.core.config import _overlay_source
from credproxy_cli.core.paths import resolve_singleton
from credproxy_cli.core.presets import build_preset, load_preset_sources


def test_oracle_agent_ships_via_the_profile_template():
    # The profile's template mounts the oracle agent def where claude-code-setup.sh
    # installs it ($CLAUDE_CONFIG_DIR/agents/); base ships no agents, only the mechanism.
    data = tomllib.loads(resolve_singleton("workspace.template.toml").read_text())
    sources = [m.get("overlay") for m in data.get("mounts", [])]
    assert "agents/oracle.md" in sources
    # And the source resolves through the overlay search to a real oracle manifest.
    text = open(_overlay_source("agents/oracle.md", "test")).read()
    assert "name: oracle" in text and "model: fable" in text


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
