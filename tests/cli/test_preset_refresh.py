"""`preset refresh` (#59 v2): re-expand stamped packs against their current
definitions -- per-block update-cleanly / skip-hand-edited(+diff) / add / prune,
identity (placeholder/provider/secret) preserved, all-or-nothing.

The anchor is the round-trip no-op: a freshly-stamped pack refreshes to zero
writes because `rev`/`sha` are recomputed with the exact #56 helpers.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from test_porcelain import _run, _run_loose


# ---- helpers -----------------------------------------------------------------


def _write_preset(name: str, toml: str, *, tier: str = "user"):
    from credproxy_cli.core.paths import config_dir
    base = config_dir() if tier == "user" else config_dir() / "_ov" / tier
    pd = base / "presets"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{name}.toml").write_text(textwrap.dedent(toml))
    return base


def _pack_file(base, rel: str, body: str = "echo hi\n"):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def _make_ws(name: str, content: str):
    from credproxy_cli.core.paths import workspaces_config_dir
    from credproxy_cli.core.workspace import Workspace
    wd = workspaces_config_dir()
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{name}.toml").write_text(textwrap.dedent(content))
    return Workspace(name)


_WS_MIN = 'image = "python:3.12-slim"\nuser = "vscode"\n'

# A two-binding GitHub-shaped pack (shared placeholder).
_GH = """
    [placeholder]
    prefix = "ghp_"
    length = 40
    charset = "alnumeric"
    [[part]]
    suffix = "api"
    injector = "bearer"
    hosts = ["api.github.com"]
    env = "GITHUB_TOKEN"
    [[part]]
    suffix = "git"
    injector = "basic"
    hosts = ["github.com"]
"""


def _stamp(ws, preset, provider=None, secret=None):
    """Apply a pack the way `preset add` does (the fixture under test refreshes
    the result)."""
    from credproxy_cli.core import preset_stamp
    from credproxy_cli.core.presets import build_preset
    exp = build_preset(preset, provider, secret)
    preset_stamp.stamp(ws, preset, exp.rev, bindings=list(exp.bindings),
                       rules=list(exp.rules), mounts=list(exp.mounts),
                       env_items=list(exp.env), setup=[dict(s) for s in exp.setup])
    return exp


# ---- round-trip no-op --------------------------------------------------------


def test_refresh_noop_is_zero_write(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    before = ws.config_path.read_text()
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert ws.config_path.read_text() == before          # nothing written
    assert "up to date" in (out + err)


def test_refresh_noop_container_half(xdg):
    """A container-half pack (bindings + mount + env + setup) also round-trips to
    a zero-write no-op immediately after stamping."""
    base = _write_preset("cont", _GH + """
        [[mount]]
        overlay = "setup.d/c.sh"
        target = "/opt/c.sh"
        [env]
        C_VAR = "one"
        [[setup]]
        run = "bash /opt/c.sh"
        order = 45
    """)
    _pack_file(base, "setup.d/c.sh")
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "cont", "env", "TOK")
    before = ws.config_path.read_text()
    code, out, err = _run(["workspace", "w", "preset", "refresh", "cont"])
    assert code == 0, out + err
    assert ws.config_path.read_text() == before


# ---- container half (env / mount / setup) ------------------------------------


_CONT = """
    [env]
    C_VAR = "one"
    D_VAR = "two"
    [[setup]]
    run = "bash /opt/c.sh"
    order = 45
    [[mount]]
    volume = "cache"
    target = "/cache"
