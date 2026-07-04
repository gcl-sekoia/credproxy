"""Tests for `credproxy doctor` (core/doctor.py + the porcelain command)."""
from __future__ import annotations

import json
import textwrap

import pytest


def _write(workspaces_dir, name, body):
    (workspaces_dir / f"{name}.toml").write_text(textwrap.dedent(body))


def test_doctor_reports_all_binding_failures(xdg, workspaces_dir):
    # A binding with BOTH a bad injector and a bad host glob: doctor reports both
    # in one run (unlike start, which fails at the first). Env checks may vary by
    # host, so assert only the workspace-level checks.
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "bad", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "does-not-exist-zzz"
        provider = "env"
        secret = "TOK"
        hosts = ["*.com"]
        placeholder = "PH"
    """)
    fails = [c for c in doctor.run("bad") if not c.ok]
    assert any(c.id.endswith(":injector") for c in fails)    # bad injector
    assert any(c.id.endswith(":host[0]") for c in fails)     # bad host pattern


def test_doctor_valid_workspace_passes_its_checks(xdg, workspaces_dir):
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "good", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["api.github.com"]
        placeholder = "PH"
    """)
    ids = {c.id: c for c in doctor.run("good")}
    # Assert the SPECIFIC checks are present and ok -- so deleting a check layer
    # can't make this pass vacuously (an all-ok run with fewer checks).
    for cid in ("ws:good:config", "ws:good:bindings", "ws:good:rules"):
        assert cid in ids, f"missing check {cid}"
        assert ids[cid].ok, f"{cid} unexpectedly failed: {ids[cid].message}"


def test_doctor_catches_missing_required_binding_fields(xdg, workspaces_dir):
    """The layer-1 probes only fire on keys that are PRESENT; the aggregate
    `:bindings` check (real load_bindings) catches a binding missing required
    fields -- a config that `start` rejects but shallow probing green-lit (#30.1)."""
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "req", """
        image = "x"
        [[binding]]
        name = "b"
    """)
    checks = {c.id: c for c in doctor.run("req")}
    assert "ws:req:bindings" in checks and not checks["ws:req:bindings"].ok


def test_doctor_catches_broken_rule(xdg, workspaces_dir):
    """The `[[rule]]` layer is validated too, symmetric with bindings (#30.2)."""
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "rr", """
        image = "x"
        [[rule]]
        name = "r"
        action = "script"
        script = "no-such-script-zzz"
        hosts = ["api.github.com"]
    """)
    checks = {c.id: c for c in doctor.run("rr")}
    assert "ws:rr:rules" in checks and not checks["ws:rr:rules"].ok


def test_doctor_valid_rule_passes(xdg, workspaces_dir):
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "okr", """
        image = "x"
        [[rule]]
        name = "r"
        action = "block"
        hosts = ["api.github.com"]
    """)
    checks = {c.id: c for c in doctor.run("okr")}
    assert checks["ws:okr:rules"].ok


def test_doctor_check_ids_are_unique_and_index_qualified(xdg, workspaces_dir):
    """Two bad host globs in one binding emit two DISTINCT ids (#30.4) -- a
    --json consumer keying by id can't silently drop one."""
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "dup", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["*.com", "*.net"]
        placeholder = "PH"
    """)
    host_fails = [c for c in doctor.run("dup") if not c.ok and ":host[" in c.id]
    assert {c.id for c in host_fails} == {"ws:dup:binding[0]:host[0]",
                                          "ws:dup:binding[0]:host[1]"}


def test_doctor_invalid_name_rejected(xdg):
    """An explicit NAME goes through the same validation as every other command,
    so a traversal/reserved name is refused rather than reading outside the
    workspaces dir (#30.3)."""
    from credproxy_cli.core import doctor
    from credproxy_cli.core.errors import WorkspaceError
    with pytest.raises(WorkspaceError):
        doctor.run("../../etc/passwd")
    with pytest.raises(WorkspaceError):
        doctor.run("binding")  # a reserved command name


def test_doctor_missing_docker_reported(xdg, monkeypatch):
    from credproxy_cli.core import doctor
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    checks = doctor._env_checks()
    assert checks[0].id == "docker" and not checks[0].ok
    assert "not found" in checks[0].message and checks[0].hint


def test_doctor_missing_workspace_reported(xdg):
    from credproxy_cli.core import doctor
    fails = [c for c in doctor.run("nope") if not c.ok]
    assert any("no config file" in c.message for c in fails)


def test_doctor_malformed_toml_reported_not_crash(xdg, workspaces_dir):
    """A malformed TOML must be REPORTED (config/bindings/rules all fail), never
    crash doctor -- load_bindings/load_rules do a raw tomllib.loads whose
    TOMLDecodeError isn't a CredproxyError."""
    from credproxy_cli.core import doctor
    (workspaces_dir / "broke.toml").write_text('image = "x"\nthis is not = = toml\n')
    checks = doctor.run("broke")   # must not raise
    ids = {c.id: c for c in checks}
    assert not ids["ws:broke:config"].ok
    assert not ids["ws:broke:rules"].ok
