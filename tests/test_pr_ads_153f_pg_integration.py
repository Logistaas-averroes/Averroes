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

    covered = analyze_geo_coverage(CUSTOMER, date(2026, 4, 1), date(2026, 4, 30))
    assert covered["available"] is True
    assert covered["complete"] is True          # fetched, genuinely zero spend
    assert covered["missing_days"] == 0

    uncovered = analyze_geo_coverage(CUSTOMER, date(2026, 4, 1), date(2026, 5, 31))
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

    before = analyze_geo_coverage(CUSTOMER, date(2026, 4, 1), date(2026, 5, 31))
    assert before["complete"] is False
    assert before["failed_chunks"] == [{"chunk_start": "2026-05-01",
                                        "chunk_end": "2026-05-31"}]

    w.upsert_geo_coverage(CUSTOMER, "2026-05-01", "2026-05-31", "verified",
                          rows_written=15)
    after = analyze_geo_coverage(CUSTOMER, date(2026, 4, 1), date(2026, 5, 31))
    assert after["complete"] is True
    assert after["failed_chunks"] == []


def test_the_repository_round_trips_the_chunk_diagnostics(pg):
    w, repo = _writers(), _repo()
    w.upsert_geo_coverage(CUSTOMER, "2026-03-01", "2026-03-31", "failed",
                          error_message="boom", sync_run_id="run-9")

    chunks = repo.fetch_geo_coverage(CUSTOMER, date(2026, 3, 1),
                                     date(2026, 3, 31))["chunks"]
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


# ═════════════════════════════════════════════════════════════════════════════
# BLOCKER 2 — the range is REPLACED, atomically
# ═════════════════════════════════════════════════════════════════════════════

def _geo_row(customer, spend_date, criterion, campaign="c1", micros=1_000_000):
    return {"customer_id": customer, "currency_code": "GBP",
            "country_criterion_id": criterion, "country_code": "GB",
            "country_name": "United Kingdom", "campaign_id": campaign,
            "spend_date": spend_date, "cost_micros": micros}


def test_a_row_google_stops_reporting_is_removed_by_the_refresh(pg):
    """The seven-day rolling refresh only works if refresh means REPLACE.

    Google restates recent spend. A merge-only write cannot express "this row no
    longer exists", so the stale row would survive and the chunk would then be
    certified over spend Google no longer reports.
    """
    w = _writers()
    w.replace_geo_daily_spend_chunk(CUSTOMER, "2026-04-01", "2026-04-30", [
        _geo_row(CUSTOMER, "2026-04-10", "2826"),
        _geo_row(CUSTOMER, "2026-04-11", "2840"),
    ])
    assert _rows(pg, "SELECT COUNT(*) AS n FROM google_ads_geo_daily_spend "
                     "WHERE customer_id = %s", (CUSTOMER,))[0]["n"] == 2

    # The restated response no longer contains the second row.
    out = w.replace_geo_daily_spend_chunk(CUSTOMER, "2026-04-01", "2026-04-30", [
        _geo_row(CUSTOMER, "2026-04-10", "2826"),
    ])
    assert out["replaced"] is True
    assert out["deleted"] == 2 and out["written"] == 1

    remaining = _rows(pg, "SELECT spend_date, country_criterion_id "
                          "FROM google_ads_geo_daily_spend WHERE customer_id = %s",
                      (CUSTOMER,))
    assert len(remaining) == 1
    assert remaining[0]["country_criterion_id"] == "2826"


def test_a_genuinely_empty_response_empties_the_range(pg):
    """An empty response is a real answer, and must be expressible.

    This is the worst case for a merge-only writer: it writes nothing, every
    stale row survives, and the chunk is still marked verified.
    """
    w = _writers()
    w.replace_geo_daily_spend_chunk(CUSTOMER, "2026-05-01", "2026-05-31",
                                    [_geo_row(CUSTOMER, "2026-05-09", "2826")])
    out = w.replace_geo_daily_spend_chunk(CUSTOMER, "2026-05-01", "2026-05-31", [])
    assert out["replaced"] is True          # explicit success, not a silent zero
    assert out["deleted"] == 1 and out["written"] == 0
    assert _rows(pg, "SELECT COUNT(*) AS n FROM google_ads_geo_daily_spend "
                     "WHERE customer_id = %s", (CUSTOMER,))[0]["n"] == 0


