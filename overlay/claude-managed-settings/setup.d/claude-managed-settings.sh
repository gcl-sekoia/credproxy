#!/bin/sh
# claude-managed-settings lib — client-side companion to the settings-rewrite rule.
# Delete Claude Code's cached server-managed settings so the next fetch goes
# through the proxy and the rule rewrites it (no stale pre-rewrite copy lingers).
# Run from a profile's setup.sh. Harmless without the rule (just forces a refetch).
rm -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/remote-settings.json"
