"""Start orchestration: the ONLY cross-module sequencer in the engine plane.

`startup` wires the container primitives (`containers`), the setup runner
(`setup`), and the proxy HTTP transport together into the workspace lifecycle:
`start` (ensure proxy -> health wait -> resolve+push -> ensure workspace ->
setup), `recreate`, `apply`'s reconcile split, `reload`, and the `push`/`resolve`
verbs. It imports `containers`/`setup`; those (and `sessions`) must NOT import
`startup` -- the intra-engine boundary that keeps the sequencing one-directional.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import containers, docker, setup
from .containers import Notify, _noop, BindingSummary
from ..model.config import (
    load_config,
    workspace_spec_hash,
)
from ..errors import (
    ConfigError, DockerError, ImageError, ProxyError, WorkspaceError,
)
from .imageenv import ImageEnv
from ..model.workspace import Workspace, ensure_token, hostname_for
from ..paths import IMAGE_TAG
from .proxy_http import proxy_status, wait_for_ready
from .push import push_config


def push_workspace(ws: Workspace, notify: Notify = _noop, *,
                   wait: bool = False, timeout: float = 120.0) -> str:
    """The `push` verb: resolve every binding's secret and POST the FULL wire
    config (bindings + rules, the SAME body `start` sends) to the workspace's
    proxy -- managed or attached. Records applied bindings/rules metadata + the
    returned config_generation into the lock's `applied` section (G5) so `inspect`
    drift works. Returns the admin URL pushed to.

    `wait` polls `/health` (never `/ready`, I1) until capture-ready or `timeout`.
    A blocking per-workspace push lock makes a concurrent push WAIT then re-push
    rather than race (never skip). Atomic fail-closed (I3): an unresolvable ref
    aborts the whole push before anything is sent (materialize/wire_config raise)."""
    from . import push as core_push
    from ..model.lock import save_lock
    from ..model.resolver import resolve_workspace
    from ..model.rules import combined_fingerprint
    from ..model.workspace import read_token

    admin_url = containers.resolve_admin_url(ws, notify)
    if wait:
        core_push.wait_for_health(admin_url, timeout, notify)
    token = read_token(ws)
    with core_push.workspace_push_lock(ws):
        notify("pushing config...")
        resolved = resolve_workspace(ws)
        for n in resolved.notes:
            notify(f"note: {n}")
        # Persist any newly-minted resolver placeholders FIRST, then record the
        # `applied` section -- both under the held flock, so the applied update's
        # load-modify-write reads the fresh lock and preserves the placeholders.
        if resolved.lock_dirty:
            save_lock(ws, resolved.lock)
        bindings, rules = resolved.bindings, resolved.rules
        fp = combined_fingerprint(bindings, rules)
        _, _, generation = core_push.push_to_target(
            admin_url, token, bindings, rules, fp, notify)
        containers._write_applied_push(ws, bindings, rules, generation)
    return admin_url


def resolve_workspace_wire(ws: Workspace, notify: Notify = _noop) -> dict:
    """Build the FULL wire config (bindings + rules + fingerprint) with resolved
    secret VALUES, without contacting any proxy -- the `resolve` verb. Names and
    placeholders are materialized (like push) so the wire is complete and valid.

    The result carries real secrets: it is the one at-rest disclosure path, so
    `resolve --out` writes it mode 0600 and warns outside the state dir."""
    from ..model.lock import save_lock
    from ..model.resolver import resolve_workspace
    from ..model.rules import combined_fingerprint
    from ..model.wire import build_wire

    with ws.lock():
        resolved = resolve_workspace(ws)
        if resolved.lock_dirty:
            save_lock(ws, resolved.lock)
    bindings, rules = resolved.bindings, resolved.rules
    fp = combined_fingerprint(bindings, rules)
    return build_wire(bindings, rules, fp)


def start_workspace(ws: Workspace, notify: Notify = _noop,
                    force_push: bool = True) -> None:
    """Bring the workspace to fully-running, holding the per-workspace lifecycle
    lock so a concurrent start/enter of the SAME workspace can't race container
    creation, setup, and state writes. See _start_workspace_locked for the steps."""
    with ws.lock():
        _start_workspace_locked(ws, notify, force_push)


def _start_workspace_locked(ws: Workspace, notify: Notify = _noop,
                            force_push: bool = True) -> None:
    """Idempotently bring the workspace to fully-running. Auto-creates
    the workspace files if missing. Multiple workspaces run independently;
    other running workspaces are left untouched.

    `force_push` (default True for explicit `start`) always re-pushes config;
    `enter` passes force_push=False for a fast path that skips the push -- and
    the provider calls it implies -- when the already-running proxy reports the
    intended config's fingerprint.

    Progress is reported through `notify`."""
    if not ws.exists():
        containers.create_workspace_files(ws)
        notify(f"created workspace '{ws.name}'")

    meta = ImageEnv.load()
    # Resolve once (config-v2): `resolved.config` is the container half with any
    # `[[pack]]` container half (mounts/env/setup) merged in and binds
    # existence-checked (check_bind_exists=True), so a pack mount feeds the spec
    # hash exactly like a stamped one; `resolved.bindings`/`.rules` are the merged
    # (literal + pack) set the push below uses.
    from ..model.resolver import resolve_workspace
    resolved = resolve_workspace(ws, check_bind_exists=True)
    for n in resolved.notes:
        notify(f"note: {n}")
    cfg = resolved.config
    containers._reserved_uid_check(cfg, meta)

    ensure_token(ws)

    # ---- proxy ----
    image_id = docker.inspect(IMAGE_TAG, "{{.Id}}")
    if image_id is None:
        raise ImageError(
            f"image {IMAGE_TAG} not found; run `credproxy dev build` first"
        )

    proxy_fresh = False  # created or started this call -> tmpfs config is empty
    status = docker.container_status(ws.proxy_container)
    if status is not None and \
            docker.inspect(ws.proxy_container, "{{.Image}}") != image_id:
        notify("proxy image changed; recreating proxy "
               "(workspace will need re-bootstrap)")
        # Remove the WORKSPACE container FIRST: it shares the proxy's netns
        # (--network container:<proxy>), and Docker refuses to remove a container
        # whose netns a still-running container is using -- so removing the proxy
        # while the workspace is up would fail. It's recreated below regardless
        # (the new proxy id changes the spec hash). The proxy removal is CHECKED
        # so a genuine failure aborts here rather than silently colliding on the
        # re-create with a leftover container.
        docker.docker_quiet(["rm", "-f", ws.ws_container])
        docker.docker(["rm", "-f", ws.proxy_container])
        status = None
    if status is None:
        notify("starting proxy...")
        containers.create_proxy(ws, meta)
        proxy_fresh = True
    elif status != "running":
        docker.docker(["start", ws.proxy_container])
        proxy_fresh = True

    # Resolve the ephemeral host port assigned to this workspace's proxy.
    host_port = docker.resolve_host_port(ws.proxy_container, meta.http_port)
    try:
        wait_for_ready(host_port)
    except ProxyError as e:
        # The bare readiness error ("Connection refused") hides the usual
        # cause: the proxy crashed on boot. Surface its exit + log tail inline
        # so the user doesn't have to run `logs` separately to find out.
        raise ProxyError(f"{e}\n{containers._proxy_diagnostics(ws)}") from e

    # Push the bindings config -- but on the `enter` fast path, skip it (and the
    # provider calls it implies) when the already-running proxy reports the
    # intended config's fingerprint. The proxy's tmpfs config does not survive a
    # restart, so a (re)started proxy (proxy_fresh) always gets a push. `resolved`
    # (bindings/rules/config, lock) was computed up front.
    from ..model.lock import save_lock
    from ..model.rules import combined_fingerprint
    if resolved.lock_dirty:
        save_lock(ws, resolved.lock)
    bindings, rules = resolved.bindings, resolved.rules
    want_fp = combined_fingerprint(bindings, rules)
    status = None if (force_push or proxy_fresh) else proxy_status(ws, host_port)
    if containers._should_push(force_push, proxy_fresh, status, want_fp):
        notify("pushing config...")
        pushed_bindings, pushed_rules, generation = push_config(
            ws, host_port, notify, bindings=bindings, rules=rules,
            fingerprint=want_fp)
        containers._write_applied_push(ws, pushed_bindings, pushed_rules, generation)
    else:
        notify("config unchanged on the proxy; skipped push "
               "(use `enter --push` to refresh)")

    # ---- workspace container ----
    proxy_id = docker.inspect(ws.proxy_container, "{{.Id}}")
    spec_hash = workspace_spec_hash(cfg, proxy_id, hostname_for(ws.name))
    status = docker.container_status(ws.ws_container)
    if status is not None:
        current = docker.inspect(
            ws.ws_container, '{{index .Config.Labels "credproxy.spec"}}'
        )
        if current != spec_hash:
            notify("workspace spec changed; recreating workspace container")
            docker.docker_quiet(["rm", "-f", ws.ws_container])
            status = None
    if status is None:
        notify("starting workspace container...")
        try:
            containers.create_ws_container(ws, cfg, spec_hash, proxy_id=proxy_id)
        except DockerError as e:
            # A runc-on-rootless-podman `sysfs` failure surfaces here as a raw
            # OCI mount error; enrich it with the two remedies when we recognize
            # it (#50). Non-matching failures re-raise unchanged.
            raise containers._enrich_ws_run_error(e, cfg)
    elif status != "running":
        docker.docker(["start", ws.ws_container])

    # ---- setup (once per container instance; retries a failed prior attempt) ----
    # Gate on the container id: a freshly created/recreated container has a new
    # id (-> run), a plain stop/start keeps the same id (-> skip), and the marker
    # is written only AFTER setup succeeds -- so a failed setup re-runs on the
    # next `start`.
    container_id = docker.inspect(ws.ws_container, "{{.Id}}")
    if setup._setup_needed(setup._read_setup_marker(ws), container_id):
        # Before setup, since a setup command's user phase may write into a
        # nested mount's parent (e.g. clone a sibling repo under ~/src). Same
        # cadence as setup: runs once per fresh/recreated container (the
        # fabricated parents live in the home volume, so idempotent thereafter).
        containers.chown_mount_parents(ws, cfg, notify)
        # `bindings` (materialized above) supplies the binding env for typed
        # setup steps -- placeholders are known post-push, so no in-container curl.
        setup.run_setup(ws, cfg, notify, bindings=bindings)
        # After setup: the `user` may have been provisioned by it, and must
        # exist before we chown user-owned volumes to it by name.
        setup.chown_user_owned_volumes(ws, cfg, notify)
        setup._write_setup_marker(ws, container_id)


def recreate_workspace(ws: Workspace, notify: Notify = _noop,
                       include_proxy: bool = False,
                       reset_volumes: list[str] | None = None) -> None:
    """Force-rebuild the workspace container (and the proxy too if
    `include_proxy`), then bring everything back up via `start_workspace`.

    By default this preserves all managed volumes, the config file, auth token,
    and state dir -- only the container(s) are destroyed, so persistent data
    survives and `setup` re-runs (the rebuilt container has a fresh id). It's
    "give me a clean container without losing my workspace".

    `reset_volumes` additionally drops the named managed volumes (e.g. `home`,
    `cache`), which `start_workspace` re-seeds from the image -- the one recreate
    mode that destroys data, so callers gate it like `delete`. Bind/overlay mounts
    are host paths and are never touched; config/token/state survive, so the
    workspace stays defined.

    Removing the container(s) makes `start_workspace` see them absent and create
    fresh ones, reusing the still-running proxy when we keep it. Recreating the
    proxy gives it a new id -- part of the workspace's spec hash -- so
    `start_workspace` recreates the workspace alongside it regardless, and the
    workspace re-bootstraps CA trust against the proxy's regenerated CA."""
    reset_volumes = reset_volumes or []
    # Validate --reset-volume names against the workspace's DECLARED managed
    # volumes UP FRONT (before destroying anything): a typo like
    # `--reset-volume hmoe` must error loudly, not silently preserve `home` while
    # reporting success. (A declared-but-not-yet-created volume is fine to
    # "reset" -- the removal below tolerates a missing one.)
    if reset_volumes:
        from ..model.resolver import resolve_workspace
        cfg = resolve_workspace(ws).config
        allowed = {m["name"] for m in cfg["mounts"] if m["kind"] == "volume"}
        unknown = sorted(v for v in reset_volumes if v not in allowed)
        if unknown:
            raise ConfigError(
                f"--reset-volume: {', '.join(unknown)} not a managed volume of "
                f"'{ws.name}' (declared: {', '.join(sorted(allowed)) or 'none'})"
            )
    # Hold the lifecycle lock across remove-then-start so a concurrent start/enter
    # can't slip in between (and re-create the container we just removed, or race
    # the volume reset). start_workspace re-enters the same lock.
    with ws.lock():
        notify("recreating workspace container...")
        docker.docker_quiet(["rm", "-f", ws.ws_container])
        if include_proxy:
            notify("recreating proxy container...")
            docker.docker_quiet(["rm", "-f", ws.proxy_container])
        for name in reset_volumes:
            # The container is already removed, so the volume is free to drop;
            # `start_workspace` re-creates it, seeded from the image.
            notify(f"resetting volume '{name}'...")
            docker.docker_quiet(["volume", "rm", ws.volume(name)])
        start_workspace(ws, notify=notify)


