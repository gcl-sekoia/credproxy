"""`credproxy script check` -- compile .star scripts before push.

Runs against a temp overlay of scripts (a referenced scripted-injector script, an
unreferenced neutral script, an unreferenced rule-only script, a syntax-error
script). These execute the ON-HOST compile path -- the CLI test venv carries the
Starlark runtime -- so the real classification + compile is exercised; the image
fallback is covered by mocked tests in test_dev_test_overlays / by inspection.
"""
from __future__ import annotations

import json

import pytest

from test_porcelain import _run


@pytest.fixture
def script_overlay(tmp_path, monkeypatch, xdg):
    ov = tmp_path / "ov"
    (ov / "scripts").mkdir(parents=True)
    (ov / "injectors").mkdir(parents=True)

    # Referenced by a scripted-injector manifest (family=sign, slots=["k"]).
    (ov / "injectors" / "refd.toml").write_text(
        'scheme = "script"\nscript = "refd"\nfamily = "sign"\n'
        'slots = ["k"]\nlocation_kind = "header"\n')
    (ov / "scripts" / "refd.star").write_text(
        "def on_request():\n"
        "    req_set_header('X-Key', secret('k'))\n"   # uses the declared slot
        "    return True\n")

    # Unreferenced, compiles under BOTH profiles (only common primitives).
    (ov / "scripts" / "neutral.star").write_text(
        "def on_request():\n"
        "    req_set_header('X-Seen', 'yes')\n"
        "    return True\n")

    # Unreferenced, rule-only: block() exists only in the rule profile.
    (ov / "scripts" / "ruleonly.star").write_text(
        "def on_request():\n"
        "    block(403)\n")

    # Syntax error: compiles under neither profile.
    (ov / "scripts" / "bad.star").write_text(
        "def on_request(\n    return True\n")

    monkeypatch.setenv("CREDPROXY_OVERLAY_PATH", str(ov))
    return ov


def test_syntax_error_fails(script_overlay):
    code, out, err = _run(["script", "check", "bad"])
    assert code == 1
    assert "FAIL" in out
    assert "bad" in out


def test_injector_referenced_pairs_with_manifest(script_overlay):
    code, out, err = _run(["--json", "script", "check", "refd"])
    assert code == 0
    (row,) = json.loads(out)
    assert row["name"] == "refd"
    assert row["ok"] is True
    # Referenced -> compiled under the INJECTOR profile only (its manifest slots).
    assert row["profiles"] == ["inject"]
    assert row["origin"] == "overlay:ov"


def test_unreferenced_tries_both_profiles(script_overlay):
    code, out, err = _run(["--json", "script", "check", "neutral"])
    assert code == 0
    (row,) = json.loads(out)
    assert row["ok"] is True
    # A neutral script compiles under both the injector and rule profiles.
    assert set(row["profiles"]) == {"inject", "rule"}


def test_unreferenced_rule_only_falls_back_to_rule(script_overlay):
    code, out, err = _run(["--json", "script", "check", "ruleonly"])
    assert code == 0
    (row,) = json.loads(out)
    assert row["ok"] is True
    # block() is rule-only, so the injector profile fails and rule wins.
    assert row["profiles"] == ["rule"]


def test_json_shape(script_overlay):
    code, out, err = _run(["--json", "script", "check", "refd"])
    (row,) = json.loads(out)
    for key in ("name", "origin", "ok", "error"):
        assert key in row
    assert row["error"] is None


def test_json_error_populated_on_failure(script_overlay):
    code, out, err = _run(["--json", "script", "check", "bad"])
    assert code == 1
    (row,) = json.loads(out)
    assert row["ok"] is False
    assert row["error"] and "bad.star" in row["error"]


def test_check_all_nonzero_when_any_fails(script_overlay):
    # No NAME -> checks every resolvable script (overlay + builtins); the overlay's
    # `bad` makes the whole run non-zero.
    code, out, err = _run(["--json", "script", "check"])
    assert code == 1
    rows = {r["name"]: r for r in json.loads(out)}
    assert rows["bad"]["ok"] is False
    assert rows["refd"]["ok"] is True
    # builtins are included in a check-all
    assert "ovh" in rows and rows["ovh"]["ok"] is True


def test_unknown_script_name(script_overlay):
    code, out, err = _run(["script", "check", "does-not-exist"])
    assert code != 0
