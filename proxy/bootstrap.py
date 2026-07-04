"""Bootstrap routes: workspace-facing endpoints on the merged HTTP API.

Reached from the workspace via the iptables sentinel:80 ->
CREDPROXY_HTTP_PORT redirect installed in entrypoint.sh. All routes
are GET, all unauthenticated -- the data they expose is what the
workspace needs to function (CA cert, env vars, placeholders).

Inward API / least-disclosure: /setup returns the `bindings` list with
only the fields the workspace needs for self-configuration:
  name, placeholder, env, scheme, params, hosts.
It does NOT expose provider, secret-id, or real credential values --
those never reach the proxy from the push model anyway.
"""
import json
import os
from pathlib import Path

from aiohttp import web

from admin import STATE_KEY
from config import Credentials

CA_CERT_PATH = Path("/home/mitmuser/.mitmproxy/mitmproxy-ca-cert.pem")
VERSION = "0.0.1"

CA_ENV = {
    "SSL_CERT_FILE": "/tmp/proxy-ca.crt",
    "REQUESTS_CA_BUNDLE": "/tmp/proxy-ca.crt",
    "NODE_EXTRA_CA_CERTS": "/tmp/proxy-ca.crt",
    "GIT_SSL_CAINFO": "/tmp/proxy-ca.crt",
    "CARGO_HTTP_CAINFO": "/tmp/proxy-ca.crt",
    "AWS_CA_BUNDLE": "/tmp/proxy-ca.crt",
}

BOOTSTRAP_SH = """#!/bin/sh
# Run via: curl -sSL http://proxy.local/bootstrap.sh | sh
# Run as root (the default in most workspace images).
set -eu
CA_ONLY=/tmp/proxy-ca-only.crt   # the proxy CA alone (1 cert)
CA_PATH=/tmp/proxy-ca.crt        # system roots + proxy CA (what the env vars point at)
PROFILE_PATH=/etc/profile.d/credproxy.sh

curl -sf -o "$CA_ONLY" http://proxy.local/ca.crt

# Build the bundle the CA env vars point at: the system roots PLUS the proxy CA.
# Tools that honor SSL_CERT_FILE/REQUESTS_CA_BUNDLE/etc. as their SOLE trust
# anchor (mise, node, cargo, requests-via-certifi, aws) then verify BOTH
# intercepted hosts (signed by the proxy CA) and passthrough hosts (real certs,
# via the system roots). A proxy-CA-only bundle here breaks every passthrough host.
SYS_CA=""
for c in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
  if [ -f "$c" ]; then SYS_CA="$c"; break; fi
done
if [ -n "$SYS_CA" ]; then
  cat "$SYS_CA" "$CA_ONLY" > "$CA_PATH"
else
  # No system root bundle (minimal image): fall back to proxy-CA-only, so
  # intercepted hosts still work. Passthrough hosts will fail for env-var-only
  # tools -- such images usually lack roots anyway; install ca-certificates.
  cp "$CA_ONLY" "$CA_PATH"
  echo "credproxy: no system CA bundle found; $CA_PATH has the proxy CA only -- env-var-only tools cannot verify passthrough hosts (install ca-certificates)" >&2
fi

# System-wide trust covers curl, git, openssl, python stdlib. Best-effort:
# images without ca-certificates installed skip this gracefully. Install ONLY
# the proxy CA (the single cert) here -- never the combined bundle, or
# update-ca-certificates would re-append every system root to the system store.
if command -v update-ca-certificates >/dev/null 2>&1; then
  cp "$CA_ONLY" /usr/local/share/ca-certificates/proxy.crt 2>/dev/null \\
    && update-ca-certificates >/dev/null 2>&1 || true
fi

# Persistent env vars for tools that ignore the system trust store
# (Python requests via certifi, Node, Cargo, AWS SDKs). Picked up by
# future login shells; for the current shell, source it manually.
# Pulled from /env.sh so CA_ENV in the proxy is the single source.
if [ -d /etc/profile.d ] && [ -w /etc/profile.d ]; then
  curl -sf http://proxy.local/env.sh > "$PROFILE_PATH"
fi

echo "Bootstrap complete. CA bundle at $CA_PATH; env in $PROFILE_PATH."
"""

ENV_SH = "".join(f'export {k}="{v}"\n' for k, v in CA_ENV.items())

