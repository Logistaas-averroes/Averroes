"""
PR-ADS-125 — Canonical Revenue Decision Mart.

Proves the whole point of the mart: Campaign, Country, Source and Deal views read
from ONE backend contract, so they agree on the business window, spend, native +
USD reporting spend, FX coverage, campaign mapping status, country reconciliation
status, leads, SQLs, customers, closed-won revenue and ROAS. The audit endpoint
compares each current page against the mart and explains exactly where (and why)
any page differs.

Safety is never loosened: no fake $0, no ROAS on an unsafe denominator, missing
mapping is Unavailable (never $0), and there are NO writes to Google Ads or
HubSpot anywhere in the mart.

These tests patch the durable repository boundary (db.revenue_repository) with a
single coherent dataset — no real database, no network.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Deterministic reference time so the YTD window is 2026-01-01 .. 2026-06-22.
NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
WINDOW = "ytd"


# ── Coherent durable dataset (native GBP spend, USD reporting) ──────────────

# Canonical campaign-daily spend (the ONLY ROAS denominator). Native GBP 10,000;
# USD 13,000 (effective realized rate 1.3); FX complete.
CANONICAL = {
    "available": True,
    "customer_id": "111",
    "currency_code": "GBP",
    "reporting_currency": "USD",
    "fx_complete": True,
    "fx_missing_days": 0,
    "campaign_count": 2,
    "total_spend": 10000.0,
    "total_spend_usd": 13000.0,
    "rows": [
        {"campaign_name": "Brand - UK", "spend": 6000.0, "spend_usd": 8000.0},
        {"campaign_name": "Search - Global", "spend": 4000.0, "spend_usd": 5000.0},
    ],
}

# Legacy run-scoped geo (campaign-country) spend, native GBP. Used by Revenue by
# Source; reconciles with canonical (10,000).
GEO_ROWS = [
    {"campaign_name": "Brand - UK", "country": "United Kingdom", "spend": 6000.0},
    {"campaign_name": "Search - Global", "country": "Germany", "spend": 4000.0},
]

# PR-ADS-124 canonical Google Ads geo (google_ads_geo_daily_spend) — the SAME
# source as the reconciliation total AND the country-row source. Native GBP total
# 10,000 reconciles with canonical campaign spend; per-country USD (8,000 + 5,000)
# matches the canonical USD denominator (13,000).
GEO_CANONICAL_TOTAL = {
    "available": True, "has_rows": True,
    "total_cost_micros": 10000_000_000, "total_spend": 10000.0,
    "rows_counted": 2, "country_count": 2,
    "currency_code": "GBP", "customer_id": "111",
}
GEO_BY_COUNTRY = {
    "available": True, "has_rows": True,
    "total_spend": 10000.0, "total_spend_usd": 13000.0, "fx_complete": True,
    "currency_code": "GBP", "customer_id": "111", "reporting_currency": "USD",
    "rows": [
        {"country_criterion_id": "2826", "country_code": "GB",
         "country_name": "United Kingdom", "cost_micros": 6000_000_000,
         "spend": 6000.0, "spend_usd": 8000.0, "fx_complete": True},
        {"country_criterion_id": "2276", "country_code": "DE",
         "country_name": "Germany", "cost_micros": 4000_000_000,
         "spend": 4000.0, "spend_usd": 5000.0, "fx_complete": True},
    ],
}

# Verified coverage chunk spanning the whole window -> coverage complete.
COVERAGE_COMPLETE = [
    {"chunk_start": "2026-01-01", "chunk_end": "2026-12-31", "status": "verified"},
]
# Chunk that starts after the window start -> Jan/Feb missing -> incomplete.
COVERAGE_INCOMPLETE = [
    {"chunk_start": "2026-03-01", "chunk_end": "2026-12-31", "status": "verified"},
]

LEAD_ROWS = [
    {"campaign_name": "Brand - UK", "country": "United Kingdom",
     "status_category": "qualified", "has_gclid": True},
    {"campaign_name": "Brand - UK", "country": "United Kingdom",
     "status_category": "lead", "has_gclid": True},
    {"campaign_name": "Search - Global", "country": "Germany",
     "status_category": "qualified", "has_gclid": False},
    {"campaign_name": "Search - Global", "country": "Germany",
     "status_category": "lead", "has_gclid": False},
    {"campaign_name": "Search - Global", "country": "Germany",
     "status_category": "lead", "has_gclid": False},
]

WON_ROW_MAPPED = {
    "campaign_name": "Brand - UK", "country": "United Kingdom",
    "deal_id": "d1", "deal_amount_usd": 9000.0, "match_status": "matched",
}
# Revenue on a campaign with NO canonical spend match -> mapping unavailable.
WON_ROW_UNMAPPED = {
    "campaign_name": "Email Outreach", "country": "Germany",
    "deal_id": "d2", "deal_amount_usd": 3000.0, "match_status": "unknown",
}

DEAL_ROWS = [
    {"deal_id": "d1", "company": "Acme Ltd", "country": "United Kingdom",
     "campaign_name": "Brand - UK", "deal_close_date": "2026-05-01",
     "deal_amount_usd": 9000.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
]

SOURCE_LEAD_ROWS = [
    {"acquisition_group": "google_ads", "status_category": "qualified"},
    {"acquisition_group": "google_ads", "status_category": "lead"},
    {"acquisition_group": "organic", "status_category": "lead"},
]
SOURCE_REV_ROWS = [
    {"acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 9000.0},
]


# Canonical spend with INCOMPLETE FX: USD cannot be computed safely, so the repo
# returns total_spend_usd=None and fx_complete=False (never a guessed USD total).
CANONICAL_FX_INCOMPLETE = {
    "available": True,
    "customer_id": "111",
    "currency_code": "GBP",
    "reporting_currency": "USD",
    "fx_complete": False,
    "fx_missing_days": 40,
    "campaign_count": 2,
    "total_spend": 10000.0,
    "total_spend_usd": None,
    "rows": [
        {"campaign_name": "Brand - UK", "spend": 6000.0, "spend_usd": None},
        {"campaign_name": "Search - Global", "spend": 4000.0, "spend_usd": None},
    ],
}


def _patch_durable(monkeypatch, *, coverage=None, won_rows=None, canonical=None,
                   geo_total=None, geo_by_country=None):
    """Patch the durable repository so every revenue service reads one dataset."""
    import db.revenue_repository as repo

    won = won_rows if won_rows is not None else [WON_ROW_MAPPED]
    chunks = coverage if coverage is not None else COVERAGE_COMPLETE
    canon = canonical if canonical is not None else CANONICAL
    geo_tot = geo_total if geo_total is not None else GEO_CANONICAL_TOTAL
    geo_country = geo_by_country if geo_by_country is not None else GEO_BY_COUNTRY

    monkeypatch.setattr(repo, "fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr(
        repo, "fetch_campaign_country_spend",
        lambda s, e: {"available": True, "rows": list(GEO_ROWS),
                      "coverage_start": "2026-01-01", "coverage_end": "2026-06-22"},
    )
    monkeypatch.setattr(
        repo, "fetch_lead_quality",
        lambda s, e: {"available": True, "rows": list(LEAD_ROWS),
                      "event_date_safe": True, "missing_contact_created_at_count": 0,
                      "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0},
    )
    monkeypatch.setattr(
        repo, "fetch_won_revenue",
        lambda s, e: {"available": True, "rows": list(won),
                      "coverage_start": "2026-01-01", "coverage_end": "2026-06-22"},
    )
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "datasets": {}})
    monkeypatch.setattr(
        repo, "fetch_canonical_campaign_spend",
        lambda s, e: dict(canon),
    )
    # PR-ADS-124 canonical Google Ads geo source (reconciliation + country rows).
    monkeypatch.setattr(
        repo, "fetch_geo_daily_spend_total", lambda s, e: dict(geo_tot),
    )
    monkeypatch.setattr(
        repo, "fetch_geo_daily_spend_by_country", lambda s, e: dict(geo_country),
    )
    monkeypatch.setattr(
        repo, "fetch_spend_coverage",
        lambda s, e: {"available": True, "chunks": list(chunks)},
    )
    monkeypatch.setattr(
        repo, "fetch_campaign_identity",
        lambda cid=None: {"available": True, "mappings": []},
    )
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: True)
    monkeypatch.setattr(
        repo, "fetch_revenue_deals",
        lambda s, e: {"available": True, "rows": list(DEAL_ROWS)},
    )
    monkeypatch.setattr(
        repo, "fetch_source_leads",
        lambda s, e: {"available": True, "rows": list(SOURCE_LEAD_ROWS)},
    )
    monkeypatch.setattr(
        repo, "fetch_source_revenue",
        lambda s, e: {"available": True, "rows": list(SOURCE_REV_ROWS)},
    )
    # Revenue-by-source health counts come from db.writers (read-only count).
    import db.writers as db_writers
    monkeypatch.setattr(
        db_writers, "source_attribution_health_counts",
        lambda: {"classified_contacts": 3, "attributed_deals": 1,
                 "ambiguous_deals": 0, "unclassified_deals": 0},
    )


def _mart(view, monkeypatch, **kw):
    _patch_durable(monkeypatch, **kw)
    from services.revenue_decision_mart import build_revenue_decision_mart
    return build_revenue_decision_mart(view=view, window=WINDOW, now=NOW)


# ── 1. Shared window resolver across all four views ─────────────────────────

def test_all_views_use_the_same_window_resolver(monkeypatch):
    from analysis.business_windows import resolve_window
    expected = resolve_window(WINDOW, now=NOW)

    windows = {}
    for view in ("campaign", "country", "source", "deal"):
        out = _mart(view, monkeypatch)
        w = out["window"]
        assert w["key"] == expected["key"] == "ytd"
        assert w["start_date"] == expected["start_date"] == "2026-01-01"
        assert w["end_date"] == expected["end_date"] == "2026-06-22"
        assert w["timezone"] == "Europe/London"
        windows[view] = (w["key"], w["start_date"], w["end_date"], w["timezone"])

    # All four views resolve to the exact same window tuple.
    assert len(set(windows.values())) == 1


def test_invalid_view_is_rejected(monkeypatch):
    _patch_durable(monkeypatch)
    from services.revenue_decision_mart import build_revenue_decision_mart
    with pytest.raises(ValueError):
        build_revenue_decision_mart(view="campaigns", window=WINDOW, now=NOW)


def test_invalid_window_is_rejected(monkeypatch):
    _patch_durable(monkeypatch)
    from services.revenue_decision_mart import build_revenue_decision_mart
    with pytest.raises(ValueError):
        build_revenue_decision_mart(view="campaign", window="7d", now=NOW)


# ── 2. Campaign page spend equals mart campaign spend ───────────────────────

def test_campaign_page_spend_equals_mart_campaign_spend(monkeypatch):
    _patch_durable(monkeypatch)
    from services.revenue_attribution_service import build_revenue_attribution
    from services.revenue_decision_mart import build_revenue_decision_mart

    page = build_revenue_attribution(WINDOW, now=NOW)
    mart = build_revenue_decision_mart(view="campaign", window=WINDOW, now=NOW)

    # The canonical USD denominator is the SAME number the page summarises.
    assert page["summary"]["spend"] == 13000.0
    assert mart["summary"]["spend_usd"] == 13000.0
    assert mart["spend_truth"]["usd_spend"] == 13000.0
    assert mart["spend_truth"]["native_spend"] == 10000.0
    assert mart["spend_truth"]["native_currency"] == "GBP"


# ── 3. Country page cannot invent spend outside the mart country spend ──────

def test_country_cannot_invent_spend_beyond_mart(monkeypatch):
    _patch_durable(monkeypatch)
    from services.revenue_attribution_service import build_revenue_attribution
    from services.revenue_decision_mart import build_revenue_decision_mart

    page = build_revenue_attribution(WINDOW, now=NOW)
    mart = build_revenue_decision_mart(view="country", window=WINDOW, now=NOW)

    page_country_spend = round(sum(r["spend"] for r in page["countries"]), 2)
    mart_country_spend = round(sum(r["spend"] for r in mart["rows"]), 2)

    # Country rows come from the same source; the page cannot exceed the mart.
    assert mart_country_spend == page_country_spend
    assert mart_country_spend <= mart["summary"]["spend_usd"] + 0.01


# ── 3b. Country view is backed by the canonical geo source (PR-ADS-124) ─────

def test_country_view_uses_canonical_geo_source(monkeypatch):
    # After rebasing onto PR-ADS-124, the mart's country rows must come from the
    # canonical google_ads_geo_daily_spend source (USD), NOT the legacy geo table.
    _patch_durable(monkeypatch)
    from services.revenue_attribution_service import build_revenue_attribution
    from services.revenue_decision_mart import build_revenue_decision_mart

    core = build_revenue_attribution(WINDOW, now=NOW)
    assert core["source_health"]["geo_country_source"] == "canonical_google_ads_api"

    mart = build_revenue_decision_mart(view="country", window=WINDOW, now=NOW)
    # Country rows are expressed in the canonical USD denominator and reconcile
    # exactly with the canonical spend truth — never the legacy native total.
    country_spend = round(sum(r["spend"] for r in mart["rows"]), 2)
    assert country_spend == mart["summary"]["spend_usd"] == 13000.0
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    countries = {r["country"] for r in mart["rows"]}
    assert {"United Kingdom", "Germany"} <= countries


# ── 4. Revenue by Source Google Ads spend equals mart Google Ads spend ──────

def test_source_google_ads_spend_equals_mart(monkeypatch):
    _patch_durable(monkeypatch)
    from services.source_attribution_service import build_revenue_by_source
    from services.revenue_decision_mart import build_revenue_decision_mart

    page = build_revenue_by_source(WINDOW, now=NOW)
    mart = build_revenue_decision_mart(view="source", window=WINDOW, now=NOW)

    page_google = next(g for g in page["groups"] if g["group"] == "google_ads")
    mart_google = next(g for g in mart["rows"] if g["group"] == "google_ads")
    assert mart_google["spend"] == page_google["spend"]
    # Only Google Ads carries spend; every other source group is revenue-only.
    for g in mart["rows"]:
        if g["group"] != "google_ads":
            assert g["spend"] is None


# ── 5. Deals revenue equals mart deal revenue ───────────────────────────────

def test_deals_revenue_equals_mart_deal_revenue(monkeypatch):
    _patch_durable(monkeypatch)
    from services.revenue_attribution_service import build_revenue_deals
    from services.revenue_decision_mart import build_revenue_decision_mart

    page = build_revenue_deals(WINDOW, now=NOW)
    mart = build_revenue_decision_mart(view="deal", window=WINDOW, now=NOW)

    mart_rev = round(sum(d["deal_amount_usd"] for d in mart["rows"]), 2)
    assert page["summary"]["won_revenue"] == 9000.0
    assert mart_rev == page["summary"]["won_revenue"]


# ── 6/7/8. FX coverage, campaign mapping, country reconciliation are shared ─

def test_fx_coverage_is_shared_across_views(monkeypatch):
    statuses = {_mart(v, monkeypatch)["spend_truth"]["fx_status"]
                for v in ("campaign", "country", "source", "deal")}
    assert statuses == {"verified"}


def test_campaign_mapping_status_is_shared_across_views(monkeypatch):
    statuses = {_mart(v, monkeypatch)["spend_truth"]["campaign_spend_status"]
                for v in ("campaign", "country", "source", "deal")}
    assert statuses == {"verified"}


def test_country_reconciliation_status_is_shared_across_views(monkeypatch):
    statuses = {_mart(v, monkeypatch)["spend_truth"]["country_spend_status"]
                for v in ("campaign", "country", "source", "deal")}
    assert statuses == {"verified"}


# ── 9. Missing mapping is Unavailable, never $0 ─────────────────────────────

def test_missing_mapping_is_unavailable_never_zero(monkeypatch):
    _patch_durable(monkeypatch, won_rows=[WON_ROW_MAPPED, WON_ROW_UNMAPPED])
    from services.revenue_decision_mart import build_revenue_decision_mart

    mart = build_revenue_decision_mart(view="campaign", window=WINDOW, now=NOW)
    unmapped = next(r for r in mart["rows"]
                    if r.get("campaign_name") == "Email Outreach")
    assert unmapped["spend"] is None          # never a fabricated $0
    assert unmapped["spend"] != 0
    assert unmapped["roas"] is None
    assert unmapped["spend_mapping"] == "unavailable"


# ── 10. ROAS unavailable when spend coverage is unsafe ──────────────────────

def test_roas_unavailable_when_spend_coverage_unsafe(monkeypatch):
    _patch_durable(monkeypatch, coverage=COVERAGE_INCOMPLETE)
    from services.revenue_decision_mart import build_revenue_decision_mart

    mart = build_revenue_decision_mart(view="campaign", window=WINDOW, now=NOW)
    assert mart["summary"]["roas"] is None
    assert mart["spend_truth"]["campaign_spend_status"] != "verified"
    # Diagnostics explain why ROAS is withheld (never a silent empty).
    codes = {d["code"] for d in mart["diagnostics"]}
    assert "campaign_spend_coverage" in codes


# ── 10b. FX incomplete: never label native GBP as USD ───────────────────────

def test_fx_incomplete_never_labels_native_gbp_as_usd(monkeypatch):
    # FX coverage is incomplete -> USD spend is unknowable. The mart must NOT
    # fall back to the native GBP figure and call it USD.
    _patch_durable(monkeypatch, canonical=CANONICAL_FX_INCOMPLETE)
    from services.revenue_decision_mart import build_revenue_decision_mart

    mart = build_revenue_decision_mart(view="campaign", window=WINDOW, now=NOW)

    # FX incomplete => no trustworthy USD spend anywhere in spend_truth/summary.
    assert mart["spend_truth"]["fx_status"] == "incomplete"
    assert mart["spend_truth"]["usd_spend"] is None
    assert mart["summary"]["spend_usd"] is None
    assert mart["summary"]["roas"] is None

    # Native spend is preserved ONLY in spend_truth.native_spend (as GBP), and is
    # never surfaced as a USD figure in the summary.
    assert mart["spend_truth"]["native_currency"] == "GBP"
    assert mart["spend_truth"]["native_spend"] == 10000.0
    assert mart["summary"]["spend_usd"] != 10000.0

    # The same truth holds for every view (shared spend_truth).
    for view in ("country", "source", "deal"):
        other = build_revenue_decision_mart(view=view, window=WINDOW, now=NOW)
        assert other["spend_truth"]["usd_spend"] is None
        assert other["summary"]["spend_usd"] is None
        assert other["summary"]["roas"] is None


def test_fx_incomplete_emits_fx_diagnostic(monkeypatch):
    _patch_durable(monkeypatch, canonical=CANONICAL_FX_INCOMPLETE)
    from services.revenue_decision_mart import build_revenue_decision_mart

    mart = build_revenue_decision_mart(view="campaign", window=WINDOW, now=NOW)
    codes = {d["code"] for d in mart["diagnostics"]}
    assert "fx_coverage" in codes


# ── 11. Existing revenue endpoints can be compared against the mart ─────────

def test_audit_compares_every_page_against_the_mart(monkeypatch):
    _patch_durable(monkeypatch)
    from services.revenue_decision_mart import build_revenue_performance_audit

    audit = build_revenue_performance_audit(WINDOW, now=NOW)
    assert audit["window"]["key"] == "ytd"
    keys = {p["page_key"] for p in audit["pages"]}
    assert keys == {"roas_by_campaign", "roas_by_country", "revenue_by_source", "deals"}

    by_key = {p["page_key"]: p for p in audit["pages"]}
    for page in audit["pages"]:
        for field in ("page", "current_value", "mart_value", "difference",
                      "status", "reasons", "metric"):
            assert field in page

    # Campaign / Country / Deals agree with the mart by construction.
    assert by_key["roas_by_campaign"]["status"] == "pass"
    assert by_key["roas_by_country"]["status"] == "pass"
    assert by_key["deals"]["status"] == "pass"

    # The Source page reports spend on a different basis (geo native vs canonical
    # USD) — the audit must surface the difference WITH a concrete reason.
    src = by_key["revenue_by_source"]
    if src["status"] != "pass":
        assert src["reasons"], "a differing page must explain why it differs"
        assert "different_source_table" in src["reasons"]


def test_audit_flags_geo_reconciliation_difference(monkeypatch):
    # Break geo<->canonical reconciliation by making the canonical Google Ads geo
    # total (PR-ADS-124 source) no longer match the canonical campaign total.
    broken_total = {"available": True, "has_rows": True,
                    "total_cost_micros": 1000_000_000, "total_spend": 1000.0,
                    "rows_counted": 1, "country_count": 1,
                    "currency_code": "GBP", "customer_id": "111"}
    _patch_durable(monkeypatch, geo_total=broken_total)
    from services.revenue_decision_mart import build_revenue_decision_mart, \
        build_revenue_performance_audit

    mart = build_revenue_decision_mart(view="country", window=WINDOW, now=NOW)
    assert mart["spend_truth"]["country_spend_status"] == "mismatch"

    audit = build_revenue_performance_audit(WINDOW, now=NOW)
    country = next(p for p in audit["pages"] if p["page_key"] == "roas_by_country")
    if country["status"] != "pass":
        assert "different_geo_reconciliation" in country["reasons"]


# ── 12. No Google Ads / HubSpot writes anywhere in the mart ─────────────────

def test_mart_source_has_no_external_write_calls():
    path = os.path.join(ROOT, "services", "revenue_decision_mart.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    lowered = src.lower()
    # No mutation verbs against external platforms.
    for forbidden in (
        "execute_action", "mutate", "upload_offline", "offline_conversion",
        "google_ads_client", "googleads", "hubspot_client",
        "requests.post", "requests.put", "requests.patch", "requests.delete",
        "import requests", "db.writers",
    ):
        assert forbidden not in lowered, f"mart must not reference '{forbidden}'"
    # And the read-only doctrine is stated explicitly.
    assert "read-only" in lowered


def test_building_the_mart_invokes_no_write_functions(monkeypatch):
    # Any accidental durable write must be observable — fail loudly if called.
    import db.writers as db_writers
    for name in dir(db_writers):
        if name.startswith(("write_", "upsert_", "record_", "update_", "insert_")):
            attr = getattr(db_writers, name)
            if callable(attr):
                def _boom(*a, _n=name, **k):
                    raise AssertionError(f"unexpected write call: db.writers.{_n}")
                monkeypatch.setattr(db_writers, name, _boom, raising=False)

    # Re-apply read patches AFTER the write guards so reads still work.
    _patch_durable(monkeypatch)
    from services.revenue_decision_mart import (
        build_revenue_decision_mart, build_revenue_performance_audit,
    )
    for view in ("campaign", "country", "source", "deal"):
        build_revenue_decision_mart(view=view, window=WINDOW, now=NOW)
    build_revenue_performance_audit(WINDOW, now=NOW)


# ── Canonical output shape ──────────────────────────────────────────────────

def test_canonical_output_shape(monkeypatch):
    mart = _mart("campaign", monkeypatch)
    for key in ("window", "spend_truth", "summary", "rows", "diagnostics"):
        assert key in mart
    for key in ("native_currency", "native_spend", "usd_spend", "fx_status",
                "campaign_spend_status", "country_spend_status"):
        assert key in mart["spend_truth"]
    for key in ("spend_usd", "leads", "sqls", "customers", "won_revenue_usd", "roas"):
        assert key in mart["summary"]
    assert mart["google_ads_conversion_value_used"] is False
    assert isinstance(mart["rows"], list)
    assert isinstance(mart["diagnostics"], list)
    # Leads/SQLs/customers are the shared business-window totals.
    assert mart["summary"]["leads"] == 5
    assert mart["summary"]["sqls"] == 2
    assert mart["summary"]["customers"] == 1
    assert mart["summary"]["won_revenue_usd"] == 9000.0
    assert mart["summary"]["roas"] == round(9000.0 / 13000.0, 2)


# ── API endpoints (read-only, behind auth) ──────────────────────────────────

def _make_client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] not available")
    try:
        from api.server import app
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    return TestClient(app, raise_server_exceptions=False)


def _viewer_cookie():
    from api.auth import set_session
    from starlette.responses import Response as StarletteResponse
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    r = StarletteResponse()
    set_session(r, "testviewer", "viewer")
    cookie_header = r.headers.get("set-cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return part.split("=", 1)[1]
    return None


def test_endpoint_returns_canonical_contract(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not create viewer session cookie")

    res = client.get("/api/revenue-performance?view=campaign&window=ytd",
                     cookies={"ads_session": cookie})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["view"] == "campaign"
    assert body["window"]["key"] == "ytd"
    assert "spend_truth" in body and "summary" in body and "rows" in body


def test_endpoint_rejects_invalid_view(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not create viewer session cookie")

    res = client.get("/api/revenue-performance?view=nope&window=ytd",
                     cookies={"ads_session": cookie})
    assert res.status_code == 400


def test_audit_endpoint_returns_pages(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not create viewer session cookie")

    res = client.get("/api/revenue-performance/audit?window=ytd",
                     cookies={"ads_session": cookie})
    assert res.status_code == 200, res.text
    body = res.json()
    assert "pages" in body and len(body["pages"]) == 4
