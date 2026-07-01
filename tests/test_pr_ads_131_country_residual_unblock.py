"""
PR-ADS-131 — Country ROAS Residual Bucket + Last Quarter Unblock.

After PR-ADS-130 the app EXPLAINS why ROAS by Country is blocked when the Google
Ads geographic_view total falls below canonical campaign spend by design (spend
Google Ads could not assign to any country). Running Geo Sync again cannot close
that gap.

PR-ADS-131 unblocks the page SAFELY by adding an explicit residual bucket:

    Sum(country-attributed spend) + Unattributed/No-Country residual
        = canonical campaign spend

The residual is surfaced, never spread across real countries, never treated as a
real country, and the tolerance is never loosened. Missing geo dates, campaigns
missing geo, and incomplete FX/coverage still BLOCK.

These tests prove the mart contract (reconciled_with_residual state + residual
row), the safety gates, and the static frontend contract. Read-only throughout.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_pr_ads_125_revenue_decision_mart as t125  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()


# ── Fixtures: the Last Quarter residual scenario ────────────────────────────
# Canonical campaign spend 27,867.98 GBP; canonical geo (country-attributed)
# spend 26,331.25 GBP -> a 1,536.73 GBP shortfall (5.51%, above the 2% tolerance).
# The shortfall is a by-design unattributed residual (geo rows exist for every
# campaign-spend day AND every campaign, but the geo total is still lower).

CANONICAL_LQ = {
    "available": True, "customer_id": "111", "currency_code": "GBP",
    "reporting_currency": "USD", "fx_complete": True, "fx_missing_days": 0,
    "campaign_count": 2, "total_spend": 27867.98, "total_spend_usd": 36228.37,
    "rows": [
        {"campaign_name": "Gulf", "spend": 17867.98, "spend_usd": 23228.37},
        {"campaign_name": "MENA", "spend": 10000.0, "spend_usd": 13000.0},
    ],
}
GEO_TOTAL_LQ = {
    "available": True, "has_rows": True,
    "total_cost_micros": 26331_250_000, "total_spend": 26331.25,
    "rows_counted": 2, "country_count": 2, "currency_code": "GBP", "customer_id": "111",
}
GEO_BY_COUNTRY_LQ = {
    "available": True, "has_rows": True,
    "total_spend": 26331.25, "total_spend_usd": 34230.63, "fx_complete": True,
    "currency_code": "GBP", "customer_id": "111", "reporting_currency": "USD",
    "rows": [
        {"country_criterion_id": "2826", "country_code": "GB",
         "country_name": "United Kingdom", "cost_micros": 16331_250_000,
         "spend": 16331.25, "spend_usd": 21230.63, "fx_complete": True},
        {"country_criterion_id": "2276", "country_code": "DE",
         "country_name": "Germany", "cost_micros": 10000_000_000,
         "spend": 10000.0, "spend_usd": 13000.0, "fx_complete": True},
    ],
}
# By-design residual: every campaign-spend day has geo spend, every campaign has
# geo spend, but the geo total is still below canonical -> the reason resolves to
# geo_report_does_not_reconcile_by_design.
BREAKDOWN_BY_DESIGN = {
    "available": True,
    "daily": [{"spend_date": "2026-04-15", "campaign_spend": 27867.98, "geo_spend": 26331.25}],
    "by_campaign": [
        {"campaign_id": "1", "campaign_name": "Gulf", "campaign_spend": 17867.98, "geo_spend": 17000.0},
        {"campaign_id": "2", "campaign_name": "MENA", "campaign_spend": 10000.0, "geo_spend": 9331.25},
    ],
    "unmapped_geo_native": 1536.73, "geo_rows_with_null_country": 8, "campaign_rows_counted": 200,
}


def _country_mart(monkeypatch, *, canonical=None, breakdown=None):
    import db.revenue_repository as repo
    t125._patch_durable(
        monkeypatch,
        canonical=canonical if canonical is not None else CANONICAL_LQ,
        geo_total=GEO_TOTAL_LQ, geo_by_country=GEO_BY_COUNTRY_LQ,
    )
    monkeypatch.setattr(
        repo, "fetch_geo_reconciliation_breakdown",
        lambda s, e: breakdown if breakdown is not None else BREAKDOWN_BY_DESIGN,
    )
    from services.revenue_decision_mart import build_revenue_decision_mart
    return build_revenue_decision_mart(view="country", window=t125.WINDOW, now=t125.NOW)


def _residual_rows(mart):
    return [r for r in mart["rows"] if r.get("is_residual")]


def _real_rows(mart):
    return [r for r in mart["rows"] if not r.get("is_residual")]


# ── Test 1 — residual state exists ──────────────────────────────────────────

def test_residual_state_exists(monkeypatch):
    mart = _country_mart(monkeypatch)
    st = mart["spend_truth"]
    assert st["country_spend_status"] == "reconciled_with_residual"
    assert st["country_roas_unblockable"] is True
    # It is NOT a plain verified reconcile, and the page is decision-ready.
    assert mart["readiness"]["country_decision_ready"] is True


# ── Test 2 — residual row is added ──────────────────────────────────────────

def test_residual_row_is_added(monkeypatch):
    mart = _country_mart(monkeypatch)
    residual = _residual_rows(mart)
    assert len(residual) == 1
    row = residual[0]
    assert row["country"] == "Unattributed / No Country"
    assert row["is_residual"] is True
    assert row["country_code"] is None
    # Not a real country: no leads/revenue, decision "unattributed".
    assert row["leads"] == 0 and row["customers"] == 0 and row["won_revenue"] == 0
    assert row["decision"] == "unattributed"
    # No revenue basis -> ROAS is null, never a fake computed 0.0.
    assert row["roas"] is None


# ── Test 3 — residual amount equals the gap ─────────────────────────────────

def test_residual_amount_equals_gap(monkeypatch):
    mart = _country_mart(monkeypatch)
    st = mart["spend_truth"]
    # 27,867.98 - 26,331.25 = 1,536.73 (native GBP).
    assert st["country_residual_native"] == 1536.73
    assert _residual_rows(mart)[0]["spend_native"] == 1536.73
    # Residual share ≈ 5.51%.
    assert round(st["country_residual_pct"], 2) == 5.51


# ── Test 4 — residual is NOT distributed across countries ────────────────────

def test_residual_not_distributed(monkeypatch):
    mart = _country_mart(monkeypatch)
    real = _real_rows(mart)
    # Each real country keeps ONLY its country-attributed (geo) USD spend.
    by_name = {r["country"]: r["spend"] for r in real}
    assert by_name["United Kingdom"] == 21230.63
    assert by_name["Germany"] == 13000.0
    # The sum of real country spend equals the geo country-attributed USD total —
    # the residual was NOT added onto any real country.
    assert round(sum(by_name.values()), 2) == 34230.63
    # Real rows carry no residual flag.
    assert all(not r.get("is_residual") for r in real)


# ── Test 5 — missing geo dates still block ──────────────────────────────────

def test_missing_geo_dates_still_block(monkeypatch):
    # A campaign-spend day with NO geo spend -> genuine missing geo, not a residual.
    breakdown = {
        "available": True,
        "daily": [
            {"spend_date": "2026-04-15", "campaign_spend": 17867.98, "geo_spend": 16331.25},
            {"spend_date": "2026-04-16", "campaign_spend": 10000.0, "geo_spend": 0.0},
        ],
        "by_campaign": [
            {"campaign_id": "1", "campaign_name": "Gulf", "campaign_spend": 17867.98, "geo_spend": 16331.25},
            {"campaign_id": "2", "campaign_name": "MENA", "campaign_spend": 10000.0, "geo_spend": 10000.0},
        ],
        "unmapped_geo_native": 0.0, "geo_rows_with_null_country": 0, "campaign_rows_counted": 200,
    }
    mart = _country_mart(monkeypatch, breakdown=breakdown)
    st = mart["spend_truth"]
    assert st["country_spend_status"] == "mismatch"
    assert st["country_roas_unblockable"] is False
    assert _residual_rows(mart) == []


# ── Test 6 — campaigns missing geo still block ──────────────────────────────

def test_campaigns_missing_geo_still_block(monkeypatch):
    # A campaign that spent but has NO geo rows -> blocked, not a residual.
    breakdown = {
        "available": True,
        "daily": [{"spend_date": "2026-04-15", "campaign_spend": 27867.98, "geo_spend": 26331.25}],
        "by_campaign": [
            {"campaign_id": "1", "campaign_name": "Gulf", "campaign_spend": 17867.98, "geo_spend": 16331.25},
            {"campaign_id": "2", "campaign_name": "MENA", "campaign_spend": 10000.0, "geo_spend": 0.0},
        ],
        "unmapped_geo_native": 0.0, "geo_rows_with_null_country": 0, "campaign_rows_counted": 200,
    }
    mart = _country_mart(monkeypatch, breakdown=breakdown)
    st = mart["spend_truth"]
    assert st["country_spend_status"] == "mismatch"
    assert st["country_roas_unblockable"] is False
    assert _residual_rows(mart) == []


# ── Test 7 — FX incomplete still blocks ─────────────────────────────────────

def test_fx_incomplete_still_blocks(monkeypatch):
    fx_incomplete = dict(CANONICAL_LQ)
    fx_incomplete["fx_complete"] = False
    fx_incomplete["fx_missing_days"] = 30
    fx_incomplete["total_spend_usd"] = None
    fx_incomplete["rows"] = [
        {"campaign_name": "Gulf", "spend": 17867.98, "spend_usd": None},
        {"campaign_name": "MENA", "spend": 10000.0, "spend_usd": None},
    ]
    mart = _country_mart(monkeypatch, canonical=fx_incomplete)
    st = mart["spend_truth"]
    assert st["country_spend_status"] != "reconciled_with_residual"
    assert st["country_roas_unblockable"] is False
    assert _residual_rows(mart) == []


# ── Test 8 — frontend allows residual mode (and blocks the rest) ────────────

def test_frontend_allows_residual_mode():
    i = JS.find("function renderRoasCountriesPage")
    assert i != -1
    body = JS[i:i + 2600]
    # The table renders for verified OR reconciled_with_residual; other statuses block.
    assert 'country_spend_status === "reconciled_with_residual"' in body
    assert 'st.country_spend_status !== "verified" && !residualMode' in body
    assert "renderCountrySpendUnreconciledState(st)" in body
    # The residual bucket is pinned regardless of the decision filter.
    assert "r.is_residual" in body


# ── Test 9 — frontend residual warning + row ────────────────────────────────

def test_frontend_residual_warning_and_row():
    assert "function renderCountryResidualWarning" in JS
    i = JS.find("function renderCountryResidualWarning")
    warn = JS[i:i + 900]
    assert "Country spend includes an unattributed residual" in warn
    assert "could not be assigned to a country" in warn
    # The table styles the residual row: "Unattributed / No Country" + "Residual" badge.
    tbl = JS[JS.find("function renderRoasCountryTable"):JS.find("function renderRoasCountryTable") + 2400]
    assert "Unattributed / No Country" in tbl
    assert "Residual" in tbl
    assert "revenue-residual-row" in tbl
    # Revenue Health shows the "Reconciled with residual" audit state.
    assert "Reconciled with residual" in JS
    assert "Geo country-attributed spend" in JS
    # Styling exists (never an unstyled raw row).
    assert ".revenue-residual-warning" in CSS
    assert ".revenue-residual-row" in CSS


# ── Test 10 — read-only safety (no platform writes introduced) ──────────────

def test_no_writes_introduced():
    changed = [
        os.path.join(ROOT, "services", "revenue_attribution_service.py"),
        os.path.join(ROOT, "services", "revenue_decision_mart.py"),
        os.path.join(ROOT, "services", "google_ads_geo_sync_service.py"),
        os.path.join(ROOT, "static", "app.js"),
    ]
    forbidden = (
        ".mutate(", "upload_offline", "offline_conversion",
        "google_ads_client", "hubspot_client",
        "requests.post", "requests.put", "requests.patch", "requests.delete",
        "set_budget", "set_bid", "pause_campaign", "enable_campaign",
    )
    for path in changed:
        src = open(path, encoding="utf-8").read().lower()
        for token in forbidden:
            assert token not in src, f"{os.path.basename(path)} must not reference '{token}'"
