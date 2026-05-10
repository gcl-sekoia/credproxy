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
