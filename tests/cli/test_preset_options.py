"""Pack `[[option]]` whole-field values + loose-surface prompting (#59 v3).

An option supplies the ENTIRE value of a host-half field (a mount `bind`/`volume`
source, a `[[requires]]` path) via a STRUCTURAL `{ option = "id" }` marker -- never
a token inside a string. It resolves at expansion time: explicit `--opt id=value`
(or a template `[preset.options]` table) -> prompt (loose+TTY only) -> declared
`default` -> fail with the structured `{preset, missing}` error. The resolved
whole value lands as literal config in the stamped mount; the marker never reaches
the workspace TOML. `preset refresh` reads the value back from the stamped mount.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from test_porcelain import _run, _run_loose


# ---- helpers -----------------------------------------------------------------


def _write_preset(name: str, toml: str):
    from credproxy_cli.core.paths import config_dir
    d = config_dir() / "presets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(textwrap.dedent(toml))
    return d / f"{name}.toml"


def _make_ws(name: str, content: str = 'image = "python:3.12-slim"\n'):
    from credproxy_cli.core.paths import workspaces_config_dir
    from credproxy_cli.core.workspace import Workspace
    wd = workspaces_config_dir()
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{name}.toml").write_text(textwrap.dedent(content))
    return Workspace(name)


def _config_text(name: str) -> str:
    from credproxy_cli.core.paths import workspaces_config_dir
    return (workspaces_config_dir() / f"{name}.toml").read_text()


# A container-only pack whose mount source is supplied by a `sock_dir` option and
# whose `[[requires]]` path reuses the same option (the git-signing shape).
_OPT_PRESET = """
    [[option]]
    id = "sock_dir"
    type = "string"
    default = "~/.ssh/credproxy-agent"
    description = "host directory holding the signing agent socket"

    [[mount]]
    bind = { option = "sock_dir" }
    target = "/ssh-agent"

    [[setup]]
    run = "echo configure git signing"
    order = 40

    [[requires]]
    kind = "path"
    path = { option = "sock_dir" }
    hint = "start the signing agent"
"""

# A pack whose option has NO default (required).
_OPT_REQUIRED = """
    [[option]]
    id = "sock_dir"
    type = "string"
    description = "host socket dir (required)"

    [[mount]]
    bind = { option = "sock_dir" }
    target = "/ssh-agent"
"""


# ---- parse / validate matrix -------------------------------------------------


def _load(name):
    from credproxy_cli.core.presets import get_preset
    return get_preset(name)


def test_option_parses_string_enum_bool(xdg):
    _write_preset("p", """
        [[option]]
        id = "s"
        type = "string"
        default = "x"
        [[option]]
        id = "e"
        type = "enum"
        choices = ["a", "b"]
        default = "b"
        [[option]]
        id = "flag"
        type = "bool"
        default = true
        [[mount]]
        bind = { option = "s" }
        target = "/x"
    """)
    spec = _load("p")
    by_id = {o.id: o for o in spec.options}
    assert by_id["s"].type == "string" and by_id["s"].default == "x"
    assert by_id["e"].choices == ("a", "b") and by_id["e"].default == "b"
    assert by_id["flag"].type == "bool" and by_id["flag"].default is True


def test_enum_default_must_be_member(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "e"
        type = "enum"
        choices = ["a", "b"]
        default = "zzz"
        [[mount]]
        bind = { option = "e" }
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="not one of the choices"):
        load_presets()


def test_enum_requires_nonempty_choices(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "e"
        type = "enum"
        [[mount]]
        bind = { option = "e" }
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="non-empty 'choices'"):
        load_presets()


def test_bool_default_must_be_bool(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "flag"
        type = "bool"
        default = "yes"
        [[mount]]
        bind = "/lit"
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="default must be a boolean"):
        load_presets()


def test_duplicate_option_id_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "x"
        type = "string"
        [[option]]
        id = "x"
        type = "string"
        [[mount]]
        bind = { option = "x" }
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="duplicate .*option.* id"):
        load_presets()


def test_option_unknown_key_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "x"
        type = "string"
        bogus = 1
        [[mount]]
        bind = { option = "x" }
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="unknown key"):
        load_presets()


def test_marker_in_container_half_field_rejected(xdg):
    """An option marker in a container-half field (mount `target`) is rejected."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "x"
        type = "string"
        default = "/host"
        [[mount]]
        bind = "/host"
        target = { option = "x" }
    """)
    with pytest.raises(ConfigError, match="container-half"):
        load_presets()


def test_marker_undefined_option_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "x"
        type = "string"
        default = "/host"
        [[mount]]
        bind = { option = "nope" }
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="undefined option 'nope'"):
        load_presets()


def test_marker_on_overlay_source_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "x"
        type = "string"
        default = "rel"
        [[mount]]
        overlay = { option = "x" }
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="overlay"):
        load_presets()


def test_bool_option_cannot_supply_a_source(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "flag"
        type = "bool"
        default = true
        [[mount]]
        bind = { option = "flag" }
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="bool"):
        load_presets()


def test_malformed_marker_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "x"
        type = "string"
        default = "/host"
        [[mount]]
        bind = { option = "x", extra = 1 }
        target = "/x"
    """)
    with pytest.raises(ConfigError, match="extra key"):
        load_presets()


