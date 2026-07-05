← [04 · Your first credential](04-first-credential.md) · [index](../README.md) · [06 · Presets](06-presets.md) →

# 05 · Secret managers

The last chapter used the `env` [provider](../concepts.md#provider): your token
sat in a host environment variable. That is fine for a demo, but your real
secrets probably live in 1Password, the macOS Keychain, or your `gh` login.
This chapter swaps the provider. The rest of the binding does not change.

## The providers that ship

Run `credproxy provider list` to see what is built in:

```console
$ credproxy provider list
NAME               SOURCE   DESCRIPTION
bw                 builtin  Bitwarden (bw CLI)
docker-credential  builtin  Docker credential helper (registry auth)
env                builtin  Host environment variables
gh-cli             builtin  GitHub auth token (gh CLI)
keychain           builtin  macOS Keychain (security CLI)
op                 builtin  1Password (op CLI)
```

Each one fetches a secret from a different backend. They all speak the same tiny
protocol, so from credproxy's side they are interchangeable.

## The ref is provider-specific

A binding names a **secret reference** — the `--secret` value — that tells the
provider *which* secret to fetch. Its meaning depends on the provider. Ask any
provider how to write its ref with `credproxy provider show NAME`:

```console
$ credproxy provider show op
provider: op
  ...
  help:
    Reads secrets from 1Password (op CLI).
      ref:     a secret reference, op://<vault>/<item>/<field>
      example: --provider op --secret "op://Private/Anthropic/credential"
```

A few common ones:

| Provider | `--secret` ref | Example |
|---|---|---|
| `env` | environment variable name | `GITHUB_TOKEN` |
| `op` | 1Password secret reference | `op://Private/GitHub/token` |
| `keychain` | Keychain item service name | `github-token` |
| `bw` | Bitwarden `item[#field]` | `github#password` |
| `gh-cli` | a GitHub hostname (or empty) | `github.com` |

## Swap the provider

Take the binding from chapter 04. To read the token from 1Password instead of an
environment variable, only two flags change — `--provider` and the `--secret`
ref that matches it:

```sh
credp binding add \
    --injector bearer --provider op --secret 'op://Private/GitHub/token' \
    --host api.github.com --env GITHUB_TOKEN
```

The `--injector`, `--host`, and `--env` are identical. The injector decides how
the credential is *sent*; the provider decides where it is *read from*. They are
independent.

> [!NOTE]
> A binding is just a block in your config file. To change an existing binding's
> provider, you can also edit `provider` and `secret` in the TOML directly, then
> `credp apply`. The file is the source of truth.

## Dry-run the fetch before you start

You do not need to start a container to check that a provider can reach a
secret. `credp binding test` resolves each binding through its provider and
reports the length of what it got back — never the value:

```console
$ credp binding test
ok    bearer-op  (provider op, value length 40)
```

If 1Password is locked or the ref is wrong, this is where it fails, with the
provider's own error. Run it before `start` whenever you change a secret source.

There is also an **ad-hoc** form that tests a provider and ref *before* you bind
anything. It resolves no workspace, so the name is ignored:

```console
$ credproxy workspace binding test --provider op --secret 'op://Private/GitHub/token'
ok    op  (provider op, value length 40)
```

*(Sample output; requires `op` signed in.)* Add `--injector bearer` to also
check the injector resolves.

## Multi-slot secrets

Some injectors need more than one value. AWS `sigv4`, for example, signs each
request with an access-key id **and** a secret-access-key. You supply both by
repeating `--secret` as `SLOT=REF`:

```sh
credp binding add --injector sigv4 --provider env \
    --secret access_key_id=AWS_ACCESS_KEY_ID \
    --secret secret_access_key=AWS_SECRET_ACCESS_KEY \
    --host '*.amazonaws.com'
```

```console
$ credp binding add --injector sigv4 --provider env --secret access_key_id=AWS_ACCESS_KEY_ID --secret secret_access_key=AWS_SECRET_ACCESS_KEY --host '*.amazonaws.com'
added binding 'sigv4-env' to workspace 'myproject'
  injector    sigv4
  provider    env
  secret      access_key_id=AWS_ACCESS_KEY_ID, secret_access_key=AWS_SECRET_ACCESS_KEY
  hosts       *.amazonaws.com
  placeholder (none)
```

The injector declares which slots it needs; each slot names its own ref, and the
refs can even come from the same provider. (`sigv4` has no placeholder — it
re-signs each request rather than swapping a token. The
[injectors reference](../reference/injectors.md) covers that.)

> [!TIP]
> Notice `--host '*.amazonaws.com'`: a host can be a glob, so one binding covers
> every AWS regional endpoint. The full pattern rules are in the
> [configuration reference](../reference/configuration.md#host-patterns).
> Otherwise, continue.

---

**Next:** [06 · Presets](06-presets.md) — wire every host of a service with one
command.
