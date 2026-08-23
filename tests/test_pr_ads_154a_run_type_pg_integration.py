"""
tests/test_pr_ads_154a_run_type_pg_integration.py

PR-ADS-154A — PostgreSQL-backed proof that `runs.run_type` accepts the
scheduler's canonical run type.

Why this must hit a real database
─────────────────────────────────
The defect existed *only* at the PostgreSQL boundary. Every mocked writer test
passed, every scheduler test passed, and production still failed on::

    value too long for type character varying(20)

`runs.run_type` was VARCHAR(20); `daily_incremental_sync` is 22 characters. No
amount of in-process mocking can catch a column width — the column is the thing
under test. So this suite applies the REAL schema to a REAL cluster and writes
the REAL value through the REAL writer.

What it proves
──────────────
  §1  the schema declares a `run_type` wide enough for the canonical value;
  §2  `write_run()` returns a genuine id for `daily_incremental_sync`;
  §3  the stored value reads back byte-for-byte — not truncated, not coerced;
  §4  `init_db()` is idempotent: running it twice is safe and changes nothing;
  §5  the migration widens a legacy VARCHAR(20) column in place;
  §6  existing rows — including shorter legacy run types — survive untouched;
  §7  `write_run_detailed()` reports a rejection honestly instead of as an
      outage, with the driver's diagnosis carried through and redacted.

The suite spins up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user, reusing the 153E-A harness. If the binaries or that user
are unavailable the module is skipped — and CI fails loudly on a skip, because
a skipped database suite is not merge evidence.

Read-only against every external platform; the only writes are local.

Run with:
    python -m pytest tests/test_pr_ads_154a_run_type_pg_integration.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# Reuse the 153E-A cluster harness verbatim — one implementation of "a real
# PostgreSQL", so a fix to the fixture benefits every suite.
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

pytestmark = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

#: The exact value production failed on. Imported from the scheduler rather
#: than retyped, so this suite tracks the contract instead of a copy of it.
from scheduler.incremental_sync import RUN_TYPE  # noqa: E402

_STARTED_AT = "2026-08-20T09:00:00Z"


def _writers():
    import db.writers as w
    return w


def _rows(pg, sql, params=()):
    import psycopg2
    import psycopg2.extras
    with psycopg2.connect(pg.url) as conn, \
            conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _run_type_length(pg) -> int | None:
    return _rows(pg, """
        SELECT character_maximum_length AS len
          FROM information_schema.columns
         WHERE table_name = 'runs' AND column_name = 'run_type'
    """)[0]["len"]


# ═════════════════════════════════════════════════════════════════════════════
# §1–§3 — the canonical run type is writable and round-trips intact
# ═════════════════════════════════════════════════════════════════════════════

def test_the_schema_declares_a_column_wide_enough_for_the_canonical_run_type(pg):
    """The column must fit the value the application has always written.

    Asserted against the value itself rather than a hard-coded 64, so shortening
    the column below what the scheduler emits fails here — whatever the numbers
    happen to be.
    """
    length = _run_type_length(pg)
    assert length is not None, "run_type must stay a bounded varchar, not become TEXT"
    assert length >= len(RUN_TYPE), (
        f"runs.run_type is VARCHAR({length}) but the scheduler writes "
        f"{RUN_TYPE!r} ({len(RUN_TYPE)} characters)")
    assert length == 64        # the contract this PR sets


def test_write_run_returns_a_real_id_for_the_canonical_run_type(pg):
    """The production failure, inverted: this INSERT now succeeds."""
    w = _writers()
    run_id = w.write_run({
        "run_type": RUN_TYPE,
        "started_at": _STARTED_AT,
        "status": "running",
    })
    assert run_id is not None, "write_run returned no id for the canonical run type"
    assert isinstance(run_id, int) and run_id > 0


def test_the_stored_run_type_reads_back_unchanged(pg):
    """Not truncated, not coerced, not silently rewritten.

    A column that merely ACCEPTS the write is not enough — PostgreSQL would
    have raised rather than truncated, but a future `varchar(22)` plus a longer
    run type would fail the same way, so the round trip is asserted directly.
    """
    w = _writers()
    run_id = w.write_run({
        "run_type": RUN_TYPE,
        "started_at": _STARTED_AT,
        "status": "running",
    })
    stored = _rows(pg, "SELECT run_type, status FROM runs WHERE id = %s", (run_id,))
    assert len(stored) == 1
    assert stored[0]["run_type"] == RUN_TYPE
    assert len(stored[0]["run_type"]) == len(RUN_TYPE) == 22
    assert stored[0]["status"] == "running"


# ═════════════════════════════════════════════════════════════════════════════
# §4–§6 — the migration is idempotent and preserves existing rows
# ═════════════════════════════════════════════════════════════════════════════

def test_schema_initialization_is_idempotent(pg):
    """Deployment runs `init_db()` on every boot, so twice must equal once."""
    from db.schema import init_db

    w = _writers()
    first = w.write_run({"run_type": RUN_TYPE, "started_at": _STARTED_AT,
                         "status": "running"})

    init_db()          # a redeploy
    init_db()          # and another

    assert _run_type_length(pg) == 64
    # The row written before the re-initialization is untouched...
    stored = _rows(pg, "SELECT run_type FROM runs WHERE id = %s", (first,))
    assert stored[0]["run_type"] == RUN_TYPE
    # ...and the table still accepts new writes afterwards.
    second = w.write_run({"run_type": RUN_TYPE, "started_at": _STARTED_AT,
                          "status": "running"})
    assert second is not None and second != first


def test_the_migration_widens_a_legacy_narrow_column_in_place(pg):
    """The decisive case: production's table already exists.

    `CREATE TABLE IF NOT EXISTS` shapes only NEW databases, so without the
    ALTER the deployed schema keeps VARCHAR(20) and every incremental run keeps
    failing. This reconstructs the production starting state — a narrow column
    holding real rows — and proves `init_db()` repairs it without a manual SQL
    command and without disturbing the data.
    """
    import psycopg2
    from db.schema import init_db

    w = _writers()
    # Pre-existing history, written under the old narrow column.
    legacy_ids = [
        w.write_run({"run_type": rt, "started_at": _STARTED_AT, "status": "success"})
        for rt in ("daily", "backfill", "revenue_recovery")
    ]
    assert all(legacy_ids)

    # Regress the column to production's shape.
    with psycopg2.connect(pg.url) as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE runs ALTER COLUMN run_type TYPE VARCHAR(20)")
    assert _run_type_length(pg) == 20

    # The production failure reproduces exactly.
    assert w.write_run({"run_type": RUN_TYPE, "started_at": _STARTED_AT,
                        "status": "running"}) is None

    # Deployment repairs it.
    init_db()
    assert _run_type_length(pg) == 64

    # ...and the same write now succeeds.
    repaired = w.write_run({"run_type": RUN_TYPE, "started_at": _STARTED_AT,
                            "status": "running"})
    assert repaired is not None
    assert _rows(pg, "SELECT run_type FROM runs WHERE id = %s",
                 (repaired,))[0]["run_type"] == RUN_TYPE

    # Every pre-existing row survived the widening, value for value. A wider
    # domain still contains the shorter ones.
    surviving = _rows(pg, "SELECT id, run_type FROM runs WHERE id = ANY(%s) ORDER BY id",
                      (legacy_ids,))
    assert [r["run_type"] for r in surviving] == ["daily", "backfill", "revenue_recovery"]
    assert [r["id"] for r in surviving] == sorted(legacy_ids)


def test_widening_twice_is_a_no_op(pg):
    """The guard checks the current length, so a redeploy issues no DDL."""
    from db.schema import init_db

    init_db()
    length_once = _run_type_length(pg)
    init_db()
    assert _run_type_length(pg) == length_once == 64


# ═════════════════════════════════════════════════════════════════════════════
# §7 — a rejection is reported as a rejection, not as an outage
# ═════════════════════════════════════════════════════════════════════════════

def test_a_rejected_run_record_reports_the_real_reason(pg):
    """`write_run_detailed` distinguishes "unreachable" from "refused".

    Reporting the VARCHAR(20) rejection as `database_unavailable` sent an
    operator to check a connection that was already fine. The driver's own
    message IS the diagnosis, so it is carried through.
    """
    import psycopg2

    w = _writers()
    with psycopg2.connect(pg.url) as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE runs ALTER COLUMN run_type TYPE VARCHAR(20)")

    run_id, error = w.write_run_detailed({
        "run_type": RUN_TYPE, "started_at": _STARTED_AT, "status": "running"})
    assert run_id is None
    assert error, "a failed run-record write must say why"
    assert "too long" in error.lower()
    assert "character varying(20)" in error
    # The database WAS reachable — the message must not imply otherwise.
    assert "pool is not available" not in error

    # A successful write reports no error at all.
    from db.schema import init_db
    init_db()
    ok_id, ok_error = w.write_run_detailed({
        "run_type": RUN_TYPE, "started_at": _STARTED_AT, "status": "running"})
    assert ok_id is not None
    assert ok_error is None


def test_the_error_detail_never_carries_the_connection_string(pg):
    """A diagnostic that leaks the DSN leaks the password with it."""
    w = _writers()
    leaky = Exception(
        f'could not connect to server: connection to "{pg.url}" failed; '
        'password=hunter2')
    detail = w.safe_db_error(leaky)
    assert "[redacted]" in detail
    assert "hunter2" not in detail
    assert "postgresql://" not in detail
    assert "postgres://" not in detail
    # ...while an ordinary constraint message survives intact, because it is
    # the whole reason a caller asked.
    plain = w.safe_db_error(
        Exception("value too long for type character varying(20)"))
    assert plain == "value too long for type character varying(20)"