def add_managed_volume(ws: Workspace, *, name: str, target: str,
                       readonly: bool, preserve: bool,
                       user_owned: bool = False,
                       notify: Notify = _noop) -> None:
    """Add a managed-volume mount to the workspace config, optionally seeding it
    with the live container's data at `target` before the recreate that applies
    it.

    Without `preserve`: a pure config edit -- the new volume is image-seeded on
    the next `start`, like any added volume, and the change is deferred (the
    caller hints `start`). With `preserve`: capture the current container's data
    at `target` into the new volume, then recreate so the volume mounts
    populated. A mount can't be attached to a running container, so applying ANY
    new mount requires recreating it; `preserve` just carries the data across
    that unavoidable recreate (Docker skips its own image-seed because the
    pre-seeded volume is non-empty).

    Validation mirrors load_config (absolute target, valid volume name, no
    duplicate target/name), so a bad request fails before anything is touched.
    A volume named `home` is the `home = "..."` sugar, handled by write_added_mount."""
    from ..model import config as core_config
    from ..model.resolver import resolve_workspace

    cfg = resolve_workspace(ws).config

    # ---- validate up front (touch nothing until this passes) ----
    if not target.startswith("/"):
        raise ConfigError(f"mount target must be absolute: {target!r}")
    norm = target.rstrip("/") or "/"
    if name == "home":
        if readonly:
            raise ConfigError("the home volume can't be read-only")
        existing_home = cfg.get("home")
        if existing_home and (existing_home.rstrip("/") or "/") != norm:
            raise ConfigError(
                f"workspace '{ws.name}' already sets home = {existing_home!r}; "
                f"edit it directly to change the home path"
            )
    elif not core_config._VOLUME_NAME_RE.match(name):
        raise ConfigError(
            f"volume name {name!r} is invalid (letters/digits/_.-, starting alnum)"
        )
    if user_owned:
        u = cfg.get("user")
        if not u or u.split(":", 1)[0] in ("root", "0"):
            raise ConfigError(
                f"workspace '{ws.name}' has no non-root `user`; --user-owned "
                f"would chown the volume to nobody (set a `user` first)"
            )
    for m in cfg["mounts"]:
        if (m["target"].rstrip("/") or "/") == norm:
            raise ConfigError(
                f"workspace '{ws.name}' already mounts {m['target']!r}"
            )
        if m["kind"] == "volume" and m["name"] == name:
            raise ConfigError(
                f"workspace '{ws.name}' already has a volume named {name!r}"
            )

    volume = ws.volume(name)

    with ws.lock():
        if preserve:
            status = docker.container_status(ws.ws_container)
            if status is None:
                raise WorkspaceError(
                    f"workspace '{ws.name}' has no container to preserve data "
                    f"from; add the mount without --preserve"
                )
            # Pre-create the labelled volume (idempotent; same labels as
            # _ensure_managed_volumes) so the capture has a target.
            docker.docker_quiet([
                "volume", "create",
                "--label", f"{containers._VOLUME_OWNER_LABEL}={ws.name}",
                "--label", f"credproxy.volume={name}",
                volume,
            ])
            # Quiesce the WORKSPACE container only (not stop_workspace, which
            # would also stop the proxy) for a consistent snapshot.
            if status == "running":
                notify(f"stopping workspace container to snapshot {target}...")
                docker.docker_quiet(["stop", "-t", "1", ws.ws_container])
            notify(f"capturing {target} into volume '{name}'...")
            try:
                docker.seed_volume_from_container(
                    ws.ws_container, target, volume, IMAGE_TAG,
                    userns_flags=containers._host_user_run_flags(cfg),
                )
            except DockerError:
                # Roll back: drop the half-seeded volume, leave the TOML
                # untouched, and bring the container back up.
                docker.docker_quiet(["volume", "rm", volume])
                docker.docker_quiet(["start", ws.ws_container])
                raise

        # Apply the config edit (surgical, comment-preserving).
        core_config.write_added_mount(ws, name, target, readonly, user_owned)

        if preserve:
            # Recreate so the populated volume is mounted. start_workspace skips
            # Docker's image-seed because the volume is non-empty.
            notify("recreating workspace container with the new volume...")
            recreate_workspace(ws, notify=notify)


