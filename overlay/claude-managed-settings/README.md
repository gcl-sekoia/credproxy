# overlay: claude-managed-settings

A **standalone policy pack** overlay: it can make the workspace's own Claude Code client
settings win over an org's **server-managed** settings, by rewriting the settings response
on the wire. The overlay ships only the *mechanism* — the patch it applies is a no-op by
default; you supply the actual (opinionated) patch. Activate it on top of
[`base`](../base/README.md) when you want it; the [`50-example`](../50-example/README.md)
profile turns it on with a real patch.

## What the pack does

`presets/claude-managed-settings.toml` is a pure-rule pack (no credential) + one setup step:

- a **hidden `script` rule** on `GET api.anthropic.com/api/claude_code/settings` that runs
  [`scripts/claude-code-settings-rewrite.star`](scripts/claude-code-settings-rewrite.star)
  — an RFC 7386 merge-patch of the settings document. The script ships **no policy** (empty
  default), and the pack's `[rule.params].settings_patch` is a no-op **`"{}"`** — the field
  is present so it's discoverable, but the pack imposes nothing on its own.
- a **setup step** (`order 30`, an inline `rm`) that deletes Claude Code's cached
  `remote-settings.json` so the next fetch goes through the proxy and gets rewritten.

Because response rules run **after** credential injection, the script never sees a real
secret — it only rewrites the settings JSON. See the credproxy rules model
([`docs/reference/rules.md`](../../docs/reference/rules.md)).

**This is the SERVER-settings half.** Claude Code has two settings layers: the org's
server-managed settings (what this pack rewrites on the wire) and the user's own client
`settings.json` (what the base [`claude-code`](../base/README.md) pack seeds from a mounted
defaults file). They interact — e.g. `defaultMode = "bypassPermissions"` only takes effect
if the client sets it **and** this pack strips the org's `disableBypassPermissionsMode`.
So an "unrestricted Claude" outcome needs both: the client default (claude-code +
`settings-defaults.json`) and this server-side strip.

## Use it

```sh
credproxy workspace NAME preset add claude-managed-settings   # no provider/secret needed
credproxy workspace NAME start                                # or apply, to push it
```

As applied, the rule is a no-op (`settings_patch = "{}"`). To make it do something, either
edit the stamped `[[rule]]`'s `settings_patch` in the workspace's `<name>.toml`, or — the
reusable way — **shadow this pack** from a higher-priority overlay with your own patch
(`null` deletes a key, per merge-patch). The [`50-example`](../50-example/README.md)
overlay does exactly that (`presets/claude-managed-settings.toml`), stripping the org-pushed
permission / sandbox / env-scrub gates.

## Testing

`tests/` unit-tests both the pack's expansion and the rewrite script (via the
[testkit](../../docs/advanced/overlays.md)); `credproxy dev test` runs them.
