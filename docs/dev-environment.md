# Developing credproxy

Setting up the dev/test loop. Using the CLI needs **no** install (`bin/credproxy`
runs the stdlib-only package directly); this is only for hacking on the repo and
running its suites.

## Setup

One command provisions the full test stack into a uv-managed `.venv`:

```sh
uv sync --group proxy
```

- Plain `uv sync` installs only the CLI + pytest — enough for the CLI suite
  (`tests/cli`), and deliberately stdlib-only otherwise (the CLI is stdlib-only
  by design). The `proxy` group adds the proxy + overlay suites' runtime deps
  (mitmproxy, aiohttp, pyyaml, starlark-pyo3, pytest-aiohttp).
- **Use the group, not an ad-hoc install.** `uv pip install -r
  proxy/requirements.txt` works once but the next `uv sync` prunes it back to the
  lock — only `--group proxy` is durable. (The two dep lists are kept in lockstep
  by `tests/cli/test_dep_sync.py`; `proxy/requirements.txt` remains the image's
  pip source.)

## Running the suites

```sh
credproxy dev test          # CLI + proxy + overlay suites (on-host when deps import)
```

`dev test` runs everything **on-host** when the proxy deps are importable (fast;
`--container` forces the image). It needs no environment setup: `tests/conftest.py`
puts the proxy dir on `sys.path` and supplies the `CREDPROXY_*` constants contract
(parsed from `proxy/Dockerfile`, the single source of truth). Run individual suites
directly the same way:

```sh
uv run pytest tests/cli -q                        # CLI suite
uv run pytest tests/ --ignore tests/cli -q        # proxy suite (conftest self-provisions)
```

## Working without a Docker daemon

On Claude Code on the web, CI sandboxes, or any container where no engine is
reachable: everything above still works — `dev test` runs on-host and never needs
the daemon. What's unavailable is `credproxy dev build`, workspace lifecycle
(`start`/`enter`/…), and `dev test --container`.

`credproxy script check` and the overlay suites are also daemon-free: they compile
`.star` scripts / drive the testkit in the on-host runtime (needs only the venv's
`starlark`/`mitmproxy`, no `CREDPROXY_*` env — the compile imports `starlark`, not
`constants`). `doctor NAME` reuses that on-host compile and skips-with-a-note when
the runtime isn't importable, so it stays daemon-free too. To run an overlay's
tests directly (rather than via `dev test`, which handles them), they need the
proxy dir on the path since they carry no conftest:

```sh
CREDPROXY_OVERLAY_PATH=/path/to/overlay \
  PYTHONPATH="$PWD/proxy:$PWD/cli" uv run pytest overlay/<name>/tests -q
```

## Expected failures without a container engine

A green run in a no-engine sandbox still shows a few failures that are purely
environmental — if these are the *only* ones, the suite is effectively green:

- `tests/cli/test_lifecycle.py::test_workspace_volumes_label_isolates_name_prefix_siblings`
  and `tests/cli/test_doctor.py::test_doctor_missing_overlay_is_failing_check` —
  need `docker` on PATH.
- `tests/cli/test_providers.py::test_sh_provider_ref_with_space_not_split` — the
  test sets an env var whose *name* contains a space; some containers' `/bin/sh`
  drops such variables before the sh provider sees them. Passes on a normal shell.

Don't "fix" these — they pass in a full environment.

## Misc

- Outbound HTTPS goes through the session's agent proxy; `uv`/`pip` work as-is.
  Never disable TLS verification.
- To automate setup in a Claude Code session, a `SessionStart` hook running
  `uv sync --group proxy` is the supported mechanism (kept manual by default so
  local sessions are unaffected).
