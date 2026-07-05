# Customizing credproxy with overlays

An org or team often wants its own defaults: a standard workspace image, an
internal CA in every container's setup, a vault provider, an artifact-registry
preset. credproxy is built so you can do all of that **without editing engine
code** — and, ideally, without maintaining a code fork at all.

## The resolution order

Every customizable asset resolves through one ordered search path, most specific
first:

```
user            $XDG_CONFIG_HOME/credproxy/   per-machine, the end user
  ↓ shadows
overlays         CREDPROXY_OVERLAY_PATH (declared order) or <repo>/overlay/
  ↓ shadows
builtin          cli/credproxy_cli/builtin/   upstream defaults (in-package)
```

A same-named file in a higher tier **shadows** the lower one; a new name **adds**
to the set. This is `paths.overlay_roots()` — the single seam both
`paths.layered_dirs()` (the registries: injectors, providers, scripts, presets)
and `paths.resolve_singleton()` (the one singleton, `workspace.template.toml`)
derive from, so every asset kind resolves through exactly the same tiers.

The **middle tier is N overlays, not one.** `CREDPROXY_OVERLAY_PATH` is an
`os.pathsep`-separated list of directories (`:` on Unix), searched
leftmost-first — PATH semantics. So you can layer a team overlay over an
org-wide one, or a machine overlay over a shipped bundle, without merging them:

```sh
export CREDPROXY_OVERLAY_PATH=/etc/credproxy/team-ml:/etc/credproxy/org-base
```

Rules for the variable:

- **Unset** falls back to the single `<repo>/overlay/` (the default).
- **Set-but-empty** (`CREDPROXY_OVERLAY_PATH=""`) means **no overlays** — an
  explicit opt-out, distinct from unset.
- Empty entries within the list (`a::b`, a trailing `:`) are skipped.
- The variable **replaces** the default entirely; it never appends to it.
- Overlays are labelled `overlay:<dir basename>` (e.g. `overlay:team-ml`);
  duplicate basenames get a deterministic numeric suffix (`overlay:base`,
  `overlay:base#2`) in declared order. These labels show up in `credproxy info`,
  the registry `list` commands, and `doctor` check ids.

A missing overlay dir is tolerated during resolution (it simply contributes
nothing) — `credproxy doctor` is what flags a configured-but-missing overlay
loudly (one existence check per entry).

## Two ways to customize

### 1. Point at an overlay bundle — no fork (recommended)

Set `CREDPROXY_OVERLAY_PATH` to any directory (or `:`-list of directories) with
the layout below — a deb/rpm payload, a git submodule, `/etc/credproxy/overlay`,
a dotfiles dir:

```sh
export CREDPROXY_OVERLAY_PATH=/etc/credproxy/overlay
```

Nothing to merge: you ship the overlay as data, on whatever cadence you like, and
track upstream credproxy unmodified.

### 2. Fork the repo

Commit your customizations under `overlay/` (which upstream ships empty except a
README). Your **entire diff against upstream lives in `overlay/`**, and upstream
never writes there, so `git merge upstream/main` is conflict-free in perpetuity.
The engine and builtin defaults you inherit; your overlay you own. `overlay/` is
just the default single entry on `CREDPROXY_OVERLAY_PATH` — the fork story and
the no-fork bundle are the same mechanism.

## What you can put in an overlay

```
<overlay>/
  workspace.template.toml      # the scaffold a fresh `create` produces
  injectors/<name>.toml        # request-shaping schemes
  providers/<name>             # secret-source executables
  scripts/<name>.star          # sandboxed Starlark injector / rule bodies
  presets/<name>.toml          # service setup packs: bindings + rule guardrails
```

> The only hardcoded engine constant is the proxy image tag (`IMAGE_TAG`). There
> is **no default *workspace* image knob** and **no `home` fallback** — the
> default workspace image is simply the `image` line in
> `workspace.template.toml`, and `home` is optional sugar for a managed volume
> (omit it for an ephemeral home).

### `workspace.template.toml` — the scaffold

