#!/bin/sh
# Proxy entrypoint. Runs as root: installs iptables rules in this netns
# (which becomes the shared netns for the workspace container), then drops
# to the mitmproxy uid and execs python directly. Python is PID 1 and
# handles SIGHUP for in-place reload.
#
# Constants come from the Dockerfile's `ENV` block (inherited through
# the process environment); see proxy/Dockerfile for the full list.
set -eu

# ORDERING IS LOAD-BEARING: the iptables rules below MUST be installed before
# the `exec ... python` at the bottom of this file. That ordering is the whole
# reason `/health` (which observes only that the listeners accept) can imply
# capture-readiness: a listener physically cannot come up before the rules that
# redirect traffic to it, so `/health` green => capture active. Under `set -e`
# any rule failure aborts before the exec. Do NOT move the python exec above the
# iptables block, and do NOT install rules after starting python. A guard test
# (tests/test_entrypoint.py) asserts the last `iptables` line precedes the exec.
echo "[entrypoint] installing iptables rules"

# Bind sentinel address; gives an implicit route via lo. Idempotent.
ip addr add "${CREDPROXY_SENTINEL_IP}/32" dev lo 2>/dev/null || true

# Make proxy.local resolve. Workspace containers joined via
# --network=container: inherit /etc/hosts from this container.
if ! grep -q "proxy.local" /etc/hosts; then
    echo "${CREDPROXY_SENTINEL_IP} proxy.local" >> /etc/hosts
fi

# nat OUTPUT — order matters.
# 1. Sentinel:80 -> merged HTTP API (admin + bootstrap routes).
iptables -t nat -A OUTPUT -d "$CREDPROXY_SENTINEL_IP" -p tcp --dport 80 \
    -j REDIRECT --to-port "$CREDPROXY_HTTP_PORT"
# 2. Don't loop mitmproxy's own outbound back into itself.
iptables -t nat -A OUTPUT -m owner --uid-owner "$CREDPROXY_MITMPROXY_UID" -j RETURN
# 3. Don't touch workspace-internal loopback.
iptables -t nat -A OUTPUT -d 127.0.0.0/8 -j RETURN
# 4. Send everything else to mitmproxy.
iptables -t nat -A OUTPUT -p tcp -j REDIRECT --to-port "$CREDPROXY_PROXY_PORT"

# filter OUTPUT — force HTTP/3 to fall back to TCP.
iptables -A OUTPUT -p udp --dport 443 -j DROP

# IPv6: not supported in v1; drop everything. May fail in environments
# without ip6tables; non-fatal.
ip6tables -P OUTPUT  DROP 2>/dev/null || true
ip6tables -P INPUT   DROP 2>/dev/null || true
ip6tables -P FORWARD DROP 2>/dev/null || true

# Token is bind-mounted at $CREDPROXY_TOKEN_PATH (mode 0644 on the host so
# uid 31337 can read it through the bind mount). Python reads it fresh
# per request -- no staging copy needed.
if [ ! -e "$CREDPROXY_TOKEN_PATH" ]; then
    echo "[entrypoint] $CREDPROXY_TOKEN_PATH missing -- did you bind-mount the host token?" >&2
    exit 1
fi

echo "[entrypoint] dropping to uid $CREDPROXY_MITMPROXY_UID, exec python"
# setpriv preserves env, so HOME would still point at /root. mitmproxy reads
# ~/.mitmproxy as its confdir; force it to mitmuser's home. Python is PID 1
# inside the netns; SIGHUP triggers an in-place re-exec for `credproxy reload`.
# PYTHONDONTWRITEBYTECODE keeps .pyc out of the bind-mounted /opt/proxy.
exec env HOME=/home/mitmuser PYTHONDONTWRITEBYTECODE=1 setpriv \
    --reuid="$CREDPROXY_MITMPROXY_UID" \
    --regid="$CREDPROXY_MITMPROXY_UID" \
    --clear-groups \
    python -u "$CREDPROXY_SOURCE/main.py"
