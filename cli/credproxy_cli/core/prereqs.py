"""Evaluate a preset's declarative `[[requires]]` host-prerequisites (#58).

A pack DECLARES host-side prerequisites its container half can't provide -- a
signing-key socket dir, the `gh` CLI on PATH, an env var, a provider that can
serve the secret. This module implements the four fixed check KINDS, host-side
and read-only. **A pack never supplies shell**: the check kinds are closed and
implemented here, so cloning a fork's overlay can't run its code at check time.
The single sanctioned host-executable seam stays the provider protocol, reached
only through the existing `bindings.test_binding` path for the `provider` kind.

Checks are advisory at stamp time (`preset add`/`create` report failures but
still stamp -- the config is durable, host state is fixable afterward) and
authoritative at `doctor` time. `doctor` re-runs them, discovering which packs a
workspace uses via the provenance markers (`preset_stamp.applied_preset_names`).

The `provider` kind checks the provider actually CHOSEN at stamp time (not a
pack default), resolved by the caller from the stamped bindings; `fetch = true`
additionally test-fetches the secret. That fetch is gated by `do_fetch` -- run
at stamp time (interactive, like `binding test`) and under `doctor NAME
--fetch`, but never during a nameless `doctor` scan-all.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from .errors import CredproxyError
from .presets import _Require


@dataclass(frozen=True)
class RequireResult:
    """The outcome of one `[[requires]]` check. `detail` is a short human phrase
    (what was/wasn't found); `hint` is the pack's remedy, carried through on
    failure so the porcelain / doctor layer can surface it."""
    kind: str
    ok: bool
    detail: str
    hint: str | None


def summary(r: RequireResult) -> dict:
    """JSON-clean `{kind, ok, detail, hint}` for the `--json` `requires` array."""
    return {"kind": r.kind, "ok": r.ok, "detail": r.detail, "hint": r.hint}


def evaluate(requires, *, provider: str | None, secret: str | None,
             do_fetch: bool) -> list[RequireResult]:
    """Run every check in `requires`, in declared order, returning one
    `RequireResult` each. Never raises -- a provider/fetch error is captured into
    its result (advisory callers must not be broken by a bad host state).

    `provider`/`secret` are the pack's resolved credential (for the `provider`
    kind); `do_fetch` gates whether a `fetch = true` provider check actually
    test-fetches the secret (else it degrades to a resolve-only provider check).
    """
    out: list[RequireResult] = []
    for rq in requires:
        if rq.kind == "path":
            out.append(_check_path(rq))
        elif rq.kind == "command":
            out.append(_check_command(rq))
        elif rq.kind == "env":
            out.append(_check_env(rq))
        elif rq.kind == "provider":
            out.append(_check_provider(rq, provider, secret, do_fetch))
    return out


def _check_path(rq: _Require) -> RequireResult:
    """Host path must exist (`~` and `$VAR` expanded). Looked up only -- never
    read or executed."""
    raw = rq.path or ""
    expanded = os.path.expanduser(os.path.expandvars(raw))
    if os.path.exists(expanded):
        return RequireResult("path", True, f"{expanded} exists", rq.hint)
    return RequireResult("path", False, f"{expanded} does not exist", rq.hint)


def _check_command(rq: _Require) -> RequireResult:
    """Command must be found on the host PATH (`shutil.which` -- looked up, NEVER
    run)."""
    cmd = rq.command or ""
    found = shutil.which(cmd)
    if found:
        return RequireResult("command", True, f"{cmd} found ({found})", rq.hint)
    return RequireResult("command", False, f"{cmd} not found on PATH", rq.hint)


def _check_env(rq: _Require) -> RequireResult:
    """Env var must be set and non-empty in the host environment."""
    var = rq.var or ""
    val = os.environ.get(var)
    if val:
        return RequireResult("env", True, f"{var} is set", rq.hint)
    return RequireResult("env", False, f"{var} is unset or empty", rq.hint)


def _check_provider(rq: _Require, provider: str | None, secret: str | None,
                    do_fetch: bool) -> RequireResult:
    """The chosen provider must resolve; with `fetch = true` (and `do_fetch`) it
    must also serve the secret. Goes through the existing provider protocol only
    (`find_provider` + `bindings.test_binding`) -- the reported length never
    reveals the value."""
    from .bindings import Binding, test_binding
    from .providers import find_provider

    if not provider:
        return RequireResult(
            "provider", False,
            "provider for this pack could not be determined "
            "(stamped binding missing?)", rq.hint)
    try:
        find_provider(provider)
    except CredproxyError as e:
        return RequireResult("provider", False,
                             f"provider '{provider}' does not resolve: {e}",
                             rq.hint)

    if not (rq.fetch and do_fetch):
        note = "" if not rq.fetch else " (fetch skipped; run `doctor NAME --fetch`)"
        return RequireResult("provider", True,
                             f"provider '{provider}' resolves{note}", rq.hint)

    if not secret:
        return RequireResult(
            "provider", False,
            f"provider '{provider}' resolves but no secret ref is available to "
            "test-fetch", rq.hint)
    probe = Binding(name=f"requires:{provider}", injector="", provider=provider,
                    secret=secret, hosts=(), placeholder=None, env=None)
    r = test_binding(probe)
    if r.ok:
        return RequireResult(
            "provider", True,
            f"provider '{provider}' fetched the secret ({r.value_len} chars)",
            rq.hint)
    return RequireResult("provider", False,
                         f"provider '{provider}' fetch failed: {r.error}",
                         rq.hint)
