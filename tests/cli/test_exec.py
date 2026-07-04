"""Tests for `exec` (lifecycle._exec_cmd argv + exec_workspace no-session)."""
from __future__ import annotations


def test_exec_cmd_raw_argv_by_default():
    from credproxy_cli.core.lifecycle import _exec_cmd
    cmd = _exec_cmd({"user": "dev", "home": "/home/dev"}, "cx",
                    ["gh", "auth", "status"], login=False, isatty=False)
    assert cmd[:2] == ["docker", "exec"]
    assert cmd[cmd.index("--workdir") + 1] == "/home/dev"
    assert cmd[cmd.index("-u") + 1] == "dev"
    assert "--tty=false" in cmd and "--interactive=true" in cmd
    # container, then the command as RAW argv (no shell wrapper)
    assert cmd[cmd.index("cx") + 1:] == ["gh", "auth", "status"]


def test_exec_cmd_login_wraps_bash_login_shell():
    from credproxy_cli.core.lifecycle import _exec_cmd
    cmd = _exec_cmd({}, "cx", ["gh", "x"], login=True, isatty=True)
    assert "--tty=true" in cmd
    i = cmd.index("cx")
    assert cmd[i + 1:i + 4] == ["bash", "-lc", 'exec "$@"']
    assert cmd[-2:] == ["gh", "x"]
