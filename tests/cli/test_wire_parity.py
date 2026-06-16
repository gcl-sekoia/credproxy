"""Parity: every wire config the CLI's wire_config() emits must be accepted by
the proxy's load_resolved(). The CLI and proxy are separate deploy units (the
CLI can't import the proxy), so the wire contract can drift silently -- this
feeds REAL CLI output into the REAL proxy validator, per builtin injector.

proxy/config.py + schemes.py import on the host (no mitmproxy/aiohttp dep), the
same way tests/cli/test_scheme_catalog_drift.py reaches the proxy catalog.
Script schemes need the Starlark runtime (proxy image only), so they're covered
by the in-image tests/test_scripted_config.py and skipped here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _proxy_config():
    proxy_dir = str(Path(__file__).resolve().parents[2] / "proxy")
    if proxy_dir not in sys.path:
        sys.path.insert(0, proxy_dir)
    import config as proxy_config
    return proxy_config


def _minimal_binding(inj):
    """A minimal valid Binding using injector `inj`: one host, every slot filled
    with a ref, a placeholder for substitute schemes."""
    from credproxy_cli.core.bindings import Binding
    slots = inj.spec.slots
    secret = "ref" if (len(slots) == 1 and slots[0] == "value") \
        else {s: f"ref-{s}" for s in slots}
    placeholder = inj.placeholder.generate() if inj.spec.uses_placeholder else None
    return Binding(name=f"{inj.name}-b", injector=inj.name, provider="env",
                   secret=secret, hosts=("api.example.com",),
                   placeholder=placeholder, env=None)


def test_wire_config_round_trips_through_proxy(xdg):
    """For every builtin built-in injector, CLI wire_config -> proxy
    load_resolved with no error (catches wire-contract drift between units)."""
    from credproxy_cli.core.bindings import wire_config
    from credproxy_cli.core.injectors import list_injectors
    proxy_config = _proxy_config()

    def fake_fetch(provider, refs):
        return {r: f"val-{r}" for r in refs}

    builtins = [d for d in list_injectors() if d.scheme != "script"]
    # Sanity: the families we expect are present (so this isn't vacuously empty).
    assert {d.name for d in builtins} >= {
        "bearer", "basic", "body", "sigv4", "oauth2-reseal"}
    for inj in builtins:
        wire = wire_config([_minimal_binding(inj)], fetch_many=fake_fetch)
        try:
            proxy_config.load_resolved(wire)  # raises ConfigError on drift
        except Exception as e:  # noqa: BLE001 - surface which injector drifted
            pytest.fail(f"injector {inj.name!r} ({inj.scheme}) wire config rejected "
                        f"by proxy load_resolved: {type(e).__name__}: {e}")


def test_proxy_validator_is_not_a_noop(xdg):
    """The parity assertion is only meaningful if load_resolved actually rejects
    a malformed wire config."""
    proxy_config = _proxy_config()
    with pytest.raises(Exception):
        proxy_config.load_resolved({"bindings": [{"name": "x"}]})  # missing fields
