"""PostgreSQL credential-injecting connection broker (the second listener).

Postgres is not HTTP, so it cannot ride mitmproxy: the wire is binary, TLS is a
STARTTLS-style upgrade (so there is no SNI/decision point before the pre-auth
plaintext exchange), and SCRAM means the password never crosses the wire to be
substituted. So credproxy brokers it instead of intercepting it -- the workspace
*explicitly dials* ``proxy.local:5432`` and the broker re-originates the
connection to the real database with an injected credential.

Mechanism: **terminate-and-re-originate**. The broker understands only the
startup/SSL/auth handshake; once the server leg is authenticated it becomes a
bidirectional byte-pump, so every Postgres feature (extended query, COPY,
LISTEN/NOTIFY, prepared statements) works without the broker parsing a single
query.

    client leg (workspace)                          server leg (real DB)
    ----------------------                          --------------------
    StartupMessage user=<binding name>   --.
                                            |  select binding by the `user` field
                                            |  connect host:port, optional TLS
                                            '->  StartupMessage user=<real user>
                                                 cleartext / MD5 / SCRAM auth
    AuthenticationOk                     <--'    AuthenticationOk
    ParameterStatus* (relayed)           <----   ParameterStatus*
    BackendKeyData (pid/secret FAKED)    <----   BackendKeyData (real pid/secret)
    ReadyForQuery                        <----   ReadyForQuery
    <=================== raw byte-pump both directions ===================>

**Trust legs are asymmetric on purpose.** The client leg is trust-accept (no
password demanded from the workspace -- it holds only the binding name, exactly
the inert-placeholder trust domain the HTTP side uses). The server leg does a
genuine auth handshake with the *real* credential and defaults to TLS
verify-full (``pg.DEFAULT_SSLMODE``): the broker originates a real, credentialed
session, so an unverified server leg is an active-MITM hole with a bigger blast
radius than a wrong password.

**Query cancellation** is the sharp edge. A CancelRequest arrives on a *new*
connection with NO startup packet and NO user field, so the binding selector
does not apply. The broker fabricates its own ``(pid, secret)`` per session,
keeps a table mapping the fake pair back to ``(binding, real_pid, real_secret)``,
rewrites the BackendKeyData it relays to the client, and on a CancelRequest
looks the fake pair up and re-issues a real CancelRequest to the upstream. Every
new connection is peeked for CancelRequest *before* assuming a StartupMessage.

Runs on the proxy's asyncio loop as uid ``CREDPROXY_MITMPROXY_UID``, so its
server-leg outbound is already owner-exempted from the iptables catch-all (no
redirect loop, mitmproxy never in the path). The pushed bindings are live-read
off ``AppState`` per connection, so a config re-push takes effect on the next
connect without a restart. Secrets arrive already resolved (``pg.py``) -- the
proxy never fetches a credential.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import ssl
from dataclasses import dataclass
from typing import Callable

import audit
import log
from pg import PgBinding, PgCredentials

# ---- Protocol constants (see the Postgres frontend/backend protocol) ----

PROTOCOL_VERSION = 196608  # 3.0, the `code` field of a StartupMessage
SSL_REQUEST_CODE = 80877103
GSSENC_REQUEST_CODE = 80877104
CANCEL_REQUEST_CODE = 80877102

# Length bounds -- refuse to allocate on an attacker-declared length. Startup
# packets and auth messages are tiny; the startup *tail* (ParameterStatus /
# NoticeResponse) is bounded generously since a notice can be verbose.
MAX_STARTUP_LEN = 64 * 1024
MAX_AUTH_MSG = 64 * 1024
MAX_TAIL_MSG = 1024 * 1024

# Timeouts (seconds).
CONNECT_TIMEOUT = 10.0   # server-leg TCP connect + SSL negotiation
AUTH_TIMEOUT = 15.0      # server-leg auth handshake AND the post-auth startup tail
# Client-leg pre-startup phase (SSL negotiation + StartupMessage). A client that
# opens a connection and then trickles/withholds bytes must not tie up a task and
# socket forever (a mild slow-loris); the workspace is only semi-trusted.
STARTUP_TIMEOUT = 30.0

_PUMP_CHUNK = 65536


# ---- Errors ----


class ProtocolError(Exception):
    """A protocol violation on either leg (framing, unexpected message)."""


class AuthUnsupported(Exception):
    """The upstream demanded an auth method the broker does not implement."""


class AuthFailed(Exception):
    """The upstream rejected our injected credential. Carries the real
    ErrorResponse fields (SQLSTATE, message) -- for AUDIT ONLY, never relayed
    to the workspace (they can leak the real username)."""

    def __init__(self, fields: dict[str, str]):
        super().__init__(fields.get("M", "authentication failed"))
        self.fields = fields


# ---- Pure wire helpers (unit-tested directly) ----


def _i32(n: int) -> bytes:
    return int(n).to_bytes(4, "big")


def _u32(b: bytes) -> int:
    return int.from_bytes(b, "big")


def build_message(type_byte: bytes, body: bytes) -> bytes:
    """A typed backend/frontend message: 1-byte tag + Int32 length (self-
    inclusive) + body."""
    return type_byte + _i32(len(body) + 4) + body


def build_startup(params: dict[str, str]) -> bytes:
    """A StartupMessage: Int32 length + Int32 version + key\\0value\\0... + \\0."""
    body = bytearray(_i32(PROTOCOL_VERSION))
    for k, v in params.items():
        body += k.encode() + b"\0" + v.encode() + b"\0"
    body += b"\0"
    return _i32(len(body) + 4) + bytes(body)


def parse_startup_params(payload: bytes) -> dict[str, str]:
    """Parse the key/value tail of a StartupMessage (bytes after the 8-byte
    length+version header)."""
    parts = payload.split(b"\0")
    params: dict[str, str] = {}
    it = iter(parts)
    for key in it:
        if key == b"":  # the terminating empty key
            break
        try:
            val = next(it)
        except StopIteration:
            break
        params[key.decode("utf-8", "replace")] = val.decode("utf-8", "replace")
    return params


def build_error(severity: str, code: str, message: str) -> bytes:
    """An ErrorResponse the broker sends to the client. Deliberately generic:
    the real upstream detail goes to the audit log, never here."""
    body = bytearray()
    body += b"S" + severity.encode() + b"\0"
    body += b"V" + severity.encode() + b"\0"
    body += b"C" + code.encode() + b"\0"
    body += b"M" + message.encode() + b"\0"
    body += b"\0"
    return build_message(b"E", bytes(body))


def parse_error_fields(body: bytes) -> dict[str, str]:
    """Parse ErrorResponse/NoticeResponse fields (1-byte code + cstring each,
    terminated by a zero byte). Used to lift the real SQLSTATE/message into the
    audit event on an auth failure."""
    fields: dict[str, str] = {}
    i = 0
    while i < len(body):
        code = body[i:i + 1]
        if code == b"\0":
            break
        end = body.find(b"\0", i + 1)
        if end < 0:
            break
        fields[code.decode("latin1")] = body[i + 1:end].decode("utf-8", "replace")
        i = end + 1
    return fields


def md5_password(user: str, password: str, salt: bytes) -> bytes:
    """The AuthenticationMD5Password response body:
    ``md5`` + md5( md5(password+user) + salt )."""
    inner = hashlib.md5((password + user).encode()).hexdigest()
    outer = hashlib.md5(inner.encode() + salt).hexdigest()
    return b"md5" + outer.encode()


# ---- SCRAM-SHA-256 (RFC 5802 / RFC 7677), channel binding not used ----

# base64("n,,") -- the GS2 header the client-final message echoes as `c=`.
_GS2_HEADER_B64 = base64.b64encode(b"n,,").decode()  # "biws"


def gen_nonce() -> str:
    """A fresh SCRAM client nonce. base64 alphabet excludes ',', so it is a
    valid SCRAM printable value."""
    return base64.b64encode(os.urandom(18)).decode()


def scram_client_first(nonce: str) -> tuple[str, bytes]:
    """Return (client-first-message-bare, SASLInitialResponse bytes). The bare
    message is retained for the AuthMessage in the final step. The username is
    sent empty -- Postgres uses the startup `user`, not this field."""
    bare = f"n=,r={nonce}"
    first = "n,," + bare  # GS2 header + bare
    body = b"SCRAM-SHA-256\0" + _i32(len(first)) + first.encode()
    return bare, build_message(b"p", body)


def scram_client_final(
    password: str, client_nonce: str, client_first_bare: str, server_first: str
) -> tuple[bytes, bytes]:
    """Given the server-first-message, compute the client-final SASLResponse
    bytes and the expected ServerSignature (to verify the server-final)."""
    attrs = _scram_attrs(server_first)
    combined_nonce = attrs["r"]
    if not combined_nonce.startswith(client_nonce):
        raise ProtocolError("SCRAM server nonce does not extend the client nonce")
    salt = base64.b64decode(attrs["s"])
    iters = int(attrs["i"])

    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters, dklen=32)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()

    final_no_proof = f"c={_GS2_HEADER_B64},r={combined_nonce}"
    auth_message = f"{client_first_bare},{server_first},{final_no_proof}"
    client_sig = hmac.new(stored_key, auth_message.encode(), hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, client_sig))
    client_final = f"{final_no_proof},p={base64.b64encode(proof).decode()}"

    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    server_sig = hmac.new(server_key, auth_message.encode(), hashlib.sha256).digest()
    return build_message(b"p", client_final.encode()), server_sig


def scram_verify_final(server_final: str, expected_sig: bytes) -> bool:
    attrs = _scram_attrs(server_final)
    got = base64.b64decode(attrs.get("v", ""))
    return hmac.compare_digest(got, expected_sig)


def _scram_attrs(msg: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in msg.split(","):
        k, _, v = token.partition("=")
        out[k] = v
    return out


# ---- Async framing helpers ----


async def _read_startup_header(reader: asyncio.StreamReader) -> tuple[int, int]:
    """Read the 8-byte (length, code) header shared by SSLRequest /
    GSSENCRequest / CancelRequest / StartupMessage."""
    header = await reader.readexactly(8)
    return _u32(header[:4]), _u32(header[4:8])


async def _read_message(
    reader: asyncio.StreamReader, max_len: int
) -> tuple[bytes, bytes]:
    """Read one typed message (1-byte tag + Int32 length + body), bounding the
    declared length before allocating."""
    type_byte = await reader.readexactly(1)
    length = _u32(await reader.readexactly(4))
    if length < 4 or length - 4 > max_len:
        raise ProtocolError(f"message length {length} out of bounds")
    body = await reader.readexactly(length - 4)
    return type_byte, body


# ---- Server-leg connect + auth ----


def _ssl_context(binding: PgBinding) -> ssl.SSLContext:
    """Build the server-leg SSL context per the binding's sslmode. verify-ca /
    verify-full get a verifying context (against `sslrootcert` if given, else
    the system store); require / allow / prefer encrypt without verifying."""
    if binding.sslmode in ("verify-ca", "verify-full"):
        ctx = ssl.create_default_context(cafile=binding.sslrootcert)
        ctx.check_hostname = binding.sslmode == "verify-full"
        return ctx
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def connect_server(
    binding: PgBinding,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open the server-leg TCP connection and perform the SSLRequest
    negotiation per sslmode, leaving the stream positioned at the startup phase
    (ready for a StartupMessage OR a CancelRequest). Raises on a TLS policy the
    server won't satisfy."""
    reader, writer = await asyncio.open_connection(binding.host, binding.port)
    if binding.sslmode == "disable":
        return reader, writer
    writer.write(_i32(8) + _i32(SSL_REQUEST_CODE))
    await writer.drain()
    resp = await reader.readexactly(1)
    if resp == b"S":
        await writer.start_tls(_ssl_context(binding), server_hostname=binding.host)
        return reader, writer
    if resp == b"N":
        if binding.sslmode in ("require", "verify-ca", "verify-full"):
            raise ProtocolError(
                f"upstream refused TLS but sslmode={binding.sslmode} requires it")
        return reader, writer  # allow / prefer: fall back to plaintext
    raise ProtocolError(f"unexpected SSL negotiation reply {resp!r}")