"""


def test_refresh_container_half_update_and_prune(xdg, monkeypatch):
    """env value change -> updated; setup run change -> updated; a dropped env
    key + dropped mount -> pruned under --prune. All in one all-or-nothing pass."""
    _write_preset("cont", _CONT)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "cont")
    # Drop D_VAR + the cache mount; change C_VAR's value and setup's run.
    _write_preset("cont", """
        [env]
        C_VAR = "CHANGED"
        [[setup]]
        run = "bash /opt/c2.sh"
        order = 45
    """)
    # A live container -> the container-half change flags the spec-drift hint
    # (finding 9a gates the hint on container existence).
    from credproxy_cli.porcelain import cli as pcli
    monkeypatch.setattr(pcli.core_docker, "container_status", lambda _n: "running")
    code, out, err = _run(
        ["workspace", "w", "preset", "refresh", "cont", "--prune"])
    assert code == 0, out + err
    from credproxy_cli.core.config import load_config
    cfg = load_config(ws)
    assert cfg["env"] == {"C_VAR": "CHANGED"}
    assert cfg["setup"][0]["run"] == "bash /opt/c2.sh"
    assert cfg["mounts"] == []
    # A container-half change flags the spec-drift restart hint.
    assert "restart to apply" in (out + err)


# ---- update cleanly ----------------------------------------------------------


def test_refresh_updates_changed_block_only(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    before = ws.config_path.read_text()
    # gh-api's on-disk chunk (marker + block) must survive byte-for-byte.
    gh_api_chunk = before.split("# credproxy:preset")[1]

    # Change gh-git's hosts.
    _write_preset("gh", _GH.replace('hosts = ["github.com"]',
                                    'hosts = ["github.com", "ghcr.io"]'))
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    after = ws.config_path.read_text()
    assert after != before
    # gh-api chunk untouched (its marker rev unchanged, block identical).
    assert after.split("# credproxy:preset")[1] == gh_api_chunk
    # gh-git now carries the new host + a re-parseable config.
    from credproxy_cli.core.bindings import load_bindings
    hosts = {b.name: b.hosts for b in load_bindings(ws)}
    assert hosts["gh-git"] == ("github.com", "ghcr.io")
    assert "1 updated" in (out + err)
    # Idempotent: a second refresh is a no-op.
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0 and ws.config_path.read_text() == after


def test_refresh_preserves_placeholder_and_credential(xdg):
    """The shared placeholder + provider/secret are read back from the stamped
    bindings and preserved byte-for-byte across an update (never regenerated)."""
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    from credproxy_cli.core.bindings import load_bindings
    ph_before = {b.name: b.placeholder for b in load_bindings(ws)}
    prov_before = {b.name: (b.provider, b.secret) for b in load_bindings(ws)}

    _write_preset("gh", _GH.replace('hosts = ["github.com"]',
                                    'hosts = ["example.com"]'))
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    after = load_bindings(ws)
    assert {b.name: b.placeholder for b in after} == ph_before
    assert {b.name: (b.provider, b.secret) for b in after} == prov_before
    # One shared placeholder across all parts, unchanged.
    assert len({b.placeholder for b in after}) == 1


# ---- skip hand-edited --------------------------------------------------------


def test_refresh_skips_hand_edited_with_diff(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    text = ws.config_path.read_text()
    # Hand-edit gh-api's env line (a change refresh must NOT overwrite).
    edited = text.replace('env      = "GITHUB_TOKEN"', 'env      = "GH_PAT"')
    ws.config_path.write_text(edited)

    # Change gh-git in the definition too, so refresh has real work on the OTHER
    # block while skipping the edited one (report-all, not fail-first).
    _write_preset("gh", _GH.replace('hosts = ["github.com"]',
                                    'hosts = ["github.com", "ghcr.io"]'))
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    after = ws.config_path.read_text()
    # The hand-edit survives (never overwritten).
    assert 'env      = "GH_PAT"' in after
    assert "skipped (hand-edited)" in (out + err)
    assert "diff for binding gh-api" in (out + err)
    # gh-git was still refreshed.
    from credproxy_cli.core.bindings import load_bindings
    assert {b.name: b.hosts for b in load_bindings(ws)}["gh-git"] \
        == ("github.com", "ghcr.io")


def test_refresh_json_diff_present(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    text = ws.config_path.read_text()
    ws.config_path.write_text(
        text.replace('hosts    = ["api.github.com"]',
                     'hosts    = ["api.github.com", "extra.example"]'))
    code, out, err = _run(["--json", "workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    obj = json.loads(out)
    acts = {(a["kind"], a["target"]): a for a in obj["presets"][0]["actions"]}
    assert acts[("binding", "gh-api")]["action"] == "skipped-edited"
    assert acts[("binding", "gh-api")]["diff"]      # a non-empty diff string
    assert acts[("binding", "gh-git")]["action"] == "up-to-date"


# ---- added block -------------------------------------------------------------


def test_refresh_adds_new_part_reusing_identity(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    from credproxy_cli.core.bindings import load_bindings
    shared_ph = load_bindings(ws)[0].placeholder

    # Definition gains a third part.
    _write_preset("gh", _GH + """
        [[part]]
        suffix = "ghcr"
        injector = "basic"
        hosts = ["ghcr.io"]
    """)
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert "1 added" in (out + err)
    bs = {b.name: b for b in load_bindings(ws)}
    assert "gh-ghcr" in bs
    # The new part reuses the siblings' shared placeholder + provider/secret.
    assert bs["gh-ghcr"].placeholder == shared_ph
    assert (bs["gh-ghcr"].provider, bs["gh-ghcr"].secret) == ("env", "TOK")


# ---- rules -------------------------------------------------------------------


_RULEPACK = """
    [[rule]]
    suffix = "block-x"
    action = "block"
    hosts = ["x.example"]
