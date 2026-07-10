"""The machine-owned workspace lockfile (`lock.json`).

The workspace TOML is the hand-authored intent file (comments sacred); every
value credproxy GENERATES lives here instead, so the CLI never has to rewrite
inside the user's file. `placeholders` (#62) and `presets` (#63) are written by
the MODEL-plane resolver; `applied` (#65 -- last-applied container spec, pushed
bindings/rules metadata, the pushed `config_generation`, and the setup-completed
container id) is written by the ENGINE plane at push/start/setup success points.

The file is canonical JSON (`sort_keys=True, indent=2` + trailing newline),
written atomically, never hand-edited, and safe to regenerate. `load_lock`
preserves EVERY top-level key it reads -- including ones this version doesn't
know about -- so any section round-trips unclobbered through a code path that
only touches another. That round-trip is what lets the two writers coexist: the
resolver's `save_lock` preserves `applied`, and an engine `update("applied", ...)`
preserves `placeholders`/`presets`, provided both writes happen under the SAME
held workspace flock (so neither reads a stale file).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..paths import atomic_write_text

if TYPE_CHECKING:
    from .workspace import Workspace

LOCK_VERSION = 1


def _empty_lock() -> dict:
    return {"version": LOCK_VERSION, "placeholders": {}}


def load_lock(ws: "Workspace") -> dict:
    """Read the workspace's `lock.json`, or return a fresh empty lock if absent.

    The returned dict is preserved verbatim (all keys, known and unknown) so a
    caller that only touches `placeholders` leaves any other section intact when
    it saves. A missing/blank file yields `{"version": 1, "placeholders": {}}`;
    a present file that omits `placeholders`/`version` is backfilled with the
    defaults WITHOUT dropping its other keys."""
    path = ws.lock_json_path
    if not path.exists():
        return _empty_lock()
    text = path.read_text()
    if not text.strip():
        return _empty_lock()
    lock = json.loads(text)
    if not isinstance(lock, dict):
        raise ValueError(f"{path}: lock file must be a JSON object")
    lock.setdefault("version", LOCK_VERSION)
    ph = lock.setdefault("placeholders", {})
    if not isinstance(ph, dict):
        raise ValueError(f"{path}: lock `placeholders` must be a JSON object")
    # Placeholder values are always strings; a non-string would flow straight into
    # binding validation as a corrupt placeholder. Fail cleanly here instead.
    if not all(isinstance(v, str) for v in ph.values()):
        raise ValueError(
            f"{path}: lock file corrupt (placeholder values must be strings) -- "
            f"delete it to regenerate")
    return lock


def save_lock(ws: "Workspace", lock: dict) -> None:
    """Write `lock` as canonical JSON (sorted keys, 2-space indent, trailing
    newline) atomically. Persists whatever dict it is given -- including unknown
    top-level sections -- so it is the exact round-trip partner of `load_lock`."""
    ws.ensure_state_dir()
    atomic_write_text(
        ws.lock_json_path,
        json.dumps(lock, sort_keys=True, indent=2) + "\n",
    )


def update(ws: "Workspace", section: str, value) -> None:
    """Load-modify-write the whole lock, setting ONLY `section` and preserving
    every other top-level key, then write it back atomically.

    The narrow API the ENGINE routes its `applied` writes through, so an engine
    write and the resolver's `save_lock` (placeholders/presets) never clobber
    each other: each reads the file, replaces just its own section, and writes
    every other section back verbatim. Callers already hold the workspace flock,
    so the read-modify-write is race-free within that held section."""
    lock = load_lock(ws)
    lock[section] = value
    save_lock(ws, lock)
