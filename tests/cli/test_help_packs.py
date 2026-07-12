"""Tests for the usability fixes from the blind-agent round:

  - `--help` is honored on subcommands (the leaf argparse parsers are
    add_help=False, so the hand-rolled dispatch must do it) and never has a
    side effect -- in particular `scaffold --help` must not write a file named
    '--help' (the original bug).
  - `binding add`/`binding test`/`create` print descriptive help.
  - `pack list` enumerates the coordinated multi-binding sets, and a
    `--pack` add announces its expansion.
  - the scaffolded TOML template references a real injector (not the `github`
    pack) and the canonical `workspace NAME start` verb order.
"""
from __future__ import annotations

import json

import pytest

from test_porcelain import _run


def _rbindings(ws):
    from credproxy_cli.core.model.resolver import resolve_workspace
    return resolve_workspace(ws).bindings


def _rrules(ws):
    from credproxy_cli.core.model.resolver import resolve_workspace
    return resolve_workspace(ws).rules


# ---- scaffold --help no longer mutates state ---------------------------------


@pytest.mark.parametrize("kind", ["provider", "injector"])
def test_scaffold_help_shows_help_and_writes_nothing(kind, xdg):
    from credproxy_cli.core.paths import (
        injectors_config_dir, providers_config_dir,
    )
    code, out, err = _run([kind, "scaffold", "--help"])
    assert code == 0
    assert "scaffold NAME" in (out + err)
    # The original bug: '--help' got treated as a NAME and a file was created.
    reg = providers_config_dir() if kind == "provider" else injectors_config_dir()
    assert not reg.exists() or not any(reg.iterdir())


@pytest.mark.parametrize("kind", ["provider", "injector"])
def test_scaffold_rejects_flag_like_name(kind, xdg):
    from credproxy_cli.core.paths import (
        injectors_config_dir, providers_config_dir,
    )
    code, out, err = _run([kind, "scaffold", "--weird"])
    assert code == 1
    # Rejected, and nothing written (the real invariant).
    reg = providers_config_dir() if kind == "provider" else injectors_config_dir()
    assert not reg.exists() or not any(reg.iterdir())


def test_scaffold_core_rejects_bad_names():
    from credproxy_cli.core.model.scaffold import scaffold
    from credproxy_cli.core.errors import CredproxyError
    for bad in ("--help", "", "a/b", ".."):
        with pytest.raises(CredproxyError):
            scaffold("provider", bad)


# ---- uniform --help on subcommands -------------------------------------------


def test_create_help_exits_zero_with_text(xdg):
    code, out, err = _run(["workspace", "create", "--help"])
    assert code == 0
    blob = out + err
    assert "workspace create NAME" in blob


def test_binding_add_help_describes_flags(xdg):
    code, out, err = _run(["workspace", "foo", "binding", "add", "--help"])
    assert code == 0
    blob = out + err
    # The friction the blind agents hit: what does --secret mean for env?
    assert "host env var NAME" in blob
    assert "--injector" in blob   # --pack retired -> `pack add` noun


def test_binding_test_help_exits_zero(xdg):
    code, out, err = _run(["workspace", "foo", "binding", "test", "--help"])
    assert code == 0
    assert "ad-hoc" in (out + err)


def test_dev_help_exits_zero():
    code, out, err = _run(["dev", "--help"])
    assert code == 0
    assert "build" in (out + err)


@pytest.mark.parametrize("verb", ["start", "stop", "delete", "apply",
                                  "inspect", "edit", "logs", "enter"])
def test_lifecycle_verb_help_is_descriptive(verb, xdg):
    """Each lifecycle verb's --help exits 0 with a real description (not just a
    bare `usage:` line), and -- crucially -- does NOT execute the verb."""
    code, out, err = _run(["workspace", "foo", verb, "--help"])
    assert code == 0
    blob = out + err
    assert f"workspace NAME {verb}" in blob
    assert " -- " in blob  # has a description clause, not only a usage line


def test_start_help_does_not_start(xdg):
    """--help must short-circuit before any handler runs (no docker calls)."""
    # `foo` does not exist; if start ran it would error on the missing
    # workspace/docker. Help must return cleanly instead.
    code, out, err = _run(["workspace", "foo", "start", "--help"])
    assert code == 0


# ---- pack list -------------------------------------------------------------


def test_pack_list_human():
    code, out, err = _run(["pack", "list"])
    assert code == 0
    blob = out + err
    assert "github" in blob
    assert "github-api" in blob and "github-git" in blob and "github-ghcr" in blob


