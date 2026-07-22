"""Phase 2: the `pg_bindings` loader + its admin-push wiring."""
import json

import pytest
from aiohttp import web

import admin
import bootstrap
import pg
from pg import DEFAULT_PG_PORT, DEFAULT_SSLMODE, PgBinding, PgConfigError, load_pg


def _entry(**over):
    e = {"name": "analytics", "host": "db.internal", "dbname": "warehouse",
         "username": "svc", "password": "pw"}
    e.update(over)
    return e


# ---- loader ----


def test_absent_or_empty_is_empty():
    assert load_pg({}).bindings == {}
    assert load_pg({"pg_bindings": []}).bindings == {}
    assert load_pg({"pg_bindings": None}).bindings == {}


def test_defaults_filled():
    creds = load_pg({"pg_bindings": [_entry()]})
    b = creds.get("analytics")
    assert b.port == DEFAULT_PG_PORT
    assert b.sslmode == DEFAULT_SSLMODE == "verify-full"
    assert b.sslrootcert is None and b.env is None
    assert b.username == "svc" and b.password == "pw"


def test_all_fields():
    creds = load_pg({"pg_bindings": [_entry(
        port=6432, sslmode="require", sslrootcert="/ca.pem", env="DATABASE_URL")]})
    b = creds.get("analytics")
    assert (b.port, b.sslmode, b.sslrootcert, b.env) == \
        (6432, "require", "/ca.pem", "DATABASE_URL")


def test_password_optional_for_trust_auth():
    assert load_pg({"pg_bindings": [_entry(password="")]}).get("analytics").password == ""
    # absent entirely also fine
    e = _entry()
    del e["password"]
    assert load_pg({"pg_bindings": [e]}).get("analytics").password == ""


@pytest.mark.parametrize("over,msg", [
    ({"name": ""}, "name"),
    ({"host": ""}, "host"),
    ({"dbname": ""}, "dbname"),
    ({"username": ""}, "username"),
    ({"port": 0}, "port"),
    ({"port": 70000}, "port"),
    ({"port": "5432"}, "port"),
    ({"port": True}, "port"),
    ({"sslmode": "bogus"}, "sslmode"),
    ({"sslrootcert": ""}, "sslrootcert"),
    ({"env": ""}, "env"),
])
def test_field_validation(over, msg):
    with pytest.raises(PgConfigError, match=msg):
        load_pg({"pg_bindings": [_entry(**over)]})


def test_duplicate_name_rejected():
    with pytest.raises(PgConfigError, match="duplicate"):
        load_pg({"pg_bindings": [_entry(), _entry()]})


def test_reserved_name_collision_rejected():
    with pytest.raises(PgConfigError, match="collides"):
        load_pg({"pg_bindings": [_entry(name="dup")]}, reserved={"dup"})


def test_unresolved_secret_ref_rejected():
    with pytest.raises(PgConfigError, match="unresolved"):
        load_pg({"pg_bindings": [_entry(password="${secret:db_pw}")]})
    with pytest.raises(PgConfigError, match="unresolved"):
        load_pg({"pg_bindings": [_entry(username="${secret:db_user}")]})


def test_not_a_list_rejected():
    with pytest.raises(PgConfigError, match="must be an array"):
        load_pg({"pg_bindings": {"analytics": {}}})


# ---- admin push wiring (mirrors test_admin.py's fixtures) ----


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "TOKEN_PATH", tmp_path / "auth.token")
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "auth.token").write_text("tok")
    return admin.AppState()


@pytest.fixture
def app(state):
    app = web.Application(middlewares=[admin.no_store, admin.fetch_metadata_guard])
    app[admin.STATE_KEY] = state
    app.router.add_routes(admin.admin_routes)
    app.router.add_routes(bootstrap.bootstrap_routes)
    return app


PG_CONFIG = {
    "bindings": [],
    "pg_bindings": [{"name": "analytics", "host": "db.internal",
                     "dbname": "warehouse", "username": "svc", "password": "pw",
                     "env": "DATABASE_URL"}],
}


