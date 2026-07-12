"""A missing `docker` binary must surface as a DependencyError (one clean
`[credproxy] ...` line), never a raw FileNotFoundError traceback (#16).

Every CHECKED docker call routes through core.engine.docker._run/_popen, which
translate FileNotFoundError -> DependencyError; the odd-shaped porcelain call
sites (imageenv.load, dev-test execvp) catch-and-translate to the same shared
message. Best-effort calls (docker_quiet, logs_tail) must SWALLOW a missing
binary so they never mask the error they're helping diagnose.
"""
from __future__ import annotations

import subprocess

import pytest

from credproxy_cli.core.engine import docker
from credproxy_cli.core.errors import DependencyError


@pytest.fixture
def no_docker(monkeypatch):
    """Make every subprocess.run/Popen for `docker` raise FileNotFoundError, as
    the real subprocess layer does when the binary isn't on PATH."""
    def boom(argv, *a, **k):
        raise FileNotFoundError(2, "No such file or directory", "docker")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


# ---- checked helpers translate ----

@pytest.mark.parametrize("call", [
    lambda: docker.docker(["ps"]),
    lambda: docker.docker(["build", "."], stream=True),
    lambda: docker.docker_output(["ps"]),
    lambda: docker.inspect("credproxy-proxy-x", "{{.State.Status}}"),
    lambda: docker.container_status("credproxy-proxy-x"),
    lambda: docker.resolve_host_port("credproxy-proxy-x", 39998),
    lambda: docker.seed_volume_from_container("c", "/data", "vol", "img"),
])
def test_checked_docker_calls_raise_dependency_error(no_docker, call):
    with pytest.raises(DependencyError) as ei:
        call()
    assert "docker not found on PATH" in str(ei.value)


def test_dependency_error_message_is_the_shared_constant(no_docker):
    with pytest.raises(DependencyError) as ei:
        docker.docker_output(["ps"])
    assert str(ei.value) == docker.DOCKER_MISSING_MSG


# ---- best-effort helpers swallow (never mask the error being diagnosed) ----

def test_docker_quiet_swallows_missing_binary(no_docker):
    # Must not raise -- best-effort cleanup.
    docker.docker_quiet(["rm", "-f", "credproxy-proxy-x"])


def test_logs_tail_returns_empty_on_missing_binary(no_docker):
    assert docker.logs_tail("credproxy-proxy-x") == ""


# ---- imageenv.load: the first docker call a Docker-less new user hits ----

def test_imageenv_load_translates_missing_docker(monkeypatch):
    from credproxy_cli.core.engine import imageenv

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "docker")
    monkeypatch.setattr(subprocess, "check_output", boom)
    with pytest.raises(DependencyError) as ei:
        imageenv.ImageEnv.load("some-image")
    assert str(ei.value) == docker.DOCKER_MISSING_MSG
