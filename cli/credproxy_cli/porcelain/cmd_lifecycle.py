"""The container-lifecycle verbs: enter/exec/start/stop/recreate/apply/push/
resolve/logs, plus the stateless top-level `push`. These drive the engine-plane
`containers`/`sessions`/`startup`/`push` modules; the log formatter turns the
proxy's structured record stream into human lines."""
from __future__ import annotations

import os
import sys

from ..core.engine import docker as core_docker
from ..core.engine import containers, sessions, startup
from ..core.errors import DependencyError
from ..core.model.workspace import Workspace
from . import render
from .render import fail, say
from .common import (
    Ctx, _resolve_ws, _require_exists, _reject_if_attached, ensure_proxy_image,
    _confirm_destructive, _LeafParser,
)


def do_enter(ctx: Ctx, name: str | None, trailing: list[str],
             user_override: str | None = None, push: bool = False) -> None:
    if ctx.json:
        fail("enter does not support --json (it execs an interactive shell)")
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "enter")
    # Empty trailing -> the core runs the config `shell` (default: a login
    # shell); an explicit `-- CMD` runs bare. Resolved in _enter_exec_cmd, which
    # has the loaded config.
    exit_code = sessions.enter_workspace(
        ws, trailing, notify=say, user_override=user_override, push=push)
    sys.exit(exit_code)


def do_exec(ctx: Ctx, name: str | None, trailing: list[str], *,
            login: bool = False, raw: bool = False, push: bool = False,
            user: str | None = None) -> None:
    """One-shot: run `-- CMD...` in the workspace and propagate its exit code.
    The non-interactive sibling of `enter` -- never initiates an auto-stop, so
    it's safe to fire many times from a script.

    Environment: default sources the CA-trust env (like `enter -- CMD`); `--raw`
    is a direct execve (no shell, for minimal images); `--login` a bash login
    shell. `--user` overrides the config user for this call."""
    if not trailing:
        fail("`exec` needs a command: `credproxy workspace NAME exec -- CMD...` "
             "(for an interactive shell use `enter`)")
    if login and raw:
        fail("`--login` and `--raw` are mutually exclusive (they select different "
             "command environments)")
    # `exec` is a transparent pipe: the command's own stdout is arbitrary bytes,
    # not credproxy's to structure, so --json has nothing to wrap. Reject it
    # rather than emit non-JSON on a --json invocation (which a jq pipeline would
    # choke on); the exit code is already the process's exit code.
    if ctx.json:
        fail("`exec` streams the command's output verbatim; `--json` does not "
             "apply (the exit code is the command's exit code)")
    mode = "login" if login else "raw" if raw else "shim"
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "exec")
    exit_code = sessions.exec_workspace(
        ws, trailing, notify=say, mode=mode, user_override=user, push=push)
    sys.exit(exit_code)


def do_start(ctx: Ctx, name: str | None) -> None:
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "start")
    ensure_proxy_image(ctx)
    startup.start_workspace(ws, notify=say)
    render.OUT.started(ws.name)


def do_stop(ctx: Ctx, name: str | None) -> None:
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _reject_if_attached(ws, "stop")
    containers.stop_workspace(ws)
    render.OUT.stopped(ws.name)


def do_apply(ctx: Ctx, name: str | None) -> None:
    from ..core.model import config as core_config

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    # An attached workspace has no container spec to reconcile -- `apply` IS a
    # push (resolve secrets + POST the full wire config to its external proxy).
    if core_config.quick_attach(ws):
        admin_url = startup.push_workspace(ws, notify=say)
        render.OUT.pushed(ws.name, admin_url, attached=True, as_apply=True)
        return
    result = startup.apply_config(ws, notify=say)
    render.OUT.applied(ws.name, result)


def do_push(ctx: Ctx, name: str | None, wait: bool, timeout: float) -> None:
    """`push`: resolve secrets and POST the full wire config (bindings + rules) to
    the workspace's proxy -- managed (its published port) or attached (the
    `attach` target). `--wait` polls /health first."""
    from ..core.model import config as core_config

    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    attached = core_config.quick_attach(ws)
    admin_url = startup.push_workspace(ws, notify=say, wait=wait, timeout=timeout)
    render.OUT.pushed(ws.name, admin_url, attached=attached)


