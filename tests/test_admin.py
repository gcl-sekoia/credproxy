"""Tests for the merged HTTP API: admin (bearer-gated) + bootstrap routes."""
import json

import pytest
from aiohttp import web

import admin
import bootstrap
import schemes
from config import BindingCredentials, InwardBinding, Transform


def _xform(placeholder, real, *, header="Authorization", name="b"):
    """A bearer Transform for tests."""
    return Transform(name, schemes.SCHEMES["bearer"], {"header": header},
                     placeholder, {"value": real})


def _async_return(value):
    """A monkeypatch stand-in for the async `_listener_bound` probe."""
    async def _f(*a, **k):
        return value
    return _f


@pytest.fixture
def state(monkeypatch, tmp_path):
    """Fresh AppState; TOKEN_PATH/CONFIG_PATH redirected to tmp_path,
    token file pre-populated so admin_config's per-call read succeeds.

    Makes `/health` capture-ready by default -- the mitmproxy-listener probe
    returns True and the CA exists -- so the guard/route tests get 200; the
    readiness tests below override one or the other. (The real probe has its own
    unit test.)"""
    token_path = tmp_path / "auth.token"
    monkeypatch.setattr(admin, "TOKEN_PATH", token_path)
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.json")
    token_path.write_text("established")
    ca = tmp_path / "mitmproxy-ca-cert.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setattr(bootstrap, "CA_CERT_PATH", ca)
    monkeypatch.setattr(bootstrap, "_listener_bound", _async_return(True))
    return admin.AppState()


@pytest.fixture
def app(state):
    app = web.Application(
        middlewares=[admin.no_store, admin.fetch_metadata_guard]
    )
    app[admin.STATE_KEY] = state
    app.router.add_routes(admin.admin_routes)
    app.router.add_routes(bootstrap.bootstrap_routes)
    return app


VALID_CONFIG = {
    "bindings": [
        {
            "name": "github-env",
            "hosts": ["api.github.com"],
            "scheme": "bearer",
            "params": {"header": "Authorization"},
            "placeholder": "credproxy_test",
            "secret": {"value": "github_pat_real"},
            "env": "GITHUB_TOKEN",
        }
    ]
}


# ---- load_initial_state ----

def test_load_initial_state_missing_token_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "TOKEN_PATH", tmp_path / "auth.token")
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.json")
    with pytest.raises(SystemExit, match="missing"):
        admin.load_initial_state()


def test_load_initial_state_empty_token_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "TOKEN_PATH", tmp_path / "auth.token")
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "auth.token").write_text("")
    with pytest.raises(SystemExit, match="empty"):
        admin.load_initial_state()


def test_load_initial_state_token_only_no_config(monkeypatch, tmp_path):
    """Token present, config absent -> proxy starts with empty intercept set."""
    monkeypatch.setattr(admin, "TOKEN_PATH", tmp_path / "auth.token")
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "auth.token").write_text("xyz\n")
    state = admin.load_initial_state()
    assert state.creds.intercept_hosts() == set()


def test_load_initial_state_token_and_config(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "TOKEN_PATH", tmp_path / "auth.token")
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "auth.token").write_text("xyz")
    (tmp_path / "config.json").write_text(json.dumps(VALID_CONFIG))
    state = admin.load_initial_state()
    assert state.creds.intercept_hosts() == {"api.github.com"}


def test_load_initial_state_invalid_config_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "TOKEN_PATH", tmp_path / "auth.token")
    monkeypatch.setattr(admin, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "auth.token").write_text("xyz")
    (tmp_path / "config.json").write_text(json.dumps({"not-bindings": {}}))
    with pytest.raises(SystemExit, match="persisted config invalid"):
        admin.load_initial_state()


# ---- /admin/config: bearer auth ----

async def test_post_with_correct_token_reloads(aiohttp_client, app, state):
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"},
        json=VALID_CONFIG,
    )
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "generation": 1}
    assert "api.github.com" in state.creds.intercept_hosts()
    # The persisted envelope is the client body PLUS the proxy-internal generation.
    persisted = json.loads(admin.CONFIG_PATH.read_text())
    assert persisted == dict(VALID_CONFIG, generation=1)
    assert state.generation == 1


async def test_post_no_authorization_header_401(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.post("/admin/config", json=VALID_CONFIG)
    assert resp.status == 401


async def test_post_non_bearer_scheme_401(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Basic c2VjcmV0"},
        json=VALID_CONFIG,
    )
    assert resp.status == 401


async def test_post_with_wrong_token_401(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer wrong"},
        json=VALID_CONFIG,
    )
    assert resp.status == 401


async def test_post_close_match_token_401(aiohttp_client, app):
    """Off-by-one-character token must still 401 (no prefix-match leak)."""
    admin.TOKEN_PATH.write_text("established-token-abc")
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established-token-ab"},
        json=VALID_CONFIG,
    )
    assert resp.status == 401


async def test_post_wrong_token_beats_bad_body(aiohttp_client, app):
    """Auth check must precede body parsing/validation: an attacker
    sending a bogus body should not be able to fingerprint schema
    errors (400) before being rejected for auth (401)."""
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/config",
        headers={
            "Authorization": "Bearer wrong",
            "Content-Type": "application/json",
        },
        data=b"not json",
    )
    assert resp.status == 401

    resp2 = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer wrong"},
        json={"not-bindings": {}},
    )
    assert resp2.status == 401