LLMS_TXT = """\
This sandbox runs behind a transparent egress proxy.

Bootstrap (do this once, as root):

    curl -sSL http://proxy.local/bootstrap.sh | sh

That installs the proxy CA system-wide and writes env vars to
/etc/profile.d/credproxy.sh. HTTPS to configured hosts is intercepted;
everything else is byte-passthrough.

For intercepted hosts, the proxy injects credentials automatically. Fetch the
active bindings -- what to present, and where -- from /setup:

    curl -s http://proxy.local/setup | jq .bindings

Each binding entry has:
  name        -- a handle for this credential (e.g. "github-api")
  placeholder -- the inert sentinel to send as the credential value. null for
                 sign-family schemes (sigv4) that compute auth per request.
  env         -- suggested env var name to export the placeholder as (may be null)
  scheme      -- how the proxy injects: bearer | basic | body (swap a
                 placeholder), sigv4 (re-sign), oauth2-reseal, or script
  params      -- scheme-specific settings (e.g. {"header": "Authorization"})
  hosts       -- hostnames this binding covers (may be GLOBS -- see below)

What YOU must do, per scheme:
  bearer/basic/body  Send the `placeholder` where the credential normally goes
                     (bearer: the header in params.header, default Authorization;
                     basic: as the username or password of HTTP Basic; body:
                     anywhere in the request body). The proxy swaps in the real
                     value. Example: env "GITHUB_TOKEN", placeholder "ghp_xxx..."
                     -> export GITHUB_TOKEN=ghp_xxx... and use it as usual.
  sigv4              No placeholder. Configure your AWS SDK with ANY dummy STATIC
                     credentials (an access key id + secret) and NO session
                     token, and sign normally; the proxy re-signs with the real
                     key. Do NOT use temporary/STS credentials (a session token)
                     -- they pass through unsigned and get rejected.
  oauth2-reseal      The `placeholder` is your client_secret for the TOKEN
                     endpoint (send it in the token request body). The proxy
                     authenticates, then returns a placeholder in place of the
                     minted access token -- present THAT on the API hosts. The
                     real token never enters the sandbox.
  script             A custom injector: present the `placeholder` as usual. Some
                     scripts need it in a SPECIFIC header that /setup does not
                     disclose -- if a script binding won't authenticate, ask the
                     operator where its placeholder must ride.

You never see a real credential value -- the proxy holds it. A request to an
intercepted host whose placeholder doesn't match is forwarded AS-IS: if you get
an upstream 401 while sending a placeholder-shaped token, the binding didn't fire
(wrong header, missing placeholder). The reason is in the proxy log -- ask the
operator to check `credproxy workspace NAME logs`.

Responses on intercepted hosts may be modified or refused by policy; the rules in
/setup's `rules` array are not necessarily exhaustive. A refused request returns
a synthetic status/body rather than the upstream's.

Network limits (invisible from inside the sandbox -- suspect these if a tool
hangs or a host is unreachable):
  - IPv6 is dropped entirely; use IPv4.
  - HTTP/3 / QUIC (UDP port 443) is dropped to force TCP fallback; a QUIC-pinned
    tool that won't fall back will hang. Disable HTTP/3.
  - `intercept_hosts` in /setup may contain GLOB patterns (e.g. *.amazonaws.com)
    where `*` spans dots -- match accordingly, don't compare literally.

If proxy.local does not resolve, use 169.254.1.1 directly.

Endpoints (all GET):
  /health        capture-readiness probe (json; 503 until intercept+CA are up)
  /ca.crt        CA certificate (PEM)
  /bootstrap.sh  one-shot setup: install CA + write /etc/profile.d
  /env.sh        env-var exports only (for `eval` use)
  /setup         JSON: ca_url, env, version, intercept_hosts, bindings, rules
  /llms.txt      this file
"""


def workspace_bindings(creds: Credentials) -> list[dict]:
    """JSON shape for /setup's `bindings` field.

    Returns only the workspace-safe binding fields: name, placeholder,
    env, scheme, params, hosts. Real credential values are intentionally
    absent (least disclosure). This data is safe to expose because
    placeholders are inert sentinels and params carry no secret.
    """
    return [
        {
            "name": b.name,
            "placeholder": b.placeholder,
            "env": b.env,
            "scheme": b.scheme,
            "params": b.params,
            "hosts": b.hosts,
        }
        for b in creds.inward_bindings()
    ]


def _json(obj, status: int = 200) -> web.Response:
    """JSON response, pretty-printed with a trailing newline so a bare `curl`
    of a bootstrap route reads cleanly. Insertion order is preserved (no key
    sorting). `jq` and parsers are unaffected by the whitespace."""
    return web.json_response(
        obj, status=status, dumps=lambda o: json.dumps(o, indent=2) + "\n")


