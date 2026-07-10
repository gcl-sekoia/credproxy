"""The `workspace` noun's non-lifecycle verbs: create/use/current/list/info/
bind-dir/edit/delete-adjacent inspect+config, plus the create-time template-preset
expansion and the `create` argv helpers."""
from __future__ import annotations

import argparse
import os
import tomllib

from ..core.model import dirmatch
from ..core.engine import containers, sessions
from ..core.model import pointer
from ..core.model import workspace as core_workspace
from ..core.errors import CredproxyError
from ..core.model.workspace import Workspace, for_name
from . import render
from .render import fail, say
from .common import (
    Ctx, _resolve_ws, _require_exists, _confirm_destructive, _is_default,
    _LeafParser,
)
from .cmd_preset import (
    _PRESET_BLOCK_RE, _PRESET_CHILD_RE, _PRESET_REF_RE,
    _expansion_summary, _newly_intercepted, _render_preset_ref_block,
    _resolve_preset_option_values, _resolve_preset_credential_interactive,
)


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
    # Resolve any template-declared `[[preset]]` references (#57, config-v2) IN
    # MEMORY -- prompt/default the credential + options and rewrite each block with
    # explicit values; the references SURVIVE into the stamped config (the
    # expansion is minted into the lock on the first resolve). All-or-nothing: any
    # failure raises/exits here, before a single byte is written.
    final_text, preset_announces, prereq_inputs = _expand_template_presets(
        ctx, base_text, source)
    if attach is not None:
        containers.create_attached_workspace_files(ws, selector, text=final_text)
    else:
        containers.create_workspace_files(ws, text=final_text)
    # Host-prerequisite checks (#58) run only AFTER the atomic write succeeds, so
    # a `fetch=true` provider check never execs a provider for a create that then
    # aborts. Advisory -- the reference is durable, host state is fixable afterward.
    if prereq_inputs:
        from ..core.model import prereqs
        for announce, (spec, provider, secret) in zip(preset_announces,
                                                       prereq_inputs):
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


def _rewrite_template_preset_blocks(text: str, blocks: list[str]) -> str:
    """Replace each `[[preset]]` block in `text` (in file order) with the
    canonical reference `blocks[i]` (resolved provider/secret/options written
    explicitly), via the same span machinery `remove_binding` uses. Folds one
    preceding blank separator into each replacement's leading blank so spacing
    stays tidy. `blocks` is 1:1 with the `[[preset]]` headers in `text`."""
    from ..core.model.bindings import _block_spans

    lines = text.splitlines(keepends=True)
    spans = _block_spans(text, _PRESET_BLOCK_RE, _PRESET_CHILD_RE)
    for (start, end), block in reversed(list(zip(spans, blocks))):
        if start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [block]
    return "".join(lines)


