"""PostgreSQL credential-broker config: the `pg_bindings` wire section.

A **sibling** of the HTTP binding model, not a scheme. A pg binding never
joins the mitmproxy intercept set, has no placeholder, no substitute/sign
family, and no host-glob matching -- forcing it through `config.load_resolved`
would fight every invariant there (see the design note in CLAUDE.md). So it
gets its own structs and its own loader, exactly as `rules` sits beside
`bindings`.

The proxy receives already-resolved bindings (literal `username`/`password`,
no provider refs) as a top-level `pg_bindings` array on the SAME
POST /admin/config wire; `admin.py` loads this section alongside
`config.load_resolved` and swaps both under the one `generation` counter.

    {
      "pg_bindings": [
        {
          "name":     "analytics",              # selector: the startup `user`
          "host":     "db.internal",            # real upstream host
          "port":     5432,                      # optional, default 5432
          "dbname":   "warehouse",               # fallback db (never the user)
          "sslmode":  "verify-full",             # default; server-leg TLS policy
          "sslrootcert": "/path/ca.pem",         # optional private-CA bundle
          "username": "<real>",                  # resolved, pushed
          "password": "<real>",                  # resolved, pushed
          "env":      "DATABASE_URL"             # optional; DSN target for /exports.sh
        }
      ]
    }

The Phase-2 loader (`load_pg`) validates and produces a `PgCredentials`.
Phase 1 only needs the structs, so the broker (`pgbroker.py`) can be built
and tested against them before the admin wiring lands.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# The server-leg TLS policies, mirroring libpq's sslmode. Default is the
# strictest (verify-full): the broker originates the real connection with the
# real credential, so an unverified server leg is an active-MITM hole with a
# far bigger blast radius than a wrong password (it steals the whole session).
# `disable` skips TLS entirely; `allow`/`prefer` attempt-but-don't-verify;
# `require` encrypts without verifying; `verify-ca`/`verify-full` verify the
# cert (and, for -full, the hostname).
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_SSLMODE = "verify-full"
DEFAULT_PG_PORT = 5432


@dataclass(frozen=True)
class PgBinding:
    """One named upstream the broker can dial with an injected credential.

    `name` is the selector: the workspace connects as this Postgres user and
    the broker re-originates to `host:port` as the real `username`/`password`.
    Secrets arrive already resolved (the host CLI fetched them from the
    provider and pushed literal values) -- the proxy never fetches.
    """
    name: str
    host: str
    port: int
    dbname: str
    username: str
    password: str
    sslmode: str = DEFAULT_SSLMODE
    sslrootcert: str | None = None
    env: str | None = None


@dataclass(frozen=True)
class PgCredentials:
    """The loaded, immutable set of pg bindings, addressed by name.

    Swapped atomically on each accepted config push (like `Credentials`).
    `get` is the broker's per-connection selector lookup -- it live-reads the
    current instance off `AppState`, so a re-push takes effect on the next
    connection without a restart.
    """
    bindings: dict[str, PgBinding]

    def get(self, name: str) -> PgBinding | None:
        return self.bindings.get(name)

    def names(self) -> list[str]:
        return sorted(self.bindings)


EMPTY = PgCredentials({})


# ---- Loader (the `pg_bindings` wire section) ----


class PgConfigError(Exception):
    """Raised on `pg_bindings` validation failure. Kept distinct from
    config.ConfigError so pg.py stays independent of the HTTP binding model;
    admin_config catches both. main.py SystemExits at startup; the admin
    endpoint returns 400."""


def _fail(msg: str) -> None:
    raise PgConfigError(f"[pg] {msg}")


def _check_resolved(value: str, source: str, where: str) -> None:
    # The proxy receives already-resolved values; a lingering ${secret:...} ref
    # means the host CLI failed to resolve -- reject rather than dial with it.
    if "${secret:" in value:
        _fail(f"{source}: {where} still contains an unresolved secret reference")


def load_pg(
    raw: Any, source: str = "<resolved>", *, reserved: Iterable[str] = ()
) -> PgCredentials:
    """Build PgCredentials from a parsed config dict's optional top-level
    `pg_bindings` array (absent/empty -> EMPTY). `reserved` is the set of names
    already claimed by HTTP bindings/rules -- the config namespace is shared, so
    a pg binding can't collide with one (mirrors the CLI's RESERVED_NAMES)."""
    if not isinstance(raw, dict):
        _fail(f"{source}: config must be an object")
    entries = raw.get("pg_bindings") or []
    if not isinstance(entries, list):
        _fail(f"{source}: `pg_bindings` must be an array")

    reserved_set = set(reserved)
    bindings: dict[str, PgBinding] = {}
    for i, entry in enumerate(entries):
        where = f"pg_bindings[{i}]"
        if not isinstance(entry, dict):
            _fail(f"{source}: {where} must be an object")

        name = entry.get("name")
        if not isinstance(name, str) or not name:
            _fail(f"{source}: {where}.name must be a non-empty string")
        if name in bindings:
            _fail(f"{source}: duplicate pg binding name '{name}'")
        if name in reserved_set:
            _fail(f"{source}: pg binding name '{name}' collides with a "
                  f"binding/rule of the same name")

        host = entry.get("host")
        if not isinstance(host, str) or not host:
            _fail(f"{source}: {where}.host must be a non-empty string")

        port = entry.get("port", DEFAULT_PG_PORT)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            _fail(f"{source}: {where}.port must be an integer 1..65535")

        dbname = entry.get("dbname")
        if not isinstance(dbname, str) or not dbname:
            _fail(f"{source}: {where}.dbname must be a non-empty string")

        username = entry.get("username")
        if not isinstance(username, str) or not username:
            _fail(f"{source}: {where}.username must be a non-empty string")
        _check_resolved(username, source, f"{where}.username")

        # Password is optional: a trust-auth upstream sends AuthenticationOk
        # with no challenge, so the broker never needs one.
        password = entry.get("password", "")
        if not isinstance(password, str):
            _fail(f"{source}: {where}.password must be a string")
        _check_resolved(password, source, f"{where}.password")

        sslmode = entry.get("sslmode", DEFAULT_SSLMODE)
        if sslmode not in SSLMODES:
            _fail(f"{source}: {where}.sslmode must be one of {', '.join(SSLMODES)} "
                  f"(got {sslmode!r})")

        sslrootcert = entry.get("sslrootcert")
        if sslrootcert is not None and (not isinstance(sslrootcert, str) or not sslrootcert):
            _fail(f"{source}: {where}.sslrootcert must be a non-empty string or absent")

        env = entry.get("env")
        if env is not None and (not isinstance(env, str) or not env):
            _fail(f"{source}: {where}.env must be a non-empty string or absent")

        bindings[name] = PgBinding(
            name=name, host=host, port=port, dbname=dbname, username=username,
            password=password, sslmode=sslmode, sslrootcert=sslrootcert, env=env)

    return PgCredentials(bindings)


def reserved_names(creds) -> set[str]:
    """The HTTP-side names (bindings + rules) a pg binding must not collide
    with. Takes a `config.Credentials` (duck-typed to avoid the import)."""
    names = {b.name for b in creds.inward_bindings()}
    names |= {r.name for r in creds.rule_set().all()}
    return names