def test_option_only_pack_is_not_a_pack(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import load_presets
    _write_preset("p", """
        [[option]]
        id = "x"
        type = "string"
        default = "y"
    """)
    with pytest.raises(ConfigError, match="needs at least one"):
        load_presets()


# ---- resolution order (core) -------------------------------------------------


def test_resolve_explicit_beats_default(xdg):
    from credproxy_cli.core.presets import resolve_options
    _write_preset("gitsign", _OPT_PRESET)
    spec = _load("gitsign")
    vals, missing = resolve_options(spec, {"sock_dir": "/tmp/a"})
    assert missing == [] and vals == {"sock_dir": "/tmp/a"}


def test_resolve_default_used(xdg):
    from credproxy_cli.core.presets import resolve_options
    _write_preset("gitsign", _OPT_PRESET)
    spec = _load("gitsign")
    vals, missing = resolve_options(spec, {})
    assert missing == [] and vals == {"sock_dir": "~/.ssh/credproxy-agent"}


def test_resolve_missing_required(xdg):
    from credproxy_cli.core.presets import resolve_options
    _write_preset("gitsign", _OPT_REQUIRED)
    spec = _load("gitsign")
    vals, missing = resolve_options(spec, {})
    assert vals == {} and [o.id for o in missing] == ["sock_dir"]


def test_resolve_unknown_opt_id_errors(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.presets import resolve_options
    _write_preset("gitsign", _OPT_PRESET)
    spec = _load("gitsign")
    with pytest.raises(ConfigError, match="unknown option"):
        resolve_options(spec, {"nope": "x"})


def test_resolve_prompt_supplies_value(xdg):
    from credproxy_cli.core.presets import resolve_options
    _write_preset("gitsign", _OPT_REQUIRED)
    spec = _load("gitsign")
    vals, missing = resolve_options(
        spec, {}, prompt=lambda opt: "/prompted")
    assert missing == [] and vals == {"sock_dir": "/prompted"}


# ---- whole-field substitution: literal in stamp, marker gone -----------------


def test_preset_add_stamps_literal_from_default(xdg):
    _write_preset("gitsign", _OPT_PRESET)
    _make_ws("w")
    code, out, err = _run(["workspace", "w", "preset", "add", "gitsign"])
    assert code == 0, out + err
    text = _config_text("w")
    assert 'bind = "~/.ssh/credproxy-agent"' in text
    assert "option" not in text            # the marker never reaches the config


def test_preset_add_opt_overrides_default(xdg):
    _write_preset("gitsign", _OPT_PRESET)
    _make_ws("w")
    code, out, err = _run(["workspace", "w", "preset", "add", "gitsign",
                           "--opt", "sock_dir=/tmp/agent"])
    assert code == 0, out + err
    text = _config_text("w")
    assert 'bind = "/tmp/agent"' in text
    assert "credproxy-agent" not in text


def test_bad_opt_syntax_fails(xdg):
    _write_preset("gitsign", _OPT_PRESET)
    _make_ws("w")
    code, out, err = _run(["workspace", "w", "preset", "add", "gitsign",
                           "--opt", "noeq"])
    assert code == 1
    assert "id=value" in (out + err)


# ---- missing required: strict + loose-no-TTY fail structured -----------------


def test_missing_required_strict_fails(xdg):
    _write_preset("req", _OPT_REQUIRED)
    _make_ws("w")
    code, out, err = _run(["workspace", "w", "preset", "add", "req"])
    assert code == 1
    assert "sock_dir" in (out + err)


def test_missing_required_json_shape(xdg):
    _write_preset("req", _OPT_REQUIRED)
    _make_ws("w")
    code, out, err = _run(["--json", "workspace", "w", "preset", "add", "req"])
    assert code == 1
    obj = json.loads(out)["error"]
    assert obj["type"] == "PresetOptionsError"
    assert obj["preset"] == "req"
    assert obj["missing"] == [
        {"id": "sock_dir", "type": "string", "description": "host socket dir (required)"}]


def test_missing_required_loose_no_tty_fails(xdg):
    _write_preset("req", _OPT_REQUIRED)
    _make_ws("w")
    # loose but stdin is NOT a TTY -> no prompt, structured fail-closed.
    code, out, err = _run_loose(["workspace", "w", "preset", "add", "req"],
                                stdin_text="", stdin_isatty=False)
    assert code == 1
    assert "sock_dir" in (out + err)


def test_missing_required_loose_tty_prompts(xdg, monkeypatch):
    _write_preset("req", _OPT_REQUIRED)
    _make_ws("w")
    from credproxy_cli.porcelain import prompt as prompt_mod
    monkeypatch.setattr(prompt_mod, "ask_option", lambda opt: "/from-prompt")
    code, out, err = _run_loose(["workspace", "w", "preset", "add", "req"],
                                stdin_text="", stdin_isatty=True)
    assert code == 0, out + err
    assert 'bind = "/from-prompt"' in _config_text("w")


def test_strict_never_prompts_even_with_tty(xdg, monkeypatch):
    """Prompting is loose-only: strict fails structured even on a TTY."""
    _write_preset("req", _OPT_REQUIRED)
    _make_ws("w")
    from credproxy_cli.porcelain import prompt as prompt_mod
    called = []
    monkeypatch.setattr(prompt_mod, "ask_option",
                        lambda opt: called.append(opt) or "/x")
    code, out, err = _run(["workspace", "w", "preset", "add", "req"],
                          stdin_text="", stdin_isatty=True)
    assert code == 1 and not called


# ---- template [preset.options] -----------------------------------------------


def _template(toml: str) -> None:
    from credproxy_cli.core.paths import config_dir
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "workspace.template.toml").write_text(textwrap.dedent(toml))


_MIN = 'image = "python:3.12-slim"\n'


def test_template_options_supply_value(xdg):
    _write_preset("gitsign", _OPT_PRESET)
    _template(_MIN + textwrap.dedent("""
        [[preset]]
        name = "gitsign"
        [preset.options]
        sock_dir = "/tmpl/agent"
    """))
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 0, out + err
    assert 'bind = "/tmpl/agent"' in _config_text("proj")


def test_template_missing_required_option_fails_json(xdg):
    _write_preset("req", _OPT_REQUIRED)
    _template(_MIN + '\n[[preset]]\nname = "req"\n')
    code, out, err = _run(["--json", "workspace", "create", "proj"])
    assert code == 1
    obj = json.loads(out)["error"]
    assert obj["type"] == "PresetOptionsError" and obj["preset"] == "req"
    # all-or-nothing: nothing written
    from credproxy_cli.core.paths import workspaces_config_dir
    assert not (workspaces_config_dir() / "proj.toml").exists()


def test_template_option_prompt_loose_tty(xdg, monkeypatch):
    _write_preset("req", _OPT_REQUIRED)
    _template(_MIN + '\n[[preset]]\nname = "req"\n')
    from credproxy_cli.porcelain import prompt as prompt_mod
    monkeypatch.setattr(prompt_mod, "ask_option", lambda opt: "/tmpl-prompt")
    code, out, err = _run_loose(["workspace", "create", "proj"],
                                stdin_text="", stdin_isatty=True)
    assert code == 0, out + err
    assert 'bind = "/tmpl-prompt"' in _config_text("proj")


# ---- provider/secret prompting (decision 4) ----------------------------------


_BINDING_NODEFAULT = """
    [placeholder]
    prefix = "t_"
    length = 12
    charset = "alnumeric"

    [[part]]
    suffix = "api"
    injector = "bearer"
    hosts = ["api.svc.example.com"]
"""


def test_provider_secret_prompt_loose_tty(xdg, monkeypatch):
    _write_preset("svc", _BINDING_NODEFAULT)
    _make_ws("w")
    from credproxy_cli.porcelain import prompt as prompt_mod
    monkeypatch.setattr(prompt_mod, "ask_provider", lambda default: "env")
    monkeypatch.setattr(prompt_mod, "ask_secret",
                        lambda provider, default, slots=(): "SVC_TOKEN")
    code, out, err = _run_loose(["workspace", "w", "preset", "add", "svc"],
                                stdin_text="", stdin_isatty=True)
    assert code == 0, out + err
    text = _config_text("w")
    assert '"env"' in text and '"SVC_TOKEN"' in text


def test_provider_secret_strict_never_prompts(xdg, monkeypatch):
    _write_preset("svc", _BINDING_NODEFAULT)
    _make_ws("w")
    from credproxy_cli.porcelain import prompt as prompt_mod
    called = []
    monkeypatch.setattr(prompt_mod, "ask_provider",
                        lambda default: called.append("p") or "env")
    code, out, err = _run(["workspace", "w", "preset", "add", "svc"],
                          stdin_text="", stdin_isatty=True)
    assert code == 1 and not called
    assert "provider" in (out + err)


def test_secret_validate_at_prompt_loops(xdg, monkeypatch):
    """The real ask_secret validate loop: a bad ref fetch fails + loops, a good
    one reports length and returns. Drives the actual prompt via stdin."""
    from credproxy_cli.porcelain import prompt as prompt_mod
    monkeypatch.setenv("GOOD_TOK", "abcdef")
    monkeypatch.delenv("BAD_TOK", raising=False)
    # First ref (BAD_TOK): validate=yes -> fetch fails -> loop. Second ref
    # (GOOD_TOK): validate=yes -> ok.
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        "BAD_TOK\ny\nGOOD_TOK\ny\n"))
    ref = prompt_mod.ask_secret("env", None)
    assert ref == "GOOD_TOK"


