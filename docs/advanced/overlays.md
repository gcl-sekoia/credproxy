[← docs index](../README.md) · [Concepts](../concepts.md)

# Customizing credproxy with overlays

An org or team often wants its own defaults: a standard workspace image, an
internal CA in every container's setup, a vault provider, an artifact-registry
preset. credproxy is built so you can do all of that **without editing engine
code** — and, ideally, without maintaining a code fork at all.

## The resolution order

Every customizable asset resolves through one ordered search path, most specific
first:

```
user            $XDG_CONFIG_HOME/credproxy/   per-machine, the end user
  ↓ shadows
overlays         CREDPROXY_OVERLAY_PATH (declared order) or <repo>/overlay/*/
  ↓ shadows
builtin          cli/credproxy_cli/builtin/   upstream defaults (in-package)
```

A same-named file in a higher tier **shadows** the lower one; a new name **adds**
to the set. This is `paths.overlay_roots()` — the single seam both
`paths.layered_dirs()` (the registries: injectors, providers, scripts, presets)
and `paths.resolve_singleton()` (the one singleton, `workspace.template.toml`)
derive from, so every asset kind resolves through exactly the same tiers.

The **middle tier is N overlays, not one.** `CREDPROXY_OVERLAY_PATH` is an
`os.pathsep`-separated list of directories (`:` on Unix), searched
leftmost-first — PATH semantics. So you can layer a team overlay over an
org-wide one, or a machine overlay over a shipped bundle, without merging them:

```sh
export CREDPROXY_OVERLAY_PATH=/etc/credproxy/team-ml:/etc/credproxy/org-base
```

Rules for the variable:

- **Unset** falls back to **discovery**: every *subdirectory* of the
  `<repo>/overlay/` container is one overlay (see below).
- **Set-but-empty** (`CREDPROXY_OVERLAY_PATH=""`) means **no overlays** — an
  explicit opt-out, distinct from unset.
- Empty entries within the list (`a::b`, a trailing `:`) are skipped.
- The variable **replaces** the default entirely; it never appends to it.
- Overlays are labelled `overlay:<dir basename>` (e.g. `overlay:team-ml`);
  duplicate basenames get a deterministic numeric suffix (`overlay:base`,
  `overlay:base#2`) in declared order. These labels show up in `credproxy info`,
  the registry `list` commands, and `doctor` check ids.

A missing env-listed overlay dir is tolerated during resolution (it simply
contributes nothing) — `credproxy doctor` is what flags a configured-but-missing
entry loudly (one existence check per entry, only when the env var is set;
discovered overlays exist by construction, so unset means no overlay checks).

### The default: `<repo>/overlay/` is a container of named overlays

With the env var unset, credproxy scans `<repo>/overlay/` for
**subdirectories** — each one is an overlay, labeled `overlay:<basename>`.
Upstream ships the container with only a README, so out of the box there are no
overlays at all.

- Ordering is **lexical by basename** — the earliest wins a name conflict. When
  stacking overlays, make the order explicit with numeric prefixes
  (`10-base/`, `20-team/`; `10-base` shadows `20-team`).
- **Any subdirectory activates** — don't park scratch or backup directories in
  the container; `credproxy info` shows what got picked up.
- Files at the container's top level (the README) are ignored; the registry
  subdirs and `workspace.template.toml` go **inside** the named overlay.

## Two ways to customize

### 1. Point at an overlay bundle — no fork (recommended)

Set `CREDPROXY_OVERLAY_PATH` to any directory (or `:`-list of directories) with
the layout below — a deb/rpm payload, a git submodule, `/etc/credproxy/overlay`,
a dotfiles dir:

```sh
export CREDPROXY_OVERLAY_PATH=/etc/credproxy/overlay
```

Nothing to merge: you ship the overlay as data, on whatever cadence you like, and
track upstream credproxy unmodified.

### 2. Fork the repo

Create a named overlay under the `overlay/` container (which upstream ships
empty except a README) and commit your customizations there:

```sh
mkdir overlay/acme-corp        # discovered immediately, labeled overlay:acme-corp
```

