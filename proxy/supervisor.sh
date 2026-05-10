#!/bin/sh
# Tiny supervisor: respawn the python sidecar process on death.
# `make reload` (-> reload.sh) kills the python child; this loop
# brings it back with freshly imported source. SIGTERM/INT shuts
# everything down cleanly.
#
# Auth token is bind-mounted from the host at /run/secrets-ro/auth.token
# and read per request by admin.py. Config on tmpfs at
# /run/secrets/config.json is written by POST /admin/config and survives
# python respawns; full container restart drops it (host re-pushes).
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
