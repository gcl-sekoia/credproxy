"""Proxy configuration: intercept set + per-host header injection.

The on-disk format is a single YAML file (CONFIG_PATH). Callers go
through the Credentials interface, never the file directly — so a future
host-plugin IPC implementation can swap in without touching the inject
path.

Schema:

    hosts:
      api.github.com:
        inject:
          Authorization: "Bearer ghp_..."
          X-Custom: "value"

A host listed under `hosts:` is intercepted (TLS terminated). Empty
`inject:` is allowed — intercept and log, no header injection.
"""
from pathlib import Path
from typing import Protocol

import yaml

CONFIG_PATH = Path("/opt/proxy/config.yaml")


class Credentials(Protocol):
    def intercept_hosts(self) -> set[str]: ...
    def headers_for(self, host: str) -> dict[str, str]: ...


class YamlCredentials:
    def __init__(self, hosts: dict[str, dict[str, str]]):
        self._hosts = hosts

    def intercept_hosts(self) -> set[str]:
        return set(self._hosts)

    def headers_for(self, host: str) -> dict[str, str]:
        return dict(self._hosts.get(host, {}))


def load(path: Path = CONFIG_PATH) -> YamlCredentials:
    if not path.exists():
        raise SystemExit(f"[config] missing config file: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"[config] malformed YAML in {path}: {e}")

    if not isinstance(raw, dict) or "hosts" not in raw:
        raise SystemExit(f"[config] {path}: missing top-level `hosts:` key")
    hosts_raw = raw["hosts"] or {}
    if not isinstance(hosts_raw, dict):
        raise SystemExit(f"[config] {path}: `hosts:` must be a mapping")

    hosts: dict[str, dict[str, str]] = {}
    for host, entry in hosts_raw.items():
        entry = entry or {}
        if not isinstance(entry, dict):
            raise SystemExit(f"[config] {path}: hosts.{host} must be a mapping")
        inject = entry.get("inject") or {}
        if not isinstance(inject, dict):
            raise SystemExit(
                f"[config] {path}: hosts.{host}.inject must be a mapping"
            )
        for header, value in inject.items():
            if not isinstance(value, str):
                raise SystemExit(
                    f"[config] {path}: hosts.{host}.inject.{header} "
                    f"must be a string, got {type(value).__name__}"
                )
        hosts[host] = dict(inject)
    return YamlCredentials(hosts)
