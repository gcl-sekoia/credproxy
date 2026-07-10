"""Pure transport to the proxy's HTTP API over the published 127.0.0.1 port.

Read-only/status round-trips (GET /admin/config, POST /admin/rule-test) and the
/health readiness poll. The config-PUSH path (materialize + resolve secrets +
encode the wire body + POST) lives in the push engine (`engine/push.py`), which
composes the model-plane wire encoder with this transport. Failures raise
ProxyError (connect / readiness / 401 / non-200).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..errors import ProxyError
from ..model.workspace import Workspace, read_token


def _http_post_json(url: str, body: bytes, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw}
    except urllib.error.URLError as e:
        raise ProxyError(f"connect error talking to the proxy: {e.reason}")


def get_config(admin_url: str, token: str, timeout: float = 2.0) -> dict | None:
    """GET <admin_url>/admin/config: the parsed superset dict, or None if the proxy
    can't be reached or doesn't answer 200. Callers treat None as 'proxy offline /
    can't confirm'.

    Transport-only (per #61): it returns the raw dict and imports no binding/rule
    model -- the projection/comparison lives in the model plane. The body carries
    `loaded`/`fingerprint` (fast path) plus `generation`/`bindings`/`rules` (the
    sanitized live config), but this layer stays agnostic to the shape. Works
    against ANY loopback admin URL (a managed proxy's published port or an attached
    proxy's resolved URL), so the live drift compare rides the exact URL push does."""
    req = urllib.request.Request(
        f"{admin_url}/admin/config",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                payload = json.loads(resp.read().decode())
                return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, json.JSONDecodeError, ConnectionError,
            TimeoutError, OSError):
        return None
    return None


def proxy_status(ws: Workspace, http_port: int) -> dict | None:
    """GET /admin/config on the managed proxy's published port: the superset dict
    (the fast path reads its `loaded`/`fingerprint` fields), or None if unreachable.
    A thin wrapper over `get_config` so the fast path and the live drift compare hit
    the same transport."""
    return get_config(f"http://127.0.0.1:{http_port}", read_token(ws))


def rule_test_live(ws: Workspace, http_port: int, method: str, url: str) -> dict:
    """POST /admin/rule-test: the running proxy's authoritative rule dry-run for
    (method, url) against its LOADED config -- exact per-script phase + the
    intercept decision. Raises ProxyError on 401/non-200/connect failure."""
    status, payload = _http_post_json(
        f"http://127.0.0.1:{http_port}/admin/rule-test",
        json.dumps({"method": method, "url": url}).encode(),
        read_token(ws),
    )
    if status == 200:
        return payload
    if status == 401:
        raise ProxyError(
            f"proxy rejected the token (HTTP 401); check {ws.token_path}")
    raise ProxyError(
        f"proxy rule-test failed (HTTP {status}): {payload.get('error', payload)}")


def wait_for_ready(http_port: int, timeout: float = 15.0) -> None:
    """Poll /health until the proxy is capture-ready (200) or `timeout` elapses.

    /health returns 503 with a `{"pending": [...]}` body while the mitmproxy
    listener or CA isn't up yet (urllib raises HTTPError, a URLError subclass, so
    that's treated as keep-polling). On timeout we surface the LAST pending reason
    -- the exact thing that was still missing -- instead of a bare 503, so a stuck
    boot names what it's stuck on rather than leaving the operator to guess."""
    deadline = time.monotonic() + timeout
    last_pending: list | None = None
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{http_port}/health", timeout=1
            ) as resp:
                if resp.status == 200:
                    return
        except urllib.error.HTTPError as e:
            last_err = e
            # 503 carries the capture-readiness reason in its body; keep the most
            # recent so the timeout message can name it.
            if e.code == 503:
                try:
                    last_pending = json.loads(e.read()).get("pending")
                except (ValueError, OSError):
                    pass
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
        time.sleep(0.1)
    detail = (f"still waiting on: {', '.join(last_pending)}"
              if last_pending else str(last_err))
    raise ProxyError(
        f"proxy did not become capture-ready within {timeout:.0f}s ({detail})"
    )
