# overlay: github-auth (lib)

Makes `gh` and `git push` (over HTTPS) work from inside the workspace using GitHub
credentials injected on the wire — the real token never enters the container. It's the
setup half; the credentials themselves come from two `[[binding]]`s in the workspace
config (below).

How it works: the proxy swaps a placeholder for the real token on requests to
`api.github.com` (bearer) and `github.com` (HTTP basic). `gh` picks up the placeholder
from `$GITHUB_TOKEN` automatically; `git` can't read that env var, so this lib points
git's credential helper at gh's (`gh auth git-credential`), which serves the placeholder
at push time — and the `basic` scheme swaps it. Git identity is taken from the
authenticated account.

**Contributes:** a `setup.d` step (git credential helper + git identity). No secret,
no host helper.

## Compose from a profile

```toml
{ overlay = "setup.d/github-auth.sh", target = "/opt/setup.d/45-github-auth.sh" },
```
`setup-runner` runs it. gh needs nothing configured (it honors `$GITHUB_TOKEN`); the
step writes the git credential helper and derives `user.name`/`user.email`.

## Workspace config it relies on

The credentials are two bindings the profile carries in `workspace.template.toml` — a
GitHub token from the host's `gh` login (`gh-cli` provider), injected two ways. They
**must share one placeholder**: `gh` hands `github-api`'s placeholder (`$GITHUB_TOKEN`)
to git for `github.com`, so `github-git` has to carry the same value to swap it.
```toml
[[binding]]
name     = "github-api"
injector = "bearer"
provider = "gh-cli"
secret   = "github.com"
hosts    = ["api.github.com"]
placeholder = "ghp_…"            # SHARED with github-git
env      = "GITHUB_TOKEN"

[[binding]]
name     = "github-git"
injector = "basic"
provider = "gh-cli"
secret   = "github.com"
hosts    = ["github.com"]
placeholder = "ghp_…"            # SHARED with github-api
```
Requires an authenticated `gh` **on the host** (`gh auth login`) — the `gh-cli` provider
reads that session. This is the same as the builtin `github` preset, minus the `ghcr`
part; run `credproxy preset list` to see it.

## Configure

Nothing to configure. Add/remove `ghcr.io` or an Enterprise host by editing the bindings.
If you don't want git identity auto-set, drop the last block of the setup step (the
credential helper alone is enough for `gh`/`git push`).
