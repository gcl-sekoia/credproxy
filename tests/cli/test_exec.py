"""Tests for `exec` (sessions._exec_cmd argv modes + exec_workspace behavior)."""
from __future__ import annotations

import types

import pytest


# ---- _exec_cmd argv modes ----------------------------------------------------


def test_exec_cmd_default_uses_ca_trust_shim():
    """Default mode wraps CMD in the SAME env shim `enter -- CMD` uses, so the
    CA-trust env is sourced (the headline #31 fix -- `exec -- curl` must not fail
    TLS where `enter -- curl` succeeds)."""
    from credproxy_cli.core.engine.sessions import _exec_cmd, DEFAULT_ENTER_PRELUDE
    cmd = _exec_cmd({"user": "dev", "home": "/home/dev"}, "cx",
                    ["curl", "https://api.github.com"],
                    mode="shim", user_override=None, isatty=False)
    assert cmd[:2] == ["docker", "exec"]
    assert cmd[cmd.index("--workdir") + 1] == "/home/dev"
    assert cmd[cmd.index("-u") + 1] == "dev"
    assert "--tty=false" in cmd and "--interactive=true" in cmd
    # container, then the sh env shim sourcing the CA-trust prelude, then exec CMD
    i = cmd.index("cx")
    assert cmd[i + 1:i + 4] == ["sh", "-c", f'{DEFAULT_ENTER_PRELUDE}; exec "$@"']
    assert cmd[i + 4] == "credproxy-exec"          # $0 label
    assert cmd[-2:] == ["curl", "https://api.github.com"]


def test_exec_cmd_raw_is_direct_execve():
    from credproxy_cli.core.engine.sessions import _exec_cmd
    cmd = _exec_cmd({"home": "/h"}, "cx", ["gh", "auth", "status"],
                    mode="raw", user_override=None, isatty=False)
    # No shell wrapper: the command follows the container as raw argv.
    assert cmd[cmd.index("cx") + 1:] == ["gh", "auth", "status"]
    assert "sh" not in cmd and "bash" not in cmd


def test_exec_cmd_login_wraps_bash_login_shell():
    from credproxy_cli.core.engine.sessions import _exec_cmd
    cmd = _exec_cmd({}, "cx", ["gh", "x"], mode="login",
                    user_override=None, isatty=True)
    assert "--tty=true" in cmd
    i = cmd.index("cx")
    assert cmd[i + 1:i + 4] == ["bash", "-lc", 'exec "$@"']
    assert cmd[-2:] == ["gh", "x"]


def test_exec_cmd_user_override_beats_config_user():
    from credproxy_cli.core.engine.sessions import _exec_cmd
    cmd = _exec_cmd({"user": "dev"}, "cx", ["id"], mode="raw",
                    user_override="root", isatty=False)
    # docker last-wins: only the override -u should be honoured (no config -u dev
    # after it), so the LAST -u value is root.
    us = [cmd[i + 1] for i, t in enumerate(cmd) if t == "-u"]
    assert us[-1] == "root"


def test_enter_and_exec_share_the_docker_exec_prefix():
    """Regression guard: both verbs build the prefix through _docker_exec_argv, so
    they honour workdir/user/exec_flags/session booleans identically."""
    from credproxy_cli.core.engine.sessions import _exec_cmd, _enter_exec_cmd
    cfg = {"user": "dev", "home": "/h", "exec_flags": ["--env", "X=1"]}
    e = _enter_exec_cmd(cfg, "cx", ["cmd"], user_override=None, isatty=False)
    x = _exec_cmd(cfg, "cx", ["cmd"], mode="raw", user_override=None, isatty=False)
    # Same prefix up to and including the container name.
    assert e[:e.index("cx") + 1] == x[:x.index("cx") + 1]


# ---- exec_workspace: no auto-stop, but reaper-visible; exit mapping ----------


