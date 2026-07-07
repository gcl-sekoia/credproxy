# credproxy — gcl-sekoia fork

A fork of **[credproxy](https://github.com/gregclermont/credproxy)** with an opinionated,
batteries-included overlay setup layered on top.

credproxy keeps your real credentials out of a dev container: your tools send a
*placeholder*, and a proxy swaps in the real secret on the way out, so the secret never
enters the container. The full product story — how it works, the credential model, the
reference docs — is in the [main README](../README.md) and the [docs](../docs/README.md),
unchanged from upstream.

## What this fork adds

Everything lives in [`overlay/`](../overlay/README.md) — a library of small, composable
**lib overlays** plus one **opinionated example profile** that wires them together:

- **Lib overlays** — `setup-runner`, `toolchain`, `git-signing`, `github-auth`,
  `claude-setup`, `claude-managed-settings`, `claude-session-context`. Each is
  single-purpose, safe by default, and documented by its own README.
- **[`50-example`](../overlay/50-example/README.md)** — a complete worked profile that
  composes the libs (its `workspace.template.toml` is what `credproxy create` stamps) and
  demonstrates each override style: an additive tool list, a settings file-shadow, and a
  rule param.

Build your own on top by adding a higher-priority overlay that *shadows* just the pieces
you want to change — [`overlay/README.md`](../overlay/README.md) explains the fork/layer model.

## Quickstart

```sh
git clone https://github.com/gcl-sekoia/credproxy.git
cd credproxy
export PATH="$PWD/bin:$PATH"
credp create myproject --here && credp start
```

Full install, daily workflow, and the credential model: the
[main README](../README.md) and the [install guide](../docs/guide/01-install.md).
