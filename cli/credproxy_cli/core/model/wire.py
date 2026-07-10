"""The proxy wire-format encoder (model plane).

Owns the single place `{bindings, rules, fingerprint}` is shaped into the POST
body. Extracted from the push engine so the transport layer
(`engine/proxy_http.py`) never imports `bindings`/`rules` -- it accepts an
already-encoded body. The engine's `push.push_to_target` composes this encoder
(model) with the transport POST (engine).
"""
from __future__ import annotations


def build_wire(bindings, rules, fingerprint: str | None = None) -> dict:
    """Assemble the FULL proxy wire body from resolved bindings + rules: this is
    the ONE place `{bindings, rules, fingerprint}` is shaped, so a managed,
    attached, and stateless push all POST byte-identical bodies for the same
    inputs. `wire_config` resolves each binding's secret via its provider."""
    from .bindings import wire_config
    from .rules import rule_wire_entries

    wire = wire_config(bindings)
    wire["rules"] = rule_wire_entries(rules)
    if fingerprint is not None:
        wire["fingerprint"] = fingerprint
    return wire
