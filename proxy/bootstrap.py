"""Bootstrap routes: workspace-facing endpoints on the merged HTTP API.

Reached from the workspace via the iptables sentinel:80 ->
CREDPROXY_HTTP_PORT redirect installed in entrypoint.sh. All routes
are GET, all unauthenticated -- the data they expose is what the
workspace needs to function (CA cert, env vars, placeholders).

Inward API / least-disclosure: /setup returns `bindings` as an OBJECT keyed
by binding name (a set addressed by name -- names are unique per the wire
loader), each value carrying only the fields the workspace needs for
self-configuration: placeholder, env, scheme, params, hosts. `rules` stays an
ordered ARRAY (declaration order is the evaluation-order semantic). Neither
exposes provider, secret-id, or real credential values -- those never reach
the proxy from the push model anyway.
"""
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from admin import STATE_KEY
from config import Credentials
from constants import PG_CLIENT_PORT, PG_PORT, PROXY_PORT
from pg import PgCredentials

CA_CERT_PATH = Path("/home/mitmuser/.mitmproxy/mitmproxy-ca-cert.pem")
VERSION = "0.0.1"

# Where the mitmproxy transparent listener binds (main.run). `/health` TCP-probes
# it directly rather than trusting an addon-set flag, so readiness is observed
# live -- version-proof against mitmproxy's (undocumented, unpinned) hook ordering
# and continuously truthful if the listener ever dies without taking PID 1 down.
PROXY_HOST = "127.0.0.1"

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

# Persistent env for future login shells (tools that ignore the system trust
# store: Python requests via certifi, Node, Cargo, AWS SDKs). Two parts:
#   1. A SNAPSHOT of the CA-trust exports (/env.sh). Static per proxy lifetime;
#      CA trust must not depend on the proxy answering at shell startup.
#   2. A DYNAMIC line that re-fetches the binding exports (/exports.sh) on every
#      new login shell, so a freshly-added binding's placeholder is already in
#      the environment. Degrades to a silent no-op if the proxy is unreachable.
if [ -d /etc/profile.d ] && [ -w /etc/profile.d ]; then
  curl -sf http://proxy.local/env.sh > "$PROFILE_PATH"
  echo 'eval "$(curl -sf --max-time 1 http://proxy.local/exports.sh 2>/dev/null)"' >> "$PROFILE_PATH"
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
active bindings -- what to present, and where -- from /setup. `bindings` is an
OBJECT keyed by binding name (iterate with `to_entries`; the key is the name):

    curl -s http://proxy.local/setup | jq '.bindings | to_entries[]'

Each binding value has:
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
                     value. If the binding has an `env`, a LOGIN shell (after
                     bootstrap) already exports the placeholder under that name
                     via /etc/profile.d -- e.g. $GITHUB_TOKEN is set. The
                     fallback for any other shell (exports every binding's
                     placeholder into the current one):

                         eval "$(curl -s http://proxy.local/exports.sh)"

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

PostgreSQL: databases are NOT reached through the HTTP interception above --
they go through a separate credential-injecting broker you dial explicitly.
Fetch the pg upstreams (an object keyed by binding name) from /setup:

    curl -s http://proxy.local/setup | jq '.pg_bindings'

