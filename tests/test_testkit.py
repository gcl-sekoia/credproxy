"""Tests for `testkit` itself -- the harness overlay authors use.

Exercises the parts the dogfood injector tests (test_ovh_script,
test_jwt_bearer_script) don't: make_request's footgun-hiding, rule-script running
via run_rule, resolution through an overlay, and the manifest/script slot-drift
guard. These set CREDPROXY_OVERLAY_PATH + XDG to a temp overlay so `find_injector`
/`find_script` resolve test fixtures rather than the shipped builtins.
"""
import pytest

import testkit


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """A temp overlay + empty user XDG. Returns the overlay root; drop
    `injectors/<n>.toml` and `scripts/<n>.star` under it and they resolve first
    (nothing in the user tier, so the overlay wins over builtin)."""
    cfg = tmp_path / "config"
    state = tmp_path / "state"
    ov = tmp_path / "overlay"
    for d in (cfg, state, ov / "injectors", ov / "scripts"):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    return ov


def _write_injector(ov, name, body):
    (ov / "injectors" / f"{name}.toml").write_text(body)


def _write_script(ov, name, body):
    (ov / "scripts" / f"{name}.star").write_text(body)


# --------------------------------------------------------------------------
# make_request
# --------------------------------------------------------------------------


def test_make_request_strips_defaults_and_sets_host():
    req = testkit.make_request("GET", "https://api.example.com/v1/x?y=1")
    # treq's default 'header' + bogus 'content-length' are gone.
    assert "header" not in req.headers
    assert "content-length" not in req.headers
    # Host is set from the URL -> pretty_host (what the proxy scopes on) is right.
    assert req.headers["Host"] == "api.example.com"
    assert req.pretty_host == "api.example.com"
    assert req.method == "GET"
    assert req.path == "/v1/x?y=1"


def test_make_request_host_header_override():
    """A Host in `headers` overrides the URL host -- reproducing transparent mode
    (destination IP, real hostname in the Host header)."""
    req = testkit.make_request("GET", "https://10.0.0.1/x",
                               headers={"Host": "real.example.com"})
    assert req.pretty_host == "real.example.com"


def test_make_request_body_and_headers():
    req = testkit.make_request("POST", "https://api.example.com/r",
                               headers={"Content-Type": "application/json"},
                               body='{"a":1}')
    assert req.content == b'{"a":1}'
    assert req.headers["Content-Type"] == "application/json"


# --------------------------------------------------------------------------
# load_injector through an overlay (built-in scheme + scripted)
# --------------------------------------------------------------------------


def test_load_injector_builtin_scheme_via_overlay(overlay):
    _write_injector(overlay, "ov-bearer", 'scheme = "bearer"\n')
    kit = testkit.load_injector("ov-bearer")
    req = testkit.make_request("GET", "https://api.example.com/",
                               headers={"Authorization": "Bearer PH"})
    result = kit.run(req, {"value": "REAL"}, placeholder="PH")
    assert result.injected is True
    assert req.headers["Authorization"] == "Bearer REAL"


def test_load_injector_scripted_via_overlay(overlay):
    _write_injector(overlay, "ov-sign", (
        'scheme = "script"\n'
        'script = "ov-sign"\n'
        'family = "sign"\n'
        'slots = ["api_key"]\n'
        'location_kind = "header"\n'
    ))
    _write_script(overlay, "ov-sign", (
        "def on_request():\n"
        "    req_set_header('X-Api-Key', secret('api_key'))\n"
        "    return True\n"
    ))
    kit = testkit.load_injector("ov-sign")
    req = testkit.make_request("GET", "https://api.example.com/")
    result = kit.run(req, {"api_key": "SEKRET"})
    assert result.injected is True
    assert req.headers["X-Api-Key"] == "SEKRET"


# --------------------------------------------------------------------------
# Manifest / script slot drift surfaces
# --------------------------------------------------------------------------


