← [07 · Rules](07-rules.md) · [index](../README.md)

# 08 · Going further

You have the core loop: create a workspace, wire a credential, add guardrails.
This chapter is a map of what is next. Each section is short and points at the
reference page that covers it in full.

## Many workspaces at once

A [workspace](../concepts.md#workspace) is cheap and self-contained, so keep one
per project. Each gets its own proxy, its own token, and its own config file, and
they run at the same time without colliding — the proxy's host port is assigned
fresh per container, so there is nothing to coordinate.

```console
$ credp list
   NAME  STATUS   IMAGE
   demo  stopped  mcr.microsoft.com/devcontainers/base:ubuntu
   gh    stopped  mcr.microsoft.com/devcontainers/base:ubuntu
```

Because you linked directories in chapter 03, `cd`-ing into a project and running
a bare `credp enter` targets that project's workspace. No global switch to flip.

## Mounts and a persistent home

By default a workspace's filesystem is fresh each time it is rebuilt. To keep
data across rebuilds, use a mount. The simplest is a **persistent home**: set
`home` in the config and that directory becomes a managed volume that survives
`stop`, `start`, and `recreate`.

```toml
home = "/home/vscode"
```

For other paths — a package cache, a scratch directory — add a managed volume:

```sh
credp mount add --volume cache --target /home/vscode/.cache
```

You can also bind a host directory straight in (`"~/code:/code"` in `mounts`).
The full model — binds, volumes, overlay mounts, and how each behaves on rebuild
— is in the [configuration reference](../reference/configuration.md).

## Custom images (bring your own image)

The workspace runs **your** image, unmodified. The default template starts from a
devcontainers base, but you can point `image` at anything. One thing that image
must do is trust the proxy's certificate, or HTTPS to intercepted hosts will
fail. The default template handles this for you in its `setup` step:

```toml
setup = [
  "curl -fsSL http://proxy.local/bootstrap.sh | sh",
]
```

That line fetches the proxy's CA over the private network and installs it. If you
switch to a custom image and drop the default `setup`, **add that line back** (it
needs `curl` and `ca-certificates` in the image). Without it, the workspace has
no way to trust the proxy, and every intercepted HTTPS call reports a certificate
error.

> [!WARNING]
> The bootstrap only works at run time, from inside the workspace's network —
> `proxy.local` is not reachable while an image is being built. So credentialed
> or proxy-dependent setup belongs in the `setup` list, which runs after the
> container joins the network, not in a Dockerfile. The
> [workspace reference](../reference/workspace.md) explains the boundary.

## Where to go from here

You now know enough to use credproxy day to day. When you need more:

- **[Overlays](../advanced/overlays.md)** — ship your team's own defaults,
  providers, injectors, and packs without editing credproxy or maintaining a
  fork.
- **[Composability](../advanced/composability.md)** — attached workspaces: let
  Docker Compose, a devcontainer, or CI run the containers while credproxy
  supplies only the credentials.
- **[Injectors reference](../reference/injectors.md)** — the credential-shaping
  schemes in full, including scripted injectors for services no built-in scheme
  covers.
- **[Security](../security.md)** — the honest threat model: what credproxy
  protects against, and what it deliberately does not.

---

That is the guide. Head back to the [documentation index](../README.md) for the
reference pages, or revisit [How it works](../how-it-works.md) now that the
pieces are familiar.
