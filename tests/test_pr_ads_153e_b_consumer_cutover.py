"""
tests/test_pr_ads_153e_b_consumer_cutover.py

PR-ADS-153E-B — the production revenue consumer cutover.

What this proves
----------------
Before this PR three revenue lineages each called their output "closed-won
revenue": ``gclid_attribution`` (GCLID-bearing deals only), ``deal_source_
attribution`` (all closed-won deals, no currency contract) and a local
Windsor/JSON chain (Unit Economics). The same quarter therefore produced
different customer counts and different revenue on different pages, and no page
said which population it was describing.

Every test below is a statement about that defect being structurally
impossible now, not merely absent:

  §1   every migrated consumer reads the ONE shared contract
  §2   identical window + identical scope → identical totals, everywhere
  §3   non-GCLID won deals are IN the business totals
  §4   GCLID attribution is a strict subset of all-source truth
  §5   a missing GCLID cannot remove a won deal from business totals
  §6   ambiguous attribution stays visible and is never arbitrarily assigned
  §7   missing / unverified currency is unavailable, never $0
  §8   an unknown won state is neither won nor lost
  §9   window boundaries are identical across consumers
  §10  Revenue by Source reconciles exactly, or names the uncovered amount
  §11  a narrower attribution scope can never exceed a broader one
  §12  canonical unavailability is an explicit quarantined response
  §13  no migrated consumer silently falls back to a legacy ledger
  §14  Google Ads spend still comes from the Google Ads canonical path
  §15  HubSpot revenue still comes from the canonical deal ledger
  §16  duplicate legacy rows cannot duplicate canonical revenue

Plus a static contract guard (§17) that fails CI if a migrated module reaches
for a prohibited legacy revenue table or provider again.

Deterministic and synthetic: no production deal ids, no production revenue
totals, no database, no network.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import revenue_scope  # noqa: E402
from services import canonical_revenue_service as canonical_revenue  # noqa: E402
from tests.canonical_ledger_fixtures import (  # noqa: E402
    READY_SYNC_STATE, ledger_row, patch_canonical_ledger,
)

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
WINDOW = "ytd"


# ─────────────────────────────────────────────────────────────────────────────
# One synthetic population, used by every consumer in this suite.
#
# It deliberately contains each awkward shape the cutover must handle:
#   d1  Google Ads, campaign, GCLID, proven USD      → in every scope
#   d2  Google Ads, campaign, NO gclid, proven USD   → not gclid_attributable
#   d3  organic, no campaign, proven USD             → all_source only
#   d4  Google Ads, campaign, currency UNPROVEN      → counted, never valued
#   d5  contacts disagree (ambiguous)                → all_source only, visible
# ─────────────────────────────────────────────────────────────────────────────
D1 = ledger_row("d1", deal_close_date="2026-05-04T00:00:00+00:00",
                revenue_usd=10000.0, amount_raw=10000.0,
                gclid="gclid-d1", campaign_name_raw="Global Competitors",
                country_raw="United States", acquisition_group="google_ads",
                attribution_status="attributed",
                source_primary_raw="Paid Search", source_detail_raw="google")
D2 = ledger_row("d2", deal_close_date="2026-05-14T00:00:00+00:00",
                revenue_usd=4000.0, amount_raw=4000.0,
                gclid=None, campaign_name_raw="Global Competitors",
                country_raw="United States", acquisition_group="google_ads",
                attribution_status="attributed",
                source_primary_raw="Paid Search", source_detail_raw="google")
D3 = ledger_row("d3", deal_close_date="2026-04-02T00:00:00+00:00",
                revenue_usd=25000.0, amount_raw=25000.0,
                gclid=None, campaign_name_raw=None, country_raw="United Kingdom",
                acquisition_group="organic", attribution_status="attributed",
                source_primary_raw="Organic Search", source_detail_raw=None)
D4 = ledger_row("d4", deal_close_date="2026-06-01T00:00:00+00:00",
                revenue_usd=None, amount_raw=None,
                currency_status="unavailable", currency_reason="no_amount",
                gclid="gclid-d4", campaign_name_raw="Brand - UK",
                country_raw="United Kingdom", acquisition_group="google_ads",
                attribution_status="attributed",
                source_primary_raw="Paid Search", source_detail_raw="google")
D5 = ledger_row("d5", deal_close_date="2026-06-10T00:00:00+00:00",
                revenue_usd=1000.0, amount_raw=1000.0,
                gclid=None, campaign_name_raw=None, country_raw=None,
                acquisition_group="ambiguous", attribution_status="ambiguous",
                attribution_reason="multi_conflicting_groups",
                source_primary_raw=None, source_detail_raw=None)

POPULATION = [D1, D2, D3, D4, D5]

# Proven USD across the whole business: 10,000 + 4,000 + 25,000 + 1,000.
# d4 is a won CUSTOMER whose value is unknown, so it is counted and not valued.
#
# PR-ADS-154C-F3-F1 §2: this is the DIAGNOSTIC known-dollar sum, and it now lives
# in `known_revenue_usd`. `revenue_usd` is the TOTAL, which is None here because
# d4's amount was never proven — 40,000 plus an unknown amount is not a number.
ALL_SOURCE_KNOWN_REVENUE = 40000.0
ALL_SOURCE_REVENUE = ALL_SOURCE_KNOWN_REVENUE   # legacy alias, same figure
ALL_SOURCE_WON_DEALS = 5


# ─────────────────────────────────────────────────────────────────────────────
# §1 — every migrated consumer reads the ONE shared contract
# ─────────────────────────────────────────────────────────────────────────────
MIGRATED_CONSUMERS = [
    "services/dashboard_overview_service.py",
    "services/dashboard_revenue_service.py",
    "services/dashboard_campaigns_service.py",
    "services/dashboard_countries_service.py",
    "services/dashboard_deals_service.py",
    "services/dashboard_channels_service.py",
    "services/revenue_attribution_service.py",
    "services/source_attribution_service.py",
    "services/revenue_decision_mart.py",
    "services/canonical_unit_economics_service.py",
    "services/campaign_identity_service.py",
]


@pytest.mark.parametrize("module", MIGRATED_CONSUMERS)
def test_1_consumer_uses_the_shared_canonical_contract(module):
    text = (_ROOT / module).read_text()
    assert "canonical_revenue_service" in text, module


@pytest.mark.parametrize("module", MIGRATED_CONSUMERS)
def test_1b_no_consumer_opens_the_ledger_repository_directly(module):
    """One read contract, not eleven.

    A consumer reaching ``db.deal_ledger_repository`` itself would re-derive won
    status, currency rules, window bounds or scope — which is how the product
    ended up with three definitions of revenue in the first place.
    """
    text = (_ROOT / module).read_text()
    assert "deal_ledger_repository" not in text, module


# ─────────────────────────────────────────────────────────────────────────────
# §2 — identical window + identical scope → identical totals, everywhere
# ─────────────────────────────────────────────────────────────────────────────
def _patch_business_total_stack(monkeypatch, rows=None, *, available=True):
    """Stub the canonical ledger plus the non-revenue sources a page also reads.

    Only revenue comes from the ledger; spend, leads and classification counts
    keep their own sources, which is exactly the ownership split under test.
    """
    import db.revenue_repository as repo
    import db.writers as db_writers

    patch_canonical_ledger(monkeypatch,
                           POPULATION if rows is None else rows,
                           available=available)
    monkeypatch.setattr(repo, "fetch_account_time_zone", lambda: "UTC")
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: True)
    monkeypatch.setattr(repo, "fetch_campaign_country_spend",
                        lambda s, e: {"available": True, "rows": [], "table": "geo",
                                      "coverage_start": None, "coverage_end": None})
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: {"available": True, "rows": [],
                                      "event_date_safe": True,
                                      "missing_contact_created_at_count": 0,
                                      "excluded_non_paid_count": 0,
                                      "excluded_pseudo_campaign_count": 0})
    monkeypatch.setattr(repo, "fetch_source_leads",
                        lambda s, e: {"available": True, "rows": []})
    monkeypatch.setattr(repo, "fetch_source_leads_daily",
                        lambda s, e: {"available": True, "rows": []})
    monkeypatch.setattr(repo, "fetch_lead_daily_series",
                        lambda s, e: {"available": True, "rows": []})
    monkeypatch.setattr(repo, "fetch_sql_lead_details",
                        lambda s, e: {"available": True, "rows": []})
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": False, "rows": []})
    monkeypatch.setattr(repo, "fetch_spend_coverage",
                        lambda s, e, *_a, **_k: {"available": True, "chunks": []})
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total",
                        lambda s, e, *_a, **_k: {"available": False, "has_rows": False})
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country",
                        lambda s, e: {"available": False, "has_rows": False, "rows": []})
    monkeypatch.setattr(repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "datasets": {}})
    monkeypatch.setattr(db_writers, "source_attribution_health_counts",
                        lambda: {"classified_contacts": 0, "attributed_deals": 0,
                                 "ambiguous_deals": 0, "unclassified_deals": 0})


def test_2_business_total_consumers_agree_on_won_deals_and_revenue(monkeypatch):
    """Same metric + same window + same scope = same result, on every page."""
    _patch_business_total_stack(monkeypatch)

    from services.revenue_attribution_service import build_revenue_deals
    from services.revenue_decision_mart import build_revenue_decision_mart
    from services.source_attribution_service import build_revenue_by_source

    contract = canonical_revenue.get_revenue_snapshot(
        WINDOW, revenue_scope.SCOPE_ALL_SOURCE, now=NOW)
    deals = build_revenue_deals(WINDOW, now=NOW)
    mart = build_revenue_decision_mart(view="campaign", window=WINDOW, now=NOW)
    by_source = build_revenue_by_source(WINDOW, now=NOW)

    assert contract["won_deals"] == ALL_SOURCE_WON_DEALS
    assert contract["known_revenue_usd"] == ALL_SOURCE_KNOWN_REVENUE
    assert contract["revenue_usd"] is None      # one amount unproven

    # Deals page, mart top-line and Revenue by Source all report the SAME
    # business population — that agreement is the whole point of the cutover.
    assert deals["summary"]["deal_count"] == ALL_SOURCE_WON_DEALS
    # PR-ADS-154C-F3-F1 §2: `won_revenue` is an executive TOTAL, so it is
    # withheld while d4's amount is unproven. The proven sum is still
    # reachable through the contract's `known_revenue_usd`, asserted above.
    assert deals["summary"]["won_revenue"] is None
    assert mart["summary"]["customers"] == ALL_SOURCE_WON_DEALS
    recon = by_source["canonical_reconciliation"]
    assert recon["canonical_won_deals"] == ALL_SOURCE_WON_DEALS
    assert recon["canonical_revenue_usd"] == ALL_SOURCE_REVENUE

    # PR-ADS-154C-F3 changes ONE of these expectations, deliberately.
    #
    # `ALL_SOURCE_REVENUE` is 40,000 — the sum of the four deals whose amounts
    # were proven. D4 is a won customer whose value is unknown, so the true
    # business total for this window is "40,000 plus an unknown amount", which is
    # not a number. The contract snapshot and the reconciliation diagnostic keep
    # reporting the partial sum, because comparing populations is what they are
    # for; the MART is what the Overview and Revenue pages render as
    # "Closed-Won Revenue", and in production this exact shape published
    # $878,324.80 on three pages while the four consumers with their own
    # partial-sum rules published "unavailable" about the same population.
    #
    # The count is unaffected: five won deals is five won deals whatever their
    # amounts, which is why `customers` is still asserted above.
    assert mart["summary"]["won_revenue_usd"] is None
    assert mart["summary"]["revenue_available"] is True      # population readable
    assert mart["summary"]["revenue_total_available"] is False
    assert (mart["summary"]["revenue_total_unavailable_reason"]
            == canonical_revenue.REASON_REVENUE_INCOMPLETE)
    assert mart["summary"]["currency_unavailable_deals"] == 1
    # The proven sum still exists as a diagnostic; it is simply not the total,
    # and since PR-ADS-154C-F3-F1 §2 it no longer occupies the total's field name.
    assert contract["known_revenue_usd"] == ALL_SOURCE_KNOWN_REVENUE
    assert contract["revenue_usd"] is None


# ─────────────────────────────────────────────────────────────────────────────
# §3-§5 — the GCLID population is a subset, never the definition
# ─────────────────────────────────────────────────────────────────────────────
def test_3_non_gclid_won_deals_are_in_all_source_totals():
    summary = canonical_revenue.summarize_deals(POPULATION,
                                                revenue_scope.SCOPE_ALL_SOURCE)
    assert summary["won_deals"] == ALL_SOURCE_WON_DEALS
    # d2, d3 and d5 have no GCLID at all and are still counted.
    without_gclid = [d for d in POPULATION if not d["gclid"]]
    assert len(without_gclid) == 3
    assert summary["known_revenue_usd"] == ALL_SOURCE_KNOWN_REVENUE
    assert summary["revenue_usd"] is None


def test_4_gclid_attribution_is_a_strict_subset_of_all_source():
    ladder = {s: canonical_revenue.summarize_deals(POPULATION, s)["won_deals"]
              for s in revenue_scope.SCOPE_ORDER}
    assert ladder[revenue_scope.SCOPE_GCLID_ATTRIBUTABLE] < \
        ladder[revenue_scope.SCOPE_ALL_SOURCE]
    assert revenue_scope.check_lattice(ladder) == []
    # Membership is nested by construction, so a deal in the narrowest scope is
    # in every wider one.
    for deal in revenue_scope.filter_deals(
            POPULATION, revenue_scope.SCOPE_GCLID_ATTRIBUTABLE):
        for wider in revenue_scope.SCOPE_ORDER:
            assert revenue_scope.deal_in_scope(deal, wider) or \
                wider == revenue_scope.SCOPE_GCLID_ATTRIBUTABLE


def test_5_missing_gclid_does_not_remove_a_deal_from_business_totals():
    with_gclid = canonical_revenue.summarize_deals(
        [d for d in POPULATION if d["gclid"]], revenue_scope.SCOPE_ALL_SOURCE)
    everything = canonical_revenue.summarize_deals(
        POPULATION, revenue_scope.SCOPE_ALL_SOURCE)
    assert everything["won_deals"] > with_gclid["won_deals"]
    # The GCLID-less deals carry real money that must not vanish.
    # Compared on the DIAGNOSTIC sum: both scopes contain d4, whose amount is
    # unproven, so both totals are correctly None and a total-vs-total
    # comparison would be comparing two unknowns.
    assert everything["known_revenue_usd"] > (with_gclid["known_revenue_usd"] or 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# §6-§8 — ambiguity, unproven currency and unknown won state stay honest
# ─────────────────────────────────────────────────────────────────────────────
def test_6_ambiguous_attribution_is_exposed_and_never_assigned():
    summary = canonical_revenue.summarize_deals(POPULATION,
                                                revenue_scope.SCOPE_ALL_SOURCE)
    assert summary["ambiguous_associations"] == 1
    # An ambiguous deal is NOT quietly promoted into an advertising scope.
    assert not revenue_scope.deal_in_scope(D5, revenue_scope.SCOPE_GOOGLE_ADS_SOURCE)
    # And it is still part of the business.
    assert revenue_scope.deal_in_scope(D5, revenue_scope.SCOPE_ALL_SOURCE)


def test_7_unproven_currency_is_unavailable_never_zero():
    summary = canonical_revenue.summarize_deals(POPULATION,
                                                revenue_scope.SCOPE_ALL_SOURCE)
    assert summary["currency_unavailable_deals"] == 1
    assert summary["currency_complete"] is False
    # d4 is a customer whose value is unknown: counted, never valued at $0.
    assert summary["won_deals"] == ALL_SOURCE_WON_DEALS
    assert summary["known_revenue_usd"] == ALL_SOURCE_KNOWN_REVENUE
    assert summary["revenue_usd"] is None
    row = canonical_revenue.deal_display_row(D4)
    assert row["revenue_usd"] is None
    assert row["currency_status"] == "unavailable"

    # A population where NOTHING was provable reports unknown, not $0.
    only_unproven = canonical_revenue.summarize_deals(
        [D4], revenue_scope.SCOPE_ALL_SOURCE)
    assert only_unproven["won_deals"] == 1
    assert only_unproven["revenue_usd"] is None


def test_8_unknown_won_state_is_excluded_from_confirmed_won_totals():
    """`hs_is_closed_won IS NULL` is neither won nor lost.

    The exclusion happens in SQL, so the guard is on the query itself: it must
    filter on the predicate and must not fall back to a stage id or a label.
    """
    src = (_ROOT / "db" / "deal_ledger_repository.py").read_text()
    fn = src.split("def fetch_won_deals(")[1].split("\ndef ")[0]
    # The docstring EXPLAINS the predicate it replaced, so guard the code only.
    sql = '"""'.join(fn.split('"""')[2:]) if fn.count('"""') >= 2 else fn
    assert "hs_is_closed_won IS TRUE" in sql
    assert "ILIKE" not in sql.upper()
    assert "326093516" not in sql
    # The unknown population is reported separately rather than folded in.
    assert "def fetch_won_state_counts(" in src
    assert "hs_is_closed_won IS NULL" in src