"""


def test_refresh_rule_pack_add_and_update(xdg):
    """A pure-rule pack: definition adds a second rule and changes the first's
    host -> the first is updated cleanly, the second is added."""
    _write_preset("guard", _RULEPACK)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "guard")
    _write_preset("guard", """
        [[rule]]
        suffix = "block-x"
        action = "block"
        hosts = ["x.example", "y.example"]
        [[rule]]
        suffix = "block-z"
        action = "block"
        hosts = ["z.example"]
    """)
    code, out, err = _run(["workspace", "w", "preset", "refresh", "guard"])
    assert code == 0, out + err
    from credproxy_cli.core.rules import load_rules
    rules = {r.name: r.hosts for r in load_rules(ws)}
    assert rules["guard-block-x"] == ("x.example", "y.example")
    assert rules["guard-block-z"] == ("z.example",)
    assert "1 updated" in (out + err) and "1 added" in (out + err)


# ---- prune -------------------------------------------------------------------


def test_refresh_reports_prunable_without_flag(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    before = ws.config_path.read_text()
    # Definition drops the git part.
    _write_preset("gh", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]
        env = "GITHUB_TOKEN"
    """)
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    # Reported, NOT removed.
    assert "--prune" in (out + err)
    assert ws.config_path.read_text() == before
    from credproxy_cli.core.bindings import load_bindings
    assert {b.name for b in load_bindings(ws)} == {"gh-api", "gh-git"}


def test_refresh_prune_removes_vanished_block(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    _write_preset("gh", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]
        env = "GITHUB_TOKEN"
    """)
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh", "--prune"])
    assert code == 0, out + err
    assert "pruned" in (out + err)
    from credproxy_cli.core.bindings import load_bindings
    assert {b.name for b in load_bindings(ws)} == {"gh-api"}
    # The remaining block is intact + re-parseable, no orphan marker for gh-git.
    from credproxy_cli.core import preset_stamp
    text = ws.config_path.read_text()
    assert "gh-git" not in text
    assert preset_stamp.applied_preset_names(text) == ["gh"]


def test_refresh_prune_gated_on_implicit_default_no_tty(xdg):
    """`--prune` is destructive: on the loose surface, an implicit (default)
    workspace fails closed without a TTY (like `binding remove`)."""
    from credproxy_cli.core.pointer import set_default
    from credproxy_cli.core.workspace import Workspace
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    set_default(Workspace("w"))
    _write_preset("gh", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]
        env = "GITHUB_TOKEN"
    """)
    before = ws.config_path.read_text()
    # Implicit workspace (no NAME) + --prune, stdin not a TTY -> refuse.
    code, out, err = _run_loose(["preset", "refresh", "--prune"])
    assert code == 1
    assert "confirmation" in (out + err) or "TTY" in (out + err)
    assert ws.config_path.read_text() == before

    # --yes bypasses the gate.
    code, out, err = _run_loose(["preset", "refresh", "--prune", "--yes"])
    assert code == 0, out + err
    from credproxy_cli.core.bindings import load_bindings
    assert {b.name for b in load_bindings(ws)} == {"gh-api"}


