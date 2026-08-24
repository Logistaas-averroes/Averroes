"""
PR-ADS-139 — Dashboard Deals, Opportunities & Pipeline Intelligence.

Proves the sixth (final) Dashboard tab (Dashboard → Deals) and its read-only contract:

  - GET /api/dashboard/deals exists, is auth-gated, read-only, and uses BUSINESS
    windows; ad windows (30d) are a 400, not a coerced fallback.
  - Composes the canonical Revenue Decision Mart (deal + campaign views), the
    Revenue-by-Source contract, and the closed-won deal ledger — no new business
    math. The Google Ads conversion value is never used, and no ROAS is shown.
  - HubSpot closed-won is revenue truth (USD). Open pipeline is NOT revenue; a
    closed-lost deal is NOT negative revenue; an opportunity is NOT a customer.
  - The durable ledger stores closed-won ONLY, so open pipeline, closed-lost,
    opportunity aging, opportunity-created counts and the two opportunity rates are
    "Unavailable" (null + reason) — never invented, never a fabricated $0 / 0%.
  - A null deal amount stays null (Amount unavailable), never a lowered $0. Rates
    whose denominator is unsafe are null. A missing prior period yields "No comparison".
  - Qualified SQLs with no closed-won deal are preserved ("not yet closed-won"),
    never claimed as "no deal" (open/lost pipeline is not visible).
  - Disconnected revenue integration / deal-ledger outage blank pipeline/revenue
    safely. Frontend: the Deals tab exists, activates, is bookmarkable, reuses the
    dash-* system, shows no ROAS, labels open pipeline "not revenue", and never
    leaks undefined/null/NaN.
  - Read-only: no platform-write verbs in touched files; the new repository fetcher
    is SELECT-only; building the contract calls no db.writers write fn.
"""

import os
import sys
from datetime import date, datetime, timezone

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
SERVICE = open(os.path.join(ROOT, "services", "dashboard_deals_service.py"), encoding="utf-8").read()
REPO_SRC = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
WINDOW = "ytd"

_BLOCK_START = "// ── Dashboard — Deals, Opportunities & Pipeline Intelligence tab"
_BLOCK_END = "// ── Campaigns page"


def _block():
    start = JS.find(_BLOCK_START)
    end = JS.find(_BLOCK_END)
    assert start != -1 and end > start, "deals block not found"
    return JS[start:end]


def _slice(hay, marker, span=3200):
    i = hay.find(marker)
    assert i != -1, f"{marker} not found"
    return hay[i:i + span]


def _repo_fn_body(name):
    i = REPO_SRC.find(f"def {name}")
    assert i != -1, f"{name} not found"
    j = REPO_SRC.find("\ndef ", i + 1)
    return REPO_SRC[i:(j if j != -1 else len(REPO_SRC))]


