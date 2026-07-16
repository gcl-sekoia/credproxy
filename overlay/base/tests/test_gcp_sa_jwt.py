"""Tests for the base overlay's `gcp-sa-jwt` scripted injector.

Drives the injector through `testkit` (which resolves the `gcp-sa-jwt` manifest
+ `gcp-sa-jwt.star` and builds the scheme like the push path), then verifies the
GCP self-signed-JWT specifics the generic `jwt-bearer` builtin can't express:

  * the JOSE header carries `kid` = the SA's private_key_id (Google selects the
    verifying public key by it — the whole reason this exists),
  * `iss` == `sub` == the SA client_email,
  * `aud` is derived per request as `https://<host>/` (so one `*.googleapis.com`
    binding mints the right audience for every service),
  * an explicit `aud` param overrides the derivation,
  * the RS256 signature verifies against the key's public half.
"""
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import testkit

_KID = "abc123def456"
_EMAIL = "resource-explorer@my-project.iam.gserviceaccount.com"


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _make_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


def _run(pem, host="cloudasset.googleapis.com", params=None):
    kit = testkit.load_injector("gcp-sa-jwt")
    req = testkit.make_request("GET", f"https://{host}/v1/organizations/1/assets")
    secrets = {"private_key": pem, "key_id": _KID, "client_email": _EMAIL}
    result = kit.run(req, secrets, params=params)
    return result, req


def _parts(req):
    auth = req.headers["Authorization"]
    assert auth.startswith("Bearer ")
    h_b64, c_b64, sig_b64 = auth[len("Bearer "):].split(".")
    return h_b64, c_b64, sig_b64


def test_mints_a_verifiable_gcp_self_signed_jwt():
    key, pem = _make_key()

    before = int(time.time())
    result, req = _run(pem)
    after = int(time.time())

    assert result.injected is True
    h_b64, c_b64, sig_b64 = _parts(req)

    # Signature verifies over "header.claims" with the key's public half.
    signing_input = (h_b64 + "." + c_b64).encode()
    key.public_key().verify(_b64url_decode(sig_b64), signing_input,
                            padding.PKCS1v15(), hashes.SHA256())

    hdr = json.loads(_b64url_decode(h_b64))
    assert hdr["alg"] == "RS256" and hdr["typ"] == "JWT"
    # The distinguishing feature: kid in the JOSE header = the SA private_key_id.
    assert hdr["kid"] == _KID

    claims = json.loads(_b64url_decode(c_b64))
    assert claims["iss"] == _EMAIL and claims["sub"] == _EMAIL
    # aud derived from the target host.
    assert claims["aud"] == "https://cloudasset.googleapis.com/"
    assert claims["exp"] - claims["iat"] == 3600
    assert before <= claims["iat"] <= after


def test_aud_tracks_the_request_host():
    _, pem = _make_key()
    _, req = _run(pem, host="storage.googleapis.com")
    _, c_b64, _ = _parts(req)
    claims = json.loads(_b64url_decode(c_b64))
    assert claims["aud"] == "https://storage.googleapis.com/"


def test_explicit_aud_param_overrides_derivation():
    _, pem = _make_key()
    _, req = _run(pem, params={"aud": "https://oauth2.googleapis.com/token"})
    _, c_b64, _ = _parts(req)
    claims = json.loads(_b64url_decode(c_b64))
    assert claims["aud"] == "https://oauth2.googleapis.com/token"


def test_ttl_param_sets_lifetime():
    _, pem = _make_key()
    _, req = _run(pem, params={"ttl": "1800"})
    _, c_b64, _ = _parts(req)
    claims = json.loads(_b64url_decode(c_b64))
    assert claims["exp"] - claims["iat"] == 1800


def test_wrong_key_fails_verification():
    key, pem = _make_key()
    other, _ = _make_key()
    _, req = _run(pem)
    h_b64, c_b64, sig_b64 = _parts(req)
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        other.public_key().verify(_b64url_decode(sig_b64),
                                   (h_b64 + "." + c_b64).encode(),
                                   padding.PKCS1v15(), hashes.SHA256())
