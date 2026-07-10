"""Tests for the proxy-image ergonomics (#43 phase 0):

- the deterministic `proxy/` source digest (paths.proxy_src_digest),
- `dev build` stamping it as an image label,
- `ensure_proxy_image` on the start/recreate paths: offer-to-build on a missing
  image (loose) / precise remedy (strict), and the present-but-stale warning.
"""
from __future__ import annotations

import io
import json
import sys

import pytest


# ---- porcelain driver (mirrors test_porcelain.py) ----------------------------


def _run(argv, *, stdin_text=None, stdin_isatty=False):
    from credproxy_cli.porcelain import render

    render.set_format(False)
    old = (sys.argv[:], sys.stdout, sys.stderr, sys.stdin)
    sys.argv = ["credproxy"] + argv
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

    if stdin_text is not None:
        class FakeStdin:
            def __init__(self, text, tty):
                self._io = io.StringIO(text)
                self._tty = tty

            def isatty(self):
                return self._tty

            def read(self, *a, **kw):
                return self._io.read(*a, **kw)

            def readline(self, *a, **kw):
                return self._io.readline(*a, **kw)
        sys.stdin = FakeStdin(stdin_text, stdin_isatty)

    ec = 0
    try:
        from credproxy_cli.porcelain.cli import main
        main(loose_default=False)
    except SystemExit as e:
        ec = e.code if isinstance(e.code, int) else 1
    finally:
        stdout = sys.stdout.getvalue()
        stderr = sys.stderr.getvalue()
        sys.argv, sys.stdout, sys.stderr, sys.stdin = old
        render.set_format(False)
    return ec, stdout, stderr


def _run_loose(argv, **kw):
    return _run(["--loose"] + argv, **kw)


# ---- fixtures / stubs --------------------------------------------------------


def _prep_ws(workspaces_dir, name="myws"):
    (workspaces_dir / f"{name}.toml").write_text('image = "x"\n')


def _stub_build(monkeypatch):
    """Replace do_dev_build so no real docker build runs; record invocations.
    `ensure_proxy_image` (in common) calls it via the cmd_dev module attribute."""
    from credproxy_cli.porcelain import cmd_dev
    calls = []
    monkeypatch.setattr(cmd_dev, "do_dev_build", lambda ctx: calls.append(ctx))
    return calls


def _stub_start(monkeypatch):
    from credproxy_cli.porcelain import cmd_lifecycle
    calls = []
    monkeypatch.setattr(cmd_lifecycle.startup, "start_workspace",
                        lambda ws, notify=None, **kw: calls.append(ws.name))
    return calls


def _stub_inspect(monkeypatch, *, present, label=None):
    """Fake core_docker.inspect: the presence probe ({{.Id}}) and the label read.
    ensure_proxy_image (in common) reads it via common.core_docker."""
    from credproxy_cli.porcelain import common

    def fake(ref, fmt):
        if ".Id" in fmt:
            return "sha256:deadbeef" if present else None
        if "Config.Labels" in fmt:
            return label
        return None
    monkeypatch.setattr(common.core_docker, "inspect", fake)


def _stub_digest(monkeypatch, value):
    from credproxy_cli.core import paths
    monkeypatch.setattr(paths, "proxy_src_digest", lambda: value)


# ---- digest ------------------------------------------------------------------


def test_digest_deterministic_and_content_sensitive(tmp_path, monkeypatch):
    from credproxy_cli.core import paths
    proxy = tmp_path / "proxy"
    (proxy / "sub").mkdir(parents=True)
    (proxy / "a.py").write_text("print(1)\n")
    (proxy / "sub" / "b.txt").write_text("hello")
    monkeypatch.setattr(paths, "PROXY_DIR", proxy)

    d1 = paths.proxy_src_digest()
    d2 = paths.proxy_src_digest()
    assert d1 and d1 == d2                       # same tree -> same digest

    (proxy / "a.py").write_text("print(2)\n")
    assert paths.proxy_src_digest() != d1        # touch a file -> different digest


def test_digest_ignores_pycache_and_pyc(tmp_path, monkeypatch):
    from credproxy_cli.core import paths
    proxy = tmp_path / "proxy"
    (proxy / "sub").mkdir(parents=True)
    (proxy / "a.py").write_text("x")
    monkeypatch.setattr(paths, "PROXY_DIR", proxy)
    base = paths.proxy_src_digest()

    (proxy / "a.pyc").write_text("bytecode")            # a stray .pyc
    pc = proxy / "__pycache__"
    pc.mkdir()
    (pc / "a.cpython-312.pyc").write_text("more")       # a __pycache__ dir
    (proxy / "sub" / "__pycache__").mkdir()
    (proxy / "sub" / "__pycache__" / "z.pyc").write_text("z")
    assert paths.proxy_src_digest() == base             # none of it counts


def test_digest_none_without_checkout(tmp_path, monkeypatch):
    from credproxy_cli.core import paths
    monkeypatch.setattr(paths, "PROXY_DIR", tmp_path / "nope")
    assert paths.proxy_src_digest() is None


# ---- dev build stamps the label ----------------------------------------------