def _expand_template_presets(ctx: Ctx, base_text: str, source: str):
    """Resolve template-declared `[[preset]]` references (#57, config-v2) at
    `create` time: prompt/default the credential + options NOW and rewrite each
    surviving `[[preset]]` block with the resolved values written explicitly (the
    expansion itself lands in the lock on the first resolve, never in the TOML).
    Returns `(final_text, announces, prereq_inputs)` -- `announces` is the
    per-entry render metadata (built from an in-memory expansion, display only)
    and `prereq_inputs` the `(literal_spec, provider, secret)` tuples the caller
    runs `[[requires]]` host-prereq checks against.

    All-or-nothing: everything is composed + validated in memory (`validate_text`)
    and every failure raises/exits before the caller writes, so a bad entry leaves
    no partial config."""
    from ..core.errors import PresetTemplateError
    from ..core.model.bindings import _block_spans
    from ..core.model.presets import (
        apply_option_values, build_preset, expansion_to_lock, get_preset,
        parse_template_presets,
    )
    from ..core.model import prereqs
    from ..core.model.resolver import validate_text
    from ..core.providers import find_provider

    try:
        raw = tomllib.loads(base_text)
    except tomllib.TOMLDecodeError as e:
        if _PRESET_REF_RE.search(base_text):
            fail(f"{source}: template is malformed TOML ({e}); its preset "
                 f"entries can't be expanded -- fix the template's `[[preset]]` "
                 f"syntax")
        return base_text, [], []

    if "preset" not in raw:
        return base_text, [], []

    entries = parse_template_presets(raw, source)   # ConfigError on a bad entry

    # Require the documented `[[preset]]` block form (the rewrite only touches
    # `[[preset]]` HEADER blocks; an inline `preset = [...]` would survive).
    n_blocks = len(_block_spans(base_text, _PRESET_BLOCK_RE))
    if n_blocks != len(entries):
        fail(f"{source}: declare template presets as `[[preset]]` blocks, not an "
             f"inline `preset = [...]` array/table")
    if not entries:
        fail(f"{source}: the `preset` key is empty; remove it, or declare "
             f"template presets as `[[preset]]` blocks")

    from ..core.model.presets import PresetRef

    announces: list[dict] = []
    ref_blocks: list[str] = []
    prereq_inputs: list[tuple] = []      # (literal_spec, provider, secret) per entry
    accum_hosts: list[str] = []
    for entry in entries:
        spec = get_preset(entry.name)               # CredproxyError on unknown pack
        option_values = _resolve_preset_option_values(
            ctx, spec, dict(entry.options or {}))
        provider = secret = None
        if spec.needs_credential:
            def _missing(missing, _entry=entry):
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
        ref_blocks.append(
            _render_preset_ref_block(entry.name, provider, secret, option_values))

        # In-memory expansion for the announce (display only; the lock is minted on
        # the first resolve). `disable`/`override` aren't template-expressible, so a
        # bare PresetRef suffices.
        exp = build_preset(entry.name, provider, secret, options=option_values)
        expansion = expansion_to_lock(
            exp, PresetRef(entry.name, provider, secret, option_values, (), {}))
        summary = _expansion_summary(entry.name, {"expansion": expansion})
        # `create` writes NO lock, so the shared placeholder above is a discarded
        # sentinel -- the real one is minted at the first persisting resolve. Omit
        # it from the announce so `--json` never reports a value that won't exist.
        for b in summary["bindings"]:
            b.pop("placeholder", None)
        exp_hosts = [h for b in summary["bindings"] for h in b["hosts"]] \
            + [h for r in summary["rules"] for h in r["hosts"]]
        summary["newly_intercepted"] = _newly_intercepted(accum_hosts, exp_hosts)
        accum_hosts += exp_hosts
        summary["requires"] = []
        announces.append(summary)
        literal_spec = apply_option_values(spec, option_values) \
            if spec.options else spec
        prereq_inputs.append((literal_spec, provider, secret))

    final_text = _rewrite_template_preset_blocks(base_text, ref_blocks)

    # All-or-nothing: the composed config must resolve (references expand,
    # collisions clean) before the caller writes a single byte.
    validate_text(final_text, source)
    return final_text, announces, prereq_inputs


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
        containers.delete_workspace(ws, keep_volumes=keep_volumes, containers=False)
    else:
        containers.delete_workspace(ws, keep_volumes=keep_volumes)
    if was_default:
        pointer.clear_default()
    render.OUT.deleted(ws.name)


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
    for s in containers.list_workspaces():
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
        # Effective view: fold in any `[[preset]]` container half (config-v2) via
        # the resolver, then fill exec-time defaults.
        from ..core.model.resolver import resolve_workspace
        cfg = sessions.effective_config(
            resolve_workspace(ws, check_bind_exists=True).config)
    render.OUT.config({
        "mode": "declared" if declared else "effective",
        "config_path": str(ws.config_path),
        "config": cfg,
    })


def do_inspect(ctx: Ctx, name: str | None) -> None:
    ws = _resolve_ws(ctx, name)
    data = containers.inspect_workspace(ws)
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
        # Live drift against the running proxy (what it is ACTUALLY holding), or
        # null when the proxy is unreachable (the offline drift stands alone).
        # verdict + the DISPLAY-only lossy projection (what the proxy is running).
        # The verdict is decided by the offline content-complete drift, never the
        # projection (which omits secret/provider/params/rule-details).
        "live": None if data.live is None else {
            "verdict": data.live.verdict,
            "in_sync": data.live.in_sync,
            "generation": data.live.generation,
            "applied_generation": data.live.applied_generation,
            "projection": data.live.projection,
        },
        # Context for drift label: stopped workspace means bindings in
        # the lock's `applied.bindings` were "last applied" not "live".
        "_running": data.running,
    })


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


def _create_dir(a: argparse.Namespace) -> str | None:
    """Resolve the directory association from `create` flags (--here/--dir)."""
    if a.here and a.directory is not None:
        fail("give --here or --dir, not both")
    if a.here:
        return os.getcwd()
    if a.directory is not None:
        return os.path.abspath(os.path.expanduser(a.directory))
    return None
