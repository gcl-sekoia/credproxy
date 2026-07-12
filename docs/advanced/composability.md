[← docs index](../README.md) · [Concepts](../concepts.md)

# Composability — attached workspaces

An **attached** workspace is a first-class credproxy workspace whose *containers*
are run by someone else — a Docker Compose file, a devcontainer, a CI job, a
hand-run `docker run`. credproxy owns only that workspace's **credentials**: it
resolves each binding's secret host-side and pushes the wire config to a proxy
it did not create. Everything else about a workspace — its name, token, state
dir, drift files, cwd-addressing, bindings and rules — is unchanged. There is no
second identity system; an attached workspace is just a workspace that declares
an `attach` table instead of container fields.

This builds on credproxy's existing decisions — the push model
([`providers.md`](../reference/providers.md)), the binding/scheme model
([`injectors.md`](../reference/injectors.md)), the host-owned bearer token, the
ephemeral-port-resolved-at-call-time rule — and nothing here weakens the threat
model in `CLAUDE.md`.

## Why — unbundling `start`

`credproxy workspace NAME start` bundles three separable concerns:

1. **proxy** — the proxy container + netns + iptables (egress capture).
2. **config** — resolve each binding's secret(s) host-side and push the wire
   config to `/admin/config`.
3. **workspace** — the workspace container lifecycle (create, spec-drift,
   mounts, `setup`).

When credproxy owns all three, the bundle is convenient. To **compose** with a
manager that already owns (3) — and usually (1), e.g. a Compose file that starts
the proxy as a sibling service — credproxy must be able to do (2) against objects
it did not create. Previously (2) existed only as an implicit step inside
`start`/`apply`. Attached workspaces make it a first-class verb (`push`) and let
it target a foreign proxy, while `start`/`apply` keep calling the **same** engine
— an extraction, not a parallel path.

## The attach model

A workspace TOML declares an `attach` table in place of the container fields:

```toml
# workspaces/myproject.toml — attached: credproxy manages credentials, not containers
attach = { compose_project = "myproject" }
directory = "/home/me/src/myproject"      # optional; ordinary cwd-addressing

[[binding]]
injector = "bearer"
provider = "op"
secret   = "op://dev/github/token"
hosts    = ["api.github.com"]

[[rule]]                                  # rules ride every push, exactly as for a managed workspace
action = "block"
hosts  = ["api.github.com"]
method = ["DELETE"]
path   = "/repos/**"
```

- **Identity is the workspace name, as everywhere.** The token
  (`state/workspaces/<name>/auth.token`), state dir, the lock's `applied`
  bindings/rules drift metadata, the `directory` cwd resolver, and the loose
  default pointer are all the existing machinery, untouched.
- `attach` is **mutually exclusive** with every container-lifecycle key
  (`image`, `home`, `mounts`, `env`, `setup`, `user`, `user_uid`,
  `map_host_user`, `run_flags`, `shell`, `workdir`, `enter_prelude`,
  `exec_flags`, `auto_stop`). `load_config` rejects the mix, naming the offending
  keys. `directory` and `[[binding]]`/`[[rule]]` stay valid.
- Exactly **one** selector, validated at load:

  | selector | resolves to |
  |---|---|
  | `admin_url = "http://127.0.0.1:PORT"` | that URL verbatim (must be **loopback** — I8). No discovery. |
  | `container = "NAME_OR_ID"` | `docker port <container> <CREDPROXY_HTTP_PORT>` → `http://127.0.0.1:P` |
  | `discover = "k=v,k=v"` | the single running container matching **all** labels → the same port derivation |
  | `compose_project = "P"` | sugar for `discover = "com.docker.compose.project=P,com.docker.compose.service=proxy"` — the one Compose-aware bit, clearly delimited; normalized to `discover` at load |

  `discover` matching zero or more-than-one container is an error (ambiguity is
  never silently resolved), and the published port is resolved **at every
  invocation**, never cached (I6).

### Verb behavior on an attached workspace

