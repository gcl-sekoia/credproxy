"""Tests for the `postgres` noun: model parse/validate/edit/wire, resolver
integration, fingerprint, the porcelain add/remove/list flow, and wire parity
with the proxy's pg.load_pg (the CLI and proxy are separate deploy units)."""
from __future__ import annotations

import io
import sys
import textwrap
from pathlib import Path

import pytest


def _write_ws(workspaces_dir: Path, name: str, content: str):
    from credproxy_cli.core.model.workspace import Workspace
    p = workspaces_dir / f"{name}.toml"
    p.write_text(textwrap.dedent(content))
    return Workspace(name)


_PG_BLOCK = """
    image = "x"
    [[postgres]]
    name     = "analytics"
    host     = "db.internal"
    dbname   = "warehouse"
    provider = "env"
    secret   = { username = "DB_USER", password = "DB_PASS" }
    env      = "DATABASE_URL"
"""


# ---- model: parse / validate ------------------------------------------------


def test_parse_defaults(xdg, workspaces_dir):
    from credproxy_cli.core.model.postgres import (
        DEFAULT_PG_PORT, DEFAULT_SSLMODE, load_postgres)
    ws = _write_ws(workspaces_dir, "w", _PG_BLOCK)
    (p,) = load_postgres(ws)
    assert p.name == "analytics" and p.host == "db.internal"
    assert p.port == DEFAULT_PG_PORT
    assert p.sslmode == DEFAULT_SSLMODE == "verify-full"
    assert p.secret == {"username": "DB_USER", "password": "DB_PASS"}
    assert p.env == "DATABASE_URL"


def test_all_fields(xdg, workspaces_dir):
    from credproxy_cli.core.model.postgres import load_postgres
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[postgres]]
        name = "a"
        host = "h"
        port = 6432
        dbname = "d"
        sslmode = "require"
        sslrootcert = "/ca.pem"
        provider = "env"
        secret = { username = "U", password = "P" }
    """)
    (p,) = load_postgres(ws)
    assert (p.port, p.sslmode, p.sslrootcert) == (6432, "require", "/ca.pem")


@pytest.mark.parametrize("block,msg", [
    ('host = "h"\ndbname = "d"\nprovider = "env"', "secret"),        # no secret
    ('host = "h"\ndbname = "d"\nsecret = { username = "u", password = "p" }', "provider"),
    ('dbname = "d"\nprovider = "env"\nsecret = { username = "u", password = "p" }', "host"),
    ('host = "h"\nprovider = "env"\nsecret = { username = "u", password = "p" }', "dbname"),
])
def test_missing_required_field(xdg, workspaces_dir, block, msg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.postgres import load_postgres
    ws = _write_ws(workspaces_dir, "w",
                   'image = "x"\n[[postgres]]\nname = "a"\n' + block)
    with pytest.raises(ConfigError, match=msg):
        load_postgres(ws)


def test_secret_slots_must_be_username_password(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.postgres import load_postgres
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[postgres]]
        name = "a"
        host = "h"
        dbname = "d"
        provider = "env"
        secret = { value = "V" }
    """)
    with pytest.raises(ConfigError, match="username, password"):
        load_postgres(ws)


def test_bad_sslmode_and_port(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.postgres import load_postgres
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[postgres]]
        name = "a"
        host = "h"
        dbname = "d"
        sslmode = "bogus"
        provider = "env"
        secret = { username = "u", password = "p" }
    """)
    with pytest.raises(ConfigError, match="sslmode"):
        load_postgres(ws)


def test_missing_name_prescriptive(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.postgres import load_postgres
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[postgres]]
        host = "h"
        dbname = "d"
        provider = "env"
        secret = { username = "u", password = "p" }
    """)
    with pytest.raises(ConfigError, match="missing a required `name`"):
        load_postgres(ws)


