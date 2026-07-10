"""`credproxy script check`: compile `.star` scripts before they are pushed.

A scripted injector/rule is compiled only at proxy push (`start`/`apply`), so a
syntax error or a forbidden-primitive reference surfaces late. This module
compiles resolvable scripts in the proxy runtime -- on-host when the Starlark
runtime imports there, else in the proxy image (the same fallback `dev test`
uses) -- so an author gets the verdict up front.

**Two primitive profiles, classified not guessed.** A bare `.star` doesn't say
whether it is an injector script or a rule script; they compile under different
primitive sets. Rule:

  - a script referenced by a resolvable `scheme = "script"` injector manifest is
    compiled under the INJECTOR profile PAIRED with that manifest (its
    family/slots/location), so a slot/family mistake in the manifest surfaces too;
  - an unreferenced script is tried under BOTH profiles and passes if EITHER
    compiles, reporting which profile(s) succeeded.

The compile itself is identical whether it runs on-host or in the image; the
host resolves the scripts + injector references and hands sources + classification
to the runtime.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass

from ..errors import CredproxyError, DependencyError, ImageError
from ..model.injectors import list_injectors
from ..model.scripts import Script, find_script, list_scripts


@dataclass(frozen=True)
class ScriptCheckResult:
    name: str
    origin: str            # tier label: "user" | "overlay:<name>" | "builtin"
    ok: bool
    error: str | None
    profiles: tuple[str, ...]  # profile(s) that compiled ("inject"/"rule")


# --------------------------------------------------------------------------
# Classification + planning (host-side)
# --------------------------------------------------------------------------


def _reference_map() -> dict[str, dict]:
    """Map each script name -> the compile metadata of the FIRST resolvable
    `scheme = "script"` injector that references it (family/slots/location/header
    default). Resolving those injectors validates their family/slots, so pairing
    a script with its manifest catches slot/family mistakes as well."""
    refs: dict[str, dict] = {}
    for inj in list_injectors():
        if inj.scheme == "script" and inj.script and inj.script not in refs:
            refs[inj.script] = {
                "family": inj.spec.family,
                "slots": list(inj.spec.slots),
                "location_kind": inj.spec.location_kind,
                "header_default": inj.spec.header_default,
            }
    return refs


def _plan(name: str | None) -> list[dict]:
    """Build the per-script compile plan. A referenced script gets mode "inject"
    with its manifest metadata; an unreferenced one gets mode "both"."""
    scripts: list[Script] = [find_script(name)] if name else list_scripts()
    refs = _reference_map()
    plan: list[dict] = []
    for s in scripts:
        entry = {"name": s.name, "origin": s.source_origin, "source": s.source}
        meta = refs.get(s.name)
        if meta is not None:
            entry.update(mode="inject", **meta)
        else:
            entry["mode"] = "both"
        plan.append(entry)
    return plan


# --------------------------------------------------------------------------
# Compilation (proxy runtime -- on-host or in the image)
# --------------------------------------------------------------------------
#
# `_CHECKER` runs INSIDE the proxy runtime (on-host subprocess or `docker run`).
# It reads the plan as JSON on stdin and emits `{"results": [...]}` on stdout.
# stdout is redirected to devnull around the compile so the runtime's one-time
# "no cancellation" log line can't corrupt the JSON result.

_CHECKER = r'''
import sys, json, os

plan = json.load(sys.stdin)
_real = sys.stdout
sys.stdout = open(os.devnull, "w")
from starlark_runtime import ScriptedScheme

def _inject(name, src, family, slots, location_kind, header_default):
    ScriptedScheme(name, src, family=family, slots=tuple(slots),
                   location_kind=location_kind, header_default=header_default)

def _rule(name, src):
    ScriptedScheme(name, src, kind="rule")

def _err(prefix, ex):
    return "%s%s: %s" % (prefix, type(ex).__name__, ex)

results = []
for e in plan["scripts"]:
    name, src, mode = e["name"], e["source"], e["mode"]
    ok, error, profiles = False, None, []
    if mode == "inject":
        try:
            _inject(name, src, e["family"], e["slots"], e["location_kind"],
                    e["header_default"])
            ok, profiles = True, ["inject"]
        except Exception as ex:
            error = _err("", ex)
    elif mode == "rule":
        try:
            _rule(name, src)
            ok, profiles = True, ["rule"]
        except Exception as ex:
            error = _err("", ex)
    else:  # both: pass if EITHER profile compiles
        errs = []
        try:
            _inject(name, src, "substitute", ["value"], "header", "Authorization")
            profiles.append("inject")
        except Exception as ex:
            errs.append(_err("inject: ", ex))
        try:
            _rule(name, src)
            profiles.append("rule")
        except Exception as ex:
            errs.append(_err("rule: ", ex))
        ok = bool(profiles)
        if not ok:
            error = "; ".join(errs)
    results.append({"name": name, "origin": e["origin"], "ok": ok,
                    "error": error, "profiles": profiles})

sys.stdout = _real
print(json.dumps({"results": results}))
'''


def _add_proxy_to_path() -> None:
    from ..paths import PROXY_DIR
    p = str(PROXY_DIR)
    if PROXY_DIR.is_dir() and p not in sys.path:
        sys.path.insert(0, p)


def starlark_importable() -> bool:
    """True if the proxy Starlark runtime imports on-host (so a compile needs no
    docker). Adds the proxy dir to sys.path first (it is not there by default)."""
    _add_proxy_to_path()
    return importlib.util.find_spec("starlark") is not None


def _compile_in_process(plan: list[dict]) -> list[dict]:
    """Compile the plan in-process (starlark importable on-host). stdout is
    suppressed around each construction so the runtime's log line stays out of
    the result."""
    import contextlib
    import io

    _add_proxy_to_path()
    from starlark_runtime import ScriptedScheme

    def _one(e: dict) -> dict:
        name, src, mode = e["name"], e["source"], e["mode"]
        ok, error, profiles = False, None, []
        with contextlib.redirect_stdout(io.StringIO()):
            if mode == "inject":
                try:
                    ScriptedScheme(name, src, family=e["family"],
                                   slots=tuple(e["slots"]),
                                   location_kind=e["location_kind"],
                                   header_default=e["header_default"])
                    ok, profiles = True, ["inject"]
                except Exception as ex:
                    error = f"{type(ex).__name__}: {ex}"
            elif mode == "rule":
                try:
                    ScriptedScheme(name, src, kind="rule")
                    ok, profiles = True, ["rule"]
                except Exception as ex:
                    error = f"{type(ex).__name__}: {ex}"
            else:  # both
                errs = []
                try:
                    ScriptedScheme(name, src)  # inject defaults
                    profiles.append("inject")
                except Exception as ex:
                    errs.append(f"inject: {type(ex).__name__}: {ex}")
                try:
                    ScriptedScheme(name, src, kind="rule")
                    profiles.append("rule")
                except Exception as ex:
                    errs.append(f"rule: {type(ex).__name__}: {ex}")
                ok = bool(profiles)
                if not ok:
                    error = "; ".join(errs)
        return {"name": name, "origin": e["origin"], "ok": ok,
                "error": error, "profiles": profiles}

    return [_one(e) for e in plan]


def _compile_in_image(plan: list[dict]) -> list[dict]:
    """Compile the plan inside the proxy image via `docker run` (Starlark not
    importable on-host). Mirrors `dev test`'s image fallback; mounts the live
    proxy source when the repo is checked out so a stale image can't mislead."""
    import json as _json
    import subprocess

    from . import docker as core_docker
    from .imageenv import ImageEnv
    from ..paths import IMAGE_TAG, PROXY_DIR

    meta = ImageEnv.load()
    cmd = ["docker", "run", "--rm", "-i"]
    if PROXY_DIR.is_dir():
        cmd += ["-v", f"{PROXY_DIR}:{meta.source}:ro"]
    cmd += ["-w", meta.source, "--entrypoint", "python", IMAGE_TAG, "-c", _CHECKER]
    try:
        r = subprocess.run(cmd, input=_json.dumps({"scripts": plan}),
                           capture_output=True, text=True)
    except FileNotFoundError:
        raise DependencyError(core_docker.DOCKER_MISSING_MSG)
    if r.returncode != 0:
        out = (r.stdout + r.stderr).strip()
        if "Unable to find image" in out or "No such image" in out:
            raise ImageError(f"proxy image '{IMAGE_TAG}' not found; build it with "
                             f"`credproxy dev build`")
        raise CredproxyError(f"script check: compile failed in the image: {out}")
    try:
        return _json.loads(r.stdout)["results"]
    except Exception:
        raise CredproxyError(
            f"script check: unexpected checker output: {r.stdout!r}")


