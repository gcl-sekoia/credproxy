"""Tests for `credproxy doctor` (core/doctor.py + the porcelain command)."""
from __future__ import annotations

import json
import textwrap

import pytest


def _write(workspaces_dir, name, body):
    (workspaces_dir / f"{name}.toml").write_text(textwrap.dedent(body))


def test_doctor_reports_all_binding_failures(xdg, workspaces_dir):
    # A binding with BOTH a bad injector and a bad host glob: doctor reports both
    # in one run (unlike start, which fails at the first). Env checks may vary by
    # host, so assert only the workspace-level checks.
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "bad", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "does-not-exist-zzz"
        provider = "env"
        secret = "TOK"
        hosts = ["*.com"]
        placeholder = "PH"
    """)
    fails = [c for c in doctor.run("bad") if not c.ok]
    assert any(c.id.endswith(":injector") for c in fails)   # bad injector
    assert any(c.id.endswith(":host") for c in fails)       # bad host pattern


def test_doctor_valid_workspace_passes_its_checks(xdg, workspaces_dir):
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "good", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["api.github.com"]
        placeholder = "PH"
    """)
    ws_checks = [c for c in doctor.run("good") if c.id.startswith("ws:good")]
    assert ws_checks and all(c.ok for c in ws_checks)


def test_doctor_missing_docker_reported(xdg, monkeypatch):
    from credproxy_cli.core import doctor
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    checks = doctor._env_checks()
    assert checks[0].id == "docker" and not checks[0].ok
    assert "not found" in checks[0].message and checks[0].hint


def test_doctor_missing_workspace_reported(xdg):
    from credproxy_cli.core import doctor
    fails = [c for c in doctor.run("nope") if not c.ok]
    assert any("no config file" in c.message for c in fails)
