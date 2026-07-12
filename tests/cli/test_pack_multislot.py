"""Multi-slot pack credentials (#71): `pack add --secret SLOT=REF` parity
with `binding add`, so a pack can carry a multi-slot injector (sigv4 / ovh).

Covers the CLI parse (repeatable `--secret SLOT=REF` -> a `secret = { ... }`
table in the `[[pack]]` reference), the atomic slot-set validation (a wrong
slot set writes nothing), and the `pack list` + lock round-trip.
"""
from __future__ import annotations

import textwrap

from test_porcelain import _run


def _write_pack(name: str, toml: str):
    from credproxy_cli.core.paths import config_dir
    d = config_dir() / "packs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(textwrap.dedent(toml))


def _make_ws(name: str):
    from credproxy_cli.core.paths import workspaces_config_dir
    wd = workspaces_config_dir()
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{name}.toml").write_text('image = "python:3.12-slim"\n')


def _config_text(name: str) -> str:
    from credproxy_cli.core.paths import workspaces_config_dir
    return (workspaces_config_dir() / f"{name}.toml").read_text()


# A sign-family, MULTI-SLOT pack (sigv4: access_key_id / secret_access_key).
_AWS = """
    [placeholder]
    prefix = "aws_"
    length = 20
    charset = "hex"
    [[part]]
    suffix = "sts"
    injector = "sigv4"
    hosts = ["sts.amazonaws.com"]
    [[part]]
    suffix = "s3"
    injector = "sigv4"
    hosts = ["*.s3.amazonaws.com"]
"""


def test_pack_add_multislot_writes_secret_table(xdg):
    _write_pack("aws", _AWS)
    _make_ws("w")
    code, out, err = _run([
        "workspace", "w", "pack", "add", "aws", "--provider", "env",
        "--secret", "access_key_id=AWS_KEY",
        "--secret", "secret_access_key=AWS_SECRET",
    ])
    assert code == 0, out + err
    text = _config_text("w")
    # The reference records the resolved multi-slot table verbatim.
    assert "[[pack]]" in text
    assert "access_key_id" in text and "secret_access_key" in text
    # The lock snapshots the same table onto every part's binding.
    from credproxy_cli.core.model.lock import load_lock
    from credproxy_cli.core.model.workspace import Workspace
    lock = load_lock(Workspace("w"))
    for b in lock["packs"]["aws"]["expansion"]["bindings"]:
        assert b["secret"] == {"access_key_id": "AWS_KEY",
                              "secret_access_key": "AWS_SECRET"}


def test_pack_add_wrong_slot_set_fails_atomically(xdg):
    _write_pack("aws", _AWS)
    _make_ws("w")
    before = _config_text("w")
    code, out, err = _run([
        "workspace", "w", "pack", "add", "aws", "--provider", "env",
        "--secret", "access_key_id=AWS_KEY",   # missing secret_access_key
    ])
    assert code == 1
    assert "SLOT=REF" in (out + err) or "declare" in (out + err)
    # Nothing written: no `[[pack]]` block, no lock.
    assert _config_text("w") == before
    from credproxy_cli.core.model.workspace import Workspace
    assert not Workspace("w").lock_json_path.exists()


def test_pack_add_missing_secret_names_the_slots(xdg):
    _write_pack("aws", _AWS)
    _make_ws("w")
    code, out, err = _run(
        ["workspace", "w", "pack", "add", "aws", "--provider", "env"])
    assert code == 1
    # The remedy names the exact slots to supply.
    assert "access_key_id" in (out + err) and "secret_access_key" in (out + err)


def test_multislot_pack_never_prompts_secret_loose_tty(xdg, monkeypatch):
    """Multi-slot secret prompting is punted: even on loose+TTY, a multi-slot pack
    with no --secret falls closed to the explicit-flags error, never calling
    ask_secret (which can't express a slot map)."""
    from test_porcelain import _run_loose
    from credproxy_cli.porcelain import prompt as prompt_mod
    called = []
    monkeypatch.setattr(prompt_mod, "ask_secret",
                        lambda *a, **k: called.append(a) or "X")
    _write_pack("aws", _AWS)
    _make_ws("w")
    code, out, err = _run_loose(
        ["workspace", "w", "pack", "add", "aws", "--provider", "env"],
        stdin_text="", stdin_isatty=True)
    assert code == 1 and not called
    assert "SLOT=REF" in (out + err)
