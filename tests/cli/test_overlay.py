"""Tests for the overlay system (the org/fork customization tiers).

Resolution is user (XDG) > overlays (CREDPROXY_OVERLAY_PATH, declared order) >
builtin. These verify overlay discovery, N-overlay ordering/labels, the uniform
singleton walk (user tier included), the de-templated scaffold, and the
CREDPROXY_OVERLAY_PATH env semantics.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """A single temp overlay dir wired in via CREDPROXY_OVERLAY_PATH."""
    d = tmp_path / "overlay"
    d.mkdir()
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(d))
    return d


def _user_dir(xdg, kind: str):
    d = xdg["config"] / "credproxy" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- workspace.template.toml (the singleton) ---------------------------------


def test_overlay_template_shadows_builtin(xdg, overlay):
    """An overlay's literal workspace.template.toml is used over the builtin
    (only `{name}` is substituted)."""
    (overlay / "workspace.template.toml").write_text(
        '# ACME workspace {name}\nimage = "acme/base:1"\n# acme-marker\n'
    )
    from credproxy_cli.core.model.config import render_template
    text = render_template("w")
    assert "acme-marker" in text
    assert "ACME workspace w" in text          # {name} substituted
    assert 'image = "acme/base:1"' in text     # the overlay's literal image


def test_user_template_shadows_overlay_and_builtin(xdg, overlay):
    """The singleton now rides the same walk as the registries, so a personal
    user template shadows every overlay's and the builtin default."""
    (overlay / "workspace.template.toml").write_text('image = "overlay/img:1"\n')
    (xdg["config"] / "credproxy").mkdir(parents=True, exist_ok=True)
    (xdg["config"] / "credproxy" / "workspace.template.toml").write_text(
        'image = "user/img:9"\n# user-marker\n'
    )
    from credproxy_cli.core.model.config import render_template
    text = render_template("w")
    assert "user-marker" in text
    assert 'image = "user/img:9"' in text


def test_template_falls_through_to_builtin(xdg):
    """With no user or overlay template, the builtin default is used."""
    from credproxy_cli.core.model.config import render_template
    text = render_template("w")
    assert "credproxy workspace w start" in text  # builtin header, {name} filled


def test_overlay_template_shadows_overlay(xdg, tmp_path, monkeypatch):
    """Two overlays supplying the same singleton: the leftmost (most specific)
    wins."""
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    (a / "workspace.template.toml").write_text('image = "a/img"\n# a-marker\n')
    (b / "workspace.template.toml").write_text('image = "b/img"\n# b-marker\n')
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    from credproxy_cli.core.model.config import render_template
    assert "a-marker" in render_template("w")


# ---- de-templated scaffold (plain {name} replace, no str.format) -------------


def test_scaffold_is_verbatim_except_name(xdg, overlay):
    """The template is a LITERAL file: only the exact token `{name}` is replaced.
    Stray braces, `${VAR}`, and inline tables survive byte-for-byte -- no
    brace-doubling contract."""
    (overlay / "workspace.template.toml").write_text(
        'image = "img:{name}"\n'
        'env = { FOO = "${VAR}", BRACE = "{foo}" }\n'
        'mounts = [{ volume = "cache", target = "/c" }]\n'
        '# literal { a = 1 } and {name} both here\n'
    )
    from credproxy_cli.core.model.config import render_template
    text = render_template("proj")
    assert 'image = "img:proj"' in text                        # {name} -> proj
    assert 'env = { FOO = "${VAR}", BRACE = "{foo}" }' in text  # untouched
    assert 'mounts = [{ volume = "cache", target = "/c" }]' in text
    assert "# literal { a = 1 } and proj both here" in text     # {name} everywhere


# ---- definitions: injectors --------------------------------------------------


def test_overlay_injector_is_found_and_sourced(xdg, overlay):
    """A new injector in the overlay resolves with an `overlay:<base>` source."""
    (overlay / "injectors").mkdir()
    (overlay / "injectors" / "acme.toml").write_text('scheme = "bearer"\n')
    from credproxy_cli.core.model.injectors import find_injector, list_injectors
    inj = find_injector("acme")
    assert inj.source == "overlay:overlay"
    assert "acme" in {i.name for i in list_injectors()}


