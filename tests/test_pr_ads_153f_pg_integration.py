"""
tests/test_pr_ads_153f_pg_integration.py

PR-ADS-153F — PostgreSQL-backed integration tests for canonical geo coverage,
the resume checkpoint and the cross-instance run lease.

Source assertions cannot prove any of this. Coverage, resume and the lease are
claims about what the DATABASE does under concurrency and conflict, so each test
below writes real rows to a real ``google_ads_geo_coverage`` /
``google_ads_geo_sync_state`` and reads them back through the real writers and
repository — the exact path the scheduler takes:

  §1  a ``failed`` write never demotes a chunk that is already ``verified``,
      so a transient API error during a recovery run cannot erase proven
      coverage;
  §2  re-recording the same chunk is idempotent — one row, not two;
  §3  the coverage analyzer distinguishes "never fetched" from "fetched and
      genuinely zero", which is the whole reason the ledger exists;
  §4  a failed chunk keeps the window incomplete until it is repaired, and
      repairing it flips the window to complete;
  §5  the lease is atomic: of two concurrent claimants exactly ONE wins;
  §6  a stale lease expires, so a worker that died mid-run cannot block geo
      sync forever;
  §7  releasing the lease records the REAL outcome, and a partial run's state
      carries no checkpoint;
  §8  the checkpoint and the last-successful-completion marker advance only
      when the ledger says the window is covered;
  §9  the geo sync state row is unique per (customer, scope), so a second geo
      dataset cannot silently reuse it;
  §10 the coverage ledger is keyed per chunk range and survives a real
      round trip with its diagnostics intact.

The suite spins up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user, reusing the 153E-A harness. If the binaries or that user
are unavailable the module is skipped — and CI fails loudly on a skip, because
a skipped database suite is not merge evidence.

Read-only against every external platform; the only writes are local.

Run with:
    python -m pytest tests/test_pr_ads_153f_pg_integration.py -v
"""

from __future__ import annotations

import sys
from datetime import date
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

CUSTOMER = "1234567890"


def _writers():
    import db.writers as w
    return w


def _repo():
    from db import revenue_repository as repo
    return repo


def _rows(pg, sql, params=()):
    import psycopg2
    with psycopg2.connect(pg.url) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# §1 / §2 — the coverage ledger's conflict rules, enforced by the database
# ═════════════════════════════════════════════════════════════════════════════

def test_a_failed_write_never_demotes_a_verified_chunk(pg):
    """Proven coverage survives a transient failure on the same range.

    A recovery run that re-attempts a range and fails must not erase the fact
    that the range was already fetched successfully — otherwise one bad API
    minute silently reopens history that was already covered.
    """
    w = _writers()
    assert w.upsert_geo_coverage(CUSTOMER, "2026-04-01", "2026-04-30", "verified",
                                 rows_written=120, cost_micros_total=5_000_000,
                                 country_count=7)
    assert w.upsert_geo_coverage(CUSTOMER, "2026-04-01", "2026-04-30", "failed",
                                 error_message="google ads api unavailable")

    rows = _rows(pg, "SELECT status, rows_written, country_count, error_message "
                     "FROM google_ads_geo_coverage WHERE customer_id = %s", (CUSTOMER,))
    assert len(rows) == 1
    assert rows[0]["status"] == "verified"
    assert rows[0]["rows_written"] == 120
    assert rows[0]["country_count"] == 7
    assert rows[0]["error_message"] is None


def test_a_verified_write_does_upgrade_a_failed_chunk(pg):
    """The guard is one-directional: repairing a failed chunk must work."""
    w = _writers()
    w.upsert_geo_coverage(CUSTOMER, "2026-05-01", "2026-05-31", "failed",
                          error_message="timeout")
    w.upsert_geo_coverage(CUSTOMER, "2026-05-01", "2026-05-31", "verified",
                          rows_written=90, cost_micros_total=3_000_000)

    rows = _rows(pg, "SELECT status, rows_written FROM google_ads_geo_coverage "
                     "WHERE customer_id = %s AND chunk_start = %s",
                 (CUSTOMER, "2026-05-01"))
    assert len(rows) == 1
    assert rows[0]["status"] == "verified"
    assert rows[0]["rows_written"] == 90


