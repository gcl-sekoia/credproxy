---
name: suggesting-bindings
description: >-
  Suggest copy-pasteable credproxy `[[binding]]` definitions when you are an
  agent running inside a credproxy workspace and need a credential injected into
  your outbound requests. Use this whenever you are behind a credproxy proxy and
  need to call an authenticated API but only hold placeholder tokens — you hit a
  401/403, an API needs a key/token you don't have, a TLS-trust error appears on
  an intercepted host, or the operator asks how to add a credential, token, API
  key, secret, or "binding" to the workspace. You cannot run the host `credproxy`
  CLI, so this produces a ready-to-paste binding block plus the exact `binding
  add` command for the operator, and picks the right injector (bearer, basic,
  body, sigv4, jwt-bearer, ovh, oauth2-reseal) and provider (env, op, keychain,
  bw, gh-cli, docker-credential).
---

# Suggesting credproxy bindings

You are an agent running **inside a credproxy workspace container**. Your egress
is transparently intercepted by a proxy that can inject real credentials into
your outbound requests — so you can call authenticated APIs **without ever
holding the secret**. The secret lives on the host; you only ever see an inert
`placeholder`.

Your job with this skill: propose **copy-pasteable `[[binding]]` definitions** for
the credentials you (or the operator) need. You **cannot** create bindings
yourself — the `credproxy` CLI runs on the host, which you can't reach. You
produce a precise, ready-to-use block and the one command that installs it, and
hand it to the operator.

## First, look at what's already wired

Before suggesting anything, check the live state — the proxy exposes it and you
*can* reach this from inside the workspace:

```
curl -s http://proxy.local/setup
```

That returns the currently-intercepted hosts and the existing bindings (names,
placeholders, env vars, schemes, hosts) — **never** secret values. If the
credential you need is already bound, there's nothing to suggest: use the env var
it names (it holds the placeholder; send it like a real token). Only suggest a new
binding for something *not* already there.

## The mental model

A **binding** answers three questions:

1. **Which hosts?** (`hosts`) — the security scope. The real credential is
   injected **only** on requests to these hosts; every other host sees nothing.
2. **How is the credential shaped into the request?** (`injector`) — a bearer
   header, HTTP Basic, a signed request, a form-body field, …
3. **Where does the real value come from?** (`provider` + `secret`) — an env var
   on the host, a 1Password item, the `gh` login, a Bitwarden entry, …

You know (1) and (2) from the API you're calling. You usually **cannot know (3)**
— where the operator keeps secrets is their choice. So propose a sensible default
and **clearly mark `provider`/`secret` as operator-adjustable**, unless the
operator has told you where the secret lives.

**You never see or handle the real secret.** After a binding is installed, the
workspace env var named by `env` holds the *placeholder* in your login shell. Use
it exactly as you would the real token; the proxy swaps it on the way out.

## What to hand the operator

Give **both** of these when you can:

**A. The `[[binding]]` TOML block** — pasted into the workspace config file
(`$XDG_CONFIG_HOME/credproxy/workspaces/<name>.toml`), then applied with
`credproxy workspace <name> apply`:

```toml
[[binding]]
name     = "openai"              # any unique handle
injector = "bearer"
provider = "env"                 # EDIT: where the secret actually lives
secret   = "OPENAI_API_KEY"      # EDIT: the ref your provider understands
hosts    = ["api.openai.com"]
env      = "OPENAI_API_KEY"      # the workspace env var set to the placeholder
```

**B. The equivalent `binding add` command** — one line, no file editing:

```
credproxy workspace <name> binding add \
    --injector bearer --provider env --secret OPENAI_API_KEY \
    --host api.openai.com --name openai --env OPENAI_API_KEY
```

Rules for good suggestions:

- **Always mark `provider`/`secret` EDIT-me** unless you have real evidence of
  where the secret lives. Default to `provider = "env"` with an obvious env-var
  name.
- **Omit `placeholder`.** It is auto-generated into the host-side lockfile;
  hand-setting it is only for matching a strict token-format check.
- **Scope `hosts` as tightly as correctness allows.** Use a glob only when the API
  genuinely spans subdomains (see *Host scoping*).
- **`env` names the workspace var that carries the placeholder.** Set it to the
  var your client already reads (`OPENAI_API_KEY`, `GITHUB_TOKEN`, …). Omit it (or
  `env = false`) for sign-family injectors that add their own headers.