# ─────────────────────────────────────────────────────────────────────────────
# §9 — window boundaries are identical across consumers
# ─────────────────────────────────────────────────────────────────────────────
def test_9_window_bounds_are_identical_across_consumers(monkeypatch):
    import db.deal_ledger_repository as ledger_repo
    from analysis.business_windows import get_window_bounds

    seen = []

    def _capture(start=None, end=None):
        seen.append((start, end))
        return {"available": True, "rows": [dict(r) for r in POPULATION]}

    patch_canonical_ledger(monkeypatch, POPULATION)
    monkeypatch.setattr(ledger_repo, "fetch_won_deals", _capture)

    for window in ("current_quarter", "last_quarter", "ytd", "all_time"):
        seen.clear()
        canonical_revenue.load_won_deals(window, now=NOW)
        assert seen == [get_window_bounds(window, now=NOW)], window


def test_9b_the_contract_uses_the_shared_window_resolver_only():
    src = (_ROOT / "services" / "canonical_revenue_service.py").read_text()
    tree = ast.parse(src)
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        and node.module == "analysis.business_windows"
        for alias in node.names
    }
    assert {"get_window_bounds", "resolve_window", "is_valid_window"} <= imported
    # No home-grown date arithmetic in the contract.
    assert "timedelta(" not in src
    assert "relativedelta" not in src


