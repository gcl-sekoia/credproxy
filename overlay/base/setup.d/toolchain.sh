#!/bin/bash
# Install a dev toolchain via mise. The tool set is data-driven and COMPOSABLE:
# the union of every /opt/toolchain/tools.d/*.list (column 1 = mise name) is
# installed, so a profile adds tools by mounting another fragment — no need to copy
# the base list. Change the toolset by editing/adding fragments, not this script.
# Run by the toolchain preset's [[setup]] step, as the workspace user.
set -euo pipefail

TOOLS_DIR="${TOOLCHAIN_TOOLS_DIR:-/opt/toolchain/tools.d}"
[ -d "$TOOLS_DIR" ] || { echo "toolchain: tools dir $TOOLS_DIR not found — nothing to install, aborting" >&2; exit 1; }

# Column 1 (install source) across all fragments: a mise package name to install.
# Skip blanks, #-comments, and `-` (a tool present by other means -- base image or
# another setup step -- that we advertise but don't install; see base.list).
names=()
for lf in "$TOOLS_DIR"/*.list; do
    [ -e "$lf" ] || continue
    while read -r mise _rest; do
        case "${mise:-}" in ''|'#'*|'-') continue;; esac
        names+=("$mise")
    done < "$lf"
done
[ "${#names[@]}" -gt 0 ] || { echo "toolchain: no tools listed in $TOOLS_DIR/*.list — nothing to install, aborting" >&2; exit 1; }
mapfile -t names < <(printf '%s\n' "${names[@]}" | awk '!seen[$0]++')   # dedup, keep first-seen order

curl -fsSL https://mise.run | sh
# The shims live under mise's data dir; the `cache` pack may redirect it (MISE_DATA_DIR),
# so derive the path from it rather than hardcoding ~/.local/share (runtime `mise activate`
# in mise.zsh already derives it — this covers the setup-step session too).
export PATH="$HOME/.local/bin:${MISE_DATA_DIR:-$HOME/.local/share/mise}/shims:$PATH"
mise use -g "${names[@]}"

# A default Python via uv (only when uv is part of the toolset).
if command -v uv >/dev/null 2>&1; then
    uv python install --default --preview-features python-install-default
fi
