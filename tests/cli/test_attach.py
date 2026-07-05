"""Tests for #41 phase 2: attached workspaces, the `push`/`resolve` verbs, the
stateless push escape hatch, discovery, the loopback invariant, and locking.

Docker/HTTP are mocked as in test_lifecycle.py; the CLI is driven via
test_porcelain._run.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from test_porcelain import _run, _run_loose


# ---- helpers -----------------------------------------------------------------


def _attach_ws(workspaces_dir, name: str, body: str):
    from credproxy_cli.core.workspace import Workspace
    (workspaces_dir / f"{name}.toml").write_text(body)
    return Workspace(name)


def _managed_ws(workspaces_dir, name: str, body: str = 'image = "x"\n'):
    from credproxy_cli.core.workspace import Workspace
    (workspaces_dir / f"{name}.toml").write_text(body)
    return Workspace(name)


_FAKE_ENV = type("FakeEnv", (), {
    "http_port": 39998, "tmpfs": "/run/secrets",
    "token": "/run/secrets-ro/auth.token", "source": "/opt/proxy",
    "mitmproxy_uid": 31337,
})()


def _patch_imageenv(monkeypatch):
    monkeypatch.setattr("credproxy_cli.core.imageenv.ImageEnv.load",
                        classmethod(lambda cls, image=None: _FAKE_ENV))


# ---- attach parsing (config.load_config / _parse_attach) ---------------------


def test_attach_compose_project_normalizes(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    ws = _attach_ws(workspaces_dir, "cp", 'attach = { compose_project = "proj" }\n')
    cfg = load_config(ws)
    assert cfg["attach"] == {
        "discover": "com.docker.compose.project=proj,"
                    "com.docker.compose.service=proxy"}


def test_attach_container_selector(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    ws = _attach_ws(workspaces_dir, "c", 'attach = { container = "pbox" }\n')
    assert load_config(ws)["attach"] == {"container": "pbox"}


def test_attach_admin_url_selector_strips_slash(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    ws = _attach_ws(workspaces_dir, "a",
                    'attach = { admin_url = "http://127.0.0.1:5000/" }\n')
    assert load_config(ws)["attach"] == {"admin_url": "http://127.0.0.1:5000"}


def test_attach_discover_selector(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    ws = _attach_ws(workspaces_dir, "d",
                    'attach = { discover = "role=proxy,tier=dev" }\n')
    assert load_config(ws)["attach"] == {"discover": "role=proxy,tier=dev"}


def test_attach_exactly_one_selector(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    from credproxy_cli.core.errors import ConfigError
    ws = _attach_ws(workspaces_dir, "two",
                    'attach = { container = "a", admin_url = "http://127.0.0.1:1" }\n')
    with pytest.raises(ConfigError, match="exactly one"):
        load_config(ws)
    ws2 = _attach_ws(workspaces_dir, "zero", "attach = {}\n")
    with pytest.raises(ConfigError, match="exactly one"):
        load_config(ws2)


def test_attach_mutual_exclusion_names_offending_keys(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    from credproxy_cli.core.errors import ConfigError
    ws = _attach_ws(workspaces_dir, "mx",
                    'attach = { container = "p" }\nimage = "x"\nenv = { A = "b" }\n')
    with pytest.raises(ConfigError) as ei:
        load_config(ws)
    msg = str(ei.value)
    assert "mutually exclusive" in msg and "image" in msg and "env" in msg


def test_attach_bad_discover_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    from credproxy_cli.core.errors import ConfigError
    ws = _attach_ws(workspaces_dir, "bd", 'attach = { discover = "novalue" }\n')
    with pytest.raises(ConfigError, match="key=value"):
        load_config(ws)


def test_attach_non_loopback_admin_rejected_at_load(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    from credproxy_cli.core.errors import ConfigError
    ws = _attach_ws(workspaces_dir, "nl",
                    'attach = { admin_url = "http://8.8.8.8:9" }\n')
    with pytest.raises(ConfigError, match="loopback"):
        load_config(ws)


def test_attach_directory_and_bindings_still_valid(xdg, workspaces_dir):
    from credproxy_cli.core.config import load_config
    from credproxy_cli.core.bindings import load_bindings
    ws = _attach_ws(workspaces_dir, "ok", (
        'attach = { container = "p" }\n'
        'directory = "/abs/proj"\n'
        '[[binding]]\ninjector="bearer"\nprovider="env"\n'
        'secret="T"\nhosts=["api.github.com"]\n'))
    cfg = load_config(ws)
    assert cfg["directory"] == "/abs/proj"
    assert len(load_bindings(ws)) == 1


# ---- I8: loopback checker ----------------------------------------------------


def test_require_loopback_accepts_and_rejects(xdg):
    from credproxy_cli.core.push import require_loopback
    from credproxy_cli.core.errors import ConfigError
    require_loopback("http://127.0.0.1:9")
    require_loopback("http://localhost:9")
    require_loopback("http://127.5.5.5:9")     # anywhere in 127.0.0.0/8
    for bad in ("http://8.8.8.8:9", "http://example.com:9",
                "http://10.0.0.1:9", "ftp://127.0.0.1:9"):
        with pytest.raises(ConfigError):
            require_loopback(bad)


# ---- verb gating on attached workspaces --------------------------------------


_ATTACH_TOML = 'attach = { container = "p" }\n'


@pytest.mark.parametrize("argv", [
    ["workspace", "svc", "start"],
    ["workspace", "svc", "stop"],
    ["workspace", "svc", "recreate"],
    ["workspace", "svc", "logs"],
    ["workspace", "svc", "enter"],
    ["workspace", "svc", "exec", "--", "true"],
    ["workspace", "svc", "mount", "add", "--volume", "v", "--target", "/t"],
    ["dev", "reload", "svc"],
])
def test_gated_verbs_refuse_on_attached(xdg, workspaces_dir, argv):
    _attach_ws(workspaces_dir, "svc", _ATTACH_TOML)
    ec, out, err = _run(argv)
    assert ec != 0
    assert "is attached" in err


def test_delete_attached_removes_config_and_state_only(xdg, workspaces_dir,
                                                       monkeypatch):
    from credproxy_cli.core.workspace import Workspace, ensure_token
    ws = _attach_ws(workspaces_dir, "svc", _ATTACH_TOML)
    ensure_token(ws)
    assert ws.config_path.exists() and ws.token_path.exists()

    rm_calls = []
    monkeypatch.setattr("credproxy_cli.core.lifecycle.docker.docker_quiet",
                        lambda argv: rm_calls.append(argv))

    ec, out, err = _run(["workspace", "svc", "delete"])
    assert ec == 0
    assert not ws.config_path.exists()
    assert not ws.state_dir.exists()
    assert rm_calls == []          # no docker rm / volume rm for an attached ws


def test_apply_on_attached_is_push(xdg, workspaces_dir, monkeypatch):
    _attach_ws(workspaces_dir, "svc", _ATTACH_TOML)
    called = {}

    def fake_push(ws, notify=None, *, wait=False, timeout=120.0):
        called["ws"] = ws.name
        return "http://127.0.0.1:1234"
    monkeypatch.setattr("credproxy_cli.core.lifecycle.push_workspace", fake_push)

    ec, out, err = _run(["workspace", "svc", "apply"])
    assert ec == 0
    assert called["ws"] == "svc"
    assert "pushed" in out.lower()


def test_inspect_attached_shows_target(xdg, workspaces_dir, monkeypatch):
    _attach_ws(workspaces_dir, "svc", _ATTACH_TOML)
    # attach target resolution is best-effort; make it resolve to a fake URL.
    monkeypatch.setattr("credproxy_cli.core.lifecycle.resolve_admin_url",
                        lambda ws, notify=None: "http://127.0.0.1:5555")
    ec, out, err = _run(["workspace", "svc", "inspect"])
    assert ec == 0
    assert "attach" in out and "container" in out
    assert "http://127.0.0.1:5555" in out


def test_inspect_attached_json_carries_attach(xdg, workspaces_dir, monkeypatch):
    _attach_ws(workspaces_dir, "svc", _ATTACH_TOML)
    monkeypatch.setattr("credproxy_cli.core.lifecycle.resolve_admin_url",
                        lambda ws, notify=None: "http://127.0.0.1:5555")
    ec, out, err = _run(["--json", "workspace", "svc", "inspect"])
    assert ec == 0
    data = json.loads(out)
    assert data["attach"] == {"container": "p"}
    assert data["attach_target"] == "http://127.0.0.1:5555"


def test_rule_test_live_routes_through_target(xdg, workspaces_dir, monkeypatch):
    """`rule test --live` on an attached workspace resolves the attach target and
    POSTs to /admin/rule-test there (the same target resolution as push)."""
    from credproxy_cli.core.workspace import Workspace, ensure_token
    _attach_ws(workspaces_dir, "svc", (
        'attach = { admin_url = "http://127.0.0.1:7000" }\n'
        '[[rule]]\naction="block"\nhosts=["api.github.com"]\n'))
    ensure_token(Workspace("svc"))
    seen = {}

    def fake_rt(admin_url, token, method, url):
        seen["admin_url"] = admin_url
        return {"method": method, "host": "api.github.com", "path": "/x",
                "intercepted": True, "matches": []}
    monkeypatch.setattr("credproxy_cli.core.push.rule_test", fake_rt)

    ec, out, err = _run(["workspace", "svc", "rule", "test", "GET",
                         "https://api.github.com/x", "--live"])
    assert ec == 0
    assert seen["admin_url"] == "http://127.0.0.1:7000"


# ---- discovery (core_push.resolve_admin_url) ---------------------------------


def test_resolve_admin_url_admin_url_verbatim(xdg):
    from credproxy_cli.core import push
    assert push.resolve_admin_url({"admin_url": "http://127.0.0.1:9"}) \
        == "http://127.0.0.1:9"


def test_resolve_admin_url_container(xdg, monkeypatch):
    from credproxy_cli.core import push
    _patch_imageenv(monkeypatch)
    monkeypatch.setattr(push.docker, "resolve_host_port",
                        lambda name, port: 54321)
    assert push.resolve_admin_url({"container": "pbox"}) \
        == "http://127.0.0.1:54321"


def test_resolve_admin_url_discover_single(xdg, monkeypatch):
    from credproxy_cli.core import push
    _patch_imageenv(monkeypatch)
    monkeypatch.setattr(push.docker, "docker_output", lambda args: "onlyproxy\n")
    monkeypatch.setattr(push.docker, "resolve_host_port", lambda name, port: 40000)
    url = push.resolve_admin_url({"discover": "role=proxy"})
    assert url == "http://127.0.0.1:40000"


def test_resolve_admin_url_discover_no_match(xdg, monkeypatch):
    from credproxy_cli.core import push
    from credproxy_cli.core.errors import ConfigError
    _patch_imageenv(monkeypatch)
    monkeypatch.setattr(push.docker, "docker_output", lambda args: "\n")
    with pytest.raises(ConfigError, match="no running container"):
        push.resolve_admin_url({"discover": "role=proxy"})


def test_resolve_admin_url_discover_ambiguous(xdg, monkeypatch):
    from credproxy_cli.core import push
    from credproxy_cli.core.errors import ConfigError
    _patch_imageenv(monkeypatch)
    monkeypatch.setattr(push.docker, "docker_output", lambda args: "a\nb\n")
    with pytest.raises(ConfigError, match="ambiguous"):
        push.resolve_admin_url({"discover": "role=proxy"})


def test_discover_filters_one_per_pair(xdg, monkeypatch):
    from credproxy_cli.core import push
    _patch_imageenv(monkeypatch)
    seen = {}
    monkeypatch.setattr(push.docker, "docker_output",
                        lambda args: seen.setdefault("args", args) and "x\n" or "x\n")
    monkeypatch.setattr(push.docker, "resolve_host_port", lambda name, port: 1)
    push.resolve_admin_url({"discover": "k1=v1,k2=v2"})
    args = seen["args"]
    assert args.count("--filter") == 2
    assert "label=k1=v1" in args and "label=k2=v2" in args


# ---- G4: managed push with the proxy not running -----------------------------


def test_managed_push_proxy_stopped_errors_toward_start(xdg, workspaces_dir,
                                                        monkeypatch):
    from credproxy_cli.core import lifecycle
    from credproxy_cli.core.errors import WorkspaceError
    ws = _managed_ws(workspaces_dir, "m")
    _patch_imageenv(monkeypatch)
    monkeypatch.setattr(lifecycle.docker, "container_status", lambda name: None)
    with pytest.raises(WorkspaceError, match="start"):
        lifecycle.resolve_admin_url(ws)


# ---- push shares the engine (managed) ----------------------------------------


def test_push_managed_calls_engine_once_and_records(xdg, workspaces_dir,
                                                    monkeypatch):
    from credproxy_cli.core import lifecycle, push as core_push
    from credproxy_cli.core.workspace import ensure_token
    ws = _managed_ws(workspaces_dir, "m")
    ensure_token(ws)
    monkeypatch.setattr(lifecycle, "resolve_admin_url",
                        lambda ws, notify=None: "http://127.0.0.1:9")
    calls = []

    def fake_engine(admin_url, token, bindings, rules, fp=None, notify=None):
        calls.append(admin_url)
        return bindings, rules
    monkeypatch.setattr(core_push, "push_to_target", fake_engine)

    url = lifecycle.push_workspace(ws)
    assert calls == ["http://127.0.0.1:9"]
    assert ws.applied_bindings_path.exists()
    assert ws.applied_rules_path.exists()


# ---- push (attached) end-to-end via mocked discovery + POST ------------------


def test_push_attached_discovers_and_posts(xdg, workspaces_dir, monkeypatch):
    _attach_ws(workspaces_dir, "svc", (
        'attach = { discover = "role=proxy" }\n'
        '[[binding]]\ninjector="bearer"\nprovider="env"\n'
        'secret="GH"\nhosts=["api.github.com"]\n'))
    monkeypatch.setenv("GH", "secretval")
    from credproxy_cli.core.workspace import Workspace, ensure_token
    ensure_token(Workspace("svc"))
    _patch_imageenv(monkeypatch)
    from credproxy_cli.core import push as core_push
    monkeypatch.setattr(core_push.docker, "docker_output", lambda args: "theproxy\n")
    monkeypatch.setattr(core_push.docker, "resolve_host_port",
                        lambda name, port: 45678)

    posted = {}

    def fake_post(url, body, token):
        posted["url"] = url
        posted["body"] = json.loads(body)
        return 200, {"ok": True}
    monkeypatch.setattr(core_push, "_http_post_json", fake_post)

    ec, out, err = _run(["workspace", "svc", "push"])
    assert ec == 0
    assert posted["url"] == "http://127.0.0.1:45678/admin/config"
    body = posted["body"]
    assert len(body["bindings"]) == 1 and "rules" in body
    assert body["bindings"][0]["secret"] == {"value": "secretval"}


# ---- stateless push ----------------------------------------------------------


def _write_stateless_cfg(tmp_path) -> str:
    p = tmp_path / "cfg.toml"
    p.write_text(
        '[[binding]]\ninjector="bearer"\nprovider="env"\n'
        'secret="GH"\nhosts=["api.github.com"]\n'
        '[[rule]]\naction="block"\nhosts=["api.evil.com"]\n')
    return str(p)


def _write_token(tmp_path) -> str:
    p = tmp_path / "tok"
    p.write_text("tok-xyz\n")
    return str(p)


def test_stateless_push_happy_and_wire_parity(xdg, tmp_path, monkeypatch):
    monkeypatch.setenv("GH", "sekret")
    from credproxy_cli.core import push as core_push
    posted = {}

    def fake_post(url, body, token):
        posted["url"] = url
        posted["body"] = json.loads(body)
        posted["token"] = token
        return 200, {"ok": True}
    monkeypatch.setattr(core_push, "_http_post_json", fake_post)

    ec, out, err = _run(["push", "--admin", "http://127.0.0.1:8080",
                         "--config", _write_stateless_cfg(tmp_path),
                         "--token", _write_token(tmp_path)])
    assert ec == 0
    assert posted["url"] == "http://127.0.0.1:8080/admin/config"
    assert posted["token"] == "tok-xyz"
    body = posted["body"]
    # G3 wire parity: bindings AND rules both present, secrets resolved.
    assert len(body["bindings"]) == 1 and len(body["rules"]) == 1
    assert body["bindings"][0]["secret"] == {"value": "sekret"}
    assert body["rules"][0]["action"] == "block"


def test_stateless_push_missing_flag(xdg, tmp_path):
    ec, out, err = _run(["push", "--admin", "http://127.0.0.1:1",
                         "--config", _write_stateless_cfg(tmp_path)])
    assert ec != 0
    assert "--token" in (out + err)


def test_stateless_push_container_key_rejected(xdg, tmp_path):
    cfg = tmp_path / "bad.toml"
    cfg.write_text('image = "x"\n[[binding]]\ninjector="bearer"\n'
                   'provider="env"\nsecret="T"\nhosts=["h.io"]\n')
    ec, out, err = _run(["push", "--admin", "http://127.0.0.1:1",
                         "--config", str(cfg), "--token", _write_token(tmp_path)])
    assert ec != 0
    assert "image" in (out + err)


def test_stateless_push_non_loopback_refused(xdg, tmp_path):
    ec, out, err = _run(["push", "--admin", "http://8.8.8.8:8080",
                         "--config", _write_stateless_cfg(tmp_path),
                         "--token", _write_token(tmp_path)])
    assert ec != 0
    assert "loopback" in (out + err)


# ---- locking -----------------------------------------------------------------


def test_target_lock_waits_for_holder(xdg, monkeypatch):
    import fcntl
    from credproxy_cli.core import push
    from credproxy_cli.core.paths import state_dir
    url = "http://127.0.0.1:9"
    import hashlib
    locks = state_dir() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / (hashlib.sha256(url.encode()).hexdigest() + ".lock")
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)

    entered = threading.Event()
    done = threading.Event()

    def worker():
        with push.target_push_lock(url):
            entered.set()
        done.set()
    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.25)
    assert not entered.is_set()          # blocked by our held lock
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    assert done.wait(3)                  # proceeds once released
    t.join(1)


def test_two_sequential_stateless_pushes_both_send(xdg, tmp_path, monkeypatch):
    monkeypatch.setenv("GH", "v")
    from credproxy_cli.core import push as core_push
    posts = []
    monkeypatch.setattr(core_push, "_http_post_json",
                        lambda url, body, token: posts.append(url) or (200, {"ok": True}))
    cfg = _write_stateless_cfg(tmp_path)
    tok = _write_token(tmp_path)
    for _ in range(2):
        ec, out, err = _run(["push", "--admin", "http://127.0.0.1:1",
                             "--config", cfg, "--token", tok])
        assert ec == 0
    assert len(posts) == 2               # never skipped -- wait-then-repush


# ---- resolve -----------------------------------------------------------------


def _resolve_ws(workspaces_dir, name="r"):
    from credproxy_cli.core.workspace import Workspace
    (workspaces_dir / f"{name}.toml").write_text(
        'image = "x"\n[[binding]]\ninjector="bearer"\nprovider="env"\n'
        'secret="GH"\nhosts=["api.github.com"]\n')
    return Workspace(name)


def test_resolve_json_emits_wire(xdg, workspaces_dir, monkeypatch):
    monkeypatch.setenv("GH", "topsecret")
    _resolve_ws(workspaces_dir)
    ec, out, err = _run(["--json", "workspace", "r", "resolve", "--json"])
    assert ec == 0
    wire = json.loads(out)
    assert "bindings" in wire and "rules" in wire and "fingerprint" in wire
    assert wire["bindings"][0]["secret"] == {"value": "topsecret"}


def test_resolve_out_writes_0600(xdg, workspaces_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("GH", "s")
    ws = _resolve_ws(workspaces_dir)
    out_file = ws.state_dir / "config.json"
    ec, o, err = _run(["workspace", "r", "resolve", "--out", str(out_file)])
    assert ec == 0
    assert out_file.exists()
    assert (out_file.stat().st_mode & 0o777) == 0o600
    json.loads(out_file.read_text())     # valid JSON wire


def test_resolve_out_outside_state_warns(xdg, workspaces_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("GH", "s")
    _resolve_ws(workspaces_dir)
    outside = tmp_path / "leak.json"
    ec, o, err = _run(["workspace", "r", "resolve", "--out", str(outside)])
    assert ec == 0
    assert "outside the workspace state dir" in err
    assert (outside.stat().st_mode & 0o777) == 0o600


def test_resolve_requires_exactly_one_of_json_out(xdg, workspaces_dir, monkeypatch):
    monkeypatch.setenv("GH", "s")
    ws = _resolve_ws(workspaces_dir)
    ec, o, err = _run(["workspace", "r", "resolve"])
    assert ec != 0 and "exactly one" in (o + err)
    ec, o, err = _run(["--json", "workspace", "r", "resolve", "--json",
                       "--out", str(ws.state_dir / "c.json")])
    assert ec != 0 and "exactly one" in (o + err)


# ---- create --attach ---------------------------------------------------------


def test_create_attach_stamps_selector(xdg, workspaces_dir):
    ec, out, err = _run(["workspace", "create", "svc",
                         "--attach", "compose-project=myproj"])
    assert ec == 0
    text = (workspaces_dir / "svc.toml").read_text()
    assert 'attach = { compose_project = "myproj" }' in text
    # token is still created (it authenticates the push).
    from credproxy_cli.core.workspace import Workspace
    assert Workspace("svc").token_path.exists()


# ---- attach-aware follow-up hints (never name the gated `start`) --------------


def test_create_attach_hints_push_not_start(xdg, workspaces_dir):
    ec, out, err = _run(["workspace", "create", "svc",
                         "--attach", "container=p"])
    assert ec == 0
    assert "credproxy workspace svc push" in err
    assert "start" not in err            # `start` is gated on attached


def test_binding_add_on_attached_hints_push(xdg, workspaces_dir):
    _attach_ws(workspaces_dir, "svc", _ATTACH_TOML)
    ec, out, err = _run(["workspace", "svc", "binding", "add",
                         "--injector", "bearer", "--provider", "env",
                         "--secret", "T", "--host", "api.github.com"])
    assert ec == 0
    assert "credproxy workspace svc push" in err
    assert "start" not in err


def test_rule_add_on_attached_hints_push(xdg, workspaces_dir):
    _attach_ws(workspaces_dir, "svc", _ATTACH_TOML)
    ec, out, err = _run(["workspace", "svc", "rule", "add", "block",
                         "--host", "api.evil.com"])
    assert ec == 0
    assert "credproxy workspace svc push" in err
    assert "start" not in err


def test_rule_add_managed_still_hints_start(xdg, workspaces_dir):
    """The managed hint is unchanged: rule add on a managed workspace keeps
    pointing at `start` (or `apply`)."""
    _managed_ws(workspaces_dir, "m")
    ec, out, err = _run(["workspace", "m", "rule", "add", "block",
                         "--host", "api.evil.com"])
    assert ec == 0
    assert "credproxy workspace m start" in err


def test_create_attach_container_and_admin_url(xdg, workspaces_dir):
    _run(["workspace", "create", "c", "--attach", "container=pbox"])
    assert 'attach = { container = "pbox" }' in (workspaces_dir / "c.toml").read_text()
    _run(["workspace", "create", "a", "--attach", "admin-url=http://127.0.0.1:9"])
    assert 'admin_url = "http://127.0.0.1:9"' in (workspaces_dir / "a.toml").read_text()


def test_create_attach_bad_admin_url_rejected(xdg, workspaces_dir):
    ec, out, err = _run(["workspace", "create", "bad",
                         "--attach", "admin-url=http://example.com:9"])
    assert ec != 0 and "loopback" in (out + err)
    assert not (workspaces_dir / "bad.toml").exists()


def test_create_attach_template_from_overlay(xdg, tmp_path, monkeypatch,
                                             workspaces_dir):
    """The attach template rides resolve_singleton, so an overlay's copy wins."""
    overlay = tmp_path / "ov"
    overlay.mkdir()
    (overlay / "workspace.attach.template.toml").write_text(
        '# ACME attach {name}\nattach = { compose_project = "{name}" }\n# acme-mark\n')
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(overlay))
    ec, out, err = _run(["workspace", "create", "svc",
                         "--attach", "container=box"])
    assert ec == 0
    text = (workspaces_dir / "svc.toml").read_text()
    assert "acme-mark" in text and "ACME attach svc" in text
    assert 'attach = { container = "box" }' in text     # selector stamped over


