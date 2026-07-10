"""The `dev` harness (build/test/reload), `doctor`, and `emit-compose` -- the
commands that operate on credproxy itself or on the whole environment rather than
one workspace's containers."""
from __future__ import annotations

import os
import sys

from ..core.engine import docker as core_docker
from ..core.engine import lifecycle
from ..core.errors import DependencyError
from ..core.model.workspace import for_name
from ..core.paths import IMAGE_TAG, PROXY_DIR, TESTS_DIR
from . import render
from .render import fail, say
from .common import Ctx, _resolve_ws, _require_exists, _reject_if_attached


def do_dev_build(ctx: Ctx) -> None:
    from ..core import paths

    if not PROXY_DIR.is_dir():
        fail(f"{PROXY_DIR} not found -- `dev` commands need the repo checkout")

    args = ["build", "-t", IMAGE_TAG]
    # Stamp the source digest so `start`/`doctor` can detect a checkout that has
    # drifted from the built image (a `git pull` that touched proxy/).
    digest = paths.proxy_src_digest()
    if digest:
        args += ["--label", f"{paths.SRC_DIGEST_LABEL}={digest}"]
    args += [str(PROXY_DIR)]
    core_docker.docker(args, stream=True)


def do_dev_test(ctx: Ctx, trailing: list[str], cli_only: bool = False,
                proxy_only: bool = False, force_container: bool = False) -> None:
    """Run the test suite(s).

    Default: run BOTH the host-side CLI tests (tests/cli/) and the proxy suite
    (tests/). The proxy suite runs ON-HOST when its runtime deps (mitmproxy,
    aiohttp, starlark) import there -- near-instant -- else inside the image via
    `docker run`. Trailing args after `--` pass through to the proxy pytest.

    --cli:       host CLI tests only (no docker required).
    --proxy:     proxy suite only.
    --container: force the proxy suite into the image even if the deps are
                 importable on the host (the canonical, version-pinned env).

    Overlay tests: each configured overlay (`CREDPROXY_OVERLAY_PATH`) with a
    `tests/` subdir is run as its OWN pytest invocation (its `test_*.py` module
    basenames would collide with the repo suite under one rootdir), using the
    same on-host-or-image fallback as the proxy suite. The full overlay chain is
    always mounted + on the resolution path so an overlay test can resolve
    injectors/scripts from any tier.
    """
    import importlib.util
    import subprocess
    from ..core.engine.imageenv import ImageEnv
    from ..core.paths import TESTS_DIR, REPO_ROOT, overlay_dirs

    run_cli = not proxy_only
    run_proxy = not cli_only

    cli_failed = False
    if run_cli:
        if importlib.util.find_spec("pytest") is None:
            # Graceful note rather than a raw "No module named pytest".
            msg = ("host pytest not found; skipping CLI tests "
                   "(install pytest to run tests/cli/)")
            if not run_proxy:
                fail(msg)
            say(msg)
        else:
            say("running host-side CLI tests (tests/cli/)...")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(REPO_ROOT / "tests" / "cli"), "-v"],
                check=False,
            )
            cli_failed = result.returncode != 0
            say("CLI tests FAILED" if cli_failed else "CLI tests passed.")
            if not run_proxy:
                sys.exit(result.returncode)

    # Every configured overlay, indexed (for container mount paths) + the subset
    # that ships a tests/ suite. The full chain is mounted even when only one
    # overlay's tests run, so resolution sees every tier (an overlay test may
    # resolve a definition another overlay shadows).
    all_overlays = list(enumerate(overlay_dirs()))                  # [(i, (label, dir))]
    overlay_suites = [(i, label, d) for i, (label, d) in all_overlays
                      if (d / "tests").is_dir()]

    # Proxy suite. tests/cli/ is excluded (host-only; its module names collide
    # with the proxy suite's under one rootdir). Prefer running it ON-HOST when
    # the proxy's runtime deps import there -- it skips the ~container-startup
    # tax that dominates the inner loop -- falling back to the image otherwise.
    proxy_deps = ("mitmproxy", "aiohttp", "starlark")
    missing = [m for m in proxy_deps if importlib.util.find_spec(m) is None]

    if not force_container and not missing:
        say("running proxy suite on-host (deps present; --container forces the image)...")
        env = {**os.environ, "PYTHONPATH": str(PROXY_DIR)}
        host_cmd = [sys.executable, "-m", "pytest", "-v", str(TESTS_DIR),
                    "--ignore", str(TESTS_DIR / "cli")] + trailing
        # Exec the proxy suite (preserves TTY) ONLY when it is the last thing to
        # run -- nothing before it failed and no overlay suites follow. Otherwise
        # run each suite as a subprocess and combine exit codes.
        if not cli_failed and not overlay_suites:
            os.execvpe(sys.executable, host_cmd, env)  # replace proc
        combined = cli_failed
        combined |= subprocess.run(host_cmd, check=False, env=env).returncode != 0
        # Overlay tests import `testkit` (proxy dir) + resolve the CLI package;
        # PYTHONPATH covers both. os.environ carries CREDPROXY_OVERLAY_PATH so
        # resolution sees the same chain we discovered above.
        ov_env = {**os.environ,
                  "PYTHONPATH": os.pathsep.join([str(PROXY_DIR), str(REPO_ROOT / "cli")])}
        for i, label, d in overlay_suites:
            say(f"running overlay tests: {label} ({d / 'tests'})...")
            ov_cmd = [sys.executable, "-m", "pytest", "-v", str(d / "tests")]
            combined |= subprocess.run(ov_cmd, check=False, env=ov_env).returncode != 0
        sys.exit(1 if combined else 0)

    if not force_container:
        say(f"proxy deps not importable on host ({', '.join(missing)}); running the "
            f"proxy suite in the image. Install them (see proxy/requirements.txt) "
            f"for the faster on-host path.")
    meta = ImageEnv.load()

    # Mounts + CREDPROXY_OVERLAY_PATH rewrite for the whole overlay chain: each
    # host overlay dir is bind-mounted read-only at /opt/overlays/<i> and the env
    # var is rewritten to those container paths (declared order preserved), so
    # resolution INSIDE the image sees the overlays rather than absent host paths.
    overlay_mounts: list[str] = []
    container_overlay_paths: list[str] = []
    for i, (label, d) in all_overlays:
        cpath = f"/opt/overlays/{i}"
        overlay_mounts += ["-v", f"{d}:{cpath}:ro"]
        container_overlay_paths.append(cpath)
    overlay_env = (["-e", "CREDPROXY_OVERLAY_PATH=" + ":".join(container_overlay_paths)]
                   if container_overlay_paths else [])

    def _docker_run(pytest_args: list[str], *, extra_env: list[str]) -> list[str]:
        return [
            "docker", "run", "--rm",
            "-v", f"{PROXY_DIR}:{meta.source}",
            "-v", f"{TESTS_DIR}:/opt/tests",
            # Read-only so the proxy suite can validate the CLI's builtin scripts
            # (the dogfood .star) against the Python built-ins -- single source of
            # truth, even though the proxy never reads cli/ at runtime.
            "-v", f"{REPO_ROOT / 'cli'}:/opt/cli:ro",
            *overlay_mounts,
            *extra_env,
            "-w", "/opt",
            "--entrypoint", "python",
            IMAGE_TAG,
            *pytest_args,
        ]

    proxy_cmd = _docker_run(
        ["-m", "pytest", "-v", "tests/", "--ignore=tests/cli", *trailing],
        extra_env=[],
    )
    # Overlay suites run under the rewritten CREDPROXY_OVERLAY_PATH; PYTHONPATH
    # covers the proxy dir (for `import testkit`) + the CLI package (testkit also
    # self-inserts /opt/cli, but be explicit since overlay tests carry no conftest).
    ov_extra_env = overlay_env + ["-e", f"PYTHONPATH={meta.source}:/opt/cli"]
    overlay_cmds = [
        (label, _docker_run(["-m", "pytest", "-v", f"/opt/overlays/{i}/tests"],
                            extra_env=ov_extra_env))
        for i, label, d in overlay_suites
    ]

    try:
        if not cli_failed and not overlay_cmds:
            # Proxy suite is the only thing to run: exec (preserves TTY).
            os.execvp("docker", proxy_cmd)
        combined = cli_failed
        combined |= subprocess.run(proxy_cmd, check=False).returncode != 0
        for label, ocmd in overlay_cmds:
            say(f"running overlay tests in image: {label}...")
            combined |= subprocess.run(ocmd, check=False).returncode != 0
        sys.exit(1 if combined else 0)
    except FileNotFoundError:
        # subprocess.run / execvp both raise this if `docker` isn't on PATH.
        raise DependencyError(core_docker.DOCKER_MISSING_MSG)