async def authenticate_server(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, binding: PgBinding
) -> None:
    """Drive the server-leg auth handshake with the real credential. The caller
    has already sent the StartupMessage. Returns on AuthenticationOk; raises
    AuthFailed / AuthUnsupported / ProtocolError otherwise."""
    while True:
        type_byte, body = await _read_message(reader, MAX_AUTH_MSG)
        if type_byte == b"R":
            auth_code = _u32(body[:4])
            if auth_code == 0:  # AuthenticationOk
                return
            if auth_code == 3:  # cleartext
                writer.write(build_message(b"p", binding.password.encode() + b"\0"))
                await writer.drain()
            elif auth_code == 5:  # MD5
                salt = body[4:8]
                writer.write(build_message(
                    b"p", md5_password(binding.username, binding.password, salt) + b"\0"))
                await writer.drain()
            elif auth_code == 10:  # SASL (SCRAM)
                await _scram_exchange(reader, writer, binding, body[4:])
            else:
                raise AuthUnsupported(f"unsupported auth method {auth_code}")
        elif type_byte == b"E":
            raise AuthFailed(parse_error_fields(body))
        elif type_byte == b"N":  # NoticeResponse mid-auth: informational
            continue
        elif type_byte == b"v":
            # NegotiateProtocolVersion: the server doesn't recognize a requested
            # minor version or a `_pq_.`-prefixed option (we forward the client's
            # startup params verbatim, so a client-set option can trigger it). It
            # is advisory -- auth continues normally -- so skip it. Not raising
            # here is what keeps a client with an exotic startup param working,
            # exactly as it would against vanilla Postgres.
            continue
        else:
            raise ProtocolError(f"unexpected message {type_byte!r} during auth")