Your **entire diff against upstream lives in `overlay/acme-corp/`**, and
upstream never writes there, so `git merge upstream/main` is conflict-free in
perpetuity. The engine and builtin defaults you inherit; your overlay you own.
A discovered overlay and an env-listed bundle are the same mechanism — the fork
story and the no-fork bundle differ only in how the directory is named.

## What you can put in an overlay

```
<overlay>/
  workspace.template.toml         # the scaffold a fresh `create` produces
  workspace.attach.template.toml  # the scaffold `create --attach` produces (see composability.md)
  injectors/<name>.toml           # request-shaping schemes
  providers/<name>                # secret-source executables
  scripts/<name>.star             # sandboxed Starlark injector / rule bodies
  presets/<name>.toml             # service setup packs: bindings + rule guardrails
```

> The only hardcoded engine constant is the proxy image tag (`IMAGE_TAG`). There
> is **no default *workspace* image knob** and **no `home` fallback** — the
> default workspace image is simply the `image` line in
> `workspace.template.toml`, and `home` is optional sugar for a managed volume
> (omit it for an ephemeral home).

### `workspace.template.toml` — the scaffold

The `<name>.toml` body a fresh `credproxy create` writes. Make it your canonical
default workspace — your image, your `user`/`home`, your `setup`, even default
`[[binding]]` blocks (or `[[preset]]` entries, expanded at create — see below)
for org infrastructure. It is a **literal** workspace
config: every occurrence of the exact token `{name}` is replaced with the
workspace name, and **nothing else** is touched — no `str.format`, so literal
braces (`{ volume = ... }` inline tables, `${VAR}`, a stray `{foo}`) need no
escaping or doubling. To run a different image, edit `image` (and `user`/`home`
to match) here, or per workspace in the generated `<name>.toml`.

Because it rides the same walk as the registries, a **user** can keep a personal
`$XDG_CONFIG_HOME/credproxy/workspace.template.toml` that shadows every overlay's
(and the builtin) — the same shadow rule as any other asset.

> **The `injector scaffold` / `provider scaffold` templates stay builtin.**
> `credproxy injector scaffold` seeds from the builtin `bearer`, and
> `provider scaffold` from the builtin `env`, ignoring overlays. That is
> deliberate: a scaffold is an *upstream authoring template* to start from, not
> an overlay-customizable default.

### Registries — injectors / providers / scripts / presets

Drop a `<name>.toml` (or executable, or `.star`) in the matching subdir. Same
name as a builtin (or a less-specific overlay) **replaces** it; a new name
**adds** it. The shapes match the builtin examples — see
[`injectors.md`](../reference/injectors.md), [`providers.md`](../reference/providers.md), and
`cli/credproxy_cli/builtin/presets/github.toml`.

