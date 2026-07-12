"""The `rule` noun: add/remove/list/test handlers, the K=V parser, the rule-row
projection, and the argparse subparser builder for the rule verb tree."""
from __future__ import annotations

import argparse

from ..core.engine import containers
from ..core.errors import CredproxyError
from ..core.model.workspace import Workspace
from . import render
from .render import fail, say
from .common import Ctx, _resolve_ws, _require_exists, _confirm_destructive


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

    admin_url = containers.resolve_admin_url(ws, notify=say)
    result = core_push.rule_test(admin_url, read_token(ws), a.method, a.url)
    render.OUT.rule_test_live(a.method.upper(), a.url, result)


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
