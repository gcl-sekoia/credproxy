"""config-v2 (#63): presets are durable `[[preset]]` REFERENCES; the resolver
expands them and snapshots the expansion in the lockfile.

Covers the resolver's expand-vs-reuse decision (inputs compared structurally),
definition-change inertness + the surfaced note, the round-trip anchor extended
to presets, `disable` / `[preset.override.*]`, unknown-suffix errors, the lock
schema, and literal-then-preset merge ordering + collisions.
"""
from __future__ import annotations

import textwrap

import pytest


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
    default_provider = "gh-cli"
    default_secret = "github.com"
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
    [[rule]]
    suffix = "noDelete"
    hosts = ["api.github.com"]
    action = "block"
    methods = ["DELETE"]
"""


# ---- expansion + lock schema -------------------------------------------------


def test_reference_expands_and_snapshots_lock(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w",
             'image = "x"\n[[preset]]\nname = "github"\n')
    r = resolve_workspace(ws)
    assert r.lock_dirty is True
    # Literal-then-preset: no literal here, so just the preset expansion.
    assert [b.name for b in r.bindings] == ["github-api", "github-git"]
    assert [rl.name for rl in r.rules] == ["github-noDelete"]
    # All bindings share ONE placeholder.
    phs = {b.placeholder for b in r.bindings}
    assert len(phs) == 1 and next(iter(phs)).startswith("ghp_")
    # Lock snapshot: definition_rev, inputs, placeholder, expansion.
    entry = r.lock["presets"]["github"]
    assert set(entry) == {"definition_rev", "inputs", "placeholder", "expansion"}
    assert entry["inputs"] == {}          # hand-authored ref, all defaults omitted
    assert entry["placeholder"] == next(iter(phs))
    assert [b["name"] for b in entry["expansion"]["bindings"]] \
        == ["github-api", "github-git"]


def test_literal_then_preset_merge_order(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[binding]]
        name = "lit"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["lit.example.com"]
        [[preset]]
        name = "github"
    """)
    r = resolve_workspace(ws)
    assert [b.name for b in r.bindings] == ["lit", "github-api", "github-git"]


def test_roundtrip_no_op(xdg, workspaces_dir):
    """resolve -> persist -> resolve is a no-op (the #62 anchor, extended to
    presets)."""
    from dataclasses import replace
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "github"\n')
    r1 = resolve_workspace(ws)
    assert r1.lock_dirty is True
    save_lock(ws, r1.lock)
    r2 = resolve_workspace(ws)
    assert r2.lock_dirty is False
    assert r2 == replace(r1, lock_dirty=False)


# ---- definition-change inertness + note --------------------------------------


def test_definition_change_is_inert_with_note(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "github"\n')
    r1 = resolve_workspace(ws)
    save_lock(ws, r1.lock)

    # Mutate the definition (add a third host binding).
    _preset("github", _GH + """
        [[part]]
        suffix = "ghcr"
        injector = "basic"
        hosts = ["ghcr.io"]
    """)
    r2 = resolve_workspace(ws)
    # Inert: same effective model as the snapshot (no ghcr binding).
    assert [b.name for b in r2.bindings] == ["github-api", "github-git"]
    assert r2.lock_dirty is False
    assert any("definition changed since lock" in n for n in r2.notes)


def test_definition_change_note_surfaces_on_stderr(xdg, workspaces_dir):
    """The resolve-time 'definition changed' note reaches the operator on stderr
    via a mutating verb -- here `preset add` (which resolves for the real action).
    The unit path already asserts `resolved.notes`; this asserts the surfacing."""
    from test_porcelain import _run
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    _preset("policy",
            '[[rule]]\nsuffix = "b"\nhosts = ["x.example"]\naction = "block"\n')
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "github"\n')
    # Persist github's lock so its INPUTS match on the next resolve (reuse path).
    save_lock(ws, resolve_workspace(ws).lock)
    # Change github's definition WITHOUT touching the ref inputs (inert change).
    _preset("github", _GH + """
        [[part]]
        suffix = "ghcr"
        injector = "basic"
        hosts = ["ghcr.io"]
    """)
    # Adding an unrelated pure-rule pack resolves + surfaces the github note.
    code, out, err = _run(["workspace", "w", "preset", "add", "policy"])
    assert code == 0, out + err
    assert "note:" in err and "github" in err and "definition changed" in err


def test_disable_enable_cycle_keeps_placeholder(xdg, workspaces_dir):
    """The shared placeholder is the pack's STABLE identity: disabling every
    placeholder-bearing part must NOT null the recorded placeholder, and removing
    the disable later must reuse it (not mint a fresh one)."""
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    # 1. No disable: mint + persist the shared placeholder.
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "github"\n')
    r1 = resolve_workspace(ws)
    save_lock(ws, r1.lock)
    ph0 = r1.lock["presets"]["github"]["placeholder"]
    assert ph0 and next(b.placeholder for b in r1.bindings
                        if b.name == "github-api") == ph0

    # 2. Disable EVERY placeholder-bearing part -> the expansion carries no
    #    placeholder, but the RECORDED placeholder must be PRESERVED (not nulled).
    ws.config_path.write_text(
        'image = "x"\n[[preset]]\nname = "github"\ndisable = ["api", "git"]\n')
    r2 = resolve_workspace(ws)
    assert r2.lock_dirty is True
    save_lock(ws, r2.lock)
    assert [b.name for b in r2.bindings] == []               # both dropped
    assert r2.lock["presets"]["github"]["placeholder"] == ph0

    # 3. Remove the disable -> re-expand reuses the SAME placeholder (no rotation).
    ws.config_path.write_text('image = "x"\n[[preset]]\nname = "github"\n')
    r3 = resolve_workspace(ws)
    api = next(b for b in r3.bindings if b.name == "github-api")
    assert api.placeholder == ph0
    assert r3.lock["presets"]["github"]["placeholder"] == ph0


def test_override_may_not_replace_placeholder(xdg, workspaces_dir):
    """`placeholder` is generated identity like `name`/`suffix` -- an override
    that sets it is refused (it would displace the recorded shared placeholder)."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[preset]]
        name = "github"
        [preset.override.api]
        placeholder = "ghp_evil"
    """)
    with pytest.raises(ConfigError, match="may not replace identity field"):
        resolve_workspace(ws)


