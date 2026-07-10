"""Templates can declare presets: `[[preset]]` written into the config at
`create` time (#57, config-v2).

A `workspace.template.toml` / `workspace.attach.template.toml` may carry
`[[preset]]` entries; `create` consumes them, resolves the credential/options
through the SAME core `preset add` uses (default resolution, collision checks),
and writes ONE all-or-nothing config carrying each as a `[[preset]]` REFERENCE
that the resolver expands (config-v2 -- the reference survives into the config;
the placeholder is minted into the lock at the first resolve, not `create`).

Covers: entry validation, default-resolution parity with `preset add`, textual
identity of create-written vs add-written references, all-or-nothing (no orphaned
file/token/state), the loader rejection message, the attach container-half
refusal, per-workspace placeholders minted at first resolve, and the
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
    # The reference SURVIVES into the stamped config (config-v2); the resolver
    # expands it (no literal `[[binding]]` blocks are written).
    text = _config_text("proj")
    assert "[[preset]]" in text
    assert "[[binding]]" not in text
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.workspace import Workspace
    resolved = resolve_workspace(Workspace("proj"))
    assert len(resolved.bindings) == 3
    names = [b["name"] for b in obj["presets"][0]["bindings"]]
    assert names == ["github-api", "github-git", "github-ghcr"]
    # create writes no lock, so its announce carries NO placeholder (minted at the
    # first resolve). The three parts share ONE placeholder at resolve time.
    assert "placeholder" not in obj["presets"][0]["bindings"][0]
    phs = {b.placeholder for b in resolved.bindings}
    assert len(phs) == 1 and next(iter(phs)).startswith("ghp_")


def test_created_config_loads_clean(xdg):
    """The created config carries the `[[preset]]` reference (config-v2, no literal
    binding blocks), and every loader/resolver accepts it (acceptance criterion 3)."""
    _template(_MIN + '\n[[preset]]\nname = "github"\n')
    assert _run(["workspace", "create", "proj"])[0] == 0
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.workspace import Workspace
    ws = Workspace("proj")
    assert load_config(ws)["image"] == "python:3.12-slim"
    resolved = resolve_workspace(ws)
    assert len(resolved.bindings) == 3
    assert resolved.rules == []
    assert "[[preset]]" in _config_text("proj")


def test_fresh_placeholder_differs_across_workspaces(xdg):
    """Acceptance criterion 1: two created workspaces get DIFFERENT placeholders,
    each minted at the first persisting resolve (create writes no lock) and then
    STABLE across re-resolves -- read back from the lock, never regenerated."""
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.workspace import Workspace
    _template(_MIN + '\n[[preset]]\nname = "github"\n')

    def _first_ph(name):
        assert _run(["workspace", "create", name])[0] == 0
        ws = Workspace(name)
        resolved = resolve_workspace(ws)
        save_lock(ws, resolved.lock)              # persist the minted identity
        return ws, resolved.bindings[0].placeholder

    wa, pa = _first_ph("a")
    wb, pb = _first_ph("b")
    assert pa and pb and pa != pb
    # A second resolve reads the SAME placeholder back from the lock (stable).
    assert resolve_workspace(wa).bindings[0].placeholder == pa


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
    """The `[[preset]]` reference `create` writes from a template entry is
    BYTE-IDENTICAL to `create` (plain) followed by `preset add` -- same renderers,
    same core. Pin the generated placeholder for a deterministic comparison."""
    from credproxy_cli.core.model.injectors import Placeholder
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
    from credproxy_cli.core.model.presets import get_preset, resolve_preset_credential
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
    assert "github-api" in err and "collides with a literal" in err
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


def test_loader_accepts_preset_in_workspace_config(xdg):
    """config-v2: a `[[preset]]` reference is a first-class workspace-config
    construct now; the loader accepts it and the resolver expands it."""
    from credproxy_cli.core.model.config import load_config
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.paths import workspaces_config_dir
    from credproxy_cli.core.model.workspace import Workspace
    d = workspaces_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "w.toml").write_text('image = "x"\n[[preset]]\nname = "github"\n')
    ws = Workspace("w")
    assert load_config(ws)["image"] == "x"          # loader accepts `preset`
    assert len(resolve_workspace(ws).bindings) == 3  # resolver expands it


# ---- attached workspaces -----------------------------------------------------


def test_attach_template_binding_only_preset_ok(xdg):
    _attach_template(
        'attach = { compose_project = "{name}" }\n\n[[preset]]\nname = "github"\n')
    code, out, err = _run(
        ["--json", "workspace", "create", "att", "--attach", "container=foo"])
    assert code == 0
    text = _config_text("att")
    assert "[[preset]]" in text and "attach" in text
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.workspace import Workspace
    assert len(resolve_workspace(Workspace("att")).bindings) == 3


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


# ---- empty / inline / malformed preset keys (findings 1, 2, 5) ---------------


@pytest.mark.parametrize("body", [
    "preset = []\n",                       # empty inline array
    "\npreset = [\n]\n",                   # empty inline array, multiline
])
def test_empty_preset_key_rejected_nothing_written(xdg, body):
    """A `preset = []` (or any zero-entry inline form) must NOT survive into the
    stamped config (finding 1); create fails with the block-form remedy, not a
    downstream "use preset add" mismatch."""
    _template(_MIN + body)
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "`preset` key is empty" in err and "[[preset]]" in err
    _assert_no_workspace("proj")


def test_inline_preset_array_rejected(xdg):
    """An inline `preset = [{...}]` parses to entries but the surgical stripper
    only removes `[[preset]]` header blocks, so it would survive -- reject at
    create naming the block-form remedy (finding 2, option b)."""
    _template(_MIN + '\npreset = [{ name = "github" }]\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "declare template presets as `[[preset]]` blocks" in err
    _assert_no_workspace("proj")


def test_inline_preset_table_rejected(xdg):
    """`preset = { name = "x" }` (inline table, not array) is caught by the array
    validator with its own message; still nothing is written."""
    _template(_MIN + '\npreset = { name = "github" }\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "array of tables" in err
    _assert_no_workspace("proj")


def test_malformed_template_with_preset_ref_fails(xdg):
    """A template that fails to parse AND references presets can't be expanded --
    fail create rather than write a broken config with unexpanded preset text
    (finding 5)."""
    _template('image = "x"\nuser = \n[[preset]]\nname = "github"\n')  # bad TOML
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "malformed" in err and "[[preset]]" in err
    _assert_no_workspace("proj")


def test_malformed_template_without_preset_ref_writes_verbatim(xdg):
    """A malformed template with NO preset reference keeps the historical
    write-verbatim behavior (the parse error surfaces later, at start)."""
    _template('image = "x"\nuser = \n')          # bad TOML, no preset
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 0
    # It was written verbatim (broken TOML and all); create doesn't validate.
    assert "user =" in _config_text("proj")


# ---- multi-entry templates (finding 3) ---------------------------------------


def test_two_entries_expand_in_declaration_order(xdg):
    """Two `[[preset]]` entries expand IN ORDER; the first pack's blocks precede
    the second's in the stamped text and in the announcement."""
    _preset("svc", _NODEFAULT)
    _template(_MIN + '\n[[preset]]\nname = "github"\n\n'
              '[[preset]]\nname = "svc"\nprovider = "env"\nsecret = "T"\n')
    code, out, err = _run(["--json", "workspace", "create", "proj"])
    assert code == 0, out + err
    obj = json.loads(out)
    assert [p["preset"] for p in obj["presets"]] == ["github", "svc"]
    text = _config_text("proj")
    assert text.count("[[preset]]") == 2 and "[[binding]]" not in text
    # github's 3 parts + svc's 1 part, github first (literal-then-preset, in
    # `[[preset]]` declaration order).
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.workspace import Workspace
    names = [b.name for b in resolve_workspace(Workspace("proj")).bindings]
    assert names == ["github-api", "github-git", "github-ghcr", "svc-api"]


