"""A small, supported harness for unit-testing injection scripts and rules.

Overlay authors ship `.star` scripts (scripted injectors and rule scripts) and
need a way to test them the way the proxy actually runs them -- with the real
manifest+script pairing, not a hand-built `ScriptedScheme(...)` that re-declares
`family`/`slots`/`location` and can silently drift from the injector TOML. This
module is that harness. It builds each scheme through the SAME path the wire/push
loader uses (`core.model.injectors.find_injector` + `core.model.scripts.find_script` for the
metadata + source; `schemes.SCHEMES` for a built-in), so a manifest that
disagrees with its script fails the test rather than a hand-rolled test passing
against a config the proxy would reject.

Usage from a pytest file (drop it in `<overlay>/tests/`)::

    import testkit

    def test_my_injector_signs():
        kit = testkit.load_injector("my-injector")
        req = testkit.make_request("GET", "https://api.example.com/v1/me")
        result = kit.run(req, {"api_key": "REAL"})
        assert result.injected
        assert req.headers["Authorization"] == "..."   # observe the mutation

    def test_my_rule_blocks():
        script = testkit.load_rule_script("my-guard")
        req = testkit.make_request("DELETE", "https://api.example.com/things/1")
        outcome = testkit.run_rule(script, req)
        assert outcome.blocked and outcome.response.status == 403

`make_request` hides the two footguns every hand-rolled test open-codes: it wraps
`mitmproxy.test.tutils.treq`, strips treq's default header set (which carries a
bogus `content-length`), and sets `Host` from the URL so `pretty_host` (what the
proxy host-scopes and scripts sign over) is correct.

**Cross-package import.** `load_injector`/`load_rule_script` need
`credproxy_cli.core`, which is NOT on the proxy's PYTHONPATH: on-host the CLI
lives at `<repo>/cli`, and in the proxy image it is bind-mounted read-only at
`/opt/cli` (the `dev test` container path). Both are `<this file>/../../cli`, so
we add that to `sys.path` lazily inside the functions that need it. Nothing in
the proxy *runtime* imports this module, so a runtime image with no `cli/` mount
is unaffected -- the CLI dep is only touched when a test actually loads a script.

**API versioning is intentionally deferred.** The harness builds against the
current script primitive `api` (1); revisit once `starlark_runtime.API_VERSION`
first bumps and a test needs to pin an older surface.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _ensure_cli_importable() -> None:
    """Put the CLI package dir on sys.path (idempotent). `<this file>/../../cli`
    resolves to `<repo>/cli` on-host and `/opt/cli` in the proxy image (both mount
    `proxy/` one level under the repo/`/opt` root)."""
    cli_dir = Path(__file__).resolve().parents[1] / "cli"
    p = str(cli_dir)
    if cli_dir.is_dir() and p not in sys.path:
        sys.path.insert(0, p)


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


def make_request(method: str, url: str, headers: dict | None = None,
                 body: bytes | str = b""):
    """Build a mitmproxy request for a scheme/rule under test.

    Wraps `tutils.treq` but hides its footguns: treq seeds a default header set
    (including a stale `content-length: 7`) that every hand-rolled test has to
    `.clear()`, and it does not set a `Host` header, so `pretty_host` -- what the
    proxy host-scopes on and what a sign script signs over -- would be wrong. This
    strips the defaults and sets `Host` from `url`.

    `url`'s host becomes both the destination host and the `Host` header; pass a
    `Host` in `headers` to override the header alone (e.g. to reproduce transparent
    mode, where the destination is an IP but the Host header is the real name).
    `body` may be bytes or str.
    """
    from mitmproxy.test import tutils

    parts = urlsplit(url)
    host = parts.hostname or ""
    scheme = parts.scheme or "https"
    port = parts.port or (443 if scheme == "https" else 80)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    if isinstance(method, str):
        method = method.encode()
    if isinstance(body, str):
        body = body.encode()

    req = tutils.treq(host=host, port=port, method=method,
                      scheme=scheme.encode(), path=target.encode(), content=body)
    req.headers.clear()          # strip treq's default header + bogus content-length
    req.headers["Host"] = host
    for k, v in (headers or {}).items():
        req.headers[k] = v
    return req


def make_response(req, status: int = 200, body: bytes | str = b"",
                  headers: dict | None = None):
    """Build a mitmproxy flow carrying `req` plus a response, for a response-phase
    rule (`run_rule_response`) or a re-seal injector (`InjectorHarness.run_response`)
    under test.

    Wraps `tutils.tresp` but hides the same footgun `make_request` does: tresp
    seeds a default response header set (a `header-response` and a stale
    `content-length: 7` over a `message` body) that every hand-rolled test has to
    `.clear()`. This strips those defaults, sets `status`/`body`, and applies
    `headers`, then returns the flow -- `.request` is the `req` you pass,
    `.response` the response just built (what a `RuleResponseCtx`/`ResponseCtx`
    wraps). `body` may be bytes or str."""
    from mitmproxy.test import tflow, tutils

    if isinstance(body, str):
        body = body.encode()
    resp = tutils.tresp(status_code=int(status), content=body)
    resp.headers.clear()         # strip tresp's default headers + bogus content-length
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return tflow.tflow(req=req, resp=resp)


# --------------------------------------------------------------------------
# Injectors
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InjectorResult:
    """The outcome of one `on_request`. `injected` is the scheme's boolean return
    (True iff it changed the request); the mutated request is observed via the
    `req` the caller still holds (also echoed here as `request`)."""
    injected: bool
    request: object


@dataclass(frozen=True)
class MintRecord:
    """One `mint()` a re-seal injector's `on_response` performed, as captured by
    the harness's recording minter. `value` is the runtime-derived secret (the
    minted token), `ttl` its lifetime (None = never expires), `api_hosts` the
    hosts the swap was registered on, `header` the API-host header the workspace
    presents it in, and `placeholder` the deterministic sentinel the harness
    handed back (which the script writes into the body in place of `value`)."""
    value: str
    ttl: float | None
    api_hosts: tuple
    header: str
    placeholder: str


class _RecordingMinter:
    """A fake `config.RuntimeMinter` for `InjectorHarness.run_response`.

    The real minter couples to `BindingCredentials`/config (registering a runtime
    bearer swap per API host, emitting an audit event) -- engine internals with
    their own upstream tests (`tests/test_reseal.py`). The harness's job is
    narrower: let an overlay author assert what THEIR script minted (value, ttl,
    hosts, header) and how it rewrote the body, without dragging config in. So this
    only implements `mint(value, ttl, api_hosts, header)` -- the exact surface
    `ResponseCtx.mint`/`mint_into_json` call -- returns a deterministic placeholder
    (`minted-1`, `minted-2`, ...), and records a `MintRecord`. It does NOT enforce
    the engine's non-empty-`api_hosts` / finite-TTL guards (those are
    RuntimeMinter's, tested there), so a script can be driven through edge cases."""

    def __init__(self):
        self.records: list[MintRecord] = []

    def mint(self, value: str, ttl, api_hosts, header: str = "Authorization") -> str:
        placeholder = f"minted-{len(self.records) + 1}"
        self.records.append(MintRecord(
            value=value, ttl=ttl, api_hosts=tuple(api_hosts or ()),
            header=header, placeholder=placeholder,
        ))
        return placeholder