The `<name>.toml` body a fresh `credproxy create` writes. Make it your canonical
default workspace — your image, your `user`/`home`, your `setup`, even default
`[[binding]]` blocks for org infrastructure. It is a **literal** workspace
config: every occurrence of the exact token `{name}` is replaced with the
workspace name, and **nothing else** is touched — no `str.format`, so literal
braces (`{ volume = ... }` inline tables, `${VAR}`, a stray `{foo}`) need no
escaping or doubling. To run a different image, edit `image` (and `user`/`home`
to match) here, or per workspace in the generated `<name>.toml`.

Because it rides the same walk as the registries, a **user** can keep a personal
`$XDG_CONFIG_HOME/credproxy/workspace.template.toml` that shadows every overlay's
(and the builtin) — the same shadow rule as any other asset.

> **The `injector scaffold` / `provider scaffold` templates stay builtin.**
> `credproxy injector scaffold` seeds from the builtin `bearer`, and
> `provider scaffold` from the builtin `env`, ignoring overlays. That is
> deliberate: a scaffold is an *upstream authoring template* to start from, not
> an overlay-customizable default.

### Registries — injectors / providers / scripts / presets

Drop a `<name>.toml` (or executable, or `.star`) in the matching subdir. Same
name as a builtin (or a less-specific overlay) **replaces** it; a new name
**adds** it. The shapes match the builtin examples — see
[`injectors.md`](injectors.md), [`providers.md`](providers.md), and
`cli/credproxy_cli/builtin/presets/github.toml`.

**Shipping a policy as a pack.** A rule script (`scripts/readonly-guard.star`)
and a **preset** that wires it (`presets/org-guardrails.toml`, an optional
`[[rule]]` array; see [`rules.md`](rules.md#distributing-a-policy-script--preset))
travel together in the overlay, so a workspace applies the whole policy with one
`credproxy workspace NAME preset add org-guardrails`. A preset can carry bindings,
rules, or both — a **pure-rule** pack (no `[placeholder]`/provider) is a
credential-free policy bundle.

**Template vs. preset — both exist on purpose.** Baking `[[binding]]`/`[[rule]]`
blocks into `workspace.template.toml` applies them to **every** workspace at
**create** time, all-or-nothing. A **preset** is the **per-service, composable,
post-create** granularity: applied to the workspaces that need it, when they need
it, and stacked with others. Use the template for "every box gets this"; use a
preset for "this box also talks to service X."

## Shipping static files (overlay mounts)

Beyond the registries, an overlay can hold **arbitrary static files** — a CA
cert, an `.npmrc`, a `.gitconfig`, a setup script — and mount them into every
workspace. In `workspace.template.toml` (or a workspace's `mounts`), an
`{ overlay = "REL", target = "/dst" }` mount binds a path **relative to an
overlay dir** into the container (confined within the overlay dir, read-only by
default). With N overlays, `REL` is searched in declared order — the first
overlay containing it wins — so a more-specific overlay can override a shipped
file. The overlay becomes a self-contained bundle: declarative config *and* the
static assets it references. See [`configuration.md`](configuration.md) `mounts`.

## Provenance — which override actually won

`credproxy info` answers "is my overlay active, and which of my overrides won"
without reading source files: it lists the overlays in resolution order (with
present/absent), the per-tier registry counts keyed by full label, and an
`overlay_overrides` total (the **effective** view — an overlay asset a user file
shadows counts as `user`, not as an overlay override). The registry `list`
commands (`injector list`, `provider list`, `preset list`) annotate each row
with the tiers it **shadows**, e.g. `bearer   overlay:team-ml   (shadows
builtin)`.

## Precedence and testing

A user's `$XDG_CONFIG_HOME/credproxy/` file still wins over the overlays, which
win over builtin — for **every** asset including `workspace.template.toml`, so an
individual can override an org default locally with no exception. To verify an
overlay in place, point `CREDPROXY_OVERLAY_PATH` at it and run `credproxy info`,
`credproxy injector list`, `credproxy preset list`, `credproxy config`, or
`credproxy workspace create … && credproxy workspace … config --declared`.