def do_resolve(ctx: Ctx, name: str | None, out: str | None) -> None:
    """`resolve`: build the full wire config (with resolved secret VALUES) without
    contacting any proxy. Exactly one of `--json` (blob to stdout) or `--out FILE`
    (mode 0600)."""
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    if bool(ctx.json) == bool(out):
        fail("`resolve` needs exactly one of --json (blob to stdout) or "
             "--out FILE (writes mode 0600)")
    wire = startup.resolve_workspace_wire(ws, notify=say)
    if out is not None:
        _write_resolved(ws, wire, out)
        render.OUT.resolved(ws.name, out)
    else:
        # --json: emit the wire blob (real secrets) to stdout.
        import json as _json
        print(_json.dumps(wire))


def _write_resolved(ws: Workspace, wire: dict, out: str) -> None:
    """Write the resolved wire config to `out`, mode 0600 (it carries real secret
    values -- the one at-rest disclosure path). Warn if `out` is outside the
    workspace state dir, where it could be committed to a repo."""
    import json as _json
    import os as _os

    path = _os.path.abspath(_os.path.expanduser(out))
    state = _os.path.abspath(str(ws.state_dir))
    if not (path == state or path.startswith(state + _os.sep)):
        say(f"warning: {out} is outside the workspace state dir -- it holds "
            f"RESOLVED secret values (mode 0600); do not commit it to a repo")
    fd = _os.open(path, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
    with _os.fdopen(fd, "w") as f:
        f.write(_json.dumps(wire) + "\n")


def do_recreate(ctx: Ctx, name: str | None, include_proxy: bool,
                reset_volumes: list[str]) -> None:
    implicit = name is None
    ws = _resolve_ws(ctx, name)
    _require_exists(ws)
    _reject_if_attached(ws, "recreate")
    # Plain recreate keeps all persistent state, so it isn't gated. --reset-volume
    # wipes a volume's data (the one recreate mode that destroys data), so it is
    # gated like delete: confirm on an implicit default workspace (loose surface).
    if reset_volumes:
        _confirm_destructive(ctx, ws, implicit, "reset volume(s) of")
    ensure_proxy_image(ctx)
    startup.recreate_workspace(ws, notify=say, include_proxy=include_proxy,
                                 reset_volumes=reset_volumes)
    render.OUT.recreated(ws.name, include_proxy, reset_volumes)


def do_logs(ctx: Ctx, name: str | None, audit: bool = False) -> None:
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "logs")
    _logs_stream(ws, as_json=ctx.json, audit_only=audit)


# The proxy prefixes every structured record with this (see proxy/log.py).
_LOG_PREFIX = "credproxy "


def _parse_credproxy_line(line: str) -> dict | None:
    """Parse one `docker logs` line into a proxy structured record, or None if it
    isn't one. Requires the `credproxy ` prefix at the START of the line (not
    anywhere in it) plus a JSON object carrying a `kind`. Because the proxy
    JSON-encodes every untrusted value (a rule/scheme error message that can echo
    workspace input), such content is escaped inside the record and can NEVER
    spill a forged `credproxy {...}` line of its own -- the substring-forgery the
    old text stream allowed is structurally impossible."""
    import json
    if not line.startswith(_LOG_PREFIX):
        return None
    try:
        rec = json.loads(line[len(_LOG_PREFIX):])
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) and "kind" in rec else None


def _logs_stream(ws: Workspace, as_json: bool, audit_only: bool) -> None:
    """Tail `docker logs -f` and reformat the proxy's structured `credproxy {json}`
    records; mitmproxy's own termlog passes through verbatim (never mistaken for
    a proxy record). Default: pretty one line per record. `--json`: the raw
    records as JSON-lines (a non-proxy line wraps as `{"kind":"raw","line":...}`).
    `--audit`: only `kind == "audit"` records. docker's log driver is the durable
    store (survives stop/start)."""
    import json
    import subprocess

    try:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", ws.proxy_container],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except FileNotFoundError:
        raise DependencyError(core_docker.DOCKER_MISSING_MSG)
    interrupted = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            rec = _parse_credproxy_line(line)
            if audit_only:
                if rec is not None and rec.get("kind") == "audit":
                    print(json.dumps(rec) if as_json else _format_record(rec),
                          flush=True)
            elif rec is not None:
                print(json.dumps(rec) if as_json else _format_record(rec),
                      flush=True)
            elif as_json:      # non-proxy line (mitmproxy etc.)
                print(json.dumps({"kind": "raw", "line": line.rstrip("\n")}),
                      flush=True)
            else:
                print(line, end="", flush=True)   # pass mitmproxy output through
    except KeyboardInterrupt:
        interrupted = True
    finally:
        proc.terminate()
        rc = proc.wait()
    # A non-zero exit we didn't cause (Ctrl-C) is a real failure -- e.g. the
    # container doesn't exist; propagate it rather than reporting success.
    if not interrupted and rc:
        fail(f"docker logs exited with status {rc}")


