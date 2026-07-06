"""
PR-ADS-136 — Dashboard Channels & Platforms.

Proves the third Dashboard tab (Dashboard → Channels) and its read-only contract:

  - GET /api/dashboard/channels exists, is auth-gated, read-only, and uses
    BUSINESS windows; ad windows (30d) are a 400, not a coerced fallback.
  - It re-presents the PR-ADS-133 source taxonomy in executive channel/platform
    language via the SHARED classifier — it adds no new taxonomy and no new
    business math. Revenue is HubSpot closed-won only; the Google Ads conversion
    value is never used.
  - Only Google Ads / Paid Search is spend-connected and ROAS-eligible. Paid
    Social, Organic, Referrals, Offline and Unclassified are revenue/SQL/customer
    attribution only: spend_usd is null and roas is null with a "no connected
    spend source" status — never a fabricated Meta/LinkedIn/Organic spend/ROAS.
  - No fake $0 / 0.00x / 0%: an unknown deal amount makes that channel's /
    platform's revenue (and any ROAS) Unavailable, never a lowered $0; a
    disconnected revenue integration blanks per-channel revenue and the trend;
    an incomplete Google Ads FX/coverage makes spend + ROAS Unavailable.
  - Unclassified / Needs Review revenue is preserved — never dropped, never
    forced into Organic or Google Ads.
  - The channel trend buckets SQLs by contact-created date and customers/revenue
    by close date; a bucket with an unknown amount reports null revenue.
  - Frontend: the Channels tab exists and activates (Overview stays default), is
    hash-linkable, calls /api/dashboard/channels, reuses the PR-134/135 KPI /
    chart / decision-card / truth-footer system, never leaks undefined/null/NaN,
    and never renders a ROAS number on a non-Google row.
  - Read-only: no platform-write verbs in the touched files; the new repository
    fetchers are read-only; building the channels contract calls no db.writers
    write function.

Patches the durable repository boundary (db.revenue_repository) with one
coherent dataset — no real database, no network.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SERVICE = open(
    os.path.join(ROOT, "services", "dashboard_channels_service.py"), encoding="utf-8"
).read()
REPO_SRC = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
WINDOW = "ytd"


def _slice(hay, marker, span=3200):
    i = hay.find(marker)
    assert i != -1, f"{marker} not found"
    return hay[i:i + span]


def _repo_fn_body(name):
    i = REPO_SRC.find(f"def {name}")
    assert i != -1, f"{name} not found"
    j = REPO_SRC.find("\ndef ", i + 1)
    return REPO_SRC[i:(j if j != -1 else len(REPO_SRC))]


# ── Coherent durable dataset (mart: GBP 10,000 / USD 13,000; 4 customers / 42k) ──

CANONICAL = {
    "available": True, "customer_id": "111", "currency_code": "GBP",
    "reporting_currency": "USD", "fx_complete": True, "fx_missing_days": 0,
    "campaign_count": 2, "total_spend": 10000.0, "total_spend_usd": 13000.0,
    "rows": [
        {"campaign_name": "Global Competitors", "spend": 6000.0, "spend_usd": 8000.0},
        {"campaign_name": "Brand - UK", "spend": 4000.0, "spend_usd": 5000.0},
    ],
}
GEO_ROWS = [
    {"campaign_name": "Global Competitors", "country": "United States", "spend": 6000.0},
    {"campaign_name": "Brand - UK", "country": "United Kingdom", "spend": 4000.0},
]
GEO_CANONICAL_TOTAL = {
    "available": True, "has_rows": True, "total_cost_micros": 10000_000_000,
    "total_spend": 10000.0, "rows_counted": 2, "country_count": 2,
    "currency_code": "GBP", "customer_id": "111",
}
GEO_BY_COUNTRY = {
    "available": True, "has_rows": True, "total_spend": 10000.0,
    "total_spend_usd": 13000.0, "fx_complete": True, "currency_code": "GBP",
    "customer_id": "111", "reporting_currency": "USD",
    "rows": [
        {"country_criterion_id": "2840", "country_code": "US", "country_name": "United States",
         "cost_micros": 6000_000_000, "spend": 6000.0, "spend_usd": 8000.0, "fx_complete": True},
        {"country_criterion_id": "2826", "country_code": "GB", "country_name": "United Kingdom",
         "cost_micros": 4000_000_000, "spend": 4000.0, "spend_usd": 5000.0, "fx_complete": True},
    ],
}
COVERAGE_COMPLETE = [
    {"chunk_start": "2026-01-01", "chunk_end": "2026-12-31", "status": "verified"},
]
LEAD_ROWS = (
    [{"campaign_name": "Global Competitors", "country": "United States",
      "status_category": "qualified", "has_gclid": True}] * 20
    + [{"campaign_name": "Global Competitors", "country": "United States",
        "status_category": "lead", "has_gclid": True}] * 80
)
WON_ROWS = [
    {"campaign_name": "Global Competitors", "country": "United States",
     "deal_id": "d1", "deal_amount_usd": 18000.0, "match_status": "matched"},
    {"campaign_name": "Global Competitors", "country": "United States",
     "deal_id": "d2", "deal_amount_usd": 11000.0, "match_status": "matched"},
    {"campaign_name": "Brand - UK", "country": "United Kingdom",
     "deal_id": "d3", "deal_amount_usd": 9000.0, "match_status": "matched"},
    {"campaign_name": None, "country": None,
     "deal_id": "d4", "deal_amount_usd": 4000.0, "match_status": "unknown"},
]
DEAL_ROWS = [
    {"deal_id": "d1", "company": "Acme", "country": "United States",
     "campaign_name": "Global Competitors", "deal_close_date": "2026-06-14",
     "deal_amount_usd": 18000.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
]

# ── Source classification rows (correct PR-133 vocabulary) ───────────────────
# Google Ads (spend-connected) · Paid Social / LinkedIn · Organic Social /
# LinkedIn (SQLs only) · one unattributed deal (Unclassified). Revenue sums to
# 42,000 across 4 customers, matching the mart.
SOURCE_LEAD_ROWS = (
    [{"acquisition_group": "google_ads", "status_category": "qualified",
      "source_primary_raw": "Paid Search", "source_detail_raw": "google"}] * 20
    + [{"acquisition_group": "other_paid", "status_category": "qualified",
        "source_primary_raw": "Paid Social", "source_detail_raw": "LinkedIn"}] * 6
    + [{"acquisition_group": "organic", "status_category": "qualified",
        "source_primary_raw": "Organic Social", "source_detail_raw": "LinkedIn"}] * 8
    + [{"acquisition_group": "google_ads", "status_category": "lead",
        "source_primary_raw": "Paid Search", "source_detail_raw": "google"}] * 40
)
SOURCE_REV_ROWS = [
    {"acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 18000.0, "source_primary_raw": "Paid Search", "source_detail_raw": "google"},
    {"acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 11000.0, "source_primary_raw": "Paid Search", "source_detail_raw": "google"},
    {"acquisition_group": "other_paid", "attribution_status": "attributed",
     "deal_amount_usd": 9000.0, "source_primary_raw": "Paid Social", "source_detail_raw": "LinkedIn"},
    {"acquisition_group": None, "attribution_status": "unattributed",
     "deal_amount_usd": 4000.0, "source_primary_raw": None, "source_detail_raw": None},
]
SOURCE_LEAD_DAILY = (
    [{"event_date": "2026-05-10", "acquisition_group": "google_ads", "status_category": "qualified",
      "source_primary_raw": "Paid Search", "source_detail_raw": "google"}] * 20
    + [{"event_date": "2026-05-10", "acquisition_group": "other_paid", "status_category": "qualified",
        "source_primary_raw": "Paid Social", "source_detail_raw": "LinkedIn"}] * 6
    + [{"event_date": "2026-04-10", "acquisition_group": "organic", "status_category": "qualified",
        "source_primary_raw": "Organic Social", "source_detail_raw": "LinkedIn"}] * 8
)
SOURCE_REV_DAILY = [
    {"close_date": "2026-06-14", "acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 18000.0, "source_primary_raw": "Paid Search", "source_detail_raw": "google"},
    {"close_date": "2026-05-23", "acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 11000.0, "source_primary_raw": "Paid Search", "source_detail_raw": "google"},
    {"close_date": "2026-04-23", "acquisition_group": "other_paid", "attribution_status": "attributed",
     "deal_amount_usd": 9000.0, "source_primary_raw": "Paid Social", "source_detail_raw": "LinkedIn"},
    {"close_date": "2026-06-19", "acquisition_group": None, "attribution_status": "unattributed",
     "deal_amount_usd": 4000.0, "source_primary_raw": None, "source_detail_raw": None},
]


def _patch_durable(monkeypatch, *, canonical=None, revenue_connected=True,
                   source_rev=None, source_rev_daily=None, lead_daily=None):
    import db.revenue_repository as repo

    canon = canonical if canonical is not None else CANONICAL
    src_rev = source_rev if source_rev is not None else SOURCE_REV_ROWS
    src_rev_daily = source_rev_daily if source_rev_daily is not None else SOURCE_REV_DAILY
    ld = lead_daily if lead_daily is not None else SOURCE_LEAD_DAILY

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
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country", lambda s, e: dict(GEO_BY_COUNTRY))
    monkeypatch.setattr(repo, "fetch_spend_coverage",
                        lambda s, e: {"available": True, "chunks": list(COVERAGE_COMPLETE)})
    monkeypatch.setattr(repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: revenue_connected)
    monkeypatch.setattr(repo, "fetch_revenue_deals",
                        lambda s, e: {"available": True, "rows": list(DEAL_ROWS)})
    monkeypatch.setattr(repo, "fetch_source_leads",
                        lambda s, e: {"available": True, "rows": list(SOURCE_LEAD_ROWS)})
    monkeypatch.setattr(repo, "fetch_source_revenue",
                        lambda s, e: {"available": True, "rows": list(src_rev)})
    monkeypatch.setattr(repo, "fetch_source_leads_daily",
                        lambda s, e: {"available": True, "rows": list(ld)})
    monkeypatch.setattr(repo, "fetch_source_revenue_daily",
                        lambda s, e: {"available": True, "rows": list(src_rev_daily)})
    import db.writers as db_writers
    monkeypatch.setattr(db_writers, "source_attribution_health_counts",
                        lambda: {"classified_contacts": 34, "attributed_deals": 3,
                                 "ambiguous_deals": 0, "unclassified_deals": 1})


def _channels(monkeypatch, window=WINDOW, **kw):
    _patch_durable(monkeypatch, **kw)
    from services.dashboard_channels_service import build_dashboard_channels
    return build_dashboard_channels(window, now=NOW)


def _ch(out, channel):
    return next((c for c in out["channel_mix"] if c["channel"] == channel), None)


# ══════════════════ 1. Endpoint + windows ═══════════════════════════════════


def test_endpoint_registered():
    assert '@app.get("/api/dashboard/channels")' in SERVER
    assert "build_dashboard_channels" in SERVER
    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/dashboard/channels" in routes


def test_all_business_windows_accepted(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_channels_service import build_dashboard_channels
    for key in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert build_dashboard_channels(key, now=NOW)["window"]["key"] == key


def test_invalid_window_raises(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_channels_service import build_dashboard_channels
    for bad in ("30d", "60d", "7d", "", "quarter"):
        with pytest.raises(ValueError):
            build_dashboard_channels(bad, now=NOW)


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
    r = client.get("/api/dashboard/channels?window=ytd")
    assert r.status_code in (401, 403), r.text


def test_endpoint_accepts_business_window_rejects_ad_window(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not mint viewer cookie")
    ok = client.get("/api/dashboard/channels?window=last_quarter", cookies={"ads_session": cookie})
    assert ok.status_code == 200, ok.text
    assert ok.json()["window"]["key"] == "last_quarter"
    bad = client.get("/api/dashboard/channels?window=30d", cookies={"ads_session": cookie})
    assert bad.status_code == 400, bad.text


# ══════════════════ 2. Composition + revenue truth ══════════════════════════


def test_read_only_and_conversion_value_never_used(monkeypatch):
    out = _channels(monkeypatch)
    assert out["read_only"] is True
    assert out["google_ads_conversion_value_used"] is False
    assert out["source_truth"] == "revenue_by_source_taxonomy"
    # Service composes the shared classifier via the source service's own taxonomy
    # helpers (identical classification to the channel-mix path), never re-implements it.
    assert "_taxonomy_for_section" in SERVICE
    assert "conversion value" in SERVICE.lower()


def test_revenue_is_hubspot_closed_won(monkeypatch):
    out = _channels(monkeypatch)
    k = out["kpis"]
    assert k["closed_won_revenue_usd"] == 42000.0
    assert k["total_customers"] == 4
    # SQLs are source-based on this tab (sum of channel rows): 20 google + 6 paid
    # social + 8 organic social = 34 — reconciles with the mix/trend, not the mart.
    assert k["total_sqls"] == 34


def test_kpi_spend_connected_vs_revenue_only_split(monkeypatch):
    out = _channels(monkeypatch)
    k = out["kpis"]
    # Google Ads $29,000 spend-connected; Paid Social $9,000 + Unclassified
    # $4,000 revenue-only = $13,000.
    assert k["spend_connected_revenue_usd"] == 29000.0
    assert k["revenue_only_revenue_usd"] == 13000.0
    assert round(k["spend_connected_share"], 4) == round(29000.0 / 42000.0, 4)


def test_kpis_reconcile_with_channel_rows(monkeypatch):
    # The Channels-tab KPI totals derive from the channel rows (Revenue-by-Source),
    # so they reconcile exactly with the mix, matrix and trend — not a mart total.
    out = _channels(monkeypatch)
    k = out["kpis"]
    rows = out["channel_mix"]
    assert k["total_sqls"] == sum(c["sqls"] for c in rows)
    assert k["total_customers"] == sum(c["customers"] for c in rows)  # revenue connected
    assert k["closed_won_revenue_usd"] == round(sum(c["won_revenue_usd"] for c in rows), 2)
    # KPI SQLs also equals the trend's SQL sum (same source taxonomy).
    trend_sqls = sum(ch["sqls"] for p in out["trend"]["points"] for ch in p["channels"])
    assert k["total_sqls"] == trend_sqls


def test_kpi_revenue_unavailable_when_any_channel_amount_unknown(monkeypatch):
    # One unknown deal amount makes the KPI revenue Unavailable (never a partial
    # sum), while SQLs/customers still reconcile with the channel rows.
    src_rev = [dict(SOURCE_REV_ROWS[0], deal_amount_usd=None)] + SOURCE_REV_ROWS[1:]
    out = _channels(monkeypatch, source_rev=src_rev)
    k = out["kpis"]
    assert k["closed_won_revenue_usd"] is None
    assert k["spend_connected_revenue_usd"] is None
    assert k["revenue_only_revenue_usd"] is None
    rows = out["channel_mix"]
    assert k["total_sqls"] == sum(c["sqls"] for c in rows)
    assert k["total_customers"] == sum((c["customers"] or 0) for c in rows)


def test_channels_map_to_executive_buckets(monkeypatch):
    out = _channels(monkeypatch)
    google = _ch(out, "google_ads")
    paid_social = _ch(out, "paid_social")
    organic_social = _ch(out, "organic_social")
    unclassified = _ch(out, "unclassified")
    assert google and google["sqls"] == 20 and google["customers"] == 2
    assert google["won_revenue_usd"] == 29000.0
    assert paid_social and paid_social["customers"] == 1 and paid_social["won_revenue_usd"] == 9000.0
    assert organic_social and organic_social["sqls"] == 8 and organic_social["customers"] == 0
    assert unclassified and unclassified["customers"] == 1 and unclassified["won_revenue_usd"] == 4000.0


def test_unclassified_revenue_is_preserved(monkeypatch):
    out = _channels(monkeypatch)
    unclassified = _ch(out, "unclassified")
    assert unclassified is not None, "unattributed revenue must survive under Unclassified"
    assert unclassified["won_revenue_usd"] == 4000.0
    # Never forced into Google Ads or Organic.
    assert _ch(out, "google_ads")["won_revenue_usd"] == 29000.0


# ══════════════════ 3. Spend / ROAS truth (only Google Ads) ═════════════════


def test_only_google_ads_is_spend_connected(monkeypatch):
    out = _channels(monkeypatch)
    google = _ch(out, "google_ads")
    assert google["spend_connected"] is True
    assert google["spend_usd"] == 13000.0
    assert google["roas"] == round(29000.0 / 13000.0, 2)
    for other in ("paid_social", "organic_social", "unclassified"):
        row = _ch(out, other)
        if row is None:
            continue
        assert row["spend_connected"] is False, f"{other} must not be spend-connected"
        assert row["spend_usd"] is None, f"{other} must not show spend"
        assert row["roas"] is None, f"{other} must never show ROAS"


def test_platform_matrix_roas_only_on_google(monkeypatch):
    out = _channels(monkeypatch)
    for p in out["platform_matrix"]:
        is_google = p["channel"] == "google_ads" and p["platform"] == "google_ads"
        if is_google:
            assert p["roas"] is not None and p["spend_usd"] is not None
            assert p["spend_connected"] is True
        else:
            assert p["roas"] is None, f"{p['platform']} must never carry ROAS"
            assert p["spend_usd"] is None
            assert p["spend_connected"] is False


def test_fx_incomplete_keeps_spend_connected_via_native_gbp(monkeypatch):
    # Native GBP spend present but FX/USD unavailable: the Google Ads spend SOURCE
    # stays connected (native GBP available), USD spend + ROAS are Unavailable, and
    # the status must NOT say "no connected spend source".
    canon = dict(CANONICAL, fx_complete=False, fx_missing_days=5, total_spend_usd=None,
                 rows=[dict(r, spend_usd=None) for r in CANONICAL["rows"]])
    out = _channels(monkeypatch, canonical=canon)
    google = _ch(out, "google_ads")
    assert google["spend_usd"] is None and google["spend_usd"] != 0  # USD Unavailable
    assert google["roas"] is None and google["roas_available"] is False  # ROAS Unavailable
    assert google["spend_connected"] is True  # native GBP source still connected
    # Native GBP spend remains available and is never labelled USD.
    assert google["native_spend"] and google["native_spend"]["amount"] == 10000.0
    assert google["native_spend"]["currency"] == "GBP"
    assert "no connected spend source" not in (google["status"] or "").lower()
    # The Google Ads platform row carries the same native GBP fallback.
    gp = next(p for p in out["platform_matrix"] if p["channel"] == "google_ads")
    assert gp["spend_usd"] is None and gp["spend_connected"] is True
    assert gp["native_spend"] and gp["native_spend"]["currency"] == "GBP"
    assert out["truth_status"]["google_ads_spend"] in ("partial", "unavailable")


# ══════════════════ 4. No fake $0 / null-amount doctrine ════════════════════


def test_null_amount_makes_google_revenue_and_roas_unavailable(monkeypatch):
    # A Google-attributed deal with an unknown amount: the source service sums it
    # as $0, but the tab must report the channel's revenue (and ROAS) Unavailable
    # rather than a silently lowered figure.
    src_rev = [dict(SOURCE_REV_ROWS[0], deal_amount_usd=None)] + SOURCE_REV_ROWS[1:]
    out = _channels(monkeypatch, source_rev=src_rev)
    google = _ch(out, "google_ads")
    assert google["won_revenue_usd"] is None and google["won_revenue_usd"] != 0
    assert google["roas"] is None
    # ROAS is withheld (roas_available False) because revenue is incomplete, but
    # the Google Ads spend SOURCE is still connected and its spend still shown.
    assert google["roas_available"] is False
    assert google["spend_connected"] is True
    assert google["spend_usd"] is not None
    # The spend/revenue split cannot be completed either.
    assert out["kpis"]["spend_connected_revenue_usd"] is None
    assert out["kpis"]["revenue_only_revenue_usd"] is None


def test_null_amount_platform_revenue_unavailable(monkeypatch):
    src_rev = [dict(SOURCE_REV_ROWS[2], deal_amount_usd=None)] + [SOURCE_REV_ROWS[0], SOURCE_REV_ROWS[1], SOURCE_REV_ROWS[3]]
    out = _channels(monkeypatch, source_rev=src_rev)
    linkedin = next((p for p in out["platform_matrix"]
                     if p["channel"] == "paid_social"), None)
    assert linkedin is not None
    assert linkedin["won_revenue_usd"] is None and linkedin["won_revenue_usd"] != 0


def test_disconnected_integration_never_fabricates_zero(monkeypatch):
    out = _channels(monkeypatch, revenue_connected=False)
    k = out["kpis"]
    assert k["closed_won_revenue_usd"] is None
    assert k["total_customers"] is None
    assert k["spend_connected_revenue_usd"] is None
    assert k["revenue_only_revenue_usd"] is None
    for c in out["channel_mix"]:
        # Customers AND revenue derive from the revenue integration → both None.
        assert c["customers"] is None, f"{c['channel']} customers must be None when disconnected"
        assert c["won_revenue_usd"] is None, f"{c['channel']} revenue must be None when disconnected"
        assert c["roas"] is None
        # Leads / SQLs come from contacts and survive the revenue outage.
        assert c["sqls"] is not None
    for p in out["platform_matrix"]:
        assert p["customers"] is None and p["won_revenue_usd"] is None and p["roas"] is None
    assert out["quality_matrix"] == []  # customers + revenue unknown → withheld
    assert out["trend"]["status"] == "unavailable"
    assert out["truth_status"]["revenue"] == "blocked"


def test_non_google_spend_is_structurally_unavailable(monkeypatch):
    out = _channels(monkeypatch)
    assert out["truth_status"]["non_google_spend"] == "unavailable"
    metrics = {u["metric"] for u in out["unavailable"]}
    assert "paid_social_roas" in metrics
    assert "non_google_spend" in metrics


def test_fmt_usd_short_none_is_unavailable():
    from services.dashboard_channels_service import _fmt_usd_short
    assert _fmt_usd_short(None) == "Unavailable"
    assert _fmt_usd_short(29000) == "$29.0k"
    assert _fmt_usd_short(2_500_000) == "$2.5M"


def test_roas_available_iff_roas_is_a_real_number(monkeypatch):
    # roas_available is a strict invariant: true exactly when roas is present.
    out = _channels(monkeypatch)
    for c in out["channel_mix"]:
        assert c["roas_available"] == (c["roas"] is not None)
    for p in out["platform_matrix"]:
        assert p["roas_available"] == (p["roas"] is not None)
    # Disconnected: roas is cleared → roas_available must be cleared too (no stale
    # "connected ROAS" pair), while the Google spend source may stay connected.
    out2 = _channels(monkeypatch, revenue_connected=False)
    for row in out2["channel_mix"] + out2["platform_matrix"]:
        assert row["roas"] is None and row["roas_available"] is False


def test_unverifiable_raw_revenue_fails_closed(monkeypatch):
    # The second (unknown-amount detection) fetch_source_revenue call failing must
    # not let a possibly-understated total render as complete — fail closed.
    import db.revenue_repository as repo
    _patch_durable(monkeypatch)
    state = {"n": 0}

    def flaky(s, e):
        state["n"] += 1
        # 1st call (inside build_revenue_by_source) succeeds; 2nd (detection) fails.
        return ({"available": True, "rows": list(SOURCE_REV_ROWS)} if state["n"] == 1
                else {"available": False, "rows": []})

    monkeypatch.setattr(repo, "fetch_source_revenue", flaky)
    from services.dashboard_channels_service import build_dashboard_channels
    out = build_dashboard_channels(WINDOW, now=NOW)
    for c in out["channel_mix"]:
        assert c["won_revenue_usd"] is None, "revenue must be Unavailable when amounts unverifiable"
        assert c["roas"] is None
    for p in out["platform_matrix"]:
        assert p["won_revenue_usd"] is None and p["roas"] is None
    # Counts still survive (they don't depend on the amount).
    assert any((c["customers"] or 0) > 0 for c in out["channel_mix"])


def test_group_disagree_deal_not_force_credited_to_google(monkeypatch):
    # A deal STORED in the google_ads group but with a missing raw primary source
    # (source-unconfirmed) must route to Unclassified in BOTH the channel mix and
    # the trend — identical classification, never force-credited to Google Ads.
    ambiguous_rev = [
        {"acquisition_group": "google_ads", "attribution_status": "attributed",
         "deal_amount_usd": 50000.0, "source_primary_raw": None, "source_detail_raw": None},
    ]
    ambiguous_daily = [
        {"close_date": "2026-05-10", "acquisition_group": "google_ads",
         "attribution_status": "attributed", "deal_amount_usd": 50000.0,
         "source_primary_raw": None, "source_detail_raw": None},
    ]
    out = _channels(monkeypatch, source_rev=ambiguous_rev, source_rev_daily=ambiguous_daily)
    unclassified = _ch(out, "unclassified")
    google = _ch(out, "google_ads")
    # Channel mix: the customer + $50k land in Unclassified, not Google Ads.
    assert unclassified is not None and (unclassified["customers"] or 0) >= 1
    assert google is None or (google["customers"] or 0) == 0
    # Trend: the same close-date bucket credits Unclassified, never Google Ads.
    trend_channels = {ch["channel"] for p in out["trend"]["points"] for ch in p["channels"]}
    assert "unclassified" in trend_channels
    google_trend_cust = sum(ch["customers"] for p in out["trend"]["points"]
                            for ch in p["channels"] if ch["channel"] == "google_ads")
    assert google_trend_cust == 0


# ══════════════════ 5. Trend (per channel, own event date) ══════════════════


def test_trend_buckets_sqls_by_created_and_revenue_by_close(monkeypatch):
    out = _channels(monkeypatch)
    trend = out["trend"]
    assert trend["status"] == "ready"
    assert trend["bucket"] in ("week", "month")
    # SQLs land in the created-date buckets; revenue/customers in close-date ones.
    total_sqls = sum(ch["sqls"] for p in trend["points"] for ch in p["channels"])
    total_customers = sum(ch["customers"] for p in trend["points"] for ch in p["channels"])
    assert total_sqls == 34  # 20 google + 6 paid social + 8 organic social
    assert total_customers == 4
    win = out["window"]
    for p in trend["points"]:
        assert win["start_date"] <= p["period_start"] <= win["end_date"]


def test_trend_null_amount_reports_null_revenue_not_zero(monkeypatch):
    daily = [dict(SOURCE_REV_DAILY[0], deal_amount_usd=None)] + SOURCE_REV_DAILY[1:]
    out = _channels(monkeypatch, source_rev_daily=daily)
    trend = out["trend"]
    # The google channel's bucket for that close date has a customer but null
    # revenue — never a fabricated $0.
    null_cells = [ch for p in trend["points"] for ch in p["channels"]
                  if ch["channel"] == "google_ads" and ch["revenue_usd"] is None and ch["customers"] > 0]
    assert null_cells, "expected a google bucket with a customer and null revenue"


def test_trend_bucket_label_is_portable(monkeypatch):
    out = _channels(monkeypatch)
    import re
    labels = [p["period_label"] for p in out["trend"]["points"]]
    assert labels
    for lbl in labels:
        # Weekly "9 Feb" or monthly "Feb 2026" — never a glibc-only "%-d".
        assert re.match(r"^\d{1,2} [A-Z][a-z]{2}$", lbl) or re.match(r"^[A-Z][a-z]{2} \d{4}$", lbl), lbl


# ══════════════════ 6. Quality matrix + decision cards + footer ═════════════


def test_quality_matrix_axes(monkeypatch):
    out = _channels(monkeypatch)
    qm = out["quality_matrix"]
    assert qm, "expected quality-matrix points"
    google = next(p for p in qm if p["channel"] == "google_ads")
    assert google["sqls"] == 20 and google["customers"] == 2 and google["revenue_usd"] == 29000.0
    assert google["spend_connected"] is True
    # A demand-only channel (SQLs, no customers) is still plotted.
    organic = next(p for p in qm if p["channel"] == "organic_social")
    assert organic["sqls"] == 8 and organic["customers"] == 0


def test_decision_cards_shape_and_safety(monkeypatch):
    out = _channels(monkeypatch)
    types = [c["type"] for c in out["decision_cards"]]
    assert types == ["scale", "watch", "investigate", "unavailable"]
    for c in out["decision_cards"]:
        assert c["headline"] and c["body"]
        assert "$0" not in c["body"]


def test_decision_cards_never_fabricate_zero_for_unknown_revenue(monkeypatch):
    # Unclassified deal amount unknown: the Investigate card references it but
    # must say Unavailable, not $0.
    src_rev = SOURCE_REV_ROWS[:3] + [dict(SOURCE_REV_ROWS[3], deal_amount_usd=None)]
    out = _channels(monkeypatch, source_rev=src_rev)
    text = " ".join((c.get("headline") or "") + " " + (c.get("body") or "")
                    for c in out["decision_cards"])
    assert "$0" not in text


def test_decision_cards_degrade_when_disconnected(monkeypatch):
    out = _channels(monkeypatch, revenue_connected=False)
    types = [c["type"] for c in out["decision_cards"]]
    assert types == ["scale", "watch", "investigate", "unavailable"]
    for c in out["decision_cards"]:
        assert c["headline"] and c["body"]


def test_truth_status_keys(monkeypatch):
    out = _channels(monkeypatch)
    ts = out["truth_status"]
    for key in ("source_attribution", "revenue", "google_ads_spend",
                "non_google_spend", "platform_classification"):
        assert key in ts
    assert ts["revenue"] == "ready"
    assert ts["google_ads_spend"] == "ready"


def test_no_fake_deltas_when_previous_baseline_missing(monkeypatch):
    out = _channels(monkeypatch, window="all_time")
    pc = out["period_change"]
    assert pc["available"] is False
    assert pc["metrics"] == {}
    assert "period_change" in {u["metric"] for u in out["unavailable"]}


# ══════════════════ 7. Read-only safety ═════════════════════════════════════


def test_new_repo_fetchers_are_read_only():
    for name in ("fetch_source_leads_daily", "fetch_source_revenue_daily"):
        body = _repo_fn_body(name).lower()
        for forbidden in ("insert", "update ", "delete", "upsert", "drop", "alter",
                          "requests.post", "requests.patch", "requests.put"):
            assert forbidden not in body, f"{name} must be read-only ('{forbidden}')"


def test_no_platform_writes_in_touched_files():
    files = [
        "services/dashboard_channels_service.py",
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
                          "create_deal", "update_deal", "create_contact", "update_contact"):
            assert forbidden not in src, f"{rel} must not reference '{forbidden}'"


def test_building_channels_invokes_no_write_functions(monkeypatch):
    import db.writers as db_writers
    for name in dir(db_writers):
        if name.startswith(("write_", "upsert_", "record_", "update_", "insert_")):
            attr = getattr(db_writers, name)
            if callable(attr):
                def _boom(*a, _n=name, **k):
                    raise AssertionError(f"unexpected write call: db.writers.{_n}")
                monkeypatch.setattr(db_writers, name, _boom, raising=False)
    _patch_durable(monkeypatch)
    from services.dashboard_channels_service import build_dashboard_channels
    for key in ("current_quarter", "last_quarter", "ytd", "all_time"):
        build_dashboard_channels(key, now=NOW)


# ══════════════════ 8. Frontend: Channels tab ═══════════════════════════════


def _dashboard_section():
    import re
    m = re.search(r'<section id="page-dashboard".*?</section>', HTML, re.DOTALL)
    assert m, "page-dashboard section missing"
    return m.group(0)


def test_channels_tab_enabled_overview_default():
    section = _dashboard_section()
    assert 'data-dash-tab="channels"' in section
    assert 'data-dash-tab="overview"' in section
    # Overview keeps the active class by default; Channels is NOT disabled.
    assert 'class="dashboard-tab is-active" data-dash-tab="overview"' in section
    import re
    ch = re.search(r'<button[^>]*data-dash-tab="channels"[^>]*>', section).group(0)
    assert "disabled" not in ch
    assert 'id="dashboard-channels-root"' in section


def test_channels_tab_is_hash_linkable():
    assert "hashToDashTab" in JS
    assert '"channels"' in _slice(JS, "const DASHBOARD_TABS", 120)
    fn = _slice(JS, "function activateDashboardTab", 1400)
    assert "dashboard-channels-root" in fn
    assert "loadDashboardChannels" in fn


def test_channels_tab_calls_its_endpoint():
    loader = _slice(JS, "async function loadDashboardChannels", 1400)
    assert "/api/dashboard/channels?window=" in loader
    assert "getRoasBusinessWindow()" in loader
    assert "_revReqSeq.dashChannels" in loader


def test_channels_kpi_cards_render_five_metrics():
    fn = _slice(JS, "function renderChanKpiRow", 4000)
    for label in ("Closed-Won Revenue", "Spend-Connected Revenue", "Revenue-Only Revenue",
                  "Customers", "SQLs"):
        assert label in fn
    assert "dash-kpi-card" in fn
    assert "dashValue(" in fn


def test_channels_reuses_pr134_design_system():
    start = JS.find("// ── Dashboard — Channels & Platforms")
    end = JS.find("// ── Campaigns page")
    assert start != -1 and end > start, "channels block not found"
    block = JS[start:end]
    for cls in ("dash-kpi-card", "dash-panel", "dash-truth-footer",
                "dash-combo-chart", "dash-chart-hit"):
        assert cls in block, f"channels tab should reuse PR-134 class {cls}"
    assert "renderDashDecisionCards(" in block
    assert "renderDashSkeleton()" in block
    for lib in ("chart.js", "Chart(", "d3.", "highcharts", "recharts", "echarts"):
        assert lib not in JS


def test_channels_momentum_metric_toggle_defaults_customers():
    block = JS[JS.find("// ── Dashboard — Channels & Platforms"):JS.find("// ── Campaigns page")]
    assert '_chanMomentumMetric = "customers"' in block
    assert "chan-metric-btn" in block
    for m in ("SQLs", "Customers", "Revenue"):
        assert m in block


def test_channels_platform_matrix_never_shows_roas_on_non_google():
    block = JS[JS.find("// ── Dashboard — Channels & Platforms"):JS.find("// ── Campaigns page")]
    # ROAS is gated on roas_available (Google Ads only); non-connected rows route
    # to an Unavailable cell — never fmtRoasMultiple.
    assert "roas_available" in block
    assert "no connected spend source" in block.lower()
    fn = _slice(JS, "function chanPlatformRoasCell", 500)
    assert "roas_available" in fn
    assert "Unavailable" in fn


def test_channels_block_has_no_undefined_or_null_leaks():
    block = JS[JS.find("// ── Dashboard — Channels & Platforms"):JS.find("// ── Campaigns page")]
    assert ">undefined<" not in block
    assert ">null<" not in block
    assert ">NaN<" not in block


def test_channels_admin_gated_links_respect_role():
    footer = _slice(JS, "function renderChanTruthFooter", 1400)
    assert "dashCanNavigate(" in footer


def test_channels_responsive_classes_exist():
    for cls in (".chan-mix", ".chan-platform-scroll", ".chan-channel-row",
                ".chan-metric-toggle", ".chan-donut", ".dash-tab-intro"):
        assert cls in CSS, f"{cls} missing from styles.css"
    assert "@media (max-width: 1100px)" in CSS
    assert "@media (max-width: 640px)" in CSS


def test_channel_drawer_is_platform_breakdown_not_deal_proof():
    block = JS[JS.find("// ── Dashboard — Channels & Platforms"):JS.find("// ── Campaigns page")]
    # The drawer is explicitly a PLATFORM breakdown and disclaims client/deal proof.
    assert "platform breakdown" in block
    assert "not individual client/deal records" in block
    # The channels block never claims per-deal / per-client proof rows.
    for phrase in ("deal proof", "client proof", "deal-level proof", "deals — proof"):
        assert phrase not in block.lower()


def test_native_gbp_shown_when_usd_unavailable():
    block = JS[JS.find("// ── Dashboard — Channels & Platforms"):JS.find("// ── Campaigns page")]
    # Spend cells fall back to native GBP (fmtCompactCurrency, never $) when USD
    # is unavailable but the spend source is connected.
    assert "native_spend" in block
    assert "fmtCompactCurrency(nat.amount" in block


def test_channels_error_and_loading_states_exist():
    assert "Channels &amp; Platforms unavailable" in JS
    loader = _slice(JS, "async function loadDashboardChannels", 1400)
    assert "renderDashSkeleton()" in loader
    assert "is-refreshing" in loader
