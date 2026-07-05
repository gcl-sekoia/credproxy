"""`credproxy doctor`: environment preflight + config validation in one sweep.

The weakest first-run paths -- docker missing/unreachable, proxy image not built,
an invalid hand-edited workspace TOML, an injector/provider that doesn't resolve,
a bad host glob -- all fail today one-error-at-a-time and only at action time
(`start`, `binding add`). `doctor` runs every cheap check at once and reports
**all** failures. No side effects; `fetch=True` (opt-in) additionally resolves
secrets (which can prompt/unlock). Pure data out (`list[Check]`); the porcelain
layer renders and sets the exit code.
"""
from __future__ import annotations

import shutil
import subprocess
import tomllib
from dataclasses import dataclass

from . import hostmatch
from .errors import CredproxyError
from .imageenv import ImageEnv
from .paths import IMAGE_TAG, overlay_dirs
from .workspace import Workspace, for_name, list_names


@dataclass
class Check:
    id: str                       # stable, e.g. "docker", "image", "ws:cfg:myproj"
    ok: bool
    message: str
    hint: str | None = None


def _ok(id: str, message: str) -> Check:
    return Check(id, True, message)


def _fail(id: str, message: str, hint: str | None = None) -> Check:
    return Check(id, False, message, hint)


def run(ws_name: str | None = None, *, fetch: bool = False) -> list[Check]:
    """All checks: the environment, then each target workspace (NAME, or every
    workspace when NAME is None).

    An explicit NAME goes through `for_name`, the same charset/reserved-name/
    traversal validation every other command uses -- so `doctor '../../etc/passwd'`
    is a clean error, not a config read outside the workspaces dir. `list_names()`
    only returns already-valid names, so the scan-all path needs no re-validation."""
    checks = _env_checks()
    targets = [for_name(ws_name)] if ws_name else [Workspace(n) for n in list_names()]
    for ws in targets:
        checks += _workspace_checks(ws, fetch=fetch)
    return checks


def _env_checks() -> list[Check]:
    out: list[Check] = []
    if shutil.which("docker") is None:
        # Nothing else works without the engine; stop here with a clear hint.
        return [_fail("docker", "docker not found on PATH",
                      "install Docker or rootless Podman (see README)")]
    try:
        r = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
        out.append(_ok("docker", "docker daemon reachable") if r.returncode == 0
                   else _fail("docker", "docker found but the daemon is unreachable",
                              "start Docker / the podman socket"))
    except (OSError, subprocess.SubprocessError) as e:
        out.append(_fail("docker", f"`docker info` failed: {e}"))

    try:
        ImageEnv.load(IMAGE_TAG)
        out.append(_ok("image", f"proxy image {IMAGE_TAG} present + valid"))
    except CredproxyError as e:
        out.append(_fail("image", str(e), "run `credproxy dev build`"))

    # One existence check per CONFIGURED overlay entry (index-qualified so a
    # --json consumer can't collide two). Resolution stays tolerant of a missing
    # overlay elsewhere -- flagging it loudly is doctor's job. The default
    # `<repo>/overlay/` counts as configured (upstream ships it), so it too must
    # exist. `overlay_dirs()` labels are `overlay:<base>`; the check id carries
    # the bare basename for readability.
    for i, (label, d) in enumerate(overlay_dirs()):
        base = label.split(":", 1)[1]
        cid = f"overlay[{i}]:{base}:exists"
        out.append(_ok(cid, f"overlay {d} exists") if d.is_dir() else
                   _fail(cid, f"configured overlay {d} does not exist",
                         "create the directory or drop it from CREDPROXY_OVERLAY_PATH"))
    return out


def _workspace_checks(ws: Workspace, *, fetch: bool) -> list[Check]:
    if not ws.exists():
        return [_fail(f"ws:{ws.name}", f"workspace '{ws.name}' has no config file",
                      f"credproxy workspace create {ws.name}")]
    out: list[Check] = []
    from .config import load_config
    try:
        load_config(ws)
        out.append(_ok(f"ws:{ws.name}:config", f"[{ws.name}] container config valid"))
    except CredproxyError as e:
        out.append(_fail(f"ws:{ws.name}:config", f"[{ws.name}] config: {e}"))
    out += _binding_checks(ws, fetch=fetch)
    out += _script_compile_checks(ws)
    out += _rule_checks(ws)
    return out


def _script_compile_checks(ws: Workspace) -> list[Check]:
    """Upgrade the binding-layer script-existence probe to a real COMPILE for each
    binding whose injector is scripted -- but only when the proxy Starlark runtime
    imports on-host (no docker, no venv required for doctor's other checks). When
    it doesn't import, emit a single skip-with-note pointing at `script check`
    rather than failing (doctor must degrade gracefully)."""
    from . import scriptcheck
    from .bindings import load_bindings
    from .injectors import find_injector
    from .scripts import find_script

    try:
        bindings = load_bindings(ws)
    except (CredproxyError, tomllib.TOMLDecodeError, OSError):
        return []  # the :bindings check already reported the parse/validate failure

    # Distinct scripted injectors referenced by this workspace's bindings.
    scripted: dict[str, object] = {}
    for b in bindings:
        try:
            inj = find_injector(b.injector)
        except CredproxyError:
            continue  # :binding[i]:injector already flagged it
        if inj.scheme == "script" and inj.script:
            scripted.setdefault(inj.script, inj)
    if not scripted:
        return []

    if not scriptcheck.starlark_importable():
        return [Check(f"ws:{ws.name}:scripts", True,
                      f"[{ws.name}] {len(scripted)} scripted injector(s) resolve; "
                      f"compile skipped (Starlark runtime not importable on-host)",
                      "run `credproxy script check` for a full compile "
                      "(on-host with the proxy deps, or in the image)")]

    out: list[Check] = []
    for script_name, inj in scripted.items():
        cid = f"ws:{ws.name}:script:{script_name}"
        try:
            source = find_script(inj.script).source
        except CredproxyError as e:
            out.append(_fail(cid, f"[{ws.name}] script '{script_name}': {e}"))
            continue
        err = scriptcheck.compile_injector_paired(inj, source)
        out.append(_ok(cid, f"[{ws.name}] script '{script_name}' compiles")
                   if err is None else
                   _fail(cid, f"[{ws.name}] script '{script_name}' fails to compile: {err}"))
    return out


