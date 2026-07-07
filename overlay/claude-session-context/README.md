# overlay: claude-session-context (lib)

Injects a short orientation note into every Claude Code session — what tools are
available, the credproxy context — so the agent starts each session already aware of
its environment. The note is assembled from drop-in **fragments**, so any lib can add
a section.

**Contributes:** the session-context runner (which scans `/opt/session-context.d/`), a
`setup.d` step (registers the hook), and a base `session-context.d` fragment (the
credproxy summary).

## Compose from a profile

```toml
{ overlay = "session-context.sh",             target = "/opt/session-context.sh" },              # the runner
{ overlay = "setup.d/claude-session-context.sh", target = "/opt/setup.d/50-claude-session-context.sh" }, # registers the hook
{ overlay = "session-context.d/credproxy.sh", target = "/opt/session-context.d/10-credproxy.sh" }, # base fragment
```
`setup-runner` runs the setup step, which registers `bash /opt/session-context.sh` as
Claude Code's `SessionStart` hook (matcher `startup|resume|compact`) in
`$CLAUDE_CONFIG_DIR/settings.json`. At session start the runner concatenates every
`*.sh` in `/opt/session-context.d/` (filename order) and prints the combined markdown.

(The runner and drop-in dir keep the plain `session-context` name — that's the generic
mechanism; the lib is its Claude-specific packaging. Same split as `setup-runner` owning
the `/opt/setup.d/` aggregator.)

## Configure — add a section

A fragment is just a script that prints markdown to stdout (one that prints nothing, or
errors, is skipped). Ship it under `session-context.d/` in any overlay and mount it into
the drop-in dir with an order prefix:
```toml
{ overlay = "session-context.d/mynote.sh", target = "/opt/session-context.d/30-mynote.sh" },
```
```sh
# session-context.d/mynote.sh
echo "# Project"
echo "Run the test suite with \`just test\`."
```
Fragments inherit the workspace env (PATH incl. mise shims, `SSH_AUTH_SOCK`, …), so they
can probe live state. The `NN-` target prefix sets the order; distinct filenames let
several overlays each contribute without colliding. (`$SESSION_CONTEXT_DIR` overrides
the drop-in dir; `$SESSION_CONTEXT_RUNNER` the runner path.)

Preview it from inside the workspace: `bash /opt/session-context.sh`.
