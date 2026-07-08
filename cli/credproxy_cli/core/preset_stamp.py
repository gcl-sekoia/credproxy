"""Stamp a preset's expansion into a workspace TOML as ordinary literal config.

A preset is **expansion, not a link**: `preset add` writes plain
`[[binding]]`/`[[rule]]` blocks AND container-half config (`mounts`/`setup`
elements, `[env]` keys) into the workspace file, then forgets. Nothing here is
ever read back by the load path (tomllib drops comments), so the provenance
comments this module writes are inert -- they exist for a future `preset`
refresh / doctor (#58) and for the v1 double-add guard (`already_applied`).

The hard part is TOML surgery that touches ONLY the intended spans:

  - `mounts`/`setup` are top-level ARRAY keys: new elements are inserted before
    the closing `]` of a multiline inline array (rewritten to multiline from a
    single-line/empty array first, existing element text preserved verbatim), or
    a fresh `key = [ ... ]` is created at the END OF THE ROOT REGION (before the
    first top-level table header -- appending at EOF would wrongly nest the key
    under a trailing `[env]`/`[[binding]]` section). A workspace whose mounts /
    setup are `[[mounts]]` / `[[setup]]` array-of-tables blocks gets appended
    blocks of the same form instead (a `mounts =`/`setup =` key would collide
    with them -- "Cannot mutate immutable namespace").
  - `[env]` keys append at the end of an existing `[env]` section, else a fresh
    `[env]` is created at the root-region end.
  - `[[binding]]`/`[[rule]]` blocks append at EOF (array-of-tables merge in file
    order), each under its own provenance comment line.

Every stamped span carries a provenance marker:

    # credproxy:preset name=<name> rev=<12hex> sha=<12hex>

`rev` = the first-12-hex of sha256 over the preset definition-file bytes.

`sha` = the first-12-hex of sha256 over the exact VISIBLE on-disk stamped text,
by span kind (so #58 / a refresh can recompute it deterministically):

  - **array element** (`mounts`/`setup`): the bare rendered element string as it
    appears between the surrounding punctuation -- e.g. `{ run = "x", order = 1 }`
    -- WITHOUT the 2-space indent, the trailing separating comma, the two spaces
    before the marker, or the trailing newline.
  - **env line**: the `KEY = "value"` code, WITHOUT the two spaces before the
    marker or the trailing newline.
  - **`[[...]]` block** (`binding`/`rule`/`mounts`): the whole block text from
    its `[[...]]` header line through its trailing newline (inclusive) -- i.e.
    exactly the bytes written after the marker line -- WITHOUT the phantom
    leading blank line (that blank is inter-block spacing, not block content) and
    WITHOUT the marker comment line itself.

Placement: a standalone line above a `[[...]]` block; a trailing comment on an
array element / env line.

A MANDATORY verify step re-parses the composed text with tomllib and asserts it
equals the old parse PLUS exactly the intended additions -- on any mismatch the
add aborts with NO write. All writes are atomic (`_atomic_write_text`).
"""
from __future__ import annotations

import copy
import hashlib
import re
import tomllib

from .bindings import (
    _array_depth_delta,
    _atomic_write_text,
    _render_binding_block,
    _TABLE_HEADER_RE,
    _toml_key,
    _toml_str,
)
from .config import inline_array_span, last_code_char_index
from .errors import ConfigError
from .rules import _render_rule_block
from .workspace import Workspace

# `[[mounts]]` / `[[setup]]` array-of-tables headers (a workspace built its
# mounts/setup via `mount add` / a block form rather than an inline `key = [...]`
# array). Tolerant of inner whitespace (`[[ mounts ]]`).
_MOUNTS_BLOCK_RE = re.compile(r"^\s*\[\[\s*mounts\s*\]\]\s*(#.*)?$")
_SETUP_BLOCK_RE = re.compile(r"^\s*\[\[\s*setup\s*\]\]\s*(#.*)?$")
# `[env]` section header, tolerant of inner whitespace (`[ env ]`).
_ENV_HEADER_RE = re.compile(r"^\s*\[\s*env\s*\]\s*(#.*)?$")
# The two `env` shapes preset stamping can't extend surgically: a top-level
# inline table (`env = { ... }`) or dotted keys (`env.KEY = ...`). Detected up
# front so the failure names the remedy instead of the opaque verify abort.
_ENV_INLINE_RE = re.compile(r"^\s*env\s*=")
_ENV_DOTTED_RE = re.compile(r"^\s*env\s*\.")


# ---- provenance --------------------------------------------------------------


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _marker(name: str, rev: str, element_text: str) -> str:
    return f"# credproxy:preset name={name} rev={rev} sha={_sha12(element_text)}"


