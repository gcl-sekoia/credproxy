#!/bin/bash
# setup-runner lib — the workspace setup orchestrator. A profile mounts this at
# /opt/workspace-setup.sh and calls it from its `setup` array (which credproxy
# runs as root). It drops to the workspace user, then runs every lib setup step
# mounted into /opt/setup.d/ (order = NN filename prefix). Enabling/disabling a
# lib is purely mounting/unmounting its /opt/setup.d/ fragment — no edit here.
set -euo pipefail
trap 'echo "FATAL: $0 failed at line $LINENO" >&2' ERR

# credproxy runs `setup` as root; drop to the workspace user (exposed by credproxy
# as CREDPROXY_USER) so the steps write to that user's home. -E keeps the env
# (CREDPROXY_*, SSH_AUTH_SOCK, LANG, …); -H sets HOME. Unset/root -> run as-is
# (root-based images).
if [ "$(id -u)" = 0 ] && [ -n "${CREDPROXY_USER:-}" ] && [ "$CREDPROXY_USER" != root ]; then
    exec sudo -u "$CREDPROXY_USER" -E -H bash "$0"
fi

for f in /opt/setup.d/*.sh; do
    [ -e "$f" ] || continue
    echo "setup.d: $(basename "$f")"
    bash "$f"
done
