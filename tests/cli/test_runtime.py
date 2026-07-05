"""Tests for core/runtime.py: the podman-rootless probe and its failure modes.

The probe shells `docker info -f '{{.Host.Security.Rootless}}'`. We stub
subprocess.run so the test never touches a real daemon, and clear the lru_cache
between cases.
"""
from __future__ import annotations

import subprocess
import types

import pytest


@pytest.fixture
def runtime():
    from credproxy_cli.core import runtime
    # Both predicates share one memoized probe (`_probe`); clear it between
    # cases so each stub is observed.
    runtime._probe.cache_clear()
    yield runtime
    runtime._probe.cache_clear()


def _stub(monkeypatch, runtime, *, returncode=0, stdout="", raises=None):
    def fake_run(args, **kw):
        if raises is not None:
            raise raises
        return types.SimpleNamespace(returncode=returncode, stdout=stdout)
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)


def test_podman_rootless_true(runtime, monkeypatch):
    """Podman rootless: the template returns 'true'."""
    _stub(monkeypatch, runtime, returncode=0, stdout="true\n")
    assert runtime.is_podman_rootless() is True


def test_podman_rootful_false(runtime, monkeypatch):
    """Podman rootful: template returns 'false'."""
    _stub(monkeypatch, runtime, returncode=0, stdout="false\n")
    assert runtime.is_podman_rootless() is False


def test_real_docker_template_errors_to_false(runtime, monkeypatch):
    """Real Docker has no .Host field -> non-zero exit -> False."""
    _stub(monkeypatch, runtime, returncode=1, stdout="")
    assert runtime.is_podman_rootless() is False


def test_no_binary_is_false(runtime, monkeypatch):
    """Missing docker binary (OSError) -> False, no raise."""
    _stub(monkeypatch, runtime, raises=FileNotFoundError())
    assert runtime.is_podman_rootless() is False


def test_timeout_is_false(runtime, monkeypatch):
    """A daemon timeout -> False, no raise."""
    _stub(monkeypatch, runtime, raises=subprocess.TimeoutExpired(cmd="docker", timeout=10))
    assert runtime.is_podman_rootless() is False


def test_result_is_cached(runtime, monkeypatch):
    """The probe is memoized: the second call doesn't shell out again."""
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout="true")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    assert runtime.is_podman_rootless() is True
    assert runtime.is_podman_rootless() is True
    assert len(calls) == 1


# ---- is_podman(): the coarser engine discriminator (any podman) -------------


def test_is_podman_rootless_is_podman(runtime, monkeypatch):
    """Rootless podman: exit 0 prints 'true' -> is_podman True."""
    _stub(monkeypatch, runtime, returncode=0, stdout="true\n")
    assert runtime.is_podman() is True


def test_is_podman_rootful_is_podman(runtime, monkeypatch):
    """Rootful podman: exit 0 prints 'false' -> is_podman True (zero exit is the
    discriminator, not the printed value)."""
    _stub(monkeypatch, runtime, returncode=0, stdout="false\n")
    assert runtime.is_podman() is True
    # ...and the rootless predicate correctly disagrees on the same probe.
    assert runtime.is_podman_rootless() is False


def test_is_podman_docker_template_error_is_false(runtime, monkeypatch):
    """Real Docker: the template errors (non-zero exit) -> is_podman False."""
    _stub(monkeypatch, runtime, returncode=1, stdout="")
    assert runtime.is_podman() is False


def test_is_podman_probe_failure_is_false(runtime, monkeypatch):
    """A probe failure (no binary/timeout) -> is_podman False."""
    _stub(monkeypatch, runtime, raises=FileNotFoundError())
    assert runtime.is_podman() is False


def test_both_predicates_share_one_round_trip(runtime, monkeypatch):
    """is_podman() and is_podman_rootless() read the SAME cached probe, so the
    two together shell out only once."""
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout="true")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    assert runtime.is_podman() is True
    assert runtime.is_podman_rootless() is True
    assert len(calls) == 1
