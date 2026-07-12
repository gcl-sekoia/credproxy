"""The `mount` noun: the `mount add` handler and its argparse subparser builder."""
from __future__ import annotations

import argparse

from ..core.engine import docker as core_docker
from ..core.engine import sessions as core_sessions, startup
from . import render
from .render import fail, say
from .common import (
    Ctx, _resolve_ws, _require_exists, _reject_if_attached,
    _confirm_running_recreate,
)


def do_mount_add(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    """Add a managed-volume mount to a workspace. `--preserve` first captures the
    live container's data at --target into the new volume, then recreates so the
    volume mounts populated (otherwise the change is deferred to the next
    `start`, with the volume image-seeded as usual)."""
    if not a.mount_volume:
        fail("`mount add` needs --volume NAME")
    if not a.mount_target:
        fail("`mount add` needs --target PATH")

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _reject_if_attached(ws, "mount add")

    # Gate only the disruptive path: --preserve restarts the container, killing
    # any live `enter` sessions. A plain add is a deferred config edit, like
    # editing the file, so it isn't gated (mirrors un-gated plain `recreate`).
    if a.mount_preserve:
        status = core_docker.container_status(ws.ws_container)
        sessions = (core_sessions._count_live_sessions(ws)
                    if status == "running" else 0)
        if sessions:
            _confirm_running_recreate(ctx, ws, sessions)

    startup.add_managed_volume(
        ws, name=a.mount_volume, target=a.mount_target,
        readonly=a.mount_ro, preserve=a.mount_preserve,
        user_owned=a.mount_user_owned, notify=say,
    )
    render.OUT.mount_added(ws.name, a.mount_volume, a.mount_target,
                           a.mount_ro, applied=a.mount_preserve)


def _mount_subparsers(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser("add")
    p.add_argument("--volume", dest="mount_volume", default=None, metavar="NAME")
    p.add_argument("--target", dest="mount_target", default=None, metavar="PATH")
    p.add_argument("--ro", dest="mount_ro", action="store_true")
    # Seed the new volume with the current container's data at --target before
    # the recreate that applies the mount (otherwise the volume starts empty /
    # image-seeded). Requires an existing container to copy from.
    p.add_argument("--preserve", dest="mount_preserve", action="store_true")
    # Chown the volume to the workspace `user` after setup, so a non-root user
    # can write a volume mounted at an image-absent path (otherwise root-owned).
    p.add_argument("--user-owned", dest="mount_user_owned", action="store_true")
