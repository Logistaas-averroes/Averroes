"""
PR-ADS-138 — Dashboard Countries & Geo Intelligence.

Proves the fifth Dashboard tab (Dashboard → Countries) and its read-only contract:

  - GET /api/dashboard/countries exists, is auth-gated, read-only, and uses
    BUSINESS windows; ad windows (30d) are a 400, not a coerced fallback.
  - It composes the canonical Revenue Decision Mart (view="country"), the
    per-country native-GBP + FX-gated-USD geo spend, and the closed-won deal
    ledger — no new business math, no new attribution model. The Google Ads
    conversion value is never used as revenue.
  - Google Ads geo spend is native GBP and is never labelled USD. USD spend +
    Geo ROAS are Unavailable when FX is incomplete, but native GBP spend is still
    present.
  - Geo ROAS appears only when a country has real attributed spend + FX + revenue
    are safe and geo reconciliation is unblockable (never a misleading 0.00x).
  - No fake $0 / 0.00x / 0%: a missing deal amount makes that country's revenue
    (and ROAS) Unavailable, never a lowered $0; a missing prior period yields
    "No comparison".
  - The unattributed residual (spend with no country + revenue with no country) is
    PRESERVED in an explicit bucket — never distributed across real countries,
    never a real-country row, never mapped, never given a ROAS, never a drawer.
  - Frontend: the Countries tab exists and activates (Overview stays default),
    is hash-linkable, calls /api/dashboard/countries, reuses the PR-134/135/136/
    137 system, renders native GBP (never "$"), never a ROAS on a non-safe row,
    never leaks undefined/null/NaN, the residual row is not clickable, and the map
    excludes the residual.
  - Read-only: no platform-write verbs in the touched files; building the contract
    calls no db.writers write fn.

Patches the durable repository boundary (db.revenue_repository) with one coherent
dataset — no real database, no network.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

from tests.canonical_ledger_fixtures import (  # noqa: E402
    from_legacy_deal_rows, patch_canonical_ledger,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SERVICE = open(
    os.path.join(ROOT, "services", "dashboard_countries_service.py"), encoding="utf-8"
).read()

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
WINDOW = "ytd"

# The Countries frontend block sits between these two markers in app.js.
_BLOCK_START = "// ── Dashboard — Countries & Geo Intelligence tab"
_BLOCK_END = "// ── Campaigns page"


def _block():
    start = JS.find(_BLOCK_START)
    end = JS.find(_BLOCK_END)
    assert start != -1 and end > start, "countries block not found"
    return JS[start:end]


def _slice(hay, marker, span=3200):
    i = hay.find(marker)
    assert i != -1, f"{marker} not found"
    return hay[i:i + span]


# ── Coherent durable dataset (GBP 10,000 / USD 13,000; US revenue + GB spend) ─

CANONICAL = {
    "available": True, "customer_id": "111", "currency_code": "GBP",
    "reporting_currency": "USD", "fx_complete": True, "fx_missing_days": 0,
    "campaign_count": 2, "total_spend": 10000.0, "total_spend_usd": 13000.0,
    "total_cost_micros": 10000_000_000,
    "rows": [
        {"campaign_id": "c1", "campaign_name": "Global Competitors", "cost_micros": 6000_000_000,
         "spend": 6000.0, "spend_usd": 8000.0, "fx_complete": True},
        {"campaign_id": "c2", "campaign_name": "Brand - UK", "cost_micros": 4000_000_000,
         "spend": 4000.0, "spend_usd": 5000.0, "fx_complete": True},
    ],
    "coverage_start": "2026-01-01", "coverage_end": "2026-06-22",
}
GEO_ROWS = [
    {"campaign_name": "Global Competitors", "country": "United States", "spend": 6000.0},
    {"campaign_name": "Brand - UK", "country": "United Kingdom", "spend": 4000.0},
]
GEO_CANONICAL_TOTAL = {"available": True, "has_rows": True, "total_cost_micros": 10000_000_000,
                       "total_spend": 10000.0, "rows_counted": 2, "country_count": 2,
                       "currency_code": "GBP", "customer_id": "111"}
GEO_BY_COUNTRY = {"available": True, "has_rows": True, "total_spend": 10000.0,
                  "total_spend_usd": 13000.0, "fx_complete": True, "currency_code": "GBP",
                  "customer_id": "111", "reporting_currency": "USD",
                  "rows": [
                      {"country_criterion_id": "2840", "country_code": "US", "country_name": "United States",
                       "cost_micros": 6000_000_000, "spend": 6000.0, "spend_usd": 8000.0, "fx_complete": True},
                      {"country_criterion_id": "2826", "country_code": "GB", "country_name": "United Kingdom",
                       "cost_micros": 4000_000_000, "spend": 4000.0, "spend_usd": 5000.0, "fx_complete": True}]}
COVERAGE_COMPLETE = [{"chunk_start": "2026-01-01", "chunk_end": "2026-12-31", "status": "verified"}]
LEAD_ROWS = (
    [{"campaign_name": "Global Competitors", "country": "United States",
      "status_category": "qualified", "has_gclid": True}] * 20
    + [{"campaign_name": "Global Competitors", "country": "United States",
        "status_category": "lead", "has_gclid": True}] * 80
    + [{"campaign_name": "Brand - UK", "country": "United Kingdom",
        "status_category": "qualified", "has_gclid": True}] * 5
)
WON_ROWS = [
    {"campaign_name": "Global Competitors", "country": "United States",
     "deal_id": "d1", "deal_amount_usd": 18000.0, "match_status": "matched"},
    {"campaign_name": "Global Competitors", "country": "United States",
     "deal_id": "d2", "deal_amount_usd": 11000.0, "match_status": "matched"},
    {"campaign_name": None, "country": None,
     "deal_id": "d4", "deal_amount_usd": 4000.0, "match_status": "unknown"},
]
DEAL_ROWS = [
    {"deal_id": "d1", "company": "Acme Logistics", "country": "United States",
     "campaign_name": "Global Competitors", "deal_close_date": "2026-06-14",
     "deal_amount_usd": 18000.0, "deal_stage_label": "Closed Won", "match_status": "matched", "match_source": "gclid"},
    {"deal_id": "d2", "company": "Blue Cargo", "country": "United States",
     "campaign_name": "Global Competitors", "deal_close_date": "2026-05-23",
     "deal_amount_usd": 11000.0, "deal_stage_label": "Closed Won", "match_status": "matched", "match_source": "gclid"},
    {"deal_id": "d4", "company": "Ghost Co", "country": None, "campaign_name": None,
     "deal_close_date": "2026-06-19", "deal_amount_usd": 4000.0, "deal_stage_label": "Closed Won",
     "match_status": "unknown", "match_source": None},
]


# PR-ADS-153F: the durable geo coverage ledger is a MANDATORY gate input, so a
# fixture describing a healthy account must prove the window was fetched — not
# merely supply totals for it. Tests wanting the never-fetched case pass
# `geo_coverage=[]`.
GEO_COVERAGE_COMPLETE = [
    {"chunk_start": "2000-01-01", "chunk_end": "2100-01-01", "status": "verified",
     "rows_written": 13516, "cost_micros_total": 5_000_000, "country_count": 191},
]


def _patch_durable(monkeypatch, *, canonical=None, geo_by_country=None, deals=None,
                   revenue_connected=True, geo_coverage=None):
    import db.revenue_repository as repo

    canon = canonical if canonical is not None else CANONICAL
    geo = geo_by_country if geo_by_country is not None else GEO_BY_COUNTRY
    dl = deals if deals is not None else DEAL_ROWS

    monkeypatch.setattr(repo, "fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr(repo, "fetch_campaign_country_spend",
                        lambda s, e: {"available": True, "rows": list(GEO_ROWS),
                                      "coverage_start": "2026-01-01", "coverage_end": "2026-06-22"})
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: {"available": True, "rows": list(LEAD_ROWS),
                                      "event_date_safe": True, "missing_contact_created_at_count": 0,
                                      "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0})
    monkeypatch.setattr(repo, "fetch_won_revenue",
                        lambda s, e: {"available": True, "rows": list(WON_ROWS),
                                      "coverage_start": "2026-01-01", "coverage_end": "2026-06-22"})
    monkeypatch.setattr(repo, "fetch_sync_state", lambda: {"available": True, "datasets": {}})
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e: dict(canon))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total", lambda s, e: dict(GEO_CANONICAL_TOTAL))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country", lambda s, e: dict(geo))
    monkeypatch.setattr(repo, "fetch_spend_coverage",
                        lambda s, e: {"available": True, "chunks": list(COVERAGE_COMPLETE)})
    monkeypatch.setattr(repo, "fetch_geo_coverage",
                        lambda c, s, e: {"available": True,
                                         "chunks": list(GEO_COVERAGE_COMPLETE
                                                        if geo_coverage is None
                                                        else geo_coverage)})
    monkeypatch.setattr(repo, "fetch_campaign_identity", lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: revenue_connected)
    # PR-ADS-153E-B: closed-won proof deals come from the canonical deal ledger.
    patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(dl))
    import db.writers as db_writers
    monkeypatch.setattr(db_writers, "source_attribution_health_counts",
                        lambda: {"classified_contacts": 25, "attributed_deals": 2,
                                 "ambiguous_deals": 0, "unclassified_deals": 1})


def _countries(monkeypatch, window=WINDOW, **kw):
    _patch_durable(monkeypatch, **kw)
    from services.dashboard_countries_service import build_dashboard_countries
    return build_dashboard_countries(window, now=NOW)


def _ctry(out, code):
    return next((c for c in out["countries"] if c["country_code"] == code), None)


# ══════════════════ 1. Endpoint + windows + auth ════════════════════════════


def test_endpoint_registered():
    assert '@app.get("/api/dashboard/countries")' in SERVER
    assert "build_dashboard_countries" in SERVER
    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/dashboard/countries" in routes


def test_all_business_windows_accepted(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_countries_service import build_dashboard_countries
    for key in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert build_dashboard_countries(key, now=NOW)["window"]["key"] == key


def test_invalid_window_raises(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_countries_service import build_dashboard_countries
    for bad in ("30d", "60d", "7d", "14d", "", "quarter"):
        with pytest.raises(ValueError):
            build_dashboard_countries(bad, now=NOW)


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
    for part in r.headers.get("set-cookie", "").split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return part.split("=", 1)[1]
    return None


def test_endpoint_requires_auth():
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    client = _make_client()
    r = client.get("/api/dashboard/countries?window=ytd")
    assert r.status_code in (401, 403), r.text


def test_endpoint_accepts_business_window_rejects_ad_window(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not mint viewer cookie")
    ok = client.get("/api/dashboard/countries?window=last_quarter", cookies={"ads_session": cookie})
    assert ok.status_code == 200, ok.text
    assert ok.json()["window"]["key"] == "last_quarter"
    bad = client.get("/api/dashboard/countries?window=30d", cookies={"ads_session": cookie})
    assert bad.status_code == 400, bad.text


# ══════════════════ 2. Composition + truth ══════════════════════════════════


def test_read_only_and_conversion_value_never_used(monkeypatch):
    out = _countries(monkeypatch)
    assert out["read_only"] is True
    assert out["google_ads_conversion_value_used"] is False
    assert out["source_truth"] == "revenue_decision_mart_country_view"
    assert "conversion value" in SERVICE.lower()


def test_kpis_from_country_attributed_truth(monkeypatch):
    out = _countries(monkeypatch)
    k = out["kpis"]
    assert k["verified_spend_native"] == {"amount": 10000.0, "currency": "GBP"}
    assert k["verified_spend_usd"] == 13000.0
    assert k["countries_with_spend"] == 2
    assert k["countries_with_customers"] == 1
    assert k["sqls"] == 25  # 20 US + 5 GB
    assert k["customers"] == 2  # US only (Ghost Co has no country → residual)
    assert k["won_revenue_usd"] == 29000.0
    assert k["geo_roas"] == round(29000.0 / 13000.0, 2)  # 2.23 — country-attributed


def test_kpis_reconcile_with_country_rows(monkeypatch):
    out = _countries(monkeypatch)
    k = out["kpis"]
    real = [c for c in out["countries"] if not c["is_residual"]]
    assert k["sqls"] == sum(c["sqls"] for c in real)
    assert k["customers"] == sum(c["customers"] for c in real)
    assert k["won_revenue_usd"] == round(sum(c["won_revenue_usd"] for c in real), 2)
    assert k["verified_spend_native"]["amount"] == round(sum(c["spend_native"] for c in real), 2)
    assert k["verified_spend_usd"] == round(sum(c["spend_usd"] for c in real), 2)


def test_native_gbp_never_labelled_usd(monkeypatch):
    out = _countries(monkeypatch)
    assert out["kpis"]["verified_spend_native"]["currency"] == "GBP"
    for c in out["countries"]:
        assert c["native_currency"] == "GBP"


def test_country_rows_carry_region(monkeypatch):
    out = _countries(monkeypatch)
    assert _ctry(out, "US")["region"] == "North America"
    assert _ctry(out, "GB")["region"] == "Europe"


# ══════════════════ 3. Spend / FX / ROAS truth ══════════════════════════════


def test_fx_incomplete_keeps_native_gbp_but_withholds_usd_and_roas(monkeypatch):
    canon = dict(CANONICAL, fx_complete=False, fx_missing_days=5, total_spend_usd=None,
                 rows=[dict(r, spend_usd=None, fx_complete=False) for r in CANONICAL["rows"]])
    geo = dict(GEO_BY_COUNTRY, total_spend_usd=None, fx_complete=False,
               rows=[dict(r, spend_usd=None, fx_complete=False) for r in GEO_BY_COUNTRY["rows"]])
    out = _countries(monkeypatch, canonical=canon, geo_by_country=geo)
    k = out["kpis"]
    assert k["verified_spend_native"]["amount"] == 10000.0  # native GBP still present
    assert k["verified_spend_usd"] is None  # USD Unavailable
    assert k["geo_roas"] is None  # ROAS Unavailable
    us = _ctry(out, "US")
    assert us["spend_native"] == 6000.0 and us["spend_native"] != 0
    assert us["spend_usd"] is None
    assert us["roas"] is None and us["roas_available"] is False
    # ROAS unavailable reason must cite FX / USD.
    roas_u = next(u for u in out["unavailable"] if u["metric"] == "geo_roas")
    assert "FX" in roas_u["reason"] or "USD" in roas_u["reason"]


def test_geo_roas_only_when_spend_and_revenue_safe(monkeypatch):
    out = _countries(monkeypatch)
    us = _ctry(out, "US")
    assert us["roas"] == round(29000.0 / 8000.0, 2) and us["roas_available"] is True
    # A country with verified spend but ZERO revenue shows no misleading 0.00x —
    # its status conveys the outcome.
    gb = _ctry(out, "GB")
    assert gb["roas"] is None and gb["roas_available"] is False
    assert gb["status"] == "SQL producer"


def test_geo_roas_is_revenue_over_spend_only(monkeypatch):
    assert "conversion value" in SERVICE.lower()
    out = _countries(monkeypatch)
    assert out["google_ads_conversion_value_used"] is False
    assert out["kpis"]["geo_roas"] == round(29000.0 / 13000.0, 2)


def test_roas_helper_never_fakes_zero():
    from services.dashboard_countries_service import _roas
    assert _roas(29000, 8000) == round(29000 / 8000, 2)
    assert _roas(0, 8000) is None      # zero revenue → no 0.00x
    assert _roas(29000, 0) is None     # zero spend → no divide
    assert _roas(None, 8000) is None
    assert _roas(29000, None) is None


# ══════════════════ 4. Residual isolation (NEVER distributed) ════════════════


def test_residual_revenue_preserved_not_a_country(monkeypatch):
    # The closed-won deal with no country (Ghost Co) is preserved in the residual
    # bucket — never forced onto a real country, never a real-country row.
    out = _countries(monkeypatch)
    res = out["residual"]
    assert res["label"] == "Unknown / Unattributed country"
    assert res["customers"] == 1
    assert res["won_revenue_usd"] == 4000.0
    # It is NOT in the countries list and NO country row is a residual.
    assert all(not c["is_residual"] for c in out["countries"])
    assert all(c["country_name"] != "Unknown / Unattributed country" for c in out["countries"])
    # Residual revenue/customers surfaced as KPIs, separate from country totals.
    assert out["kpis"]["residual_revenue_usd"] == 4000.0
    assert out["kpis"]["residual_customers"] == 1
    # Country-attributed customers exclude the residual customer.
    assert out["kpis"]["customers"] == 2


def test_residual_never_mapped_never_roas_never_drawer(monkeypatch):
    out = _countries(monkeypatch)
    res = out["residual"]
    assert res["roas"] is None
    assert res["roas_available"] is False
    assert res["drawer_available"] is False


def test_build_residual_is_canonical_mart_minus_countries():
    # Residual customers / revenue / SQLs are the mart window TOTAL minus the sum
    # of the real country rows (canonical), NOT the drawer deal ledger. The geo
    # spend residual is carried from spend_truth. Never spread across countries.
    from services.dashboard_countries_service import _build_residual
    summary = {"customers": 5, "won_revenue_usd": 90000.0, "sqls": 40}
    real_rows = [
        {"customers": 3, "won_revenue_usd": 60000.0, "sqls": 25},
        {"customers": 1, "won_revenue_usd": 15000.0, "sqls": 10},
    ]
    spend_truth = {
        "native_currency": "GBP",
        "country_residual_native": 500.0,
        "country_residual_usd": 640.0,
        "country_residual_reason":
            "Google Ads geographic view does not assign this spend to a country.",
    }
    res = _build_residual(summary, real_rows, spend_truth, None, False, True)
    assert res["customers"] == 1          # 5 − (3+1)
    assert res["won_revenue_usd"] == 15000.0   # 90000 − (60000+15000)
    assert res["sqls"] == 5               # 40 − (25+10)
    assert res["spend_native"] == 500.0
    assert res["spend_usd"] == 640.0
    assert res["roas"] is None and res["drawer_available"] is False
    assert "does not assign this spend to a country" in res["reason"]


def test_build_residual_null_amount_is_unavailable_not_zero():
    # A residual deal with an unknown amount withholds residual revenue (never a
    # lowered $0) but customers stay countable from the canonical mart total.
    from services.dashboard_countries_service import _build_residual
    summary = {"customers": 3, "won_revenue_usd": 33000.0, "sqls": 20}
    real_rows = [{"customers": 2, "won_revenue_usd": 29000.0, "sqls": 20}]
    res = _build_residual(summary, real_rows, {"native_currency": "GBP"}, None, True, True)
    assert res["won_revenue_usd"] is None  # never a lowered $0
    assert res["customers"] == 1           # 3 − 2, still countable


def test_residual_withheld_not_zeroed_on_canonical_ledger_outage(monkeypatch):
    # PR-ADS-153E-B: the residual used to be reconstructed from a mart total that
    # had its own revenue lineage, so it could survive a deal-ledger outage. The
    # mart total IS the canonical ledger now, so an outage withholds the residual
    # too. What must never happen — and is what this test guards — is the residual
    # silently collapsing to 0 customers / $0, which would read as "no
    # unattributed revenue exists" rather than "we could not read it".
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [], available=False)
    from services.dashboard_countries_service import build_dashboard_countries
    out = build_dashboard_countries(WINDOW, now=NOW)
    assert out["residual"]["customers"] != 0
    assert out["residual"]["won_revenue_usd"] != 0
    assert out["revenue_available"] is False
    assert out["legacy_fallback_used"] is False
    assert out["deal_proof_available"] is False
    assert "deal_proof" in {u["metric"] for u in out["unavailable"]}


def test_residual_deal_null_amount_withholds_revenue_keeps_customers(monkeypatch):
    # The no-country residual deal (d4) has an unknown amount → residual revenue is
    # Unavailable (never a lowered $0), but customers stay countable, and the
    # unavailable payload flags residual_revenue_usd with the unknown-amount reason.
    deals = DEAL_ROWS[:2] + [dict(DEAL_ROWS[2], deal_amount_usd=None)]
    out = _countries(monkeypatch, deals=deals)
    assert out["residual"]["won_revenue_usd"] is None
    assert out["residual"]["customers"] == 1
    assert out["kpis"]["residual_revenue_usd"] is None
    ru = next(u for u in out["unavailable"] if u["metric"] == "residual_revenue_usd")
    assert "amount" in ru["reason"].lower()


def test_residual_excluded_from_country_kpis_and_roas(monkeypatch):
    # Residual is never a country row, never gets a ROAS, and is never in the
    # country-attributed KPI ROAS numerator.
    out = _countries(monkeypatch)
    real = [c for c in out["countries"] if not c["is_residual"]]
    assert len(real) == len(out["countries"])  # residual is not in countries[]
    assert out["residual"]["roas"] is None and out["residual"]["roas_available"] is False
    assert out["residual"]["drawer_available"] is False
    # Country-attributed KPIs exclude the residual; ROAS numerator is real-only.
    assert out["kpis"]["customers"] == sum(c["customers"] for c in real)
    assert out["kpis"]["won_revenue_usd"] == round(sum(c["won_revenue_usd"] for c in real), 2)
    assert out["kpis"]["won_revenue_usd"] == 29000.0
    assert out["kpis"]["residual_revenue_usd"] == 4000.0
    assert out["kpis"]["geo_roas"] == round(29000.0 / 13000.0, 2)


# ══════════════════ 5. No fake $0 / disconnected / period change ═════════════


def test_null_amount_makes_country_revenue_and_roas_unavailable(monkeypatch):
    # A US deal with an unknown amount → that country's revenue (and ROAS)
    # Unavailable, never a lowered $0. KPI revenue also Unavailable.
    deals = [dict(DEAL_ROWS[0], deal_amount_usd=None)] + DEAL_ROWS[1:]
    out = _countries(monkeypatch, deals=deals)
    us = _ctry(out, "US")
    assert us["won_revenue_usd"] is None and us["won_revenue_usd"] != 0
    assert us["roas"] is None
    assert out["kpis"]["won_revenue_usd"] is None
    assert out["kpis"]["geo_roas"] is None


def test_disconnected_integration_blanks_customers_and_revenue(monkeypatch):
    out = _countries(monkeypatch, revenue_connected=False)
    k = out["kpis"]
    assert k["customers"] is None
    assert k["won_revenue_usd"] is None
    assert k["geo_roas"] is None
    assert k["residual_customers"] is None
    assert k["residual_revenue_usd"] is None
    assert k["verified_spend_native"]["amount"] == 10000.0  # spend survives
    assert k["sqls"] == 25  # SQLs come from leads and survive
    for c in out["countries"]:
        assert c["customers"] is None, f"{c['country_name']} customers must be None"
        assert c["won_revenue_usd"] is None and c["roas"] is None
    assert out["truth_status"]["revenue"] == "blocked"


def test_disconnected_status_is_not_revenue_proven_and_cards_have_no_none(monkeypatch):
    # Regression: when the integration is disconnected the numbers are blanked, so
    # a country's STATUS must also never claim "Revenue proven" over Unavailable
    # numbers, and decision-card copy must never render a literal "None".
    out = _countries(monkeypatch, revenue_connected=False)
    assert all(c["status"] != "Revenue proven" for c in out["countries"])
    # No card may render a literal Python "None" (a leaked blanked value). The
    # phrase "never a lowered $0" is legitimate doctrine copy, so $0 is not checked.
    for card in out["decision_cards"]:
        assert "None " not in card["body"] and "None." not in card["body"], card["body"]


def test_geo_source_outage_yields_unavailable_not_zero(monkeypatch):
    # Regression: a geo-spend source outage must render Unavailable spend, never a
    # fabricated £0 total — even for the country rows.
    out = _countries(monkeypatch, geo_by_country={"available": False, "has_rows": False, "rows": []})
    assert out["kpis"]["verified_spend_native"]["amount"] is None
    assert out["kpis"]["verified_spend_usd"] is None
    assert out["kpis"]["geo_roas"] is None
    for c in out["countries"]:
        assert c["spend_native"] is None  # never a fabricated 0 on outage
        assert c["roas"] is None


def test_country_status_distinguishes_unavailable_spend_from_zero():
    # Regression (Finding 5): None spend (source down) must NOT read as a measured
    # "No spend in window" zero when the country has no other proof.
    from services.dashboard_countries_service import (
        _country_status, STATUS_NO_SPEND, STATUS_GEO_REVIEW)
    # Measured zero spend, no proof → honestly "No spend in window".
    assert _country_status(native_spend=0.0, usd_spend=0.0, fx_complete=True, sqls=0,
                           customers=0, won_revenue=0, attribution_status="mapped") == STATUS_NO_SPEND
    # Unknown spend (geo down), no proof → never asserted as a measured zero.
    assert _country_status(native_spend=None, usd_spend=None, fx_complete=False, sqls=0,
                           customers=0, won_revenue=None, attribution_status="mapped") == STATUS_GEO_REVIEW


def test_geo_spend_merge_accumulates_duplicate_codes():
    # Regression (Finding 4): two geo rows resolving to the same country are
    # SUMMED, never last-wins (which would silently drop spend).
    #
    # PR-ADS-153F: the map is now keyed on the CANONICAL country key rather than
    # carrying a by_code map AND a by_name map. That is a stronger form of the
    # same guarantee — "UAE" and "United Arab Emirates" merge because they
    # resolve to the same identity, not because a caller happened to try the
    # code map before the name map.
    from analysis.country_identity import country_key
    from services.dashboard_countries_service import _geo_spend_by_country
    ae = country_key("United Arab Emirates")
    maps = _geo_spend_by_country({"rows": [
        {"country_code": "AE", "country_name": "United Arab Emirates", "spend": 6000.0, "spend_usd": 7800.0, "fx_complete": True},
        {"country_code": "AE", "country_name": "UAE", "spend": 1000.0, "spend_usd": 1300.0, "fx_complete": True},
    ]})
    assert maps["by_key"][ae]["native"] == 7000.0
    assert maps["by_key"][ae]["usd"] == 9100.0
    # A FX gap on either side withholds the merged USD (never a partial sum).
    maps2 = _geo_spend_by_country({"rows": [
        {"country_code": "AE", "country_name": "x", "spend": 6000.0, "spend_usd": 7800.0, "fx_complete": True},
        {"country_code": "AE", "country_name": "y", "spend": 1000.0, "spend_usd": None, "fx_complete": False},
    ]})
    assert maps2["by_key"][ae]["native"] == 7000.0
    assert maps2["by_key"][ae]["usd"] is None
    assert maps2["by_key"][ae]["fx_complete"] is False


def test_geo_spend_without_an_identifiable_country_goes_to_the_residual():
    """PR-ADS-153F: geo spend with no usable country is kept, not dropped.

    It is real Google Ads spend and must stay in the account total, but it is
    not evidence about any market — so it lands in the residual rather than
    being attached to a country or silently discarded.
    """
    from services.dashboard_countries_service import _geo_spend_by_country
    maps = _geo_spend_by_country({"rows": [
        {"country_code": None, "country_name": None, "spend": 500.0,
         "spend_usd": 650.0, "fx_complete": True},
        {"country_code": "XX", "country_name": "Atlantis", "spend": 250.0,
         "spend_usd": 325.0, "fx_complete": True},
    ]})
    assert maps["by_key"] == {}
    assert maps["residual"]["native"] == 750.0
    assert maps["residual"]["usd"] == 975.0


def test_no_fake_deltas_when_previous_baseline_missing(monkeypatch):
    out = _countries(monkeypatch, window="all_time")
    pc = out["period_change"]
    assert pc["available"] is False
    assert pc["metrics"] == {}
    assert "period_change" in {u["metric"] for u in out["unavailable"]}


def test_period_change_metrics_are_country_attributed(monkeypatch):
    out = _countries(monkeypatch, window="ytd")
    pc = out["period_change"]
    # Whatever the availability, the comparison metric keys are the country KPIs.
    for key in ("won_revenue_usd", "customers", "sqls", "verified_spend_usd", "geo_roas"):
        assert key in pc["metrics"]


# ══════════════════ 6. Deal proof unavailable ═══════════════════════════════


def test_deal_ledger_unavailable_does_not_claim_no_deals(monkeypatch):
    # PR-ADS-153E-B: the canonical ledger IS country revenue now, so an outage
    # withholds it. The guard is that nothing renders as a measured zero, and no
    # legacy ledger is consulted.
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [], available=False)
    from services.dashboard_countries_service import build_dashboard_countries
    out = build_dashboard_countries(WINDOW, now=NOW)
    assert out["deal_proof_available"] is False
    assert out["truth_status"]["deal_proof"] == "unavailable"
    assert "deal_proof" in {u["metric"] for u in out["unavailable"]}
    assert out["revenue_available"] is False
    assert out["legacy_fallback_used"] is False
    assert _ctry(out, "US")["won_revenue_usd"] is None, "an outage is not $0"
    assert out["kpis"]["won_revenue_usd"] is None
    # No deals attached, but the drawer must say Unavailable, not "no deals".
    assert _ctry(out, "US")["deals"] == []


def test_drawer_handles_deal_proof_unavailable():
    fn = _slice(JS, "function wireCtryDrawers", 4000)
    assert "deal_proof_available" in fn
    assert "Closed-won deal proof unavailable" in fn
    assert "No closed-won deals to prove this country" in fn


# ══════════════════ 7. Regional mix + cards + truth status ═══════════════════


def test_regional_mix_groups_countries(monkeypatch):
    out = _countries(monkeypatch)
    mix = {r["region"]: r for r in out["regional_mix"]}
    assert "North America" in mix and "Europe" in mix
    assert mix["North America"]["won_revenue_usd"] == 29000.0
    assert mix["North America"]["customers"] == 2
    assert mix["Europe"]["customers"] == 0
    # No fabricated percentages: a region's roas is only present when safe.
    assert mix["Europe"]["roas"] is None


def test_decision_cards_shape_and_safety(monkeypatch):
    out = _countries(monkeypatch)
    types = [c["type"] for c in out["decision_cards"]]
    assert types == ["scale", "watch", "investigate", "unavailable"]
    for c in out["decision_cards"]:
        assert c["headline"] and c["body"]
        assert "$0" not in c["body"]
    scale = next(c for c in out["decision_cards"] if c["type"] == "scale")
    assert "United States" in scale["headline"]


def test_truth_status_keys(monkeypatch):
    out = _countries(monkeypatch)
    ts = out["truth_status"]
    for key in ("spend", "fx", "revenue", "geo_attribution", "residual", "deal_proof"):
        assert key in ts
    # Healthy window: everything reconciles.
    assert ts["geo_attribution"] == "ready"
    assert ts["residual"] == "ready"


# ══════════════════ 8. Region mapping helper (introduced, nothing mislabelled)


def test_region_mapping_helper():
    from services.dashboard_countries_service import _region_for_code, REGION_UNKNOWN
    assert _region_for_code("US") == "North America"
    assert _region_for_code("GB") == "Europe"
    assert _region_for_code("AE") == "Middle East"
    assert _region_for_code("MX") == "LATAM"
    assert _region_for_code("EG") == "Africa"
    assert _region_for_code("SG") == "APAC"
    # An unresolved / unknown code is never guessed — it lands in a clear lane.
    assert _region_for_code(None) == REGION_UNKNOWN
    assert _region_for_code("ZZ") == REGION_UNKNOWN
    assert _region_for_code("") == REGION_UNKNOWN


# ══════════════════ 9. Read-only safety ═════════════════════════════════════


def test_no_platform_writes_in_touched_files():
    files = [
        "services/dashboard_countries_service.py",
        "api/server.py",
        "static/app.js",
    ]
    for rel in files:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
        for forbidden in (".mutate(", "upload_offline", "offline_conversion",
                          "google_ads_upload", "upload_conversions",
                          "requests.post", "requests.put", "requests.patch", "requests.delete",
                          "set_budget", "set_bid", "pause_campaign", "enable_campaign",
                          "create_deal", "update_deal", "create_contact", "update_contact",
                          "add_negative", "negative_keyword"):
            assert forbidden not in src, f"{rel} must not reference '{forbidden}'"


def test_building_countries_invokes_no_write_functions(monkeypatch):
    import db.writers as db_writers
    for name in dir(db_writers):
        if name.startswith(("write_", "upsert_", "record_", "update_", "insert_")):
            attr = getattr(db_writers, name)
            if callable(attr):
                def _boom(*a, _n=name, **k):
                    raise AssertionError(f"unexpected write call: db.writers.{_n}")
                monkeypatch.setattr(db_writers, name, _boom, raising=False)
    _patch_durable(monkeypatch)
    from services.dashboard_countries_service import build_dashboard_countries
    for key in ("current_quarter", "last_quarter", "ytd", "all_time"):
        build_dashboard_countries(key, now=NOW)


# ══════════════════ 10. Frontend: Countries tab ═════════════════════════════


def _dashboard_section():
    import re
    m = re.search(r'<section id="page-dashboard".*?</section>', HTML, re.DOTALL)
    assert m, "page-dashboard section missing"
    return m.group(0)


def test_countries_tab_enabled_overview_default():
    section = _dashboard_section()
    assert 'data-dash-tab="countries"' in section
    assert 'class="dashboard-tab is-active" data-dash-tab="overview"' in section
    import re
    ch = re.search(r'<button[^>]*data-dash-tab="countries"[^>]*>', section).group(0)
    assert "disabled" not in ch
    assert 'id="dashboard-countries-root"' in section


def test_countries_tab_is_hash_linkable():
    assert '"countries"' in _slice(JS, "const DASHBOARD_TABS", 160)
    fn = _slice(JS, "function activateDashboardTab", 2000)
    assert "dashboard-countries-root" in fn
    assert "loadDashboardCountries" in fn


def test_countries_tab_calls_its_endpoint():
    loader = _slice(JS, "async function loadDashboardCountries", 1500)
    assert "/api/dashboard/countries?window=" in loader
    assert "getRoasBusinessWindow()" in loader
    assert "_revReqSeq.dashCountries" in loader


def test_countries_kpi_cards_render_six_metrics():
    fn = _slice(JS, "function renderCtryKpiRow", 4200)
    for label in ("Verified Geo Spend", "SQLs", "Customers", "Won Revenue",
                  "Geo ROAS", "Residual Revenue"):
        assert label in fn
    assert "dash-kpi-card" in fn


def test_countries_reuses_prior_design_system():
    block = _block()
    for cls in ("dash-kpi-card", "dash-panel", "dash-truth-footer", "dash-chart-hit"):
        assert cls in block, f"countries tab should reuse the dashboard class {cls}"
    assert "renderDashDecisionCards(" in block
    assert "renderDashSkeleton()" in block
    for lib in ("chart.js", "Chart(", "d3.", "highcharts", "recharts", "echarts"):
        assert lib not in JS


def test_native_gbp_rendered_never_dollar():
    block = _block()
    assert "fmtCompactCurrency" in block
    assert "ctryNativeBig" in block
    fn = _slice(JS, "function ctrySpendCell", 600)
    assert "spend_native" in fn and "spend_usd" in fn


def test_geo_roas_cell_gated_on_availability():
    fn = _slice(JS, "function ctryRoasCell", 500)
    assert "roas_available" in fn
    assert "ctry-status" in fn  # non-safe ROAS falls back to status, never a fake x


def test_map_excludes_residual():
    fn = _slice(JS, "function renderCtryGeoMap", 3200)
    assert "is_residual" in fn  # the map filters the residual out


def test_leaderboard_residual_row_not_clickable():
    lb = _slice(JS, "function renderCtryLeaderboard", 3200)
    assert "ctry-row--residual" in lb
    drawer = _slice(JS, "function wireCtryDrawers", 4000)
    # The drawer wiring explicitly skips the residual row.
    assert ".ctry-row:not(.ctry-row--residual)" in drawer


def test_drawer_renders_proof_columns():
    start = JS.find("const CTRY_DEAL_COLUMNS")
    assert start != -1
    block = JS[start:start + 600]
    for label in ("Company", "Company ID", "Main Contact", "Contact ID", "Deal",
                  "Deal ID", "Amount", "Close Date", "Attribution"):
        assert label in block
    fn = _slice(JS, "function ctryDealCell", 400)
    assert "dashValue(deal.amount_usd, fmtMoney)" in fn  # null → Unavailable, never $0


def test_truth_footer_has_residual_and_geo_chips():
    footer = _slice(JS, "function renderCtryTruthFooter", 1600)
    assert "Geo attribution" in footer
    assert "Residual" in footer
    assert "Deal proof" in footer
    assert "residual is isolated, never distributed" in footer


def test_countries_admin_gated_links_respect_role():
    footer = _slice(JS, "function renderCtryTruthFooter", 1600)
    assert "dashCanNavigate(" in footer
    drawer = _slice(JS, "function wireCtryDrawers", 4000)
    assert "dashCanNavigate(" in drawer


def test_countries_block_has_no_undefined_or_null_leaks():
    block = _block()
    assert ">undefined<" not in block
    assert ">null<" not in block
    assert ">NaN<" not in block


def test_countries_responsive_classes_exist():
    for cls in (".ctry-table", ".ctry-scroll", ".ctry-map", ".ctry-lane",
                ".ctry-bubble", ".ctry-drawer", ".ctry-region-list", ".dash-tab-intro"):
        assert cls in CSS, f"{cls} missing from styles.css"
    assert "@media (max-width: 1100px)" in CSS
    assert "@media (max-width: 640px)" in CSS


def test_countries_error_and_loading_states_exist():
    assert "Countries &amp; Markets unavailable" in JS
    loader = _slice(JS, "async function loadDashboardCountries", 1500)
    assert "renderDashSkeleton()" in loader
    assert "is-refreshing" in loader