# ── Coherent durable dataset ─────────────────────────────────────────────────

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
LEAD_ROWS = (
    [{"campaign_name": "Global Competitors", "country": "United States",
      "status_category": "qualified", "has_gclid": True}] * 20
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
# PR-ADS-153E-B: one canonical row set now feeds BOTH the deal table and the
# source breakdown, so the source fields live on the deal rows themselves. They
# reproduce the old SRC_REVENUE truth exactly (all three deals Google Ads,
# 29,000 + 4,000) — the fixture is unified, not changed.
DEAL_ROWS = [
    {"deal_id": "d1", "company": "Acme", "country": "United States", "campaign_name": "Global Competitors",
     "deal_close_date": "2026-06-14", "deal_amount_usd": 18000.0, "deal_stage_label": "Deal Won",
     "match_status": "matched", "match_source": "gclid",
     "acquisition_group": "google_ads", "attribution_status": "attributed",
     "source_primary_raw": "Paid Search", "source_detail_raw": "google"},
    {"deal_id": "d2", "company": "Blue", "country": "United States", "campaign_name": "Global Competitors",
     "deal_close_date": "2026-05-23", "deal_amount_usd": 11000.0, "deal_stage_label": "Deal Won",
     "match_status": "matched", "match_source": "gclid",
     "acquisition_group": "google_ads", "attribution_status": "attributed",
     "source_primary_raw": "Paid Search", "source_detail_raw": "google"},
    {"deal_id": "d4", "company": "Ghost", "country": None, "campaign_name": None,
     "deal_close_date": "2026-06-19", "deal_amount_usd": 4000.0, "deal_stage_label": "won",
     "match_status": "unknown", "match_source": None,
     "acquisition_group": "google_ads", "attribution_status": "attributed",
     "source_primary_raw": "Paid Search", "source_detail_raw": "google"},
]
SRC_LEADS = (
    [{"acquisition_group": "google_ads", "status_category": "qualified",
      "source_primary_raw": "google", "source_detail_raw": "cpc"}] * 23
    + [{"acquisition_group": "organic", "status_category": "qualified",
        "source_primary_raw": "google", "source_detail_raw": "organic"}] * 5
)
SRC_REVENUE = [
    {"acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 29000.0, "source_primary_raw": "google", "source_detail_raw": "cpc"},
    {"acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 4000.0, "source_primary_raw": "google", "source_detail_raw": "cpc"},
]
SQL_LEADS = {"available": True, "rows": [
    {"contact_id": "c1", "company": "Acme", "campaign_name": "Global Competitors",
     "country": "United States", "sql_date": date(2026, 6, 1), "has_gclid": True, "has_closed_won_deal": True},
    {"contact_id": "c2", "company": "Pending Co", "campaign_name": "Global Competitors",
     "country": "United States", "sql_date": date(2026, 6, 10), "has_gclid": True, "has_closed_won_deal": False},
    {"contact_id": "c3", "company": "Old Lead", "campaign_name": "Brand - UK",
     "country": "United Kingdom", "sql_date": date(2026, 1, 5), "has_gclid": True, "has_closed_won_deal": False},
    # A qualified paid-search SQL WITH a campaign but no gclid → attribution review
    # (the reachable case; the fetcher requires a non-null campaign to reconcile with
    # the canonical SQL count, so gclid presence is the attribution signal).
    {"contact_id": "c7", "company": "No Attr", "campaign_name": "Brand - UK",
     "country": "United Kingdom", "sql_date": date(2026, 6, 15), "has_gclid": False, "has_closed_won_deal": False},
]}


def _patch_durable(monkeypatch, *, deals=None, sql_leads=None, revenue_connected=True,
                   ledger_available=True):
    import db.revenue_repository as repo

    dl = deals if deals is not None else DEAL_ROWS
    sl = sql_leads if sql_leads is not None else SQL_LEADS
    ledger = ({"available": True, "rows": list(dl)} if ledger_available
              else {"available": False, "rows": []})

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
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e, *_a, **_k: dict(CANONICAL))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total",
                        lambda s, e, *_a, **_k: {"available": True, "has_rows": True, "total_spend": 10000.0,
                                      "total_cost_micros": 10000_000_000, "currency_code": "GBP", "customer_id": "111"})
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country",
                        lambda s, e: {"available": True, "has_rows": False, "rows": [], "total_spend": 10000.0,
                                      "total_spend_usd": 13000.0, "fx_complete": True, "currency_code": "GBP",
                                      "customer_id": "111", "reporting_currency": "USD"})
    monkeypatch.setattr(repo, "fetch_spend_coverage",
                        lambda s, e, *_a, **_k: {"available": True, "chunks": [{"chunk_start": "2026-01-01", "chunk_end": "2026-12-31", "status": "verified"}]})
    monkeypatch.setattr(repo, "fetch_campaign_identity", lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: revenue_connected)
    # PR-ADS-153E-B: the closed-won population is the canonical deal ledger.
    patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(dl),
                           available=ledger_available)
    monkeypatch.setattr(repo, "fetch_source_leads", lambda s, e: {"available": True, "rows": list(SRC_LEADS)})
    monkeypatch.setattr(repo, "fetch_source_revenue", lambda s, e: {"available": True, "rows": list(SRC_REVENUE)})
    monkeypatch.setattr(repo, "fetch_sql_lead_details", lambda s, e: dict(sl) if sl is not None else {"available": False, "rows": []})
    import db.writers as db_writers
    monkeypatch.setattr(db_writers, "source_attribution_health_counts",
                        lambda: {"classified_contacts": 28, "attributed_deals": 3, "ambiguous_deals": 0, "unclassified_deals": 0})


