"""Templates can declare presets: `[[preset]]` expanded at `create` time (#57).

A `workspace.template.toml` / `workspace.attach.template.toml` may carry
`[[preset]]` entries; `create` consumes them, expands each through the SAME
core `preset add` uses (shared placeholder, collision checks, stamping), and
writes ONE all-or-nothing config -- the `[[preset]]` blocks never survive into
the stamped `<name>.toml`, and the loader rejects `preset` in a workspace config.

Covers: entry validation, default-resolution parity with `preset add`, textual
identity of create-stamped vs add-stamped output, all-or-nothing (no orphaned
file/token/state), the loader rejection message, the attach container-half
refusal, freshly-generated per-workspace placeholders, and the
newly-intercepted announcement.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from test_porcelain import _run


# ---- helpers -----------------------------------------------------------------


def _template(toml: str) -> None:
    """Install a user-tier `workspace.template.toml` (shadows the builtin)."""
    from credproxy_cli.core.paths import config_dir
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "workspace.template.toml").write_text(textwrap.dedent(toml))


def _attach_template(toml: str) -> None:
    from credproxy_cli.core.paths import config_dir
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "workspace.attach.template.toml").write_text(textwrap.dedent(toml))


def _preset(name: str, toml: str) -> None:
    """Install a user-tier preset pack."""
    from credproxy_cli.core.paths import config_dir
    pd = config_dir() / "presets"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{name}.toml").write_text(textwrap.dedent(toml))


def _config_text(name: str) -> str:
    from credproxy_cli.core.paths import workspaces_config_dir
    return (workspaces_config_dir() / f"{name}.toml").read_text()


_MIN = 'image = "python:3.12-slim"\nuser = "vscode"\n'

_NODEFAULT = """\
    [placeholder]
    prefix = "t_"
    length = 12
    charset = "alnumeric"
    [[part]]
    suffix = "api"
    injector = "bearer"
    hosts = ["api.svc.example.com"]