def test_overlay_injector_shadows_builtin(xdg, overlay):
    """A same-named overlay injector shadows the builtin one."""
    (overlay / "injectors").mkdir()
    (overlay / "injectors" / "bearer.toml").write_text('scheme = "basic"\n')
    from credproxy_cli.core.model.injectors import find_injector
    inj = find_injector("bearer")
    assert inj.source == "overlay:overlay"
    assert inj.scheme == "basic"   # the overlay's, not the builtin bearer


def test_three_way_precedence(xdg, overlay):
    """user shadows overlay shadows builtin for the SAME asset name; the winner
    records both losers as `shadows`, most-specific-first."""
    (overlay / "injectors").mkdir()
    (overlay / "injectors" / "bearer.toml").write_text('scheme = "basic"\n')
    ud = _user_dir(xdg, "injectors")
    (ud / "bearer.toml").write_text('scheme = "body"\n')
    from credproxy_cli.core.model.injectors import find_injector, list_injectors
    assert find_injector("bearer").source == "user"
    row = next(i for i in list_injectors() if i.name == "bearer")
    assert row.source == "user"
    assert row.shadows == ("overlay:overlay", "builtin")


def test_overlay_shadows_overlay_injector(xdg, tmp_path, monkeypatch):
    """Two overlays, same injector name: the leftmost wins; the winning row
    records the shadowed peer."""
    a = tmp_path / "a"; (a / "injectors").mkdir(parents=True)
    b = tmp_path / "b"; (b / "injectors").mkdir(parents=True)
    (a / "injectors" / "tok.toml").write_text('scheme = "bearer"\n')
    (b / "injectors" / "tok.toml").write_text('scheme = "basic"\n')
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    from credproxy_cli.core.model.injectors import find_injector, list_injectors
    assert find_injector("tok").source == "overlay:a"
    row = next(i for i in list_injectors() if i.name == "tok")
    assert row.shadows == ("overlay:b",)


# ---- definitions: providers / scripts / presets (shadow representative) -------


def test_overlay_shadows_overlay_provider(xdg, tmp_path, monkeypatch):
    a = tmp_path / "a"; (a / "providers").mkdir(parents=True)
    b = tmp_path / "b"; (b / "providers").mkdir(parents=True)
    for d in (a, b):
        p = d / "providers" / "vault"
        p.write_text("#!/bin/sh\nexit 3\n")
        p.chmod(0o755)
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    from credproxy_cli.core.providers import find_provider, list_providers
    assert find_provider("vault").source == "overlay:a"
    row = next(p for p in list_providers() if p.name == "vault")
    assert row.shadows == ("overlay:b",)


def test_overlay_shadows_overlay_script(xdg, tmp_path, monkeypatch):
    a = tmp_path / "a"; (a / "scripts").mkdir(parents=True)
    b = tmp_path / "b"; (b / "scripts").mkdir(parents=True)
    (a / "scripts" / "guard.star").write_text("# a\n")
    (b / "scripts" / "guard.star").write_text("# b\n")
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    from credproxy_cli.core.model.scripts import find_script, list_scripts
    assert find_script("guard").source_origin == "overlay:a"
    row = next(s for s in list_scripts() if s.name == "guard")
    assert row.shadows == ("overlay:b",)


# ---- definitions: presets ----------------------------------------------------


def test_overlay_preset_is_resolvable(xdg, overlay):
    (overlay / "presets").mkdir()
    (overlay / "presets" / "acme.toml").write_text(
        "default_provider = \"env\"\n"
        "[placeholder]\nprefix = \"acme_\"\nlength = 32\ncharset = \"hex\"\n"
        "[[part]]\nsuffix = \"api\"\ninjector = \"bearer\"\n"
        "hosts = [\"api.acme.example\"]\nenv = \"ACME_TOKEN\"\n"
    )
    from credproxy_cli.core.model.presets import build_preset, load_presets
    assert "acme" in load_presets()
    assert "github" in load_presets()   # builtin still present
    exp = build_preset("acme", "env", "ACME_TOKEN")
    assert [b.name for b in exp.bindings] == ["acme-api"]
    assert exp.bindings[0].hosts == ("api.acme.example",)
    assert exp.rules == ()               # binding-only preset