Each value has `env` (suggested env var, e.g. DATABASE_URL), `dbname`, and a
ready-made `dsn`. Connect your client to that `dsn`
(postgresql://<binding>@proxy.local:5432/<db>) -- you authenticate as the
binding NAME with NO password (the client leg is trusted loopback); the broker
re-originates to the real database with the real credential. After bootstrap a
login shell already exports the DSN under `env` (e.g. $DATABASE_URL) via
/exports.sh. You never see the real host, user, or password.

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
  /ready         creds-readiness probe (json; 503 until /health passes AND a
                 config has been pushed -- carries {"generation": N})
  /ca.crt        CA certificate (PEM)
  /bootstrap.sh  one-shot setup: install CA + write /etc/profile.d
  /env.sh        CA-trust env exports only (for `eval` use)
  /exports.sh    binding placeholder exports (`export ENV="placeholder"`)
  /setup         JSON: ca_url, ca_env, version, intercept_hosts, bindings,
                 pg_bindings, rules
  /llms.txt      this file
"""


def workspace_bindings(creds: Credentials) -> dict:
    """JSON shape for /setup's `bindings` field: an OBJECT keyed by binding
    name (unique per the wire loader), so the workspace addresses a binding by
    name without scanning. The inner value carries only the workspace-safe
    fields -- placeholder, env, scheme, params, hosts (the `name` is the key,
    never repeated). Real credential values are intentionally absent (least
    disclosure): placeholders are inert sentinels and params carry no secret.
    """
    return {
        b.name: {
            "placeholder": b.placeholder,
            "env": b.env,
            "scheme": b.scheme,
            "params": b.params,
            "hosts": b.hosts,
        }
        for b in creds.inward_bindings()
    }


def pg_workspace_bindings(pg_creds: PgCredentials) -> dict:
    """JSON shape for /setup's `pg_bindings`: an OBJECT keyed by binding name,
    each value carrying ONLY the least-disclosure fields the workspace needs to
    build its DSN -- the effective env var, the database name, and the ready-made
    loopback `dsn`. NEVER the real upstream host, username, password, sslmode, or
    sslrootcert (those are the operator's, and the workspace dials proxy.local
    regardless). The `name` is the key, never repeated."""
    return {
        b.name: {"env": b.env, "dbname": b.dbname, "dsn": pg_dsn(b)}
        for b in pg_creds.bindings.values()
    }


def _sh_squote(value: str) -> str:
    """POSIX single-quote `value` so it is safe as a shell word. Single quotes
    disable every expansion (`$`, backtick, `\\`, `"`), and an embedded single
    quote is closed, escaped, and reopened (`'\\''`). Placeholders are
    alnumeric by construction today, but this holds if that ever changes."""
    return "'" + value.replace("'", "'\\''") + "'"


# The env NAME is interpolated UNQUOTED into `export NAME=...`, so it must be a
# shell identifier or the whole script errors in every login shell's eval. The
# CLI rejects non-identifiers at parse (core/injectors.ENV_NAME_RE, mirrored
# here -- the wire may come from a non-CLI pusher). Check with .fullmatch(),
# never .match() + `$`: a `$` anchor still accepts a trailing newline
# ("FOO\n"), the exact line break-out this guard exists to stop.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def pg_dsn(binding) -> str:
    """The workspace-facing DSN for a pg binding: the workspace connects to the
    broker at proxy.local:PG_CLIENT_PORT as the binding NAME (the selector), and
    the broker re-originates to the real DB. The client leg is plain loopback
    (sslmode=disable on THIS hop) -- the broker's server-leg TLS is independent.
    Name and dbname are percent-encoded so an odd name still yields a valid URI
    that round-trips back to the exact selector."""
    user = quote(binding.name, safe="")
    db = quote(binding.dbname, safe="")
    return f"postgresql://{user}@proxy.local:{PG_CLIENT_PORT}/{db}?sslmode=disable"


def exports_body(creds: Credentials, pg_creds: PgCredentials | None = None) -> str:
    """Body of /exports.sh: one `export ENV=<value>` line per binding that has an
    effective env var name -- the inert placeholder for an HTTP binding (skipping
    sign-family/no-placeholder bindings), the loopback DSN for a pg binding. A
    non-identifier env is skipped with an observable comment (env names are
    already disclosed via /setup) rather than breaking the whole script. Reads
    the LIVE config each call. Nothing to export -> a valid empty script."""
    lines = []
    for b in creds.inward_bindings():
        if not b.env or b.placeholder is None:
            continue
        if not _ENV_NAME_RE.fullmatch(b.env):
            # A comment runs to end of line; scrub CR/LF from the (wire-supplied)
            # name so it cannot escape the comment onto a live line.
            name = b.name.replace("\r", " ").replace("\n", " ")
            lines.append(f"# skipped '{name}': env is not a shell identifier")
            continue
        lines.append(f"export {b.env}={_sh_squote(b.placeholder)}")
    for pb in (pg_creds.bindings.values() if pg_creds else ()):
        if not pb.env:
            continue
        if not _ENV_NAME_RE.fullmatch(pb.env):
            name = pb.name.replace("\r", " ").replace("\n", " ")
            lines.append(f"# skipped pg '{name}': env is not a shell identifier")
            continue
        lines.append(f"export {pb.env}={_sh_squote(pg_dsn(pb))}")
    if not lines:
        return "# no credproxy exports\n"
    return "\n".join(lines) + "\n"


def _json(obj, status: int = 200) -> web.Response:
    """JSON response, pretty-printed with a trailing newline so a bare `curl`
    of a bootstrap route reads cleanly. Insertion order is preserved (no key
    sorting). `jq` and parsers are unaffected by the whitespace."""
    return web.json_response(
        obj, status=status, dumps=lambda o: json.dumps(o, indent=2) + "\n")


async def _listener_bound(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if the mitmproxy transparent listener is accepting connections. A
    live connect+close observes the actual bind state each call -- no addon flag
    to go stale if the server dies, no reliance on mitmproxy internal hook order.
    A failed connect (refused, during boot) is cheap and never reaches mitmproxy;
    a success is one benign empty connection (the probe closes immediately)."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def _capture_pending() -> list[str]:
    """What's still missing for the proxy to be *capture-ready* (not merely
    alive), most-fundamental first. Empty list == ready.

    - `mitmproxy-listener`: the transparent listener isn't accepting yet. The HTTP
      listener answering `/health` proves only its OWN bind, not mitmproxy's, so
      we probe the mitmproxy port directly.
    - `ca-cert`: mitmproxy hasn't written its CA yet, so the first TLS connection
      to an intercepted host would fail with a cert error even though the listener
      is up. (Cheap belt-and-suspenders -- today the CA is written at DumpMaster
      construction, before the listener binds, but this guards an odd confdir.)

    iptables is NOT probed: the entrypoint installs the rules under `set -e`
    before it execs python, so any answer at all implies they're in; and python
    then runs as an unprivileged uid that couldn't read the nat table anyway."""
    pending = []
    if not await _listener_bound(PROXY_HOST, PROXY_PORT):
        pending.append("mitmproxy-listener")
    # The pg broker is a second always-on listener (started unconditionally in
    # main.run, creds-agnostic like the others). Probe it too so `start`/`--wait`
    # don't hand off a workspace whose pg egress isn't yet being brokered.
    if not await _listener_bound(PROXY_HOST, PG_PORT):
        pending.append("pg-listener")
    if not CA_CERT_PATH.exists():
        pending.append("ca-cert")
    return pending