def _binding_checks(ws: Workspace, *, fetch: bool) -> list[Check]:
    """Binding checks in two layers, so a run both reports EVERY problem and
    upholds the "doctor passes => start passes" contract:

    1. Independent per-binding probes (injector resolves, provider resolves, each
       host glob valid) off the raw TOML -- report-all, so one broken binding
       doesn't hide the next one's problems.
    2. The real aggregate `load_bindings(ws)` -- the SAME parse+validate `start`
       runs. This catches what the shallow probes structurally can't: missing
       required fields, duplicate names, secret slots that don't match the scheme,
       (host, wire-location) collisions, a scripted injector naming a missing
       `.star`. First-error, but that's the action-time behavior we're mirroring.

    `fetch` (opt-in) additionally resolves each secret via its provider; it reuses
    the layer-2 parse, so it means only "also fetch", never a different verdict."""
    out: list[Check] = []
    try:
        raw = tomllib.loads(ws.config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [_fail(f"ws:{ws.name}:toml", f"[{ws.name}] TOML parse: {e}")]
    bindings = raw.get("binding") or []
    if not isinstance(bindings, list):
        return [_fail(f"ws:{ws.name}:bindings", f"[{ws.name}] `binding` must be an array")]

    _probe_bindings_raw(ws, bindings, out)

    # Layer 2: the authoritative parse+validate. Reuse its result for --fetch so a
    # parse failure can't yield a different exit code with vs. without --fetch.
    from .bindings import load_bindings, test_bindings
    bs = None
    try:
        bs = load_bindings(ws)
        out.append(_ok(f"ws:{ws.name}:bindings",
                       f"[{ws.name}] {len(bs)} binding(s) pass static checks"))
    except (CredproxyError, tomllib.TOMLDecodeError, OSError) as e:
        # load_bindings does a raw tomllib.loads (TOMLDecodeError isn't a
        # CredproxyError); the top-of-function probe already returns on a parse
        # error, but stay defensive against a between-reads change.
        out.append(_fail(f"ws:{ws.name}:bindings", f"[{ws.name}] bindings: {e}"))

    if fetch and bs is not None:
        for r in test_bindings(bs):
            out.append(_ok(f"ws:{ws.name}:{r.name}:fetch",
                           f"[{ws.name}] {r.name}: secret resolved ({r.value_len} chars)")
                       if r.ok else
                       _fail(f"ws:{ws.name}:{r.name}:fetch", f"[{ws.name}] {r.name}: {r.error}"))
    return out


def _probe_bindings_raw(ws: Workspace, bindings: list, out: list[Check]) -> None:
    """Layer-1 report-all probes. Check ids are qualified by binding INDEX (not
    the human name, which may be absent or duplicated) plus a host index, so no
    two failures ever share an id -- a `--json` consumer keying by id can't
    silently drop one. The human `name` still rides in the message."""
    from .injectors import find_injector
    from .providers import find_provider
    for i, b in enumerate(bindings):
        bid = f"ws:{ws.name}:binding[{i}]"
        if not isinstance(b, dict):
            out.append(_fail(bid, f"[{ws.name}] binding[{i}] is not a table"))
            continue
        label = b.get("name") or f"binding[{i}]"
        inj = b.get("injector")
        if isinstance(inj, str) and inj:
            try:
                find_injector(inj)
            except CredproxyError as e:
                out.append(_fail(f"{bid}:injector", f"[{ws.name}] {label}: {e}"))
        prov = b.get("provider")
        if isinstance(prov, str) and prov:
            try:
                find_provider(prov)
            except CredproxyError as e:
                out.append(_fail(f"{bid}:provider", f"[{ws.name}] {label}: {e}"))
        hosts = b.get("hosts")
        if isinstance(hosts, list):
            for j, h in enumerate(hosts):
                if isinstance(h, str) and hostmatch.is_pattern(h):
                    err = hostmatch.validate_pattern(h)
                    if err:
                        out.append(_fail(f"{bid}:host[{j}]", f"[{ws.name}] {label}: {err}"))


def _rule_checks(ws: Workspace) -> list[Check]:
    """The credential-free `[[rule]]` layer runs its own parse+validate at `start`
    (`materialize_rules -> rules.validate`: bad path glob, unknown script, duplicate
    names, action-field errors). Mirror the layer-2 bindings check so a broken rule
    is caught by doctor too, not one PR later at `start`."""
    from .rules import load_rules
    try:
        rs = load_rules(ws)
        return [_ok(f"ws:{ws.name}:rules", f"[{ws.name}] {len(rs)} rule(s) valid")]
    except (CredproxyError, tomllib.TOMLDecodeError, OSError) as e:
        # load_rules does a raw tomllib.loads, which raises TOMLDecodeError (not a
        # CredproxyError) on a malformed file -- catch it so doctor reports the
        # broken workspace instead of crashing on the command whose whole job is
        # to report failures cleanly. (`:config`/`:toml` already flag the parse
        # error; this keeps the sweep going.)
        return [_fail(f"ws:{ws.name}:rules", f"[{ws.name}] rules: {e}")]