def test_refresh_explicit_name_prune_not_gated(xdg):
    """An EXPLICIT workspace name never prompts, even with --prune."""
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    _write_preset("gh", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]
        env = "GITHUB_TOKEN"
    """)
    code, out, err = _run_loose(
        ["workspace", "w", "preset", "refresh", "gh", "--prune"])
    assert code == 0, out + err


# ---- unresolvable pack -------------------------------------------------------


def test_refresh_explicit_unresolvable_errors(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    # Remove the pack from the registry.
    from credproxy_cli.core.paths import config_dir
    (config_dir() / "presets" / "gh.toml").unlink()
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 1
    assert "no longer in the registry" in (out + err)


def test_refresh_all_skips_unresolvable_with_note(xdg):
    _write_preset("gh", _GH)
    _write_preset("other", """
        [[rule]]
        suffix = "block-x"
        action = "block"
        hosts = ["x.example"]
    """)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    _stamp(ws, "other")
    before = ws.config_path.read_text()
    from credproxy_cli.core.paths import config_dir
    (config_dir() / "presets" / "gh.toml").unlink()   # gh now unresolvable
    # Refresh ALL: gh skipped-with-note, other still processed.
    code, out, err = _run(["workspace", "w", "preset", "refresh"])
    assert code == 0, out + err
    assert "no longer in the registry" in (out + err)
    # `other` had no definition change -> still a no-op, file unchanged.
    assert ws.config_path.read_text() == before


def test_refresh_explicit_not_applied_errors(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)   # nothing stamped
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 1
    assert "not applied" in (out + err)


# ---- attached ----------------------------------------------------------------


def test_refresh_attached_refuses_container_half(xdg):
    base = _write_preset("cont", _GH + """
        [env]
        C_VAR = "one"
    """)
    ws = _make_ws("attd", 'attach = { container = "extbox" }\n')
    # Stamp only the binding half (attach can't take env) so there IS an applied
    # marker, then a container-half definition triggers the refusal.
    from credproxy_cli.core import preset_stamp
    from credproxy_cli.core.presets import build_preset
    exp = build_preset("cont", "env", "TOK")
    preset_stamp.stamp(ws, "cont", exp.rev, bindings=list(exp.bindings),
                       rules=[], mounts=[], env_items=[], setup=[])
    code, out, err = _run(["workspace", "attd", "preset", "refresh", "cont"])
    assert code == 1
    assert "attached" in (out + err)


def test_refresh_attached_binding_only_ok(xdg):
    _write_preset("gh", _GH)
    ws = _make_ws("attd", 'attach = { container = "extbox" }\n')
    _stamp(ws, "gh", "env", "TOK")
    before = ws.config_path.read_text()
    code, out, err = _run(["workspace", "attd", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert ws.config_path.read_text() == before   # no-op, no refusal


# ---- #59 v2 review: multiline-safe marker scan (BLOCKER, finding 1) -----------


def test_multiline_string_mask_indices(xdg):
    """`multiline_string_line_indices` marks every line whose START is inside a
    triple-quoted string (so the marker/blockspan scanners skip them)."""
    from credproxy_cli.core.preset_stamp import multiline_string_line_indices as M
    t = 'a = 1\nrun = """\nline in str\nstill in str\n"""\nb = 2\n'
    # 0:a=1  1:run=""" (opens, not masked)  2,3:inside  4:""" (starts inside)  5:b=2
    assert M(t) == frozenset({2, 3, 4})
    # a single-line triple string opens+closes on one line -> nothing masked
    assert M('x = """v"""\ny = 2\n') == frozenset()


def test_applied_names_ignores_marker_in_multiline_string(xdg):
    """A full-shape marker copied into a `\"\"\"...\"\"\"` value is a string value,
    never a discovered pack; a real trailing comment after a closed single-line
    triple string still counts."""
    from credproxy_cli.core.preset_stamp import applied_preset_names
    m = "# credproxy:preset name=ghost rev=aaaaaaaaaaaa sha=bbbbbbbbbbbb"
    # interior of a multiline string
    assert applied_preset_names(
        f'x = "y"\n[[setup]]\nrun = """\n{m}\n"""\norder = 1\n') == []
    # a marker-shaped substring inside a single-line triple string
    assert applied_preset_names(f'note = """{m}"""\n') == []
    # a genuine trailing comment after a closed single-line triple string IS seen
    assert applied_preset_names(
        'x = """v"""  # credproxy:preset name=real '
        'rev=aaaaaaaaaaaa sha=bbbbbbbbbbbb\n') == ["real"]


def test_refresh_ignores_marker_copied_into_multiline_string(xdg):
    """The exact review probe: a stamped env marker line COPIED into a hand-written
    `[[setup]]` `run = \"\"\"...\"\"\"` script must never be treated as a real
    stamp -- refresh updates only the genuine env and leaves the string's foreign
    bytes byte-identical (never a foreign-byte rewrite, never a silent mis-target).
    """
    _write_preset("cont", '[env]\nC_VAR = "one"\n')
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "cont")
    text = ws.config_path.read_text()
    marker_line = next(l for l in text.splitlines() if l.startswith("C_VAR"))
    # A user's own setup script echoes config -- including a copy of the marker.
    poisoned = text + (
        '\n[[setup]]\nrun = """\n'
        'echo building\n'
        f'{marker_line}\n'
        '"""\norder = 5\n')
    ws.config_path.write_text(poisoned)
    # Change the definition value.
    _write_preset("cont", '[env]\nC_VAR = "two"\n')
    code, out, err = _run(["workspace", "w", "preset", "refresh", "cont"])
    assert code == 0, out + err
    after = ws.config_path.read_text()
    # The copy inside the multiline string is byte-preserved (foreign bytes safe).
    assert marker_line in after
    # The genuine env updated; the string still reads "one".
    from credproxy_cli.core.config import load_config
    assert load_config(ws)["env"]["C_VAR"] == "two"
    assert after.count('C_VAR = "two"') == 1