def do_dev_reload(ctx: Ctx, name: str | None) -> None:
    ws = _resolve_ws(ctx, name)
    _reject_if_attached(ws, "dev reload")
    lifecycle.reload_proxy(ws)
    render.OUT.reloaded(ws.name)


def do_doctor(ctx: Ctx, name: str | None, fetch: bool) -> None:
    """Environment preflight + config validation. Reports ALL failures; exits
    non-zero iff any check fails. NAME limits to one workspace (default: all)."""
    from ..core.engine import doctor
    # A bare read-only scan-all is fine (matches `list`), but `--fetch` resolves
    # secrets -- which can prompt / unlock a vault -- so refuse to fan that out
    # across every workspace from one nameless command; require an explicit NAME.
    if fetch and name is None:
        fail("`doctor --fetch` needs a workspace NAME (it resolves secrets, which "
             "can prompt or unlock a vault -- refusing to fan that out across every "
             "workspace); run `doctor` with no NAME for a read-only scan of all")
    checks = doctor.run(name, fetch=fetch)
    render.OUT.doctor([{"id": c.id, "ok": c.ok, "message": c.message,
                        "hint": c.hint} for c in checks])
    if any(not c.ok for c in checks):
        sys.exit(1)


def do_emit_compose(ctx: Ctx, name: str | None, image: str | None) -> None:
    """Build + print the Compose fragment. With NAME, bake the workspace's real
    token path (the workspace must exist); without NAME, emit a
    ${CREDPROXY_STATE:?...}/auth.token reference plus a comment on where that dir
    lives. Every port/path is read from the image's ENV contract via ImageEnv."""
    from ..core.engine import compose as core_compose
    from ..core.engine.imageenv import ImageEnv
    from ..core.paths import IMAGE_TAG

    image_tag = image or IMAGE_TAG
    meta = ImageEnv.load(image_tag)
    if name is not None:
        ws = for_name(name)
        _require_exists(ws)
        token_source = str(ws.token_path)
        note = None
    else:
        # No workspace resolved: leave the token path to a Compose `.env`.
        # `:?` makes Compose fail loudly if CREDPROXY_STATE is unset rather than
        # silently bind-mounting an empty path.
        token_source = "${CREDPROXY_STATE:?set to the workspace state dir}/auth.token"
        note = ("# CREDPROXY_STATE is the workspace state dir "
                "($XDG_STATE_HOME/credproxy/workspaces/NAME; "
                "`credproxy workspace NAME inspect` shows it).")
    print(core_compose.emit_compose(meta, image_tag, token_source, note))
