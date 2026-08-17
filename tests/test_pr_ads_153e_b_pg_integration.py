"""
tests/test_pr_ads_153e_b_pg_integration.py

PR-ADS-153E-B — PostgreSQL-backed integration tests for the production revenue
read contract.

Source assertions cannot prove any of this. Each test below writes real rows to
a real ``hubspot_deal_ledger`` and reads them back through
``services.canonical_revenue_service`` — the exact path a page takes:

  §1  the won predicate is enforced IN SQL: `hs_is_closed_won IS NULL` is
      neither won nor lost, and a "won"-looking stage LABEL cannot smuggle a
      non-won deal into revenue;
  §2  the business window is applied by the database, with an EXCLUSIVE upper
      bound, so a deal that closed on the first instant of the next quarter is
      not counted twice;
  §3  a deal whose currency was never proven is a customer with no value — it
      survives the round trip as NULL and never sums as 0.00;
  §4  one deal, re-synced with new attribution, stays ONE row and is counted
      once — the duplicate-revenue defect `gclid_attribution` could produce;
  §5  the scope lattice holds over rows that came out of the database;
  §6  the coverage gate is enforced against real sync state: a ledger whose
      bootstrap never completed serves nothing.

The suite spins up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user, reusing the 153E-A harness. If the binaries or that user
are unavailable the module is skipped — and CI fails loudly on a skip, because
a skipped database suite is not merge evidence.

Read-only against every external platform; the only writes are local.

Run with:
    python -m pytest tests/test_pr_ads_153e_b_pg_integration.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# Reuse the 153E-A cluster harness verbatim — one implementation of "a real
# PostgreSQL", so a fix to the fixture benefits every suite.
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

from analysis import revenue_scope  # noqa: E402
from services import canonical_revenue_service as canonical_revenue  # noqa: E402

pytestmark = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — write straight to the ledger so the READ path is what is tested
# ─────────────────────────────────────────────────────────────────────────────
_INSERT = """
INSERT INTO hubspot_deal_ledger (
    deal_id, deal_name, pipeline_id, deal_stage_id, deal_stage_label,
    hs_is_closed, hs_is_closed_won, deal_created_at, deal_close_date,
    hubspot_lastmodified_at, amount_raw, deal_currency_code,
    amount_in_home_currency, home_currency_code, revenue_usd,
    currency_status, currency_reason, primary_contact_id, association_count,
    association_status, association_reason, gclid, campaign_name_raw,
    keyword_raw, country_raw, source_primary_raw, source_detail_raw,
    acquisition_group, attribution_status, attribution_reason,
    sync_batch_id, source_fetched_at, created_at, updated_at
) VALUES (
    %(deal_id)s, %(deal_name)s, 'default', %(stage_id)s, %(stage_label)s,
    TRUE, %(won)s, '2026-01-01T00:00:00+00:00', %(close_date)s,
    '2026-06-01T00:00:00+00:00', %(amount)s, 'USD',
    %(amount)s, 'USD', %(revenue_usd)s,
    %(currency_status)s, %(currency_reason)s, %(contact_id)s, 1,
    'resolved', 'single_contact', %(gclid)s, %(campaign)s,
    NULL, %(country)s, %(source_primary)s, NULL,
    %(group)s, %(attribution_status)s, 'single_contact',
    NULL, NOW(), NOW(), NOW()
)
ON CONFLICT (deal_id) DO UPDATE SET
    revenue_usd = EXCLUDED.revenue_usd,
    currency_status = EXCLUDED.currency_status,
    gclid = EXCLUDED.gclid,
    campaign_name_raw = EXCLUDED.campaign_name_raw,
    acquisition_group = EXCLUDED.acquisition_group,
    updated_at = NOW()
