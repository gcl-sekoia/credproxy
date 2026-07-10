"""Architecture guard for the model/ <- engine/ plane split (#61).

The model plane (`credproxy_cli.core.model.*`) is the pure config/validation
tier: parsing TOML, shaping the wire body, materializing bindings/rules. It must
NOT reach into the engine plane (docker/subprocess/HTTP transport) nor into
porcelain. This test parses each model module's AST (top-level AND function-body
imports) and asserts the boundary holds.

`providers.py` lives at `core/` root (a genuinely shared leaf that shells host
executables), so it is not under `model/` and is not walked here.
"""
from __future__ import annotations

import ast
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[2] / "cli" / "credproxy_cli" / "core"
MODEL_DIR = CORE_DIR / "model"

# Bare engine module names (as they'd appear in `from ..engine import X` or a
# stray `from ..docker import ...`). The model plane must not import any of them.
ENGINE_MODULES = {
    "docker", "imageenv", "runtime", "compose", "push",
    "proxy_http", "doctor", "scriptcheck",
    # #68 split the old lifecycle monolith into these four.
    "containers", "setup", "startup", "sessions",
}

ENGINE_DIR = CORE_DIR / "engine"


def _model_files():
    return sorted(MODEL_DIR.glob("*.py"))


def _imports(tree: ast.AST):
    """Yield (module_str_or_None, imported_names, node) for every import
    statement anywhere in the tree (top-level and nested in function bodies).

    For `import a.b` -> module is 'a.b', names empty.
    For `from x.y import a, b` -> module 'x.y' (relative level baked into a
    leading-dots prefix), names ['a','b'].
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, [], node
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            mod = (prefix + (node.module or "")) if node.level else (node.module or "")
            yield mod, [a.name for a in node.names], node


def test_model_dir_present():
    files = _model_files()
    assert files, f"no model modules found under {MODEL_DIR}"
    # sanity: the split actually happened
    names = {p.stem for p in files}
    assert "config" in names and "wire" in names and "attach" in names


def test_model_never_imports_subprocess():
    offenders = []
    for f in _model_files():
        tree = ast.parse(f.read_text(), filename=str(f))
        for mod, names, node in _imports(tree):
            base = (mod or "").lstrip(".")
            top = base.split(".")[0] if base else ""
            if top == "subprocess" or "subprocess" in names:
                offenders.append(f"{f.name}:{node.lineno} imports subprocess")
    assert not offenders, (
        "model/ modules must not import subprocess (engine-plane concern):\n"
        + "\n".join(offenders))


def test_model_never_imports_engine():
    offenders = []
    for f in _model_files():
        tree = ast.parse(f.read_text(), filename=str(f))
        for mod, names, node in _imports(tree):
            m = mod or ""
            # normalize away leading relative dots for tail inspection
            stripped = m.lstrip(".")
            parts = stripped.split(".") if stripped else []

            hit = None
            # `from ..engine import X` / `from ..engine.foo import ...` /
            # `import credproxy_cli.core.engine...`
            if "engine" in parts:
                hit = m
            # a bare-name engine module reached relatively: `from ..docker import`
            # or `from .docker import` -> tail component is an engine module,
            # and there's no `model`/`engine` qualifier in between.
            elif parts and parts[-1] in ENGINE_MODULES and "model" not in parts:
                hit = m
            # `from <core-ish> import <engine name / the engine package>`, both
            # relative (`from .. import docker`, dots resolve to core) and
            # absolute (`from credproxy_cli.core import docker`, `... import engine`).
            else:
                core_ish = (
                    m in ("..", "...")                   # from .. / ... import X
                    or stripped == "credproxy_cli.core"  # absolute core package
                )
                bad = [n for n in names if n in ENGINE_MODULES or n == "engine"]
                if core_ish and bad:
                    hit = f"{m} import {', '.join(bad)}"

            if hit is not None:
                offenders.append(f"{f.name}:{node.lineno} imports engine: {hit}")
    assert not offenders, (
        "model/ modules must not import the engine plane:\n" + "\n".join(offenders))


def test_model_never_imports_porcelain():
    offenders = []
    for f in _model_files():
        tree = ast.parse(f.read_text(), filename=str(f))
        for mod, names, node in _imports(tree):
            m = mod or ""
            if "porcelain" in m.split(".") or "porcelain" in names:
                offenders.append(f"{f.name}:{node.lineno} imports porcelain: {m}")
    assert not offenders, (
        "model/ modules must not import porcelain:\n" + "\n".join(offenders))


def _core_files():
    """Every module under core/ (model/, engine/, and the shared leaves at the
    root like providers.py, errors.py, paths.py) -- recursively."""
    return sorted(CORE_DIR.rglob("*.py"))


def test_core_never_imports_porcelain():
    """The WHOLE core plane (model + engine + shared leaves) is porcelain-free:
    porcelain may import core, never the reverse. This is the plane boundary the
    #67 cli split must not blur -- a `from ..porcelain import X` (or a nested
    function-body import of it) anywhere under core/ would let engine/model code
    reach up into the CLI front-end. AST-walked so a lazy in-function import is
    caught too."""
    offenders = []
    for f in _core_files():
        tree = ast.parse(f.read_text(), filename=str(f))
        for mod, names, node in _imports(tree):
            m = mod or ""
            if "porcelain" in m.split(".") or "porcelain" in names:
                rel = f.relative_to(CORE_DIR)
                offenders.append(f"{rel}:{node.lineno} imports porcelain: {m}")
    assert not offenders, (
        "core/ modules must not import porcelain (porcelain imports core, never "
        "the reverse):\n" + "\n".join(offenders))


# ---- intra-engine boundary (#68): startup is the sole sequencer --------------
#
# The #68 split made `startup` the ONLY cross-module sequencer: it imports
# `containers`/`setup`/`sessions` and wires them together. Those three must NOT
# top-level-import `startup` back (that would make the sequencing bidirectional
# and risk an import cycle). `sessions` genuinely needs the start entry point
# (`enter`/`exec` start the workspace), so it imports `startup` LAZILY inside the
# functions that need it -- which is why this rule checks TOP-LEVEL imports only.


def _toplevel_imports(tree: ast.Module):
    """Yield (module_str_or_None, imported_names, node) for imports executed at
    MODULE LOAD -- i.e. anywhere NOT inside a function body. This includes an
    import nested in a module-level `try`/`if`/`with`/`for`/class body (all run at
    import time), but NOT a lazy `def foo(): import ...` (which is allowed --
    sessions uses one for the start entry point). Descends into every node except
    Function/AsyncFunction bodies."""
    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # a function body's imports are lazy, not load-time
            if isinstance(child, ast.Import):
                for alias in child.names:
                    yield alias.name, [], child
            elif isinstance(child, ast.ImportFrom):
                prefix = "." * child.level
                mod = (prefix + (child.module or "")) if child.level \
                    else (child.module or "")
                yield mod, [a.name for a in child.names], child
            else:
                yield from walk(child)
    yield from walk(tree)


def test_engine_leaves_never_toplevel_import_startup():
    """`containers`/`setup`/`sessions` must not TOP-LEVEL-import `startup` -- the
    one-directional sequencing boundary (#68). A lazy in-function import (which
    sessions uses for the start entry point) is allowed and not flagged."""
    offenders = []
    for stem in ("containers", "setup", "sessions"):
        f = ENGINE_DIR / f"{stem}.py"
        tree = ast.parse(f.read_text(), filename=str(f))
        for mod, names, node in _toplevel_imports(tree):
            stripped = (mod or "").lstrip(".")
            parts = stripped.split(".") if stripped else []
            # `from .startup import X` / `from ..engine.startup import X` /
            # `import credproxy_cli.core.engine.startup`
            hit = parts and parts[-1] == "startup"
            # `from . import startup` / `from ..engine import startup`
            hit = hit or "startup" in names
            if hit:
                offenders.append(f"{f.name}:{node.lineno} top-level imports startup")
    assert not offenders, (
        "engine leaves (containers/setup/sessions) must not top-level-import "
        "startup -- startup is the sole cross-module sequencer; use a lazy "
        "in-function import if the start entry point is genuinely needed:\n"
        + "\n".join(offenders))
