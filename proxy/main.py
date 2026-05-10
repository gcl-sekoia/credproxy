"""Proxy entrypoint: mitmproxy (transparent) + merged HTTP API.

Two listeners on one asyncio loop:

- mitmproxy on 127.0.0.1:39999 (transparent intercept).
- aiohttp on 0.0.0.0:39998 -- admin routes (bearer-gated) and
  bootstrap routes (workspace-facing, open) on the same listener.

The auth token is bind-mounted at /run/secrets-ro/auth.token from the
host; admin.py reads it fresh per request, so host-side rotation
takes effect without a restart. Config lives on tmpfs at
/run/secrets/config.json, written by POST /admin/config. Python
respawns within the same container reload state from tmpfs; full
container restart drops the config (host re-pushes via bin/credproxy
push-config). The bash supervisor restarts python on death.
"""
import asyncio
import sys

from aiohttp import web
from mitmproxy import options
from mitmproxy.tools.dump import DumpMaster

import addon
import admin
import bootstrap
from constants import HTTP_PORT, PROXY_PORT


def make_http_app(state: admin.AppState) -> web.Application:
    app = web.Application(
        middlewares=[admin.no_store, admin.access_log, admin.fetch_metadata_guard]
    )
    app[admin.STATE_KEY] = state
    app.router.add_routes(admin.admin_routes)
    app.router.add_routes(bootstrap.bootstrap_routes)
    return app


async def run() -> None:
    state = admin.load_initial_state()
    print(
        f"[main] state: intercept_hosts={sorted(state.creds.intercept_hosts())}",
        flush=True,
    )

    http_runner = web.AppRunner(make_http_app(state), access_log=None)
    await http_runner.setup()
    await web.TCPSite(http_runner, "0.0.0.0", HTTP_PORT).start()
    print(
        f"[main] HTTP API listening on 0.0.0.0:{HTTP_PORT}",
        flush=True,
    )

    opts = options.Options(
        listen_host="127.0.0.1",
        listen_port=PROXY_PORT,
        mode=["transparent"],
    )
    master = DumpMaster(opts, with_termlog=True, with_dumper=False)
    master.addons.add(addon.HostnameLogger(state))
    print(
        f"[main] mitmproxy listening on 127.0.0.1:{PROXY_PORT} (transparent)",
        flush=True,
    )

    try:
        await master.run()
    finally:
        await http_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)
