# Overlay (org / fork customization)

This directory is the default **overlay**: the middle tier of credproxy's
resolution chain, between an end user's personal config and the in-package
upstream defaults:

```
user ($XDG_CONFIG_HOME/credproxy)  →  overlays (this dir)  →  builtin (upstream)
```

It's how an **org or fork customizes credproxy without touching engine code**.
Everything here is data; upstream ships this directory empty (just this README),
so a fork only ever *adds* files and never conflicts on `git merge upstream`.
Your entire diff against upstream lives here.

You don't have to fork at all: set **`CREDPROXY_OVERLAY_PATH`** to point at any
directory — or an `os.pathsep`-separated list of directories, searched
leftmost-first — with this same layout (a deb/rpm payload, a git submodule,
`/etc/credproxy/overlay`). The variable replaces this default entirely; unset
falls back to this `<repo>/overlay/`, and a set-but-empty value (`""`) means no
overlays at all.

## What you can put here

| File / dir | Overrides | Effect |
|---|---|---|
| `workspace.template.toml` | the builtin scaffold (or a less-specific overlay's) | the `<name>.toml` a fresh `credproxy create` produces — your canonical default workspace (image, user, setup, even default `[[binding]]` blocks). |
| `injectors/<name>.toml` | a same-named injector below (or new) | a request-shaping scheme. |
| `providers/<name>` | a same-named provider below | a secret source executable. |
| `scripts/<name>.star` | a same-named script below | a sandboxed Starlark injector / rule body. |
| `presets/<name>.toml` | a same-named preset below | a service setup pack (bindings + rule guardrails). |

A same-named file **shadows** the one below it; a new name **adds** to the set.
Among multiple overlays, the leftmost on `CREDPROXY_OVERLAY_PATH` wins.

See the builtin defaults under `cli/credproxy_cli/builtin/` for complete worked
copies. Full guide: [`docs/overlays.md`](../docs/overlays.md).

## `workspace.template.toml` note

The scaffold is a **literal workspace config** — its `image`, `user`, `home`,
`setup` are concrete values. credproxy replaces every occurrence of the exact
token `{name}` with the workspace name and touches nothing else (no
`str.format`), so literal braces need no escaping. The proxy image tag and the
workspace image are not separate knobs: the workspace image is just the `image`
line here, and the proxy image tag (`IMAGE_TAG`) is fixed in the engine. To run a
different workspace image, edit `image` (and `user`/`home` to match) in your
template — or, per workspace, in the generated `<name>.toml`.

## Precedence

A user file under `$XDG_CONFIG_HOME/credproxy/` shadows every overlay — for
**every** asset, including `workspace.template.toml` (the singleton rides the
same walk as the registries). So an individual can always override an org
default locally, with no carve-out.
