#!/usr/bin/env bash
# session-context fragment (toolchain lib): list installed CLIs from the UNION of
# /opt/toolchain/tools.d/*.list — the same fragments the installer reads, so the
# inventory can't drift from what's installed. Advertises each tool (col 2 = command,
# col 3+ = description) that has a description and is present; deduped by command.
set -u
TOOLS_DIR="${TOOLCHAIN_TOOLS_DIR:-/opt/toolchain/tools.d}"
[ -d "$TOOLS_DIR" ] || exit 0

rows=""
declare -A seen
for lf in "$TOOLS_DIR"/*.list; do
    [ -e "$lf" ] || continue
    while read -r _mise cmd desc; do
        case "${_mise:-}" in ''|'#'*) continue;; esac
        [ -n "${desc:-}" ] || continue                 # no description -> not advertised
        [ -n "${seen[$cmd]:-}" ] && continue           # already advertised by an earlier fragment
        command -v "$cmd" >/dev/null 2>&1 || continue  # only if actually installed
        seen[$cmd]=1
        rows+="- \`$cmd\` — $desc"$'\n'
    done < "$lf"
done

[ -n "$rows" ] || exit 0
echo "# This workspace's environment"
echo
echo "Extra CLIs installed here and preferred when shelling out (native Read/Grep/Glob still first):"
printf '%s' "$rows"
