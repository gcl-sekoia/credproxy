← [06 · Presets](06-presets.md) · [index](../README.md) · [08 · Going further](08-going-further.md) →

# 07 · Rules

Bindings put credentials *into* requests. A [rule](../concepts.md#rule) does the
opposite kind of job: it governs traffic on a host you name, with no credential
involved. A rule can **block** a request, **respond** with a canned answer, or
**rewrite** its headers. Use rules as guardrails — for example, to stop a tool
in the workspace from deleting a repository.

## Block a request

The `github` preset from the last chapter lets the workspace reach
`api.github.com`. Suppose you want that, but never a `DELETE`. Add a block rule:

```console
$ credp rule add block --host api.github.com --method DELETE
added rule 'block-api-github-com' to workspace 'myproject'
  hosts    api.github.com
  methods  DELETE
  action   block
  visible
run `credproxy workspace myproject start` (or `apply`) to push it to the proxy
```

`block` is a subcommand, not a flag — each action (`block`, `respond`,
`rewrite`, `script`) owns its own options. `block` refuses the request with a
403 by default; `--status N` picks another code. Scope it with any mix of
`--host` (repeatable, globs allowed), `--method`, and `--path` (a glob like
`/repos/**`).

## Test it without a container

`credp rule test METHOD URL` answers "would this be blocked, and by which rule?"
It reads your config file, so no proxy needs to be running:

```console
$ credp rule test DELETE https://api.github.com/repos/me/project
matched: block-api-github-com → block 403
$ credp rule test GET https://api.github.com/user
no rule matches GET https://api.github.com/user
```

This is the tool to reach for whenever a request is blocked and you want to know
why, or why not.

## The other actions

- **`respond`** returns a fixed response and never contacts the server —
  `credp rule add respond --host api.openai.com --path /v1/models --status 200
  --body '{}'`. Good for stubbing an endpoint during tests.
- **`rewrite`** adds or removes headers on the request (or the response) and lets
  it continue — `credp rule add rewrite --host api.github.com --header
  'X-Trace=1'`.

Rules are evaluated in the order they appear in your config. Rewrites accumulate;
the first `block` or `respond` that matches wins and stops the rest.

## Visibility: what the workspace sees

Every rule is either **visible** or **hidden** — this controls what the
*workspace* learns, never what you as the operator can see (your logs always show
every rule). A visible block identifies itself: it adds an `X-Credproxy-Rule`
header and a small JSON body saying which rule fired. A hidden block is a bare
status code with no explanation, which is what you want when a fake failure must
be indistinguishable from a real one.

Terminal actions (`block`, `respond`) default to visible; `rewrite` and `script`
default to hidden. Flip either with `--visible` or `--hidden`. `credp rule list`
marks the hidden ones:

```console
$ credp rule list
NAME                  HOSTS           METHODS  PATH  ACTION  VISIBILITY
block-api-github-com  api.github.com  DELETE   *     block   visible
```

> [!WARNING]
> Adding a rule to a host that had **no** binding flips that host from
> pass-through to intercepted. The proxy only opens TLS on hosts you name for a
> binding *or* a rule; naming a fresh host for a rule adds it to that set. A
> workspace that has not installed the proxy's certificate will then get a **TLS
> certificate error** on that host — not a 403. If a rule seems to break a host
> instead of governing it, this is almost always why. The
> [troubleshooting page](../troubleshooting.md) has the fix.

> [!NOTE]
> Rules are not an egress allowlist. They govern HTTP only on hosts you name;
> every unnamed host still passes straight through, untouched and unblockable.
> credproxy is a credential boundary, not a firewall — see
> [Security](../security.md).

> [!TIP]
> Rules can also run a sandboxed script for logic a flag cannot express —
> redacting a field, conditional blocking. That is the
> [rules reference](../reference/rules.md). Otherwise, continue.

---

**Next:** [08 · Going further](08-going-further.md) — many workspaces, mounts,
custom images, and where to go from here.
