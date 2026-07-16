# overlay: 50-example

An **opinionated example profile** — the one this repo's author dogfoods. It mostly
**composes** the [`base`](../base/README.md) library and the
[`claude-managed-settings`](../claude-managed-settings/README.md) policy pack into a
batteries-included workspace, adds a couple of small packs of its own (`persist`, and a
shadow of `claude-managed-settings` carrying the real patch), and a little profile glue.
Fork it, or layer your own higher-priority overlay over it, as a starting point.

It sits **on top** of the other overlays: the `50-` prefix sorts before the letter-named
`base`, so this overlay's `workspace.template.toml` **shadows** base's (a template is a
singleton — the highest-priority overlay's wins). The rest of what it ships are new files
(no conflicts), resolved wherever they're referenced.

## What it adds on top of base

- **`workspace.template.toml`** — base's neutral scaffold **plus** opinions, expressed as
  `[[pack]]` entries: the `claude-code` pack wired to Bitwarden (`provider = "bw"`), the
  `claude-managed-settings` pack turned on, base's `cache` pack (opt-in there, activated
  here for faster recreates), the `persist` pack below, and the profile mounts below. It
  carries no `setup`/`[env]` of its own — all of that comes from packs.
- **`packs/persist.toml`** — a pack this overlay defines: a durable `/persist` volume +
  its pre-setup chown + the `CLAUDE_CONFIG_DIR`/`HISTFILE` env, so Claude config and shell
  history survive recreates.
- **`packs/claude-managed-settings.toml`** — a **shadow** of the base policy pack (this
  overlay sorts first, so it wins): same mechanism, but supplies the real
  org-gate-stripping `settings_patch` the neutral pack leaves as `"{}"`. Since a template
  can't inject rule params into a pack, the patch lives in this shadow pack.
- **`tools.d/extras.list`** — a fuller tool kit (claude, ast-grep, bat, delta, gron, yq,
  eza), union-merged with base toolchain's `base.list` — the *additive-fragment* override
  style.
- **`claude-settings-defaults.json`** — opinionated Claude client defaults
  (`bypassPermissions`, `model = opus`), mounted at `/opt/claude-code/settings-defaults.json`
  where the base `claude-code` pack deep-merges it — the *profile-supplies-the-policy-file*
  style. (Pairs with the `claude-managed-settings` shadow: this sets the client default,
  that strips the org's server-side veto.)
- **`omz-custom/prompt.zsh`** — a prompt tweak (username → workspace hostname), alongside
  the mise-activation drop-in the base toolchain pack ships.
- **`session-context.d/example.sh`** — an inert copy-me SessionStart fragment; edit it and
  uncomment its mount in the template to add project orientation (the `claude-code` pack's
  hook concatenates every `/opt/session-context.d/*.sh`).
- **`skills/suggesting-bindings/`** — a Claude Code **skill** the base `claude-code` pack
  installs into `$CLAUDE_CONFIG_DIR/skills/` (the *profile-supplies-the-asset, base-supplies-
  the-mechanism* style, same as the agent defs). It teaches a workspace agent to suggest
  copy-pasteable `[[binding]]` blocks for credentials it needs — see *Keeping the skill
  fresh* below.

## Keeping the `suggesting-bindings` skill fresh

The skill is **agent-facing and auto-invoked**, and it **hardcodes** the binding
model: the builtin injector catalog, the provider list, the `binding`/`pack` CLI
command surface, and the `[[binding]]`/`[[pack]]` TOML shape. None of that is
derived at runtime, so when any of it evolves the skill drifts *silently* and
starts handing agents wrong suggestions.

**When you change the binding/injector/provider set, the `binding`/`pack` CLI
flags, the config/TOML syntax, or the host-pattern rules — update the skill in the
same change** (`skills/suggesting-bindings/SKILL.md` + `references/injectors.md`),
exactly as you would update `docs/` and the tests. This matters most on an
**upstream sync**: a rebase that pulls in a new injector, a renamed flag, or a
config-shape change must carry the skill forward too.

`tests/test_skill.py` is the tripwire for the mechanical half — it fails if a
builtin injector or provider isn't mentioned in the skill (so adding/renaming one
upstream breaks the overlay suite until the skill is updated). It can't catch
CLI-flag or TOML-syntax drift, though — that half is on you, which is why it's
called out here.

## Override styles it demonstrates

Four ways to customize without editing the upstream packs: an **additive fragment**
(`extras.list` into the toolchain union), a **policy file** the mechanism pack merges
(`claude-settings-defaults.json`), **shadowing a pack to set its policy**
(`packs/claude-managed-settings.toml` supplying the patch the base pack leaves as
`"{}"`), and **composing a separate policy-pack overlay** at all. The same shadow trick
extends anywhere: add a higher-priority overlay that shadows just the piece you want (a
`packs/<name>.toml`, or your own `workspace.template.toml`) — see
[`overlay/README.md`](../README.md).