def test_two_entries_env_collision_aborts(xdg):
    """Two entries stamping a conflicting env key (different value) fail the whole
    create; nothing is written."""
    _preset("ca", '[env]\nFOO = "a"\n')
    _preset("cb", '[env]\nFOO = "b"\n')
    _template(_MIN + '\n[[preset]]\nname = "ca"\n\n[[preset]]\nname = "cb"\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "FOO" in err and "already set" in err
    _assert_no_workspace("proj")


def test_two_entries_same_pack_double_add_aborts(xdg):
    """Two entries naming the SAME pack trip the double-add guard, and at create
    the remedy names the duplicate template entry (finding 4)."""
    _template(_MIN + '\n[[preset]]\nname = "github"\n\n[[preset]]\nname = "github"\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "duplicate `[[preset]]` reference" in err and "github" in err
    _assert_no_workspace("proj")


def test_two_entries_mount_clash_aborts(xdg):
    """Two entries mounting at the same target fail; nothing is written."""
    _preset("ma", '[[mount]]\nvolume = "va"\ntarget = "/cache"\n')
    _preset("mb", '[[mount]]\nvolume = "vb"\ntarget = "/cache"\n')
    _template(_MIN + '\n[[preset]]\nname = "ma"\n\n[[preset]]\nname = "mb"\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "already mounted" in err
    _assert_no_workspace("proj")


# ---- container-half pack at create -------------------------------------------


_CONTAINER = (
    '[env]\nFOO = "bar"\n'
    '[[mount]]\nvolume = "cache"\ntarget = "/cache"\n'
    '[[setup]]\nrun = "echo hi"\norder = 30\n'
)


def test_container_half_pack_stamps_at_create(xdg):
    """A pure-container pack (mounts/env/setup) expands at create on a managed
    template and the stamped config loads with all three sections present."""
    _preset("cont", _CONTAINER)
    _template(_MIN + '\n[[preset]]\nname = "cont"\n')
    assert _run(["workspace", "create", "proj"])[0] == 0
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.workspace import Workspace
    cfg = resolve_workspace(Workspace("proj")).config
    assert cfg["env"] == {"FOO": "bar"}
    assert {m["target"] for m in cfg["mounts"]} == {"/cache"}
    assert cfg["setup"][-1]["run"] == "echo hi"
    assert "[[preset]]" in _config_text("proj")


def test_textual_identity_container_half_create_vs_add(xdg):
    """A container-half pack expanded at create is BYTE-IDENTICAL to create
    (plain) + `preset add` (no placeholder, so no pinning needed)."""
    _preset("cont", _CONTAINER)
    _template(_MIN + '\n[[preset]]\nname = "cont"\n')
    assert _run(["workspace", "create", "viatemplate"])[0] == 0

    _template(_MIN)                              # plain template, no preset
    assert _run(["workspace", "create", "viaadd"])[0] == 0
    assert _run(["workspace", "viaadd", "preset", "add", "cont"])[0] == 0

    assert _config_text("viatemplate") == _config_text("viaadd")


# ---- existing-set validation (finding 7) -------------------------------------


def test_pure_rule_pack_still_validates_existing_bindings(xdg):
    """A pack that adds NO bindings must still validate the EXISTING binding set
    standalone, so a pre-existing duplicate surfaces at create (finding 7)."""
    _preset("policy", '[[rule]]\nsuffix = "block"\naction = "block"\n'
            'hosts = ["evil.example.com"]\n')
    _template(_MIN + textwrap.dedent('''
        [[binding]]
        name = "dup"
        injector = "bearer"
        provider = "env"
        secret = "T"
        hosts = ["a.example.com"]

        [[binding]]
        name = "dup"
        injector = "bearer"
        provider = "env"
        secret = "T2"
        hosts = ["b.example.com"]

        [[preset]]
        name = "policy"
    '''))
    code, out, err = _run(["workspace", "create", "proj"])
    assert code == 1
    assert "dup" in err                          # the pre-existing collision
    _assert_no_workspace("proj")


# ---- loader rejection on an attached config (finding 3) ----------------------


# ---- create runs the pack's host-prereq checks (#57 + #58 finding 5) ---------


def test_create_template_preset_runs_requires(xdg):
    """#57 create expanding a template `[[preset]]` runs the pack's #58 host-prereq
    checks (only `preset add` exercised them before). A failing `command` requires
    is reported while the config still lands (advisory)."""
    _preset("gitsign", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]
        [[requires]]
        kind = "command"
        command = "definitely-absent-cmd-zzz"
        hint = "install the tool"
    """)
    _template(_MIN + '\n[[preset]]\nname = "gitsign"\n'
              'provider = "env"\nsecret = "TOK"\n')
    code, out, err = _run(["--json", "workspace", "create", "proj"])
    assert code == 0, out + err
    obj = json.loads(out)
    reqs = obj["presets"][0]["requires"]
    assert reqs and reqs[0]["kind"] == "command" and reqs[0]["ok"] is False
    assert reqs[0]["hint"] == "install the tool"
    assert "[[preset]]" in _config_text("proj")   # the reference landed


def test_create_requires_run_after_write_not_when_later_entry_aborts(xdg, monkeypatch):
    """Finding 5: requires (which may exec a provider) run only AFTER the atomic
    write succeeds. A create that aborts on a later entry invokes NO prereqs at
    all -- so no provider is exec'd for a create that writes nothing."""
    calls: list[int] = []
    from credproxy_cli.core.model import prereqs
    real = prereqs.evaluate
    monkeypatch.setattr(prereqs, "evaluate",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    _preset("first", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]
        [[requires]]
        kind = "provider"
        fetch = true
    """)
    _template(_MIN
              + '\n[[preset]]\nname = "first"\nprovider = "env"\nsecret = "TOK"\n'
              + '\n[[preset]]\nname = "nonexistent-pack-zzz"\n')
    code, out, err = _run(["workspace", "create", "proj"])
    assert code != 0                               # aborts on the unknown 2nd pack
    assert calls == []                             # requires never evaluated
    _assert_no_workspace("proj")
