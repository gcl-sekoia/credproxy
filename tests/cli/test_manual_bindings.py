"""Tests for on-demand ("manual") bindings: the `manual`/`required_for_setup`
model flags, the `select_active` selection seam, the host-tracked `applied.active`
set + its reset rule, `binding activate|deactivate|list`, and the `doctor --fetch`
skip. Phases 1-2 are CLI-only (no proxy/wire change), so everything here exercises
the model + engine + porcelain planes with a faked docker/proxy.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _write_ws(workspaces_dir: Path, name: str, content: str):
    from credproxy_cli.core.model.workspace import Workspace
    p = workspaces_dir / f"{name}.toml"
    p.write_text(textwrap.dedent(content))
    return Workspace(name)


def _binding(name, *, manual=False, required_for_setup=False, hosts=("h.com",)):
    from credproxy_cli.core.model.bindings import Binding
    return Binding(name=name, injector="bearer", provider="env", secret="TOK",
                   hosts=tuple(hosts), placeholder=f"PH-{name}", env=None,
                   manual=manual, required_for_setup=required_for_setup)


_TWO_BINDINGS = """
    image = "x"

    [[binding]]
    name     = "always"
    injector = "bearer"
    provider = "env"
    secret   = "ALWAYS_TOK"
    hosts    = ["api.always.com"]

    [[binding]]
    name     = "gh"
    injector = "bearer"
    provider = "env"
    secret   = "GH_TOK"
    hosts    = ["api.github.com"]
    manual   = true
"""


# ---- model: parse / validate ------------------------------------------------


def test_parse_manual_and_required(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import load_bindings
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[binding]]
        name = "a"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["h.com"]
        manual = true
        required_for_setup = true
    """)
    (b,) = load_bindings(ws)
    assert b.manual is True and b.required_for_setup is True


def test_parse_defaults_false(xdg, workspaces_dir):
    from credproxy_cli.core.model.bindings import load_bindings
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[binding]]
        name = "a"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["h.com"]
    """)
    (b,) = load_bindings(ws)
    assert b.manual is False and b.required_for_setup is False


def test_required_for_setup_without_manual_rejected(xdg, workspaces_dir):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.bindings import load_bindings
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"
        [[binding]]
        name = "a"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["h.com"]
        required_for_setup = true
    """)
    with pytest.raises(ConfigError, match="required_for_setup"):
        load_bindings(ws)


@pytest.mark.parametrize("val", ['"yes"', "1", "0"])
def test_manual_must_be_boolean(xdg, workspaces_dir, val):
    from credproxy_cli.core.errors import ConfigError
    from credproxy_cli.core.model.bindings import load_bindings
    ws = _write_ws(workspaces_dir, "w", f"""
        image = "x"
        [[binding]]
        name = "a"
        injector = "bearer"
        provider = "env"
        secret = "TOK"
        hosts = ["h.com"]
        manual = {val}
    """)
    with pytest.raises(ConfigError, match="manual"):
        load_bindings(ws)


# ---- model: select_active ---------------------------------------------------


def test_select_active_default_excludes_manual():
    from credproxy_cli.core.model.bindings import select_active
    bs = [_binding("always"), _binding("gh", manual=True)]
    assert [b.name for b in select_active(bs, set())] == ["always"]


def test_select_active_includes_activated_manual():
    from credproxy_cli.core.model.bindings import select_active
    bs = [_binding("always"), _binding("gh", manual=True)]
    assert [b.name for b in select_active(bs, {"gh"})] == ["always", "gh"]


def test_select_active_always_on_never_filtered():
    from credproxy_cli.core.model.bindings import select_active
    bs = [_binding("always")]
    # An always-on binding is present regardless of the active set.
    assert [b.name for b in select_active(bs, set())] == ["always"]
    assert [b.name for b in select_active(bs, {"nope"})] == ["always"]


# ---- fingerprint: the enter-doesn't-deactivate invariant --------------------


def test_fingerprint_differs_by_selected_set():
    from credproxy_cli.core.model.bindings import select_active
    from credproxy_cli.core.model.rules import combined_fingerprint
    bs = [_binding("always"), _binding("gh", manual=True)]
    off = combined_fingerprint(select_active(bs, set()), [])
    on = combined_fingerprint(select_active(bs, {"gh"}), [])
    assert off != on