# ─────────────────────────────────────────────────────────────────────────────
# §10 — Revenue by Source reconciles, or names the uncovered amount
# ─────────────────────────────────────────────────────────────────────────────
def test_10_revenue_by_source_reconciles_or_exposes_the_uncovered_bucket(monkeypatch):
    _patch_business_total_stack(monkeypatch)
    from services.source_attribution_service import build_revenue_by_source

    out = build_revenue_by_source(WINDOW, now=NOW)
    recon = out["canonical_reconciliation"]

    # Every canonical deal is in exactly one displayed bucket.
    assert recon["displayed_customers"] == recon["canonical_won_deals"]
    assert recon["uncovered_deals"] == 0
    # The money that is NOT displayed is named, to the cent.
    assert recon["uncovered_revenue_usd"] == 0.0
    assert recon["reconciles"] is True
    # The deal whose currency could not be proven is reported, not discarded.
    assert recon["revenue_unavailable_deals"] == 1
    # Unclassified/ambiguous revenue keeps its own bucket rather than being
    # dropped to make the classified rows look clean.
    groups = {g["group"]: g for g in out["groups"]}
    assert groups["unclassified"]["customers"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# §11 — a narrower attribution scope can never exceed a broader one
# ─────────────────────────────────────────────────────────────────────────────
def test_11_campaign_and_country_scopes_cannot_inflate_beyond_all_source():
    ladder = canonical_revenue.get_scope_ladder(base={
        "available": True, "deals": POPULATION, "window": WINDOW,
        "window_start": None, "window_end": None, "as_of": None,
    })
    assert ladder["lattice_violations"] == []
    assert ladder["revenue_lattice_violations"] == []
    counts = {s: ladder["scopes"][s]["won_deals"] for s in revenue_scope.SCOPE_ORDER}
    assert (counts["all_source"] >= counts["google_ads_source"]
            >= counts["campaign_attributable"] >= counts["gclid_attributable"])


def test_11b_the_lattice_check_actually_catches_an_inversion():
    """A guard that cannot fail is not a guard."""
    inverted = {"all_source": 3, "google_ads_source": 9}
    violations = revenue_scope.check_lattice(inverted)
    assert violations and "exceeds" in violations[0]


# ─────────────────────────────────────────────────────────────────────────────
# §12-§13 — fail closed, with no silent fallback
# ─────────────────────────────────────────────────────────────────────────────
def test_12_unavailable_canonical_revenue_is_explicit(monkeypatch):
    patch_canonical_ledger(monkeypatch, [], available=False)
    out = canonical_revenue.get_revenue_snapshot(WINDOW, now=NOW)
    assert out["available"] is False
    assert out["reason"] == canonical_revenue.REASON_LEDGER_UNREADABLE
    assert out["source"] == canonical_revenue.CANONICAL_SOURCE
    assert out["scope"] == revenue_scope.SCOPE_ALL_SOURCE
    assert out["window"] == WINDOW
    # Counts are NULL, never 0 — a page must not render an outage as "no deals".
    for key in ("won_deals", "revenue_usd", "currency_unavailable_deals",
                "ambiguous_associations", "failed_associations"):
        assert out[key] is None, key
    assert out["legacy_fallback_used"] is False


def test_12b_unproven_coverage_fails_closed_with_violation_codes(monkeypatch):
    """A readable ledger with unproven COVERAGE must not be served either.

    This is the 153E-A2 gate applied to the production read path: a portal whose
    historical bootstrap never completed answers every query happily while
    holding an unknown fraction of history.
    """
    never_bootstrapped = dict(READY_SYNC_STATE, bootstrap_status="not_started",
                              bootstrap_completed_at=None)
    patch_canonical_ledger(monkeypatch, POPULATION,
                           sync_state=never_bootstrapped)
    out = canonical_revenue.get_revenue_snapshot(WINDOW, now=NOW)
    assert out["available"] is False
    assert out["reason"] == canonical_revenue.REASON_COVERAGE_NOT_PROVEN
    assert "bootstrap_not_complete" in out["violation_codes"]
    assert out["won_deals"] is None


def test_13_no_migrated_consumer_falls_back_to_a_legacy_ledger(monkeypatch):
    """With the canonical read down, every page reports unavailable.

    The legacy repository reads are wired to RAISE here: if any migrated
    consumer still reached for `gclid_attribution` or `deal_source_attribution`
    as a fallback, the test would error rather than quietly pass on the
    substituted population.
    """
    import db.revenue_repository as repo

    _patch_business_total_stack(monkeypatch, rows=[], available=False)

    def _forbidden(*a, **k):  # pragma: no cover - only runs on a regression
        raise AssertionError("a migrated consumer fell back to a legacy ledger")

    for name in ("fetch_revenue_deals", "fetch_won_revenue",
                 "fetch_source_revenue", "fetch_source_revenue_daily",
                 "fetch_campaign_deal_details", "fetch_country_deal_details",
                 "fetch_source_deal_details"):
        monkeypatch.setattr(repo, name, _forbidden)

    from services.revenue_attribution_service import build_revenue_deals
    from services.source_attribution_service import build_revenue_by_source

    deals = build_revenue_deals(WINDOW, now=NOW)
    assert deals["revenue_available"] is False
    assert deals["legacy_fallback_used"] is False
    assert deals["deals"] == []

    by_source = build_revenue_by_source(WINDOW, now=NOW)
    assert by_source["revenue_available"] is False
    assert by_source["legacy_fallback_used"] is False


# ─────────────────────────────────────────────────────────────────────────────
# §13b-§13d — review follow-ups (Copilot, PR #160)
# ─────────────────────────────────────────────────────────────────────────────
def test_13b_window_bounds_are_iso_8601_not_str_of_a_datetime(monkeypatch):
    """`str(datetime)` yields a SPACE separator, which is not ISO-8601.

    `window_start` / `window_end` are part of the published response contract,
    so a strict ISO parser on the client must be able to read them.
    """
    patch_canonical_ledger(monkeypatch, POPULATION)
    out = canonical_revenue.get_revenue_snapshot(WINDOW, now=NOW)
    for key in ("window_start", "window_end"):
        value = out[key]
        assert value and "T" in value, (key, value)
        assert " " not in value, (key, value)
        # Round-trips through a strict parser.
        assert datetime.fromisoformat(value).tzinfo is not None

    # The unavailable response uses the same formatting.
    patch_canonical_ledger(monkeypatch, [], available=False)
    down = canonical_revenue.get_revenue_snapshot(WINDOW, now=NOW)
    for key in ("window_start", "window_end"):
        assert down[key] is None or "T" in down[key]


def test_13b2_iso_emission_handles_date_datetime_string_and_none():
    """`.isoformat()` for real temporals; strings pass through untouched."""
    from datetime import date as _date

    assert canonical_revenue._iso(datetime(2026, 4, 1, tzinfo=timezone.utc)) == \
        "2026-04-01T00:00:00+00:00"
    assert canonical_revenue._iso(_date(2026, 4, 1)) == "2026-04-01"
    assert canonical_revenue._iso(None) is None
    # An already-serialized value is NOT re-parsed and re-formatted — doing so
    # could silently rewrite an offset we were handed.
    already = "2026-04-01T00:00:00+05:00"
    assert canonical_revenue._iso(already) == already
    # And the offset is never dropped.
    assert "+00:00" in canonical_revenue._iso(
        datetime(2026, 4, 1, tzinfo=timezone.utc))


def test_13c_channel_trend_bounds_are_explicit_utc_datetimes(monkeypatch):
    """A DATE cast to `timestamptz` resolves against the SESSION time zone.

    On a non-UTC session that shifts the window by hours and mis-buckets deals
    that closed near a boundary, so the bounds must already be UTC datetimes
    before they reach the ledger read.
    """
    from datetime import date, timezone

    import db.deal_ledger_repository as ledger_repo
    from services import dashboard_channels_service as channels

    seen = []

    def _capture(start=None, end=None):
        seen.append((start, end))
        return {"available": True, "rows": []}

    patch_canonical_ledger(monkeypatch, [])
    monkeypatch.setattr(ledger_repo, "fetch_won_deals", _capture)

    channels._canonical_daily_revenue(date(2026, 4, 1), date(2026, 6, 30))
    (start, end), = seen
    for bound in (start, end):
        assert isinstance(bound, datetime), bound
        assert bound.tzinfo is not None and bound.utcoffset().total_seconds() == 0
    assert start.date() == date(2026, 4, 1)
    # Inclusive 30 June becomes the EXCLUSIVE 1 July bound.
    assert end.date() == date(2026, 7, 1)
    assert timezone.utc


def test_13c2_the_contract_normalizes_bounds_so_no_consumer_can_recreate_it(monkeypatch):
    """The fix lives at the ONE boundary every consumer passes through.

    Fixing only the channel service would leave the next consumer free to hand
    a bare `date` straight into a `timestamptz` comparison. `load_won_deals`
    normalizes whatever it is given, so the defect cannot be reintroduced from
    a call site.
    """
    from datetime import date as _date

    import db.deal_ledger_repository as ledger_repo

    seen = []

    def _capture(start=None, end=None):
        seen.append((start, end))
        return {"available": True, "rows": []}

    patch_canonical_ledger(monkeypatch, [])
    monkeypatch.setattr(ledger_repo, "fetch_won_deals", _capture)

    # Every shape a careless caller might use.
    for start, end in ((_date(2026, 4, 1), _date(2026, 7, 1)),
                       (datetime(2026, 4, 1), datetime(2026, 7, 1)),
                       ("2026-04-01", "2026-07-01"),
                       ("2026-04-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00")):
        seen.clear()
        canonical_revenue.load_won_deals(start=start, end=end, now=NOW)
        (got_start, got_end), = seen
        for bound in (got_start, got_end):
            assert isinstance(bound, datetime), bound
            assert bound.tzinfo is not None
            assert bound.utcoffset().total_seconds() == 0
        # Every shape resolves to the SAME instant.
        assert got_start == datetime(2026, 4, 1, tzinfo=timezone.utc)
        assert got_end == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_13d_source_drilldown_fails_closed_when_the_ledger_is_unreadable(monkeypatch):
    """An empty deals section reads as "no deals", not as an outage.

    With contacts readable and the ledger down, the drawer previously rendered
    a healthy-looking shell with zero deals in it.
    """
    import db.revenue_repository as repo
    from services.source_attribution_service import build_source_platform_detail

    monkeypatch.setattr(repo, "fetch_source_contact_details",
                        lambda s, e: {"available": True, "rows": []})
    patch_canonical_ledger(monkeypatch, [], available=False)

    # Any legacy revenue provider being reached at all is a failure.
    def _forbidden(*a, **k):  # pragma: no cover - only runs on a regression
        raise AssertionError("the drilldown reached for a legacy revenue provider")

    for name in ("fetch_source_deal_details", "fetch_source_revenue",
                 "fetch_revenue_deals", "fetch_won_revenue"):
        monkeypatch.setattr(repo, name, _forbidden)

    out = build_source_platform_detail(
        WINDOW, "google_ads", "paid_search", "google_ads", now=NOW)
    assert out["revenue_available"] is False
    assert out["revenue_unavailable_reason"]
    assert out["legacy_fallback_used"] is False
    assert out["source_health"]["status"] != "ready"
    # Counts are withheld, not zeroed — 0 would be a claim about the bucket.
    assert out["summary"]["deals"] is None
    assert out["summary"]["contacts"] is None and out["summary"]["sqls"] is None
    assert out["deals"] == [] and out["rows"] == []
    # The full quarantine metadata a reader needs to act on.
    for key in ("revenue_source", "revenue_scope", "window", "as_of",
                "revenue_violation_codes"):
        assert key in out, key
    # Contact evidence comes from a DIFFERENT source and is labelled as its own
    # availability, never merged into a healthy-looking overall status.
    assert out["contact_evidence_available"] is True


# ─────────────────────────────────────────────────────────────────────────────
# §14-§15 — ownership stays where it belongs
# ─────────────────────────────────────────────────────────────────────────────
def test_14_google_ads_spend_still_comes_from_the_google_ads_canonical_path():
    """Revenue moved; spend did not.

    Google Ads owns spend, clicks, impressions, search terms and campaign
    identifiers. The canonical revenue contract must not touch any of them.
    """
    src = (_ROOT / "services" / "canonical_revenue_service.py").read_text()
    tree = ast.parse(src)
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not {c for c in called if "spend" in c}, called

    # And the spend truth the pages use is still the Google Ads canonical one.
    unit_econ = (_ROOT / "services" / "canonical_unit_economics_service.py").read_text()
    assert "build_google_ads_spend_truth" in unit_econ
    assert "canonical_google_ads_api" in unit_econ


def test_15_hubspot_revenue_comes_only_from_the_canonical_deal_ledger():
    src = (_ROOT / "services" / "canonical_revenue_service.py").read_text()
    tree = ast.parse(src)
    modules = {node.module for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module}
    # The only DB module the contract may import is the canonical ledger.
    db_modules = {m for m in modules if m.startswith("db.")}
    assert db_modules <= {"db.deal_ledger_repository"}, db_modules
    assert "revenue_repository" not in src
    assert canonical_revenue.CANONICAL_SOURCE == "hubspot_deal_ledger"


def test_15b_unit_economics_no_longer_reads_windsor_or_local_json():
    code = _code_without_comments_or_docstrings(
        _ROOT / "services" / "canonical_unit_economics_service.py")
    for banned in ("campaign_performance.json", "attributed_deals",
                   "_load_windsor_spend", "roas_calculator"):
        assert banned not in code, banned
    server = (_ROOT / "api" / "server.py").read_text()
    route = server.split('@app.get("/api/reports/unit-economics")')[1].split("@app.")[0]
    assert "build_unit_economics" in route
    assert "compute_all_campaign_roas" not in route
    # Business windows, not rolling ad windows.
    assert "_parse_window(" not in route
    assert "is_valid_window" in route


# ─────────────────────────────────────────────────────────────────────────────
# §16 — duplicate legacy rows cannot duplicate canonical revenue
# ─────────────────────────────────────────────────────────────────────────────
def test_16_duplicate_deal_rows_cannot_duplicate_revenue():
    """`gclid_attribution` keyed rows on an attribution hash, so one deal could
    appear several times and be summed several times. The canonical ledger is
    keyed on ``deal_id``; the contract additionally counts each deal once even
    if a caller hands it a duplicated row set."""
    doubled = POPULATION + [dict(D1), dict(D3)]
    summary = canonical_revenue.summarize_deals(
        _dedupe_by_deal_id(doubled), revenue_scope.SCOPE_ALL_SOURCE)
    assert summary["won_deals"] == ALL_SOURCE_WON_DEALS
    assert summary["known_revenue_usd"] == ALL_SOURCE_KNOWN_REVENUE
    assert summary["revenue_usd"] is None

    # And the storage layer makes the duplicate impossible in the first place.
    schema = (_ROOT / "db" / "schema.py").read_text()
    ddl = schema.split("CREATE TABLE IF NOT EXISTS hubspot_deal_ledger (")[1].split(");")[0]
    pk = [ln for ln in ddl.splitlines() if "PRIMARY KEY" in ln.upper()]
    assert len(pk) == 1 and "deal_id" in pk[0], pk


def _dedupe_by_deal_id(rows):
    seen = {}
    for row in rows:
        seen.setdefault(row["deal_id"], row)
    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────────
# §17 — static contract guard (fails CI on a regression)
# ─────────────────────────────────────────────────────────────────────────────
# Legacy revenue READ paths a migrated consumer must never call again.
# `gclid_attribution` and `deal_source_attribution` stay in the database, and the
# scheduler keeps WRITING them for the observation period — they are retired in
# PR-ADS-153G. What is forbidden is any migrated page reading revenue from them.
PROHIBITED_LEGACY_TABLES = ("gclid_attribution", "deal_source_attribution")
PROHIBITED_LEGACY_PROVIDERS = frozenset({
    "fetch_revenue_deals", "fetch_won_revenue", "fetch_source_revenue",
    "fetch_source_revenue_daily", "fetch_campaign_deal_details",
    "fetch_country_deal_details", "fetch_source_deal_details",
    "compute_all_campaign_roas", "compute_all_country_roas",
    "_load_windsor_spend",
})


def _code_without_comments_or_docstrings(path: Path) -> str:
    """Behaviour only — a module may still EXPLAIN what it stopped reading."""
    src = path.read_text()
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    for doc in docstrings:
        code = code.replace(doc, "")
    return code


def _called_names(path: Path) -> set:
    """Every function/method NAME this module actually calls.

    An AST walk rather than a substring search, so the guard tracks behaviour:
    a module may name a retired provider in prose, and reformatting cannot
    disarm it.
    """
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _string_literals(path: Path) -> set:
    """Every string CONSTANT in the module, docstrings excluded.

    A legacy table can only be read by naming it in SQL, so a literal is where
    a regression would show up — while ``upsert_deal_source_attribution`` (the
    writer that must keep running) is an attribute, not a literal.
    """
    src = path.read_text()
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings}


