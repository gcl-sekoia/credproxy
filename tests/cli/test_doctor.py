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


def test_doctor_missing_overlay_is_failing_check(xdg, tmp_path, monkeypatch):
    """Each env-listed overlay gets one index-qualified existence check; a
    configured-but-missing entry FAILS (resolution stays tolerant elsewhere)."""
    import os
    from credproxy_cli.core import doctor
    present = tmp_path / "ok"; present.mkdir()
    missing = tmp_path / "gone"          # never created
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH",
                       os.pathsep.join([str(present), str(missing)]))
    checks = {c.id: c for c in doctor._env_checks()}
    assert checks["overlay[0]:ok:exists"].ok
    assert not checks["overlay[1]:gone:exists"].ok
    assert checks["overlay[1]:gone:exists"].hint


def test_doctor_no_overlays_no_overlay_checks(xdg, monkeypatch):
    """An explicit opt-out (set-empty) configures zero overlays -> no checks."""
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", "")
    from credproxy_cli.core import doctor
    assert not any(c.id.startswith("overlay[") for c in doctor._env_checks())


def test_doctor_unset_env_no_overlay_checks(xdg, tmp_path, monkeypatch):
    """With CREDPROXY_OVERLAY_PATH unset, discovered container subdirs exist by
    construction -- doctor emits no overlay existence checks (only env-listed
    entries can be typo'd)."""
    monkeypatch.delenv("CREDPROXY_OVERLAY_PATH", raising=False)
    from credproxy_cli.core import doctor, paths
    container = tmp_path / "overlay"
    (container / "acme").mkdir(parents=True)   # a discovered overlay
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    assert not any(c.id.startswith("overlay[") for c in doctor._env_checks())


def test_doctor_missing_docker_reported(xdg, monkeypatch):
    from credproxy_cli.core import doctor
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    checks = doctor._env_checks()
    assert checks[0].id == "docker" and not checks[0].ok
    assert "not found" in checks[0].message and checks[0].hint


def test_doctor_image_staleness_hint_on_mismatch(monkeypatch):
    """A checkout that has drifted from the built image is a PASSING check with a
    rebuild hint -- the old image still works, so it's never a failure (#43)."""
    from credproxy_cli.core import doctor
    from credproxy_cli.core import docker as core_docker
    monkeypatch.setattr(doctor, "proxy_src_digest", lambda: "NEW")
    monkeypatch.setattr(core_docker, "inspect", lambda ref, fmt: "OLD")
    checks = doctor._image_staleness_check()
    assert len(checks) == 1
    c = checks[0]
    assert c.id == "image:fresh" and c.ok and c.hint
    assert "changed" in c.message


def test_doctor_image_staleness_ok_no_hint_on_match(monkeypatch):
    from credproxy_cli.core import doctor
    from credproxy_cli.core import docker as core_docker
    monkeypatch.setattr(doctor, "proxy_src_digest", lambda: "SAME")
    monkeypatch.setattr(core_docker, "inspect", lambda ref, fmt: "SAME")
    (c,) = doctor._image_staleness_check()
    assert c.ok and c.hint is None


def test_doctor_image_staleness_unknown_label(monkeypatch):
    """An image built before this change has no label ('<no value>') -> a passing
    check with a hint, distinct from a real mismatch."""
    from credproxy_cli.core import doctor
    from credproxy_cli.core import docker as core_docker
    monkeypatch.setattr(doctor, "proxy_src_digest", lambda: "NEW")
    monkeypatch.setattr(core_docker, "inspect", lambda ref, fmt: "<no value>")
    (c,) = doctor._image_staleness_check()
    assert c.ok and c.hint and "predates" in c.message


def test_doctor_image_staleness_skipped_without_checkout(monkeypatch):
    from credproxy_cli.core import doctor
    monkeypatch.setattr(doctor, "proxy_src_digest", lambda: None)
    assert doctor._image_staleness_check() == []


def _overlay_scripted(tmp_path, monkeypatch, script_body):
    """Set up an overlay with a scripted injector `sig` + its script, and return
    nothing (the overlay is on CREDPROXY_OVERLAY_PATH)."""
    ov = tmp_path / "ov"
    (ov / "scripts").mkdir(parents=True)
    (ov / "injectors").mkdir(parents=True)
    (ov / "injectors" / "sig.toml").write_text(
        'scheme = "script"\nscript = "sig"\nfamily = "sign"\n'
        'slots = ["k"]\nlocation_kind = "header"\n')
    (ov / "scripts" / "sig.star").write_text(script_body)
    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))


