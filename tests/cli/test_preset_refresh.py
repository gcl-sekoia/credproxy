"""`preset refresh` + `preset remove` on the config-v2 reference/snapshot model
(#64).

Refresh is "force re-expand + structurally diff two snapshots" (the resolver's
ONE re-expand path, so identity/placeholder are preserved); remove is a
whole-block delete of the `[[preset]]` reference (+ its `[preset.options]` /
`[preset.override.*]` child sub-tables) plus dropping the lock snapshot.

The old span/sha three-way-classification machinery is gone by construction --
there is no stamped text to hand-edit -- so those states are not tested. The
SEMANTIC assertions (up-to-date, new-part addition, vanished-part removal,
placeholder stability) are ported here onto the new model.
"""
from __future__ import annotations

import json
import textwrap

from test_porcelain import _run, _run_loose


def _preset(name: str, toml: str):
    from credproxy_cli.core.paths import config_dir
    d = config_dir() / "presets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(textwrap.dedent(toml))
    return d / f"{name}.toml"


def _ws(workspaces_dir, name, content):
    from credproxy_cli.core.model.workspace import Workspace
    (workspaces_dir / f"{name}.toml").write_text(textwrap.dedent(content))
    return Workspace(name)


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

_GH_REF = 'image = "x"\n[[preset]]\nname = "gh"\nprovider = "env"\nsecret = "TOK"\n'


def _add_and_lock(workspaces_dir):
    """A `gh`-referencing workspace with the snapshot minted + persisted (the
    state `preset add` leaves)."""
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("gh", _GH)
    ws = _ws(workspaces_dir, "w", _GH_REF)
    save_lock(ws, resolve_workspace(ws).lock)
    return ws


# ---- round-trip anchor -------------------------------------------------------


def test_refresh_immediately_after_add_is_zero_write(xdg, workspaces_dir):
    """The anchor: refresh right after add -> changed=false, lockfile
    byte-identical (zero writes)."""
    ws = _add_and_lock(workspaces_dir)
    lock_before = ws.lock_json_path.read_text()
    toml_before = ws.config_path.read_text()
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert "up to date" in out
    assert ws.lock_json_path.read_text() == lock_before      # byte-identical
    assert ws.config_path.read_text() == toml_before     # never touches the TOML