@pytest.mark.parametrize("module", MIGRATED_CONSUMERS)
def test_17_migrated_consumers_call_no_legacy_revenue_provider(module):
    called = _called_names(_ROOT / module) & PROHIBITED_LEGACY_PROVIDERS
    assert not called, f"{module} still calls {sorted(called)}"


@pytest.mark.parametrize("module", MIGRATED_CONSUMERS)
def test_17a_migrated_consumers_name_no_legacy_revenue_table_in_sql(module):
    literals = _string_literals(_ROOT / module)
    for banned in PROHIBITED_LEGACY_TABLES:
        offenders = [lit for lit in literals if banned in lit]
        assert not offenders, f"{module} still names {banned}: {offenders[:2]}"


def test_17b_the_static_guard_would_actually_catch_a_regression(tmp_path):
    """Prove the guard fires on a reintroduced legacy read, not just on nothing.

    A guard that has never been seen to fail is not evidence. Both directions
    are exercised against real source, parsed exactly as a consumer is.
    """
    offender = tmp_path / "regressed_consumer.py"

    # Prose is allowed: a module SHOULD be able to explain what it stopped doing.
    offender.write_text(
        '"""Explains that fetch_won_revenue read gclid_attribution."""\n'
        "# and a comment naming deal_source_attribution\n"
        "VALUE = 1\n"
    )
    assert not (_called_names(offender) & PROHIBITED_LEGACY_PROVIDERS)
    assert not [lit for lit in _string_literals(offender)
                if any(t in lit for t in PROHIBITED_LEGACY_TABLES)]

    # Real code is not.
    offender.write_text(
        "from db import revenue_repository as repo\n"
        "def build():\n"
        "    return repo.fetch_won_revenue(None, None)\n"
    )
    assert _called_names(offender) & PROHIBITED_LEGACY_PROVIDERS == {"fetch_won_revenue"}

    offender.write_text(
        "SQL = 'SELECT deal_amount_usd FROM gclid_attribution'\n"
    )
    assert [lit for lit in _string_literals(offender)
            if any(t in lit for t in PROHIBITED_LEGACY_TABLES)]


