"""The porcelain front-end: argument parsing, convenience resolution, and
rendering. Two surfaces over one core.

Surfaces (chosen purely by invocation, never by terminal sniffing):
  - STRICT (`credproxy`): every workspace named explicitly; omitting one is a
    clear error. No default-workspace resolution, no prompts ever, no aliases.
    The scriptable contract.
  - LOOSE (`credproxy --loose`, aliased `credp`): adds default-workspace
    resolution (announced on stderr), short command aliases that resolve to
    canonical commands with no independent behavior, and the confirmation gate
    on destructive-and-implicit actions.

`--json` is orthogonal to the surface: it selects the renderer only.

Grammar (canonical):
    credproxy workspace create NAME
    credproxy workspace use NAME
    credproxy workspace list [FILTER]
    credproxy list [FILTER]                       # canonical survey
    credproxy workspace NAME {enter|start|stop|recreate|delete|apply|inspect|logs}
    credproxy workspace NAME binding {add|remove|list|test} ...
    credproxy injector {scaffold NAME|list}
    credproxy provider {scaffold NAME|list}
    credproxy dev {build|test|reload}

argparse can't express name-before-verb, so the `workspace` noun is dispatched
by a small hand-rolled router (peek the second token: a verb routes directly,
anything else is a workspace name and the third token is the verb). Leaf
commands' flags still go through argparse.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib

from ..core.model import dirmatch
from ..core.engine import docker as core_docker
from ..core.engine import lifecycle
from ..core.model import pointer
from ..core.model import workspace as core_workspace
from ..core.errors import CredproxyError, DependencyError, ImageError
from ..core.model.workspace import RESERVED_NAMES, Workspace, for_name
from ..core.paths import (
    IMAGE_TAG,
    PROXY_DIR,
    TESTS_DIR,
)
from . import render
from .render import fail, say


# Workspace-scoped verbs (the `workspace NAME <verb>` tail).
_WS_VERBS = {
    "enter", "exec", "edit", "start", "stop", "recreate", "delete", "apply",
    "inspect", "config", "logs", "binding", "bind-dir", "mount", "rule",
    "push", "resolve",
}
# Workspace-level verbs that take a name as their argument, not a subject.
_WS_NOUN_VERBS = {"create", "use", "list"}
# Top-level meta commands: no workspace argument. Every token in the three
# command sets above and here must be in core's RESERVED_NAMES (a workspace
# can't take a colliding name) -- guarded by test_reserved_names_cover_all_cli_verbs.
_META_COMMANDS = {"list", "current", "info", "doctor"}


# ---- a parsed invocation ----------------------------------------------------


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
    do_dev_build(ctx)


def _warn_if_stale_image(ctx: Ctx) -> None:
    """The image is present: compare the checkout's source digest against the
    `credproxy.src_digest` label `dev build` stamped. A mismatch is NOT an error
    (the old image still works), so never block. Loose+TTY offers a rebuild
    (default NO); strict prints a one-line warning and proceeds; an image with no
    label (built before this change) is 'unknown' -> the same warning, never a
    rebuild prompt. Skipped silently without a repo checkout (nothing to compare)."""
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
        do_dev_build(ctx)
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


# ---- workspace commands ------------------------------------------------------


def do_create(ctx: Ctx, name: str | None, directory: str | None = None,
              attach: str | None = None) -> None:
    # Loose-surface convenience: an omitted NAME is derived from the --here/--dir
    # directory basename. Strict always names explicitly; with no directory there
    # is nothing to derive from.
    if name is None:
        if not ctx.loose:
            fail("workspace name required (strict surface names every workspace); "
                 "`credp create --here` derives one from the directory")
        if directory is None:
            fail("nothing to derive a name from: pass NAME, or --here/--dir")
        name = core_workspace.derive_workspace_name(directory)
        say(f"derived workspace name '{name}' from {directory}")
    ws = for_name(name)  # reserved-name / charset check happens here
    # Fail fast on an existing workspace BEFORE any template render / preset
    # expansion, so create stays all-or-nothing (nothing resolved or written for a
    # name that's already taken). create_*_workspace_files re-checks under its own
    # write, closing the (host-single-user) race.
    if ws.exists():
        from ..core.errors import WorkspaceError
        raise WorkspaceError(
            f"workspace '{ws.name}' already exists ({ws.config_path})")
    from ..core.model import config as core_config
    source = str(ws.config_path)
    selector = None
    if attach is not None:
        # `--attach SELECTOR` scaffolds an ATTACHED workspace from the attach
        # template (overlay-overridable). --here/--dir still make sense (cwd
        # resolution applies to attached workspaces too), handled below.
        selector = _attach_selector_from_flag(attach)
        base_text = core_config.render_attach_template(ws.name, selector)
    else:
        base_text = core_config.render_template(ws.name)
    # Expand any template-declared `[[preset]]` entries (#57) into ordinary
    # stamped blocks IN MEMORY -- all-or-nothing: any failure raises/exits here,
    # before a single byte is written (no config file, no token, no state dir).
    final_text, preset_announces, stamped_packs = _expand_template_presets(
        ctx, base_text, source)
    if attach is not None:
        lifecycle.create_attached_workspace_files(ws, selector, text=final_text)
    else:
        lifecycle.create_workspace_files(ws, text=final_text)
    # Host-prerequisite checks (#58) run only AFTER the atomic write succeeds, so
    # a `fetch=true` provider check never execs a provider for a create that then
    # aborts (finding 5). Advisory -- the stamp is durable, host state is fixable
    # afterward. `do_fetch=True` (create is interactive, like `preset add`).
    if stamped_packs:
        from ..core.model import prereqs
        for announce, (spec, provider, secret) in zip(preset_announces,
                                                       stamped_packs):
            announce["requires"] = [prereqs.summary(r) for r in prereqs.evaluate(
                spec.requires, provider=provider, secret=secret, do_fetch=True)]
    render.OUT.created(ws.name, str(ws.config_path), attached=attach is not None,
                       presets=preset_announces)
    # Optional cwd-association (`--here`/`--dir`): record the directory this
    # workspace is "for" so `credp <verb>` resolves it from there (dirmatch).
    if directory is not None:
        real = os.path.realpath(directory)
        if real == os.path.sep or real == os.path.realpath(os.path.expanduser("~")):
            say(f"note: {directory} is too broad -- cwd resolution ignores it")
        claimer = dirmatch.find_claimer(directory, exclude=ws.name)
        if claimer:
            say(f"note: directory {directory} is also claimed by '{claimer}'")
        core_config.associate_directory(ws, directory)
        say(f"associated with directory {directory}")
    # Loose convenience: seed the default-workspace pointer when it is unset,
    # so `credp enter` works immediately without a separate `use`. Only fills a
    # vacuum -- never overrides an existing selection -- and is announced. The
    # pointer is a loose-surface concept, so strict `create` never touches it.
    if ctx.loose and pointer.read_default() is None:
        pointer.set_default(ws)
        say(f"set '{ws.name}' as the default workspace")


# A `[[preset]]` array-of-tables header line (a trailing comment is allowed),
# for the surgical strip of template-declared entries before writing the config.
_PRESET_BLOCK_RE = re.compile(r"^\s*\[\[\s*preset\s*\]\]\s*(#.*)?$")
# A `[preset.<child>]` sub-table header (e.g. `[preset.options]`, #59) that BELONGS
# to the current `[[preset]]` element -- folded INTO its span so the strip removes
# it too (mirrors the `[rule.headers]` child handling in `_block_spans`). TOML
# permits whitespace around the dotted-key separator (`[preset . options]`), so the
# pattern tolerates it too (N5) rather than letting the child header escape the
# strip and fail create with a misleading survivor error.
_PRESET_CHILD_RE = re.compile(r"^\s*\[\s*preset\s*\.\s*[^\[\]\n]+\]\s*(#.*)?$")


def _strip_preset_blocks(text: str) -> str:
    """Remove every `[[preset]]` block from `text` via surgical span removal (the
    same `_block_spans` machinery `remove_binding` uses), so a template's expanded
    entries never survive into the stamped `<name>.toml`. Also folds in an
    immediately-adjacent run of comment-only lines above the header (a `# label`
    that belongs to the block) plus one preceding blank separator, so a labelled
    block leaves no orphan and no blank runs accumulate.

    Line-based, NOT multiline-string aware: a `[[preset]]`-lookalike inside a
    `\"\"\"...\"\"\"` value would be mis-stripped. This is the reason
    `_expand_template_presets` re-parses the composed text afterward and fails
    closed on the resulting invalid/`preset`-bearing TOML rather than writing it.
    The comment fold is deliberately conservative (only lines DIRECTLY above the
    header, no intervening blank), which can in a rare shape absorb a preceding
    block's trailing comment -- an acceptable edge for the common labelled-block
    case."""
    from ..core.model.bindings import _block_spans

    lines = text.splitlines(keepends=True)
    for start, end in reversed(_block_spans(text, _PRESET_BLOCK_RE,
                                            _PRESET_CHILD_RE)):
        while start > 0 and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        if start > 0 and lines[start - 1].strip() == "":
            start -= 1
        del lines[start:end]
    return "".join(lines)


# A `[[preset]]` header OR an inline `preset =` assignment, for detecting preset
# references in a template we can't fully parse (finding 5) and (via the block
# regex above) counting the block form.
_PRESET_REF_RE = re.compile(r"^\s*(\[\[\s*preset\s*\]\]|preset\s*=)", re.M)


def _expand_template_presets(ctx: Ctx, base_text: str, source: str):
    """Expand template-declared `[[preset]]` entries (#57) into the final config
    text: strip the `[[preset]]` blocks and stamp each pack's expansion through
    the SAME `_expand_preset_into_text` core `preset add` uses. Returns
    `(final_text, announces, stamped_packs)` -- `announces` is the per-entry
    render metadata; `stamped_packs` is the `(literal_spec, provider, secret)` per
    pack (option markers already substituted) so the caller can run the #58
    host-prerequisite checks AFTER it writes (finding 5), never invoking a provider
    for a create that then aborts.

    All-or-nothing: everything is composed in memory and every failure raises/exits
    before the caller writes, so a bad entry leaves no partial config. Entries are
    expanded in declaration order and each is validated against the accumulating
    text (prior stamps + the template's literal config), so cross-entry and
    entry-vs-literal collisions fail here."""
    from ..core.model.bindings import _block_spans
    from ..core.errors import PresetTemplateError
    from ..core.model.presets import (
        apply_option_values, build_preset, get_preset, parse_template_presets,
    )
    from ..core.providers import find_provider

    try:
        raw = tomllib.loads(base_text)
    except tomllib.TOMLDecodeError as e:
        # A malformed template can't be inspected for presets. If it textually
        # references presets, its entries can't be expanded -- fail create rather
        # than silently writing a broken workspace with unexpanded preset text
        # (finding 5). A malformed template with NO preset reference keeps the
        # historical write-verbatim behavior (the parse error surfaces at `start`).
        if _PRESET_REF_RE.search(base_text):
            fail(f"{source}: template is malformed TOML ({e}); its preset "
                 f"entries can't be expanded -- fix the template's `[[preset]]` "
                 f"syntax")
        return base_text, [], []

    # Gate on the KEY'S PRESENCE, not on entries being non-empty: any `preset`
    # key must be stripped/rejected here so it never survives into the stamped
    # config (where every later command would reject it with a misleading
    # "use preset add" remedy -- findings 1/2).
    if "preset" not in raw:
        return base_text, [], []

    entries = parse_template_presets(raw, source)   # ConfigError on a bad entry

    # Require the documented `[[preset]]` array-of-tables block form (finding 2,
    # option b). The surgical stripper only removes `[[preset]]` HEADER blocks, so
    # an inline `preset = [...]`/`preset = {...}` would survive strip. Each block
    # yields exactly one entry, so a count mismatch means an inline spelling.
    n_blocks = len(_block_spans(base_text, _PRESET_BLOCK_RE))
    if n_blocks != len(entries):
        fail(f"{source}: declare template presets as `[[preset]]` blocks, not an "
             f"inline `preset = [...]` array/table")
    if not entries:
        # `preset = []` (or an empty inline form): key present, zero blocks --
        # nothing to expand, but the inline key can't be surgically stripped and
        # must not survive. Reject at create with the block-form remedy.
        fail(f"{source}: the `preset` key is empty; remove it, or declare "
             f"template presets as `[[preset]]` blocks")

    text = _strip_preset_blocks(base_text)
    announces: list[dict] = []
    # (literal_spec, provider, secret) per stamped pack (option markers already
    # substituted), so the host-prerequisite checks (#58) run AFTER the atomic
    # write succeeds -- a `fetch=true` provider check execs a provider, and a later
    # entry aborting create must not have invoked one for a create that then writes
    # nothing (finding 5).
    stamped_packs: list[tuple] = []
    for entry in entries:
        spec = get_preset(entry.name)               # CredproxyError on unknown pack
        # Pack options (#59): explicit from the entry's `[preset.options]`, else
        # prompt on loose+TTY, else default, else the structured missing error.
        option_values = _resolve_preset_option_values(
            ctx, spec, dict(entry.options or {}))
        provider = secret = None
        if spec.needs_credential:
            def _missing(missing, _entry=entry):
                # Prompting on loose+TTY handled inside the interactive resolver;
                # here we're already past it -> the create-flavored structured error.
                raise PresetTemplateError(_entry.name, missing)

            provider, secret = _resolve_preset_credential_interactive(
                ctx, spec, entry.provider, entry.secret, on_missing=_missing)
            find_provider(provider)                 # ProviderError if it doesn't resolve
        elif entry.provider or entry.secret:
            shape = "container-only (mounts/env/setup)" if spec.has_container_half \
                else "pure-rule"
            fail(f"template preset '{entry.name}' is a {shape} pack with no "
                 f"bindings -- it needs no provider/secret; remove those fields "
                 f"from its `[[preset]]` entry")
        exp = build_preset(entry.name, provider, secret, options=option_values)
        literal_spec = apply_option_values(spec, option_values) \
            if spec.options else spec
        text, announce = _expand_preset_into_text(
            text, exp, entry.name, source, context="create")
        announces.append(announce)
        stamped_packs.append((literal_spec, provider, secret))

    # Post-strip safety net (backstops findings 1/2): the composed config must
    # carry no top-level `preset` key. The stripper is line-based, so re-parse and
    # fail closed (no write) if a `preset` key somehow survived -- a credproxy bug.
    try:
        recheck = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        recheck = {}
    if "preset" in recheck:
        fail(f"{source}: internal error -- a `preset` key survived template "
             f"expansion (credproxy bug); nothing was written")
    return text, announces, stamped_packs


def do_bind_dir(ctx: Ctx, name: str | None, directory_flag: str | None) -> None:
    """Associate a workspace with a host directory (default: cwd), so a loose
    `credp <verb>` run from at/under it resolves here. Sugar over editing the
    `directory` field; the TOML stays the source of truth."""
    from ..core.model import config as core_config

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    directory = (os.path.abspath(os.path.expanduser(directory_flag))
                 if directory_flag else os.getcwd())
    real = os.path.realpath(directory)
    if real == os.path.sep or real == os.path.realpath(os.path.expanduser("~")):
        say(f"note: {directory} is too broad -- cwd resolution ignores it")
    claimer = dirmatch.find_claimer(directory, exclude=ws.name)
    if claimer:
        say(f"note: directory {directory} is also claimed by '{claimer}'")
    core_config.associate_directory(ws, directory)
    render.OUT.bound_dir(ws.name, directory)


def do_use(ctx: Ctx, name: str) -> None:
    ws = for_name(name)
    pointer.set_default(ws)  # verifies existence
    render.OUT.used(ws.name)


def do_current(ctx: Ctx) -> None:
    """Report the workspace a bare verb targets, distinct from the default
    pointer. Loose-only (the strict surface has no implicit target). Mirrors
    `_resolve_ws`: cwd match first, then the default -- the source (and any
    shadowed default) are announced on stderr, so stdout stays just the name
    for `$(credp current)`."""
    default = pointer.read_default()
    try:
        here = dirmatch.resolve_cwd()
    except CredproxyError as e:
        # cwd is contested -> a bare verb errors here; say so, then fall back to
        # the default pointer for reference.
        say(str(e))
        render.OUT.current(default, "default" if default else None, default)
        return
    if here is not None:
        if default == here.name:
            say("current directory; also default")
        elif default:
            say(f"current directory; default is '{default}'")
        else:
            say("current directory (no default set)")
        render.OUT.current(here.name, "directory", default)
    elif default:
        say("default")
        render.OUT.current(default, "default", default)
    else:
        render.OUT.current(None, None, default)


def do_list(ctx: Ctx, filter_: str | None) -> None:
    from ..core.model import config as core_config

    # The default pointer and cwd matching are loose-only implicit-targeting
    # concepts; the strict surface is a plain inventory that consults neither
    # (so the `*`/`→` markers never appear there). The DIRECTORY column is
    # factual config and shows on both.
    default = pointer.read_default() if ctx.loose else None
    here_name = None
    if ctx.loose:
        # Which workspace (if any) cwd resolves to -- informational marker only.
        # Tolerate ambiguity (don't crash `list` over it).
        try:
            here_ws = dirmatch.resolve_cwd()
            here_name = here_ws.name if here_ws else None
        except CredproxyError:
            here_name = None
    rows = []
    for s in lifecycle.list_workspaces():
        if filter_ and filter_ not in s.name:
            continue
        rows.append({
            "name": s.name,
            "running": s.running,
            "image": s.image,
            "default": s.name == default,
            "directory": core_config.quick_directory(Workspace(s.name)),
            "here": s.name == here_name,
        })
    render.OUT.workspace_list(rows)


def do_info(ctx: Ctx) -> None:
    """Inspect the *centralized* (non-workspace) config and state: the default
    pointer, resolved roots (config/state/builtin), the ordered overlays, the
    proxy image, the per-tier registry breakdown, and the env overrides in
    effect. The default workspace is a loose-only concept, so it appears only on
    the loose surface (consistent with `list`/`current`); everything else is
    surface-agnostic."""
    from collections import Counter
    from ..core import paths
    from ..core.model import presets as core_presets
    from ..core.model.injectors import list_injectors
    from ..core.providers import list_providers
    from ..core.model.scripts import list_scripts

    # Tier labels in resolution order (user > overlays > builtin), driving both
    # the registry counters and the render columns.
    roots = paths.overlay_roots()
    labels = [label for label, _ in roots]

    def tiers(counter: Counter) -> dict:
        return {label: counter.get(label, 0) for label in labels}

    registries = {
        "injectors": tiers(Counter(i.source for i in list_injectors())),
        "providers": tiers(Counter(p.source for p in list_providers())),
        "scripts": tiers(Counter(s.source_origin for s in list_scripts())),
        "presets": tiers(Counter(core_presets.load_preset_sources().values())),
    }

    overlays = paths.overlay_dirs()
    overlay_labels = {label for label, _ in overlays}
    # "overlay_overrides" = registry entries resolving from any overlay tier plus
    # 1 per overlay-supplied singleton. The EFFECTIVE view: an entry a user file
    # shadows counts as `user`, so it is NOT counted here.
    overlay_overrides = sum(
        c for r in registries.values()
        for label, c in r.items() if label in overlay_labels
    )
    tmpl = paths.resolve_singleton("workspace.template.toml")
    if tmpl is not None and any(tmpl == d / "workspace.template.toml"
                                for _, d in overlays):
        overlay_overrides += 1

    data: dict = {}
    if ctx.loose:  # the default pointer is a loose-only concept
        data["default_workspace"] = pointer.read_default()
    data["workspaces"] = len(core_workspace.list_names())
    data["paths"] = {
        "config": str(paths.config_dir()),
        "state": str(paths.state_dir()),
        "builtin": str(paths.BUILTIN_DIR),
    }
    data["overlays"] = [
        {"label": label, "path": str(d), "present": d.is_dir()}
        for label, d in overlays
    ]
    data["proxy_image"] = paths.IMAGE_TAG
    data["overlay_overrides"] = overlay_overrides
    data["registries"] = registries
    data["env"] = {
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
        "CREDPROXY_OVERLAY_PATH": os.environ.get("CREDPROXY_OVERLAY_PATH"),
        "EDITOR": os.environ.get("VISUAL") or os.environ.get("EDITOR"),
    }
    render.OUT.info(data)


def do_enter(ctx: Ctx, name: str | None, trailing: list[str],
             user_override: str | None = None, push: bool = False) -> None:
    if ctx.json:
        fail("enter does not support --json (it execs an interactive shell)")
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "enter")
    # Empty trailing -> the core runs the config `shell` (default: a login
    # shell); an explicit `-- CMD` runs bare. Resolved in _enter_exec_cmd, which
    # has the loaded config.
    exit_code = lifecycle.enter_workspace(
        ws, trailing, notify=say, user_override=user_override, push=push)
    sys.exit(exit_code)


def do_exec(ctx: Ctx, name: str | None, trailing: list[str], *,
            login: bool = False, raw: bool = False, push: bool = False,
            user: str | None = None) -> None:
    """One-shot: run `-- CMD...` in the workspace and propagate its exit code.
    The non-interactive sibling of `enter` -- never initiates an auto-stop, so
    it's safe to fire many times from a script.

    Environment: default sources the CA-trust env (like `enter -- CMD`); `--raw`
    is a direct execve (no shell, for minimal images); `--login` a bash login
    shell. `--user` overrides the config user for this call."""
    if not trailing:
        fail("`exec` needs a command: `credproxy workspace NAME exec -- CMD...` "
             "(for an interactive shell use `enter`)")
    if login and raw:
        fail("`--login` and `--raw` are mutually exclusive (they select different "
             "command environments)")
    # `exec` is a transparent pipe: the command's own stdout is arbitrary bytes,
    # not credproxy's to structure, so --json has nothing to wrap. Reject it
    # rather than emit non-JSON on a --json invocation (which a jq pipeline would
    # choke on); the exit code is already the process's exit code.
    if ctx.json:
        fail("`exec` streams the command's output verbatim; `--json` does not "
             "apply (the exit code is the command's exit code)")
    mode = "login" if login else "raw" if raw else "shim"
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "exec")
    exit_code = lifecycle.exec_workspace(
        ws, trailing, notify=say, mode=mode, user_override=user, push=push)
    sys.exit(exit_code)


def do_edit(ctx: Ctx, name: str | None) -> None:
    """Open the workspace's config file in $EDITOR, then validate it. The file
    is the source of truth; this is sugar over editing it directly."""
    import shlex
    import subprocess

    if ctx.json:
        fail("edit does not support --json (it opens an interactive editor)")
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    cmd = shlex.split(editor) + [str(ws.config_path)]
    try:
        rc = subprocess.run(cmd).returncode
    except FileNotFoundError:
        fail(f"could not launch editor '{editor}' (set $EDITOR or $VISUAL)")
    if rc != 0:
        fail(f"editor exited with status {rc}; config left as-is")

    # Post-edit validation: report problems but never revert -- it's the
    # user's file. load_config/load_bindings/load_rules parse and validate
    # without writing; `[[rule]]` is part of the file this edits, so validate it.
    from ..core.model import bindings as core_bindings
    from ..core.model import config as core_config
    from ..core.model import rules as core_rules
    # An attached workspace has no `start`; its follow-up verb is `push`
    # (`apply` is its alias there).
    attached = core_config.quick_attach(ws)
    try:
        core_config.load_config(ws)
        core_bindings.load_bindings(ws)
        core_rules.load_rules(ws)
    except CredproxyError as e:
        say(f"warning: config is invalid — {e}")
        hint = "`push`/`apply`" if attached else "`start`/`apply`"
        say(f"fix it before {hint}, or the workspace won't update cleanly.")
        return
    if attached:
        say("edited. changes are not live yet: `push` (or `apply`) sends them "
            "to the attached proxy.")
    else:
        say("edited. changes are not live yet: `apply` (bindings) or "
            "`start` (image/home/mounts/env/setup).")


def do_start(ctx: Ctx, name: str | None) -> None:
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "start")
    ensure_proxy_image(ctx)
    lifecycle.start_workspace(ws, notify=say)
    render.OUT.started(ws.name)


def do_stop(ctx: Ctx, name: str | None) -> None:
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _reject_if_attached(ws, "stop")
    lifecycle.stop_workspace(ws)
    render.OUT.stopped(ws.name)


def do_delete(ctx: Ctx, name: str | None, keep_volumes: bool) -> None:
    from ..core.model import config as core_config

    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _confirm_destructive(ctx, ws, implicit, "delete")
    was_default = _is_default(ws)
    # An attached workspace owns no containers/volumes -- remove only its config
    # file + state dir (its `attach` gate keeps the confirmation flow the same).
    if core_config.quick_attach(ws):
        lifecycle.delete_workspace(ws, keep_volumes=keep_volumes, containers=False)
    else:
        lifecycle.delete_workspace(ws, keep_volumes=keep_volumes)
    if was_default:
        pointer.clear_default()
    render.OUT.deleted(ws.name)


def do_apply(ctx: Ctx, name: str | None) -> None:
    from ..core.model import config as core_config

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    # An attached workspace has no container spec to reconcile -- `apply` IS a
    # push (resolve secrets + POST the full wire config to its external proxy).
    if core_config.quick_attach(ws):
        admin_url = lifecycle.push_workspace(ws, notify=say)
        render.OUT.pushed(ws.name, admin_url, attached=True, as_apply=True)
        return
    result = lifecycle.apply_config(ws, notify=say)
    render.OUT.applied(ws.name, result)


def do_push(ctx: Ctx, name: str | None, wait: bool, timeout: float) -> None:
    """`push`: resolve secrets and POST the full wire config (bindings + rules) to
    the workspace's proxy -- managed (its published port) or attached (the
    `attach` target). `--wait` polls /health first."""
    from ..core.model import config as core_config

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    attached = core_config.quick_attach(ws)
    admin_url = lifecycle.push_workspace(ws, notify=say, wait=wait, timeout=timeout)
    render.OUT.pushed(ws.name, admin_url, attached=attached)


def do_resolve(ctx: Ctx, name: str | None, out: str | None) -> None:
    """`resolve`: build the full wire config (with resolved secret VALUES) without
    contacting any proxy. Exactly one of `--json` (blob to stdout) or `--out FILE`
    (mode 0600)."""
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    if bool(ctx.json) == bool(out):
        fail("`resolve` needs exactly one of --json (blob to stdout) or "
             "--out FILE (writes mode 0600)")
    wire = lifecycle.resolve_workspace_wire(ws, notify=say)
    if out is not None:
        _write_resolved(ws, wire, out)
        render.OUT.resolved(ws.name, out)
    else:
        # --json: emit the wire blob (real secrets) to stdout.
        import json as _json
        print(_json.dumps(wire))


def _write_resolved(ws: Workspace, wire: dict, out: str) -> None:
    """Write the resolved wire config to `out`, mode 0600 (it carries real secret
    values -- the one at-rest disclosure path). Warn if `out` is outside the
    workspace state dir, where it could be committed to a repo."""
    import json as _json
    import os as _os

    path = _os.path.abspath(_os.path.expanduser(out))
    state = _os.path.abspath(str(ws.state_dir))
    if not (path == state or path.startswith(state + _os.sep)):
        say(f"warning: {out} is outside the workspace state dir -- it holds "
            f"RESOLVED secret values (mode 0600); do not commit it to a repo")
    fd = _os.open(path, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
    with _os.fdopen(fd, "w") as f:
        f.write(_json.dumps(wire) + "\n")


def do_recreate(ctx: Ctx, name: str | None, include_proxy: bool,
                reset_volumes: list[str]) -> None:
    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _reject_if_attached(ws, "recreate")
    # Plain recreate keeps all persistent state, so it isn't gated. --reset-volume
    # wipes a volume's data (the one recreate mode that destroys data), so it is
    # gated like delete: confirm on an implicit default workspace (loose surface).
    if reset_volumes:
        _confirm_destructive(ctx, ws, implicit, "reset volume(s) of")
    ensure_proxy_image(ctx)
    lifecycle.recreate_workspace(ws, notify=say, include_proxy=include_proxy,
                                 reset_volumes=reset_volumes)
    render.OUT.recreated(ws.name, include_proxy, reset_volumes)


def do_config(ctx: Ctx, name: str | None, declared: bool) -> None:
    """Dump a workspace's container-side config. Default mode is `effective` --
    every field with its in-effect value, all defaults filled (the workspaceFolder
    `workdir`, the enter shim, etc.) -- so you can see what actually applies
    without it being in the file. `--declared` shows only what's literally in the
    TOML, before defaults."""
    from ..core.model import config as core_config
    ws = _resolve_ws(ctx, name)
    if declared:
        cfg = core_config.declared_config(ws)
    else:
        cfg = lifecycle.effective_config(core_config.load_config(ws))
    render.OUT.config({
        "mode": "declared" if declared else "effective",
        "config_path": str(ws.config_path),
        "config": cfg,
    })


def do_inspect(ctx: Ctx, name: str | None) -> None:
    ws = _resolve_ws(ctx, name)
    data = lifecycle.inspect_workspace(ws)
    render.OUT.inspect({
        "name": data.name,
        "config_path": data.config_path,
        "config": data.config,
        "proxy_status": data.proxy_status,
        "ws_status": data.ws_status,
        "running": data.running,
        "host_port": data.host_port,
        "attach": data.attach,
        "attach_target": data.attach_target,
        "bindings": [
            {
                "name": b.name,
                "injector": b.injector,
                "provider": b.provider,
                "secret": b.secret,
                "hosts": list(b.hosts),
                "placeholder": b.placeholder,
                "env": b.env,
            }
            for b in data.bindings
        ],
        "rules": [
            {
                "name": r.name,
                "hosts": list(r.hosts),
                "methods": list(r.methods) if r.methods else None,
                "path": r.path,
                "action": r.action,
                "visible": r.effective_visible,
                "script": r.script,
            }
            for r in data.rules
        ],
        "drift": {
            "in_sync": data.drift.in_sync,
            "changes": [
                {
                    "kind": c.kind,
                    "item": c.item,
                    "applied": c.applied,
                    "configured": c.configured,
                }
                for c in data.drift.changes
            ],
        },
        # Context for drift label: stopped workspace means bindings in
        # applied-bindings.json were "last applied" not "live".
        "_running": data.running,
    })


def do_logs(ctx: Ctx, name: str | None, audit: bool = False) -> None:
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "logs")
    _logs_stream(ws, as_json=ctx.json, audit_only=audit)


# The proxy prefixes every structured record with this (see proxy/log.py).
_LOG_PREFIX = "credproxy "


def _parse_credproxy_line(line: str) -> dict | None:
    """Parse one `docker logs` line into a proxy structured record, or None if it
    isn't one. Requires the `credproxy ` prefix at the START of the line (not
    anywhere in it) plus a JSON object carrying a `kind`. Because the proxy
    JSON-encodes every untrusted value (a rule/scheme error message that can echo
    workspace input), such content is escaped inside the record and can NEVER
    spill a forged `credproxy {...}` line of its own -- the substring-forgery the
    old text stream allowed is structurally impossible."""
    import json
    if not line.startswith(_LOG_PREFIX):
        return None
    try:
        rec = json.loads(line[len(_LOG_PREFIX):])
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) and "kind" in rec else None


def _logs_stream(ws: Workspace, as_json: bool, audit_only: bool) -> None:
    """Tail `docker logs -f` and reformat the proxy's structured `credproxy {json}`
    records; mitmproxy's own termlog passes through verbatim (never mistaken for
    a proxy record). Default: pretty one line per record. `--json`: the raw
    records as JSON-lines (a non-proxy line wraps as `{"kind":"raw","line":...}`).
    `--audit`: only `kind == "audit"` records. docker's log driver is the durable
    store (survives stop/start)."""
    import json
    import subprocess

    try:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", ws.proxy_container],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except FileNotFoundError:
        raise DependencyError(core_docker.DOCKER_MISSING_MSG)
    interrupted = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            rec = _parse_credproxy_line(line)
            if audit_only:
                if rec is not None and rec.get("kind") == "audit":
                    print(json.dumps(rec) if as_json else _format_record(rec),
                          flush=True)
            elif rec is not None:
                print(json.dumps(rec) if as_json else _format_record(rec),
                      flush=True)
            elif as_json:      # non-proxy line (mitmproxy etc.)
                print(json.dumps({"kind": "raw", "line": line.rstrip("\n")}),
                      flush=True)
            else:
                print(line, end="", flush=True)   # pass mitmproxy output through
    except KeyboardInterrupt:
        interrupted = True
    finally:
        proc.terminate()
        rc = proc.wait()
    # A non-zero exit we didn't cause (Ctrl-C) is a real failure -- e.g. the
    # container doesn't exist; propagate it rather than reporting success.
    if not interrupted and rc:
        fail(f"docker logs exited with status {rc}")


def _format_record(rec: dict) -> str:
    """One-line human rendering of a proxy structured record (log.py). Tolerant of
    missing keys and unknown/future kinds."""
    ts = rec.get("ts", "")
    kind = rec.get("kind", "?")
    where = f"{rec.get('method', '')} {rec.get('host', '')}" \
            f"{rec.get('path', '')}".strip()
    if kind == "audit":
        subj = rec.get("binding") or rec.get("rule") or ""
        detail = " ".join(p for p in (subj and f"'{subj}'", rec.get("outcome", ""))
                          if p)
        return f"{ts}  audit {rec.get('event', '?'):<9} {where}  {detail}".rstrip()
    if kind in ("http", "api"):
        marks = rec.get("marks")
        return f"{ts}  {kind:<6} {where}" \
               f"{' (' + ' '.join(marks) + ')' if marks else ''}".rstrip()
    if kind == "sni":
        err = f" -- {rec['error']}" if rec.get("error") else ""
        return f"{ts}  sni    {rec.get('sni') or '<no-sni>'} " \
               f"({rec.get('decision', '?')}){err}"
    if kind == "rule-error":
        return f"{ts}  rule   {rec.get('rule', '')} failed: {rec.get('error', '')}"
    if kind in ("scheme", "script"):
        detail = rec.get("error") or rec.get("reason", "")
        # Sanitized script failures carry a safe source:line location (#33 rung 3).
        if rec.get("line") is not None:
            detail = f"{detail} at {rec.get('source', '?')}:{rec['line']}".strip()
        return f"{ts}  {kind:<6} {rec.get('scheme', '')} " \
               f"{rec.get('phase') or rec.get('hook', '')}: {detail}".rstrip(": ")
    rest = " ".join(f"{k}={v}" for k, v in rec.items() if k not in ("ts", "kind"))
    return f"{ts}  {kind:<6} {rest}".rstrip()


# ---- binding commands --------------------------------------------------------


def _parse_secret_args(
    values: list[str] | None, slots: tuple[str, ...] = (),
) -> str | dict[str, str] | None:
    """Turn repeated --secret values into a single bare ref (single-slot) or a
    slot->ref table (multi-slot).

    A lone --secret is a bare ref kept verbatim even if it contains '=' (e.g. a
    vault path with a query string) -- UNLESS it is written `SLOT=REF` and SLOT
    is one of the injector's declared `slots`, in which case it is that named
    slot (so `--secret private_key=REF` works for a single non-`value` slot like
    jwt-bearer's). Multi-slot requires two or more SLOT=REF flags; each is split
    on its first '=', so a REF may itself contain '='. Splitting on a declared
    slot name (not just any '=') is what keeps a lone `=`-containing ref
    unambiguous."""
    if not values:
        return None
    if len(values) == 1:
        slot, sep, ref = values[0].partition("=")
        if sep and ref and slot in slots:
            return {slot: ref}        # a single, explicitly-named slot
        return values[0]              # bare ref (the single-slot `value` sugar)
    out: dict[str, str] = {}
    for v in values:
        slot, sep, ref = v.partition("=")
        if not sep or not slot or not ref:
            fail(f"--secret '{v}' must be SLOT=REF for a multi-slot secret")
        if slot in out:
            fail(f"--secret slot '{slot}' given more than once")
        out[slot] = ref
    return out


def do_binding_add(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    from ..core.model import bindings as core_bindings
    from ..core.model.bindings import Binding
    from ..core.model.injectors import find_injector
    from ..core.providers import find_provider

    if a.injector is None:
        fail("`binding add` needs --injector (coordinated multi-binding sets and "
             "guardrails live in `workspace NAME preset add PRESET`)")

    if not a.host:
        fail("`binding add --injector` needs at least one --host")

    if not a.provider:
        fail("`binding add --injector` needs --provider")

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    injector = find_injector(a.injector)
    find_provider(a.provider)

    # Parse --secret with the injector's declared slots, so a lone
    # `--secret SLOT=REF` for a single named slot (e.g. jwt-bearer's
    # private_key) is recognized rather than swallowed as a bare `value` ref.
    secret = _parse_secret_args(a.secret, injector.spec.slots)
    if secret is None:
        fail("`binding add` needs --secret")

    # Lock the read-validate-write: two concurrent `binding add` must not both
    # read the same file, pick the same auto-name, and last-writer-wins (the
    # per-file atomic write prevents a torn file, not a lost update).
    with ws.lock():
        existing = core_bindings.load_bindings(ws)
        taken = {b.name for b in existing}
        bname = a.binding_name or core_bindings._auto_name(a.injector, a.provider, taken)
        if bname in taken:
            fail(f"binding name '{bname}' already exists in workspace '{ws.name}'")

        # An explicit --placeholder is written into the block (hand-owned, wins);
        # otherwise the placeholder is LOCK-managed -- nothing is written into the
        # TOML, and resolve_workspace mints its identity into the lock below.
        placeholder = a.placeholder
        # `--no-env` writes `env = false` (suppress the injector's hint); else
        # bake the effective env (explicit override, or the injector's hint) so
        # the file records the concrete choice.
        if a.no_env:
            env = None
            env_suppressed = True
        else:
            env = a.env or injector.env
            env_suppressed = False

        binding = Binding(
            name=bname,
            injector=a.injector,
            provider=a.provider,
            secret=secret,
            hosts=tuple(a.host),
            placeholder=placeholder,
            env=env,
            env_suppressed=env_suppressed,
        )
        core_bindings.validate(existing + [binding], str(ws.config_path))

        # Snapshot BEFORE appending: resolve_workspace below runs the full
        # container-half + wire validation, so a PRE-EXISTING unrelated error
        # (missing image, bad [[mounts]]) would raise AFTER the block is on disk,
        # leaving the hand-owned file half-written. Restore on any failure.
        original = ws.config_path.read_text()
        core_bindings.append_binding(ws, binding)

        # Mint the (lock-managed) placeholder identity now, so it is stable for a
        # later `resolve`/`push --config` -- resolve_workspace re-validates the
        # whole file and records generated placeholders into the lock.
        from ..core.model.lock import save_lock
        from ..core.model.resolver import resolve_workspace
        from ..core.paths import atomic_write_text
        try:
            resolved = resolve_workspace(ws)
        except CredproxyError as e:
            atomic_write_text(ws.config_path, original)  # never half-write
            fail(f"binding not added: workspace '{ws.name}' config has a "
                 f"pre-existing error: {e}")
        if resolved.lock_dirty:
            save_lock(ws, resolved.lock)
        placeholder = next((b.placeholder for b in resolved.bindings
                            if b.name == bname), placeholder)

    from ..core.model import config as core_config
    render.OUT.binding_added(bname, ws.name, {
        "name": bname,
        "injector": binding.injector,
        "provider": binding.provider,
        "secret": binding.secret,
        "hosts": list(binding.hosts),
        "placeholder": placeholder,
        "env": env,
    }, attached=core_config.quick_attach(ws))


def _newly_intercepted(existing_hosts, new_hosts) -> list[str]:
    """Hosts the preset newly flips to TLS-intercepted: `new_hosts` not already
    covered by `existing_hosts` (a literal already named, or matched by an
    existing glob). Adding a rule to a previously-passthrough host intercepts it
    (the UNION intercept set), so `preset add` announces this rather than letting
    the operator discover a fresh CA-cert error."""
    from ..core.model import hostmatch
    existing_lowered = {h.lower() for h in existing_hosts}
    globs = [hostmatch.compile_pattern(h.lower())
             for h in existing_hosts if hostmatch.is_pattern(h)]
    out, seen = [], set()
    for h in new_hosts:
        hl = h.lower()
        if hl in seen:
            continue
        seen.add(hl)
        already = hl in existing_lowered or (
            not hostmatch.is_pattern(h) and any(g.fullmatch(hl) for g in globs))
        if not already:
            out.append(h)
    return out


def _expand_preset_into_text(text: str, exp, preset_name: str, source: str,
                             context: str = "add"):
    """Shared preset expand-validate-compose core used by BOTH `preset add` and
    `create`'s template-declared `[[preset]]` expansion (#57): validate a
    `PresetExpansion` against the CURRENT TOML `text` (attach/container-half,
    double-add guard, name/mount-target/env collisions, cross-binding/rule
    semantic checks) and return `(new_text, announce)` -- the composed TOML plus
    the render-ready announcement dict. Pure w.r.t. disk (no read/write); the
    caller writes `new_text`. Raises via `fail()` on any conflict, so nothing is
    written on failure -- the basis for `create`'s all-or-nothing guarantee.

    Existing state is derived from `text` itself (parsed bindings/rules with
    auto-names filled + the normalized container config), so `create` can feed the
    ACCUMULATING text across multiple template entries and each is validated
    against every prior stamp + the literal template config.

    `context` (`"add"` | `"create"`) only flavors the duplicate-pack remedy: at
    `preset add` the fix is to remove the stamped blocks; at `create` (template
    `[[preset]]`) it's to remove the duplicate template entry."""
    from ..core.model import bindings as core_bindings
    from ..core.model import config as core_config
    from ..core.model import preset_stamp
    from ..core.model import rules as core_rules
    from ..core.model.presets import mount_table

    dup_remedy = (
        "remove the duplicate `[[preset]]` entry from the template"
        if context == "create"
        else "remove the stamped blocks first to re-apply")

    # Normalized container config from the current text; attach detection rides it
    # (attached -> empty mounts/env, so the container-half loops below are no-ops).
    # Defer host-bind existence (check_bind_exists=False): a prior stamp (or this
    # pack's own option-fed mount) may name a source that need not exist until
    # runtime (a socket dir, `~/.ssh/...-agent`) -- `start` existence-checks it.
    cfg = core_config.load_config_from_text(text, source, check_bind_exists=False)
    attached = cfg.get("attach") is not None

    # An attached workspace has no credproxy-managed container, so it can't accept
    # the container half (mounts/env/setup). Binding/rule-only packs still apply.
    if attached and exp.has_container_half:
        fail(f"preset '{preset_name}' carries container-half config "
             f"(mounts/env/setup), but the workspace is attached -- its container "
             f"is managed externally, so credproxy can't stamp a mount/env/setup "
             f"for it. Only binding/rule-only packs apply to an attached workspace.")

    # Double-add guard: any provenance marker for THIS preset already present
    # (protects pure-container packs, which have no binding-name clash to trip).
    if preset_stamp.already_applied(text, preset_name):
        fail(f"preset '{preset_name}' is already applied here (its provenance "
             f"marker is in the config; {dup_remedy})")

    raw = tomllib.loads(text)
    existing_b = core_bindings._with_auto_names(
        core_bindings._parse_bindings(raw, source))
    existing_r = core_rules._with_auto_names(core_rules._parse_rules(raw, source))
    new_b, new_r = list(exp.bindings), list(exp.rules)

    # Collision: a generated <preset>-<suffix> clashing with an existing name
    # fails the WHOLE add before any write (no partial stamp).
    btaken = {b.name for b in existing_b}
    rtaken = {r.name for r in existing_r}
    for b in new_b:
        if b.name in btaken:
            fail(f"preset '{preset_name}' would create binding '{b.name}', which "
                 f"already exists (no changes made)")
    for r in new_r:
        if r.name in rtaken:
            fail(f"preset '{preset_name}' would create rule '{r.name}', which "
                 f"already exists (no changes made)")

    # Container-half collisions (a no-op for attached, whose cfg mounts/env are
    # empty and whose container half was rejected above). Normalize each new mount
    # through the SAME config._parse_mount (bind kept literal) so the merged-set
    # check matches load_config exactly.
    env_to_stamp: list[tuple[str, str]] = []
    skipped_env: list[str] = []
    new_mount_norms = [
        core_config._parse_mount(mount_table(m),
                                 f"preset '{preset_name}' mount",
                                 expand_bind=False)
        for m in exp.mounts
    ]
    existing_targets = {m["target"].rstrip("/") or "/" for m in cfg["mounts"]}
    for nm in new_mount_norms:
        t = nm["target"].rstrip("/") or "/"
        if t in existing_targets:
            fail(f"preset '{preset_name}' would mount at {nm['target']!r}, which "
                 f"is already mounted (no changes made)")
    core_config.validate_mount_set(
        cfg["mounts"] + new_mount_norms, source, cfg["user"])

    # env: absent -> stamp; present-identical -> skip + note; present but
    # DIFFERENT -> fail the whole add.
    for k, v in exp.env:
        if k in cfg["env"]:
            if cfg["env"][k] == v:
                skipped_env.append(k)
                continue
            fail(f"preset '{preset_name}' sets env {k}={v!r}, but env {k}="
                 f"{cfg['env'][k]!r} is already set (different value; no changes made)")
        env_to_stamp.append((k, v))

    # Full semantic validation on the combined binding/rule sets (cross-binding
    # /rule collisions, script resolution) before writing. Run UNCONDITIONALLY --
    # even a pack that adds no bindings/rules must still validate the EXISTING set
    # standalone, so a pre-existing invalidity (e.g. a duplicate binding name)
    # surfaces here at `preset add`/`create`, not deferred to `start` (finding 7).
    core_bindings.validate(existing_b + new_b, source)
    core_rules.validate(existing_r + new_r, source)

    existing_hosts = [h for b in existing_b for h in b.hosts] \
        + [h for r in existing_r for h in r.hosts]
    new_hosts = [h for b in new_b for h in b.hosts] \
        + [h for r in new_r for h in r.hosts]
    newly = _newly_intercepted(existing_hosts, new_hosts)

    new_text = preset_stamp.compose(
        text, preset_name, exp.rev,
        bindings=new_b, rules=new_r,
        mounts=list(exp.mounts), env_items=env_to_stamp,
        setup=[dict(s) for s in exp.setup])

    announce = {
        "preset": preset_name,
        "attached": attached,
        "bindings": [{"name": b.name, "injector": b.injector,
                      "provider": b.provider, "secret": b.secret,
                      "hosts": list(b.hosts), "placeholder": b.placeholder,
                      "env": b.env} for b in new_b],
        "rules": [{"name": r.name, "hosts": list(r.hosts), "action": r.action,
                   "script": r.script, "visible": r.effective_visible}
                  for r in new_r],
        "newly_intercepted": newly,
        "mounts": [{"kind": m.kind, "source": m.value, "target": m.target}
                   for m in exp.mounts],
        "env": [{"key": k, "value": v} for k, v in env_to_stamp],
        "skipped_env": skipped_env,
        "setup": [dict(s) for s in exp.setup],
        # A stamped container half drifts the spec hash. Gate on what was ACTUALLY
        # stamped, not merely what the pack CARRIES: an env-only pack whose every
        # key was skipped as present-identical wrote a byte-identical file (no
        # drift). Mounts/setup are always stamped when present (a collision fails).
        "stamped_container_half": bool(exp.mounts or exp.setup or env_to_stamp),
    }
    return new_text, announce


def _parse_opt_flags(opts: list[str] | None) -> dict:
    """Parse repeatable `--opt id=value` flags into `{id: value}` (values are raw
    strings; coercion against each option's type happens in `resolve_options`). A
    later `--opt` for the same id wins. Malformed (`no '='`, empty id) fails."""
    out: dict = {}
    for raw in opts or []:
        if "=" not in raw:
            fail(f"--opt expects id=value, got {raw!r}")
        key, val = raw.split("=", 1)
        key = key.strip()
        if not key:
            fail(f"--opt expects a non-empty id, got {raw!r}")
        out[key] = val
    return out


def _resolve_preset_option_values(ctx: Ctx, spec, explicit: dict) -> dict:
    """Resolve every pack `[[option]]` to a value in the settled order (#59):
    explicit (`--opt`/template `[preset.options]`) -> prompt (loose+TTY only) ->
    default -> structured fail. Returns `{id: value}` (empty for an option-less
    pack). Raises `PresetOptionsError` (structured under `--json`) when a required
    option can't be resolved without prompting."""
    if not spec.options:
        # An explicit --opt for a pack that declares no options is a typo worth
        # surfacing (resolve_options rejects unknown ids), so still route through it.
        if not explicit:
            return {}
    from ..core.errors import PresetOptionsError
    from ..core.model.presets import option_summary, resolve_options
    from . import prompt as prompt_mod

    ask = prompt_mod.ask_option if prompt_mod.prompting_enabled(ctx) else None
    values, missing = resolve_options(spec, explicit, prompt=ask)
    if missing:
        raise PresetOptionsError(spec.name, [option_summary(o) for o in missing])
    return values


def _resolve_preset_credential_interactive(
        ctx: Ctx, spec, provider_arg, secret_arg, *, on_missing):
    """Resolve a pack's provider/secret with the shared defaulting, THEN prompt on
    loose+TTY for anything still missing (decision 4: provider picker + secret with
    a validate-at-prompt loop). Strict / loose-no-TTY don't prompt -- `on_missing`
    (a callable taking the `missing` list) fires instead, rendering the caller's
    own structured/human error. Returns `(provider, secret)`."""
    from ..core.model.presets import resolve_preset_credential
    from . import prompt as prompt_mod

    provider, secret, missing = resolve_preset_credential(
        spec, provider_arg, secret_arg)
    if missing and prompt_mod.prompting_enabled(ctx):
        if "provider" in missing:
            provider = prompt_mod.ask_provider(spec.default_provider)
            # Re-apply defaulting now the provider is known (a prompted provider
            # equal to default_provider makes default_secret eligible).
            provider, secret, missing = resolve_preset_credential(
                spec, provider, secret_arg)
        if "secret" in missing:
            hint_default = (spec.default_secret
                            if provider == spec.default_provider else None)
            secret = prompt_mod.ask_secret(provider, hint_default)
            missing = [m for m in missing if m != "secret"]
    if missing:
        on_missing(missing)   # renders + exits (fail / raise)
    return provider, secret


def do_preset_add(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    """Apply a preset as a service setup pack: stamp its `[[binding]]` set AND its
    `[[rule]]` guardrails into the workspace, all-or-nothing. A pure-rule preset
    needs no provider/secret."""
    from ..core.model.presets import build_preset, get_preset
    from ..core.providers import find_provider

    spec = get_preset(a.preset)               # CredproxyError -> clean fail on unknown

    # Pack options (#59): resolved before the credential so a bad --opt fails fast.
    option_values = _resolve_preset_option_values(
        ctx, spec, _parse_opt_flags(a.opt))

    provider = secret = None
    if spec.needs_credential:
        # Secret: explicit, else the preset default (applied in
        # resolve_preset_credential -- the shared defaulting core create reuses).
        secret_arg = _parse_secret_args(a.secret)
        if secret_arg is not None and not isinstance(secret_arg, str):
            fail("`preset add` needs a single --secret REF")

        def _missing(missing):
            if "provider" in missing:
                fail("preset '%s' has bindings but no default provider -- pass "
                     "--provider" % a.preset)
            fail("`preset add` needs --secret (its meaning depends on --provider)")

        provider, secret = _resolve_preset_credential_interactive(
            ctx, spec, a.provider, secret_arg, on_missing=_missing)
        find_provider(provider)
    elif a.provider or a.secret:
        # No bindings -> needs no credential. Name the pack's actual shape so the
        # message is accurate for a pure-CONTAINER (mounts/env/setup) or
        # pure-rule pack, not just a rule pack.
        shape = "container-only (mounts/env/setup)" if spec.has_container_half \
            else "pure-rule"
        fail(f"preset '{a.preset}' is a {shape} pack with no bindings -- it "
             f"needs no credential, so --provider/--secret don't apply")

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    from ..core.paths import atomic_write_text
    from ..core.model.presets import apply_option_values

    exp = build_preset(a.preset, provider, secret, options=option_values)
    # Requires (#58) aren't stamped, so their option markers are substituted here
    # (the literal spec) for the advisory prereq run below.
    literal_spec = apply_option_values(spec, option_values) if spec.options else spec

    with ws.lock():                          # atomic read-validate-write
        text = ws.config_path.read_text()
        new_text, announce = _expand_preset_into_text(
            text, exp, a.preset, str(ws.config_path))
        atomic_write_text(ws.config_path, new_text)

    attached = announce["attached"]
    stamped_container_half = announce["stamped_container_half"]
    # `preset add` is otherwise a pure config edit, so a missing/unreachable
    # docker must not fail it: if we can't check, assume no container.
    container_exists = False
    if not attached and stamped_container_half:
        from ..core.errors import CredproxyError
        try:
            container_exists = \
                core_docker.container_status(ws.ws_container) is not None
        except CredproxyError:
            container_exists = False
    # Host-prerequisite checks (#58): advisory here -- the stamp already landed
    # (durable config), so a failing check reports + hints but never fails the
    # add. `do_fetch=True` (a `fetch=true` provider check is an interactive user
    # action, like `binding test`). Report ALL, not fail-first.
    from ..core.model import prereqs
    requires = [prereqs.summary(r) for r in prereqs.evaluate(
        literal_spec.requires, provider=provider, secret=secret, do_fetch=True)]
    render.OUT.preset_applied(
        ws.name, announce["preset"], announce["bindings"], announce["rules"],
        announce["newly_intercepted"], mounts=announce["mounts"],
        env=announce["env"], skipped_env=announce["skipped_env"],
        setup=announce["setup"],
        recreate=(container_exists and stamped_container_half),
        attached=attached, requires=requires)


def do_preset_refresh(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    """Re-expand stamped pack(s) against their current definitions and update the
    workspace TOML: per-block update-cleanly / skip-hand-edited (with a diff) /
    add / prune (only under --prune). No PRESET -> every applied pack; explicit
    PRESET -> just that pack (error if unknown or not applied)."""
    from ..core.model import config as core_config
    from ..core.model import preset_refresh, preset_stamp
    from ..core.paths import atomic_write_text
    from ..core.model.presets import load_presets

    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    # --prune deletes stamped blocks (high recovery cost) -> destructive gate.
    if a.prune:
        _confirm_destructive(ctx, ws, implicit, "prune stamped blocks of")

    source = str(ws.config_path)
    with ws.lock():                          # atomic read-classify-write
        text = ws.config_path.read_text()
        applied = preset_stamp.applied_preset_names(text)
        attached = core_config.quick_attach(ws)

        if a.preset is not None:
            if a.preset not in applied:
                fail(f"preset '{a.preset}' is not applied to workspace "
                     f"'{ws.name}' (no provenance marker); apply it first with "
                     f"`preset add {a.preset}`")
            targets = [a.preset]
        else:
            targets = applied
            if not targets:
                say(f"workspace '{ws.name}' has no applied presets -- nothing "
                    f"to refresh")
                render.OUT.preset_refreshed(
                    ws.name, [], newly_intercepted=[], container_changed=False,
                    attached=attached, skipped_unresolved=[],
                    skipped_attached=[], container_exists=False)
                return

        known = load_presets()
        results: list[dict] = []
        skipped_unresolved: list[str] = []   # no longer in the registry
        skipped_attached: list[str] = []     # container-half pack, attached ws
        container_changed = False
        cur = text
        for pname in targets:
            spec = known.get(pname)
            if spec is None:
                if a.preset is not None:
                    fail(f"preset '{pname}' is no longer in the registry, so it "
                         f"can't be refreshed (its stamped blocks remain)")
                skipped_unresolved.append(pname)
                continue
            # An attached workspace can't accept a container-half refresh (its
            # container is external) -- mirror `preset add`'s refusal.
            if attached and (spec.has_container_half
                             or _has_stamped_container_half(cur, pname)):
                if a.preset is not None:
                    fail(f"preset '{pname}' carries container-half config "
                         f"(mounts/env/setup), but the workspace is attached -- "
                         f"only binding/rule-only packs refresh on an attached "
                         f"workspace")
                skipped_attached.append(pname)
                continue
            res = preset_refresh.refresh_preset(
                cur, pname, spec, prune=a.prune, source=source)
            cur = res.new_text
            container_changed = container_changed or res.container_changed
            results.append({
                "preset": res.preset,
                "changed": res.changed,
                "actions": [_refresh_action_dict(act) for act in res.actions],
            })

        newly = _newly_intercepted_between(text, cur) if cur != text else []
        if cur != text:
            atomic_write_text(ws.config_path, cur)

    # Gate the container-drift restart hint on the workspace container actually
    # EXISTING (mirrors `do_preset_add`): a never-created workspace shows no
    # spurious "restart to apply" line. A missing/unreachable docker means we
    # can't check -> assume no container.
    container_exists = False
    if not attached and container_changed:
        from ..core.errors import CredproxyError
        try:
            container_exists = \
                core_docker.container_status(ws.ws_container) is not None
        except CredproxyError:
            container_exists = False

    render.OUT.preset_refreshed(
        ws.name, results, newly_intercepted=newly,
        container_changed=container_changed, attached=attached,
        skipped_unresolved=skipped_unresolved,
        skipped_attached=skipped_attached, container_exists=container_exists)


def _refresh_action_dict(act) -> dict:
    """One refresh action -> a JSON-clean dict. `diff` (set only for a
    skipped-edited block) is omitted when null, per the `diff?` shape."""
    d = {"kind": act.kind, "target": act.target, "action": act.action}
    if act.diff is not None:
        d["diff"] = act.diff
    return d


def _has_stamped_container_half(text: str, preset_name: str) -> bool:
    """True iff `text` carries a stamped mounts/env/setup element for
    `preset_name` (so refresh would touch the container half even if the current
    definition no longer declares one -- e.g. a prune)."""
    from ..core.model import preset_refresh
    return any(s.kind in ("env", "mount", "setup")
               for s in preset_refresh._locate(text, preset_name))


def _newly_intercepted_between(old_text: str, new_text: str) -> list[str]:
    """Hosts newly TLS-intercepted by a refresh: bindings/rules hosts present in
    `new_text` but not already covered by `old_text`'s host set."""
    from ..core.model import bindings as core_bindings
    from ..core.model import rules as core_rules

    def _hosts(text: str) -> list[str]:
        raw = tomllib.loads(text)
        bs = core_bindings._with_auto_names(
            core_bindings._parse_bindings(raw, "refresh"))
        rs = core_rules._with_auto_names(core_rules._parse_rules(raw, "refresh"))
        return [h for b in bs for h in b.hosts] + [h for r in rs for h in r.hosts]

    return _newly_intercepted(_hosts(old_text), _hosts(new_text))


def do_binding_remove(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    from ..core.model import bindings as core_bindings

    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _confirm_destructive(ctx, ws, implicit, "remove binding from")
    with ws.lock():                          # atomic read-modify-write of the TOML
        core_bindings.remove_binding(ws, a.binding_name)
    render.OUT.binding_removed(a.binding_name, ws.name)


def do_binding_list(ctx: Ctx, name: str | None) -> None:
    from ..core.model.resolver import resolve_workspace

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    # Read-only: resolve placeholders from the lock in memory, never persist (a
    # not-yet-persisted placeholder is ephemeral until start/push/add/test mints
    # it into the lock).
    bindings = resolve_workspace(ws).bindings
    rows = [
        {
            "name": b.name,
            "injector": b.injector,
            "provider": b.provider,
            "secret": b.secret,
            "hosts": list(b.hosts),
            "placeholder": b.placeholder,
            "env": b.env,
        }
        for b in bindings
    ]
    render.OUT.binding_list(ws.name, rows)


def do_binding_test(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    from ..core.model import bindings as core_bindings

    # Ad-hoc mode: `binding test --provider P --secret REF [--injector I]`
    # exercises a definition before it is bound -- no workspace involved.
    if a.injector is not None or a.provider is not None or a.secret is not None:
        _do_binding_test_adhoc(ctx, name, a)
        return

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    # Resolve placeholders from the lock and PERSIST it: `binding test` mints the
    # placeholder identity a later `push --config` (from `resolve`) relies on. The
    # provider fetch below needs no lock (and can be slow).
    from ..core.model.lock import save_lock
    from ..core.model.resolver import resolve_workspace
    with ws.lock():
        resolved = resolve_workspace(ws)
        if resolved.lock_dirty:
            save_lock(ws, resolved.lock)
    bindings = resolved.bindings
    if a.binding_name is not None:
        bindings = [b for b in bindings if b.name == a.binding_name]
        if not bindings:
            fail(f"binding '{a.binding_name}' not found in workspace '{ws.name}'")

    # Batch by provider: a workspace whose bindings share one provider (e.g. a
    # vault) resolves it once for the whole `binding test`, not once per binding.
    results = []
    any_fail = False
    for b, r in zip(bindings, core_bindings.test_bindings(bindings)):
        if not r.ok:
            any_fail = True
        results.append({
            "name": b.name,
            "provider": b.provider,
            "ok": r.ok,
            "value_len": r.value_len,
            "error": r.error,
            "note": r.note,
        })
    render.OUT.binding_test(results)
    if any_fail:
        sys.exit(1)


def _do_binding_test_adhoc(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    """Standalone test of a definition before it is bound: resolve the
    injector/provider, exec the provider, report ok/length. No workspace."""
    from ..core.model import bindings as core_bindings
    from ..core.model.injectors import find_injector
    from ..core.providers import find_provider

    if a.binding_name is not None:
        fail("cannot combine a binding NAME with ad-hoc --provider/--secret")

    # Resolve the injector first (if any) so its declared slots disambiguate a
    # lone `--secret SLOT=REF` for a single named slot (parity with binding add).
    slots: tuple[str, ...] = ()
    label = a.provider
    if a.injector is not None:
        slots = find_injector(a.injector).spec.slots  # raises InjectorError
        label = f"{a.injector}-{a.provider}"

    secret = _parse_secret_args(a.secret, slots)
    if not a.provider or secret is None:
        fail("ad-hoc `binding test` needs --provider and --secret")

    find_provider(a.provider)  # raises ProviderError if it doesn't resolve

    probe = core_bindings.Binding(
        name=label, injector=a.injector or "", provider=a.provider,
        secret=secret, hosts=(), placeholder=None, env=None,
    )
    r = core_bindings.test_binding(probe)
    render.OUT.binding_test([{
        "name": label,
        "provider": a.provider,
        "ok": r.ok,
        "value_len": r.value_len,
        "error": r.error,
        "note": r.note,
    }])
    if not r.ok:
        sys.exit(1)


# ---- rule commands -----------------------------------------------------------


def _parse_kv(values: list[str] | None, flag: str) -> dict | None:
    """Parse repeated `K=V` flags into a dict (order-preserving). None if unset."""
    if not values:
        return None
    out: dict[str, str] = {}
    for v in values:
        key, sep, val = v.partition("=")
        if not sep or not key:
            fail(f"{flag} '{v}' must be K=V")
        out[key] = val
    return out


def _rule_row(rule) -> dict:
    """Operator-facing rule summary for rendering (no secret -- rules have none;
    params are operator-plaintext config, so they ride the row -- workspace-facing
    disclosure is /setup, which excludes them)."""
    return {
        "name": rule.name,
        "hosts": list(rule.hosts),
        "methods": list(rule.methods) if rule.methods else None,
        "path": rule.path,
        "action": rule.action,
        "visible": rule.effective_visible,
        "script": rule.script,
        "status": rule.status,
        "params": rule.params,
    }


def do_rule_add(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    from dataclasses import replace as _replace

    from ..core.model import rules as core_rules

    action = a.rule_action                   # the subcommand: block/respond/rewrite/script
    if not a.host:
        fail("`rule add` needs at least one --host")

    # Each action's subparser owns exactly its own flags (argparse rejects the
    # rest), so we just marshal the flags that are present into the `[[rule]]`
    # entry shape and route it through the ONE field validator (_parse_rule_entry)
    # -- the same one the load path uses. Missing/malformed action params (a
    # respond without --status, an empty rewrite) fail there, uniformly.
    entry: dict = {"action": action, "hosts": list(a.host)}
    if a.method:
        entry["methods"] = list(a.method)
    if a.path is not None:
        entry["path"] = a.path
    if a.rule_visible is not None:
        entry["visible"] = a.rule_visible
    if action == "block":
        if a.status is not None:
            entry["status"] = a.status
    elif action == "respond":
        if a.status is not None:
            entry["status"] = a.status       # _parse_rule_entry requires it
        if a.body is not None:
            entry["body"] = a.body
        if a.header:
            entry["headers"] = _parse_kv(a.header, "--header")
    elif action == "rewrite":
        if a.header:
            entry["set_headers"] = _parse_kv(a.header, "--header")
        if a.remove_header:
            entry["remove_headers"] = list(a.remove_header)
        if a.resp_header:
            entry["resp_set_headers"] = _parse_kv(a.resp_header, "--resp-header")
        if a.resp_remove_header:
            entry["resp_remove_headers"] = list(a.resp_remove_header)
    elif action == "script":
        if a.script is not None:
            entry["script"] = a.script

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    with ws.lock():                          # atomic read-validate-write
        existing = core_rules.load_rules(ws)
        taken = {r.name for r in existing}
        if a.rule_name:
            entry["name"] = a.rule_name
        rule = core_rules._parse_rule_entry(entry, str(ws.config_path), "rule add")
        if rule.name is None:
            rule = _replace(rule, name=core_rules._auto_name(rule, taken))
        if rule.name in taken:
            fail(f"rule name '{rule.name}' already exists in workspace '{ws.name}'")
        core_rules.validate(existing + [rule], str(ws.config_path))

        # Snapshot before the append: resolve_workspace runs the full config-plane
        # validation, so a PRE-EXISTING unrelated error would raise after the block
        # is on disk. Restore on failure (mirrors do_binding_add).
        original = ws.config_path.read_text()
        core_rules.append_rule(ws, rule)
        # Persist a dirty lock (spec: rule add persists it too) -- a rule carries
        # no placeholder, but resolving the whole workspace may prune a stale
        # lock entry / mint a sibling binding's placeholder that isn't recorded yet.
        from ..core.model.lock import save_lock
        from ..core.model.resolver import resolve_workspace
        from ..core.paths import atomic_write_text
        try:
            resolved = resolve_workspace(ws)
        except CredproxyError as e:
            atomic_write_text(ws.config_path, original)  # never half-write
            fail(f"rule not added: workspace '{ws.name}' config has a "
                 f"pre-existing error: {e}")
        if resolved.lock_dirty:
            save_lock(ws, resolved.lock)

    from ..core.model import config as core_config
    render.OUT.rule_added(rule.name, ws.name, _rule_row(rule),
                          attached=core_config.quick_attach(ws))


def do_rule_remove(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    from ..core.model import rules as core_rules

    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _confirm_destructive(ctx, ws, implicit, "remove rule from")
    with ws.lock():
        core_rules.remove_rule(ws, a.rule_name)
        # Persist a dirty lock (spec: rule remove persists it too) -- e.g. a
        # stale placeholder entry pruned on resolve. The removal already
        # succeeded and is the user's intent, so a resolve failure from an
        # UNRELATED pre-existing config error (a broken container half) must
        # not fail the command; the lock reconciles on the next resolve.
        from ..core.errors import CredproxyError
        from ..core.model.lock import save_lock
        from ..core.model.resolver import resolve_workspace
        try:
            resolved = resolve_workspace(ws)
        except CredproxyError:
            resolved = None
        if resolved is not None and resolved.lock_dirty:
            save_lock(ws, resolved.lock)
    render.OUT.rule_removed(a.rule_name, ws.name)


def do_rule_list(ctx: Ctx, name: str | None) -> None:
    from ..core.model.resolver import resolve_workspace

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    # Read-only: rules carry no placeholder, so this just parses + validates
    # (names are hand-authored now); nothing is written.
    rules = resolve_workspace(ws).rules
    render.OUT.rule_list(ws.name, [_rule_row(r) for r in rules])


def do_rule_test(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    from urllib.parse import urlsplit

    from ..core.model import rules as core_rules

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    if getattr(a, "rule_live", False):
        _do_rule_test_live(ctx, ws, a)
        return

    parts = urlsplit(a.url)
    host = parts.hostname
    if not host:
        fail(f"'{a.url}' has no host (use a full URL, e.g. https://api.github.com/x)")
    path = parts.path or "/"
    # Read-only (offline dry-run): parse + validate, no write.
    from ..core.model.resolver import resolve_workspace
    rules = resolve_workspace(ws).rules
    matches = core_rules.match_rules(rules, a.method, host, path)
    # Enrich each match with the rule's action detail (status/script) for display.
    by_name = {r.name: r for r in rules}
    rows = []
    for m in matches:
        r = by_name[m.name]
        rows.append({"name": m.name, "action": m.action, "visible": m.visible,
                     "script": r.script, "status": r.status,
                     "terminal": m.terminal, "may_terminate": m.may_terminate,
                     "conditional": m.conditional})
    render.OUT.rule_test(a.method.upper(), a.url, rows)


def _do_rule_test_live(ctx: Ctx, ws: Workspace, a: argparse.Namespace) -> None:
    """`rule test --live`: ask the RUNNING proxy for the authoritative answer
    (exact per-script phase + intercept decision) against its LOADED config --
    which may lag the edited TOML until `apply`/`start`/`push`. Routes through the
    same target resolution as `push`, so it works for an attached workspace too
    (its externally-run proxy, via the `attach` selector)."""
    from ..core.engine import push as core_push
    from ..core.model.workspace import read_token

    admin_url = lifecycle.resolve_admin_url(ws, notify=say)
    result = core_push.rule_test(admin_url, read_token(ws), a.method, a.url)
    render.OUT.rule_test_live(a.method.upper(), a.url, result)


# ---- mount commands ----------------------------------------------------------


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
        sessions = (lifecycle._count_live_sessions(ws)
                    if status == "running" else 0)
        if sessions:
            _confirm_running_recreate(ctx, ws, sessions)

    lifecycle.add_managed_volume(
        ws, name=a.mount_volume, target=a.mount_target,
        readonly=a.mount_ro, preserve=a.mount_preserve,
        user_owned=a.mount_user_owned, notify=say,
    )
    render.OUT.mount_added(ws.name, a.mount_volume, a.mount_target,
                           a.mount_ro, applied=a.mount_preserve)


# ---- injector / provider -----------------------------------------------------


def do_scaffold(ctx: Ctx, kind: str, name: str, lang: str = "python") -> None:
    from ..core.model.scaffold import scaffold

    result = scaffold(kind, name, lang)
    render.OUT.scaffolded(result.kind, result.name, str(result.path))
    if kind == "provider":
        say("the template is just a starting point -- a provider can be any "
            "executable that speaks the JSON protocol (docs/reference/providers.md).")


def do_def_list(ctx: Ctx, kind: str) -> None:
    if kind == "injector":
        from ..core.model.injectors import list_injectors
        rows = [
            {
                "name": d.name,
                "scheme": d.scheme if d.scheme != "script"
                else f"script:{d.spec.family}",
                "source": d.source,
                "shadows": list(d.shadows),
            }
            for d in list_injectors()
        ]
    else:
        from ..core.providers import list_providers
        rows = [
            {"name": d.name, "source": d.source, "description": d.description or "",
             "shadows": list(d.shadows)}
            for d in list_providers()
        ]
    render.OUT.def_list(kind, rows)


def do_preset_list(ctx: Ctx) -> None:
    from ..core.model.presets import describe_presets

    render.OUT.preset_list(describe_presets())


def do_provider_show(ctx: Ctx, name: str) -> None:
    from ..core.providers import find_provider, _describe, _help

    p = find_provider(name)  # raises ProviderError if missing / not executable
    render.OUT.provider_show({
        "name": p.name,
        "source": p.source,
        "path": str(p.exe),
        "description": _describe(p.exe),
        "help": _help(p.exe),
    })


# ---- dev harness -------------------------------------------------------------


def do_dev_build(ctx: Ctx) -> None:
    from ..core import paths

    if not PROXY_DIR.is_dir():
        fail(f"{PROXY_DIR} not found -- `dev` commands need the repo checkout")

    args = ["build", "-t", IMAGE_TAG]
    # Stamp the source digest so `start`/`doctor` can detect a checkout that has
    # drifted from the built image (a `git pull` that touched proxy/).
    digest = paths.proxy_src_digest()
    if digest:
        args += ["--label", f"{paths.SRC_DIGEST_LABEL}={digest}"]
    args += [str(PROXY_DIR)]
    core_docker.docker(args, stream=True)


def do_dev_test(ctx: Ctx, trailing: list[str], cli_only: bool = False,
                proxy_only: bool = False, force_container: bool = False) -> None:
    """Run the test suite(s).

    Default: run BOTH the host-side CLI tests (tests/cli/) and the proxy suite
    (tests/). The proxy suite runs ON-HOST when its runtime deps (mitmproxy,
    aiohttp, starlark) import there -- near-instant -- else inside the image via
    `docker run`. Trailing args after `--` pass through to the proxy pytest.

    --cli:       host CLI tests only (no docker required).
    --proxy:     proxy suite only.
    --container: force the proxy suite into the image even if the deps are
                 importable on the host (the canonical, version-pinned env).

    Overlay tests: each configured overlay (`CREDPROXY_OVERLAY_PATH`) with a
    `tests/` subdir is run as its OWN pytest invocation (its `test_*.py` module
    basenames would collide with the repo suite under one rootdir), using the
    same on-host-or-image fallback as the proxy suite. The full overlay chain is
    always mounted + on the resolution path so an overlay test can resolve
    injectors/scripts from any tier.
    """
    import importlib.util
    import subprocess
    from ..core.engine.imageenv import ImageEnv
    from ..core.paths import TESTS_DIR, REPO_ROOT, overlay_dirs

    run_cli = not proxy_only
    run_proxy = not cli_only

    cli_failed = False
    if run_cli:
        if importlib.util.find_spec("pytest") is None:
            # Graceful note rather than a raw "No module named pytest".
            msg = ("host pytest not found; skipping CLI tests "
                   "(install pytest to run tests/cli/)")
            if not run_proxy:
                fail(msg)
            say(msg)
        else:
            say("running host-side CLI tests (tests/cli/)...")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(REPO_ROOT / "tests" / "cli"), "-v"],
                check=False,
            )
            cli_failed = result.returncode != 0
            say("CLI tests FAILED" if cli_failed else "CLI tests passed.")
            if not run_proxy:
                sys.exit(result.returncode)

    # Every configured overlay, indexed (for container mount paths) + the subset
    # that ships a tests/ suite. The full chain is mounted even when only one
    # overlay's tests run, so resolution sees every tier (an overlay test may
    # resolve a definition another overlay shadows).
    all_overlays = list(enumerate(overlay_dirs()))                  # [(i, (label, dir))]
    overlay_suites = [(i, label, d) for i, (label, d) in all_overlays
                      if (d / "tests").is_dir()]

    # Proxy suite. tests/cli/ is excluded (host-only; its module names collide
    # with the proxy suite's under one rootdir). Prefer running it ON-HOST when
    # the proxy's runtime deps import there -- it skips the ~container-startup
    # tax that dominates the inner loop -- falling back to the image otherwise.
    proxy_deps = ("mitmproxy", "aiohttp", "starlark")
    missing = [m for m in proxy_deps if importlib.util.find_spec(m) is None]

    if not force_container and not missing:
        say("running proxy suite on-host (deps present; --container forces the image)...")
        env = {**os.environ, "PYTHONPATH": str(PROXY_DIR)}
        host_cmd = [sys.executable, "-m", "pytest", "-v", str(TESTS_DIR),
                    "--ignore", str(TESTS_DIR / "cli")] + trailing
        # Exec the proxy suite (preserves TTY) ONLY when it is the last thing to
        # run -- nothing before it failed and no overlay suites follow. Otherwise
        # run each suite as a subprocess and combine exit codes.
        if not cli_failed and not overlay_suites:
            os.execvpe(sys.executable, host_cmd, env)  # replace proc
        combined = cli_failed
        combined |= subprocess.run(host_cmd, check=False, env=env).returncode != 0
        # Overlay tests import `testkit` (proxy dir) + resolve the CLI package;
        # PYTHONPATH covers both. os.environ carries CREDPROXY_OVERLAY_PATH so
        # resolution sees the same chain we discovered above.
        ov_env = {**os.environ,
                  "PYTHONPATH": os.pathsep.join([str(PROXY_DIR), str(REPO_ROOT / "cli")])}
        for i, label, d in overlay_suites:
            say(f"running overlay tests: {label} ({d / 'tests'})...")
            ov_cmd = [sys.executable, "-m", "pytest", "-v", str(d / "tests")]
            combined |= subprocess.run(ov_cmd, check=False, env=ov_env).returncode != 0
        sys.exit(1 if combined else 0)

    if not force_container:
        say(f"proxy deps not importable on host ({', '.join(missing)}); running the "
            f"proxy suite in the image. Install them (see proxy/requirements.txt) "
            f"for the faster on-host path.")
    meta = ImageEnv.load()

    # Mounts + CREDPROXY_OVERLAY_PATH rewrite for the whole overlay chain: each
    # host overlay dir is bind-mounted read-only at /opt/overlays/<i> and the env
    # var is rewritten to those container paths (declared order preserved), so
    # resolution INSIDE the image sees the overlays rather than absent host paths.
    overlay_mounts: list[str] = []
    container_overlay_paths: list[str] = []
    for i, (label, d) in all_overlays:
        cpath = f"/opt/overlays/{i}"
        overlay_mounts += ["-v", f"{d}:{cpath}:ro"]
        container_overlay_paths.append(cpath)
    overlay_env = (["-e", "CREDPROXY_OVERLAY_PATH=" + ":".join(container_overlay_paths)]
                   if container_overlay_paths else [])

    def _docker_run(pytest_args: list[str], *, extra_env: list[str]) -> list[str]:
        return [
            "docker", "run", "--rm",
            "-v", f"{PROXY_DIR}:{meta.source}",
            "-v", f"{TESTS_DIR}:/opt/tests",
            # Read-only so the proxy suite can validate the CLI's builtin scripts
            # (the dogfood .star) against the Python built-ins -- single source of
            # truth, even though the proxy never reads cli/ at runtime.
            "-v", f"{REPO_ROOT / 'cli'}:/opt/cli:ro",
            *overlay_mounts,
            *extra_env,
            "-w", "/opt",
            "--entrypoint", "python",
            IMAGE_TAG,
            *pytest_args,
        ]

    proxy_cmd = _docker_run(
        ["-m", "pytest", "-v", "tests/", "--ignore=tests/cli", *trailing],
        extra_env=[],
    )
    # Overlay suites run under the rewritten CREDPROXY_OVERLAY_PATH; PYTHONPATH
    # covers the proxy dir (for `import testkit`) + the CLI package (testkit also
    # self-inserts /opt/cli, but be explicit since overlay tests carry no conftest).
    ov_extra_env = overlay_env + ["-e", f"PYTHONPATH={meta.source}:/opt/cli"]
    overlay_cmds = [
        (label, _docker_run(["-m", "pytest", "-v", f"/opt/overlays/{i}/tests"],
                            extra_env=ov_extra_env))
        for i, label, d in overlay_suites
    ]

    try:
        if not cli_failed and not overlay_cmds:
            # Proxy suite is the only thing to run: exec (preserves TTY).
            os.execvp("docker", proxy_cmd)
        combined = cli_failed
        combined |= subprocess.run(proxy_cmd, check=False).returncode != 0
        for label, ocmd in overlay_cmds:
            say(f"running overlay tests in image: {label}...")
            combined |= subprocess.run(ocmd, check=False).returncode != 0
        sys.exit(1 if combined else 0)
    except FileNotFoundError:
        # subprocess.run / execvp both raise this if `docker` isn't on PATH.
        raise DependencyError(core_docker.DOCKER_MISSING_MSG)


def do_dev_reload(ctx: Ctx, name: str | None) -> None:
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "dev reload")
    lifecycle.reload_proxy(ws)
    render.OUT.reloaded(ws.name)


# ---- argparse leaf parsers ---------------------------------------------------
#
# argparse handles each leaf command's flags. The dispatcher feeds it a
# normalized argv (canonicalized so name-before-verb and aliases collapse to a
# single internal form: `_ws <verb> [NAME] ...`).


def _binding_subparsers(parent: argparse._SubParsersAction) -> None:
    p = parent.add_parser("add")
    # Single-binding path. Coordinated multi-binding sets + guardrails are the
    # `preset` noun's job (`workspace NAME preset add PRESET`), not a flag here.
    p.add_argument("--injector", default=None)
    p.add_argument("--provider", default=None)
    # Repeatable: a single bare REF is single-slot; one or more `slot=ref`
    # values form a multi-slot secret table.
    p.add_argument("--secret", action="append", metavar="REF|SLOT=REF")
    # Repeatable. A literal hostname is matched exactly; a value containing `*`
    # is a glob (`*` spans dots), so `*.amazonaws.com` scopes one binding to
    # every AWS region/service endpoint. The two rightmost labels must be
    # literal (`*.example.com` ok; `*.com`/`*` rejected).
    p.add_argument("--host", action="append", metavar="HOST|GLOB")
    p.add_argument("--name", dest="binding_name", default=None)
    p.add_argument("--placeholder", default=None)
    # --env overrides the injector's suggested env; --no-env suppresses it
    # (writes `env = false`), so the placeholder is exposed under no env var.
    env_group = p.add_mutually_exclusive_group()
    env_group.add_argument("--env", default=None)
    env_group.add_argument("--no-env", action="store_true")

    p = parent.add_parser("remove")
    p.add_argument("binding_name", metavar="NAME")

    parent.add_parser("list")

    p = parent.add_parser("test")
    p.add_argument("binding_name", metavar="NAME", nargs="?", default=None)
    # Ad-hoc mode: test a definition before it is bound (no workspace needed).
    p.add_argument("--injector", default=None)
    p.add_argument("--provider", default=None)
    p.add_argument("--secret", action="append", metavar="REF|SLOT=REF")


def _rule_subparsers(parent: argparse._SubParsersAction) -> None:
    add = parent.add_parser("add")
    # The action is a SUBCOMMAND (`rule add block|respond|rewrite|script`), not a
    # `--action` flag, so each action's parser owns exactly its own params and
    # argparse rejects an out-of-action flag structurally -- no hand-rolled
    # rejection table. The scoping flags common to every action live on a shared
    # parent parser mixed into each via `parents=`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", action="append", metavar="HOST|GLOB", dest="host",
                        help="literal or glob (`*.amazonaws.com`); repeatable")
    common.add_argument("--method", action="append", metavar="METHOD", dest="method",
                        help="restrict to these methods (repeatable; default all)")
    common.add_argument("--path", default=None, metavar="GLOB",
                        help="path glob: * within a segment, ** across (e.g. /repos/**)")
    common.add_argument("--name", dest="rule_name", default=None,
                        help="rule name (auto-generated if omitted)")
    common.add_argument("--visible", dest="rule_visible", action="store_true",
                        default=None, help="force enumerated + attributed")
    common.add_argument("--hidden", dest="rule_visible", action="store_false",
                        default=None, help="force unenumerated + unattributed")
    asub = add.add_subparsers(dest="rule_action", required=True,
                              metavar="{block,respond,rewrite,script}")

    pb = asub.add_parser("block", parents=[common])
    pb.add_argument("--status", type=int, default=None, help="refuse status (default 403)")

    pr = asub.add_parser("respond", parents=[common])
    pr.add_argument("--status", type=int, default=None, help="response status (required)")
    pr.add_argument("--body", default=None, help="response body")
    pr.add_argument("--header", action="append", metavar="K=V", dest="header",
                    help="a response header (repeatable)")

    pw = asub.add_parser("rewrite", parents=[common])
    pw.add_argument("--header", action="append", metavar="K=V", dest="header",
                    help="set a request header (repeatable)")
    pw.add_argument("--remove-header", action="append", metavar="NAME",
                    dest="remove_header", help="remove a request header")
    pw.add_argument("--resp-header", action="append", metavar="K=V",
                    dest="resp_header", help="set a response header")
    pw.add_argument("--resp-remove-header", action="append", metavar="NAME",
                    dest="resp_remove_header", help="remove a response header")

    ps = asub.add_parser("script", parents=[common])
    ps.add_argument("--script", default=None, metavar="NAME",
                    help="the .star rule script name")

    p = parent.add_parser("remove")
    p.add_argument("rule_name", metavar="NAME")

    parent.add_parser("list")

    p = parent.add_parser("test")
    p.add_argument("method", metavar="METHOD")
    p.add_argument("url", metavar="URL")
    p.add_argument("--live", action="store_true", dest="rule_live",
                   help="ask the RUNNING proxy for the authoritative answer "
                        "(exact script phase) against its loaded config, instead "
                        "of the offline config-file dry-run")


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


class _LeafParser(argparse.ArgumentParser):
    """ArgumentParser whose `error` routes through the porcelain renderer, so a
    bad/unknown/missing arg serializes as a JSON error object under --json (and a
    clean `[credproxy] ` line otherwise) and exits non-zero -- instead of
    argparse's raw usage dump to stderr + SystemExit(2), which bypassed the
    renderer entirely. Sub-parsers inherit this class (argparse defaults
    parser_class to type(self)), so the whole verb tree is covered."""

    def error(self, message: str):  # noqa: D401 - argparse hook
        fail(f"{self.prog}: {message}")


def _build_leaf_parser() -> argparse.ArgumentParser:
    """Parser for the verb tail of a workspace-scoped command. The dispatcher
    has already stripped `workspace` and the workspace name; what remains is
    `<verb> [args]`. NAME is threaded separately (resolved by the dispatcher)."""
    parser = _LeafParser(prog="credproxy workspace", add_help=False)
    sub = parser.add_subparsers(dest="verb", required=True)

    p_enter = sub.add_parser("enter")
    # One-session override of the config `user` (e.g. `enter --user root` for a
    # debug shell in a non-root workspace) without editing the config file.
    p_enter.add_argument("--user", dest="enter_user", default=None)
    # Force a config re-push (re-resolve secrets) even if the proxy already has
    # the current config -- e.g. after rotating a secret in place. Default skips
    # the push when the proxy's config fingerprint already matches.
    p_enter.add_argument("--push", dest="enter_push", action="store_true")
    p_exec = sub.add_parser("exec")
    # Default sources the CA-trust env (like `enter -- CMD`). `--login` upgrades to
    # a full bash login shell (/etc/profile.d + rc, mise shims); `--raw` drops to a
    # direct execve (no shell, for minimal images) -- the two are exclusive.
    p_exec.add_argument("--login", dest="exec_login", action="store_true")
    p_exec.add_argument("--raw", dest="exec_raw", action="store_true")
    # One-off `-u` override, beating config `user` for this call (parity with enter).
    p_exec.add_argument("--user", dest="exec_user", default=None)
    p_exec.add_argument("--push", dest="exec_push", action="store_true")
    sub.add_parser("edit")
    sub.add_parser("start")
    sub.add_parser("stop")
    p_recreate = sub.add_parser("recreate")
    # Default rebuilds only the workspace container (keeps the running proxy +
    # its CA). `--proxy`/`--all` also recreates the proxy (full re-bootstrap).
    p_recreate.add_argument("--proxy", "--all", dest="recreate_proxy",
                            action="store_true")
    # Also wipe the named managed volume(s) (re-seeded from the image), e.g.
    # `--reset-volume home`. Repeatable. Destroys data, so it's gated like
    # delete; bind/overlay (host-path) mounts are untouched.
    p_recreate.add_argument("--reset-volume", dest="recreate_reset_volumes",
                            action="append", metavar="NAME", default=[])
    p_delete = sub.add_parser("delete")
    # Keep the workspace's managed volumes instead of wiping them (they become
    # orphans unless a same-named workspace is recreated).
    p_delete.add_argument("--keep-volumes", dest="delete_keep_volumes",
                          action="store_true")
    sub.add_parser("apply")
    p_push = sub.add_parser("push")
    # --wait polls the proxy's /health (never /ready) until capture-ready before
    # pushing -- for an attached proxy that Compose/CI just started.
    p_push.add_argument("--wait", dest="push_wait", action="store_true")
    p_push.add_argument("--timeout", dest="push_timeout", type=float, default=120.0,
                        metavar="SECS")
    p_resolve = sub.add_parser("resolve")
    # Exactly one of --json (global, blob to stdout) or --out FILE (mode 0600).
    p_resolve.add_argument("--out", dest="resolve_out", default=None, metavar="FILE")
    sub.add_parser("inspect")
    p_config = sub.add_parser("config")
    p_config.add_argument("--declared", action="store_true", dest="config_declared")
    p_logs = sub.add_parser("logs")
    # --audit filters the stream to the structured `[audit]` events (#24):
    # credential-use and rule-hit records, pretty-printed (or raw JSON with
    # --json). Without it, `logs` streams the full proxy log verbatim.
    p_logs.add_argument("--audit", action="store_true", dest="logs_audit")

    p_bind = sub.add_parser("bind-dir")
    # Defaults to the current directory; --dir associates a different one.
    p_bind.add_argument("--dir", dest="bind_directory", default=None, metavar="PATH")

    binding = sub.add_parser("binding")
    bsub = binding.add_subparsers(dest="bindingcmd", required=True)
    _binding_subparsers(bsub)

    mount = sub.add_parser("mount")
    msub = mount.add_subparsers(dest="mountcmd", required=True)
    _mount_subparsers(msub)

    rule = sub.add_parser("rule")
    rsub = rule.add_subparsers(dest="rulecmd", required=True)
    _rule_subparsers(rsub)

    preset = sub.add_parser("preset")
    psub = preset.add_subparsers(dest="presetcmd", required=True)
    pa = psub.add_parser("add")
    pa.add_argument("preset", metavar="PRESET")
    # Optional: a binding-bearing preset may carry a default provider/secret; a
    # pure-rule preset needs neither. Enforced (conditionally) in the handler.
    pa.add_argument("--provider", default=None)
    pa.add_argument("--secret", action="append", metavar="REF|SLOT=REF")
    # Pack `[[option]]` values, whole-field host-half parameters (#59). Repeatable
    # `--opt id=value`; unresolved required options prompt on loose+TTY, else fail
    # with the structured missing error.
    pa.add_argument("--opt", action="append", metavar="ID=VALUE", default=None)

    pr = psub.add_parser("refresh")
    # Optional PRESET: omitted -> every applied pack; named -> just that one.
    pr.add_argument("preset", metavar="PRESET", nargs="?", default=None)
    # Delete stamped blocks whose definition counterpart vanished (else reported
    # only). Gated like the destructive set on an implicit workspace.
    pr.add_argument("--prune", action="store_true")

    return parser


# ---- top-level dispatch ------------------------------------------------------


def _print_help(loose: bool = False) -> None:
    say(_LOOSE_HELP if loose else _STRICT_HELP)


_STRICT_HELP = (
    "credproxy -- workspace manager for the credential-injecting proxy.\n"
    "\n"
    "Strict surface: name every workspace explicitly, no default resolution,\n"
    "no prompts. The scriptable contract. `credp` is the human alias\n"
    "(`credproxy --loose`): default-workspace resolution, short aliases, and a\n"
    "confirmation gate on destructive actions -- run `credp --help` for that.\n"
    "\n"
    "Workspaces:\n"
    "  credproxy workspace create NAME\n"
    "  credproxy workspace list [FILTER]   (or: credproxy list [FILTER])\n"
    "  credproxy info                      (global config & state: paths, overlays, registries)\n"
    "  credproxy doctor [NAME] [--fetch]   (preflight: docker/image/config/bindings)\n"
    "  credproxy version                   (or: credproxy --version)\n"
    "  credproxy workspace NAME enter|edit|start|stop|recreate|delete|apply|inspect|logs\n"
    "  credproxy workspace NAME push [--wait]   (resolve + POST config to the proxy)\n"
    "  credproxy workspace NAME resolve --json|--out FILE   (build wire config, no proxy)\n"
    "  credproxy workspace create NAME --attach SELECTOR   (attached: externally-run containers)\n"
    "  credproxy push --admin URL --config FILE --token FILE   (stateless push; no workspace)\n"
    "  credproxy emit-compose [NAME] [--image TAG]   (Docker Compose proxy-sidecar fragment)\n"
    "  credproxy workspace NAME exec [--login|--raw] -- CMD...   (one-shot; scriptable)\n"
    "  credproxy workspace NAME bind-dir [--dir PATH]   (associate with a directory)\n"
    "  credproxy workspace NAME mount add --volume NAME --target PATH [--ro] [--preserve] [--user-owned]\n"
    "  credproxy workspace NAME binding add|remove|list|test ...\n"
    "  credproxy workspace NAME rule add|remove|list|test ...   (traffic guardrails)\n"
    "  credproxy workspace NAME preset add PRESET   (service pack: bindings + rules)\n"
    "  credproxy workspace NAME preset refresh [PRESET] [--prune]   (re-expand stamped packs)\n"
    "  credproxy workspace binding test --provider P --secret REF [--injector I]\n"
    "      (ad-hoc: test a definition before binding it; no workspace needed)\n"
    "Definitions:\n"
    "  credproxy injector scaffold NAME [--script] | list | check NAME | api\n"
    "  credproxy provider scaffold NAME | provider list | show NAME\n"
    "  credproxy script check [NAME]       (compile .star scripts before push)\n"
    "  credproxy preset list               (service setup packs: bindings + guardrails)\n"
    "Dev harness:\n"
    "  credproxy dev build|test|reload\n"
    "\n"
    "Global flags: --loose (human surface; use the `credp` alias), --json,\n"
    "  --yes (bypass confirmation)."
)


_LOOSE_HELP = (
    "credp -- human surface for credproxy (credproxy --loose).\n"
    "\n"
    "An omitted workspace resolves to the current default (announced on\n"
    "stderr); destructive actions on the default workspace ask first.\n"
    "\n"
    "Workspaces (omit NAME to resolve by current directory, then the default):\n"
    "  credp use NAME                  set the default workspace\n"
    "  credp current                   show the workspace a bare command targets\n"
    "  credp create [NAME] [--here]    (NAME derived from the dir if omitted)\n"
    "  credp bind-dir [NAME] [--dir PATH]  associate a workspace with a directory\n"
    "  credp list [FILTER]\n"
    "  credp info                      global config & state (paths, overlays, registries)\n"
    "  credp enter|edit|start|stop|recreate|delete|apply|inspect|logs [NAME]\n"
    "  credp push [NAME] [--wait]      resolve + POST config to the workspace's proxy\n"
    "  credp resolve [NAME] --json|--out FILE   build wire config (no proxy)\n"
    "  credp create [NAME] --attach SELECTOR    attached workspace (externally-run containers)\n"
    "  credp emit-compose [NAME] [--image TAG]  Docker Compose proxy-sidecar fragment\n"
    "  credp mount add --volume NAME --target PATH [--ro] [--preserve] [--user-owned]\n"
    "  credp binding add|remove|list|test ...   (acts on the default workspace)\n"
    "  credp rule add|remove|list|test ...      (traffic guardrails)\n"
    "  credp preset add PRESET                  (service pack: bindings + rules)\n"
    "  credp preset refresh [PRESET] [--prune]  (re-expand stamped packs)\n"
    "  credp binding test --provider P --secret REF [--injector I]\n"
    "      (ad-hoc: test a definition before binding it; no workspace needed)\n"
    "Definitions:\n"
    "  credp injector scaffold NAME [--script] | list | check NAME | api\n"
    "  credp provider scaffold NAME | provider list | show NAME\n"
    "  credp script check [NAME]       (compile .star scripts before push)\n"
    "  credp preset list               (service setup packs: bindings + guardrails)\n"
    "Dev harness:\n"
    "  credp dev build|test|reload\n"
    "\n"
    "The canonical `credproxy workspace NAME <verb>` forms work too and are the\n"
    "scriptable contract. Global flags: --json, --yes (bypass confirmation)."
)


# Per-command help. The leaf parsers are deliberately `add_help=False` (we don't
# want raw argparse usage spew), so `--help` is honored by the hand-rolled
# dispatch via these prose blocks instead.
_BINDING_ADD_HELP = (
    "credproxy workspace NAME binding add --injector INJ -- bind a credential\n"
    "into requests for one or more hosts. (Coordinated multi-binding sets +\n"
    "guardrails are the `preset` noun: `workspace NAME preset add PRESET`.)\n"
    "\n"
    "  --injector INJ    how the credential is shaped into the request (bearer,\n"
    "                    basic, body, sigv4, ...). See `credproxy injector list`.\n"
    "  --provider PROV   where the value comes from. Required.\n"
    "                    See `credproxy provider list`.\n"
    "  --secret REF      the reference the provider resolves. For the `env`\n"
    "                    provider REF is the host env var NAME (not the value).\n"
    "                    Repeat as SLOT=REF for a multi-slot secret.\n"
    "  --host HOST       host this binding applies to; repeatable. Required.\n"
    "  --name NAME       binding name (auto: <injector>-<provider>[-N]).\n"
    "  --placeholder PH  inert sentinel swapped for the real value at egress\n"
    "                    (auto-generated for substitute schemes).\n"
    "  --env VAR         env var name exposed to the workspace via /setup and\n"
    "                    /exports.sh (defaults to the injector's suggested env).\n"
    "  --no-env          suppress the injector's suggested env (writes\n"
    "                    `env = false`); mutually exclusive with --env.\n"
)

_PRESET_ADD_HELP = (
    "credproxy workspace NAME preset add PRESET -- apply a service setup pack:\n"
    "stamp its `[[binding]]` set, its `[[rule]]` guardrails, AND its container\n"
    "half (`[[mount]]`/`[env]`/`[[setup]]`) into the workspace, all-or-nothing.\n"
    "`credproxy preset list` shows every pack.\n"
    "\n"
    "  --provider PROV   where binding values come from (a binding-bearing preset\n"
    "                    may supply a default; a rule/container-only pack needs none).\n"
    "  --secret REF      the reference the provider resolves (see `binding add`);\n"
    "                    may be defaulted by a preset for its default provider.\n"
    "\n"
    "Expansion, not a link: it writes ordinary blocks/config (names\n"
    "`<preset>-<suffix>`); edit/remove afterward is normal. A binding/rule on a\n"
    "host with no prior binding flips it to TLS-intercepted; the container half\n"
    "changes the workspace spec (restart to apply if the container exists) --\n"
    "`preset add` announces both. An attached workspace refuses a container-half\n"
    "pack. Re-adding the same pack is refused (provenance guard).\n"
    "\n"
    "credproxy workspace NAME preset refresh [PRESET] [--prune] -- re-expand\n"
    "stamped pack(s) against their CURRENT definitions and update the TOML. No\n"
    "PRESET refreshes every applied pack. Per block: unchanged -> up to date;\n"
    "definition changed but block unedited -> updated cleanly (new marker); block\n"
    "hand-edited since stamping -> skipped with a diff (never overwritten); a\n"
    "definition-new block -> added; a vanished block -> reported, and removed\n"
    "only with --prune. The shared placeholder + provider/secret are preserved,\n"
    "never regenerated. All-or-nothing; no live link (an operator-clock refresh).\n"
)

_BINDING_TEST_HELP = (
    "credproxy workspace NAME binding test [BINDING] -- dry-run resolve binding\n"
    "secrets via their providers. Reports ok/length per binding (never the\n"
    "secret value); exits 1 if any fail.\n"
    "\n"
    "  BINDING                       test only this binding (default: all).\n"
    "  --provider P --secret REF [--injector I]\n"
    "                                ad-hoc: test a definition before it is\n"
    "                                bound (no workspace needed).\n"
)

_MOUNT_ADD_HELP = (
    "credproxy workspace NAME mount add -- add a managed-volume mount to the\n"
    "workspace (a named, persistent, per-workspace Docker volume).\n"
    "\n"
    "  --volume NAME   the volume's name (letters/digits/_.-). `home` writes the\n"
    "                  `home = ...` sugar instead of a mounts entry.\n"
    "  --target PATH   absolute path the volume mounts at inside the workspace.\n"
    "  --ro            mount read-only.\n"
    "  --preserve      seed the new volume with the CURRENT container's data at\n"
    "                  --target before applying. A mount can't attach to a live\n"
    "                  container, so this stops + recreates the workspace (which\n"
    "                  carries the data across); without it the volume starts\n"
    "                  empty/image-seeded and the change is deferred to `start`.\n"
    "                  On a running workspace with live `enter` sessions it asks\n"
    "                  first (loose) / needs --yes (strict), since they're killed.\n"
    "  --user-owned    chown the volume to the workspace `user` (after setup) so a\n"
    "                  non-root user can write it -- needed when it mounts at a path\n"
    "                  the image doesn't populate (else root-owned). Requires a\n"
    "                  non-root `user`; not valid for the `home` sugar.\n"
)

_RULE_ADD_HELP = (
    "credproxy workspace NAME rule add ACTION --host HOST [--host HOST...] ...\n"
    "  Govern traffic on an intercepted host (no credential involved). ACTION is a\n"
    "  SUBCOMMAND -- one of block|respond|rewrite|script (not a flag). Adding a rule\n"
    "  to a passthrough host makes it TLS-intercepted (workspace must bootstrap CA).\n"
    "\n"
    "  Scoping (every action):\n"
    "    --host HOST|GLOB   literal or glob (`*.amazonaws.com`); repeatable, required\n"
    "    --method METHOD    restrict to these methods (repeatable; default all)\n"
    "    --path GLOB        path glob: `*` within a segment, `**` across\n"
    "                       (e.g. /repos/**); default all paths\n"
    "    --name NAME        rule name (auto-generated if omitted)\n"
    "    --visible/--hidden override the per-family enumeration+attribution default\n"
    "                       (block/respond visible; rewrite/script hidden)\n"
    "\n"
    "  Per action (each owns exactly these flags):\n"
    "    block    [--status N]                              refuse (default 403)\n"
    "    respond  --status N [--body TEXT] [--header K=V...] stub a response\n"
    "    rewrite  [--header K=V] [--remove-header NAME]      request headers\n"
    "             [--resp-header K=V] [--resp-remove-header NAME]  response headers\n"
    "    script   --script NAME                             a .star rule script\n"
    "\n"
    "  Examples:\n"
    "    rule add block --host api.github.com --method DELETE --path '/repos/**'\n"
    "    rule add respond --host api.openai.com --path /v1/models --status 200 --body '{}'\n"
    "    rule add script --host api.github.com --path '/users/**' --script scrub-emails\n"
    "\n"
    "  A visible block self-identifies (X-Credproxy-Rule header + a credproxy JSON\n"
    "  body); a hidden block is a bare status. Hidden rules are excluded from\n"
    "  /setup but ALWAYS logged/audited for the operator. See docs/reference/rules.md."
)

_CREATE_HELP = (
    "credproxy workspace create NAME -- scaffold a workspace config file and\n"
    "auth token (does not start anything). The scaffold sets a concrete image;\n"
    "edit the generated <name>.toml to change it (its comments show what to\n"
    "adjust), or override the template in an overlay (see docs/advanced/overlays.md).\n"
    "\n"
    "  --here        associate the workspace with the current directory, so a\n"
    "                loose `credp <verb>` run from at/under it resolves here.\n"
    "  --dir PATH    associate with PATH instead of the current directory.\n"
    "  --attach SEL  scaffold an ATTACHED workspace (containers managed externally;\n"
    "                credproxy only pushes credentials). SEL is compose-project=P |\n"
    "                container=X | admin-url=URL | discover=k=v[,k=v]. --here/--dir\n"
    "                still apply. Use `push` (not start/enter) on it.\n"
    "\n"
    "On the loose surface (`credp`), NAME may be omitted with --here/--dir: it is\n"
    "derived from the directory's basename (sanitized to the name charset, and\n"
    "deduped with a numeric suffix). Strict `credproxy` always requires NAME.\n"
)


_PUSH_STATELESS_HELP = (
    "credproxy push --admin URL --config FILE --token FILE [--wait] [--timeout SECS]\n"
    "  Stateless escape hatch: push a config to an arbitrary proxy with no\n"
    "  workspace and no state (for Compose/devcontainers/CI).\n"
    "\n"
    "  --admin URL    the proxy's admin base URL. MUST be loopback (127.0.0.0/8 or\n"
    "                 localhost) -- the wire carries resolved secrets over plain HTTP.\n"
    "  --config FILE  a workspace-TOML SUBSET: only [[binding]] + [[rule]] (any\n"
    "                 container/attach key is rejected). Build one with `resolve`.\n"
    "  --token FILE   file holding the proxy's bearer token.\n"
    "  --wait         poll /health until capture-ready before pushing.\n"
    "  --timeout SECS --wait timeout (default 120).\n"
    "\n"
    "To push a workspace instead, use `credproxy workspace NAME push`."
)


def _wants_help(argv: list[str]) -> bool:
    """True if argv contains a help flag. Used by the hand-rolled dispatch to
    honor `-h`/`--help` on subcommands (the leaf argparse parsers suppress it)."""
    return any(t in ("-h", "--help") for t in argv)


def _scaffold_help(kind: str) -> str:
    s = (
        f"credproxy {kind} scaffold NAME -- copy the builtin {kind} template "
        f"into\nyour registry as NAME, to author from. NAME must not start "
        f"with '-'."
    )
    if kind == "injector":
        s += (
            "\n\n--script [sign|substitute]  instead emit a SCRIPTED (custom) "
            "injector\n  (a manifest + a .star with the primitive-API reference "
            "inline) -- use\n  this when no built-in scheme fits. Pick the family:\n"
            "    sign        compute auth material on every request (e.g. an HMAC\n"
            "                signature); no placeholder. [default]\n"
            "    substitute  swap an inert placeholder the workspace holds for the\n"
            "                real secret value.\n"
            "  See `injector api` for the full reference; check it with "
            "`injector check NAME`."
        )
    if kind == "provider":
        s += (
            "\n\n--lang [python|sh]  template language (default python; "
            "sh = POSIX shell + jq).\n\n"
            "A provider is ANY executable -- a script in any language, or a "
            "compiled\nbinary -- that speaks the JSON stdin/stdout protocol "
            "(docs/reference/providers.md);\nit can also be a directory with an executable "
            "`run`."
        )
    s += f"\nThen `credproxy {kind} list` shows it."
    return s


# What each workspace-scoped verb does, surfaced on `... NAME <verb> --help`.
# Kept terse but descriptive: the blind-agent rounds showed a bare `usage:`
# line for the lifecycle verbs read as "is this command even doing anything?".
_VERB_HELP = {
    "enter": (
        "credproxy workspace NAME enter [--user USER] [--push] [-- CMD...] -- open\n"
        "a shell (default bash, or run CMD) in the workspace, starting it if needed.\n"
        "  --user USER   run as USER for this session (overrides config `user`).\n"
        "  --push        force a config re-push (re-resolve secrets) even if the\n"
        "                proxy already has the current config -- e.g. after\n"
        "                rotating a secret in place. Default skips the redundant push."
    ),
    "exec": (
        "credproxy workspace NAME exec [--login|--raw] [--user U] [--push] -- CMD...\n"
        "-- run a one-shot command in the workspace (starting it if needed) and\n"
        "propagate its exit code. The non-interactive sibling of `enter`: it never\n"
        "INITIATES an auto-stop, so it's safe to fire many times from a script/agent,\n"
        "and it takes a fast path when the workspace is already running.\n"
        "  (default) source the CA-trust env, like `enter -- CMD` (so `exec -- curl\n"
        "           https://…` works against the proxy). Honours `enter_prelude`.\n"
        "  --raw    direct execve: no shell wrapper, no CA-trust env (minimal images).\n"
        "  --login  a bash login shell (/etc/profile.d + rc, mise shims); needs bash.\n"
        "  --user U run as U for this call (overrides config `user`).\n"
        "  --push   force a config re-push (as `enter --push`); also the full start.\n"
        "(`--json` does not apply -- exec streams the command's output verbatim.)"
    ),
    "start": (
        "credproxy workspace NAME start -- (re)start the proxy, wait for health,\n"
        "push the resolved bindings, then (re)start the workspace. Creates the\n"
        "containers if missing; recreates one whose spec (image/mounts/env/...)\n"
        "has drifted. Safe to re-run."
    ),
    "stop": (
        "credproxy workspace NAME stop -- stop both containers (kept, not removed).\n"
        "Config and state survive; a later `start`/`enter` resumes."
    ),
    "recreate": (
        "credproxy workspace NAME recreate [--proxy] [--reset-volume NAME ...] --\n"
        "rebuild the workspace container from a clean slate (re-runs setup), then\n"
        "start it. Keeps all managed volumes, config, auth token, and state -- only\n"
        "the container is replaced (unlike `delete`). `--proxy` (alias `--all`) also\n"
        "recreates the proxy container, regenerating its CA (full re-bootstrap).\n"
        "`--reset-volume NAME` (repeatable) ALSO wipes that managed volume,\n"
        "re-seeded from the image (e.g. `--reset-volume home`) -- bind/overlay\n"
        "host-path mounts are untouched, and config/token/state survive. It\n"
        "destroys data, so on the loose surface it prompts for an implicit default\n"
        "workspace (pass --yes to bypass)."
    ),
    "delete": (
        "credproxy workspace NAME delete [--keep-volumes] -- remove both\n"
        "containers, the workspace's managed volumes, the config file, and the\n"
        "state dir. `--keep-volumes` preserves the volumes (they orphan unless a\n"
        "same-named workspace is recreated). Not reversible. (On the loose surface,\n"
        "deleting the default workspace prompts first.)"
    ),
    "apply": (
        "credproxy workspace NAME apply -- reconcile a running workspace with its\n"
        "config: binding changes are re-pushed live; container-spec changes\n"
        "(image/home/mounts/env/setup) are deferred with a `start` hint. Reports\n"
        "what was applied vs deferred. On an ATTACHED workspace, `apply` == `push`."
    ),
    "push": (
        "credproxy workspace NAME push [--wait] [--timeout SECS] -- resolve every\n"
        "binding's secret and POST the full wire config (bindings + rules) to the\n"
        "workspace's proxy. For a managed workspace this targets its running\n"
        "proxy's published port (start it first); for an ATTACHED workspace it\n"
        "resolves the `attach` selector (compose/container/discover/admin_url).\n"
        "  --wait          poll the proxy's /health until capture-ready first\n"
        "                  (never /ready -- that gates on this very push).\n"
        "  --timeout SECS  --wait timeout (default 120)."
    ),
    "resolve": (
        "credproxy workspace NAME resolve (--json | --out FILE) -- build the full\n"
        "wire config (with RESOLVED secret values) WITHOUT contacting any proxy.\n"
        "  --json      write the config blob to stdout.\n"
        "  --out FILE  write it to FILE, mode 0600 (it holds real secrets; a path\n"
        "              outside the workspace state dir warns). Exactly one required.\n"
        "Feeds the stateless `credproxy push --config FILE`."
    ),
    "inspect": (
        "credproxy workspace NAME inspect -- show config, running state, host port,\n"
        "binding summary, and any drift against what was last applied."
    ),
    "config": (
        "credproxy workspace NAME config [--declared] -- dump the container-side\n"
        "config. Default `effective`: every field with its in-effect value, all\n"
        "defaults filled (so you see what applies even when it's not in the file).\n"
        "`--declared` shows only what's literally in the .toml. `--json` for both."
    ),
    "edit": (
        "credproxy workspace NAME edit -- open the config in $VISUAL/$EDITOR\n"
        "(default vi), then validate it. Sugar over editing the .toml directly;\n"
        "hints `apply`/`start` afterward."
    ),
    "logs": (
        "credproxy workspace NAME logs -- follow the proxy container's logs\n"
        "(docker logs -f)."
    ),
    "bind-dir": (
        "credproxy workspace NAME bind-dir [--dir PATH] -- associate the workspace\n"
        "with a host directory (default: the current directory), so a loose\n"
        "`credp <verb>` run from at or under it resolves to NAME. Sugar over\n"
        "editing the `directory` field in the .toml (the source of truth)."
    ),
}


def _verb_help(verb_argv: list[str]) -> str:
    """Contextual help for a workspace-scoped verb (`--help` on the leaf)."""
    verb = verb_argv[0] if verb_argv else ""
    if verb == "binding":
        sub = verb_argv[1] if len(verb_argv) > 1 and not verb_argv[1].startswith("-") else ""
        if sub == "add":
            return _BINDING_ADD_HELP
        if sub == "test":
            return _BINDING_TEST_HELP
        return ("credproxy workspace NAME binding {add|remove|list|test} ...\n"
                "Run `binding add --help` or `binding test --help` for details.")
    if verb == "mount":
        sub = verb_argv[1] if len(verb_argv) > 1 and not verb_argv[1].startswith("-") else ""
        if sub == "add":
            return _MOUNT_ADD_HELP
        return ("credproxy workspace NAME mount add ...\n"
                "Run `mount add --help` for details.")
    if verb == "rule":
        sub = verb_argv[1] if len(verb_argv) > 1 and not verb_argv[1].startswith("-") else ""
        if sub == "add":
            return _RULE_ADD_HELP
        return ("credproxy workspace NAME rule {add|remove|list|test} ...\n"
                "Rules govern traffic on intercepted hosts (block/respond/rewrite/\n"
                "script), credential-free. `rule test METHOD URL` dry-runs the\n"
                "matcher. Run `rule add --help` for details.")
    if verb == "preset":
        return _PRESET_ADD_HELP
    if verb in _VERB_HELP:
        return _VERB_HELP[verb]
    return f"usage: credproxy workspace NAME {verb}"


def _split_trailing(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split off a `-- CMD...` tail (for `enter` and `dev test`)."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _pop_global_flags(argv: list[str]) -> tuple[list[str], bool, bool, bool]:
    """Pull the order-independent global flags (--loose/--json/--yes) out of
    argv wherever they appear, returning the remainder and the flag values."""
    loose = as_json = assume_yes = False
    rest = []
    for tok in argv:
        if tok == "--loose":
            loose = True
        elif tok == "--json":
            as_json = True
        elif tok in ("--yes", "-y"):
            assume_yes = True
        else:
            rest.append(tok)
    return rest, loose, as_json, assume_yes


def _dispatch_workspace(ctx: Ctx, rest: list[str], trailing: list[str]) -> None:
    """Hand-rolled router for the `workspace` noun.

    `rest` is everything after `workspace`. Peek the first token:
      - a workspace-level verb (`create`/`use`/`list`) -> handle directly;
      - a workspace-scoped verb (`enter`/.../`binding`) with NO name -> in
        loose mode resolve the default; in strict mode this is an error;
      - otherwise the first token is a workspace NAME and the next is the
        scoped verb.
    """
    if not rest:
        fail("usage: credproxy workspace {create|use|list|NAME <verb>}")

    head = rest[0]

    if head == "create":
        if _wants_help(rest):
            say(_CREATE_HELP)
            return
        a = _parse_create(rest[1:])
        do_create(ctx, a.name, _create_dir(a), a.attach)
        return
    if head == "use":
        # `use` mutates the loose default-workspace pointer, so it's loose-only
        # (strict names every workspace explicitly). Its reader, `current`, is
        # loose-only too -- both sides of the default concept live on loose.
        if not ctx.loose:
            fail("`workspace use` sets the loose default-workspace pointer; it is "
                 "loose-only -- use the `credp` alias (or `credproxy --loose`). "
                 "The strict surface names every workspace explicitly.")
        if len(rest) != 2:
            fail("usage: credp workspace use NAME")
        do_use(ctx, rest[1])
        return
    if head == "list":
        if len(rest) > 2:
            fail("usage: credproxy workspace list [FILTER]")
        do_list(ctx, rest[1] if len(rest) > 1 else None)
        return

    if head in _WS_VERBS:
        # Verb with no explicit name -> default resolution (loose) / error.
        _run_ws_verb(ctx, None, rest, trailing)
        return

    # Otherwise head is a workspace name; rest[1:] is `<verb> ...`.
    name = head
    if len(rest) < 2:
        fail(f"usage: credproxy workspace {name} <verb>")
    _run_ws_verb(ctx, name, rest[1:], trailing)


def _run_ws_verb(
    ctx: Ctx, name: str | None, verb_argv: list[str], trailing: list[str]
) -> None:
    """Parse and run a workspace-scoped verb. `verb_argv` starts with the
    verb. `name` is the (possibly None) explicit workspace name."""
    if _wants_help(verb_argv):
        say(_verb_help(verb_argv))
        return
    a = _build_leaf_parser().parse_args(verb_argv)
    verb = a.verb
    if verb == "enter":
        do_enter(ctx, name, trailing, a.enter_user, a.enter_push)
    elif verb == "exec":
        do_exec(ctx, name, trailing, login=a.exec_login, raw=a.exec_raw,
                push=a.exec_push, user=a.exec_user)
    elif verb == "edit":
        do_edit(ctx, name)
    elif verb == "start":
        do_start(ctx, name)
    elif verb == "stop":
        do_stop(ctx, name)
    elif verb == "delete":
        do_delete(ctx, name, a.delete_keep_volumes)
    elif verb == "apply":
        do_apply(ctx, name)
    elif verb == "push":
        do_push(ctx, name, a.push_wait, a.push_timeout)
    elif verb == "resolve":
        do_resolve(ctx, name, a.resolve_out)
    elif verb == "recreate":
        do_recreate(ctx, name, a.recreate_proxy, a.recreate_reset_volumes)
    elif verb == "inspect":
        do_inspect(ctx, name)
    elif verb == "config":
        do_config(ctx, name, a.config_declared)
    elif verb == "logs":
        do_logs(ctx, name, getattr(a, "logs_audit", False))
    elif verb == "bind-dir":
        do_bind_dir(ctx, name, a.bind_directory)
    elif verb == "binding":
        bc = a.bindingcmd
        if bc == "add":
            do_binding_add(ctx, name, a)
        elif bc == "remove":
            do_binding_remove(ctx, name, a)
        elif bc == "list":
            do_binding_list(ctx, name)
        elif bc == "test":
            do_binding_test(ctx, name, a)
    elif verb == "mount":
        if a.mountcmd == "add":
            do_mount_add(ctx, name, a)
    elif verb == "rule":
        rc = a.rulecmd
        if rc == "add":
            do_rule_add(ctx, name, a)
        elif rc == "remove":
            do_rule_remove(ctx, name, a)
        elif rc == "list":
            do_rule_list(ctx, name)
        elif rc == "test":
            do_rule_test(ctx, name, a)
    elif verb == "preset":
        if a.presetcmd == "add":
            do_preset_add(ctx, name, a)
        elif a.presetcmd == "refresh":
            do_preset_refresh(ctx, name, a)


def _parse_create(argv: list[str]) -> argparse.Namespace:
    p = _LeafParser(prog="credproxy workspace create", add_help=False)
    # Optional: on the loose surface, an omitted NAME is derived from the
    # --here/--dir directory basename (strict still requires it). See do_create.
    p.add_argument("name", nargs="?", default=None)
    # Associate the new workspace with a host directory, so `credp <verb>` run
    # from at/under it resolves here. --here uses the current directory.
    p.add_argument("--here", action="store_true")
    p.add_argument("--dir", dest="directory", default=None, metavar="PATH")
    # Scaffold an ATTACHED workspace (containers managed externally). SELECTOR is
    # compose-project=P | container=X | admin-url=U | discover=k=v[,k=v].
    p.add_argument("--attach", dest="attach", default=None, metavar="SELECTOR")
    return p.parse_args(argv)


# CLI attach selector (dashed) -> the TOML `attach` key (underscored).
_ATTACH_FLAG_KEYS = {
    "compose-project": "compose_project", "container": "container",
    "admin-url": "admin_url", "discover": "discover",
}


def _attach_selector_from_flag(spec: str) -> dict:
    """Parse a `--attach SELECTOR` value (`compose-project=P` | `container=X` |
    `admin-url=U` | `discover=k=v[,k=v]`) into a normalized-shape `{key: value}`
    selector, validated by the same config-side attach validator (loopback for
    admin-url, discover syntax) so `create --attach` and `load_config` agree."""
    from ..core.model import config as core_config

    key, sep, val = spec.partition("=")
    if not sep or not key or not val:
        fail("--attach must be SELECTOR=VALUE, e.g. --attach compose-project=myproj "
             "(or container=NAME, admin-url=URL, discover=label=value)")
    mapped = _ATTACH_FLAG_KEYS.get(key)
    if mapped is None:
        fail(f"unknown attach selector '{key}' (one of "
             f"{', '.join(sorted(_ATTACH_FLAG_KEYS))})")
    selector = {mapped: val}
    core_config._parse_attach(selector, "create --attach")   # validate (raises)
    return selector


def _create_dir(a: argparse.Namespace) -> str | None:
    """Resolve the directory association from `create` flags (--here/--dir)."""
    if a.here and a.directory is not None:
        fail("give --here or --dir, not both")
    if a.here:
        return os.getcwd()
    if a.directory is not None:
        return os.path.abspath(os.path.expanduser(a.directory))
    return None


# ---- loose aliases -----------------------------------------------------------
#
# In loose mode, short top-level verbs resolve to canonical commands with NO
# independent behavior. They simply translate to the workspace dispatcher.

_ALIAS_TO_WS_VERB = {
    "enter", "exec", "edit", "start", "stop", "recreate", "delete", "apply",
    "inspect", "config", "logs", "binding", "bind-dir", "mount", "rule",
    "resolve",
}


def _dispatch_alias(ctx: Ctx, head: str, rest: list[str], trailing: list[str]) -> None:
    """Loose-only top-level aliases. `head` is the alias verb already consumed;
    `rest` is what follows."""
    if head == "use":
        if len(rest) != 1:
            fail("usage: credp use NAME")
        do_use(ctx, rest[0])
        return
    if head == "create":
        if _wants_help(rest):
            say(_CREATE_HELP)
            return
        a = _parse_create(rest)
        do_create(ctx, a.name, _create_dir(a), a.attach)
        return
    if head == "list":
        do_list(ctx, rest[0] if rest else None)
        return

    if head == "binding":
        # `credp binding <subcmd> ... [NAME]` -> resolve default workspace.
        # NAME is never given on the alias (the alias assumes the default);
        # an explicit workspace uses the canonical `workspace NAME binding`.
        _run_ws_verb(ctx, None, ["binding", *rest], trailing)
        return

    if head == "mount":
        # Like `binding`: the sub-noun's subcommand (`add`) would otherwise be
        # eaten as a NAME by the generic alias path below. The alias always acts
        # on the resolved default workspace; explicit names use the canonical
        # `workspace NAME mount` form.
        _run_ws_verb(ctx, None, ["mount", *rest], trailing)
        return

    if head == "rule":
        # Same as `binding`/`mount`: the sub-noun's subcommand would be eaten as
        # a NAME by the generic path. Always acts on the resolved default
        # workspace; explicit names use `workspace NAME rule`.
        _run_ws_verb(ctx, None, ["rule", *rest], trailing)
        return

    if head in _ALIAS_TO_WS_VERB:
        # A leading non-flag token overrides the default workspace
        # (`credp enter myproj`); flags are forwarded to the verb parser
        # (`credp enter --user root`, optionally `credp enter myproj --user root`).
        name = None
        verb_args = list(rest)
        if verb_args and not verb_args[0].startswith("-"):
            name = verb_args.pop(0)
        _run_ws_verb(ctx, name, [head, *verb_args], trailing)
        return

    fail(f"unknown command '{head}'")


# ---- main --------------------------------------------------------------------


def main_loose() -> None:
    """Console-script entry point for the loose surface (`credp`), equivalent to
    `credproxy --loose`. The strict surface uses `main` directly. (The `bin/`
    shims call `main(loose_default=...)` and stay the no-install path.)"""
    main(loose_default=True)


def main(loose_default: bool = False) -> None:
    argv = sys.argv[1:]

    argv, trailing = _split_trailing(argv)
    argv, loose, as_json, assume_yes = _pop_global_flags(argv)
    loose = loose or loose_default

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help(loose)
        sys.exit(0)

    if argv[0] in ("version", "--version"):
        import json
        from .. import __version__
        print(json.dumps({"credproxy": __version__}) if as_json
              else f"credproxy {__version__}")
        sys.exit(0)

    render.set_format(as_json)
    ctx = Ctx(loose=loose, as_json=as_json, assume_yes=assume_yes)

    head, rest = argv[0], argv[1:]

    try:
        if head == "workspace":
            _dispatch_workspace(ctx, rest, trailing)
        elif head in _META_COMMANDS:
            _dispatch_meta(ctx, head, rest)
        elif head in ("injector", "provider"):
            _dispatch_def(ctx, head, rest)
        elif head == "preset":
            _dispatch_preset(ctx, rest)
        elif head == "script":
            _dispatch_script(ctx, rest)
        elif head == "dev":
            _dispatch_dev(ctx, rest, trailing)
        elif head == "push":
            _dispatch_push(ctx, rest, trailing)
        elif head == "emit-compose":
            _dispatch_emit_compose(ctx, rest)
        elif loose:
            # Loose surface: top-level aliases.
            _dispatch_alias(ctx, head, rest, trailing)
        else:
            fail(
                f"unknown command '{head}' "
                f"(strict surface; see `credproxy --help`)"
            )
    except CredproxyError as e:
        fail(e)


def _dispatch_meta(ctx: Ctx, head: str, rest: list[str]) -> None:
    """Top-level meta commands (no workspace argument)."""
    if head == "list":
        if len(rest) > 1:
            fail("usage: credproxy list [FILTER]")
        do_list(ctx, rest[0] if rest else None)
    elif head == "current":
        # `current` reports the loose default/cwd-resolved target -- an implicit-
        # targeting concept the strict surface disclaims (like its writer, `use`).
        if not ctx.loose:
            fail("`current` reports the loose default/cwd-resolved workspace; it "
                 "is loose-only -- use the `credp` alias (or `credproxy --loose`). "
                 "The strict surface names every workspace explicitly.")
        if rest:
            fail("usage: credp current (takes no arguments)")
        do_current(ctx)
    elif head == "info":
        if rest:
            fail("usage: credproxy info (takes no arguments)")
        do_info(ctx)
    elif head == "doctor":
        fetch = "--fetch" in rest
        positional = [a for a in rest if not a.startswith("-")]
        unknown = [a for a in rest if a.startswith("-") and a != "--fetch"]
        if unknown or len(positional) > 1:
            fail("usage: credproxy doctor [NAME] [--fetch]")
        do_doctor(ctx, positional[0] if positional else None, fetch)


def do_doctor(ctx: Ctx, name: str | None, fetch: bool) -> None:
    """Environment preflight + config validation. Reports ALL failures; exits
    non-zero iff any check fails. NAME limits to one workspace (default: all)."""
    from ..core.engine import doctor
    # A bare read-only scan-all is fine (matches `list`), but `--fetch` resolves
    # secrets -- which can prompt / unlock a vault -- so refuse to fan that out
    # across every workspace from one nameless command; require an explicit NAME.
    if fetch and name is None:
        fail("`doctor --fetch` needs a workspace NAME (it resolves secrets, which "
             "can prompt or unlock a vault -- refusing to fan that out across every "
             "workspace); run `doctor` with no NAME for a read-only scan of all")
    checks = doctor.run(name, fetch=fetch)
    render.OUT.doctor([{"id": c.id, "ok": c.ok, "message": c.message,
                        "hint": c.hint} for c in checks])
    if any(not c.ok for c in checks):
        sys.exit(1)


def _dispatch_def(ctx: Ctx, kind: str, rest: list[str]) -> None:
    sub = rest[0] if rest else None

    if sub == "scaffold":
        args = rest[1:]
        # Honor --help BEFORE treating the next token as a NAME -- otherwise
        # `scaffold --help` would scaffold a file literally named '--help'.
        if _wants_help(args):
            say(_scaffold_help(kind))
            return
        name, script_mode, family, lang = _parse_scaffold_args(kind, args)
        if script_mode:
            if kind != "injector":
                fail("--script is only valid for `injector scaffold`")
            do_scaffold_script(ctx, name, family)
        else:
            do_scaffold(ctx, kind, name, lang)
        return

    if sub == "check" and kind == "injector":
        args = rest[1:]
        if _wants_help(args) or not args:
            say("usage: credproxy injector check NAME [--compile]\n"
                "Validate a scripted injector host-side (manifest parses and the\n"
                "named .star resolves); --compile additionally compiles the .star\n"
                "in the proxy image (needs docker + the built image).")
            return
        names = [a for a in args if not a.startswith("-")]
        flags = [a for a in args if a.startswith("-")]
        bad = [f for f in flags if f != "--compile"]
        if bad or len(names) != 1:
            fail("usage: credproxy injector check NAME [--compile]")
        do_injector_check(ctx, names[0], "--compile" in flags)
        return

    if sub == "api" and kind == "injector":
        if _wants_help(rest[1:]):
            say("usage: credproxy injector api\n"
                "Print the scripted-injector authoring reference (manifest fields\n"
                "+ the Starlark primitive API) without scaffolding anything.")
            return
        do_injector_api(ctx)
        return

    if sub == "show" and kind == "provider":
        args = rest[1:]
        names = [a for a in args if not a.startswith("-")]
        if _wants_help(args) or len(names) != 1:
            say("usage: credproxy provider show NAME\n"
                "Show a provider's source, resolved path, description, and help.")
            return
        do_provider_show(ctx, names[0])
        return

    if sub == "list":
        do_def_list(ctx, kind)
        return

    usage = (
        "usage: credproxy injector {scaffold NAME [--script [sign|substitute]]"
        "|list|check NAME|api}"
        if kind == "injector"
        else "usage: credproxy provider {scaffold NAME|list|show NAME}"
    )
    if _wants_help(rest):
        say(usage)
        return
    if not rest:
        fail(usage)
    fail(f"unknown {kind} command '{rest[0]}'")


def _parse_scaffold_args(kind: str, args: list[str]) -> tuple[str, bool, str, str]:
    """Parse `scaffold` args: a NAME plus optional `--script [sign|substitute]`
    (injector) or `--lang python|sh` (provider)."""
    name: str | None = None
    script_mode = False
    family = "sign"
    lang = "python"
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--script":
            script_mode = True
            if i + 1 < len(args) and args[i + 1] in ("sign", "substitute"):
                family = args[i + 1]
                i += 1
        elif tok == "--lang":
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                fail("--lang needs a value (python or sh)")
            lang = args[i + 1]
            i += 1
        elif tok.startswith("-"):
            fail(f"unknown flag {tok!r}; usage: credproxy {kind} scaffold NAME "
                 f"[--script [sign|substitute]] [--lang python|sh]")
        elif name is None:
            name = tok
        else:
            fail(f"usage: credproxy {kind} scaffold NAME")
        i += 1
    if name is None:
        fail(f"usage: credproxy {kind} scaffold NAME")
    return name, script_mode, family, lang


def do_scaffold_script(ctx: Ctx, name: str, family: str) -> None:
    from ..core.model.scaffold import scaffold_script

    r = scaffold_script(name, family)
    render.OUT.scaffolded_script(
        r.name, str(r.injector_path), str(r.script_path), r.family)


def do_injector_api(ctx: Ctx) -> None:
    from ..core.model.scaffold import script_api_reference

    render.OUT.injector_api(script_api_reference())


def do_injector_check(ctx: Ctx, name: str, do_compile: bool) -> None:
    from ..core.model.injectors import find_injector
    from ..core.model.scripts import find_script

    inj = find_injector(name)  # parses + validates the manifest (raises if bad)
    if inj.scheme != "script":
        render.OUT.injector_check(name, {
            "scheme": inj.scheme, "scripted": False, "ok": True,
            "detail": f"built-in scheme '{inj.scheme}'; nothing to compile"})
        return
    script = find_script(inj.script)  # raises InjectorError if missing
    detail = (f"manifest ok (family={inj.spec.family}, "
              f"slots={list(inj.spec.slots)}); script '{inj.script}' "
              f"resolves ({script.source_origin})")
    if not do_compile:
        render.OUT.injector_check(name, {
            "scheme": "script", "scripted": True, "ok": True,
            "compiled": False, "detail": detail})
        return
    err = _compile_script_in_image(script.source)
    render.OUT.injector_check(name, {
        "scheme": "script", "scripted": True, "ok": err is None,
        "compiled": True, "detail": detail, "compile_error": err})
    if err is not None:
        sys.exit(1)


def _compile_script_in_image(source: str) -> str | None:
    """Compile a `.star` in the proxy image (which carries the Starlark runtime),
    so the host needs no starlark dep. Returns None on success, else the error
    text. Mirrors what the proxy does at push time. Needs docker + the image."""
    import os
    import subprocess
    import tempfile

    pycode = (
        "import sys\n"
        "from starlark_runtime import ScriptedScheme\n"
        "src = open('/work/check.star').read()\n"
        "try:\n"
        "    ScriptedScheme(name='check', source=src, filename='check.star')\n"
        "except Exception as e:\n"
        "    print('%s: %s' % (type(e).__name__, e)); sys.exit(1)\n"
        "print('ok')\n"
    )
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o755)
        p = os.path.join(d, "check.star")
        with open(p, "w") as f:
            f.write(source)
        os.chmod(p, 0o644)
        cmd = ["docker", "run", "--rm", "-v", f"{d}:/work:ro"]
        # Prefer the live proxy source when the repo is checked out (parity with
        # `dev test`), so a `dev build`-stale image doesn't give wrong verdicts;
        # otherwise the baked image's runtime is the contract.
        if PROXY_DIR.is_dir():
            cmd += ["-v", f"{PROXY_DIR}:/opt/proxy:ro"]
        cmd += ["-w", "/opt/proxy", "--entrypoint", "python", IMAGE_TAG,
                "-c", pycode]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            fail("`injector check --compile` needs docker (not found on PATH)")
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        return None
    if "Unable to find image" in out or "No such image" in out:
        fail(f"proxy image '{IMAGE_TAG}' not found; build it with "
             f"`credproxy dev build`")
    return out or f"compile failed (exit {r.returncode})"


def _dispatch_push(ctx: Ctx, rest: list[str], trailing: list[str]) -> None:
    """The top-level `push`: two forms, distinguished by `--admin`.

    - STATELESS escape hatch: `credproxy push --admin URL --config FILE --token
      FILE [--wait] [--timeout SECS]` -- no workspace, no state (both surfaces).
    - the loose workspace-push alias: `credp push [NAME]` -> the default/cwd
      workspace's `push`. Strict has no bare `push NAME` form (it names workspaces
      explicitly via `workspace NAME push`)."""
    if "--admin" in rest or "--config" in rest or "--token" in rest:
        if _wants_help(rest):
            say(_PUSH_STATELESS_HELP)
            return
        do_push_stateless(ctx, rest)
        return
    if _wants_help(rest):
        say("credproxy push -- push config to a proxy.\n"
            "  credproxy workspace NAME push [--wait] [--timeout SECS]\n"
            "  credproxy push --admin URL --config FILE --token FILE  (stateless)\n"
            "  credp push [NAME]                                       (loose alias)")
        return
    if not ctx.loose:
        fail("`credproxy push` is the stateless escape hatch and needs "
             "--admin URL --config FILE --token FILE; to push a workspace use "
             "`credproxy workspace NAME push`")
    name = None
    verb_args = list(rest)
    if verb_args and not verb_args[0].startswith("-"):
        name = verb_args.pop(0)
    _run_ws_verb(ctx, name, ["push", *verb_args], trailing)


def do_push_stateless(ctx: Ctx, rest: list[str]) -> None:
    """Push a `[[binding]]+[[rule]]` config FILE to an arbitrary loopback proxy
    admin URL, authed with a token FILE -- no workspace, no state. The CI/scripting
    escape hatch."""
    from ..core.engine import push as core_push
    from ..core.model.rules import combined_fingerprint

    p = _LeafParser(prog="credproxy push", add_help=False)
    p.add_argument("--admin", dest="admin", required=True, metavar="URL")
    p.add_argument("--config", dest="config_file", required=True, metavar="FILE")
    p.add_argument("--token", dest="token_file", required=True, metavar="FILE")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=120.0, metavar="SECS")
    a = p.parse_args(rest)

    from ..core.model.attach import normalize_admin_url, require_loopback
    admin_url = normalize_admin_url(a.admin)
    require_loopback(admin_url)                               # I8
    bindings, rules = core_push.load_stateless_config(a.config_file)
    token = _read_token_file(a.token_file)
    if a.wait:
        core_push.wait_for_health(admin_url, a.timeout, say)
    fp = combined_fingerprint(bindings, rules)
    with core_push.target_push_lock(admin_url):
        say("pushing config...")
        core_push.push_to_target(admin_url, token, bindings, rules, fp, notify=say)
    render.OUT.pushed(None, admin_url, attached=None, stateless=True)


def _read_token_file(path: str) -> str:
    from pathlib import Path
    p = Path(os.path.expanduser(path))
    if not p.exists():
        fail(f"--token file not found: {path}")
    token = p.read_text().strip()
    if not token:
        fail(f"--token file is empty: {path}")
    return token


_EMIT_COMPOSE_HELP = (
    "credproxy emit-compose [NAME] [--image TAG] -- print a Docker Compose\n"
    "fragment for a credproxy proxy sidecar to stdout (the one Compose-aware\n"
    "command; everything else is parent-agnostic). Ports and mount paths come\n"
    "from the proxy image's ENV contract, so they can't go stale.\n"
    "\n"
    "  NAME          bake this workspace's real token path (<state>/auth.token)\n"
    "                into the bind mount -- the workspace must exist (pairs with\n"
    "                `create --attach`). Omit NAME to emit a\n"
    "                ${CREDPROXY_STATE:?...}/auth.token reference a Compose .env\n"
    "                can interpolate.\n"
    "  --image TAG   proxy image for `docker inspect` AND the emitted `image:`\n"
    "                line (default the built-in proxy image tag).\n"
    "\n"
    "The healthcheck gates on /ready (creds-ready), not /health, so a Compose\n"
    "`service_healthy` dependency opens only once credentials are pushed. Push\n"
    "after `up`: `credproxy workspace NAME push` (or the stateless `credproxy\n"
    "push`). (`--json` does not apply -- it emits YAML.)"
)


def _dispatch_emit_compose(ctx: Ctx, rest: list[str]) -> None:
    """Top-level `emit-compose [NAME] [--image TAG]`: a Compose fragment to
    stdout. Available on both surfaces (it names no implicit workspace)."""
    if _wants_help(rest):
        say(_EMIT_COMPOSE_HELP)
        return
    # YAML is not credproxy's to structure, so --json has nothing to wrap --
    # refuse it rather than emit non-JSON on a --json call (mirrors `exec`).
    if ctx.json:
        fail("emit-compose prints a YAML Compose fragment; `--json` does not "
             "apply")
    p = _LeafParser(prog="credproxy emit-compose", add_help=False)
    p.add_argument("name", nargs="?", default=None)
    p.add_argument("--image", dest="image", default=None, metavar="TAG")
    a = p.parse_args(rest)
    do_emit_compose(ctx, a.name, a.image)


def do_emit_compose(ctx: Ctx, name: str | None, image: str | None) -> None:
    """Build + print the Compose fragment. With NAME, bake the workspace's real
    token path (the workspace must exist); without NAME, emit a
    ${CREDPROXY_STATE:?...}/auth.token reference plus a comment on where that dir
    lives. Every port/path is read from the image's ENV contract via ImageEnv."""
    from ..core.engine import compose as core_compose
    from ..core.engine.imageenv import ImageEnv
    from ..core.paths import IMAGE_TAG

    image_tag = image or IMAGE_TAG
    meta = ImageEnv.load(image_tag)
    if name is not None:
        ws = for_name(name)
        _require_exists(ws)
        token_source = str(ws.token_path)
        note = None
    else:
        # No workspace resolved: leave the token path to a Compose `.env`.
        # `:?` makes Compose fail loudly if CREDPROXY_STATE is unset rather than
        # silently bind-mounting an empty path.
        token_source = "${CREDPROXY_STATE:?set to the workspace state dir}/auth.token"
        note = ("# CREDPROXY_STATE is the workspace state dir "
                "($XDG_STATE_HOME/credproxy/workspaces/NAME; "
                "`credproxy workspace NAME inspect` shows it).")
    print(core_compose.emit_compose(meta, image_tag, token_source, note))


def do_script_check(ctx: Ctx, name: str | None, force_container: bool) -> None:
    """Compile the named script (or every resolvable script) in the proxy runtime
    and report per-script results. Exit 0 iff all pass."""
    from ..core.engine import scriptcheck

    results = scriptcheck.run(name, force_container=force_container)
    if not results:
        say(f"no script '{name}' found" if name else "no scripts to check")
    render.OUT.script_check([
        {"name": r.name, "origin": r.origin, "ok": r.ok, "error": r.error,
         "profiles": list(r.profiles)}
        for r in results
    ])
    if any(not r.ok for r in results):
        sys.exit(1)


def _dispatch_script(ctx: Ctx, rest: list[str]) -> None:
    """`script` definition commands. Today just `check [NAME]` -- compile scripts
    before push (sibling of `injector list`/`provider list`/`preset list`)."""
    usage = ("usage: credproxy script check [NAME] [--container]\n"
             "Compile resolvable .star scripts in the proxy runtime (on-host when\n"
             "the Starlark deps import, else in the image). A script referenced by\n"
             "a scripted-injector manifest is compiled under the injector profile\n"
             "paired with that manifest; an unreferenced script is tried under both\n"
             "the injector and rule profiles. Exit 0 iff all compile.")
    if not rest or _wants_help(rest):
        say(usage)
        return
    if rest[0] != "check":
        fail(f"unknown script command '{rest[0]}' ({usage})")
    args = rest[1:]
    names = [a for a in args if not a.startswith("-")]
    flags = [a for a in args if a.startswith("-")]
    force_container = "--container" in flags or "--docker" in flags
    bad = [f for f in flags if f not in ("--container", "--docker")]
    if bad or len(names) > 1:
        fail(usage)
    do_script_check(ctx, names[0] if names else None, force_container)


def _dispatch_preset(ctx: Ctx, rest: list[str]) -> None:
    # `preset` is dual-role: `list` is definitional (no workspace, both surfaces);
    # `add` is workspace-scoped (it stamps into a workspace TOML). A bare `preset`
    # or `--help` lists, since the listing IS the documentation.
    if not rest or _wants_help(rest) or rest[0] == "list":
        do_preset_list(ctx)
        return
    if rest[0] in ("add", "refresh"):
        # Top-level `preset add`/`refresh` is the loose implicit-workspace form;
        # strict requires the explicit `workspace NAME preset ...`.
        if not ctx.loose:
            fail(f"`preset {rest[0]}` needs a workspace: "
                 f"`credproxy workspace NAME preset {rest[0]}`")
        _run_ws_verb(ctx, None, ["preset", *rest], [])
        return
    fail(f"unknown preset command '{rest[0]}' (usage: credproxy preset list  |  "
         f"credproxy workspace NAME preset add|refresh ...)")


def _dispatch_dev(ctx: Ctx, rest: list[str], trailing: list[str]) -> None:
    usage = "usage: credproxy dev {build|test|reload}"
    if _wants_help(rest):
        say(usage)
        return
    if not rest:
        fail(usage)
    sub = rest[0]
    if sub == "build":
        do_dev_build(ctx)
    elif sub == "test":
        test_args = rest[1:]  # selection flags (pytest args go after `--`)
        _known = ("--cli", "--proxy", "--container", "--docker")
        unknown = [a for a in test_args if a not in _known]
        if unknown:
            fail(f"dev test: unknown flag(s) {' '.join(unknown)} "
                 f"(use --cli/--proxy/--container; pytest args go after `--`)")
        cli_only = "--cli" in test_args
        proxy_only = "--proxy" in test_args
        force_container = "--container" in test_args or "--docker" in test_args
        if cli_only and proxy_only:
            # Both = "neither" under the old logic, yet the proxy path still ran.
            fail("dev test: --cli and --proxy are mutually exclusive "
                 "(omit both to run both suites)")
        do_dev_test(ctx, trailing, cli_only=cli_only, proxy_only=proxy_only,
                    force_container=force_container)
    elif sub == "reload":
        do_dev_reload(ctx, rest[1] if len(rest) > 1 else None)
    else:
        fail(f"unknown dev command '{sub}'")
