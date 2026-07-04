"""`workspace start` readiness-failure diagnostics.

When the proxy doesn't answer /health, the bare "Connection refused" hid the
usual cause -- the proxy crashed on boot. `_proxy_diagnostics` inspects the
container and surfaces its exit code + log tail inline (blind-agent finding:
they had to run `logs` separately to learn the real reason). These tests cover
the diagnostic formatting with docker stubbed out (no daemon needed).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def ws():
    from credproxy_cli.core.workspace import Workspace
    return Workspace("demo")


def _patch_docker(monkeypatch, *, status, exit_code="1", logs=""):
    from credproxy_cli.core import docker
    monkeypatch.setattr(docker, "container_status", lambda n: status)
    monkeypatch.setattr(docker, "inspect", lambda n, f: exit_code)
    monkeypatch.setattr(docker, "logs_tail", lambda n, k=20: logs)


def test_diagnostics_crashed_surfaces_exit_and_logs(monkeypatch, ws):
    from credproxy_cli.core import lifecycle
    _patch_docker(
        monkeypatch, status="exited", exit_code="1",
        logs="Traceback (most recent call last):\n"
             "ModuleNotFoundError: No module named 'placeholders'\n",
    )
    out = lifecycle._proxy_diagnostics(ws)
    assert "exited (code 1)" in out and "crashed on startup" in out
    assert "placeholders" in out                      # the real cause, inline
    assert f"credproxy workspace {ws.name} logs" in out


def test_diagnostics_running_but_unready(monkeypatch, ws):
    from credproxy_cli.core import lifecycle
    _patch_docker(monkeypatch, status="running", logs="starting up...\n")
    out = lifecycle._proxy_diagnostics(ws)
    assert "running" in out and "not yet capture-ready" in out


def test_diagnostics_container_gone(monkeypatch, ws):
    from credproxy_cli.core import lifecycle
    _patch_docker(monkeypatch, status=None)
    out = lifecycle._proxy_diagnostics(ws)
    assert "gone" in out


def test_diagnostics_no_logs_still_ok(monkeypatch, ws):
    from credproxy_cli.core import lifecycle
    _patch_docker(monkeypatch, status="exited", exit_code="2", logs="")
    out = lifecycle._proxy_diagnostics(ws)
    assert "exited (code 2)" in out
    # No log tail available, but the full-logs hint is still present.
    assert "last proxy log lines" not in out
    assert f"credproxy workspace {ws.name} logs" in out
