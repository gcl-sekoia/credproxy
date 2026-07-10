"""Declarative `[[requires]]` host-prerequisite checks (#58).

Covers the parse/validate matrix per kind, the four check kinds' pass/fail
against a controlled environment, `preset add`'s advisory exit-0-with-warnings
behavior + `--json` shape, and `doctor`'s authoritative marker-discovered
re-check with `--fetch` gating.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from test_porcelain import _run


# ---- helpers -----------------------------------------------------------------


def _write_preset(name: str, toml: str):
    from credproxy_cli.core.paths import config_dir
    d = config_dir() / "presets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(textwrap.dedent(toml))
    return d / f"{name}.toml"


def _make_ws(name: str, content: str = 'image = "python:3.12-slim"\n'):
    from credproxy_cli.core.paths import workspaces_config_dir
    from credproxy_cli.core.model.workspace import Workspace
    wd = workspaces_config_dir()
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{name}.toml").write_text(textwrap.dedent(content))
    return Workspace(name)


# A binding preset (`env` provider) plus one `path` requires. The provider
# check needs the pack to have bindings.
_BINDING_PRESET = """
    [placeholder]
    prefix = "ghp_"
    length = 40
    charset = "alnumeric"

    [[part]]
    suffix = "api"
    injector = "bearer"
    hosts = ["api.github.com"]
    env = "GITHUB_TOKEN"

    [[requires]]
    kind = "path"
    path = "{path}"
    hint = "create the socket dir"
