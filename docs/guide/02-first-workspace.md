← [01 · Install](01-install.md) · [index](../README.md) · [03 · Daily workflow](03-daily-workflow.md) →

# 02 · Your first workspace

A [workspace](../concepts.md#workspace) is a named development environment: a
container you work in, plus a private proxy in front of its network. You create
it once, then start and enter it by name. This chapter builds one.

## Create the workspace

Run this in a project directory you want to work in:

```console
$ credp create myproject --here
created workspace 'myproject' at ~/.config/credproxy/workspaces/myproject.toml
edit ~/.config/credproxy/workspaces/myproject.toml, then run `credproxy workspace myproject start`
associated with directory /home/you/projects/myproject
set 'myproject' as the default workspace
```

`create` writes a config file and stops. It does not start any container yet.
`--here` links the workspace to the current directory, so later you can run
`credp` commands from this folder without naming the workspace. Because this is
your first workspace, it also became the default.

> [!NOTE]
> The `--here` flag is optional. Without it, you always name the workspace:
> `credp create myproject`. Directory addressing is covered in the
> [daily workflow](03-daily-workflow.md).

## Start it (and build the proxy image)

`credp start` brings up both containers. On your very first start, the proxy
image does not exist yet, so credproxy offers to build it. Say yes; the build
takes about a minute and happens only once.

```console
$ credp start
proxy image 'credproxy:dev' not found — build it now (runs docker build, ~a minute)? [Y/n] y
building proxy image 'credproxy:dev'...
...
workspace 'myproject' running
```

`start` starts the proxy, waits for it to be ready, pushes your configuration,
then starts the workspace container. It is safe to re-run at any time.

> [!TIP]
> Want to know exactly what "ready" means and how the proxy captures traffic? →
> [How it works](../how-it-works.md). Otherwise, continue.

## Enter it

`credp enter` opens a shell inside the workspace container:

```console
$ credp enter
vscode@73fd8fd26e2e:~$ whoami
vscode
vscode@73fd8fd26e2e:~$ curl -s https://example.com -o /dev/null -w '%{http_code}\n'
200
```

> [!NOTE]
> The hostname in the prompt (`73fd8fd26e2e` here) is the container's ID. Yours
> will differ.

You are now inside your container. Its network already flows through the proxy.
Regular HTTPS works because the workspace trusts the proxy's certificate, set up
automatically when the container was created. Type `exit` to leave.

You have a running workspace, but no credentials are wired yet. That is the
[next chapter](04-first-credential.md).

<details><summary>What just got created</summary>

Creating and starting the workspace produced four things:

- **Two containers.** A privileged **proxy** container that manages the network
  and holds credentials, and **your** workspace container (an unprivileged,
  unmodified image). They share one network namespace.
- **A config file** at `~/.config/credproxy/workspaces/myproject.toml`. This is
  the single source of truth for the workspace: its image, mounts, environment,
  and (soon) its credential bindings. Edit it directly or with credproxy
  commands.
- **A state directory** at `~/.local/state/credproxy/workspaces/myproject/`. It
  holds the proxy's authentication token and bookkeeping. You never edit this by
  hand.

The exact paths follow the XDG base-directory convention and may differ on
macOS. See [How it works](../how-it-works.md) for the container architecture.

</details>

---

**Next:** [03 · Daily workflow](03-daily-workflow.md) — the everyday commands and
the `credp` / `credproxy` split.