_MARKER_RE = re.compile(r"#\s*credproxy:preset\s+name=(\S+)\s")


def _line_comment(line: str) -> str:
    """The comment portion of one TOML line -- from the first `#` that is OUTSIDE
    a string to end of line -- or "" if the line has no real comment. A `#`
    inside a `"..."`/`'...'` string value is not a comment (string scanning
    mirrors the rest of this module)."""
    in_str = False
    str_ch = ""
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_str:
            if str_ch == '"' and c == "\\":
                i += 2
                continue
            if c == str_ch:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str = True
            str_ch = c
            i += 1
            continue
        if c == "#":
            return line[i:]
        i += 1
    return ""


def already_applied(text: str, name: str) -> bool:
    """True iff `text` already carries a provenance marker for preset `name` --
    the double-add guard (protects pure-container packs, which have no
    binding-name collision to trip). Matches the exact name token, and ONLY in a
    real comment (a `#` outside any string) -- so a marker-looking substring
    inside a string VALUE (`NOTE = "... # credproxy:preset name=x ..."`) can't
    false-positive the guard."""
    for line in text.splitlines():
        comment = _line_comment(line)
        if not comment:
            continue
        m = _MARKER_RE.search(comment)
        if m and m.group(1) == name:
            return True
    return False


# ---- element / block rendering ----------------------------------------------


def _render_mount_inline(pm) -> str:
    """A mount inline table, e.g. `{ overlay = "acme:x.sh", target = "/opt/x" }`.
    `readonly` is emitted only when the pack declared it (load applies the
    per-kind default otherwise)."""
    parts = [f"{pm.kind} = {_toml_str(pm.value)}",
             f"target = {_toml_str(pm.target)}"]
    if pm.readonly is not None:
        parts.append(f"readonly = {'true' if pm.readonly else 'false'}")
    if pm.user_owned:
        parts.append("user_owned = true")
    return "{ " + ", ".join(parts) + " }"


def _render_mount_block(pm) -> str:
    """A `[[mounts]]` array-of-tables block (leading blank line), for a workspace
    that already uses the block form."""
    lines = ["", "[[mounts]]", f"{pm.kind} = {_toml_str(pm.value)}",
             f"target = {_toml_str(pm.target)}"]
    if pm.readonly is not None:
        lines.append(f"readonly = {'true' if pm.readonly else 'false'}")
    if pm.user_owned:
        lines.append("user_owned = true")
    return "\n".join(lines) + "\n"


def _render_setup_inline(step: dict) -> str:
    """A setup inline table, e.g. `{ run = "bash /opt/x.sh", order = 45 }`.
    `user` is emitted only when non-default (`root`); load defaults it to
    `workspace`."""
    parts = [f"run = {_toml_str(step['run'])}"]
    if step.get("user", "workspace") != "workspace":
        parts.append(f"user = {_toml_str(step['user'])}")
    parts.append(f"order = {step['order']}")
    return "{ " + ", ".join(parts) + " }"


def _render_setup_block(step: dict) -> str:
    """A `[[setup]]` array-of-tables block (leading blank line), for a workspace
    that already declares setup as blocks (symmetric with `_render_mount_block`).
    `user` is emitted only when non-default (`root`)."""
    lines = ["", "[[setup]]", f"run = {_toml_str(step['run'])}"]
    if step.get("user", "workspace") != "workspace":
        lines.append(f"user = {_toml_str(step['user'])}")
    lines.append(f"order = {step['order']}")
    return "\n".join(lines) + "\n"


def _render_env_line(key: str, value: str) -> str:
    """The `KEY = "value"` code of one env line (no comment)."""
    return f"{_toml_key(key)} = {_toml_str(value)}"


# ---- root-region + inline-array surgery -------------------------------------


def _root_region_end(lines: list[str]) -> int:
    """Index of the first top-level table-header line (`[x]`/`[[x]]`), tracking
    multiline-array bracket depth so an array continuation line beginning with
    `[` is not mistaken for a header. len(lines) when there is no table header.
    Top-level array keys (`mounts`/`setup`) and a fresh `[env]` must land BEFORE
    this line, else they'd nest under a trailing table section."""
    depth = 0
    for i, ln in enumerate(lines):
        if depth == 0 and _TABLE_HEADER_RE.match(ln):
            return i
        depth = max(0, depth + _array_depth_delta(ln))
    return len(lines)


def _element_lines(elements: list[str], name: str, rev: str) -> str:
    """Render array element lines (2-space indent, trailing comma, trailing
    provenance comment), one per element, each newline-terminated."""
    out = []
    for el in elements:
        out.append(f"  {el},  {_marker(name, rev, el)}\n")
    return "".join(out)


