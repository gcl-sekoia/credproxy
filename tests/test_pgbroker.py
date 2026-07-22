"""Phase 1 protocol-core tests for the PostgreSQL broker.

Two layers, neither needs a real Postgres:

- Pure wire/auth helpers against known vectors (RFC 7677 SCRAM, a fixed MD5
  vector, startup/error round-trips).
- End-to-end integration against an in-process **fake** Postgres server that
  scripts the server side of the handshake (cleartext / MD5 / SCRAM / auth
  failure), so the real `PgBroker` + `connect_server` drive real asyncio
  streams: credential injection, BackendKeyData rewrite, cancel routing, and
  the sanitized-error split are all exercised.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac

import pytest

import pgbroker as pb
from pg import PgBinding, PgCredentials


# ---- Pure helpers ----


def test_startup_round_trip():
    wire = pb.build_startup({"user": "alice", "database": "shop"})
    length = int.from_bytes(wire[:4], "big")
    assert length == len(wire)
    assert int.from_bytes(wire[4:8], "big") == pb.PROTOCOL_VERSION
    params = pb.parse_startup_params(wire[8:])
    assert params == {"user": "alice", "database": "shop"}


def test_error_round_trip():
    wire = pb.build_error("FATAL", "28P01", "nope")
    assert wire[:1] == b"E"
    body = wire[5:]
    fields = pb.parse_error_fields(body)
    assert fields["S"] == "FATAL"
    assert fields["C"] == "28P01"
    assert fields["M"] == "nope"


def test_md5_known_vector():
    # md5("md5" prefix aside): outer = md5( md5(password+user) + salt )
    user, password, salt = "bob", "secret", b"\x01\x02\x03\x04"
    inner = hashlib.md5((password + user).encode()).hexdigest()
    outer = hashlib.md5(inner.encode() + salt).hexdigest()
    assert pb.md5_password(user, password, salt) == b"md5" + outer.encode()


def test_scram_rfc7677_vector():
    # RFC 7677 section 3 worked example (username user, password "pencil").
    client_nonce = "rOprNGfwEbeRWgbNEkqO"
    client_first_bare = f"n=user,r={client_nonce}"
    server_first = (
        "r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
        "s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096"
    )
    client_final, server_sig = pb.scram_client_final(
        "pencil", client_nonce, client_first_bare, server_first)
    # The proof and server signature are the RFC's published values.
    assert b"p=dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=" in client_final
    assert base64.b64encode(server_sig).decode() == \
        "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    # And the verifier accepts the matching server-final.
    server_final = f"v={base64.b64encode(server_sig).decode()}"
    assert pb.scram_verify_final(server_final, server_sig)
    assert not pb.scram_verify_final("v=AAAA", server_sig)


def test_scram_nonce_mismatch_rejected():
    with pytest.raises(pb.ProtocolError):
        pb.scram_client_final(
            "pencil", "clientnonce", "n=,r=clientnonce",
            "r=DIFFERENTnonce,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096")


# ---- Fake Postgres server for integration ----


async def _read_msg(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    type_byte = await reader.readexactly(1)
    length = int.from_bytes(await reader.readexactly(4), "big")
    return type_byte, await reader.readexactly(length - 4)


class FakePg:
    """Scripts the server side of the handshake. `auth` selects the challenge."""

    def __init__(self, *, auth="scram", password="s3cret",
                 real_pid=4321, real_secret=98765, refuse_ssl=False):
        self.auth = auth
        self.password = password
        self.real_pid = real_pid
        self.real_secret = real_secret
        self.refuse_ssl = refuse_ssl
        self.seen_user: str | None = None
        self.seen_db: str | None = None
        self.cancel_received: tuple[int, int] | None = None
        self.cancel_event = asyncio.Event()

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            length, code = await pb._read_startup_header(r)
            if code == pb.SSL_REQUEST_CODE:
                w.write(b"N")  # this fake always speaks plaintext
                await w.drain()
                length, code = await pb._read_startup_header(r)
            if code == pb.CANCEL_REQUEST_CODE:
                rest = await r.readexactly(8)
                self.cancel_received = (int.from_bytes(rest[:4], "big"),
                                        int.from_bytes(rest[4:8], "big"))
                self.cancel_event.set()
                w.close()
                return

            params = pb.parse_startup_params(await r.readexactly(length - 8))
            self.seen_user = params.get("user")
            self.seen_db = params.get("database")

            await self._do_auth(r, w)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def _do_auth(self, r, w) -> None:
        if self.auth == "cleartext":
            w.write(pb.build_message(b"R", pb._i32(3)))
            await w.drain()
            _, body = await _read_msg(r)
            assert body.rstrip(b"\0").decode() == self.password
        elif self.auth == "md5":
            salt = b"WXYZ"
            w.write(pb.build_message(b"R", pb._i32(5) + salt))
            await w.drain()
            _, body = await _read_msg(r)
            assert body.rstrip(b"\0") == pb.md5_password(
                self.seen_user, self.password, salt)
        elif self.auth == "scram":
            await self._do_scram(r, w)
        elif self.auth == "fail":
            w.write(pb.build_message(b"R", pb._i32(3)))
            await w.drain()
            await _read_msg(r)  # consume the password
            w.write(pb.build_error(
                "FATAL", "28P01",
                'password authentication failed for user "svc_real_account"'))
            await w.drain()
            return
        # success tail
        w.write(pb.build_message(b"R", pb._i32(0)))  # AuthenticationOk
        w.write(pb.build_message(b"S", b"server_version\x0016.0\x00"))
        w.write(pb.build_message(
            b"K", pb._i32(self.real_pid) + pb._i32(self.real_secret)))
        w.write(pb.build_message(b"Z", b"I"))
        await w.drain()
        await self._echo(r, w)

    async def _do_scram(self, r, w) -> None:
        w.write(pb.build_message(b"R", pb._i32(10) + b"SCRAM-SHA-256\x00\x00"))
        await w.drain()
        _, body = await _read_msg(r)
        _, _, rest = body.partition(b"\0")
        datalen = int.from_bytes(rest[:4], "big")
        client_first = rest[4:4 + datalen].decode()
        client_first_bare = client_first[3:]  # strip "n,,"
        cnonce = dict(x.split("=", 1) for x in client_first_bare.split(","))["r"]

        salt = b"0123456789abcdef"
        iters = 4096
        snonce = cnonce + "FAKEsrv"
        server_first = (f"r={snonce},s={base64.b64encode(salt).decode()},"
                        f"i={iters}")
        w.write(pb.build_message(b"R", pb._i32(11) + server_first.encode()))
        await w.drain()

        _, body = await _read_msg(r)
        client_final = body.decode()
        salted = hashlib.pbkdf2_hmac(
            "sha256", self.password.encode(), salt, iters, dklen=32)
        final_no_proof = client_final.rsplit(",p=", 1)[0]
        auth_message = f"{client_first_bare},{server_first},{final_no_proof}"
        server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        server_sig = hmac.new(server_key, auth_message.encode(),
                              hashlib.sha256).digest()
        w.write(pb.build_message(
            b"R", pb._i32(12) + f"v={base64.b64encode(server_sig).decode()}".encode()))
        await w.drain()

    async def _echo(self, r, w) -> None:
        try:
            while True:
                data = await r.read(4096)
                if not data:
                    break
                w.write(data)
                await w.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _client_startup(port: int, user: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(pb.build_startup({"user": user, "database": "appdb"}))
    await writer.drain()
    return reader, writer


def _binding(fake_port, *, sslmode="disable", password="s3cret", name="mybind"):
    return PgBinding(
        name=name, host="127.0.0.1", port=fake_port, dbname="realdb",
        username="svc_real_account", password=password, sslmode=sslmode)


async def _run_session(auth, sslmode="disable"):
    """Bring up a fake DB + broker, do a full client session, and return the
    fake, the BackendKeyData the client saw, and the broker (for cancel)."""
    fake = FakePg(auth=auth)
    fake_server, fake_port = await _serve(fake.handle)
    binding = _binding(fake_port, sslmode=sslmode)
    creds = PgCredentials({binding.name: binding})
    broker = pb.PgBroker(lambda: creds)
    broker_server, broker_port = await _serve(broker.handle)
    return fake, fake_server, broker, broker_port


@pytest.mark.parametrize("auth", ["cleartext", "md5", "scram"])
async def test_full_session_all_auth_methods(auth):
    fake, fake_server, broker, broker_port = await _run_session(auth)
    try:
        cr, cw = await _client_startup(broker_port, "mybind")
        # AuthenticationOk
        t, body = await _read_msg(cr)
        assert t == b"R" and int.from_bytes(body[:4], "big") == 0
        # ParameterStatus (relayed verbatim)
        t, _ = await _read_msg(cr)
        assert t == b"S"
        # BackendKeyData -- pid/secret REWRITTEN, not the real ones
        t, body = await _read_msg(cr)
        assert t == b"K"
        client_pid = int.from_bytes(body[:4], "big")
        client_secret = int.from_bytes(body[4:8], "big")
        assert (client_pid, client_secret) != (fake.real_pid, fake.real_secret)
        # ReadyForQuery
        t, _ = await _read_msg(cr)
        assert t == b"Z"

        # The server leg re-originated with the REAL username, resolved db.
        assert fake.seen_user == "svc_real_account"
        assert fake.seen_db == "appdb"

        # Byte-pump: arbitrary post-auth bytes round-trip through the echo.
        cw.write(b"hello-postgres")
        await cw.drain()
        assert await cr.readexactly(14) == b"hello-postgres"
        cw.close()
    finally:
        fake_server.close()


async def test_ssl_negotiation_falls_back_to_plaintext():
    # sslmode=prefer -> broker sends SSLRequest, fake refuses ('N'), broker
    # continues plaintext. Exercises the negotiation loop.
    fake, fake_server, broker, broker_port = await _run_session(
        "scram", sslmode="prefer")
    try:
        cr, cw = await _client_startup(broker_port, "mybind")
        t, body = await _read_msg(cr)
        assert t == b"R" and int.from_bytes(body[:4], "big") == 0
        cw.close()
    finally:
        fake_server.close()


async def test_cancel_request_routes_real_key():
    fake, fake_server, broker, broker_port = await _run_session("scram")
    try:
        cr, cw = await _client_startup(broker_port, "mybind")
        # walk to BackendKeyData to learn the fake key
        await _read_msg(cr)  # AuthenticationOk
        await _read_msg(cr)  # ParameterStatus
        t, body = await _read_msg(cr)  # BackendKeyData
        fake_pid = int.from_bytes(body[:4], "big")
        fake_secret = int.from_bytes(body[4:8], "big")
        await _read_msg(cr)  # ReadyForQuery

        # New connection: a CancelRequest carrying the FAKE key.
        ccr, ccw = await asyncio.open_connection("127.0.0.1", broker_port)
        ccw.write(pb._i32(16) + pb._i32(pb.CANCEL_REQUEST_CODE)
                  + pb._i32(fake_pid) + pb._i32(fake_secret))
        await ccw.drain()

        await asyncio.wait_for(fake.cancel_event.wait(), 5.0)
        # The broker re-issued the REAL pid/secret upstream.
        assert fake.cancel_received == (fake.real_pid, fake.real_secret)
        cw.close()
        ccw.close()
    finally:
        fake_server.close()


async def test_unknown_user_rejected_generically():
    fake, fake_server, broker, broker_port = await _run_session("scram")
    try:
        cr, cw = await _client_startup(broker_port, "does-not-exist")
        t, body = await _read_msg(cr)
        assert t == b"E"
        fields = pb.parse_error_fields(body)
        assert fields["C"] == "28000"
        assert "does-not-exist" in fields["M"]  # the client-supplied user, safe
        cw.close()
    finally:
        fake_server.close()


async def test_auth_failure_sanitized_to_client_real_detail_to_audit(capsys):
    fake, fake_server, broker, broker_port = await _run_session("fail")
    try:
        cr, cw = await _client_startup(broker_port, "mybind")
        t, body = await _read_msg(cr)
        assert t == b"E"
        fields = pb.parse_error_fields(body)
        # The workspace NEVER sees the real upstream username or detail.
        assert "svc_real_account" not in fields["M"]
        assert fields["M"] == \
            "credproxy: authentication to upstream database failed"
        cw.close()
    finally:
        fake_server.close()
    # The real detail went to the audit log only.
    out = capsys.readouterr().out
    assert "pg-auth-fail" in out
    assert "svc_real_account" in out
