#!/bin/bash
# codex pack: authenticate OpenAI's Codex CLI off the injected API key, without the
# real key ever entering the container. The `codex-api` bearer part swaps the
# placeholder in $OPENAI_API_KEY on api.openai.com.
#
# Codex resolves an API key from either $OPENAI_API_KEY (set by the part's `env`) or
# ~/.codex/auth.json (seeded here). Seeding both covers every Codex version: the env
# var alone works for versions that read it directly, and `codex login --with-api-key`
# writes auth.json for versions that require an explicit login. Both carry the SAME
# placeholder, so the two methods can't fight (the known "run codex logout first"
# footgun only bites when the key differs).
#
# Runs as the workspace user with the binding env injected (so $OPENAI_API_KEY is
# already the placeholder — no need to pull /exports.sh). Idempotent.
set -euo pipefail

# mise shims aren't on PATH in a non-login setup step; add them so `codex` resolves.
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"

# Best-effort: a workspace without the codex CLI (or without the api part) still
# gets $OPENAI_API_KEY in its login shell — just skip seeding auth.json rather than
# failing the whole setup. `codex login --with-api-key` reads the key from stdin.
if command -v codex >/dev/null 2>&1 && [ -n "${OPENAI_API_KEY:-}" ]; then
    if printf '%s' "$OPENAI_API_KEY" | codex login --with-api-key >/dev/null 2>&1; then
        echo "codex: authenticated with the injected API key (auth.json seeded)"
    else
        echo "codex: 'codex login --with-api-key' failed — \$OPENAI_API_KEY is still" \
             "set in the login shell, so a version that reads the env var directly" \
             "will work; check the codex-api binding otherwise." >&2
    fi
else
    echo "codex: codex CLI not found or \$OPENAI_API_KEY unset — skipped auth.json" \
         "seeding. Install the CLI (toolchain tools.d: npm:@openai/codex) and check" \
         "the codex-api binding." >&2
fi
