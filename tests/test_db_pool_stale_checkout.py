"""The pools must never hand out a connection that died while idle.

A pooled connection whose server-side backend went away (PostgreSQL restart,
idle-session timeout, a `pg_terminate_backend`) still looks usable to the
pool: it is only discovered to be dead when the request that was handed it
tries to run a query, and that request fails. The pools must validate a
connection on checkout and recycle a dead one instead of serving it.

Everything here runs in-process: `_FakeConnection` stands in for
`psycopg.Connection`, so the test opens no socket and touches no database.
The pool machinery itself is the real `psycopg_pool.ConnectionPool`.
"""
from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool

from backend import db


class FakeConnection:
    """A connection whose backend can be terminated under it."""

    def __init__(self, autocommit: bool = False) -> None:
        self.alive = True
        self.autocommit = autocommit
        self.closed = False
        self.pgconn = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self._pool: object | None = None

    def execute(self, _query, _params=None):
        if not self.alive:
            # What psycopg reports for a backend that went away, and the
            # state the pool reads to decide the connection is unusable.
            self.pgconn.transaction_status = TransactionStatus.UNKNOWN
            raise psycopg.OperationalError(
                "server closed the connection unexpectedly"
            )
        return self

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False


class FakeServer:
    """Hands out fake connections and can kill the ones it has open."""

    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []

    def connect(self, autocommit: bool = False) -> FakeConnection:
        conn = FakeConnection(autocommit=autocommit)
        self.connections.append(conn)
        return conn

    def terminate_open_backends(self) -> None:
        """Terminate every connection opened so far, as a server restart
        would. Connections opened afterwards are healthy."""
        for conn in self.connections:
            conn.alive = False


def _fake_pool_class(server: FakeServer) -> type[ConnectionPool]:
    """A ConnectionPool that talks to `server` instead of PostgreSQL."""

    class _FakeConnection(FakeConnection):
        @classmethod
        def connect(cls, _conninfo: str = "", **kwargs):
            return server.connect(autocommit=bool(kwargs.get("autocommit")))

    class _FakeConnectionPool(ConnectionPool):
        def __init__(self, conninfo: str = "", **kwargs) -> None:
            kwargs["connection_class"] = _FakeConnection
            super().__init__(conninfo, **kwargs)

    return _FakeConnectionPool


@pytest.mark.parametrize(
    ("pool_factory", "cache_attr", "dsn_env"),
    [
        pytest.param(db.viz_pool, "_VIZ", "DATABASE_URL_VIZ", id="viz"),
        pytest.param(db.auth_pool, "_AUTH", "DATABASE_URL_AUTH", id="auth"),
    ],
)
def test_pool_replaces_connection_terminated_while_idle(
    monkeypatch, pool_factory, cache_attr, dsn_env
):
    """A dead pooled connection must not be handed out on checkout."""
    server = FakeServer()
    # Never dialled — the fake connection class replaces psycopg's — but the
    # factories read the DSN out of the environment.
    monkeypatch.setenv(dsn_env, "postgresql:///pool-checkout-test")
    monkeypatch.setattr(db, "ConnectionPool", _fake_pool_class(server))
    monkeypatch.setattr(db, cache_attr, None)

    pool = pool_factory()
    try:
        pool.wait(timeout=5)
        assert server.connections, "pool opened no connections"
        server.terminate_open_backends()

        with pool.connection() as conn:
            assert conn.alive, (
                "pool handed out a connection whose backend was terminated "
                "while it sat idle in the pool"
            )
    finally:
        pool.close()
