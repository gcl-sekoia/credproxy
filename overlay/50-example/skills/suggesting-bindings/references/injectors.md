# Injector reference

Full behavior of each injector family, the injectors that need editing before
use, multi-slot secrets, custom headers, and worked examples. Read this when the
compact table in `SKILL.md` isn't enough to write a correct binding.

## How each family behaves

### substitute — `bearer`, `basic`, `body`, `query`

The workspace sends the inert placeholder; the proxy swaps it for the real value
on the scoped hosts. The common case. The env var named by `env` carries the
placeholder, so a client that reads that var "just works."

- **`bearer`** — swaps the placeholder inside the `Authorization` header (the
  `Bearer ` prefix the client sends is left as-is). Single slot: `value`. The
  header is configurable via a custom injector (see *Custom headers*).
- **`basic`** — decodes the `Authorization: Basic` blob, swaps the component equal
  to the placeholder (the password by default), and re-encodes. The placeholder is
  a **bare token** — no hand-computed base64. Single slot: `value`.
- **`body`** — swaps the placeholder anywhere in the request **body**. For OAuth2
  client-credentials (`client_secret=…` in a form/JSON body) and other
  key-in-body APIs. Single slot: `value`.
- **`query`** — swaps the placeholder inside the URL **query string**,
  percent-encoding the value. For APIs that authenticate via a query parameter
  with no header form (Shodan's `?key=…`); the workspace sends
  `?key=<placeholder>`. Single slot: `value`. Prefer a header scheme when the API
  offers one — a query credential can leak back if the server echoes the URL
  (redirects, error bodies).

### sign — `sigv4`, `ovh`, `jwt-bearer`

The credential is a **signing key that never transits the wire**, so there is
nothing to substitute. The proxy adds/rewrites the signature on every matching
request. Usually **no placeholder and no `env`**.

- **`sigv4`** — AWS Signature V4. The workspace's AWS SDK signs each request with
  **throwaway** credentials; the proxy parses the scope, recomputes the canonical
  request, and re-signs with the real key. Region/service are read from the
  request, so one binding on `*.amazonaws.com` covers everything. **Workspace-side
  setup:** set dummy `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` to any
  non-empty values so the SDK produces a signed request. Multi-slot:
  `access_key_id`, `secret_access_key`.
- **`ovh`** — computes the four OVH signature headers (`X-Ovh-Application`,
  `X-Ovh-Consumer`, `X-Ovh-Timestamp`, `X-Ovh-Signature`). Multi-slot: `app_key`,
  `app_secret`, `consumer_key`.
- **`jwt-bearer`** — mints a fresh self-signed RS256 JWT per request, signed with
  an RSA private key from the provider, and sets `Authorization: Bearer <jwt>`.
  For GCP service-account direct auth and RFC 7523 private-key JWT assertions.
  Single slot: `private_key` (PEM). **Needs `iss`/`aud`/`ttl`** — copy and edit
  (see *Injectors that need editing*).

Sign-family injectors optionally take a `placeholder` on the binding: the proxy
then signs only requests carrying it, enabling per-request opt-in and several
identities on one host. Rarely needed — suggest it only if asked.

### re-seal — `oauth2-reseal`

For the OAuth2 client-credentials flow where **even the short-lived returned
token** must stay out of the workspace. Scope the binding to the **token
endpoint** host. On the token request the proxy swaps the `client_secret`
placeholder; on the token response it mints the returned `access_token` into a
fresh dynamic placeholder registered on `api_hosts`, and rewrites the response so
the workspace receives the placeholder. Later calls to `api_hosts` carry that
placeholder, which the proxy swaps for the real token. Single slot: `value` (the
durable `client_secret`). **Needs `api_hosts`** — copy and edit.

There is also a **scripted twin, `oauth-reseal`** — the same re-seal flow written
as a `.star` script rather than the built-in `oauth2-reseal` scheme. Prefer the
built-in `oauth2-reseal`; reach for `oauth-reseal` only when the operator needs to
customize the flow (it's the template to copy). Don't suggest it as the default.

## Injectors that need editing before use

`oauth2-reseal` and (for `iss`/`aud`/`ttl`) `jwt-bearer` carry `[params]` that are
deployment-specific. A **user copy shadows the builtin**:

```
cp <builtin>/injectors/oauth2-reseal.toml $XDG_CONFIG_HOME/credproxy/injectors/
# then edit [params].api_hosts (the hosts where the minted token is used), etc.
```

Tell the operator this rather than pretending a bare binding will work. Do **not**
suggest a plain `oauth2-reseal` binding without flagging the copy-and-edit step.

## Custom headers (a non-default header)

The header a `bearer`/`basic` injector uses is fixed on the **injector**, not the
binding — a `[[binding]]` has no `params` field, so a `[binding.params]` sub-table
is silently ignored. When an API reads a header other than `Authorization` (e.g.
Anthropic's `x-api-key`), the operator copies the injector and sets the param:

```toml
# $XDG_CONFIG_HOME/credproxy/injectors/x-api-key.toml
scheme = "bearer"
[params]
header = "x-api-key"
```

```toml
# then in the workspace config:
[[binding]]
name     = "anthropic"
injector = "x-api-key"           # the custom injector above
provider = "op"                  # EDIT to your secret store
secret   = "op://Private/Anthropic/credential"   # EDIT
hosts    = ["api.anthropic.com"]
env      = "ANTHROPIC_API_KEY"
```

## Multi-slot secrets

`sigv4` and `ovh` need several values at once. Use an inline table mapping each
slot to a ref instead of a bare string:

```toml
[[binding]]
name     = "aws"
injector = "sigv4"
provider = "env"                 # EDIT to your secret store
secret   = { access_key_id = "AWS_ACCESS_KEY_ID", secret_access_key = "AWS_SECRET_ACCESS_KEY" }
hosts    = ["*.amazonaws.com"]
```

Command form repeats `--secret SLOT=REF`:

```
credproxy workspace <name> binding add --injector sigv4 --provider env \
    --secret access_key_id=AWS_ACCESS_KEY_ID \
    --secret secret_access_key=AWS_SECRET_ACCESS_KEY \
    --host '*.amazonaws.com' --name aws
```

A single-slot scheme whose slot isn't named `value` (e.g. `jwt-bearer`'s
`private_key`) still needs the explicit slot name:
`secret = { private_key = "<REF>" }` / `--secret private_key=<REF>`.

## Worked examples

**OpenAI (bearer, env):**
```toml
[[binding]]
name     = "openai"
injector = "bearer"
provider = "env"                 # EDIT to your secret store
secret   = "OPENAI_API_KEY"      # EDIT to your ref
hosts    = ["api.openai.com"]
env      = "OPENAI_API_KEY"
```

**A private container registry (basic):**
```toml
[[binding]]
name     = "registry"
injector = "basic"
provider = "docker-credential"   # or env / op / …
secret   = "registry.example.com"
hosts    = ["registry.example.com"]
```

**Shodan (query) — the key rides the URL `?key=…`, not a header:**
```toml
[[binding]]
name     = "shodan"
injector = "query"
provider = "env"                 # EDIT to your secret store
secret   = "SHODAN_API_KEY"      # EDIT to your ref
hosts    = ["api.shodan.io"]
env      = "SHODAN_API_KEY"       # the client sends ?key=<placeholder>
```

**AWS (sigv4) — also set dummy `AWS_*` vars in the workspace:**
```toml
[[binding]]
name     = "aws"
injector = "sigv4"
provider = "env"                 # EDIT to your secret store
secret   = { access_key_id = "AWS_ACCESS_KEY_ID", secret_access_key = "AWS_SECRET_ACCESS_KEY" }
hosts    = ["*.amazonaws.com"]
```

**GCP service account (jwt-bearer) — after copying the injector to set iss/aud:**
```toml
[[binding]]
name     = "gcp"
injector = "jwt-bearer"          # a copy with [params].iss/aud edited for your SA
provider = "op"                  # EDIT to your secret store
secret   = { private_key = "op://Private/gcp-sa/private_key" }   # EDIT
hosts    = ["oauth2.googleapis.com"]
```

**GitHub — prefer the pack:**
```
credproxy workspace <name> pack add github
```