def test_a_failed_replacement_preserves_the_previously_committed_range(pg):
    """Validation happens BEFORE the delete, and the whole thing is one
    transaction — so a bad response cannot leave the range emptied."""
    from db.writers import GeoRangeReplacementError

    w = _writers()
    w.replace_geo_daily_spend_chunk(CUSTOMER, "2026-06-01", "2026-06-30", [
        _geo_row(CUSTOMER, "2026-06-10", "2826"),
        _geo_row(CUSTOMER, "2026-06-11", "2840"),
    ])

    # A row belonging to another account.
    with pytest.raises(GeoRangeReplacementError):
        w.replace_geo_daily_spend_chunk(CUSTOMER, "2026-06-01", "2026-06-30", [
            _geo_row("other-account", "2026-06-12", "2826")])
    # A row outside the requested range.
    with pytest.raises(GeoRangeReplacementError):
        w.replace_geo_daily_spend_chunk(CUSTOMER, "2026-06-01", "2026-06-30", [
            _geo_row(CUSTOMER, "2026-07-01", "2826")])

    kept = _rows(pg, "SELECT spend_date FROM google_ads_geo_daily_spend "
                     "WHERE customer_id = %s ORDER BY spend_date", (CUSTOMER,))
    assert len(kept) == 2, "a rejected replacement must not empty the range"


def test_a_failed_replacement_does_not_verify_new_coverage(pg):
    """No coverage row is written before the replacement commits."""
    from db.writers import GeoRangeReplacementError
    from services.google_ads_geo_sync_service import analyze_geo_coverage

    w = _writers()
    with pytest.raises(GeoRangeReplacementError):
        w.replace_geo_daily_spend_chunk(CUSTOMER, "2026-08-01", "2026-08-31", [
            _geo_row("someone-else", "2026-08-02", "2826")])

    assert _rows(pg, "SELECT COUNT(*) AS n FROM google_ads_geo_coverage "
                     "WHERE customer_id = %s AND chunk_start = %s",
                 (CUSTOMER, "2026-08-01"))[0]["n"] == 0
    cov = analyze_geo_coverage(CUSTOMER, date(2026, 8, 1), date(2026, 8, 31))
    assert cov["complete"] is False