@dataclass(frozen=True)
class InjectorResponseResult:
    """The outcome of one `on_response`. `handled` is the scheme's boolean return
    (True iff it acted on the response); `flow` is the flow whose response the
    scheme mutated (body rewritten to carry the placeholder, etc.); `mints` is the
    ordered list of `MintRecord`s the recording minter captured (empty if the
    scheme minted nothing)."""
    handled: bool
    flow: object
    mints: list


class InjectorHarness:
    """A resolved injector ready to drive: the built scheme plus the merged
    manifest params. `run` constructs the exact `RequestCtx` the proxy would and
    calls `on_request`; `run_response` the `ResponseCtx` (with a recording fake
    minter) and calls `on_response`."""

    def __init__(self, name: str, scheme, params: dict, slots: tuple[str, ...]):
        self.name = name
        self.scheme = scheme
        self.params = params
        self.slots = slots

    def _check_slots(self, secrets: dict) -> None:
        """Enforce that `secrets`' keys equal the scheme's declared slots (the same
        check `config.load_resolved` makes at push), so a manifest that declares
        different slots than the script/test expects fails HERE rather than passing
        a test the proxy would reject."""
        want = set(self.slots)
        got = set(secrets)
        if got != want:
            raise ValueError(
                f"injector '{self.name}' declares secret slot(s) "
                f"{{{', '.join(sorted(want))}}}, but the test provided "
                f"{{{', '.join(sorted(got))}}} -- manifest/script slots disagree"
            )

    def run(self, req, secrets: dict, params: dict | None = None,
            placeholder: str | None = None) -> InjectorResult:
        """Run `on_request` against `req`. `secrets` is a slot->value map -- its
        keys must equal the scheme's declared slots (the same check
        `config.load_resolved` enforces at push), so a manifest that declares
        different slots than the script/test expects fails HERE rather than
        passing a test the proxy would reject. `params` defaults to the injector's
        merged manifest params; `placeholder` is the inert sentinel the workspace
        would present (None for a sign-family default).

        The request phase is minter-less, mirroring the proxy: `mint()`/
        `mint_into_json()` are response-phase-only, so a scripted injector that
        calls them in `on_request` fails closed (`injected=False` -- the runtime
        swallows request-phase hook errors, it does NOT raise); use `run_response`
        for the mint path."""
        import schemes

        self._check_slots(secrets)
        ctx = schemes.RequestCtx(
            req, dict(secrets),
            self.params if params is None else params,
            placeholder,
        )
        injected = self.scheme.on_request(ctx)
        return InjectorResult(injected=bool(injected), request=req)

    def run_response(self, flow, secrets: dict, params: dict | None = None,
                     placeholder: str | None = None) -> InjectorResponseResult:
        """Run `on_response` against `flow` and return its outcome -- the runner
        for the re-seal family's `mint` path.

        Constructs the exact `ResponseCtx` the proxy would, but with a RECORDING
        fake minter (`_RecordingMinter`) in place of the real `config.RuntimeMinter`
        so the test asserts what the script minted (value/ttl/hosts/header, on
        `result.mints`) and how it rewrote the body (on `flow.response`), without
        dragging `BindingCredentials`/config in. `secrets` slot validation matches
        `run` (a manifest/script slot disagreement fails here); `params` defaults to
        the merged manifest params; `placeholder` is the request-time sentinel.
        Build `flow` with `make_response`."""
        import schemes

        self._check_slots(secrets)
        minter = _RecordingMinter()
        ctx = schemes.ResponseCtx(
            flow, dict(secrets),
            self.params if params is None else params,
            placeholder, minter=minter,
        )
        handled = self.scheme.on_response(ctx)
        return InjectorResponseResult(handled=bool(handled), flow=flow,
                                      mints=list(minter.records))