async def test_post_invalid_json_400(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/config",
        headers={
            "Authorization": "Bearer established",
            "Content-Type": "application/json",
        },
        data=b"not json",
    )
    assert resp.status == 400


async def test_post_invalid_config_does_not_overwrite(aiohttp_client, app, state):
    """Bad config validation -> 400 -> on-disk + in-memory state untouched."""
    admin.CONFIG_PATH.write_text(json.dumps(VALID_CONFIG))
    initial_creds = state.creds
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"},
        json={"not-bindings": {}},
    )
    assert resp.status == 400
    assert json.loads(admin.CONFIG_PATH.read_text()) == VALID_CONFIG
    assert state.creds is initial_creds


async def test_post_unresolved_secret_rejected(aiohttp_client, app):
    bad = {"bindings": [
        {"name": "b", "hosts": ["api.github.com"], "scheme": "bearer",
         "placeholder": "ph", "secret": {"value": "${secret:GITHUB_PAT}"}}
    ]}
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"},
        json=bad,
    )
    assert resp.status == 400
    body = await resp.json()
    assert "GITHUB_PAT" in body["error"]


async def test_token_rotation_takes_effect_without_restart(aiohttp_client, app):
    """Rewriting TOKEN_PATH mid-flight: the old value 401s on the next
    request, the new value works -- no app restart required."""
    client = await aiohttp_client(app)

    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"},
        json=VALID_CONFIG,
    )
    assert resp.status == 200

    admin.TOKEN_PATH.write_text("rotated")

    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"},
        json=VALID_CONFIG,
    )
    assert resp.status == 401

    resp = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer rotated"},
        json=VALID_CONFIG,
    )
    assert resp.status == 200


# ---- fetch_metadata_guard ----

