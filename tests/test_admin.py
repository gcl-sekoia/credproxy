"""Tests for proxy/admin.py — bearer-auth middleware + /admin/health."""
import pytest

import admin


@pytest.fixture
def app():
    return admin.make_admin_app(auth_token="secret-token-abc")


async def test_health_with_correct_token(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get(
        "/admin/health",
        headers={"Authorization": "Bearer secret-token-abc"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True}


async def test_health_no_authorization_header(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/admin/health")
    assert resp.status == 401


async def test_health_wrong_scheme(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get(
        "/admin/health",
        headers={"Authorization": "Basic c2VjcmV0LXRva2VuLWFiYw=="},
    )
    assert resp.status == 401


async def test_health_empty_bearer(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get(
        "/admin/health",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status == 401


async def test_health_wrong_token(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get(
        "/admin/health",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status == 401


async def test_health_close_match_token(aiohttp_client, app):
    """Off-by-one-character token must still 401 (no prefix-match leak)."""
    client = await aiohttp_client(app)
    resp = await client.get(
        "/admin/health",
        headers={"Authorization": "Bearer secret-token-ab"},
    )
    assert resp.status == 401
