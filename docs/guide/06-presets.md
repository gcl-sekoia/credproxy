← [05 · Secret managers](05-secret-managers.md) · [index](../README.md) · [07 · Rules](07-rules.md) →

# 06 · Presets

One credential often needs to work across several hosts of the same service, and
each host may want it in a different form. A GitHub token is a `bearer` token to
`api.github.com` but HTTP `basic` auth to `github.com` and `ghcr.io`. Wiring
that by hand is three bindings. A [preset](../concepts.md#preset) does it in one
command.

## See the expansion first

`credproxy preset list` shows every preset and exactly what it expands to, so
you know before you apply:

```console
$ credproxy preset list
Service setup packs (bindings + guardrails). Apply with:
  credproxy workspace NAME preset add NAME [--provider P --secret REF]

github  (3 bindings, 0 rules)  [builtin]
  binding github-api     bearer  api.github.com  env GITHUB_TOKEN
  binding github-git     basic   github.com
  binding github-ghcr    basic   ghcr.io
```

The `github` preset makes three bindings from one token: `bearer` on the API
host, `basic` on the two git/registry hosts.

## Apply it

The `github` preset defaults its provider to `gh-cli`, which reads your existing
`gh` login. So if you have run `gh auth login`, the whole thing is flagless:

```console
$ credp preset add github
applied preset 'github' to workspace 'myproject': 3 binding(s), 0 rule(s)
  binding github-api       bearer  api.github.com
  binding github-git       basic   github.com
  binding github-ghcr      basic   ghcr.io
newly intercepted (TLS-terminated) host(s): api.github.com, github.com, ghcr.io — a workspace that hasn't bootstrapped the CA will see a cert error there until it does
run `credproxy workspace myproject start` (or `apply`) to push it to the proxy
```

*(Sample output; needs a `gh` login.)* To read the token from somewhere else,
pass `--provider` and `--secret` just like a binding:

```sh
credp preset add github --provider op --secret 'op://Private/GitHub/token'
```

## What the reference expands to

A preset is a durable **reference**: `preset add` appends a small `[[preset]]`
block to your config file, and credproxy expands it — into ordinary bindings,
rules, and container config — every time it resolves the workspace. The proxy
never hears the word "preset"; it only ever sees the expanded bindings. Look in
the TOML and you will find just the reference:

```toml
[[preset]]
name     = "github"
provider = "gh-cli"
secret   = "github.com"
```

That reference expands to three plain bindings sharing one placeholder — a
GitHub PAT is `bearer` on `api.github.com` but HTTP `basic` on `github.com` /
`ghcr.io`. They all carry the same placeholder, so a single fake token works
everywhere and any client-side token-shape check still passes. The full
expansion (names, hosts, the shared placeholder) is snapshotted in the workspace
lockfile (`lock.json`), never written into your hand-owned TOML. `credp binding
list` shows the expanded bindings like any other; to change them, edit the
`[[preset]]` block's own inputs (below) — you don't hand-edit the expansion.


## Multi-slot credentials

Some injectors take more than one secret. AWS `sigv4` signs with an
`access_key_id` **and** a `secret_access_key`; OVH's signer takes three. A pack's
parts all share one credential, so `preset add --secret` mirrors `binding add`
exactly — repeat `--secret SLOT=REF`, once per slot:

```sh
credp preset add aws --provider env \
    --secret access_key_id=AWS_ACCESS_KEY_ID \
    --secret secret_access_key=AWS_SECRET_ACCESS_KEY
```

The reference records the slot map as a table:

```toml
[[preset]]
name     = "aws"
provider = "env"
secret   = { access_key_id = "AWS_ACCESS_KEY_ID", secret_access_key = "AWS_SECRET_ACCESS_KEY" }
```

Because every part of a pack shares that one credential, **every part's injector
must declare the same slots**. A `--secret` slot set that doesn't match the
injectors' — or a pack whose parts disagree on their slots — fails the whole
`preset add` before anything is written. Multi-slot packs have no `--secret`
default and aren't prompted (on the loose surface): always pass the explicit
`--secret SLOT=REF` flags.

## Presets can carry rules too

A preset is a whole **service setup pack**, not just credentials. It may also
ship credential-free [rules](../concepts.md#rule) — guardrails like "block
`DELETE` on this host" — and a preset can be rules only, with no credential at
all. Rules are the next chapter.

## Presets can carry the container half too

A service often needs more than a token: a setup script, an env var, a mounted
file. A preset can ship those as well — `[[mount]]`, `[env]`, and `[[setup]]`
sections that the resolver merges into the container-side of your effective
workspace config, right alongside the bindings. A pack can be *only* container config, with no
credential at all. A `github-auth` pack might look like this:

```toml
[placeholder]
prefix = "ghp_"
length = 40
charset = "alnumeric"

[[part]]
suffix   = "api"
injector = "bearer"
hosts    = ["api.github.com"]
env      = "GITHUB_TOKEN"

[[mount]]
overlay = "setup.d/github-auth.sh"   # a file the pack ships (see Overlays)
target  = "/opt/github-auth.sh"

[[setup]]
run   = "bash /opt/github-auth.sh"
order = 45                            # `order` is required in a pack
```

Applying it references the pack; the resolver merges the mount and the setup step
into your effective container config, alongside the bindings:

```console
$ credp preset add github-auth
applied preset 'github-auth' to workspace 'myproject': 1 binding(s), 0 rule(s), 1 mount(s), 1 setup step(s)
  binding github-auth-api    bearer  api.github.com
  mount   overlay user:setup.d/github-auth.sh -> /opt/github-auth.sh
  setup   [45] bash /opt/github-auth.sh
```

The container half (`mounts`/`env`/`setup`) changes the workspace spec, so if
the container already exists credproxy tells you to `start` again to apply it.
A second `preset add` of the same pack is refused (a `[[preset]]` block already
names it) rather than duplicated. An
[attached workspace](../advanced/composability.md) — whose container credproxy
does not manage — refuses a container-half pack; binding/rule-only packs still
apply there.

Files a pack ships (that `setup.d/github-auth.sh`) live in the same layered
registry as the pack itself and are resolved from the pack's own tier →
[Overlays](../advanced/overlays.md).

## When a pack's definition changes

The reference is a **snapshot**: the expansion recorded in the lockfile is pinned
at the moment you added the pack, so a definition that changes upstream (a new
host, an added rule, a dropped part) is **inert** until you ask for it —
credproxy surfaces a note that the definition drifted but keeps serving the
snapshot.

Re-expanding a snapshot on your own clock is `preset refresh`:

```console
$ credp preset refresh github --check      # preview; writes nothing
preset 'github': 1 added
  binding github-ghcr  added
$ credp preset refresh github              # apply: re-snapshot the expansion
preset 'github': 1 added
  binding github-ghcr  added
```

`preset refresh` force-re-expands the reference against the **current** pack
definition and structurally diffs the new expansion against the locked one
(per entry: `added` / `removed` / `changed`, with a field-level diff for a change).
A dropped definition part simply becomes a `removed` entry — there is no `--prune`
flag. `--check` prints the same diff **without writing** (a preview, and the
CI-friendly "is anything stale?" probe; it exits 0 whether or not anything
changed). Omitting the pack name refreshes **every** `[[preset]]` reference in the
file. Identity is preserved exactly: the shared placeholder is reused (never
rotated — rotating it would break placeholder-consuming state and cross-binding
sharing), and a refresh that would collide with a literal entry (or another
preset) fails atomically, naming both sides, with nothing written.

You *can* also change a pack's inputs yourself: edit the `[[preset]]` block's
own fields — `provider`, `secret`, `[preset.options]`, `disable`, or
`[preset.override.<suffix>]` — and the next resolve re-expands automatically
against the current definition (you touched the reference; that is your clock).
**That is also how you keep a hand change across a refresh:** there is no stamped
text to edit, so express the customization as a `disable` or
`[preset.override.<suffix>]` on the reference — those are the reference's own
inputs, so a refresh preserves them.

To drop a pack entirely, `preset remove PRESET` deletes its `[[preset]]` block
(and any `[preset.options]` / `[preset.override.*]` sub-tables) and its lock
snapshot, reporting what leaves the effective model:

```console
$ credp preset remove github
removed preset 'github' from workspace 'myproject': 3 binding(s), 0 rule(s) left the effective model
  binding github-api       bearer  api.github.com
  ...
```

`preset refresh` (when it has real changes) and `preset remove` are both gated
like the destructive set on the loose surface when they target an implicitly-
resolved workspace: they confirm first (`--yes` bypasses, and they fail closed
without a terminal). `--check` never gates.

## Pack options (host-half parameters)

Some host-half values a pack can't hard-code — a signing agent's socket dir, an
op:// path — differ per operator. A pack declares an `[[option]]` and references
it as the **whole value** of a host-half field with a structural
`{ option = "id" }` marker:

```toml
[[option]]
id          = "sock_dir"
type        = "string"                     # "string" | "enum" | "bool"
default     = "~/.ssh/credproxy-agent"
description = "host directory holding the signing agent's socket"

[[mount]]
bind   = { option = "sock_dir" }           # the whole source is the option
target = "/ssh-agent"

[[requires]]
kind = "path"
path = { option = "sock_dir" }             # reuse the same value here
```

An option supplies the **entire** value of a host-half field — a mount
`bind`/`volume` source, a `[[requires]]` `path`, or a `[[part]]`/`[[rule]]` `hosts`
element — never a token inside a string (there is no `{opt.x}`-in-string, no
conditionals, no interpolation). The container half (a mount `target`, `[env]`
values, `[[setup]]` steps) is the pack author's fixed namespace, so an option
marker there is rejected.

A `hosts` marker is what makes a **generic self-hosted-service pack** possible:
the hostname of a GitLab, Artifactory, or Vault instance is the per-org value, so a
pack points its `[[part]]` (and any `[[rule]]` guardrail) at a `host` option
instead of forking the pack per org. A `hosts` array can mix literals and markers:

```toml
[[part]]
suffix   = "api"
injector = "bearer"
hosts    = [{ option = "gitlab_host" }]
```

The resolved host is validated like any binding/rule host (a bad glob fails the
add) and joins the intercept set — `preset add` announces it as newly intercepted.

Values resolve at expansion time in one order: an explicit `--opt id=value`
(repeatable) → a prompt (only on the loose `credp` surface, in a terminal) → the
declared `default` → otherwise the add fails listing the missing options.

```console
$ credp preset add git-signing --opt sock_dir=/run/user/1000/gcr
```

The resolved value is written explicitly into the reference's `[preset.options]`
sub-table (`sock_dir = "/run/user/1000/gcr"`) and substituted into the expanded
mount source; the `{ option = … }` marker itself never reaches your workspace
file. On the strict `credproxy` surface (and on `credp` without a terminal), a
required option with no `--opt` and no default fails with a structured error
naming what to supply — never a prompt.

The same loose-surface prompting can fill an omitted **provider**/**secret** (a
picker over your registry and a free-text ref with an offered validate-now fetch),
turning a typo'd secret into an immediate, fixable moment instead of a first-start
face-plant.

## Declaring host prerequisites

Some things a pack needs live on the **host**, not in the container: the `gh` CLI
must be installed, a signing key's socket dir must exist, a provider must be able
to actually serve the secret. A pack can **declare** those with `[[requires]]`
blocks so the tooling checks them and tells you exactly what to fix.

```toml
[[requires]]
kind = "command"           # found on the host PATH (looked up, never run)
command = "gh"
hint = "install the GitHub CLI: https://cli.github.com"

[[requires]]
kind = "path"              # host path exists (~ and $VARS expanded)
path = "~/.ssh/credproxy-agent"
hint = "run credproxy-signing-agent to create the socket dir"

[[requires]]
kind = "env"               # host env var set and non-empty
var = "SOME_VAR"
hint = "export SOME_VAR before start"

[[requires]]
kind = "provider"          # the provider chosen for this pack resolves
fetch = true               # (optional) also test-fetch the secret
hint = "authenticate: gh auth login"
```

The four kinds — `command`, `path`, `env`, `provider` — are **the whole set**,
implemented by credproxy itself. A pack **never supplies a script to run** on the
host: declaring a prerequisite is safe even from a freshly cloned overlay,
because nothing pack-authored executes. The one sanctioned host-executable is a
[provider](../reference/providers.md), reached only through the normal provider
protocol for the `provider` kind (and `fetch = true`, like `binding test`, may
prompt or unlock a vault).

Checks are **advisory** at `preset add` / `create` time and **authoritative** at
`doctor` time:

```console
$ credp preset add git-signing
applied preset 'git-signing' to workspace 'myproject': 1 binding(s), 0 rule(s)
  binding git-signing-key   ...
unmet prerequisite (path): /home/you/.ssh/credproxy-agent does not exist -- run credproxy-signing-agent to create the socket dir
1 unmet prerequisite(s) above -- fix them, then `credproxy doctor myproject` to re-check
```

The reference still lands — the config is durable and the host state is fixable
afterward, so failing checks warn but never block the add (it exits 0). Once you
fix the prerequisite, `credproxy doctor myproject` goes green:

```console
$ credproxy doctor myproject
✗ [myproject] preset 'git-signing' requires (path): /home/you/.ssh/credproxy-agent does not exist  → run credproxy-signing-agent ...
$ credproxy-signing-agent            # create the socket dir
$ credproxy doctor myproject
✓ [myproject] preset 'git-signing' requires (path): /home/you/.ssh/credproxy-agent exists
```

`doctor` discovers which packs a workspace uses from its `[[preset]]` references
and the lock snapshot, so it re-checks exactly the packs you applied. A
`fetch = true` provider check runs only under `doctor NAME --fetch` (it resolves
a secret, which can prompt) — a plain `doctor` degrades it to a resolve-only
provider check and never fetches.

> [!WARNING]
> Notice the `newly intercepted` line in the output. Adding a binding (or a rule)
> for a host tells the proxy to open TLS to it. A workspace that has not
> installed the proxy's certificate will get a certificate error on that host
> until it bootstraps. The default workspace image bootstraps automatically; a
> custom image may not. [Chapter 08](08-going-further.md) covers custom images.

> [!TIP]
> Presets live in the same layered registry as providers and injectors, so your
> team can ship its own — an internal artifact registry, a vault-backed service —
> and everyone applies it with one command → [Overlays](../advanced/overlays.md).
> Otherwise, continue.

---

**Next:** [07 · Rules](07-rules.md) — add guardrails that never touch a
credential.