def test_deleted_pack_reuses_lock_snapshot(xdg, workspaces_dir):
    """A pack removed from the registry after its snapshot was locked does NOT
    brick the workspace: the reuse path (unchanged inputs) serves the verbatim
    snapshot + a note, since the definition is only needed for the rev advisory."""
    import os
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    # A name with NO builtin, so removing the file truly deletes the pack.
    path = _preset("acme", _GH)
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "acme"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    os.remove(path)                             # pack vanishes from the registry
    r = resolve_workspace(ws)
    assert [b.name for b in r.bindings] == ["acme-api", "acme-git"]
    assert [rl.name for rl in r.rules] == ["acme-noDelete"]
    assert r.lock_dirty is False
    assert any("no longer resolvable" in n for n in r.notes)


def test_deleted_pack_with_edited_inputs_still_errors(xdg, workspaces_dir):
    """The tolerance is limited to the REUSE path: editing the ref's inputs forces
    a re-expand, which legitimately requires the pack to exist."""
    import os
    from credproxy_cli.core.errors import CredproxyError
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    path = _preset("acme", _GH)
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "acme"\n')
    save_lock(ws, resolve_workspace(ws).lock)
    os.remove(path)
    ws.config_path.write_text(
        'image = "x"\n[[preset]]\nname = "acme"\ndisable = ["git"]\n')
    with pytest.raises(CredproxyError, match="unknown preset"):
        resolve_workspace(ws)


def test_override_unknown_binding_field_errors(xdg, workspaces_dir):
    """A `[preset.override.<suffix>]` on a BINDING that names an unknown field
    (a typo like `host` for `hosts`) errors instead of silently no-opping."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[preset]]
        name = "github"
        [preset.override.api]
        host = ["api.github.example.com"]
    """)
    with pytest.raises(ConfigError, match="unknown binding field"):
        resolve_workspace(ws)


def test_override_unknown_binding_field_errors_even_when_disabled(xdg, workspaces_dir):
    """An override field typo is validated regardless of `disable` -- the check
    runs before the disable filter, so a disabled binding's bad override still
    errors rather than silently no-opping."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[preset]]
        name = "github"
        disable = ["api"]
        [preset.override.api]
        host = ["api.github.example.com"]
    """)
    with pytest.raises(ConfigError, match="unknown binding field"):
        resolve_workspace(ws)


def test_editing_inputs_reexpands_from_current_definition(xdg, workspaces_dir):
    from credproxy_cli.core.model.lock import save_lock
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("svc", """
        [[option]]
        id = "sock"
        type = "string"
        default = "/a"
        [[mount]]
        bind = { option = "sock" }
        target = "/s"
        [[setup]]
        run = "echo hi"
        order = 1
    """)
    ws = _ws(workspaces_dir, "w", 'image = "x"\n[[preset]]\nname = "svc"\n')
    r1 = resolve_workspace(ws)
    save_lock(ws, r1.lock)
    src1 = next(m for m in r1.config["mounts"] if m["target"] == "/s")["source"]
    assert src1 == "/a"

    # Change the definition AND the ref's own inputs (an option) -> re-expand
    # picks up the new definition.
    _preset("svc", """
        [[option]]
        id = "sock"
        type = "string"
        default = "/a"
        [[mount]]
        bind = { option = "sock" }
        target = "/s"
        [[setup]]
        run = "echo CHANGED"
        order = 1
    """)
    ws.config_path.write_text(
        'image = "x"\n[[preset]]\nname = "svc"\n[preset.options]\nsock = "/b"\n')
    r2 = resolve_workspace(ws)
    assert r2.lock_dirty is True
    src2 = next(m for m in r2.config["mounts"] if m["target"] == "/s")["source"]
    assert src2 == "/b"                                  # new input applied
    assert r2.config["setup"][-1]["run"] == "echo CHANGED"  # new definition applied


