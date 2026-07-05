"""Tests for the builtin OVH API scripted injector (ovh).

Drives the injector through `testkit` -- which resolves the `ovh` manifest AND
its `ovh.star` through the layered registry and builds the scheme exactly like
the push path -- then independently recomputes the expected SHA1 in Python to
verify all four injected headers. Because the harness pairs manifest+script, a
manifest that declared the wrong slots would fail this test rather than passing
against a config the proxy would reject.
"""
import hashlib

import testkit

_SECRETS = {"app_key": "AK", "app_secret": "AS", "consumer_key": "CK"}


def _kit():
    return testkit.load_injector("ovh")


def test_ovh_sets_signed_headers():
    kit = _kit()
    req = testkit.make_request("GET", "https://eu.api.ovh.com/1.0/me")

    assert kit.run(req, _SECRETS).injected is True

    ts = req.headers["X-Ovh-Timestamp"]
    # Empty body -> base has "++" between url and ts
    base = "AS+CK+GET+https://eu.api.ovh.com/1.0/me++" + ts
    expected_sig = "$1$" + hashlib.sha1(base.encode()).hexdigest()

    assert req.headers["X-Ovh-Signature"] == expected_sig
    assert req.headers["X-Ovh-Application"] == "AK"
    assert req.headers["X-Ovh-Consumer"] == "CK"


def test_ovh_signs_hostname_not_ip():
    """Regression for #7: in transparent mode flow.request.host is the
    destination IP; the script must sign the HOSTNAME URL (from pretty_host /
    the Host header), or OVH rejects with 'Invalid signature'."""
    kit = _kit()
    # Destination is the IP; the Host header is the real hostname.
    req = testkit.make_request("GET", "https://54.88.241.89/1.0/me",
                               headers={"Host": "eu.api.ovh.com"})

    assert kit.run(req, _SECRETS).injected is True

    ts = req.headers["X-Ovh-Timestamp"]
    # Signed over the hostname URL, not https://54.88.241.89/...
    base = "AS+CK+GET+https://eu.api.ovh.com/1.0/me++" + ts
    expected_sig = "$1$" + hashlib.sha1(base.encode()).hexdigest()
    assert req.headers["X-Ovh-Signature"] == expected_sig


def test_ovh_placeholder_present_signs_and_overwrites_app():
    """With a placeholder, the workspace presents it as X-Ovh-Application; the
    proxy signs and overwrites the four headers with the real app key."""
    kit = _kit()
    req = testkit.make_request("GET", "https://eu.api.ovh.com/1.0/me",
                               headers={"X-Ovh-Application": "PLACEHOLDER-APP"})

    assert kit.run(req, _SECRETS, placeholder="PLACEHOLDER-APP").injected is True
    assert req.headers["X-Ovh-Application"] == "AK"   # placeholder -> real app key
    assert "X-Ovh-Signature" in req.headers


def test_ovh_placeholder_mismatch_skips():
    """No / wrong X-Ovh-Application -> not our request; add no signature."""
    kit = _kit()
    req = testkit.make_request("GET", "https://eu.api.ovh.com/1.0/me")

    assert kit.run(req, _SECRETS, placeholder="PLACEHOLDER-APP").injected is False
    assert "X-Ovh-Signature" not in req.headers


def test_ovh_post_with_body():
    kit = _kit()
    body = '{"description":"test"}'
    req = testkit.make_request(
        "POST", "https://eu.api.ovh.com/1.0/domain/zone/example.com/record",
        headers={"Content-Type": "application/json"}, body=body)

    assert kit.run(req, _SECRETS).injected is True

    ts = req.headers["X-Ovh-Timestamp"]
    url = "https://eu.api.ovh.com/1.0/domain/zone/example.com/record"
    base = "AS+CK+POST+" + url + "+" + body + "+" + ts
    expected_sig = "$1$" + hashlib.sha1(base.encode()).hexdigest()

    assert req.headers["X-Ovh-Signature"] == expected_sig
    assert req.headers["X-Ovh-Application"] == "AK"
    assert req.headers["X-Ovh-Consumer"] == "CK"