def _format_record(rec: dict) -> str:
    """One-line human rendering of a proxy structured record (log.py). Tolerant of
    missing keys and unknown/future kinds."""
    ts = rec.get("ts", "")
    kind = rec.get("kind", "?")
    where = f"{rec.get('method', '')} {rec.get('host', '')}" \
            f"{rec.get('path', '')}".strip()
    if kind == "audit":
        subj = rec.get("binding") or rec.get("rule") or ""
        detail = " ".join(p for p in (subj and f"'{subj}'", rec.get("outcome", ""))
                          if p)
        return f"{ts}  audit {rec.get('event', '?'):<9} {where}  {detail}".rstrip()
    if kind in ("http", "api"):
        marks = rec.get("marks")
        return f"{ts}  {kind:<6} {where}" \
               f"{' (' + ' '.join(marks) + ')' if marks else ''}".rstrip()
    if kind == "sni":
        err = f" -- {rec['error']}" if rec.get("error") else ""
        return f"{ts}  sni    {rec.get('sni') or '<no-sni>'} " \
               f"({rec.get('decision', '?')}){err}"
    if kind == "rule-error":
        return f"{ts}  rule   {rec.get('rule', '')} failed: {rec.get('error', '')}"
    if kind in ("scheme", "script"):
        detail = rec.get("error") or rec.get("reason", "")
        # Sanitized script failures carry a safe source:line location (#33 rung 3).
        if rec.get("line") is not None:
            detail = f"{detail} at {rec.get('source', '?')}:{rec['line']}".strip()
        return f"{ts}  {kind:<6} {rec.get('scheme', '')} " \
               f"{rec.get('phase') or rec.get('hook', '')}: {detail}".rstrip(": ")
    rest = " ".join(f"{k}={v}" for k, v in rec.items() if k not in ("ts", "kind"))
    return f"{ts}  {kind:<6} {rest}".rstrip()


def do_push_stateless(ctx: Ctx, rest: list[str]) -> None:
    """Push a `[[binding]]+[[rule]]` config FILE to an arbitrary loopback proxy
    admin URL, authed with a token FILE -- no workspace, no state. The CI/scripting
    escape hatch."""
    from ..core.engine import push as core_push
    from ..core.model.rules import combined_fingerprint

    p = _LeafParser(prog="credproxy push", add_help=False)
    p.add_argument("--admin", dest="admin", required=True, metavar="URL")
    p.add_argument("--config", dest="config_file", required=True, metavar="FILE")
    p.add_argument("--token", dest="token_file", required=True, metavar="FILE")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=120.0, metavar="SECS")
    a = p.parse_args(rest)

    from ..core.model.attach import normalize_admin_url, require_loopback
    admin_url = normalize_admin_url(a.admin)
    require_loopback(admin_url)                               # I8
    bindings, rules = core_push.load_stateless_config(a.config_file)
    token = _read_token_file(a.token_file)
    if a.wait:
        core_push.wait_for_health(admin_url, a.timeout, say)
    fp = combined_fingerprint(bindings, rules)
    with core_push.target_push_lock(admin_url):
        say("pushing config...")
        core_push.push_to_target(admin_url, token, bindings, rules, fp, notify=say)
    render.OUT.pushed(None, admin_url, attached=None, stateless=True)


def _read_token_file(path: str) -> str:
    from pathlib import Path
    p = Path(os.path.expanduser(path))
    if not p.exists():
        fail(f"--token file not found: {path}")
    token = p.read_text().strip()
    if not token:
        fail(f"--token file is empty: {path}")
    return token
