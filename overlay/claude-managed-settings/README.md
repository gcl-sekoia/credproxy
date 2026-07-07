# overlay: claude-managed-settings (lib)

Rewrites the server-managed settings document Claude Code fetches from
`api.anthropic.com` by applying a merge-patch you supply — e.g. dropping the
restrictions the server pushes. It's a credential-free proxy **rule** (plus a
client-side cache reset) and carries no secret. Distinct from the `claude-code`
*credential* binding, and from [`claude-setup`](../claude-setup/README.md), which sets
*client*-side config.

**Contributes:** a proxy rule + preset (resolved by name, not mounted) and a `setup.d`
step (clears Claude Code's cached copy of the settings).

## Compose from a profile

Stamp the rule with the preset:
```sh
credproxy workspace NAME preset add claude-managed-settings
```
This adds the `claude-code-settings-rewrite` rule (hidden, response-phase) scoped to
`api.anthropic.com` + `/api/claude_code/settings` — but it ships **no policy of its
own**, so it's inert until you set a `settings_patch` (see Configure). Then mount the
cache-reset step so a stale pre-rewrite copy doesn't linger:
```toml
{ overlay = "setup.d/claude-managed-settings.sh", target = "/opt/setup.d/30-claude-managed-settings.sh" },
```
(The reset is belt-and-suspenders — the rule already forces a fresh fetch on every
request — and harmless without the rule.)

## Configure — the settings patch

The rewrite script ships an **empty** `DEFAULT_PATCH`, so with no config the rule forces
a fresh fetch but changes nothing. Make it do something by setting `settings_patch` on
the rule — a JSON [RFC 7386](https://www.rfc-editor.org/rfc/rfc7386) merge-patch string
in a `[rule.params]` sub-table (`null` deletes a key, anything else replaces it):
```toml
[[rule]]
# … the stamped rule …
[rule.params]
# strip the org-pushed env-scrub + permission/sandbox gates (what 50-example sets):
settings_patch = '{"env": {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": null}, "permissions": {"allow": null, "deny": null, "disableBypassPermissionsMode": null, "defaultMode": null}, "sandbox": {"enabled": null}}'
```
`"{}"` is an explicit no-op. Moving the patch out of the script keeps the lib generic —
it imposes no policy until a profile asks it to.

## Files
- `scripts/claude-code-settings-rewrite.star` — the rule body.
- `presets/claude-managed-settings.toml` — the preset that stamps it.
- `setup.d/claude-managed-settings.sh` — the client-side cache reset.
- `tests/` — testkit tests (`credproxy dev test`).