def _capture_pending(state) -> list[str]:
    """What's still missing for the proxy to be *capture-ready* (not merely
    alive), most-fundamental first. Empty list == ready.

    - `mitmproxy-listener`: the transparent listener isn't bound yet (the addon's
      `running` hook hasn't fired). The HTTP listener answering `/health` proves
      only its OWN bind, not mitmproxy's.
    - `ca-cert`: mitmproxy hasn't generated its CA yet, so the first TLS
      connection to an intercepted host would fail with a cert error even though
      the listener is up.

    iptables is NOT probed: the entrypoint installs the rules under `set -e`
    before it execs python, so any answer at all implies they're in; and python
    then runs as an unprivileged uid that couldn't read the nat table anyway."""
    pending = []
    if not getattr(state, "capture_ready", False):
        pending.append("mitmproxy-listener")
    if not CA_CERT_PATH.exists():
        pending.append("ca-cert")
    return pending


async def health(request: web.Request) -> web.Response:
    """Capture-readiness probe (not liveness): 200 only once egress is actually
    being intercepted -- the mitmproxy listener is bound AND its CA exists.
    503 with a `pending` list until then, so `start`/`push --wait` and an
    external health-gate (Compose `service_healthy`) hold off handing the
    workspace a proxy that isn't yet capturing traffic (#23). Creds-readiness
    (a config having been pushed) is deliberately a SEPARATE, future signal --
    folding it in here would deadlock standalone `start`, which waits on
    `/health` and only THEN pushes config."""
    state = request.app[STATE_KEY]
    pending = _capture_pending(state)
    body = {"ok": not pending, "version": VERSION}
    if pending:
        body["pending"] = pending
        return _json(body, status=503)
    return _json(body)


async def ca_crt(_: web.Request) -> web.Response:
    try:
        pem = CA_CERT_PATH.read_bytes()
    except FileNotFoundError:
        return web.Response(status=503, text="CA not yet generated\n")
    return web.Response(body=pem, content_type="application/x-pem-file")


async def bootstrap_sh(_: web.Request) -> web.Response:
    return web.Response(body=BOOTSTRAP_SH, content_type="text/x-shellscript")


async def env_sh(_: web.Request) -> web.Response:
    return web.Response(body=ENV_SH, content_type="text/x-shellscript")


async def setup(request: web.Request) -> web.Response:
    state = request.app[STATE_KEY]
    return _json({
        "version": VERSION,
        "workspace": os.environ.get("CREDPROXY_WORKSPACE") or None,
        "ca_url": "http://proxy.local/ca.crt",
        "env": CA_ENV,
        # Least disclosure: hosts referenced ONLY by a hidden rule are withheld
        # here (a hidden tripwire must not be passively enumerable via /setup);
        # the decision path still intercepts them.
        "intercept_hosts": sorted(state.creds.disclosed_intercept_hosts()),
        "bindings": workspace_bindings(state.creds),
        # Least disclosure: only VISIBLE rules are enumerated (name, hosts,
        # methods, path, action -- never script source or rewrite values); hidden
        # rules are excluded entirely. The /llms.txt sentence keeps the workspace
        # honest-in-general that the list may not be exhaustive.
        "rules": state.creds.rule_set().inward_rules(),
    })


async def llms_txt(_: web.Request) -> web.Response:
    return web.Response(body=LLMS_TXT, content_type="text/plain", charset="utf-8")


async def index(_: web.Request) -> web.Response:
    """Friendly route map for a bare GET / (e.g. `curl http://proxy.local`),
    instead of a 404. Exposes only route names and the workspace name (already
    public via /setup) -- nothing sensitive."""
    ws = os.environ.get("CREDPROXY_WORKSPACE") or "?"
    body = (
        f"credproxy proxy — workspace '{ws}'\n\n"
        "Bootstrap routes (open, no auth):\n"
        "  GET /             this page\n"
        "  GET /health       capture-readiness (503 until intercept+CA up)\n"
        "  GET /ca.crt       proxy CA certificate (PEM)\n"
        "  GET /bootstrap.sh install CA + trust env  (curl -sSL proxy.local/bootstrap.sh | sh)\n"
        "  GET /env.sh       CA-trust env exports\n"
        "  GET /setup        bindings + workspace info (JSON)\n"
        "  GET /llms.txt     guidance for agents\n\n"
        "Admin routes (/admin/*) require a bearer token and are host-only.\n"
    )
    return web.Response(text=body, content_type="text/plain")


bootstrap_routes = [
    web.get("/", index),
    web.get("/health", health),
    web.get("/ca.crt", ca_crt),
    web.get("/bootstrap.sh", bootstrap_sh),
    web.get("/env.sh", env_sh),
    web.get("/setup", setup),
    web.get("/llms.txt", llms_txt),
]
