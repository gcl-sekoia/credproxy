# Concepts

[← docs index](README.md)

This page defines every term credproxy uses. Other pages link here on first
use. Each entry is one plain sentence and one concrete example. Read it once,
then use it as a reference.

---

### Workspace

A named, persistent development environment: one container you work in, plus a
private proxy that sits in front of its network. You create a workspace once,
then start, enter, and stop it by name.

*Example:* `credp create myproject` makes a workspace called `myproject`. Its
config lives in one file, `myproject.toml`.

### Proxy

The container that sits between your workspace and the internet. It holds your
real credentials, watches outbound requests, and swaps in the real credential
for approved hosts. You never work inside the proxy; credproxy manages it for
you.

*Example:* every workspace gets its own proxy container, started automatically
by `credp start`.

### Placeholder

A fake credential that is the right shape but carries no secret. It lives inside
the workspace so tools have something to send. The proxy replaces it with the
real credential on the way out.

*Example:* your workspace holds `ghp_AOFWLTeyzi8jUF1YTApGxjlCpXn62zQ4KpX7` (a
correctly-shaped but inert GitHub token — `ghp_` plus 36 characters). The proxy
swaps it for your real token when you call `api.github.com`.

### Provider

Where a real credential comes from. A provider is a small program on your host
that fetches one secret when asked. credproxy ships several.

*Example:* the `env` provider reads a host environment variable. The `op`
provider reads a 1Password item. The `gh-cli` provider reads your `gh` login.

### Injector

How a credential is placed into a request. Different services expect
credentials in different forms, so you pick the injector that matches.

*Example:* the `bearer` injector sends the credential as an
`Authorization: Bearer <token>` header. The `basic` injector sends it as HTTP
Basic auth. The `sigv4` injector signs the request the way AWS expects.

### Binding

The rule that ties one credential to one or more hosts: a provider (where the
value comes from) plus an injector (how it is sent) plus the hosts it applies
to. A binding is what makes a credential actually work inside a workspace.

*Example:* "use the `env` provider's `GITHUB_TOKEN`, send it as a `bearer`
token, but only to `api.github.com`."

### Preset

A ready-made set of bindings (and optional rules) for one service, so you do not
have to wire each host by hand. One credential often needs different injectors on
different hosts of the same service.

*Example:* the `github` preset creates three bindings from one token: `bearer`
on `api.github.com`, `basic` on `github.com`, and `basic` on `ghcr.io`.

### Rule

A credential-free policy for traffic on a host you name: block a request, return
a canned response, or rewrite headers. Rules never touch secrets; they add
guardrails.

*Example:* a rule that blocks every `DELETE` request to `api.github.com`, so a
tool in the workspace cannot delete a repository.

### Overlay

A folder where your team can ship its own defaults, providers, injectors, and
presets without changing credproxy's code. credproxy searches your overlays
before its built-in definitions.

*Example:* your team ships an overlay with a `vault` provider and a custom
workspace template that every colleague picks up automatically.

### Attached workspace

A workspace where another tool (Docker Compose, a devcontainer, CI) runs the
containers, and credproxy only supplies credentials. You use `push` instead of
`start` for it.

*Example:* CI brings up a proxy and a job container with Compose, and credproxy
pushes the resolved secrets into the running proxy.

---

Want the full picture of how these fit together at runtime? →
[How it works](how-it-works.md). Ready to build one? →
[The guide](guide/01-install.md).