def _insert_into_inline_array(text: str, span: tuple[int, int],
                              elements: list[str], name: str, rev: str) -> str:
    """Insert element lines before the closing `]` of the `key = [ ... ]` array
    at `span`, rewriting a single-line/empty array to multiline first and keeping
    existing element (and comment) text verbatim.

    The separating comma is decided by a string/comment-aware scan for the last
    CODE character (`last_code_char_index`) and spliced in directly AFTER it --
    so it lands between the last element and any trailing comment,
    never inside a comment (`# note` -> `# note,`) and never after a `#`/`,` that
    only appears inside a string value."""
    open_idx, close_idx = span
    head = text[:open_idx + 1]        # up to and incl. "["
    inner = text[open_idx + 1:close_idx]
    tail = text[close_idx:]           # "]" onward
    body = _element_lines(elements, name, rev)
    last = last_code_char_index(inner)
    if last is not None and inner[last] != ",":
        # Splice a separating comma right after the last element's last code
        # char (comments/whitespace excluded), preserving any trailing comment.
        inner = inner[:last + 1] + "," + inner[last + 1:]
    content = inner.rstrip()          # keep leading newline + existing elems/comments
    if content == "":
        # empty / whitespace-only array -> `[\n  <new>,  # ...\n]`
        return head + "\n" + body + tail
    lead = "" if content.startswith(("\n", "\r")) else "\n  "
    return head + lead + content + "\n" + body + tail


def _create_inline_array(text: str, key: str, elements: list[str],
                         name: str, rev: str) -> str:
    """Create a fresh `key = [ ... ]` inline array at the end of the root region."""
    lines = text.splitlines(keepends=True)
    at = _root_region_end(lines)
    block = f"{key} = [\n" + _element_lines(elements, name, rev) + "]\n"
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines.insert(at, block)
    return "".join(lines)


def _has_block_header(text: str, header_re: re.Pattern) -> bool:
    return any(header_re.match(ln) for ln in text.splitlines())


def _append_blocks(text: str, blocks: list[str], name: str, rev: str) -> str:
    """Append fully-rendered array-of-tables blocks (each starting with a leading
    blank line then the `[[...]]` header) at EOF, inserting a provenance comment
    line immediately above each block header."""
    if text and not text.endswith("\n"):
        text += "\n"
    for blk in blocks:
        body = blk[1:] if blk.startswith("\n") else blk   # drop the leading "\n"
        # The marker sha covers the EXACT on-disk block text (`body`: the
        # `[[...]]` header line through the trailing newline) -- NOT the phantom
        # leading blank line, which is inter-block spacing, not block content.
        text += "\n" + _marker(name, rev, body) + "\n" + body
    return text


def _stamp_array(text: str, key: str, elements: list[str],
                 blocks: list[str], name: str, rev: str,
                 block_re: re.Pattern | None) -> str:
    """Stamp `elements` into the top-level `key` array. Prefers an existing
    inline `key = [...]` array; falls back to appended `[[key]]` blocks when the
    workspace uses that form (mounts only); else creates a fresh inline array."""
    span = inline_array_span(text, key)
    if span is not None:
        return _insert_into_inline_array(text, span, elements, name, rev)
    if block_re is not None and _has_block_header(text, block_re):
        return _append_blocks(text, blocks, name, rev)
    return _create_inline_array(text, key, elements, name, rev)


def _stamp_env(text: str, items: list[tuple[str, str]], name: str, rev: str) -> str:
    """Append `KEY = "value"  # <marker>` lines to an existing `[env]` section,
    else create `[env]` at the end of the root region."""
    lines = text.splitlines(keepends=True)

    # A root-level inline-table (`env = { ... }`) or dotted-key (`env.KEY = ...`)
    # env can't be extended by appending an `[env]` section (tomllib would reject
    # the redefinition). Detect it up front and fail with the remedy, rather than
    # letting the surgery produce an opaque "invalid TOML" verify abort.
    root_end = _root_region_end(lines)
    for ln in lines[:root_end]:
        if _ENV_INLINE_RE.match(ln) or _ENV_DOTTED_RE.match(ln):
            raise ConfigError(
                "workspace declares `env` as an inline table / dotted keys, "
                "which preset env stamping can't extend surgically; convert it "
                "to an `[env]` section header to accept preset env stamping (no "
                "changes were written)")

    key_lines = []
    for k, v in items:
        code = _render_env_line(k, v)
        key_lines.append(f"{code}  {_marker(name, rev, code)}\n")

    idx = next((i for i, ln in enumerate(lines) if _ENV_HEADER_RE.match(ln)), None)
    if idx is not None:
        # End of the [env] section: the next top-level table header (or EOF),
        # trimming trailing blank lines so the keys land tightly.
        end = idx + 1
        depth = 0
        while end < len(lines):
            if depth == 0 and _TABLE_HEADER_RE.match(lines[end]):
                break
            depth = max(0, depth + _array_depth_delta(lines[end]))
            end += 1
        ins = end
        while ins - 1 > idx and lines[ins - 1].strip() == "":
            ins -= 1
        if ins > 0 and not lines[ins - 1].endswith("\n"):
            lines[ins - 1] += "\n"
        for kl in reversed(key_lines):
            lines.insert(ins, kl)
        return "".join(lines)

    at = _root_region_end(lines)
    block = "[env]\n" + "".join(key_lines)
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines.insert(at, block)
    return "".join(lines)


