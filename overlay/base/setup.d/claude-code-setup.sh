#!/bin/bash
# Claude Code client config (part of the claude-code pack):
#   1. skip interactive onboarding when a non-interactive token is present, and
#   2. apply baseline settings defaults (from $CLAUDE_CODE_DEFAULTS).
# The defaults are a data file (a profile mounts one — edit/shadow it to change
# policy; no logic here). Idempotent; run by the claude-code preset's [[setup]]
# step, as the workspace user.
set -euo pipefail

DEFAULTS_FILE="${CLAUDE_CODE_DEFAULTS:-/opt/claude-code/settings-defaults.json}"

# CLAUDE_CONFIG_DIR may point at a not-yet-created dir (e.g. a subdir on a persistent
# volume), and the ~/.claude fallback can be absent too; ensure it exists before we
# write config into it.
mkdir -p "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# 1. Onboarding: mark complete so a token-authenticated workspace skips the wizard.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    claude_json="${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json"
    config="$(jq '.' "$claude_json" 2>/dev/null || echo '{}')"
    if ! printf '%s\n' "$config" | jq -e '.hasCompletedOnboarding == true' >/dev/null; then
        printf '%s\n' "$config" | jq '.hasCompletedOnboarding = true' > "$claude_json"
    fi
fi

# 2. Settings defaults: merge under the existing settings (existing values win, so
#    the defaults only fill absent keys). `$d[0] * .` = deep-merge, current wins.
if [ -f "$DEFAULTS_FILE" ]; then
    settings_file="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
    settings="$(jq '.' "$settings_file" 2>/dev/null || echo '{}')"
    tmp="$(mktemp)"
    printf '%s\n' "$settings" | jq --slurpfile d "$DEFAULTS_FILE" '$d[0] * .' > "$tmp" \
        && mv "$tmp" "$settings_file" \
        || { rm -f "$tmp"; echo "claude-code: failed to write $settings_file" >&2; exit 1; }
fi
