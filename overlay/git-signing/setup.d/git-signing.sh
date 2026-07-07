#!/bin/bash
# git commit signing over a forwarded, dedicated ssh-agent (git-signing lib).
#
# Two modes:
#   --print-key : print `key::<pubkey>` read from the forwarded agent, for git's
#                 gpg.ssh.defaultKeyCommand (git runs it fresh on every commit).
#                 Exits 1 when the agent holds no key.
#   (no args)   : configure git for ssh commit signing (idempotent). Called by
#                 a profile's setup.sh at container setup, and safe to re-run by
#                 hand from a live workspace to (re)configure or troubleshoot.
#
# Signing is DYNAMIC: user.signingkey stays UNSET and the key is derived from the
# agent at commit time via defaultKeyCommand. So loading the key AFTER the
# container started just works (no reconfigure needed), and a signed commit made
# while the agent is empty fails loudly instead of going silently unsigned.
set -euo pipefail

if [ "${1:-}" = "--print-key" ]; then
    # stdout must carry ONLY the `key::` line (git reads it as the signing key);
    # the no-key hint goes to stderr, which git surfaces as a `warning:` on the
    # failed commit — pointing at the host-side helper.
    key=$(ssh-add -L 2>/dev/null | grep -m1 '^ssh-') || key=""
    if [ -z "$key" ]; then
        echo -e "\n" \
                "\n  credproxy: no signing key in the forwarded ssh-agent." \
                "\n  On the HOST run 'credproxy-signing-agent' (loads the key), then retry." >&2
        exit 1
    fi
    printf 'key::%s\n' "$key"
    exit 0
fi

# No forwarded agent -> this workspace doesn't want signing; leave git untouched
# (so ordinary commits keep working). Re-run this after forwarding an agent.
if [ -z "${SSH_AUTH_SOCK:-}" ]; then
    echo "git-signing: no agent forwarded (SSH_AUTH_SOCK unset); git signing left off." >&2
    exit 0
fi

# Absolute path to this script, so the persisted defaultKeyCommand (re-run by git
# from an arbitrary cwd at commit time) and the self-call below don't hardcode the
# mount path. readlink -f resolves it whether $0 was absolute or relative.
self="$(readlink -f "$0")"

git config --global gpg.format ssh
git config --global gpg.ssh.defaultKeyCommand "bash '$self' --print-key"
git config --global commit.gpgsign true
git config --global tag.gpgsign true
git config --global --unset user.signingkey 2>/dev/null || true   # force defaultKeyCommand

if key=$(bash "$self" --print-key 2>/dev/null); then
    # Enable LOCAL verification (git log --show-signature / verify-commit) by
    # mapping this key to an allowed signer. Regenerated on every run, so the
    # by-hand re-run after loading/rotating the key refreshes it too. Principal is
    # the git identity when set (nicer "Good signature for <email>"), else "*" —
    # this step can run before the identity is configured, and the file only ever
    # holds this one dedicated key, so "*" just means "don't filter by email".
    signers="${XDG_CONFIG_HOME:-$HOME/.config}/git/allowed_signers"
    principal="$(git config --global user.email 2>/dev/null || true)"
    mkdir -p "$(dirname "$signers")"
    printf '%s %s\n' "${principal:-*}" "${key#key::}" > "$signers"
    git config --global gpg.ssh.allowedSignersFile "$signers"
    echo "git-signing: configured; commits sign with the agent key, local verify enabled."
else
    echo "git-signing: configured, but the agent holds no key yet — load it on the host" \
         "(ssh-add), then re-run this script to enable signing + verification." >&2
fi