async def _read_auth_message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Read the next auth-phase message, transparently skipping any interruptible
    NoticeResponse ('N') a server or a fronting pooler may interleave. Postgres
    treats notices as deliverable at any point, so a mid-SCRAM notice must not
    break the exchange (the outer auth loop already skips them; this gives the
    SCRAM sub-exchange the same tolerance)."""
    while True:
        type_byte, body = await _read_message(reader, MAX_AUTH_MSG)
        if type_byte != b"N":
            return type_byte, body


async def _scram_exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    binding: PgBinding,
    mechanisms_blob: bytes,
) -> None:
    """Run the SCRAM-SHA-256 client exchange through the server-final message.
    Leaves the following AuthenticationOk for the caller's loop to consume."""
    mechs = [m.decode("latin1") for m in mechanisms_blob.split(b"\0") if m]
    if "SCRAM-SHA-256" not in mechs:
        raise AuthUnsupported(f"no supported SASL mechanism offered: {mechs}")

    nonce = gen_nonce()
    bare, initial = scram_client_first(nonce)
    writer.write(initial)
    await writer.drain()

    type_byte, body = await _read_auth_message(reader)
    if type_byte == b"E":
        raise AuthFailed(parse_error_fields(body))
    if type_byte != b"R" or _u32(body[:4]) != 11:  # AuthenticationSASLContinue
        raise ProtocolError("expected AuthenticationSASLContinue")
    server_first = body[4:].decode("utf-8")

    client_final, expected_sig = scram_client_final(
        binding.password, nonce, bare, server_first)
    writer.write(client_final)
    await writer.drain()

    type_byte, body = await _read_auth_message(reader)
    if type_byte == b"E":
        raise AuthFailed(parse_error_fields(body))
    if type_byte != b"R" or _u32(body[:4]) != 12:  # AuthenticationSASLFinal
        raise ProtocolError("expected AuthenticationSASLFinal")
    server_final = body[4:].decode("utf-8")
    if not scram_verify_final(server_final, expected_sig):
        raise ProtocolError("SCRAM server signature verification failed")


