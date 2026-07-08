#!/usr/bin/env bash
# session-context fragment: orient an agent to the credproxy workspace it runs in, from
# the proxy's live /setup — generic to any credproxy workspace. Bounded curl so a slow
# proxy never stalls session start. If /setup doesn't answer but proxy.local resolves,
# we're in a credproxy netns with the proxy down — say egress is broken rather than
# vanishing silently (the one moment orientation matters most). The prose states the two
# things an agent must do differently: treat non-listed hosts as normal passthrough, and
# recognize a proxy-CA TLS error (fix via /env.sh, never `-k`). SSH egress is deliberately
# NOT flagged — verified to work (mitmproxy relays the non-TLS connection transparently).
setup="$(curl -fsS --max-time 2 http://proxy.local/setup 2>/dev/null || true)"
if [ -z "$setup" ]; then
    if command -v getent >/dev/null 2>&1 && getent hosts proxy.local >/dev/null 2>&1; then
        printf '# credproxy\n\nThis is a credproxy workspace but its proxy is not answering — egress is likely broken; expect connection/TLS errors until it recovers (retry `curl http://proxy.local/setup`).\n'
    fi
    exit 0
fi
command -v jq >/dev/null 2>&1 || exit 0

ws="$(printf '%s' "$setup"    | jq -r '.workspace // ""')"
hosts="$(printf '%s' "$setup" | jq -r '(.intercept_hosts // []) | join(", ")')"
envs="$(printf '%s' "$setup"  | jq -r '[.bindings[]? | select(.env) | .env] | join(", ")')"
rules="$(printf '%s' "$setup" | jq -r '
    [.rules[]?
       | .name + " (" + .action
         + (if .methods then " " + (.methods | join("/")) else "" end)
         + (if .path then " " + .path else "" end) + ")"] | join(", ")')"

echo "# credproxy"
echo
name_clause=""; [ -n "$ws" ] && name_clause=" named \`$ws\`"
echo "You are inside a credproxy workspace${name_clause}. Your egress runs through a proxy container."
if [ -n "$hosts" ]; then
    echo "TLS is intercepted only to: $hosts — **every other host is untouched passthrough and works normally**."
    [ -n "$envs" ] && echo "These env vars hold *placeholders* the proxy swaps for the real secret in transit: $envs. Use them exactly like real tokens — requests authenticate normally. The placeholders are inert and safe to print; the real values never enter this container, so don't overwrite these vars or try to recover the real values."
    [ -n "$rules" ] && echo "Traffic rules on those hosts (requests may be blocked, answered, or rewritten): $rules"
    echo "A TLS/certificate error on one of these hosts means the tool isn't trusting the proxy CA (common with Node/Python/Rust/AWS SDKs that bundle their own certs) — fix with \`eval \"\$(curl -s http://proxy.local/env.sh)\"\` **in the same shell as the failing command** (exported vars don't persist across separate tool calls), never by disabling TLS verification."
else
    echo "No hosts are currently intercepted — all egress is untouched passthrough."
fi
echo "Live details (intercepted hosts, credential bindings, traffic rules): \`curl http://proxy.local/setup\`."
exit 0
