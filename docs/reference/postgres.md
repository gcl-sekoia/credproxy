[← docs index](../README.md) · [Concepts](../concepts.md)

# PostgreSQL — the credential-injecting connection broker

Everything else credproxy does rides HTTP through mitmproxy. PostgreSQL can't:
it's a binary protocol, its TLS is a STARTTLS-style upgrade (so there is no SNI
or any other decision point before the pre-auth plaintext exchange), and its
modern auth is SCRAM-SHA-256 — the password never crosses the wire, so there is
nothing to substitute. So credproxy **brokers** Postgres instead of intercepting
it: a second listener the workspace *explicitly dials* at `proxy.local:5432`.

The workspace connects as the **binding name** with **no password**; the broker
selects the matching `[[postgres]]` upstream, re-originates a fresh connection to
the real database with the real credential, and then becomes a transparent
byte-pump. The real host, username, and password never enter the workspace.

## Why explicit, not transparent

Transparent SNI-based interception is credproxy's rule for HTTP because there's a
decision point (the ClientHello) before any secret moves. Postgres has none — the
SSL negotiation and the startup packet are the *first* bytes, before the client
has proven anything. So the broker can't be a transparent interceptor; it's an
endpoint the workspace dials on purpose. (The general test for a future protocol:
*is there a decision point before the pre-auth plaintext exchange?* If not, it
needs a broker, not interception.)

This is the second deliberate carve-out from "transparent capture of all TCP"
(after dropping HTTP/3 at the firewall). A narrow, destination-scoped iptables
rule redirects **only** `proxy.local:5432` to the broker; real Postgres traffic
to any other host stays untouched passthrough.

## How the handshake works

```
workspace (client leg)                        broker → real DB (server leg)
----------------------                        ----------------------------
StartupMessage user=<binding>   ─┐
                                 │  select the [[postgres]] binding by `user`
                                 │  connect host:port, optional TLS (verify-full)
                                 └► StartupMessage user=<real user>
                                    cleartext / MD5 / SCRAM-SHA-256 auth
AuthenticationOk                <─┘  AuthenticationOk
ParameterStatus* (relayed)      <──  ParameterStatus*
BackendKeyData (pid/secret FAKE)<──  BackendKeyData (real pid/secret)
ReadyForQuery                   <──  ReadyForQuery
   <=============== raw byte-pump both directions ===============>
```

The two legs are asymmetric on purpose. The **client leg** is trust-accept: the
workspace holds only the binding name, exactly the inert-placeholder trust domain
the HTTP side uses. The **server leg** does a real auth handshake with the real
credential and defaults to TLS **verify-full** (`sslmode`) — the broker
originates a credentialed session, so an unverified server leg would be an
active-MITM hole with a bigger blast radius than a wrong password.

Once authenticated the broker just copies bytes, so every Postgres feature works
unchanged — extended query, `COPY`, `LISTEN`/`NOTIFY`, prepared statements. Query
cancellation is handled too: a `CancelRequest` arrives on a fresh connection with
no user field, so the broker fabricates its own per-session `(pid, secret)`,
rewrites the `BackendKeyData` it relays, and re-issues a real cancel upstream when
the fake pair comes back.

## Configuring a pg binding

A `[[postgres]]` block is a named upstream. It carries no placeholder and no
scheme — just where to connect and which provider resolves the credential:

```toml
[[postgres]]
name     = "analytics"
host     = "db.internal"          # the REAL upstream host
port     = 5432                    # optional (default 5432)
dbname   = "warehouse"
sslmode  = "verify-full"           # optional (default; server-leg TLS policy)
provider = "vault"
secret   = { username = "db/analytics#user", password = "db/analytics#pass" }
env      = "DATABASE_URL"          # optional; DSN export target
```

- **`secret`** is always a `{ username, password }` pair resolved from the
  provider (like a multi-slot HTTP binding). The host CLI fetches both at push
  time and sends resolved values to the proxy — the proxy never calls a provider.
- **`sslmode`** is the *server-leg* policy: `disable`, `allow`, `prefer`,
  `require`, `verify-ca`, or `verify-full` (the default). Point `sslrootcert` at a
  private-CA bundle to verify a self-signed upstream.
