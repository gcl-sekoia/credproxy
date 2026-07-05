# Security

[← docs index](README.md) · [Concepts](concepts.md)

This page is the honest threat model. It says what credproxy protects against,
what it does not, and where the sharp edges are. credproxy is a **developer
convenience boundary, not a hardened jail** — and the value it does provide only
holds if you know the shape of it. Honest beats reassuring.

## What credproxy protects

The one property everything else rests on:

> [!IMPORTANT]
> **The real credential never enters the workspace container.** The workspace
> holds only [placeholders](concepts.md#placeholder) — fake tokens of the right
> shape. The host fetches each real secret and pushes it into the
> [proxy](concepts.md#proxy); the proxy swaps it in on the way out. A tool that
> reads every environment variable, dumps the filesystem, or exfiltrates the
> whole container leaks placeholders, not secrets.

This is what makes it safe to run an untrusted or semi-trusted tool — an LLM
agent, a scraped install script, a CI job — against your real credentials. The
tool gets to *use* the credential on the hosts you approved, without ever
*holding* it. See [How it works](how-it-works.md) for the push model in detail.

A second, smaller property follows from the same design: because you name each
host a credential may be used on, a leaked placeholder is useless anywhere else.
The proxy only substitutes the real value on requests to a
[binding's](concepts.md#binding) hosts.

## What credproxy does NOT protect against

Be equally clear about the limits.

> [!IMPORTANT]
> **credproxy does not contain an adversarial workspace.** It is not a sandbox
> escape barrier. A determined, malicious process inside the workspace has many
> avenues credproxy makes no attempt to close, and defeating it is explicitly out
> of scope. The boundary is meant to stop *accidental* and *casual* credential
> exposure, not a dedicated attacker who controls the workspace.

Two consequences worth stating plainly:

- **No egress allowlisting.** credproxy is not a firewall. Every host you do
  *not* name passes straight through, untouched and unblockable. [Rules](concepts.md#rule)
  govern HTTP only on hosts you name; there is no deny-by-default, and the host
  patterns deliberately cannot express "block everything except X" (a bare `*`
  or `*.com` is rejected). If you need network egress control, that is a
  different tool's job.
- **A used credential can still be misused within its scope.** Once the proxy
  injects a real token into an approved request, the workspace's tool is talking
  to the real service with real authority on that host. Rules can narrow the
  blast radius (block `DELETE`, block a sub-path), but within what you allow, the
  credential works.

## Who can push configuration

The proxy's admin API is what turns placeholders into real secrets, so who can
reach it matters.

> [!IMPORTANT]
> Pushing configuration requires a **bearer token that only the host owner can
> read**. The token lives on the host filesystem, outside the workspace, and is
> bind-mounted into the proxy read-only. The workspace can reach the admin API
> over the shared network but gets `401` without the token — and there is no
> window in which that endpoint is unauthenticated.

Because the pushed configuration carries **resolved secret values over plain
HTTP** (there is no TLS on the admin API), the admin API is reachable only over
the host's loopback network. A remote proxy is out of the model by design; do
not try to point credproxy at one.

## The browser-on-host story

A web page you visit on the host runs in your browser, on the same machine as the
proxy's loopback admin port. credproxy blocks it two ways, both before any
handler runs:

- **Private Network Access.** credproxy never sends the
  `Access-Control-Allow-Private-Network` header, so Chrome's Private Network
  Access rules stop a public web page from reaching the loopback admin API.
- **Fetch metadata.** A middleware rejects any request whose `Sec-Fetch-Site`
  marks it as cross-site or same-site — i.e. anything a browser initiated.

## The multi-user-host caveat

This is the honest sharp edge.

> [!WARNING]
> On a shared host, **another user account on the same machine can read the
> proxy's auth token** and forge admin requests. The token file is world-readable
> by design (so the in-container user can read it through the bind mount). This
> is a documented limitation: credproxy targets a single-user developer
> workstation.

The damage ceiling is bounded, though. Another host user can cause denial of
service or replace your pushed configuration — but they **cannot read your
secrets that way**. The real credentials live in your providers (1Password, the
Keychain, your `gh` login) and only ever enter the proxy through a
bearer-authenticated push that the host owner initiates. A forged push can
disrupt; it does not exfiltrate.

The same-user case is simpler and out of scope: a malicious process running as
*you* already has your SSH keys, your environment, and your provider sessions.
credproxy adds nothing to defend against that, and does not pretend to.

## In one paragraph

credproxy keeps real credentials out of a workspace you do not fully trust, and
lets that workspace use them only on hosts you named. It is not a firewall, not a
sandbox, and not multi-user-hardened. Use it to raise the floor — to make casual
credential leakage structurally hard — not as the only thing standing between a
determined attacker and your secrets.

---

**Next:** [Troubleshooting](troubleshooting.md), or back to the
[documentation index](README.md).
