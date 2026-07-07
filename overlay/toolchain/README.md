# overlay: toolchain (lib)

Installs a set of command-line dev tools into the workspace with
[mise](https://mise.jdx.dev), and tells each Claude Code session which of them are
available. The tool set is a plain text list, so changing it takes no code edits.

**Contributes:** a `setup.d` step (the mise install), a `session-context.d` fragment
(the "tools available" note), and `tools.d` data (the list).

## Compose from a profile

```toml
{ overlay = "setup.d/toolchain.sh", target = "/opt/setup.d/10-toolchain.sh" },        # installs the tools
{ overlay = "tools.d/base.list",    target = "/opt/toolchain/tools.d/10-base.list" },  # the tool list
{ overlay = "session-context.d/tools.sh", target = "/opt/session-context.d/20-tools.sh" }, # the session note
```
`setup-runner` runs the install step automatically. It reads the list from
`/opt/toolchain/tools.d/` (override with `$TOOLCHAIN_TOOLS_DIR`).

## Make the tools available on PATH

The setup step *installs* mise (via `mise.run`) and the tools, but they only land on an
interactive shell's `PATH` once that shell **activates** mise. Activation is shell
config, so it lives in the profile, not this lib — 50-example does it in its zsh drop-in
(`omz-custom/profile.zsh`):
```sh
eval "$(mise activate zsh)"
```
A profile that composes `toolchain` must run the equivalent for whatever shell it uses
(`mise activate bash`, …), or the installed tools won't be found in a session. (The
setup step manages its own `PATH` for the install, so this only concerns interactive use.)

## Configure — the tool list

Each line of a `tools.d/*.list` file is one tool: **mise-name · command · description**.

```
# <mise-name>  <command>  [description]
uv          uv        Python envs/deps/run (prefer over pip & venv)
ripgrep     rg        ripgrep — fast content search
claude      claude
```
- **column 1** is what `mise use -g` installs.
- **columns 2–3** drive the session note: the command probed with `command -v`, and
  the text shown for it. A tool with **no description** (like `claude`) is installed
  but not listed in the note.
- `#` comments and blank lines are ignored.

The installer and the note read the **union of every** `tools.d/*.list`, so the set is
composable:

- **Add tools** — ship another fragment and mount it alongside the base (no need to copy it):
  ```toml
  { overlay = "tools.d/rust.list", target = "/opt/toolchain/tools.d/20-rust.list" },
  ```
  ```
  # tools.d/rust.list
  rust  cargo  Rust toolchain (cargo, rustc)
  ```
- **Replace the base** — mount only your own fragment (don't mount `base.list`).
- **Remove a base tool** — the union is additive; replace the base rather than dropping a fragment.