def test_pack_list_json():
    code, out, err = _run(["--json", "pack", "list"])
    assert code == 0
    data = json.loads(out)
    names = {p["name"] for p in data}
    assert "github" in names
    gh = next(p for p in data if p["name"] == "github")
    assert len(gh["bindings"]) == 3
    api = next(b for b in gh["bindings"] if b["name"] == "github-api")
    assert api["injector"] == "bearer" and api["hosts"] == ["api.github.com"]


def test_pack_bare_and_help_both_list():
    for argv in (["pack"], ["pack", "--help"]):
        code, out, err = _run(argv)
        assert code == 0 and "github" in (out + err)


def test_pack_unknown_subcommand_errors():
    code, out, err = _run(["pack", "bogus"])
    assert code == 1


# ---- pack expansion is announced -------------------------------------------


def test_pack_add_announces_expansion(ws_factory):
    ws_factory("demo")
    code, out, err = _run(
        ["workspace", "demo", "pack", "add", "github",
         "--provider", "env", "--secret", "GITHUB_TOKEN"]
    )
    assert code == 0
    blob = out + err
    assert "applied pack 'github'" in blob and "3 binding(s)" in blob


# ---- pack default provider / secret ----------------------------------------


def test_pack_github_defaults_provider_and_secret(ws_factory):
    """`pack add github` with no flags wires all three bindings off the gh-cli
    provider with the github.com host as the ref."""
    ws = ws_factory("demo")
    code, out, err = _run(["workspace", "demo", "pack", "add", "github"])
    assert code == 0, out + err
    from credproxy_cli.core.model.bindings import load_bindings
    bindings = _rbindings(ws)
    assert {b.name for b in bindings} == {"github-api", "github-git", "github-ghcr"}
    assert all(b.provider == "gh-cli" and b.secret == "github.com" for b in bindings)


def test_pack_github_secret_override_keeps_default_provider(ws_factory):
    """An explicit --secret (e.g. an Enterprise host) overrides the ref while the
    provider still defaults to gh-cli."""
    ws = ws_factory("demo")
    code, out, err = _run(["workspace", "demo", "pack", "add", "github",
                           "--secret", "ghe.corp.com"])
    assert code == 0, out + err
    from credproxy_cli.core.model.bindings import load_bindings
    assert all(b.provider == "gh-cli" and b.secret == "ghe.corp.com"
               for b in _rbindings(ws))


def test_pack_nondefault_provider_requires_secret(ws_factory):
    """A non-default provider can't borrow the default ref -- a ref's meaning is
    provider-specific -- so --secret stays required."""
    ws_factory("demo")
    code, out, err = _run(["workspace", "demo", "pack", "add", "github",
                           "--provider", "env"])
    assert code == 1
    assert "needs --secret" in (out + err)


def test_injector_add_still_requires_provider(ws_factory):
    """With --provider now optional at the parser level (so packs can default
    it), the --injector path must still demand it explicitly."""
    ws_factory("demo")
    code, out, err = _run(["workspace", "demo", "binding", "add", "--injector",
                           "bearer", "--secret", "TOK", "--host", "api.example.com"])
    assert code == 1
    assert "missing: --provider" in (out + err)


# ---- scaffolded template hygiene ---------------------------------------------


def test_template_uses_real_injector_and_verb_order(ws_factory):
    """The template must not present the `github` PACK as an injector, and
    must use the canonical `workspace NAME start` verb order."""
    from credproxy_cli.core.model.config import render_template
    rendered = render_template("demo")
    assert 'injector = "github"' not in rendered
    assert 'injector = "bearer"' in rendered
    assert "credproxy workspace demo start" in rendered
    assert "credproxy start demo" not in rendered


# ---- pack add: service setup packs (#37) ----------------------------------


def _install_pack(pack_name: str, toml: str, scripts: dict | None = None):
    from credproxy_cli.core.paths import config_dir, scripts_config_dir
    pd = config_dir() / "packs"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{pack_name}.toml").write_text(toml)
    for sname, src in (scripts or {}).items():
        sd = scripts_config_dir()
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"{sname}.star").write_text(src)


_GUARD_STAR = "def on_request():\n    return True\n"

