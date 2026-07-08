#!/usr/bin/env bash
# Claude Code SessionStart hook — extensible session context.
#
#   --install : register this script as the SessionStart hook in
#               $CLAUDE_CONFIG_DIR/settings.json (idempotent). Run from setup.
#   (no args) : the hook body — run every fragment in the drop-in dir and emit
#               their combined markdown as the session context.
#
# Extensible: any overlay can contribute a fragment (a script that prints markdown
# to stdout); a profile mounts it into the drop-in dir ($SESSION_CONTEXT_DIR,
# default /opt/session-context.d), ordered by filename. See README.md.
set -uo pipefail
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"

DIR="${SESSION_CONTEXT_DIR:-/opt/session-context.d}"

if [ "${1:-}" = "--install" ]; then
    # readlink -f so the persisted hook command is the absolute mount path,
    # regardless of how this script was invoked.
    self="$(readlink -f "$0")"
    settings_file="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
    cmd="bash $self"
    settings="$(jq '.' "$settings_file" 2>/dev/null || echo '{}')"
    tmp="$(mktemp)"
    # Idempotent: strip any prior copy of our hook (by command), then re-add.
    printf '%s\n' "$settings" \
      | jq --arg cmd "$cmd" '
          .hooks.SessionStart = (
            ((.hooks.SessionStart // [])
              | map(select([.hooks[]?.command] | index($cmd) | not)))
            + [{matcher:"startup|resume|compact", hooks:[{type:"command",command:$cmd}]}])' \
      > "$tmp" && mv "$tmp" "$settings_file" \
      || { rm -f "$tmp"; echo "session-context: failed to update $settings_file" >&2; exit 1; }
    echo "session-context: SessionStart hook -> $cmd"
    exit 0
fi

# Hook body: concatenate fragments (blank line between), skipping empty/failing ones.
[ -d "$DIR" ] || exit 0
shopt -s nullglob
first=1
for f in "$DIR"/*.sh; do
    out="$(bash "$f" 2>/dev/null)" || out=""
    [ -n "$out" ] || continue
    [ "$first" = 1 ] || echo
    printf '%s\n' "$out"
    first=0
done
exit 0