# ---- refresh read-back -------------------------------------------------------


def test_refresh_preserves_option_value(xdg, tmp_path):
    # A real (existing) bind source: `refresh` re-validates the full config, which
    # existence-checks a bind mount source (unlike `add`).
    agent = tmp_path / "agent"
    agent.mkdir()
    _write_preset("gitsign", _OPT_PRESET)
    _make_ws("w")
    _run(["workspace", "w", "preset", "add", "gitsign",
          "--opt", f"sock_dir={agent}"])
    assert f'bind = "{agent}"' in _config_text("w")
    # Refresh must NOT reset the option-derived value back to the default.
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gitsign"])
    assert code == 0, out + err
    text = _config_text("w")
    assert f'bind = "{agent}"' in text
    assert "credproxy-agent" not in text


def test_refresh_option_value_survives_definition_change(xdg, tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    _write_preset("gitsign", _OPT_PRESET)
    _make_ws("w")
    _run(["workspace", "w", "preset", "add", "gitsign",
          "--opt", f"sock_dir={agent}"])
    # Change the pack: add an env var. The mount's option-derived source must
    # still round-trip from the stamped value (not reset to the default).
    _write_preset("gitsign", _OPT_PRESET + '\n    [env]\n    FOO = "bar"\n')
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gitsign"])
    assert code == 0, out + err
    text = _config_text("w")
    assert f'bind = "{agent}"' in text
    assert 'FOO = "bar"' in text


# ---- doctor requires-only option degrades to skip-with-note ------------------


def test_doctor_requires_only_option_skips(xdg):
    """An option feeding ONLY a `[[requires]]` path (nowhere stamped) and with no
    default is unrecoverable at doctor time -> skip-with-note, never a crash."""
    _write_preset("ronly", """
        [placeholder]
        prefix = "t_"
        length = 12
        charset = "alnumeric"

        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.svc.example.com"]

        [[option]]
        id = "sock_dir"
        type = "string"
        description = "socket dir"

        [[requires]]
        kind = "path"
        path = { option = "sock_dir" }
        hint = "start the agent"
    """)
    _make_ws("w")
    # Supply the option at add so the pack stamps (it's only used in requires).
    code, out, err = _run(["workspace", "w", "preset", "add", "ronly",
                           "--provider", "env", "--secret", "TOK",
                           "--opt", "sock_dir=/tmp/x"])
    assert code == 0, out + err
    from credproxy_cli.core import doctor
    from credproxy_cli.core.workspace import Workspace
    checks = doctor._preset_requires_checks(Workspace("w"), fetch=False)
    notes = [c for c in checks if "used only here" in c.message]
    assert notes and all(c.ok for c in notes)
