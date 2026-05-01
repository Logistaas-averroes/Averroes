"""
db/connection.py

PostgreSQL connection pool for the Logistaas Ads Intelligence System.

Responsibility:
  - Read DATABASE_URL from environment.
  - Initialise a ThreadedConnectionPool (min 1, max 10 connections).
  - Expose get_conn() as a context manager that yields a live connection
    or None when the pool is unavailable.
  - All failures are non-fatal: if DATABASE_URL is absent or the pool cannot
    be created, writes silently no-op and the JSON fallback remains active.

Usage:
    from db.connection import init_pool, get_conn

    init_pool()          # call once at startup

    with get_conn() as conn:
        if conn is None:
            return       # DB unavailable — skip write
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
"""

import contextlib
import logging
import os

import psycopg2
from psycopg2 import pool

log = logging.getLogger(__name__)

_pool = None


def init_pool() -> None:
    """Initialise the connection pool from DATABASE_URL.

    Safe to call multiple times — subsequent calls after a successful
    initialisation are a no-op.  All errors are logged and swallowed.
    """
    global _pool
    if _pool is not None:
        return

    url = os.getenv("DATABASE_URL")
    if not url:
        log.warning("DATABASE_URL not set — database writes disabled")
        return

    try:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, url)
        log.info("Database connection pool initialised")
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to initialise database connection pool: %s", exc)
        _pool = None


@contextlib.contextmanager
def get_conn():
    """Context manager that yields a database connection or None.

    On success the connection is committed on exit.
    On exception the connection is rolled back and the exception re-raised.
    The connection is always returned to the pool in the finally block.

    Yields None (instead of raising) when the pool has not been initialised,
    so callers can use a simple ``if conn is None: return`` guard.
    """
    if _pool is None:
        yield None
        return

    conn = None
    try:
        conn = _pool.getconn()
        yield conn
        conn.commit()
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        if conn is not None:
            try:
                _pool.putconn(conn)
            except Exception:  # noqa: BLE001
                pass
