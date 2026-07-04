"""The --json scriptable-surface contract: stdout carries exactly one parseable
JSON value (or JSON-lines for `logs`), errors serialize as a JSON object, and
prompts/usage never leak onto stdout."""
from __future__ import annotations

import json
import shutil

import pytest

from test_porcelain import _run


def test_json_leaf_argparse_error_is_json(xdg, workspaces_dir):
    """A bad flag on a verb used to dump raw argparse usage to stderr + exit 2,
    bypassing the renderer. It must now serialize as a JSON error on stdout."""
    code, out, err = _run(["--json", "workspace", "w", "recreate", "--bogus"])
    assert code != 0
    assert "error" in json.loads(out)        # single JSON object on stdout


def test_json_create_argparse_error_is_json(xdg):
    code, out, err = _run(["--json", "workspace", "create", "w", "--bogus"])
    assert code != 0
    assert "error" in json.loads(out)


def test_json_confirm_prompt_goes_to_stderr_not_stdout(xdg, workspaces_dir):
    """The destructive-confirm prompt must not land on stdout (it would corrupt
    the JSON stream); aborting yields a JSON error object, prompt on stderr."""
    from credproxy_cli.core import pointer
    from credproxy_cli.core.workspace import for_name

    assert _run(["workspace", "create", "w"])[0] == 0
    pointer.set_default(for_name("w"))               # make `delete` implicit

    code, out, err = _run(["--loose", "--json", "delete"],
                          stdin_text="n\n", stdin_isatty=True)
    assert code != 0
    assert json.loads(out)["error"]["message"] == "aborted"   # stdout: just JSON
    assert 'Delete workspace "w"' in err                       # prompt on stderr


def test_json_preset_emits_single_object(xdg, workspaces_dir):
    """`preset add` expands to several bindings/rules but is one command, so
    --json must emit ONE object, not one per binding."""
    assert _run(["workspace", "create", "w"])[0] == 0
    code, out, err = _run(["--json", "workspace", "w", "preset", "add", "github"])
    assert code == 0, out + err
    obj = json.loads(out)                            # parses as a SINGLE value
    assert obj["workspace"] == "w" and obj["preset"] == "github"
    assert len(obj["bindings"]) == 3                 # github preset -> 3 bindings
    assert obj["rules"] == []                        # binding-only builtin preset
    assert "newly_intercepted" in obj


@pytest.mark.skipif(shutil.which("docker") is None, reason="logs needs docker")
def test_json_logs_propagates_missing_container(xdg, workspaces_dir):
    """`logs --json` on a workspace whose proxy container doesn't exist must
    propagate docker's non-zero exit, not report success."""
    assert _run(["workspace", "create", "w"])[0] == 0   # config only; no container
    code, out, err = _run(["--json", "workspace", "w", "logs"])
    assert code != 0