async def test_sfs_cross_site_rejected(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get(
        "/health", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert resp.status == 403


async def test_sfs_same_site_rejected(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get(
        "/health", headers={"Sec-Fetch-Site": "same-site"}
    )
    assert resp.status == 403


async def test_sfs_same_origin_allowed(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get(
        "/health", headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert resp.status == 200


async def test_sfs_none_allowed(aiohttp_client, app):
    """Sec-Fetch-Site: none -- address-bar / bookmark fetches."""
    client = await aiohttp_client(app)
    resp = await client.get(
        "/health", headers={"Sec-Fetch-Site": "none"}
    )
    assert resp.status == 200


async def test_sfs_missing_allowed(aiohttp_client, app):
    """Non-browser clients (curl, host CLI) don't send Sec-Fetch-Site."""
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200


# ---- bootstrap routes on the merged listener ----

async def test_health_route(aiohttp_client, app):
    """Capture-ready (fixture default): 200, ok, no `pending`."""
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert "pending" not in body


async def test_health_503_when_listener_not_bound(aiohttp_client, app, monkeypatch):
    """Liveness != readiness: the HTTP listener answers, but while the mitmproxy
    listener isn't accepting, `/health` reports 503 with the listener pending (#23)."""
    monkeypatch.setattr(bootstrap, "_listener_bound", _async_return(False))
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 503
    body = await resp.json()
    assert body["ok"] is False
    # Both always-on listeners are probed; the shared stub reports both down.
    assert body["pending"] == ["mitmproxy-listener", "pg-listener"]


async def test_health_503_before_ca_generated(aiohttp_client, app, monkeypatch, tmp_path):
    """Listener up but CA not yet written: 503 with `ca-cert` pending -- the
    exact window #23 names (ready reported before the CA exists)."""
    monkeypatch.setattr(bootstrap, "CA_CERT_PATH", tmp_path / "absent-ca.pem")
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 503
    body = await resp.json()
    assert body["pending"] == ["ca-cert"]


async def test_health_503_lists_all_pending_at_boot(aiohttp_client, app, monkeypatch, tmp_path):
    """Cold start -- neither listener nor CA up: both pending, most-fundamental
    (listener) first."""
    monkeypatch.setattr(bootstrap, "_listener_bound", _async_return(False))
    monkeypatch.setattr(bootstrap, "CA_CERT_PATH", tmp_path / "absent-ca.pem")
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 503
    assert (await resp.json())["pending"] == \
        ["mitmproxy-listener", "pg-listener", "ca-cert"]


async def test_health_503_when_only_pg_listener_down(aiohttp_client, app, monkeypatch):
    """Port-aware probe: mitmproxy up but the pg broker not yet accepting ->
    only `pg-listener` pending. The pg listener is part of capture-readiness."""
    async def probe(host, port, timeout=0.5):
        return port != bootstrap.PG_PORT
    monkeypatch.setattr(bootstrap, "_listener_bound", probe)
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 503
    assert (await resp.json())["pending"] == ["pg-listener"]


async def test_listener_bound_probe_observes_real_socket():
    """The probe `/health` relies on: True against a live listener, False against
    a closed port. This is the version-proof replacement for trusting an addon
    flag -- so it gets a real-socket test."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    host, port = srv.getsockname()
    try:
        assert await bootstrap._listener_bound(host, port) is True
    finally:
        srv.close()
    # Port is closed now -> connect refused -> not bound.
    assert await bootstrap._listener_bound(host, port) is False


# ---- /ready: creds-readiness (capture-ready AND a config pushed) ----


def _app_over_state(st):
    """Build a fresh HTTP app bound to `st` -- mirrors the `app` fixture but for a
    state we constructed ourselves (e.g. a reload-restored one)."""
    a = web.Application(middlewares=[admin.no_store, admin.fetch_metadata_guard])
    a[admin.STATE_KEY] = st
    a.router.add_routes(admin.admin_routes)
    a.router.add_routes(bootstrap.bootstrap_routes)
    return a


async def test_ready_503_before_any_push(aiohttp_client, app):
    """Generation 0 (no config yet) -> /ready is 503 with `config` pending and
    generation 0, EVEN though /health (capture-ready) is 200. This is the I1/I2
    non-collapse: /health = capture-ready, /ready = /health + creds-ready."""
    client = await aiohttp_client(app)
    r_health = await client.get("/health")
    assert r_health.status == 200  # capture-ready per the fixture

    r_ready = await client.get("/ready")
    assert r_ready.status == 503
    body = await r_ready.json()
    assert body["ok"] is False
    assert body["generation"] == 0
    assert body["pending"] == ["config"]


async def test_ready_200_after_valid_push(aiohttp_client, app):
    client = await aiohttp_client(app)
    r = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=VALID_CONFIG)
    assert r.status == 200

    r_ready = await client.get("/ready")
    assert r_ready.status == 200
    body = await r_ready.json()
    assert body["ok"] is True
    assert body["generation"] == 1
    assert "pending" not in body


async def test_ready_generation_increments_on_second_push(aiohttp_client, app):
    client = await aiohttp_client(app)
    for expected in (1, 2):
        r = await client.post(
            "/admin/config",
            headers={"Authorization": "Bearer established"}, json=VALID_CONFIG)
        assert (await r.json())["generation"] == expected
        assert (await (await client.get("/ready")).json())["generation"] == expected


async def test_ready_green_for_rules_only_config(aiohttp_client, app):
    """A rules-only config (zero bindings, >= 1 rule) is a valid, ready state:
    gating on the generation counter -- not `bindings` being non-empty -- is what
    lets a guardrail-only proxy go green."""
    rules_only = {"bindings": [], "rules": [
        {"name": "blk", "hosts": ["api.github.com"], "action": "block"},
    ]}
    client = await aiohttp_client(app)
    r = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=rules_only)
    assert r.status == 200
    r_ready = await client.get("/ready")
    assert r_ready.status == 200
    assert (await r_ready.json())["generation"] == 1


async def test_ready_503_when_capture_not_ready_despite_config(
        aiohttp_client, app, monkeypatch):
    """Even with a config pushed (generation >= 1), /ready stays 503 while the
    capture layer isn't ready -- it is a superset of /health, not a replacement."""
    client = await aiohttp_client(app)
    r = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=VALID_CONFIG)
    assert r.status == 200
    monkeypatch.setattr(bootstrap, "_listener_bound", _async_return(False))
    r_ready = await client.get("/ready")
    assert r_ready.status == 503
    body = await r_ready.json()
    assert body["generation"] == 1
    assert "mitmproxy-listener" in body["pending"]
    assert "config" not in body["pending"]


async def test_generation_persists_across_reload(aiohttp_client, app, state):
    """Simulate `credproxy dev reload`: a POST writes config.json to the tmpfs,
    then a fresh AppState built from that same tmpfs (via load_initial_state, as
    the re-exec'd process does) restores the generation -- so /ready stays green
    across the reload instead of flapping red."""
    client = await aiohttp_client(app)
    for _ in range(2):
        r = await client.post(
            "/admin/config",
            headers={"Authorization": "Bearer established"}, json=VALID_CONFIG)
        assert r.status == 200
    assert state.generation == 2

    # Fresh process: re-read the surviving tmpfs. TOKEN_PATH/CONFIG_PATH are the
    # monkeypatched tmp_path files the POSTs just wrote.
    restored = admin.load_initial_state()
    assert restored.generation == 2
    assert "api.github.com" in restored.creds.intercept_hosts()

    fresh_client = await aiohttp_client(_app_over_state(restored))
    r_ready = await fresh_client.get("/ready")
    assert r_ready.status == 200
    assert (await r_ready.json())["generation"] == 2


async def test_invalid_config_does_not_increment_generation(aiohttp_client, app, state):
    """A validation failure returns 400 without bumping the generation -- so a
    bad push can't flip /ready green or advance the counter."""
    client = await aiohttp_client(app)
    r = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=VALID_CONFIG)
    assert r.status == 200
    assert state.generation == 1

    r_bad = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json={"not-bindings": {}})
    assert r_bad.status == 400
    assert state.generation == 1
    assert (await (await client.get("/ready")).json())["generation"] == 1


async def test_setup_carries_config_generation(aiohttp_client, app):
    """`/setup` exposes config_generation so a workspace-side consumer can poll
    readiness without an admin route -- and it tracks the counter."""
    client = await aiohttp_client(app)
    body0 = await (await client.get("/setup")).json()
    assert body0["config_generation"] == 0

    r = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=VALID_CONFIG)
    assert r.status == 200
    body1 = await (await client.get("/setup")).json()
    assert body1["config_generation"] == 1


async def test_setup_generation_adds_no_disclosure(aiohttp_client, app):
    """config_generation is the ONLY new /setup field; the disclosure posture is
    otherwise unchanged (no secret/provider/secret-id keys creep in)."""
    client = await aiohttp_client(app)
    await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=VALID_CONFIG)
    body = await (await client.get("/setup")).json()
    assert set(body.keys()) == {
        "version", "workspace", "config_generation", "ca_url", "ca_env",
        "intercept_hosts", "bindings", "pg_bindings", "rules",
    }