| verb | behavior |
|---|---|
| `push` | resolve + POST the full wire config (see below) |
| `apply` | **≡ `push`** (there is no container spec to reconcile) |
| `binding` / `rule` add/remove/list/test | work unchanged |
| `rule test --live` | works — the proxy is reachable via the attach target |
| `inspect` | shows the attach selector + the resolved target + binding/rule drift |
| `doctor` | validates the `attach` block + bindings/rules + token; **no** container checks |
| `delete` | removes **config + state only** — never touches the foreign containers or volumes (the loose-surface confirmation gate still applies) |
| `resolve` | works — build the wire config without contacting any proxy |
| `start` / `stop` / `recreate` / `enter` / `exec` / `logs` / `mount` / `dev reload` | **refuse** with a one-line error naming the attach model and pointing at `push` |

Follow-up hints are attach-aware: `create --attach`, `binding add`, and
`rule add` on an attached workspace point at `credproxy workspace NAME push`,
never the gated `start`.

### Scaffolding an attached workspace

```
credproxy workspace create NAME --attach SELECTOR
```

`SELECTOR` is `compose-project=P` | `container=X` | `admin-url=URL` |
`discover=k=v[,k=v]`. It stamps from a **new** singleton template,
`workspace.attach.template.toml`, which rides `resolve_singleton()` — so a user
(`$XDG_CONFIG_HOME/credproxy/workspace.attach.template.toml`) or an overlay can
customize it exactly like the managed `workspace.template.toml`
([`overlays.md`](overlays.md)). Plain `create` keeps stamping the managed
template. The token is created as usual (it authenticates the push); `--here` /
`--dir` still record a `directory` association.

## Two readiness signals

Egress-capture readiness and credential readiness have different owners and
lifetimes, so they are **two endpoints, never one** (invariant I2):

| endpoint | 200 when | consumed by |
|---|---|---|
| `/health` | **capture-ready** (creds-agnostic): the transparent mitmproxy listener accepts connections **and** the CA exists | `start`, `push --wait`, any liveness probe |
| `/ready` | `/health` is green **and** `config_generation ≥ 1` (a config has been pushed) | an external health-gate (Compose `depends_on: service_healthy`) |

`POST /admin/config` bumps a **generation counter** on every *accepted* push (a
validation failure never touches it); `/ready`'s body carries
`{"generation": N}`, and `/setup` exposes the same `config_generation` so a
consumer can poll readiness *from inside* the workspace without any admin-route
access (least-disclosure unchanged — it is a monotonic integer, no secret).

Two properties of that counter are load-bearing and deliberate:

- **Rules-only configs go ready.** Gating on the generation counter — not "the
  bindings list is non-empty" — means a pure guardrail proxy (rules, zero
  bindings) is a valid ready state, and its push bumps the generation like any
  other.
- **Generation survives a reload, resets on stop/start.** It is persisted inside
  the tmpfs config envelope, so `credproxy dev reload` (a SIGHUP in-place
  re-exec, which keeps the tmpfs) leaves `/ready` green. A `docker stop`/`start`
  clears the tmpfs → generation 0 → `/ready` red until the next push — which is
  **correct**, mirroring the fact that `start` itself always re-pushes after a
  restart. Do not "fix" it.

`/health` is deliberately kept independent of config: credproxy's own `start`
waits on `/health` and *then* pushes, so a creds-gated `/health` would deadlock
standalone `start` the same way waiting on `/ready` before pushing would (I1).

