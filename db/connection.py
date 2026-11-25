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

# PR-ADS-155-F1-F2. Imported at MODULE scope, and safe to: `db.redaction`
# imports nothing from the `db` package, so there is no cycle. The redactor
# previously lived in `db.writers`, which imports THIS module — which is why
# `init_pool` had no way to reach it and logged its exception raw.
from db.redaction import safe_db_error

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
        # PR-ADS-155-F1-F2: REDACTED, never raw. psycopg2 embeds the DSN in
        # several connection failures (a failed password authentication among
        # them), and a DSN carries the password — so the previous
        # `log.error(..., exc)` could put a live production credential into the
        # Render logs. This is the earliest place the secret could escape:
        # redacting only what `ensure_database_ready` RETURNS was too late,
        # because this line had already run.
        log.error("Failed to initialise database connection pool: %s",
                  safe_db_error(exc))
        _pool = None


@contextlib.contextmanager
def get_conn():
    """Context manager that yields a database connection or None.

    On success the connection is committed on exit.
    On exception the connection is rolled back and the exception re-raised.
    The connection is always returned to the pool in the finally block.

    Yields None (instead of raising) when the pool has not been initialised,
    so callers can use a simple ``if conn is None: return`` guard.

    PR-ADS-179A: inside :func:`pinned_read_snapshot` this yields a view of
    the block's ONE pinned connection instead (see there). Everywhere else —
    every web request, every sync — nothing changes.
    """
    pin = _PINNED.get()
    if pin is not None:
        with pin.enter() as view:
            yield view
        return
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


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-179A — one pinned READ snapshot for a block of reads
# ═════════════════════════════════════════════════════════════════════════════
#
# A page builder makes dozens of reads, each through its own ``get_conn()``:
# each is its own transaction, so under READ COMMITTED each sees the database
# as of its own statement. ``pinned_read_snapshot()`` makes every ``get_conn()``
# inside the block (in this thread / context) answer from ONE connection in
# ONE ``REPEATABLE READ, READ ONLY`` transaction, so every read in the block
# sees the database at one instant — and so a value computed inside the block
# is a pure function of (that instant, its arguments), which is what lets the
# certification audits build a shared input once per window
# (``services.snapshot_memo``) and still check exactly what a request at that
# instant would have served.
#
# Readers were written for a transaction of their own, so the pinned view
# keeps that contract inside the shared one:
#
# * each ``get_conn()`` is a SAVEPOINT: a clean exit RELEASEs it; an exception
#   (or the reader's own ``rollback()``) rolls back TO it — the reader's failed
#   statement is undone, the snapshot is not; ``commit()`` releases and re-opens
#   it (a READ ONLY transaction has nothing to commit);
# * a reader's own ``SET TRANSACTION ISOLATION LEVEL REPEATABLE READ`` /
#   ``SET TRANSACTION READ ONLY`` is already satisfied, so it is not re-sent
#   (PostgreSQL refuses SET TRANSACTION ISOLATION LEVEL after the first query);
#   any OTHER transaction mode (READ COMMITTED, SERIALIZABLE, READ WRITE) is a
#   request this snapshot cannot honour and raises ``PinnedSnapshotError`` —
#   fail closed, never a silently weaker read;
# * the transaction is READ ONLY: PostgreSQL itself refuses any write;
# * on exit the transaction is rolled back and the connection returned.
#
# Only the certification audits open one. Threads do not inherit it (a
# ContextVar), so a reader on another thread uses the pool exactly as before.
import contextvars as _contextvars  # noqa: E402
import itertools as _itertools  # noqa: E402
import re as _re  # noqa: E402

PINNED_SNAPSHOT_MODE = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

_PINNED: _contextvars.ContextVar = _contextvars.ContextVar(
    "pinned_read_snapshot", default=None)

#: Transaction-mode statements a reader may send that the pinned snapshot
#: already satisfies. Matched on the whole statement, case- and
#: whitespace-insensitively; a comma between modes is optional.
_SATISFIED_MODES = _re.compile(
    r"^\s*SET\s+TRANSACTION\s+(?:"
    r"ISOLATION\s+LEVEL\s+REPEATABLE\s+READ(?:\s*,?\s*READ\s+ONLY)?"
    r"|READ\s+ONLY(?:\s*,?\s*ISOLATION\s+LEVEL\s+REPEATABLE\s+READ)?"
    r")\s*;?\s*$", _re.IGNORECASE)
_ANY_MODE = _re.compile(
    r"^\s*(?:SET\s+TRANSACTION|SET\s+SESSION\s+CHARACTERISTICS|BEGIN\b|START\s+TRANSACTION"
    r"|COMMIT\b|END\b|ROLLBACK\s*(?:WORK|TRANSACTION)?\s*;?\s*$)", _re.IGNORECASE)


