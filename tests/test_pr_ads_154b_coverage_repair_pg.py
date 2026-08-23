"""
tests/test_pr_ads_154b_coverage_repair_pg.py

PR-ADS-154B — PostgreSQL-backed proof of the coverage SQL and the repair loop.

Why this must hit a real database
─────────────────────────────────
Every defect this PR fixes lives in SQL or in the ledger's key structure, and a
mocked repository cannot express either:

  * the four reads gained a ``customer_id`` filter. Whether that filter actually
    partitions the rows is a property of the WHERE clause, not of the Python
    around it — a stub returning a canned dict passes with or without it.
  * the coverage ledger is UNIQUE on ``(customer_id, chunk_start, chunk_end)``,
    which is precisely why a failed chunk can only be flipped by a re-fetch at
    its own boundaries. That is a schema fact.
  * ``COUNT(DISTINCT customer_id)`` reporting a mixed total is a fact about
    aggregation over real rows.

What it proves
──────────────
  §1  a customer-scoped read returns ONLY that customer's rows, on all four
      reads, while the unscoped call keeps its account-wide meaning;
  §2  mixed accounts and mixed currencies are COUNTED, so a caller can tell a
      single-account total from a blended one;
  §3  a failed chunk survives a re-fetch under different boundaries, and is
      repaired by one at its own — the exact production trap;
  §4  a superseded failure no longer blocks, and a partially-covered one still
      does;
  §5  the repair service verifies from the LEDGER, not from its own counters;
  §6  ``ok`` is False whenever either coverage side is short, and True only when
      both are proven.

The suite reuses the 153E-A throwaway-cluster harness. If the binaries or the
unprivileged ``postgres`` user are unavailable the module skips — and CI fails
loudly on a skip, because a skipped database suite is not merge evidence.

Read-only against every external platform; the only writes are local. Google Ads
and the FX provider are stubbed at the service seams and are never contacted.

Run with:
    python -m pytest tests/test_pr_ads_154b_coverage_repair_pg.py -v
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

pytestmark = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

_A = "111-111-1111"          # the configured account
_B = "999-999-9999"          # a second account that must never leak in

_FROM = date(2026, 1, 1)
_TO = date(2026, 3, 31)


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures — real rows in real tables
# ═════════════════════════════════════════════════════════════════════════════

def _exec(sql, params=()):
    from db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def _spend_row(customer_id, campaign_id, spend_date, micros, currency="GBP"):
    _exec(
        """
        INSERT INTO google_ads_campaign_daily_spend
            (customer_id, currency_code, campaign_id, campaign_name,
             spend_date, cost_micros, spend_account_currency)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (customer_id, campaign_id, spend_date) DO UPDATE
            SET cost_micros = EXCLUDED.cost_micros
        """,
        (customer_id, currency, campaign_id, f"C{campaign_id}",
         spend_date, micros, micros / 1_000_000),
    )


def _geo_row(customer_id, spend_date, micros, currency="GBP", country="2826"):
    _exec(
        """
        INSERT INTO google_ads_geo_daily_spend
            (customer_id, currency_code, country_criterion_id, country_code,
             campaign_id, spend_date, cost_micros)
        VALUES (%s, %s, %s, 'GB', 'c1', %s, %s)
        ON CONFLICT (customer_id, country_criterion_id, campaign_id, spend_date)
        DO UPDATE SET cost_micros = EXCLUDED.cost_micros
        """,
        (customer_id, currency, country, spend_date, micros),
    )


def _fx_row(rate_date, base="GBP", quote="USD", rate=1.25):
    import db.writers as w
    w.upsert_fx_rates([{"rate_date": rate_date, "base_currency": base,
                        "quote_currency": quote, "rate": rate,
                        "provider": "test", "source_version": "v1"}])


def _coverage(customer_id, start, end, status="verified", micros=0):
    import db.writers as w
    assert w.upsert_spend_coverage(customer_id, start, end, status,
                                   rows_written=1, cost_micros_total=micros)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the customer_id filter actually partitions the rows
# ═════════════════════════════════════════════════════════════════════════════

def test_campaign_spend_scoped_to_one_account_excludes_the_other(pg):
    from db import revenue_repository as repo

    _spend_row(_A, "c1", "2026-02-01", 10_000_000)
    _spend_row(_B, "c9", "2026-02-01", 77_000_000)

    scoped = repo.fetch_canonical_campaign_spend(_FROM, _TO, _A)
    assert scoped["total_cost_micros"] == 10_000_000
    assert scoped["customer_id"] == _A
    assert scoped["customer_count"] == 1

    # The unscoped call keeps its account-wide meaning — this is an addition,
    # not a silent change of what every existing caller asked for.
    everyone = repo.fetch_canonical_campaign_spend(_FROM, _TO)
    assert everyone["total_cost_micros"] == 87_000_000
    assert everyone["customer_count"] == 2


def test_geo_total_scoped_to_one_account_excludes_the_other(pg):
    from db import revenue_repository as repo

    _geo_row(_A, "2026-02-01", 9_800_000)
    _geo_row(_B, "2026-02-01", 55_000_000)

    scoped = repo.fetch_geo_daily_spend_total(_FROM, _TO, _A)
    assert scoped["total_cost_micros"] == 9_800_000
    assert scoped["customer_count"] == 1
    assert repo.fetch_geo_daily_spend_total(_FROM, _TO)["customer_count"] == 2


def test_the_coverage_ledger_is_scoped_like_the_geo_ledger_already_was(pg):
    """The defect this closes: B's verified history certifying A's window.

    ``fetch_geo_coverage`` has taken a mandatory customer_id since PR-ADS-153F.
    Its spend counterpart did not, so an account with no coverage of its own
    could be declared fully covered by another account's chunks.
    """
    from db import revenue_repository as repo

    _coverage(_B, "2026-01-01", "2026-03-31", "verified")

    a_chunks = repo.fetch_spend_coverage(_FROM, _TO, _A)["chunks"]
    assert a_chunks == [], "account A has no coverage of its own"

    b_chunks = repo.fetch_spend_coverage(_FROM, _TO, _B)["chunks"]
    assert len(b_chunks) == 1 and b_chunks[0]["customer_id"] == _B
    # Unscoped still sees both, which is exactly why scoping had to be explicit.
    assert len(repo.fetch_spend_coverage(_FROM, _TO)["chunks"]) == 1


def test_fx_coverage_is_measured_over_one_accounts_spend_dates(pg):
    """B's unconverted spend day must not make A's FX coverage look incomplete."""
    from db import revenue_repository as repo

    _spend_row(_A, "c1", "2026-02-01", 10_000_000)
    _fx_row("2026-02-01")
    _spend_row(_B, "c9", "2026-02-05", 10_000_000)      # deliberately no FX rate

    scoped = repo.fetch_fx_coverage(_FROM, _TO, "GBP", "USD", customer_id=_A)
    assert scoped["complete"] is True
    assert scoped["spend_days"] == 1

    blended = repo.fetch_fx_coverage(_FROM, _TO, "GBP", "USD")
    assert blended["complete"] is False
    assert blended["spend_days"] == 2


# ═════════════════════════════════════════════════════════════════════════════
# §2 — a blended total is COUNTED, never presented as a single-account one
# ═════════════════════════════════════════════════════════════════════════════

def test_a_mixed_currency_total_is_reported_as_mixed(pg):
    """MIN(currency_code) names one member of the set; the count sizes it.

    Neither query converts, so a GBP row and a EUR row are summed as though the
    micros were commensurate. The count is what lets the caller refuse.
    """
    from db import revenue_repository as repo

    _spend_row(_A, "c1", "2026-02-01", 10_000_000, currency="GBP")
    _spend_row(_A, "c2", "2026-02-02", 10_000_000, currency="EUR")

    out = repo.fetch_canonical_campaign_spend(_FROM, _TO, _A)
    assert out["currency_count"] == 2
    assert out["currency_code"] in ("GBP", "EUR")     # MIN() — one of them
    assert out["total_cost_micros"] == 20_000_000     # summed unconverted


# ═════════════════════════════════════════════════════════════════════════════
# §3 — the unique key is why a failed chunk needs its OWN boundaries
# ═════════════════════════════════════════════════════════════════════════════

def test_a_refetch_under_different_boundaries_leaves_the_failed_row_standing(pg):
    """The production trap, reproduced against the real unique constraint.

    A chunk that failed as a rolling 7-day range is never rewritten by a monthly
    backfill: different (chunk_start, chunk_end) means a different row. The
    failed row survives, and while ANY failed row was fatal it blocked the
    window forever with no repair short of manual SQL.
    """
    from db import revenue_repository as repo

    _coverage(_A, "2026-02-01", "2026-02-07", "failed")
    _coverage(_A, "2026-02-01", "2026-02-28", "verified")   # a monthly re-fetch

    chunks = repo.fetch_spend_coverage(_FROM, _TO, _A)["chunks"]
    statuses = {(c["chunk_start"], c["chunk_end"]): c["status"] for c in chunks}
    assert statuses[("2026-02-01", "2026-02-07")] == "failed"
    assert statuses[("2026-02-01", "2026-02-28")] == "verified"


def test_a_refetch_at_the_recorded_boundaries_repairs_the_row(pg):
    """...and this is how the repair command clears it: same key, new status."""
    from db import revenue_repository as repo

    _coverage(_A, "2026-02-01", "2026-02-07", "failed")
    _coverage(_A, "2026-02-01", "2026-02-07", "verified")

    chunks = repo.fetch_spend_coverage(_FROM, _TO, _A)["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["status"] == "verified"


def test_the_repair_service_finds_the_failed_ranges_to_retry(pg):
    from services.canonical_coverage_repair_service import failed_spend_chunks

    _coverage(_A, "2026-02-01", "2026-02-07", "failed")
    _coverage(_A, "2026-03-01", "2026-03-31", "verified")
    _coverage(_B, "2026-02-10", "2026-02-17", "failed")     # another account

    found = failed_spend_chunks(_A, _FROM, _TO)
    assert found == [{"chunk_start": "2026-02-01", "chunk_end": "2026-02-07"}]


# ═════════════════════════════════════════════════════════════════════════════
# §4 — completeness over a real ledger
# ═════════════════════════════════════════════════════════════════════════════

def test_a_superseded_failure_no_longer_blocks_the_window(pg):
    from services.canonical_coverage_repair_service import verify_spend_coverage

    _coverage(_A, "2026-02-01", "2026-02-07", "failed")
    _coverage(_A, "2026-01-01", "2026-03-31", "verified")

    out = verify_spend_coverage(_A, _FROM, _TO)
    assert out["complete"] is True
    assert out["failed_chunks"] == []
    assert out["superseded_failed_chunks"] == [
        {"chunk_start": "2026-02-01", "chunk_end": "2026-02-07"}]


def test_a_failure_with_one_unproven_day_still_blocks(pg):
    from services.canonical_coverage_repair_service import verify_spend_coverage

    _coverage(_A, "2026-02-01", "2026-02-07", "failed")
    # Covers all of the failed range except 2026-02-07.
    _coverage(_A, "2026-01-01", "2026-02-06", "verified")
    _coverage(_A, "2026-02-08", "2026-03-31", "verified")

    out = verify_spend_coverage(_A, _FROM, _TO)
    assert out["complete"] is False
    assert out["failed_chunks"] == [
        {"chunk_start": "2026-02-01", "chunk_end": "2026-02-07"}]


def test_an_unfetched_range_is_reported_as_a_missing_range(pg):
    from services.canonical_coverage_repair_service import verify_spend_coverage

    _coverage(_A, "2026-01-01", "2026-02-28", "verified")

    out = verify_spend_coverage(_A, _FROM, _TO)
    assert out["complete"] is False
    assert out["missing_ranges"] == [{"start": "2026-03-01", "end": "2026-03-31"}]


# ═════════════════════════════════════════════════════════════════════════════
# §5–§6 — the repair loop verifies from the ledger and sets `ok` accordingly
# ═════════════════════════════════════════════════════════════════════════════

def _stub_google_ads_and_fx(monkeypatch, *, spend_days, unpublished_fx=()):
    """Stand in for both external reads. Neither platform is contacted.

    The account is configured through the ENVIRONMENT rather than by patching
    ``configured_customer_id``, so the real resolver runs and every module that
    imported it by value sees the same answer.

    ``unpublished_fx`` names the dates the provider refuses; every other day in
    the requested range gets a rate, which is what a real reference-rate feed
    does. Defaulting the other way made each test carry FX noise for days it was
    not about.
    """
    import services.google_ads_spend_service as spend_svc
    import services.fx_service as fx_svc

    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", _A)
    monkeypatch.setattr(
        spend_svc, "fetch_daily_spend",
        lambda s, e: {"customer_id": _A, "source_query_version": "v1", "rows": [
            {"customer_id": _A, "currency_code": "GBP", "campaign_id": "c1",
             "campaign_name": "C1", "spend_date": d, "cost_micros": 1_000_000}
            for d in spend_days if s <= d <= e]})
    monkeypatch.setattr(spend_svc, "fetch_account_daily_spend",
                        lambda s, e: {"rows": []})

    def _rate(d, b, q):
        if d in unpublished_fx:
            raise RuntimeError(f"no rate published for {d}")
        return {"rate_date": d, "base_currency": b, "quote_currency": q,
                "rate": 1.25, "provider": "test", "source_version": "v1"}

    monkeypatch.setattr(fx_svc, "fetch_fx_rate", _rate)


def test_a_complete_repair_reports_ok_and_proves_both_sides(pg, monkeypatch):
    """The success case: coverage read BACK from the ledger, not asserted."""
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)

    out = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    cov = out["coverage"]
    assert out["status"] == "success"
    assert cov["ok"] is True
    assert cov["campaign_coverage_complete"] is True
    assert cov["fx_coverage_complete"] is True
    assert cov["fx_spend_days"] == 3 and cov["fx_covered_days"] == 3
    assert out["customer_id"] == _A


def test_a_missing_fx_date_keeps_ok_false_even_though_spend_is_complete(pg, monkeypatch):
    """Spend can be perfect while FX is short — and that is not success.

    This is half of the production gap: `campaign_coverage_incomplete` and
    `fx_coverage_incomplete` are separate conditions and the command must refuse
    on either.
    """
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days,
                            unpublished_fx={"2026-03-05"})

    out = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    cov = out["coverage"]
    assert out["status"] == "incomplete"
    assert cov["campaign_coverage_complete"] is True
    assert cov["fx_coverage_complete"] is False
    assert "2026-03-05" in cov["fx_missing_dates"]
    assert cov["ok"] is False


def test_a_failed_spend_chunk_keeps_ok_false(pg, monkeypatch):
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx
    import services.google_ads_spend_service as spend_svc

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)

    # February refuses, every other month succeeds.
    real = spend_svc.fetch_daily_spend

    def _flaky(s, e):
        if s.startswith("2026-02"):
            raise RuntimeError("Google Ads returned RESOURCE_EXHAUSTED")
        return real(s, e)

    monkeypatch.setattr(spend_svc, "fetch_daily_spend", _flaky)

    out = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    assert out["coverage"]["ok"] is False
    assert out["coverage"]["campaign_coverage_complete"] is False
    assert out["coverage"]["campaign_failed_chunks"]
    assert out["errors"]


def test_rerunning_after_a_partial_repair_completes_it(pg, monkeypatch):
    """Resume: the second run repairs only what the first could not.

    The chunk that failed is retried at its recorded boundaries, which is the
    only re-fetch the unique key lets flip it.
    """
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx
    import services.google_ads_spend_service as spend_svc

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)
    healthy = spend_svc.fetch_daily_spend

    def _flaky(s, e):
        if s.startswith("2026-02"):
            raise RuntimeError("Google Ads returned RESOURCE_EXHAUSTED")
        return healthy(s, e)

    monkeypatch.setattr(spend_svc, "fetch_daily_spend", _flaky)
    first = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    assert first["coverage"]["ok"] is False

    # The API recovers; the same command is re-run unchanged.
    monkeypatch.setattr(spend_svc, "fetch_daily_spend", healthy)
    second = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    assert second["coverage"]["ok"] is True
    assert second["coverage"]["campaign_failed_chunks"] == []


def test_the_repair_is_idempotent(pg, monkeypatch):
    """Running it twice on healthy data changes nothing and stays ok."""
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx
    from db import revenue_repository as repo

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)

    first = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    total_after_first = repo.fetch_canonical_campaign_spend(_FROM, _TO, _A)["total_cost_micros"]

    second = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    total_after_second = repo.fetch_canonical_campaign_spend(_FROM, _TO, _A)["total_cost_micros"]

    assert first["coverage"]["ok"] is True and second["coverage"]["ok"] is True
    assert total_after_first == total_after_second, "a rerun must not double-count"


def test_restart_also_retries_the_failed_boundary_chunks(pg, monkeypatch):
    """`--restart` must repair failed rows too — review finding on this PR.

    The boundary retry was gated on `resume`, so a restart skipped it. That was
    wrong in the one direction that matters: `--restart` is what an operator
    reaches for when a range looks wrong, and it re-fetches in MONTHLY chunks,
    which write different ledger keys and leave the original failed 7-day rows
    standing. The mode most likely to be used on a damaged window was the mode
    that skipped the only step able to repair it.
    """
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx
    from db import revenue_repository as repo

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)
    # A failure recorded under the scheduler's rolling 7-day chunking.
    _coverage(_A, "2026-02-01", "2026-02-07", "failed")

    out = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO, resume=False)

    assert out["spend_backfill"]["retried_failed_chunks"] == [
        {"chunk_start": "2026-02-01", "chunk_end": "2026-02-07", "status": "success"}]
    # The row itself is now verified, not merely superseded by a monthly chunk.
    rows = {(c["chunk_start"], c["chunk_end"]): c["status"]
            for c in repo.fetch_spend_coverage(_FROM, _TO, _A)["chunks"]}
    assert rows[("2026-02-01", "2026-02-07")] == "verified"
    assert out["coverage"]["ok"] is True


def test_resume_skips_ranges_the_ledger_already_proves(pg, monkeypatch):
    """`resume` must mean "repair what is missing", not "re-fetch everything".

    `run_google_ads_spend_backfill` consults `resume` only when a
    `load_completed` callback names the chunks to skip. Without one, `resume`
    changed nothing and every run re-fetched the whole range — the wrong
    contract, and a great deal of Google Ads quota.
    """
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx
    import services.google_ads_spend_service as spend_svc

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)

    fetched: list[tuple] = []
    real = spend_svc.fetch_daily_spend
    monkeypatch.setattr(spend_svc, "fetch_daily_spend",
                        lambda s, e: (fetched.append((s, e)), real(s, e))[1])

    first = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    assert first["coverage"]["ok"] is True
    assert len(fetched) == 3, "first pass fetches all three months"

    fetched.clear()
    second = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    assert second["coverage"]["ok"] is True
    assert fetched == [], "a proven range must not be re-fetched on resume"
    assert second["spend_backfill"]["chunks_skipped_already_verified"] == 3


def test_restart_refetches_even_a_proven_range(pg, monkeypatch):
    """The other half of the contract: `--restart` deliberately redoes the work."""
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx
    import services.google_ads_spend_service as spend_svc

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)
    repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)

    fetched: list[tuple] = []
    real = spend_svc.fetch_daily_spend
    monkeypatch.setattr(spend_svc, "fetch_daily_spend",
                        lambda s, e: (fetched.append((s, e)), real(s, e))[1])

    out = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO, resume=False)
    assert len(fetched) == 3, "restart re-fetches every chunk"
    assert out["spend_backfill"]["chunks_skipped_already_verified"] == 0
    assert out["coverage"]["ok"] is True


def test_resume_refetches_a_partially_covered_chunk(pg, monkeypatch):
    """Partial coverage is not proof, so the whole chunk is redone.

    Redoing a little proven work is the cheap side of this trade; skipping an
    unproven day is the expensive one.
    """
    from services.canonical_coverage_repair_service import already_verified_chunk_keys

    # January fully verified; February only half; March untouched.
    _coverage(_A, "2026-01-01", "2026-01-31", "verified")
    _coverage(_A, "2026-02-01", "2026-02-14", "verified")

    keys = already_verified_chunk_keys(_A, _FROM, _TO, chunk_months=1)
    assert keys == ["2026-01-01:2026-01-31"]


def test_a_dry_run_writes_nothing_and_is_never_ok(pg, monkeypatch):
    """A run that writes nothing proves nothing, however healthy it looks."""
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx
    from db import revenue_repository as repo

    days = ["2026-01-05", "2026-02-05", "2026-03-05"]
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)

    out = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO, dry_run=True)
    assert out["status"] == "dry_run"
    assert out["coverage"]["ok"] is False
    assert repo.fetch_canonical_campaign_spend(_FROM, _TO, _A)["total_cost_micros"] == 0


def test_an_inverted_range_is_refused_before_any_external_call(pg, monkeypatch):
    from services.canonical_coverage_repair_service import (
        CoverageRepairInputError, repair_canonical_spend_and_fx,
    )
    import services.google_ads_spend_service as spend_svc

    called = []
    monkeypatch.setattr(spend_svc, "fetch_daily_spend",
                        lambda s, e: called.append((s, e)) or {"rows": []})

    with pytest.raises(CoverageRepairInputError):
        repair_canonical_spend_and_fx(date_from=_TO, date_to=_FROM)
    assert called == [], "Google Ads must not be contacted for an invalid range"


def test_both_boundary_days_are_inside_the_repaired_range(pg, monkeypatch):
    """Inclusive start AND inclusive end — the canonical window contract.

    Every coverage query filters `spend_date >= start AND spend_date <= end`. A
    repair that treated the end as exclusive would leave the last day of every
    window unproven, which is the single most likely day to be missing.
    """
    from services.canonical_coverage_repair_service import repair_canonical_spend_and_fx
    from db import revenue_repository as repo

    days = ["2026-01-01", "2026-03-31"]        # exactly the two boundaries
    _stub_google_ads_and_fx(monkeypatch, spend_days=days)

    out = repair_canonical_spend_and_fx(date_from=_FROM, date_to=_TO)
    assert out["bounds"] == "inclusive_start_inclusive_end"
    assert out["coverage"]["ok"] is True

    stored = repo.fetch_canonical_campaign_spend(_FROM, _TO, _A)
    assert stored["total_cost_micros"] == 2_000_000, "both boundary days stored"
    assert out["coverage"]["fx_spend_days"] == 2


def test_micros_convert_to_native_currency_on_both_sides(pg):
    """A 6-decimal native figure, from BIGINT micros, on campaign and geo alike.

    Both totals must divide by the same 1e6 or the reconciliation compares a
    number against a millionth of itself.
    """
    from db import revenue_repository as repo

    _spend_row(_A, "c1", "2026-02-01", 1_234_567)
    _geo_row(_A, "2026-02-01", 1_234_567)

    campaign = repo.fetch_canonical_campaign_spend(_FROM, _TO, _A)
    geo = repo.fetch_geo_daily_spend_total(_FROM, _TO, _A)
    assert campaign["total_spend"] == pytest.approx(1.234567)
    assert geo["total_spend"] == pytest.approx(1.234567)
    assert campaign["total_cost_micros"] == geo["total_cost_micros"] == 1_234_567