# ---- disable / override ------------------------------------------------------


def test_disable_drops_part_and_rule(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w",
             'image = "x"\n[[preset]]\nname = "github"\n'
             'disable = ["git", "noDelete"]\n')
    r = resolve_workspace(ws)
    assert [b.name for b in r.bindings] == ["github-api"]
    assert r.rules == []


def test_override_replaces_whole_field(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[preset]]
        name = "github"
        [preset.override.api]
        hosts = ["api.github.example.com"]
    """)
    r = resolve_workspace(ws)
    api = next(b for b in r.bindings if b.name == "github-api")
    assert api.hosts == ("api.github.example.com",)     # whole field replaced


def test_unknown_disable_suffix_errors_naming_valid(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w",
             'image = "x"\n[[preset]]\nname = "github"\ndisable = ["nope"]\n')
    with pytest.raises(ConfigError, match=r"unknown suffix.*valid suffixes"):
        resolve_workspace(ws)


def test_override_may_not_touch_identity_field(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[preset]]
        name = "github"
        [preset.override.api]
        name = "renamed"
    """)
    with pytest.raises(ConfigError, match="may not replace identity field"):
        resolve_workspace(ws)


# ---- collisions --------------------------------------------------------------


def test_preset_vs_literal_binding_collision_names_both(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[binding]]
        name = "github-api"
        injector = "bearer"
        provider = "env"
        secret = "T"
        hosts = ["api.github.com"]
        [[preset]]
        name = "github"
    """)
    with pytest.raises(ConfigError, match="collides with a literal"):
        resolve_workspace(ws)


# ---- loader accepts + validation ---------------------------------------------


def test_unknown_key_under_preset_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w",
             'image = "x"\n[[preset]]\nname = "github"\nbogus = 1\n')
    with pytest.raises(ConfigError, match="unknown key"):
        resolve_workspace(ws)


def test_duplicate_reference_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("github", _GH)
    ws = _ws(workspaces_dir, "w",
             'image = "x"\n[[preset]]\nname = "github"\n[[preset]]\nname = "github"\n')
    with pytest.raises(ConfigError, match="duplicate `\\[\\[preset\\]\\]` reference"):
        resolve_workspace(ws)


# ---- multi-slot credentials (#71) --------------------------------------------


# A sign-family, MULTI-SLOT pack: both parts use sigv4 (slots access_key_id /
# secret_access_key), so one credential's two refs thread into every part.
_AWS = """
    [placeholder]
    prefix = "aws_"
    length = 20
    charset = "hex"
    [[part]]
    suffix = "sts"
    injector = "sigv4"
    hosts = ["sts.amazonaws.com"]
    [[part]]
    suffix = "s3"
    injector = "sigv4"
    hosts = ["*.s3.amazonaws.com"]
"""


def test_multislot_reference_expands_with_table_secret(xdg, workspaces_dir):
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("aws", _AWS)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[preset]]
        name = "aws"
        provider = "env"
        secret = { access_key_id = "AWS_KEY", secret_access_key = "AWS_SECRET" }
    """)
    r = resolve_workspace(ws)
    assert [b.name for b in r.bindings] == ["aws-sts", "aws-s3"]
    # Every part's binding carries the SAME slot->ref table verbatim.
    for b in r.bindings:
        assert b.secret == {"access_key_id": "AWS_KEY",
                            "secret_access_key": "AWS_SECRET"}
    # The lock records the table secret as an input (so a change re-expands).
    assert r.lock["presets"]["aws"]["inputs"]["secret"] == {
        "access_key_id": "AWS_KEY", "secret_access_key": "AWS_SECRET"}


def test_multislot_secret_slot_mismatch_rejected(xdg, workspaces_dir):
    """A `secret` table whose slots don't equal the injectors' declared set fails
    the whole expansion (atomic), preset-framed."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.resolver import resolve_workspace
    _preset("aws", _AWS)
    ws = _ws(workspaces_dir, "w", """
        image = "x"
        [[preset]]
        name = "aws"
        provider = "env"
        secret = { access_key_id = "AWS_KEY" }
    """)
    with pytest.raises(ConfigError, match="the pack's injector.* declare"):
        resolve_workspace(ws)


def test_parts_with_divergent_slot_sets_rejected(xdg, workspaces_dir):
    """Parts couple one credential, so their injectors must agree on slots; a
    sigv4 part next to a bearer part is a pack-definition error surfaced at
    expansion."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import preset_slot_set, get_preset
    _preset("mixed", """
        [placeholder]
        prefix = "m_"
        length = 20
        charset = "hex"
        [[part]]
        suffix = "a"
        injector = "sigv4"
        hosts = ["a.example.com"]
        [[part]]
        suffix = "b"
        injector = "bearer"
        hosts = ["b.example.com"]
    """)
    with pytest.raises(ConfigError, match="must declare the same secret slots"):
        preset_slot_set(get_preset("mixed"))
