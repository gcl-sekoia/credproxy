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


def test_refresh_container_half_update_and_prune(xdg):
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
