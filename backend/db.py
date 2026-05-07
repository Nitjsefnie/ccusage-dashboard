"""Postgres connection pools.

Two pools:
- viz_pool   → claude_viz (this app's tables)
- auth_pool  → external users DB (read-only access to users.config for auth)

The pools never join across DBs.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

_VIZ: ConnectionPool | None = None
_AUTH: ConnectionPool | None = None


def viz_pool() -> ConnectionPool:
    global _VIZ
    if _VIZ is None:
        _VIZ = ConnectionPool(
            os.environ["DATABASE_URL_VIZ"],
            min_size=1, max_size=8, timeout=10,
            kwargs={"autocommit": False},
        )
    return _VIZ


def auth_pool() -> ConnectionPool:
    global _AUTH
    if _AUTH is None:
        _AUTH = ConnectionPool(
            os.environ["DATABASE_URL_AUTH"],
            min_size=1, max_size=4, timeout=10,
            kwargs={"autocommit": True},
        )
    return _AUTH


@contextmanager
def viz_conn():
    with viz_pool().connection() as conn:
        yield conn


@contextmanager
def auth_conn():
    with auth_pool().connection() as conn:
        yield conn


def schema_check() -> None:
    """Fail fast at startup if either DB's required shape is missing.

    For claude_viz: 'files' table exists.
    For the auth DB: 'users' table has a JSONB 'config' column.
    Raises RuntimeError on any mismatch.
    """
    with viz_conn() as c:
        row = c.execute(
            "SELECT to_regclass('public.files')"
        ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError(
                "claude_viz.files missing — run backend/schema.sql"
            )
    with auth_conn() as c:
        row = c.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' "
            "AND column_name='config'"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "auth DB users table has no 'config' column"
            )
        if row[0] != "jsonb":
            raise RuntimeError(
                f"auth DB users.config must be JSONB, got {row[0]!r}"
            )


def load_dotenv(path: str = ".env") -> None:
    """Tiny dotenv loader. Avoids the python-dotenv dependency.

    Constraints (intentional simplicity — keep values plain):
    - Values are read literally; quotes are NOT stripped. ADMIN_TOKEN="abc"
      stores the value with the literal quotes.
    - The 'export ' prefix is NOT supported (the line key would become
      'export ADMIN_TOKEN', not 'ADMIN_TOKEN').
    - The first '=' splits key from value, so values may contain '=' freely.
    - Existing env vars are NEVER overwritten (uses os.environ.setdefault).
    - Comment lines (starting with '#') and blank lines are skipped.
    """
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