# ---- verify ------------------------------------------------------------------


def _frag_table(inline: str) -> dict:
    return tomllib.loads(f"__x = {inline}")["__x"]


def _frag_block(block: str, key: str) -> dict:
    return tomllib.loads(block)[key][0]


def _verify(old_text: str, new_text: str, *, mounts, setup, env_items,
            bindings, rules) -> None:
    """Re-parse both texts and assert `new == old + exactly the intended
    additions`. Any mismatch (a surgery bug, an unexpected reflow) aborts the add
    with NO write."""
    try:
        old = tomllib.loads(old_text)
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"preset stamping produced invalid TOML ({e}); no changes were "
            f"written. This usually means the workspace TOML has a shape the "
            f"surgical stamp mis-read -- e.g. a multiline string whose content "
            f"looks like a `[table]`/`key = [` line, or an unusual mounts/setup/"
            f"env declaration. Simplify that section (or file a bug) and retry")
    expected = copy.deepcopy(old)
    if mounts:
        expected["mounts"] = expected.get("mounts", []) + \
            [_frag_table(_render_mount_inline(m)) for m in mounts]
    if setup:
        expected["setup"] = expected.get("setup", []) + \
            [_frag_table(_render_setup_inline(s)) for s in setup]
    if env_items:
        e = dict(expected.get("env", {}))
        for k, v in env_items:
            e[k] = v
        expected["env"] = e
    if bindings:
        expected["binding"] = expected.get("binding", []) + \
            [_frag_block(_render_binding_block(b), "binding") for b in bindings]
    if rules:
        expected["rule"] = expected.get("rule", []) + \
            [_frag_block(_render_rule_block(r), "rule") for r in rules]
    if new != expected:
        raise ConfigError(
            "preset stamping changed the config in an unexpected way; no "
            "changes were written. Likely causes: a workspace key with the same "
            "name as one the preset stamps (mounts/setup/env/binding/rule) "
            "declared in an unusual form, or a multiline string containing "
            "header-looking lines. Simplify that section (or file a bug) and retry")


# ---- public API --------------------------------------------------------------


def compose(text: str, name: str, rev: str, *, bindings, rules, mounts,
            env_items, setup) -> str:
    """Compose the new workspace-TOML text with every stamped span (mounts/setup
    array elements, [env] keys, binding/rule blocks) added, verified against a
    tomllib re-parse. Pure -- returns the text; the caller writes it."""
    old_text = text
    if mounts:
        text = _stamp_array(
            text, "mounts",
            [_render_mount_inline(m) for m in mounts],
            [_render_mount_block(m) for m in mounts],
            name, rev, _MOUNTS_BLOCK_RE)
    if setup:
        text = _stamp_array(
            text, "setup",
            [_render_setup_inline(s) for s in setup],
            [_render_setup_block(s) for s in setup],
            name, rev, _SETUP_BLOCK_RE)
    if env_items:
        text = _stamp_env(text, env_items, name, rev)
    if bindings:
        text = _append_blocks(
            text, [_render_binding_block(b) for b in bindings], name, rev)
    if rules:
        text = _append_blocks(
            text, [_render_rule_block(r) for r in rules], name, rev)
    _verify(old_text, text, mounts=mounts, setup=setup, env_items=env_items,
            bindings=bindings, rules=rules)
    return text


def stamp(ws: Workspace, name: str, rev: str, *, bindings, rules, mounts,
          env_items, setup) -> None:
    """Atomically stamp a preset's expansion into the workspace TOML (compose +
    verify + one atomic write). All lists may be empty."""
    text = compose(ws.config_path.read_text(), name, rev,
                   bindings=bindings, rules=rules, mounts=mounts,
                   env_items=env_items, setup=setup)
    _atomic_write_text(ws.config_path, text)
