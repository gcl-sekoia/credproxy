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
    from credproxy_cli.core.presets import load_presets
    _write_preset("badcs", '[placeholder]\nprefix = "p_"\nlength = 32\ncharset = "nope"\n')
    with pytest.raises(ConfigError, match="charset"):
        load_presets()


def test_preset_rejects_zero_entropy_length(xdg):
    """length <= len(prefix) would generate just the prefix -- identical across
    workspaces, defeating placeholder uniqueness. Must fail at parse."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("badlen", '[placeholder]\nprefix = "p_"\nlength = 2\ncharset = "hex"\n')
    with pytest.raises(ConfigError, match="must exceed"):
        load_presets()


def test_preset_valid_placeholder_builds(xdg):
    from credproxy_cli.core.presets import get_preset
    _write_preset("good", '[placeholder]\nprefix = "g_"\nlength = 18\ncharset = "hex"\n')
    ph = get_preset("good").placeholder.generate()
    assert ph.startswith("g_") and len(ph) == 18


def test_preset_placeholder_defaults_missing_fields(xdg):
    """Missing placeholder fields now DEFAULT (consistent with injector parsing)
    rather than raising a KeyError; only present-but-invalid values are rejected."""
    from credproxy_cli.core.presets import get_preset
    _write_preset("partial", '[placeholder]\nprefix = "x_"\n')   # no length/charset
    assert get_preset("partial").placeholder.prefix == "x_"