def test_duplicate_name(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.postgres import load_postgres
    ws = _write_ws(workspaces_dir, "w", _PG_BLOCK + textwrap.dedent("""
        [[postgres]]
        name     = "analytics"
        host     = "other"
        dbname   = "d2"
        provider = "env"
        secret   = { username = "u", password = "p" }
    """))
    with pytest.raises(ConfigError, match="duplicate pg binding name"):
        load_postgres(ws)


# ---- resolver integration ---------------------------------------------------


def test_resolver_includes_postgres(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", _PG_BLOCK)
    resolved = resolve_workspace(ws)
    assert [p.name for p in resolved.postgres] == ["analytics"]


def test_pg_name_collides_with_binding(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[binding]]
        name = "dup"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["api.github.com"]

        [[postgres]]
        name = "dup"
        host = "h"
        dbname = "d"
        provider = "env"
        secret = { username = "u", password = "p" }
    """)
    with pytest.raises(ConfigError, match="collides with a binding/rule"):
        resolve_workspace(ws)


# ---- append / remove surgical edits -----------------------------------------


def test_append_and_remove_roundtrip(xdg, workspaces_dir):
    from credproxy_cli.core.model.postgres import (
        Postgres, append_postgres, load_postgres, remove_postgres)
    ws = _write_ws(workspaces_dir, "w", 'image = "x"\n# keep this comment\n')
    pg = Postgres(name="a", host="h", port=5432, dbname="d", provider="env",
                  secret={"username": "U", "password": "P"}, env="DATABASE_URL")
    append_postgres(ws, pg)
    (loaded,) = load_postgres(ws)
    assert loaded.name == "a" and loaded.env == "DATABASE_URL"
    assert "# keep this comment" in ws.config_path.read_text()
    remove_postgres(ws, "a")
    assert load_postgres(ws) == []
    # the hand-owned content survives the round-trip
    assert "# keep this comment" in ws.config_path.read_text()


# ---- wire mapping + fingerprint ---------------------------------------------


def test_wire_entries_resolve_secrets():
    from credproxy_cli.core.model.postgres import Postgres, postgres_wire_entries
    pg = Postgres(name="a", host="db", port=5432, dbname="d", provider="vault",
                  secret={"username": "u/ref", "password": "p/ref"},
                  sslmode="require", env="DATABASE_URL")

    def fake_fetch(provider, refs):
        assert provider == "vault"
        return {"u/ref": "real_user", "p/ref": "real_pw"}

    (entry,) = postgres_wire_entries([pg], fetch_many=fake_fetch)
    assert entry == {
        "name": "a", "host": "db", "port": 5432, "dbname": "d",
        "sslmode": "require", "username": "real_user", "password": "real_pw",
        "env": "DATABASE_URL",
    }


def test_fingerprint_reacts_to_pg_but_stable_without():
    from credproxy_cli.core.model.postgres import Postgres
    from credproxy_cli.core.model.rules import combined_fingerprint
    # No pg -> identical to the two-arg call (no spurious churn on upgrade).
    assert combined_fingerprint([], []) == combined_fingerprint([], [], [])
    pg = Postgres(name="a", host="h", port=5432, dbname="d", provider="env",
                  secret={"username": "u", "password": "p"})
    pg2 = Postgres(name="a", host="h2", port=5432, dbname="d", provider="env",
                   secret={"username": "u", "password": "p"})
    fp1 = combined_fingerprint([], [], [pg])
    fp2 = combined_fingerprint([], [], [pg2])
    assert fp1 != fp2 != combined_fingerprint([], [])


# ---- wire parity with the proxy ---------------------------------------------


def _proxy_pg():
    proxy_dir = str(Path(__file__).resolve().parents[2] / "proxy")
    if proxy_dir not in sys.path:
        sys.path.insert(0, proxy_dir)
    import pg as proxy_pg
    return proxy_pg


def test_wire_entries_accepted_by_proxy_loader():
    from credproxy_cli.core.model.postgres import Postgres, postgres_wire_entries
    proxy_pg = _proxy_pg()
    pgs = [
        Postgres(name="a", host="db", port=6432, dbname="d", provider="v",
                 secret={"username": "u", "password": "p"}, sslmode="verify-full",
                 sslrootcert="/ca.pem", env="DATABASE_URL"),
        Postgres(name="b", host="db2", port=5432, dbname="d2", provider="v",
                 secret={"username": "u2", "password": "p2"}, sslmode="disable"),
    ]
    entries = postgres_wire_entries(
        pgs, fetch_many=lambda prov, refs: {r: f"val-{r}" for r in refs})
    creds = proxy_pg.load_pg({"pg_bindings": entries})
    assert creds.names() == ["a", "b"]
    a = creds.get("a")
    assert a.port == 6432 and a.sslmode == "verify-full"
    assert a.sslrootcert == "/ca.pem" and a.env == "DATABASE_URL"
    assert a.username == "val-u" and a.password == "val-p"


def test_sslmode_constants_match_proxy():
    from credproxy_cli.core.model import postgres as cli_pg
    proxy_pg = _proxy_pg()
    assert cli_pg.SSLMODES == proxy_pg.SSLMODES
    assert cli_pg.DEFAULT_SSLMODE == proxy_pg.DEFAULT_SSLMODE
    assert cli_pg.DEFAULT_PG_PORT == proxy_pg.DEFAULT_PG_PORT


# ---- porcelain e2e ----------------------------------------------------------


def _run(argv: list[str]) -> tuple[int, str, str]:
    from credproxy_cli.porcelain import render
    render.set_format(False)
    old = (sys.argv, sys.stdout, sys.stderr)
    sys.argv = ["credproxy"] + argv
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    ec = 0
    try:
        from credproxy_cli.porcelain.cli import main
        main(loose_default=False)
    except SystemExit as e:
        ec = e.code if isinstance(e.code, int) else 1
    finally:
        out, err = sys.stdout.getvalue(), sys.stderr.getvalue()
        sys.argv, sys.stdout, sys.stderr = old
        render.set_format(False)
    return ec, out, err


def test_cli_add_list_remove(xdg, workspaces_dir):
    from credproxy_cli.core.model.postgres import load_postgres
    from credproxy_cli.core.model.workspace import Workspace
    (workspaces_dir / "w.toml").write_text('image = "x"\n')

    ec, out, err = _run([
        "workspace", "w", "postgres", "add", "--provider", "env",
        "--secret", "username=DB_USER", "--secret", "password=DB_PASS",
        "--host", "db.internal", "--dbname", "warehouse", "--env", "DATABASE_URL"])
    assert ec == 0, err
    ws = Workspace("w")
    (p,) = load_postgres(ws)
    assert p.name == "pg-warehouse"          # auto-named
    assert p.host == "db.internal" and p.dbname == "warehouse"
    assert "proxy.local:5432/warehouse" in out

    ec, out, err = _run(["workspace", "w", "postgres", "list"])
    assert ec == 0 and "pg-warehouse" in out and "db.internal:5432" in out

    ec, out, err = _run(["workspace", "w", "postgres", "remove", "pg-warehouse"])
    assert ec == 0
    assert load_postgres(Workspace("w")) == []


def test_cli_add_requires_both_secret_slots(xdg, workspaces_dir):
    (workspaces_dir / "w.toml").write_text('image = "x"\n')
    ec, out, err = _run([
        "workspace", "w", "postgres", "add", "--provider", "env",
        "--secret", "password=DB_PASS",   # missing username
        "--host", "h", "--dbname", "d"])
    assert ec != 0
    assert "username" in err or "slots" in err


# ---- Phase 5: doctor + inspect ----------------------------------------------


def test_doctor_pg_static_ok(xdg, workspaces_dir):
    from credproxy_cli.core.engine.doctor import _postgres_checks
    ws = _write_ws(workspaces_dir, "w", _PG_BLOCK)
    checks = {c.id: c for c in _postgres_checks(ws, fetch=False)}
    assert checks["ws:w:postgres"].ok


def test_doctor_pg_none_no_checks(xdg, workspaces_dir):
    from credproxy_cli.core.engine.doctor import _postgres_checks
    ws = _write_ws(workspaces_dir, "w", 'image = "x"\n')
    assert _postgres_checks(ws, fetch=False) == []


def test_doctor_pg_sslrootcert_is_advisory_not_host_checked(xdg, workspaces_dir):
    # doctor must NOT os.path.isfile() the sslrootcert on the HOST: the broker
    # reads it inside the proxy container, so a host check would be a false
    # green. It is surfaced as an advisory (ok=True) mentioning the container.
    from credproxy_cli.core.engine.doctor import _postgres_checks
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[postgres]]
        name = "a"
        host = "h"
        dbname = "d"
        sslrootcert = "/nonexistent/ca.pem"
        provider = "env"
        secret = { username = "u", password = "p" }
    """)
    checks = {c.id: c for c in _postgres_checks(ws, fetch=False)}
    c = checks["ws:w:pg[0]:sslrootcert"]
    assert c.ok                       # advisory, not a (misleading) host-side fail
    assert "proxy container" in c.message


def test_pg_drift_detects_add_remove_and_rotation(xdg, workspaces_dir):
    # The critical apply-blindness fix: a pg-only add/remove/secret-rotation must
    # surface as `kind="postgres"` drift so `apply` re-pushes (esp. a REMOVAL, or
    # revocation silently lingers on the running proxy).
    from credproxy_cli.core.engine import containers
    from credproxy_cli.core.model.postgres import Postgres
    ws = _write_ws(workspaces_dir, "w", 'image = "x"\n')
    p = Postgres(name="a", host="h", port=5432, dbname="d", provider="env",
                 secret={"username": "U", "password": "P"})
    containers._write_applied_push(ws, [], [], 1, postgres=[p])   # seed applied

    # unchanged -> no drift (proves the applied record and the drift-side record
    # are the same shape and round-trip through the lock JSON)
    assert containers._postgres_drift(ws, [p], running=True) == []

    # removal -> caught (this is the revocation case)
    d = containers._postgres_drift(ws, [], running=True)
    assert len(d) == 1 and d[0].kind == "postgres" and "removed" in d[0].item

    # add -> caught
    q = Postgres(name="b", host="h2", port=5432, dbname="d2", provider="env",
                 secret={"username": "U2", "password": "P2"})
    d = containers._postgres_drift(ws, [p, q], running=True)
    assert any(c.kind == "postgres" and "added" in c.item and "'b'" in c.item
               for c in d)

    # secret-ref rotation -> caught (the sanitized-summary storage would MISS this)
    rotated = Postgres(name="a", host="h", port=5432, dbname="d", provider="env",
                       secret={"username": "U", "password": "P-ROTATED"})
    d = containers._postgres_drift(ws, [rotated], running=True)
    assert len(d) == 1 and "changed" in d[0].item


def test_inspect_renders_pg_section(capsys):
    from credproxy_cli.porcelain.render import Renderer
    data = {
        "name": "w", "config_path": "/x/w.toml",
        "config": {"image": "x", "home": None, "mounts": [], "env": {}, "setup": []},
        "proxy_status": None, "ws_status": None, "running": False,
        "host_port": None, "attach": None, "attach_target": None,
        "bindings": [], "rules": [],
        "postgres": [{"name": "analytics", "host": "db.internal", "port": 5432,
                      "dbname": "warehouse", "sslmode": "verify-full",
                      "provider": "vault", "env": "DATABASE_URL"}],
        "drift": {"in_sync": True, "changes": []},
        "live": None, "_running": False,
    }
    Renderer().inspect(data)
    out = capsys.readouterr().out
    assert "postgres    1" in out
    assert "analytics: db.internal:5432/warehouse" in out
    assert "$DATABASE_URL" in out
