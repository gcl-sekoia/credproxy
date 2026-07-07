# overlay: 50-example

An **opinionated example profile** — a complete, working credproxy setup (the one this
repo's author dogfoods) that doubles as a worked example of the compose/override
patterns. It ships only the workspace scaffold (`workspace.template.toml`) and an
oh-my-zsh prompt drop-in (`omz-custom/profile.zsh`), and *composes* the reusable lib
overlays below. Fork it — or layer a higher-priority overlay over it — as a starting point.

## Composed libs

- **[`setup-runner`](../setup-runner/README.md)** — the setup orchestrator: drops to
  `$CREDPROXY_USER` and runs the `/opt/setup.d/` steps in order. Always mounted (base
  infra) at `/opt/workspace-setup.sh`, called by the `setup` array.
- **[`toolchain`](../toolchain/README.md)** — installs a dev CLI toolchain via mise. The
  lib ships a lean generic base list; this profile adds its own kit via `tools.d/extras.list`,
  union-merged (the add pattern). Also contributes the installed-tools `session-context`
  fragment.
- **[`git-signing`](../git-signing/README.md)** — git commit signing via a forwarded,
  dedicated ssh-agent. Opt-in (commented in the template; active in the credproxy
  dogfood workspace).
- **[`github-auth`](../github-auth/README.md)** — makes `gh` and `git push` work off a
  wire-injected GitHub token (real token never enters the container). Pairs with the
  `github-api`/`github-git` bindings in the template; requires `gh auth login` on the host.
- **[`claude-session-context`](../claude-session-context/README.md)** — the extensible Claude Code
  SessionStart context hook. The profile mounts the runner and the base `credproxy`
  fragment; the `toolchain` lib adds the installed-CLI fragment; the setup step
  registers the hook. Ships a copy-me `session-context.d/example.sh` (inert) with a
  commented mount — edit + uncomment to add a profile note.
- **[`claude-setup`](../claude-setup/README.md)** — Claude Code client config
  (onboarding-skip + settings merge). The lib default is empty; this profile **overrides**
  it by shipping its own `claude-settings-defaults.json` (`bypassPermissions`/`model`) —
  a file-shadow example.
- **[`claude-managed-settings`](../claude-managed-settings/README.md)** — rewrites the
  server-managed Claude Code settings (proxy rule + preset), plus a client-side
  cache-reset setup step.

## How composition works

The mount list in `workspace.template.toml` is the single enable/disable control. A
lib's setup step is mounted into `/opt/setup.d/NN-<lib>.sh`, and `setup-runner` runs
everything under `/opt/setup.d/` in NN order (after dropping to `$CREDPROXY_USER`) —
with no per-lib knowledge, so enabling or disabling a lib is purely adding or removing
its mounts. Runtime and data files (the tool list, the session-context runner and
fragments, `claude-settings-defaults.json`) mount at their own `/opt/<lib>/` paths.
`git-signing` is the one opt-in lib (commented in the template; active in the dogfood
workspace).