@dataclass(frozen=True)
class ApplyResult:
    """Structured result of an `apply` operation.

    applied:  list of item labels that were live-applied (e.g. "bindings (x, y)")
    deferred: list of item labels that cannot be live-applied (e.g. "image")
              with a restart hint embedded in the label.
    """
    applied: tuple[str, ...]
    deferred: tuple[str, ...]


def apply_config(ws: Workspace, notify: Notify = _noop) -> ApplyResult:
    """Best-effort reconcile: apply what can be applied live; defer the rest.

    - Bindings drift → re-resolve + push to the running proxy. Reports
      "applied: bindings (...)". Updates the lock's `applied` section on success.
    - Container-spec drift (image/home/mounts/env/setup) → CANNOT be applied
      live; reported as "deferred: <field> (restart to apply: ...)".
    - Nothing drifted → both lists empty.
    - Workspace not running → raises WorkspaceError.

    Returns ApplyResult; never raises on deferred items (exit 0 is the contract).
    """
    if docker.container_status(ws.proxy_container) != "running":
        raise WorkspaceError(
            f"workspace '{ws.name}' is not running; "
            f"start it first (`credproxy workspace {ws.name} start`)"
        )

    # Hold the workspace flock across the WHOLE read-modify-write: resolve reads
    # the lock, then we persist placeholders and record `applied` -- a concurrent
    # flocked start/push must not interleave between the read and those writes and
    # clobber a section (#65 review). Reentrant, so a nested push_config is fine.
    with ws.lock():
        return _apply_config_locked(ws, notify)


