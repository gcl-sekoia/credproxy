"""Guard tests that keep the prose docs honest against the *real* CLI.

Two lints sweep README.md + docs/**/*.md:

1. **Command lint** -- every `credproxy`/`credp` command line in a fenced code
   block is validated against the actual argparse parser tree the CLI builds
   (`porcelain.cli._build_leaf_parser` for the workspace-verb tree, plus the
   hand-rolled top-level dispatch mirrored from the same module's constants).
   Both the subcommand *path* (verb resolves) and every `--flag` (known to the
   resolved subparser or a global) are checked. This is the guard that would
   have caught the retired `binding add --pack` flag surviving in the docs
   (see `test_regression_stale_pack_flag`).

2. **Link + anchor lint** -- every relative markdown link resolves to a file,
   and every `#anchor` (cross-file or same-file) matches a heading under
   GitHub's slugging rules.

Stdlib only (re/pathlib/shlex/argparse introspection); one pass over the files,
no subprocess. The parser objects are imported once.
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

import pytest

# --- repo layout (robust to cwd) ---------------------------------------------
_REPO = Path(__file__).resolve().parents[2]
_CLI_DIR = str(_REPO / "cli")
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from credproxy_cli.porcelain import cli as _cli  # noqa: E402


def _doc_files() -> list[Path]:
    files = [_REPO / "README.md"]
    files += sorted((_REPO / "docs").rglob("*.md"))
    return [f for f in files if f.is_file()]


# =============================================================================
# Command lint
# =============================================================================

# Global flags, popped order-independently by porcelain.cli._pop_global_flags,
# plus the help flags every dispatch level honors. Mirrored from that function.
_GLOBALS = {"--loose", "--json", "--yes", "-y", "-h", "--help"}

# Flags of the hand-rolled top-level commands that have NO argparse object to
# introspect (their dispatch parses argv by hand). Mirrored from porcelain.cli:
#   _parse_create / _dispatch_def / _dispatch_script / _dispatch_dev /
#   _dispatch_meta(doctor) / _dispatch_emit_compose / do_push_stateless.
_CREATE_OPTS = {"--here", "--dir", "--attach"}  # _parse_create
_PUSH_STATELESS_OPTS = {"--admin", "--config", "--token", "--wait", "--timeout"}


def _parser_options(parser: argparse.ArgumentParser) -> set[str]:
    opts: set[str] = set()
    for a in parser._actions:
        opts.update(a.option_strings)
    return opts


def _subparser_choices(parser: argparse.ArgumentParser) -> dict:
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


# The one big real parser: the whole workspace-scoped verb tree
# (enter/.../binding/rule/mount/pack and their sub-actions).
_LEAF = _cli._build_leaf_parser()


def _first_nonflag(tokens: list[str]) -> int | None:
    for i, t in enumerate(tokens):
        if not t.startswith("-"):
            return i
    return None


def _descend(parser: argparse.ArgumentParser, tokens: list[str]) -> tuple[set[str], str | None]:
    """Walk the real argparse subparser tree, consuming subcommand tokens and
    returning (union of option strings along the path, verb-path error | None).

    Only non-flag tokens that are subcommand *choices* are consumed; positional
    values and flag values are skipped (subparser-bearing parsers in this tree
    carry no options of their own, so a flag never precedes its subcommand)."""
    allowed = _parser_options(parser)
    choices = _subparser_choices(parser)
    if not choices:
        return allowed, None
    idx = _first_nonflag(tokens)
    if idx is None:
        return allowed, f"missing subcommand (expected one of {sorted(choices)})"
    tok = tokens[idx]
    if tok not in choices:
        return allowed, f"unknown subcommand {tok!r} (expected one of {sorted(choices)})"
    child_allowed, err = _descend(choices[tok], tokens[idx + 1:])
    return allowed | child_allowed, err


def _walk_leaf(tokens: list[str]) -> tuple[set[str], str | None]:
    allowed, err = _descend(_LEAF, tokens)
    return allowed | _GLOBALS, err


def _resolve(surface: str, rest: list[str]) -> tuple[set[str], str | None]:
    """Resolve a (globals-stripped, pre-`--`) command to its allowed option set.

    Mirrors porcelain.cli.main()'s dispatch. Returns (allowed_options, error);
    a non-None error is a verb-path failure (unknown command/subcommand)."""
    loose = surface == "credp"
    if not rest:
        return _GLOBALS, None  # bare `credproxy` -> help
    head, tail = rest[0], rest[1:]

    if head == "workspace":
        return _resolve_workspace(tail)
    if head in ("injector", "provider"):
        return _resolve_def(head, tail)
    if head == "pack":
        return _resolve_pack(tail)
    if head == "script":
        return _resolve_script(tail)
    if head == "dev":
        return _resolve_dev(tail)
    if head == "push":
        return _resolve_push(tail)
    if head == "emit-compose":
        return _GLOBALS | {"--image"}, None
    if head in _cli._META_COMMANDS:  # list/current/info/doctor
        if head == "doctor":
            return _GLOBALS | {"--fetch"}, None
        return _GLOBALS, None
    if head in ("version", "--version"):
        return _GLOBALS, None
    if loose:
        return _resolve_alias(head, tail)
    return None, f"unknown command {head!r}"


def _resolve_workspace(tail: list[str]) -> tuple[set[str], str | None]:
    # Mirrors _dispatch_workspace.
    if not tail:
        return _GLOBALS, "usage: credproxy workspace {create|use|list|NAME <verb>}"
    h = tail[0]
    if h == "create":
        return _GLOBALS | _CREATE_OPTS, None
    if h in ("use", "list"):
        return _GLOBALS, None
    if h in _cli._WS_VERBS:  # verb with no explicit NAME
        return _walk_leaf(tail)
    # otherwise h is a workspace NAME (a placeholder token is fine, skipped)
    if len(tail) < 2:
        return _GLOBALS, f"usage: credproxy workspace {h} <verb>"
    return _walk_leaf(tail[1:])


def _resolve_def(kind: str, tail: list[str]) -> tuple[set[str], str | None]:
    # Mirrors _dispatch_def.
    sub = tail[0] if tail else None
    if sub == "scaffold":
        return _GLOBALS | ({"--script", "--lang"} if kind == "injector" else {"--lang"}), None
    if sub == "list":
        return _GLOBALS, None
    if kind == "injector" and sub == "check":
        return _GLOBALS | {"--compile"}, None
    if kind == "injector" and sub == "api":
        return _GLOBALS, None
    if kind == "provider" and sub == "show":
        return _GLOBALS, None
    return None, f"unknown {kind} command {sub!r}"


def _resolve_pack(tail: list[str]) -> tuple[set[str], str | None]:
    # Mirrors _dispatch_pack: `list` (definitional) or add/refresh/remove
    # (-> leaf tree).
    if not tail or tail[0] == "list":
        return _GLOBALS, None
    if tail[0] in ("add", "refresh", "remove"):
        return _walk_leaf(["pack", *tail])
    return None, f"unknown pack command {tail[0]!r}"


def _resolve_script(tail: list[str]) -> tuple[set[str], str | None]:
    # Mirrors _dispatch_script.
    if not tail or tail[0] == "check":
        return _GLOBALS | {"--container", "--docker"}, None
    return None, f"unknown script command {tail[0]!r}"


def _resolve_dev(tail: list[str]) -> tuple[set[str], str | None]:
    # Mirrors _dispatch_dev.
    if not tail:
        return _GLOBALS, "usage: credproxy dev {build|test|reload}"
    sub = tail[0]
    if sub == "build":
        return _GLOBALS, None
    if sub == "test":
        return _GLOBALS | {"--cli", "--proxy", "--container", "--docker"}, None
    if sub == "reload":
        return _GLOBALS, None
    return None, f"unknown dev command {sub!r}"


def _resolve_push(tail: list[str]) -> tuple[set[str], str | None]:
    # Mirrors _dispatch_push: stateless form iff --admin/--config/--token present,
    # else the loose `push [NAME]` alias onto the leaf `push` verb.
    if {"--admin", "--config", "--token"} & set(tail):
        return _GLOBALS | _PUSH_STATELESS_OPTS, None
    return _walk_leaf(["push", *tail])


def _resolve_alias(head: str, tail: list[str]) -> tuple[set[str], str | None]:
    # Mirrors _dispatch_alias (loose surface top-level aliases).
    if head in ("binding", "mount", "rule"):
        return _walk_leaf([head, *tail])
    if head == "use":
        return _GLOBALS, None
    if head == "create":
        return _GLOBALS | _CREATE_OPTS, None
    if head == "list":
        return _GLOBALS, None
    if head in _cli._ALIAS_TO_WS_VERB:
        args = list(tail)
        if args and not args[0].startswith("-"):  # optional leading NAME
            args = args[1:]
        return _walk_leaf([head, *args])
    return None, f"unknown command {head!r}"


# --- command extraction ------------------------------------------------------
_FENCE = re.compile(r"^```(\w*)\s*$")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SH_LANGS = {"sh", "bash", "shell"}


def _logical_lines(raw_lines: list[str]) -> list[str]:
    """Join `\\`-continued physical lines into logical lines (a continuation
    line has no `$ ` prompt of its own, so this must run before console
    filtering)."""
    out: list[str] = []
    buf: str | None = None
    for ln in raw_lines:
        cur = ln[:-1] if ln.rstrip().endswith("\\") else None
        if cur is not None:
            piece = ln.rstrip()[:-1]  # drop trailing backslash
            buf = piece if buf is None else buf + " " + piece.strip()
        else:
            if buf is not None:
                out.append((buf + " " + ln.strip()).strip())
                buf = None
            else:
                out.append(ln)
    if buf is not None:
        out.append(buf)
    return out


def _iter_command_lines():
    """Yield (file, logical_command_str, surface) for each credproxy/credp
    invocation in an sh/console fenced block."""
    for f in _doc_files():
        block_lang: str | None = None
        block: list[str] = []
        for ln in f.read_text().splitlines():
            m = _FENCE.match(ln)
            if m:
                if block_lang is None:
                    block_lang = m.group(1) or "plain"
                    block = []
                else:
                    yield from _emit_block(f, block_lang, block)
                    block_lang = None
                continue
            if block_lang is not None:
                block.append(ln)


def _emit_block(f: Path, lang: str, raw: list[str]):
    if lang not in _SH_LANGS and lang != "console":
        return
    for line in _logical_lines(raw):
        s = line.strip()
        if lang == "console":
            if not s.startswith("$ "):
                continue  # output, or an in-container prompt line -> not linted
            s = s[2:].strip()
        if not s or s.startswith("#"):
            continue
        try:
            toks = shlex.split(s, comments=True)
        except ValueError:
            continue  # unbalanced quotes in an illustrative snippet -> skip
        if not toks:
            continue
        # strip leading `env`/`VAR=value` prefixes
        i = 0
        while i < len(toks) and (toks[i] == "env" or _ENV_ASSIGN.match(toks[i])):
            i += 1
        toks = toks[i:]
        if not toks or toks[0] not in ("credproxy", "credp"):
            continue
        yield f, s, toks[0], toks


def _is_flag(tok: str) -> bool:
    return tok.startswith("-") and tok not in ("-", "--")


def _lint_command(surface: str, toks: list[str]) -> str | None:
    """Return an error string if the command is invalid, else None. `toks`
    includes the `credproxy`/`credp` binary token at index 0."""
    args = toks[1:]
    # tokens after a bare `--` are the payload command -> never linted
    if "--" in args:
        args = args[: args.index("--")]
    # pop globals (also caught: a lone --help/-h means "print help", always valid)
    if any(t in ("-h", "--help") for t in args):
        return None
    rest = [t for t in args if t not in ("--loose", "--json", "--yes", "-y")]
    allowed, err = _resolve(surface, rest)
    if err is not None:
        return f"verb path: {err}"
    flags = []
    for t in rest:
        if _is_flag(t):
            flags.append(t.split("=", 1)[0])
    bad = [flg for flg in flags if flg not in allowed]
    if bad:
        return f"unknown flag(s) {' '.join(bad)} for `{' '.join([surface] + rest)}`"
    return None


# --- the command-lint test ---------------------------------------------------

# Legitimately-unvalidatable command lines. Investigate before adding an entry:
# a lint failure usually means the doc is wrong, which is the whole point.
_COMMAND_SKIP: set[str] = set()


def test_doc_commands_valid():
    failures = []
    count = 0
    for f, s, surface, toks in _iter_command_lines():
        if s in _COMMAND_SKIP:
            continue
        count += 1
        err = _lint_command(surface, toks)
        if err:
            rel = f.relative_to(_REPO)
            failures.append(f"{rel}: `{s}`\n    -> {err}")
    assert count > 0, "extracted no command lines -- extraction is broken"
    assert not failures, "stale/invalid CLI command(s) in docs:\n" + "\n".join(failures)


def test_regression_stale_pack_flag():
    """The historical staleness the whole lint exists to catch: `binding add
    --pack` was retired but survived in the README quickstart. Prove the
    validator rejects it on both surfaces, and accepts the valid replacement."""
    stale_loose = shlex.split("credp binding add --pack github")
    stale_strict = shlex.split("credproxy workspace w binding add --pack github")
    assert _lint_command("credp", stale_loose) is not None
    assert _lint_command("credproxy", stale_strict) is not None

    valid = shlex.split(
        "credp binding add --injector bearer --provider env "
        "--secret GITHUB_TOKEN --host api.github.com"
    )
    assert _lint_command("credp", valid) is None
    # and the pack noun that replaced it
    assert _lint_command("credp", shlex.split("credp pack add github")) is None


# =============================================================================
# Link + anchor lint
# =============================================================================
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_INLINE_CODE = re.compile(r"`[^`]*`")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_LINK_IN_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _slug(text: str) -> str:
    """GitHub heading slug: strip md links to their text, lowercase, drop
    punctuation (keep word chars/space/hyphen), spaces -> hyphens (no collapse
    of consecutive spaces)."""
    text = _MD_LINK_IN_TEXT.sub(r"\1", text)
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = s.replace(" ", "-")
    return s


def _headings_and_links(f: Path):
    """Return (set of heading anchors with dedup suffixes, list of (target)
    links) for a file, skipping fenced code blocks and inline code."""
    anchors: list[str] = []
    links: list[str] = []
    infence = False
    for ln in f.read_text().splitlines():
        if ln.lstrip().startswith("```"):
            infence = not infence
            continue
        if infence:
            continue
        hm = _HEADING.match(ln)
        if hm:
            base = _slug(hm.group(2))
            # GitHub dedup: first occurrence is bare, then -1, -2, ...
            dup = sum(1 for a in anchors if a == base or re.fullmatch(re.escape(base) + r"-\d+", a))
            anchors.append(base if dup == 0 else f"{base}-{dup}")
        line = _INLINE_CODE.sub("", ln)
        for m in _LINK.finditer(line):
            links.append(m.group(1).strip())
    return set(anchors), links


def _anchor_index() -> dict[Path, set[str]]:
    return {f: _headings_and_links(f)[0] for f in _doc_files()}


# Links that legitimately cannot be validated (should stay empty).
_LINK_SKIP: set[tuple[str, str]] = set()


def test_doc_links_resolve():
    anchors = _anchor_index()
    failures = []
    count = 0
    for f in _doc_files():
        _, links = _headings_and_links(f)
        for target in links:
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            rel = str(f.relative_to(_REPO))
            if (rel, target) in _LINK_SKIP:
                continue
            count += 1
            path_part, _, anchor = target.partition("#")
            if path_part == "":
                dest = f  # same-file #anchor
            else:
                dest = (f.parent / path_part).resolve()
                if not dest.exists():
                    failures.append(f"{rel}: `{target}` -> missing file {path_part}")
                    continue
            if anchor:
                dest = dest if path_part == "" else dest
                have = anchors.get(dest)
                if have is None:  # non-.md target or outside the doc set
                    if dest.suffix == ".md":
                        have = _headings_and_links(dest)[0]
                    else:
                        continue
                if anchor not in have:
                    failures.append(
                        f"{rel}: `{target}` -> no heading anchor '#{anchor}' in "
                        f"{dest.relative_to(_REPO) if _REPO in dest.parents or dest == f else dest}")
    assert count > 0, "extracted no links -- extraction is broken"
    assert not failures, "broken doc link(s)/anchor(s):\n" + "\n".join(failures)


def test_slug_known_tricky_case():
    """The `+`-with-surrounding-spaces case that must survive slugging."""
    assert _slug("Distributing a policy: script + pack") == "distributing-a-policy-script--pack"
    assert _slug("Interception is a union -- a rule can flip a host to intercepted") \
        .startswith("interception-is-a-union")


# --- debug harness: prints coverage counts when run directly -----------------
if __name__ == "__main__":
    cmds = list(_iter_command_lines())
    print(f"command lines linted: {len(cmds)}")
    total_links = 0
    for f in _doc_files():
        _, links = _headings_and_links(f)
        total_links += sum(
            1 for t in links if not t.startswith(("http://", "https://", "mailto:")))
    print(f"relative links/anchors linted: {total_links}")
    # surface any failures verbatim
    for f, s, surface, toks in cmds:
        err = _lint_command(surface, toks)
        if err:
            print("CMD FAIL:", f.relative_to(_REPO), "::", s, "->", err)
