"""Interactive/one-shot sessions: enter/exec, the shared docker-exec argv,
session pidfiles + the auto-stop reaper.

`enter` and `exec` build their `docker exec` argv through the ONE
`_docker_exec_argv` helper (so the two verbs can't drift on workdir/user/
exec_flags/session-control ordering). Both start the workspace first via
`startup.start_workspace`, imported LAZILY inside the functions so this module
never top-level-imports `startup` (the intra-engine boundary: `startup` is the
only cross-module sequencer; `containers`/`setup`/`sessions` must not import it).
"""
from __future__ import annotations

import os
import subprocess

from . import containers, docker
from .containers import Notify, _noop
from ..model.config import load_config
from ..model.workspace import Workspace


# ---- auto-stop / session tracking -------------------------------------------


def _session_pidfile(ws: Workspace, pid: int) -> "Path":
    from pathlib import Path
    return ws.sessions_dir / str(pid)


def _clean_stale_sessions(ws: Workspace) -> None:
    """Remove pidfiles for processes that are no longer alive."""
    if not ws.sessions_dir.exists():
        return
    import os
    for pidfile in ws.sessions_dir.iterdir():
        try:
            pid = int(pidfile.name)
        except ValueError:
            pidfile.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, 0)  # liveness check
        except ProcessLookupError:
            pidfile.unlink(missing_ok=True)
        except PermissionError:
            pass  # process exists but owned by another user; leave it


def _count_live_sessions(ws: Workspace, exclude_pid: int | None = None) -> int:
    """Count live (other than exclude_pid) sessions for the workspace."""
    if not ws.sessions_dir.exists():
        return 0
    import os
    count = 0
    for pidfile in ws.sessions_dir.iterdir():
        try:
            pid = int(pidfile.name)
        except ValueError:
            continue
        if pid == exclude_pid:
            continue
        try:
            os.kill(pid, 0)
            count += 1
        except (ProcessLookupError, PermissionError):
            pass
    return count


def _docker_exec_argv(cfg: dict, container: str, cmd_argv: list[str], *,
                      user_override: str | None, isatty: bool) -> list[str]:
    """The `docker exec` argv shared by `enter` and `exec` (so the two verbs can't
    drift on how they honour the same config). `cmd_argv` is the ALREADY-assembled
    command -- env-shim vs login-shell vs raw is the caller's decision.

    Ordering exploits docker's last-wins flag parsing to keep credproxy in
    control of session behaviour while still honouring `user` + the `exec_flags`
    escape hatch: the default `--workdir` (config `workdir`, else `home`), then
    config `user`, then `exec_flags` (may override -w/-u or add -e), then the
    per-call `user_override`, then credproxy's session-control flags as EXPLICIT
    booleans last -- so a stray -d/-t/-i in `exec_flags` can't detach the session
    or break pidfile tracking, and a -w there still wins."""
    out = ["docker", "exec"]
    # Land in `workdir` (the workspaceFolder analog), defaulting to `home`, so we
    # drop into the project/home rather than the image's WORKDIR. Emitted before
    # exec_flags so a --workdir there still wins (docker last-wins).
    workdir = cfg.get("workdir") or cfg.get("home")
    if workdir:
        out += ["--workdir", workdir]
    if cfg.get("user") and not user_override:
        out += ["-u", cfg["user"]]
    out += cfg.get("exec_flags") or []
    if user_override:
        out += ["-u", user_override]
    out += ["--interactive=true", f"--tty={'true' if isatty else 'false'}", "--detach=false"]
    out.append(container)
    out += cmd_argv
    return out


def _enter_exec_cmd(cfg: dict, container: str, cmd: list[str], *,
                    user_override: str | None, isatty: bool) -> list[str]:
    """Assemble the `docker exec` argv for `enter`: the shared prefix plus the
    command wrapped in the env shim (`_enter_command`)."""
    if not cmd:
        # No explicit `-- CMD`: run the config `shell`, defaulting to a login
        # shell. `enter` is "log into the workspace" (ssh model), so the
        # interactive entry sources the full login env; an explicit command
        # stays bare/non-login (the ssh `host cmd` model).
        cmd = list(cfg.get("shell") or DEFAULT_ENTER_CMD)
    return _docker_exec_argv(cfg, container, _enter_command(cfg, cmd),
                             user_override=user_override, isatty=isatty)


# Default `enter` command when none is given and no `shell` is configured: a
# LOGIN shell, so interactive entry behaves like logging into the box.
DEFAULT_ENTER_CMD = ["bash", "-l"]


# Default `enter` env shim: source the proxy's bootstrap-written env file (CA
# bundle vars) before exec'ing the command. Guarded by `[ -f ... ]` so a missing
# file (bootstrap not run yet) is a no-op, not an error that would abort before
# the exec.
DEFAULT_ENTER_PRELUDE = (
    "[ -f /etc/profile.d/credproxy.sh ] && . /etc/profile.d/credproxy.sh"
)


