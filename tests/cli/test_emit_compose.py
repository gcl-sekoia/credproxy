"""Tests for #41 phase 3: `credproxy emit-compose` -- the Docker Compose
proxy-sidecar fragment.

ImageEnv is mocked (no docker daemon here) with DISTINCTIVE values, so a passing
assertion proves the port/paths are derived from the image's ENV contract rather
than hardcoded. The CLI is driven via test_porcelain._run.
"""
from __future__ import annotations

import yaml

from test_porcelain import _run


# Distinctive values (not the real defaults) so a match proves ENV-derivation.
_FAKE_ENV = type("FakeEnv", (), {
    "http_port": 45000, "tmpfs": "/custom/tmp",
    "token": "/custom/secrets-ro/auth.token", "source": "/opt/proxy",
    "mitmproxy_uid": 31337,
})()


def _patch_imageenv(monkeypatch, capture=None):
    def load(cls, image=None):
        if capture is not None:
            capture.append(image)
        return _FAKE_ENV
    monkeypatch.setattr("credproxy_cli.core.engine.imageenv.ImageEnv.load",
                        classmethod(load))


def _create_ws(workspaces_dir, name: str) -> None:
    from credproxy_cli.core.model.workspace import Workspace, ensure_token
    (workspaces_dir / f"{name}.toml").write_text('image = "x"\n')
    ensure_token(Workspace(name))


# ---- no-NAME form ------------------------------------------------------------


def test_emit_compose_no_name_derives_from_imageenv(xdg, monkeypatch):
    _patch_imageenv(monkeypatch)
    ec, out, err = _run(["emit-compose"])
    assert ec == 0
    doc = yaml.safe_load(out)
    proxy = doc["services"]["proxy"]
    # capability, tmpfs (mode 1777, from the ENV path), port (ephemeral loopback)
    assert proxy["cap_add"] == ["NET_ADMIN"]
    assert proxy["tmpfs"] == ["/custom/tmp:size=64k,mode=1777"]
    assert proxy["ports"] == ["127.0.0.1::45000"]
    # token bind mount, read-only, at the ENV token target
    assert proxy["volumes"] == [
        "${CREDPROXY_STATE:?set to the workspace state dir}/auth.token"
        ":/custom/secrets-ro/auth.token:ro"]


def test_emit_compose_no_name_uses_state_interpolation(xdg, monkeypatch):
    _patch_imageenv(monkeypatch)
    ec, out, err = _run(["emit-compose"])
    assert ec == 0
    # The unresolved token source is a ${CREDPROXY_STATE:?...} reference a
    # Compose .env can fill, with a comment on where that dir comes from.
    assert "${CREDPROXY_STATE:?set to the workspace state dir}/auth.token" in out
    assert "$XDG_STATE_HOME/credproxy/workspaces/NAME" in out


def test_emit_compose_healthcheck_probes_ready_via_python(xdg, monkeypatch):
    _patch_imageenv(monkeypatch)
    ec, out, err = _run(["emit-compose"])
    assert ec == 0
    hc = yaml.safe_load(out)["services"]["proxy"]["healthcheck"]
    assert hc["test"][:3] == ["CMD", "python", "-c"]
    probe = hc["test"][3]
    # python urllib one-liner against /ready (NOT /health) on the ENV port.
    assert "urllib.request" in probe
    assert "/ready" in probe and "/health" not in probe
    assert ":45000/ready" in probe
    # The probe itself uses python only (the image has no curl/wget).
    assert "curl" not in probe and "wget" not in probe


def test_emit_compose_workspace_service_lines(xdg, monkeypatch):
    _patch_imageenv(monkeypatch)
    ec, out, err = _run(["emit-compose"])
    assert ec == 0
    ws = yaml.safe_load(out)["services"]["workspace"]
    assert ws["network_mode"] == "service:proxy"
    assert ws["depends_on"] == {"proxy": {"condition": "service_healthy"}}


# ---- NAME form ---------------------------------------------------------------


def test_emit_compose_name_bakes_real_token_path(xdg, workspaces_dir, monkeypatch):
    _patch_imageenv(monkeypatch)
    _create_ws(workspaces_dir, "svc")
    from credproxy_cli.core.model.workspace import Workspace
    ec, out, err = _run(["emit-compose", "svc"])
    assert ec == 0
    real = str(Workspace("svc").token_path)
    vol = yaml.safe_load(out)["services"]["proxy"]["volumes"][0]
    assert vol == f"{real}:/custom/secrets-ro/auth.token:ro"
    # No .env indirection when the workspace is named.
    assert "CREDPROXY_STATE" not in out


def test_emit_compose_name_must_exist(xdg, monkeypatch):
    _patch_imageenv(monkeypatch)
    ec, out, err = _run(["emit-compose", "nope"])
    assert ec != 0
    assert "not found" in (out + err)


# ---- --image override --------------------------------------------------------


def test_emit_compose_image_override(xdg, monkeypatch):
    captured: list = []
    _patch_imageenv(monkeypatch, capture=captured)
    ec, out, err = _run(["emit-compose", "--image", "acme/proxy:1.2"])
    assert ec == 0
    # The tag is inspected AND baked into the emitted image: line.
    assert captured == ["acme/proxy:1.2"]
    assert yaml.safe_load(out)["services"]["proxy"]["image"] == "acme/proxy:1.2"


def test_emit_compose_default_image_tag(xdg, monkeypatch):
    _patch_imageenv(monkeypatch)
    from credproxy_cli.core.paths import IMAGE_TAG
    ec, out, err = _run(["emit-compose"])
    assert ec == 0
    assert yaml.safe_load(out)["services"]["proxy"]["image"] == IMAGE_TAG


# ---- --json refused ----------------------------------------------------------


def test_emit_compose_json_refused(xdg, monkeypatch):
    _patch_imageenv(monkeypatch)
    ec, out, err = _run(["--json", "emit-compose"])
    assert ec != 0
    assert "json" in (out + err).lower()


# ---- reserved name -----------------------------------------------------------


def test_emit_compose_is_reserved_name(xdg):
    from credproxy_cli.core.model.workspace import RESERVED_NAMES
    assert "emit-compose" in RESERVED_NAMES
    ec, out, err = _run(["workspace", "create", "emit-compose"])
    assert ec != 0
    assert "reserved" in (out + err).lower()