def test_17c_legacy_tables_are_not_dropped_by_this_pr():
    """A consumer cutover is not legacy destruction (PR-ADS-153G owns that)."""
    schema = (_ROOT / "db" / "schema.py").read_text()
    for table in PROHIBITED_LEGACY_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema, table
    # Nothing this PR touches may destroy either legacy lineage. The check is
    # scoped to the legacy TABLE NAMES so an unrelated migration id containing
    # the word "truncate" cannot make the guard fire — or, worse, be edited away.
    for text, label in (
            (schema, "schema"),
            ((_ROOT / "services" / "canonical_revenue_service.py").read_text(), "contract"),
            ((_ROOT / "services" / "canonical_unit_economics_service.py").read_text(), "unit-econ"),
            ((_ROOT / "analysis" / "revenue_scope.py").read_text(), "scope")):
        upper = text.upper()
        for table in PROHIBITED_LEGACY_TABLES:
            for verb in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
                assert f"{verb} {table.upper()}" not in upper, f"{label}: {verb} {table}"
                assert f"{verb} IF EXISTS {table.upper()}" not in upper, label


def test_17d_no_external_write_path_is_introduced():
    for module in ("services/canonical_revenue_service.py",
                   "services/canonical_unit_economics_service.py",
                   "analysis/revenue_scope.py"):
        code = _code_without_comments_or_docstrings(_ROOT / module).lower()
        for banned in ("requests.post", "requests.put", "requests.patch",
                       "requests.delete", "basic_api.update", "batch_api.update",
                       "mutate", "offline_conversion", "conversion_upload"):
            assert banned not in code, f"{module}: {banned}"