def test_running_hook_is_log_only_no_state():
    """The addon's `running` hook is now boot-visibility logging only -- it must
    NOT touch AppState (readiness is observed live via the port probe), and
    AppState carries no capture-ready field to go stale."""
    import addon
    st = admin.AppState()
    assert not hasattr(st, "capture_ready")
    addon.HostnameLogger(st).running()          # must not raise / set anything
    assert not hasattr(st, "capture_ready")


async def test_index_route_map(aiohttp_client, app):
    """Bare GET / returns a plain-text route map instead of a 404."""
    client = await aiohttp_client(app)
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.content_type == "text/plain"
    text = await resp.text()
    assert "/setup" in text and "/bootstrap.sh" in text
    assert "/exports.sh" in text


async def test_setup_static_fields(aiohttp_client, app):
    """Static fields present even with an empty credentials state."""
    client = await aiohttp_client(app)
    resp = await client.get("/setup")
    assert resp.status == 200
    # Pretty-printed with a trailing newline for clean `curl` output.
    text = await resp.text()
    assert text.endswith("\n")
    assert "\n  " in text  # indented
    body = await resp.json()
    assert body["ca_url"] == "http://proxy.local/ca.crt"
    assert body["version"] == bootstrap.VERSION
    assert body["ca_env"] == bootstrap.CA_ENV
    assert body["intercept_hosts"] == []
    assert body["bindings"] == {}


async def test_setup_reflects_state(aiohttp_client, app, state):
    """After a config push, /setup returns the inward bindings shape."""
    state.creds = BindingCredentials(
        {"api.github.com": [_xform("ph", "real")]},
        [InwardBinding(name="gh", placeholder="ph", env="GH_TOKEN",
                       scheme="bearer", params={"header": "Authorization"},
                       hosts=["api.github.com"])],
    )
    client = await aiohttp_client(app)
    resp = await client.get("/setup")
    assert resp.status == 200
    body = await resp.json()
    assert body["intercept_hosts"] == ["api.github.com"]
    bindings = body["bindings"]
    # Keyed by binding name; the key is the sole carrier of the name (no inner
    # `name` field).
    assert list(bindings.keys()) == ["gh"]
    b = bindings["gh"]
    assert "name" not in b
    assert b["placeholder"] == "ph"
    assert b["env"] == "GH_TOKEN"
    assert b["scheme"] == "bearer"
    assert b["params"] == {"header": "Authorization"}
    assert b["hosts"] == ["api.github.com"]


async def test_setup_exposes_workspace_name(aiohttp_client, app, monkeypatch):
    """The workspace's own name is exposed for self-identification (e.g. PS1)."""
    monkeypatch.setenv("CREDPROXY_WORKSPACE", "myproj")
    client = await aiohttp_client(app)
    resp = await client.get("/setup")
    body = await resp.json()
    assert body["workspace"] == "myproj"


async def test_setup_workspace_name_absent_is_null(aiohttp_client, app, monkeypatch):
    """Gracefully null when the env var is unset (e.g. a proxy created before
    this field existed)."""
    monkeypatch.delenv("CREDPROXY_WORKSPACE", raising=False)
    client = await aiohttp_client(app)
    resp = await client.get("/setup")
    body = await resp.json()
    assert body["workspace"] is None


async def test_setup_least_disclosure(aiohttp_client, app, state):
    """Inward API: real credential values must NOT appear in /setup response."""
    state.creds = BindingCredentials(
        {"api.github.com": [_xform("ph_sentinel", "super_secret_real")]},
        [InwardBinding(name="gh", placeholder="ph_sentinel", env=None,
                       scheme="bearer", params={"header": "Authorization"},
                       hosts=["api.github.com"])],
    )
    client = await aiohttp_client(app)
    resp = await client.get("/setup")
    body_text = await resp.text()
    assert "super_secret_real" not in body_text
    # provider and secret-id are CLI-side only; confirm they also can't appear
    # (they are never sent to the proxy in the push model).


# ---- bootstrap CA bundle: combined system roots + proxy CA (issue #10) -------