def test_refresh_fails_closed_on_duplicate_identity(xdg):
    """Belt-and-suspenders: two stamped items sharing (kind, identity) -- e.g. a
    hand-duplicated `[[setup]]` block, both carrying a real marker -- fail closed
    (no write), never silently mis-target an edit."""
    _write_preset("cont", '[[setup]]\nrun = "bash /opt/c.sh"\norder = 45\n')
    ws = _make_ws("w", _WS_MIN)
    from credproxy_cli.core import preset_stamp
    from credproxy_cli.core.presets import build_preset
    exp = build_preset("cont")
    preset_stamp.stamp(ws, "cont", exp.rev, bindings=[], rules=[], mounts=[],
                       env_items=[], setup=[dict(s) for s in exp.setup])
    # Convert the inline stamped setup into two identical [[setup]] blocks (same
    # order=45), each under a real marker -- a duplicate join identity.
    marker = preset_stamp._marker(
        "cont", exp.rev, '[[setup]]\nrun = "bash /opt/c.sh"\norder = 45\n')
    blk = (f'{marker}\n[[setup]]\nrun = "bash /opt/c.sh"\norder = 45\n')
    ws.config_path.write_text(_WS_MIN + "\n" + blk + "\n" + blk)
    before = ws.config_path.read_text()
    code, out, err = _run(["workspace", "w", "preset", "refresh", "cont"])
    assert code == 1
    assert "identity" in (out + err) and "refusing" in (out + err)
    assert ws.config_path.read_text() == before   # no write