"""


# ---- parse / validate matrix -------------------------------------------------


def test_parse_all_kinds(xdg):
    from credproxy_cli.core.model.presets import get_preset
    _write_preset("allkinds", """
        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["h.example"]

        [placeholder]
        prefix = "p_"
        length = 20
        charset = "hex"

        [[requires]]
        kind = "path"
        path = "~/.ssh/x"
        hint = "make it"

        [[requires]]
        kind = "command"
        command = "gh"

        [[requires]]
        kind = "env"
        var = "SOME_VAR"

        [[requires]]
        kind = "provider"
        fetch = true
        hint = "gh auth login"
    """)
    spec = get_preset("allkinds")
    kinds = [r.kind for r in spec.requires]
    assert kinds == ["path", "command", "env", "provider"]
    assert spec.requires[0].path == "~/.ssh/x" and spec.requires[0].hint == "make it"
    assert spec.requires[1].command == "gh" and spec.requires[1].hint is None
    assert spec.requires[2].var == "SOME_VAR"
    assert spec.requires[3].kind == "provider" and spec.requires[3].fetch is True


def test_parse_rejects_unknown_kind(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("badkind", """
        [[rule]]
        suffix = "r"
        action = "block"
        hosts = ["h.example"]

        [[requires]]
        kind = "url"
        hint = "nope"
    """)
    with pytest.raises(ConfigError, match="'kind' must be one of"):
        load_presets()


@pytest.mark.parametrize("kind,field", [("path", "path"), ("command", "command"),
                                        ("env", "var")])
def test_parse_missing_payload_field(xdg, kind, field):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("nofield", f"""
        [[rule]]
        suffix = "r"
        action = "block"
        hosts = ["h.example"]

        [[requires]]
        kind = "{kind}"
    """)
    with pytest.raises(ConfigError, match=f"needs a non-empty '{field}'"):
        load_presets()


def test_parse_fetch_on_nonprovider_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("badfetch", """
        [[rule]]
        suffix = "r"
        action = "block"
        hosts = ["h.example"]

        [[requires]]
        kind = "command"
        command = "gh"
        fetch = true
    """)
    with pytest.raises(ConfigError, match="'fetch' applies only to a 'provider'"):
        load_presets()


def test_parse_provider_check_on_pure_container_rejected(xdg):
    """A provider check needs [[part]] bindings -- there's nothing to fetch on a
    pure-rule / pure-container pack, so it's a definition error."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("purerule", """
        [[rule]]
        suffix = "r"
        action = "block"
        hosts = ["h.example"]

        [[requires]]
        kind = "provider"
    """)
    with pytest.raises(ConfigError, match="needs the pack to have \\[\\[part\\]\\]"):
        load_presets()


def test_parse_unknown_key_rejected(xdg):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("extrakey", """
        [[rule]]
        suffix = "r"
        action = "block"
        hosts = ["h.example"]

        [[requires]]
        kind = "env"
        var = "X"
        bogus = "y"
    """)
    with pytest.raises(ConfigError, match="unknown key"):
        load_presets()


def test_parse_requires_only_pack_rejected(xdg):
    """A pack that is ONLY [[requires]] has nothing to stamp -- rejected as empty."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("reqonly", """
        [[requires]]
        kind = "command"
        command = "gh"
    """)
    with pytest.raises(ConfigError, match="at least one"):
        load_presets()


# ---- the four check kinds ----------------------------------------------------


def _req(kind, **kw):
    from credproxy_cli.core.model.presets import _Require
    return _Require(kind=kind, **kw)


def test_check_path(xdg, tmp_path):
    from credproxy_cli.core.model import prereqs
    present = tmp_path / "exists"
    present.mkdir()
    [ok] = prereqs.evaluate([_req("path", path=str(present))],
                            provider=None, secret=None, do_fetch=False)
    assert ok.ok and "exists" in ok.detail
    [bad] = prereqs.evaluate([_req("path", path=str(tmp_path / "nope"), hint="mk")],
                             provider=None, secret=None, do_fetch=False)
    assert not bad.ok and bad.hint == "mk"


def test_check_command(xdg, tmp_path, monkeypatch):
    from credproxy_cli.core.model import prereqs
    # A fake binary on a manipulated PATH is found; a bogus name is not.
    fake = tmp_path / "myfakecmd"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    [ok] = prereqs.evaluate([_req("command", command="myfakecmd")],
                            provider=None, secret=None, do_fetch=False)
    assert ok.ok
    [bad] = prereqs.evaluate([_req("command", command="definitely-absent-zzz")],
                             provider=None, secret=None, do_fetch=False)
    assert not bad.ok


def test_check_env(xdg, monkeypatch):
    from credproxy_cli.core.model import prereqs
    monkeypatch.setenv("PREREQ_SET", "v")
    monkeypatch.delenv("PREREQ_UNSET", raising=False)
    [ok] = prereqs.evaluate([_req("env", var="PREREQ_SET")],
                            provider=None, secret=None, do_fetch=False)
    [bad] = prereqs.evaluate([_req("env", var="PREREQ_UNSET")],
                             provider=None, secret=None, do_fetch=False)
    assert ok.ok and not bad.ok


def test_check_provider_resolve_and_fetch(xdg, monkeypatch):
    from credproxy_cli.core.model import prereqs
    monkeypatch.setenv("PREREQ_TOK", "secretvalue")
    # resolve-only (fetch=False): provider resolves.
    [r] = prereqs.evaluate([_req("provider", fetch=False)],
                           provider="env", secret="PREREQ_TOK", do_fetch=True)
    assert r.ok and "resolves" in r.detail
    # fetch=True + do_fetch=True: secret is fetched (length reported, not value).
    [f] = prereqs.evaluate([_req("provider", fetch=True)],
                           provider="env", secret="PREREQ_TOK", do_fetch=True)
    assert f.ok and "secretvalue" not in f.detail and "chars" in f.detail
    # fetch=True but secret unset -> fetch fails.
    monkeypatch.delenv("PREREQ_MISSING", raising=False)
    [bad] = prereqs.evaluate([_req("provider", fetch=True, hint="gh auth login")],
                             provider="env", secret="PREREQ_MISSING", do_fetch=True)
    assert not bad.ok and bad.hint == "gh auth login"


def test_check_provider_fetch_gated_by_do_fetch(xdg, monkeypatch):
    """A fetch=true check with do_fetch=False degrades to resolve-only -- no
    provider fetch, so an unfetchable secret still passes."""
    from credproxy_cli.core.model import prereqs
    monkeypatch.delenv("PREREQ_MISSING", raising=False)
    [r] = prereqs.evaluate([_req("provider", fetch=True)],
                           provider="env", secret="PREREQ_MISSING", do_fetch=False)
    assert r.ok and "fetch skipped" in r.detail


def test_check_provider_unknown_fails(xdg):
    from credproxy_cli.core.model import prereqs
    [r] = prereqs.evaluate([_req("provider", fetch=False)],
                           provider="nonexistent-zzz", secret="X", do_fetch=True)
    assert not r.ok


# ---- preset add: advisory (exit 0), --json shape -----------------------------


def test_preset_add_reports_failed_requires_but_exits_0(xdg, tmp_path):
    _write_preset("gitsign", _BINDING_PRESET.format(path=str(tmp_path / "absent")))
    ws = _make_ws("w")
    code, out, err = _run(["workspace", "w", "preset", "add", "gitsign",
                           "--provider", "env", "--secret", "TOK"])
    blob = out + err
    assert code == 0, blob                       # advisory: still exit 0
    assert "unmet prerequisite" in blob and "create the socket dir" in blob
    # The stamp DID land (durable config).
    from credproxy_cli.core.model.bindings import load_bindings
    assert {b.name for b in load_bindings(ws)} == {"gitsign-api"}


def test_preset_add_json_requires_array(xdg, tmp_path):
    _write_preset("gitsign", _BINDING_PRESET.format(path=str(tmp_path / "absent")))
    _make_ws("w")
    code, out, err = _run(["--json", "workspace", "w", "preset", "add", "gitsign",
                           "--provider", "env", "--secret", "TOK"])
    assert code == 0, out + err
    data = json.loads(out)
    reqs = data["requires"]
    assert len(reqs) == 1
    assert reqs[0]["kind"] == "path" and reqs[0]["ok"] is False
    assert reqs[0]["hint"] == "create the socket dir"


def test_preset_add_provider_fetch_failure_reported(xdg, monkeypatch):
    """acceptance #2: an unauthenticated provider fetch is reported with its
    hint at add time (still exit 0)."""
    monkeypatch.delenv("UNSET_TOK", raising=False)
    _write_preset("gh", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"

        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]

        [[requires]]
        kind = "provider"
        fetch = true
        hint = "authenticate: gh auth login"
    """)
    _make_ws("w")
    code, out, err = _run(["workspace", "w", "preset", "add", "gh",
                           "--provider", "env", "--secret", "UNSET_TOK"])
    blob = out + err
    assert code == 0, blob
    assert "gh auth login" in blob and "unmet prerequisite (provider)" in blob


