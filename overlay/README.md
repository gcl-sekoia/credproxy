# Overlays (org / fork customization)

This directory is the default **overlay container**: every *subdirectory* you
create here is one overlay — a named bundle of customizations in credproxy's
resolution chain, between an end user's personal config and the in-package
upstream defaults:

```
user ($XDG_CONFIG_HOME/credproxy)  →  overlays (subdirs here)  →  builtin (upstream)
```

It's how an **org or fork customizes credproxy without touching engine code** —
everything here is data. **Upstream ships this container empty (just this README)**; a
fork adds directories here, and since its whole diff lives in `overlay/`, it never
conflicts on `git merge upstream`:

```sh
mkdir overlay/acme-corp        # active immediately, labeled overlay:acme-corp
```

**This repo is such a fork** — the overlays listed below are its additions. To build on
them without editing them (staying merge-clean against *this* fork), **layer instead of
edit**: give your overlay a higher priority (earlier basename) and ship a same-named
asset to **shadow** just the piece you want — exactly what `50-example` does to
`claude-setup`'s empty default.

## What's here

This fork populates the container with reusable pieces to compose, layer over, or fork:

- **Lib overlays** — composable, single-purpose, inert building blocks, each with its own
  README: `setup-runner`, `toolchain`, `git-signing`, `github-auth`, `claude-setup`,
  `claude-managed-settings`, `claude-session-context`.
- **`50-example`** — an opinionated worked profile that *composes* the libs; its
  `workspace.template.toml` is what `credproxy create` stamps. A fork-me starting point
  that also demonstrates each override style (additive fragment, file shadow, rule param).

## Layout — inside your named overlay

The registry subdirs and the scaffold template go **inside** the named overlay,
not at this container's top level:

```
overlay/acme-corp/
  workspace.template.toml      # the scaffold a fresh `credproxy create` produces
  injectors/<name>.toml        # request-shaping schemes
  providers/<name>             # secret-source executables
  scripts/<name>.star          # sandboxed Starlark injector / rule bodies
  packs/<name>.toml          # service setup packs: bindings + rule guardrails
  tests/test_*.py              # optional; run by `credproxy dev test` (testkit)
```

A same-named file **shadows** the tier below it; a new name **adds** to the set.
A user file under `$XDG_CONFIG_HOME/credproxy/` shadows every overlay — for
**every** asset, including `workspace.template.toml` (the singleton rides the
same walk as the registries), so an individual can always override an org
default locally.

## Ordering and activation

- Multiple subdirectories are all active, searched in **lexical order by
  basename** — the earliest wins a name conflict. When order matters, make it
  explicit with numeric prefixes: `10-base/`, `20-team/` (then `10-base`
  shadows `20-team`).
- **Any subdirectory here activates as an overlay** — don't park scratch or
  backup directories in this container; they'd be picked up (and shown by
  `credproxy info`).
- The `CREDPROXY_OVERLAY_PATH` env var still **overrides and replaces** this
  discovery entirely, for external bundles (a deb/rpm payload, a git submodule,
  `/etc/credproxy/…`): an `os.pathsep`-separated list searched leftmost-first;
  set-but-empty (`""`) means no overlays at all.

## `workspace.template.toml` note

The scaffold is a **literal workspace config** — its `image`, `user`, `home`,
`setup` are concrete values. credproxy replaces every occurrence of the exact
token `{name}` with the workspace name and touches nothing else (no
`str.format`), so literal braces need no escaping. The proxy image tag and the
workspace image are not separate knobs: the workspace image is just the `image`
line here, and the proxy image tag (`IMAGE_TAG`) is fixed in the engine. To run a
different workspace image, edit `image` (and `user`/`home` to match) in your
template — or, per workspace, in the generated `<name>.toml`.

See the builtin defaults under `cli/credproxy_cli/builtin/` for complete worked
copies. Full guide: [`docs/advanced/overlays.md`](../docs/advanced/overlays.md).
