"""Drift guard: pyproject's `proxy` dependency-group must match proxy/requirements.txt.

Two dep lists exist by necessity -- pyproject `[dependency-groups]` feeds the
on-host dev env (`uv sync --group proxy`), while proxy/requirements.txt is the
image's pip source (the Dockerfile build context is proxy/, no pyproject there).
PEP 735 groups can't `-r`-include a requirements file, so the lists are kept in
lockstep by hand and this test fails the moment they diverge (the same drift-guard
pattern as test_docs_lint.py). The `proxy` group resolves its `{include-group =
"dev"}` so pytest counts once.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _canon(req: str) -> tuple[str, str]:
    """(canonical-name, despaced-specifier) for one requirement string."""
    m = re.match(r"\s*([A-Za-z0-9._-]+)\s*(.*)", req)
    assert m, f"unparseable requirement: {req!r}"
    name = re.sub(r"[-_.]+", "-", m.group(1)).lower()   # PEP 503 canonical
    return name, m.group(2).replace(" ", "")


def _pyproject_proxy_group() -> set[tuple[str, str]]:
    data = tomllib.loads((_REPO / "pyproject.toml").read_text())
    groups = data["dependency-groups"]
    out: set[tuple[str, str]] = set()
    for entry in groups["proxy"]:
        if isinstance(entry, dict):                      # {include-group = "..."}
            for sub in groups[entry["include-group"]]:
                out.add(_canon(sub))
        else:
            out.add(_canon(entry))
    return out


def _requirements_txt() -> set[tuple[str, str]]:
    lines = (_REPO / "proxy" / "requirements.txt").read_text().splitlines()
    return {_canon(ln) for ln in lines
            if ln.strip() and not ln.lstrip().startswith("#")}


def test_proxy_group_matches_requirements_txt():
    grp = _pyproject_proxy_group()
    req = _requirements_txt()
    assert grp == req, (
        "pyproject `proxy` dependency-group and proxy/requirements.txt drifted.\n"
        f"  only in pyproject: {sorted(grp - req)}\n"
        f"  only in requirements.txt: {sorted(req - grp)}\n"
        "Keep them in lockstep (see the comments in both files).")
