"""Definition/registry commands: injector & provider scaffolds and listings, the
scripted-injector authoring helpers (`injector api`/`injector check`), the
top-level `pack list`, `provider show`, and `script check`."""
from __future__ import annotations

import sys

from ..core.paths import IMAGE_TAG, PROXY_DIR
from . import render
from .render import fail, say
from .common import Ctx


def do_scaffold(ctx: Ctx, kind: str, name: str, lang: str = "python") -> None:
    from ..core.model.scaffold import scaffold

    result = scaffold(kind, name, lang)
    render.OUT.scaffolded(result.kind, result.name, str(result.path))
    if kind == "provider":
        say("the template is just a starting point -- a provider can be any "
            "executable that speaks the JSON protocol (docs/reference/providers.md).")


def do_def_list(ctx: Ctx, kind: str) -> None:
    if kind == "injector":
        from ..core.model.injectors import list_injectors
        rows = [
            {
                "name": d.name,
                "scheme": d.scheme if d.scheme != "script"
                else f"script:{d.spec.family}",
                "source": d.source,
                "shadows": list(d.shadows),
            }
            for d in list_injectors()
        ]
    else:
        from ..core.providers import list_providers
        rows = [
            {"name": d.name, "source": d.source, "description": d.description or "",
             "shadows": list(d.shadows)}
            for d in list_providers()
        ]
    render.OUT.def_list(kind, rows)


def do_pack_list(ctx: Ctx) -> None:
    from ..core.model.packs import describe_packs

    render.OUT.pack_list(describe_packs())


def do_provider_show(ctx: Ctx, name: str) -> None:
    from ..core.providers import find_provider, _describe, _help

    p = find_provider(name)  # raises ProviderError if missing / not executable
    render.OUT.provider_show({
        "name": p.name,
        "source": p.source,
        "path": str(p.exe),
        "description": _describe(p.exe),
        "help": _help(p.exe),
    })


def do_scaffold_script(ctx: Ctx, name: str, family: str) -> None:
    from ..core.model.scaffold import scaffold_script

    r = scaffold_script(name, family)
    render.OUT.scaffolded_script(
        r.name, str(r.injector_path), str(r.script_path), r.family)


def do_injector_api(ctx: Ctx) -> None:
    from ..core.model.scaffold import script_api_reference

    render.OUT.injector_api(script_api_reference())


def do_injector_check(ctx: Ctx, name: str, do_compile: bool) -> None:
    from ..core.model.injectors import find_injector
    from ..core.model.scripts import find_script

    inj = find_injector(name)  # parses + validates the manifest (raises if bad)
    if inj.scheme != "script":
        render.OUT.injector_check(name, {
            "scheme": inj.scheme, "scripted": False, "ok": True,
            "detail": f"built-in scheme '{inj.scheme}'; nothing to compile"})
        return
    script = find_script(inj.script)  # raises InjectorError if missing
    detail = (f"manifest ok (family={inj.spec.family}, "
              f"slots={list(inj.spec.slots)}); script '{inj.script}' "
              f"resolves ({script.source_origin})")
    if not do_compile:
        render.OUT.injector_check(name, {
            "scheme": "script", "scripted": True, "ok": True,
            "compiled": False, "detail": detail})
        return
    err = _compile_script_in_image(script.source)
    render.OUT.injector_check(name, {
        "scheme": "script", "scripted": True, "ok": err is None,
        "compiled": True, "detail": detail, "compile_error": err})
    if err is not None:
        sys.exit(1)


def _compile_script_in_image(source: str) -> str | None:
    """Compile a `.star` in the proxy image (which carries the Starlark runtime),
    so the host needs no starlark dep. Returns None on success, else the error
    text. Mirrors what the proxy does at push time. Needs docker + the image."""
    import os
    import subprocess
    import tempfile

    pycode = (
        "import sys\n"
        "from starlark_runtime import ScriptedScheme\n"
        "src = open('/work/check.star').read()\n"
        "try:\n"
        "    ScriptedScheme(name='check', source=src, filename='check.star')\n"
        "except Exception as e:\n"
        "    print('%s: %s' % (type(e).__name__, e)); sys.exit(1)\n"
        "print('ok')\n"
    )
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o755)
        p = os.path.join(d, "check.star")
        with open(p, "w") as f:
            f.write(source)
        os.chmod(p, 0o644)
        cmd = ["docker", "run", "--rm", "-v", f"{d}:/work:ro"]
        # Prefer the live proxy source when the repo is checked out (parity with
        # `dev test`), so a `dev build`-stale image doesn't give wrong verdicts;
        # otherwise the baked image's runtime is the contract.
        if PROXY_DIR.is_dir():
            cmd += ["-v", f"{PROXY_DIR}:/opt/proxy:ro"]
        cmd += ["-w", "/opt/proxy", "--entrypoint", "python", IMAGE_TAG,
                "-c", pycode]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            fail("`injector check --compile` needs docker (not found on PATH)")
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        return None
    if "Unable to find image" in out or "No such image" in out:
        fail(f"proxy image '{IMAGE_TAG}' not found; build it with "
             f"`credproxy dev build`")
    return out or f"compile failed (exit {r.returncode})"


def do_script_check(ctx: Ctx, name: str | None, force_container: bool) -> None:
    """Compile the named script (or every resolvable script) in the proxy runtime
    and report per-script results. Exit 0 iff all pass."""
    from ..core.engine import scriptcheck

    results = scriptcheck.run(name, force_container=force_container)
    if not results:
        say(f"no script '{name}' found" if name else "no scripts to check")
    render.OUT.script_check([
        {"name": r.name, "origin": r.origin, "ok": r.ok, "error": r.error,
         "profiles": list(r.profiles)}
        for r in results
    ])
    if any(not r.ok for r in results):
        sys.exit(1)
