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

PR-ADS-178B (docs/61): the tab republishes the Pipeline builder
(``services.pipeline_service.build_pipeline``) — no composition of its own. Its
KPIs are ``{value, status, reason}``; the always-Unavailable opportunity /
open-pipeline / aging / rate panels, the decision cards, the campaign
breakdown and the legacy "Qualified (lead status)" backlog (owner decision 4)
are deleted, and the ledger's closed-won-only scope is stated once in the
truth footer. Each case below keeps its property on that contract; the per-test
reasons are in docs/61 §7.2.
"""

import os
import sys
from datetime import date, datetime, timezone

import pytest

from tests.canonical_ledger_fixtures import (
    make_canonical_unavailable,  # noqa: E402
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

# PR-ADS-178B: the tab's block (its loader and renderer); the panels it draws
# are the Pipeline page's renderers (pipelineKpiCardsHtml, …).
_BLOCK_START = "// ── Dashboard — Deals tab (PR-ADS-139; one builder since PR-ADS-178B)"
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
    # PR-ADS-161E: contact c1 (Acme's SQL) is this won deal's associated
    # contact — the canonical deal→contact fact the legacy fixture expressed as
    # `has_closed_won_deal` on the SQL row.
    {"deal_id": "d1", "contact_id": "c1", "company": "Acme", "country": "United States", "campaign_name": "Global Competitors",
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
    # PR-ADS-177A: the mart's leads / SQLs are the acquisition cohort — the
    # SAME contacts, served from the canonical contact funnel (created in the
    # window; healthy sync state assessed by the real freshness rule).
    from tests.cohort_fixture import contacts_from_lead_rows, patch_cohort
    patch_cohort(monkeypatch, contacts_from_lead_rows(
        LEAD_ROWS, created=datetime(2026, 5, 15, 12, tzinfo=timezone.utc)), as_of=NOW)
    monkeypatch.setattr(repo, "fetch_won_revenue",
                        lambda s, e: {"available": True, "rows": list(WON_ROWS),
                                      "coverage_start": "2026-01-01", "coverage_end": "2026-06-22"})
    monkeypatch.setattr(repo, "fetch_sync_state", lambda: {"available": True, "datasets": {}})
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e, *_a, **_k: dict(CANONICAL))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total",
                        lambda s, e, *_a, **_k: {"available": True, "has_rows": True, "total_spend": 10000.0,
                                      "total_cost_micros": 10000_000_000, "currency_code": "GBP", "customer_id": "111"})
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country",
                        lambda s, e, *_a, **_k: {"available": True, "has_rows": False, "rows": [], "total_spend": 10000.0,
                                      "total_spend_usd": 13000.0, "fx_complete": True, "currency_code": "GBP",
                                      "customer_id": "111", "reporting_currency": "USD"})
    monkeypatch.setattr(repo, "fetch_spend_coverage",
                        lambda s, e, *_a, **_k: {"available": True, "chunks": [{"chunk_start": "2026-01-01", "chunk_end": "2026-12-31", "status": "verified"}]})
    monkeypatch.setattr(repo, "fetch_campaign_identity", lambda cid=None: {"available": True, "mappings": []})
    # PR-ADS-153E-B: the closed-won population is the canonical deal ledger.
    patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(dl),
                           available=ledger_available)
    # PR-ADS-161E: "not connected" is now the canonical population being
    # unavailable — the legacy gclid_attribution gate is gone.
    if not revenue_connected:
        make_canonical_unavailable(monkeypatch)
    monkeypatch.setattr(repo, "fetch_source_leads", lambda s, e: {"available": True, "rows": list(SRC_LEADS)})
    monkeypatch.setattr(repo, "fetch_source_revenue", lambda s, e: {"available": True, "rows": list(SRC_REVENUE)})
    # PR-ADS-178B: `fetch_sql_lead_details` is deleted with the lead-status
    # backlog it fed (owner decision 4); nothing reads it, so nothing is patched.
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
    # PR-ADS-178B: every KPI is {value, status, reason} (the Pipeline builder).
    assert k["sqls"]["value"] == 25
    assert k["closed_won_deals"]["value"] == 3
    assert k["won_revenue_usd"]["value"] == 33000.0
    # PR-ADS-161E: retired. 3 / 25 divided all-source won deals (close date) by
    # campaign-attributable SQLs (contact creation) — two populations, two
    # cohorts — so it is unavailable with the reason, never a conversion.
    rate = out["sql_to_customer_rate"]
    assert rate["value"] is None and rate["status"] == "unavailable"
    assert rate["reason"] == (
        "sql_to_customer_rate_needs_one_acquisition_cohort_and_scope")


def test_closed_won_revenue_only_from_closed_won_deals(monkeypatch):
    out = _deals(monkeypatch)
    # PR-ADS-178B: the list is exactly the canonical closed-won population
    # (the ledger rows' won stage), shown only beside its published count.
    assert sorted(d["deal_id"] for d in out["deals"]) == ["d1", "d2", "d4"]
    assert out["closed_won_population"] == {"value": 3, "status": "published", "reason": None}
    assert {d["deal_stage_label"] for d in out["deals"]} == {"Deal Won", "won"}


def test_open_pipeline_is_unavailable_not_revenue(monkeypatch):
    # PR-ADS-178B: the always-Unavailable open-pipeline / opportunity figures
    # are gone (brief: one footer line instead). Still no open-pipeline figure
    # anywhere, and the page says why once.
    import json
    out = _deals(monkeypatch)
    text = json.dumps(out, default=str)
    for key in ("open_pipeline_usd", "opportunities_created", "open_opportunities"):
        assert f'"{key}"' not in text, key
    assert "Open and lost deals are not in the deal ledger" in out["truth_footer"]


def test_closed_lost_is_unavailable_not_negative_revenue(monkeypatch):
    import json
    out = _deals(monkeypatch)
    text = json.dumps(out, default=str)
    assert '"closed_lost_deals"' not in text and '"lost_pipeline_usd"' not in text
    assert "lost deals" in out["truth_footer"]
    # No negative amounts anywhere.
    for d in out["deals"]:
        assert d["deal_amount_usd"] is None or d["deal_amount_usd"] >= 0


def test_opportunity_rates_unavailable(monkeypatch):
    # PR-ADS-178B: no opportunity rate is published anywhere (the ledger holds
    # no opportunity); the one rate the page names is unavailable with why.
    import json
    out = _deals(monkeypatch)
    text = json.dumps(out, default=str)
    assert '"sql_to_opportunity_rate"' not in text
    assert '"opportunity_to_customer_rate"' not in text
    assert out["sql_to_customer_rate"]["status"] == "unavailable"


def test_pipeline_stage_data_flagged_unavailable(monkeypatch):
    # PR-ADS-178B: the closed-won-only scope is stated once, in the footer.
    out = _deals(monkeypatch)
    assert out["truth_footer"] == (
        "Open and lost deals are not in the deal ledger: only closed-won deals are "
        "synced, so open pipeline, lost deals and opportunity aging are not shown here.")


def test_aging_buckets_all_unavailable(monkeypatch):
    # PR-ADS-178B: the aging panel (four always-Unavailable buckets) is
    # dropped; nothing ages a deal the ledger does not hold.
    out = _deals(monkeypatch)
    assert "aging_buckets" not in out
    assert "opportunity aging" in out["truth_footer"]


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
    # The canonical ledger names the DEAL; the contact's employer is not a
    # ledger field and is not on the row (PR-ADS-178B: dropped, always null).
    ghost = next(d for d in out["deals"] if d["deal_id"] == "d4")
    assert ghost["deal_amount_usd"] is None  # never a lowered $0
    assert "company" not in ghost
    # The page says why, never "$0" (dealAmountHtml).
    assert "no proven USD amount" in JS[JS.find("function dealAmountHtml"):][:400]


def test_rate_never_fake_zero_percent():
    # PR-ADS-178B: the tab computes no rate at all (its `_rate` is deleted with
    # the always-Unavailable rate cards); the one it names is unavailable.
    import services.dashboard_deals_service as svc
    assert not hasattr(svc, "_rate")
    src = open(os.path.join(ROOT, "services", "pipeline_service.py"), encoding="utf-8").read()
    assert "cwt.incompatible_rate(cwt.R_SQL_TO_CUSTOMER_INCOMPATIBLE)" in src


# ══════════════════ 4. Source / campaign pipeline ═══════════════════════════


def test_source_breakdown_closed_won_no_open_pipeline(monkeypatch):
    # PR-ADS-178B: the breakdown is the deal list by each deal's OWN source
    # group, through the reader contract; owner decision 4 drops its legacy
    # lead-status ("Qualified (lead status)") column.
    out = _deals(monkeypatch)
    by = {r["source_group"]: r for r in out["source_breakdown"]}
    assert "google_ads" in by
    g = by["google_ads"]
    assert "sqls" not in g
    assert g["closed_won_deals"] == 3
    assert g["won_revenue"] == 33000.0
    # Open pipeline is not durable → no such field on any source.
    for r in out["source_breakdown"]:
        assert "open_pipeline_usd" not in r
        assert "sql_to_opportunity_rate" not in r


def test_campaign_breakdown_reconciles(monkeypatch):
    # PR-ADS-178B: the tab's "Campaign → Pipeline" breakdown read the mart's
    # through-today campaign view — a second build of figures the Campaigns
    # page owns. It is deleted; every deal row still carries its campaign label.
    out = _deals(monkeypatch, deals=DEAL_ROWS[:2])
    assert "campaign_breakdown" not in out
    assert {d["campaign_name"] for d in out["deals"]} == {"Global Competitors"}


# ══════════════════ 5. SQLs not yet closed-won ══════════════════════════════


def test_sql_no_deal_preserved_and_excludes_customers(monkeypatch):
    # PR-ADS-178B, owner decision 4: the legacy "Qualified (lead status) · no
    # won deal" backlog is dropped and NOT replaced by "cohort SQLs with no won
    # deal" (that would mix two cohorts). This case and the three that pinned
    # the backlog's own states (source down, revenue disconnected, no GCLID)
    # are replaced by: no backlog, no reader, no key.
    import json
    import db.revenue_repository as repo
    out = _deals(monkeypatch)
    assert not hasattr(repo, "fetch_sql_lead_details")
    text = json.dumps(out, default=str)
    for gone in ("sql_no_deal", "lead_status_backlog", "lead_status_qualified_no_won_deal",
                 "Qualified (lead status)"):
        assert gone not in text, gone


def test_null_amount_withholds_aggregate_revenue(monkeypatch):
    # Regression (backend review): a closed-won deal with a null amount must NOT be
    # summed as $0 in the aggregates. The top-line KPI + the affected campaign's
    # revenue are withheld (Unavailable), consistent with the deal row, and the
    # unavailable payload flags closed_won_revenue_usd. Customers still count.
    deals = DEAL_ROWS[:2] + [dict(DEAL_ROWS[2], deal_amount_usd=None, campaign_name="Global Competitors")]
    out = _deals(monkeypatch, deals=deals)
    rev = out["kpis"]["won_revenue_usd"]
    assert rev["value"] is None and rev["status"] != "published"  # never a lowered $0
    assert rev["reason"]                                           # says why
    assert out["kpis"]["closed_won_deals"]["value"] == 3          # won deals still counted
    assert out["kpis"]["average_deal_value_usd"]["value"] is None
    # PR-ADS-178B: the per-source part holding the unpriced deal is withheld
    # too (reader contract); its won deals survive.
    g = next(r for r in out["source_breakdown"] if r["source_group"] == "google_ads")
    assert g["won_revenue"] is None
    assert g["closed_won_deals"] == 3  # won deals survive


def test_funnel_none_count_is_unavailable_not_no_activity(monkeypatch):
    # Regression (Copilot): a withheld (None) count must never read as "no
    # activity". PR-ADS-178B: the deal funnel is gone; the deal count is a
    # {value, status, reason} that is not published, and the list is absent
    # (never [], which would say "none").
    out = _deals(monkeypatch, revenue_connected=False)
    deals = out["kpis"]["closed_won_deals"]
    assert deals["value"] is None and deals["status"] != "published" and deals["reason"]
    assert out["deals"] is None


# ══════════════════ 6. Disconnected / ledger outage / period change ═════════


def test_161e_an_unplaced_google_ads_deal_withholds_per_campaign_figures(monkeypatch):
    """PR-ADS-161E: Ghost (d4) is a Google Ads deal with no campaign. It could
    belong to any campaign, so every per-campaign deal count and revenue would
    be a lower bound shown as a total — the canonical campaign rule withholds
    them, with the reason, while the page's all-source figures still publish."""
    out = _deals(monkeypatch)
    # PR-ADS-178B: the tab publishes no per-campaign figure any more (the
    # campaign breakdown is deleted), so none can be a lower bound; the
    # all-source figures still publish.
    assert "campaign_breakdown" not in out
    assert out["kpis"]["closed_won_deals"]["value"] == 3
    assert out["kpis"]["won_revenue_usd"]["value"] == 33000.0


