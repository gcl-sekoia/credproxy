# overlay: base

A **neutral, reusable library** of credproxy [preset](../../docs/guide/06-presets.md)
packs plus a plain `workspace.template.toml` that composes them. Nothing opinionated
lives here — this is the foundation the [`50-example`](../50-example/README.md) profile
layers its opinions on top of, and a clean starting point on its own.

Everything is data — no engine code. Cherry-pick a pack with `credproxy workspace NAME
preset add PACK`; run `credproxy preset list` to see each pack's expansion first.

## The preset packs

A pack carries any subset of: `[[part]]` bindings (a credential shaped for a host),
`[[rule]]` guardrails, the **container half** (`[[mount]]` / `[env]` / `[[setup]]` —
files, env vars, and ordered setup steps that run as the workspace user with the binding
env injected), `[[requires]]` host-prerequisite checks, and `[[option]]` host-half
parameters. `preset add`/`create` stamp them as ordinary config (expansion, not a link).

| Pack | Credential? | What it does |
|------|-------------|--------------|
| **proxy-ca** | — | Installs the proxy's TLS CA into the trust store (one root setup step, `order 0`). A BYO image doesn't trust it out of the box; the credproxy default image auto-bootstraps, so this is opt-in per template. |
| **toolchain** | — | Installs a dev CLI toolchain via [mise](https://mise.jdx.dev) (setup `order 10`). Data-driven: the installer reads the union of `/opt/toolchain/tools.d/*.list`, so a profile adds tools on top of the lean `base.list` by mounting another fragment. Also ships a mise-activation zsh drop-in (CLIs on the interactive PATH) and `[env]` `LANG`/`COLORTERM` for correct glyphs + truecolor. |
| **cache** | — | A `/cache` managed volume for **regenerable** toolchain state — points `XDG_CACHE_HOME` (the XDG-honoring cache tier: mise/uv/pip) plus the DATA-tier install dirs (`MISE_DATA_DIR`, uv pythons/tools) at it, so a recreate skips re-installing the toolchain. Pre-setup chown (`order 5`), then the `toolchain` install (`order 10`) populates it. The discardable sibling of `persist`: `recreate --reset-volume cache` is safe because that recreate re-runs the `toolchain` setup step, repopulating the volume. Opt-in (left out of the base template). |
| **claude-code** | *(you choose)* | **Umbrella pack — everything to run Claude Code as a client:** the Anthropic OAuth token (`bearer` on `api.anthropic.com`; the container sees only a token-shaped `$CLAUDE_CODE_OAUTH_TOKEN` placeholder), onboarding-skip + client-`settings.json` merge (setup `order 20`, merges a JSON *if mounted* at `/opt/claude-code/settings-defaults.json`), a SessionStart orientation hook (`order 50`; ships the credproxy + installed-tools notes, extensible via `/opt/session-context.d/*.sh`), and `MISE_MINIMUM_RELEASE_AGE_EXCLUDES=claude`. Assumes the `claude` CLI is installed (a toolchain tool). **Neutral** — no default vault, so `preset add claude-code --provider … --secret …` (or a template `[[preset]]`) supplies the token source. |
| **github-auth** | `gh-cli` | Makes `gh` **and** `git push` (HTTPS) work off one GitHub token — `bearer` on `api.github.com`, HTTP `basic` on `github.com`, sharing one placeholder. Setup step (`order 45`) bridges git's credential helper to `gh` and derives the git identity. `[[requires]]` a host `gh` login. |
| **git-signing** | — | Signs commits with an ssh key held in a **forwarded** ssh-agent (setup `order 40`). Only the agent socket dir is bind-mounted — the key never enters the container. Opt-in; the host socket dir is an `[[option]]` (`sock_dir`). |
| **gcloud** | `gcloud` | Makes `gcloud`/`gsutil`/`bq` work off the host's Google login — `bearer` on `*.googleapis.com` off a **host-minted** ~1h access token (`CLOUDSDK_AUTH_ACCESS_TOKEN`), so the refresh token / SA key never enter the container; `[env]` sets the gcloud-only CA knob (`CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE`). Token is static per push — `credp push` to refresh. `[[requires]]` a host `gcloud` login. |

*(Patching Claude Code's **server-managed** settings is a separate, opinionated policy —
it lives in the [`claude-managed-settings`](../claude-managed-settings/README.md) overlay.
Note the two settings layers interact: for e.g. `bypassPermissions` to take effect you
need both `claude-code`'s client default AND `claude-managed-settings` stripping the
org's `disableBypassPermissionsMode`.)*

## The template

`workspace.template.toml` is a **neutral scaffold**: the devcontainer base image, a
project bind mount (**edit before `start`**), and the generic packs as `[[preset]]`
entries (`proxy-ca`, `toolchain`, `github-auth`). `cache`, `claude-code`, and `git-signing`
are left commented — `cache` trades disk for faster recreates (a persistent volume, so
off by default), Claude Code is opt-in (it needs the `claude` CLI + a token source), and
signing needs a host socket dir. All setup steps and env come from the packs — the
template itself carries no `setup`/`[env]`.

## git-signing: the host helper

`bin/credproxy-signing-agent` runs **on the host** (not in the container): it starts a
dedicated ssh-agent holding only the signing key, on a fixed socket, for forwarding into
the workspace. Put its dir on your `PATH`, run it before `start`, then enable the
`git-signing` pack (uncomment its `[[preset]]`, or `preset add git-signing --opt
sock_dir=...`).

## Testing

`tests/` holds structural checks on the packs (via `credproxy_cli.core.presets`);
`credproxy dev test` discovers and runs them. See
[`docs/advanced/overlays.md`](../../docs/advanced/overlays.md) "Testing your overlay".
