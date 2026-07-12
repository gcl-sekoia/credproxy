"""Tests for core/model/resolver.py: `resolve_workspace` -- the one boundary
between the config plane (intent TOML + lockfile) and everything else.

Placeholder identity is keyed by binding name and lives in the lock: a generated
placeholder is stable across resolves, regenerated when the lock is deleted or the
binding renamed, and an explicit `placeholder` in the TOML always wins and never
enters the lock. Resolution is side-effect-free -- it never writes."""
from __future__ import annotations

import textwrap

import pytest


def _write_ws(workspaces_dir, name, content):
    from credproxy_cli.core.model.workspace import Workspace
    (workspaces_dir / f"{name}.toml").write_text(textwrap.dedent(content))
    return Workspace(name)


_LOCK_MANAGED = """\
    image = "x"

    [[binding]]
    name     = "gh"
    injector = "bearer"
    provider = "env"
    secret   = "TOK"
    hosts    = ["api.github.com"]
"""


def test_resolve_generates_and_marks_dirty(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", _LOCK_MANAGED)
    r = resolve_workspace(ws)
    assert r.lock_dirty is True                       # a placeholder was minted
    b = r.bindings[0]
    assert b.placeholder and b.placeholder.startswith("credproxy_")
    assert r.lock["placeholders"] == {"gh": b.placeholder}
    # Side-effect-free: nothing was written to disk.
    assert not ws.lock_json_path.exists()


def test_resolve_is_side_effect_free_and_stable(xdg, workspaces_dir):
    """resolve -> persist -> resolve is a no-op: same placeholder, lock_dirty
    False, identical ResolvedWorkspace."""
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", _LOCK_MANAGED)

    from dataclasses import replace

    r1 = resolve_workspace(ws)
    assert r1.lock_dirty is True
    save_lock(ws, r1.lock)

    r2 = resolve_workspace(ws)
    assert r2.lock_dirty is False
    # Identical apart from the (expected) lock_dirty flip: same config, bindings,
    # rules, and lock content.
    assert r2 == replace(r1, lock_dirty=False)
    assert r2.bindings[0].placeholder == r1.bindings[0].placeholder


def test_delete_lock_regenerates(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", _LOCK_MANAGED)
    r1 = resolve_workspace(ws)
    save_lock(ws, r1.lock)
    ws.lock_json_path.unlink()
    r2 = resolve_workspace(ws)
    assert r2.lock_dirty is True
    assert r2.bindings[0].placeholder != r1.bindings[0].placeholder


def test_rename_binding_regenerates(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", _LOCK_MANAGED)
    r1 = resolve_workspace(ws)
    save_lock(ws, r1.lock)
    # Rename the binding: placeholder identity is keyed by name, so it changes.
    ws.config_path.write_text(
        ws.config_path.read_text().replace('name     = "gh"', 'name     = "gh2"'))
    r2 = resolve_workspace(ws)
    assert r2.lock_dirty is True
    assert set(r2.lock["placeholders"]) == {"gh2"}     # stale "gh" dropped
    assert r2.bindings[0].placeholder != r1.bindings[0].placeholder


def test_explicit_placeholder_wins_and_stays_out_of_lock(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", """\
        image = "x"

        [[binding]]
        name        = "gh"
        injector    = "bearer"
        provider    = "env"
        secret      = "TOK"
        hosts       = ["api.github.com"]
        placeholder = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    """)
    r = resolve_workspace(ws)
    assert r.bindings[0].placeholder == "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    assert r.lock["placeholders"] == {}                # explicit never enters lock
    assert r.lock_dirty is False


def test_stale_lock_entry_dropped(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import load_lock, save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", _LOCK_MANAGED)
    ws.ensure_state_dir()
    save_lock(ws, {"version": 1, "placeholders": {"gh": "ph_gh", "gone": "ph_x"}})
    r = resolve_workspace(ws)
    assert r.lock_dirty is True
    assert set(r.lock["placeholders"]) == {"gh"}       # "gone" pruned
    assert r.bindings[0].placeholder == "ph_gh"        # kept for the live name


def test_sign_scheme_has_no_placeholder(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", """\
        image = "x"

        [[binding]]
        name     = "aws"
        injector = "sigv4"
        provider = "env"
        secret   = { access_key_id = "AKID", secret_access_key = "SAK" }
        hosts    = ["s3.amazonaws.com"]
        [binding.params]
        region  = "us-east-1"
        service = "s3"
    """)
    r = resolve_workspace(ws)
    assert r.bindings[0].placeholder is None
    assert r.lock["placeholders"] == {}
    assert r.lock_dirty is False


def test_missing_binding_name_raises(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _write_ws(workspaces_dir, "w", """\
        image = "x"

        [[binding]]
        injector = "bearer"
        provider = "env"
        secret   = "TOK"
        hosts    = ["api.github.com"]
    """)
    with pytest.raises(ConfigError, match="missing a required `name`"):
        resolve_workspace(ws)
