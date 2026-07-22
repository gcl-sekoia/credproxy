"""The `postgres` noun: workspace-owned PostgreSQL broker upstreams.

A *pg binding* is the sibling of a `[[binding]]` for databases. Postgres is not
HTTP, so it never rides mitmproxy: the workspace dials the proxy's dedicated
broker at ``proxy.local:5432`` and the broker re-originates the connection to the
real database with an injected credential (see proxy/pgbroker.py). It lives as a
`[[postgres]]` table in the workspace TOML and carries NO placeholder, no
scheme/injector, and no host globs -- it is a plain named upstream:

    [[postgres]]
    name     = "analytics"
    host     = "db.internal"      # real upstream host
    port     = 5432                # optional (default 5432)
    dbname   = "warehouse"
    sslmode  = "verify-full"       # optional (default; server-leg TLS policy)
    provider = "vault"
    secret   = { username = "db/analytics#user", password = "db/analytics#pass" }
    env      = "DATABASE_URL"      # optional; DSN target for /exports.sh

This module mirrors core/model/rules.py: parse + validate the `[[postgres]]`
array (`name` REQUIRED, hand-authored), append/remove a whole named block (reusing
the array-depth-aware block machinery in bindings.py), and map resolved pg
bindings onto the proxy `pg_bindings` wire shape (resolving username/password from
the provider). It does NOT go through `config.load_resolved` / the scheme model.

Provider batching mirrors bindings: refs are grouped by provider so a costly
provider (a vault that must unlock) is invoked once per resolve across all pg
bindings that share it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable

from ..errors import ConfigError, CredproxyError
from .bindings import (
    _atomic_write_text,
    _block_spans,
    _toml_key,
    _toml_str,
)
from .injectors import ENV_NAME_RE
from ..providers import fetch_many as provider_fetch_many
from .workspace import Workspace

import tomllib

# --- sslmode / defaults: MIRROR of proxy/pg.py (a separate deploy unit; the CLI
# can't import the proxy). Kept in lockstep by tests/cli/test_pg_wire_parity.py,
# exactly as schemes.py mirrors the proxy's scheme names. ---
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_SSLMODE = "verify-full"
DEFAULT_PG_PORT = 5432
# A pg credential is always a (username, password) pair resolved from the
# provider -- the fixed slot set (mirrors the proxy's secret expectations).
PG_SLOTS = ("username", "password")

# The `[[postgres]]` table header (a trailing comment is allowed), fed to the
# generic array-depth-aware block machinery shared with bindings/rules.
_POSTGRES_HEADER_RE = re.compile(r"^\s*\[\[\s*postgres\s*\]\]\s*(#.*)?$")
# A `[postgres.secret]` child sub-table (the alternative to the inline `secret =
# { ... }` we render): fold it into the preceding element's span so `postgres
# remove` doesn't orphan it.
_POSTGRES_CHILD_RE = re.compile(r"^\s*\[\s*postgres\.[^\[\]\n]*\]\s*(#.*)?$")


@dataclass(frozen=True)
class Postgres:
    name: str | None          # None until materialized (auto-named at add)
    host: str
    port: int
    dbname: str
    provider: str
    secret: dict[str, str]    # slot -> provider ref; slots == PG_SLOTS
    sslmode: str = DEFAULT_SSLMODE
    sslrootcert: str | None = None
    env: str | None = None     # DSN export target; None means no export


# ---- parsing / validation ---------------------------------------------------


def _parse_postgres_entry(p: dict, source: str, where: str) -> Postgres:
    """Validate one `[[postgres]]` entry's field shapes/types. Cross-entry
    checks (unique names, provider resolution) are `validate`'s job. The single
    field validator shared by the load path and `postgres add`, mirrored to
    proxy/pg.load_pg for wire parity."""
    if not isinstance(p, dict):
        raise ConfigError(f"{source}: {where} must be a table")

    name = p.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise ConfigError(f"{source}: {where}.name must be a non-empty string")

    host = p.get("host")
    if not isinstance(host, str) or not host:
        raise ConfigError(f"{source}: {where}.host is required (string)")

    port = p.get("port", DEFAULT_PG_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"{source}: {where}.port must be an integer 1..65535")

    dbname = p.get("dbname")
    if not isinstance(dbname, str) or not dbname:
        raise ConfigError(f"{source}: {where}.dbname is required (string)")

    provider = p.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ConfigError(f"{source}: {where}.provider is required (string)")

    secret = p.get("secret")
    if not isinstance(secret, dict) or not secret or not all(
        isinstance(k, str) and k and isinstance(v, str) and v
        for k, v in secret.items()
    ):
        raise ConfigError(
            f"{source}: {where}.secret must be a table mapping the slots "
            f"{{{', '.join(PG_SLOTS)}}} to non-empty provider refs")
    if set(secret) != set(PG_SLOTS):
        raise ConfigError(
            f"{source}: {where}.secret needs exactly the slots "
            f"{{{', '.join(PG_SLOTS)}}}, got {{{', '.join(sorted(secret))}}}")

    sslmode = p.get("sslmode", DEFAULT_SSLMODE)
    if sslmode not in SSLMODES:
        raise ConfigError(
            f"{source}: {where}.sslmode must be one of {', '.join(SSLMODES)} "
            f"(got {sslmode!r})")

    sslrootcert = p.get("sslrootcert")
    if sslrootcert is not None and (not isinstance(sslrootcert, str) or not sslrootcert):
        raise ConfigError(
            f"{source}: {where}.sslrootcert must be a non-empty string or absent")

    env = p.get("env")
    if env is not None and (not isinstance(env, str) or not env):
        raise ConfigError(f"{source}: {where}.env must be a non-empty string or absent")

    return Postgres(
        name=name, host=host, port=port, dbname=dbname, provider=provider,
        secret=dict(secret), sslmode=sslmode, sslrootcert=sslrootcert, env=env)


def _parse_postgres(raw: dict, source: str) -> list[Postgres]:
    """Parse the `[[postgres]]` array from a raw TOML dict via the per-entry
    validator. Cross-entry checks are `validate`'s job."""
    items = raw.get("postgres") or []
    if not isinstance(items, list):
        raise ConfigError(f"{source}: `postgres` must be an array of tables")
    return [_parse_postgres_entry(p, source, f"postgres[{i}]")
            for i, p in enumerate(items)]