- **`env`**, if set, is the environment variable the DSN is exported under (via
  `/exports.sh` — see below). Omit it for an upstream you dial by hand.

Names are unique across bindings, rules, **and** pg bindings (the proxy keys
`/setup` by name across all three).

## Using it from the workspace

The workspace dials the broker, not the database. After bootstrapping, a login
shell already has the DSN exported under `env`:

```
$ echo $DATABASE_URL
postgresql://analytics@proxy.local:5432/warehouse?sslmode=disable
$ psql "$DATABASE_URL" -c 'select 1'
```

The DSN's `sslmode=disable` is for the **client leg only** — plain loopback
inside the shared network namespace, where there is no eavesdropper. The broker's
connection to the real database uses the `[[postgres]]` block's `sslmode`,
independently. You authenticate as the binding name with no password.

`curl -s http://proxy.local/setup | jq '.pg_bindings'` lists the pg upstreams
available (keyed by name), each with its `env`, `dbname`, and ready-made `dsn` —
never the real host, user, or password.

## CLI

```
credproxy workspace NAME postgres add --provider PROV --secret username=REF --secret password=REF --host HOST --dbname DB [--port N] [--sslmode MODE] [--sslrootcert PATH] [--name NAME] [--env VAR]
credproxy workspace NAME postgres remove NAME
credproxy workspace NAME postgres list
credproxy workspace NAME postgres test NAME
```

`postgres add` appends a `[[postgres]]` block (auto-named `pg-<dbname>` unless you
pass `--name`) and validates the whole set before writing. On the loose surface
the sub-noun resolves the default workspace: `credp postgres add …`,
`credp postgres list`, `credp postgres remove NAME`.

`postgres test` is a resolve-only dry run: it fetches each binding's username and
password from the provider and reports the value lengths (never the values), so
you can confirm the provider path works before starting. A full server-leg
handshake to the real database is `credproxy doctor NAME --fetch`'s territory (it
resolves the credential); the broker itself validates the live connection on the
first real client.

## What the proxy sees, logs, and discloses

Audit events (`credproxy workspace NAME logs --audit`) record each brokered
connection and every auth outcome as structural facts only — binding name, host,
database — **never** the username or password. An upstream auth failure is
sanitized to the workspace (a generic "authentication to upstream database
failed"); the real SQLSTATE and message — which can name the real user — go only
to the audit log.

## `dbname` is a default, not an access boundary

The broker connects the server leg to the database the **client** asks for,
falling back to the binding's `dbname` only when the client names none
(`postgresql://analytics@proxy.local:5432/OTHER` reaches `OTHER`, not
`warehouse`). This matches how a normal Postgres connection works — the injected
credential's `GRANT`s are the real boundary, exactly as they would be for a
direct connection. So `dbname` scopes the *default*, not *what the role can
reach*: if you provision one broadly-privileged role per server instance, a
workspace can reach every database that role is granted. To confine a binding to
a single database, scope the underlying **role's grants** (or give it a
database-specific login), not the `dbname` field.

## Limits

- IPv4 only, like the rest of credproxy.
- The stateless `credproxy push --config` escape hatch carries only
  `[[binding]]`/`[[rule]]`, not `[[postgres]]` — use a workspace for pg bindings.
- One credential shape: a `{ username, password }` pair from a provider. A
  fixed/literal username is not yet expressible.
- Auth methods: cleartext, MD5, and SCRAM-SHA-256. Channel-binding
  (`SCRAM-SHA-256-PLUS`) is **not** supported — if an upstream offers *only* the
  `-PLUS` variant the broker fails closed (a clear "no supported SASL mechanism"
  error), never silently downgrading.
- `verify-full` (the default `sslmode`) needs the server's CA reachable **inside
  the proxy container** via `sslrootcert`. credproxy has no first-class way to
  mount an operator CA into the proxy container yet, so for a private-CA database
  (e.g. a managed cloud DB) either use `require` (encrypted, unverified) or bake
  the CA into a custom proxy image. `doctor` cannot verify container-side
  reachability of `sslrootcert` — it only reminds you it must exist there.
