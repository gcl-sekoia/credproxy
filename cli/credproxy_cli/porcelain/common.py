"""Shared porcelain scaffolding: the invocation context, workspace resolution,
the strict/loose confirmation gates, the proxy-image ensure/warn helpers, and the
small existence/attach guards. Every noun handler module builds on this.

The strict/loose duality lives here (resolution + gates); noun handlers receive
already-resolved targets and never consult the default pointer or cwd."""
from __future__ import annotations

import argparse
import sys

from ..core.model import dirmatch
from ..core.engine import docker as core_docker
from ..core.model import pointer
from ..core.errors import ImageError
from ..core.model.workspace import Workspace, for_name
from ..core.paths import IMAGE_TAG
from .render import fail, say


class _LeafParser(argparse.ArgumentParser):
    """ArgumentParser whose `error` routes through the porcelain renderer, so a
    bad/unknown/missing arg serializes as a JSON error object under --json (and a
    clean `[credproxy] ` line otherwise) and exits non-zero -- instead of
    argparse's raw usage dump to stderr + SystemExit(2), which bypassed the
    renderer entirely. Sub-parsers inherit this class (argparse defaults
    parser_class to type(self)), so the whole verb tree is covered."""

    def error(self, message: str):  # noqa: D401 - argparse hook
        fail(f"{self.prog}: {message}")


class Ctx:
    """Resolved invocation context shared by every handler."""

    def __init__(self, loose: bool, as_json: bool, assume_yes: bool):
        self.loose = loose
        self.json = as_json
        self.assume_yes = assume_yes


def _resolve_ws(ctx: Ctx, name: str | None) -> Workspace:
    """Resolve an (optionally omitted) workspace name to a concrete Workspace.

    STRICT: a missing name is an error -- explicit naming is the contract.
    LOOSE: a missing name resolves by current directory first (a workspace whose
    `directory` is an ancestor of cwd), then the default pointer; either way the
    resolution is announced on stderr. cwd wins because "what I mean here" beats
    "what I usually mean"."""
    if name is not None:
        return for_name(name)
    if not ctx.loose:
        fail("workspace name required (strict mode names every workspace)")
    ws = dirmatch.resolve_cwd()
    if ws is not None:
        say(f"workspace: {ws.name} (matched current directory)")
        return ws
    ws = pointer.resolve_default()
    say(f"workspace: {ws.name} (default)")
    return ws


def _is_default(ws: Workspace) -> bool:
    return pointer.read_default() == ws.name


def _confirm_destructive(ctx: Ctx, ws: Workspace, implicit: bool, verb: str) -> None:
    """The safety gate. Fires only when a destructive command targets an
    IMPLICIT (defaulted or cwd-matched) workspace, in LOOSE mode. Explicit
    targets never prompt. `--yes` bypasses. Fails closed without a TTY."""
    if not (ctx.loose and implicit):
        return
    if ctx.assume_yes:
        return
    if not sys.stdin.isatty():
        fail(
            f"refusing to {verb} the implicitly-selected workspace '{ws.name}' "
            f"without confirmation: stdin is not a TTY (pass --yes)"
        )
    suffix = ("(current default)" if pointer.read_default() == ws.name
              else "(matched current directory)")
    # Prompt to STDERR (not via input(), which writes it to stdout and would
    # corrupt a --json stdout stream). EOF (closed stdin) reads "" -> abort.
    print(f'{verb.capitalize()} workspace "{ws.name}" {suffix}? [y/N] ',
          end="", file=sys.stderr, flush=True)
    reply = sys.stdin.readline()
    if reply.strip().lower() not in ("y", "yes"):
        fail("aborted")


