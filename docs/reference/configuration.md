[← docs index](../README.md) · [Concepts](../concepts.md)

# Workspace configuration

This is the reference for the workspace TOML file — every field, and the
commands that edit it. Reach for it when you want the exact name or shape of a
setting the guide mentioned in passing, or when you would rather edit the file
directly than run a command. New to credproxy? Follow [the
guide](../guide/01-install.md) first; this page assumes you already know what a
workspace and a binding are.

A workspace is defined by a single **hand-owned** TOML file — the source of
intent. Imperative commands (`workspace create`, `binding add`, …) are sugar
that only ever append a whole block at the end of the file or delete a whole
named block, so every change they make is something you could have typed in
yourself and your comments are never touched. Generated machine state — the
inert placeholders and the snapshot of each `[[preset]]` reference's expansion —
lives beside the file in a regenerable `lock.json` (described below), never
inside the TOML; deleting the lock is safe (placeholders regenerate, presets
re-expand from the current definitions).

This doc covers both paths: the **file format** and the **CLI** that edits and
applies it. For the netns/bootstrap side of a running workspace see
[`workspace.md`](workspace.md); for writing credential backends see
[`providers.md`](providers.md).

## Where config lives

| Path | Holds |
|---|---|
| `$XDG_CONFIG_HOME/credproxy/workspaces/<name>.toml` | the workspace config (this doc). Default `~/.config/credproxy/workspaces/`. The file existing **is** the workspace existing. |
| `$XDG_CONFIG_HOME/credproxy/injectors/<name>.toml` | your injector definitions (shadow the builtin ones) |
| `$XDG_CONFIG_HOME/credproxy/providers/<name>` | your provider executables (shadow the builtin ones) |
| `$XDG_STATE_HOME/credproxy/workspaces/<name>/` | runtime state — `auth.token`, `lock.json` (machine-owned canonical JSON: binding placeholders + preset expansion snapshots + an `applied` section holding the last-pushed spec/bindings/rules metadata, the pushed config generation, and the setup-completed container id), `lifecycle.lock` (the per-workspace flock), session pidfiles. Not hand-edited. Default `~/.local/state/credproxy/`. |
| `$XDG_STATE_HOME/credproxy/default-workspace` | the current default-workspace pointer (loose surface) |

The config dir is editable; the state dir is owned by the tool. Point the two
XDG variables elsewhere to keep separate sets of workspaces (this is also how
the tests isolate themselves).

## The file format

A complete example, with every key shown:

```toml
# Workspace container image. The default ships a non-root sudo user (vscode).
image = "mcr.microsoft.com/devcontainers/base:ubuntu"

# Sugar for a managed volume named `home` mounted here (persistent, image-seeded).
# Optional — omit for an ephemeral home. Point it at the user's home so the volume
# is their home (the default image pre-creates /home/vscode owned by vscode).
home = "/home/vscode"

# User that `enter` runs as (docker exec -u). Must exist in the image (the
# default image ships `vscode`, uid 1000, passwordless sudo) or be created by
# `setup` (which runs as root). Exec-only — no recreate.
user = "vscode"

# Directory `enter` starts in (the workspaceFolder analog). Defaults to `home`.
workdir = "/code"

# HOST directory this workspace is "for". On the loose surface (`credp`), a
# command with no NAME run from at/under this path resolves to this workspace.
# Pure CLI resolution metadata — never touches the container. Usually set via
# `create --here`/`--dir` or `bind-dir` rather than by hand.
directory = "/home/me/src/myproj"

# Make `user` own the bind mounts without changing host ownership; credproxy
# picks the per-runtime lever. No-op unless `user` is set. Recreates on change.
map_host_user = true

# Escape hatch: extra flags spliced into `docker exec` for `enter`.
# credproxy keeps control of -i/-t/-d. Exec-only.
exec_flags = ["--workdir", "/srv"]

# Things mounted in. A string is a host bind ("SRC:DST[:ro]"); a table is a typed
# mount — a managed `volume` (persistent, image-seeded, ownership-clean) or an
# `overlay` mount (a path relative to an overlay dir, for static files).
mounts = [
  "~/code:/code",                                          # host bind
  "~/.gitconfig:/home/vscode/.gitconfig:ro",               # host bind, read-only
  { volume = "cache", target = "/home/vscode/.cache" },    # managed volume
  { overlay = "gitconfig", target = "/home/vscode/.gitconfig" },  # overlay file
]

# Environment variables set in the workspace container.
env = { GH_DEBUG = "1", TZ = "UTC" }

# Commands run once, after the container is (re)created. A mixed array: a plain
# string runs as root via `sh -lc` with no injected env (unchanged); a table
# runs as the workspace `user` (or root) in `(order, position)` order, with the
# binding env (each binding's placeholder under its env var) and correct HOME.
setup = [
  "curl -fsSL http://proxy.local/bootstrap.sh | sh",       # string: root, as today
  { run = "apt-get install -y build-essential", user = "root", order = 10 },
  { run = "gh auth setup-git", order = 45 },               # workspace user (default)
]

# Stop the workspace when the last `enter` session exits. Off by default.
auto_stop = true

# Credential bindings — zero or more. See "Bindings" below.
[[binding]]
name        = "github-api"        # REQUIRED — hand-authored (`binding add` writes it)
injector    = "bearer"
provider    = "env"
secret      = "GITHUB_TOKEN"      # single-slot: a bare ref
hosts       = ["api.github.com"]
# placeholder — omit it: the generated value lives in the lockfile, not here.
# Set it explicitly only to pin a specific value (then it wins and stays out of
# the lock).
env         = "GITHUB_TOKEN"      # inherits the injector's hint; `false` suppresses

# A multi-slot secret uses an inline table (slot -> provider ref) instead of a
# bare string; the scheme declares which slots it needs. E.g. a sigv4 binding
# (sign family — no placeholder; the proxy re-signs each request):
[[binding]]
name     = "aws-sts"
injector = "sigv4"
provider = "env"
secret   = { access_key_id = "AWS_ACCESS_KEY_ID", secret_access_key = "AWS_SECRET_ACCESS_KEY" }
hosts    = ["sts.amazonaws.com"]
```

