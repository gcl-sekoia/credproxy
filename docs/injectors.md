# Injectors

An **injector** defines *how* a credential is shaped into a request for a
service: which typed **scheme** the proxy runs, the scheme's params, and the
shape of the inert placeholder the workspace holds. It is the passive,
service-specific counterpart to a [provider](providers.md) (which defines
*where* the value comes from). A [binding](configuration.md#bindings) ties the
two together.

Unlike providers — which are executables — injectors are **declarative TOML
files**: passive, reusable, drop-in. The filesystem is the registry; there is
nothing to install.

## Schemes

The proxy implements a small, fixed set of typed **schemes** (design-v3). An
injector picks one and parameterizes it; the explosion of services rides on top
as configuration, not code. Schemes fall into two families:

- **substitute** — the workspace holds an inert placeholder and sends it; the
  proxy finds it in the scheme's wire location and swaps in the real value,
  decoding/re-encoding as needed.
- **sign** — no usable static value on the wire; the proxy holds a signing key
  and computes the auth material per request.

| Scheme | Family | Params | Slots | Covers |
|---|---|---|---|---|
| `bearer` | substitute | `header` (default `Authorization`) | `value` | most REST APIs (PATs, OpenAI, Stripe, …) |
| `basic` | substitute | `header` (default `Authorization`) | `value` | git-over-HTTPS, registries, any HTTP Basic |
| `body` | substitute | — | `value` | OAuth2 client-credentials, key-in-body APIs |
| `sigv4` | sign | — | `access_key_id`, `secret_access_key` | AWS + all S3-compatible services |

`bearer` substring-swaps the placeholder for the real value inside the named
header (any `Bearer `/`token ` prefix the client sent is left intact). `basic`
decodes the `Authorization: Basic` blob, swaps the component equal to the
placeholder (password by default, or username), and re-encodes — so the
placeholder is a **bare token**, never hand-computed base64. `body` swaps the
placeholder anywhere in the request body.

`sigv4` (sign family) is different: the AWS secret is a *signing key* that never
transits the wire, so there is no placeholder. The workspace's AWS SDK signs
each request with **throwaway** credentials; the proxy reads the credential
scope (region/service) the SDK chose from the incoming `Authorization`,
recomputes the canonical request, and re-signs it with the real key. It is a
**multi-slot** scheme (`access_key_id` + `secret_access_key`); region and
service are read from the request, so it takes no params.

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
scheme = "bearer"             # required: a scheme name from the table above
env    = "GITHUB_TOKEN"       # optional: suggested workspace env var

[params]                      # optional; scheme-specific (defaults merged in)
header = "Authorization"      #   bearer/basic: the header the credential rides in

[placeholder]                 # optional; pattern for the inert sentinel
prefix  = "ghp_"
length  = 40                  # total length including the prefix
charset = "alnumeric"         # alnumeric | hex | base64url
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `scheme` | string | — (required) | The typed scheme the proxy runs. Must be a known scheme. |
| `[params]` | table | scheme defaults | Scheme-specific settings, merged onto the scheme's defaults and passed to the proxy verbatim. For `bearer`/`basic`, `header` selects the header. |
| `env` | string | none | Suggested workspace-side env var name, surfaced via `/setup` and used as a binding's `env` default. |
| `[placeholder]` | table | the default pattern | Shape of the generated sentinel. Omit to use `prefix = "credproxy_"`, `length = 40`, `charset = "alnumeric"`. |
| `placeholder.prefix` | string | `"credproxy_"` | Literal leading characters. |
| `placeholder.length` | integer | `40` | Total length **including** the prefix. Must exceed the prefix length. |
| `placeholder.charset` | string | `"alnumeric"` | Alphabet for the random body. One of the charsets below. |

### Charsets

| Name | Alphabet |
|---|---|
| `alnumeric` | `A–Z`, `a–z`, `0–9` (the safe, widely format-valid default) |
| `hex` | `0–9`, `a–f` |
| `base64url` | `A–Z`, `a–z`, `0–9`, `-`, `_` |

Validation errors (missing/unknown `scheme`, a non-table `[params]`, an unknown
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

Because injection is now scheme-aware, you send the credential the natural way
for the service (e.g. `Authorization: Bearer <placeholder>`, or a
`base64(user:<placeholder>)` Basic blob your git client builds itself) and the
scheme does the right transform in transit. There is no `format` field — the
scheme owns the wire shape.

## Bundled injectors

| Name | Scheme | Params | Placeholder | env hint |
|---|---|---|---|---|
| `bearer` | `bearer` | `header = Authorization` | default (`credproxy_` + 30 alnum, 40 total) | none |
| `basic` | `basic` | `header = Authorization` | default | none |
| `body` | `body` | — | default | none |
| `sigv4` | `sigv4` | — | none (sign family) | none |

A `sigv4` binding uses a multi-slot secret, e.g.:

```sh
credproxy workspace NAME binding add --injector sigv4 --provider env \
    --secret access_key_id=AWS_ACCESS_KEY_ID \
    --secret secret_access_key=AWS_SECRET_ACCESS_KEY \
    --host sts.amazonaws.com
```

In the workspace, configure any throwaway AWS credentials (e.g. dummy
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) so the SDK produces a signed
request for the proxy to re-sign.

`bearer` doubles as the scaffold template for new injectors. A GitHub PAT, which
is `bearer` on `api.github.com` but HTTP `basic` on `github.com`/`ghcr.io`, is
generated as a coordinated set by `binding add --preset github` (the three
bindings share one bare-token placeholder) — see
[configuration.md](configuration.md#bindings).

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
scheme = "bearer"           # substring-swap in a header
env    = "ACME_API_KEY"

[params]
header = "X-Acme-Key"       # the key rides here, sent verbatim

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

## Scripted injectors (the escape hatch)

When the built-in schemes can't express a service's auth (a bespoke signature, a
multi-step token mint), an injector can run a **Starlark script** in the proxy
instead. The injector TOML stays declarative — it sets `scheme = "script"`,
names a `.star` file, and declares the metadata the host CLI can't infer by
reading Starlark (`family`, `slots`, and the wire `location_kind`). The script
carries only the logic: `on_request(ctx)` (and optionally `on_response(ctx)`).

```toml
# ~/.config/credproxy/injectors/myservice.toml
scheme = "script"
script = "myservice"         # resolves myservice.star (user dir, then bundled)
family = "sign"              # "substitute" (placeholder) | "sign" (no placeholder)
slots  = ["value"]           # secret slot names the script reads
location_kind = "header"     # where it writes, for host-collision detection
env    = "MYSERVICE_TOKEN"

[params]                      # passed to the script verbatim (ctx via param())
header = "X-MyService-Auth"
```

```python
# ~/.config/credproxy/scripts/myservice.star
def on_request(ctx):
    header_set(ctx, param(ctx, "header", "Authorization"),
               "Bearer " + secret(ctx))
    return True
```

The script discovery mirrors injectors/providers (`$XDG_CONFIG_HOME/credproxy/
scripts/<name>.star`, then the bundled set). At `start`/`apply`/`binding test`
the CLI reads the `.star` **source** and pushes it to the proxy with the config
(the push model — the proxy stays stateless and compiles what it's given, so
your scripts work with no mounts or image rebuilds).

**Sandbox.** The script runs in the proxy with access to the real credential via
`secret()`, so it is sandboxed: only the trusted primitives are callable (header
get/set, body read/replace, `b64encode`/`b64decode`, `secret`, `placeholder`,
`param`, and the crypto primitives the proxy owns); there is no `print`, I/O,
filesystem, network, `import`, or `load()`. A script can only shape the request
bound for the binding's already-fixed host, so even a shared third-party script
can't choose a destination or exfiltrate the secret. Errors fail closed (the
request is forwarded unmodified). See `proxy/starlark_runtime.py`.

### Primitives available to scripts

A script defines `on_request(ctx)` (return `True` if it injected, `False` to
skip) and optionally `on_response(ctx)`. Function names must not start with `_`.

The full set of trusted primitives:

| Primitive | Returns | Purpose |
|---|---|---|
| `secret(ctx, slot="value")` | `str` | The resolved credential for a slot — the only door to the real value. |
| `placeholder(ctx)` | `str\|None` | The inert placeholder (substitute family). |
| `param(ctx, key, default=None)` | `str` | An injector `[params]` value. |
| `header_get(ctx, name)` | `str\|None` | Read a request header (request phase). |
| `header_set(ctx, name, value)` | — | Write a request header. |
| `body_text(ctx)` | `str\|None` | Read the request body as text. |
| `set_body_text(ctx, text)` | — | Replace the request body. |
| `method(ctx)` | `str` | HTTP method of the current request. |
| `path(ctx)` | `str` | Request path including query string. |
| `host(ctx)` | `str` | Request host. |
| `b64encode(s)` | `str` | Standard base64-encode a UTF-8 string. |
| `b64decode(s)` | `str` | Standard base64-decode to a UTF-8 string. |
| `b64url_encode(s)` | `str` | Unpadded URL-safe base64 (the JWS form). |
| `hex_sha1(s)` | `str` | Hex-encoded SHA-1 digest of a UTF-8 string. |
| `hex_sha256(s)` | `str` | Hex-encoded SHA-256 digest. |
| `hmac_sha256_hex(key, msg)` | `str` | Hex-encoded HMAC-SHA-256. |
| `rs256_sign_b64url(private_key_pem, msg)` | `str` | RS256 signature of `msg`, unpadded base64url. |
| `json_encode(value)` | `str` | Compact JSON of a dict/list/str/int/bool/None. |
| `now()` | `int` | Current time as Unix seconds. |

The crypto and encoding primitives are owned and trusted by the proxy; scripts
orchestrate them and never implement crypto. The Starlark environment has no
`print`, `import`, `load()`, I/O, or JSON builtin — use `json_encode()` for
JSON serialization and `+` for string concatenation (no f-strings).

### Bundled scripted injectors

Two sign-family examples ship as bundled injectors.

**`ovh`** — signs OVH API requests. Sets `X-Ovh-Application`,
`X-Ovh-Consumer`, `X-Ovh-Timestamp`, and `X-Ovh-Signature` (`"$1$" +`
hex_sha1 over the concatenated signing string). Slots: `app_key`,
`app_secret`, `consumer_key`.

```sh
credproxy workspace NAME binding add --injector ovh --provider env \
    --secret app_key=OVH_APP_KEY \
    --secret app_secret=OVH_APP_SECRET \
    --secret consumer_key=OVH_CONSUMER_KEY \
    --host eu.api.ovh.com
```

**`jwt-bearer`** — mints a self-signed RS256 JWT assertion from an RSA private
key and sets `Authorization: Bearer <jwt>`. Slot: `private_key`. Params:
`iss`, `aud`, `ttl` (set in the injector TOML; copy it to your user injectors
dir to customize, since a user injector shadows the bundled one).

```sh
credproxy workspace NAME binding add --injector jwt-bearer --provider env \
    --secret private_key=GCP_SA_PRIVATE_KEY --host api.example.com
```

A representative excerpt from `jwt-bearer.star` showing how the primitives
compose:

```python
def on_request(ctx):
    now_ts = now()
    ttl    = int(param(ctx, "ttl", "3600"))

    header = json_encode({"alg": "RS256", "typ": "JWT"})
    claims = json_encode({
        "iss": param(ctx, "iss"),
        "aud": param(ctx, "aud"),
        "iat": now_ts,
        "exp": now_ts + ttl,
    })

    signing_input = b64url_encode(header) + "." + b64url_encode(claims)
    sig = rs256_sign_b64url(secret(ctx, "private_key"), signing_input)
    jwt = signing_input + "." + sig

    header_set(ctx, "Authorization", "Bearer " + jwt)
    return True
```

> **Status (design-v3 phase 3b).** The runtime, sandbox, full primitive set
> (including sign-family crypto: `hmac_sha256_hex`, `rs256_sign_b64url`,
> `json_encode`, `now`, and request introspection), and the bundled `ovh` and
> `jwt-bearer` examples are implemented. The runaway-deadline mechanism is
> wired and verified against starlark-pyo3's call-path `check_cancelled`
> (feature-detected); it activates automatically once a wheel carrying that
> support is published. Until then a non-terminating script hangs the proxy —
> scripts are trusted host config.

## See also

- [`configuration.md`](configuration.md) — the workspace config and `[[binding]]` blocks that reference injectors
- [`providers.md`](providers.md) — the provider side: where a credential's value comes from
