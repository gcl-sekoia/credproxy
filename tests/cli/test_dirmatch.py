"""Tests for core/dirmatch.py: cwd -> workspace resolution.

A workspace declares `directory = "/abs/path"`; running a loose command from at
or under that path resolves to it. The name stays canonical -- this is a
resolver layered on top, like the default pointer.
"""
from __future__ import annotations

import pytest


def _mkws(workspaces_dir, name: str, directory: str | None) -> None:
    body = 'image = "x"\n'
    if directory is not None:
        body += f'directory = "{directory}"\n'
    (workspaces_dir / f"{name}.toml").write_text(body)


# ---- resolve_cwd: basic matching ---------------------------------------------


def test_exact_match(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    proj = tmp_path / "proj"
    proj.mkdir()
    _mkws(workspaces_dir, "proj", str(proj))
    ws = resolve_cwd(proj)
    assert ws is not None and ws.name == "proj"


def test_walk_up_from_subdir(xdg, workspaces_dir, tmp_path):
    """cwd below the declared directory still resolves (walk-up)."""
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    proj = tmp_path / "proj"
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    _mkws(workspaces_dir, "proj", str(proj))
    ws = resolve_cwd(sub)
    assert ws is not None and ws.name == "proj"


def test_no_match_returns_none(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    (tmp_path / "proj").mkdir()
    (tmp_path / "elsewhere").mkdir()
    _mkws(workspaces_dir, "proj", str(tmp_path / "proj"))
    assert resolve_cwd(tmp_path / "elsewhere") is None


def test_workspace_without_directory_ignored(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    _mkws(workspaces_dir, "plain", None)  # no directory field
    assert resolve_cwd(tmp_path) is None


# ---- longest-prefix wins -----------------------------------------------------


def test_nested_longest_prefix_wins(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    outer = tmp_path / "src"
    inner = outer / "foo"
    deep = inner / "pkg"
    deep.mkdir(parents=True)
    _mkws(workspaces_dir, "outer", str(outer))
    _mkws(workspaces_dir, "inner", str(inner))
    # cwd under both; the more specific (inner) wins.
    ws = resolve_cwd(deep)
    assert ws is not None and ws.name == "inner"
    # at the outer level only, outer wins.
    ws = resolve_cwd(outer)
    assert ws is not None and ws.name == "outer"


# ---- ambiguity ---------------------------------------------------------------


def test_same_directory_two_workspaces_is_ambiguous(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import resolve_cwd
    from credproxy_cli.core.errors import WorkspaceError

    proj = tmp_path / "proj"
    proj.mkdir()
    _mkws(workspaces_dir, "alpha", str(proj))
    _mkws(workspaces_dir, "beta", str(proj))
    with pytest.raises(WorkspaceError, match="claimed by multiple"):
        resolve_cwd(proj)


# ---- footgun guard -----------------------------------------------------------


def test_root_directory_ignored(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    _mkws(workspaces_dir, "toobroad", "/")
    assert resolve_cwd(tmp_path) is None


def test_home_directory_ignored(xdg, workspaces_dir, tmp_path, monkeypatch):
    from pathlib import Path
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    fake_home = tmp_path / "home"
    (fake_home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    _mkws(workspaces_dir, "homews", str(fake_home))
    # A workspace anchored at $HOME would match nearly everything -> ignored.
    assert resolve_cwd(fake_home / "proj") is None


# ---- tolerance: a broken peer config must not break resolution ---------------


def test_invalid_peer_config_does_not_break_resolution(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    proj = tmp_path / "proj"
    proj.mkdir()
    _mkws(workspaces_dir, "good", str(proj))
    (workspaces_dir / "broken.toml").write_text("this is = not valid = toml [[[\n")
    ws = resolve_cwd(proj)
    assert ws is not None and ws.name == "good"


def test_nonexistent_directory_does_not_crash(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import resolve_cwd

    _mkws(workspaces_dir, "ghost", "/no/such/path/anywhere")
    # resolving from an unrelated cwd just returns None, no exception.
    assert resolve_cwd(tmp_path) is None


# ---- find_claimer ------------------------------------------------------------


def test_find_claimer_returns_owner(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import find_claimer

    proj = tmp_path / "proj"
    proj.mkdir()
    _mkws(workspaces_dir, "owner", str(proj))
    assert find_claimer(proj) == "owner"
    assert find_claimer(tmp_path / "other") is None


def test_find_claimer_excludes_self(xdg, workspaces_dir, tmp_path):
    from credproxy_cli.core.model.dirmatch import find_claimer

    proj = tmp_path / "proj"
    proj.mkdir()
    _mkws(workspaces_dir, "owner", str(proj))
    assert find_claimer(proj, exclude="owner") is None
