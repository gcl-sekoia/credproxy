#!/bin/bash
# github-auth lib: make `gh` AND `git push` (over HTTPS) work off the injected
# GitHub token, without the real token ever entering the container. Pairs with the
# `github-api` (bearer, exports GITHUB_TOKEN) + `github-git` (basic, github.com)
# bindings, which SHARE one placeholder.
#
#   * gh needs nothing configured — it honors $GITHUB_TOKEN directly.
#   * git cannot read $GITHUB_TOKEN, so point its credential helper at gh's:
#     `gh auth git-credential` resolves $GITHUB_TOKEN at push time and returns
#     username=x-access-token / password=<placeholder>, which the `basic` scheme
#     then swaps for the real token on the wire.
#   * git identity comes from the authenticated account (noreply email for privacy).
#
# Runs as the workspace user (setup-runner re-execs before calling this).
set -euo pipefail

# mise shims aren't on PATH in a non-login setup step; add them so `gh` resolves.
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"

# git-over-HTTPS auth: bridge to gh's credential helper. `!gh …` resolves gh by
# PATH at push time (in the mise-activated login shell) rather than baking gh's
# version-pinned absolute path, so a gh upgrade doesn't break it.
git config --global credential."https://github.com".helper '!gh auth git-credential'
git config --global credential."https://gist.github.com".helper '!gh auth git-credential'

# git identity from the authenticated account. `gh api user` needs $GITHUB_TOKEN
# in the env — the binding exports it into login shells but not into this setup
# step, so pull the binding env from the proxy first. Best-effort: a workspace
# without the github bindings (or host gh login) still gets the helper above and
# just skips identity, rather than failing the whole setup.
eval "$(curl -fsS http://proxy.local/exports.sh || true)"
if [ -n "${GITHUB_TOKEN:-}" ] && u="$(gh api user 2>/dev/null)"; then
    git config --global user.name  "$(printf '%s' "$u" | jq -r '.name // .login')"
    git config --global user.email "$(printf '%s' "$u" | jq -r '(.id|tostring) + "+" + .login + "@users.noreply.github.com"')"
    echo "github-auth: git configured for $(git config --global user.name) <$(git config --global user.email)>"
else
    echo "github-auth: credential helper set, but no GITHUB_TOKEN / gh api unavailable —" \
         "skipped git identity. Set user.name/email yourself, or check the github" \
         "bindings + host 'gh auth login'." >&2
fi
