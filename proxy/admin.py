"""Admin HTTP API: host-only management endpoints.

Served on 0.0.0.0:39997 inside the proxy's netns. Two access patterns:

1. From the host: docker -p 127.0.0.1:39997:39997 forwards host loopback
   to the container; arrives via the netns INPUT chain.
2. From the workspace: blocked by an iptables OUTPUT rule installed in
   entrypoint.sh: connections originating in the netns from a non-
   mitmuser uid (i.e., the workspace) get DROPped before they reach
   the listener.

All endpoints under /admin/* require `Authorization: Bearer <token>`,
matched against the auth_token passed in via the stdin envelope at
startup. The token lives only in the proxy's heap and (host-side) in
.run/auth.token, mode 0600.
"""
import hmac

from aiohttp import web

TOKEN_KEY = web.AppKey("auth_token", str)


def _unauthorized(detail: str = "missing or invalid token") -> web.Response:
    return web.Response(status=401, text=f"unauthorized: {detail}\n")


@web.middleware
async def bearer_auth(request: web.Request, handler):
    expected = request.app[TOKEN_KEY]
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme != "Bearer" or not presented:
        return _unauthorized("expected `Authorization: Bearer <token>`")
    # constant-time compare to avoid trivial timing oracle
    if not hmac.compare_digest(presented, expected):
        return _unauthorized()
    return await handler(request)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def make_admin_app(auth_token: str) -> web.Application:
    app = web.Application(middlewares=[bearer_auth])
    app[TOKEN_KEY] = auth_token
    app.router.add_get("/admin/health", health)
    return app
