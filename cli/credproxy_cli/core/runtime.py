"""Container-runtime probing (cached).

credproxy shells `docker`, which may actually be podman (the `podman-docker`
shim provides a `docker` that execs podman). Two features need to know what the
runtime really is:

- `map_host_user` cares about **podman, rootless** specifically -- the only case
  where the workspace's non-root user can't read/write bind mounts without
  `--userns=keep-id` (on Docker uids match 1:1, so no userns lever is wanted).
- the workspace-hostname flag cares about **podman at all** (rootful or
  rootless): podman leaves UTS independent on a netns join and accepts
  `--hostname` on the joiner, whereas Docker rejects it there.

Both read the SAME daemon probe, so there is exactly one round-trip per process.
The probe asks the *daemon*, so it is correct even when the binary is a shim,
via a podman-shaped `info` field that doubles as the engine discriminator:
podman's info has `.Host.Security.Rootless` (prints true/false, exit 0); real
Docker's info has no `.Host`, so the Go template errors (non-zero exit). So a
zero exit == podman regardless of the printed value; error or non-podman -> we
treat it as Docker/absent and inject nothing.
"""
from __future__ import annotations

import functools
import subprocess


@functools.lru_cache(maxsize=1)
def _probe() -> tuple[int, str]:
    """Raw result of the engine-discriminating probe -- ONE daemon round-trip,
    memoized for the process (the runtime doesn't change under a running CLI).
    Returns (returncode, stdout); both predicates below read from it. Any failure
    (no binary, daemon down, timeout) yields (-1, "") -- the safe default both
    predicates read as 'not podman'."""
    try:
        r = subprocess.run(
            ["docker", "info", "-f", "{{.Host.Security.Rootless}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return (-1, "")
    return (r.returncode, r.stdout)


def is_podman() -> bool:
    """True iff the active runtime is podman (rootful OR rootless).

    The template errors on real Docker (no `.Host`) -> non-zero exit; podman
    prints true/false -> exit 0. So a zero exit is the podman discriminator,
    independent of the rootless value. Any probe failure -> False."""
    return _probe()[0] == 0


def is_podman_rootless() -> bool:
    """True iff the active container runtime is podman running rootless.

    Any failure (no binary, daemon down, real Docker's template error) yields
    False -- the safe default that injects no userns flag."""
    rc, out = _probe()
    # Real Docker: no `.Host` field -> non-zero exit. Podman: prints true/false.
    return rc == 0 and out.strip() == "true"