def test_fingerprint_stable_when_active_unchanged():
    """The property behind 'enter never deactivates': feeding the SAME active set
    reproduces the fingerprint the activating push produced, so `_should_push`
    skips (no re-push, no re-auth)."""
    from credproxy_cli.core.engine.containers import _should_push
    from credproxy_cli.core.model.bindings import select_active
    from credproxy_cli.core.model.rules import combined_fingerprint
    bs = [_binding("always"), _binding("gh", manual=True)]
    want = combined_fingerprint(select_active(bs, {"gh"}), [])
    # A warm proxy already holding `want` -> no push.
    status = {"loaded": True, "fingerprint": want}
    assert _should_push(False, False, status, want) is False


# ---- engine: applied.active load + reset ------------------------------------


def _mk_running_ws(workspaces_dir, name, content=_TWO_BINDINGS):
    ws = _write_ws(workspaces_dir, name, content)
    ws.ensure_state_dir()
    ws.token_path.write_text("tok\n")
    return ws


def test_load_applied_active_empty_by_default(xdg, workspaces_dir):
    from credproxy_cli.core.engine.containers import _load_applied_active
    ws = _mk_running_ws(workspaces_dir, "w")
    assert _load_applied_active(ws) == set()


def test_load_applied_active_reads_lock(xdg, workspaces_dir):
    from credproxy_cli.core.engine.containers import (
        _load_applied_active, _update_applied)
    ws = _mk_running_ws(workspaces_dir, "w")
    _update_applied(ws, active=["gh", "gcloud"])
    assert _load_applied_active(ws) == {"gh", "gcloud"}


# ---- engine: set_binding_active ---------------------------------------------


@pytest.fixture
def fake_running_proxy(monkeypatch):
    """Fake a reachable, running managed proxy and capture each push's binding
    list. Returns the capture list of {names, gen}."""
    from credproxy_cli.core.engine import containers, docker, push

    monkeypatch.setattr(docker, "container_status", lambda c: "running")
    monkeypatch.setattr(containers, "resolve_admin_url",
                        lambda ws, notify=None: "http://127.0.0.1:39998")

    pushes: list[dict] = []
    gen = [0]

    def fake_push_to_target(admin_url, token, bindings, rules, fingerprint=None,
                            notify=None, postgres=()):
        gen[0] += 1
        pushes.append({"names": [b.name for b in bindings], "gen": gen[0]})
        return bindings, rules, gen[0]

    monkeypatch.setattr(push, "push_to_target", fake_push_to_target)
    return pushes


def test_activate_writes_applied_active_after_push(xdg, workspaces_dir,
                                                   fake_running_proxy):
    from credproxy_cli.core.engine import startup
    from credproxy_cli.core.engine.containers import _load_applied_active
    ws = _mk_running_ws(workspaces_dir, "w")

    result = startup.set_binding_active(ws, ["gh"], activate=True)

    assert result.changed == ("gh",)
    assert result.active == ("gh",)
    # The pushed binding list included the manual binding.
    assert "gh" in fake_running_proxy[-1]["names"]
    assert "always" in fake_running_proxy[-1]["names"]
    # Persisted only after the successful push.
    assert _load_applied_active(ws) == {"gh"}


def test_deactivate_removes_from_active(xdg, workspaces_dir, fake_running_proxy):
    from credproxy_cli.core.engine import startup
    from credproxy_cli.core.engine.containers import (
        _load_applied_active, _update_applied)
    ws = _mk_running_ws(workspaces_dir, "w")
    _update_applied(ws, active=["gh"])

    result = startup.set_binding_active(ws, ["gh"], activate=False)

    assert result.changed == ("gh",)
    assert result.active == ()
    # The narrowed push excluded the manual binding.
    assert fake_running_proxy[-1]["names"] == ["always"]
    assert _load_applied_active(ws) == set()


