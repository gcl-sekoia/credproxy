"""Tests for core/proxy_http.wait_for_ready readiness diagnostics (#23 review)."""
from __future__ import annotations

import io
import urllib.error

import pytest

from credproxy_cli.core import proxy_http
from credproxy_cli.core.errors import ProxyError


def _http_error_503(pending):
    """A urllib HTTPError(503) whose body is the /health capture-readiness JSON."""
    import json
    body = io.BytesIO(json.dumps({"ok": False, "pending": pending}).encode())
    return urllib.error.HTTPError(
        "http://x/health", 503, "Service Unavailable", {}, body)


def test_wait_for_ready_surfaces_pending_reason(monkeypatch):
    """On a stuck boot, the timeout error must NAME what /health is still waiting
    on (carried in the 503 body), not a bare `HTTP Error 503` -- the whole point
    of the pending list is to reach the operator."""
    monkeypatch.setattr(proxy_http.time, "sleep", lambda _s: None)

    def fake_urlopen(url, timeout=1):
        raise _http_error_503(["mitmproxy-listener", "ca-cert"])
    monkeypatch.setattr(proxy_http.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ProxyError) as ei:
        proxy_http.wait_for_ready(12345, timeout=0.01)
    msg = str(ei.value)
    assert "capture-ready" in msg
    assert "mitmproxy-listener" in msg and "ca-cert" in msg


def test_wait_for_ready_returns_on_200(monkeypatch):
    monkeypatch.setattr(proxy_http.time, "sleep", lambda _s: None)

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(proxy_http.urllib.request, "urlopen",
                        lambda url, timeout=1: Resp())
    proxy_http.wait_for_ready(12345, timeout=1.0)   # must not raise


def test_wait_for_ready_connection_refused_reports_last_err(monkeypatch):
    """Before the HTTP listener is even up (connection refused, no 503 body),
    fall back to the raw error rather than an empty pending message."""
    monkeypatch.setattr(proxy_http.time, "sleep", lambda _s: None)

    def fake_urlopen(url, timeout=1):
        raise urllib.error.URLError("Connection refused")
    monkeypatch.setattr(proxy_http.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ProxyError) as ei:
        proxy_http.wait_for_ready(12345, timeout=0.01)
    assert "Connection refused" in str(ei.value)
