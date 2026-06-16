"""Tests for the distribution profile overlay (the org/fork customization tier).

Resolution is three tiers -- user (XDG) > profile overlay > builtin -- selected
by CREDPROXY_PROFILE_DIR. These verify: the builtin defaults load with no
overlay; a partial profile.toml overrides a subset; and an overlay definition
(injector / preset) and the scaffold template shadow the builtin ones.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def profile_overlay(tmp_path, monkeypatch):
    """A temp profile-overlay dir wired in via CREDPROXY_PROFILE_DIR."""
    d = tmp_path / "profile"
    d.mkdir()
    monkeypatch.setenv("CREDPROXY_PROFILE_DIR", str(d))
    return d


# ---- profile.toml constants --------------------------------------------------


def test_builtin_profile_loads_without_overlay(xdg, monkeypatch, tmp_path):
    """With the overlay pointed at an empty dir, profile() is the builtin
    default."""
    monkeypatch.setenv("CREDPROXY_PROFILE_DIR", str(tmp_path / "empty"))
    from credproxy_cli.core.profile import profile
    p = profile()
    assert p.default_image == "mcr.microsoft.com/devcontainers/base:ubuntu"
    assert p.image_tag == "credproxy:dev"
    assert p.default_user == "vscode"


def test_overlay_overrides_subset_of_constants(xdg, profile_overlay):
    """A partial profile.toml overrides only its keys; the rest fall back."""
    (profile_overlay / "profile.toml").write_text(
        'default_image = "registry.acme.example/base:1"\n'
        'image_tag = "acme-credproxy:latest"\n'
    )
    from credproxy_cli.core.profile import profile
    p = profile()
    assert p.default_image == "registry.acme.example/base:1"   # overridden
    assert p.image_tag == "acme-credproxy:latest"              # overridden
    assert p.default_user == "vscode"                          # fallback
    assert p.generic_home == "/root"                           # fallback


def test_overlay_default_image_drives_scaffold(xdg, profile_overlay):
    """render_template wires the active block when the workspace image equals the
    overlay's default_image (proving the conditional keys on the profile)."""
    (profile_overlay / "profile.toml").write_text(
        'default_image = "acme/base:1"\n'
        'default_user = "acme"\n'
        'default_home = "/home/acme"\n'
    )
    from credproxy_cli.core.config import render_template
    text = render_template("w", "acme/base:1")
    assert 'image = "acme/base:1"' in text
    assert 'user = "acme"' in text and 'home = "/home/acme"' in text
    assert "map_host_user = true" in text


# ---- workspace.template.toml -------------------------------------------------


def test_overlay_template_shadows_builtin(xdg, profile_overlay):
    (profile_overlay / "workspace.template.toml").write_text(
        '# ACME workspace\nimage = "{image}"\n# acme-marker\n'
    )
    from credproxy_cli.core.config import render_template
    text = render_template("w", "acme/base:1")
    assert "acme-marker" in text
    assert 'image = "acme/base:1"' in text


# ---- definitions: injectors --------------------------------------------------


def test_overlay_injector_is_found_and_sourced(xdg, profile_overlay):
    """A new injector in the overlay resolves with source 'profile'."""
    (profile_overlay / "injectors").mkdir()
    (profile_overlay / "injectors" / "acme.toml").write_text('scheme = "bearer"\n')
    from credproxy_cli.core.injectors import find_injector, list_injectors
    inj = find_injector("acme")
    assert inj.source == "profile"
    assert "acme" in {i.name for i in list_injectors()}


def test_overlay_injector_shadows_builtin(xdg, profile_overlay):
    """A same-named overlay injector shadows the builtin one."""
    (profile_overlay / "injectors").mkdir()
    (profile_overlay / "injectors" / "bearer.toml").write_text('scheme = "basic"\n')
    from credproxy_cli.core.injectors import find_injector
    inj = find_injector("bearer")
    assert inj.source == "profile"
    assert inj.scheme == "basic"   # the overlay's, not the builtin bearer


# ---- definitions: presets ----------------------------------------------------


def test_overlay_preset_is_resolvable(xdg, profile_overlay):
    (profile_overlay / "presets").mkdir()
    (profile_overlay / "presets" / "acme.toml").write_text(
        "default_provider = \"env\"\n"
        "[placeholder]\nprefix = \"acme_\"\nlength = 32\ncharset = \"hex\"\n"
        "[[part]]\nsuffix = \"api\"\ninjector = \"bearer\"\n"
        "hosts = [\"api.acme.example\"]\nenv = \"ACME_TOKEN\"\n"
    )
    from credproxy_cli.core.presets import build_preset, load_presets
    assert "acme" in load_presets()
    assert "github" in load_presets()   # builtin still present
    bindings = build_preset("acme", "env", "ACME_TOKEN")
    assert [b.name for b in bindings] == ["acme-api"]
    assert bindings[0].hosts == ("api.acme.example",)
