# Troubleshooting

[← docs index](README.md) · [Concepts](concepts.md)

Something not working? This page is a checklist of the common failures and how to
read your way out of them.

## Run `doctor` first

Before anything else, run the built-in preflight. Unlike `start`, which stops at
the first problem, `doctor` reports **every** problem it finds in one pass — the
environment, the proxy image, and each workspace's config and bindings:

```console
$ credproxy doctor
✗ docker found but the daemon is unreachable  → start Docker / the podman socket
✗ image credproxy:dev not found; run `credproxy dev build` first  → run `credproxy dev build`
✓ [myproject] container config valid
✓ [myproject] 2 binding(s) pass static checks
✓ [myproject] 2 rule(s) valid
2 of 5 checks FAILED
```

Each failing line ends with a `→` hint. Add a workspace name to focus on one, and
add `--fetch` to also resolve every binding's secret through its provider (this
one needs an explicit name, since a provider may prompt or unlock):

```sh
credproxy doctor myproject --fetch
```

A green `doctor` means `start`'s static validation will pass. Start there, then
use the specific entries below.

## TLS certificate errors inside the workspace

A tool in the workspace reports an untrusted certificate, a bad certificate, or a
CA error on an HTTPS call. There are two usual causes.

**Cause A — the workspace never bootstrapped the proxy's CA.** To read
intercepted HTTPS, the proxy presents its own certificate, which the workspace
must trust. The default image installs that certificate in its `setup` step:

```toml
setup = ["curl -fsSL http://proxy.local/bootstrap.sh | sh"]
```

If you switched to a custom image and dropped that line, add it back (the image
needs `curl` and `ca-certificates`). See [guide 08](guide/08-going-further.md).

**Cause B — a rule flipped a pass-through host to intercepted.** The proxy opens
TLS only on hosts named by a binding or a [rule](concepts.md#rule). Adding a rule
to a host that previously had neither makes that host intercepted for the first
time. A workspace that trusts the CA already is fine; one that does not will now
see a certificate error on that host — where before the traffic passed through
untouched. This is the union-intercept tripwire; the fix is the same bootstrap as
Cause A. See [guide 07](guide/07-rules.md).

## A `401` even though the placeholder was sent

The tool sent the placeholder, the request reached the service, and the service
answered `401`. The swap did not happen. Common reasons:

- **Host mismatch.** The binding's `hosts` do not cover the host the request
  actually went to. The proxy only substitutes on a binding's hosts.
- **Wrong placement.** The placeholder went out in a header or field the injector
  is not looking at.

Read the proxy's log to see what it decided:

```sh
credp logs --audit
```

The audit stream names each binding that fired (or declined) per request, without
ever printing the secret. If you see no injection event for the call, the host or
scope is wrong; run `credp binding list` and check the `hosts` column.

## `docker` permission errors

`permission denied` talking to the Docker socket usually means your user is not
in the `docker` group (rootful Docker) or the Podman socket is not running
(rootless Podman). `credproxy doctor` reports this as `daemon is unreachable`.
Start Docker Desktop, add yourself to the `docker` group, or start the Podman
user socket, then re-run `doctor`.

## SELinux denials on Fedora / RHEL

On an SELinux-enforcing host, bind mounts can be denied. credproxy handles this
by trust level automatically: the proxy container is confined and relabels only
its own mounts, while the workspace runs with SELinux labeling disabled
(distrobox/toolbx style) so your bind-mounted project directories work **without**
being relabeled or mutated. This is a no-op on non-SELinux hosts. If you still
hit a denial, the [workspace reference](reference/workspace.md) documents the
split-labeling model in full.

## Workspace fails to start with a sysfs mount error (rootless podman + runc)

On **rootless podman using the `runc` OCI runtime**, the workspace container dies
at init while the proxy container starts fine:

```
[credproxy] docker run failed: Error: runc: runc create failed: unable to start
container process: error during container init: error mounting "sysfs" to rootfs
at "/sys": mount src=sysfs, dst=/sys, flags=MS_RDONLY|MS_NOSUID|MS_NODEV|MS_NOEXEC:
operation not permitted: OCI permission denied
```

This is a known **runc** limitation, not a credproxy bug. The workspace joins the
proxy's network namespace (`--network container:`), and the default template's
`map_host_user = true` adds `--userns=keep-id` so a non-root user owns your bind
mounts. keep-id puts the workspace in a **new** user namespace, but a fresh
read-only `sysfs` mount requires the mounter's userns to own the network
namespace — and the proxy's netns is owned by a different one. `runc` refuses the
mount; **`crun` bind-mounts `/sys` instead and works**. `credproxy doctor` flags
this combination, and `start` rewrites the raw OCI error with the two fixes below.

Fix it either way:

**Switch podman to `crun`** (its default on Fedora; the recommended fix). Add to
`~/.config/containers/containers.conf`:

```toml
[engine]
runtime = "crun"
```

**Or turn off the user-mapping** in the workspace's TOML — simplest if you don't
need a non-root user to own the bind mounts:

```toml
map_host_user = false
```

credproxy deliberately does **not** auto-select `--runtime crun` or flip the
`map_host_user` default; the runtime is your host's choice.

## "proxy source changed since the image was built"

You are running from a repo checkout, and the proxy source has changed since you
last built the image. The current image still works — this is a reminder, not an
error:

```
proxy source changed since image 'credproxy:dev' was built; rebuild with `credproxy dev build`
```

Rebuild when convenient:

```sh
credproxy dev build
```

## Port conflicts

You should not see these. The proxy's host port is assigned fresh by Docker for
each proxy container and resolved at call time — it is never a fixed number and
never persisted — so multiple workspaces run at once without coordination. If a
port error does appear, it is about your own image's published ports, not
credproxy's admin port.

## Where the logs are

The proxy writes one structured record per line. `docker logs` on the proxy
container is the durable store; it survives `stop`/`start`. The friendly view is:

```sh
credp logs           # one readable line per event, streaming
credp logs --audit   # only credential and rule events
credp logs --json    # the raw JSON records
```

Use `--audit` when you want proof a credential swap or a rule hit happened — the
records carry structural facts (binding or rule name, host, method) but never a
secret value.

---

Still stuck? The [security page](security.md) explains the boundaries (some
things are *meant* not to work), and the [reference](README.md#reference) pages
have the details behind each command.
