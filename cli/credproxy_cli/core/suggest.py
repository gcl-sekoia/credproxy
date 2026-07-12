"""Tiny did-you-mean helper (difflib), a shared core leaf like errors/paths so
both planes can use it. One place to turn an "unknown X" into "unknown X (did
you mean `Y`?)", instead of each call site re-inlining difflib (issue #74 C)."""
from __future__ import annotations

import difflib


def closest(word: str, candidates) -> str | None:
    """The single closest candidate to `word`, or None when nothing is close."""
    near = difflib.get_close_matches(word, list(candidates), n=1)
    return near[0] if near else None


def did_you_mean(word: str, candidates) -> str:
    """A ` (did you mean \\`Y\\`?)` suffix when a close candidate exists, else the
    empty string. Safe to append verbatim to any "unknown ..." message."""
    hit = closest(word, candidates)
    return f" (did you mean `{hit}`?)" if hit else ""