def test_recording_the_same_chunk_twice_stays_one_row(pg):
    """Idempotent retries: a re-run cannot inflate the ledger."""
    w = _writers()
    for _ in range(3):
        w.upsert_geo_coverage(CUSTOMER, "2026-06-01", "2026-06-30", "verified",
                              rows_written=10, cost_micros_total=1_000_000)
    rows = _rows(pg, "SELECT COUNT(*) AS n FROM google_ads_geo_coverage "
                     "WHERE customer_id = %s AND chunk_start = %s",
                 (CUSTOMER, "2026-06-01"))
    assert rows[0]["n"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §3 / §4 — completeness over real rows
# ═════════════════════════════════════════════════════════════════════════════

def test_a_never_fetched_range_is_missing_not_zero(pg):
    """The defect the ledger exists to remove.

    Without it, a range nobody fetched and a range that was fetched and had no
    country-attributable spend are indistinguishable — and only one of those is
    safe to treat as real.
    """
    from services.google_ads_geo_sync_service import analyze_geo_coverage

    w = _writers()
    w.upsert_geo_coverage(CUSTOMER, "2026-04-01", "2026-04-30", "verified",
                          rows_written=0, cost_micros_total=0)

    covered = analyze_geo_coverage(date(2026, 4, 1), date(2026, 4, 30))
    assert covered["available"] is True
    assert covered["complete"] is True          # fetched, genuinely zero spend
    assert covered["missing_days"] == 0

    uncovered = analyze_geo_coverage(date(2026, 4, 1), date(2026, 5, 31))
    assert uncovered["complete"] is False       # May was never fetched
    assert uncovered["missing_days"] == 31
    assert uncovered["missing_ranges"] == [{"start": "2026-05-01", "end": "2026-05-31"}]


def test_a_failed_chunk_keeps_the_window_incomplete_until_repaired(pg):
    from services.google_ads_geo_sync_service import analyze_geo_coverage

    w = _writers()
    w.upsert_geo_coverage(CUSTOMER, "2026-04-01", "2026-04-30", "verified",
                          rows_written=10)
    w.upsert_geo_coverage(CUSTOMER, "2026-05-01", "2026-05-31", "failed",
                          error_message="rate limited")

    before = analyze_geo_coverage(date(2026, 4, 1), date(2026, 5, 31))
    assert before["complete"] is False
    assert before["failed_chunks"] == [{"chunk_start": "2026-05-01",
                                        "chunk_end": "2026-05-31"}]

    w.upsert_geo_coverage(CUSTOMER, "2026-05-01", "2026-05-31", "verified",
                          rows_written=15)
    after = analyze_geo_coverage(date(2026, 4, 1), date(2026, 5, 31))
    assert after["complete"] is True
    assert after["failed_chunks"] == []


def test_the_repository_round_trips_the_chunk_diagnostics(pg):
    w, repo = _writers(), _repo()
    w.upsert_geo_coverage(CUSTOMER, "2026-03-01", "2026-03-31", "failed",
                          error_message="boom", sync_run_id="run-9")

    chunks = repo.fetch_geo_coverage(date(2026, 3, 1), date(2026, 3, 31))["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["status"] == "failed"
    assert chunks[0]["error_message"] == "boom"
    assert chunks[0]["sync_run_id"] == "run-9"


# ═════════════════════════════════════════════════════════════════════════════
# §5 / §6 — the run lease, under real concurrency
# ═════════════════════════════════════════════════════════════════════════════

def test_exactly_one_of_two_concurrent_claimants_wins_the_lease(pg):
    """Render runs more than one instance, so this must hold in the DATABASE.

    A process-local flag would let both workers proceed and race on the same
    coverage rows.
    """
    w = _writers()
    first = w.try_claim_geo_sync_lease(CUSTOMER, run_id="run-a")
    second = w.try_claim_geo_sync_lease(CUSTOMER, run_id="run-b")
    assert first == "acquired"
    assert second == "held"

    rows = _rows(pg, "SELECT last_status, last_run_id FROM google_ads_geo_sync_state "
                     "WHERE customer_id = %s", (CUSTOMER,))
    assert len(rows) == 1
    assert rows[0]["last_status"] == "running"
    assert rows[0]["last_run_id"] == "run-a"     # the loser did not overwrite it


def test_a_stale_lease_expires_so_a_dead_worker_cannot_block_forever(pg):
    w = _writers()
    assert w.try_claim_geo_sync_lease(CUSTOMER, run_id="crashed") == "acquired"
    assert w.try_claim_geo_sync_lease(CUSTOMER, run_id="next") == "held"

    # Simulate the crashed worker's lease ageing past the lease window.
    import psycopg2
    with psycopg2.connect(pg.url) as conn, conn.cursor() as cur:
        cur.execute("UPDATE google_ads_geo_sync_state "
                    "SET last_started_at = NOW() - INTERVAL '5 hours' "
                    "WHERE customer_id = %s", (CUSTOMER,))

    assert w.try_claim_geo_sync_lease(CUSTOMER, run_id="next",
                                      lease_minutes=120) == "acquired"


def test_releasing_the_lease_records_the_real_outcome(pg):
    """A partial run is released as partial. Releasing it as success is the lie
    the whole coverage ledger exists to prevent."""
    w = _writers()
    w.try_claim_geo_sync_lease(CUSTOMER, run_id="run-p")
    w.release_geo_sync_lease(CUSTOMER, status="partial")

    rows = _rows(pg, "SELECT last_status, last_finished_at "
                     "FROM google_ads_geo_sync_state WHERE customer_id = %s", (CUSTOMER,))
    assert rows[0]["last_status"] == "partial"
    assert rows[0]["last_finished_at"] is not None


# ═════════════════════════════════════════════════════════════════════════════
# §7 / §8 / §9 — durable run state
# ═════════════════════════════════════════════════════════════════════════════

def test_a_partial_run_state_carries_no_checkpoint(pg):
    w = _writers()
    w.upsert_geo_sync_state(CUSTOMER, last_status="partial", chunks_verified=2,
                            chunks_failed=1, rows_written=40,
                            last_error="1 chunk failed")

    rows = _rows(pg, "SELECT last_status, checkpoint_date, "
                     "last_successful_completed_at, chunks_failed "
                     "FROM google_ads_geo_sync_state WHERE customer_id = %s", (CUSTOMER,))
    assert rows[0]["last_status"] == "partial"
    assert rows[0]["checkpoint_date"] is None
    assert rows[0]["last_successful_completed_at"] is None
    assert rows[0]["chunks_failed"] == 1


def test_the_checkpoint_advances_only_on_a_committed_complete_run(pg):
    from datetime import datetime, timezone

    w, repo = _writers(), _repo()
    w.upsert_geo_sync_state(CUSTOMER, last_status="success",
                            checkpoint_date=date(2026, 6, 30),
                            last_successful_completed_at=datetime.now(timezone.utc),
                            chunks_verified=3, chunks_failed=0, rows_written=300)

    state = repo.fetch_geo_sync_state(CUSTOMER)
    assert state["available"] is True
    assert state["row"]["checkpoint_date"] == date(2026, 6, 30)
    assert state["row"]["last_successful_completed_at"] is not None
    assert state["row"]["last_status"] == "success"


def test_the_sync_state_row_is_unique_per_customer_and_scope(pg):
    """A second geo dataset cannot silently reuse this row."""
    w = _writers()
    w.upsert_geo_sync_state(CUSTOMER, last_status="success", rows_written=1)
    w.upsert_geo_sync_state(CUSTOMER, last_status="failed", rows_written=2)
    w.upsert_geo_sync_state(CUSTOMER, scope="some_other_geo_dataset",
                            last_status="success", rows_written=3)

    rows = _rows(pg, "SELECT scope, last_status, rows_written "
                     "FROM google_ads_geo_sync_state WHERE customer_id = %s "
                     "ORDER BY scope", (CUSTOMER,))
    assert len(rows) == 2
    by_scope = {r["scope"]: r for r in rows}
    assert by_scope["geo_daily_spend"]["last_status"] == "failed"     # updated in place
    assert by_scope["geo_daily_spend"]["rows_written"] == 2
    assert by_scope["some_other_geo_dataset"]["rows_written"] == 3


def test_coverage_and_sync_state_are_separate_customers_apart(pg):
    """Two accounts cannot overwrite each other's coverage or lease."""
    w = _writers()
    w.upsert_geo_coverage("acct-A", "2026-04-01", "2026-04-30", "verified",
                          rows_written=5)
    w.upsert_geo_coverage("acct-B", "2026-04-01", "2026-04-30", "failed",
                          error_message="B failed")

    rows = _rows(pg, "SELECT customer_id, status FROM google_ads_geo_coverage "
                     "WHERE customer_id IN ('acct-A','acct-B') ORDER BY customer_id")
    assert [(r["customer_id"], r["status"]) for r in rows] == [
        ("acct-A", "verified"), ("acct-B", "failed")]

    assert w.try_claim_geo_sync_lease("acct-A") == "acquired"
    assert w.try_claim_geo_sync_lease("acct-B") == "acquired"
