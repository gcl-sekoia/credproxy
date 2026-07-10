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

MODEL_DIR = Path(__file__).resolve().parents[2] / "cli" / "credproxy_cli" / "core" / "model"

# Bare engine module names (as they'd appear in `from ..engine import X` or a
# stray `from ..docker import ...`). The model plane must not import any of them.
ENGINE_MODULES = {
    "docker", "imageenv", "runtime", "compose", "lifecycle", "push",
    "proxy_http", "doctor", "scriptcheck",
}


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