def test_apply_reality_drift_resets_active(xdg, workspaces_dir, monkeypatch):
    """`apply` after an out-of-band proxy restart (reality-drift) must reset the
    active set and NOT silently re-push/re-auth a manual binding -- parity with the
    push/proxy_fresh resets. Without the reset, `apply` would push `gh` again."""
    from credproxy_cli.core.engine import startup, containers, docker
    from credproxy_cli.core.engine.containers import (
        LiveDrift, _load_applied_active, _update_applied)
    from credproxy_cli.core.engine.imageenv import ImageEnv

    ws = _mk_running_ws(workspaces_dir, "w")
    _update_applied(ws, active=["gh"], config_generation=1)

    meta = ImageEnv(http_port=39998, tmpfs="/run/secrets",
                    token="/run/secrets-ro/auth.token", source="/opt/proxy",
                    mitmproxy_uid=31337)
    monkeypatch.setattr(ImageEnv, "load", staticmethod(lambda image=None: meta))
    monkeypatch.setattr(docker, "container_status", lambda c: "running")
    monkeypatch.setattr(docker, "resolve_host_port", lambda c, p: 39998)
    # Proxy is not holding our generation -> reality-drift.
    monkeypatch.setattr(
        containers, "_live_drift",
        lambda ws, url, has_content_drift=False: LiveDrift(
            verdict="reality-drift", generation=0, applied_generation=1,
            projection={"bindings": [], "rules": []}))

    pushed: list = []

    def fake_push_config(ws, port, notify=None, bindings=None, rules=None,
                         fingerprint=None, postgres=None):
        pushed.append([b.name for b in bindings])
        return bindings, rules, 2

    monkeypatch.setattr(startup, "push_config", fake_push_config)

    startup.apply_config(ws)

    assert _load_applied_active(ws) == set()      # reset
    assert pushed[-1] == ["always"]                # gh NOT re-pushed / re-authed


def test_activate_non_manual_binding_rejected(xdg, workspaces_dir,
                                              fake_running_proxy):
    from credproxy_cli.core.engine import startup
    from credproxy_cli.core.errors import ConfigError
    ws = _mk_running_ws(workspaces_dir, "w")
    with pytest.raises(ConfigError, match="not `manual`"):
        startup.set_binding_active(ws, ["always"], activate=True)


def test_activate_unknown_binding_rejected(xdg, workspaces_dir, fake_running_proxy):
    from credproxy_cli.core.engine import startup
    from credproxy_cli.core.errors import ConfigError
    ws = _mk_running_ws(workspaces_dir, "w")
    with pytest.raises(ConfigError, match="no such binding"):
        startup.set_binding_active(ws, ["ghost"], activate=True)


def test_activate_on_stopped_proxy_errors_toward_start(xdg, workspaces_dir,
                                                       monkeypatch):
    from credproxy_cli.core.engine import startup, docker
    from credproxy_cli.core.errors import WorkspaceError
    monkeypatch.setattr(docker, "container_status", lambda c: None)
    ws = _mk_running_ws(workspaces_dir, "w")
    with pytest.raises(WorkspaceError, match="[Ss]tart it first"):
        startup.set_binding_active(ws, ["gh"], activate=True)


# ---- engine: binding refresh (surgical patch) -------------------------------


@pytest.fixture
def fake_patch(monkeypatch):
    """Fake a reachable running proxy and capture each /admin/config/patch call's
    binding-name list. Returns the capture list."""
    from credproxy_cli.core.engine import containers, docker, push
    monkeypatch.setattr(docker, "container_status", lambda c: "running")
    monkeypatch.setattr(containers, "resolve_admin_url",
                        lambda ws, notify=None: "http://127.0.0.1:39998")
    patches: list[list[str]] = []
    gen = [10]

    def fake_patch_bindings(admin_url, token, wire_bindings, fingerprint=None,
                            notify=None):
        gen[0] += 1
        patches.append([b["name"] for b in wire_bindings])
        return gen[0]

    monkeypatch.setattr(push, "patch_bindings", fake_patch_bindings)
    return patches


def test_refresh_resolves_only_named_binding(xdg, workspaces_dir, monkeypatch,
                                             fake_patch):
    from credproxy_cli.core.engine import startup
    from credproxy_cli.core.engine.containers import _load_applied
    monkeypatch.setenv("ALWAYS_TOK", "v")
    ws = _mk_running_ws(workspaces_dir, "w")

    refreshed = startup.refresh_bindings(ws, ["always"])

    assert refreshed == ("always",)
    # ONLY the named binding is in the patch payload -- gh's provider isn't touched.
    assert fake_patch[-1] == ["always"]
    # The new generation is recorded for inspect/apply.
    assert _load_applied(ws).get("config_generation") == 11


