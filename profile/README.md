# Distribution profile (org overlay)

This directory is the **profile overlay**: the middle tier of credproxy's
resolution chain, between an end user's personal config and the in-package
upstream defaults:

```
user ($XDG_CONFIG_HOME/credproxy)  →  profile (this dir)  →  bundled (upstream)
```

It's how an **org or fork customizes credproxy without touching engine code**.
Everything here is data; upstream ships this directory empty (just this README
and `*.example` files), so a fork only ever *adds* files and never conflicts on
`git merge upstream`. Your entire diff against upstream lives here.

You don't have to fork at all: set **`CREDPROXY_PROFILE_DIR`** to point at any
directory (a deb/rpm payload, a git submodule, `/etc/credproxy/profile`) with
this same layout, and the CLI uses it as the overlay.

## What you can put here

| File / dir | Overrides | Effect |
|---|---|---|
| `profile.toml` | the bundled distribution constants | default workspace image, proxy image tag, default user/home/uid, default setup commands. Set any **subset** of keys; unset keys fall back to bundled. |
| `workspace.template.toml` | the bundled scaffold | the `<name>.toml` a fresh `credproxy create` produces — your canonical default workspace (image, setup, even default `[[binding]]` blocks). |
| `injectors/<name>.toml` | a bundled injector of the same name (or new) | a request-shaping scheme. |
| `providers/<name>` | a bundled provider | a secret source executable. |
| `scripts/<name>.star` | a bundled script | a sandboxed Starlark injector body. |
| `presets/<name>.toml` | a bundled preset | a coordinated multi-binding set (e.g. your internal registry). |

A same-named file **shadows** the bundled one; a new name **adds** to it. A user
file under `$XDG_CONFIG_HOME/credproxy/` still shadows the profile in turn.

See `*.example` files here for the shape, and the bundled defaults under
`cli/credproxy_cli/bundled/` for complete worked copies. Full guide:
[`docs/forking.md`](../docs/forking.md).

## `workspace.template.toml` note

The scaffold is rendered with `str.format`, so a custom template must use the
fill-in placeholders `{name}` and `{image}` (and may use `{home_line}`,
`{user_line}`, `{map_line}`, `{user_uid_line}`, `{setup_block}` — credproxy fills
these active-vs-commented based on whether the workspace uses your
`default_image`). **Double any literal braces** (`{{ ... }}`) so they survive
formatting.
