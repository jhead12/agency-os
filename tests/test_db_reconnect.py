"""The app's per-thread Postgres connection survives the server dropping it.

On Railway a Postgres restart terminates every connection ("terminating
connection due to administrator command"); the next request on each worker
thread used to fail with a 500 instead of reconnecting.
"""

import psycopg
import pytest

from core.db import Database


def _kill(url: str, pid: int) -> None:
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))


def test_reconnects_after_server_drops_idle_connection(pg_url):
    db = Database(pg_url)
    before = db.conn.raw.info.backend_pid
    _kill(pg_url, before)

    assert db.conn.execute("SELECT 1").fetchone()[0] == 1
    assert db.conn.raw.info.backend_pid != before


def test_transaction_reconnects_after_server_drops_idle_connection(pg_url):
    db = Database(pg_url)
    _kill(pg_url, db.conn.raw.info.backend_pid)

    with db.transaction() as c:
        c.execute("CREATE TABLE IF NOT EXISTS reconnect_probe (x INTEGER)")
        c.execute("INSERT INTO reconnect_probe VALUES (?)", (1,))
    assert db.conn.execute("SELECT x FROM reconnect_probe").fetchone()[0] == 1


def test_drop_inside_transaction_still_raises(pg_url):
    db = Database(pg_url)
    with pytest.raises(psycopg.OperationalError):
        with db.transaction() as c:
            _kill(pg_url, c.raw.info.backend_pid)
            c.execute("SELECT 1")
    assert db.conn.execute("SELECT 1").fetchone()[0] == 1  # the next request recovers
