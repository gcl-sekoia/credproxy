# credproxy — gcl-sekoia fork

A fork of **[credproxy](https://github.com/gregclermont/credproxy)** with an opinionated
overlay setup layered on top.

credproxy keeps your real credentials out of a dev container: your tools send a
*placeholder*, and a proxy swaps in the real secret on the way out, so the secret never
enters the container. The full product story — how it works, the credential model, the
reference docs — is in the [main README](../README.md) and the [docs](../docs/README.md),
unchanged from upstream.

## What this fork adds

Everything lives in [`overlay/`](../overlay/README.md), as three layered overlays:

- **[`base`](../overlay/base/README.md)** — a neutral, reusable library of
  [preset](../docs/guide/06-presets.md) packs (`proxy-ca`, `toolchain`, `claude-code`,
  `github-auth`, `git-signing`), each carrying the bindings/rules/mounts/env/setup/requires
  it needs (`claude-code` is an umbrella: token + client config + session hook), plus a
  neutral workspace template.
- **[`claude-managed-settings`](../overlay/claude-managed-settings/README.md)** — a
  standalone opinionated policy pack: a hidden rule that rewrites Claude Code's
  server-managed settings on the wire.
- **[`50-example`](../overlay/50-example/README.md)** — an opinionated profile that
  composes the other two (its `workspace.template.toml` is what `credproxy create` stamps)
  and adds glue: a persist volume, a fuller tool kit, opinionated Claude defaults.

Apply any pack à la carte with `credproxy workspace NAME preset add PACK`, or build your
own on top by adding a higher-priority overlay that *shadows* just the pieces you want —
[`overlay/README.md`](../overlay/README.md) explains the fork/layer model.

## Quickstart

```sh
git clone https://github.com/gcl-sekoia/credproxy.git
cd credproxy
export PATH="$PWD/bin:$PATH"
credp create myproject --here && credp start
```

Full install, daily workflow, and the credential model: the
[main README](../README.md) and the [install guide](../docs/guide/01-install.md).