def _sanitize(token: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", token.lower()).strip("-")
    return s or "pg"


def _auto_name(pg: Postgres, taken: set[str]) -> str:
    """`pg-<dbname>`, with a numeric suffix on collision. Used by `postgres add`
    and to SUGGEST a name in the missing-`name` error."""
    base = f"pg-{_sanitize(pg.dbname)}"
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _require_postgres_names(pgs: list[Postgres], source: str) -> None:
    """Enforce the hand-authored-`name` contract (mirrors bindings/rules)."""
    taken = {p.name for p in pgs if p.name}
    for i, p in enumerate(pgs):
        if p.name is None:
            suggestion = _auto_name(p, taken)
            raise ConfigError(
                f"{source}: postgres[{i}] is missing a required `name` -- add a "
                f"line like `name = {_toml_str(suggestion)}` to its [[postgres]] "
                f"block (pg binding names are hand-authored)")


def validate(pgs: list[Postgres], source: str) -> None:
    """Cross-entry + semantic validation on field-parsed pg bindings: unique
    names, resolvable provider, valid env identifier. Names must already be
    materialized. Mirrors bindings.validate (a separate deploy unit from
    proxy/pg.load_pg, which re-checks the resolved wire)."""
    from ..providers import find_provider

    names: set[str] = set()
    for p in pgs:
        if p.name is None:
            raise ConfigError(f"{source}: a pg binding is missing a name")
        if p.name in names:
            raise ConfigError(f"{source}: duplicate pg binding name '{p.name}'")
        names.add(p.name)

        if p.env is not None and not ENV_NAME_RE.fullmatch(p.env):
            raise ConfigError(
                f"{source}: pg binding '{p.name}': env {p.env!r} must be a valid "
                f"shell/env identifier")

        find_provider(p.provider)  # raises ProviderError if unknown


def cross_names(pgs: list[Postgres]) -> set[str]:
    """The materialized pg names, for the resolver's cross-namespace collision
    check against bindings/rules."""
    return {p.name for p in pgs if p.name}


def load_postgres(ws: Workspace) -> list[Postgres]:
    """Parse + validate the workspace's `[[postgres]]` array (names REQUIRED)."""
    source = str(ws.config_path)
    pgs = _parse_postgres(tomllib.loads(ws.config_path.read_text()), source)
    _require_postgres_names(pgs, source)
    validate(pgs, source)
    return pgs


# ---- imperative edits (append at EOF / delete a whole named block) ----------


def _render_postgres_block(pg: Postgres) -> str:
    """Render a fully-formed `[[postgres]]` block (leading blank line), escaping
    every interpolated value so it round-trips as valid TOML."""
    secret_inner = ", ".join(f'{_toml_key(slot)} = {_toml_str(pg.secret[slot])}'
                             for slot in PG_SLOTS)
    lines = [
        "",
        "[[postgres]]",
        f'name     = {_toml_str(pg.name)}',
        f'host     = {_toml_str(pg.host)}',
    ]
    if pg.port != DEFAULT_PG_PORT:
        lines.append(f'port     = {pg.port}')
    lines.append(f'dbname   = {_toml_str(pg.dbname)}')
    if pg.sslmode != DEFAULT_SSLMODE:
        lines.append(f'sslmode  = {_toml_str(pg.sslmode)}')
    if pg.sslrootcert is not None:
        lines.append(f'sslrootcert = {_toml_str(pg.sslrootcert)}')
    lines.append(f'provider = {_toml_str(pg.provider)}')
    lines.append(f"secret   = {{ {secret_inner} }}")
    if pg.env is not None:
        lines.append(f'env      = {_toml_str(pg.env)}')
    return "\n".join(lines) + "\n"


def append_postgres(ws: Workspace, pg: Postgres) -> None:
    """Append a single `[[postgres]]` block to the workspace TOML."""
    text = ws.config_path.read_text()
    if text and not text.endswith("\n"):
        text += "\n"
    _atomic_write_text(ws.config_path, text + _render_postgres_block(pg))


def remove_postgres(ws: Workspace, name: str) -> None:
    """Remove the named pg binding's `[[postgres]]` block via a surgical edit."""
    text = ws.config_path.read_text()
    raw = tomllib.loads(text)
    pgs = _parse_postgres(raw, str(ws.config_path))
    matches = [i for i, p in enumerate(pgs) if p.name == name]
    if not matches:
        raise ConfigError(f"pg binding '{name}' not found in {ws.config_path}")
    if len(matches) > 1:
        raise ConfigError(
            f"pg binding '{name}' is defined more than once in {ws.config_path}; "
            f"resolve the duplicate names before removing it")
    target = matches[0]
    lines = text.splitlines(keepends=True)
    spans = _block_spans(text, _POSTGRES_HEADER_RE, _POSTGRES_CHILD_RE)
    if len(spans) != len(pgs):
        raise ConfigError(
            f"'{name}' isn't a removable `[[postgres]]` block in {ws.config_path} "
            f"-- rewrite it as a `[[postgres]]` table to remove it")
    start, end = spans[target]
    if start > 0 and lines[start - 1].strip() == "":
        start -= 1
    del lines[start:end]
    _atomic_write_text(ws.config_path, "".join(lines))


# ---- provider resolution + wire mapping -------------------------------------


def _refs_by_provider(pgs: list[Postgres]) -> dict[str, list[str]]:
    """Group pg secret refs by provider, deduped/order-preserving (mirrors
    bindings._refs_by_provider) so a provider is invoked once per resolve."""
    buckets: dict[str, dict[str, None]] = {}
    for p in pgs:
        bucket = buckets.setdefault(p.provider, {})
        for ref in p.secret.values():
            bucket[ref] = None
    return {provider: list(refs) for provider, refs in buckets.items()}


def resolve_pg_secrets(
    pgs: list[Postgres],
    fetch_many: Callable[[str, list[str]], dict[str, str]] = provider_fetch_many,
) -> dict[str, dict[str, str]]:
    """Resolve every pg binding's username/password with one provider invocation
    per distinct provider. Returns `{provider: {ref: value}}`."""
    return {
        provider: fetch_many(provider, refs)
        for provider, refs in _refs_by_provider(pgs).items()
    }


def postgres_wire_entries(
    pgs: list[Postgres],
    fetch_many: Callable[[str, list[str]], dict[str, str]] = provider_fetch_many,
) -> list[dict]:
    """Resolve each pg binding's credential and produce the proxy `pg_bindings`
    wire entries (mirrors proxy/pg.load_pg's expected shape). The resolved
    username/password are literal values -- the real secret, POSTed over loopback
    HTTP exactly like an HTTP binding's secret."""
    resolved = resolve_pg_secrets(pgs, fetch_many)
    entries: list[dict] = []
    for p in pgs:
        values = resolved[p.provider]
        entry: dict = {
            "name": p.name,
            "host": p.host,
            "port": p.port,
            "dbname": p.dbname,
            "sslmode": p.sslmode,
            "username": values[p.secret["username"]],
            "password": values[p.secret["password"]],
        }
        if p.sslrootcert is not None:
            entry["sslrootcert"] = p.sslrootcert
        if p.env is not None:
            entry["env"] = p.env
        entries.append(entry)
    return entries


def postgres_fingerprint_items(pgs: list[Postgres]) -> list[dict]:
    """Stable wire-metadata items for the config fingerprint (NO resolved secret
    -- only the refs), sorted by name so a reorder doesn't churn the hash (pg
    bindings have no ordering semantic, unlike rules)."""
    items = [
        {"name": p.name, "host": p.host, "port": p.port, "dbname": p.dbname,
         "sslmode": p.sslmode, "sslrootcert": p.sslrootcert,
         "provider": p.provider, "secret": dict(p.secret), "env": p.env}
        for p in pgs
    ]
    return sorted(items, key=lambda d: d["name"] or "")


def postgres_summaries(pgs: list[Postgres]) -> list[dict]:
    """Sanitized operator-facing summary (no secret refs/values) for drift
    reporting -- name, host, port, dbname, sslmode, effective env."""
    return [
        {"name": p.name, "host": p.host, "port": p.port, "dbname": p.dbname,
         "sslmode": p.sslmode, "env": p.env}
        for p in pgs
    ]