"""


# ---- happy path --------------------------------------------------------------


def test_create_expands_template_preset(xdg):
    _template(_MIN + '\n[[preset]]\nname = "github"\n')
    code, out, err = _run(["--json", "workspace", "create", "proj"])
    assert code == 0
    obj = json.loads(out)
    # The stamped config is written, and the `[[preset]]` key does NOT survive.
    text = _config_text("proj")
    assert "[[preset]]" not in text
    assert "[[binding]]" in text and text.count("[[binding]]") == 3
    # github's three parts share ONE freshly generated placeholder.
    names = [b["name"] for b in obj["presets"][0]["bindings"]]
    assert names == ["github-api", "github-git", "github-ghcr"]
    phs = {b["placeholder"] for b in obj["presets"][0]["bindings"]}
    assert len(phs) == 1 and next(iter(phs)).startswith("ghp_")


def test_created_config_loads_clean(xdg):
    """The stamped config carries only literal config + provenance comments, and
    every loader accepts it (acceptance criterion 3)."""
    _template(_MIN + '\n[[preset]]\nname = "github"\n')
    assert _run(["workspace", "create", "proj"])[0] == 0
    from credproxy_cli.core.bindings import load_bindings
    from credproxy_cli.core.config import load_config
    from credproxy_cli.core.rules import load_rules
    from credproxy_cli.core.workspace import Workspace
    ws = Workspace("proj")
    assert load_config(ws)["image"] == "python:3.12-slim"
    assert len(load_bindings(ws)) == 3
    assert load_rules(ws) == []
    assert "[[preset]]" not in _config_text("proj")


def test_fresh_placeholder_differs_across_workspaces(xdg):
    """Acceptance criterion 1: two created workspaces get DIFFERENT placeholders."""
    _template(_MIN + '\n[[preset]]\nname = "github"\n')
    a = json.loads(_run(["--json", "workspace", "create", "a"])[1])
    b = json.loads(_run(["--json", "workspace", "create", "b"])[1])
    pa = a["presets"][0]["bindings"][0]["placeholder"]
    pb = b["presets"][0]["bindings"][0]["placeholder"]
    assert pa != pb


def test_bare_create_still_works_without_presets(xdg):
    """The builtin template has no `[[preset]]`; a bare create must not require a
    provider login or touch preset machinery."""
    code, out, err = _run(["--json", "workspace", "create", "plain"])
    assert code == 0
    obj = json.loads(out)
    assert "presets" not in obj                 # nothing to announce
    assert "[[preset]]" not in _config_text("plain")


# ---- textual identity with preset add ----------------------------------------


def test_textual_identity_create_vs_preset_add(xdg, monkeypatch):
    """The blocks `create` stamps from a template `[[preset]]` are BYTE-IDENTICAL
    to `create` (plain) followed by `preset add` -- same renderers, same core.
    Pin the generated placeholder so even the provenance sha matches."""
    from credproxy_cli.core.injectors import Placeholder
    monkeypatch.setattr(Placeholder, "generate", lambda self: self.prefix + "PINNED")

    _template(_MIN + '\n[[preset]]\nname = "github"\n')
    assert _run(["workspace", "create", "viatemplate"])[0] == 0

    _template(_MIN)                              # plain template, no preset
    assert _run(["workspace", "create", "viaadd"])[0] == 0
    assert _run(["workspace", "viaadd", "preset", "add", "github"])[0] == 0

    assert _config_text("viatemplate") == _config_text("viaadd")


# ---- default resolution parity -----------------------------------------------


def test_default_resolution_parity(xdg):
    """`resolve_preset_credential` is the shared defaulting core: the github pack
    resolves gh-cli/github.com identically whether reached via the template entry
    or `preset add` (tested through both entry points)."""
    from credproxy_cli.core.presets import get_preset, resolve_preset_credential
    spec = get_preset("github")
    # Nothing supplied -> pack defaults fill both.
    assert resolve_preset_credential(spec, None, None) == ("gh-cli", "github.com", [])
    # A different provider drops the default_secret (ref meaning is provider-specific).
    p, s, missing = resolve_preset_credential(spec, "env", None)
    assert p == "env" and s is None and missing == ["secret"]
    # Explicit values pass through untouched.
    assert resolve_preset_credential(spec, "op", "op://x") == ("op", "op://x", [])


def test_template_default_provider_secret_reach_bindings(xdg):
    _template(_MIN + '\n[[preset]]\nname = "github"\n')
    obj = json.loads(_run(["--json", "workspace", "create", "proj"])[1])
    b0 = obj["presets"][0]["bindings"][0]
    assert b0["provider"] == "gh-cli" and b0["secret"] == "github.com"


def test_template_entry_supplies_provider_secret(xdg):
    _preset("svc", _NODEFAULT)
    _template(_MIN + '\n[[preset]]\nname = "svc"\nprovider = "env"\n'
              'secret = "SVC_TOKEN"\n')
    obj = json.loads(_run(["--json", "workspace", "create", "proj"])[1])
    b0 = obj["presets"][0]["bindings"][0]
    assert b0["provider"] == "env" and b0["secret"] == "SVC_TOKEN"


# ---- missing required fields (no prompting in v1) ----------------------------


def test_missing_provider_and_secret_fails_json(xdg):
    _preset("svc", _NODEFAULT)
    _template(_MIN + '\n[[preset]]\nname = "svc"\n')
    code, out, err = _run(["--json", "workspace", "create", "proj"])
    assert code == 1
    obj = json.loads(out)["error"]
    assert obj["type"] == "PresetTemplateError"
    assert obj["preset"] == "svc"
    assert obj["missing"] == ["provider", "secret"]
    # All-or-nothing: no config, no token, no state dir left behind.
    _assert_no_workspace("proj")


def test_missing_secret_only_fails_human(xdg):
    """provider supplied (== a provider != default) but no secret -> just secret
    is missing; both surfaces fail loudly."""
    _preset("svc", _NODEFAULT)
    _template(_MIN + '\n[[preset]]\nname = "svc"\nprovider = "env"\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "missing `secret`" in err
    _assert_no_workspace("proj")


# ---- all-or-nothing ----------------------------------------------------------


def _assert_no_workspace(name: str) -> None:
    from credproxy_cli.core.paths import (
        workspaces_config_dir, workspaces_state_dir,
    )
    assert not (workspaces_config_dir() / f"{name}.toml").exists()
    assert not (workspaces_state_dir() / name).exists()


def test_collision_between_entry_and_literal_binding_aborts(xdg):
    """A generated `<preset>-<suffix>` clashing with a literal template binding
    fails create with NOTHING written."""
    _template(_MIN + textwrap.dedent("""
        [[binding]]
        name = "github-api"
        injector = "bearer"
        provider = "env"
        secret = "T"
        hosts = ["api.github.com"]

        [[preset]]
        name = "github"
    """))
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "github-api" in err and "already exists" in err
    _assert_no_workspace("proj")


def test_unknown_preset_aborts(xdg):
    _template(_MIN + '\n[[preset]]\nname = "does-not-exist"\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "unknown preset" in err
    _assert_no_workspace("proj")


# ---- entry validation --------------------------------------------------------


@pytest.mark.parametrize("entry, needle", [
    ('provider = "env"\n', "'name' must be a non-empty string"),   # missing name
    ('name = "github"\nbogus = "x"\n', "unknown key(s): bogus"),   # unknown key
    ('name = "github"\nsecret = 123\n', "'secret' must be a non-empty string"),
    ('name = "github"\nprovider = 1\n', "'provider' must be a non-empty string"),
])
def test_entry_validation(xdg, entry, needle):
    _template(_MIN + "\n[[preset]]\n" + entry)
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert needle in err
    _assert_no_workspace("proj")


# ---- loader rejection --------------------------------------------------------


def test_loader_rejects_preset_in_workspace_config(xdg):
    from credproxy_cli.core.config import load_config
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.paths import workspaces_config_dir
    from credproxy_cli.core.workspace import Workspace
    d = workspaces_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "w.toml").write_text('image = "x"\n[[preset]]\nname = "github"\n')
    with pytest.raises(ConfigError, match="template-only key"):
        load_config(Workspace("w"))


def test_loader_rejection_message_points_at_preset_add(xdg):
    _run  # keep import used
    from credproxy_cli.core.paths import workspaces_config_dir
    d = workspaces_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "w.toml").write_text('image = "x"\n[[preset]]\nname = "github"\n')
    code, out, err = _run(["workspace", "w", "inspect"])
    assert code == 1
    assert "template-only key" in err and "preset add" in err


# ---- attached workspaces -----------------------------------------------------


def test_attach_template_binding_only_preset_ok(xdg):
    _attach_template(
        'attach = { compose_project = "{name}" }\n\n[[preset]]\nname = "github"\n')
    code, out, err = _run(
        ["--json", "workspace", "create", "att", "--attach", "container=foo"])
    assert code == 0
    text = _config_text("att")
    assert "[[preset]]" not in text and "[[binding]]" in text
    assert "attach" in text


def test_attach_template_container_half_pack_fails(xdg):
    _preset("cont", '[env]\nFOO = "bar"\n')
    _attach_template(
        'attach = { compose_project = "{name}" }\n\n[[preset]]\nname = "cont"\n')
    code, out, err = _run(
        ["workspace", "create", "att", "--attach", "container=foo"])
    assert code == 1
    assert "attached" in err and "container-half" in err
    _assert_no_workspace("att")


# ---- newly-intercepted announcement ------------------------------------------


def test_newly_intercepted_announced_at_create(xdg):
    _template(_MIN + '\n[[preset]]\nname = "github"\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 0
    # github's three hosts are newly TLS-intercepted; the advisory hits stderr.
    assert "newly intercepted" in err
    for host in ("api.github.com", "github.com", "ghcr.io"):
        assert host in err


def test_newly_intercepted_in_json(xdg):
    _template(_MIN + '\n[[preset]]\nname = "github"\n')
    obj = json.loads(_run(["--json", "workspace", "create", "proj"])[1])
    assert set(obj["presets"][0]["newly_intercepted"]) == {
        "api.github.com", "github.com", "ghcr.io"}