async def test_push_loads_pg_bindings(aiohttp_client, app, state):
    client = await aiohttp_client(app)
    resp = await client.post("/admin/config",
                             headers={"Authorization": "Bearer tok"},
                             json=PG_CONFIG)
    assert resp.status == 200
    assert (await resp.json())["generation"] == 1
    # swapped into AppState, live-readable by the broker
    assert state.pg_creds.get("analytics").host == "db.internal"
    # persisted in the tmpfs envelope alongside the HTTP config
    persisted = json.loads(admin.CONFIG_PATH.read_text())
    assert persisted["pg_bindings"][0]["name"] == "analytics"


async def test_push_rejects_invalid_pg(aiohttp_client, app):
    client = await aiohttp_client(app)
    bad = {"bindings": [], "pg_bindings": [{"name": "x"}]}  # missing host/dbname/user
    resp = await client.post("/admin/config",
                             headers={"Authorization": "Bearer tok"},
                             json=bad)
    assert resp.status == 400
    assert "[pg]" in (await resp.json())["error"]


async def test_push_rejects_pg_name_colliding_with_binding(aiohttp_client, app):
    client = await aiohttp_client(app)
    bad = {
        "bindings": [{"name": "dup", "hosts": ["api.github.com"], "scheme": "bearer",
                      "params": {"header": "Authorization"}, "placeholder": "ph",
                      "secret": {"value": "v"}}],
        "pg_bindings": [{"name": "dup", "host": "db", "dbname": "d",
                         "username": "u", "password": "p"}],
    }
    resp = await client.post("/admin/config",
                             headers={"Authorization": "Bearer tok"},
                             json=bad)
    assert resp.status == 400
    assert "collides" in (await resp.json())["error"]


# ---- Phase 3: bootstrap (DSN, /exports.sh, /setup least-disclosure) ----


def test_pg_dsn_percent_encodes():
    b = PgBinding(name="odd name", host="h", port=5432, dbname="d/b",
                  username="u", password="p")
    assert bootstrap.pg_dsn(b) == \
        "postgresql://odd%20name@proxy.local:5432/d%2Fb?sslmode=disable"


def test_exports_body_combines_http_and_pg():
    import config
    # An HTTP binding (placeholder) + a pg binding (DSN), plus an env-less pg
    # binding that must be skipped.
    http = config.load_resolved({"bindings": [
        {"name": "gh", "hosts": ["api.github.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH",
         "secret": {"value": "v"}, "env": "GH_TOKEN"}]})
    pg_creds = load_pg({"pg_bindings": [
        {"name": "analytics", "host": "h", "dbname": "wh", "username": "u",
         "password": "p", "env": "DATABASE_URL"},
        {"name": "noenv", "host": "h", "dbname": "wh", "username": "u",
         "password": "p"},
    ]})
    body = bootstrap.exports_body(http, pg_creds)
    assert "export GH_TOKEN='PH'" in body
    assert ("export DATABASE_URL="
            "'postgresql://analytics@proxy.local:5432/wh?sslmode=disable'") in body
    assert "noenv" not in body  # env-less pg binding skipped


async def test_setup_exposes_pg_least_disclosure(aiohttp_client, app):
    client = await aiohttp_client(app)
    await client.post("/admin/config", headers={"Authorization": "Bearer tok"},
                      json=PG_CONFIG)
    body = await (await client.get("/setup")).json()
    assert set(body["pg_bindings"]) == {"analytics"}
    assert body["pg_bindings"]["analytics"] == {
        "env": "DATABASE_URL", "dbname": "warehouse",
        "dsn": "postgresql://analytics@proxy.local:5432/warehouse?sslmode=disable",
    }
    # Least disclosure: the real host / username / password appear NOWHERE.
    blob = json.dumps(body)
    assert "db.internal" not in blob
    assert '"svc"' not in blob and "pw" not in blob


async def test_exports_sh_route_includes_pg(aiohttp_client, app):
    client = await aiohttp_client(app)
    await client.post("/admin/config", headers={"Authorization": "Bearer tok"},
                      json=PG_CONFIG)
    text = await (await client.get("/exports.sh")).text()
    assert ("export DATABASE_URL="
            "'postgresql://analytics@proxy.local:5432/warehouse?sslmode=disable'") in text