def test_refresh_active_manual_binding(xdg, workspaces_dir, monkeypatch, fake_patch):
    from credproxy_cli.core.engine import startup
    from credproxy_cli.core.engine.containers import _update_applied
    monkeypatch.setenv("GH_TOK", "v")
    ws = _mk_running_ws(workspaces_dir, "w")
    _update_applied(ws, active=["gh"])

    startup.refresh_bindings(ws, ["gh"])
    assert fake_patch[-1] == ["gh"]


def test_refresh_inactive_manual_errors_toward_activate(xdg, workspaces_dir,
                                                        fake_patch):
    from credproxy_cli.core.engine import startup
    from credproxy_cli.core.errors import ConfigError
    ws = _mk_running_ws(workspaces_dir, "w")   # gh is manual + inactive
    with pytest.raises(ConfigError, match="activate it first"):
        startup.refresh_bindings(ws, ["gh"])


def test_refresh_unknown_binding_errors(xdg, workspaces_dir, fake_patch):
    from credproxy_cli.core.engine import startup
    from credproxy_cli.core.errors import ConfigError
    ws = _mk_running_ws(workspaces_dir, "w")
    with pytest.raises(ConfigError, match="no such binding"):
        startup.refresh_bindings(ws, ["ghost"])


def test_refresh_on_stopped_proxy_errors(xdg, workspaces_dir, monkeypatch):
    from credproxy_cli.core.engine import startup, docker
    from credproxy_cli.core.errors import WorkspaceError
    monkeypatch.setattr(docker, "container_status", lambda c: None)
    ws = _mk_running_ws(workspaces_dir, "w")
    with pytest.raises(WorkspaceError, match="not running"):
        startup.refresh_bindings(ws, ["always"])


# ---- engine: doctor --fetch skips inactive manual ---------------------------


def test_doctor_fetch_skips_inactive_manual(xdg, workspaces_dir, monkeypatch):
    """A routine `doctor NAME --fetch` must not fetch an inactive manual binding
    (that would trigger a slow/interactive provider). Here GH_TOK is UNSET: if the
    manual binding were fetched it would FAIL, but it is skipped, so it passes."""
    from credproxy_cli.core.engine import doctor
    monkeypatch.setenv("ALWAYS_TOK", "secret-value")
    monkeypatch.delenv("GH_TOK", raising=False)
    ws = _mk_running_ws(workspaces_dir, "fetchskip")

    checks = {c.id: c for c in doctor.run("fetchskip", fetch=True)}
    always = checks["ws:fetchskip:always:fetch"]
    gh = checks["ws:fetchskip:gh:fetch"]
    assert always.ok and "resolved" in always.message
    assert gh.ok and "inactive" in gh.message


def test_doctor_fetch_resolves_active_manual(xdg, workspaces_dir, monkeypatch):
    """Once the manual binding is active (tracked in applied.active), --fetch DOES
    resolve it -- so a set-but-active provider is checked like any other."""
    from credproxy_cli.core.engine import doctor
    from credproxy_cli.core.engine.containers import _update_applied
    monkeypatch.setenv("ALWAYS_TOK", "secret-value")
    monkeypatch.setenv("GH_TOK", "gh-value")
    ws = _mk_running_ws(workspaces_dir, "fetchactive")
    _update_applied(ws, active=["gh"])

    checks = {c.id: c for c in doctor.run("fetchactive", fetch=True)}
    assert checks["ws:fetchactive:gh:fetch"].ok
    assert "resolved" in checks["ws:fetchactive:gh:fetch"].message


# ---- engine: start-flow reset + setup bracket -------------------------------