def load_injector(name: str) -> InjectorHarness:
    """Resolve an injector by name through the layered registry and build its
    scheme exactly like the push/wire path does.

    For a scripted injector (`scheme = "script"`) this pairs the manifest
    (family/slots/location/api, via `find_injector`) with its `.star` source (via
    `find_script`) and compiles a `ScriptedScheme` -- so a manifest that disagrees
    with the script is caught. For a built-in scheme it returns the same singleton
    from `schemes.SCHEMES` the proxy dispatches on. Uniform either way."""
    _ensure_cli_importable()
    from credproxy_cli.core.model.injectors import find_injector
    import schemes

    injector = find_injector(name)
    spec = injector.spec
    if injector.scheme == "script":
        from credproxy_cli.core.model.scripts import find_script
        from starlark_runtime import ScriptedScheme

        source = find_script(injector.script).source
        scheme = ScriptedScheme(
            injector.script, source,
            family=spec.family, slots=tuple(spec.slots),
            location_kind=spec.location_kind, header_default=spec.header_default,
        )
    else:
        scheme = schemes.SCHEMES[injector.scheme]
    return InjectorHarness(name, scheme, dict(injector.params), tuple(spec.slots))


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleOutcome:
    """The outcome of running a rule script's request phase. `response` is the
    synthetic response a terminal `block()`/`respond()` set (None if the rule only
    rewrote or did nothing); rewrites are observed on the `request` the caller
    holds."""
    request: object
    response: object  # rules.SyntheticResponse | None

    @property
    def terminal(self) -> bool:
        return self.response is not None

    @property
    def blocked(self) -> bool:
        return self.response is not None and self.response.kind == "block"


