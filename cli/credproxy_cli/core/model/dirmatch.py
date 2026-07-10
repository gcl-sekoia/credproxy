"""cwd -> workspace resolution (the directory resolver).

The loose/human surface resolves an omitted workspace by walking up from the
current directory and matching it against each workspace's `directory` field
(set in <name>.toml). The most-specific (longest) match wins, so nested
associations (`~/src` vs `~/src/foo`) resolve intuitively.

The name stays canonical; this is a resolver layered on top -- a sibling of
core/pointer.py. The strict surface never consults it. As with pointer.py, the
core only does the lookup; the *policy* of when to consult it (loose only,
before the default pointer, announced on stderr) lives entirely in porcelain.
"""
from __future__ import annotations

from pathlib import Path

from .config import quick_directory
from ..errors import WorkspaceError
from .workspace import Workspace, for_name, list_names


def _guard_roots() -> set[Path]:
    """Directories too broad to be a useful association ('/' and $HOME would
    match almost everything). Ignored during resolution rather than matched."""
    roots = {Path("/")}
    try:
        roots.add(Path.home())
    except RuntimeError:  # HOME unset
        pass
    return roots


def resolve_cwd(cwd: Path | None = None) -> Workspace | None:
    """The workspace whose `directory` is the closest ancestor of cwd (or cwd
    itself), or None if none matches. Raises WorkspaceError if two workspaces
    claim the same directory (ambiguous -- the user must name one).

    Both sides are canonicalized (symlinks, `..`) before comparison; a workspace
    whose `directory` no longer exists simply stops matching."""
    here = (cwd or Path.cwd()).resolve()
    guard = _guard_roots()
    matches: list[tuple[int, str, Path]] = []
    for name in list_names():
        d = quick_directory(Workspace(name))
        if not d:
            continue
        base = Path(d).resolve()
        if base in guard:
            continue
        if here == base or here.is_relative_to(base):
            matches.append((len(base.parts), name, base))
    if not matches:
        return None
    # Longest-prefix wins. Two equal-depth ancestors of the same cwd must be the
    # same path, so >1 winner means two workspaces claim one directory.
    top = max(depth for depth, _, _ in matches)
    winners = sorted(name for depth, name, _ in matches if depth == top)
    if len(winners) > 1:
        base = next(b for depth, _, b in matches if depth == top)
        raise WorkspaceError(
            f"directory {base} is claimed by multiple workspaces "
            f"({', '.join(winners)}); name one explicitly"
        )
    return for_name(winners[0])


def find_claimer(path: str | Path, *, exclude: str | None = None) -> str | None:
    """The name of an existing workspace already claiming `path` (exact match,
    canonicalized), or None. Used to warn when associating a directory. `exclude`
    skips a workspace by name, so re-binding the same one doesn't self-warn."""
    target = Path(path).resolve()
    for name in list_names():
        if name == exclude:
            continue
        d = quick_directory(Workspace(name))
        if d and Path(d).resolve() == target:
            return name
    return None
