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

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass

from . import hostmatch
from .errors import CredproxyError
from .imageenv import ImageEnv
from .paths import IMAGE_TAG
from .workspace import Workspace, list_names


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
    workspace when NAME is None)."""
    checks = _env_checks()
    names = [ws_name] if ws_name else list_names()
    for name in names:
        checks += _workspace_checks(Workspace(name), fetch=fetch)
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

    prof = os.environ.get("CREDPROXY_PROFILE_DIR")
    if prof:
        out.append(_ok("profile", f"profile overlay {prof} exists")
                   if os.path.isdir(prof) else
                   _fail("profile", f"CREDPROXY_PROFILE_DIR {prof} does not exist",
                         "unset it or create the directory"))
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
    return out


def _binding_checks(ws: Workspace, *, fetch: bool) -> list[Check]:
    """Per-binding, INDEPENDENT checks (injector resolves, provider resolves, host
    globs valid) so a run reports every problem, not just the first."""
    from .injectors import find_injector
    from .providers import find_provider
    out: list[Check] = []
    try:
        raw = tomllib.loads(ws.config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        return [_fail(f"ws:{ws.name}:toml", f"[{ws.name}] TOML parse: {e}")]
    bindings = raw.get("binding") or []
    if not isinstance(bindings, list):
        return [_fail(f"ws:{ws.name}:bindings", f"[{ws.name}] `binding` must be an array")]

    for i, b in enumerate(bindings):
        if not isinstance(b, dict):
            out.append(_fail(f"ws:{ws.name}:binding[{i}]",
                             f"[{ws.name}] binding[{i}] is not a table"))
            continue
        name = b.get("name") or f"binding[{i}]"
        inj = b.get("injector")
        if isinstance(inj, str) and inj:
            try:
                find_injector(inj)
            except CredproxyError as e:
                out.append(_fail(f"ws:{ws.name}:{name}:injector", f"[{ws.name}] {name}: {e}"))
        prov = b.get("provider")
        if isinstance(prov, str) and prov:
            try:
                find_provider(prov)
            except CredproxyError as e:
                out.append(_fail(f"ws:{ws.name}:{name}:provider", f"[{ws.name}] {name}: {e}"))
        hosts = b.get("hosts")
        if isinstance(hosts, list):
            for h in hosts:
                if isinstance(h, str) and hostmatch.is_pattern(h):
                    err = hostmatch.validate_pattern(h)
                    if err:
                        out.append(_fail(f"ws:{ws.name}:{name}:host", f"[{ws.name}] {name}: {err}"))

    if fetch:
        from .bindings import load_bindings, test_bindings
        try:
            bs = load_bindings(ws)
            for r in test_bindings(bs):
                out.append(_ok(f"ws:{ws.name}:{r.name}:fetch",
                               f"[{ws.name}] {r.name}: secret resolved ({r.value_len} chars)")
                           if r.ok else
                           _fail(f"ws:{ws.name}:{r.name}:fetch", f"[{ws.name}] {r.name}: {r.error}"))
        except CredproxyError as e:
            out.append(_fail(f"ws:{ws.name}:fetch", f"[{ws.name}] fetch: {e}"))

    if not any(not c.ok for c in out):
        out.append(_ok(f"ws:{ws.name}:bindings",
                       f"[{ws.name}] {len(bindings)} binding(s) resolve"))
    return out
