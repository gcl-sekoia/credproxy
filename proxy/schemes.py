"""Injection schemes: the typed, scheme-aware request transforms.

A *scheme* is the proxy-side mechanism that turns a credential into an
outbound request. design-v3 splits schemes into two families:

  - **substitute** — the workspace holds an inert placeholder and sends it;
    the scheme finds it in its wire location and swaps in the real value,
    decoding/re-encoding as the location dictates (`bearer`, `basic`, `body`).
  - **sign** — no usable static value on the wire; the scheme holds a signing
    key and computes auth material per request (`sigv4`, … — added later).

Every scheme — built-in here, or a sandboxed Starlark script later — is
expressed against ONE interface so the two are interchangeable:

  - `on_request(ctx)`  — mutate the outbound request. Returns True if it
    actually changed the request (used only for logging).
  - `on_response(ctx)` — *optional*; mutate the response. Plumbed from day one
    (see addon.response) but a no-op until the re-seal schemes use it.

Schemes never touch the real secret directly: they read it through
`ctx.secret(slot)`, the single door to the resolved value. The crypto and
encoding primitives (the correctness-sensitive code) live here, owned and
trusted; schemes only orchestrate them. That keeps "we own the crypto" while
leaving composition open to scripts.

`SCHEMES` is the registry the config loader dispatches on. Each scheme
declares its `slots` (the secret slot names it consumes); the substitute
family is single-slot ("value"). Adding a scheme is adding one entry here
plus a matching `SchemeSpec` in the CLI's `core/schemes.py` catalog.
"""
from __future__ import annotations

import base64
from typing import Protocol


class RequestCtx:
    """The trusted surface a scheme acts through.

    Wraps a mitmproxy request plus this binding's resolved secret slots and
    scheme params. A scheme can read/modify the request only via these
    primitives and reach the real value only via `secret()`; it never sees the
    mitmproxy flow object directly (this mirrors the OpaquePythonObject the
    Starlark escape hatch will hand scripts later).
    """

    def __init__(self, request, secrets: dict[str, str], params: dict,
                 placeholder: str | None):
        self._req = request
        self._secrets = secrets
        self.params = params
        self.placeholder = placeholder

    # -- the only door to the resolved credential --
    def secret(self, slot: str = "value") -> str:
        try:
            return self._secrets[slot]
        except KeyError:
            raise KeyError(f"no secret slot {slot!r} (have {sorted(self._secrets)})")

    # -- header primitives --
    def header_get(self, name: str) -> str | None:
        return self._req.headers.get(name)

    def header_set(self, name: str, value: str) -> None:
        self._req.headers[name] = value

    # -- body primitives (text view handles content-encoding transparently) --
    def body_text(self) -> str | None:
        return self._req.text

    def set_body_text(self, text: str) -> None:
        self._req.text = text

    # -- encoding primitives --
    @staticmethod
    def b64encode(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    @staticmethod
    def b64decode(s: str) -> bytes:
        return base64.b64decode(s)


class Scheme(Protocol):
    name: str
    family: str
    slots: tuple[str, ...]

    def on_request(self, ctx: RequestCtx) -> bool: ...
    def on_response(self, ctx: RequestCtx) -> bool: ...


class _SubstituteScheme:
    """Shared base for the placeholder-driven family: single `value` slot,
    no response phase."""

    family = "substitute"
    slots = ("value",)

    def on_response(self, ctx: RequestCtx) -> bool:  # noqa: D401 - no-op seam
        return False


class BearerScheme(_SubstituteScheme):
    """Substring-swap the placeholder for the real value inside a named header
    (default `Authorization`). The surrounding format (`Bearer `, `token `, …)
    is already on the wire — the client built the header — so we replace only
    the placeholder substring, never the whole value."""

    name = "bearer"

    def on_request(self, ctx: RequestCtx) -> bool:
        header = ctx.params.get("header", "Authorization")
        value = ctx.header_get(header)
        if value is None or ctx.placeholder is None or ctx.placeholder not in value:
            return False
        ctx.header_set(header, value.replace(ctx.placeholder, ctx.secret()))
        return True


class BasicScheme(_SubstituteScheme):
    """HTTP Basic decode-and-swap: decode `Authorization: Basic`, replace the
    component equal to the placeholder with the real value, re-encode.

    The placeholder is a BARE token (no hand-computed base64). We swap the
    password component by default — design-v3's decision — but also accept the
    placeholder in the username position, since some services (e.g. GitHub git
    over HTTPS) put the token there with a dummy password. The other component
    comes straight from the wire, so no username config is needed."""

    name = "basic"

    def on_request(self, ctx: RequestCtx) -> bool:
        header = ctx.params.get("header", "Authorization")
        value = ctx.header_get(header)
        if value is None or ctx.placeholder is None:
            return False
        prefix = "Basic "
        if not value.startswith(prefix):
            return False
        try:
            user, sep, pw = ctx.b64decode(value[len(prefix):].strip()) \
                .decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        if sep != ":":
            return False
        if pw == ctx.placeholder:
            pw = ctx.secret()
        elif user == ctx.placeholder:
            user = ctx.secret()
        else:
            return False
        ctx.header_set(header, prefix + ctx.b64encode(f"{user}:{pw}".encode("utf-8")))
        return True


class BodyScheme(_SubstituteScheme):
    """Substring-swap the placeholder for the real value anywhere in the
    request body — for credentials carried in form/JSON bodies (OAuth2
    client-credentials `client_secret=…`, key-in-body APIs). The text view
    transparently handles content-encoding."""

    name = "body"

    def on_request(self, ctx: RequestCtx) -> bool:
        text = ctx.body_text()
        if not text or ctx.placeholder is None or ctx.placeholder not in text:
            return False
        ctx.set_body_text(text.replace(ctx.placeholder, ctx.secret()))
        return True


SCHEMES: dict[str, Scheme] = {
    s.name: s for s in (BearerScheme(), BasicScheme(), BodyScheme())
}
