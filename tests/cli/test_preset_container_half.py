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
    from credproxy_cli.core.workspace import Workspace
    wd = workspaces_config_dir()
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{name}.toml").write_text(textwrap.dedent(content))
    return Workspace(name)


_WS_MIN = 'image = "python:3.12-slim"\nuser = "vscode"\n'


# ---- _parse_preset acceptance / rejection ------------------------------------


def test_parse_accepts_mount_env_setup(xdg):
    from credproxy_cli.core.presets import get_preset
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
    from credproxy_cli.core.presets import load_presets
    _write_preset("empty", "default_provider = \"env\"\n")
    with pytest.raises(ConfigError, match="at least one"):
        load_presets()


def test_parse_setup_requires_order(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("noorder", '[[setup]]\nrun = "x"\n')
    with pytest.raises(ConfigError, match="order.*required"):
        load_presets()


def test_parse_setup_rejects_bare_string(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("strsetup", 'setup = ["do a thing"]\n')
    with pytest.raises(ConfigError, match="must be a table"):
        load_presets()


def test_parse_env_rejects_non_string_value(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("badenv", "[env]\nN = 7\n")
    with pytest.raises(ConfigError, match="must be a non-empty string"):
        load_presets()


def test_parse_env_rejects_empty_value(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("emptyenv", '[env]\nN = ""\n')
    with pytest.raises(ConfigError, match="non-empty string"):
        load_presets()


# ---- shared-validator parity -------------------------------------------------


def test_mount_validator_parity_with_workspace(xdg):
    """An invalid mount fails identically in a workspace config and a preset --
    both go through config._parse_mount."""
    from credproxy_cli.core import config as core_config
    from credproxy_cli.core.errors import ConfigError
    bad = {"volume": "cache", "overlay": "x", "target": "/c"}  # two kinds
    with pytest.raises(ConfigError, match="exactly one of bind/volume/overlay") as a:
        core_config._parse_mount(bad, "ws")
    with pytest.raises(ConfigError, match="exactly one of bind/volume/overlay") as b:
        core_config._parse_mount(bad, "ws", expand_bind=False)
    # And the preset path surfaces the same underlying validator error.
    _write_preset("twokind",
                  '[[mount]]\nvolume = "cache"\nbind = "/x"\ntarget = "/c"\n')
    from credproxy_cli.core.presets import load_presets
    with pytest.raises(ConfigError, match="exactly one of bind/volume/overlay"):
        load_presets()


def test_setup_validator_parity(xdg):
    """The preset path reuses config._parse_setup_table -- a bad `user` value
    fails the same way it does in a workspace config."""
    from credproxy_cli.core import config as core_config
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match='must be "workspace" or "root"'):
        core_config._parse_setup_table({"run": "x", "user": "bob"}, "w",
                                       require_order=True)
    _write_preset("baduser",
                  '[[setup]]\nrun = "x"\nuser = "bob"\norder = 1\n')
    from credproxy_cli.core.presets import load_presets
    with pytest.raises(ConfigError, match='must be "workspace" or "root"'):
        load_presets()


# ---- qualified overlay-source resolution -------------------------------------


def test_qualified_source_resolves_per_tier(xdg, monkeypatch):
    """A pack in an overlay stamps `overlay = "<tier>:rel"`, pinned to that
    overlay's dir, and it resolves via config._overlay_source."""
    import os
    from credproxy_cli.core import config as core_config
    from credproxy_cli.core.paths import config_dir
    ov = config_dir() / "_ovdir"
    (ov / "presets").mkdir(parents=True)
    _pack_file(ov, "setup.d/y.sh")
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    resolved = core_config._overlay_source("_ovdir:setup.d/y.sh", "w")
    assert resolved == str((ov / "setup.d" / "y.sh").resolve())


def test_qualified_source_user_and_builtin_tiers(xdg):
    from credproxy_cli.core import config as core_config
    from credproxy_cli.core.paths import config_dir
    _pack_file(config_dir(), "setup.d/z.sh")
    assert core_config._overlay_source("user:setup.d/z.sh", "w") \
        == str((config_dir() / "setup.d" / "z.sh").resolve())
    # builtin tier: an existing builtin file (a preset TOML) resolves.
    got = core_config._overlay_source("builtin:presets/github.toml", "w")
    assert got.endswith("builtin/presets/github.toml")


def test_qualified_source_unknown_tier(xdg):
    from credproxy_cli.core import config as core_config
    from credproxy_cli.core.errors import ConfigError
    with pytest.raises(ConfigError, match="unknown tier 'nope'"):
        core_config._overlay_source("nope:x.sh", "w")


def test_qualified_source_escape_rejected(xdg):
    from credproxy_cli.core import config as core_config
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
    from credproxy_cli.core.presets import get_preset
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


def _stamp(ws, preset, provider=None, secret=None):
    from credproxy_cli.core import preset_stamp
    from credproxy_cli.core.presets import build_preset
    exp = build_preset(preset, provider, secret)
    preset_stamp.stamp(ws, preset, exp.rev, bindings=list(exp.bindings),
                       rules=list(exp.rules), mounts=list(exp.mounts),
                       env_items=list(exp.env), setup=[dict(s) for s in exp.setup])
    return exp


def test_stamp_creates_absent_keys_and_env(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "cont")
    text = ws.config_path.read_text()
    # The original two lines are untouched, byte for byte.
    assert text.startswith(_WS_MIN)
    assert "mounts = [\n" in text
    assert "[env]\n" in text
    assert 'C_VAR = "one"' in text
    assert "# credproxy:preset name=cont " in text


def test_stamp_into_multiline_setup_preserves_existing(xdg):
    _install_cont_preset()
    original = (
        'image = "python:3.12-slim"\n'
        'user = "vscode"\n\n'
        'setup = [\n'
        '  "curl http://proxy.local/bootstrap.sh | sh",  # keep me\n'
        ']\n'
    )
    ws = _make_ws("w", original)
    _stamp(ws, "cont")
    text = ws.config_path.read_text()
    # Existing element + its comment survive verbatim; the new step is appended
    # before the closing bracket.
    assert '"curl http://proxy.local/bootstrap.sh | sh",  # keep me' in text
    idx_old = text.index("curl http")
    idx_new = text.index("bash /opt/c.sh")
    assert idx_old < idx_new < text.index("]", idx_new)


def test_stamp_rewrites_single_line_empty_array(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + "mounts = []\n")
    _stamp(ws, "cont")
    text = ws.config_path.read_text()
    assert "mounts = [\n" in text and "/opt/c.sh" in text


def test_stamp_appends_to_existing_env_section(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + '\n[env]\nEXIST = "yes"\n')
    _stamp(ws, "cont")
    text = ws.config_path.read_text()
    assert 'EXIST = "yes"' in text and 'C_VAR = "one"' in text
    # Exactly one [env] header (no duplicate section created).
    assert text.count("[env]") == 1


def test_stamp_file_ending_in_binding_block(xdg):
    """mounts/setup/env must land in the ROOT region, not nest under a trailing
    [[binding]] table."""
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + textwrap.dedent('''
        [[binding]]
        name = "manual"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["h.example"]
        placeholder = "ph_xyz"
    '''))
    _stamp(ws, "cont")
    from credproxy_cli.core.config import load_config
    cfg = load_config(ws)   # would raise if a key nested under [[binding]]
    assert cfg["env"] == {"C_VAR": "one"}
    assert [m["target"] for m in cfg["mounts"]] == ["/opt/c.sh"]
    from credproxy_cli.core.bindings import load_bindings
    assert {b.name for b in load_bindings(ws)} == {"manual"}


def test_stamp_appends_to_mounts_block_form(xdg):
    """A workspace whose mounts are [[mounts]] blocks gets appended [[mounts]]
    blocks (never a colliding `mounts =` key)."""
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + textwrap.dedent('''
        [[mounts]]
        volume = "cache"
        target = "/cache"
    '''))
    _stamp(ws, "cont")
    text = ws.config_path.read_text()
    assert "mounts = [" not in text            # no inline key introduced
    assert text.count("[[mounts]]") == 2
    from credproxy_cli.core.config import load_config
    assert {m["target"] for m in load_config(ws)["mounts"]} == {"/cache", "/opt/c.sh"}


# ---- round-trip through every loader -----------------------------------------


def test_roundtrip_all_loaders(xdg):
    _install_cont_preset("cont", with_binding=True)
    ws = _make_ws("w", _WS_MIN + '\nsetup = [\n  "echo pre",\n]\n')
    exp = _stamp(ws, "cont", "env", "GITHUB_TOKEN")
    from credproxy_cli.core.bindings import load_bindings
    from credproxy_cli.core.config import load_config
    from credproxy_cli.core.rules import load_rules
    cfg = load_config(ws)
    assert cfg["env"] == {"C_VAR": "one"}
    assert {m["target"] for m in cfg["mounts"]} == {"/opt/c.sh"}
    assert cfg["setup"][-1] == {"run": "bash /opt/c.sh", "user": "workspace",
                                "order": 45}
    assert {b.name for b in load_bindings(ws)} == {"cont-api"}
    assert load_rules(ws) == []


# ---- collision matrix + double-add guard -------------------------------------


def test_double_add_guard(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "cont")
    code, out, err = _run(["workspace", "w", "preset", "add", "cont"])
    assert code == 1 and "already applied" in (out + err)


def test_mount_target_collision(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + textwrap.dedent('''
        [[mounts]]
        volume = "v"
        target = "/opt/c.sh"
    '''))
    code, out, err = _run(["workspace", "w", "preset", "add", "cont"])
    assert code == 1 and "already mounts" in (out + err)
    # Nothing stamped.
    assert "C_VAR" not in ws.config_path.read_text()


def test_env_identical_value_skipped(xdg):
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + '\n[env]\nC_VAR = "one"\n')
    code, out, err = _run(["workspace", "w", "preset", "add", "cont"])
    assert code == 0, out + err
    text = ws.config_path.read_text()
    # C_VAR not re-stamped (only the original line present, no provenance on it).
    assert text.count('C_VAR = "one"') == 1
    assert "already set to the same value" in (out + err)
    from credproxy_cli.core.config import load_config
    assert load_config(ws)["env"] == {"C_VAR": "one"}


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