def _confirm_running_recreate(ctx: Ctx, ws: Workspace, sessions: int) -> None:
    """Gate `mount add --preserve` when it would stop+recreate a RUNNING
    workspace that has live `enter` session(s) -- those sessions are killed by
    the recreate. Unlike _confirm_destructive, the trigger is runtime state (live
    sessions), not resolution mode, so it fires even for an explicit NAME.

    `--yes` bypasses. STRICT refuses (scriptable, never prompts -- a script must
    opt in with --yes). LOOSE prompts; fails closed without a TTY."""
    if ctx.assume_yes:
        return
    plural = "s" if sessions != 1 else ""
    msg = (f"workspace '{ws.name}' is running with {sessions} active "
           f"session{plural}; --preserve stops and recreates it "
           f"(those sessions are terminated)")
    if not ctx.loose:
        fail(f"{msg}. Re-run with --yes to proceed.")
    if not sys.stdin.isatty():
        fail(f"{msg}, and stdin is not a TTY. Re-run with --yes.")
    # Prompt to STDERR (input() would corrupt a --json stdout). EOF -> abort.
    print(f"{msg}. Continue? [y/N] ", end="", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        fail("aborted")


def ensure_proxy_image(ctx: Ctx) -> None:
    """Make sure the proxy image is present -- and warn when the checkout has
    drifted from it -- before a command that needs it (the `start`/`recreate`
    paths). So a newcomer's first `start` OFFERS to build instead of failing with
    a bare "run credproxy dev build"; the `exec` fast path (both containers already
    up) is exempt and never reaches here.

    Prompting/surface awareness is a porcelain concern (like the `_confirm_*`
    gates), so this lives here rather than in the print-free core; the core's
    ImageEnv.load missing-image error stays as the backstop for other callers."""
    if core_docker.inspect(IMAGE_TAG, "{{.Id}}") is None:
        _build_missing_image(ctx)
    else:
        _warn_if_stale_image(ctx)


def _missing_image_remedy() -> str:
    return (f"proxy image '{IMAGE_TAG}' not found; build it with: "
            f"credproxy dev build")


def _build_missing_image(ctx: Ctx) -> None:
    """Missing image. Strict never builds implicitly -- fail with the exact
    remedy. Loose offers to build inline (default Yes; `--yes` builds unprompted);
    loose without a TTY fails closed with the same remedy (matching the safety
    gate). On yes, run the `dev build` code path in-process, then continue."""
    from . import cmd_dev
    if not ctx.loose:
        fail(ImageError(_missing_image_remedy()))
    if not ctx.assume_yes:
        if not sys.stdin.isatty():
            fail(ImageError(_missing_image_remedy()))
        print(f"proxy image '{IMAGE_TAG}' not found — build it now "
              f"(runs docker build, ~a minute)? [Y/n] ",
              end="", file=sys.stderr, flush=True)
        if sys.stdin.readline().strip().lower() in ("n", "no"):
            fail(ImageError(_missing_image_remedy()))
    say(f"building proxy image '{IMAGE_TAG}'...")
    cmd_dev.do_dev_build(ctx)


def _warn_if_stale_image(ctx: Ctx) -> None:
    """The image is present: compare the checkout's source digest against the
    `credproxy.src_digest` label `dev build` stamped. A mismatch is NOT an error
    (the old image still works), so never block. Loose+TTY offers a rebuild
    (default NO); strict prints a one-line warning and proceeds; an image with no
    label (built before this change) is 'unknown' -> the same warning, never a
    rebuild prompt. Skipped silently without a repo checkout (nothing to compare)."""
    from . import cmd_dev
    from ..core import paths

    digest = paths.proxy_src_digest()
    if digest is None:
        return  # no repo checkout -> nothing to compare
    label = core_docker.inspect(
        IMAGE_TAG, '{{index .Config.Labels "' + paths.SRC_DIGEST_LABEL + '"}}')
    stamped = paths.image_label_digest(label)
    if stamped == digest:
        return  # image is up to date
    if stamped is None:
        say(f"proxy image '{IMAGE_TAG}' has no source-digest label (built before "
            f"staleness tracking); rebuild with `credproxy dev build` if it seems "
            f"out of date")
        return
    warn = (f"proxy source changed since image '{IMAGE_TAG}' was built; "
            f"rebuild with `credproxy dev build`")
    # Strict, --yes, and no-TTY all take the default (No) -- never a surprise
    # rebuild -- and just warn, proceeding with the current image.
    if not ctx.loose or ctx.assume_yes or not sys.stdin.isatty():
        say(warn)
        return
    print("proxy source changed since the image was built — rebuild now? [y/N] ",
          end="", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip().lower() in ("y", "yes"):
        say(f"rebuilding proxy image '{IMAGE_TAG}'...")
        cmd_dev.do_dev_build(ctx)
    else:
        say("proceeding with the current image")


def _require_exists(ws: Workspace) -> None:
    if not ws.exists():
        fail(f"workspace '{ws.name}' not found")


def _reject_if_attached(ws: Workspace, verb: str) -> None:
    """Refuse a container-lifecycle verb on an ATTACHED workspace: its containers
    are managed externally (Compose/devcontainers/CI), so credproxy only pushes
    credentials -- point the operator at `push`."""
    from ..core.model import config as core_config
    if core_config.quick_attach(ws):
        fail(f"workspace '{ws.name}' is attached: its containers are managed "
             f"externally, so `{verb}` doesn't apply. credproxy manages only its "
             f"credentials -- run `credproxy workspace {ws.name} push`.")
