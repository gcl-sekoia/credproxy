# Injectors

An **injector** defines *how* a credential is shaped into a request for a
service: which header it rides in, how the value is formatted, and the shape of
the inert placeholder the workspace holds. It is the passive, service-specific
counterpart to a [provider](providers.md) (which defines *where* the value comes
from). A [binding](configuration.md#bindings) ties the two together.

Unlike providers — which are executables — injectors are **declarative TOML
files**: passive, reusable, drop-in. The filesystem is the registry; there is
nothing to install.

> **Status.** The injector authoring contract is provisional (it is listed as a
> deferred sub-design in `design-v2.md`). The schema below is what the tool
> implements today; the `format` field in particular is not yet load-bearing on
> the wire — see [The `format` field](#the-format-field).

## Discovery

An injector is referenced by `<name>`. Lookup order (first match wins, so a user
definition shadows a bundled one of the same name):

1. `$XDG_CONFIG_HOME/credproxy/injectors/<name>.toml` (default
   `~/.config/credproxy/injectors/<name>.toml`)
2. Bundled with the tool at `cli/credproxy_cli/bundled/injectors/<name>.toml`

`credproxy injector list` shows every resolvable injector and its source
(`user` or `bundled`).

## Schema

```toml
header = "Authorization"      # required: header carrying the credential
format = "Bearer {value}"     # optional, default "{value}"
env    = "GITHUB_TOKEN"       # optional: suggested workspace env var

[placeholder]                 # optional; pattern for the inert sentinel
prefix  = "ghp_"
length  = 40                  # total length including the prefix
charset = "alnumeric"         # alnumeric | hex | base64url
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `header` | string | — (required) | The request header the credential rides in (e.g. `Authorization`, `X-Api-Key`). Non-empty. This is what the proxy watches, and what `(host, header)` binding-uniqueness is enforced on. |
| `format` | string | `"{value}"` | How the credential appears inside the header value. Must contain the literal `{value}`. See the note below — currently informational. |
| `env` | string | none | Suggested workspace-side env var name, surfaced to the workspace via `/setup` and used as a binding's `env` default. |
| `[placeholder]` | table | the default pattern | Shape of the generated sentinel. Omit to use `prefix = "credproxy_"`, `length = 40`, `charset = "alnumeric"`. |
| `placeholder.prefix` | string | `"credproxy_"` | Literal leading characters. |
| `placeholder.length` | integer | `40` | Total length **including** the prefix. Must be greater than the prefix length. |
| `placeholder.charset` | string | `"alnumeric"` | Alphabet for the random body. One of the charsets below. |

### Charsets

| Name | Alphabet |
|---|---|
| `alnumeric` | `A–Z`, `a–z`, `0–9` (the safe, widely format-valid default) |
| `hex` | `0–9`, `a–f` |
| `base64url` | `A–Z`, `a–z`, `0–9`, `-`, `_` |

Validation errors (missing `header`, a `format` without `{value}`, an unknown
`charset`, a `length` not exceeding the prefix, a non-string `env`, …) are
reported as an injector error naming the file and field.

## Placeholders

The placeholder is the inert sentinel the workspace actually holds and the agent
actually sends. It is generated as `prefix` followed by random characters drawn
from `charset` (via Python's `secrets`) to reach `length`. The point is to be
**format-valid for the service** — the right prefix, length, and character set —
so client-side token-format checks pass, while the real value never leaves the
host.

A placeholder is generated **once**, when the binding is first materialized, and
written back into the workspace's config file so the workspace's environment and
the proxy's expectation can never drift. The injector only supplies the
*pattern*; the concrete value lives on the binding. See
[materialization](configuration.md#bindings).

## The `format` field

The proxy's substitution is a literal **substring replace** of the placeholder
with the real value, *inside whatever header value the client sent*. So the wire
config the proxy needs is just `placeholder → real`; the surrounding format
(`Bearer `, etc.) is already present in the request the workspace made.

That means `format` is, today, **documentation and an env-var hint** — it
describes what the workspace is expected to send (and so what the placeholder is
embedded in). It is kept in the schema because the authoring contract is
expected to evolve so the proxy applies the full format itself; until then,
treat it as informational. Practically: send the credential the way `format`
says (e.g. `Authorization: Bearer <placeholder>`), and the proxy swaps the
placeholder for the real token in transit.

## Bundled injectors

| Name | Header | Format | Placeholder | env hint |
|---|---|---|---|---|
| `github` | `Authorization` | `Bearer {value}` | `ghp_` + 36 alnum (40 total), mimics a classic PAT | `GITHUB_TOKEN` |
| `bearer` | `Authorization` | `Bearer {value}` | default (`credproxy_` + 30 alnum, 40 total) | none |

`bearer` doubles as the scaffold template for new injectors.

## Authoring your own

`credproxy injector scaffold NAME` copies the bundled `bearer` template to
`$XDG_CONFIG_HOME/credproxy/injectors/NAME.toml` (it refuses to overwrite an
existing file). Edit it, then reference it from a binding:

```sh
credproxy injector scaffold acme
$EDITOR ~/.config/credproxy/injectors/acme.toml
credproxy workspace myproj binding add \
    --injector acme --provider env --secret ACME_KEY --host api.acme.example
```

A worked custom injector — an API that wants the key bare in a custom header,
with service-shaped placeholders:

```toml
# ~/.config/credproxy/injectors/acme.toml
header = "X-Acme-Key"
format = "{value}"          # the key is sent verbatim, no prefix
env    = "ACME_API_KEY"

[placeholder]
prefix  = "acme_"
length  = 32
charset = "hex"
```

Because a binding's `placeholder` and `env` are materialized from the injector
the first time the binding is loaded, change an injector's pattern *before*
creating bindings that use it; existing bindings keep their already-materialized
values (the file stays the source of truth). To re-shape an existing binding,
edit or clear its `placeholder` in the workspace config.

## See also

- [`configuration.md`](configuration.md) — the workspace config and `[[binding]]` blocks that reference injectors
- [`providers.md`](providers.md) — the provider side: where a credential's value comes from
