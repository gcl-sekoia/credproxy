"""`dev test` overlay-test discovery (do_dev_test).

Each configured overlay with a `tests/` subdir must run as its OWN pytest
invocation (never merged into the repo suite, whose module basenames collide),
with the same on-host-or-image fallback as the proxy suite. The container branch
must mount the whole overlay chain and rewrite CREDPROXY_OVERLAY_PATH to the
container paths.

subprocess / exec / docker are mocked the way tests/cli/test_lifecycle.py stubs
the docker layer -- record args, return a fake result -- so no daemon runs.
"""
from __future__ import annotations

import importlib.util
import subprocess
import os

import pytest

from credproxy_cli.porcelain import cli as porcelain


class _FakeResult:
    def __init__(self, returncode=0):
        self.returncode = returncode


@pytest.fixture
def overlay_with_tests(tmp_path, monkeypatch):
    ov = tmp_path / "team-overlay"
    (ov / "tests").mkdir(parents=True)
    (ov / "tests" / "test_ov.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    return ov


def _record_subprocess(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _FakeResult(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _ctx():
    return porcelain.Ctx(loose=False, as_json=False, assume_yes=False)


def test_onhost_overlay_tests_run_as_separate_invocation(overlay_with_tests, monkeypatch):
    ov = overlay_with_tests
    calls = _record_subprocess(monkeypatch)
    # Force the on-host branch: pretend the proxy deps import.
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    # Exec must NOT be taken (overlay suites follow the proxy suite).
    monkeypatch.setattr(os, "execvpe",
                        lambda *a, **k: pytest.fail("execvpe should not run"))

    with pytest.raises(SystemExit) as se:
        # proxy_only skips the CLI run; on-host proxy + overlay run as subprocesses.
        porcelain.do_dev_test(_ctx(), [], proxy_only=True)
    assert se.value.code == 0

    # One invocation targets the overlay's tests dir, distinct from the proxy suite.
    ov_tests = str(ov / "tests")
    overlay_calls = [c for c in calls if any(ov_tests == arg for arg in c)]
    assert len(overlay_calls) == 1, calls
    proxy_calls = [c for c in calls if any(arg.endswith("/tests") and "overlay" not in arg
                                           for arg in c)]
    # The proxy suite ran too (a separate invocation, not merged with the overlay).
    assert any("--ignore" in c for c in calls)
    assert overlay_calls[0] is not (proxy_calls[0] if proxy_calls else None)


def test_onhost_overlay_failure_fails_combined(overlay_with_tests, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        # Fail only the overlay invocation; the proxy suite passes.
        rc = 1 if any("tests" in arg and "team-overlay" in arg for arg in cmd) else 0
        return _FakeResult(rc)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(os, "execvpe", lambda *a, **k: pytest.fail("no exec"))

    with pytest.raises(SystemExit) as se:
        porcelain.do_dev_test(_ctx(), [], proxy_only=True)
    assert se.value.code == 1


def test_container_branch_mounts_chain_and_rewrites_env(overlay_with_tests, monkeypatch):
    ov = overlay_with_tests
    calls = _record_subprocess(monkeypatch)
    # Force the container branch and avoid a real ImageEnv/docker inspect.
    from credproxy_cli.core.engine.imageenv import ImageEnv
    monkeypatch.setattr(ImageEnv, "load",
                        classmethod(lambda cls, image=None: ImageEnv(
                            http_port=39998, tmpfs="/run/secrets",
                            token="/run/secrets-ro/auth.token", source="/opt/proxy",
                            mitmproxy_uid=31337)))
    monkeypatch.setattr(os, "execvp", lambda *a, **k: pytest.fail("no exec"))

    with pytest.raises(SystemExit) as se:
        porcelain.do_dev_test(_ctx(), [], proxy_only=True, force_container=True)
    assert se.value.code == 0

    # Find the overlay docker invocation (targets /opt/overlays/0/tests).
    ov_cmd = next(c for c in calls
                  if any(a == "/opt/overlays/0/tests" for a in c))
    # The whole overlay chain is bind-mounted read-only at the container path...
    assert f"{ov}:/opt/overlays/0:ro" in ov_cmd
    # ...and CREDPROXY_OVERLAY_PATH is rewritten to the container path(s).
    ei = ov_cmd.index("-e")
    # (the first -e is the overlay path; assert it appears among the -e values)
    env_vals = [ov_cmd[i + 1] for i, tok in enumerate(ov_cmd) if tok == "-e"]
    assert "CREDPROXY_OVERLAY_PATH=/opt/overlays/0" in env_vals

    # The proxy suite invocation ALSO mounts the overlay chain (resolution must
    # see every tier), even though it runs the repo tests.
    proxy_cmd = next(c for c in calls if "tests/" in c and "--ignore=tests/cli" in c)
    assert f"{ov}:/opt/overlays/0:ro" in proxy_cmd