# ---- finding 2: trailing hand comment after a stamped block ------------------


def test_refresh_tolerates_trailing_hand_comment(xdg):
    """A hand comment appended after the last stamped block is not folded into the
    block body -- the block still classifies up-to-date, zero writes."""
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    ws.config_path.write_text(
        ws.config_path.read_text() + "# a hand note about gh-git\n")
    before = ws.config_path.read_text()
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert ws.config_path.read_text() == before   # zero writes
    assert "skipped (hand-edited)" not in (out + err)
    assert "up to date" in (out + err)


# ---- finding 3: edited-block detection precedes would-write; divergence -------


def test_refresh_divergent_bindings_skipped_no_rotation(xdg):
    """A hand-edited placeholder on ONE stamped binding makes the pack's bindings
    divergent -> the whole binding half is skipped (never rotated to the first
    binding's placeholder), and the edited binding is never masked as up-to-date.
    """
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    from credproxy_cli.core.bindings import load_bindings
    ph_before = {b.name: b.placeholder for b in load_bindings(ws)}
    # Hand-edit gh-api's placeholder only (first occurrence in file order).
    text = ws.config_path.read_text()
    ws.config_path.write_text(text.replace(
        'placeholder = "ghp_', 'placeholder = "zzz_', 1))
    before = ws.config_path.read_text()
    code, out, err = _run(["--json", "workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    obj = json.loads(out)
    acts = {(a["kind"], a["target"]): a["action"]
            for a in obj["presets"][0]["actions"]}
    assert acts[("binding", "gh-api")] == "skipped-divergent"
    assert acts[("binding", "gh-git")] == "skipped-divergent"
    # No credential rotation: the file is untouched.
    assert ws.config_path.read_text() == before
    after = {b.name: b.placeholder for b in load_bindings(ws)}
    assert after["gh-git"] == ph_before["gh-git"]   # sibling NOT rewritten


# ---- finding 4: attached container-half skip is labelled 'attached' ----------


def test_refresh_all_attached_container_half_labelled_attached(xdg):
    """A refresh-all on an attached workspace that skips a container-half pack
    reports it as attached-skipped, NOT 'no longer in the registry' (finding 4)."""
    _write_preset("cont", _GH + '\n[env]\nC_VAR = "one"\n')
    ws = _make_ws("attd", 'attach = { container = "extbox" }\n')
    from credproxy_cli.core import preset_stamp
    from credproxy_cli.core.presets import build_preset
    exp = build_preset("cont", "env", "TOK")
    preset_stamp.stamp(ws, "cont", exp.rev, bindings=list(exp.bindings),
                       rules=[], mounts=[], env_items=[], setup=[])
    code, out, err = _run(["--json", "workspace", "attd", "preset", "refresh"])
    assert code == 0, out + err
    obj = json.loads(out)
    assert obj["skipped_attached"] == ["cont"]
    assert obj["skipped_unresolved"] == []
    # Human surface says 'attached', never 'no longer in the registry'.
    code, out, err = _run(["workspace", "attd", "preset", "refresh"])
    assert "attached" in (out + err)
    assert "no longer in the registry" not in (out + err)


# ---- finding 6: pack rename reported legibly ---------------------------------


def test_refresh_pack_rename_reports_legible_message(xdg):
    """A pack that renamed a part's suffix (old vanishes, new appears on the same
    host/placeholder) collides without --prune -> a legible 'looks like a rename'
    message, no write (not the raw validate collision)."""
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    before = ws.config_path.read_text()
    _write_preset("gh", _GH.replace('suffix = "git"', 'suffix = "scm"'))
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 1
    assert "rename" in (out + err).lower()
    assert ws.config_path.read_text() == before   # no write


# ---- finding 7: def-new item colliding with unmanaged config -----------------


def test_refresh_added_mount_collision_is_skipped_collision(xdg, monkeypatch):
    """A definition-new mount whose target already exists as UNMANAGED config is
    reported as skipped-collision (not skipped-edited -- nothing was hand-edited).
    """
    _write_preset("cont", '[env]\nC_VAR = "one"\n')
    ws = _make_ws("w", _WS_MIN
                  + 'mounts = [{ volume = "existing", target = "/data" }]\n')
    _stamp(ws, "cont")
    _write_preset(
        "cont", '[env]\nC_VAR = "one"\n[[mount]]\nvolume = "packy"\ntarget = "/data"\n')
    from credproxy_cli.porcelain import cli as pcli
    monkeypatch.setattr(pcli.core_docker, "container_status", lambda _n: None)
    code, out, err = _run(["--json", "workspace", "w", "preset", "refresh", "cont"])
    assert code == 0, out + err
    obj = json.loads(out)
    acts = {(a["kind"], a["target"]): a["action"]
            for a in obj["presets"][0]["actions"]}
    assert acts[("mount", "/data")] == "skipped-collision"


# ---- finding 8: env classification tolerates cosmetic re-indent --------------


def test_refresh_tolerates_reindented_env(xdg):
    """Re-indenting a stamped env key is cosmetic -> still up-to-date, zero writes
    (the stamp writes it unindented; classification strips to bare)."""
    _write_preset("cont", '[env]\nC_VAR = "one"\n')
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "cont")
    reindented = ws.config_path.read_text().replace(
        'C_VAR = "one"', '    C_VAR = "one"')
    ws.config_path.write_text(reindented)
    code, out, err = _run(["workspace", "w", "preset", "refresh", "cont"])
    assert code == 0, out + err
    assert "up to date" in (out + err)
    assert "skipped (hand-edited)" not in (out + err)
    assert ws.config_path.read_text() == reindented   # zero writes


# ---- finding 9b: --json omits diff when null ---------------------------------


def test_refresh_json_omits_null_diff(xdg):
    """An up-to-date action carries no diff -> the JSON key is absent (diff?),
    not `"diff": null`."""
    _write_preset("gh", _GH)
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "gh", "env", "TOK")
    code, out, err = _run(["--json", "workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    obj = json.loads(out)
    for a in obj["presets"][0]["actions"]:
        assert "diff" not in a          # no null diff on up-to-date blocks


def test_refresh_single_binding_edit_not_masked_up_to_date(xdg):
    """Finding 3a: a lone stamped binding whose placeholder was hand-edited -- the
    kept credential is read from that very binding, so would-write matches on
    disk. The sha-first check must still classify it skipped-edited (not mask it
    as up-to-date and rotate on a later run)."""
    _write_preset("solo",
                  '[placeholder]\nprefix = "ghp_"\nlength = 40\ncharset = "alnumeric"\n'
                  '[[part]]\nsuffix = "api"\ninjector = "bearer"\n'
                  'hosts = ["api.github.com"]\n')
    ws = _make_ws("w", _WS_MIN)
    _stamp(ws, "solo", "env", "TOK")
    ws.config_path.write_text(ws.config_path.read_text().replace(
        'placeholder = "ghp_', 'placeholder = "zzz_', 1))
    before = ws.config_path.read_text()
    code, out, err = _run(["--json", "workspace", "w", "preset", "refresh", "solo"])
    assert code == 0, out + err
    obj = json.loads(out)
    acts = {(a["kind"], a["target"]): a["action"]
            for a in obj["presets"][0]["actions"]}
    assert acts[("binding", "solo-api")] == "skipped-edited"
    assert ws.config_path.read_text() == before   # not masked, not rotated
