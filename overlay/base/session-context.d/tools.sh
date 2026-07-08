#!/usr/bin/env bash
# session-context fragment (toolchain lib): advertise the workspace tool inventory
# entries worth agent context — rows in the UNION of /opt/toolchain/tools.d/*.list that
# carry a note (col 3+) AND are actually present (`command -v`), so this can't claim a
# tool that isn't there. col1 (install source) is irrelevant here: a `-`-source
# base-image tool advertises the same as a mise-installed one. Deduped by command (col 2).
# The hardcoded "standard Unix tools" line is a DISCLAIMER, not inventory — it breaks the
# "not listed => not here" inference (agents mistook the curated list for exhaustive);
# its contents needn't be complete, so it stays a plain string, not a generated list.
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
echo "These extra CLIs are available here and preferred when shelling out (native Read/Grep/Glob still first). This list is not exhaustive — standard Unix tools (\`git\`, \`curl\`, \`jq\`, \`sed\`, …) are also present:"
printf '%s' "$rows"
