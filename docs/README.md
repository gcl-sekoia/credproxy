# credproxy documentation

credproxy lets your credentials work inside a dev container without ever being
inside it. New here? Read the [project README](../README.md) for the pitch and a
5-minute quickstart, then follow the guide below in order.

## Start here

- **[The guide](guide/01-install.md)** — a step-by-step path from an empty
  machine to a working credential inside a container.
- **[Concepts](concepts.md)** — the glossary. Every term defined once, with an
  example. Other pages link here.
- **[How it works](how-it-works.md)** — the architecture: two containers, one
  network, the placeholder swap, and the push model.

## The guide

Read these in order the first time.

- [01 · Install](guide/01-install.md) — clone the repo, put the commands on your
  PATH, check prerequisites, verify with `credproxy doctor`.
- [02 · Your first workspace](guide/02-first-workspace.md) — create, start
  (build the proxy image), and enter a workspace. What gets created.
- [03 · Daily workflow](guide/03-daily-workflow.md) — `credp` vs `credproxy`,
  the default workspace, directory addressing, the start/enter/stop rhythm.
- [04 · Your first credential](guide/04-first-credential.md) — the payoff: a
  GitHub token that works inside the container while the secret stays out.
- [05 · Secret managers](guide/05-secret-managers.md) — beyond `env`: 1Password,
  the macOS Keychain, Bitwarden, and the `gh` CLI as credential sources. *(Phase 2)*
- [06 · Presets](guide/06-presets.md) — wire a whole service with one command. *(Phase 2)*
- [07 · Rules](guide/07-rules.md) — block, stub, and rewrite traffic as
  guardrails, no credential involved. *(Phase 2)*
- [08 · Going further](guide/08-going-further.md) — mounts, attached workspaces,
  and where to go next. *(Phase 2)*

## Reference

- [Configuration](reference/configuration.md) — the workspace TOML file, field
  by field, and the commands that edit it. *(Phase 2)*
- [Providers](reference/providers.md) — the built-in credential sources and how
  to write your own. *(Phase 2)*
- [Injectors](reference/injectors.md) — the credential-shaping schemes
  (`bearer`, `basic`, `sigv4`, re-seal, scripts). *(Phase 2)*
- [Rules](reference/rules.md) — the traffic-governance layer in full. *(Phase 2)*

## Understand and troubleshoot

- [Security](security.md) — the threat model: what credproxy protects against,
  and what it does not. *(Phase 2)*
- [Troubleshooting](troubleshooting.md) — common errors and how to read the
  logs. *(Phase 2)*

## Advanced

- [Overlays](advanced/overlays.md) — ship team defaults and custom definitions
  without forking. *(Phase 2)*
- [Composability](advanced/composability.md) — attached workspaces, Docker
  Compose, and CI. *(Phase 2)*

## Contributing

- [Development environment](dev-environment.md) — hacking on credproxy itself.
  *(Phase 2)*

> [!NOTE]
> Pages marked *(Phase 2)* are being written in this same pull request. Links to
> them may not resolve yet.
