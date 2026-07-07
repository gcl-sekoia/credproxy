# overlay: claude-setup (lib)

Prepares Claude Code's **client-side** config in a fresh workspace: it skips the
first-run onboarding wizard (when the workspace already has an OAuth token) and applies
a few baseline `settings.json` defaults. This touches only local client files — the
separate [`claude-managed-settings`](../claude-managed-settings/README.md) lib rewrites
the *server*-managed settings.

**Contributes:** a `setup.d` step (onboarding-skip + settings defaults) and
`claude-settings-defaults.json` (the defaults).

## Compose from a profile

```toml
{ overlay = "setup.d/claude-setup.sh", target = "/opt/setup.d/20-claude-setup.sh" },          # the setup step
{ overlay = "claude-settings-defaults.json", target = "/opt/claude-setup/settings-defaults.json" }, # the defaults
```
`setup-runner` runs the step automatically. It reads the defaults from
`/opt/claude-setup/settings-defaults.json` (override with `$CLAUDE_SETUP_DEFAULTS`).

## Configure — the default settings

The lib ships an intentionally **empty** default (`claude-settings-defaults.json` = `{}`),
so out of the box it imposes no settings — it only does the onboarding-skip. Defaults are
merged so **existing settings win**, and an empty one changes nothing.

A profile sets its own policy by **shadowing** the file: ship `claude-settings-defaults.json`
in a higher-priority overlay and it wins the mount (overlay resolution is most-specific
first). The 50-example profile does exactly this — its version turns on an opinionated dev
setup:
```json
{
  "permissions": { "defaultMode": "bypassPermissions" },
  "skipDangerousModePermissionPrompt": true,
  "model": "opus"
}
```
`bypassPermissions` turns off the permission prompts — a deliberate sandbox choice, which
is exactly why it lives in the *profile*, not this reusable lib.

## Workspace config it relies on

This lib only touches Claude Code's *client* files. Two things around it come from the
workspace's credproxy config (the profile's `workspace.template.toml`), not from this lib:

- **The OAuth token (required).** A `[[binding]]` that injects `CLAUDE_CODE_OAUTH_TOKEN`:
  the proxy swaps a placeholder for the real token on requests to `api.anthropic.com` and
  exports the placeholder into the workspace env. This authenticates Claude Code — and the
  onboarding-skip only fires when that env var is present.
  ```toml
  [[binding]]
  name     = "claude-code"
  injector = "bearer"
  provider = "bw"                 # wherever the token lives (bitwarden, 1password, env, …)
  secret   = "claude-code-oauth-token"
  hosts    = ["api.anthropic.com"]
  env      = "CLAUDE_CODE_OAUTH_TOKEN"
  ```
- **`CLAUDE_CONFIG_DIR` (a convenience).** This is a
  [Claude Code env var](https://code.claude.com/docs/en/env-vars), not something this lib
  introduces — Claude Code itself reads it to decide where its config lives, and the lib
  just follows the same paths when it writes. Everything works without it (Claude Code's
  defaults apply), but by default config is *scattered* — `~/.claude.json` sits apart from
  the `~/.claude/` dir. Pointing it at one directory pulls everything (including
  `.claude.json`) under a single path, so a persistent volume keeps all of Claude Code's
  state across a `recreate`. Putting it in a *subdir* of a shared durable-data volume
  works well, leaving room for other data (shell history, caches, …) alongside it:
  ```toml
  [env]
  CLAUDE_CONFIG_DIR = "/persist/claude"

  mounts = [
    # …
    { volume = "persist", target = "/persist" },   # durable data across recreates (Claude config, shell history, …)
  ]
  ```
  A managed volume starts **root-owned**, and this lib writes into it during setup (as the
  workspace user), so the profile must make it writable first — a `chown` in the `setup`
  array, which runs as root before the user-phase steps. The lib `mkdir -p`s the config
  dir itself, so a subdir target like `/persist/claude` works with no extra step:
  ```toml
  setup = [
    # …
    "chown \"$CREDPROXY_USER:\" /persist",
    "bash /opt/workspace-setup.sh",
  ]
  ```

(The credproxy dogfood workspace carries all of this.)