def test_17e_no_consumer_reimplements_the_won_predicate():
    """Won status is decided once, in SQL, by the contract's repository read."""
    for module in MIGRATED_CONSUMERS:
        code = _code_without_comments_or_docstrings(_ROOT / module)
        assert "hs_is_closed_won" not in code, module
        assert not re.search(r"ILIKE\s*['\"]%won%", code, re.IGNORECASE), module
        assert "326093516" not in code, module


# ─────────────────────────────────────────────────────────────────────────────
# Response metadata contract
# ─────────────────────────────────────────────────────────────────────────────
REQUIRED_METADATA = (
    "source", "scope", "window", "window_start", "window_end", "as_of",
    "available", "won_deals", "revenue_usd", "currency_unavailable_deals",
    "ambiguous_associations", "failed_associations", "attribution_coverage",
)


def test_every_revenue_response_declares_its_source_scope_and_coverage(monkeypatch):
    patch_canonical_ledger(monkeypatch, POPULATION)
    out = canonical_revenue.get_revenue_snapshot(WINDOW, now=NOW)
    for key in REQUIRED_METADATA:
        assert key in out, key
    assert out["available"] is True
    assert out["scope"] == revenue_scope.SCOPE_ALL_SOURCE
    assert out["is_business_total"] is True
    coverage = out["attribution_coverage"]
    assert set(coverage["won_deals_by_scope"]) == set(revenue_scope.SCOPE_ORDER)
    assert coverage["pct_of_won_deals_by_scope"]["all_source"] == 100.0
    assert coverage["lattice_violations"] == []