# ---- --wait polls /health, NOT /ready ---------------------------------------


def _resp(status):
    class R:
        def __init__(self, s): self.status = s
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R(status)


def test_wait_for_health_polls_health_then_pushes(xdg, monkeypatch):
    from credproxy_cli.core import push
    monkeypatch.setattr(push.time, "sleep", lambda _s: None)
    urls = []
    seq = [_http_503(), _resp(200)]

    def fake_urlopen(url, timeout=2):
        urls.append(url)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(push.urllib.request, "urlopen", fake_urlopen)
    push.wait_for_health("http://127.0.0.1:9", timeout=5)
    assert all(u.endswith("/health") for u in urls)
    assert not any("/ready" in u for u in urls)     # I1: never /ready


def test_wait_for_health_timeout_clean_error(xdg, monkeypatch):
    from credproxy_cli.core import push
    from credproxy_cli.core.errors import ProxyError
    monkeypatch.setattr(push.time, "sleep", lambda _s: None)
    monkeypatch.setattr(push.urllib.request, "urlopen",
                        lambda url, timeout=2: (_ for _ in ()).throw(_http_503()))
    with pytest.raises(ProxyError, match="capture-ready"):
        push.wait_for_health("http://127.0.0.1:9", timeout=0.01)


def _http_503():
    body = io.BytesIO(json.dumps({"ok": False, "pending": ["ca-cert"]}).encode())
    return urllib.error.HTTPError("http://x/health", 503, "SU", {}, body)