def test_overlay_shadows_overlay_preset(xdg, tmp_path, monkeypatch):
    a = tmp_path / "a"; (a / "presets").mkdir(parents=True)
    b = tmp_path / "b"; (b / "presets").mkdir(parents=True)
    body = ("[placeholder]\nprefix = \"p_\"\nlength = 16\ncharset = \"hex\"\n"
            "[[part]]\nsuffix = \"api\"\ninjector = \"bearer\"\nhosts = [\"h.example\"]\n")
    (a / "presets" / "pack.toml").write_text(body)
    (b / "presets" / "pack.toml").write_text(body)
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    from credproxy_cli.core.model.presets import load_preset_sources, describe_presets
    assert load_preset_sources()["pack"] == "overlay:a"
    row = next(r for r in describe_presets() if r["name"] == "pack")
    assert row["shadows"] == ["overlay:b"]


# ---- CREDPROXY_OVERLAY_PATH semantics ----------------------------------------


def test_overlay_path_unset_discovers_container_subdirs(xdg, tmp_path, monkeypatch):
    """Unset -> discovery: each SUBDIR of the <repo>/overlay/ container is one
    overlay, lexical order by basename; files (the README) are skipped."""
    monkeypatch.delenv("CREDPROXY_OVERLAY_PATH", raising=False)
    from credproxy_cli.core import paths
    container = tmp_path / "overlay"
    container.mkdir()
    (container / "20-team").mkdir()
    (container / "10-base").mkdir()
    (container / "README.md").write_text("not an overlay\n")   # stray file skipped
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    assert paths.overlay_dirs() == [
        ("overlay:10-base", container / "10-base"),
        ("overlay:20-team", container / "20-team"),
    ]


def test_overlay_container_empty_or_absent_means_no_overlays(xdg, tmp_path,
                                                             monkeypatch):
    """A subdir-less container (upstream ships just the README) or a missing one
    discovers nothing -- no phantom overlay tier, no doctor failure."""
    monkeypatch.delenv("CREDPROXY_OVERLAY_PATH", raising=False)
    from credproxy_cli.core import paths
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    assert paths.overlay_dirs() == []          # absent container
    container = tmp_path / "overlay"
    container.mkdir()
    (container / "README.md").write_text("x\n")
    assert paths.overlay_dirs() == []          # container with only a file


def test_overlay_path_set_empty_means_no_overlays(xdg, monkeypatch):
    """Set-but-empty is an explicit opt-out: NO overlays (distinct from unset)."""
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", "")
    from credproxy_cli.core import paths
    assert paths.overlay_dirs() == []
    # overlay_roots collapses to user > builtin only.
    assert [l for l, _ in paths.overlay_roots()] == ["user", "builtin"]


def test_overlay_path_skips_empty_entries_and_honors_pathsep(xdg, tmp_path,
                                                             monkeypatch):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    # Leading/embedded empty entries (a::b, trailing sep) are skipped.
    val = os.pathsep.join(["", str(a), "", str(b)]) + os.pathsep
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", val)
    from credproxy_cli.core import paths
    dirs = paths.overlay_dirs()
    assert [str(d) for _, d in dirs] == [str(a), str(b)]


def test_overlay_label_dedup(xdg, tmp_path, monkeypatch):
    """Two overlays with the same basename get deterministic distinct labels."""
    a = tmp_path / "x" / "base"; a.mkdir(parents=True)
    b = tmp_path / "y" / "base"; b.mkdir(parents=True)
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    from credproxy_cli.core import paths
    assert [l for l, _ in paths.overlay_dirs()] == ["overlay:base", "overlay:base#2"]