def _enter_command(cfg: dict, cmd: list[str], label: str = "credproxy-enter") -> list[str]:
    """The command argv, optionally wrapped in an env shim. Shared by `enter` and
    `exec`'s default mode (`label` sets `$0`, shown in errors, per verb).

    By default credproxy wraps the command in `sh -c '<prelude>; exec "$@"'`,
    where the prelude sources the proxy's bootstrap-written env file
    (/etc/profile.d/credproxy.sh -- the CA-bundle vars). This is the only way to
    get that env into BOTH an interactive shell AND a bare `-- cmd` AND their
    subprocesses: docker exec is a direct execve (no shell init, no PAM), so the
    env file otherwise loads only in a login shell. `exec "$@"` replaces the shim
    in place, so there's no extra PID and signals/TTY/exit code/argv all pass
    through; `$0` is the label shown in error messages.

    Escape hatch: `enter_prelude` overrides the shell snippet; set it to "" to
    skip wrapping entirely (direct execve, no /bin/sh dependency). `exec --raw`
    is the per-call equivalent."""
    prelude = cfg.get("enter_prelude")
    if prelude is None:
        prelude = DEFAULT_ENTER_PRELUDE
    if not prelude:
        return list(cmd)
    return ["sh", "-c", f'{prelude}; exec "$@"', label, *cmd]


def effective_config(cfg: dict) -> dict:
    """A copy of the parsed config with the *enter-time* defaults resolved, for
    display (`config`/`inspect`).

    load_config already fills the create-time defaults (image, home, empty
    mounts/env/setup, map_host_user). This additionally resolves the two fields
    whose defaults are computed at enter time, so they don't show as null when
    they actually have an effect: `workdir` -> `home`, and `enter_prelude` ->
    the default shim snippet. The result reflects what `enter` actually does."""
    out = dict(cfg)
    out["workdir"] = cfg.get("workdir") or cfg.get("home")
    ep = cfg.get("enter_prelude")
    out["enter_prelude"] = DEFAULT_ENTER_PRELUDE if ep is None else ep
    out["shell"] = list(cfg.get("shell") or DEFAULT_ENTER_CMD)
    # user_uid defaults to the host uid (the keep-id target when unset)
    uid = cfg.get("user_uid")
    if uid is None and hasattr(os, "getuid"):
        uid = os.getuid()
    out["user_uid"] = uid
    return out


def enter_workspace(ws: Workspace, cmd: list[str], notify: Notify = _noop,
                    user_override: str | None = None, push: bool = False) -> int:
    """Start the workspace (if not running), run `cmd` inside it, and handle
    auto-stop when the session ends.

    Returns the exit code of the command.

    Session tracking: writes a pidfile to <state_dir>/sessions/<pid> before
    running. This uses subprocess.run (not os.execvp) so we can clean up and
    check auto-stop after the command exits.

    User: the config `user` runs the exec as that user (`docker exec -u`);
    `user_override` (from `enter --user`) beats it for one session. The escape
    hatch `exec_flags` is spliced in too. Ordering exploits docker's last-wins
    parsing: config user, then exec_flags (may override -u or add -w/-e), then
    the override, then credproxy's session-control flags as EXPLICIT booleans
    last -- so a stray -d/-t/-i in exec_flags can't break session tracking.

    Signal handling: subprocess.run propagates SIGINT to the subprocess via
    the normal terminal signal delivery; we do NOT set up SIGINT forwarding
    explicitly since docker exec in the same process group receives it.
    """
    import os
    import sys
    from . import startup

    pid = os.getpid()
    # Hold the lifecycle lock across start + session registration, then release it
    # BEFORE the blocking session (the interactive exec must not serialize other
    # enters). Registering the pidfile under the lock means a concurrent auto-stop
    # counts this in-flight enter and can't stop the workspace during/just-after
    # start, out from under us.
    with ws.lock():
        startup.start_workspace(ws, notify, force_push=push)
        cfg = load_config(ws)
        exec_cmd = _enter_exec_cmd(
            cfg, ws.ws_container, cmd,
            user_override=user_override, isatty=sys.stdin.isatty(),
        )
        ws.sessions_dir.mkdir(parents=True, exist_ok=True)
        pidfile = _session_pidfile(ws, pid)
        pidfile.write_text(str(pid))

    try:
        result = subprocess.run(exec_cmd, check=False)
        exit_code = result.returncode
    finally:
        # Always clean up our pidfile.
        pidfile.unlink(missing_ok=True)

    # Auto-stop: read config fresh (live config edit semantics).
    _maybe_auto_stop(ws, pid, notify)

    # Map a signal death (-N) to 128+N, matching the shell convention (SIGINT ->
    # 130, not the OS-truncated 254) and the subprocess's own exit code.
    return 128 - exit_code if exit_code < 0 else exit_code


