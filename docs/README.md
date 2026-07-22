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
  the macOS Keychain, Bitwarden, and the `gh` CLI as credential sources.
- [06 · Packs](guide/06-packs.md) — wire a whole service with one command.
- [07 · Rules](guide/07-rules.md) — block, stub, and rewrite traffic as
  guardrails, no credential involved.
- [08 · Going further](guide/08-going-further.md) — many workspaces, mounts,
  custom images, and where to go next.

## Reference

- [Configuration](reference/configuration.md) — the workspace TOML file, field
  by field, and the commands that edit it.
- [Providers](reference/providers.md) — the built-in credential sources and how
  to write your own.
- [Injectors](reference/injectors.md) — the credential-shaping schemes
  (`bearer`, `basic`, `sigv4`, re-seal, scripts).
- [Rules](reference/rules.md) — the traffic-governance layer in full.
- [PostgreSQL](reference/postgres.md) — the credential-injecting connection
  broker: a second listener the workspace dials at `proxy.local:5432`.
- [Workspace internals](reference/workspace.md) — the bring-your-own-image
  contract: what joining the proxy's network imposes, and how users, mounts, and
  SELinux behave.

## Understand and troubleshoot

- [Security](security.md) — the threat model: what credproxy protects against,
  and what it does not.
- [Troubleshooting](troubleshooting.md) — common errors and how to read the
  logs.

## Advanced

- [Overlays](advanced/overlays.md) — ship team defaults and custom definitions
  without forking.
- [Composability](advanced/composability.md) — attached workspaces, Docker
  Compose, and CI.

## Contributing

- [Development environment](dev-environment.md) — hacking on credproxy itself.
