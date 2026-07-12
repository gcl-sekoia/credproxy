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


def summarize_wire(bindings, rules) -> dict:
    """The SANITIZED wire summary the proxy reports at GET /admin/config, derived
    from the resolved model -- the CLI half of the field contract with
    proxy/config.sanitized_live_config (a separate deploy unit; wire-parity tested).

    Deliberately tighter than the full push body / /setup: it carries NO secret
    value, NO `params`, NO header/body value -- only the fields both sides can
    compare for live drift:
      - bindings: name, hosts, scheme, placeholder, EFFECTIVE env
      - rules:    name, hosts, action, effective visible

    `env` and `visible` are resolved through the SAME `effective_env`/
    `effective_visible` the push path uses, so the CLI's projection of a config and
    the proxy's projection of that same pushed config are byte-equal (the parity
    test asserts it). Secret resolution is NOT triggered -- this reads injector
    metadata only."""
    from .bindings import effective_env
    from .injectors import find_injector

    binding_summaries = []
    for b in bindings:
        injector = find_injector(b.injector)
        binding_summaries.append({
            "name": b.name,
            "hosts": list(b.hosts),
            "scheme": injector.scheme,
            "placeholder": b.placeholder,
            "env": effective_env(b, injector),
        })
    rule_summaries = [
        {"name": r.name, "hosts": list(r.hosts), "action": r.action,
         "visible": r.effective_visible}
        for r in rules
    ]
    return {"bindings": binding_summaries, "rules": rule_summaries}
