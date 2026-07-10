"""Tests for core/model/lock.py: the machine-owned workspace lockfile.

The lock is canonical JSON, atomic, and MUST round-trip unknown top-level keys
(issues #63/#65 add `presets`/`applied` sections an older code path must not
clobber)."""
from __future__ import annotations

import json


def _ws(workspaces_dir, name="w"):
    from credproxy_cli.core.model.workspace import Workspace
    (workspaces_dir / f"{name}.toml").write_text('image = "x"\n')
    return Workspace(name)


def test_load_lock_absent_is_empty(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import load_lock
    ws = _ws(workspaces_dir)
    assert load_lock(ws) == {"version": 1, "placeholders": {}}


def test_save_then_load_round_trip(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import load_lock, save_lock
    ws = _ws(workspaces_dir)
    lock = {"version": 1, "placeholders": {"a": "ph_a"}}
    save_lock(ws, lock)
    assert load_lock(ws) == lock


def test_save_is_canonical_json(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import save_lock
    ws = _ws(workspaces_dir)
    save_lock(ws, {"placeholders": {"b": "2", "a": "1"}, "version": 1})
    text = ws.lock_json_path.read_text()
    # sorted keys, 2-space indent, trailing newline.
    assert text.endswith("\n")
    assert text == json.dumps(
        {"placeholders": {"a": "1", "b": "2"}, "version": 1},
        sort_keys=True, indent=2) + "\n"


def test_unknown_top_level_keys_round_trip(xdg, workspaces_dir):
    """A future section (`presets`/`applied`) written by newer code survives a
    load+save cycle by older code that only touches `placeholders`."""
    from credproxy_cli.core.model.lock import load_lock, save_lock
    ws = _ws(workspaces_dir)
    ws.ensure_state_dir()
    ws.lock_json_path.write_text(json.dumps({
        "version": 1,
        "placeholders": {"a": "ph_a"},
        "presets": {"github": {"rev": "abc"}},   # unknown to this issue
        "applied": {"bindings": ["a"]},          # unknown to this issue
    }))
    lock = load_lock(ws)
    assert lock["presets"] == {"github": {"rev": "abc"}}
    # Mutate only placeholders, save, and confirm the unknown sections persist.
    lock["placeholders"]["a"] = "ph_a2"
    save_lock(ws, lock)
    again = load_lock(ws)
    assert again["placeholders"] == {"a": "ph_a2"}
    assert again["presets"] == {"github": {"rev": "abc"}}
    assert again["applied"] == {"bindings": ["a"]}


def test_lock_json_path_distinct_from_flock(xdg, workspaces_dir):
    """`lock_json_path` (state) is NOT `lock_path` (the lifecycle flock)."""
    ws = _ws(workspaces_dir)
    assert ws.lock_json_path.name == "lock.json"
    assert ws.lock_json_path != ws.lock_path
