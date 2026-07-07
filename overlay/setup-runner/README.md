# overlay: setup-runner (lib)

The setup **orchestrator** every workspace mounts. credproxy runs a workspace's `setup`
steps as root; this drops to the workspace user and then runs each lib's setup step from
`/opt/setup.d/` in order. It's base infrastructure — a profile always mounts it — not an
opt-in feature.

**Contributes:** the `/opt/setup.d` runner (no `setup.d` step of its own).

## Compose from a profile

```toml
mounts = [
  { overlay = "setup.sh", target = "/opt/workspace-setup.sh" },   # this runner
  # … each lib's setup step → /opt/setup.d/NN-<lib>.sh …
]
setup = [
  # … profile-specific root steps (e.g. CA bootstrap, volume chown) …
  "bash /opt/workspace-setup.sh",
]
```
A lib is "enabled" iff its setup step is mounted into `/opt/setup.d/` — so the mount
list is the single on/off control. Steps run in `NN-` filename order.

## Configure

Nothing to configure. The user it drops to comes from **`CREDPROXY_USER`** (credproxy
exposes the workspace's configured `user` automatically). If that's unset or `root`, the
runner runs the steps as-is — so root-based images work too.
