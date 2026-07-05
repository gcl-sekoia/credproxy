"""Tests for the builtin `oauth-reseal` scripted injector's RESPONSE phase.

Drives `on_response` (the `mint` path) through `testkit` -- which resolves the
`oauth-reseal` manifest AND `oauth-reseal.star` through the layered registry and
pairs them exactly like the push path, then wraps the response in a RECORDING
fake minter so the test asserts what the SCRIPT minted (value/ttl/hosts/header)
and how it rewrote the body, without dragging config in.

The builtin manifest declares `api_hosts = ["api.example.com"]` and
`reseal_header = "Authorization"`; the script mints the returned `access_token`
onto those hosts with `expires_in` as the TTL and rewrites the body so the
workspace receives the placeholder, not the real token.
"""
import json

import testkit

_SECRETS = {"value": "CLIENT_SECRET"}   # the manifest's single `value` slot


def _kit():
    return testkit.load_injector("oauth-reseal")


def _token_flow(body):
    req = testkit.make_request("POST", "https://oauth.example.com/token",
                               body="grant_type=client_credentials")
    return testkit.make_response(req, status=200, body=body)


def test_mints_token_and_rewrites_body():
    kit = _kit()
    flow = _token_flow(json.dumps({"access_token": "MINTED-TOKEN",
                                   "expires_in": 1200}))
    result = kit.run_response(flow, _SECRETS)

    assert result.handled is True
    [mint] = result.mints
    assert mint.value == "MINTED-TOKEN"            # the real token, minted
    assert mint.ttl == 1200                        # from expires_in
    assert mint.api_hosts == ("api.example.com",)  # from the manifest params
    assert mint.header == "Authorization"          # reseal_header default

    body = json.loads(flow.response.text)
    assert body["access_token"] == mint.placeholder   # placeholder, not the token
    assert body["access_token"] == "minted-1"         # deterministic
    assert "MINTED-TOKEN" not in flow.response.text    # real token scrubbed


def test_default_ttl_when_expires_in_absent():
    """The script falls back to `tok.get("expires_in", 3600)` when the response
    omits it."""
    kit = _kit()
    flow = _token_flow(json.dumps({"access_token": "T"}))
    result = kit.run_response(flow, _SECRETS)

    assert result.handled is True
    assert result.mints[0].ttl == 3600


def test_non_200_does_not_mint():
    kit = _kit()
    req = testkit.make_request("POST", "https://oauth.example.com/token",
                               body="grant_type=client_credentials")
    flow = testkit.make_response(req, status=401,
                                 body=json.dumps({"error": "invalid_client"}))
    result = kit.run_response(flow, _SECRETS)

    assert result.handled is False
    assert result.mints == []
    assert json.loads(flow.response.text) == {"error": "invalid_client"}   # untouched


def test_missing_access_token_does_not_mint():
    kit = _kit()
    flow = _token_flow(json.dumps({"token_type": "bearer"}))   # no access_token
    result = kit.run_response(flow, _SECRETS)

    assert result.handled is False
    assert result.mints == []


def test_run_response_enforces_slot_set():
    """run_response validates secrets against the manifest's declared slots, just
    like run -- a wrong slot fails the harness, not the proxy."""
    import pytest
    kit = _kit()
    flow = _token_flow(json.dumps({"access_token": "T", "expires_in": 60}))
    with pytest.raises(ValueError, match="slot"):
        kit.run_response(flow, {"wrong_slot": "x"})
