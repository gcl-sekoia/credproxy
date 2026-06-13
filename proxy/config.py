"""Proxy configuration: intercept set + per-host placeholder substitution.

The proxy receives an already-resolved config (literal `real:` values,
no template references) via POST /admin/config. The host CLI
`bin/credproxy config` is the supported producer; it fetches each
binding's secret from its provider before posting.

This module validates the parsed dict and produces a BindingCredentials
instance. Schema:

    {
      "bindings": [
        {
          "name":        "github-env",          # non-empty, unique across bindings
          "hosts":       ["api.github.com"],     # non-empty list of strings
          "header":      "Authorization",        # non-empty string
          "placeholder": "ghp_xxx...",           # non-empty string; no ${secret:...}
          "real":        "<resolved secret>",    # non-empty string; no ${secret:...}
          "env":         "GITHUB_TOKEN"          # optional; suggested env var or null/absent
        }
      ]
    }

Uniqueness constraints:
  - `name` is unique across bindings (non-empty string).
  - `(host, header)` pair is unique across all bindings.

Credentials class API:
  - `intercept_hosts()` -> set[str]: union of all bindings' hosts.
  - `substitutions_for(host)` -> list[Substitution]: substitutions for that host.

Inward API / least-disclosure: `inward_bindings()` returns the
workspace-facing binding metadata (name, placeholder, env, header,
hosts) with `real` excluded. This is the source for /setup.
"""
import re
from dataclasses import dataclass
from typing import Any, Protocol

_SECRET_REF = re.compile(r"\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Substitution:
    header: str
    placeholder: str
    real: str


@dataclass(frozen=True)
class InwardBinding:
    """Workspace-safe binding descriptor: no real credential, no provider/secret-id."""
    name: str
    placeholder: str
    env: str | None
    header: str
    hosts: list[str]


class Credentials(Protocol):
    def intercept_hosts(self) -> set[str]: ...
    def substitutions_for(self, host: str) -> list[Substitution]: ...
    def inward_bindings(self) -> list[InwardBinding]: ...


class BindingCredentials:
    """Credentials built from the bindings wire format."""

    def __init__(
        self,
        hosts: dict[str, list[Substitution]],
        bindings: list[InwardBinding] | None = None,
    ):
        self._hosts = hosts
        self._bindings: list[InwardBinding] = bindings or []

    def intercept_hosts(self) -> set[str]:
        return set(self._hosts)

    def substitutions_for(self, host: str) -> list[Substitution]:
        return list(self._hosts.get(host, []))

    def inward_bindings(self) -> list[InwardBinding]:
        return list(self._bindings)


class ConfigError(Exception):
    """Raised on validation failure. Callers decide how to handle:
    main.py SystemExits at startup; the admin endpoint returns 400."""


def _fail(msg: str) -> None:
    raise ConfigError(f"[config] {msg}")


def load_resolved(raw: Any, source: str = "<resolved>") -> BindingCredentials:
    """Build credentials from a parsed dict (already-resolved values).

    `raw` must conform to the bindings schema documented at the top of
    this module. Any remaining `${...}` template-looking text in `real`
    or `placeholder` causes a validation error -- secret resolution is
    the caller's responsibility.
    """
    if not isinstance(raw, dict) or "bindings" not in raw:
        _fail(f"{source}: missing top-level `bindings:` key")

    bindings_raw = raw["bindings"]
    if not isinstance(bindings_raw, list):
        _fail(f"{source}: `bindings` must be an array")

    names_seen: set[str] = set()
    host_header_seen: dict[tuple[str, str], str] = {}  # (host, header) -> binding name
    hosts: dict[str, list[Substitution]] = {}
    inward: list[InwardBinding] = []

    for i, entry in enumerate(bindings_raw):
        where = f"bindings[{i}]"
        if not isinstance(entry, dict):
            _fail(f"{source}: {where} must be an object")

        # --- name ---
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            _fail(f"{source}: {where}.name must be a non-empty string")
        if name in names_seen:
            _fail(f"{source}: duplicate binding name '{name}'")
        names_seen.add(name)

        # --- hosts ---
        binding_hosts = entry.get("hosts")
        if not isinstance(binding_hosts, list) or not binding_hosts \
                or not all(isinstance(h, str) and h for h in binding_hosts):
            _fail(f"{source}: {where}.hosts must be a non-empty array of strings")

        # --- header ---
        header = entry.get("header")
        if not isinstance(header, str) or not header:
            _fail(f"{source}: {where}.header must be a non-empty string")

        # --- placeholder ---
        placeholder = entry.get("placeholder")
        if not isinstance(placeholder, str) or not placeholder:
            _fail(f"{source}: {where}.placeholder must be a non-empty string")
        unresolved_ph = _SECRET_REF.search(placeholder)
        if unresolved_ph:
            _fail(
                f"{source}: {where}.placeholder contains "
                f"unresolved ${{secret:{unresolved_ph.group(1)}}} -- "
                f"the caller is expected to resolve before posting"
            )

        # --- real ---
        real = entry.get("real")
        if not isinstance(real, str) or not real:
            _fail(f"{source}: {where}.real must be a non-empty string")
        unresolved_real = _SECRET_REF.search(real)
        if unresolved_real:
            _fail(
                f"{source}: {where}.real contains "
                f"unresolved ${{secret:{unresolved_real.group(1)}}} -- "
                f"the caller is expected to resolve before posting"
            )

        # --- env (optional) ---
        env = entry.get("env")
        if env is not None and (not isinstance(env, str) or not env):
            _fail(f"{source}: {where}.env must be a non-empty string or absent/null")

        # --- (host, header) uniqueness across bindings ---
        for host in binding_hosts:
            key = (host, header)
            if key in host_header_seen:
                _fail(
                    f"{source}: bindings '{host_header_seen[key]}' and '{name}' "
                    f"both claim header '{header}' on host '{host}'"
                )
            host_header_seen[key] = name

        sub = Substitution(header=header, placeholder=placeholder, real=real)
        for host in binding_hosts:
            hosts.setdefault(host, []).append(sub)

        inward.append(InwardBinding(
            name=name,
            placeholder=placeholder,
            env=env,
            header=header,
            hosts=list(binding_hosts),
        ))

    return BindingCredentials(hosts, inward)