def test_refresh_json_unchanged_shape(xdg, workspaces_dir):
    ws = _add_and_lock(workspaces_dir)
    code, out, err = _run(["--json", "workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    obj = json.loads(out)
    p = obj["presets"][0]
    assert p["preset"] == "gh" and p["changed"] is False
    assert p["definition_rev"]["old"] == p["definition_rev"]["new"]
    assert p["entries"] == []


# ---- definition edit: check vs apply -----------------------------------------


def test_refresh_check_shows_diff_writes_nothing(xdg, workspaces_dir):
    ws = _add_and_lock(workspaces_dir)
    lock_before = ws.lock_json_path.read_text()
    # Change gh-git's hosts in the definition.
    _preset("gh", _GH.replace('hosts = ["github.com"]',
                              'hosts = ["github.com", "ghcr.io"]'))
    code, out, err = _run(
        ["--json", "workspace", "w", "preset", "refresh", "gh", "--check"])
    assert code == 0, out + err
    obj = json.loads(out)
    assert obj["check"] is True
    p = obj["presets"][0]
    assert p["changed"] is True
    ent = {(e["kind"], e["name"]): e for e in p["entries"]}
    assert ent[("binding", "gh-git")]["action"] == "changed"
    assert ent[("binding", "gh-git")]["diff"]           # a non-empty diff string
    # --check writes NOTHING.
    assert ws.lock_json_path.read_text() == lock_before


def test_refresh_apply_persists_and_resolve_reflects(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _add_and_lock(workspaces_dir)
    _preset("gh", _GH.replace('hosts = ["github.com"]',
                              'hosts = ["github.com", "ghcr.io"]'))
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert "changed" in out
    # A subsequent resolve reflects the new material (no more dirty).
    r = resolve_workspace(ws)
    assert r.lock_dirty is False
    hosts = {b.name: b.hosts for b in r.bindings}
    assert hosts["gh-git"] == ("github.com", "ghcr.io")


# ---- semantic ports ----------------------------------------------------------


def test_refresh_adds_new_part_reusing_placeholder(xdg, workspaces_dir):
    """A definition-new part is 'added' and shares the locked placeholder."""
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _add_and_lock(workspaces_dir)
    shared = {b.placeholder for b in resolve_workspace(ws).bindings}
    assert len(shared) == 1
    _preset("gh", _GH + """
        [[part]]
        suffix = "ghcr"
        injector = "basic"
        hosts = ["ghcr.io"]
    """)
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert "1 added" in out and "gh-ghcr" in out
    bs = {b.name: b for b in resolve_workspace(ws).bindings}
    assert "gh-ghcr" in bs
    # New part reuses the siblings' shared placeholder (never rotated).
    assert bs["gh-ghcr"].placeholder == next(iter(shared))
    assert {b.placeholder for b in bs.values()} == shared


def test_refresh_removes_vanished_part(xdg, workspaces_dir):
    """A part dropped from the definition simply vanishes from the snapshot --
    the diff's 'removed' case, no --prune flag."""
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _add_and_lock(workspaces_dir)
    # Drop the git part.
    _preset("gh", """
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
    code, out, err = _run(
        ["--json", "workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    ent = {(e["kind"], e["name"]): e
           for e in json.loads(out)["presets"][0]["entries"]}
    assert ent[("binding", "gh-git")]["action"] == "removed"
    assert {b.name for b in resolve_workspace(ws).bindings} == {"gh-api"}


def test_refresh_placeholder_stable_across_change(xdg, workspaces_dir):
    """Placeholder stability: a refresh that modifies a part leaves the shared
    placeholder unchanged."""
    from credproxy_cli.core.model.resolver import resolve_workspace
    ws = _add_and_lock(workspaces_dir)
    ph_before = {b.placeholder for b in resolve_workspace(ws).bindings}
    _preset("gh", _GH.replace('hosts = ["api.github.com"]',
                              'hosts = ["api.github.com", "uploads.github.com"]'))
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert {b.placeholder for b in resolve_workspace(ws).bindings} == ph_before


def test_refresh_rule_pack_add_and_change(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("guard", """
        [[rule]]
        suffix = "block-x"
        action = "block"
        hosts = ["x.example"]
    """)
    from credproxy_cli.core.model.lock import save_lock
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "guard"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    _preset("guard", """
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
    rules = {r.name: r.hosts for r in resolve_workspace(ws).rules}
    assert rules["guard-block-x"] == ("x.example", "y.example")
    assert rules["guard-block-z"] == ("z.example",)


# ---- container half ----------------------------------------------------------


def test_refresh_container_half_env_and_setup(xdg, workspaces_dir, monkeypatch):
    """Env value change + setup run change surface as entry diffs and the
    spec-drift restart hint fires when the container exists."""
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.lock import save_lock
    _preset("cont", """
        [env]
        C_VAR = "one"
        [[setup]]
        run = "bash /opt/c.sh"
        order = 45
    """)
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "cont"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    _preset("cont", """
        [env]
        C_VAR = "CHANGED"
        [[setup]]
        run = "bash /opt/c2.sh"
        order = 45
    """)
    from credproxy_cli.porcelain import cli as pcli
    monkeypatch.setattr(pcli.core_docker, "container_status", lambda _n: "running")
    code, out, err = _run(["workspace", "w", "preset", "refresh", "cont"])
    assert code == 0, out + err
    assert "restart to apply" in err
    cfg = resolve_workspace(ws).config
    assert cfg["env"] == {"C_VAR": "CHANGED"}
    assert cfg["setup"][-1]["run"] == "bash /opt/c2.sh"


# ---- collision ---------------------------------------------------------------


def test_refresh_collision_fails_atomically_naming_both(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("gh", _GH)
    ws = _ws(workspaces_dir, "w", _GH_REF + textwrap.dedent("""
        [[binding]]
        name = "gh-ghcr"
        injector = "bearer"
        provider = "env"
        secret = "T"
        hosts = ["other.example"]
    """))
    save_lock(ws, resolve_workspace(ws).lock)
    lock_before = ws.lock_json_path.read_text()
    # The definition grows a `ghcr` part -> collides with the literal `gh-ghcr`.
    _preset("gh", _GH + """
        [[part]]
        suffix = "ghcr"
        injector = "basic"
        hosts = ["ghcr.io"]
    """)
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 1
    assert "gh-ghcr" in (out + err) and "collides with a literal" in (out + err)
    assert ws.lock_json_path.read_text() == lock_before       # nothing written


# ---- targeting ---------------------------------------------------------------


def test_refresh_named_pack_not_referenced_errors(xdg, workspaces_dir):
    _add_and_lock(workspaces_dir)
    code, out, err = _run(["workspace", "w", "preset", "refresh", "nope"])
    assert code == 1
    assert "not referenced" in (out + err)


def test_refresh_all_refs_when_no_name(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.lock import save_lock
    _preset("gh", _GH)
    _preset("guard",
            '[[rule]]\nsuffix = "b"\nhosts = ["x.example"]\naction = "block"\n')
    ws = _ws(workspaces_dir, "w",
             _GH_REF + '[[preset]]\nname = "guard"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    # Edit both packs.
    _preset("gh", _GH.replace('hosts = ["github.com"]',
                              'hosts = ["github.com", "ghcr.io"]'))
    _preset("guard",
            '[[rule]]\nsuffix = "b"\nhosts = ["x.example", "y.example"]\n'
            'action = "block"\n')
    code, out, err = _run(["--json", "workspace", "w", "preset", "refresh"])
    assert code == 0, out + err
    names = {p["preset"] for p in json.loads(out)["presets"]}
    assert names == {"gh", "guard"}


# ---- safety gate -------------------------------------------------------------


def test_refresh_gated_on_implicit_default_when_changed_no_tty(xdg, workspaces_dir):
    """A non-`--check` refresh with real changes on an IMPLICIT (default)
    workspace fails closed without a TTY (like the destructive set)."""
    from credproxy_cli.core.model.pointer import set_default
    from credproxy_cli.core.model.workspace import Workspace
    ws = _add_and_lock(workspaces_dir)
    set_default(Workspace("w"))
    _preset("gh", _GH.replace('hosts = ["github.com"]',
                              'hosts = ["github.com", "ghcr.io"]'))
    lock_before = ws.lock_json_path.read_text()
    code, out, err = _run_loose(["preset", "refresh"])
    assert code == 1
    assert "confirmation" in (out + err) or "TTY" in (out + err)
    assert ws.lock_json_path.read_text() == lock_before       # nothing written


def test_refresh_check_never_gates(xdg, workspaces_dir):
    """`--check` never gates -- even implicit + changed on loose, no TTY."""
    from credproxy_cli.core.model.pointer import set_default
    from credproxy_cli.core.model.workspace import Workspace
    ws = _add_and_lock(workspaces_dir)
    set_default(Workspace("w"))
    _preset("gh", _GH.replace('hosts = ["github.com"]',
                              'hosts = ["github.com", "ghcr.io"]'))
    code, out, err = _run_loose(["preset", "refresh", "--check"])
    assert code == 0, out + err


def test_refresh_unchanged_never_gates(xdg, workspaces_dir):
    """No diff -> no gate even on an implicit default without a TTY."""
    from credproxy_cli.core.model.pointer import set_default
    from credproxy_cli.core.model.workspace import Workspace
    ws = _add_and_lock(workspaces_dir)
    set_default(Workspace("w"))
    code, out, err = _run_loose(["preset", "refresh"])
    assert code == 0, out + err
    assert "up to date" in out


# ---- preset remove -----------------------------------------------------------


def test_remove_leaves_toml_byte_identical_except_block(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import load_lock, save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("gh", _GH)
    ws = _ws(workspaces_dir, "w", textwrap.dedent('''\
        image = "x"

        # a sacred comment
        [[binding]]
        name = "lit"
        injector = "bearer"
        provider = "env"
        secret = "T"
        hosts = ["lit.com"]

        [[preset]]
        name     = "gh"
        provider = "env"
        secret   = "TOK"
        '''))
    save_lock(ws, resolve_workspace(ws).lock)
    code, out, err = _run(["workspace", "w", "preset", "remove", "gh"])
    assert code == 0, out + err
    after = ws.config_path.read_text()
    assert "gh" not in after
    assert "# a sacred comment" in after and 'name = "lit"' in after
    # Lock snapshot dropped.
    assert "gh" not in load_lock(ws).get("presets", {})
    # A resolve afterward shows no trace.
    assert {b.name for b in resolve_workspace(ws).bindings} == {"lit"}


def test_remove_deletes_child_subtables(xdg, workspaces_dir):
    """The critical case: `[preset.options]` + `[preset.override.*]` child tables
    must be folded into the block span and deleted with it (no orphans)."""
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("svc", """
        [placeholder]
        prefix = "svc_"
        length = 12
        charset = "alnumeric"
        [[option]]
        id = "sock"
        type = "string"
        default = "/a"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.svc.com"]
        env = "SVC"
        [[mount]]
        bind = { option = "sock" }
        target = "/s"
    """)
    ws = _ws(workspaces_dir, "w", textwrap.dedent('''\
        image = "x"

        [[preset]]
        name     = "svc"
        provider = "env"
        secret   = "SVC"
        [preset.options]
        sock = "/run/x"
        [preset.override.api]
        hosts = ["api.svc.example"]

        [[preset]]
        name = "svc2ndkeep"
        '''))
    _preset("svc2ndkeep",
            '[[rule]]\nsuffix = "b"\nhosts = ["x.example"]\naction = "block"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    code, out, err = _run(["workspace", "w", "preset", "remove", "svc"])
    assert code == 0, out + err
    after = ws.config_path.read_text()
    assert "[preset.options]" not in after
    assert "[preset.override.api]" not in after
    assert "svc" not in after.replace("svc2ndkeep", "")   # no svc trace
    assert 'name = "svc2ndkeep"' in after                 # the other ref survives
    # File still parses + resolves clean.
    r = resolve_workspace(ws)
    assert [b.name for b in r.bindings] == []
    assert [rl.name for rl in r.rules] == ["svc2ndkeep-b"]


def test_remove_unknown_pack_errors(xdg, workspaces_dir):
    _add_and_lock(workspaces_dir)
    code, out, err = _run(["workspace", "w", "preset", "remove", "nope"])
    assert code == 1
    assert "not referenced" in (out + err)


def test_remove_gated_on_implicit_default_no_tty(xdg, workspaces_dir):
    from credproxy_cli.core.model.pointer import set_default
    from credproxy_cli.core.model.workspace import Workspace
    ws = _add_and_lock(workspaces_dir)
    set_default(Workspace("w"))
    before = ws.config_path.read_text()
    # Implicit workspace (no NAME) + no TTY -> the gate refuses.
    code, out, err = _run_loose(["preset", "remove", "gh"])
    assert code == 1
    assert "confirmation" in (out + err) or "TTY" in (out + err)
    assert ws.config_path.read_text() == before


def test_remove_json_shape(xdg, workspaces_dir):
    ws = _add_and_lock(workspaces_dir)
    code, out, err = _run(["--json", "workspace", "w", "preset", "remove", "gh"])
    assert code == 0, out + err
    obj = json.loads(out)
    assert obj["removed"] == "gh"
    assert {b["name"] for b in obj["bindings"]} == {"gh-api", "gh-git"}
    assert set(obj["no_longer_intercepted"]) == {"api.github.com", "github.com"}


# ---- preset remove is resolution-free (#64 fix 1) ----------------------------
# `preset remove` must succeed exactly when removal is the FIX -- i.e. when the
# model does NOT resolve. The pre- and post-edit resolves are best-effort
# reporting only; the sole hard preconditions are "the pack is referenced" + the
# destructive gate (mirrors `binding remove`, which never resolves).


def test_remove_succeeds_despite_unrelated_literal_collision(xdg, workspaces_dir):
    """(a) A literal `[[binding]]` colliding with the preset expansion makes the
    model unresolvable -- yet `preset remove` (removing the collision) succeeds."""
    import pytest
    from credproxy_cli.core.model.lock import load_lock, save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("gh", _GH)
    ws = _ws(workspaces_dir, "w", _GH_REF)
    save_lock(ws, resolve_workspace(ws).lock)        # valid lock minted here
    # Introduce a literal binding colliding with the preset-expanded `gh-api`.
    ws.config_path.write_text(ws.config_path.read_text() + textwrap.dedent("""
        [[binding]]
        name = "gh-api"
        injector = "bearer"
        provider = "env"
        secret = "T"
        hosts = ["other.example"]
    """))
    with pytest.raises(Exception):                   # the model no longer resolves
        resolve_workspace(ws)
    code, out, err = _run(["workspace", "w", "preset", "remove", "gh"])
    assert code == 0, out + err                       # remove is the fix -> succeeds
    assert "removed preset 'gh'" in out
    after = ws.config_path.read_text()
    assert "[[preset]]" not in after
    assert 'name = "gh-api"' in after                 # the literal survives
    assert "gh" not in load_lock(ws).get("presets", {})   # snapshot dropped
    # The collision is gone with the preset, so the model resolves clean again.
    assert {b.name for b in resolve_workspace(ws).bindings} == {"gh-api"}


def test_remove_succeeds_for_dangling_ref(xdg, workspaces_dir):
    """(b) A pack deleted from the registry + the ref's inputs subsequently edited
    (`disable`) makes a resolve re-expand -> `unknown preset`. `preset remove`
    (the only non-hand way to drop the dangling ref) still succeeds."""
    import pytest
    from credproxy_cli.core.model.lock import load_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.paths import config_dir
    ws = _add_and_lock(workspaces_dir)
    # Delete the pack AND edit the ref inputs so a resolve must re-expand.
    (config_dir() / "presets" / "gh.toml").unlink()
    ws.config_path.write_text(_GH_REF + 'disable = ["git"]\n')
    with pytest.raises(Exception):
        resolve_workspace(ws)
    code, out, err = _run(["workspace", "w", "preset", "remove", "gh"])
    assert code == 0, out + err
    assert "[[preset]]" not in ws.config_path.read_text()
    assert "gh" not in load_lock(ws).get("presets", {})


def test_remove_succeeds_when_remaining_model_independently_broken(xdg, workspaces_dir):
    """(c) A successful remove that leaves an UNRELATED breakage still reports the
    removal and exits per the removal (not the post-edit resolve). A `--json`
    consumer must see `removed`, never an `error`, for a mutation that happened."""
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("gh", _GH)
    ws = _ws(workspaces_dir, "w", _GH_REF)
    save_lock(ws, resolve_workspace(ws).lock)
    # An independently-broken literal binding (unknown injector) that survives the
    # remove and keeps the model unresolvable AFTER it.
    ws.config_path.write_text(ws.config_path.read_text() + textwrap.dedent("""
        [[binding]]
        name = "broken"
        injector = "no-such-injector"
        provider = "env"
        secret = "T"
        hosts = ["broken.example"]
    """))
    code, out, err = _run(["--json", "workspace", "w", "preset", "remove", "gh"])
    assert code == 0, out + err
    obj = json.loads(out)
    assert obj["removed"] == "gh"                      # reported, not an error
    after = ws.config_path.read_text()
    assert "[[preset]]" not in after
    assert 'name = "broken"' in after                 # the unrelated breakage stays


def test_remove_folds_spaced_dot_child_subtable(xdg, workspaces_dir):
    """#64 fix 2: `[preset . options]` (a whitespace-spelled child sub-table, valid
    TOML naming the SAME table) must fold into the block span and delete cleanly.
    A spelling-divergent child regex orphaned it -> file corruption."""
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("svc", """
        [placeholder]
        prefix = "svc_"
        length = 12
        charset = "alnumeric"
        [[option]]
        id = "sock"
        type = "string"
        default = "/a"
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.svc.com"]
        env = "SVC"
        [[mount]]
        bind = { option = "sock" }
        target = "/s"
    """)
    ws = _ws(workspaces_dir, "w", textwrap.dedent('''\
        image = "x"

        [[preset]]
        name     = "svc"
        provider = "env"
        secret   = "SVC"
        [preset . options]
        sock = "/run/x"

        [[binding]]
        name = "keep"
        injector = "bearer"
        provider = "env"
        secret = "T"
        hosts = ["keep.com"]
        '''))
    save_lock(ws, resolve_workspace(ws).lock)
    keep_block = '[[binding]]\nname = "keep"'
    assert keep_block in ws.config_path.read_text()
    code, out, err = _run(["workspace", "w", "preset", "remove", "svc"])
    assert code == 0, out + err
    after = ws.config_path.read_text()
    # The spaced-dot child folded into the span and was deleted -- no orphan.
    assert "[preset . options]" not in after
    assert "[[preset]]" not in after
    assert "sock" not in after
    # The sibling literal block is byte-identical (untouched).
    assert keep_block in after
    # File still parses + resolves clean (not bricked).
    assert {b.name for b in resolve_workspace(ws).bindings} == {"keep"}


# ---- refresh reporting fidelity (#64 fixes 3-6) ------------------------------


def test_refresh_comment_only_edit_reports_written(xdg, workspaces_dir):
    """Fix 3: a definition edit that changes only the rev (a comment) rewrites the
    lock but leaves the expansion identical -> `changed:false` yet `written:true`
    (a CI consumer keying on `changed` must still see the file mutated), and the
    human path says `definition rev updated (expansion unchanged)`."""
    ws = _add_and_lock(workspaces_dir)
    lock_before = ws.lock_json_path.read_text()
    _preset("gh", _GH + "\n# a harmless comment\n")
    code, out, err = _run(["--json", "workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    obj = json.loads(out)
    assert obj["written"] is True
    p = obj["presets"][0]
    assert p["changed"] is False
    assert p["definition_rev"]["old"] != p["definition_rev"]["new"]
    assert ws.lock_json_path.read_text() != lock_before   # lock actually mutated

    # Human path: re-dirty with another comment and check the one-line note.
    _preset("gh", _GH + "\n# another comment\n")
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert "up to date" in out
    assert "definition rev updated (expansion unchanged)" in err


def test_refresh_named_notes_sideeffect_reexpand_of_other_pack(xdg, workspaces_dir):
    """Fix 4: `preset refresh gh` persists any OTHER pack whose ref inputs were
    edited (a whole-lock resolve) -- surface a note for each non-targeted pack
    whose lock snapshot changed as a side effect."""
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("gh", _GH)
    _preset("guard",
            '[[rule]]\nsuffix = "b"\nhosts = ["x.example"]\naction = "block"\n')
    ws = _ws(workspaces_dir, "w", _GH_REF + '[[preset]]\nname = "guard"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    # Edit GUARD's ref inputs (disable), not gh's, then refresh ONLY gh.
    ws.config_path.write_text(
        _GH_REF + '[[preset]]\nname = "guard"\ndisable = ["b"]\n')
    code, out, err = _run(["workspace", "w", "preset", "refresh", "gh"])
    assert code == 0, out + err
    assert "preset 'guard' inputs changed" in err
    assert "run 'preset refresh guard'" in err


def test_refresh_check_suppresses_restart_hint_and_docker_probe(
        xdg, workspaces_dir, monkeypatch):
    """Fix 5: `--check` writes nothing, so a "restart to apply" hint would be
    false -- suppress the hint AND the docker status probe on a pure preview."""
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.lock import save_lock
    _preset("cont", '[env]\nC_VAR = "one"\n')
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "cont"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    _preset("cont", '[env]\nC_VAR = "CHANGED"\n')
    from credproxy_cli.porcelain import cli as pcli
    called: list = []
    monkeypatch.setattr(pcli.core_docker, "container_status",
                        lambda n: called.append(n) or "running")
    code, out, err = _run(
        ["workspace", "w", "preset", "refresh", "cont", "--check"])
    assert code == 0, out + err
    assert "restart to apply" not in err       # suppressed on a preview
    assert called == []                        # docker never probed


def test_refresh_mount_entry_carries_target(xdg, workspaces_dir):
    """Fix 6: a `kind:"mount"` EntryDiff also emits `target` (not only `name`)."""
    from credproxy_cli.core.model.resolver import resolve_workspace
    from credproxy_cli.core.model.lock import save_lock
    _preset("m", '[[mount]]\nvolume = "data"\ntarget = "/data"\n')
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "m"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    _preset("m", '[[mount]]\nvolume = "data"\ntarget = "/data"\nreadonly = true\n')
    code, out, err = _run(
        ["--json", "workspace", "w", "preset", "refresh", "m", "--check"])
    assert code == 0, out + err
    ents = json.loads(out)["presets"][0]["entries"]
    mnt = [e for e in ents if e["kind"] == "mount"][0]
    assert mnt["action"] == "changed"
    assert mnt["target"] == "/data" and mnt["name"] == "/data"
