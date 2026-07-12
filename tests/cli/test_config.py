"""Tests for core/config.py: load_config validation rules, template round-trip,
and workspace_spec_hash."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


# ---- helpers -----------------------------------------------------------------


def _write(workspaces_dir: Path, name: str, content: str):
    p = workspaces_dir / f"{name}.toml"
    p.write_text(textwrap.dedent(content))
    return p


# ---- happy path / defaults ---------------------------------------------------


def test_load_config_minimal(xdg, workspaces_dir):
    """Minimal config (image only) loads and applies defaults."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "myws", 'image = "alpine:3"\n')
    ws = Workspace("myws")
    cfg = load_config(ws)

    assert cfg["image"] == "alpine:3"
    assert cfg["home"] is None             # no home -> no managed home volume
    assert cfg["mounts"] == []             # incl. no home volume
    assert cfg["env"] == {}
    assert cfg["setup"] == []


def test_load_config_requires_image(xdg, workspaces_dir):
    """`image` is mandatory -- there is no built-in default to fall back to;
    omitting it is a clear error (the scaffold always writes one)."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "noimg", "")   # no image
    ws = Workspace("noimg")
    with pytest.raises(ConfigError, match="image.*required"):
        load_config(ws)


def test_load_config_full(xdg, tmp_path, workspaces_dir):
    """Config with all fields present is loaded correctly."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    # We need an existing directory for the mount source.
    src = tmp_path / "code"
    src.mkdir()

    _write(workspaces_dir, "full", f"""\
        image = "ubuntu:22.04"
        home = "/home/user"
        mounts = ["{src}:/code"]
        env = {{ FOO = "bar" }}
        setup = ["echo hi"]
    """)
    ws = Workspace("full")
    cfg = load_config(ws)

    assert cfg["image"] == "ubuntu:22.04"
    assert cfg["home"] == "/home/user"
    # mounts = [home volume (sugar, prepended), the bind]
    assert cfg["mounts"][0] == {"kind": "volume", "name": "home",
                                "target": "/home/user", "readonly": False}
    assert cfg["mounts"][1] == {"kind": "bind", "source": str(src),
                                "target": "/code", "readonly": False}
    assert len(cfg["mounts"]) == 2
    assert cfg["env"] == {"FOO": "bar"}
    assert cfg["setup"] == ["echo hi"]