def _start_for_exec(ws: Workspace, notify: Notify, *, push: bool) -> None:
    """Bring the workspace up for `exec`, with a fast path for the hot loop.

    If BOTH containers are already running and no `--push` was asked, skip the
    full `start` reconciliation (image/spec-drift checks, host-port resolve, the
    `wait_for_ready` HTTP round-trip) -- ~6 docker forks + a network hop that
    would otherwise dominate a burst of quick commands. `exec` is the "fire many
    commands" verb; keeping config/drift in sync is what `start`/`apply` are for,
    and `--push` (or either container being down) forces the full path. The
    trade-off: an `exec` right after editing a binding won't pick it up until a
    `start`/`apply`/`exec --push`."""
    from . import startup
    if not push and ws.exists() \
            and docker.container_status(ws.proxy_container) == "running" \
            and docker.container_status(ws.ws_container) == "running":
        return
    startup.start_workspace(ws, notify, force_push=push)


def exec_workspace(ws: Workspace, cmd: list[str], notify: Notify = _noop, *,
                   mode: str = "shim", user_override: str | None = None,
                   push: bool = False) -> int:
    """One-shot: start the workspace if needed, run `cmd` inside it, return its
    exit code. The scriptable sibling of `enter`: it never INITIATES an auto-stop
    (no teardown churn when firing many quick commands) and takes the `exec` fast
    path when the workspace is already up.

    It DOES register a session pidfile for the command's duration -- so a
    concurrent `enter` session's auto-stop teardown counts this in-flight exec and
    can't `docker stop` the box out from under it -- but never calls
    `_maybe_auto_stop` itself. So `exec` is protected from the reaper without ever
    becoming one.

    `mode` picks the command's environment: "shim" (default) sources the CA-trust
    env like `enter -- CMD`; "raw" is a direct execve (no shell, minimal images);
    "login" is a `bash -lc` login shell. `user_override` (`--user`) beats config
    `user` for this call. The lock is held only around start + pidfile
    registration, not the (possibly long) command."""
    import os
    import sys

    pid = os.getpid()
    with ws.lock():
        _start_for_exec(ws, notify, push=push)
        cfg = load_config(ws)
        exec_cmd = _exec_cmd(cfg, ws.ws_container, cmd, mode=mode,
                             user_override=user_override, isatty=sys.stdin.isatty())
        ws.sessions_dir.mkdir(parents=True, exist_ok=True)
        pidfile = _session_pidfile(ws, pid)
        pidfile.write_text(str(pid))
    try:
        rc = subprocess.run(exec_cmd, check=False).returncode
    finally:
        pidfile.unlink(missing_ok=True)
    # Map a signal death (-N) to the shell convention 128+N, so SIGINT reports 130
    # (not the OS-truncated 254) and the returned code matches the process's own
    # exit -- this verb's whole headline is "propagate its exit code".
    return 128 - rc if rc < 0 else rc


def _exec_cmd(cfg: dict, container: str, cmd: list[str], *, mode: str,
              user_override: str | None, isatty: bool) -> list[str]:
    """Assemble the `docker exec` argv for `exec` via the shared prefix
    (`_docker_exec_argv`), wrapping the command per `mode`:

    - "shim" (default): the same env shim `enter -- CMD` uses, so the CA-trust
      env (SSL_CERT_FILE etc.) is set -- `exec -- curl https://…` works against
      the intercepting proxy just like `enter -- curl …`. Honours `enter_prelude`.
    - "raw": direct execve, no shell wrapper -- predictable, no /bin/sh dependency
      (minimal/distroless images), at the cost of the CA-trust env.
    - "login": `bash -lc` login shell, so /etc/profile.d + the login rc load
      (mise shims); needs bash in the image.

    `--tty` only when stdin is a TTY, so a piped one-shot isn't given a pty."""
    if mode == "raw":
        cmd_argv = list(cmd)
    elif mode == "login":
        cmd_argv = ["bash", "-lc", 'exec "$@"', "credproxy-exec", *cmd]
    else:  # "shim" -- CA-trust env, parity with `enter -- CMD`
        cmd_argv = _enter_command(cfg, cmd, label="credproxy-exec")
    return _docker_exec_argv(cfg, container, cmd_argv,
                             user_override=user_override, isatty=isatty)


def _maybe_auto_stop(ws: Workspace, our_pid: int, notify: Notify) -> None:
    """Stop the workspace if auto_stop is enabled and no other sessions live."""
    import tomllib

    # Read config fresh -- auto_stop may have been edited mid-session. Match
    # load_config's strictness with `is True`: a non-bool (e.g. the string
    # "false") must not enable auto-stop via a mid-session edit either.
    if not ws.config_path.exists():
        return
    try:
        raw = tomllib.loads(ws.config_path.read_text())
    except Exception:
        return
    if raw.get("auto_stop") is not True:
        return

    # Decide-and-stop under the lifecycle lock so the "no other sessions -> stop"
    # check is atomic against a concurrent enter registering its session: either
    # we see its pidfile (and don't stop), or it waits for the lock and re-runs
    # start after our stop -- never a stop racing a just-registered session.
    with ws.lock():
        _clean_stale_sessions(ws)
        if _count_live_sessions(ws, exclude_pid=our_pid) > 0:
            return  # other sessions still alive; don't stop
        notify(f"auto_stop: stopping workspace '{ws.name}'")
        containers.stop_workspace(ws)
