#!/usr/bin/env bash
# session-context fragment: summarize the credproxy workspace from the proxy's
# /setup — generic to any credproxy workspace. Bounded curl so a slow/absent
# proxy never stalls session start.
setup="$(curl -fsS --max-time 2 http://proxy.local/setup 2>/dev/null || true)"
{ [ -n "$setup" ] && command -v jq >/dev/null 2>&1; } || exit 0
ws="$(printf '%s' "$setup" | jq -r '.workspace // "?"')"
hosts="$(printf '%s' "$setup" | jq -r '(.intercept_hosts // []) | join(", ")')"
envs="$(printf '%s' "$setup" | jq -r '[.bindings[]? | select(.env) | .env] | join(", ")')"
echo "# credproxy"
echo "You are inside a credproxy workspace (\`$ws\`): egress is transparently proxied and"
echo "credentials are injected on the wire, so real secrets never enter this container."
[ -n "$hosts" ] && echo "TLS-intercepted host(s): $hosts"
[ -n "$envs" ]  && echo "Credential env var(s) hold *placeholders*, not real values (proxy swaps them in): $envs"
exit 0
