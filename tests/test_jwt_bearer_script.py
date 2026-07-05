"""Tests for the builtin jwt-bearer scripted injector.

Drives the injector through `testkit` (which resolves the `jwt-bearer` manifest
+ `jwt-bearer.star` and builds the scheme like the push path), then verifies that
on_request mints an RS256-signed JWT, injects it as Authorization: Bearer <jwt>,
and that the JWT's header, claims, and signature are all well-formed and
verifiable with the corresponding public key.
"""
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import testkit

# The test uses its own iss/aud (distinct from the manifest defaults) to assert
# the params actually flow through, so every run() passes params explicitly.
_PARAMS = {"iss": "svc@example.com", "aud": "https://api.example.com", "ttl": "60"}


def _b64url_decode(s: str) -> bytes:
    """Decode an unpadded base64url string."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _make_key():
    """Generate a fresh 2048-bit RSA key pair; return (private_key, pem_str)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


def _run(pem: str, params: dict | None = None, placeholder=None,
         authorization=None):
    """Resolve the jwt-bearer injector and run it against a fresh request.
    Returns (result, req)."""
    kit = testkit.load_injector("jwt-bearer")
    headers = {"Authorization": authorization} if authorization is not None else None
    req = testkit.make_request("GET", "https://api.example.com/", headers=headers)
    result = kit.run(req, {"private_key": pem}, params=params or _PARAMS,
                     placeholder=placeholder)
    return result, req


# ---------------------------------------------------------------------------
# Core: mints a verifiable RS256 JWT
# ---------------------------------------------------------------------------

def test_jwt_bearer_mints_verifiable_token():
    key, pem = _make_key()

    before = int(time.time())
    result, req = _run(pem)
    after = int(time.time())

    assert result.injected is True

    auth = req.headers["Authorization"]
    assert auth.startswith("Bearer ")

    parts = auth[len("Bearer "):].split(".")
    assert len(parts) == 3, "JWT must have three dot-separated parts"
    h_b64, c_b64, sig_b64 = parts

    # Verify the RS256 signature over "header.claims"
    signing_input = (h_b64 + "." + c_b64).encode()
    sig = _b64url_decode(sig_b64)
    # Raises InvalidSignature on failure -- that IS the test assertion.
    key.public_key().verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())

    # Check decoded header
    hdr = json.loads(_b64url_decode(h_b64))
    assert hdr["alg"] == "RS256"
    assert hdr["typ"] == "JWT"

    # Check decoded claims
    claims = json.loads(_b64url_decode(c_b64))
    assert claims["iss"] == "svc@example.com"
    assert claims["aud"] == "https://api.example.com"
    assert claims["exp"] - claims["iat"] == 60
    assert before <= claims["iat"] <= after


# ---------------------------------------------------------------------------
# TTL: custom lifetime is reflected in exp-iat delta
# ---------------------------------------------------------------------------

def test_jwt_bearer_custom_ttl():
    key, pem = _make_key()
    _, req = _run(pem, params={
        "iss": "svc@example.com",
        "aud": "https://api.example.com",
        "ttl": "7200",
    })
    _, c_b64, _ = req.headers["Authorization"][len("Bearer "):].split(".")
    claims = json.loads(_b64url_decode(c_b64))
    assert claims["exp"] - claims["iat"] == 7200


# ---------------------------------------------------------------------------
# sub claim: present only when non-empty
# ---------------------------------------------------------------------------

def test_jwt_bearer_sub_included_when_set():
    key, pem = _make_key()
    _, req = _run(pem, params={
        "iss": "svc@example.com",
        "aud": "https://api.example.com",
        "ttl": "60",
        "sub": "user@example.com",
    })
    _, c_b64, _ = req.headers["Authorization"][len("Bearer "):].split(".")
    claims = json.loads(_b64url_decode(c_b64))
    assert claims["sub"] == "user@example.com"


def test_jwt_bearer_sub_absent_when_empty():
    key, pem = _make_key()
    _, req = _run(pem, params={
        "iss": "svc@example.com",
        "aud": "https://api.example.com",
        "ttl": "60",
        "sub": "",
    })
    _, c_b64, _ = req.headers["Authorization"][len("Bearer "):].split(".")
    claims = json.loads(_b64url_decode(c_b64))
    assert "sub" not in claims


def test_jwt_bearer_sub_absent_by_default():
    """When sub param is not provided at all, it should not appear in claims."""
    key, pem = _make_key()
    # No "sub" key in params at all
    _, req = _run(pem, params={
        "iss": "svc@example.com",
        "aud": "https://api.example.com",
        "ttl": "60",
    })
    _, c_b64, _ = req.headers["Authorization"][len("Bearer "):].split(".")
    claims = json.loads(_b64url_decode(c_b64))
    assert "sub" not in claims


# ---------------------------------------------------------------------------
# Each call mints a fresh JWT
# ---------------------------------------------------------------------------

def test_jwt_bearer_each_call_is_fresh():
    """Two successive calls both produce well-formed Bearer tokens (iat may match
    within one second, so we only assert both parse)."""
    key, pem = _make_key()
    _, req1 = _run(pem)
    _, req2 = _run(pem)
    for auth in (req1.headers["Authorization"], req2.headers["Authorization"]):
        assert auth.startswith("Bearer ")
        assert len(auth[len("Bearer "):].split(".")) == 3


# ---------------------------------------------------------------------------
# Placeholder gate: when the binding declares a placeholder, mint ONLY for
# requests that carry it (per-request opt-in + multi-identity disambiguation).
# ---------------------------------------------------------------------------

def test_jwt_bearer_placeholder_present_mints():
    """Workspace presents the placeholder bearer -> real JWT is minted in place."""
    key, pem = _make_key()
    result, req = _run(pem, placeholder="PLACEHOLDER-TOKEN",
                       authorization="Bearer PLACEHOLDER-TOKEN")
    assert result.injected is True
    auth = req.headers["Authorization"]
    assert auth.startswith("Bearer ")
    assert auth != "Bearer PLACEHOLDER-TOKEN"          # placeholder replaced
    assert len(auth[len("Bearer "):].split(".")) == 3  # by a real 3-part JWT


def test_jwt_bearer_placeholder_absent_skips():
    """No Authorization header -> not our request; leave it untouched."""
    key, pem = _make_key()
    result, req = _run(pem, placeholder="PLACEHOLDER-TOKEN")
    assert result.injected is False
    assert "Authorization" not in req.headers


def test_jwt_bearer_placeholder_mismatch_skips():
    """A different token (another identity) -> not ours; leave it untouched."""
    key, pem = _make_key()
    result, req = _run(pem, placeholder="PLACEHOLDER-TOKEN",
                       authorization="Bearer SOMEONE-ELSES-TOKEN")
    assert result.injected is False
    assert req.headers["Authorization"] == "Bearer SOMEONE-ELSES-TOKEN"


# ---------------------------------------------------------------------------
# Signature is invalid when verified with a DIFFERENT key (sanity check)
# ---------------------------------------------------------------------------

def test_jwt_bearer_wrong_key_fails_verification():
    key, pem = _make_key()
    other_key, _ = _make_key()   # unrelated key pair
    _, req = _run(pem)

    h_b64, c_b64, sig_b64 = req.headers["Authorization"][len("Bearer "):].split(".")
    sig = _b64url_decode(sig_b64)
    signing_input = (h_b64 + "." + c_b64).encode()

    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        other_key.public_key().verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
