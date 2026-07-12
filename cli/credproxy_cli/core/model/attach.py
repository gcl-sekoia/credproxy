"""Pure attach-selector + admin-URL validation helpers (model plane).

Extracted from the push engine so the config loader (model) can validate an
`attach` table without importing the engine's `push` module (a model->engine
inversion). These are pure: URL/selector parsing and the loopback invariant,
stdlib + ConfigError only, no docker, no subprocess, no I/O.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from ..errors import ConfigError


# ---- loopback invariant (I8) -------------------------------------------------


def require_loopback(url: str) -> None:
    """Refuse any admin URL whose host is not loopback. The push wire carries
    RESOLVED secret values over plain HTTP (no TLS on the admin API), so it is
    only safe when the proxy is reachable on 127.0.0.0/8 (or `localhost`) --
    i.e. the same host, via a published ephemeral port or the shared netns. This
    is the ONE enforcement point for both `attach.admin_url` (checked at config
    load) and `credproxy push --admin` (checked at dispatch)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(
            f"admin URL {url!r} must be an http(s) URL")
    host = parts.hostname
    if host is None:
        raise ConfigError(f"admin URL {url!r} has no host")
    if host == "localhost":
        return
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None or not ip.is_loopback:
        raise ConfigError(
            f"admin URL {url!r} is not loopback: the push wire carries resolved "
            f"secret values over plain HTTP, so it is only safe to a proxy on "
            f"127.0.0.0/8 or localhost (the same host, via its published port or "
            f"the shared netns)")


def normalize_admin_url(url: str) -> str:
    """Strip a trailing slash so `f'{admin_url}/admin/config'` is well-formed."""
    return url.rstrip("/")


# ---- discover-selector parsing ----------------------------------------------


def parse_discover(spec: str) -> list[tuple[str, str]]:
    """Public: parse + validate a `discover` spec into (key, value) pairs.
    Comma-separated `key=value`, both non-empty. Raises ConfigError."""
    return _parse_discover(spec)


def _parse_discover(spec: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in spec.split(","):
        key, sep, val = part.partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not key or not val:
            raise ConfigError(
                f"attach discover {spec!r} must be comma-separated key=value "
                f"pairs (both non-empty), got segment {part!r}")
        pairs.append((key, val))
    if not pairs:
        raise ConfigError(f"attach discover {spec!r} is empty")
    return pairs