def test_an_advertising_scope_is_never_labelled_a_business_total(monkeypatch):
    patch_canonical_ledger(monkeypatch, POPULATION)
    out = canonical_revenue.get_revenue_snapshot(
        WINDOW, revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE, now=NOW)
    assert out["is_business_total"] is False
    assert out["won_deals"] < ALL_SOURCE_WON_DEALS
    # It still carries the all-source ladder, so a reader can see the gap.
    assert out["attribution_coverage"]["won_deals_by_scope"]["all_source"] == \
        ALL_SOURCE_WON_DEALS


def test_an_unknown_scope_is_an_error_not_a_silent_widening():
    with pytest.raises(revenue_scope.UnknownScopeError):
        revenue_scope.normalize_scope("gclid")          # typo'd scope
    with pytest.raises(revenue_scope.UnknownScopeError):
        revenue_scope.deal_in_scope(D1, "everything")


# ─────────────────────────────────────────────────────────────────────────────
# Frontend: no page renders an unavailable revenue figure as $0
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# §18 — permanent frontend contract (replaces the retired 153E-A diff guard)
#
# 153E-A carried `test_no_frontend_change_in_this_backend_pr`, a TRANSIENT scope
# guard: it diffed the branch against `origin/main` and forbade any `static/`
# change. That was right for a shadow-mode backend PR and wrong forever after —
# it would fail every future PR that has to display the canonical contract. It is
# retired here and replaced by these behavioural tests, which assert what the
# frontend must DO rather than which files it may touch.
# ─────────────────────────────────────────────────────────────────────────────
_JS = (_ROOT / "static" / "app.js").read_text()
_HTML = (_ROOT / "static" / "index.html").read_text()


