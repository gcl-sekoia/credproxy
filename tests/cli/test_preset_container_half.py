"""Presets carry the container half: `[[mount]]` / `[env]` / `[[setup]]` (#56).

Covers _parse_preset acceptance/rejection for the new sections, shared-validator
parity with workspace-config parsing, the surgical stamping (golden text), the
collision matrix + double-add guard, qualified overlay-source resolution per
tier, the attached-workspace refusal + recreate announcement, and the post-stamp
loader round-trip.
"""
from __future__ import annotations

import textwrap

import pytest

from test_porcelain import _run


# ---- helpers -----------------------------------------------------------------


def _write_preset(name: str, toml: str, *, tier: str = "user"):
    """Install a preset TOML in a tier (user / an overlay dir under config).
    Returns the preset dir path so callers can drop pack files beside it."""
    from credproxy_cli.core.paths import config_dir
    base = config_dir() if tier == "user" else config_dir() / "_ov" / tier
    pd = base / "presets"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{name}.toml").write_text(textwrap.dedent(toml))
    return base


def _pack_file(base, rel: str, body: str = "echo hi\n"):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def _make_ws(name: str, content: str):
    from credproxy_cli.core.paths import workspaces_config_dir
    from credproxy_cli.core.model.workspace import Workspace
    wd = workspaces_config_dir()
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{name}.toml").write_text(textwrap.dedent(content))
    return Workspace(name)


_WS_MIN = 'image = "python:3.12-slim"\nuser = "vscode"\n'


# ---- _parse_preset acceptance / rejection ------------------------------------


def test_parse_accepts_mount_env_setup(xdg):
    from credproxy_cli.core.model.presets import get_preset
    base = _write_preset("cont", """
        [[mount]]
        overlay = "setup.d/x.sh"
        target = "/opt/x.sh"
        [[mount]]
        volume = "cache"
        target = "/cache"
        [env]
        FOO = "bar"
        [[setup]]
        run = "bash /opt/x.sh"
        order = 30
    """)
    _pack_file(base, "setup.d/x.sh")
    spec = get_preset("cont")
    assert spec.needs_credential is False and spec.has_container_half is True
    assert [m.kind for m in spec.mounts] == ["overlay", "volume"]
    # overlay source is qualified to the pack's owning tier.
    assert spec.mounts[0].value == "user:setup.d/x.sh"
    assert spec.env == (("FOO", "bar"),)
    assert spec.setup == ({"run": "bash /opt/x.sh", "user": "workspace", "order": 30},)