def test_attached_allows_binding_only_pack(xdg):
    _write_preset("binonly",
                  '[placeholder]\nprefix = "ghp_"\nlength = 40\ncharset = "alnumeric"\n'
                  '[[part]]\nsuffix = "api"\ninjector = "bearer"\n'
                  'hosts = ["api.github.com"]\n')
    ws = _make_ws("attd", 'attach = { container = "extbox" }\n')
    code, out, err = _run(["workspace", "attd", "preset", "add", "binonly",
                           "--provider", "env", "--secret", "TOK"])
    assert code == 0, out + err
    from credproxy_cli.core.bindings import load_bindings
    assert {b.name for b in load_bindings(ws)} == {"binonly-api"}


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
    from credproxy_cli.core.presets import describe_presets
    row = next(p for p in describe_presets() if p["name"] == "cont")
    assert row["mounts"] == [{"kind": "overlay", "source": "user:setup.d/c.sh",
                              "target": "/opt/c.sh"}]
    assert row["env"] == [{"key": "C_VAR", "value": "one"}]
    assert row["setup"] == [{"run": "bash /opt/c.sh", "user": "workspace",
                             "order": 45}]


# ============================================================================
# #56 review follow-ups
# ============================================================================

import re as _re


def _strip_markers(text: str) -> str:
    """The stamped text with every provenance marker removed -- a standalone
    `# credproxy:preset ...` line dropped entirely, a trailing `  # credproxy:
    preset ...` comment trimmed -- so a golden compares the surgical SKELETON
    (which is stable) rather than the rev/sha-varying marker."""
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("# credproxy:preset"):
            continue
        out.append(_re.sub(r"\s*#\s*credproxy:preset\s.*$", "", line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _setup_only_preset(name="su"):
    """A pure-setup pack: one `{ run = "bash /opt/c.sh", order = 45 }` step."""
    _write_preset(name, """
        [[setup]]
        run = "bash /opt/c.sh"
        order = 45
    """)


# ---- Finding 1: comma-scanner (string/comment-aware) -- byte-for-byte golden --


def test_comma_scanner_comment_only_last_line_no_mutation(xdg):
    """A comment-only last line after a comma-terminated element must NOT get the
    separating comma spliced into it (the silent-mutation case)."""
    _setup_only_preset()
    ws = _make_ws("w", _WS_MIN + 'setup = [\n  "a",\n  # user note\n]\n')
    _stamp(ws, "su")
    text = ws.config_path.read_text()
    # The user's comment survives verbatim -- NOT turned into `# user note,`.
    assert "  # user note\n" in text
    assert "# user note," not in text
    assert _strip_markers(text) == (
        _WS_MIN
        + 'setup = [\n'
          '  "a",\n'
          '  # user note\n'
          '  { run = "bash /opt/c.sh", order = 45 },\n'
          ']\n')
    from credproxy_cli.core.config import load_config
    # A bare-string setup entry stays a `str` (the escape-hatch form); the
    # stamped table normalizes to a dict.
    assert load_config(ws)["setup"] == [
        "a",
        {"run": "bash /opt/c.sh", "user": "workspace", "order": 45}]


def test_comma_scanner_hash_in_string_no_double_comma(xdg):
    """A `#` inside a string with a trailing comma must not fool the scanner into
    a second comma (`,,`, the spurious-abort case)."""
    _setup_only_preset()
    ws = _make_ws("w", _WS_MIN
                  + 'setup = [\n  { run = "echo a#b", order = 1 },\n]\n')
    _stamp(ws, "su")
    text = ws.config_path.read_text()
    assert ",," not in text
    assert _strip_markers(text) == (
        _WS_MIN
        + 'setup = [\n'
          '  { run = "echo a#b", order = 1 },\n'
          '  { run = "bash /opt/c.sh", order = 45 },\n'
          ']\n')


def test_comma_scanner_hash_in_string_no_trailing_comma(xdg):
    """A `#`-in-string last element WITHOUT a trailing comma gets exactly one
    comma spliced after the closing `}` (not inside the string's `#`)."""
    _setup_only_preset()
    ws = _make_ws("w", _WS_MIN
                  + 'setup = [\n  { run = "echo a#b", order = 1 }\n]\n')
    _stamp(ws, "su")
    text = ws.config_path.read_text()
    assert _strip_markers(text) == (
        _WS_MIN
        + 'setup = [\n'
          '  { run = "echo a#b", order = 1 },\n'
          '  { run = "bash /opt/c.sh", order = 45 },\n'
          ']\n')


def test_comma_scanner_commaless_element_with_trailing_comment(xdg):
    """A comma-less last element with a trailing comment gets the comma spliced
    BETWEEN the element and the comment (not after the comment)."""
    _setup_only_preset()
    ws = _make_ws("w", _WS_MIN
                  + 'setup = [\n  { run = "x", order = 1 }  # note\n]\n')
    _stamp(ws, "su")
    text = ws.config_path.read_text()
    assert '{ run = "x", order = 1 },  # note\n' in text
    assert _strip_markers(text) == (
        _WS_MIN
        + 'setup = [\n'
          '  { run = "x", order = 1 },  # note\n'
          '  { run = "bash /opt/c.sh", order = 45 },\n'
          ']\n')
    from credproxy_cli.core.config import load_config
    assert [s["order"] for s in load_config(ws)["setup"]] == [1, 45]


# ---- Finding 6: the span scanner is shared with mount add --------------------


def test_span_scanner_is_shared(xdg):
    """`preset_stamp` reuses `config.inline_array_span` -- one scanner, no fork."""
    from credproxy_cli.core import config, preset_stamp
    assert preset_stamp.inline_array_span is config.inline_array_span
    assert preset_stamp.last_code_char_index is config.last_code_char_index
    # And the mount-add path uses it for the `mounts` key.
    assert config.inline_array_span('mounts = [ "a:/a" ]\n', "mounts") is not None
    assert config.inline_array_span('mounts = [ "a:/a" ]\n', "setup") is None


# ---- Finding 2: block-marker sha == sha256 of the exact on-disk block text ----


def test_block_marker_sha_matches_on_disk_block(xdg):
    """The `sha=` on a `[[binding]]` block's provenance marker is sha256[:12] of
    the EXACT on-disk block text (header line through trailing newline) -- so a
    future refresh (#58) can recompute it deterministically."""
    _write_preset("bo",
                  '[placeholder]\nprefix = "ghp_"\nlength = 40\ncharset = "alnumeric"\n'
                  '[[part]]\nsuffix = "api"\ninjector = "bearer"\n'
                  'hosts = ["api.github.com"]\n')
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "bo", "env", "TOK")
    text = ws.config_path.read_text()
    lines = text.splitlines(keepends=True)
    bi = next(i for i, ln in enumerate(lines) if ln.strip() == "[[binding]]")
    marker = lines[bi - 1]
    sha_in_marker = _re.search(r"sha=(\w+)", marker).group(1)
    # The binding is the last block -> its on-disk text runs to EOF.
    block_text = "".join(lines[bi:])
    from credproxy_cli.core.preset_stamp import _sha12
    assert _sha12(block_text) == sha_in_marker
    # And the marker line carries no phantom leading-newline in its hash input.
    assert block_text.startswith("[[binding]]\n")


def test_element_marker_sha_matches_bare_element(xdg):
    """An array-element marker's `sha=` is sha256[:12] of the BARE rendered
    element string (no indent, comma, or comment) -- the visible-text convention."""
    _setup_only_preset()
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "su")
    text = ws.config_path.read_text()
    line = next(ln for ln in text.splitlines() if "credproxy:preset" in ln
                and "run" in ln)
    sha_in_marker = _re.search(r"sha=(\w+)", line).group(1)
    from credproxy_cli.core.preset_stamp import _sha12
    assert _sha12('{ run = "bash /opt/c.sh", order = 45 }') == sha_in_marker


