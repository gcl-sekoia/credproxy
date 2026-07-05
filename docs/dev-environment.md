# Developing credproxy without Docker (cloud / sandboxed environments)

Notes for working on this repo in an environment without a Docker daemon —
e.g. Claude Code on the web, CI sandboxes, or any container where `docker`
exists but no daemon is reachable. Everything here was verified on a
Claude Code web session (Debian-based container, python3.12, root).

## What doesn't work, and what replaces it

| Normally | Without a daemon |
|---|---|
| `credproxy dev test` (container fallback) | run pytest directly via a venv (below) |
| `credproxy dev build`, workspace lifecycle | unavailable — static work + tests only |
| proxy suite inside the image (ENV baked in) | export the ENV contract by hand (below) |

## One-time setup: a venv with the proxy runtime deps

The system python has none of the deps, and installing to it fails anyway:
mitmproxy pins `pyperclip==1.9.0` (sdist-only), whose build breaks under the
Debian-patched system setuptools (`AttributeError: install_layout`). A clean
venv sidesteps that patch entirely — inside a venv the same sdist builds fine.

```sh
python3 -m venv ~/.venv-credproxy
~/.venv-credproxy/bin/pip install -q --upgrade pip setuptools wheel
~/.venv-credproxy/bin/pip install -q -r proxy/requirements.txt   # includes pytest
```

Do NOT `pip install` to the system python; that's where the pyperclip build
failure lives.

## Running the suites

CLI suite (host-side, needs only pytest):

```sh
~/.venv-credproxy/bin/python -m pytest tests/cli -q
```

Proxy suite on-host: `proxy/constants.py` reads the `CREDPROXY_*` values from
the process environment with no defaults (in the image they come from the
Dockerfile `ENV` declarations — the single source of truth; keep these in sync
with `proxy/Dockerfile` if they ever change):

```sh
env CREDPROXY_MITMPROXY_UID=31337 \
    CREDPROXY_HTTP_PORT=39998 \
    CREDPROXY_PROXY_PORT=39999 \
    CREDPROXY_SENTINEL_IP=169.254.1.1 \
    CREDPROXY_TOKEN_PATH=/run/secrets-ro/auth.token \
    CREDPROXY_TMPFS=/run/secrets \
    PYTHONPATH="$PWD/proxy" \
    ~/.venv-credproxy/bin/python -m pytest tests/ --ignore tests/cli -q
```

`credproxy dev test`'s on-host path works too if you run it *with the venv
python* (it checks imports in the running interpreter) and export the env vars
above — but the two direct pytest commands are simpler and equivalent.

## Known environmental failure (not a real bug)

`tests/cli/test_providers.py::test_sh_provider_ref_with_space_not_split`
fails in these containers: the test sets an env var whose *name* contains a
space (`My Token`), and the container's `/bin/sh` drops such variables from
the environment it passes to children, so the sh provider under test never
sees it. It passes on typical dev machines. If it is the only CLI-suite
failure, the suite is effectively green — don't chase it, don't "fix" it.

Reference baseline (2026-07, for calibration): CLI suite 708 passed /
1 failed (the above) / 3 skipped; proxy suite 325 passed.

## Misc

- Outbound HTTPS goes through the session's agent proxy; `pip` works as-is.
  Never disable TLS verification — see `/root/.ccr/README.md` in-session.
- If this setup should become automatic, a Claude Code `SessionStart` hook
  running the venv setup is the supported mechanism; so far it's manual to
  keep local sessions unaffected.
