"""The builtin `op` (1Password) provider.

It shells out to the `op` CLI, so these tests run it against a fake `op` on
PATH -- exercising request parsing, response shape, newline handling, and exit
codes without 1Password.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

_REF = "op://Private/Good/credential"


def _op():
    from credproxy_cli.core.paths import builtin_providers_dir
    return builtin_providers_dir() / "op"


@pytest.fixture
def fake_op(tmp_path, monkeypatch):
    """A stub `op` on PATH: returns 'sekret\\n' for one reference, fails else."""
    binop = tmp_path / "op"
    binop.write_text(
        "#!/bin/sh\n"
        'ref=""\n'
        'for a in "$@"; do case "$a" in op://*) ref="$a";; esac; done\n'
        f'[ "$ref" = "{_REF}" ] && {{ printf \'sekret\\n\'; exit 0; }}\n'
        'echo "[ERROR] item not found" >&2; exit 1\n'
    )
    binop.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return binop


def _run(req: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_op())], input=json.dumps(req), capture_output=True, text=True,
    )


def test_op_found(fake_op):
    r = _run({"version": 1, "op": "get", "secrets": [_REF]})
    assert r.returncode == 0
    assert json.loads(r.stdout) == {"values": {_REF: "sekret"}}  # newline stripped


def test_op_not_found(fake_op):
    r = _run({"version": 1, "op": "get", "secrets": ["op://x/y/z"]})
    assert r.returncode == 2
    assert "could not read" in r.stderr


def test_op_unsupported_version(fake_op):
    r = _run({"version": 2, "op": "get", "secrets": []})
    assert r.returncode == 3


def test_op_is_executable():
    assert os.access(_op(), os.X_OK)