def test_disconnected_integration_blanks_revenue(monkeypatch):
    out = _deals(monkeypatch, revenue_connected=False)
    k = out["kpis"]
    for key in ("closed_won_deals", "customers", "won_revenue_usd", "average_deal_value_usd"):
        assert k[key]["value"] is None and k[key]["status"] != "published", key
    assert out["sql_to_customer_rate"]["value"] is None
    assert k["sqls"]["value"] == 25  # SQLs survive
    # PR-ADS-178B: the deal proof is absent with the population's verdict.
    assert out["deals_status"] == k["closed_won_deals"]["status"]
    assert out["deals"] is None


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
    # PR-ADS-178B: an unreadable ledger is UNAVAILABLE (an outage), with no list.
    assert out["deals"] is None
    assert out["deals_status"] == "unavailable"
    assert out["kpis"]["closed_won_deals"]["status"] == "unavailable"


def test_no_fake_deltas_when_previous_baseline_missing(monkeypatch):
    out = _deals(monkeypatch, window="all_time")
    pc = out["period_change"]
    assert pc["available"] is False
    assert pc["metrics"] == {}
    # PR-ADS-178B: the reason is on the block itself (the `unavailable` list
    # went with the always-Unavailable cards it mostly described).
    assert pc["reason"] == "All Time has no previous period to compare against."


