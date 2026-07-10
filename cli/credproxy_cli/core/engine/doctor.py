"""`credproxy doctor`: environment preflight + config validation in one sweep.

The weakest first-run paths -- docker missing/unreachable, proxy image not built,
an invalid hand-edited workspace TOML, an injector/provider that doesn't resolve,
a bad host glob -- all fail today one-error-at-a-time and only at action time
(`start`, `binding add`). `doctor` runs every cheap check at once and reports
**all** failures. No side effects; `fetch=True` (opt-in) additionally resolves
secrets (which can prompt/unlock). Pure data out (`list[Check]`); the porcelain
layer renders and sets the exit code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass

from ..model import hostmatch
from ..errors import CredproxyError
from .imageenv import ImageEnv
from ..paths import IMAGE_TAG, SRC_DIGEST_LABEL, image_label_digest, overlay_dirs, proxy_src_digest
from ..model.workspace import Workspace, for_name, list_names


@dataclass
class Check:
    id: str                       # stable, e.g. "docker", "image", "ws:cfg:myproj"
    ok: bool
    message: str
    hint: str | None = None


def _ok(id: str, message: str) -> Check:
    return Check(id, True, message)


def _fail(id: str, message: str, hint: str | None = None) -> Check:
    return Check(id, False, message, hint)


def run(ws_name: str | None = None, *, fetch: bool = False) -> list[Check]:
    """All checks: the environment, then each target workspace (NAME, or every
    workspace when NAME is None).

    An explicit NAME goes through `for_name`, the same charset/reserved-name/
    traversal validation every other command uses -- so `doctor '../../etc/passwd'`
    is a clean error, not a config read outside the workspaces dir. `list_names()`
    only returns already-valid names, so the scan-all path needs no re-validation."""
    targets = [for_name(ws_name)] if ws_name else [Workspace(n) for n in list_names()]
    # An attached workspace has no container/image of its own -- its containers are
    # managed externally -- so a `doctor NAME` for one skips the proxy-image env
    # check (docker is still checked; discovery may need it). The scan-all path
    # keeps the image check (some workspace may be managed).
    from ..model.config import quick_attach
    skip_image = bool(ws_name) and len(targets) == 1 and quick_attach(targets[0])
    checks = _env_checks(skip_image=skip_image)
    for ws in targets:
        checks += _workspace_checks(ws, fetch=fetch)
    return checks


def _env_checks(*, skip_image: bool = False) -> list[Check]:
    out: list[Check] = []
    if shutil.which("docker") is None:
        # Nothing else works without the engine; stop here with a clear hint.
        return [_fail("docker", "docker not found on PATH",
                      "install Docker or rootless Podman (see README)")]
    try:
        r = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
        out.append(_ok("docker", "docker daemon reachable") if r.returncode == 0
                   else _fail("docker", "docker found but the daemon is unreachable",
                              "start Docker / the podman socket"))
    except (OSError, subprocess.SubprocessError) as e:
        out.append(_fail("docker", f"`docker info` failed: {e}"))

    # An attached-only `doctor NAME` doesn't run credproxy's proxy container, so
    # the proxy image needn't be built locally -- skip the check.
    if not skip_image:
        try:
            ImageEnv.load(IMAGE_TAG)
            out.append(_ok("image", f"proxy image {IMAGE_TAG} present + valid"))
            out += _image_staleness_check()
        except CredproxyError as e:
            out.append(_fail("image", str(e), "run `credproxy dev build`"))

    # One existence check per EXPLICITLY configured overlay entry (index-
    # qualified so a --json consumer can't collide two). Only when
    # CREDPROXY_OVERLAY_PATH is set: an env entry can be typo'd, while the
    # default (discovered subdirs of the <repo>/overlay/ container) exists by
    # construction -- so unset env means no overlay checks at all. Resolution
    # stays tolerant of a missing entry elsewhere; flagging it loudly is
    # doctor's job. `overlay_dirs()` labels are `overlay:<base>`; the check id
    # carries the bare basename for readability.
    if os.environ.get("CREDPROXY_OVERLAY_PATH") is not None:
        for i, (label, d) in enumerate(overlay_dirs()):
            base = label.split(":", 1)[1]
            cid = f"overlay[{i}]:{base}:exists"
            out.append(_ok(cid, f"overlay {d} exists") if d.is_dir() else
                       _fail(cid, f"configured overlay {d} does not exist",
                             "create the directory or drop it from CREDPROXY_OVERLAY_PATH"))
    return out


def _image_staleness_check() -> list[Check]:
    """Compare the checkout's source digest against the `credproxy.src_digest`
    label `dev build` stamped on the (present, already-validated) image. A
    mismatch is NOT a failure -- the old image still works -- so it's a PASSING
    check carrying a rebuild hint. Skipped when there's no repo checkout (nothing
    to compare). Runs only after the `:image` check passed, so docker inspect works."""
    from . import docker as core_docker

    digest = proxy_src_digest()
    if digest is None:
        return []  # no repo checkout -> nothing to compare
    label = core_docker.inspect(
        IMAGE_TAG, '{{index .Config.Labels "' + SRC_DIGEST_LABEL + '"}}')
    stamped = image_label_digest(label)
    if stamped == digest:
        return [_ok("image:fresh", f"proxy image {IMAGE_TAG} matches the checkout")]
    if stamped is None:
        return [Check("image:fresh", True,
                      f"proxy image {IMAGE_TAG} predates source-digest tracking",
                      "rebuild with `credproxy dev build` if it seems out of date")]
    return [Check("image:fresh", True,
                  f"proxy source changed since {IMAGE_TAG} was built",
                  "rebuild with `credproxy dev build` to pick up the changes")]


def _workspace_checks(ws: Workspace, *, fetch: bool) -> list[Check]:
    if not ws.exists():
        return [_fail(f"ws:{ws.name}", f"workspace '{ws.name}' has no config file",
                      f"credproxy workspace create {ws.name}")]
    out: list[Check] = []
    from ..model.config import load_config, quick_attach
    attached = quick_attach(ws)
    try:
        cfg = load_config(ws)
        label = "attach block valid" if attached else "container config valid"
        out.append(_ok(f"ws:{ws.name}:config", f"[{ws.name}] {label}"))
    except CredproxyError as e:
        cfg = None
        out.append(_fail(f"ws:{ws.name}:config", f"[{ws.name}] config: {e}"))
    if attached:
        # The push (managed or attached) is bearer-authed with the host token, so
        # a missing/empty token would fail every push -- surface it here.
        tok = ws.token_path
        out.append(
            _ok(f"ws:{ws.name}:token", f"[{ws.name}] auth token present")
            if tok.exists() and tok.read_text().strip() else
            _fail(f"ws:{ws.name}:token", f"[{ws.name}] auth token missing/empty",
                  f"recreate it with `credproxy workspace {ws.name} push` "
                  f"(or delete + recreate the workspace)"))
    elif cfg is not None:
        # Managed workspace only (attached has no container of its own to run):
        # predict the runc `sysfs` failure (#50) before `start` hits the raw OCI
        # error. Skips silently on every not-bad case.
        out += _runc_keep_id_check(ws, cfg)
    binding_checks, fetched_refs = _binding_checks(ws, fetch=fetch)
    out += binding_checks
    out += _script_compile_checks(ws)
    out += _rule_checks(ws)
    out += _preset_requires_checks(ws, fetch=fetch, fetched_refs=fetched_refs)
    out += _proxy_config_sync_check(ws)
    return out


def _proxy_config_sync_check(ws: Workspace) -> list[Check]:
    """When the proxy is running/reachable, compare the generation it reports
    (GET /admin/config) against the lock's `applied.config_generation` -- the
    last generation credproxy recorded pushing. A mismatch means the proxy holds
    a config credproxy didn't push (it restarted and lost its tmpfs, or a stateless
    push came from elsewhere): a re-push (`apply`) heals it.

    Skip-with-note (ok=True) when the proxy is stopped/unreachable -- doctor stays
    green offline; a stopped proxy is nothing to reconcile. Rides the same admin
    URL resolution `push`/`apply` use, so it covers attached workspaces too."""
    from . import lifecycle
    from .proxy_http import get_config
    from ..model.workspace import read_token

    cid = f"ws:{ws.name}:proxy:config-sync"
    try:
        admin_url = lifecycle.resolve_admin_url(ws)
        token = read_token(ws)
    except CredproxyError:
        # Proxy not running / unreachable / token missing -> nothing to compare.
        return [Check(cid, True,
                      f"[{ws.name}] proxy not reachable -- config-sync check skipped")]
    live = get_config(admin_url, token)
    if live is None:
        return [Check(cid, True,
                      f"[{ws.name}] proxy did not answer -- config-sync check skipped")]
    applied_gen = lifecycle._load_applied(ws).get("config_generation")
    live_gen = live.get("generation")
    if live_gen == applied_gen:
        return [_ok(cid,
                    f"[{ws.name}] proxy config generation matches ({live_gen})")]
    return [_fail(
        cid,
        f"[{ws.name}] proxy config generation {live_gen} != last pushed "
        f"{applied_gen} -- the proxy holds a config credproxy didn't push",
        f"credproxy workspace {ws.name} apply")]


def _runc_keep_id_check(ws: Workspace, cfg: dict) -> list[Check]:
    """Predict the runc `sysfs` failure (#50): on rootless podman with the `runc`
    OCI runtime, a config that emits `--userns=keep-id` (map_host_user + non-root
    user, no `run_flags --userns` override) combines with the always-present
    netns join to fail the workspace container at init -- runc refuses the fresh
    read-only sysfs mount when keep-id's userns doesn't own the joined netns
    (crun handles it fine). FAIL loudly with both remedies so a green doctor
    really means `start` will get past container init.

    Emits NOTHING on every not-bad case -- Docker, crun, rootful podman,
    map_host_user off, or a hand-rolled --userns. `emits_keep_id` already gates
    on rootless podman + credproxy-owned keep-id; we add the runtime==runc check.
    Both read the SAME cached runtime probe doctor's env checks already ran, so
    no extra docker round-trip."""
    from . import lifecycle
    from .runtime import oci_runtime
    if not lifecycle.emits_keep_id(cfg) or oci_runtime() != "runc":
        return []
    return [_fail(
        f"ws:{ws.name}:runc-sysfs",
        f"[{ws.name}] rootless podman with runc + map_host_user will fail the "
        f"workspace container at init (sysfs mount under --userns=keep-id on the "
        f"shared-netns join)",
        'switch podman to crun (~/.config/containers/containers.conf -> '
        '[engine] runtime = "crun") or set `map_host_user = false` in the '
        "workspace TOML; see docs/troubleshooting.md")]


def _script_compile_checks(ws: Workspace) -> list[Check]:
    """Upgrade the binding-layer script-existence probe to a real COMPILE for each
    binding whose injector is scripted -- but only when the proxy Starlark runtime
    imports on-host (no docker, no venv required for doctor's other checks). When
    it doesn't import, emit a single skip-with-note pointing at `script check`
    rather than failing (doctor must degrade gracefully)."""
    from . import scriptcheck
    from ..model.injectors import find_injector
    from ..model.resolver import resolve_workspace
    from ..model.scripts import find_script

    try:
        bindings = resolve_workspace(ws).bindings
    except (CredproxyError, tomllib.TOMLDecodeError, OSError):
        return []  # the :bindings check already reported the parse/validate failure

    # Distinct scripted injectors referenced by this workspace's bindings.
    scripted: dict[str, object] = {}
    for b in bindings:
        try:
            inj = find_injector(b.injector)
        except CredproxyError:
            continue  # :binding[i]:injector already flagged it
        if inj.scheme == "script" and inj.script:
            scripted.setdefault(inj.script, inj)
    if not scripted:
        return []

    if not scriptcheck.starlark_importable():
        return [Check(f"ws:{ws.name}:scripts", True,
                      f"[{ws.name}] {len(scripted)} scripted injector(s) resolve; "
                      f"compile skipped (Starlark runtime not importable on-host)",
                      "run `credproxy script check` for a full compile "
                      "(on-host with the proxy deps, or in the image)")]

    out: list[Check] = []
    for script_name, inj in scripted.items():
        cid = f"ws:{ws.name}:script:{script_name}"
        try:
            source = find_script(inj.script).source
        except CredproxyError as e:
            out.append(_fail(cid, f"[{ws.name}] script '{script_name}': {e}"))
            continue
        err = scriptcheck.compile_injector_paired(inj, source)
        out.append(_ok(cid, f"[{ws.name}] script '{script_name}' compiles")
                   if err is None else
                   _fail(cid, f"[{ws.name}] script '{script_name}' fails to compile: {err}"))
    return out


def _binding_checks(ws: Workspace, *, fetch: bool) -> tuple[list[Check], dict]:
    """Binding checks in two layers, so a run both reports EVERY problem and
    upholds the "doctor passes => start passes" contract:

    1. Independent per-binding probes (injector resolves, provider resolves, each
       host glob valid) off the raw TOML -- report-all, so one broken binding
       doesn't hide the next one's problems.
    2. The real aggregate `load_bindings(ws)` -- the SAME parse+validate `start`
       runs. This catches what the shallow probes structurally can't: missing
       required fields, duplicate names, secret slots that don't match the scheme,
       (host, wire-location) collisions, a scripted injector naming a missing
       `.star`. First-error, but that's the action-time behavior we're mirroring.

    `fetch` (opt-in) additionally resolves each secret via its provider; it reuses
    the layer-2 parse, so it means only "also fetch", never a different verdict.

    Returns `(checks, fetched)` where `fetched` maps `{(provider, ref): ok}` for
    every binding secret this run already fetched -- the preset-requires layer
    reads it to avoid re-invoking a provider it just called (finding 4). Empty
    unless `fetch`."""
    out: list[Check] = []
    fetched: dict[tuple[str | None, str | None], bool] = {}
    try:
        raw = tomllib.loads(ws.config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        return ([_fail(f"ws:{ws.name}:toml", f"[{ws.name}] TOML parse: {e}")],
                fetched)
    bindings = raw.get("binding") or []
    if not isinstance(bindings, list):
        return ([_fail(f"ws:{ws.name}:bindings",
                       f"[{ws.name}] `binding` must be an array")], fetched)

    _probe_bindings_raw(ws, bindings, out)

    # Layer 2: the authoritative parse+validate. Reuse its result for --fetch so a
    # parse failure can't yield a different exit code with vs. without --fetch.
    # Goes through `resolve_workspace` (config-v2), so preset-EXPANDED bindings are
    # validated + fetched as ordinary bindings -- no special-casing.
    from ..model.bindings import test_bindings
    from ..model.resolver import resolve_workspace
    bs = None
    try:
        bs = resolve_workspace(ws).bindings
        out.append(_ok(f"ws:{ws.name}:bindings",
                       f"[{ws.name}] {len(bs)} binding(s) pass static checks"))
    except (CredproxyError, tomllib.TOMLDecodeError, OSError) as e:
        # load_bindings does a raw tomllib.loads (TOMLDecodeError isn't a
        # CredproxyError); the top-of-function probe already returns on a parse
        # error, but stay defensive against a between-reads change.
        out.append(_fail(f"ws:{ws.name}:bindings", f"[{ws.name}] bindings: {e}"))

    if fetch and bs is not None:
        results = test_bindings(bs)
        for r in results:
            out.append(_ok(f"ws:{ws.name}:{r.name}:fetch",
                           f"[{ws.name}] {r.name}: secret resolved ({r.value_len} chars)")
                       if r.ok else
                       _fail(f"ws:{ws.name}:{r.name}:fetch", f"[{ws.name}] {r.name}: {r.error}"))
        # Record each fetched (provider, first-ref) -> ok so the preset-requires
        # layer can reuse the outcome rather than re-invoking the provider
        # (finding 4). `test_bindings` keeps input order, so zip is 1:1.
        for b, r in zip(bs, results):
            fetched[(b.provider, _secret_ref(b.secret))] = r.ok
    return out, fetched


def _probe_bindings_raw(ws: Workspace, bindings: list, out: list[Check]) -> None:
    """Layer-1 report-all probes. Check ids are qualified by binding INDEX (not
    the human name, which may be absent or duplicated) plus a host index, so no
    two failures ever share an id -- a `--json` consumer keying by id can't
    silently drop one. The human `name` still rides in the message."""
    from ..model.injectors import find_injector
    from ..providers import find_provider
    for i, b in enumerate(bindings):
        bid = f"ws:{ws.name}:binding[{i}]"
        if not isinstance(b, dict):
            out.append(_fail(bid, f"[{ws.name}] binding[{i}] is not a table"))
            continue
        label = b.get("name") or f"binding[{i}]"
        inj = b.get("injector")
        if isinstance(inj, str) and inj:
            try:
                find_injector(inj)
            except CredproxyError as e:
                out.append(_fail(f"{bid}:injector", f"[{ws.name}] {label}: {e}"))
        prov = b.get("provider")
        if isinstance(prov, str) and prov:
            try:
                find_provider(prov)
            except CredproxyError as e:
                out.append(_fail(f"{bid}:provider", f"[{ws.name}] {label}: {e}"))
        hosts = b.get("hosts")
        if isinstance(hosts, list):
            for j, h in enumerate(hosts):
                if isinstance(h, str) and hostmatch.is_pattern(h):
                    err = hostmatch.validate_pattern(h)
                    if err:
                        out.append(_fail(f"{bid}:host[{j}]", f"[{ws.name}] {label}: {err}"))


def _preset_requires_checks(ws: Workspace, *, fetch: bool,
                            fetched_refs: dict | None = None) -> list[Check]:
    """Re-run each referenced preset's declarative `[[requires]]` host-prereq
    checks (#58) -- the authoritative side (`preset add`/`create` are advisory).

    Discovery is via the workspace's `[[preset]]` references + the lock (config-v2),
    not provenance comments. For each still-resolvable pack, re-run its checks; a
    reference naming a pack that no longer resolves in the registry is a
    skip-with-note (`ok=True`). A pack whose lock snapshot's `definition_rev` no
    longer matches the current definition surfaces a passing note (run
    `preset refresh`).

    The `provider` kind checks the provider CHOSEN at reference time, recovered from
    the pack's lock snapshot (or the ref's own provider). A `fetch=true` check runs
    only under `doctor NAME --fetch`; without it it degrades to a resolve-only
    provider check -- so a nameless `doctor` scan-all never invokes a provider."""
    from ..model import prereqs
    from ..model.lock import load_lock
    from ..model.presets import (
        load_presets, parse_preset_refs, resolve_preset_credential,
        resolve_requires_for_check,
    )

    source = str(ws.config_path)
    try:
        raw = tomllib.loads(ws.config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    try:
        refs = parse_preset_refs(raw, source)
    except CredproxyError:
        return []   # the :bindings check already reported the malformed reference
    if not refs:
        return []

    # `load_presets()` raises `ConfigError` on the FIRST unparseable registry
    # `.toml` -- report it as a failing check rather than aborting the sweep.
    try:
        presets = load_presets()
    except CredproxyError as e:
        return [_fail(f"ws:{ws.name}:presets:load",
                      f"[{ws.name}] preset registry failed to load: {e}",
                      "fix or remove the malformed preset file in the registry")]

    lock_presets = load_lock(ws).get("presets", {})

    out: list[Check] = []
    for ref in refs:
        pack = ref.name
        spec = presets.get(pack)
        if spec is None:
            out.append(Check(
                f"ws:{ws.name}:preset:{pack}", True,
                f"[{ws.name}] preset '{pack}' is referenced but no longer resolves "
                f"in the registry -- prerequisite checks skipped",
                "the reference still works from the lock snapshot; reinstall the "
                "pack to re-check its prerequisites"))
            continue

        entry = lock_presets.get(pack) if isinstance(lock_presets, dict) else None
        if isinstance(entry, dict) and entry.get("definition_rev") != spec.rev:
            out.append(_ok(
                f"ws:{ws.name}:preset:{pack}:changed",
                f"[{ws.name}] preset '{pack}' definition changed since the lock "
                f"snapshot -- run `credproxy workspace {ws.name} preset refresh`"))

        if not spec.requires:
            continue

        provider, secret = _ref_credential(spec, ref, entry)
        # Options feeding a requires path live in the intent file (`[preset.options]`)
        # or default -- always recoverable, so no skip-with-note is needed for them.
        resolved_requires, skipped = resolve_requires_for_check(spec, ref.options)
        for j, rq in enumerate(skipped):
            out.append(Check(
                f"ws:{ws.name}:preset:{pack}:requires-opt[{j}]", True,
                f"[{ws.name}] preset '{pack}' requires ({rq.kind}): path option "
                f"'{rq.path_option}' has no value or default -- check skipped",
                f"supply it in the `[preset.options]` of the `[[preset]]` block "
                f"(or give the option a default)"))
        results = prereqs.evaluate(resolved_requires, provider=provider,
                                   secret=_secret_ref(secret), do_fetch=fetch,
                                   fetched_refs=fetched_refs)
        for i, r in enumerate(results):
            cid = f"ws:{ws.name}:preset:{pack}:requires[{i}]"
            msg = f"[{ws.name}] preset '{pack}' requires ({r.kind}): {r.detail}"
            out.append(_ok(cid, msg) if r.ok else Check(cid, False, msg, r.hint))
    return out


def _ref_credential(spec, ref, entry) -> tuple[str | None, object]:
    """The (provider, secret) a preset reference resolves to: the lock snapshot's
    first binding (authoritative -- what was recorded at reference time), else the
    ref's own provider/secret with pack defaults applied. (None, None) for a pack
    with no bindings."""
    from ..model.presets import resolve_preset_credential
    if isinstance(entry, dict):
        for b in entry.get("expansion", {}).get("bindings", []):
            return b.get("provider"), b.get("secret")
    provider, secret, _missing = resolve_preset_credential(
        spec, ref.provider, ref.secret)
    return provider, secret



def _secret_ref(secret) -> str | None:
    """A single secret ref for a provider test-fetch. A binding's secret is a
    bare ref (str) or a {slot: ref} map; the provider check just needs one ref to
    prove the provider serves the credential, so pick the first."""
    if isinstance(secret, str):
        return secret
    if isinstance(secret, dict) and secret:
        return next(iter(secret.values()))
    return None


def _rule_checks(ws: Workspace) -> list[Check]:
    """The credential-free `[[rule]]` layer runs its own parse+validate at `start`
    (`load_rules -> rules.validate`: missing/duplicate names, bad path glob, unknown
    script, action-field errors). Mirror the layer-2 bindings check so a broken rule
    is caught by doctor too, not one PR later at `start`."""
    from ..model.rules import load_rules
    try:
        rs = load_rules(ws)
        return [_ok(f"ws:{ws.name}:rules", f"[{ws.name}] {len(rs)} rule(s) valid")]
    except (CredproxyError, tomllib.TOMLDecodeError, OSError) as e:
        # load_rules does a raw tomllib.loads, which raises TOMLDecodeError (not a
        # CredproxyError) on a malformed file -- catch it so doctor reports the
        # broken workspace instead of crashing on the command whose whole job is
        # to report failures cleanly. (`:config`/`:toml` already flag the parse
        # error; this keeps the sweep going.)
        return [_fail(f"ws:{ws.name}:rules", f"[{ws.name}] rules: {e}")]