"""


def _insert(pg, deal_id, *, won=True, close_date="2026-05-04T00:00:00+00:00",
            amount=1000.0, revenue_usd=1000.0, currency_status="verified_usd",
            currency_reason="deal_currency_is_usd", gclid=None, campaign=None,
            country=None, group="unclassified", attribution_status="unclassified",
            stage_id="326093516", stage_label="Deal Won / Payment Received",
            contact_id=None, source_primary=None):
    with pg.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_INSERT, {
                "deal_id": deal_id, "deal_name": f"Deal {deal_id}",
                "stage_id": stage_id, "stage_label": stage_label, "won": won,
                "close_date": close_date, "amount": amount,
                "revenue_usd": revenue_usd, "currency_status": currency_status,
                "currency_reason": currency_reason, "contact_id": contact_id,
                "gclid": gclid, "campaign": campaign, "country": country,
                "source_primary": source_primary, "group": group,
                "attribution_status": attribution_status,
            })
        conn.commit()


_READY_SYNC = """
INSERT INTO hubspot_deal_sync_state (
    scope, bootstrap_status, bootstrap_started_at, bootstrap_completed_at,
    last_modified_watermark, last_incremental_at, last_status, last_error,
    last_sync_mode, deals_seen, pages_fetched, association_failures, updated_at
) VALUES (
    'deals', %(bootstrap_status)s, %(started)s, %(completed)s,
    '2026-06-22T00:00:00+00:00', %(incremental)s, 'success', NULL,
    'incremental', 10, 1, 0, NOW()
)
ON CONFLICT (scope) DO UPDATE SET
    bootstrap_status = EXCLUDED.bootstrap_status,
    bootstrap_started_at = EXCLUDED.bootstrap_started_at,
    bootstrap_completed_at = EXCLUDED.bootstrap_completed_at,
    last_incremental_at = EXCLUDED.last_incremental_at,
    updated_at = NOW()