# ---- doctor: marker-discovered, --fetch gated --------------------------------


def _add_gitsign(ws_name: str, tmp_path):
    """Stamp the `gitsign` binding preset (failing path requires) into a fresh
    workspace, returning the workspace."""
    ws = _make_ws(ws_name)
    code, out, err = _run(["workspace", ws_name, "preset", "add", "gitsign",
                           "--provider", "env", "--secret", "TOK"])
    assert code == 0, out + err
    return ws


def test_doctor_reports_stamped_requires(xdg, tmp_path):
    """acceptance #1: doctor shows the failing path check (with its hint) until
    the dir exists; discovered via the provenance marker."""
    _write_preset("gitsign", _BINDING_PRESET.format(path=str(tmp_path / "absent")))
    _add_gitsign("w", tmp_path)
    from credproxy_cli.core.engine import doctor
    checks = {c.id: c for c in doctor.run("w")}
    cid = "ws:w:preset:gitsign:requires[0]"
    assert cid in checks
    assert not checks[cid].ok
    assert checks[cid].hint == "create the socket dir"

    # Once the dir exists, the same check passes.
    (tmp_path / "absent").mkdir()
    ok = {c.id: c for c in doctor.run("w")}[cid]
    assert ok.ok


def test_doctor_check_id_and_message(xdg, tmp_path):
    _write_preset("gitsign", _BINDING_PRESET.format(path=str(tmp_path / "absent")))
    _add_gitsign("w", tmp_path)
    from credproxy_cli.core.engine import doctor
    c = {c.id: c for c in doctor.run("w")}["ws:w:preset:gitsign:requires[0]"]
    assert "[w] preset 'gitsign' requires (path)" in c.message