_MIXED_PACK = """
[placeholder]
prefix = "ghp_"
length = 40
charset = "alnumeric"

[[part]]
suffix = "api"
injector = "bearer"
hosts = ["api.github.com"]
env = "GITHUB_TOKEN"

[[rule]]
suffix = "readonly"
hosts = ["api.github.com"]
action = "script"
script = "guard"
[rule.params]
allow_prefixes = ["/repos/scratch-"]
"""


def test_pack_add_stamps_bindings_and_rules(ws_factory):
    ws = ws_factory("demo")
    _install_pack("gh-guarded", _MIXED_PACK, scripts={"guard": _GUARD_STAR})
    code, out, err = _run(["workspace", "demo", "pack", "add", "gh-guarded",
                           "--provider", "env", "--secret", "GITHUB_TOKEN"])
    assert code == 0, out + err
    from credproxy_cli.core.model.bindings import load_bindings
    from credproxy_cli.core.model.rules import load_rules
    assert [b.name for b in _rbindings(ws)] == ["gh-guarded-api"]
    rules = _rrules(ws)
    assert [r.name for r in rules] == ["gh-guarded-readonly"]
    # the [rule.params] sub-table round-trips through the surgical stamp + reload.
    assert rules[0].params == {"allow_prefixes": ["/repos/scratch-"]}


def test_pack_add_pure_rule_no_flags(ws_factory):
    ws = ws_factory("demo")
    _install_pack("policy",
                    '[[rule]]\nsuffix="noDelete"\nhosts=["api.github.com"]\n'
                    'action="block"\nmethods=["DELETE"]\n')
    code, out, err = _run(["workspace", "demo", "pack", "add", "policy"])
    assert code == 0, out + err
    from credproxy_cli.core.model.bindings import load_bindings
    from credproxy_cli.core.model.rules import load_rules
    assert _rbindings(ws) == []
    assert [r.name for r in _rrules(ws)] == ["policy-noDelete"]


def test_pack_add_pure_rule_rejects_provider(ws_factory):
    ws_factory("demo")
    _install_pack("policy", '[[rule]]\nsuffix="b"\nhosts=["h.example"]\n'
                              'action="block"\n')
    code, out, err = _run(["workspace", "demo", "pack", "add", "policy",
                           "--provider", "env", "--secret", "X"])
    assert code == 1
    assert "pure-rule" in (out + err)


def test_pack_add_atomic_collision_no_partial_stamp(ws_factory):
    ws = ws_factory("demo")
    _install_pack("gh-guarded", _MIXED_PACK, scripts={"guard": _GUARD_STAR})
    # Pre-create a binding whose name collides with the pack's rule expansion.
    from credproxy_cli.core.model.bindings import Binding, append_bindings
    append_bindings(ws, [Binding(
        name="gh-guarded-api", injector="bearer", provider="env", secret="T",
        hosts=("api.github.com",), placeholder="ghp_x", env=None)])
    code, out, err = _run(["workspace", "demo", "pack", "add", "gh-guarded",
                           "--provider", "env", "--secret", "T"])
    assert code == 1
    assert "collides with a literal" in (out + err)
    # No PARTIAL stamp: the rule must NOT have been written.
    assert _rrules(ws) == []


def test_pack_add_announces_newly_intercepted(ws_factory):
    ws_factory("demo")
    _install_pack("policy", '[[rule]]\nsuffix="b"\nhosts=["fresh.example.com"]\n'
                              'action="block"\n')
    code, out, err = _run(["workspace", "demo", "pack", "add", "policy"])
    assert code == 0, out + err
    assert "newly intercepted" in (out + err) and "fresh.example.com" in (out + err)


def test_binding_add_pack_flag_retired(ws_factory):
    ws_factory("demo")
    code, out, err = _run(["workspace", "demo", "binding", "add",
                           "--pack", "github"])
    # --pack is gone from binding add; argparse rejects the unknown flag.
    assert code != 0


def test_pack_add_loose_resolves_default_workspace(ws_factory):
    from credproxy_cli.core.model.pointer import set_default
    ws = ws_factory("demo")
    set_default(ws)
    _install_pack("policy", '[[rule]]\nsuffix="b"\nhosts=["h.example"]\n'
                              'action="block"\n')
    code, out, err = _run(["--loose", "pack", "add", "policy"])
    assert code == 0, out + err
    from credproxy_cli.core.model.rules import load_rules
    assert [r.name for r in _rrules(ws)] == ["policy-b"]


def test_pack_add_strict_top_level_needs_workspace():
    code, out, err = _run(["pack", "add", "github"])   # strict, no workspace
    assert code == 1 and "needs a workspace" in (out + err)
