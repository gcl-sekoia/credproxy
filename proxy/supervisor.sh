#!/bin/sh
# Tiny supervisor: respawn the python sidecar process forever.
# `make reload` (-> reload.sh) kills the python child; this loop
# brings it back with freshly imported source AND a fresh read of the
# secrets file. SIGTERM/INT shuts everything down cleanly.
#
# Secrets pipeline:
#   1. docker run -i hands stdin to this process. We slurp it once
#      and write to /run/secrets/secrets.json (tmpfs, mode 0600).
#   2. Each python spawn reads that file as its stdin.
#   3. add_secret.py (called via `docker exec -i --user 31337` from
#      `make add-secret`) atomically rewrites the file at runtime; the
#      next reload picks up the new state without container restart.
#
# The file lives on tmpfs (--tmpfs /run/secrets in `make up`) so secrets
# never hit persistent disk. Inside the container, only mitmuser
# (uid 31337) can read it. From outside, reading requires root or
# CAP_SYS_PTRACE on the container.
set -u

export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

SECRETS_FILE=/run/secrets/secrets.json

# Initial population from stdin. EOF -> empty file -> python sees {}.
( umask 077; cat > "$SECRETS_FILE" )

shutting_down=0
child=""

handle_term() {
    shutting_down=1
    [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
}

trap handle_term TERM INT

while :; do
    python -u /opt/proxy/main.py < "$SECRETS_FILE" &
    child=$!
    wait "$child" 2>/dev/null || true

    if [ "$shutting_down" = "1" ]; then
        exit 0
    fi

    echo "[supervisor] python exited; restarting"
    sleep 0.3
done
