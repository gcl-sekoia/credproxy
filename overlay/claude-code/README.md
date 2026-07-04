# overlay: claude-code

Patches the server-managed settings Claude Code reads from `api.anthropic.com`, so a
workspace sees a modified org policy.

## Apply

    credproxy workspace NAME preset add claude-code

This stamps the `claude-code-settings-rewrite` rule (hidden, response-phase) scoped to
`api.anthropic.com` and `/api/claude_code/settings`. It carries no credential.

## Policy

With no per-rule config the rule applies the `DEFAULT_PATCH` in
`scripts/claude-code-settings-rewrite.star`, an RFC 7386 merge patch that deletes the
env-scrub flag and the permission/sandbox gates the org pushes. To change it for one
workspace, set `[rule.params].settings_patch` on the stamped rule to a JSON merge-patch
string; `"{}"` leaves the settings untouched.

## Files

- `scripts/claude-code-settings-rewrite.star` — the rule body.
- `presets/claude-code.toml` — the preset that stamps the rule.
- `tests/` — testkit tests, run by `credproxy dev test`.
