"""Live end-to-end integration: the real PgBroker in front of a REAL PostgreSQL,
driven by a real `psql`. Skipped unless a reachable Postgres is provided.

Enable by pointing `CREDPROXY_TEST_PG` at a real database as a libpq URL that
INCLUDES the real credentials, e.g.

    CREDPROXY_TEST_PG=postgresql://user:pass@127.0.0.1:5432/db \
        credproxy dev test --proxy -- tests/test_pg_integration.py

The test stands up the broker on-host (no NET_ADMIN/netns needed -- it's a plain
asyncio listener), pointed at that DB, then connects `psql` THROUGH the broker as
the binding name with NO password. It exercises what only a real server can: the
real SCRAM-SHA-256 handshake (real salt/nonce/iterations), the post-auth
byte-pump, the sanitized upstream-auth-failure split, and real query
cancellation routed via the fabricated (pid, secret). The mock-server coverage
lives in test_pgbroker.py; this proves parity against the genuine wire.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
from urllib.parse import unquote, urlsplit

import pytest

import pgbroker
from pg import PgBinding, PgCredentials

_DSN = os.environ.get("CREDPROXY_TEST_PG")

pytestmark = [
    pytest.mark.skipif(not _DSN, reason="set CREDPROXY_TEST_PG to a real pg URL"),
    pytest.mark.skipif(shutil.which("psql") is None, reason="psql not on PATH"),
]


def _parse_dsn(dsn: str) -> dict:
    u = urlsplit(dsn)
    return {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 5432,
        "username": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "dbname": (u.path or "/postgres").lstrip("/") or "postgres",
    }


def _creds():
    real = _parse_dsn(_DSN)
    good = PgBinding(name="itdb", sslmode="disable", **real)
    bad = PgBinding(name="itbad", sslmode="disable",
                    **{**real, "password": real["password"] + "-WRONG"})
    return PgCredentials({"itdb": good, "itbad": bad}), real


async def _serve():
    creds, real = _creds()
    broker = pgbroker.PgBroker(lambda: creds)
    server = await asyncio.start_server(broker.handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], real


async def _psql(port: int, user: str, sql: str, db: str):
    dsn = f"postgresql://{user}@127.0.0.1:{port}/{db}?sslmode=disable"
    proc = await asyncio.create_subprocess_exec(
        "psql", dsn, "-tAc", sql,
        env={**os.environ, "PGCONNECT_TIMEOUT": "8"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 20)
    return proc.returncode, out.decode().strip(), err.decode().strip()


async def test_scram_reorigination_reports_real_user():
    server, port, real = await _serve()
    try:
        rc, out, err = await _psql(port, "itdb", "SELECT current_user", real["dbname"])
        # Connected as the binding name with no password; the broker re-auth'd to
        # the real DB, so current_user is the REAL user, not the binding name.
        assert rc == 0, err
        assert out == real["username"]
    finally:
        server.close()
        await server.wait_closed()


async def test_byte_pump_write_and_read_in_session():
    server, port, real = await _serve()
    try:
        rc, out, err = await _psql(
            port, "itdb",
            "CREATE TEMP TABLE t(x int); INSERT INTO t VALUES (1),(2),(3); "
            "SELECT count(*) FROM t", real["dbname"])
        assert rc == 0, err
        assert out.splitlines()[-1] == "3"
    finally:
        server.close()
        await server.wait_closed()


async def test_upstream_auth_failure_is_sanitized():
    server, port, real = await _serve()
    try:
        rc, out, err = await _psql(port, "itbad", "SELECT 1", real["dbname"])
        assert rc != 0
        assert "credproxy: authentication to upstream database failed" in err
        # The real username must NEVER leak to the client.
        assert real["username"] not in err
    finally:
        server.close()
        await server.wait_closed()


async def test_real_query_cancellation_routes_upstream():
    server, port, real = await _serve()
    try:
        dsn = f"postgresql://itdb@127.0.0.1:{port}/{real['dbname']}?sslmode=disable"
        proc = await asyncio.create_subprocess_exec(
            "psql", dsn, "-tAc", "SELECT pg_sleep(30)",
            env={**os.environ, "PGCONNECT_TIMEOUT": "8"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.sleep(2.0)                      # let the sleep start upstream
        started = asyncio.get_event_loop().time()
        proc.send_signal(signal.SIGINT)              # psql -> CancelRequest -> broker
        _, err_b = await asyncio.wait_for(proc.communicate(), 15)
        elapsed = asyncio.get_event_loop().time() - started
        # The broker mapped the fabricated (pid,secret) back and re-issued a real
        # cancel: pg aborts the sleep long before its 30s.
        assert elapsed < 10, f"cancel not honored (took {elapsed:.1f}s)"
        assert "canceling statement" in err_b.decode()
    finally:
        server.close()
        await server.wait_closed()