def _deals(monkeypatch, window=WINDOW, **kw):
    _patch_durable(monkeypatch, **kw)
    from services.dashboard_deals_service import build_dashboard_deals
    return build_dashboard_deals(window, now=NOW)


# ══════════════════ 1. Endpoint + windows + auth ════════════════════════════


def test_endpoint_registered():
    assert '@app.get("/api/dashboard/deals")' in SERVER
    assert "build_dashboard_deals" in SERVER
    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/dashboard/deals" in routes


def test_all_business_windows_accepted(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_deals_service import build_dashboard_deals
    for key in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert build_dashboard_deals(key, now=NOW)["window"]["key"] == key


def test_invalid_window_raises(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_deals_service import build_dashboard_deals
    for bad in ("30d", "60d", "7d", "14d", "", "quarter"):
        with pytest.raises(ValueError):
            build_dashboard_deals(bad, now=NOW)


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
    r = client.get("/api/dashboard/deals?window=ytd")
    assert r.status_code in (401, 403), r.text


def test_endpoint_accepts_business_window_rejects_ad_window(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not mint viewer cookie")
    ok = client.get("/api/dashboard/deals?window=last_quarter", cookies={"ads_session": cookie})
    assert ok.status_code == 200, ok.text
    assert ok.json()["window"]["key"] == "last_quarter"
    bad = client.get("/api/dashboard/deals?window=30d", cookies={"ads_session": cookie})
    assert bad.status_code == 400, bad.text


# ══════════════════ 2. Truth + composition ══════════════════════════════════


def test_read_only_and_conversion_value_never_used(monkeypatch):
    out = _deals(monkeypatch)
    assert out["read_only"] is True
    assert out["google_ads_conversion_value_used"] is False
    # PR-ADS-153E-B: the deal population is the canonical ledger.
    assert out["source_truth"] == "hubspot_deal_ledger"
    assert out["revenue_scope"] == "all_source"
    assert out["legacy_fallback_used"] is False


def test_kpis_closed_won_and_sqls(monkeypatch):
    out = _deals(monkeypatch)
    k = out["kpis"]
    assert k["sqls"] == 25
    assert k["closed_won_customers"] == 3
    assert k["closed_won_revenue_usd"] == 33000.0
    assert k["sql_to_customer_rate"] == round(3 / 25, 4)  # real full-funnel conversion


def test_closed_won_revenue_only_from_closed_won_deals(monkeypatch):
    out = _deals(monkeypatch)
    for d in out["deals"]:
        assert d["stage_group"] == "Closed Won"


def test_open_pipeline_is_unavailable_not_revenue(monkeypatch):
    out = _deals(monkeypatch)
    k = out["kpis"]
    assert k["open_pipeline_usd"] is None
    assert k["opportunities_created"] is None
    assert k["open_opportunities"] is None
    # The funnel's Open Pipeline stage is a null count marked Stage unavailable.
    open_stage = next(s for s in out["funnel"] if s["stage"] == "Open Pipeline")
    assert open_stage["count"] is None
    assert open_stage["amount_usd"] is None
    assert open_stage["status"] == "Stage unavailable"


def test_closed_lost_is_unavailable_not_negative_revenue(monkeypatch):
    out = _deals(monkeypatch)
    assert out["kpis"]["closed_lost_deals"] is None
    assert out["kpis"]["lost_pipeline_usd"] is None
    # No negative amounts anywhere.
    for d in out["deals"]:
        assert d["amount_usd"] is None or d["amount_usd"] >= 0


def test_opportunity_rates_unavailable(monkeypatch):
    out = _deals(monkeypatch)
    assert out["kpis"]["sql_to_opportunity_rate"] is None
    assert out["kpis"]["opportunity_to_customer_rate"] is None
    metrics = {u["metric"] for u in out["unavailable"]}
    assert "sql_to_opportunity_rate" in metrics
    assert "opportunity_to_customer_rate" in metrics


def test_pipeline_stage_data_flagged_unavailable(monkeypatch):
    out = _deals(monkeypatch)
    assert out["pipeline_stage_data_available"] is False
    assert "durable HubSpot deal ledger" in out["pipeline_stage_reason"]
    metrics = {u["metric"] for u in out["unavailable"]}
    for m in ("open_pipeline_usd", "opportunities_created", "closed_lost_deals",
              "opportunity_aging", "avg_days_to_close"):
        assert m in metrics


def test_aging_buckets_all_unavailable(monkeypatch):
    out = _deals(monkeypatch)
    assert len(out["aging_buckets"]) == 4
    for b in out["aging_buckets"]:
        assert b["open_opportunities"] is None
        assert b["open_pipeline_usd"] is None
        assert b["status"] == "Stage unavailable"


def test_no_roas_field_anywhere(monkeypatch):
    # This tab must never compute or expose a ROAS (default=str serialises dates,
    # mirroring FastAPI's jsonable_encoder).
    import json
    out = _deals(monkeypatch)
    assert "roas" not in json.dumps(out, default=str).lower()


# ══════════════════ 3. No fake $0 / 0% ═══════════════════════════════════════


def test_null_amount_stays_unavailable_not_zero(monkeypatch):
    deals = DEAL_ROWS[:2] + [dict(DEAL_ROWS[2], deal_amount_usd=None)]
    out = _deals(monkeypatch, deals=deals)
    # The canonical ledger names the DEAL; `company` (the contact's employer) is
    # not a ledger field and is explicitly Unavailable.
    ghost = next(d for d in out["deals"] if d["deal_name"] == "Ghost")
    assert ghost["amount_usd"] is None  # never a lowered $0
    assert ghost["status"] == "Amount unavailable"


def test_rate_never_fake_zero_percent():
    from services.dashboard_deals_service import _rate
    assert _rate(3, 25) == round(3 / 25, 4)
    assert _rate(0, 25) == 0.0     # a REAL measured 0% (0 customers is a true zero)
    assert _rate(3, 0) is None     # unsafe denominator → Unavailable, never 0%
    assert _rate(None, 25) is None
    assert _rate(3, None) is None


# ══════════════════ 4. Source / campaign pipeline ═══════════════════════════


def test_source_breakdown_closed_won_no_open_pipeline(monkeypatch):
    out = _deals(monkeypatch)
    by = {r["source_group"]: r for r in out["source_breakdown"]}
    assert "google_ads" in by
    g = by["google_ads"]
    assert g["sqls"] == 23
    assert g["closed_won_customers"] == 3
    assert g["closed_won_revenue_usd"] == 33000.0
    # Open pipeline is not durable → Unavailable for every source.
    for r in out["source_breakdown"]:
        assert r["open_pipeline_usd"] is None
        assert r["sql_to_opportunity_rate"] is None


def test_campaign_breakdown_reconciles(monkeypatch):
    out = _deals(monkeypatch)
    by = {r["campaign_name"]: r for r in out["campaign_breakdown"]}
    assert by["Global Competitors"]["sqls"] == 20
    assert by["Global Competitors"]["closed_won_customers"] == 2
    assert by["Global Competitors"]["closed_won_revenue_usd"] == 29000.0
    for r in out["campaign_breakdown"]:
        assert r["open_pipeline_usd"] is None


# ══════════════════ 5. SQLs not yet closed-won ══════════════════════════════


def test_sql_no_deal_preserved_and_excludes_customers(monkeypatch):
    out = _deals(monkeypatch)
    companies = [r["company"] for r in out["sql_no_deal"]]
    # Acme (has a closed-won deal) is excluded; the three pending SQLs remain.
    assert "Acme" not in companies
    assert set(companies) == {"Pending Co", "Old Lead", "No Attr"}
    assert out["sql_no_deal_total"] == 3
    assert out["kpis"]["sqls_not_closed_won"] == 3
    # An aging SQL (168 days) is flagged for review; a contact with no campaign is attribution review.
    by = {r["company"]: r for r in out["sql_no_deal"]}
    assert by["Old Lead"]["status"] == "Aging SQL — needs sales review"
    assert by["No Attr"]["status"] == "Attribution needs review"
    assert by["Pending Co"]["days_since_sql"] == 12


def test_sql_no_deal_unavailable_when_source_down(monkeypatch):
    out = _deals(monkeypatch, sql_leads={"available": False, "rows": []})
    assert out["sql_no_deal_available"] is False
    assert out["sql_no_deal"] == []
    assert out["kpis"]["sqls_not_closed_won"] is None
    assert "sql_no_deal" in {u["metric"] for u in out["unavailable"]}
    # The reason names the lead-source outage (not the disconnected case).
    reason = next(u["reason"] for u in out["unavailable"] if u["metric"] == "sql_no_deal")
    assert "lead source" in reason.lower() or "qualified-lead source" in reason.lower()


def test_sql_no_deal_unavailable_when_revenue_disconnected(monkeypatch):
    # When there are no durable closed-won deals, has_closed_won_deal is unreliable,
    # so the panel is Unavailable with a DISTINCT reason (not the lead-source outage).
    out = _deals(monkeypatch, revenue_connected=False)
    assert out["sql_no_deal_available"] is False
    assert out["sql_no_deal"] == []
    assert out["kpis"]["sqls_not_closed_won"] is None
    reason = next(u["reason"] for u in out["unavailable"] if u["metric"] == "sql_no_deal")
    assert "disconnected" in reason.lower()


def test_sql_no_gclid_is_attribution_review(monkeypatch):
    # A qualified SQL with a campaign but no gclid → "Attribution needs review"
    # (reachable in production; the null-campaign case is filtered out upstream).
    out = _deals(monkeypatch)
    by = {r["company"]: r for r in out["sql_no_deal"]}
    assert by["No Attr"]["status"] == "Attribution needs review"


def test_null_amount_withholds_aggregate_revenue(monkeypatch):
    # Regression (backend review): a closed-won deal with a null amount must NOT be
    # summed as $0 in the aggregates. The top-line KPI + the affected campaign's
    # revenue are withheld (Unavailable), consistent with the deal row, and the
    # unavailable payload flags closed_won_revenue_usd. Customers still count.
    deals = DEAL_ROWS[:2] + [dict(DEAL_ROWS[2], deal_amount_usd=None, campaign_name="Global Competitors")]
    out = _deals(monkeypatch, deals=deals)
    assert out["kpis"]["closed_won_revenue_usd"] is None  # never a lowered $0
    assert out["kpis"]["closed_won_customers"] == 3       # customers still counted
    assert "closed_won_revenue_usd" in {u["metric"] for u in out["unavailable"]}
    gc = next(r for r in out["campaign_breakdown"] if r["campaign_name"] == "Global Competitors")
    assert gc["closed_won_revenue_usd"] is None
    # All three fixture deals now carry the same campaign, and one canonical row
    # set feeds both the table and the breakdown — so the campaign holds 3.
    assert gc["closed_won_customers"] == 3  # customers survive


def test_funnel_none_count_is_unavailable_not_no_activity(monkeypatch):
    # Regression (Copilot): a withheld (None) count must read "Stage unavailable",
    # never "No activity in window". Disconnected → Closed Won customers is None.
    out = _deals(monkeypatch, revenue_connected=False)
    closed_won = next(s for s in out["funnel"] if s["stage"] == "Closed Won")
    assert closed_won["count"] is None
    assert closed_won["status"] == "Stage unavailable"


# ══════════════════ 6. Disconnected / ledger outage / period change ═════════


def test_disconnected_integration_blanks_revenue(monkeypatch):
    out = _deals(monkeypatch, revenue_connected=False)
    k = out["kpis"]
    assert k["closed_won_customers"] is None
    assert k["closed_won_revenue_usd"] is None
    assert k["sql_to_customer_rate"] is None
    assert k["sqls"] == 25  # SQLs survive
    assert out["deal_proof_available"] is False
    assert out["truth_status"]["deals"] == "blocked"
    assert out["deals"] == []


def test_deal_ledger_outage_blanks_deal_proof(monkeypatch):
    import db.revenue_repository as repo
    _patch_durable(monkeypatch)

    def deal_view(view="deal", window=WINDOW, now=None):
        from services.revenue_decision_mart import build_revenue_decision_mart as real
        return real(view=view, window=window, now=now)
    # Force the ledger status to unavailable by making the canonical read fail.
    patch_canonical_ledger(monkeypatch, [], available=False)
    from services.dashboard_deals_service import build_dashboard_deals
    out = build_dashboard_deals(WINDOW, now=NOW)
    assert out["deal_proof_available"] is False
    assert out["deals"] == []
    assert out["truth_status"]["deals"] == "unavailable"


def test_no_fake_deltas_when_previous_baseline_missing(monkeypatch):
    out = _deals(monkeypatch, window="all_time")
    pc = out["period_change"]
    assert pc["available"] is False
    assert pc["metrics"] == {}
    assert "period_change" in {u["metric"] for u in out["unavailable"]}


def test_decision_cards_shape_and_safety(monkeypatch):
    out = _deals(monkeypatch)
    types = [c["type"] for c in out["decision_cards"]]
    assert types == ["scale", "watch", "investigate", "unavailable"]
    for c in out["decision_cards"]:
        assert c["headline"] and c["body"]
        assert "$0" not in c["body"]
    unavail = next(c for c in out["decision_cards"] if c["type"] == "unavailable")
    assert "durable HubSpot deal ledger" in unavail["body"]


def test_truth_status_keys(monkeypatch):
    out = _deals(monkeypatch)
    ts = out["truth_status"]
    for key in ("sqls", "deals", "stages", "amounts", "attribution", "read_only"):
        assert key in ts
    assert ts["stages"] == "partial"  # only closed-won is durable
    assert ts["read_only"] == "ready"


# ══════════════════ 7. Read-only safety ═════════════════════════════════════


def test_new_repo_fetcher_is_read_only():
    body = _repo_fn_body("fetch_sql_lead_details").lower()
    for forbidden in ("insert", "update ", "delete", "upsert", "drop", "alter",
                      "requests.post", "requests.patch", "requests.put"):
        assert forbidden not in body, f"fetch_sql_lead_details must be read-only ('{forbidden}')"
    assert "select" in body


def test_sql_fetcher_status_filter_is_post_dedup():
    # Regression (backend review): the SQL definition must mirror fetch_lead_quality
    # (dedup across all rows, then latest-row-wins qualified) so the panel's SQL count
    # reconciles with the canonical `sqls` KPI. Status is filtered in the OUTER query,
    # never inside the dedup CTE.
    body = _repo_fn_body("fetch_sql_lead_details")
    assert "WHERE d.status_category = 'qualified'" in body
    cte = body[:body.find("SELECT d.contact_id")]
    assert "status_category = 'qualified'" not in cte


def test_no_platform_writes_in_touched_files():
    files = [
        "services/dashboard_deals_service.py",
        "db/revenue_repository.py",
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


def test_building_deals_invokes_no_write_functions(monkeypatch):
    import db.writers as db_writers
    for name in dir(db_writers):
        if name.startswith(("write_", "upsert_", "record_", "update_", "insert_")):
            attr = getattr(db_writers, name)
            if callable(attr):
                def _boom(*a, _n=name, **k):
                    raise AssertionError(f"unexpected write call: db.writers.{_n}")
                monkeypatch.setattr(db_writers, name, _boom, raising=False)
    _patch_durable(monkeypatch)
    from services.dashboard_deals_service import build_dashboard_deals
    for key in ("current_quarter", "last_quarter", "ytd", "all_time"):
        build_dashboard_deals(key, now=NOW)


# ══════════════════ 8. Frontend: Deals tab ══════════════════════════════════


def _dashboard_section():
    import re
    m = re.search(r'<section id="page-dashboard".*?</section>', HTML, re.DOTALL)
    assert m, "page-dashboard section missing"
    return m.group(0)


def test_deals_tab_enabled_overview_default():
    section = _dashboard_section()
    assert 'data-dash-tab="deals"' in section
    assert 'class="dashboard-tab is-active" data-dash-tab="overview"' in section
    import re
    ch = re.search(r'<button[^>]*data-dash-tab="deals"[^>]*>', section).group(0)
    assert "disabled" not in ch
    assert 'id="dashboard-deals-root"' in section
    # Deals is the last tab now — no disabled placeholders remain.
    assert section.count('class="dashboard-tab" disabled') == 0


def test_deals_tab_is_hash_linkable():
    assert '"deals"' in _slice(JS, "const DASHBOARD_TABS", 200)
    fn = _slice(JS, "function activateDashboardTab", 2200)
    assert "dashboard-deals-root" in fn
    assert "loadDashboardDeals" in fn


def test_deals_tab_calls_its_endpoint():
    loader = _slice(JS, "async function loadDashboardDeals", 1500)
    assert "/api/dashboard/deals?window=" in loader
    assert "getRoasBusinessWindow()" in loader
    assert "_revReqSeq.dashDeals" in loader


def test_deals_renders_all_panels():
    block = _block()
    for fn in ("renderDealKpiRow", "renderDealFunnel", "renderDealAging",
               "renderDealSourcePipeline", "renderDealCampaignPipeline",
               "renderDealTable", "renderDealSqlNoDeal", "renderDealTruthFooter",
               "wireDealDrawers"):
        assert fn in block, f"deals tab should define {fn}"
    assert "renderDashDecisionCards(" in block
    assert "renderDashSkeleton()" in block


def test_open_pipeline_labeled_not_revenue():
    block = _block()
    assert "not revenue" in block.lower()


def test_no_roas_rendered_on_this_tab():
    block = _block()
    # No ROAS VALUE is ever formatted on this tab (fmtRoasMultiple is never called).
    assert "fmtRoasMultiple" not in block


def test_deals_reuses_prior_design_system():
    block = _block()
    for cls in ("dash-kpi-card", "dash-panel", "dash-truth-footer"):
        assert cls in block
    for lib in ("chart.js", "Chart(", "d3.", "highcharts", "recharts", "echarts"):
        assert lib not in JS


def test_drawer_has_proof_fields():
    start = JS.find("const DEAL_DRAWER_FIELDS")
    assert start != -1
    block = JS[start:start + 900]
    for label in ("Company", "Company ID", "Main Contact", "Contact ID", "Deal",
                  "Deal ID", "Amount", "Stage", "Created date", "Close date",
                  "Campaign / source", "Country", "Attribution"):
        assert label in block
    fn = _slice(JS, "function dealDrawerCell", 400)
    assert "dashValue(deal.amount_usd, fmtMoney)" in fn  # null → Unavailable, never $0


def test_deals_block_has_no_undefined_or_null_leaks():
    block = _block()
    assert ">undefined<" not in block
    assert ">null<" not in block
    assert ">NaN<" not in block


def test_deals_admin_gated_links_respect_role():
    footer = _slice(JS, "function renderDealTruthFooter", 1200)
    assert "dashCanNavigate(" in footer
    drawer = _slice(JS, "function wireDealDrawers", 2600)
    assert "dashCanNavigate(" in drawer


def test_deals_responsive_classes_exist():
    for cls in (".deal-kpi-grid", ".deal-funnel", ".deal-scroll", ".deal-table",
                ".deal-drawer", ".deal-aging-list", ".dash-tab-intro"):
        assert cls in CSS, f"{cls} missing from styles.css"
    assert "@media (max-width: 1100px)" in CSS
    assert "@media (max-width: 640px)" in CSS


def test_deals_error_and_loading_states_exist():
    assert "Deals &amp; Pipeline unavailable" in JS
    loader = _slice(JS, "async function loadDashboardDeals", 1500)
    assert "renderDashSkeleton()" in loader
    assert "is-refreshing" in loader
