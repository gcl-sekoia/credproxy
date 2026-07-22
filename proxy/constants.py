"""Runtime constants -- read from the inherited process environment.

Values are declared as `ENV` in proxy/Dockerfile and inherited by the
process. Casts to int here so callers don't have to. Path/string
constants used elsewhere (CREDPROXY_TMPFS, CREDPROXY_TOKEN_PATH, ...) are
read at their use site rather than centralized here.
"""
import os

MITMPROXY_UID = int(os.environ["CREDPROXY_MITMPROXY_UID"])
HTTP_PORT     = int(os.environ["CREDPROXY_HTTP_PORT"])
PROXY_PORT    = int(os.environ["CREDPROXY_PROXY_PORT"])
SENTINEL_IP   = os.environ["CREDPROXY_SENTINEL_IP"]

# PostgreSQL credential broker. PG_PORT is the movable internal bind (the pg
# analogue of PROXY_PORT); PG_CLIENT_PORT (5432) is the fixed, well-known port
# the workspace dials at proxy.local. iptables REDIRECTs sentinel:PG_CLIENT_PORT
# -> PG_PORT, mirroring the HTTP :80 -> HTTP_PORT split. Both are shared between
# entrypoint.sh (the redirect rule) and Python (the listener bind + the DSN
# rendered by /exports.sh), so both live in the Dockerfile ENV block -- the
# shell<->python single source, exactly like PROXY_PORT. Neither is ever
# host-published (the broker is netns-only), so the host CLI never reads them.
PG_PORT        = int(os.environ["CREDPROXY_PG_PORT"])
PG_CLIENT_PORT = int(os.environ["CREDPROXY_PG_CLIENT_PORT"])
