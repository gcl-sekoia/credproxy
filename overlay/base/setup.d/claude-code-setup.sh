#!/bin/bash
# Claude Code client config (part of the claude-code pack):
#   1. skip interactive onboarding when a non-interactive token is present,
#   2. apply baseline settings defaults (from $CLAUDE_CODE_DEFAULTS),
#   3. install shipped agent manifests (from $CLAUDE_CODE_AGENTS_DIR) into the
#      config's agents/ dir, and
#   4. install shipped skills (from $CLAUDE_CODE_SKILLS_DIR) into the config's
#      skills/ dir.
# The defaults, agents, and skills are all data (a profile mounts them —
# edit/shadow to change policy; no logic here); base ships none of them, only
# these mechanisms.
# Idempotent; run by the claude-code pack's [[setup]] step, as the workspace user.
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

# 3. Agent manifests: copy every shipped agent def into the config's agents/ dir.
#    Mounted read-only (a profile ships them), so copy to land editable, config-dir-
#    resolved files. Only shipped names are (re)written each run -- the overlay is the
#    source of truth for those; user-authored agents of other names are left untouched.
AGENTS_DIR="${CLAUDE_CODE_AGENTS_DIR:-/opt/claude-code/agents.d}"
if [ -d "$AGENTS_DIR" ]; then
    agents_dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/agents"
    mkdir -p "$agents_dest"
    for a in "$AGENTS_DIR"/*.md; do
        [ -e "$a" ] || continue                        # empty dir -> nothing shipped
        cp "$a" "$agents_dest/" \
            || { echo "claude-code: failed to install agent $(basename "$a")" >&2; exit 1; }
    done
fi

# 4. Skills: install every shipped skill into the config's skills/ dir. A skill is
#    a DIRECTORY (SKILL.md + optional references/scripts), unlike a single-file
#    agent, so each entry under skills.d/ is a subdir copied wholesale. Mounted
#    read-only (a profile ships them), so copy to land editable, config-dir-resolved
#    files. The shipped copy is replaced each run (the overlay is the source of
#    truth for those names); a user-authored skill of another name is untouched.
SKILLS_DIR="${CLAUDE_CODE_SKILLS_DIR:-/opt/claude-code/skills.d}"
if [ -d "$SKILLS_DIR" ]; then
    skills_dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
    mkdir -p "$skills_dest"
    for s in "$SKILLS_DIR"/*/; do
        [ -d "$s" ] || continue                        # empty dir -> nothing shipped
        name="$(basename "$s")"
        rm -rf "$skills_dest/$name"                     # replace the shipped copy wholesale
        cp -r "$s" "$skills_dest/$name" \
            || { echo "claude-code: failed to install skill $name" >&2; exit 1; }
    done
fi
