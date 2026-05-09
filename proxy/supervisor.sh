#!/bin/sh
# Tiny supervisor: respawn the python sidecar process on death.
# `make reload` (-> reload.sh) kills the python child; this loop
# brings it back with freshly imported source. SIGTERM/INT shuts
# everything down cleanly.
#
# State (auth token + config) is persisted by the python process to
# /run/secrets/{auth.token,config.json} (tmpfs, mode 0400 owned by
# mitmuser/uid 31337). Respawned python reads both at startup; if
# either is missing the proxy is in TOFU mode and the host CLI's first
# push-config call claims it.
set -u

export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

shutting_down=0
child=""

handle_term() {
    shutting_down=1
    [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
}

trap handle_term TERM INT

while :; do
    python -u /opt/proxy/main.py &
    child=$!
    wait "$child" 2>/dev/null || true

    if [ "$shutting_down" = "1" ]; then
        exit 0
    fi

    echo "[supervisor] python exited; restarting"
    sleep 0.3
done
