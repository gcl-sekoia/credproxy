"""The `postgres` noun: add/remove/list/test handlers + the argparse subparser
builder for the pg-binding verb tree. Mirrors cmd_rule.py / cmd_binding.py."""
from __future__ import annotations

import argparse

from ..core.errors import CredproxyError
from . import render
from .render import fail
from .common import Ctx, _resolve_ws, _require_exists, _confirm_destructive


def _parse_secret_args(values: list[str] | None) -> dict[str, str]:
    """Parse repeated `--secret SLOT=REF` into a slot->ref dict. A pg credential
    is always (username, password), so a bare `REF` (no SLOT=) is rejected with a
    prescriptive fix -- unlike a single-slot binding, there is no default slot."""
    if not values:
        fail("`postgres add` needs --secret username=REF and --secret password=REF")
    out: dict[str, str] = {}
    for v in values:
        slot, sep, ref = v.partition("=")
        if not sep or not slot or not ref:
            fail(f"--secret '{v}' must be SLOT=REF "
                 f"(e.g. --secret username=db/user --secret password=db/pass)")
        if slot in out:
            fail(f"--secret slot '{slot}' given twice")
        out[slot] = ref
    return out


def _pg_row(pg) -> dict:
    """Operator-facing pg-binding summary for rendering (secret shown as refs,
    never resolved values)."""
    from ..core.model.bindings import secret_display
    return {
        "name": pg.name,
        "host": pg.host,
        "port": pg.port,
        "dbname": pg.dbname,
        "sslmode": pg.sslmode,
        "sslrootcert": pg.sslrootcert,
        "provider": pg.provider,
        "secret": secret_display(pg.secret),
        "env": pg.env,
    }


def do_postgres_add(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    from dataclasses import replace as _replace

    from ..core.model import postgres as core_pg

    if not a.host:
        fail("`postgres add` needs --host")
    if not a.dbname:
        fail("`postgres add` needs --dbname")
    if not a.provider:
        fail("`postgres add` needs --provider")

    entry: dict = {
        "host": a.host,
        "dbname": a.dbname,
        "provider": a.provider,
        "secret": _parse_secret_args(a.secret),
    }
    if a.port is not None:
        entry["port"] = a.port
    if a.sslmode is not None:
        entry["sslmode"] = a.sslmode
    if a.sslrootcert is not None:
        entry["sslrootcert"] = a.sslrootcert
    if a.env is not None:
        entry["env"] = a.env
    if a.pg_name:
        entry["name"] = a.pg_name

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)

    with ws.lock():                          # atomic read-validate-write
        existing = core_pg.load_postgres(ws)
        taken = {p.name for p in existing}
        pg = core_pg._parse_postgres_entry(entry, str(ws.config_path), "postgres add")
        if pg.name is None:
            pg = _replace(pg, name=core_pg._auto_name(pg, taken))
        if pg.name in taken:
            fail(f"pg binding name '{pg.name}' already exists in "
                 f"workspace '{ws.name}'")
        core_pg.validate(existing + [pg], str(ws.config_path))

        # Snapshot before the append: resolve_workspace runs full config-plane
        # validation, so a pre-existing unrelated error would raise after the block
        # is on disk. Restore on failure (mirrors do_rule_add).
        original = ws.config_path.read_text()
        core_pg.append_postgres(ws, pg)
        from ..core.model.lock import save_lock
        from ..core.model.resolver import resolve_workspace
        from ..core.paths import atomic_write_text
        try:
            resolved = resolve_workspace(ws)
        except CredproxyError as e:
            atomic_write_text(ws.config_path, original)  # never half-write
            fail(f"pg binding not added: workspace '{ws.name}' config has a "
                 f"pre-existing error: {e}")
        if resolved.lock_dirty:
            save_lock(ws, resolved.lock)

    from ..core.model import config as core_config
    render.OUT.postgres_added(pg.name, ws.name, _pg_row(pg),
                              attached=core_config.quick_attach(ws))


def do_postgres_remove(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    from ..core.model import postgres as core_pg

    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _confirm_destructive(ctx, ws, implicit, "remove pg binding from")
    with ws.lock():
        core_pg.remove_postgres(ws, a.pg_name)
    render.OUT.postgres_removed(a.pg_name, ws.name)


def do_postgres_list(ctx: Ctx, name: str | None) -> None:
    from ..core.model.resolver import resolve_workspace

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    pgs = resolve_workspace(ws).postgres
    render.OUT.postgres_list(ws.name, [_pg_row(p) for p in pgs])


def do_postgres_test(ctx: Ctx, name: str | None, a: argparse.Namespace) -> None:
    """Resolve-only dry run (the default): fetch each pg binding's username +
    password from its provider and report the value lengths (never the values).
    Exit 1 if any fetch fails. A full server-leg handshake is `doctor NAME
    --fetch`'s job (it can reach the real DB)."""
    from ..core.model.resolver import resolve_workspace
    from ..core.providers import fetch_many as provider_fetch_many

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    pgs = resolve_workspace(ws).postgres
    if a.pg_name:
        pgs = [p for p in pgs if p.name == a.pg_name]
        if not pgs:
            fail(f"no pg binding '{a.pg_name}' in workspace '{ws.name}'")

    results: list[dict] = []
    any_fail = False
    for p in pgs:
        row: dict = {"name": p.name, "provider": p.provider}
        try:
            refs = list(p.secret.values())
            values = provider_fetch_many(p.provider, refs)
            row["ok"] = True
            row["slots"] = {slot: len(values[ref]) for slot, ref in p.secret.items()}
        except CredproxyError as e:
            row["ok"] = False
            row["error"] = str(e)
            any_fail = True
        results.append(row)
    render.OUT.postgres_test(results)
    if any_fail:
        raise SystemExit(1)


def _postgres_subparsers(parent: argparse._SubParsersAction) -> None:
    add = parent.add_parser("add")
    add.add_argument("--provider", default=None,
                     help="provider that resolves the username + password")
    add.add_argument("--secret", action="append", metavar="SLOT=REF",
                     help="username=REF and password=REF (both required)")
    add.add_argument("--host", default=None, help="real upstream database host")
    add.add_argument("--port", type=int, default=None, help="upstream port (default 5432)")
    add.add_argument("--dbname", default=None, help="database name to connect to")
    add.add_argument("--sslmode", default=None,
                     help="server-leg TLS policy (default verify-full)")
    add.add_argument("--sslrootcert", default=None, metavar="PATH",
                     help="private-CA bundle for the server leg")
    add.add_argument("--name", dest="pg_name", default=None,
                     help="pg binding name (auto-generated if omitted)")
    add.add_argument("--env", default=None, metavar="VAR",
                     help="export the DSN under this env var (e.g. DATABASE_URL)")

    p = parent.add_parser("remove")
    p.add_argument("pg_name", metavar="NAME")

    parent.add_parser("list")

    p = parent.add_parser("test")
    p.add_argument("pg_name", metavar="NAME", nargs="?", default=None,
                   help="test just this pg binding (default: all)")