def load_rule_script(name: str) -> object:
    """Resolve a `.star` rule script by name and compile it under the `kind="rule"`
    Starlark profile (restricted primitives, no secret/mint/crypto, plus
    `block`/`respond`). A script that references a forbidden primitive fails to
    compile HERE -- the same construction the proxy runs at push."""
    _ensure_cli_importable()
    from credproxy_cli.core.model.scripts import find_script
    from starlark_runtime import ScriptedScheme

    source = find_script(name).source
    return ScriptedScheme(name, source, kind="rule")


def run_rule(script, req, params: dict | None = None) -> RuleOutcome:
    """Drive a rule script's REQUEST phase against `req` and return its outcome.

    Builds the same `RuleRequestCtx` the addon uses (empty secret map, the
    `block`/`respond` sinks, the Host/:authority rewrite guard), binds this rule's
    `params` (what `param()` reads), runs `on_request`, and reports whether a
    terminal response was set. Rewrites land on `req`. A script error raises
    (rule scripts fail closed toward the policy, unsanitized) -- assert on that
    with `pytest.raises`."""
    from rules import RuleRequestCtx

    ctx = RuleRequestCtx(req)
    ctx.params = dict(params or {})
    script.on_request(ctx)
    return RuleOutcome(request=req, response=ctx.pending)


@dataclass(frozen=True)
class RuleResponseOutcome:
    """The outcome of running a rule script's RESPONSE phase. `response` is the
    synthetic response a terminal `block()`/`respond()` set (None if the rule only
    rewrote the response body/headers or did nothing); in-place response mutations
    (a scrubbed body, a stripped header) are observed on `flow.response`."""
    flow: object
    response: object  # rules.SyntheticResponse | None

    @property
    def terminal(self) -> bool:
        return self.response is not None

    @property
    def blocked(self) -> bool:
        return self.response is not None and self.response.kind == "block"


def run_rule_response(script, flow, params: dict | None = None) -> RuleResponseOutcome:
    """Drive a rule script's RESPONSE phase against `flow` and return its outcome.

    Builds the same `RuleResponseCtx` the addon uses (no secret, no minter, the
    `block`/`respond` sinks), binds this rule's `params` (what `param()` reads),
    runs `on_response`, and reports whether a terminal response was set. Body/header
    rewrites land on `flow.response`. A script error raises (rule scripts fail
    closed toward the policy, unsanitized) -- assert on that with `pytest.raises`.
    Build `flow` with `make_response`."""
    from rules import RuleResponseCtx

    ctx = RuleResponseCtx(flow)
    ctx.params = dict(params or {})
    script.on_response(ctx)
    return RuleResponseOutcome(flow=flow, response=ctx.pending)