# ---- Cancel routing ----


@dataclass(frozen=True)
class _CancelTarget:
    binding: PgBinding
    real_pid: int
    real_secret: int


def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
    except Exception:
        pass


# ---- The broker ----


class PgBroker:
    """Handles one listener's connections. `get_creds` live-reads the current
    pushed `PgCredentials` off AppState (so a re-push takes effect on the next
    connect). `connect` is the server-leg opener, overridable in tests."""

    def __init__(
        self,
        get_creds: Callable[[], PgCredentials],
        *,
        connect: Callable[[PgBinding], "asyncio.Future"] | None = None,
    ):
        self._get_creds = get_creds
        self._connect = connect or connect_server
        self._cancel_table: dict[tuple[int, int], _CancelTarget] = {}

    async def handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        server_writer: asyncio.StreamWriter | None = None
        # Cancel-table keys this connection registers. Cleaned up in `finally`
        # via this list (NOT a single returned key) so an entry registered on
        # BackendKeyData is still dropped if the startup tail then RAISES before
        # returning (e.g. an ErrorResponse arriving after BackendKeyData) --
        # otherwise the entry would leak into the table permanently.
        registered_keys: list[tuple[int, int]] = []
        try:
            # The whole client-leg pre-startup phase is bounded (slow-loris guard).
            outcome = await asyncio.wait_for(
                self._read_client_startup(client_reader, client_writer),
                STARTUP_TIMEOUT)
            kind, payload = outcome
            if kind == "cancel":
                return
            if kind == "reject":
                await self._reject(client_writer, *payload)
                return
            params = payload

            selector = params.get("user")
            if not selector:
                await self._reject(client_writer, "08P01",
                                   "credproxy: missing user in startup message")
                return

            binding = self._get_creds().get(selector)
            if binding is None:
                audit.emit("pg-no-binding", user=selector)
                await self._reject(client_writer, "28000",
                                   f'credproxy: no pg binding for user "{selector}"')
                return

            # dbname falls back to the binding's configured db, NEVER the user
            # (which is the binding name); libpq defaults db to user when unset.
            dbname = params.get("database", binding.dbname)
            audit.emit("pg-connect", binding=binding.name,
                       host=binding.host, port=binding.port, dbname=dbname)

            try:
                server_reader, server_writer = await asyncio.wait_for(
                    self._connect(binding), CONNECT_TIMEOUT)
                server_params = dict(params, user=binding.username, database=dbname)
                server_writer.write(build_startup(server_params))
                await server_writer.drain()
                await asyncio.wait_for(
                    authenticate_server(server_reader, server_writer, binding),
                    AUTH_TIMEOUT)
            except AuthFailed as e:
                # The real detail (which can name the real user) goes ONLY to
                # the audit log; the workspace gets a generic message.
                audit.emit("pg-auth-fail", binding=binding.name, host=binding.host,
                           sqlstate=e.fields.get("C"), detail=e.fields.get("M"))
                await self._reject(client_writer, "28P01",
                                   "credproxy: authentication to upstream database failed")
                return
            except (AuthUnsupported, ProtocolError, ssl.SSLError, OSError,
                    asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
                audit.emit("pg-error", binding=binding.name, host=binding.host,
                           error=type(e).__name__, detail=str(e))
                await self._reject(client_writer, "08006",
                                   "credproxy: could not establish upstream connection")
                return

            # Server authenticated: tell the client OK, then relay the startup
            # tail (rewriting BackendKeyData), then byte-pump. The tail is bounded
            # too -- a server that auths then stalls before ReadyForQuery must not
            # hang the (already-AuthenticationOk'd) client forever.
            client_writer.write(build_message(b"R", _i32(0)))  # AuthenticationOk
            await asyncio.wait_for(
                self._relay_startup_tail(
                    server_reader, client_writer, binding, registered_keys),
                AUTH_TIMEOUT)
            await self._pump(client_reader, client_writer,
                             server_reader, server_writer)
        except (asyncio.IncompleteReadError, ConnectionError, ProtocolError,
                ssl.SSLError, OSError, asyncio.TimeoutError):
            pass
        finally:
            for key in registered_keys:
                self._cancel_table.pop(key, None)
            _close(server_writer)
            _close(client_writer)

    async def _read_client_startup(
        self, client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> tuple[str, object]:
        """Read the client-leg pre-startup phase and classify it. Returns one of:
          ("cancel", None)             -- was a CancelRequest, already routed;
          ("reject", (code, message))  -- bad protocol/length, caller rejects;
          ("ok", params)               -- a valid StartupMessage's param dict.
        Factored out so the caller can bound the whole phase with one timeout."""
        length, code = await _read_startup_header(client_reader)

        # A brand-new connection may be a CancelRequest (no user field) -- peek
        # before assuming a StartupMessage.
        if code == CANCEL_REQUEST_CODE:
            await self._handle_cancel(client_reader, length)
            return ("cancel", None)

        # Negotiation loop: reply 'N' (unwilling) to any SSLRequest /
        # GSSENCRequest until a real StartupMessage (or a CancelRequest that
        # follows an SSL negotiation).
        while code in (SSL_REQUEST_CODE, GSSENC_REQUEST_CODE):
            client_writer.write(b"N")
            await client_writer.drain()
            length, code = await _read_startup_header(client_reader)
            if code == CANCEL_REQUEST_CODE:
                await self._handle_cancel(client_reader, length)
                return ("cancel", None)

        if code != PROTOCOL_VERSION:
            return ("reject", ("08P01", "credproxy: unsupported protocol version"))
        if length < 8 or length - 8 > MAX_STARTUP_LEN:
            return ("reject", ("08P01", "credproxy: startup message out of bounds"))
        params = parse_startup_params(await client_reader.readexactly(length - 8))
        return ("ok", params)

    async def _relay_startup_tail(
        self,
        server_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        binding: PgBinding,
        registered: list,
    ) -> None:
        """Relay ParameterStatus / BackendKeyData / ReadyForQuery verbatim to
        the client, substituting a fabricated (pid, secret) into BackendKeyData
        and recording the cancel-table entry. Appends each registered cancel key
        to `registered` (so the caller cleans it up even if this raises after a
        BackendKeyData -- e.g. a trailing ErrorResponse)."""
        while True:
            type_byte, body = await _read_message(server_reader, MAX_TAIL_MSG)
            if type_byte == b"K":  # BackendKeyData
                real_pid, real_secret = _u32(body[:4]), _u32(body[4:8])
                fake_pid, fake_secret = self._register_cancel(
                    binding, real_pid, real_secret)
                registered.append((fake_pid, fake_secret))
                client_writer.write(build_message(
                    b"K", _i32(fake_pid) + _i32(fake_secret)))
            elif type_byte == b"Z":  # ReadyForQuery -> startup complete
                client_writer.write(build_message(b"Z", body))
                await client_writer.drain()
                return
            elif type_byte == b"E":  # startup-phase error -> forward, then stop
                client_writer.write(build_message(b"E", body))
                await client_writer.drain()
                raise ProtocolError("upstream error during startup")
            else:  # ParameterStatus, NoticeResponse, etc -> verbatim
                client_writer.write(build_message(type_byte, body))

    def _register_cancel(
        self, binding: PgBinding, real_pid: int, real_secret: int
    ) -> tuple[int, int]:
        while True:
            fake_pid = _u32(os.urandom(4)) & 0x7FFFFFFF  # positive Int32
            fake_secret = _u32(os.urandom(4))
            key = (fake_pid, fake_secret)
            if key not in self._cancel_table:
                self._cancel_table[key] = _CancelTarget(binding, real_pid, real_secret)
                return key

    async def _handle_cancel(
        self, client_reader: asyncio.StreamReader, length: int
    ) -> None:
        """Route a CancelRequest: look the fake (pid, secret) up and re-issue a
        real CancelRequest to the upstream on a fresh connection."""
        if length != 16:
            return  # malformed CancelRequest
        rest = await client_reader.readexactly(8)
        key = (_u32(rest[:4]), _u32(rest[4:8]))
        target = self._cancel_table.get(key)
        if target is None:
            audit.emit("pg-cancel", outcome="unknown-key")
            return
        audit.emit("pg-cancel", binding=target.binding.name, host=target.binding.host)
        server_writer: asyncio.StreamWriter | None = None
        try:
            server_reader, server_writer = await asyncio.wait_for(
                self._connect(target.binding), CONNECT_TIMEOUT)
            server_writer.write(
                _i32(16) + _i32(CANCEL_REQUEST_CODE)
                + _i32(target.real_pid) + _i32(target.real_secret))
            await server_writer.drain()
        except (OSError, ssl.SSLError, ProtocolError, asyncio.TimeoutError,
                asyncio.IncompleteReadError) as e:
            audit.emit("pg-cancel", binding=target.binding.name,
                       outcome="failed", error=type(e).__name__)
        finally:
            _close(server_writer)

    async def _pump(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        server_reader: asyncio.StreamReader,
        server_writer: asyncio.StreamWriter,
    ) -> None:
        """Bidirectional raw byte-pump until either side closes."""
        async def one(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(_PUMP_CHUNK)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
                pass
            finally:
                try:
                    dst.write_eof()
                except Exception:
                    pass

        await asyncio.gather(
            one(client_reader, server_writer),
            one(server_reader, client_writer),
            return_exceptions=True,
        )

    async def _reject(
        self, client_writer: asyncio.StreamWriter, code: str, message: str
    ) -> None:
        client_writer.write(build_error("FATAL", code, message))
        try:
            await client_writer.drain()
        except Exception:
            pass
        log.emit("pg", msg="rejected connection", code=code)