**`GET /admin/config` (bearer-gated) reports what the proxy is running.** The same
route that accepts a `POST` push answers a `GET` with a superset used two ways:
`{"loaded", "fingerprint"}` (read from the tmpfs config file) power the `enter`
fast path — the host skips a redundant re-push when the proxy already holds the
intended config's fingerprint — and `{"generation", "bindings", "rules"}` (built
from the **loaded** config objects, not a tmpfs re-read) report what the proxy is
*actually* running, so `inspect`/`apply`/`doctor` can drift the resolved intent
against reality (see [Drift against reality](../reference/configuration.md#drift-against-reality)).
The **generation** is the reality discriminator the verdict keys on; the
binding/rule projection is **sanitized** and used for **display only** —
deliberately tighter than `/setup`: `{name, hosts, scheme, placeholder, env}` per
binding and `{name, hosts, action, visible}` per rule, **never** a secret value, a
`params` value, or a header/body value (so it is lossy — a changed secret ref or
rule detail is invisible in it, which is why the verdict reads the content-complete
offline drift, not this projection). Since this route is loopback + bearer-gated and the projection carries no
secret, an attached workspace's `inspect`/`apply`/`doctor` reads it over the same
resolved `attach` admin URL that `push` posts to.

## Commands

All these live on the **strict** surface (the scriptable contract); the loose
`credp` aliases resolve the implicit workspace via the existing cwd/default
resolver — no new resolution logic.

### `credproxy workspace NAME push [--wait] [--timeout SECS]`

The first-class extraction of step (2): resolve every binding's refs (batched
per provider exactly as `start` does — [`providers.md`](../reference/providers.md)) and POST
the **full wire config, bindings *and* rules**, to the workspace's proxy — the
managed proxy for a managed workspace, the resolved attach target for an
attached one. `start`/`apply` call this same engine internally.

- **Atomic, fail-closed** — if any ref fails to resolve, nothing is sent and the
  command exits nonzero naming the binding (I3). Under an external health-gate
  this surfaces as the proxy staying un-`/ready`, so the bring-up fails closed.
- **`--wait`** polls **`/health`** (never `/ready` — I1) until capture-ready,
  then pushes; `--timeout` (default 120s) bounds it, and the poll interval is
  ~0.5s (a provider may prompt/unlock, so the default is generous).
- **Lock** — a blocking `flock` on the per-workspace `<state>/lifecycle.lock`
  (the same reentrant lock that serializes `start`/`apply`/`recreate`) is held
  around the resolve+POST. A second concurrent `push` of the same workspace
  **waits for the holder, then re-pushes** (the config may have changed
  underneath) — collapse never means skip. Foreground by default; the *caller*
  backgrounds it with `&` if its orchestration needs to (I5).
- **Managed proxy stopped** — `push` on a managed workspace whose proxy is not
  running is a clean error pointing at `start` (it does not auto-start; that is
  `start`'s job — G4).

The pushed drift metadata (the lock's `applied.bindings` / `applied.rules`)
updates on an attached push exactly as on a managed one, so `inspect` drift works
identically (G5).

### `credproxy push --admin URL --config FILE --token FILE [--wait] [--timeout SECS]`

The **stateless** escape hatch — top-level, no workspace file, no state — for CI
one-offs and hand-run proxies. `--config` is a workspace-TOML **subset**:
`[[binding]]` / `[[rule]]` only (any container or `attach` key is rejected,
naming it), validated by the same binding/rule validators the workspace path
uses, so a stateless push and a managed one agree on what a valid config is.
`--admin` must be loopback (I8). The lock is keyed by a hash of the target under
`$XDG_STATE_HOME/credproxy/locks/`, same wait-then-repush semantics. This is the
10% path; attached workspaces are the primary story.

### `credproxy workspace NAME resolve (--json | --out FILE)`

Step (2) **without the POST** — the deliver-at-creation channel. Exactly one of:

- **`--json`** — write the wire config (with resolved secret **values**) to
  stdout. Host-transient: capture it into an env-sourced Compose secret so the
  secret lives only in the bring-up process env and the proxy's tmpfs.
- **`--out FILE`** — the at-rest variant, written **mode 0600**. A path outside
  the workspace state dir gets a stderr warning naming the risk (it holds real
  secrets; never write it into a repo / `.devcontainer`, where it can be
  committed). Trade-off: config is **baked at creation** — no live
  `apply`/rotation; a binding change means re-deliver. Prefer `push`.

`resolve` generalises the ad-hoc `binding test --provider … --secret …`
(dry-run a single definition) to "resolve a whole binding set to wire form."

### `credproxy emit-compose [NAME] [--image TAG]`

The **one deliberately Compose-aware** command (everything else is
parent-agnostic). It prints a Docker Compose fragment to stdout: the proxy
service (NET_ADMIN, a mode-1777 tmpfs for the pushed config, the read-only token
bind, an ephemeral loopback admin port) plus the two lines a workspace service
needs (`network_mode: "service:proxy"` and `depends_on: { proxy: { condition:
service_healthy } }`). Every port and mount-target path comes from the proxy
image's `ENV` contract via `ImageEnv.load` — no hand-maintained constants, so a
`dev build` that bumps the image can't leave a stale literal. `--image TAG`
overrides the image (inspected **and** baked into the emitted `image:` line).

- The healthcheck probes **`/ready`**, not `/health`: a Compose `service_healthy`
  gate opens only once credentials are pushed. The proxy image ships python but
  no curl/wget, so the probe is a `python -c` urllib one-liner that exits nonzero
  on any non-200.
- With **NAME**, the workspace's real token path (`<state>/auth.token`) is baked
  into the bind mount (the workspace must exist — this pairs with
  `create --attach`). Without **NAME**, the token source is
  `${CREDPROXY_STATE:?…}/auth.token` for a Compose `.env` to interpolate, with a
  comment on where that dir lives (`$XDG_STATE_HOME/credproxy/workspaces/NAME`;
  `credproxy workspace NAME inspect` shows it).
- `--json` is refused (it emits YAML). It is pure convenience — you can always
  hand-write the service.

## Integration patterns

How the pieces combine. The first is canonical; the rest are alternatives with
different trade-offs.

### A. Compose health-gated sidecar (recommended)

The proxy is a Compose service; the workspace shares its netns and gates on it.
`credproxy emit-compose myproject` scaffolds exactly this.

1. `credproxy workspace create myproject --attach compose-project=myproject`,
   then add bindings/rules (`binding add`, `rule add`, `preset add`).
2. Bring up the **proxy** service (its token bind-mounts `<state>/auth.token`;
   `emit-compose` wires the path). Its healthcheck probes **`/ready`**, so it
   reports healthy only once credentials land.
3. `credproxy workspace myproject push --wait` — waits for `/health`
   (capture-ready), then resolves secrets and pushes; the proxy flips to
   `/ready`, so its healthcheck goes green.
4. The workspace service `depends_on: { proxy: { condition: service_healthy } }`
   — so it does not start until the proxy is **captured *and* credentialed**.

`docker compose up` (or `devcontainer up`) returning thus means "ready." The
only integration-owned policy is *when* to run the `push` (e.g. a pre-up hook
that backgrounds `push --wait &`); every mechanic — discover, wait-for-listen,
resolve, lock, POST — is credproxy's. A worked target is
[claude-code-devcontainer](https://github.com/trailofbits/claude-code-devcontainer)
in Compose mode (proxy service + a `network_mode: "service:proxy"` workspace).

### B. Deliver-at-creation (file or env secret)

Skip the push: `credproxy workspace NAME resolve` produces the wire blob and the
manager hands it to the proxy at startup.

- **env-sourced** (host-transient):
  `export CREDPROXY_CONFIG="$(credproxy workspace NAME resolve --json)"` then a
  Compose `environment`-sourced secret — no host file; the secret lives only in
  the bring-up process env and the proxy tmpfs. Honest caveat (I4): that env is
  visible in `docker inspect` — within the threat model (same-user is out of
  scope), but say it.
- **file-sourced** (at-rest):
  `credproxy workspace NAME resolve --out <state>/config.json` then a Compose
  `file:` secret / bind mount.

Trade-off: config is **baked at creation** — no live `apply`/rotation; a binding
change means re-deliver. Prefer A unless a no-push flow is required (see I4). The
file variant is the only one that works with **no wrapper at all** (a pre-create
hook can write a file but cannot export env), at the cost of an at-rest secret.

### C. Generic / hand-run / CI

Proxy started however (a bare `docker run`, a CI service container):

```
credproxy push --admin http://127.0.0.1:$PORT --config ./creds.toml --token ./tok --wait
```

`creds.toml` is a `[[binding]]`/`[[rule]]` subset (build one with `resolve`).

### D. Daemon (future — out of scope)

A host process watching `docker events` for a proxy container appearing and
running `push --wait` against it automatically — removing the manual/`&` push
from pattern A. It **reuses `push --wait` verbatim**; the commands above are
designed to be daemon-drivable from day one. It is strictly constrained by **I9**
(below): it matches *discovered containers* against *host-registered workspaces*,
never the other way around.

## Invariants — do not reverse

- **I1** — `--wait` polls `/health`, never `/ready`. `/ready` is gated on the
  very push that `--wait` precedes; waiting on it deadlocks
  (`push → ready → creds → push`). "Wait for the proxy" means wait for the
  **listener to accept**, not for the health-gate to open.
- **I2** — two readiness signals, never collapsed: `/health` = capture-ready,
  `/ready` = creds-ready. A creds-gated `/health` deadlocks credproxy's own
  `start`, which waits on `/health` *then* pushes.
- **I3** — push is atomic and fail-closed; a partial config is never sent; exit
  nonzero naming the failure. Under an external health-gate this surfaces as the
  proxy staying unhealthy — the bring-up fails closed.
- **I4** — secrets posture transient by default (`push`, `resolve --json`: RAM +
  authenticated transit + the proxy tmpfs). `resolve --out FILE` is the at-rest
  escape hatch: **0600**, under the state dir, **never in the repo /
  `.devcontainer`**, session-lived. The env-sourced Compose variant is visible
  in `docker inspect` — within the threat model (same-user out of scope), but say
  it.
- **I5** — parent-agnostic boundary: credproxy provides **mechanics** (discover,
  wait-for-listen, resolve, lock, push); the integration owns **orchestration
  policy** (when to invoke, whether to background, lifecycle). `compose_project`
  and `emit-compose` are the only Compose-aware sugar and are clearly delimited.
- **I6** — the published port is resolved at call time
  (`container`/`discover`/`compose_project`), never persisted.
- **I7** — CA trust is **pulled, not pushed**: credproxy exposes its CA over the
  open bootstrap route (`proxy.local`); the consumer's own `setup`/`postCreate`
  installs it. credproxy never reaches into a foreign container's trust store —
  that would couple it to every image's CA layout.
- **I8** — `admin_url` (and `--admin`) must resolve to a **loopback** address
  (`127.0.0.0/8` or `localhost`); anything else is refused, at config load and at
  dispatch. The push carries resolved secret values bearer-authed over **plain
  HTTP** (there is no TLS on the admin API); that is safe precisely because the
  admin port only ever exists on loopback — via a published ephemeral port or the
  shared netns. Enforce the assumption.
- **I9** — **registration is host-side only.** Container-supplied data (labels,
  env, names) must never *select* a host-side secret source. The host-declared
  `attach` selector matches containers; the arrow never points the other way.
  This kills, in advance, the convenient-but-wrong daemon design where a
  container label names the binding set — a rogue container wearing a crafted
  label would get real secrets resolved and pushed to itself. Any future
  `push --watch`/daemon matches discovered containers against host-registered
  workspaces, full stop.

## Open questions

- **enter / exec / logs via the attach selector.** Good UX later, but it needs
  the selector to also identify the *workspace* container (a second selector?),
  so it is deliberately not in this cut — hence those verbs refuse on an attached
  workspace.
- **`resolve --out` placement enforcement.** Currently warn-not-refuse outside
  the state dir; tighten to refuse-with-`--force` if the warning proves
  ignorable.
- **Daemon mapping / auth.** Constrained by I9 (host-registered workspaces only);
  the rest is deferred with the daemon itself.