class PinnedSnapshotError(RuntimeError):
    """A reader inside a pinned read snapshot asked for something the snapshot
    cannot give it (another isolation level, a read-write transaction, its own
    BEGIN / COMMIT), or the snapshot's savepoint bookkeeping failed."""


class _PinnedCursor:
    """A reader's cursor on the pinned connection. Statements pass through,
    except transaction-mode statements (see the section comment)."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, vars=None):  # noqa: A002 — psycopg2's own name
        if isinstance(query, str):
            if _SATISFIED_MODES.match(query):
                return None
            if _ANY_MODE.match(query):
                raise PinnedSnapshotError(
                    f"inside a pinned REPEATABLE READ, READ ONLY snapshot a reader "
                    f"may not run {query.strip()[:80]!r}")
        return self._cursor.execute(query, vars)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def __iter__(self):
        return iter(self._cursor)

    def __next__(self):
        return next(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _PinnedView:
    """What ``get_conn()`` yields inside a pinned snapshot: the shared
    connection, seen through one SAVEPOINT."""

    def __init__(self, pin, savepoint: str):
        self._pin = pin
        self._savepoint = savepoint

    def cursor(self, *args, **kwargs):
        return _PinnedCursor(self._pin.conn.cursor(*args, **kwargs))

    def commit(self):
        self._pin.run(f"RELEASE SAVEPOINT {self._savepoint}")
        self._pin.run(f"SAVEPOINT {self._savepoint}")

    def rollback(self):
        self._pin.run(f"ROLLBACK TO SAVEPOINT {self._savepoint}")

    def close(self):
        raise PinnedSnapshotError("a reader may not close the pinned connection")

    @property
    def pinned_read_snapshot(self):
        return self._pin

    def __getattr__(self, name):
        if name in ("autocommit", "set_session", "set_isolation_level", "tpc_begin"):
            raise PinnedSnapshotError(
                f"a reader may not use connection.{name} inside a pinned snapshot")
        return getattr(self._pin.conn, name)


_PIN_SERIALS = _itertools.count(1)


class _Pin:
    """One pinned READ ONLY transaction (``pinned_read_snapshot``).
    ``serial`` names it uniquely for the life of the process."""

    def __init__(self, conn):
        self.conn = conn
        self.serial = next(_PIN_SERIALS)
        self._names = _itertools.count(1)

    def run(self, sql: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql)

    @contextlib.contextmanager
    def enter(self):
        savepoint = f"pinned_read_{next(self._names)}"
        self.run(f"SAVEPOINT {savepoint}")
        view = _PinnedView(self, savepoint)
        try:
            yield view
        except BaseException:
            try:
                self.run(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.run(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:  # noqa: BLE001 — the reader's error is the one to raise
                pass
            raise
        try:
            self.run(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:  # noqa: BLE001
            raise PinnedSnapshotError(
                f"savepoint {savepoint} could not be released ({safe_db_error(exc)}); "
                "the reads inside it are not trusted") from None


def current_read_snapshot():
    """The active pinned read snapshot in this context, or None."""
    return _PINNED.get()


@contextlib.contextmanager
def pinned_read_snapshot():
    """Run every ``get_conn()`` in the block on ONE ``REPEATABLE READ, READ
    ONLY`` transaction (see the section comment). Yields the pin, or None when
    no database is configured (the block then runs exactly as it would
    without it). Nested blocks share the outermost snapshot.

    The snapshot is TAKEN when the block starts (its first statement), so
    every read in the block sees the database as of that instant.
    """
    if _PINNED.get() is not None:
        yield _PINNED.get()
        return
    if _pool is None:
        yield None
        return
    conn = _pool.getconn()
    broken = False
    try:
        with conn.cursor() as cur:
            cur.execute(PINNED_SNAPSHOT_MODE)
            # The snapshot is taken at the first query, not at BEGIN: take it
            # now, so "the instant this window was read" is the block's start.
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            broken = True
        _pool.putconn(conn, close=broken)
        raise
    pin = _Pin(conn)
    token = _PINNED.set(pin)
    try:
        yield pin
    finally:
        _PINNED.reset(token)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            broken = True
        try:
            _pool.putconn(conn, close=broken)
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
    # PR-ADS-155-F1-F2: the redactor is now a module-scope import (see the top
    # of this file), so the ordinary path cannot fail. This re-resolves it
    # defensively anyway, because the guarantee this function makes is about
    # what it RETURNS, and a NameError here would propagate the original
    # exception — DSN and all — to whatever was logging it.
    #
    # PR-ADS-155-F1-F1: the fallback used to stringify the exception. That
    # inverted the whole point — the one path where the redactor is unavailable
    # is exactly the path that must reveal LESS, not more. An unredactable error
    # yields a CONSTANT: the caller still learns which step failed (the prefixes
    # below say so), and learns nothing about the credentials. A diagnosis is
    # worth having; it is not worth a password.
    try:
        from db.redaction import safe_db_error
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