**Shipping a policy as a pack.** A rule script (`scripts/readonly-guard.star`)
and a **preset** that wires it (`presets/org-guardrails.toml`, an optional
`[[rule]]` array; see [`rules.md`](../reference/rules.md#distributing-a-policy-script--preset))
travel together in the overlay, so a workspace applies the whole policy with one
`credproxy workspace NAME preset add org-guardrails`. A preset can carry bindings
(`[[part]]`), rules (`[[rule]]`), **and the container half** (`[[mount]]`, `[env]`,
`[[setup]]`) — any subset. A **pure-rule** pack (no `[placeholder]`/provider) is a
credential-free policy bundle; a **pure-container** pack (mounts/env/setup only)
ships a service's setup without a credential.

**A pack's files ride the pack's tier.** A `[[mount]]` in a preset that names an
`overlay = "setup.d/x.sh"` source resolves within the pack's *own* tier — the
stamp records the qualified form `overlay = "<tier>:setup.d/x.sh"` (the tier is
an overlay basename, or `user`/`builtin`), so the file is pinned to the pack and
unaffected by overlay reordering/shadowing. `[[setup]]` steps must declare an
explicit `order` (packs are explicit about ordering); host-bind sources are baked
as literal v1 defaults, existence-checked when the workspace starts. Each stamped
block carries an inert `# credproxy:preset …` provenance comment, so re-applying
the same pack is refused rather than duplicated.

**Parameterize the host half with `[[option]]`.** A host-half value that differs
per operator — a socket dir, an op:// path — is declared as an option and
referenced as the **whole value** of a host-half field with a structural
`{ option = "id" }` marker (a mount `bind`/`volume` source, or a `[[requires]]`
`path`). It is never a token inside a string, and never a container-half field
(`target`, `[env]` values, `[[setup]]`):

```toml
[[option]]
id          = "sock_dir"
type        = "string"                     # "string" | "enum" (with choices) | "bool"
default     = "~/.ssh/credproxy-agent"     # optional; omit to make it required
description = "host directory holding the signing agent's socket"

[[mount]]
bind   = { option = "sock_dir" }
target = "/ssh-agent"
```

A value resolves at expansion time — explicit `--opt id=value` / a template
`[preset.options]` table → a prompt (loose surface + terminal only) → the
`default` → otherwise the add fails with a structured missing-options error. The
resolved literal is stamped (the marker never reaches the workspace TOML), and
`preset refresh` reads a mount-feeding option's value back from the stamped mount.
An option used **only** in a `[[requires]]` path with no default can't be read
back later, so give such an option a default (or also use it in a stamped mount).

**Declare your pack's host prerequisites.** A pack often needs host state its
container half can't provide — the `gh` CLI installed, a signing-agent socket
dir, a set env var, a provider that can serve the secret. Declare each with a
`[[requires]]` block so `preset add`/`create` check it (advisory — the pack still
stamps) and `doctor` re-checks it (authoritative):

```toml
[[requires]]
kind = "command"           # on the host PATH (shutil.which — looked up, never run)
command = "gh"
hint = "install the GitHub CLI: https://cli.github.com"

[[requires]]
kind = "path"              # host path exists (~ / $VARS expanded)
path = "~/.ssh/credproxy-agent"

[[requires]]
kind = "env"               # host env var set and non-empty
var = "SOME_VAR"

[[requires]]
kind = "provider"          # the provider chosen for this pack resolves
fetch = true               # (optional) also test-fetch the secret; needs [[part]]
hint = "authenticate: gh auth login"
```

The four kinds — `command` / `path` / `env` / `provider` — are the **whole set**,
implemented by credproxy. **A pack never supplies a script to run** on the host,
so activating an overlay from a fresh clone can't execute pack-authored code; the
only host-executable is a [provider](../reference/providers.md), reached through
the normal protocol for the `provider` kind. `doctor` finds which packs a
workspace uses from the stamped provenance comments; a `fetch = true` check runs
only under `doctor NAME --fetch`. See [the preset guide](../guide/06-presets.md#declaring-host-prerequisites).

**A template can *declare* presets.** Instead of inlining literal `[[binding]]`
blocks (which bypass preset machinery), a `workspace.template.toml` /
`workspace.attach.template.toml` may carry `[[preset]]` entries that `create`
expands through the **same** path as `preset add` — so each created workspace
gets its own freshly generated shared placeholder, and the template shrinks from
mechanism to intent:

```toml
# in your overlay's workspace.template.toml, alongside ordinary literal config
[[preset]]
name = "github"                     # complete defaults (gh-cli / github.com): nothing else needed

[[preset]]
name     = "claude-code"
provider = "bw"
secret   = "claude-code-oauth-token"
```

Each entry takes `name` (required) and optional `provider` / `secret` (a single
ref, defaulting exactly as `preset add` does — the pack's `default_provider`,
and `default_secret` only when the resolved provider *is* the default). The
`[[preset]]` blocks are **consumed at create and never survive** into the stamped
`<name>.toml` (the loader **rejects** `preset` in a workspace config — references
live only in templates). Create is **all-or-nothing**: a defaults-less pack with
no provider/secret, a name collision against a literal `[[binding]]`, or an
attach template naming a container-half pack fails the whole create with nothing
written (no config, no token, no state). There is **no prompting** — a missing
field is a loud error naming exactly what to fill (or drop the entry and run
`preset add` after). Leave `[[preset]]` out of the *builtin* templates so a bare
`credproxy create` needs no provider login; template presets are for
overlays/forks.

**Template vs. preset — both exist on purpose.** Baking `[[binding]]`/`[[rule]]`
blocks (or now `[[preset]]` entries) into `workspace.template.toml` applies them
to **every** workspace at **create** time, all-or-nothing. A **preset** applied
with `preset add` is the **per-service, composable, post-create** granularity:
applied to the workspaces that need it, when they need it, and stacked with
others. Use the template for "every box gets this"; use a bare `preset add` for
"this box also talks to service X."

## Shipping static files (overlay mounts)

Beyond the registries, an overlay can hold **arbitrary static files** — a CA
cert, an `.npmrc`, a `.gitconfig`, a setup script — and mount them into every
workspace. In `workspace.template.toml` (or a workspace's `mounts`), an
`{ overlay = "REL", target = "/dst" }` mount binds a path **relative to an
overlay dir** into the container (confined within the overlay dir, read-only by
default). With N overlays, `REL` is searched in declared order — the first
overlay containing it wins — so a more-specific overlay can override a shipped
file. The overlay becomes a self-contained bundle: declarative config *and* the
static assets it references. See [`configuration.md`](../reference/configuration.md) `mounts`.

## Provenance — which override actually won

`credproxy info` answers "is my overlay active, and which of my overrides won"
without reading source files: it lists the overlays in resolution order (with
present/absent), the per-tier registry counts keyed by full label, and an
`overlay_overrides` total (the **effective** view — an overlay asset a user file
shadows counts as `user`, not as an overlay override). The registry `list`
commands (`injector list`, `provider list`, `preset list`) annotate each row
with the tiers it **shadows**, e.g. `bearer   overlay:team-ml   (shadows
builtin)`.

## Testing your overlay

An overlay's `.star` scripts (scripted injectors and rule scripts) are real
logic and deserve real tests. Two supported tools:

### `credproxy script check [NAME]`

Compiles every resolvable script (or one NAME) in the proxy runtime — on-host
when the Starlark deps are importable, otherwise inside the proxy image — and
reports which compile. It classifies each script the way the proxy would: a
script named by a `scheme = "script"` injector manifest is compiled under the
**injector** profile paired with that manifest (so a slot/family mistake surfaces
too); an unreferenced script is tried under **both** the injector and rule
profiles and passes if either compiles. Exit 0 iff all compile; `--json` emits
`{name, origin, ok, error, profiles}` per script.

```sh
CREDPROXY_OVERLAY_PATH=/path/to/overlay credproxy script check
```

`credproxy doctor NAME` also compiles the scripts a workspace's bindings
reference (when the runtime imports on-host; skipped-with-note otherwise).

### The testkit — unit tests for scripts

`proxy/testkit.py` is a small, supported harness that drives a script exactly the
way the proxy runs it: it resolves the injector **manifest + `.star` together** and
builds the scheme through the same path the push/wire loader uses, so a test can't
pass against a manifest the proxy would reject (the drift a hand-built
`ScriptedScheme(...)` hides). Drop a `test_*.py` in `<overlay>/tests/`:

```python
# my-overlay/tests/test_ovh.py
import hashlib
import testkit

def test_ovh_signs():
    kit = testkit.load_injector("ovh")                     # manifest + ovh.star
    req = testkit.make_request("GET", "https://eu.api.ovh.com/1.0/me")
    result = kit.run(req, {"app_key": "AK", "app_secret": "AS",
                           "consumer_key": "CK"})
    assert result.injected                                 # on_request returned True
    # Independently recompute what the signature must be (this is why it's pytest,
    # not a declarative DSL): assert on the exact bytes the script produced.
    ts = req.headers["X-Ovh-Timestamp"]
    base = "AS+CK+GET+https://eu.api.ovh.com/1.0/me++" + ts
    assert req.headers["X-Ovh-Signature"] == "$1$" + hashlib.sha1(base.encode()).hexdigest()
```

Public API:

- `load_injector(name)` → a harness with `.run(req, secrets, params=None,
  placeholder=None)` → a result exposing `.injected` (the `on_request` bool); the
  mutated request is observed via the `req` you hold. `secrets` keys must equal the
  manifest's declared slots (the same check the proxy makes) — so manifest/script
  slot drift fails the test. Works for built-in schemes too.
- `make_request(method, url, headers=None, body=b"")` → a request with treq's
  default headers stripped and `Host` set from the URL (the two footguns every
  hand-rolled test open-codes). Pass a `Host` in `headers` to reproduce transparent
  mode (destination IP, real hostname in the header).
- `load_rule_script(name)` / `run_rule(script, req, params=None)` → the rule-side
  sibling: compile under the `kind="rule"` profile and run the request phase,
  returning an outcome with `.terminal` / `.blocked` / `.response` (a
  `block()`/`respond()`) and any rewrites on `req`. A script error raises (rule
  scripts fail closed toward the policy), so assert with `pytest.raises`.
- `make_response(req, status=200, body=b"", headers=None)` → a flow carrying `req`
  plus a response, with `tresp`'s default headers stripped (the response-phase
  twin of `make_request`). Feed it to the two response runners below.
- `run_rule_response(script, flow, params=None)` → run a rule script's **response**
  phase, returning the same `.terminal` / `.blocked` / `.response` outcome as
  `run_rule`; in-place body/header rewrites land on `flow.response`. A script error
  raises, same as the request side. (A rule that only defines `on_response` — a
  body scrubber — has no request runner; this is it.)
- `.run_response(flow, secrets, params=None, placeholder=None)` (on the
  `load_injector` harness) → run a scripted injector's **response** phase — the
  re-seal `mint` path — with the same slot validation as `.run`. A **recording fake
  minter** stands in for the real `RuntimeMinter` (which couples to config), so the
  result exposes `.handled` (the `on_response` bool), `.flow`, and `.mints` — a list
  of `MintRecord(value, ttl, api_hosts, header, placeholder)` capturing exactly what
  the script minted; the placeholders are deterministic (`minted-1`, `minted-2`, …)
  and the body rewrite (placeholder swapped in for the token) is observed on
  `flow.response`.

```python
# response-phase rule: a body scrubber
def test_scrub():
    script = testkit.load_rule_script("scrub-emails")
    flow = testkit.make_response(
        testkit.make_request("GET", "https://api.github.com/users/octocat"),
        status=200, body='{"login":"octocat","email":"octo@example.com"}')
    testkit.run_rule_response(script, flow)
    assert '"email":null' in flow.response.text.replace(" ", "")

# response-phase injector: re-seal mint
def test_reseal_mints():
    kit = testkit.load_injector("oauth-reseal")               # manifest + .star
    flow = testkit.make_response(
        testkit.make_request("POST", "https://oauth.example.com/token"),
        status=200, body='{"access_token":"REAL","expires_in":1200}')
    result = kit.run_response(flow, {"value": "CLIENT_SECRET"})
    assert result.handled and result.mints[0].value == "REAL"
    assert result.mints[0].ttl == 1200       # from expires_in
    assert "REAL" not in flow.response.text  # body carries the placeholder now
```

`credproxy dev test` **discovers** these: every configured overlay with a
`tests/` dir runs as its own pytest invocation (separate from the repo suite,
whose module basenames would collide), using the same on-host-or-image fallback
as the proxy suite. The whole overlay chain is mounted and on the resolution path
during the run, so an overlay test can resolve a definition another tier ships.

> **No API versioning yet.** The testkit tracks the current script `api` (1);
> that's revisited only when `api` first bumps.

## Precedence and testing

A user's `$XDG_CONFIG_HOME/credproxy/` file still wins over the overlays, which
win over builtin — for **every** asset including `workspace.template.toml`, so an
individual can override an org default locally with no exception. To verify an
overlay in place, point `CREDPROXY_OVERLAY_PATH` at it and run `credproxy info`,
`credproxy injector list`, `credproxy preset list`, `credproxy config`, or
`credproxy workspace create … && credproxy workspace … config --declared`.