def _fake_ws(tmp_path):
    """A stand-in Workspace with just the state-dir plumbing exec_workspace uses."""
    import contextlib
    sessions = tmp_path / "sessions"

    @contextlib.contextmanager
    def lock():
        yield

    return types.SimpleNamespace(
        name="w", ws_container="cx", proxy_container="px",
        sessions_dir=sessions, exists=lambda: True, lock=lock,
    )


def test_exec_registers_pidfile_but_never_auto_stops(tmp_path, monkeypatch):
    """exec must be VISIBLE to the auto-stop reaper (writes a pidfile) yet never
    INITIATE a stop (no _maybe_auto_stop call) -- so a concurrent enter teardown
    can't stop the box under it, and exec itself causes no churn (#31.3)."""
    import subprocess
    from credproxy_cli.core.engine import containers, setup, startup, sessions

    ws = _fake_ws(tmp_path)
    seen = {"pidfile_existed_during_run": False, "auto_stop_called": False}

    monkeypatch.setattr(sessions, "_start_for_exec", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "load_config", lambda ws: {"user": "dev"})
    monkeypatch.setattr(sessions, "_maybe_auto_stop",
                        lambda *a, **k: seen.__setitem__("auto_stop_called", True))

    def fake_run(cmd, check=False):
        # A session pidfile exists WHILE the command runs (reaper can see us).
        seen["pidfile_existed_during_run"] = any(ws.sessions_dir.glob("*"))
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sessions.exec_workspace(ws, ["true"], mode="raw")
    assert rc == 0
    assert seen["pidfile_existed_during_run"] is True     # reaper-visible
    assert seen["auto_stop_called"] is False              # never initiates a stop
    assert list(ws.sessions_dir.glob("*")) == []          # cleaned up after


def test_exec_maps_signal_death_exit_code(tmp_path, monkeypatch):
    """A signal death (-N) maps to 128+N, so SIGINT is 130 (not OS-truncated 254)
    and the returned code matches the process's own exit (#31.2)."""
    import signal
    import subprocess
    from credproxy_cli.core.engine import containers, setup, startup, sessions

    ws = _fake_ws(tmp_path)
    monkeypatch.setattr(sessions, "_start_for_exec", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "load_config", lambda ws: {})
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=-signal.SIGINT))
    rc = sessions.exec_workspace(ws, ["sleep", "9"], mode="raw")
    assert rc == 128 + int(signal.SIGINT)   # 130


def test_exec_fast_path_skips_start_when_both_running(tmp_path, monkeypatch):
    """Both containers running + no --push -> skip the full start reconciliation
    (no wait_for_ready / start_workspace); just exec (#31.4)."""
    from credproxy_cli.core.engine import containers, setup, startup, sessions, docker

    ws = _fake_ws(tmp_path)
    monkeypatch.setattr(docker, "container_status", lambda n: "running")
    called = {"start": False}
    monkeypatch.setattr(startup, "start_workspace",
                        lambda *a, **k: called.__setitem__("start", True))
    sessions._start_for_exec(ws, containers._noop, push=False)
    assert called["start"] is False


def test_exec_fast_path_bypassed_by_push(tmp_path, monkeypatch):
    from credproxy_cli.core.engine import containers, setup, startup, sessions, docker

    ws = _fake_ws(tmp_path)
    monkeypatch.setattr(docker, "container_status", lambda n: "running")
    called = {"start": False}
    monkeypatch.setattr(startup, "start_workspace",
                        lambda *a, **k: called.__setitem__("start", True))
    sessions._start_for_exec(ws, containers._noop, push=True)
    assert called["start"] is True


def test_exec_fast_path_bypassed_when_container_down(tmp_path, monkeypatch):
    from credproxy_cli.core.engine import containers, setup, startup, sessions, docker

    ws = _fake_ws(tmp_path)
    # proxy running, workspace container down -> full start.
    monkeypatch.setattr(docker, "container_status",
                        lambda n: "running" if n == "px" else "exited")
    called = {"start": False}
    monkeypatch.setattr(startup, "start_workspace",
                        lambda *a, **k: called.__setitem__("start", True))
    sessions._start_for_exec(ws, containers._noop, push=False)
    assert called["start"] is True