"""


def _sync_state(pg, *, bootstrap_status="complete",
                started="2026-01-02T00:00:00+00:00",
                completed="2026-01-02T06:00:00+00:00",
                incremental="2026-06-22T03:00:00+00:00"):
    with pg.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_READY_SYNC, {
                "bootstrap_status": bootstrap_status, "started": started,
                "completed": completed, "incremental": incremental,
            })
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# §1 — the won predicate is enforced in SQL
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_won_state_is_neither_won_nor_lost(pg):
    _sync_state(pg)
    _insert(pg, "won", won=True, revenue_usd=100.0)
    _insert(pg, "unknown", won=None, revenue_usd=999.0)
    _insert(pg, "lost", won=False, revenue_usd=888.0)

    snapshot = canonical_revenue.get_revenue_snapshot(
        "all_time", revenue_scope.SCOPE_ALL_SOURCE, now=NOW, include_deals=True)
    assert snapshot["available"] is True
    assert snapshot["won_deals"] == 1
    assert snapshot["revenue_usd"] == 100.0
    assert [d["deal_id"] for d in snapshot["deals"]] == ["won"]
    # The unknown deal is REPORTED, not silently dropped and not counted as won.
    assert snapshot["unknown_won_state_deals"] == 1


def test_a_won_looking_stage_label_cannot_smuggle_revenue_in(pg):
    """The exact defect the legacy `ILIKE '%won%'` predicate produced."""
    _sync_state(pg)
    _insert(pg, "real", won=True, revenue_usd=100.0)
    _insert(pg, "lost-elsewhere", won=False, revenue_usd=5000.0,
            stage_id="999", stage_label="Closed Lost - Won Elsewhere")

    snapshot = canonical_revenue.get_revenue_snapshot("all_time", now=NOW)
    assert snapshot["won_deals"] == 1
    assert snapshot["revenue_usd"] == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# §2 — the window is applied by the database, upper bound EXCLUSIVE
# ─────────────────────────────────────────────────────────────────────────────
def test_window_upper_bound_is_exclusive_so_quarters_do_not_overlap(pg):
    _sync_state(pg)
    # Q1 closes; Q2 opens on the first instant of 1 April.
    _insert(pg, "q1", close_date="2026-03-31T23:59:59+00:00", revenue_usd=10.0)
    _insert(pg, "q2", close_date="2026-04-01T00:00:00+00:00", revenue_usd=20.0)

    last_q = canonical_revenue.get_revenue_snapshot(
        "last_quarter", now=NOW, include_deals=True)
    current_q = canonical_revenue.get_revenue_snapshot(
        "current_quarter", now=NOW, include_deals=True)

    assert [d["deal_id"] for d in last_q["deals"]] == ["q1"]
    assert [d["deal_id"] for d in current_q["deals"]] == ["q2"]
    # Neither deal is counted twice, which an inclusive upper bound would do.
    assert last_q["won_deals"] + current_q["won_deals"] == 2


def test_the_session_timezone_cannot_change_the_selected_population(pg):
    """A DATE cast to `timestamptz` resolves against the SESSION time zone.

    This is the defect Copilot caught: on a server whose session is not UTC, a
    bare date bound silently shifts the window by hours and moves edge deals
    between periods. The contract now normalizes bounds to explicit UTC
    instants, so the SAME rows come back whatever the session is set to.
    """
    from datetime import date, timedelta

    _sync_state(pg)
    # Deals straddling a quarter boundary, at the instants a shift would move.
    _insert(pg, "just-inside", close_date="2026-04-01T00:30:00+00:00", revenue_usd=10.0)
    _insert(pg, "just-before", close_date="2026-03-31T23:30:00+00:00", revenue_usd=20.0)
    _insert(pg, "just-after", close_date="2026-07-01T00:30:00+00:00", revenue_usd=40.0)

    import db.connection as connection

    def _population(session_tz):
        with connection.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET TIME ZONE '{session_tz}'")
            conn.commit()
        base = canonical_revenue.load_won_deals(
            start=date(2026, 4, 1), end=date(2026, 6, 30) + timedelta(days=1))
        assert base["available"] is True
        return sorted(d["deal_id"] for d in base["deals"])

    utc = _population("UTC")
    assert utc == ["just-inside"], utc
    # Sessions either side of UTC, chosen to expose an off-by-hours shift.
    for tz in ("America/Los_Angeles", "Asia/Tokyo", "Pacific/Kiritimati"):
        assert _population(tz) == utc, tz


def test_a_deal_exactly_on_each_boundary_lands_on_the_right_side(pg):
    """Inclusive start, EXCLUSIVE end — asserted at the exact instants."""
    from datetime import datetime as _dt

    _sync_state(pg)
    _insert(pg, "at-start", close_date="2026-04-01T00:00:00+00:00", revenue_usd=1.0)
    _insert(pg, "at-end", close_date="2026-07-01T00:00:00+00:00", revenue_usd=2.0)

    base = canonical_revenue.load_won_deals(
        start=_dt(2026, 4, 1, tzinfo=timezone.utc),
        end=_dt(2026, 7, 1, tzinfo=timezone.utc))
    ids = sorted(d["deal_id"] for d in base["deals"])
    assert ids == ["at-start"], ids


# ─────────────────────────────────────────────────────────────────────────────
# §3 — unproven currency survives the round trip as NULL
# ─────────────────────────────────────────────────────────────────────────────
def test_unproven_currency_round_trips_as_null_never_zero(pg):
    _sync_state(pg)
    _insert(pg, "proven", revenue_usd=250.0)
    _insert(pg, "unproven", amount=None, revenue_usd=None,
            currency_status="unavailable", currency_reason="no_amount")

    snapshot = canonical_revenue.get_revenue_snapshot(
        "all_time", now=NOW, include_deals=True)
    assert snapshot["won_deals"] == 2                    # both are customers
    assert snapshot["revenue_usd"] == 250.0              # only one has a value
    assert snapshot["currency_unavailable_deals"] == 1
    unproven = next(d for d in snapshot["deals"] if d["deal_id"] == "unproven")
    assert unproven["revenue_usd"] is None
    assert unproven["revenue_usd"] != 0.0


# ─────────────────────────────────────────────────────────────────────────────
# §4 — one deal stays one row, and is counted once
# ─────────────────────────────────────────────────────────────────────────────
def test_resyncing_a_deal_cannot_duplicate_its_revenue(pg):
    """`gclid_attribution` keyed rows on an attribution hash, so a re-attributed
    deal became a SECOND row and its revenue was counted twice. The canonical
    ledger is keyed on ``deal_id``."""
    _sync_state(pg)
    _insert(pg, "d1", revenue_usd=5000.0, campaign="Old Campaign",
            gclid=None, group="organic", attribution_status="attributed")
    before = canonical_revenue.get_revenue_snapshot("all_time", now=NOW)

    # Same deal, re-synced with entirely new attribution evidence.
    _insert(pg, "d1", revenue_usd=5000.0, campaign="New Campaign",
            gclid="gclid-d1", group="google_ads", attribution_status="attributed")
    after = canonical_revenue.get_revenue_snapshot(
        "all_time", now=NOW, include_deals=True)

    assert before["won_deals"] == after["won_deals"] == 1
    assert before["revenue_usd"] == after["revenue_usd"] == 5000.0
    # `include_deals` returns raw canonical rows, whose campaign column is
    # `campaign_name_raw` (the display shape is `canonical_deal_rows`).
    assert after["deals"][0]["campaign_name_raw"] == "New Campaign"

    with pg.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM hubspot_deal_ledger WHERE deal_id = 'd1'")
            assert cur.fetchone()[0] == 1


# ─────────────────────────────────────────────────────────────────────────────
# §5 — the scope lattice holds over rows that came out of the database
# ─────────────────────────────────────────────────────────────────────────────
def test_scope_lattice_holds_over_database_rows(pg):
    _sync_state(pg)
    _insert(pg, "everything", revenue_usd=100.0, gclid="g1", campaign="C",
            group="google_ads", attribution_status="attributed")
    _insert(pg, "campaign-only", revenue_usd=200.0, gclid=None, campaign="C",
            group="google_ads", attribution_status="attributed")
    _insert(pg, "google-only", revenue_usd=300.0, gclid=None, campaign=None,
            group="google_ads", attribution_status="attributed")
    _insert(pg, "organic", revenue_usd=400.0, gclid=None, campaign=None,
            group="organic", attribution_status="attributed")

    ladder = canonical_revenue.get_scope_ladder("all_time", now=NOW)
    assert ladder["available"] is True
    assert ladder["lattice_violations"] == []
    assert ladder["revenue_lattice_violations"] == []

    counts = {s: ladder["scopes"][s]["won_deals"] for s in revenue_scope.SCOPE_ORDER}
    assert counts["all_source"] == 4
    assert counts["google_ads_source"] == 3
    assert counts["campaign_attributable"] == 2
    assert counts["gclid_attributable"] == 1

    revenue = {s: ladder["scopes"][s]["revenue_usd"] for s in revenue_scope.SCOPE_ORDER}
    assert revenue["all_source"] == 1000.0
    assert revenue["gclid_attributable"] == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# §6 — the coverage gate is enforced against real sync state
# ─────────────────────────────────────────────────────────────────────────────
def test_a_ledger_without_a_completed_bootstrap_serves_nothing(pg):
    """A readable ledger holding an unknown fraction of history is not truth."""
    _insert(pg, "d1", revenue_usd=1234.0)
    _sync_state(pg, bootstrap_status="in_progress", completed=None)

    snapshot = canonical_revenue.get_revenue_snapshot("all_time", now=NOW)
    assert snapshot["available"] is False
    assert snapshot["reason"] == canonical_revenue.REASON_COVERAGE_NOT_PROVEN
    assert "bootstrap_not_complete" in snapshot["violation_codes"]
    # Nothing is rendered as zero.
    assert snapshot["won_deals"] is None
    assert snapshot["revenue_usd"] is None
    assert snapshot["legacy_fallback_used"] is False


def test_no_post_bootstrap_incremental_also_fails_closed(pg):
    _insert(pg, "d1", revenue_usd=1234.0)
    # Bootstrap completed, but the last incremental predates it.
    _sync_state(pg, incremental="2026-01-01T00:00:00+00:00")

    snapshot = canonical_revenue.get_revenue_snapshot("all_time", now=NOW)
    assert snapshot["available"] is False
    assert "post_bootstrap_incremental_missing" in snapshot["violation_codes"]


def test_a_proven_ledger_serves_revenue_and_reports_its_freshness(pg):
    _sync_state(pg)
    _insert(pg, "d1", revenue_usd=1234.0)

    snapshot = canonical_revenue.get_revenue_snapshot("all_time", now=NOW)
    assert snapshot["available"] is True
    assert snapshot["source"] == "hubspot_deal_ledger"
    assert snapshot["revenue_usd"] == 1234.0
    assert snapshot["as_of"], "a served figure must carry its freshness"
    assert snapshot["readiness"]["ok"] is True
    assert snapshot["readiness"]["violation_codes"] == []