def run(name: str | None = None, *, force_container: bool = False
        ) -> list[ScriptCheckResult]:
    """Check the named script, or every resolvable script when NAME is None.
    On-host when the Starlark runtime imports there, else in the proxy image."""
    plan = _plan(name)
    if not plan:
        return []
    if not force_container and starlark_importable():
        rows = _compile_in_process(plan)
    else:
        rows = _compile_in_image(plan)
    return [ScriptCheckResult(r["name"], r["origin"], r["ok"], r["error"],
                              tuple(r["profiles"])) for r in rows]


# --------------------------------------------------------------------------
# doctor integration
# --------------------------------------------------------------------------


def compile_injector_paired(injector, source: str) -> str | None:
    """In-process compile of one scripted injector paired with its manifest.
    Returns None on success, else the error text. Caller must first confirm
    `starlark_importable()`. Used by `doctor` to upgrade its script-existence
    probe to a real compile without shelling out to docker."""
    entry = {
        "name": injector.script, "origin": injector.source, "source": source,
        "mode": "inject", "family": injector.spec.family,
        "slots": list(injector.spec.slots),
        "location_kind": injector.spec.location_kind,
        "header_default": injector.spec.header_default,
    }
    row = _compile_in_process([entry])[0]
    return None if row["ok"] else row["error"]