def test_load_config_mount_readonly(xdg, tmp_path, workspaces_dir):
    """Mount with `:ro` suffix is parsed as readonly."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    src = tmp_path / "ro"
    src.mkdir()
    _write(workspaces_dir, "rome", f'image = "x"\nmounts = ["{src}:/data:ro"]\n')
    ws = Workspace("rome")
    cfg = load_config(ws)
    assert cfg["mounts"][0]["readonly"] is True


def test_volume_user_owned_parses(xdg, workspaces_dir):
    """`user_owned = true` on a managed volume parses into the mount dict."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "uo", textwrap.dedent('''
        image = "x"
        user = "dev"
        [[mounts]]
        volume = "cache"
        target = "/home/dev/.cache"
        user_owned = true
    '''))
    cfg = load_config(Workspace("uo"))
    vol = next(m for m in cfg["mounts"] if m.get("name") == "cache")
    assert vol["user_owned"] is True


def test_volume_without_user_owned_omits_key(xdg, workspaces_dir):
    """A plain volume carries no `user_owned` key, so the normalized mount dict
    (and thus the spec hash) is byte-identical to before the flag existed."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "plain", 'image = "x"\nhome = "/home/dev"\n')
    vol = load_config(Workspace("plain"))["mounts"][0]
    assert "user_owned" not in vol


def test_user_owned_changes_spec_hash(xdg, workspaces_dir):
    """Toggling user_owned alters the spec hash -> the change forces a recreate
    (which re-runs the chown step)."""
    from credproxy_cli.core.model.config import load_config, workspace_spec_hash
    from credproxy_cli.core.model.workspace import Workspace

    base_toml = ('image = "x"\nuser = "dev"\n'
                 '[[mounts]]\nvolume = "c"\ntarget = "/home/dev/c"\n')
    _write(workspaces_dir, "w", base_toml)
    before = workspace_spec_hash(load_config(Workspace("w")), None)
    _write(workspaces_dir, "w", base_toml + "user_owned = true\n")
    after = workspace_spec_hash(load_config(Workspace("w")), None)
    assert before != after


def test_add_volume_mount_renders_user_owned(xdg):
    """The surgical writer emits `user_owned = true` in both block and inline forms."""
    from credproxy_cli.core.model.config import add_volume_mount

    block = add_volume_mount('image = "x"\n', "cache", "/c", user_owned=True)
    assert "user_owned = true" in block
    inline = add_volume_mount('mounts = []\n', "cache", "/c", user_owned=True)
    assert "user_owned = true" in inline


# ---- validation errors -------------------------------------------------------


def test_user_owned_rejected_on_bind(xdg, workspaces_dir):
    """`user_owned` is volume-only; on a bind it's an unknown key."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", textwrap.dedent('''
        image = "x"
        user = "dev"
        [[mounts]]
        bind = "/h"
        target = "/c"
        user_owned = true
    '''))
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(Workspace("bad"))


def test_user_owned_requires_non_root_user(xdg, workspaces_dir):
    """A user_owned volume with no (or root) `user` is rejected -- the flag would
    chown to nobody."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "noU", textwrap.dedent('''
        image = "x"
        [[mounts]]
        volume = "c"
        target = "/c"
        user_owned = true
    '''))
    with pytest.raises(ConfigError, match="non-root `user`"):
        load_config(Workspace("noU"))


def test_user_owned_must_be_boolean(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "t", textwrap.dedent('''
        image = "x"
        user = "dev"
        [[mounts]]
        volume = "c"
        target = "/c"
        user_owned = "yes"
    '''))
    with pytest.raises(ConfigError, match="user_owned must be a boolean"):
        load_config(Workspace("t"))


# ---- (more) validation errors ------------------------------------------------


def test_load_config_missing_file(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    ws = Workspace("ghost")
    with pytest.raises(ConfigError, match="not found"):
        load_config(ws)


def test_load_config_bad_toml(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", "image = [unterminated")
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="TOML parse error"):
        load_config(ws)


def test_load_config_image_not_string(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", "image = 42\n")
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="image.*required.*string"):
        load_config(ws)


def test_load_config_home_not_absolute(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nhome = "relative/path"\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="`home` must be an absolute path"):
        load_config(ws)


def test_load_config_home_not_string(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nhome = 99\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="`home` must be an absolute path"):
        load_config(ws)


# ---- directory (cwd-addressing) ----------------------------------------------


def test_load_config_directory(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\ndirectory = "/home/me/proj"\n')
    cfg = load_config(Workspace("w"))
    assert cfg["directory"] == "/home/me/proj"


def test_load_config_directory_absent_is_none(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\n')
    assert load_config(Workspace("w"))["directory"] is None


def test_load_config_directory_not_absolute(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\ndirectory = "relative/path"\n')
    with pytest.raises(ConfigError, match="`directory` must be an absolute path"):
        load_config(Workspace("bad"))


def test_directory_not_in_spec_hash(xdg, workspaces_dir):
    """`directory` is host-side resolution metadata; changing it must not
    recreate the container (must not alter the spec hash)."""
    from credproxy_cli.core.model.config import load_config, workspace_spec_hash
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\n')
    base = workspace_spec_hash(load_config(Workspace("w")), None)
    _write(workspaces_dir, "w", 'image = "x"\ndirectory = "/home/me/proj"\n')
    withdir = workspace_spec_hash(load_config(Workspace("w")), None)
    assert base == withdir


def test_quick_directory(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import quick_directory
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\ndirectory = "/p"\n')
    assert quick_directory(Workspace("w")) == "/p"


def test_quick_directory_absent(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import quick_directory
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\n')
    assert quick_directory(Workspace("w")) is None


def test_quick_directory_tolerant_of_bad_toml(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import quick_directory
    from credproxy_cli.core.model.workspace import Workspace

    (workspaces_dir / "w.toml").write_text("not = valid = toml [[[\n")
    assert quick_directory(Workspace("w")) is None


# ---- set_top_level_key (surgical write-back) ---------------------------------


def test_set_top_level_key_appends_when_no_tables():
    import tomllib
    from credproxy_cli.core.model.config import set_top_level_key

    out = set_top_level_key('image = "x"\n', "directory", "/p")
    assert tomllib.loads(out)["directory"] == "/p"
    assert tomllib.loads(out)["image"] == "x"


def test_set_top_level_key_replaces_existing():
    import tomllib
    from credproxy_cli.core.model.config import set_top_level_key

    out = set_top_level_key('image = "x"\ndirectory = "/old"\n', "directory", "/new")
    assert tomllib.loads(out)["directory"] == "/new"
    assert "/old" not in out


def test_set_top_level_key_inserts_before_table():
    """A new top-level key must land before the first table header to stay
    in the root table (valid TOML)."""
    import tomllib
    from credproxy_cli.core.model.config import set_top_level_key

    src = 'image = "x"\n\n[env]\nFOO = "bar"\n'
    out = set_top_level_key(src, "directory", "/p")
    parsed = tomllib.loads(out)
    assert parsed["directory"] == "/p"
    assert parsed["env"] == {"FOO": "bar"}


def test_set_top_level_key_preserves_comments():
    from credproxy_cli.core.model.config import set_top_level_key

    src = '# my workspace\nimage = "x"  # the image\n'
    out = set_top_level_key(src, "directory", "/p")
    assert "# my workspace" in out
    assert "# the image" in out


def test_load_config_mounts_not_array(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nmounts = "notarray"\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="`mounts` must be an array"):
        load_config(ws)


def test_load_config_mount_not_string(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nmounts = [42]\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match='mounts\\[0\\] must be a string'):
        load_config(ws)


def test_load_config_mount_bad_format(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nmounts = ["/only"]\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match='expected "SRC:DST"'):
        load_config(ws)


def test_load_config_mount_source_not_absolute(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nmounts = ["relative:/dst"]\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="source must be absolute"):
        load_config(ws)


def test_load_config_mount_source_missing(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nmounts = ["/nonexistent_zz:/dst"]\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(ws)


def test_load_config_mount_target_not_absolute(xdg, tmp_path, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    src = tmp_path / "s"
    src.mkdir()
    _write(workspaces_dir, "bad", f'image = "x"\nmounts = ["{src}:relative"]\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="target must be absolute"):
        load_config(ws)


# ---- typed mounts: volume / overlay / tables --------------------------------


def _cfg(workspaces_dir, body):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace
    _write(workspaces_dir, "w", f'image = "x"\n{body}\n')
    return load_config(Workspace("w"))


def test_mount_volume_table(xdg, workspaces_dir):
    cfg = _cfg(workspaces_dir, 'mounts = [{ volume = "cache", target = "/c" }]')
    assert cfg["mounts"] == [{"kind": "volume", "name": "cache",
                              "target": "/c", "readonly": False}]


def test_mount_bind_table_with_readonly(xdg, tmp_path, workspaces_dir):
    src = tmp_path / "code"; src.mkdir()
    cfg = _cfg(workspaces_dir,
               f'mounts = [{{ bind = "{src}", target = "/code", readonly = true }}]')
    assert cfg["mounts"] == [{"kind": "bind", "source": str(src),
                              "target": "/code", "readonly": True}]


def test_mount_overlay_resolves_and_defaults_readonly(xdg, workspaces_dir,
                                                      tmp_path, monkeypatch):
    ov = tmp_path / "overlay"; ov.mkdir()
    (ov / "gitconfig").write_text("[user]\n")
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    cfg = _cfg(workspaces_dir,
               'mounts = [{ overlay = "gitconfig", target = "/g" }]')
    assert cfg["mounts"] == [{"kind": "overlay", "source": str(ov / "gitconfig"),
                              "target": "/g", "readonly": True}]  # ro default


def test_mount_overlay_resolves_from_second_overlay(xdg, workspaces_dir,
                                                    tmp_path, monkeypatch):
    """The overlay path is searched in declared order; a file absent from the
    first overlay resolves from the second."""
    import os
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    (b / "gitconfig").write_text("[user]\n")
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    cfg = _cfg(workspaces_dir,
               'mounts = [{ overlay = "gitconfig", target = "/g" }]')
    assert cfg["mounts"][0]["source"] == str(b / "gitconfig")


def test_mount_overlay_reorder_changes_source(xdg, workspaces_dir,
                                             tmp_path, monkeypatch):
    """Reordering the overlays flips which absolute path wins for a same-named
    file, and that path enters the spec hash -- a recreate is the intended
    consequence, so the resolved source must change."""
    import os
    from credproxy_cli.core.model.config import workspace_spec_hash
    a = tmp_path / "a"; a.mkdir(); (a / "gitconfig").write_text("A\n")
    b = tmp_path / "b"; b.mkdir(); (b / "gitconfig").write_text("B\n")
    body = 'mounts = [{ overlay = "gitconfig", target = "/g" }]'

    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    cfg_ab = _cfg(workspaces_dir, body)
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(b), str(a)]))
    cfg_ba = _cfg(workspaces_dir, body)

    assert cfg_ab["mounts"][0]["source"] == str(a / "gitconfig")
    assert cfg_ba["mounts"][0]["source"] == str(b / "gitconfig")
    assert workspace_spec_hash(cfg_ab, None) != workspace_spec_hash(cfg_ba, None)


def test_mount_overlay_escape_rejected(xdg, workspaces_dir, tmp_path, monkeypatch):
    from credproxy_cli.core.errors import ConfigError
    ov = tmp_path / "overlay"; ov.mkdir()
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    with pytest.raises(ConfigError, match="escapes the overlay dir"):
        _cfg(workspaces_dir, 'mounts = [{ overlay = "../secret", target = "/x" }]')


def test_mount_overlay_missing_source_names_all_roots(xdg, workspaces_dir,
                                                     tmp_path, monkeypatch):
    """The not-found error lists every overlay searched."""
    import os
    from credproxy_cli.core.errors import ConfigError
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    with pytest.raises(ConfigError, match="not found") as ei:
        _cfg(workspaces_dir, 'mounts = [{ overlay = "nope", target = "/x" }]')
    assert str(a) in str(ei.value) and str(b) in str(ei.value)


def test_mount_volume_bad_name(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match="invalid"):
        _cfg(workspaces_dir, 'mounts = [{ volume = "bad/name", target = "/x" }]')


def test_mount_table_needs_exactly_one_kind(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match="exactly one of bind/volume/overlay"):
        _cfg(workspaces_dir, 'mounts = [{ target = "/x" }]')


def test_duplicate_mount_target_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match="two mounts target"):
        _cfg(workspaces_dir,
             'mounts = [{ volume = "a", target = "/x" }, { volume = "b", target = "/x" }]')


def test_home_collides_with_explicit_home_volume(xdg, workspaces_dir):
    """The `home` sugar reserves the volume name 'home'."""
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match="two volumes named 'home'"):
        _cfg(workspaces_dir,
             'home = "/h"\nmounts = [{ volume = "home", target = "/other" }]')


def test_optional_home_omitted(xdg, workspaces_dir):
    """No `home` -> no home volume mount; cfg['home'] is None."""
    cfg = _cfg(workspaces_dir, 'mounts = [{ volume = "cache", target = "/c" }]')
    assert cfg["home"] is None
    assert all(m["name"] != "home" for m in cfg["mounts"] if m["kind"] == "volume")


def test_load_config_env_not_dict(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nenv = "notatable"\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="`env` must be a table"):
        load_config(ws)


def test_load_config_env_value_not_string(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\n[env]\nFOO = 42\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="must be a string"):
        load_config(ws)


def test_load_config_setup_not_array(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nsetup = "single string"\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="`setup` must be an array"):
        load_config(ws)


def test_load_config_setup_item_not_string(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "bad", 'image = "x"\nsetup = [42]\n')
    ws = Workspace("bad")
    with pytest.raises(ConfigError, match="setup\\[0\\] must be a string"):
        load_config(ws)


# ---- template scaffold round-trip -------------------------------------------


_DEFAULT_IMAGE = "mcr.microsoft.com/devcontainers/base:ubuntu"


def test_render_template_is_valid_toml(xdg):
    """render_template output must be TOML-parseable, contain the name, and carry
    the literal default image (no `image` arg -- the template owns it)."""
    import tomllib
    from credproxy_cli.core.model.config import render_template

    text = render_template("myprojx")
    assert "myprojx" in text
    parsed = tomllib.loads(text)
    assert parsed.get("image") == _DEFAULT_IMAGE


def test_render_template_scaffolds_active_nonroot_devcontainer(xdg, workspaces_dir):
    """The literal scaffold wires the non-root vscode user, its home,
    map_host_user, and the active CA-bootstrap setup -- loads cleanly, no edits."""
    from credproxy_cli.core.model.config import load_config, render_template
    from credproxy_cli.core.model.workspace import Workspace

    (workspaces_dir / "dc.toml").write_text(render_template("dc"))
    cfg = load_config(Workspace("dc"))
    assert cfg["image"] == _DEFAULT_IMAGE
    assert cfg["user"] == "vscode"
    assert cfg["home"] == "/home/vscode"
    assert cfg["map_host_user"] is True
    assert cfg["user_uid"] == 1000
    assert cfg["setup"] == ["curl -fsSL http://proxy.local/bootstrap.sh | sh"]
    # the `home` sugar produces a managed home volume mount
    assert cfg["mounts"] == [{"kind": "volume", "name": "home",
                              "target": "/home/vscode", "readonly": False}]
    assert cfg["env"] == {}


# ---- spec hash ---------------------------------------------------------------


def test_spec_hash_stable(xdg):
    """Same inputs yield the same hash."""
    from credproxy_cli.core.model.config import workspace_spec_hash

    cfg = {"image": "x", "home": "/h", "mounts": [], "env": {}, "setup": []}
    h1 = workspace_spec_hash(cfg, "abc")
    h2 = workspace_spec_hash(cfg, "abc")
    assert h1 == h2
    assert len(h1) == 16


def test_spec_hash_changes_on_image(xdg):
    from credproxy_cli.core.model.config import workspace_spec_hash

    base = {"image": "x", "home": "/h", "mounts": [], "env": {}, "setup": []}
    import copy
    alt = copy.deepcopy(base)
    alt["image"] = "y"
    assert workspace_spec_hash(base, None) != workspace_spec_hash(alt, None)


def test_spec_hash_changes_on_proxy_id(xdg):
    from credproxy_cli.core.model.config import workspace_spec_hash

    cfg = {"image": "x", "home": "/h", "mounts": [], "env": {}, "setup": []}
    assert workspace_spec_hash(cfg, "a") != workspace_spec_hash(cfg, "b")


def test_spec_hash_ignores_user_and_exec_flags(xdg):
    """user/exec_flags/workdir are exec-only -> changing them must NOT change the
    spec hash (no container recreate)."""
    from credproxy_cli.core.model.config import workspace_spec_hash

    base = {"image": "x", "home": "/h", "mounts": [], "env": {}, "setup": []}
    withuser = {**base, "user": "dev", "exec_flags": ["--workdir", "/srv"],
                "workdir": "/code", "enter_prelude": "export X=1", "shell": ["zsh"],
                "forward_env": ["MY_VAR"]}
    assert workspace_spec_hash(base, "p") == workspace_spec_hash(withuser, "p")


def test_spec_hash_changes_on_run_flags(xdg):
    """run_flags shape the container -> changing them MUST change the spec hash
    (forces a recreate). A missing run_flags hashes the same as []."""
    from credproxy_cli.core.model.config import workspace_spec_hash

    base = {"image": "x", "home": "/h", "mounts": [], "env": {}, "setup": []}
    assert workspace_spec_hash(base, "p") == workspace_spec_hash({**base, "run_flags": []}, "p")
    withflags = {**base, "run_flags": ["--userns=keep-id:uid=1000,gid=1000"]}
    assert workspace_spec_hash(base, "p") != workspace_spec_hash(withflags, "p")


# ---- user / exec_flags -------------------------------------------------------


def test_load_config_user_and_exec_flags(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "u", """
        image = "alpine:3"
        user = "dev"
        exec_flags = ["--workdir", "/srv"]
    """)
    cfg = load_config(Workspace("u"))
    assert cfg["user"] == "dev"
    assert cfg["exec_flags"] == ["--workdir", "/srv"]


def test_load_config_user_exec_flags_default(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "d", 'image = "alpine:3"\n')
    cfg = load_config(Workspace("d"))
    assert cfg["user"] is None
    assert cfg["exec_flags"] == []


def test_load_config_user_not_string(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nuser = 5\n')
    with pytest.raises(ConfigError, match="`user` must be a non-empty string"):
        load_config(Workspace("b"))


def test_load_config_exec_flags_not_list_of_strings(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nexec_flags = [1, 2]\n')
    with pytest.raises(ConfigError, match="`exec_flags` must be an array of strings"):
        load_config(Workspace("b"))


# ---- forward_env -------------------------------------------------------------


def test_load_config_forward_env(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "f", """
        image = "alpine:3"
        forward_env = ["MY_VAR", "OTHER"]
    """)
    assert load_config(Workspace("f"))["forward_env"] == ["MY_VAR", "OTHER"]


def test_load_config_forward_env_default_empty(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "d", 'image = "alpine:3"\n')
    assert load_config(Workspace("d"))["forward_env"] == []


def test_load_config_forward_env_not_list_of_strings(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nforward_env = [1]\n')
    with pytest.raises(ConfigError, match="`forward_env` must be an array of strings"):
        load_config(Workspace("b"))


def test_load_config_forward_env_rejects_value_form(xdg, workspaces_dir):
    """Bare names only -- a `VAR=value` entry is a config error (that's what
    `exec_flags`/`env` are for)."""
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nforward_env = ["FOO=bar"]\n')
    with pytest.raises(ConfigError, match="bare env var names"):
        load_config(Workspace("b"))


def test_load_config_forward_env_rejects_glob(xdg, workspaces_dir):
    """A trailing `*` is a podman prefix-glob (`LC_*` forwards every LC_ var) --
    rejected as a name so it can't silently over-forward on podman."""
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nforward_env = ["LC_*"]\n')
    with pytest.raises(ConfigError, match="bare env var names"):
        load_config(Workspace("b"))


# ---- workdir -----------------------------------------------------------------


def test_load_config_workdir(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "alpine:3"\nworkdir = "/code"\n')
    assert load_config(Workspace("w"))["workdir"] == "/code"


def test_load_config_workdir_default(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "wd", 'image = "alpine:3"\n')
    assert load_config(Workspace("wd"))["workdir"] is None


def test_load_config_workdir_not_absolute(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nworkdir = "relative"\n')
    with pytest.raises(ConfigError, match="`workdir` must be an absolute path"):
        load_config(Workspace("b"))


# ---- enter_prelude -----------------------------------------------------------


def test_load_config_enter_prelude(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "p", 'image = "alpine:3"\nenter_prelude = "export X=1"\n')
    assert load_config(Workspace("p"))["enter_prelude"] == "export X=1"


def test_load_config_enter_prelude_default_and_empty(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "pd", 'image = "alpine:3"\n')
    assert load_config(Workspace("pd"))["enter_prelude"] is None
    # explicit "" is a valid value (disables the shim)
    _write(workspaces_dir, "pe", 'image = "alpine:3"\nenter_prelude = ""\n')
    assert load_config(Workspace("pe"))["enter_prelude"] == ""


def test_load_config_enter_prelude_not_string(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nenter_prelude = 42\n')
    with pytest.raises(ConfigError, match="`enter_prelude` must be a string"):
        load_config(Workspace("b"))


# ---- shell -------------------------------------------------------------------


def test_load_config_shell(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "s", 'image = "alpine:3"\nshell = ["zsh"]\n')
    assert load_config(Workspace("s"))["shell"] == ["zsh"]


def test_load_config_shell_default_none(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "sd", 'image = "alpine:3"\n')
    assert load_config(Workspace("sd"))["shell"] is None


def test_load_config_shell_not_list(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nshell = "zsh"\n')
    with pytest.raises(ConfigError, match="`shell` must be a non-empty array"):
        load_config(Workspace("b"))


def test_load_config_shell_empty(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nshell = []\n')
    with pytest.raises(ConfigError, match="`shell` must be a non-empty array"):
        load_config(Workspace("b"))


# ---- declared_config (config --declared) -------------------------------------


def test_declared_config_raw_keys_no_defaults(xdg, workspaces_dir):
    """declared_config returns exactly what's in the file, before defaults, and
    excludes the [[binding]] array."""
    from credproxy_cli.core.model.config import declared_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "d", """
        image = "alpine:3"
        user = "dev"
        [[binding]]
        injector = "bearer"
        provider = "env"
        secret = "X"
        hosts = ["h"]
    """)
    assert declared_config(Workspace("d")) == {"image": "alpine:3", "user": "dev"}


def test_declared_config_missing_file(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, declared_config
    from credproxy_cli.core.model.workspace import Workspace

    with pytest.raises(ConfigError, match="not found"):
        declared_config(Workspace("ghost"))


# ---- run_flags ---------------------------------------------------------------


def test_load_config_run_flags(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "r", """
        image = "alpine:3"
        run_flags = ["--userns=keep-id:uid=1000,gid=1000"]
    """)
    cfg = load_config(Workspace("r"))
    assert cfg["run_flags"] == ["--userns=keep-id:uid=1000,gid=1000"]


def test_load_config_run_flags_default(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "rd", 'image = "alpine:3"\n')
    assert load_config(Workspace("rd"))["run_flags"] == []


def test_load_config_run_flags_not_list_of_strings(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nrun_flags = [1, 2]\n')
    with pytest.raises(ConfigError, match="`run_flags` must be an array of strings"):
        load_config(Workspace("b"))


# ---- map_host_user -----------------------------------------------------------


def test_load_config_map_host_user(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "m", 'image = "alpine:3"\nuser = "dev"\nmap_host_user = true\n')
    assert load_config(Workspace("m"))["map_host_user"] is True


def test_load_config_map_host_user_default(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "md", 'image = "alpine:3"\n')
    assert load_config(Workspace("md"))["map_host_user"] is False


def test_load_config_map_host_user_not_bool(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nmap_host_user = "yes"\n')
    with pytest.raises(ConfigError, match="`map_host_user` must be a boolean"):
        load_config(Workspace("b"))


def test_spec_hash_changes_on_map_host_user(xdg):
    """map_host_user shapes the container -> changing it changes the spec hash."""
    from credproxy_cli.core.model.config import workspace_spec_hash

    base = {"image": "x", "home": "/h", "mounts": [], "env": {}, "setup": []}
    assert workspace_spec_hash(base, "p") == workspace_spec_hash({**base, "map_host_user": False}, "p")
    assert workspace_spec_hash(base, "p") != workspace_spec_hash({**base, "map_host_user": True}, "p")


def test_spec_hash_changes_on_user_uid(xdg):
    """user_uid shapes the userns -> changing it changes the spec hash."""
    from credproxy_cli.core.model.config import workspace_spec_hash

    base = {"image": "x", "home": "/h", "mounts": [], "env": {}, "setup": []}
    assert workspace_spec_hash(base, "p") != workspace_spec_hash({**base, "user_uid": 1000}, "p")


def test_spec_hash_changes_on_hostname(xdg):
    """The container hostname rides the spec hash: adding it (or changing it)
    yields a new hash, so a pre-feature workspace recreates once to pick up the
    flag. It's passed as the third arg (name-derived), not a cfg field."""
    from credproxy_cli.core.model.config import workspace_spec_hash

    base = {"image": "x", "home": "/h", "mounts": [], "env": {}, "setup": []}
    # default (no hostname) differs from a set one -> pre-feature recreate once
    assert workspace_spec_hash(base, "p") != workspace_spec_hash(base, "p", "myproj")
    # different hostnames differ
    assert workspace_spec_hash(base, "p", "a") != workspace_spec_hash(base, "p", "b")
    # same hostname is stable
    assert workspace_spec_hash(base, "p", "a") == workspace_spec_hash(base, "p", "a")


def test_load_config_user_uid(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "u", 'image = "alpine:3"\nuser = "vscode"\nuser_uid = 1000\n')
    assert load_config(Workspace("u"))["user_uid"] == 1000


def test_load_config_user_uid_default_none(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "ud", 'image = "alpine:3"\n')
    assert load_config(Workspace("ud"))["user_uid"] is None


def test_load_config_user_uid_invalid(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    for bad in ('user_uid = -1', 'user_uid = "1000"', 'user_uid = true'):
        _write(workspaces_dir, "b", f'image = "alpine:3"\nuser = "dev"\n{bad}\n')
        with pytest.raises(ConfigError, match="`user_uid` must be a non-negative integer"):
            load_config(Workspace("b"))


def test_map_host_user_requires_user(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nmap_host_user = true\n')
    with pytest.raises(ConfigError, match="`map_host_user` require[s]? `user`"):
        load_config(Workspace("b"))


def test_user_uid_requires_user(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nuser_uid = 1000\n')
    with pytest.raises(ConfigError, match="`user_uid` require[s]? `user`"):
        load_config(Workspace("b"))


def test_both_orphans_named_in_error(xdg, workspaces_dir):
    """Both offenders are named when both are set without `user`."""
    from credproxy_cli.core.model.config import ConfigError, load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "alpine:3"\nmap_host_user = true\nuser_uid = 1000\n')
    with pytest.raises(ConfigError, match="`map_host_user` and `user_uid` require `user`"):
        load_config(Workspace("b"))


# ---- add_volume_mount / write_added_mount (surgical TOML edits) --------------


def _added(text, name, target, readonly=False):
    """Apply add_volume_mount and return (new_text, parsed_mounts)."""
    import tomllib
    from credproxy_cli.core.model.config import add_volume_mount
    new = add_volume_mount(text, name, target, readonly)
    return new, tomllib.loads(new).get("mounts")


def test_add_volume_mount_absent_appends_block(xdg):
    """No `mounts` key -> a [[mounts]] block is appended (comments preserved)."""
    text = 'image = "x"\n# keep me\nhome = "/h"\n'
    new, mounts = _added(text, "cache", "/c")
    assert "# keep me" in new
    assert "[[mounts]]" in new
    assert mounts == [{"volume": "cache", "target": "/c"}]


def test_add_volume_mount_inline_nonempty_prepends(xdg):
    text = 'image = "x"\nmounts = ["~/code:/code"]\n'
    new, mounts = _added(text, "cache", "/c", readonly=True)
    assert mounts == [{"volume": "cache", "target": "/c", "readonly": True},
                      "~/code:/code"]


def test_add_volume_mount_inline_empty(xdg):
    new, mounts = _added('image = "x"\nmounts = []\n', "cache", "/c")
    assert mounts == [{"volume": "cache", "target": "/c"}]


def test_add_volume_mount_multiline_with_comment(xdg):
    text = 'image = "x"\nmounts = [\n  "~/code:/code",  # bind\n]\n'
    new, mounts = _added(text, "cache", "/c")
    assert "# bind" in new                       # comment preserved
    assert {"volume": "cache", "target": "/c"} in mounts
    assert "~/code:/code" in mounts


def test_add_volume_mount_ignores_commented_mounts(xdg):
    """A commented-out `mounts = [...]` (the template) is not edited; a block is
    appended instead, and the commented lines survive verbatim."""
    text = 'image = "x"\n# mounts = [\n#   "a:/b",\n# ]\n'
    new, mounts = _added(text, "cache", "/c")
    assert "# mounts = [" in new
    assert "[[mounts]]" in new
    assert mounts == [{"volume": "cache", "target": "/c"}]


def test_add_volume_mount_twice_yields_two_blocks(xdg):
    """Repeated adds on a block-style file append more [[mounts]] blocks (valid
    TOML array-of-tables), never an inline/array mix."""
    import tomllib
    from credproxy_cli.core.model.config import add_volume_mount
    text = add_volume_mount('image = "x"\n', "cache", "/c")
    text = add_volume_mount(text, "data", "/d")
    mounts = tomllib.loads(text).get("mounts")
    assert mounts == [{"volume": "cache", "target": "/c"},
                      {"volume": "data", "target": "/d"}]


def test_add_volume_mount_escapes_quotes(xdg):
    import tomllib
    from credproxy_cli.core.model.config import add_volume_mount
    new = add_volume_mount('image = "x"\n', "cache", '/weird"path')
    assert tomllib.loads(new)["mounts"] == [{"volume": "cache",
                                             "target": '/weird"path'}]


def test_write_added_mount_home_uses_sugar(xdg, workspaces_dir):
    """A volume named `home` is written as the `home = ...` top-level sugar, not
    a mounts entry (which would collide with the sugar at load time)."""
    import tomllib
    from credproxy_cli.core.model.config import write_added_mount
    from credproxy_cli.core.model.workspace import Workspace
    _write(workspaces_dir, "w", 'image = "x"\n')
    ws = Workspace("w")
    write_added_mount(ws, "home", "/home/vscode", False)
    raw = tomllib.loads(ws.config_path.read_text())
    assert raw["home"] == "/home/vscode"
    assert "mounts" not in raw


def test_write_added_mount_roundtrips_through_load_config(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config, write_added_mount
    from credproxy_cli.core.model.workspace import Workspace
    _write(workspaces_dir, "w", 'image = "x"\n')
    ws = Workspace("w")
    write_added_mount(ws, "cache", "/c", True)
    cfg = load_config(ws)
    assert {"kind": "volume", "name": "cache", "target": "/c",
            "readonly": True} in cfg["mounts"]


# ---- unknown top-level keys (#17) --------------------------------------------


def test_unknown_top_level_key_rejected(xdg, workspaces_dir):
    """A typo'd key silently no-ops otherwise -- reject it, naming the key."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\nsetup_cmd = ["echo hi"]\n')
    with pytest.raises(ConfigError) as ei:
        load_config(Workspace("w"))
    assert "unknown key(s)" in str(ei.value)
    assert "setup_cmd" in str(ei.value)


def test_unknown_key_suggests_close_match(xdg, workspaces_dir):
    """`mount` -> did-you-mean `mounts` (the exact trap the issue names)."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\nmount = []\n')
    with pytest.raises(ConfigError) as ei:
        load_config(Workspace("w"))
    assert "did you mean `mounts`?" in str(ei.value)


def test_binding_and_rule_tables_still_load(xdg, workspaces_dir):
    """`[[binding]]`/`[[rule]]` are parsed by their own modules; load_config must
    treat both as known top-level keys, not reject them."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", """
        image = "x"
        [[binding]]
        name = "b"
        [[rule]]
        name = "r"
    """)
    cfg = load_config(Workspace("w"))   # must not raise
    assert cfg["image"] == "x"


# ---- auto_stop (#17) ---------------------------------------------------------


def test_auto_stop_string_false_rejected(xdg, workspaces_dir):
    """`auto_stop = "false"` is a truthy STRING that would silently enable
    auto-stop; a strict bool check rejects it."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\nauto_stop = "false"\n')
    with pytest.raises(ConfigError) as ei:
        load_config(Workspace("w"))
    assert "auto_stop" in str(ei.value) and "boolean" in str(ei.value)


def test_auto_stop_defaults_false_and_roundtrips(xdg, workspaces_dir):
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "w", 'image = "x"\n')
    assert load_config(Workspace("w"))["auto_stop"] is False

    _write(workspaces_dir, "w2", 'image = "x"\nauto_stop = true\n')
    assert load_config(Workspace("w2"))["auto_stop"] is True

    # ...and it surfaces in `config --effective` (was absent from the dict before).
    from credproxy_cli.core.engine.sessions import effective_config
    assert effective_config(load_config(Workspace("w2")))["auto_stop"] is True


def test_auto_stop_not_in_spec_hash(xdg, workspaces_dir):
    """auto_stop is host-side session behavior -- toggling it must NOT recreate
    the container, so it can't enter the spec hash."""
    from credproxy_cli.core.model.config import load_config, workspace_spec_hash
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "a", 'image = "x"\nauto_stop = true\n')
    _write(workspaces_dir, "b", 'image = "x"\nauto_stop = false\n')
    h_on = workspace_spec_hash(load_config(Workspace("a")), "proxy1")
    h_off = workspace_spec_hash(load_config(Workspace("b")), "proxy1")
    assert h_on == h_off


# ---- typed `setup` entries (issue #55) ---------------------------------------


def test_setup_string_entries_stay_strings(xdg, workspaces_dir):
    """A plain-string setup array is left byte-identical -- strings stay strings,
    the escape hatch preserved exactly as before typed entries."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "s", 'image = "x"\nsetup = ["echo a", "echo b"]\n')
    assert load_config(Workspace("s"))["setup"] == ["echo a", "echo b"]


def test_setup_table_normalized_with_defaults(xdg, workspaces_dir):
    """A bare `{run="…"}` table is normalized to the canonical dict with every
    default filled: user -> "workspace", order -> 0."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "t", 'image = "x"\nsetup = [{ run = "echo hi" }]\n')
    assert load_config(Workspace("t"))["setup"] == [
        {"run": "echo hi", "user": "workspace", "order": 0}
    ]


def test_setup_table_explicit_fields(xdg, workspaces_dir):
    """Explicit user/order are carried through verbatim."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "t", 'image = "x"\n'
           'setup = [{ run = "apt-get update", user = "root", order = 10 }]\n')
    assert load_config(Workspace("t"))["setup"] == [
        {"run": "apt-get update", "user": "root", "order": 10}
    ]


def test_setup_mixed_array(xdg, workspaces_dir):
    """Strings and tables coexist in one array; strings stay strings, tables
    normalize."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "m", 'image = "x"\n'
           'setup = ["curl x", { run = "b", order = 5 }]\n')
    assert load_config(Workspace("m"))["setup"] == [
        "curl x",
        {"run": "b", "user": "workspace", "order": 5},
    ]


def test_setup_rejects_non_string_non_table(xdg, workspaces_dir):
    """A number (or any non-string/non-table) is rejected, index-named."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "x"\nsetup = ["ok", 42]\n')
    with pytest.raises(ConfigError, match=r"setup\[1\] must be a string or a table"):
        load_config(Workspace("b"))


def test_setup_table_unknown_key_rejected(xdg, workspaces_dir):
    """An unknown table key is rejected, naming the index (mirrors _parse_mount)."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "x"\n'
           'setup = [{ run = "x", shell = "zsh" }]\n')
    with pytest.raises(ConfigError, match=r"setup\[0\] unknown key\(s\): shell"):
        load_config(Workspace("b"))


def test_setup_table_run_required(xdg, workspaces_dir):
    """`run` is required and must be a non-empty string."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "x"\nsetup = [{ user = "root" }]\n')
    with pytest.raises(ConfigError, match=r"setup\[0\] `run` is required"):
        load_config(Workspace("b"))

    _write(workspaces_dir, "b2", 'image = "x"\nsetup = [{ run = "" }]\n')
    with pytest.raises(ConfigError, match=r"setup\[0\] `run` is required"):
        load_config(Workspace("b2"))


def test_setup_table_user_literal_rejected(xdg, workspaces_dir):
    """`user` accepts only "workspace"/"root" in v1 -- a literal username is
    rejected."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    _write(workspaces_dir, "b", 'image = "x"\n'
           'setup = [{ run = "x", user = "vscode" }]\n')
    with pytest.raises(ConfigError, match=r'setup\[0\] `user` must be "workspace" or "root"'):
        load_config(Workspace("b"))


def test_setup_table_order_must_be_nonneg_int(xdg, workspaces_dir):
    """`order` must be an int >= 0 -- a negative, a float, and a bool are all
    rejected (bool is an int subclass, guarded explicitly)."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.workspace import Workspace

    for val in ("-1", "1.5", "true"):
        _write(workspaces_dir, "b", 'image = "x"\n'
               f'setup = [{{ run = "x", order = {val} }}]\n')
        with pytest.raises(ConfigError, match=r"setup\[0\] `order` must be an integer"):
            load_config(Workspace("b"))


# ---- typed `setup` and the spec hash -----------------------------------------


def _spec(workspaces_dir, name, setup_toml):
    from credproxy_cli.core.model.config import load_config, workspace_spec_hash
    from credproxy_cli.core.model.workspace import Workspace
    _write(workspaces_dir, name, f'image = "x"\nsetup = {setup_toml}\n')
    return workspace_spec_hash(load_config(Workspace(name)), "proxy1")


def test_setup_hash_default_forms_equal(xdg, workspaces_dir):
    """`{run="x"}` and its fully-spelled default-equivalent hash identically --
    normalization collapses them to the same canonical dict."""
    a = _spec(workspaces_dir, "a", '[{ run = "x" }]')
    b = _spec(workspaces_dir, "b", '[{ run = "x", user = "workspace", order = 0 }]')
    assert a == b


def test_setup_hash_all_string_unchanged(xdg, workspaces_dir):
    """An all-string setup hashes the same as it always did (strings stay
    strings in the canonical form) -- no drift for a pre-feature config."""
    a = _spec(workspaces_dir, "a", '["echo a", "echo b"]')
    b = _spec(workspaces_dir, "b", '["echo a", "echo b"]')
    assert a == b


def test_setup_hash_changes_on_any_table_field(xdg, workspaces_dir):
    """Editing run/user/order each changes the hash (drift -> recreate)."""
    base = _spec(workspaces_dir, "base", '[{ run = "x", user = "workspace", order = 0 }]')
    diff_run = _spec(workspaces_dir, "r", '[{ run = "y", user = "workspace", order = 0 }]')
    diff_user = _spec(workspaces_dir, "u", '[{ run = "x", user = "root", order = 0 }]')
    diff_order = _spec(workspaces_dir, "o", '[{ run = "x", user = "workspace", order = 5 }]')
    assert base != diff_run
    assert base != diff_user
    assert base != diff_order