def test_doctor_compiles_referenced_script(xdg, workspaces_dir, tmp_path, monkeypatch):
    """When the Starlark runtime imports on-host (the CLI test venv), doctor
    upgrades the script-existence probe to a real compile paired with the
    manifest -- a valid script passes `ws:<name>:script:<script>`."""
    _overlay_scripted(tmp_path, monkeypatch,
                      "def on_request():\n"
                      "    req_set_header('X', secret('k'))\n"
                      "    return True\n")
    _write(workspaces_dir, "sc", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "sig"
        provider = "env"
        secret = { k = "TOK" }
        hosts = ["api.example.com"]
    """)
    ids = {c.id: c for c in doctor_run("sc")}
    assert "ws:sc:script:sig" in ids and ids["ws:sc:script:sig"].ok


def test_doctor_flags_broken_referenced_script(xdg, workspaces_dir, tmp_path, monkeypatch):
    _overlay_scripted(tmp_path, monkeypatch, "def on_request(\n    return True\n")
    _write(workspaces_dir, "sc", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "sig"
        provider = "env"
        secret = { k = "TOK" }
        hosts = ["api.example.com"]
    """)
    ids = {c.id: c for c in doctor_run("sc")}
    assert "ws:sc:script:sig" in ids and not ids["ws:sc:script:sig"].ok


def test_doctor_skips_compile_when_starlark_absent(xdg, workspaces_dir, tmp_path, monkeypatch):
    """No proxy runtime on-host -> the compile is skipped with a note, never a
    failure (doctor must not require the venv/docker for this)."""
    _overlay_scripted(tmp_path, monkeypatch,
                      "def on_request():\n    return True\n")
    from credproxy_cli.core import scriptcheck
    monkeypatch.setattr(scriptcheck, "starlark_importable", lambda: False)
    _write(workspaces_dir, "sc", """
        image = "x"
        [[binding]]
        name = "b"
        injector = "sig"
        provider = "env"
        secret = { k = "TOK" }
        hosts = ["api.example.com"]
    """)
    ids = {c.id: c for c in doctor_run("sc")}
    assert "ws:sc:scripts" in ids and ids["ws:sc:scripts"].ok
    assert "skipped" in ids["ws:sc:scripts"].message


def doctor_run(name):
    from credproxy_cli.core import doctor
    return doctor.run(name)


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


# ---- runc/rootless-podman sysfs preflight (#50) -----------------------------


def _patch_runtime(monkeypatch, *, rootless, runtime):
    monkeypatch.setattr("credproxy_cli.core.runtime.is_podman_rootless",
                        lambda: rootless)
    monkeypatch.setattr("credproxy_cli.core.runtime.oci_runtime",
                        lambda: runtime)


_KEEPID_TOML = """
    image = "x"
    user = "vscode"
    map_host_user = true
"""


def _runc_check(checks, name="ws1"):
    return next((c for c in checks if c.id == f"ws:{name}:runc-sysfs"), None)


def test_doctor_runc_keepid_flags_bad_combo(xdg, workspaces_dir, monkeypatch):
    """Rootless podman + runc + map_host_user (non-root user) -> a failing
    runc-sysfs check carrying both remedies."""
    import os
    if not hasattr(os, "getuid"):
        pytest.skip("no getuid on this platform")
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "ws1", _KEEPID_TOML)
    _patch_runtime(monkeypatch, rootless=True, runtime="runc")
    c = _runc_check(doctor.run("ws1"))
    assert c is not None and not c.ok
    assert 'runtime = "crun"' in c.hint and "map_host_user = false" in c.hint
    assert "troubleshooting" in c.hint


def test_doctor_runc_keepid_skipped_on_crun(xdg, workspaces_dir, monkeypatch):
    """crun handles the case -> no check emitted."""
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "ws1", _KEEPID_TOML)
    _patch_runtime(monkeypatch, rootless=True, runtime="crun")
    assert _runc_check(doctor.run("ws1")) is None


def test_doctor_runc_keepid_skipped_on_docker(xdg, workspaces_dir, monkeypatch):
    """Real Docker (not rootless podman, no runtime name) -> no check."""
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "ws1", _KEEPID_TOML)
    _patch_runtime(monkeypatch, rootless=False, runtime=None)
    assert _runc_check(doctor.run("ws1")) is None


def test_doctor_runc_keepid_skipped_when_rootful(xdg, workspaces_dir, monkeypatch):
    """Rootful podman on runc: no keep-id emitted -> no check."""
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "ws1", _KEEPID_TOML)
    _patch_runtime(monkeypatch, rootless=False, runtime="runc")
    assert _runc_check(doctor.run("ws1")) is None


def test_doctor_runc_keepid_skipped_when_map_host_user_off(xdg, workspaces_dir, monkeypatch):
    """map_host_user off -> no keep-id -> no check even on runc."""
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "ws1", '\nimage = "x"\nuser = "vscode"\n')
    _patch_runtime(monkeypatch, rootless=True, runtime="runc")
    assert _runc_check(doctor.run("ws1")) is None


def test_doctor_runc_keepid_skipped_with_run_flags_userns(xdg, workspaces_dir, monkeypatch):
    """A hand-rolled --userns in run_flags -> the user owns the mapping -> no
    check (credproxy's keep-id isn't in force)."""
    from credproxy_cli.core import doctor
    _write(workspaces_dir, "ws1", _KEEPID_TOML + '    run_flags = ["--userns=host"]\n')
    _patch_runtime(monkeypatch, rootless=True, runtime="runc")
    assert _runc_check(doctor.run("ws1")) is None