def test_doctor_unresolvable_pack_skip_note(xdg, tmp_path):
    """A marker naming a pack that no longer resolves -> ok=True skip-note."""
    path_file = _write_preset("gitsign",
                              _BINDING_PRESET.format(path=str(tmp_path / "x")))
    ws = _add_gitsign("w", tmp_path)
    path_file.unlink()  # pack gone from the registry, but its marker stays
    from credproxy_cli.core.engine import doctor
    checks = {c.id: c for c in doctor.run("w")}
    note = checks["ws:w:preset:gitsign"]
    assert note.ok and "no longer resolves" in note.message
    # No per-requires checks for a vanished pack.
    assert not any(cid.startswith("ws:w:preset:gitsign:requires")
                   for cid in checks)


def test_doctor_fetch_gating(xdg, monkeypatch, tmp_path):
    """acceptance #3: a fetch=true provider check runs only under --fetch. Without
    it (incl. a scan-all), the provider is never fetched (degrades to resolve)."""
    monkeypatch.delenv("UNSET_TOK", raising=False)
    _write_preset("gh", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"

        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]

        [[requires]]
        kind = "provider"
        fetch = true
        hint = "gh auth login"
    """)
    ws = _make_ws("w")
    code, out, err = _run(["workspace", "w", "preset", "add", "gh",
                           "--provider", "env", "--secret", "UNSET_TOK"])
    assert code == 0, out + err

    from credproxy_cli.core.engine import doctor
    cid = "ws:w:preset:gh:requires[0]"
    # No --fetch: resolve-only, so the unfetchable secret still passes.
    plain = {c.id: c for c in doctor.run("w")}[cid]
    assert plain.ok and "fetch skipped" in plain.message
    # scan-all (no NAME) also never fetches.
    allscan = {c.id: c for c in doctor.run(None)}[cid]
    assert allscan.ok
    # --fetch: the fetch is attempted and fails (secret unset).
    fetched = {c.id: c for c in doctor.run("w", fetch=True)}[cid]
    assert not fetched.ok and fetched.hint == "gh auth login"


def test_doctor_no_requires_no_checks(xdg):
    """A workspace with no stamped presets emits no preset-requires checks."""
    _make_ws("plain")
    from credproxy_cli.core.engine import doctor
    ids = {c.id for c in doctor.run("plain")}
    assert not any(":preset:" in cid for cid in ids)


# ---- review follow-ups (#58) -------------------------------------------------


def test_requires_command_is_looked_up_not_run(xdg, tmp_path):
    """Negative RCE proof (finding 8): a `command` requires is looked up on PATH,
    NEVER executed. A shell-injection-shaped command yields command-not-found and
    creates no side effect."""
    from credproxy_cli.core.model import prereqs
    sentinel = tmp_path / "credproxy_pwned"
    payload = f"sh -c 'touch {sentinel}'"
    [r] = prereqs.evaluate([_req("command", command=payload)],
                           provider=None, secret=None, do_fetch=False)
    assert not r.ok and "not found" in r.detail
    assert not sentinel.exists()


def test_parse_rejects_relative_path(xdg):
    """Finding 7: a bare relative `path` requires resolves against cwd
    (nondeterministic across doctor runs) -- rejected at definition time. An
    absolute or `~`/`$VAR`-rooted path is accepted."""
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.presets import load_presets
    _write_preset("relpath", """
        [[rule]]
        suffix = "r"
        action = "block"
        hosts = ["h.example"]

        [[requires]]
        kind = "path"
        path = "foo/bar"
    """)
    with pytest.raises(ConfigError, match="must be absolute or"):
        load_presets()


def test_parse_accepts_rooted_paths(xdg):
    """`~`/`$VAR`-rooted and absolute paths are accepted (finding 7) -- even a
    `$VAR`-rooted one whose var is currently unset (it resolves at check time)."""
    from credproxy_cli.core.model.presets import get_preset
    _write_preset("rooted", """
        [[rule]]
        suffix = "r"
        action = "block"
        hosts = ["h.example"]

        [[requires]]
        kind = "path"
        path = "$DEFINITELY_UNSET_VAR_ZZZ/sock"

        [[requires]]
        kind = "path"
        path = "~/.ssh/x"

        [[requires]]
        kind = "path"
        path = "/opt/abs"
    """)
    spec = get_preset("rooted")
    assert [r.path for r in spec.requires] == \
        ["$DEFINITELY_UNSET_VAR_ZZZ/sock", "~/.ssh/x", "/opt/abs"]


def test_doctor_malformed_registry_preset_reports_not_aborts(xdg, tmp_path):
    """Finding 1: one unparseable registry preset must NOT abort the whole doctor
    sweep -- it reports a failing load check while every other check still runs."""
    _write_preset("gitsign", _BINDING_PRESET.format(path=str(tmp_path / "absent")))
    _add_gitsign("w", tmp_path)
    # A WIP/broken preset unrelated to the workspace.
    _write_preset("broken", "this is = not = valid = toml [[[\n")
    from credproxy_cli.core.engine import doctor
    checks = {c.id: c for c in doctor.run("w")}
    assert "ws:w:presets:load" in checks and not checks["ws:w:presets:load"].ok
    # The rest of the sweep still ran (report-all, not raise).
    assert checks["ws:w:config"].ok
    assert "ws:w:bindings" in checks


def test_binding_remove_cleans_preset_marker(xdg, tmp_path):
    """Finding 2b: removing a preset-stamped binding also removes its provenance
    marker, so no orphan marker lingers and doctor emits no stale preset checks."""
    _write_preset("gitsign", _BINDING_PRESET.format(path=str(tmp_path / "x")))
    ws = _add_gitsign("w", tmp_path)
    from credproxy_cli.core.model.preset_stamp import applied_preset_names
    assert applied_preset_names(ws.config_path.read_text()) == ["gitsign"]

    code, out, err = _run(["workspace", "w", "binding", "remove", "gitsign-api"])
    assert code == 0, out + err
    text = ws.config_path.read_text()
    assert "credproxy:preset" not in text
    assert applied_preset_names(text) == []

    from credproxy_cli.core.engine import doctor
    ids = {c.id for c in doctor.run("w")}
    assert not any(":preset:" in cid for cid in ids)


def test_doctor_orphan_marker_reports_marker_remedy_not_pack_hint(xdg, tmp_path):
    """Finding 2a: when a pack's stamped binding was renamed/removed but its marker
    lingers, the provider requires check's remedy points at the orphaned marker --
    NOT the pack's own prerequisite hint."""
    _write_preset("gh", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"

        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]

        [[requires]]
        kind = "provider"
        hint = "gh auth login"
    """)
    ws = _make_ws("w")
    assert _run(["workspace", "w", "preset", "add", "gh",
                 "--provider", "env", "--secret", "TOK"])[0] == 0
    # Orphan the marker: rename the stamped binding so `gh-api` no longer exists.
    text = ws.config_path.read_text()
    renamed = text.replace('"gh-api"', '"gh-renamed"')
    assert renamed != text                         # the rename actually happened
    ws.config_path.write_text(renamed)

    from credproxy_cli.core.engine import doctor
    c = {c.id: c for c in doctor.run("w")}["ws:w:preset:gh:requires[0]"]
    assert not c.ok
    assert "stamped binding" in (c.hint or "") and "missing" in (c.hint or "")
    assert "gh auth login" not in (c.hint or "")


def test_doctor_provider_check_uses_stamped_provider_not_default(xdg):
    """Finding 8: doctor's provider requires check must use the provider CHOSEN at
    stamp time, not the pack's `default_provider` (which here doesn't resolve)."""
    _write_preset("gh", """
        default_provider = "bogus-default-zzz"

        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"

        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]

        [[requires]]
        kind = "provider"
    """)
    ws = _make_ws("w")
    assert _run(["workspace", "w", "preset", "add", "gh",
                 "--provider", "env", "--secret", "TOK"])[0] == 0
    from credproxy_cli.core.engine import doctor
    c = {c.id: c for c in doctor.run("w")}["ws:w:preset:gh:requires[0]"]
    assert c.ok                                    # 'env' resolves; default would not
    assert "'env'" in c.message and "bogus-default-zzz" not in c.message


def _counting_provider(name: str, counter_file):
    """Install a provider that appends to `counter_file` on each invocation and
    serves a dummy value for any ref -- so a test can assert how many times a
    provider was actually exec'd."""
    import os
    from credproxy_cli.core.paths import config_dir
    d = config_dir() / "providers"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(
        "#!/bin/sh\n"
        f'echo x >> "{counter_file}"\n'
        "exec python3 -c '"
        "import json,sys;"
        "req=json.load(sys.stdin);"
        'print(json.dumps({"values": {r: "v-"+r for r in req.get("secrets", [])}}))'
        "'\n")
    os.chmod(p, 0o755)
    return p