def test_manifest_script_slot_drift_surfaces(overlay):
    """The manifest declares slot `declared`, but the script (and so the test
    author) works in terms of `value`. Providing the secrets the SCRIPT needs
    fails the harness's slot check -- exactly the drift a hand-built
    ScriptedScheme(slots=...) would have hidden."""
    _write_injector(overlay, "drift", (
        'scheme = "script"\n'
        'script = "drift"\n'
        'family = "substitute"\n'
        'slots = ["declared"]\n'      # manifest says `declared`
    ))
    _write_script(overlay, "drift", (
        "def on_request():\n"
        "    req_set_header('X-K', secret('value'))\n"   # script wants `value`
        "    return True\n"
    ))
    kit = testkit.load_injector("drift")
    req = testkit.make_request("GET", "https://api.example.com/")
    with pytest.raises(ValueError) as ei:
        kit.run(req, {"value": "x"})
    assert "slot" in str(ei.value).lower()


# --------------------------------------------------------------------------
# Rule scripts via run_rule
# --------------------------------------------------------------------------


def test_run_rule_block(overlay):
    _write_script(overlay, "guard", (
        "def on_request():\n"
        "    if req_method() == 'DELETE':\n"
        "        block(403)\n"
    ))
    script = testkit.load_rule_script("guard")
    req = testkit.make_request("DELETE", "https://api.example.com/things/1")
    outcome = testkit.run_rule(script, req)
    assert outcome.terminal and outcome.blocked
    assert outcome.response.status == 403


def test_run_rule_block_skips_non_matching(overlay):
    _write_script(overlay, "guard2", (
        "def on_request():\n"
        "    if req_method() == 'DELETE':\n"
        "        block(403)\n"
    ))
    script = testkit.load_rule_script("guard2")
    req = testkit.make_request("GET", "https://api.example.com/things/1")
    outcome = testkit.run_rule(script, req)
    assert not outcome.terminal and outcome.response is None


def test_run_rule_respond(overlay):
    _write_script(overlay, "teapot", (
        "def on_request():\n"
        "    respond(418, 'no coffee')\n"
    ))
    script = testkit.load_rule_script("teapot")
    req = testkit.make_request("GET", "https://api.example.com/coffee")
    outcome = testkit.run_rule(script, req)
    assert outcome.terminal and not outcome.blocked
    assert outcome.response.kind == "respond"
    assert outcome.response.status == 418
    assert outcome.response.body == "no coffee"


def test_run_rule_rewrite(overlay):
    _write_script(overlay, "tagger", (
        "def on_request():\n"
        "    req_set_header('X-Tagged', 'yes')\n"
    ))
    script = testkit.load_rule_script("tagger")
    req = testkit.make_request("GET", "https://api.example.com/")
    outcome = testkit.run_rule(script, req)
    assert not outcome.terminal          # a rewrite is non-terminal
    assert req.headers["X-Tagged"] == "yes"   # mutation lands on the request


def test_run_rule_reads_params(overlay):
    _write_script(overlay, "paramguard", (
        "def on_request():\n"
        "    if req_method() == param('deny_method', 'DELETE'):\n"
        "        block(int(param('status', '403')))\n"
    ))
    script = testkit.load_rule_script("paramguard")
    req = testkit.make_request("POST", "https://api.example.com/x")
    outcome = testkit.run_rule(script, req, params={"deny_method": "POST",
                                                    "status": "451"})
    assert outcome.blocked and outcome.response.status == 451


def test_run_rule_script_error_raises(overlay):
    """A rule script that errors fails closed toward the policy -- run_rule
    propagates so a test can assert on it."""
    _write_script(overlay, "boom", (
        "def on_request():\n"
        "    fail('nope')\n"
    ))
    script = testkit.load_rule_script("boom")
    req = testkit.make_request("GET", "https://api.example.com/")
    with pytest.raises(Exception):
        testkit.run_rule(script, req)


def test_load_rule_script_rejects_credential_primitive(overlay):
    """A rule script may not reach secret()/crypto -- it fails to COMPILE under
    the kind='rule' profile, at load_rule_script time."""
    _write_script(overlay, "sneaky", (
        "def on_request():\n"
        "    req_set_header('X', secret('value'))\n"
    ))
    with pytest.raises(Exception):
        testkit.load_rule_script("sneaky")