def _drive_start(ws, monkeypatch, *, run_setup_exc=None, pushes=None,
                 setup_bindings=None):
    """Drive `_start_workspace_locked` with a fully-faked docker/proxy/setup, so
    the manual-selection behaviour (proxy_fresh reset, the required_for_setup
    setup bracket) can be asserted without a container engine. `run_setup_exc`
    makes the faked `run_setup` raise (to exercise the setup-failure narrowing);
    `pushes`/`setup_bindings` can be pre-created so the caller sees captures even
    when the drive raises. Returns the list of
    pushed binding-name lists, in push order."""
    from credproxy_cli.core.engine import startup, containers, setup, docker
    from credproxy_cli.core.engine.imageenv import ImageEnv

    meta = ImageEnv(http_port=39998, tmpfs="/run/secrets",
                    token="/run/secrets-ro/auth.token", source="/opt/proxy",
                    mitmproxy_uid=31337)
    monkeypatch.setattr(ImageEnv, "load", staticmethod(lambda image=None: meta))

    ids = {}

    def fake_inspect(target, template):
        if template == "{{.Id}}":
            if target == ws.proxy_container:
                return "proxyid"
            if target == ws.ws_container:
                return "wsid"
            return "imgid"
        return None  # spec label / image -> None so containers create fresh

    monkeypatch.setattr(docker, "inspect", fake_inspect)
    # Proxy + workspace both absent -> created fresh (proxy_fresh=True).
    monkeypatch.setattr(docker, "container_status", lambda c: None)
    monkeypatch.setattr(docker, "resolve_host_port", lambda c, p: 39998)
    monkeypatch.setattr(docker, "docker", lambda *a, **k: "")
    monkeypatch.setattr(docker, "docker_quiet", lambda *a, **k: "")
    monkeypatch.setattr(startup, "wait_for_ready", lambda port: None)
    monkeypatch.setattr(containers, "create_proxy", lambda ws, meta: None)
    monkeypatch.setattr(containers, "create_ws_container",
                        lambda ws, cfg, spec, proxy_id=None: None)
    monkeypatch.setattr(containers, "chown_mount_parents",
                        lambda ws, cfg, notify=None: None)

    # setup runs once (fresh container); capture the bindings it is handed.
    monkeypatch.setattr(setup, "_setup_needed", lambda marker, cid: True)
    monkeypatch.setattr(setup, "_read_setup_marker", lambda ws: None)
    monkeypatch.setattr(setup, "_write_setup_marker", lambda ws, cid: None)
    monkeypatch.setattr(setup, "chown_user_owned_volumes",
                        lambda ws, cfg, notify=None: None)
    setup_bindings = {} if setup_bindings is None else setup_bindings

    def fake_run_setup(ws, cfg, notify=None, bindings=()):
        setup_bindings.update(names=[b.name for b in bindings])
        if run_setup_exc is not None:
            raise run_setup_exc

    monkeypatch.setattr(setup, "run_setup", fake_run_setup)

    pushes = [] if pushes is None else pushes
    gen = [0]

    def fake_push_config(ws, port, notify=None, bindings=None, rules=None,
                         fingerprint=None, postgres=None):
        gen[0] += 1
        pushes.append([b.name for b in bindings])
        return bindings, rules, gen[0]

    monkeypatch.setattr(startup, "push_config", fake_push_config)

    startup._start_workspace_locked(ws)
    return pushes, setup_bindings


def test_start_fresh_proxy_resets_active(xdg, workspaces_dir, monkeypatch):
    """A fresh proxy resets applied.active to empty, so a manual binding activated
    in a prior run is OFF after a stop/start -- and is not in the pushed set."""
    from credproxy_cli.core.engine.containers import (
        _load_applied_active, _update_applied)
    ws = _mk_running_ws(workspaces_dir, "w")
    _update_applied(ws, active=["gh"])  # a stale activation from a prior run

    pushes, _ = _drive_start(ws, monkeypatch)

    assert _load_applied_active(ws) == set()          # reset
    assert pushes[0] == ["always"]                     # manual excluded


def test_setup_bracket_widens_then_narrows(xdg, workspaces_dir, monkeypatch):
    """A `required_for_setup` manual binding is pushed for the setup window only:
    the setup step sees it, but the resting config (and applied.active) exclude it."""
    from credproxy_cli.core.engine.containers import _load_applied_active
    ws = _write_ws(workspaces_dir, "w", """
        image = "x"

        [[binding]]
        name     = "always"
        injector = "bearer"
        provider = "env"
        secret   = "ALWAYS_TOK"
        hosts    = ["api.always.com"]

        [[binding]]
        name     = "prov"
        injector = "bearer"
        provider = "env"
        secret   = "PROV_TOK"
        hosts    = ["api.prov.com"]
        manual   = true
        required_for_setup = true
    """)
    ws.ensure_state_dir()
    ws.token_path.write_text("tok\n")

    pushes, setup_bindings = _drive_start(ws, monkeypatch)

    # push 1: resting selected (manual excluded); push 2: widened for setup;
    # push 3: narrowed back to the resting set.
    assert pushes[0] == ["always"]
    assert set(pushes[1]) == {"always", "prov"}
    assert pushes[-1] == ["always"]
    # The setup step saw the widened set (its placeholder is available).
    assert set(setup_bindings["names"]) == {"always", "prov"}
    # Never persisted into the active set.
    assert _load_applied_active(ws) == set()