def test_doctor_fetch_dedupes_provider_invocation(xdg, tmp_path):
    """Finding 4: under `doctor NAME --fetch`, a pack's binding fetch and its
    `fetch=true` provider requires must NOT both invoke the provider -- the
    requires layer reuses the binding fetch's outcome (one exec, not two)."""
    counter = tmp_path / "count"
    _counting_provider("countprov", counter)
    _write_preset("gh", """
        [placeholder]
        prefix = "ghp_"
        length = 40
        charset = "alnumeric"

        [[part]]
        suffix = "api"
        injector = "bearer"
        hosts = ["api.github.com"]

        [[requires]]
        kind = "provider"
        fetch = true
    """)
    ws = _make_ws("w")
    assert _run(["workspace", "w", "preset", "add", "gh",
                 "--provider", "countprov", "--secret", "TOK"])[0] == 0
    counter.write_text("")                         # reset the add-time invocations

    from credproxy_cli.core.engine import doctor
    checks = {c.id: c for c in doctor.run("w", fetch=True)}
    req = checks["ws:w:preset:gh:requires[0]"]
    assert req.ok and "covered by this workspace's binding check" in req.message
    # Exactly ONE provider exec for the whole --fetch run (binding fetch), not two.
    assert counter.read_text().count("x") == 1


def test_applied_preset_names_ignores_marker_in_multiline_string(xdg):
    """Finding 3: a marker-SHAPED line inside a multiline string value is not a
    real stamp (no full 12-hex rev/sha) and must not be discovered as a pack. A
    genuine full-shape marker is still found."""
    from credproxy_cli.core.model.preset_stamp import applied_preset_names
    phantom = textwrap.dedent('''\
        image = "x"
        [[setup]]
        run = """
        # credproxy:preset name=github rev=deploy sha=main
        echo hi
        """
        order = 1
    ''')
    assert applied_preset_names(phantom) == []
    real = ("# credproxy:preset name=github rev=abcdef012345 sha=012345abcdef\n"
            "[[binding]]\n")
    assert applied_preset_names(real) == ["github"]