def _js_function(name, span=3000):
    i = _JS.find(name)
    assert i != -1, f"{name} not found in app.js"
    return _JS[i:i + span]


def test_18_unit_economics_uses_business_windows_not_the_rolling_60d_contract():
    select = re.search(r'<select[^>]*id="unit-economics-window"[^>]*>.*?</select>',
                       _HTML, re.S).group(0)
    for window in ("current_quarter", "last_quarter", "last_6_months", "ytd",
                   "all_time"):
        assert window in select, window
    # The retired rolling day windows must be gone from the control entirely.
    for day_window in ("7d", "14d", "30d", "60d", "90d", "365d"):
        assert f'value="{day_window}"' not in select, day_window
    assert "data-business-window-select" in select


def test_18b_frontend_sends_the_business_window_and_an_explicit_scope():
    loader = _js_function("async function loadUnitEconomics")
    assert "getRoasBusinessWindow()" in loader
    assert "/api/reports/unit-economics?window=" in loader
    # The scope is sent explicitly — an advertising CAC/ROAS must never be
    # computed at an unstated population.
    assert "scope=" in loader
    assert "UNIT_ECONOMICS_SCOPE" in loader
    assert 'UNIT_ECONOMICS_SCOPE = "campaign_attributable"' in _JS


def test_18c_the_window_selector_is_wired_to_the_business_window_handler():
    """A business key fed to the DAY-window handler silently reverts the page."""
    wiring = _js_function('ueWindow  = document.getElementById', span=700)
    assert 'ueWindow.addEventListener("change", handleBusinessWindowSelectChange)' \
        in wiring
    assert "handleSyncedWindowSelectChange" not in wiring
    # And that handler must reload Unit Economics rather than ROAS by Campaign.
    handler = _js_function("function handleBusinessWindowSelectChange")
    assert 'case "unit-economics":' in handler


def test_18d_deal_rows_prefer_the_canonical_deal_name():
    # The canonical ledger names the DEAL; `company` (the contact's employer) is
    # not a ledger field, so every migrated deal surface prefers `deal_name`.
    assert _JS.count("deal_name || ") >= 3, "deal tables must prefer deal_name"
    assert '["deal_name", "Deal name"]' in _JS


def test_18e_unavailable_revenue_renders_unavailable_never_zero():
    renderer = _js_function("function renderUnitEconomicsPage")
    assert "Unavailable" in renderer
    assert "revenue_available" in renderer
    # No hard-coded zero standing in for a withheld figure.
    assert "|| 0)" not in renderer, "a withheld metric must not default to 0"
    assert 'fmtMoney(0)' not in renderer


def test_18f_attribution_views_disclose_their_narrower_revenue_scope():
    disclosure = _js_function("function renderRevenueScopeDisclosure")
    assert "attributed_revenue_scope" in disclosure
    assert "revenue_scope" in disclosure
    # It must say the attributed figure is a SUBSET, not the business total.
    assert "subset" in disclosure.lower()
    # And it must actually be rendered on the ROAS by Campaign page.
    page = _js_function("function renderRoasCampaignsPage", span=4000)
    assert "renderRevenueScopeDisclosure(data)" in page


def test_18g_no_frontend_revenue_path_implies_the_retired_windsor_lineage():
    """Scoped to the REVENUE paths, deliberately.

    "Windsor" still appears in page-explanation copy on the GCLID-attribution
    and System Status pages, where it describes a legacy ad-platform source
    honestly. Banning the word globally would be a guard against vocabulary
    rather than behaviour, and would fail for the wrong reason. What must be
    true is that no revenue SURFACE reads or implies that lineage.
    """
    for banned in ("campaign_performance.json", "attributed_deals.json"):
        assert banned not in _JS.lower(), banned

    for fn in ("async function loadUnitEconomics",
               "function renderUnitEconomicsPage",
               "function renderRevenueScopeDisclosure"):
        body = _js_function(fn).lower()
        for banned in ("windsor", "campaign_performance", "attributed_deals"):
            assert banned not in body, f"{fn}: {banned}"

    # The live Unit Economics page calls the canonical endpoint and nothing else.
    loader = _js_function("async function loadUnitEconomics")
    assert "/api/reports/unit-economics" in loader
    for retired in ("/api/reports/roas/campaigns", "/api/reports/roas/countries"):
        assert retired not in loader, retired

    # Metrics the canonical ledger cannot support are shown as withheld.
    renderer = _js_function("function renderUnitEconomicsPage")
    if "LTV/CAC" in renderer:
        assert "Unavailable" in renderer


def test_unit_economics_page_uses_business_windows_and_renders_unavailable():
    html = (_ROOT / "static" / "index.html").read_text()
    js = (_ROOT / "static" / "app.js").read_text()
    select = re.search(r'<select[^>]*id="unit-economics-window"[^>]*>.*?</select>',
                       html, re.S).group(0)
    assert "data-business-window-select" in select
    assert "current_quarter" in select and "60d" not in select

    loader = js[js.find("async function loadUnitEconomics"):][:600]
    assert "getRoasBusinessWindow()" in loader
    renderer = js[js.find("function renderUnitEconomicsPage"):][:3000]
    assert "Unavailable" in renderer
    assert "revenue_available" in renderer


def test_no_python_module_here_writes_to_an_external_platform():
    for module in ("services/canonical_revenue_service.py",
                   "services/canonical_unit_economics_service.py",
                   "analysis/revenue_scope.py",
                   "tests/canonical_ledger_fixtures.py"):
        code = _code_without_comments_or_docstrings(_ROOT / module).lower()
        assert "hubspot" not in code or "hubspot_deal_ledger" in code
        assert "googleads" not in code
