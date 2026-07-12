"""Guard the loose-surface discoverability strings (issue #74).

Two kinds of check:

1. **Example lint** -- every command example the CLI *emits* (the shared
   `render.EX_*` tails, the `render.cmd()` output, and the concrete funnel
   lines) is validated against the real argparse parser tree, reusing
   `test_docs_lint._lint_command`. So a hint/footer/skeleton example can't rot
   the way a stale doc line would.

2. **Behavioral** -- the funnel, noun-alone `list`, did-you-mean, surface-aware
   footers/empty-states, and the report-all `binding add` skeleton actually
   render, driven through `test_porcelain._run`/`_run_loose`.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

_CLI_DIR = str(Path(__file__).resolve().parents[2] / "cli")
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from credproxy_cli.porcelain import cli as _cli  # noqa: E402
from credproxy_cli.porcelain import render  # noqa: E402
from test_docs_lint import _lint_command  # noqa: E402
from test_porcelain import _run, _run_loose  # noqa: E402


# =============================================================================
# 1. Example lint -- every emitted command example must parse
# =============================================================================

# Scoped-verb example tails (valid as `credp <tail>` and `credproxy workspace
# NAME <tail>`). `EX_CREATE_HERE` is a noun-verb, handled separately.
_SCOPED_TAILS = [render.EX_BINDING_ADD, render.EX_PACK_ADD, render.EX_RULE_ADD,
                 render.EX_MOUNT_ADD]


@pytest.mark.parametrize("tail", _SCOPED_TAILS)
def test_scoped_example_tails_valid(tail):
    assert _lint_command("credp", shlex.split(f"credp {tail}")) is None
    assert _lint_command(
        "credproxy", shlex.split(f"credproxy workspace NAME {tail}")) is None


def test_create_here_example_valid():
    # `create --here` is a noun-verb: `credp create --here` /
    # `credproxy workspace create NAME`.
    assert _lint_command(
        "credp", shlex.split(f"credp {render.EX_CREATE_HERE}")) is None
    assert _lint_command(
        "credproxy", shlex.split("credproxy workspace create NAME")) is None


def test_cmd_helper_outputs_lint_clean():
    """render.cmd() must produce a parseable command on both surfaces, for a
    scoped verb and a top-level one."""
    try:
        for loose in (True, False):
            render.set_surface(loose)
            surface = "credp" if loose else "credproxy"
            for tail in (render.EX_BINDING_ADD, render.EX_PACK_ADD,
                         render.EX_RULE_ADD, "start"):
                s = render.cmd(tail)
                assert _lint_command(surface, shlex.split(s)) is None, (loose, s)
            s = render.cmd("list", top_level=True)
            assert _lint_command(surface, shlex.split(s)) is None, (loose, s)
    finally:
        render.set_surface(False)


def test_cmd_ws_threading_names_the_workspace():
    """A hint about a specific workspace (push/start follow-up) names it on both
    surfaces and stays parseable -- loose `credp start demo`, strict
    `credproxy workspace demo start` (issue #74, name-aware follow-ups)."""
    try:
        render.set_surface(True)
        assert render.cmd("start", ws="demo") == "credp start demo"
        assert _lint_command("credp", shlex.split(render.cmd("start", ws="demo"))) is None
        render.set_surface(False)
        assert render.cmd("start", ws="demo") == "credproxy workspace demo start"
        assert _lint_command(
            "credproxy", shlex.split(render.cmd("start", ws="demo"))) is None
    finally:
        render.set_surface(False)


# The concrete (non-compact, no `|`/`[...]`) command lines in the loose funnel.
# The compact usage summaries in _LOOSE_HELP_ALL/_STRICT_HELP are intentionally
# NOT linted (they carry `|`/placeholders, like docs_lint skips them).
_FUNNEL_CONCRETE = [
    "credp create --here",
    "credp enter",
    "credp list",
    "credp inspect",
    "credp logs",
    "credp help all",
]


@pytest.mark.parametrize("line", _FUNNEL_CONCRETE)
def test_funnel_concrete_lines_valid(line):
    toks = shlex.split(line)
    if toks[1:] == ["help", "all"]:
        return  # `help` is a help sentinel, not a dispatched command
    assert _lint_command("credp", toks) is None, line


def test_funnel_examples_appear_in_help_text():
    """The funnel really carries the shared examples (so the lint above guards
    what users see, not a divergent copy)."""
    assert render.EX_PACK_ADD in _cli._LOOSE_HELP
    assert render.EX_BINDING_ADD in _cli._LOOSE_HELP
    for line in _FUNNEL_CONCRETE:
        assert line in _cli._LOOSE_HELP


# =============================================================================
# 2. Behavioral
# =============================================================================


def test_bare_credp_is_the_funnel():
    ec, out, err = _run_loose([])
    assert ec == 0
    blob = out + err
    assert "Start here:" in blob
    assert "Give it credentials" in blob
    assert "credp help all" in blob


def test_help_all_is_the_full_listing():
    ec, out, err = _run_loose(["help", "all"])
    assert ec == 0
    blob = out + err
    # A line only the full inventory carries (not the funnel).
    assert "emit-compose" in blob


def test_unknown_command_did_you_mean_loose():
    ec, out, err = _run_loose(["bindings", "list"])
    assert ec != 0
    assert "did you mean `binding`?" in err


def test_unknown_command_did_you_mean_strict():
    ec, out, err = _run(["emit-composee"])
    assert ec != 0
    assert "did you mean `emit-compose`?" in err


def test_bare_noun_never_leaks_argparse_dest(xdg, workspaces_dir):
    """`workspace NAME binding` with no subcommand shows the list, never the raw
    `bindingcmd is required` argparse error (issue #74 #2)."""
    (workspaces_dir / "demo.toml").write_text('image = "x"\n')
    ec, out, err = _run(["workspace", "demo", "binding"])
    blob = out + err
    assert "bindingcmd" not in blob
    assert "no bindings in workspace 'demo'" in blob
    # surface-aware footer names the strict form
    assert "credproxy workspace NAME binding add" in err


def test_bare_noun_footer_surface_aware_loose(xdg, workspaces_dir, monkeypatch):
    (workspaces_dir / "demo.toml").write_text('image = "x"\n')
    from credproxy_cli.core.model.pointer import set_default
    from credproxy_cli.core.model.workspace import Workspace
    set_default(Workspace("demo"))
    ec, out, err = _run_loose(["rule"])
    blob = out + err
    assert "rulecmd" not in blob
    assert "no rules in workspace 'demo'" in blob
    assert "credp rule add" in err
    assert "credproxy workspace" not in err  # loose never shows the strict form


def test_empty_workspace_list_names_next_command(xdg):
    ec, out, err = _run_loose(["list"])
    assert ec == 0
    assert "no workspaces" in out
    assert "credp create --here" in err


def test_binding_add_reports_all_missing_flags(xdg, workspaces_dir):
    (workspaces_dir / "demo.toml").write_text('image = "x"\n')
    ec, out, err = _run(["workspace", "demo", "binding", "add",
                         "--injector", "bearer"])
    assert ec != 0
    # all three still-missing flags in one shot, not one at a time
    assert "--provider" in err and "--host" in err and "--secret" in err
    # copy-pasteable skeleton
    assert "credproxy workspace NAME binding add" in err


def test_binding_add_flagless_points_at_packs(xdg, workspaces_dir):
    (workspaces_dir / "demo.toml").write_text('image = "x"\n')
    ec, out, err = _run(["workspace", "demo", "binding", "add"])
    assert ec != 0
    assert "--injector" in err
    assert render.EX_PACK_ADD in err  # the pack pointer for a flagless add
