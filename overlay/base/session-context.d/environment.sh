#!/usr/bin/env bash
# session-context fragment (claude-code pack): tell the agent it's inside a container and
# which runtime, so it can tailor host-shaped advice that differs by engine (podman vs
# docker) and rootless-ness (userns/uid mapping, binding low ports, what "root" means).
# Everything is PROBED live and stated only when true (the fragment rule): engine from the
# container markers, rootless from a non-identity userns map. Not containerized -> silent.
set -u

engine=""; rootless=""
if [ -e /run/.containerenv ] || [ "${container:-}" = "podman" ]; then
    engine="podman"
    # Rootless podman runs the container in a user namespace, so /proc/self/uid_map is a
    # non-identity map; a rootful container gets the identity map `0 0 4294967295`.
    if [ -r /proc/self/uid_map ] && ! grep -qE '^[[:space:]]*0[[:space:]]+0[[:space:]]+4294967295[[:space:]]*$' /proc/self/uid_map; then
        rootless=1
    fi
elif [ -f /.dockerenv ] || [ "${container:-}" = "docker" ]; then
    engine="docker"
fi
[ -n "$engine" ] || exit 0   # can't confirm a container -> say nothing

label="$engine"; [ -n "$rootless" ] && label="rootless $engine"

echo "# This is a ${label} container"
echo
echo "You are running inside a **$engine** container managed by credproxy — **not on the user's host machine**. It is disposable and isolated: it can be rebuilt from its image at any time, and nothing here touches the host."
if [ "$engine" = "podman" ]; then
    if [ -n "$rootless" ]; then
        echo "Runtime note: this is **rootless podman** — inside-container \"root\" is a mapped unprivileged host user (see \`/proc/self/uid_map\`), binding ports <1024 and some mount/userns operations behave differently than on Docker, and \`docker\`-specific advice may need a \`podman\` equivalent."
    else
        echo "Runtime note: the engine is **podman**, so \`docker\`-specific advice may need a \`podman\` equivalent."
    fi
fi
