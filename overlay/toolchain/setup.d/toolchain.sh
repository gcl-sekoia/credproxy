#!/bin/bash
# Install a dev toolchain via mise. The tool set is data-driven and COMPOSABLE:
# the union of every /opt/toolchain/tools.d/*.list (column 1 = mise name) is
# installed, so a profile adds tools by mounting another fragment — no need to copy
# the base list. Change the toolset by editing/adding fragments, not this script.
# Runs as the workspace user (setup-runner re-execs before calling this).
set -euo pipefail

TOOLS_DIR="${TOOLCHAIN_TOOLS_DIR:-/opt/toolchain/tools.d}"
[ -d "$TOOLS_DIR" ] || { echo "toolchain: no tools dir: $TOOLS_DIR" >&2; exit 1; }

# Column 1 (mise names) across all fragments, skipping blanks and #-comments.
names=()
for lf in "$TOOLS_DIR"/*.list; do
    [ -e "$lf" ] || continue
    while read -r mise _rest; do
        case "${mise:-}" in ''|'#'*) continue;; esac
        names+=("$mise")
    done < "$lf"
done
[ "${#names[@]}" -gt 0 ] || { echo "toolchain: no tools in $TOOLS_DIR/*.list" >&2; exit 1; }
mapfile -t names < <(printf '%s\n' "${names[@]}" | awk '!seen[$0]++')   # dedup, keep first-seen order

curl -fsSL https://mise.run | sh
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"
mise use -g "${names[@]}"

# A default Python via uv (only when uv is part of the toolset).
if command -v uv >/dev/null 2>&1; then
    uv python install --default --preview-features python-install-default
fi
