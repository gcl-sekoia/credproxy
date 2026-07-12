"""Tests for core/model/lock.py: the machine-owned workspace lockfile.

The lock is canonical JSON, atomic, and MUST round-trip unknown top-level keys
(issues #63/#65 add `packs`/`applied` sections an older code path must not
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
    """A future section (`packs`/`applied`) written by newer code survives a
    load+save cycle by older code that only touches `placeholders`."""
    from credproxy_cli.core.model.lock import load_lock, save_lock
    ws = _ws(workspaces_dir)
    ws.ensure_state_dir()
    ws.lock_json_path.write_text(json.dumps({
        "version": 1,
        "placeholders": {"a": "ph_a"},
        "packs": {"github": {"rev": "abc"}},   # unknown to this issue
        "applied": {"bindings": ["a"]},          # unknown to this issue
    }))
    lock = load_lock(ws)
    assert lock["packs"] == {"github": {"rev": "abc"}}
    # Mutate only placeholders, save, and confirm the unknown sections persist.
    lock["placeholders"]["a"] = "ph_a2"
    save_lock(ws, lock)
    again = load_lock(ws)
    assert again["placeholders"] == {"a": "ph_a2"}
    assert again["packs"] == {"github": {"rev": "abc"}}
    assert again["applied"] == {"bindings": ["a"]}


def test_lock_json_path_distinct_from_flock(xdg, workspaces_dir):
    """`lock_json_path` (state) is NOT `lock_path` (the lifecycle flock)."""
    ws = _ws(workspaces_dir)
    assert ws.lock_json_path.name == "lock.json"
    assert ws.lock_json_path != ws.lock_path


def test_update_sets_only_its_section(xdg, workspaces_dir):
    """`update(section, value)` replaces just that section, backfilling defaults
    without dropping anything else."""
    from credproxy_cli.core.model.lock import load_lock, update
    ws = _ws(workspaces_dir)
    update(ws, "applied", {"config_generation": 3})
    lock = load_lock(ws)
    assert lock["applied"] == {"config_generation": 3}
    assert lock["placeholders"] == {}          # backfilled
    assert lock["version"] == 1


def test_applied_write_preserves_placeholders_and_packs_byte_for_byte(
        xdg, workspaces_dir):
    """The #65 acceptance invariant, direction 1: an engine `update("applied", …)`
    preserves the resolver's `placeholders`/`packs` byte-for-byte."""
    from credproxy_cli.core.model.lock import save_lock, update
    ws = _ws(workspaces_dir)
    # Resolver persists placeholders + packs.
    save_lock(ws, {
        "version": 1,
        "placeholders": {"gh": "ghp_x"},
        "packs": {"github": {"rev": "abc123", "sha": "def456"}},
    })
    before = ws.lock_json_path.read_text()
    # Engine records applied state.
    update(ws, "applied", {"spec": {"image": "x"}, "config_generation": 2})
    after = ws.lock_json_path.read_text()
    # The placeholders/packs lines are unchanged; only `applied` was added.
    import json
    a = json.loads(after)
    assert a["placeholders"] == {"gh": "ghp_x"}
    assert a["packs"] == {"github": {"rev": "abc123", "sha": "def456"}}
    assert a["applied"] == {"spec": {"image": "x"}, "config_generation": 2}
    # And the pre-existing sections are byte-for-byte identical in the re-serialized
    # canonical JSON (deleting `applied` from `after` reproduces `before`).
    del a["applied"]
    assert json.dumps(a, sort_keys=True, indent=2) + "\n" == before


def test_resolver_save_preserves_applied_byte_for_byte(xdg, workspaces_dir):
    """The #65 acceptance invariant, direction 2: a resolver `save_lock` that
    only rewrites `placeholders` preserves the engine's `applied` section
    byte-for-byte."""
    from credproxy_cli.core.model.lock import load_lock, save_lock, update
    import json
    ws = _ws(workspaces_dir)
    update(ws, "applied", {"bindings": [{"name": "gh"}], "config_generation": 5})
    applied_before = load_lock(ws)["applied"]
    # Resolver re-saves after minting a placeholder (round-tripping unknown keys).
    lock = load_lock(ws)
    lock["placeholders"]["gh"] = "ghp_new"
    save_lock(ws, lock)
    reloaded = load_lock(ws)
    assert reloaded["placeholders"] == {"gh": "ghp_new"}
    assert reloaded["applied"] == applied_before   # untouched
    assert json.dumps(reloaded["applied"], sort_keys=True) == \
        json.dumps(applied_before, sort_keys=True)
