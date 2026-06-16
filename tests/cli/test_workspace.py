"""Tests for core/workspace.py: name validation, reserved names, token, list."""
from __future__ import annotations

import pytest


# ---- for_name / name validation ----------------------------------------------


def test_for_name_valid(xdg):
    from credproxy_cli.core.workspace import for_name, Workspace

    ws = for_name("my-proj_123")
    assert isinstance(ws, Workspace)
    assert ws.name == "my-proj_123"


def test_for_name_starts_with_separator_rejected(xdg):
    from credproxy_cli.core.errors import WorkspaceError
    from credproxy_cli.core.workspace import for_name

    with pytest.raises(WorkspaceError, match="invalid workspace name"):
        for_name("-bad")


def test_for_name_special_chars_rejected(xdg):
    from credproxy_cli.core.errors import WorkspaceError
    from credproxy_cli.core.workspace import for_name

    with pytest.raises(WorkspaceError, match="invalid workspace name"):
        for_name("has space")


def test_reserved_names_rejected(xdg):
    """All reserved verb names must be rejected."""
    from credproxy_cli.core.errors import WorkspaceError
    from credproxy_cli.core.workspace import RESERVED_NAMES, for_name

    for name in RESERVED_NAMES:
        with pytest.raises(WorkspaceError, match="reserved command name"):
            for_name(name)


def test_reserved_names_cover_all_cli_verbs():
    """Guard against drift: every CLI verb and top-level meta command must be
    in RESERVED_NAMES, or a workspace could take a name the dispatcher reads as
    a verb (and become unaddressable). core can't import porcelain, so the two
    are maintained separately -- this test is what keeps them in sync."""
    from credproxy_cli.core.workspace import RESERVED_NAMES
    from credproxy_cli.porcelain import cli

    cli_tokens = cli._WS_VERBS | cli._WS_NOUN_VERBS | cli._META_COMMANDS
    missing = cli_tokens - RESERVED_NAMES
    assert not missing, f"CLI verbs/commands missing from RESERVED_NAMES: {missing}"


# ---- workspace paths ---------------------------------------------------------


def test_workspace_paths_derived_from_name(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import Workspace

    ws = Workspace("myws")
    assert ws.config_path.name == "myws.toml"
    assert ws.proxy_container == "credproxy-proxy-myws"
    assert ws.ws_container == "credproxy-ws-myws"
    assert ws.volume("home") == "credproxy-vol-myws-home"
    assert ws.volume_prefix == "credproxy-vol-myws-"


def test_workspace_exists(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import Workspace

    ws = Workspace("existing")
    assert not ws.exists()
    (workspaces_dir / "existing.toml").write_text('image = "x"\n')
    assert ws.exists()


# ---- ensure_token / read_token -----------------------------------------------


def test_ensure_token_creates_file(xdg):
    from credproxy_cli.core.workspace import Workspace, ensure_token, read_token

    ws = Workspace("toktest")
    ws.ensure_state_dir()
    ensure_token(ws)

    assert ws.token_path.exists()
    token = read_token(ws)
    assert len(token) == 32  # secrets.token_hex(16) -> 32 hex chars


def test_ensure_token_idempotent(xdg):
    from credproxy_cli.core.workspace import Workspace, ensure_token, read_token

    ws = Workspace("toktst2")
    ws.ensure_state_dir()
    ensure_token(ws)
    first = read_token(ws)
    ensure_token(ws)  # again
    second = read_token(ws)
    assert first == second


def test_read_token_missing_raises(xdg):
    from credproxy_cli.core.errors import WorkspaceError
    from credproxy_cli.core.workspace import Workspace, read_token

    ws = Workspace("notok")
    with pytest.raises(WorkspaceError, match="token missing"):
        read_token(ws)


# ---- list_names --------------------------------------------------------------


def test_list_names_empty(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import list_names

    assert list_names() == []


def test_list_names_sorted(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import list_names

    for name in ("charlie", "alpha", "bravo"):
        (workspaces_dir / f"{name}.toml").write_text('image = "x"\n')

    assert list_names() == ["alpha", "bravo", "charlie"]


def test_list_names_ignores_non_toml(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import list_names

    (workspaces_dir / "ws.toml").write_text('image = "x"\n')
    (workspaces_dir / "readme.txt").write_text("not a workspace")
    names = list_names()
    assert names == ["ws"]


# ---- derive_workspace_name ---------------------------------------------------


def test_derive_basename(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import derive_workspace_name
    assert derive_workspace_name("/home/me/src/myproj") == "myproj"


def test_derive_trailing_slash(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import derive_workspace_name
    assert derive_workspace_name("/home/me/src/myproj/") == "myproj"


def test_derive_sanitizes(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import derive_workspace_name
    assert derive_workspace_name("/tmp/My Proj!") == "My-Proj"  # case preserved
    assert derive_workspace_name("/tmp/.hidden") == "hidden"    # leading dot stripped


def test_derive_dedups_existing(xdg, workspaces_dir):
    from credproxy_cli.core.workspace import derive_workspace_name
    (workspaces_dir / "foo.toml").write_text('image = "x"\n')
    assert derive_workspace_name("/a/foo") == "foo-2"


def test_derive_dedups_reserved(xdg, workspaces_dir):
    """A directory named like a command verb derives a suffixed name rather
    than a reserved (rejected) one."""
    from credproxy_cli.core.workspace import derive_workspace_name
    assert derive_workspace_name("/a/config") == "config-2"


def test_derive_empty_raises(xdg, workspaces_dir):
    from credproxy_cli.core.errors import WorkspaceError
    from credproxy_cli.core.workspace import derive_workspace_name
    with pytest.raises(WorkspaceError, match="could not derive"):
        derive_workspace_name("/a/@@@")


# ---- lifecycle lock ----------------------------------------------------------


def test_lock_is_reentrant_within_process(xdg):
    """Nested acquisition (recreate -> start, enter -> start) must not deadlock,
    and the depth registry is cleaned up on exit."""
    from credproxy_cli.core import workspace as W
    ws = W.for_name("a")
    key = str(ws.lock_path)
    with ws.lock():
        assert W._lock_depth[key] == 1
        with ws.lock():                       # would deadlock against raw flock
            assert W._lock_depth[key] == 2
        assert W._lock_depth[key] == 1
    assert key not in W._lock_depth           # fully released


def test_lock_excludes_other_holders(xdg):
    """While the lock is held, a separate open-file-description on the same lock
    file cannot take it (cross-process exclusion); it's free once released."""
    import fcntl
    import os

    from credproxy_cli.core import workspace as W
    ws = W.for_name("a")
    ws.ensure_state_dir()
    probe = os.open(str(ws.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        with ws.lock():
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # released -> a non-blocking acquire now succeeds
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)