def test_decision_cards_shape_and_safety(monkeypatch):
    # PR-ADS-178B: the decision cards are dropped (brief: "the legacy decision
    # cards"). Their Watch / Investigate cards read the legacy lead-status
    # population; Scale read the source groups of a second builder. The tab
    # renders none and its payload carries none.
    out = _deals(monkeypatch)
    assert "decision_cards" not in out
    assert "renderDashDecisionCards(" not in _block()


def test_truth_status_keys(monkeypatch):
    # PR-ADS-178B: one truth-footer line replaces the status chips; read-only
    # is still stated, by the payload and the footer chip.
    out = _deals(monkeypatch)
    assert out["read_only"] is True
    assert out["truth_footer"].startswith("Open and lost deals are not in the deal ledger")
    footer = JS[JS.find("function pipelineTruthFooterHtml"):][:600]
    assert "Read-only" in footer and "d.truth_footer" in footer


# ══════════════════ 7. Read-only safety ═════════════════════════════════════


def test_new_repo_fetcher_is_read_only():
    # PR-ADS-178B: the fetcher is deleted with the backlog it fed (decision 4).
    assert "def fetch_sql_lead_details" not in REPO_SRC


def test_sql_fetcher_status_filter_is_post_dedup():
    # PR-ADS-178B: removed with the fetcher; no production module calls it.
    for rel in ("services/dashboard_deals_service.py", "services/pipeline_service.py",
                "db/revenue_repository.py", "api/server.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
        assert "fetch_sql_lead_details(" not in code, rel


def test_no_platform_writes_in_touched_files():
    files = [
        "services/dashboard_deals_service.py",
        # PR-ADS-178B: the builder the tab republishes.
        "services/pipeline_service.py",
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
    # PR-ADS-178B: the tab draws the Pipeline page's own renderers.
    block = _block()
    for fn in ("pipelineKpiCardsHtml", "pipelineSourceBreakdownHtml", "pipelineDealsHtml",
               "pipelineCustomersWonHtml", "pipelineTruthFooterHtml", "pipelineWireDeals"):
        assert fn in block, f"deals tab should render {fn}"
    assert "renderDashSkeleton()" in block


def test_open_pipeline_labeled_not_revenue():
    # PR-ADS-178B: no open-pipeline figure is drawn; the tab says open and
    # lost deals are not in the ledger.
    block = _block()
    assert "Open and lost deals are not in the deal ledger" in block


def test_no_roas_rendered_on_this_tab():
    block = _block()
    # No ROAS VALUE is ever formatted on this tab (fmtRoasMultiple is never called).
    assert "fmtRoasMultiple" not in block


def test_deals_reuses_prior_design_system():
    # PR-ADS-178B: the tab plus the Pipeline renderers it draws.
    block = _block() + JS[JS.find("function pipelineKpiCardsHtml"):
                          JS.find("// ── The leads list (section 4)")]
    for cls in ("dash-kpi-card", "dash-panel", "dash-truth-footer"):
        assert cls in block
    for lib in ("chart.js", "Chart(", "d3.", "highcharts", "recharts", "echarts"):
        assert lib not in JS


def test_drawer_has_proof_fields():
    # PR-ADS-178B: the drawer drops the always-null fields (company, company
    # id, main contact, created date, age, days to close) and keeps every field
    # the ledger carries for a deal.
    start = JS.find("const PIPELINE_DEAL_DRAWER_FIELDS")
    assert start != -1
    block = JS[start:start + 600]
    for label in ("Deal", "HubSpot deal ID", "Won revenue (USD)", "Stage", "Close date",
                  "Campaign label", "Country", "Attribution", "Source group"):
        assert label in block
    for gone in ("Company", "Main Contact", "Created date", "Age (days)", "Days to close"):
        assert gone not in block
    fn = JS[JS.find("function pipelineDealDrawerCell"):][:500]
    assert "dealAmountHtml(r)" in fn  # null → "no proven USD amount", never $0


def test_deals_block_has_no_undefined_or_null_leaks():
    block = _block()
    assert ">undefined<" not in block
    assert ">null<" not in block
    assert ">NaN<" not in block


def test_deals_admin_gated_links_respect_role():
    # PR-ADS-178B: the tab's one link (to the Pipeline page) is gated like
    # every Dashboard link; the footer and the drawer carry no link.
    block = _block()
    assert 'dashCanNavigate("pipeline")' in block
    drawer = JS[JS.find("function pipelineDealDrawerHtml"):]
    assert "href" not in drawer[:drawer.find("\n}\n")]


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
