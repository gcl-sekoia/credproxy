#!/usr/bin/env bash
# session-context fragment (claude-code pack): when the workspace redirects Claude
# Code's config dir via $CLAUDE_CONFIG_DIR (the persist pack points it at /persist so
# config survives a recreate), announce it — otherwise the agent assumes the ~/.claude
# default, hunts there, and reports its own settings/state "missing". Inert (prints
# nothing) when CLAUDE_CONFIG_DIR is unset or already the default.
set -u
dir="${CLAUDE_CONFIG_DIR:-}"
[ -n "$dir" ] && [ "$dir" != "$HOME/.claude" ] || exit 0

echo "# Claude Code config location"
echo
echo "This workspace sets \`CLAUDE_CONFIG_DIR=$dir\`, so Claude Code's own config and state live under \`$dir/\` — \`settings.json\`, \`.claude.json\`, and the projects/history/todos dirs — **not** in the default \`~/.claude/\`. Look there when inspecting or editing your config."