def _apply_config_locked(ws: Workspace, notify: Notify) -> "ApplyResult":
    from ..model.resolver import resolve_workspace

    meta = ImageEnv.load()
    host_port = docker.resolve_host_port(ws.proxy_container, meta.http_port)

    # Read current configured bindings/rules (placeholders bound from the lock,
    # no secret fetch, no push yet). resolve_workspace is side-effect-free.
    # `resolved.config` folds in any `[[pack]]` container half for drift.
    resolved = resolve_workspace(ws, check_bind_exists=True)
    for n in resolved.notes:
        notify(f"note: {n}")
    cfg = resolved.config

    # Persist any newly-minted placeholders ONCE, up front, before drift and
    # push. This keeps the placeholder identity deterministic: the drift report
    # and the push (below, given explicit bindings/rules so push_config does NOT
    # re-resolve and re-mint) both see the exact same values. It also lands a
    # stale-drop-only dirty lock that has no drift to push.
    if resolved.lock_dirty:
        from ..model.lock import save_lock
        save_lock(ws, resolved.lock)

    # Build the binding summaries for drift.
    current_binding_summaries = [
        BindingSummary(
            name=b.name,
            injector=b.injector,
            provider=b.provider,
            secret=b.secret,
            hosts=b.hosts,
            placeholder=b.placeholder,
            env=b.env,
        )
        for b in resolved.bindings
    ]

    current_rules = resolved.rules

    drift = containers._compute_drift(ws, cfg, current_binding_summaries,
                                      running=True, current_rules=current_rules)

    applied_labels: list[str] = []
    deferred_labels: list[str] = []

    # Container-spec items can't be applied live.
    container_changes = [c for c in drift.changes if c.kind == "container"]
    for change in container_changes:
        deferred_labels.append(
            f"{change.item} (restart to apply: "
            f"credproxy workspace {ws.name} start)"
        )

    # Bindings and/or rules drift -> ONE push (they ride the same wire config).
    # These come from the OFFLINE, content-complete `_compute_drift`, so they see
    # a changed secret ref / provider / injector params / rule detail that the
    # lossy live projection cannot -- they are the AUTHORITATIVE config signal.
    binding_changes = [c for c in drift.changes if c.kind == "bindings"]
    rule_changes = [c for c in drift.changes if c.kind == "rules"]
    content_drift = bool(binding_changes) or bool(rule_changes)

    # The live view can only ADD a push reason the offline signal can't see:
    # reality-drift -- the proxy lost its tmpfs (or a foreign push landed), so it
    # is not holding the generation we recorded even though the TOML == our applied
    # cache. It must NEVER veto the offline `content_drift` (the lossy projection
    # can look identical while a secret/param/rule-detail change is real). When the
    # proxy is unreachable (`live is None`) only the offline signal drives the push.
    live = containers._live_drift(ws, f"http://127.0.0.1:{host_port}",
                                  has_content_drift=content_drift)
    reality_drift = live is not None and live.verdict == "reality-drift"

    if content_drift or reality_drift:
        # Pass the already-resolved bindings/rules so push_config uses the same
        # (now-persisted) placeholder identity instead of re-resolving/re-minting.
        # On a reality-drift-only push this re-records applied.config_generation,
        # closing doctor's config-sync loop.
        pushed_bindings, pushed_rules, generation = push_config(
            ws, host_port, notify,
            bindings=resolved.bindings, rules=resolved.rules)
        containers._write_applied_push(ws, pushed_bindings, pushed_rules, generation)
        if binding_changes:
            applied_labels.append(
                f"bindings ({', '.join(c.item for c in binding_changes)})")
        if rule_changes:
            applied_labels.append(
                f"rules ({', '.join(c.item for c in rule_changes)})")
        if reality_drift and not content_drift:
            applied_labels.append(
                f"config re-pushed (proxy held a config we didn't push: "
                f"live generation {live.generation}, "
                f"last-pushed {live.applied_generation})")

    return ApplyResult(
        applied=tuple(applied_labels),
        deferred=tuple(deferred_labels),
    )


def reload_proxy(ws: Workspace) -> None:
    """SIGHUP the workspace's proxy so python re-execs in place, then wait until
    it is capture-ready again. Raises WorkspaceError if the proxy is not running.

    The re-exec'd process starts un-ready (/health 503 until the mitmproxy
    listener rebinds), so `reload` blocks on `/health` -- it means "reloaded AND
    capturing again", not just "signal delivered"; otherwise a caller could hit
    the box during the reload's un-ready window."""
    if docker.container_status(ws.proxy_container) != "running":
        raise WorkspaceError(f"proxy for workspace '{ws.name}' is not running")
    docker.docker(["kill", "--signal=HUP", ws.proxy_container])
    meta = ImageEnv.load()
    host_port = docker.resolve_host_port(ws.proxy_container, meta.http_port)
    wait_for_ready(host_port)