# ---- Finding 3: setup-block form + [ env ] spacing + inline/dotted env --------


def test_setup_block_form_appends_block(xdg):
    """A workspace whose setup is `[[setup]]` blocks gets an appended `[[setup]]`
    block (not a colliding `setup =` key -> the old 'immutable namespace' abort)."""
    _setup_only_preset()
    ws = _make_ws("w", _WS_MIN + textwrap.dedent('''
        [[setup]]
        run = "curl bootstrap"
        order = 10
    '''))
    _stamp(ws, "su")
    text = ws.config_path.read_text()
    assert "setup = [" not in text
    assert text.count("[[setup]]") == 2
    from credproxy_cli.core.config import load_config
    assert [s["order"] for s in load_config(ws)["setup"]] == [10, 45]


def test_env_header_tolerates_inner_whitespace(xdg):
    """`[ env ]` (inner spaces) is recognized -- keys append to that section, no
    duplicate header created."""
    _install_cont_preset()
    ws = _make_ws("w", _WS_MIN + '\n[ env ]\nEXIST = "yes"\n')
    _stamp(ws, "cont")
    text = ws.config_path.read_text()
    assert 'C_VAR = "one"' in text
    assert text.count("env ]") == 1 and "[env]\n" not in text
    from credproxy_cli.core.config import load_config
    assert load_config(ws)["env"] == {"EXIST": "yes", "C_VAR": "one"}


