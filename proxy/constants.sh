# Runtime constants -- single source of truth for shell + Makefile.
# Mirrored in proxy/constants.py for python; keep in sync.
#
# Plain KEY=VALUE only (no `export`, no command substitution): this
# file is sourced by entrypoint.sh AND `include`d by the root Makefile.
# Both parsers agree on this minimal syntax.

MITMPROXY_UID=31337
HTTP_PORT=39998
PROXY_PORT=39999
SENTINEL_IP=169.254.1.1