def test_bootstrap_sh_is_valid_posix_sh():
    """The embedded bootstrap script must be valid POSIX sh -- guards the
    escaping of the Python string literal (e.g. the `\\` line continuation)."""
    import subprocess
    r = subprocess.run(["sh", "-n"], input=bootstrap.BOOTSTRAP_SH,
                       text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


def test_bootstrap_sh_builds_combined_ca_bundle():
    """The CA env-var bundle (/tmp/proxy-ca.crt) is the system roots PLUS the
    proxy CA, so env-var-only tools (mise/node/cargo/aws/requests) verify
    passthrough hosts too -- not just intercepted ones."""
    sh = bootstrap.BOOTSTRAP_SH
    # The proxy CA is downloaded to its OWN file, kept apart from the bundle.
    assert 'curl -sf -o "$CA_ONLY" http://proxy.local/ca.crt' in sh
    # Combined bundle = a system root bundle ++ the proxy CA.
    assert 'cat "$SYS_CA" "$CA_ONLY" > "$CA_PATH"' in sh
    # Both Debian/Ubuntu/Alpine and RHEL/Fedora root-bundle locations are probed.
    assert "/etc/ssl/certs/ca-certificates.crt" in sh
    assert "/etc/pki/tls/certs/ca-bundle.crt" in sh


def test_bootstrap_sh_system_store_installs_only_proxy_ca():
    """Regression guard: the system-store step installs the SINGLE proxy CA, not
    the combined bundle -- else update-ca-certificates would re-append every
    system root to the system store."""
    sh = bootstrap.BOOTSTRAP_SH
    assert 'cp "$CA_ONLY" /usr/local/share/ca-certificates/proxy.crt' in sh
    assert 'cp "$CA_PATH" /usr/local/share/ca-certificates/proxy.crt' not in sh


def test_ca_env_points_at_combined_bundle():
    """Every CA env var points at the combined bundle, never the proxy-only file
    -- the bundle is what makes both intercepted and passthrough hosts verify."""
    assert set(bootstrap.CA_ENV.values()) == {"/tmp/proxy-ca.crt"}


def test_workspace_bindings_function():
    """Unit test for the bootstrap.workspace_bindings free function."""
    creds = BindingCredentials(
        {
            "api.github.com": [_xform("ph1", "r1")],
            "api.example.com": [_xform("ph2", "r2", header="X-API-Key")],
        },
        [
            InwardBinding(name="gh", placeholder="ph1", env="GH_TOKEN",
                          scheme="bearer", params={"header": "Authorization"},
                          hosts=["api.github.com"]),
            InwardBinding(name="ex", placeholder="ph2", env=None,
                          scheme="bearer", params={"header": "X-API-Key"},
                          hosts=["api.example.com"]),
        ],
    )
    by_name = bootstrap.workspace_bindings(creds)
    assert set(by_name) == {"gh", "ex"}
    # Keyed by name; the name is the key, never repeated inside the value.
    assert "name" not in by_name["gh"]
    assert by_name["gh"]["placeholder"] == "ph1"
    assert by_name["gh"]["env"] == "GH_TOKEN"
    assert by_name["gh"]["scheme"] == "bearer"
    assert by_name["gh"]["params"] == {"header": "Authorization"}
    assert by_name["gh"]["hosts"] == ["api.github.com"]
    assert "real" not in by_name["gh"]
    assert "secret" not in by_name["gh"]
    assert by_name["ex"]["env"] is None


def test_workspace_bindings_empty():
    assert bootstrap.workspace_bindings(BindingCredentials({})) == {}


# ---- /exports.sh: binding placeholder exports ----


async def test_exports_sh_content_type(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/exports.sh")
    assert resp.status == 200
    assert resp.content_type == "text/x-shellscript"


async def test_exports_sh_empty_is_valid_script(aiohttp_client, app):
    """No bindings -> a valid (empty) script, not a 404 or blank body."""
    client = await aiohttp_client(app)
    resp = await client.get("/exports.sh")
    body = await resp.text()
    assert body.strip().startswith("#")   # a comment line
    import subprocess
    r = subprocess.run(["sh", "-n"], input=body, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


async def test_exports_sh_happy_path(aiohttp_client, app, state):
    """One `export ENV="placeholder"` line per binding with env + placeholder."""
    state.creds = BindingCredentials(
        {"api.github.com": [_xform("ph1", "real")]},
        [InwardBinding(name="gh", placeholder="ph1", env="GITHUB_TOKEN",
                       scheme="bearer", params={"header": "Authorization"},
                       hosts=["api.github.com"])],
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/exports.sh")).text()
    assert "export GITHUB_TOKEN='ph1'" in body
    # The value the shell would assign is exactly the placeholder.
    import subprocess
    out = subprocess.run(["sh", "-c", body + "\nprintf %s \"$GITHUB_TOKEN\""],
                         text=True, capture_output=True)
    assert out.stdout == "ph1"


async def test_exports_sh_skips_missing_env(aiohttp_client, app, state):
    """A binding with no env is skipped (nothing to export it as)."""
    state.creds = BindingCredentials(
        {"api.example.com": [_xform("ph2", "real")]},
        [InwardBinding(name="ex", placeholder="ph2", env=None,
                       scheme="bearer", params={"header": "X-API-Key"},
                       hosts=["api.example.com"])],
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/exports.sh")).text()
    assert "ph2" not in body
    assert body.strip().startswith("#")


async def test_exports_sh_skips_null_placeholder(aiohttp_client, app, state):
    """A sign-family binding (no placeholder, e.g. sigv4) is skipped even with
    an env, since there is nothing inert to export."""
    state.creds = BindingCredentials(
        {},
        [InwardBinding(name="aws", placeholder=None, env="AWS_THING",
                       scheme="sigv4", params={}, hosts=["s3.amazonaws.com"])],
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/exports.sh")).text()
    assert "AWS_THING" not in body


async def test_exports_sh_quotes_defensively(aiohttp_client, app, state):
    """A placeholder with shell metacharacters must still assign literally --
    single-quoting disables expansion; an embedded single quote round-trips."""
    tricky = "a'b$c`d\"e\\f"
    state.creds = BindingCredentials(
        {"h": [_xform(tricky, "real")]},
        [InwardBinding(name="t", placeholder=tricky, env="TRICKY",
                       scheme="bearer", params={"header": "Authorization"},
                       hosts=["h"])],
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/exports.sh")).text()
    import subprocess
    out = subprocess.run(["sh", "-c", body + "\nprintf %s \"$TRICKY\""],
                         text=True, capture_output=True)
    assert out.stdout == tricky


async def test_exports_sh_skips_non_identifier_env(aiohttp_client, app, state):
    """A wire env that isn't a shell identifier (the CLI rejects these, but the
    wire may come from a non-CLI pusher) is skipped with an observable comment
    -- never interpolated unquoted into an `export`, which would break the whole
    script for every binding."""
    state.creds = BindingCredentials(
        {"h": [_xform("ph1", "real")], "g": [_xform("ph2", "real2")]},
        [InwardBinding(name="bad", placeholder="ph1", env="MY TOKEN",
                       scheme="bearer", params={"header": "Authorization"},
                       hosts=["h"]),
         # Trailing newline: passes a `$`-anchored .match() and would inject a
         # literal line break into the export -- only .fullmatch() catches it.
         InwardBinding(name="sneaky", placeholder="ph3", env="FOO\n",
                       scheme="bearer", params={"header": "Authorization"},
                       hosts=["h"]),
         InwardBinding(name="good", placeholder="ph2", env="OK_TOKEN",
                       scheme="bearer", params={"header": "Authorization"},
                       hosts=["g"])],
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/exports.sh")).text()
    assert "MY TOKEN" not in body
    assert "export FOO" not in body
    assert "# skipped 'bad': env is not a shell identifier" in body
    assert "# skipped 'sneaky': env is not a shell identifier" in body
    assert "export OK_TOKEN='ph2'" in body      # the good binding still exports
    import subprocess
    r = subprocess.run(["sh", "-n"], input=body, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


async def test_exports_sh_reflects_live_config(aiohttp_client, app, state):
    """/exports.sh reads the live loaded config each request (not a module
    constant), so a config swap is reflected without a proxy restart."""
    client = await aiohttp_client(app)
    assert "FOO" not in await (await client.get("/exports.sh")).text()
    state.creds = BindingCredentials(
        {"h": [_xform("phX", "real")]},
        [InwardBinding(name="b", placeholder="phX", env="FOO",
                       scheme="bearer", params={"header": "Authorization"},
                       hosts=["h"])],
    )
    assert "export FOO='phX'" in await (await client.get("/exports.sh")).text()


def test_bootstrap_sh_appends_dynamic_exports_line():
    """profile.d gets the CA-trust SNAPSHOT plus a DYNAMIC line that re-fetches
    /exports.sh on each login shell, degrading silently if the proxy is down."""
    sh = bootstrap.BOOTSTRAP_SH
    assert 'curl -sf http://proxy.local/env.sh > "$PROFILE_PATH"' in sh
    assert 'http://proxy.local/exports.sh' in sh
    assert 'eval "$(curl -sf --max-time 1 http://proxy.local/exports.sh 2>/dev/null)"' in sh


async def test_no_store_header_present(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.headers.get("Cache-Control") == "no-store"


# ---- GET /admin/config: superset (fast-path loaded/fingerprint + live config) ----


async def test_get_config_unloaded(aiohttp_client, app):
    """Empty config: generation 0, empty bindings/rules, loaded false -- still 200."""
    client = await aiohttp_client(app)
    resp = await client.get(
        "/admin/config", headers={"Authorization": "Bearer established"})
    assert resp.status == 200
    assert await resp.json() == {
        "loaded": False, "fingerprint": None,
        "generation": 0, "bindings": [], "rules": [],
    }


async def test_get_config_requires_auth(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/admin/config")
    assert resp.status == 401


async def test_get_config_reports_fingerprint(aiohttp_client, app):
    """The fast-path fields (loaded/fingerprint) survive the superset extension,
    so `_should_push` keeps working unchanged."""
    client = await aiohttp_client(app)
    body = dict(VALID_CONFIG, fingerprint="abc123")
    r = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=body)
    assert r.status == 200
    resp = await client.get(
        "/admin/config", headers={"Authorization": "Bearer established"})
    assert resp.status == 200
    got = await resp.json()
    assert got["loaded"] is True
    assert got["fingerprint"] == "abc123"
    assert got["generation"] == 1
    assert got["bindings"] == [{
        "name": "github-env", "hosts": ["api.github.com"], "scheme": "bearer",
        "placeholder": "credproxy_test", "env": "GITHUB_TOKEN",
    }]
    assert got["rules"] == []


async def test_get_config_reports_generation(aiohttp_client, app):
    """`generation` tracks the live counter -- the reality signal #66 keys on."""
    client = await aiohttp_client(app)
    for expected in (1, 2):
        await client.post(
            "/admin/config",
            headers={"Authorization": "Bearer established"}, json=VALID_CONFIG)
        got = await (await client.get(
            "/admin/config",
            headers={"Authorization": "Bearer established"})).json()
        assert got["generation"] == expected


async def test_get_config_sanitized_no_secret_or_param_or_header_leak(
        aiohttp_client, app):
    """SANITIZATION INVARIANT: the host-facing GET body NEVER carries a secret
    value, a `params` value, or a rule header/body value -- it is deliberately
    tighter than /setup. Push a config seeded with sentinels in every sensitive
    slot, GET it, and assert none appear ANYWHERE in the response."""
    cfg = {
        "bindings": [{
            "name": "gh", "hosts": ["api.github.com"], "scheme": "bearer",
            # A param whose VALUE carries a sentinel: params are excluded entirely.
            "params": {"header": "X-Custom-SENTINELPARAM"},
            "placeholder": "ph_visible",
            "secret": {"value": "SENTINELSECRET"},
            "env": "GH_TOKEN",
        }, {
            # A SIGN-family binding whose params are load-bearing (sigv4): the two
            # secret slots AND the params must all stay out of the projection.
            "name": "aws", "hosts": ["*.amazonaws.com"], "scheme": "sigv4",
            "secret": {"access_key_id": "SENTINELAKID",
                       "secret_access_key": "SENTINELSAK"},
            "params": {"region": "SENTINELREGION", "service": "SENTINELSERVICE"},
        }],
        "rules": [{
            "name": "stub", "hosts": ["api.github.com"], "action": "respond",
            "status": 200, "body": "SENTINELBODY",
            "headers": {"X-Leak": "SENTINELHEADER"},
        }, {
            # A rewrite rule: its set/resp-set header VALUES must not leak.
            "name": "rw", "hosts": ["api.github.com"], "action": "rewrite",
            "set_headers": {"X-Add": "SENTINELSETHEADER"},
            "resp_set_headers": {"X-Resp": "SENTINELRESPHEADER"},
        }, {
            # A script rule with [rule.params]: the script SOURCE and its params
            # (operator config, not secrets) are both excluded from the projection.
            "name": "guard", "hosts": ["api.github.com"], "action": "script",
            "script": "guard",
            "script_source": "def on_request():\n    pass  # SENTINELSCRIPTSRC\n",
            "params": {"cfg": "SENTINELRULEPARAM"},
        }],
    }
    client = await aiohttp_client(app)
    r = await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=cfg)
    assert r.status == 200
    resp = await client.get(
        "/admin/config", headers={"Authorization": "Bearer established"})
    assert resp.status == 200
    text = await resp.text()
    for sentinel in ("SENTINELSECRET", "SENTINELPARAM", "SENTINELBODY",
                     "SENTINELHEADER", "SENTINELAKID", "SENTINELSAK",
                     "SENTINELREGION", "SENTINELSERVICE", "SENTINELSETHEADER",
                     "SENTINELRESPHEADER", "SENTINELSCRIPTSRC", "SENTINELRULEPARAM"):
        assert sentinel not in text, sentinel
    body = await resp.json()
    assert body["generation"] == 1
    # The projection is EXACTLY the tight field set (no params/secret/header keys)
    # for every binding and rule, sign-family and script included.
    for b in body["bindings"]:
        assert set(b) == {"name", "hosts", "scheme", "placeholder", "env"}
    for rl in body["rules"]:
        assert set(rl) == {"name", "hosts", "action", "visible"}
    assert {
        "name": "gh", "hosts": ["api.github.com"], "scheme": "bearer",
        "placeholder": "ph_visible", "env": "GH_TOKEN",
    } in body["bindings"]
    assert {
        "name": "stub", "hosts": ["api.github.com"], "action": "respond",
        "visible": True,
    } in body["rules"]


async def test_get_config_reports_hidden_rules_to_operator(aiohttp_client, app):
    """Rule visibility hides from the WORKSPACE (/setup), never the operator: a
    hidden rule still appears in the bearer-gated host-facing GET, flagged
    `visible: false`."""
    cfg = {"bindings": [], "rules": [{
        "name": "trip", "hosts": ["api.github.com"], "action": "block",
        "visible": False,
    }]}
    client = await aiohttp_client(app)
    await client.post(
        "/admin/config",
        headers={"Authorization": "Bearer established"}, json=cfg)
    body = await (await client.get(
        "/admin/config", headers={"Authorization": "Bearer established"})).json()
    assert body["rules"] == [{
        "name": "trip", "hosts": ["api.github.com"], "action": "block",
        "visible": False,
    }]


# ---- /admin/rule-test (live dry-run) ----


async def test_rule_test_endpoint(aiohttp_client, app, state):
    import config
    state.creds = config.load_resolved({"bindings": [], "rules": [
        {"name": "blk", "hosts": ["api.github.com"], "action": "block",
         "methods": ["DELETE"], "path": "/repos/**"},
    ]})
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/rule-test",
        json={"method": "DELETE", "url": "https://api.github.com/repos/a/b"},
        headers={"Authorization": "Bearer established"})
    assert resp.status == 200
    data = await resp.json()
    assert data["intercepted"] is True
    assert [m["name"] for m in data["matches"]] == ["blk"]
    assert data["matches"][0]["terminal"] is True
    # a non-matching method: intercepted (union) but no rule fires
    resp2 = await client.post(
        "/admin/rule-test",
        json={"method": "GET", "url": "https://api.github.com/repos/a/b"},
        headers={"Authorization": "Bearer established"})
    assert (await resp2.json())["matches"] == []


async def test_rule_test_endpoint_requires_token(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.post(
        "/admin/rule-test", json={"method": "GET", "url": "https://x.example.com/"})
    assert resp.status == 401


async def test_llms_txt_covers_schemes_and_network_limits(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/llms.txt")
    assert resp.status == 200
    txt = await resp.text()
    for anchor in ("sigv4", "oauth2-reseal", "script", "session token",
                   "IPv6", "HTTP/3", "GLOB", "credproxy workspace NAME logs",
                   # On ONE line: wrapped inside $(...) it would split into two
                   # commands, breaking a literal copy-paste.
                   'eval "$(curl -s http://proxy.local/exports.sh)"'):
        assert anchor in txt, anchor


# ---- /admin/config/patch: surgical single-binding refresh -------------------


_TWO_BINDING_CONFIG = {
    "bindings": [
        {"name": "a", "hosts": ["api.a.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH_A",
         "secret": {"value": "a_v1"}, "env": "A_TOK"},
        {"name": "b", "hosts": ["api.b.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH_B",
         "secret": {"value": "b_v1"}, "env": "B_TOK"},
    ],
    "fingerprint": "fp-1",
}


async def _push(client, body):
    return await client.post(
        "/admin/config", headers={"Authorization": "Bearer established"}, json=body)


async def _patch(client, body):
    return await client.post(
        "/admin/config/patch",
        headers={"Authorization": "Bearer established"}, json=body)


async def test_patch_refreshes_one_binding_bumps_generation(
        aiohttp_client, app, state):
    client = await aiohttp_client(app)
    assert (await _push(client, _TWO_BINDING_CONFIG)).status == 200
    assert state.generation == 1

    resp = await _patch(client, {"bindings": [
        {"name": "b", "hosts": ["api.b.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH_B",
         "secret": {"value": "b_v2"}, "env": "B_TOK"}]})
    assert resp.status == 200
    payload = await resp.json()
    assert payload["ok"] and payload["generation"] == 2
    assert payload["patched"] == ["b"]
    assert state.generation == 2

    # The held config now has b's new value; a is untouched.
    persisted = json.loads(admin.CONFIG_PATH.read_text())
    by_name = {x["name"]: x for x in persisted["bindings"]}
    assert by_name["b"]["secret"]["value"] == "b_v2"
    assert by_name["a"]["secret"]["value"] == "a_v1"


async def test_patch_preserves_unpatched_bindings_in_live_creds(
        aiohttp_client, app, state):
    client = await aiohttp_client(app)
    await _push(client, _TWO_BINDING_CONFIG)
    await _patch(client, {"bindings": [
        {"name": "a", "hosts": ["api.a.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH_A",
         "secret": {"value": "a_v2"}, "env": "A_TOK"}]})
    # Both bindings still intercept their hosts (the swap validated the whole set).
    assert state.creds.intercepts("api.a.com")
    assert state.creds.intercepts("api.b.com")


async def test_patch_carries_fingerprint(aiohttp_client, app, state):
    client = await aiohttp_client(app)
    await _push(client, _TWO_BINDING_CONFIG)
    await _patch(client, {"bindings": [
        {"name": "a", "hosts": ["api.a.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH_A",
         "secret": {"value": "a_v2"}, "env": "A_TOK"}], "fingerprint": "fp-2"})
    persisted = json.loads(admin.CONFIG_PATH.read_text())
    assert persisted["fingerprint"] == "fp-2"


async def test_patch_unchanged_fingerprint_when_omitted(aiohttp_client, app, state):
    client = await aiohttp_client(app)
    await _push(client, _TWO_BINDING_CONFIG)
    await _patch(client, {"bindings": [
        {"name": "a", "hosts": ["api.a.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH_A",
         "secret": {"value": "a_v2"}, "env": "A_TOK"}]})
    persisted = json.loads(admin.CONFIG_PATH.read_text())
    assert persisted["fingerprint"] == "fp-1"   # preserved from the last push


async def test_patch_unknown_binding_rejected(aiohttp_client, app, state):
    client = await aiohttp_client(app)
    await _push(client, _TWO_BINDING_CONFIG)
    resp = await _patch(client, {"bindings": [
        {"name": "ghost", "hosts": ["api.x.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH_X",
         "secret": {"value": "v"}}]})
    assert resp.status == 400
    assert "ghost" in (await resp.json())["error"]
    assert state.generation == 1   # untouched


async def test_patch_with_no_config_loaded_409(aiohttp_client, app, state):
    client = await aiohttp_client(app)
    resp = await _patch(client, {"bindings": [
        {"name": "a", "hosts": ["api.a.com"], "scheme": "bearer",
         "params": {"header": "Authorization"}, "placeholder": "PH_A",
         "secret": {"value": "v"}}]})
    assert resp.status == 409


async def test_patch_empty_bindings_400(aiohttp_client, app, state):
    client = await aiohttp_client(app)
    await _push(client, _TWO_BINDING_CONFIG)
    resp = await _patch(client, {"bindings": []})
    assert resp.status == 400


async def test_patch_requires_token(aiohttp_client, app, state):
    client = await aiohttp_client(app)
    await _push(client, _TWO_BINDING_CONFIG)
    resp = await client.post("/admin/config/patch", json={"bindings": []})
    assert resp.status == 401


async def test_patch_invalid_config_does_not_overwrite(aiohttp_client, app, state):
    """A patch that produces an invalid merged config is rejected and leaves the
    live creds + generation untouched (fail-closed, same as a full push)."""
    client = await aiohttp_client(app)
    await _push(client, _TWO_BINDING_CONFIG)
    # Overlap 'a' onto b's host+header with a substring placeholder collision would
    # be rejected; simplest: a malformed scheme.
    resp = await _patch(client, {"bindings": [
        {"name": "a", "hosts": ["api.a.com"], "scheme": "no-such-scheme",
         "params": {}, "placeholder": "PH_A", "secret": {"value": "v"}}]})
    assert resp.status == 400
    assert state.generation == 1
    assert state.creds.intercepts("api.a.com")
