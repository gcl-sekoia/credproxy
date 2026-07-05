"""A small, supported harness for unit-testing injection scripts and rules.

Overlay authors ship `.star` scripts (scripted injectors and rule scripts) and
need a way to test them the way the proxy actually runs them -- with the real
manifest+script pairing, not a hand-built `ScriptedScheme(...)` that re-declares
`family`/`slots`/`location` and can silently drift from the injector TOML. This
module is that harness. It builds each scheme through the SAME path the wire/push
loader uses (`core.injectors.find_injector` + `core.scripts.find_script` for the
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


class InjectorHarness:
    """A resolved injector ready to drive: the built scheme plus the merged
    manifest params. `run` constructs the exact `RequestCtx` the proxy would and
    calls `on_request`."""

    def __init__(self, name: str, scheme, params: dict, slots: tuple[str, ...]):
        self.name = name
        self.scheme = scheme
        self.params = params
        self.slots = slots

    def run(self, req, secrets: dict, params: dict | None = None,
            placeholder: str | None = None) -> InjectorResult:
        """Run `on_request` against `req`. `secrets` is a slot->value map -- its
        keys must equal the scheme's declared slots (the same check
        `config.load_resolved` enforces at push), so a manifest that declares
        different slots than the script/test expects fails HERE rather than
        passing a test the proxy would reject. `params` defaults to the injector's
        merged manifest params; `placeholder` is the inert sentinel the workspace
        would present (None for a sign-family default)."""
        import schemes

        want = set(self.slots)
        got = set(secrets)
        if got != want:
            raise ValueError(
                f"injector '{self.name}' declares secret slot(s) "
                f"{{{', '.join(sorted(want))}}}, but the test provided "
                f"{{{', '.join(sorted(got))}}} -- manifest/script slots disagree"
            )
        ctx = schemes.RequestCtx(
            req, dict(secrets),
            self.params if params is None else params,
            placeholder,
        )
        injected = self.scheme.on_request(ctx)
        return InjectorResult(injected=bool(injected), request=req)


def load_injector(name: str) -> InjectorHarness:
    """Resolve an injector by name through the layered registry and build its
    scheme exactly like the push/wire path does.

    For a scripted injector (`scheme = "script"`) this pairs the manifest
    (family/slots/location/api, via `find_injector`) with its `.star` source (via
    `find_script`) and compiles a `ScriptedScheme` -- so a manifest that disagrees
    with the script is caught. For a built-in scheme it returns the same singleton
    from `schemes.SCHEMES` the proxy dispatches on. Uniform either way."""
    _ensure_cli_importable()
    from credproxy_cli.core.injectors import find_injector
    import schemes

    injector = find_injector(name)
    spec = injector.spec
    if injector.scheme == "script":
        from credproxy_cli.core.scripts import find_script
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
    from credproxy_cli.core.scripts import find_script
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
