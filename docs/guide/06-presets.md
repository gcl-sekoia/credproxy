← [05 · Secret managers](05-secret-managers.md) · [index](../README.md) · [07 · Rules](07-rules.md) →

# 06 · Presets

One credential often needs to work across several hosts of the same service, and
each host may want it in a different form. A GitHub token is a `bearer` token to
`api.github.com` but HTTP `basic` auth to `github.com` and `ghcr.io`. Wiring
that by hand is three bindings. A [preset](../concepts.md#preset) does it in one
command.

## See the expansion first

`credproxy preset list` shows every preset and exactly what it would stamp, so
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

## What got stamped

A preset is **expansion, not a link**: it writes ordinary `[[binding]]` blocks
into your config file and then forgets about them. The proxy never hears the word
"preset". Look in the TOML and you will find three plain bindings sharing one
placeholder:

```toml
[[binding]]
name     = "github-api"
injector = "bearer"
provider = "gh-cli"
secret   = "github.com"
hosts    = ["api.github.com"]
placeholder = "ghp_8Rrm2ERZeHSTAhrJmugGqJWiISluAsiBguAs"

[[binding]]
name     = "github-git"
injector = "basic"
# ... same provider, same placeholder ...
```

They all carry the same `placeholder`, so a single fake token works everywhere,
and any client-side token-shape check still passes. Because they are ordinary
bindings, you edit or remove them exactly as in chapter 04 — `credp binding
list`, `credp binding remove github-ghcr`, or hand-editing the file. Nothing
tracks that they came from a preset.

## Presets can carry rules too

A preset is a whole **service setup pack**, not just credentials. It may also
ship credential-free [rules](../concepts.md#rule) — guardrails like "block
`DELETE` on this host" — and a preset can be rules only, with no credential at
all. Rules are the next chapter.

## Presets can carry the container half too

A service often needs more than a token: a setup script, an env var, a mounted
file. A preset can ship those as well — `[[mount]]`, `[env]`, and `[[setup]]`
sections that stamp into the container-side of your workspace config, right
alongside the bindings. A pack can be *only* container config, with no
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

Applying it stamps the mount and the setup step next to the bindings — as plain
config, still **expansion, not a link**:

```console
$ credp preset add github-auth
applied preset 'github-auth' to workspace 'myproject': 1 binding(s), 0 rule(s), 1 mount(s), 1 setup step(s)
  binding github-auth-api    bearer  api.github.com
  mount   overlay user:setup.d/github-auth.sh -> /opt/github-auth.sh
  setup   [45] bash /opt/github-auth.sh
```

The container half (`mounts`/`env`/`setup`) changes the workspace spec, so if
the container already exists credproxy tells you to `start` again to apply it.
Each stamped block carries an inert `# credproxy:preset …` provenance comment so
a second `preset add` of the same pack is refused rather than duplicated. An
[attached workspace](../advanced/composability.md) — whose container credproxy
does not manage — refuses a container-half pack; binding/rule-only packs still
apply there.

Files a pack ships (that `setup.d/github-auth.sh`) live in the same layered
registry as the pack itself and are resolved from the pack's own tier →
[Overlays](../advanced/overlays.md).

## Refreshing a stamped pack

A preset is expansion, **not a link**: once `preset add` stamps its blocks, they
are ordinary config that never re-reads the definition. When a pack's definition
changes upstream (a new host, an added rule, a dropped part), re-expand the
stamped blocks on your own clock with `preset refresh`:

```
$ credp preset refresh github          # one pack; omit NAME for every applied pack
```

It compares each stamped block against what the current definition would write,
using the two provenance hashes the stamp recorded, and classifies per block:

- **up to date** — the block already matches; nothing written.
- **updated** — the definition changed and the block is untouched since stamping
  → the block is replaced (and its provenance marker refreshed).
- **skipped (hand-edited)** — you edited the block since it was stamped → it is
  **never** overwritten; refresh prints a diff of what it *would* write so you can
  reconcile by hand (or delete it and re-run).
- **added** — the definition gained a block → it is stamped additively, reusing
  the pack's existing shared placeholder and provider/secret.
- **vanished** — a stamped block whose definition counterpart is gone → reported
  only; pass `--prune` to delete it (a destructive action, so on the loose
  surface an implicit default workspace asks first).

The shared placeholder and the provider/secret are **preserved** (read back from
the stamped bindings, never regenerated — rotating the placeholder would break
placeholder-consuming state and cross-binding sharing). The write is
all-or-nothing. There is no merge: a hand-edited block is yours to resolve. A
pack that no longer resolves in the registry errors for an explicit
`refresh NAME` and is skipped-with-a-note when refreshing all; an attached
workspace refuses a container-half refresh (same as `preset add`).

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
`bind`/`volume` source or a `[[requires]]` `path` — never a token inside a string
(there is no `{opt.x}`-in-string, no conditionals, no interpolation). The
container half (a mount `target`, `[env]` values, `[[setup]]` steps) is the pack
author's fixed namespace, so an option marker there is rejected.

Values resolve at expansion time in one order: an explicit `--opt id=value`
(repeatable) → a prompt (only on the loose `credp` surface, in a terminal) → the
declared `default` → otherwise the add fails listing the missing options.

```console
$ credp preset add git-signing --opt sock_dir=/run/user/1000/gcr
```

The resolved value is stamped as ordinary literal config
(`bind = "/run/user/1000/gcr"`); the `{ option = … }` marker never reaches your
workspace file, and `preset refresh` reads the value back from the stamped mount
(so a refresh never re-prompts or resets it). On the strict `credproxy` surface
(and on `credp` without a terminal), a required option with no `--opt` and no
default fails with a structured error naming what to supply — never a prompt.

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

The pack still **stamps** — the config is durable and the host state is fixable
afterward, so failing checks warn but never block the add (it exits 0). Once you
fix the prerequisite, `credproxy doctor myproject` goes green:

```console
$ credproxy doctor myproject
✗ [myproject] preset 'git-signing' requires (path): /home/you/.ssh/credproxy-agent does not exist  → run credproxy-signing-agent ...
$ credproxy-signing-agent            # create the socket dir
$ credproxy doctor myproject
✓ [myproject] preset 'git-signing' requires (path): /home/you/.ssh/credproxy-agent exists
```

`doctor` discovers which packs a workspace uses from the provenance comments the
stamp left behind, so it re-checks exactly the packs you applied. A
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