def test_inline_table_env_clear_error(xdg):
    """A top-level inline-table env fails with a CLEAR remedy, not the opaque
    verify abort."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.preset_stamp import compose
    with pytest.raises(ConfigError, match="inline table / dotted keys"):
        compose(_WS_MIN + 'env = { TZ = "UTC" }\n', "p", "rev",
                bindings=[], rules=[], mounts=[], env_items=[("C_VAR", "one")],
                setup=[])


def test_dotted_key_env_clear_error(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.preset_stamp import compose
    with pytest.raises(ConfigError, match="inline table / dotted keys"):
        compose(_WS_MIN + 'env.TZ = "UTC"\n', "p", "rev",
                bindings=[], rules=[], mounts=[], env_items=[("C_VAR", "one")],
                setup=[])


# ---- Finding 4: reserved-literal / duplicate-basename tier qualifier guards ---


def test_overlay_named_user_rejected(xdg, monkeypatch):
    """An overlay dir literally named `user` shadows the reserved `user` tier
    qualifier -- loading its presets errors clearly."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.paths import config_dir
    from credproxy_cli.core.presets import load_presets
    ov = config_dir() / "ovs" / "user"
    (ov / "presets").mkdir(parents=True)
    (ov / "presets" / "p.toml").write_text('[env]\nX = "y"\n')
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    with pytest.raises(ConfigError, match="reserved 'user' tier qualifier"):
        load_presets()