## Pick the injector

Choose by **how the API expects the credential**. All are built in.

| Injector | Family | Use it for | Placeholder? |
|---|---|---|---|
| `bearer` | substitute | `Authorization: Bearer <token>` — most REST APIs (OpenAI, Stripe, Anthropic, GitHub PAT on `api.github.com`) | yes (auto) |
| `basic` | substitute | HTTP Basic — git-over-HTTPS, container registries | yes (auto) |
| `body` | substitute | A secret the client puts in the **request body** (OAuth2 `client_secret=…`) | yes (auto) |
| `sigv4` | sign | AWS SigV4 — any `*.amazonaws.com` API | no |
| `jwt-bearer` | sign | Self-signed RS256 JWT — GCP service accounts, RFC 7523 | optional |
| `ovh` | sign | OVH API request signing (`X-Ovh-*` headers) | optional |
| `oauth2-reseal` | re-seal | OAuth2 client-credentials where the returned token must **also** stay out of the workspace | dynamic |

Quick rules of thumb:

- Token in an `Authorization: Bearer` header → **`bearer`**.
- Username/password or HTTP Basic (git, registries) → **`basic`**.
- Secret goes in the POST body → **`body`**.
- AWS → **`sigv4`** (multi-slot; also set dummy `AWS_*` vars in the workspace so
  the SDK signs a request for the proxy to re-sign).
- GCP service-account / private-key JWT → **`jwt-bearer`** (needs `iss`/`aud`).
- OVH → **`ovh`** (multi-slot).
- Must hide even the short-lived OAuth access token → **`oauth2-reseal`** (needs
  `api_hosts`).
- GitHub → the **`github` pack** (see below), not hand-written bindings.

For the full per-family behavior, the injectors that must be **copied and edited**
before use (`oauth2-reseal`, `jwt-bearer`), multi-slot secret syntax, and worked
examples, read **`references/injectors.md`**.

## Providers: where the value comes from

`provider` is a host-side executable; `secret` is a ref it understands. You
usually can't know which the operator uses — **suggest one and say to adjust it.**

| Provider | `secret` ref is… | Example |
|---|---|---|
| `env` | a host **environment variable name** | `OPENAI_API_KEY` |
| `op` | a **1Password** reference | `op://Private/OpenAI/credential` |
| `keychain` | a macOS Keychain **service name** | `openai-key` |
| `bw` | a **Bitwarden** item `<item>[#<field>]` | `openai` / `aws#access_key_id` |
| `gh-cli` | a **GitHub hostname** (uses the `gh` login) | `github.com` |
| `docker-credential` | a **registry host** | `ghcr.io` |

`env` is the safest default to propose. For GitHub, prefer the `github` pack.

## Host scoping

`hosts` is the security boundary. Each entry is a **literal** hostname (exact
match) or a **glob** with `*` (spans dots): `*.amazonaws.com` matches every AWS
regional endpoint. The two rightmost labels must be literal — `*.example.com` ✓,
`*.com` / `*` ✗ (rejected: credproxy is **not** an egress allowlist). Scope as
tightly as correct; one binding can list multiple hosts.

## Packs: coordinated bindings

Some services need several bindings sharing one credential. A **pack** installs
them together. Built-in: **`github`** — one token wired as `bearer` on
`api.github.com` and HTTP `basic` on `github.com` + `ghcr.io`, off the `gh` login:

```
credproxy workspace <name> pack add github
```

Suggest this instead of hand-writing GitHub bindings. `credproxy pack list` shows
the full expansion.

## What you must not do

- **Don't ask for, print, or store the real secret.** You only touch
  placeholders. If a task seems to need the real value *in* the workspace, that's
  a sign the wrong injector was chosen — reach for a sign/re-seal injector.
- **Don't invent injector or provider names.** Use the ones above. If none fits,
  describe the auth flow and say a custom injector/script is a host-side task.
- **Don't propose `hosts = ["*"]` or a bare-TLD glob.** It's rejected by design.
- **Don't disable TLS verification** to "fix" a cert error on an intercepted
  host. The fix is to trust the proxy CA:
  `eval "$(curl -s http://proxy.local/env.sh)"` in the same shell.
