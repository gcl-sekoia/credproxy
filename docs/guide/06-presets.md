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