def test_overlay_named_builtin_rejected(xdg, monkeypatch):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.paths import config_dir
    from credproxy_cli.core.presets import load_presets
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
    from credproxy_cli.core.presets import load_presets
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


def test_no_recreate_hint_when_env_all_skipped(xdg, monkeypatch):
    """An env-only pack whose every key already matches stamps NOTHING (byte-
    identical file) -- no 'restart to apply' claim even with a live container."""
    _write_preset("envonly", '[env]\nC_VAR = "one"\n')
    ws = _make_ws("w", _WS_MIN + '\n[env]\nC_VAR = "one"\n')
    before = ws.config_path.read_text()
    from credproxy_cli.porcelain import cli as pcli
    monkeypatch.setattr(pcli.core_docker, "container_status", lambda _n: "running")
    code, out, err = _run(["workspace", "w", "preset", "add", "envonly"])
    assert code == 0, out + err
    assert "restart to apply" not in (out + err)
    assert "already set to the same value" in (out + err)
    assert ws.config_path.read_text() == before   # nothing written


# ---- Finding 8: qualified overlay source can't name the tier root itself ------


def test_qualified_source_empty_subpath_rejected(xdg):
    from credproxy_cli.core import config as core_config
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


def test_already_applied_ignores_marker_in_string_value(xdg):
    from credproxy_cli.core.preset_stamp import already_applied
    # A marker-looking substring inside a STRING VALUE (real `#` in a comment vs
    # a `#` inside a quoted value) must not trip the guard.
    text = 'note = "see # credproxy:preset name=foo rev=x sha=y here"\n'
    assert already_applied(text, "foo") is False
    # A genuine trailing-comment marker (what the stamp writes) IS detected.
    real = 'C_VAR = "one"  # credproxy:preset name=foo rev=x sha=y\n'
    assert already_applied(real, "foo") is True
    # As is a standalone block marker line.
    blk = '# credproxy:preset name=bar rev=x sha=y\n[[binding]]\n'
    assert already_applied(blk, "bar") is True