def test_dev_build_stamps_src_digest_label(tmp_path, monkeypatch):
    from credproxy_cli.core import paths
    from credproxy_cli.porcelain import cmd_dev
    from credproxy_cli.porcelain.common import Ctx

    proxy = tmp_path / "proxy"
    proxy.mkdir()
    (proxy / "Dockerfile").write_text("FROM scratch\n")
    # do_dev_build uses the module-global PROXY_DIR for is_dir()/str(); the digest
    # comes from paths.PROXY_DIR. Point both at the fake tree.
    monkeypatch.setattr(cmd_dev, "PROXY_DIR", proxy)
    monkeypatch.setattr(paths, "PROXY_DIR", proxy)

    calls = []
    monkeypatch.setattr(cmd_dev.core_docker, "docker",
                        lambda args, **kw: calls.append(args))
    cmd_dev.do_dev_build(Ctx(loose=False, as_json=False, assume_yes=False))

    args = calls[0]
    assert args[0] == "build" and "--label" in args
    label = args[args.index("--label") + 1]
    assert label == f"{paths.SRC_DIGEST_LABEL}={paths.proxy_src_digest()}"


# ---- missing image: build-or-remedy ------------------------------------------


def test_strict_missing_image_fails_with_exact_remedy(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=False)
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run(["workspace", "myws", "start"])
    assert ec != 0
    assert "not found; build it with: credproxy dev build" in err
    assert not built and not started


def test_strict_missing_image_json_error_shape(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=False)
    _stub_build(monkeypatch)
    ec, out, err = _run(["--json", "workspace", "myws", "start"])
    assert ec != 0
    obj = json.loads(out)
    assert obj["error"]["type"] == "ImageError"
    assert "credproxy dev build" in obj["error"]["message"]


def test_loose_missing_image_prompt_accept_builds_then_starts(
        xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=False)
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run_loose(["workspace", "myws", "start"],
                              stdin_text="y\n", stdin_isatty=True)
    assert ec == 0
    assert "build it now" in err
    assert len(built) == 1 and started == ["myws"]


def test_loose_missing_image_default_yes_on_empty_reply(
        xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=False)
    built = _stub_build(monkeypatch)
    _stub_start(monkeypatch)
    ec, out, err = _run_loose(["workspace", "myws", "start"],
                              stdin_text="\n", stdin_isatty=True)   # just Enter
    assert ec == 0 and len(built) == 1                              # default Yes


def test_loose_missing_image_prompt_decline_aborts(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=False)
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run_loose(["workspace", "myws", "start"],
                              stdin_text="n\n", stdin_isatty=True)
    assert ec != 0
    assert "credproxy dev build" in err
    assert not built and not started


def test_loose_missing_image_no_tty_fails_closed(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=False)
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run_loose(["workspace", "myws", "start"],
                              stdin_text="", stdin_isatty=False)
    assert ec != 0
    assert "credproxy dev build" in err
    assert "build it now" not in err          # no prompt was emitted
    assert not built and not started


def test_yes_missing_image_builds_without_prompt(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=False)
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    # loose + --yes + no TTY: builds unprompted (the safety-gate semantics).
    ec, out, err = _run_loose(["--yes", "workspace", "myws", "start"],
                              stdin_text="", stdin_isatty=False)
    assert ec == 0
    assert "build it now" not in err
    assert len(built) == 1 and started == ["myws"]


# ---- present-but-stale image -------------------------------------------------


def test_strict_stale_image_warns_and_proceeds(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=True, label="OLD")
    _stub_digest(monkeypatch, "NEW")
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run(["workspace", "myws", "start"])
    assert ec == 0
    assert "proxy source changed" in err and "credproxy dev build" in err
    assert not built and started == ["myws"]        # warns, uses the old image


def test_loose_stale_image_prompt_accept_rebuilds(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=True, label="OLD")
    _stub_digest(monkeypatch, "NEW")
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run_loose(["workspace", "myws", "start"],
                              stdin_text="y\n", stdin_isatty=True)
    assert ec == 0
    assert "rebuild now?" in err
    assert len(built) == 1 and started == ["myws"]


def test_loose_stale_image_prompt_decline_uses_old(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=True, label="OLD")
    _stub_digest(monkeypatch, "NEW")
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run_loose(["workspace", "myws", "start"],
                              stdin_text="\n", stdin_isatty=True)   # default No
    assert ec == 0
    assert not built and started == ["myws"]


def test_yes_stale_image_does_not_rebuild(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=True, label="OLD")
    _stub_digest(monkeypatch, "NEW")
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    # default-No prompt: --yes takes the default (No), never a surprise rebuild.
    ec, out, err = _run_loose(["--yes", "workspace", "myws", "start"],
                              stdin_text="", stdin_isatty=False)
    assert ec == 0
    assert not built and started == ["myws"]
    assert "proxy source changed" in err


def test_missing_label_warns_no_prompt_no_rebuild(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    # An image built before this change: label read returns docker's "<no value>".
    _stub_inspect(monkeypatch, present=True, label="<no value>")
    _stub_digest(monkeypatch, "NEW")
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run_loose(["workspace", "myws", "start"],
                              stdin_text="y\n", stdin_isatty=True)
    assert ec == 0
    assert "no source-digest label" in err
    assert "rebuild now?" not in err                # unknown -> no rebuild prompt
    assert not built and started == ["myws"]


def test_fresh_image_silent(xdg, workspaces_dir, monkeypatch):
    _prep_ws(workspaces_dir)
    _stub_inspect(monkeypatch, present=True, label="SAME")
    _stub_digest(monkeypatch, "SAME")
    built = _stub_build(monkeypatch)
    started = _stub_start(monkeypatch)
    ec, out, err = _run(["workspace", "myws", "start"])
    assert ec == 0
    assert "proxy source changed" not in err and "no source-digest label" not in err
    assert not built and started == ["myws"]
