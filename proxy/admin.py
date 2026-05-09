"""HTTP API: admin endpoints + shared AppState + middleware.

Single aiohttp listener on 0.0.0.0:HTTP_PORT serves both this module's
admin routes and bootstrap.py's workspace-facing routes. Workspace
reaches it via the iptables sentinel:80 -> HTTP_PORT redirect; host
reaches it via docker -p 127.0.0.1:HTTP_PORT.

Trust model:
- /admin/state is open: returns {"initialized": bool}; lets the host
  CLI detect the TOFU window.
- /admin/config is TOFU on first call: the bearer in that request
  becomes the canonical admin token (persisted to tmpfs); subsequent
  calls authenticate against it.
- fetch_metadata_guard rejects cross-origin browser requests; together
  with Chrome's Private Network Access default-deny (we never set
  Access-Control-Allow-Private-Network), this covers the browser
  threat without a shared secret.

Restart: token + config live on tmpfs at /run/secrets/{auth.token,
config.json}. Survives python respawns; full container restart returns
to TOFU. The host CLI surfaces the mismatch via a 401 on next push.
"""
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

import config
from config import Credentials, YamlCredentials

HTTP_PORT = 39998

TOKEN_PATH = Path("/run/secrets/auth.token")
CONFIG_PATH = Path("/run/secrets/config.json")


@dataclass
class AppState:
    token: str = ""  # "" -> TOFU
    creds: Credentials = field(default_factory=lambda: YamlCredentials({}))


STATE_KEY: web.AppKey[AppState] = web.AppKey("state", AppState)


def load_initial_state() -> AppState:
    """Boot-time state load. Either file missing -> TOFU."""
    if not TOKEN_PATH.exists() or not CONFIG_PATH.exists():
        return AppState()
    try:
        creds = config.load_resolved(
            json.loads(CONFIG_PATH.read_text()), source=str(CONFIG_PATH)
        )
    except config.ConfigError as e:
        raise SystemExit(f"[admin] persisted config invalid: {e}")
    return AppState(token=TOKEN_PATH.read_text().strip(), creds=creds)


# ---- Middleware ----


@web.middleware
async def fetch_metadata_guard(request: web.Request, handler):
    """Reject cross-origin browser requests.

    Sec-Fetch-Site is a forbidden header name (browsers set it; JS
    cannot override). If present, require same-origin or none; if
    absent, the client isn't a browser -- allow.
    """
    sfs = request.headers.get("Sec-Fetch-Site")
    if sfs is not None and sfs not in ("same-origin", "none"):
        return web.json_response(
            {"error": "cross-origin requests forbidden"}, status=403
        )
    return await handler(request)


@web.middleware
async def no_store(request: web.Request, handler):
    resp = await handler(request)
    if isinstance(resp, web.StreamResponse):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@web.middleware
async def access_log(request: web.Request, handler):
    print(f"[http] {request.method} {request.path}", flush=True)
    return await handler(request)


# ---- Admin handlers ----


async def admin_state(request: web.Request) -> web.Response:
    state = request.app[STATE_KEY]
    return web.json_response({"initialized": bool(state.token)})


async def admin_config(request: web.Request) -> web.Response:
    """POST /admin/config -- TOFU on first call, bearer-gated thereafter."""
    state = request.app[STATE_KEY]

    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme != "Bearer" or not presented:
        return web.json_response(
            {"error": "expected `Authorization: Bearer <token>`"},
            status=401,
        )

    # Authenticate before any body processing so unauthenticated probes
    # can't fingerprint config schema by reading 400 errors. TOFU is the
    # exception: any bearer is accepted because we're claiming the proxy.
    if state.token and not hmac.compare_digest(presented, state.token):
        return web.json_response({"error": "invalid token"}, status=401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    try:
        new_creds = config.load_resolved(body, source="POST /admin/config")
    except config.ConfigError as e:
        return web.json_response({"error": str(e)}, status=400)

    body_bytes = json.dumps(body).encode()

    if not state.token:
        # TOFU: first valid POST claims the proxy.
        _atomic_write(TOKEN_PATH, presented.encode(), 0o400)
        _atomic_write(CONFIG_PATH, body_bytes, 0o400)
        state.token = presented
        state.creds = new_creds
        return web.json_response({"ok": True, "initialized": True})

    _atomic_write(CONFIG_PATH, body_bytes, 0o400)
    state.creds = new_creds
    return web.json_response({"ok": True, "reloaded": True})


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    tmp = str(path) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


admin_routes = [
    web.get("/admin/state", admin_state),
    web.post("/admin/config", admin_config),
]