def test_start_no_required_for_setup_single_push(xdg, workspaces_dir, monkeypatch):
    """The common all-always-on / no-required-for-setup path pushes exactly once
    (no widen/narrow bracket)."""
    ws = _mk_running_ws(workspaces_dir, "w")  # always + manual gh (not req-for-setup)
    pushes, setup_bindings = _drive_start(ws, monkeypatch)
    assert pushes == [["always"]]
    assert setup_bindings["names"] == ["always"]


def _req_for_setup_ws(workspaces_dir, name):
    ws = _write_ws(workspaces_dir, name, """
        image = "x"

        [[binding]]
        name     = "always"
        injector = "bearer"
        provider = "env"
        secret   = "ALWAYS_TOK"
        hosts    = ["api.always.com"]

        [[binding]]
        name     = "prov"
        injector = "bearer"
        provider = "env"
        secret   = "PROV_TOK"
        hosts    = ["api.prov.com"]
        manual   = true
        required_for_setup = true
    """)
    ws.ensure_state_dir()
    ws.token_path.write_text("tok\n")
    return ws


def test_setup_failure_still_narrows_the_bracket(xdg, workspaces_dir, monkeypatch):
    """If a setup step FAILS mid-bracket, the widened `required_for_setup`
    credential must NOT be left live on the proxy: the finally-narrow re-pushes the
    resting set, and the setup error still propagates."""
    ws = _req_for_setup_ws(workspaces_dir, "w")
    pushes: list = []
    with pytest.raises(RuntimeError, match="boom"):
        _drive_start(ws, monkeypatch, run_setup_exc=RuntimeError("boom"),
                     pushes=pushes)
    # push 1 resting, push 2 widened (for setup), push 3 narrowed back -- the
    # narrow ran despite the setup failure.
    assert set(pushes[1]) == {"always", "prov"}
    assert pushes[-1] == ["always"]


# ---- porcelain: binding add / list ------------------------------------------


def test_binding_add_manual_writes_flag(xdg, workspaces_dir):
    from credproxy_cli.porcelain.cli import main as cli_main
    import sys
    from credproxy_cli.core.model.bindings import load_bindings
    from credproxy_cli.core.model.workspace import Workspace
    _write_ws(workspaces_dir, "w", 'image = "x"\n')
    old = sys.argv[:]
    try:
        sys.argv = ["credproxy", "workspace", "w", "binding", "add",
                    "--injector", "bearer", "--provider", "env",
                    "--secret", "TOK", "--host", "api.github.com",
                    "--name", "gh", "--manual"]
        try:
            cli_main(loose_default=False)
        except SystemExit:
            pass
    finally:
        sys.argv = old
    (b,) = load_bindings(Workspace("w"))
    assert b.manual is True


def test_binding_add_required_for_setup_implies_manual(xdg, workspaces_dir):
    import sys
    from credproxy_cli.porcelain.cli import main as cli_main
    from credproxy_cli.core.model.bindings import load_bindings
    from credproxy_cli.core.model.workspace import Workspace
    _write_ws(workspaces_dir, "w", 'image = "x"\n')
    old = sys.argv[:]
    try:
        sys.argv = ["credproxy", "workspace", "w", "binding", "add",
                    "--injector", "bearer", "--provider", "env",
                    "--secret", "TOK", "--host", "api.github.com",
                    "--name", "gh", "--required-for-setup"]
        try:
            cli_main(loose_default=False)
        except SystemExit:
            pass
    finally:
        sys.argv = old
    (b,) = load_bindings(Workspace("w"))
    assert b.manual is True and b.required_for_setup is True