def test_the_replacement_only_touches_its_own_customer_and_range(pg):
    w = _writers()
    w.replace_geo_daily_spend_chunk("acct-A", "2026-04-01", "2026-04-30",
                                    [_geo_row("acct-A", "2026-04-10", "2826")])
    w.replace_geo_daily_spend_chunk("acct-B", "2026-04-01", "2026-04-30",
                                    [_geo_row("acct-B", "2026-04-10", "2826")])
    # Replacing A's range must leave B's rows untouched.
    w.replace_geo_daily_spend_chunk("acct-A", "2026-04-01", "2026-04-30", [])
    assert _rows(pg, "SELECT COUNT(*) AS n FROM google_ads_geo_daily_spend "
                     "WHERE customer_id = 'acct-B'")[0]["n"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# BLOCKER 3 — coverage is account-scoped
# ═════════════════════════════════════════════════════════════════════════════

def test_verified_coverage_for_one_customer_cannot_cover_another(pg):
    from services.google_ads_geo_sync_service import analyze_geo_coverage

    w = _writers()
    w.upsert_geo_coverage("acct-A", "2026-04-01", "2026-04-30", "verified",
                          rows_written=10)

    assert analyze_geo_coverage("acct-A", date(2026, 4, 1), date(2026, 4, 30))["complete"] is True
    b = analyze_geo_coverage("acct-B", date(2026, 4, 1), date(2026, 4, 30))
    assert b["complete"] is False, "account A's coverage must not cover account B"
    assert b["missing_days"] == 30


def test_one_customer_cannot_cause_another_to_skip_its_fetch(pg):
    from services.google_ads_geo_sync_service import _verified_chunk_keys

    w = _writers()
    w.upsert_geo_coverage("acct-A", "2026-04-01", "2026-04-30", "verified",
                          rows_written=10)

    a_keys, a_ok = _verified_chunk_keys("acct-A", date(2026, 4, 1), date(2026, 4, 30))
    b_keys, b_ok = _verified_chunk_keys("acct-B", date(2026, 4, 1), date(2026, 4, 30))
    assert a_ok and b_ok
    assert "2026-04-01:2026-04-30" in a_keys
    assert b_keys == set(), "account B must still fetch a range it never fetched"


def test_failed_chunks_and_missing_ranges_are_isolated_per_customer(pg):
    from services.google_ads_geo_sync_service import analyze_geo_coverage

    w = _writers()
    w.upsert_geo_coverage("acct-A", "2026-05-01", "2026-05-31", "failed",
                          error_message="A failed")
    w.upsert_geo_coverage("acct-B", "2026-05-01", "2026-05-31", "verified",
                          rows_written=4)

    a = analyze_geo_coverage("acct-A", date(2026, 5, 1), date(2026, 5, 31))
    b = analyze_geo_coverage("acct-B", date(2026, 5, 1), date(2026, 5, 31))
    assert a["failed_chunks"] and a["complete"] is False
    assert b["failed_chunks"] == [] and b["complete"] is True


def test_coverage_reads_require_a_customer(pg):
    """No account means no answer — never a silent "nothing is covered"."""
    repo = _repo()
    out = repo.fetch_geo_coverage("", date(2026, 4, 1), date(2026, 4, 30))
    assert out["available"] is False
    assert out["reason"] == "customer_id_required"
    assert repo.fetch_geo_sync_state("")["available"] is False


# ═════════════════════════════════════════════════════════════════════════════
# BLOCKER 4 — the lease is owner-fenced
# ═════════════════════════════════════════════════════════════════════════════

def test_an_expired_worker_cannot_release_the_newer_workers_lease(pg):
    """Expiry is RECOVERY; the token is OWNERSHIP.

    Worker A overruns, worker B legitimately reclaims — and A, still running,
    must not be able to stamp its terminal status over B's live run.
    """
    import psycopg2

    w = _writers()
    assert w.try_claim_geo_sync_lease(CUSTOMER, run_id="A", lease_token="tok-A") == "acquired"
    with psycopg2.connect(pg.url) as conn, conn.cursor() as cur:
        cur.execute("UPDATE google_ads_geo_sync_state "
                    "SET last_started_at = NOW() - INTERVAL '5 hours' "
                    "WHERE customer_id = %s", (CUSTOMER,))
    assert w.try_claim_geo_sync_lease(CUSTOMER, run_id="B", lease_token="tok-B") == "acquired"

    # A finishes late and tries to release / record terminal state.
    assert w.release_geo_sync_lease(CUSTOMER, status="failed", lease_token="tok-A") is False
    assert w.upsert_geo_sync_state(CUSTOMER, lease_token="tok-A",
                                   last_status="success",
                                   checkpoint_date=date(2026, 1, 1)) is False

    row = _rows(pg, "SELECT last_status, last_run_id, lease_token, checkpoint_date "
                    "FROM google_ads_geo_sync_state WHERE customer_id = %s",
                (CUSTOMER,))[0]
    assert row["last_status"] == "running"      # B still owns it
    assert row["last_run_id"] == "B"
    assert row["lease_token"] == "tok-B"
    assert row["checkpoint_date"] is None       # A's checkpoint never landed


def test_the_owner_can_record_its_terminal_state(pg):
    w = _writers()
    w.try_claim_geo_sync_lease(CUSTOMER, run_id="A", lease_token="tok-A")
    assert w.upsert_geo_sync_state(CUSTOMER, lease_token="tok-A",
                                   last_status="success",
                                   checkpoint_date=date(2026, 6, 30)) is True
    row = _rows(pg, "SELECT last_status, checkpoint_date FROM google_ads_geo_sync_state "
                    "WHERE customer_id = %s", (CUSTOMER,))[0]
    assert row["last_status"] == "success"
    assert row["checkpoint_date"] == date(2026, 6, 30)


# ═════════════════════════════════════════════════════════════════════════════
# Additive integrity constraints
# ═════════════════════════════════════════════════════════════════════════════

def test_the_schema_rejects_rows_the_readers_would_misinterpret(pg):
    """The writers already enforce these; stating them in the schema means a
    migration or a manual fix cannot quietly produce a row that lies."""
    import psycopg2

    bad = [
        ("INSERT INTO google_ads_geo_coverage (customer_id, chunk_start, chunk_end, status) "
         "VALUES ('x', '2026-01-01', '2026-01-31', 'probably_fine')"),
        ("INSERT INTO google_ads_geo_coverage (customer_id, chunk_start, chunk_end, status) "
         "VALUES ('x', '2026-02-28', '2026-02-01', 'verified')"),
        ("INSERT INTO google_ads_geo_sync_state (customer_id, scope, last_status) "
         "VALUES ('x', 'geo_daily_spend', 'probably_done')"),
    ]
    for sql in bad:
        with psycopg2.connect(pg.url) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(sql)