# --------------------------------------------------------------------------
# make_response
# --------------------------------------------------------------------------


def test_make_response_strips_defaults_and_sets_status_body():
    req = testkit.make_request("GET", "https://api.example.com/x")
    flow = testkit.make_response(req, status=201, body='{"a":1}',
                                 headers={"Content-Type": "application/json"})
    # tresp's default 'header-response' + bogus 'content-length: 7' are gone.
    assert "header-response" not in flow.response.headers
    assert "content-length" not in flow.response.headers
    assert flow.response.status_code == 201
    assert flow.response.text == '{"a":1}'
    assert flow.response.headers["Content-Type"] == "application/json"
    assert flow.request is req                # the flow carries the request under test


# --------------------------------------------------------------------------
# Rule scripts: RESPONSE phase via run_rule_response
# --------------------------------------------------------------------------


def test_run_rule_response_rewrite(overlay):
    """A response-phase rewrite is non-terminal; the mutation lands on the
    response the caller holds."""
    _write_script(overlay, "scrubber", (
        "def on_response():\n"
        "    data = resp_json()\n"
        "    if data == None:\n"
        "        return\n"
        "    data['secret'] = None\n"
        "    resp_set_body(json_encode(data))\n"
    ))
    script = testkit.load_rule_script("scrubber")
    req = testkit.make_request("GET", "https://api.example.com/x")
    flow = testkit.make_response(req, status=200, body='{"secret":"leak","ok":1}')
    outcome = testkit.run_rule_response(script, flow)
    assert not outcome.terminal and outcome.response is None
    import json
    assert json.loads(flow.response.text) == {"secret": None, "ok": 1}


def test_run_rule_response_respond(overlay):
    _write_script(overlay, "resp-teapot", (
        "def on_response():\n"
        "    respond(418, 'no coffee')\n"
    ))
    script = testkit.load_rule_script("resp-teapot")
    req = testkit.make_request("GET", "https://api.example.com/coffee")
    flow = testkit.make_response(req, status=200, body="ok")
    outcome = testkit.run_rule_response(script, flow)
    assert outcome.terminal and not outcome.blocked
    assert outcome.response.kind == "respond"
    assert outcome.response.status == 418 and outcome.response.body == "no coffee"


def test_run_rule_response_block(overlay):
    _write_script(overlay, "resp-guard", (
        "def on_response():\n"
        "    if resp_status() == 500:\n"
        "        block(502)\n"
    ))
    script = testkit.load_rule_script("resp-guard")
    req = testkit.make_request("GET", "https://api.example.com/x")
    flow = testkit.make_response(req, status=500, body="boom")
    outcome = testkit.run_rule_response(script, flow)
    assert outcome.terminal and outcome.blocked and outcome.response.status == 502


def test_run_rule_response_reads_params(overlay):
    _write_script(overlay, "resp-param", (
        "def on_response():\n"
        "    if resp_status() == int(param('trip', '500')):\n"
        "        block(int(param('status', '502')))\n"
    ))
    script = testkit.load_rule_script("resp-param")
    req = testkit.make_request("GET", "https://api.example.com/x")
    flow = testkit.make_response(req, status=503, body="")
    outcome = testkit.run_rule_response(script, flow,
                                        params={"trip": "503", "status": "520"})
    assert outcome.blocked and outcome.response.status == 520


def test_run_rule_response_script_error_raises(overlay):
    """A response-phase rule script that errors fails closed toward the policy --
    run_rule_response propagates so a test can assert on it (unsanitized)."""
    _write_script(overlay, "resp-boom", (
        "def on_response():\n"
        "    fail('nope')\n"
    ))
    script = testkit.load_rule_script("resp-boom")
    req = testkit.make_request("GET", "https://api.example.com/x")
    flow = testkit.make_response(req, status=200, body="x")
    with pytest.raises(Exception):
        testkit.run_rule_response(script, flow)