def test_parse_rejects_empty_pack(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("empty", "default_provider = \"env\"\n")
    with pytest.raises(ConfigError, match="at least one"):
        load_presets()


def test_parse_setup_requires_order(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("noorder", '[[setup]]\nrun = "x"\n')
    with pytest.raises(ConfigError, match="order.*required"):
        load_presets()


def test_parse_setup_rejects_bare_string(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("strsetup", 'setup = ["do a thing"]\n')
    with pytest.raises(ConfigError, match="must be a table"):
        load_presets()


def test_parse_env_rejects_non_string_value(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("badenv", "[env]\nN = 7\n")
    with pytest.raises(ConfigError, match="must be a non-empty string"):
        load_presets()


def test_parse_env_rejects_empty_value(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("emptyenv", '[env]\nN = ""\n')
    with pytest.raises(ConfigError, match="non-empty string"):
        load_presets()


# ---- shared-validator parity -------------------------------------------------


def test_mount_validator_parity_with_workspace(xdg):
    """An invalid mount fails identically in a workspace config and a preset --
    both go through config._parse_mount."""
    from credproxy_cli.core.model import config as core_config
    from credproxy_cli.core.errors import ConfigError
    bad = {"volume": "cache", "overlay": "x", "target": "/c"}  # two kinds
    with pytest.raises(ConfigError, match="exactly one of bind/volume/overlay") as a:
        core_config._parse_mount(bad, "ws")
    with pytest.raises(ConfigError, match="exactly one of bind/volume/overlay") as b:
        core_config._parse_mount(bad, "ws", expand_bind=False)
    # And the preset path surfaces the same underlying validator error.
    _write_preset("twokind",
                  '[[mount]]\nvolume = "cache"\nbind = "/x"\ntarget = "/c"\n')
    from credproxy_cli.core.model.presets import load_presets
    with pytest.raises(ConfigError, match="exactly one of bind/volume/overlay"):
        load_presets()


def test_setup_validator_parity(xdg):
    """The preset path reuses config._parse_setup_table -- a bad `user` value
    fails the same way it does in a workspace config."""
    from credproxy_cli.core.model import config as core_config
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match='must be "workspace" or "root"'):
        core_config._parse_setup_table({"run": "x", "user": "bob"}, "w",
                                       require_order=True)
    _write_preset("baduser",
                  '[[setup]]\nrun = "x"\nuser = "bob"\norder = 1\n')
    from credproxy_cli.core.model.presets import load_presets
    with pytest.raises(ConfigError, match='must be "workspace" or "root"'):
        load_presets()


# ---- qualified overlay-source resolution -------------------------------------


def test_qualified_source_resolves_per_tier(xdg, monkeypatch):
    """A pack in an overlay stamps `overlay = "<tier>:rel"`, pinned to that
    overlay's dir, and it resolves via config._overlay_source."""
    import os
    from credproxy_cli.core.model import config as core_config
    from credproxy_cli.core.paths import config_dir
    ov = config_dir() / "_ovdir"
    (ov / "presets").mkdir(parents=True)
    _pack_file(ov, "setup.d/y.sh")
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    resolved = core_config._overlay_source("_ovdir:setup.d/y.sh", "w")
    assert resolved == str((ov / "setup.d" / "y.sh").resolve())


def test_qualified_source_user_and_builtin_tiers(xdg):
    from credproxy_cli.core.model import config as core_config
    from credproxy_cli.core.paths import config_dir
    _pack_file(config_dir(), "setup.d/z.sh")
    assert core_config._overlay_source("user:setup.d/z.sh", "w") \
        == str((config_dir() / "setup.d" / "z.sh").resolve())
    # builtin tier: an existing builtin file (a preset TOML) resolves.
    got = core_config._overlay_source("builtin:presets/github.toml", "w")
    assert got.endswith("builtin/presets/github.toml")


def test_qualified_source_unknown_tier(xdg):
    from credproxy_cli.core.model import config as core_config
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match="unknown tier 'nope'"):
        core_config._overlay_source("nope:x.sh", "w")


def test_qualified_source_escape_rejected(xdg):
    from credproxy_cli.core.model import config as core_config
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match="escapes"):
        core_config._overlay_source("user:../../etc/passwd", "w")


def test_preset_qualifies_overlay_to_owning_overlay_tier(xdg, monkeypatch):
    import os
    from credproxy_cli.core.paths import config_dir
    ov = config_dir() / "acme"
    (ov / "presets").mkdir(parents=True)
    _pack_file(ov, "setup.d/a.sh")
    (ov / "presets" / "acme-cont.toml").write_text(
        '[[mount]]\noverlay = "setup.d/a.sh"\ntarget = "/opt/a.sh"\n')
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    from credproxy_cli.core.model.presets import get_preset
    spec = get_preset("acme-cont")
    assert spec.mounts[0].value == "acme:setup.d/a.sh"


# ---- stamping (golden text) --------------------------------------------------


def _install_cont_preset(name="cont", *, with_binding=False):
    """A container-half pack (+ optional binding) with its overlay pack file."""
    binding = (
        '[placeholder]\nprefix = "ghp_"\nlength = 40\ncharset = "alnumeric"\n'
        '[[part]]\nsuffix = "api"\ninjector = "bearer"\n'
        'hosts = ["api.github.com"]\nenv = "GITHUB_TOKEN"\n'
    ) if with_binding else ""
    base = _write_preset(name, binding + """
        [[mount]]
        overlay = "setup.d/c.sh"
        target = "/opt/c.sh"
        [env]
        C_VAR = "one"
        [[setup]]
        run = "bash /opt/c.sh"
        order = 45
    """)
    _pack_file(base, "setup.d/c.sh")


def test_mount_target_collision(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + textwrap.dedent('''
        [[mounts]]
        volume = "v"
        target = "/opt/c.sh"
    '''))
    code, out, err = _run(["workspace", "w", "preset", "add", "cont"])
    assert code == 1 and "already mounted" in (out + err)
    # Nothing stamped.
    assert "C_VAR" not in ws.config_path.read_text()


def test_env_different_value_fails(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + '\n[env]\nC_VAR = "other"\n')
    before = ws.config_path.read_text()
    code, out, err = _run(["workspace", "w", "preset", "add", "cont"])
    assert code == 1 and "different value" in (out + err)
    assert ws.config_path.read_text() == before   # no partial write


# ---- attached refusal + recreate announcement --------------------------------


def test_attached_refuses_container_half(xdg):
    _install_cont_preset("cont", with_binding=True)
    ws = _make_ws("attd", 'attach = { container = "extbox" }\n')
    code, out, err = _run(["workspace", "attd", "preset", "add", "cont",
                           "--provider", "env", "--secret", "GITHUB_TOKEN"])
    assert code == 1 and "attached" in (out + err)
    # Nothing stamped.
    assert "cont-api" not in ws.config_path.read_text()


def test_recreate_announced_only_when_container_exists(xdg, monkeypatch):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN)
    # No container -> no recreate hint.
    from credproxy_cli.porcelain import cli as pcli
    monkeypatch.setattr(pcli.core_docker, "container_status", lambda _n: None)
    code, out, err = _run(["workspace", "w", "preset", "add", "cont"])
    assert code == 0 and "restart to apply" not in (out + err)

    # Container present -> recreate hint fires.
    ws2 = _make_ws("w2", _WS_MIN)
    monkeypatch.setattr(pcli.core_docker, "container_status", lambda _n: "running")
    code, out, err = _run(["workspace", "w2", "preset", "add", "cont"])
    assert code == 0, out + err
    assert "restart to apply: credproxy workspace w2 start" in (out + err)


# ---- describe (preset list JSON) ---------------------------------------------


def test_describe_includes_container_half(xdg):
    _install_cont_preset()
    from credproxy_cli.core.model.presets import describe_presets
    row = next(p for p in describe_presets() if p["name"] == "cont")
    assert row["mounts"] == [{"kind": "overlay", "source": "user:setup.d/c.sh",
                              "target": "/opt/c.sh"}]
    assert row["env"] == [{"key": "C_VAR", "value": "one"}]
    assert row["setup"] == [{"run": "bash /opt/c.sh", "user": "workspace",
                             "order": 45}]


# ============================================================================
# #56 review follow-ups
# ============================================================================



def test_overlay_named_user_rejected(xdg, monkeypatch):
    """An overlay dir literally named `user` shadows the reserved `user` tier
    qualifier -- loading its presets errors clearly."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.paths import config_dir
    from credproxy_cli.core.model.presets import load_presets
    ov = config_dir() / "ovs" / "user"
    (ov / "presets").mkdir(parents=True)
    (ov / "presets" / "p.toml").write_text('[env]\nX = "y"\n')
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    with pytest.raises(ConfigError, match="reserved 'user' tier qualifier"):
        load_presets()


def test_overlay_named_builtin_rejected(xdg, monkeypatch):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.paths import config_dir
    from credproxy_cli.core.model.presets import load_presets
    ov = config_dir() / "ovs" / "builtin"
    (ov / "presets").mkdir(parents=True)
    (ov / "presets" / "p.toml").write_text('[env]\nX = "y"\n')
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    with pytest.raises(ConfigError, match="reserved 'builtin' tier qualifier"):
        load_presets()


def test_duplicate_basename_qualifier_rejected(xdg, monkeypatch):
    """A duplicate-basename overlay's `#N` qualifier is order-dependent -- a pack
    in it that would auto-qualify an unqualified overlay mount errors clearly."""
    import os
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.paths import config_dir
    from credproxy_cli.core.model.presets import load_presets
    a = config_dir() / "a" / "base"
    b = config_dir() / "b" / "base"        # same basename -> b gets `base#2`
    (a / "presets").mkdir(parents=True)
    (b / "presets").mkdir(parents=True)
    _pack_file(b, "setup.d/x.sh")
    (b / "presets" / "dup.toml").write_text(
        '[[mount]]\noverlay = "setup.d/x.sh"\ntarget = "/opt/x.sh"\n')
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", os.pathsep.join([str(a), str(b)]))
    with pytest.raises(ConfigError, match="order-dependent duplicate-basename"):
        load_presets()


# ---- Finding 5: recreate hint gated on ACTUAL stamped content -----------------


def test_qualified_source_empty_subpath_rejected(xdg):
    from credproxy_cli.core.model import config as core_config
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match="tier root dir"):
        core_config._overlay_source("user:", "w")
    with pytest.raises(ConfigError, match="tier root dir"):
        core_config._overlay_source("user:.", "w")


# ---- Finding 7: pure-container pack rejects --provider with an apt message ----


def test_container_only_pack_rejects_provider(xdg):
    _install_cont_preset()          # no [[part]] -> pure container
    ws = _make_ws("w", _WS_MIN)
    code, out, err = _run(["workspace", "w", "preset", "add", "cont",
                           "--provider", "env"])
    assert code == 1
    assert "container-only" in (out + err) and "needs no credential" in (out + err)


# ---- Finding 12: double-add guard ignores a marker inside a string value ------


def _rbindings(ws):
    from credproxy_cli.core.model.resolver import resolve_workspace
    return resolve_workspace(ws).bindings


def _rconfig(ws):
    from credproxy_cli.core.model.resolver import resolve_workspace
    return resolve_workspace(ws).config


# ---- reference-model behavior (config-v2) ------------------------------------


def test_reference_expands_container_half(xdg):
    """A container-half pack referenced via `preset add` merges its mounts/env/
    setup into the resolved config (the expansion lives in the lock, not the TOML)."""
    _install_cont_preset("cont", with_binding=True)
    ws = _make_ws("w", _WS_MIN + '\nsetup = [\n  "echo pre",\n]\n')
    code, out, err = _run(["workspace", "w", "preset", "add", "cont",
                           "--provider", "env", "--secret", "GITHUB_TOKEN"])
    assert code == 0, out + err
    assert "[[preset]]" in ws.config_path.read_text()
    cfg = _rconfig(ws)
    assert cfg["env"] == {"C_VAR": "one"}
    assert {m["target"] for m in cfg["mounts"]} == {"/opt/c.sh"}
    assert cfg["setup"][-1] == {"run": "bash /opt/c.sh", "user": "workspace",
                                "order": 45}
    assert {b.name for b in _rbindings(ws)} == {"cont-api"}


def test_double_add_guard(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN)
    assert _run(["workspace", "w", "preset", "add", "cont"])[0] == 0
    code, out, err = _run(["workspace", "w", "preset", "add", "cont"])
    assert code == 1 and "already referenced" in (out + err)


def test_env_identical_value_skipped(xdg):
    """A preset env key that matches an existing literal value merges cleanly
    (identical value is fine)."""
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + '\n[env]\nC_VAR = "one"\n')
    code, out, err = _run(["workspace", "w", "preset", "add", "cont"])
    assert code == 0, out + err
    assert _rconfig(ws)["env"] == {"C_VAR": "one"}


def test_attached_allows_binding_only_pack(xdg):
    _write_preset("binonly",
                  '[placeholder]\nprefix = "ghp_"\nlength = 40\ncharset = "alnumeric"\n'
                  '[[part]]\nsuffix = "api"\ninjector = "bearer"\n'
                  'hosts = ["api.github.com"]\n')
    ws = _make_ws("attd", 'attach = { container = "extbox" }\n')
    code, out, err = _run(["workspace", "attd", "preset", "add", "binonly",
                           "--provider", "env", "--secret", "TOK"])
    assert code == 0, out + err
    assert {b.name for b in _rbindings(ws)} == {"binonly-api"}
