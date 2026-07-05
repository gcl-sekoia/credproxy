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