# --------------------------------------------------------------------------
# Injector RESPONSE phase via run_response (the re-seal / mint path)
# --------------------------------------------------------------------------


def _write_reseal_injector(ov, name, script_name, params_toml):
    _write_injector(ov, name, (
        'scheme = "script"\n'
        f'script = "{script_name}"\n'
        'family = "substitute"\n'
        'slots = ["value"]\n'
        'location_kind = "body"\n'
        f"{params_toml}"
    ))


def test_run_response_records_mints_and_deterministic_placeholders(overlay):
    """The fake minter records each mint (value/ttl/hosts/header) and hands back
    deterministic placeholders (`minted-1`, `minted-2`, ...) per harness call, so
    a test can assert what the script minted and how it rewrote the body."""
    _write_reseal_injector(overlay, "resealer", "resealer", (
        "[params]\n"
        'api_hosts = ["a.com", "b.com"]\n'
        'reseal_header = "X-Custom"\n'
    ))
    _write_script(overlay, "resealer", (
        "def on_response():\n"
        "    data = resp_json()\n"
        "    mint('FIRST', 60)\n"                       # -> minted-1
        "    mint_into_json('access_token', data['access_token'], 120)\n"  # -> minted-2
        "    return True\n"
    ))
    import json
    kit = testkit.load_injector("resealer")
    req = testkit.make_request("POST", "https://oauth.example.com/token")
    flow = testkit.make_response(req, status=200,
                                 body=json.dumps({"access_token": "REAL-TOKEN"}))
    result = kit.run_response(flow, {"value": "CS"})

    assert result.handled is True
    assert [m.placeholder for m in result.mints] == ["minted-1", "minted-2"]
    first, second = result.mints
    assert first.value == "FIRST" and first.ttl == 60
    assert second.value == "REAL-TOKEN" and second.ttl == 120
    # api_hosts + header come from the manifest params, recorded as a tuple.
    for m in result.mints:
        assert m.api_hosts == ("a.com", "b.com") and m.header == "X-Custom"
    # mint_into_json rewrote the body field to the second placeholder.
    assert json.loads(flow.response.text)["access_token"] == "minted-2"
    assert "REAL-TOKEN" not in flow.response.text

    # A fresh call resets the deterministic counter (per-call minter).
    flow2 = testkit.make_response(req, status=200,
                                  body=json.dumps({"access_token": "T2"}))
    result2 = kit.run_response(flow2, {"value": "CS"})
    assert result2.mints[0].placeholder == "minted-1"


def test_run_response_validates_slots(overlay):
    """run_response enforces the same manifest slot set as run -- a wrong slot
    fails the harness, not the proxy."""
    _write_reseal_injector(overlay, "slotcheck", "slotcheck",
                           '[params]\napi_hosts = ["a.com"]\n')
    _write_script(overlay, "slotcheck", (
        "def on_response():\n"
        "    return False\n"
    ))
    kit = testkit.load_injector("slotcheck")
    req = testkit.make_request("POST", "https://oauth.example.com/token")
    flow = testkit.make_response(req, status=200, body="{}")
    with pytest.raises(ValueError, match="slot"):
        kit.run_response(flow, {"wrong": "x"})


def test_run_request_phase_is_minterless(overlay):
    """`run` (request phase) stays minter-less, as schemes.py documents: a scripted
    injector that calls mint() in on_request is fail-closed (mint() is response-
    phase-only), so run returns injected=False and nothing is minted -- the harness
    never wires a minter into the request phase."""
    _write_reseal_injector(overlay, "early-mint", "early-mint",
                           '[params]\napi_hosts = ["a.com"]\n')
    _write_script(overlay, "early-mint", (
        "def on_request():\n"
        "    mint('X', 60)\n"                # response-phase-only -> raises, swallowed
        "    return True\n"
    ))
    kit = testkit.load_injector("early-mint")
    req = testkit.make_request("POST", "https://oauth.example.com/token")
    result = kit.run(req, {"value": "CS"}, placeholder="PH")
    assert result.injected is False         # fail-closed: no minter in the request phase
