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
import psycopg2.pool

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


def ensure_database_ready() -> tuple[bool, str | None]:
    """Initialize the connection pool and PROVE it can serve a query.

    Returns ``(ready, detail)``. ``detail`` is None when ready.

    Why this exists
    ---------------
    A standalone ``python -m …`` process is not the Flask app: nothing has
    called :func:`init_pool`, so the module-level ``_pool`` is ``None`` and every
    :func:`get_conn` yields ``None``. Each database call then degrades quietly to
    "unavailable", which is right for one optional write and catastrophic for a
    whole command — PR-ADS-155-F1 was raised because
    ``python -m scripts.report_missing_deal_amounts`` reported
    ``canonical_coverage_not_proven`` against a perfectly healthy ledger, purely
    because nothing had initialized the pool in that process.

    So the pool is not merely initialized — it is **PROBED**. :func:`init_pool`
    swallows its own failure and leaves ``_pool = None``, and a pool that exists
    is still not a database that answers, so "we called init_pool" is not
    evidence. ``SELECT 1`` is.

    Read-only and side-effect free. A caller must abort on ``False`` rather than
    continue: with no readable database, every count it would report is an
    artifact of an empty result set, not a measurement.

    Lives here, beside the pool it initializes, so that every entry point shares
    ONE implementation. ``scheduler.incremental_sync.ensure_database_ready``
    delegates to this function rather than keeping a second copy.
    """
    # PR-ADS-154A: every exception interpolated below is REDACTED first. A
    # connection failure can carry the DSN, and a DSN carries the password —
    # and this detail is operator-facing: it reaches logs, JSON reports and
    # stderr. `safe_db_error` also collapses newlines and caps the length.
    #
    # Imported inside the function on purpose: `db.writers` imports this module,
    # so a module-level import here would be a cycle.
    #
    # PR-ADS-155-F1-F1: the fallback used to stringify the exception when the
    # import failed. That inverted the whole point — the one path where the
    # redactor is unavailable is exactly the path that must reveal LESS, not
    # more, and a connection error is the failure most likely to carry the DSN.
    # An unredactable error now yields a CONSTANT: the caller still learns which
    # step failed (the prefixes below say so), and learns nothing about the
    # credentials. A diagnosis is worth having; it is not worth a password.
    try:
        from db.writers import safe_db_error
    except Exception:  # noqa: BLE001  — redaction must never be the thing that fails
        def safe_db_error(exc, limit: int = 300) -> str:  # noqa: ARG001
            return ("[error text withheld: the redactor could not be imported, "
                    "so this message cannot be proven free of credentials]")

    try:
        init_pool()
    except Exception as exc:  # noqa: BLE001
        return False, f"init_pool failed: {safe_db_error(exc)}"

    try:
        with get_conn() as conn:
            if conn is None:
                return False, ("connection pool is not available "
                               "(no DATABASE_URL, or the database is unreachable)")
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
        if not row or row[0] != 1:
            return False, "readiness probe returned no result"
    except Exception as exc:  # noqa: BLE001
        return False, f"readiness probe failed: {safe_db_error(exc)}"

    return True, None
