"""The config-push engine: the ONE resolve+POST path shared by `start`/`apply`
(via proxy_http.push_config), the `push` verb (managed + attached workspaces),
and the stateless `credproxy push --admin/--config/--token` escape hatch.

Responsibilities, all target-agnostic:
  - build the proxy wire body (resolve every binding's secret + fold in rules +
    the fingerprint) -- the single place the POST shape is assembled (G3);
  - POST it to an arbitrary `<admin_url>/admin/config`, bearer-authed;
  - resolve an attached workspace's `attach` selector to a loopback admin URL
    (container / discover / admin_url), port resolved per call (I6);
  - enforce the loopback-only invariant on every admin URL (I8) in one place;
  - poll `/health` for `--wait` (NEVER `/ready` -- that gates on the very push
    this precedes, so waiting on it would deadlock, I1);
  - hold a blocking flock around a resolve+POST so a concurrent invocation waits
    then re-pushes rather than racing (never skips).

The real secret is resolved here and POSTed over plain HTTP, which is why every
admin URL MUST be loopback -- see require_loopback.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import docker
from .errors import ConfigError, ProxyError
from .paths import state_dir

Notify = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


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


# ---- attached-target discovery ----------------------------------------------


def resolve_admin_url(attach: dict, notify: Notify = _noop) -> str:
    """Resolve a normalized `attach` selector (see config._parse_attach) to a
    loopback admin base URL, resolving any published port live (I6):

      - {"admin_url": U}   -> U verbatim (loopback-checked).
      - {"container": X}   -> `docker port X <http_port>` -> http://127.0.0.1:P.
      - {"discover": SPEC} -> `docker ps --filter label=k=v ...` -> the single
                              match's name -> the same port derivation. Zero or
                              more-than-one match is an error (never a pick).

    The container/discover port comes from the proxy image's CREDPROXY_HTTP_PORT
    (via ImageEnv), matching how the managed path derives its port."""
    if "admin_url" in attach:
        url = normalize_admin_url(attach["admin_url"])
        require_loopback(url)
        return url

    from .imageenv import ImageEnv
    meta = ImageEnv.load()
    if "container" in attach:
        container = attach["container"]
    else:
        container = _discover_container(attach["discover"], notify)
    port = docker.resolve_host_port(container, meta.http_port)
    url = f"http://127.0.0.1:{port}"
    require_loopback(url)   # belt-and-suspenders; it is loopback by construction
    return url


def _discover_container(spec: str, notify: Notify) -> str:
    """`discover = "k=v,k=v"` -> the single container matching every label
    (`docker ps --filter label=k=v` ANDs the filters). No match / >1 match is an
    error (ambiguity is never silently resolved)."""
    pairs = _parse_discover(spec)
    args = ["ps", "--format", "{{.Names}}"]
    for key, val in pairs:
        args += ["--filter", f"label={key}={val}"]
    out = docker.docker_output(args)
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not names:
        raise ConfigError(
            f"attach discover {spec!r} matched no running container")
    if len(names) > 1:
        raise ConfigError(
            f"attach discover {spec!r} is ambiguous -- matched "
            f"{len(names)} containers: {', '.join(sorted(names))}")
    return names[0]


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


# ---- the wire body (the single assembly point, G3) --------------------------


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


def push_to_target(admin_url: str, token: str, bindings, rules,
                   fingerprint: str | None = None,
                   notify: Notify = _noop) -> tuple:
    """Resolve secrets + POST the wire body to `<admin_url>/admin/config`, bearer
    token. The shared engine every push path funnels through. Returns
    `(bindings, rules)` so the caller can record applied-state. Raises ProxyError
    on connect/401/non-200. The URL must already be loopback-validated."""
    wire = build_wire(bindings, rules, fingerprint)
    body = json.dumps(wire).encode()
    status, payload = _http_post_json(f"{admin_url}/admin/config", body, token)
    if status == 200:
        return bindings, rules
    if status == 401:
        raise ProxyError(
            f"proxy at {admin_url} rejected the token (HTTP 401)")
    raise ProxyError(
        f"config push to {admin_url} failed: HTTP {status}: "
        f"{payload.get('error', payload)}")


def rule_test(admin_url: str, token: str, method: str, url: str) -> dict:
    """POST /admin/rule-test to an arbitrary loopback admin URL (the target-
    agnostic form of proxy_http.rule_test_live). Raises ProxyError on failure."""
    status, payload = _http_post_json(
        f"{admin_url}/admin/rule-test",
        json.dumps({"method": method, "url": url}).encode(), token)
    if status == 200:
        return payload
    if status == 401:
        raise ProxyError(f"proxy at {admin_url} rejected the token (HTTP 401)")
    raise ProxyError(
        f"proxy rule-test failed (HTTP {status}): {payload.get('error', payload)}")


# ---- /health wait (I1: NEVER /ready) ----------------------------------------


def wait_for_health(admin_url: str, timeout: float = 120.0,
                    notify: Notify = _noop) -> None:
    """Poll `<admin_url>/health` until capture-ready (200) or `timeout` elapses,
    ~0.5s between polls. We wait on `/health`, NOT `/ready`: `/ready` gates on a
    config having been pushed -- the very push this precedes -- so waiting on it
    would deadlock (I1). Raises ProxyError on timeout, naming the last pending
    reason the 503 body carried."""
    deadline = time.monotonic() + timeout
    last_pending: list | None = None
    last_err: Exception | None = None
    notify(f"waiting for {admin_url}/health ...")
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{admin_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 503:
                try:
                    last_pending = json.loads(e.read()).get("pending")
                except (ValueError, OSError):
                    pass
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
        time.sleep(0.5)
    detail = (f"still waiting on: {', '.join(last_pending)}"
              if last_pending else str(last_err))
    raise ProxyError(
        f"proxy at {admin_url} did not become capture-ready within "
        f"{timeout:.0f}s ({detail})")


# ---- HTTP primitive ----------------------------------------------------------


def _http_post_json(url: str, body: bytes, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw}
    except urllib.error.URLError as e:
        raise ProxyError(f"connect error talking to the proxy at {url}: {e.reason}")


# ---- locks (blocking: wait-then-repush) -------------------------------------


try:
    import fcntl  # POSIX-only
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None


@contextmanager
def workspace_push_lock(ws):
    """Blocking flock on `<state>/push.lock`, held around a workspace's
    resolve+POST. A second concurrent `push` of the same workspace WAITS for the
    holder then re-pushes (config may have changed) -- never skips."""
    ws.ensure_state_dir()
    yield from _flock(ws.state_dir / "push.lock")


@contextmanager
def target_push_lock(admin_url: str):
    """Blocking flock keyed by sha256(admin_url) under
    `$XDG_STATE_HOME/credproxy/locks/`, for the stateless push (no workspace
    state dir). Same wait-then-repush semantics as workspace_push_lock."""
    locks = state_dir() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(admin_url.encode()).hexdigest()
    yield from _flock(locks / f"{digest}.lock")


def _flock(path: Path):
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---- stateless config subset ------------------------------------------------


def load_stateless_config(path: str) -> tuple[list, list]:
    """Parse + validate a stateless push `--config` file: a workspace-TOML SUBSET
    of ONLY `[[binding]]` + `[[rule]]`. Any container-lifecycle or `attach` key is
    rejected naming it -- the file carries credentials-to-push, not a container
    spec. Reuses the same binding/rule validators the workspace path uses (G3), so
    a stateless push and a managed one agree on what a valid config is. Returns
    `(bindings, rules)` with names/placeholders filled in-memory (never written).
    """
    import tomllib

    from .bindings import bindings_from_raw
    from .rules import named_rules_from_raw, validate as validate_rules

    p = Path(path)
    if not p.exists():
        raise ConfigError(f"--config file not found: {path}")
    try:
        raw = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: TOML parse error: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a table")

    extra = sorted(set(raw) - {"binding", "rule"})
    if extra:
        raise ConfigError(
            f"{path}: a stateless --config carries only [[binding]] and [[rule]] "
            f"(not a container spec); remove: {', '.join(extra)}")

    bindings = bindings_from_raw(raw, path, fill_placeholders=True)
    rules = named_rules_from_raw(raw, path)
    validate_rules(rules, path)
    return bindings, rules
