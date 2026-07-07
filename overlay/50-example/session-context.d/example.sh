#!/usr/bin/env bash
# Example session-context fragment for this profile — a copy-me template. A fragment is
# any script that prints MARKDOWN to stdout; at Claude Code SessionStart the runner
# concatenates every /opt/session-context.d/*.sh (in NN-prefix order) into the
# orientation note. Printing nothing (like this template) is skipped, so it's inert
# until you fill it in.
#
# Fragments run with the workspace env (PATH incl. mise shims, SSH_AUTH_SOCK, the binding
# env vars, …), so you can probe live state and print conditionally — see the lib's
# credproxy.sh (reads the proxy's /setup) or toolchain's tools.sh for real examples.
#
# To use: uncomment the heredoc below and edit its markdown, then uncomment this
# fragment's mount in workspace.template.toml (the `session-context.d/example.sh` line).
# A quoted heredoc (<<'EOF') keeps backticks and $ literal, so markdown survives verbatim.

# cat <<'EOF'
# # Project
# Run the tests with `just test`; the entrypoint is `src/main.py`.
# EOF