### Container settings

| Key | Type | Default | Notes |
|---|---|---|---|
| `image` | string | **required** | The workspace container image — your own image; never modified or privileged. `credproxy create` scaffolds this to a devcontainers base that ships a non-root sudo user (`vscode`, uid 1000) plus curl + ca-certificates (so the bootstrap and a non-root shell work with no setup), along with the matching `user`/`home`/`map_host_user`. To run a different image, edit `image` here (and `user`/`home` to match — the scaffold comments explain). There is no built-in default: `image` is mandatory, and omitting it is an error. |
| `home` | string | _(none)_ | Sugar for a managed volume named `home` mounted at this (absolute) path — the persistent, image-seeded home. **Optional**: omit it for an ephemeral home (the image's, lost on recreate). The volume survives stop/start and recreate; wiped by `recreate --reset-volume home` or `delete`. Also the default `workdir`. |
| `mounts` | list | `[]` | Things mounted into the workspace. A **string** is a host **bind** (`"SRC:DST[:ro]"`; `~` expanded on `SRC`, which must be an existing absolute path; `DST` absolute). A **table** is a typed mount with exactly one of: `{ bind = "SRC", target = "/dst", readonly = false }` (host bind), `{ volume = "NAME", target = "/dst" }` (a managed named volume — persistent, image-seeded, ownership-clean; namespaced per workspace; great for caches), or `{ overlay = "REL", target = "/dst" }` (a path **relative to an overlay dir**, searched in declared order and confined within the winning overlay, read-only by default — for static files shipped with a fork; see [overlays.md](../advanced/overlays.md)). No two mounts may share a `target`; no two volumes a name (`home` is reserved for the home sugar). Managed volumes can also be added with `credproxy workspace NAME mount add --volume VOL --target PATH [--ro] [--preserve]` (a surgical TOML edit; `--volume home` writes the home sugar) — see *Adding a volume, keeping existing data* below. |
| `env` | table (string → string) | `{}` | Passed to the container as `-e KEY=VALUE`. Both keys and values must be strings. |
| `setup` | mixed list (strings + tables) | `[]` | Steps run **once**, right after the container is (re)created. A failing step stops `start` and leaves the container in place for debugging; re-run only happens on recreate (see drift below), not on every `start`. Two entry shapes — see *Typed `setup` steps* below. |
| `run_flags` | list of strings | `[]` | Escape hatch: extra flags spliced into the workspace `docker run`. credproxy's structural flags (`--name`, labels, `--network`, the home volume) are applied **after** these and win on conflict, so `run_flags` can't detach the netns or rename the container; additive flags (`--userns`, an extra `--mount`/`-v`, `--security-opt`) take effect. The main use is runtime-specific uid mapping (see *Non-root user & mount ownership* below). |
| `map_host_user` | bool | `false` | Make the non-root `user` own your bind mounts without changing host ownership. credproxy picks the runtime-appropriate lever automatically (`--userns=keep-id` on rootless podman; a no-op on Docker, where the matching uid does it). **Requires `user`** (error otherwise). The managed alternative to a hand-written `--userns` in `run_flags` — and if you set both, the `run_flags` one wins (escape hatch overrides the knob). See *Non-root user & mount ownership* below. |
| `user_uid` | int | host uid | The in-container uid of `user` — the uid `map_host_user`'s keep-id maps your host uid **onto** (rootless podman). Host uid and this need not be equal; keep-id maps across them. Defaults to your host uid (correct for a `setup`-provisioned user made as `$CREDPROXY_HOST_UID`); set it to a baked user's uid (the default image's `vscode` is `1000`, which the scaffold fills in). **Requires `user`** (error otherwise). Only consumed with `map_host_user` on rootless podman. |
| `auto_stop` | bool | `false` | When `true`, the workspace stops once the last `enter` session exits. Read fresh at session end, so toggling it mid-session takes effect immediately. A stopped workspace is resumed automatically by the next `enter`. Must be a real boolean — `auto_stop = "false"` (a string) is rejected, not silently truthy. |

**Unknown keys are rejected.** Since the TOML is the single source of truth, a
misspelled top-level key (`mount` for `mounts`, `setup_cmd`, `user_id`) would
otherwise parse fine and silently do nothing. `load_config` fails with `unknown
key(s): …` (with a did-you-mean) instead, so a typo is caught at `start`/`apply`
rather than surfacing as mysteriously-wrong behavior. (`list` and cwd resolution
stay tolerant of a broken peer config — one bad file never breaks the others.)

Changing `image`, `home`, `mounts`, `env`, `setup`, `run_flags`, or
`map_host_user` is **container-spec drift**: it requires recreating the
workspace container, which happens on the next `start` (the home volume is
preserved). Editing bindings does **not** require a recreate — see below.

#### Typed `setup` steps

`setup` is a **mixed array**: each entry is either a plain command **string** or
a **table**.

- A **string** runs exactly as it always has: as **root** (`-u 0`), via
  `sh -lc`, with **no** injected env. This is the escape hatch — its behavior is
  unchanged, and an all-string `setup` is byte-for-byte equivalent to before
  typed entries existed.
- A **table** `{ run = "CMD", user = "workspace"|"root", order = N }` gives you
  two levers:
  - **`user`** (default `"workspace"`) — `"workspace"` runs the step as the
    config [`user`](#exec-settings) with that user's correct `HOME` (resolved
    inside the container at step time, since `docker exec -u` doesn't set it);
    `"root"` runs it as root. When the config declares no `user`, `"workspace"`
    resolves to root. A literal username is **not** accepted in v1 — only the two
    keywords.
  - **`order`** (default `0`) — an integer sort key. Steps run in **stable
    `(order, position)` order**: lower `order` first regardless of where it's
    written, and equal orders keep declaration order. Strings have implicit
    `order = 0`.

  Table steps additionally receive the **binding env** — every binding's
  effective env var set to its **placeholder** (the same set `/exports.sh`
  serves a login shell), so a step can `export`-free reference e.g.
  `$GITHUB_TOKEN` and get the inert placeholder the workspace always sees.
  (Strings get no injected env.)

A workspace-user step whose user doesn't exist yet fails with a precise error
(`user 'vscode' does not exist … create it in an earlier root step or set
user = "root"`) — so provision the user in an earlier `order` root step (or bake
it into the image). Editing any of a table's `run`/`user`/`order` is spec drift
(recreate on next `start`), same as changing a string.

```toml
user = "dev"   # `user = "workspace"` (the table default) resolves to this;
               # without it a workspace-user step would resolve to root
setup = [
  "curl -fsSL http://proxy.local/bootstrap.sh | sh",       # root, no env
  { run = "useradd -m -u $CREDPROXY_HOST_UID dev", user = "root", order = 10 },
  { run = "gh auth setup-git", order = 45 },                # runs as `dev`, has HOME + binding env
]
```

#### Adding a volume, keeping existing data

If you've been using a workspace and realize a directory that lives only in the
container's writable layer (commonly an ephemeral `home`) should have been a
persistent volume, `mount add --preserve` converts it without losing the data:

```
credproxy workspace myproj mount add --volume home --target /home/vscode --preserve
```

This captures the current container's data at `--target` into the new volume,
then recreates the container with the volume mounted **populated** (a non-empty
volume suppresses Docker's image-seed). A mount can't be attached to a running
container, so applying any new mount recreates it regardless — `--preserve` just
carries the data across that recreate. File ownership is preserved across the
copy on both Docker and rootless podman (the copy helper inherits the
workspace's userns mapping). Without `--preserve` the volume starts empty
(image-seeded) and the change is deferred to the next `start`, like editing the
file. On a **running** workspace with live `enter` sessions — which the recreate
terminates — the command asks first (loose) or needs `--yes` (strict).

### Exec settings

These shape how `enter` runs commands in the container; they are **exec-only**
(not part of the container spec), so changing them takes effect on the next
`enter` with **no recreate**.

| Key | Type | Default | Notes |
|---|---|---|---|
| `user` | string | image default (root) | Runs `enter` (and `enter -- cmd`) as this user via `docker exec -u`. The user must exist in the image — built in, or created by `setup`, which always runs as **root** (so it can `useradd`, add sudoers, and `chown` the home volume to the user). `enter --user NAME` overrides it for one session (e.g. `enter --user root` for a debug shell). |
| `shell` | list of strings | `["bash", "-l"]` | Command `enter` runs when you don't pass `-- CMD` (argv list). Defaults to a **login shell** — semantically entering the workspace is "logging in" (the ssh model), so the interactive entry sources the full login environment; `enter -- CMD` stays a bare, non-login command (the ssh `host cmd` model). Set e.g. `["zsh"]` to change the entry shell, or `["bash"]` for a non-login one. |
| `workdir` | string | `home` | Directory `enter` starts in (`docker exec --workdir`) — the `workspaceFolder` analog. Defaults to `home`, so you land in your home dir rather than the image's `WORKDIR` (`/` on the devcontainers base); point it at a bind-mounted project to land there. Must be absolute. A `--workdir` in `exec_flags` still overrides it (docker last-wins). |
| `enter_prelude` | string | source the CA-env file | A shell snippet run before the enter command, via `sh -c '<prelude>; exec "$@"'`. The default sources the proxy's bootstrap-written env file (`/etc/profile.d/credproxy.sh`) so the HTTPS-CA env vars reach an interactive shell, `enter -- cmd`, **and** subprocesses — `docker exec` is a bare `execve`, so without this the env only loads in a login shell. `exec "$@"` keeps it transparent (no extra PID; signals/TTY/exit code/argv pass through). Set to `""` to skip wrapping (direct `execve`, no `/bin/sh` dependency). |
| `exec_flags` | list of strings | `[]` | Escape hatch: extra flags spliced into the `docker exec` for `enter` (e.g. `["--workdir", "/srv"]`, `["--env", "FOO=bar"]`). credproxy keeps ownership of the session-control flags (`-i`/`-t`/`-d`), so these can't detach the session or break auto-stop. |

A **string** `setup` step runs as root regardless of `user`, so it is the place
to provision a non-root user (create it, grant sudo, chown its home). A **table**
step can opt into running as that `user` instead — see *Typed `setup` steps*
above.

### Directory association (cwd resolution)

`directory` lets the **loose** surface (`credp`) resolve an omitted workspace
from where you are: a `credp <verb>` with no NAME, run at or under a workspace's
`directory`, resolves to that workspace. It's the `cd project && credp enter`
ergonomic — layered on top of the canonical name, which stays primary. This is
just another resolver, a sibling of the default-workspace pointer.

| Aspect | Behavior |
|---|---|
| Match | **Walk-up, longest-prefix**: a workspace whose `directory` is an ancestor of (or equal to) cwd matches; the most specific wins, so `~/src` and `~/src/foo` nest cleanly. Both sides are canonicalized (symlinks, `..`). |
| Order | **cwd before the default pointer** — "what I mean here" beats "what I usually mean". Whichever fires is announced on stderr (`workspace: foo (matched current directory)`). |
| Surface | **Loose only.** Strict `credproxy` never consults cwd; it always requires an explicit NAME (the scriptable contract). The field is parsed on both surfaces — only the *resolution* is loose-only. |
| Ambiguity | Two workspaces claiming the **same** directory is an error at resolve time (name one explicitly). |
| Too broad | `/` and `$HOME` are ignored as associations (they would match almost everything). |
| Container | None — host-side resolution metadata, not part of the spec hash, so changing it never recreates anything. |

Set it without hand-editing:

- `credproxy workspace create NAME --here` — associate the new workspace with the
  current directory (`--dir PATH` for another).
- `credproxy workspace NAME bind-dir [--dir PATH]` — associate an existing
  workspace (defaults to the current directory).

Both write `directory` as a surgical edit that preserves comments and ordering
(the TOML stays the single source of truth). `credproxy list` shows each
workspace's directory and marks the one matching your current directory.

### Injected environment

Beyond your `env` table, every workspace gets a few read-only breadcrumbs in its
environment — handy for `setup` scripts, shell rc, and a tenant that wants to
self-configure. They are stable per workspace/host, so (unlike `env`) they are
**not** part of the container spec hash and never cause a recreate; an existing
container picks up a newly added one on its next recreate. Your `env` is applied
last, so a key you set there shadows the breadcrumb of the same name.

| Variable | Value |
|---|---|
| `CREDPROXY_SETUP` | `http://proxy.local/llms.txt` — where a tenant (e.g. an agent) reads its own setup guidance. `proxy.local` resolves via `/etc/hosts`; `/setup` serves the machine-readable least-disclosure binding shape. |
| `CREDPROXY_WORKSPACE` | The workspace's own name — so a setup script or prompt label can read it instead of templating the literal name (also available via `/setup`). |
| `CREDPROXY_HOST_UID` / `CREDPROXY_HOST_GID` | The uid/gid the CLI runs as, i.e. the owner of your bind-mounted project dirs. The value to match a `setup`-created user to (`useradd -u $CREDPROXY_HOST_UID`) — see *Non-root user & mount ownership* below. |
| `CREDPROXY_USER` | The configured `user` (exec identity) — the name to pair with `CREDPROXY_HOST_UID` so a root `setup` script can provision it without templating the literal (`useradd -u $CREDPROXY_HOST_UID $CREDPROXY_USER`, `chown -R $CREDPROXY_USER …`). Only set when `user` is; a per-session `enter --user NAME` override does **not** change it. |

### Non-root user & mount ownership

Running the workspace as a non-root `user` (above) and bind-mounting host
directories into it runs into a runtime-specific ownership problem, and there is
no single portable flag for it — rootful and rootless runtimes have opposite uid
models. credproxy never changes host-file ownership to paper over this; instead
you pick the right lever per runtime. In every case the host bytes and ownership
are left untouched.

The lever here is `CREDPROXY_HOST_UID` / `CREDPROXY_HOST_GID` (see *Injected
environment* above) — the uid/gid the CLI runs as, i.e. the owner of your
bind-mounted project dirs. It's the value to match a `setup`-created user to
(`useradd -u $CREDPROXY_HOST_UID`).

#### The mental model

The workspace `user` runs as some uid **inside** the container — call it
`user_uid`. For it to read/write your bind mounts (owned on the host by you,
`CREDPROXY_HOST_UID`), your host identity has to map to `user_uid` inside. **How**
that mapping works — and **whether the host uid and `user_uid` may differ** —
depends on the runtime:

- **Rootless podman:** `map_host_user` adds `--userns=keep-id:uid=<user_uid>`,
  which maps **your host uid onto `user_uid`**. The two **need not be equal** —
  keep-id maps *across* them — so a baked `vscode` (uid 1000) works even when your
  host uid is 501. credproxy just needs to know `user_uid` (it defaults to your
  host uid; the scaffold sets `1000` for the default image). **runc caveat:** the
  keep-id userns plus the shared-netns join trips a `runc` limitation — the
  workspace container fails at init with a `sysfs` mount error. Use `crun` (or set
  `map_host_user = false`); see
  [troubleshooting](../troubleshooting.md#workspace-fails-to-start-with-a-sysfs-mount-error-rootless-podman--runc).
- **Rootful Docker (Linux):** container uid **==** host uid — no remapping, no
  keep-id. So here `user_uid` **must equal** your host uid for the mounts to line
  up, and `map_host_user` is a no-op. You match them by creating the user as
  `$CREDPROXY_HOST_UID`; the baked `vscode` (1000) lines up **only** at host uid
  1000.
- **Docker Desktop (macOS):** the file share is permissive — uid doesn't matter,
  it just works.
- **Rootless Docker:** no `keep-id` equivalent — **not covered**; you'd need
  idmapped bind mounts.

**Nested mount parents.** A bind target nested below `home`
(`~/src/proj:/home/vscode/src/proj`) makes the runtime fabricate the intermediate
`/home/vscode/src` as container-root — so even though the mount itself ends up
user-owned, that parent isn't, and the user can't create siblings there (a second
clone under `~/src`). Under `map_host_user` credproxy re-owns those fabricated
parents to the user's uid on each (re)create — a non-recursive `chown` of only the
dirs between `home` and the target (never the mount point, never host files),
runtime-agnostic (the parent is root on podman *and* rootful Docker). On the
manual `run_flags` path it's yours to handle (the namespace is yours).

So `user_uid` is the one knob, and it bites in exactly one place: it's the
in-container uid that keep-id targets on rootless podman. Set it wrong and the
mount shows up owned by the wrong uid inside (keep-id maps host-you onto *exactly*
that uid). `map_host_user` and `user_uid` are part of the container spec, so
changing either recreates the workspace on the next `start`. The host files are
never chowned in any case.

#### Supplying `user_uid`

**A baked user with a known uid** (the default image's `vscode` is `1000`) — tell
credproxy the uid; host uid and the user's uid then differ freely (podman):
```toml
user = "vscode"
user_uid = 1000          # the scaffold fills this in for the default image
map_host_user = true
mounts = ["~/code:/code"]
```

**A user you create in `setup`** — give it your host uid, and omit `user_uid`
(it defaults to the host uid, which then matches on podman *and* rootful Docker):
```toml
user = "dev"
map_host_user = true
mounts = ["~/code:/code"]
setup = ["useradd -u $CREDPROXY_HOST_UID -m dev || true"]
```

#### The manual path: `run_flags`

If you'd rather own the user namespace yourself (or need a non-default mapping),
skip `map_host_user` and write the flag directly:

- **Rootful Docker / Docker Desktop (macOS):** uids are 1:1, so just
  `useradd -u "$CREDPROXY_HOST_UID" dev` in `setup`; no `run_flags` needed.
- **Rootless Podman (Linux):** `run_flags = ["--userns=keep-id:uid=1000,gid=1000"]`
  plus a matching `useradd`. (`run_flags` is static TOML and can't read the env
  var, so use the same literal uid in both.) A per-mount `-v SRC:DST:idmap` is the
  finer-grained alternative.

To just change which in-container uid the mapping targets, prefer `user_uid` (above)
— `run_flags` is for a genuinely custom userns (an explicit `--uidmap`, multiple
ranges, etc.). If you set **both** `map_host_user` and a `--userns` in `run_flags`,
the `run_flags` one wins — `run_flags` is the escape hatch and overrides the
convenience knob (it's spliced after credproxy's `keep-id`, but still before the
structural flags, so it can't touch the netns).

### Bindings

A `[[binding]]` block ties an **injector** (how a credential is shaped into a
request — which typed scheme the proxy runs) to a **provider** (where its value
comes from), scoped to a set of hosts. The real secret never enters the
workspace: the workspace holds only the inert `placeholder`, and the proxy swaps
it for the real value on requests to the scoped hosts.

| Field | Required | Notes |
|---|---|---|
| `injector` | yes | Name of an injector definition (`$XDG_CONFIG_HOME/credproxy/injectors/<name>.toml`, falling back to builtin). Selects the scheme, its params, and the placeholder shape. Builtin: `bearer`, `basic`, `body`. |
| `provider` | yes | Name of a provider executable (`$XDG_CONFIG_HOME/credproxy/providers/<name>`, falling back to builtin). Builtin: `env`. |
| `secret` | yes | Either a bare ref string (single-slot), or an inline table mapping the scheme's slot names to refs (multi-slot). A ref is opaque to credproxy and meaningful only to the provider — an env-var name, a vault path, an item id. |
| `hosts` | yes | Non-empty list of hostnames the credential may be injected on. This is the security scope: a request to any other host never sees the real value. Each entry is a literal hostname (exact match) **or** a glob pattern containing `*` — see *Host patterns* below. |
| `name` | **yes** | Handle used to address the binding (`binding remove`, `binding test NAME`). Hand-authored — `binding add` writes it for you (`<injector>-<provider>`, `-2`/`-3`/… on collision), but a binding you write by hand must include one. Omitting it is an error that names the exact line to add. |
| `placeholder` | no | The inert sentinel the workspace sends (substitute schemes). **Omit it** — the generated value lives in the machine-owned lockfile (`$XDG_STATE_HOME/credproxy/workspaces/<name>/lock.json`), not the TOML, so the CLI never rewrites your file. Set it explicitly only to pin a specific value: an explicit `placeholder` **wins** and never enters the lock. |
| `env` | no | Suggested env var name surfaced to the workspace via `/setup` (and pre-exported via `/exports.sh`); must be a valid shell identifier (letters, digits, `_`; not starting with a digit). A non-empty string overrides; **absent** inherits the injector's `env` hint; **`env = false`** suppresses it entirely (no env exposed — `binding add --no-env`). `env = ""` and `env = true` are rejected. |

**One-way dataflow (intent file + lockfile).** The workspace TOML is your
hand-owned **intent** file — credproxy never rewrites inside it (comments are
sacred). The only edits it makes are appending a whole new `[[binding]]`/`[[rule]]`
block at the end (`binding add`/`rule add`) or deleting a whole named block
(`binding remove`/`rule remove`). Everything **generated** — today, each
substitute-family binding's `placeholder` — lives in the machine-owned
`lock.json` instead, keyed by binding **name**. Consequences:

- A binding with no explicit `placeholder` gets one minted into the lock the
  first time a mutating command runs (`start`, `push`, `apply`, `binding add`,
  `binding test`, `resolve`); it is stable thereafter.
- An explicit `placeholder` in the TOML always wins and is never copied into the
  lock.
- Placeholder identity is keyed by name, so **renaming a binding regenerates its
  placeholder** (and drops the stale lock entry).
- `[[rule]]` names are hand-authored the same way (rules carry no placeholder, so
  they have no lock entry).

`lock.json` is **safe to delete**: it holds only regenerable machine data. The
next resolve re-mints placeholders (a binding with no explicit `placeholder`) and
re-expands every `[[preset]]` reference from its **current** definition, then
re-snapshots the lot. (The one visible consequence: a placeholder value changes
if you delete the lock, so a workspace mid-flight would want a fresh `apply`.)

#### Host patterns

A `hosts` entry without `*` is matched exactly (the common
case, and the fast path). An entry containing `*` is a **glob**, where `*` spans
any characters including dots — so one binding can cover a family of endpoints:

```toml
hosts = ["*.amazonaws.com"]        # any AWS service, any region
hosts = ["s3.*.amazonaws.com"]     # S3 only, any region
hosts = ["github.com", "api.github.com"]   # literals (exact), unchanged
```

This is what `sigv4` wants: it reads region and service from each request, so a
single `*.amazonaws.com` binding re-signs every regional endpoint with one real
key. Patterns are validated strictly, because this scope decides where a real
credential is injected: the two rightmost labels must be literal, so
`*.example.com` and `s3.*.amazonaws.com` are allowed but `*`, `*.com`, and
`*.*` are rejected (an over-broad pattern can't inject a credential into an
attacker-chosen host). A literal host always takes priority over a pattern that
also matches it; if two *different* patterns overlap, both apply in file order
(the later one wins a shared header).

**Validation.** Binding names must be unique within the workspace, and no two
bindings may write the same wire location on the same host (e.g. both into the
`Authorization` header on `api.github.com`). For glob hosts this collision check
is by pattern string — two identical patterns collide, but two *different*
overlapping patterns are resolved at request time (file order) rather than
rejected. The binding's secret slots must match the scheme's declared slots. The
referenced injector and provider must resolve. Violations are reported as a
config error naming the file and the offending field.

**Presets — service setup packs.** A *preset* is a one-command pack for a
service: the coordinated bindings a credential needs across its hosts **and** the
credential-free rule guardrails that should ride along. `credproxy workspace NAME
preset add github` references both. Either half is optional:

- **Binding-only** (the GitHub PAT case: `bearer` on `api.github.com`, HTTP
  `basic` on `github.com`/`ghcr.io`, all sharing one bare-token placeholder — no
  hand-computed base64, no fragile coupling). Needs `--provider`/`--secret`, or
  the preset's defaults (`github` defaults `gh-cli`/`github.com`, so bare `preset
  add github` works off an existing `gh` login).
- **Pure-rule policy pack** (an org's `readonly-guard` wired to its hosts/params)
  — no `[placeholder]`, no provider/secret; `preset add org-guardrails` with zero
  flags.

It's a durable **reference, not a stamp**: `preset add` appends a small
`[[preset]]` block (the pack name plus the resolved `provider`/`secret`/
`[preset.options]`); the resolver expands it into ordinary bindings/rules (named
`<preset>-<suffix>`, including `[rule.params]`) and container config at resolve
time, and snapshots the full expansion in the lockfile — the proxy never sees a
"preset". Literal entries come first, then preset expansions in `[[preset]]`
declaration order (this ordering is the rule-evaluation order — see
[`rules.md`](rules.md)). A `[[preset]]` block may also carry `disable = [...]`
(omit part/rule suffixes) and `[preset.override.<suffix>]` (whole-field replace a
binding/rule field). The add is atomic (a name collision fails the whole thing
before any write) and announces any host it **newly TLS-intercepts** (a preset
rule on a bindings-free host flips it — see [`rules.md`](rules.md#interception-is-a-union--a-rule-can-flip-a-host-to-intercepted)).
A changed pack definition is inert until re-expanded, but editing the reference's
own inputs (provider/secret/options/disable/override) re-expands on the next
resolve. `credproxy preset list` shows every pack's full expansion before you
apply. Presets are data in the layered registry, so an org ships its own by
dropping a TOML in an overlay — see [`overlays.md`](../advanced/overlays.md).

Injector definitions are a separate declarative file type (scheme, params,
placeholder pattern, env hint) — see [`injectors.md`](injectors.md). Providers
are host-side executables — see [`providers.md`](providers.md).

## The CLI path

Every imperative command maps to an edit of, or an action driven by, the same
file. You can always skip the command and edit the TOML directly.

| Command | Effect on the config |
|---|---|
| `credproxy workspace create NAME` | Scaffold `<name>.toml` (and the state dir + `auth.token`) from the workspace template. Does not start anything. To use a non-default image, edit the scaffolded `image`. |
| `credproxy workspace NAME binding add --injector I --provider P --secret REF --host H [--host H…] [--name N] [--placeholder PH] [--env E | --no-env]` | Append a `[[binding]]` block (with a generated `name` unless you pass `--name`). The placeholder is minted into the lockfile unless you pass `--placeholder` (which writes it into the block). Validates the whole set before writing, so a rejected binding never lands in the file. Repeat `--secret SLOT=REF` for a multi-slot secret; a single `--secret SLOT=REF` works too when `SLOT` is the scheme's slot name (e.g. `jwt-bearer`'s `private_key`). |
| `credproxy workspace NAME preset add PRESET [--provider P --secret REF] [--opt ID=VALUE …]` | Apply a **service setup pack**: append a `[[preset]]` reference (with the resolved provider/secret/options written explicitly) that the resolver expands into the coordinated `[[binding]]` set (sharing one placeholder) **and** its `[[rule]]` guardrails, all-or-nothing. A binding-bearing preset takes provider/secret (or its defaults); a pure-rule pack takes none. Repeat `--secret SLOT=REF` for a multi-slot injector (e.g. `sigv4`); every part's injector must declare the same slots, and a mismatched or missing slot set fails the add before anything is written. `--opt` supplies a pack `[[option]]`'s host-half value (whole-field); an unresolved required option prompts on the loose surface + a terminal, else fails with a structured error. Announces any newly-intercepted host. `preset list` shows the full expansion first. |
| `credproxy workspace NAME binding remove BINDING_NAME` | Remove that binding's block (surgical text edit). Reversible in principle, but loses tuning — gated by confirmation when targeting the default workspace on the loose surface. |
| `credproxy workspace NAME binding list` | Read and print the bindings (placeholders resolved from the lockfile, read-only — nothing is written). Shows name, injector, provider, secret-id, hosts, env, and placeholder. |
| `credproxy workspace NAME binding test [BINDING_NAME]` | Dry-run: fetch each binding's secret through its provider and report success and **value length only** (never the value). Exit 1 if any fail. |
| `credproxy workspace binding test --provider P --secret REF [--injector I]` | Ad-hoc variant: test a provider/injector combination **before** binding it. No workspace is required. |
| `credproxy workspace NAME edit` | Open `<name>.toml` in `$VISUAL`/`$EDITOR` (default `vi`), then validate it: warns if the edit left it invalid (without reverting), otherwise hints `apply`/`start`. Pure sugar over opening the file yourself. |
| `credproxy workspace NAME config [--declared]` | Read-only: dump the container-side config. Default `effective` — every field with its in-effect value, all defaults filled (including the enter-time `workdir`→home and `enter_prelude`→shim defaults `inspect` leaves null), so you can see what actually applies even when it's not in the file. `--declared` shows only what's literally in the TOML. `--json` on both. |
| `credproxy workspace NAME inspect` | Read-only: print the parsed config, container state, resolved host port, binding summary, **itemized drift** between the file and what was last applied, and — when the proxy is reachable — a **live drift** layer comparing the resolved config against what the proxy is *actually* running (see below). |
| `credproxy workspace NAME apply` | Reconcile a running workspace to the edited file (see below). |

These read-only views are projections of the file, with no state of their own:
`config` shows the config values (effective or declared), `inspect` adds
container state and **drift**, and `edit` just opens the same `<name>.toml` in
`$EDITOR` and validates the result. The TOML file remains the single source of
truth.

### Applying changes

A file edit is not picked up automatically. How a change takes effect depends on
what you changed:

- **Bindings** are live-applicable. On a running workspace, `apply` re-resolves
  each binding's secret through its provider and pushes the new wire config to
  the proxy — no restart, no dropped connections.
- **Container settings** (`image`, `home`, `mounts`, `env`, `setup`) cannot be
  changed on a live container. `apply` reports them as **deferred** with a hint;
  `start` performs the recreate (preserving managed volumes) and re-runs
  `setup`. To force a rebuild on demand — even with no drift, e.g. to re-run
  `setup` or get a clean container — use `recreate` (workspace container only;
  `recreate --proxy` also rebuilds the proxy and regenerates its CA). Like
  `start`, it preserves all managed volumes, config, token, and state. To *also*
  start from a clean volume, `recreate --reset-volume NAME` (repeatable, e.g.
  `--reset-volume home`) wipes that managed volume — re-seeded from the image —
  while keeping the workspace defined (config, token, and state survive, and
  bind/overlay host-path mounts are untouched). It destroys data, so on the loose
  surface it prompts for an implicit default (`--yes` bypasses).

`apply` reports what it applied versus deferred; `inspect` shows the same drift
ahead of time, item by item. `start` always re-pushes bindings once the proxy is
healthy, because the proxy's config lives on tmpfs and does not survive a
`stop`/`start`.

```sh
# edit the file, then:
credproxy workspace myproj inspect   # what differs?
credproxy workspace myproj apply     # push binding changes live
credproxy workspace myproj start     # recreate for image/mounts/env/setup changes
```

### Drift against reality

The drift above compares the file against a **local record** of the last push
(the lockfile's `applied` section). That record can go stale: the proxy keeps its
config on tmpfs, so a `stop`/`start` clears it while the record still says a
config was pushed. To catch this, `inspect`/`apply`/`doctor` also ask the running
proxy what it is *actually* holding — a small, bearer-gated, **sanitized**
`GET /admin/config` that reports the live config **generation** plus a tight
binding/rule summary (never a secret value, a `params` value, or a header/body
value).

`inspect`'s **live** layer renders one verdict, decided by the config
**generation** and the offline itemized drift above — **never** by the sanitized
summary, which is *display only*. That summary is deliberately lossy (it omits the
secret ref, provider, injector `params`, and a rule's methods/path/status/body/
headers), so a change to any of those would be invisible in it; the verdict
therefore reads the content-complete offline drift instead:

- **in-sync** — the generation matches the last recorded push *and* the offline
  drift is empty.
- **config-drift** — the generation matches, but the file has moved ahead of the
  proxy (the offline drift is non-empty); `apply`/`start` pushes the change.
- **reality-drift** — the proxy's generation is *not* the one credproxy last
  recorded pushing (it lost its tmpfs on restart, or a stateless `push` landed
  from elsewhere), so the proxy is holding a config we didn't push. Takes
  precedence over content; `apply` re-pushes to heal it.

When the proxy is unreachable (or the token fails) the live layer is reported as
**live unavailable** and the lockfile-based drift stands alone. `apply` pushes
whenever the offline binding/rule drift is non-empty **or** the live layer reports
reality-drift — the live signal can only *add* a re-push reason (the tmpfs-loss
case the offline record can't see); it never suppresses a content change the lossy
summary happens to render identical. A reality-drift re-push re-records the
generation, which closes `doctor NAME`'s `ws:<name>:proxy:config-sync` check (the
live generation vs. the last recorded push), skipped when the proxy is stopped.
Attached workspaces get the same live layer over their resolved `attach` admin URL.