async def health(_: web.Request) -> web.Response:
    """Capture-readiness probe (not liveness): 200 only once egress is actually
    being intercepted -- the mitmproxy listener accepts connections AND its CA
    exists. 503 with a `pending` list until then, so `start`/`push --wait` hold
    off handing the workspace a proxy that isn't yet capturing traffic (#23).
    Creds-readiness (a config having been pushed) is deliberately a SEPARATE,
    future signal -- folding it in here would deadlock standalone `start`, which
    waits on `/health` and only THEN pushes config."""
    pending = await _capture_pending()
    body = {"ok": not pending, "version": VERSION}
    if pending:
        body["pending"] = pending
        return _json(body, status=503)
    return _json(body)


async def ready(request: web.Request) -> web.Response:
    """Creds-readiness probe: 200 only once the proxy is BOTH capture-ready
    (everything `/health` checks -- transparent listener accepting + CA on disk)
    AND has accepted at least one config push (`config_generation >= 1`).

    This is the signal an attached-workspace integration (Compose/devcontainers/CI)
    gates on before handing work to the sandbox: `/health` says traffic is being
    intercepted, `/ready` additionally says credentials have been pushed.

    Gating on the generation counter -- not "bindings is non-empty" -- is
    deliberate: a RULES-ONLY config (a guardrail proxy with zero bindings) is a
    valid, ready state, and it bumps the generation like any other accepted push.

    Kept strictly separate from `/health` (which must NEVER depend on config, or
    standalone `start` -- which waits on `/health` and only THEN pushes -- would
    deadlock). The body mirrors `/health`'s style and carries `generation` in
    both states."""
    state = request.app[STATE_KEY]
    generation = state.generation
    pending = await _capture_pending()
    if generation < 1:
        pending.append("config")
    body = {"ok": not pending, "version": VERSION, "generation": generation}
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


async def exports_sh(request: web.Request) -> web.Response:
    """Binding placeholder exports for `eval`. Kept separate from /env.sh (the
    CA-trust snapshot) so a power user can source exactly one -- and so this one
    reflects the LIVE loaded config on every request, not a module constant."""
    state = request.app[STATE_KEY]
    return web.Response(body=exports_body(state.creds, state.pg_creds),
                        content_type="text/x-shellscript")


async def setup(request: web.Request) -> web.Response:
    state = request.app[STATE_KEY]
    return _json({
        "version": VERSION,
        "workspace": os.environ.get("CREDPROXY_WORKSPACE") or None,
        # Creds-readiness counter (0 == no config pushed yet), so a workspace-side
        # consumer can poll readiness without hitting an admin route. Discloses no
        # secret -- just a monotonic bookkeeping integer.
        "config_generation": state.generation,
        "ca_url": "http://proxy.local/ca.crt",
        "ca_env": CA_ENV,
        # Least disclosure: hosts referenced ONLY by a hidden rule are withheld
        # here (a hidden tripwire must not be passively enumerable via /setup);
        # the decision path still intercepts them.
        "intercept_hosts": sorted(state.creds.disclosed_intercept_hosts()),
        "bindings": workspace_bindings(state.creds),
        # PostgreSQL broker upstreams (see pg_workspace_bindings): keyed by the
        # binding name, least-disclosure {env, dbname, dsn}. Empty object when no
        # pg bindings are configured.
        "pg_bindings": pg_workspace_bindings(state.pg_creds),
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
        "  GET /ready        creds-readiness (503 until /health + a config push)\n"
        "  GET /ca.crt       proxy CA certificate (PEM)\n"
        "  GET /bootstrap.sh install CA + trust env  (curl -sSL proxy.local/bootstrap.sh | sh)\n"
        "  GET /env.sh       CA-trust env exports\n"
        "  GET /exports.sh   binding placeholder + pg DSN exports (export ENV=...)\n"
        "  GET /setup        bindings + pg_bindings + workspace info (JSON)\n"
        "  GET /llms.txt     guidance for agents\n\n"
        "Admin routes (/admin/*) require a bearer token and are host-only.\n"
    )
    return web.Response(text=body, content_type="text/plain")


bootstrap_routes = [
    web.get("/", index),
    web.get("/health", health),
    web.get("/ready", ready),
    web.get("/ca.crt", ca_crt),
    web.get("/bootstrap.sh", bootstrap_sh),
    web.get("/env.sh", env_sh),
    web.get("/exports.sh", exports_sh),
    web.get("/setup", setup),
    web.get("/llms.txt", llms_txt),
]
