"""
PR-ADS-134 — Executive Dashboard Overview Command Center.

Proves the new Dashboard → Overview contract and page:

  - GET /api/dashboard/overview exists, is read-only, and uses BUSINESS windows
    (current_quarter | last_quarter | last_6_months | ytd | all_time) — the old
    ad-window (7d/14d/30d/60d) dashboard thinking is gone from the dashboard.
  - An invalid window is rejected (ValueError -> HTTP 400), same as the mart.
  - KPIs come only from safe canonical sources: USD spend is the canonical
    Google Ads spend (None when FX/coverage unsafe — never native GBP relabelled
    as USD), revenue is HubSpot closed-won (never Google Ads conversion value).
  - Missing FX blocks USD spend AND ROAS: both are None plus a reason — never a
    fabricated $0 / 0.00x.
  - Non-Google sources never get ROAS (revenue-only), the offline group is
    imported-records, unclassified is needs-review.
  - Previous-period deltas never fake zero: a missing prior period or missing
    metric is status="no_comparison" with delta_pct=None, never 0%.
  - The spend trend series is emitted ONLY when the mart says the denominator is
    decision-ready; otherwise every point is None (a missing day is never $0).
  - Top signals and decision cards degrade safely on empty windows.
  - Frontend: the Executive Overview markup/renderers exist, the business-window
    selector is wired, unavailable values render "Unavailable", no undefined/null
    leaks, decision cards/signals link to evidence pages, error/loading/empty
    states exist, and the layout has responsive rules.
  - Safety: no platform-write verbs anywhere in the touched files, and building
    the overview never invokes a db.writers write function.

These tests patch the durable repository boundary (db.revenue_repository) with a
single coherent dataset — no real database, no network.
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
    os.path.join(ROOT, "services", "dashboard_overview_service.py"), encoding="utf-8"
).read()
REPO_SRC = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()

# Deterministic reference time so YTD is 2026-01-01 .. 2026-06-22 and the
# current quarter is Q2-2026 (same clock as the PR-125 mart tests).
NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
WINDOW = "ytd"


def _slice(hay, marker, span=3000):
    i = hay.find(marker)
    assert i != -1, f"{marker} not found"
    return hay[i:i + span]


def _repo_fn_body(name):
    i = REPO_SRC.find(f"def {name}")
    assert i != -1, f"{name} not found in revenue_repository.py"
    j = REPO_SRC.find("\ndef ", i + 1)
    return REPO_SRC[i:(j if j != -1 else len(REPO_SRC))]


# ── Coherent durable dataset (native GBP spend, USD reporting) ──────────────
# Mirrors the PR-125 mart dataset: GBP 10,000 native / USD 13,000, revenue 9,000.

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

# FX incomplete: USD cannot be computed safely — never a guessed USD total.
CANONICAL_FX_INCOMPLETE = {
    **CANONICAL,
    "fx_complete": False,
    "fx_missing_days": 40,
    "total_spend_usd": None,
    "rows": [
        {"campaign_name": "Brand - UK", "spend": 6000.0, "spend_usd": None},
        {"campaign_name": "Search - Global", "spend": 4000.0, "spend_usd": None},
    ],
}

GEO_ROWS = [
    {"campaign_name": "Brand - UK", "country": "United Kingdom", "spend": 6000.0},
    {"campaign_name": "Search - Global", "country": "Germany", "spend": 4000.0},
]

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

# Coverage verified for all of 2026 only — so a prior-YEAR previous period is
# legitimately untrusted (exactly the no-fake-comparison case we must pin).
COVERAGE_COMPLETE = [
    {"chunk_start": "2026-01-01", "chunk_end": "2026-12-31", "status": "verified"},
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

DEAL_ROWS = [
    {"deal_id": "d1", "company": "Acme Ltd", "country": "United Kingdom",
     "campaign_name": "Brand - UK", "deal_close_date": "2026-05-01",
     "deal_amount_usd": 9000.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
]

SOURCE_LEAD_ROWS = [
    {"acquisition_group": "google_ads", "status_category": "qualified",
     "source_primary_raw": "PAID_SEARCH", "source_detail_raw": "google"},
    {"acquisition_group": "google_ads", "status_category": "lead",
     "source_primary_raw": "PAID_SEARCH", "source_detail_raw": "google"},
    {"acquisition_group": "organic", "status_category": "lead",
     "source_primary_raw": "ORGANIC_SEARCH", "source_detail_raw": "google"},
]
SOURCE_REV_ROWS = [
    {"acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 9000.0, "source_primary_raw": "PAID_SEARCH",
     "source_detail_raw": "google"},
]

SPEND_DAILY_ROWS = [
    {"spend_date": "2026-05-04", "cost_micros": 5000_000_000,
     "spend_native": 5000.0, "spend_usd": 6500.0, "fx_complete": True},
    {"spend_date": "2026-06-01", "cost_micros": 5000_000_000,
     "spend_native": 5000.0, "spend_usd": 6500.0, "fx_complete": True},
]


def _patch_durable(monkeypatch, *, coverage=None, won_rows=None, canonical=None,
                   lead_rows=None, deal_rows=None, source_lead_rows=None,
                   source_rev_rows=None, spend_daily=None):
    """Patch the durable repository so every composed service reads one dataset."""
    import db.revenue_repository as repo

    won = won_rows if won_rows is not None else [WON_ROW_MAPPED]
    chunks = coverage if coverage is not None else COVERAGE_COMPLETE
    canon = canonical if canonical is not None else CANONICAL
    leads = lead_rows if lead_rows is not None else LEAD_ROWS
    deals = deal_rows if deal_rows is not None else DEAL_ROWS
    src_leads = source_lead_rows if source_lead_rows is not None else SOURCE_LEAD_ROWS
    src_revs = source_rev_rows if source_rev_rows is not None else SOURCE_REV_ROWS
    daily = spend_daily if spend_daily is not None else SPEND_DAILY_ROWS

    monkeypatch.setattr(repo, "fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr(
        repo, "fetch_campaign_country_spend",
        lambda s, e: {"available": True, "rows": list(GEO_ROWS),
                      "coverage_start": "2026-01-01", "coverage_end": "2026-06-22"},
    )
    monkeypatch.setattr(
        repo, "fetch_lead_quality",
        lambda s, e: {"available": True, "rows": list(leads),
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
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: dict(canon))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total",
                        lambda s, e, *_a, **_k: dict(GEO_CANONICAL_TOTAL))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country",
                        lambda s, e: dict(GEO_BY_COUNTRY))
    monkeypatch.setattr(
        repo, "fetch_spend_coverage",
        lambda s, e, *_a, **_k: {"available": True, "chunks": list(chunks)},
    )
    monkeypatch.setattr(
        repo, "fetch_campaign_identity",
        lambda cid=None: {"available": True, "mappings": []},
    )
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: True)
    # PR-ADS-153E-B: one canonical row set feeds the Overview KPIs, the trend
    # and every downstream breakdown — which is the property this suite exists to
    # check. A test overriding either legacy fixture drives that one ledger.
    patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(
        won_rows if won_rows is not None else
        (deal_rows if deal_rows is not None else DEAL_ROWS)))
    monkeypatch.setattr(
        repo, "fetch_source_leads",
        lambda s, e: {"available": True, "rows": list(src_leads)},
    )
    monkeypatch.setattr(
        repo, "fetch_source_revenue",
        lambda s, e: {"available": True, "rows": list(src_revs)},
    )
    monkeypatch.setattr(
        repo, "fetch_spend_daily_series",
        lambda s, e: {"available": True, "rows": list(daily)},
    )
    import db.writers as db_writers
    monkeypatch.setattr(
        db_writers, "source_attribution_health_counts",
        lambda: {"classified_contacts": 3, "attributed_deals": 1,
                 "ambiguous_deals": 0, "unclassified_deals": 0},
    )


def _overview(monkeypatch, window=WINDOW, **kw):
    _patch_durable(monkeypatch, **kw)
    from services.dashboard_overview_service import build_dashboard_overview
    return build_dashboard_overview(window, now=NOW)


# ══════════════════ 1. Endpoint exists, business windows, 400 ═══════════════


def test_dashboard_overview_endpoint_registered():
    assert '@app.get("/api/dashboard/overview")' in SERVER
    assert "build_dashboard_overview" in SERVER
    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/dashboard/overview" in routes


def test_all_business_windows_accepted(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_overview_service import build_dashboard_overview
    for key in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        out = build_dashboard_overview(key, now=NOW)
        assert out["window"]["key"] == key


def test_invalid_window_raises_value_error(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_overview_service import build_dashboard_overview
    for bad in ("60d", "30d", "", "quarter"):
        with pytest.raises(ValueError):
            build_dashboard_overview(bad, now=NOW)


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


def test_endpoint_accepts_business_window_and_rejects_ad_window(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not mint viewer session cookie")

    ok = client.get("/api/dashboard/overview?window=last_quarter",
                    cookies={"ads_session": cookie})
    assert ok.status_code == 200, ok.text
    assert ok.json()["window"]["key"] == "last_quarter"

    # The overview is business-truth: the old ad windows are a 400, not a fallback.
    bad = client.get("/api/dashboard/overview?window=30d",
                     cookies={"ads_session": cookie})
    assert bad.status_code == 400, bad.text


# ══════════════════ 2. KPI safety: canonical sources only ═══════════════════


def test_kpis_come_from_canonical_safe_sources(monkeypatch):
    out = _overview(monkeypatch)
    k = out["kpis"]
    # USD spend is the canonical Google Ads USD denominator, never native GBP.
    assert k["google_ads_spend_usd"] == 13000.0
    assert k["native_spend"] == {"amount": 10000.0, "currency": "GBP"}
    # Revenue is HubSpot closed-won truth.
    assert k["closed_won_revenue_usd"] == 9000.0
    assert out["google_ads_conversion_value_used"] is False
    assert out["source_truth"] == "revenue_decision_mart"
    # Pipeline counts + honest rates.
    assert (k["leads"], k["sqls"], k["customers"]) == (5, 2, 1)
    assert k["sql_rate"] == 0.4
    assert k["customer_rate"] == 0.2
    assert out["read_only"] is True


def test_missing_fx_blocks_usd_spend_and_roas_never_zero(monkeypatch):
    out = _overview(monkeypatch, canonical=CANONICAL_FX_INCOMPLETE)
    k = out["kpis"]
    # None, not a fabricated $0 / 0.00x.
    assert k["google_ads_spend_usd"] is None and k["google_ads_spend_usd"] != 0
    assert k["google_ads_roas"] is None and k["google_ads_roas"] != 0
    # Native GBP stays visible and stays GBP.
    assert k["native_spend"]["amount"] == 10000.0
    assert k["native_spend"]["currency"] == "GBP"
    # The withholding is explained, never silent.
    metrics = {u["metric"] for u in out["unavailable"]}
    assert "google_ads_spend_usd" in metrics
    assert "google_ads_roas" in metrics
    assert out["truth_status"]["fx"] != "ready"


def test_missing_spend_source_reports_unavailable(monkeypatch):
    out = _overview(monkeypatch, canonical={"available": False, "rows": []})
    k = out["kpis"]
    assert k["google_ads_spend_usd"] is None
    assert k["google_ads_roas"] is None
    assert out["truth_status"]["spend"] != "ready"


def test_non_google_sources_never_get_roas(monkeypatch):
    out = _overview(monkeypatch)
    mix = {s["key"]: s for s in out["source_mix"]}
    assert set(mix) == {"google_ads", "other_paid", "organic", "offline", "unclassified"}
    for key, row in mix.items():
        # Review fix: NO mix row carries ROAS — not even Google Ads. The
        # source-page group ROAS is a geo-spend ratio ungated by coverage/FX
        # safety and could contradict the hero ROAS on the same screen; the
        # hero KPI is the only ROAS this page shows.
        assert row["roas"] is None, f"{key} must never carry ROAS on the mix"
        if key == "google_ads":
            assert row["spend_connected"] is True
            continue
        assert row["spend_connected"] is False
        assert "Revenue-only" in row["status"] or key in ("offline", "unclassified")
    assert "Imported CRM records" in mix["offline"]["status"]
    assert "Needs review" in mix["unclassified"]["status"]


def test_revenue_is_hubspot_not_google_conversion_value(monkeypatch):
    # Revenue equals the sum of HubSpot closed-won rows exactly; no other
    # figure (e.g. platform conversion value) can leak into the KPI.
    out = _overview(monkeypatch, won_rows=[WON_ROW_MAPPED,
                                           {**WON_ROW_MAPPED, "deal_id": "d2",
                                            "deal_amount_usd": 1000.0}])
    assert out["kpis"]["closed_won_revenue_usd"] == 10000.0
    assert out["google_ads_conversion_value_used"] is False
    assert "google_ads_conversion_value_used" in SERVICE
    assert "conversion value" in SERVICE.lower()


def test_disconnected_revenue_integration_never_shows_fake_zero_revenue(monkeypatch):
    # Review fix (PR-ADS-134): with the revenue integration NOT connected, the
    # mart's 0.0/0 aggregates over an unwired ledger are UNKNOWN, not real.
    import db.revenue_repository as repo
    _patch_durable(monkeypatch, won_rows=[], deal_rows=[], source_rev_rows=[])
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: False)
    from services.dashboard_overview_service import build_dashboard_overview
    out = build_dashboard_overview(WINDOW, now=NOW)
    k = out["kpis"]
    assert k["closed_won_revenue_usd"] is None and k["closed_won_revenue_usd"] != 0
    assert k["customers"] is None
    assert k["customer_rate"] is None
    assert k["google_ads_roas"] is None
    # The trend never emits a flat $0 "ready" revenue series.
    assert out["trend"]["revenue_status"] == "unavailable"
    assert all(p["revenue_usd"] is None for p in out["trend"]["points"])
    # Per-source revenue is unknown too — None, not $0.
    for row in out["source_mix"]:
        assert row["revenue_usd"] is None
        assert row["customers"] is None
    assert out["truth_status"]["revenue"] == "blocked"
    # The Scale card states the truth is unavailable instead of claiming
    # "no campaign produced a customer".
    scale = next(c for c in out["decision_cards"] if c["type"] == "scale")
    assert "unavailable" in scale["headline"].lower()
    assert scale["target_page"] == "revenue-health"


def test_unreadable_deal_ledger_downgrades_revenue_truth_chip(monkeypatch):
    # Copilot review (PR #134): with the integration connected but the deal
    # ledger unreadable, the trend withholds its revenue series — the revenue
    # truth chip must say "partial" in that state, never "ready".
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [], available=False)
    from services.dashboard_overview_service import build_dashboard_overview
    out = build_dashboard_overview(WINDOW, now=NOW)
    assert out["trend"]["revenue_status"] == "unavailable"
    assert out["truth_status"]["revenue"] == "partial"
    assert out["truth_status"]["overall"] != "ready"


def test_spend_series_fails_closed_on_inconsistent_daily_fx(monkeypatch):
    # Review fix: a day with spend_usd=None inside an otherwise-verified window
    # withholds the WHOLE series — it is never coerced to a $0 day.
    broken_daily = SPEND_DAILY_ROWS + [
        {"spend_date": "2026-06-02", "cost_micros": 1000_000_000,
         "spend_native": 1000.0, "spend_usd": None, "fx_complete": False},
    ]
    out = _overview(monkeypatch, spend_daily=broken_daily)
    trend = out["trend"]
    assert trend["spend_status"] == "unavailable"
    assert "withheld" in trend["spend_reason"]
    assert all(p["spend_usd"] is None for p in trend["points"])


def test_spend_never_competes_for_fastest_improving(monkeypatch):
    # Review fix: spend is valence-neutral — growing spend is not "improving".
    _patch_durable(monkeypatch)
    from services.dashboard_overview_service import _build_signals
    period_change = {
        "metrics": {
            "spend_usd": {"status": "ok", "delta_pct": 5.0},
            "revenue_usd": {"status": "ok", "delta_pct": 0.1},
            "sqls": {"status": "ok", "delta_pct": -0.2},
        },
    }
    signals = _build_signals([], [], [], {"sqls": 1}, {}, period_change)
    assert signals["fastest_improving"]["metric"] == "revenue_usd"
    assert signals["biggest_decline"]["metric"] == "sqls"


# ══════════════════ 3. Previous period: never a fake 0% ═════════════════════


def test_all_time_has_no_previous_period(monkeypatch):
    out = _overview(monkeypatch, window="all_time")
    pc = out["period_change"]
    assert pc["available"] is False
    assert pc["metrics"] == {}
    assert pc["reason"]
    metrics = {u["metric"] for u in out["unavailable"]}
    assert "period_change" in metrics


def test_unavailable_previous_metric_is_no_comparison_not_zero(monkeypatch):
    # Coverage is verified for 2026 only, so the prior-YEAR ytd window has an
    # untrusted spend denominator: prior ROAS is None -> no_comparison.
    out = _overview(monkeypatch, window="ytd")
    roas_delta = out["period_change"]["metrics"]["roas"]
    assert roas_delta["status"] == "no_comparison"
    assert roas_delta["delta_pct"] is None
    assert roas_delta["delta_pct"] != 0


def test_zero_baseline_is_no_comparison_not_percent(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_overview_service import _delta_metric
    m = _delta_metric(10.0, 0.0)
    assert m["status"] == "no_comparison"
    assert m["delta_pct"] is None
    m2 = _delta_metric(None, 5.0)
    assert m2["status"] == "no_comparison" and m2["delta_pct"] is None


def test_current_quarter_compares_against_last_quarter(monkeypatch):
    out = _overview(monkeypatch, window="current_quarter")
    pc = out["period_change"]
    assert pc["previous_window"]["key"] == "last_quarter"
    assert pc["label"] == "vs Last Quarter"
    assert pc["note"]  # partial-period honesty note


# ══════════════════ 4. Trend series safety ══════════════════════════════════


def test_trend_spend_series_ready_only_when_denominator_safe(monkeypatch):
    out = _overview(monkeypatch)
    trend = out["trend"]
    assert trend["spend_status"] == "ready"
    assert trend["revenue_status"] == "ready"
    assert trend["bucket"] in ("week", "month")
    # Bucket sums reconcile with the daily rows (2 x 6500 USD).
    assert sum(p["spend_usd"] or 0 for p in trend["points"]) == 13000.0
    assert sum(p["revenue_usd"] or 0 for p in trend["points"]) == 9000.0
    assert sum(p["customers"] or 0 for p in trend["points"]) == 1


def test_trend_spend_series_withheld_when_fx_incomplete(monkeypatch):
    out = _overview(monkeypatch, canonical=CANONICAL_FX_INCOMPLETE)
    trend = out["trend"]
    assert trend["spend_status"] == "unavailable"
    assert trend["spend_reason"]
    # Every point is None — a withheld series is never rendered as $0 days.
    assert all(p["spend_usd"] is None for p in trend["points"])
    # Revenue stays a partial-but-honest series.
    assert trend["revenue_status"] == "ready"


def test_trend_never_fabricates_dates_outside_window(monkeypatch):
    out = _overview(monkeypatch, window="ytd")
    win = out["window"]
    for p in out["trend"]["points"]:
        assert win["start_date"] <= p["period_start"] <= win["end_date"]


# ══════════════════ 5. Signals + decision cards ═════════════════════════════


def test_top_signals_computed_from_real_rows(monkeypatch):
    out = _overview(monkeypatch)
    sig = out["top_signals"]
    assert sig["best_campaign"]["label"] == "Brand - UK"
    assert sig["best_campaign"]["revenue_usd"] == 9000.0
    assert sig["best_campaign"]["target_page"] == "roas-campaigns"
    assert sig["best_country"]["label"] == "United Kingdom"
    assert sig["best_country"]["target_page"] == "roas-countries"
    assert sig["best_source"]["target_page"] == "revenue-by-source"


def test_top_signals_handle_empty_data_safely(monkeypatch):
    out = _overview(
        monkeypatch,
        won_rows=[], lead_rows=[], deal_rows=[],
        source_lead_rows=[], source_rev_rows=[],
        canonical={**CANONICAL, "rows": [], "total_spend": 0.0,
                   "total_spend_usd": 0.0, "campaign_count": 0},
        spend_daily=[],
    )
    sig = out["top_signals"]
    assert sig["best_campaign"] is None
    assert sig["best_country"] is None
    assert sig["biggest_waste"] is None
    # Four decision cards still render (honest empty copy, no crash).
    assert [c["type"] for c in out["decision_cards"]] == [
        "scale", "watch", "investigate", "unavailable"]


def test_waste_signal_carries_explicit_currency(monkeypatch):
    # A campaign with spend and no SQLs/customers is the waste signal. With FX
    # incomplete, row spend is native GBP and MUST be labelled GBP, never "$".
    lead_rows = [r for r in LEAD_ROWS if r["campaign_name"] != "Search - Global"]
    out = _overview(monkeypatch, canonical=CANONICAL_FX_INCOMPLETE,
                    lead_rows=lead_rows)
    waste = out["top_signals"]["biggest_waste"]
    assert waste is not None
    assert waste["currency"] == "GBP"
    investigate = next(c for c in out["decision_cards"] if c["type"] == "investigate")
    assert "GBP" in investigate["body"]
    assert "$" not in investigate["body"]


def test_decision_cards_link_to_evidence_pages(monkeypatch):
    out = _overview(monkeypatch)
    targets = {c["type"]: c["target_page"] for c in out["decision_cards"]}
    assert targets["scale"] == "roas-campaigns"
    assert targets["investigate"] in ("action-queue", "revenue-health")
    assert targets["unavailable"] == "revenue-health"


def test_withheld_lead_metrics_degrade_watch_and_waste_honestly(monkeypatch):
    import db.revenue_repository as repo
    _patch_durable(monkeypatch)
    monkeypatch.setattr(
        repo, "fetch_lead_quality",
        lambda s, e: {"available": True, "rows": [], "event_date_safe": False,
                      "missing_contact_created_at_count": 7,
                      "excluded_non_paid_count": 0,
                      "excluded_pseudo_campaign_count": 0},
    )
    from services.dashboard_overview_service import build_dashboard_overview
    out = build_dashboard_overview(WINDOW, now=NOW)
    assert out["kpis"]["sqls"] is None
    assert out["kpis"]["sql_rate"] is None  # never a fake 0%
    assert out["top_signals"]["biggest_waste"] is None  # unverifiable, not $0
    watch = next(c for c in out["decision_cards"] if c["type"] == "watch")
    assert "withheld" in watch["headline"].lower() or "withheld" in watch["body"].lower()


# ══════════════════ 6. Read-only safety ═════════════════════════════════════


def test_new_repo_fetcher_is_read_only():
    body = _repo_fn_body("fetch_spend_daily_series").lower()
    for forbidden in ("insert", "update ", "delete", "upsert", "drop", "alter",
                      "requests.post", "requests.patch", "requests.put"):
        assert forbidden not in body, f"fetch_spend_daily_series must be read-only ('{forbidden}')"


def test_no_platform_writes_in_touched_files():
    files = [
        "services/dashboard_overview_service.py",
        "db/revenue_repository.py",
        "api/server.py",
        "static/app.js",
    ]
    for rel in files:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
        for forbidden in (".mutate(", "upload_offline", "offline_conversion",
                          "google_ads_upload", "upload_conversions",
                          "requests.post", "requests.put", "requests.patch",
                          "requests.delete",
                          "set_budget", "set_bid", "pause_campaign",
                          "enable_campaign",
                          "create_deal", "update_deal", "create_contact",
                          "update_contact"):
            assert forbidden not in src, f"{rel} must not reference '{forbidden}'"


def test_building_the_overview_invokes_no_write_functions(monkeypatch):
    import db.writers as db_writers
    for name in dir(db_writers):
        if name.startswith(("write_", "upsert_", "record_", "update_", "insert_")):
            attr = getattr(db_writers, name)
            if callable(attr):
                def _boom(*a, _n=name, **k):
                    raise AssertionError(f"unexpected write call: db.writers.{_n}")
                monkeypatch.setattr(db_writers, name, _boom, raising=False)
    _patch_durable(monkeypatch)  # re-apply read patches AFTER the guards
    from services.dashboard_overview_service import build_dashboard_overview
    for key in ("current_quarter", "last_quarter", "ytd", "all_time"):
        build_dashboard_overview(key, now=NOW)


def test_service_states_read_only_doctrine():
    lowered = SERVICE.lower()
    assert "read-only" in lowered
    assert "no fake $0" in lowered or "never a fake" in lowered or "never fabricated" in lowered


# ══════════════════ 7. Frontend: Executive Overview page ════════════════════


def _dashboard_section():
    import re
    m = re.search(r'<section id="page-dashboard".*?</section>', HTML, re.DOTALL)
    assert m, "page-dashboard section missing"
    return m.group(0)


def test_dashboard_markup_is_the_executive_overview():
    section = _dashboard_section()
    assert "Executive overview of spend, revenue, pipeline, and action signals." in section
    assert 'id="dashboard-overview-root"' in section
    # Truth chips, compact — not a giant explanation block.
    assert "Read-only" in section
    assert "Spend: Google Ads" in section
    assert "Revenue: HubSpot Closed-Won" in section
    # The old diagnostic dashboard is gone.
    assert "Campaign intelligence overview" not in HTML
    assert 'id="kpi-spend"' not in HTML
    assert 'id="dash-verdict-body"' not in HTML
    assert 'id="run-history-timeline"' not in HTML


def test_dashboard_tabs_only_overview_active():
    section = _dashboard_section()
    assert 'class="dashboard-tab is-active"' in section
    for label in ("Revenue", "Channels", "Campaigns &amp; Keywords", "Countries", "Deals"):
        assert label in section
    # Overview is the active tab. PR-ADS-135/136/137/138/139 enabled Revenue,
    # Channels, Campaigns & Keywords, Countries and Deals in turn — so all six
    # dashboard tabs are now live and no disabled placeholder remains.
    assert section.count('class="dashboard-tab" disabled') == 0


def test_business_window_selector_exists_and_is_wired():
    section = _dashboard_section()
    assert 'id="dashboard-window"' in section
    assert "data-business-window-select" in section
    for key in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert f'value="{key}"' in section
    # Selector change reloads the overview; the loader reads the shared window.
    handler = _slice(JS, "function handleBusinessWindowSelectChange", 700)
    assert '"dashboard":       loadDashboardOverview();' in handler \
        or 'case "dashboard"' in handler
    loader = _slice(JS, "async function loadDashboardOverview", 1200)
    assert "getRoasBusinessWindow()" in loader
    assert "/api/dashboard/overview?window=" in loader


def test_dashboard_no_longer_uses_ad_windows():
    # The dashboard block must not fetch any ?days= endpoint; the old ad-window
    # dashboard endpoints are gone from the frontend entirely.
    assert "/api/summary?days" not in JS
    assert "/api/dashboard/trends" not in JS
    assert '"dashboard"' in _slice(JS, "const REVENUE_PAGES", 300)


def test_kpi_cards_render_all_five_hero_metrics():
    fn = _slice(JS, "function renderDashKpiRow", 4200)
    for label in ("Google Ads Spend", "Closed-Won Revenue", "Customers",
                  "SQLs", "Google Ads ROAS"):
        assert label in fn
    # Native GBP line under spend, via the currency-correct formatter.
    assert "fmtCompactCurrency(native.amount, native.currency" in fn
    # ROAS renders through the safe formatter only.
    assert "fmtRoasMultiple" in fn


def test_unavailable_renders_as_unavailable_never_blank():
    fn = _slice(JS, "function dashValue", 400)
    assert 'Unavailable' in fn
    assert "null" in fn and "undefined" in fn  # explicit guards
    # fmtRoasMultiple(null) is the house "Unavailable" (pinned in PR-109 too).
    roas_fn = _slice(JS, "function fmtRoasMultiple", 200)
    assert '"Unavailable"' in roas_fn


def test_no_undefined_or_null_leaks_in_dash_renderers():
    block = JS[JS.find("// ── Dashboard — Executive Overview"):JS.find("// ── Campaigns page")]
    assert block, "dashboard block not found"
    assert ">undefined<" not in block
    assert ">null<" not in block
    assert ">NaN<" not in block


def test_delta_chip_no_comparison_never_zero_percent():
    fn = _slice(JS, "function dashDeltaChip", 1400)
    assert "No comparison" in fn
    assert 'm.status !== "ok"' in fn


def test_channel_mix_never_renders_roas():
    # Review fix: no ROAS anywhere in the channel mix — the hero KPI carries
    # the one canonical, safety-gated ROAS on this page.
    mix_start = JS.find("function renderDashSourceMix")
    mix_end = JS.find("const DASH_SIGNAL_DEFS", mix_start)
    assert mix_start != -1 and mix_end > mix_start
    mix_block = JS[mix_start:mix_end]  # includes dashDonutSVG
    assert "fmtRoasMultiple" not in mix_block
    assert "Needs review" in mix_block
    # Unknown revenue renders Unavailable, never an implied verified $0 ring.
    assert "revenueKnown" in mix_block
    assert "Revenue blocked" in mix_block


def test_funnel_conversion_never_fakes_zero():
    fn = _slice(JS, "function dashConversion", 400)
    assert "return null" in fn
    funnel = _slice(JS, "function renderDashFunnel", 2400)
    for stage in ("Leads", "SQLs", "Customers", "Closed-Won Revenue"):
        assert stage in funnel


def test_signals_and_cards_link_to_evidence_pages():
    signals = _slice(JS, "function renderDashSignals", 1800)
    assert 'href="${target' in signals or "#/" in signals
    cards = _slice(JS, "function renderDashDecisionCards", 1200)
    assert 'href="#/${escapeHtml(c.target_page)}"' in cards
    # Service-side targets are the real evidence pages.
    for target in ('"roas-campaigns"', '"roas-countries"', '"revenue-by-source"',
                   '"action-queue"', '"revenue-health"'):
        assert target in SERVICE


def test_error_loading_empty_states_exist():
    assert "function renderDashSkeleton" in JS
    assert "Executive overview unavailable" in JS
    assert "No chartable trend in this window." in JS
    assert "is-refreshing" in JS  # refetch holds the frame, dimmed


def test_chart_and_donut_are_inline_svg_no_libraries():
    block = JS[JS.find("// ── Dashboard — Executive Overview"):JS.find("// ── Campaigns page")]
    assert "<svg" in block
    # No third-party chart libraries anywhere.
    for lib in ("chart.js", "Chart(", "d3.", "highcharts", "recharts", "echarts"):
        assert lib not in JS


def test_responsive_layout_rules_exist():
    for cls in (".dash-kpi-grid", ".dash-main-grid", ".dash-lower-grid",
                ".dashboard-tabs", ".dash-funnel", ".dash-decision-grid",
                ".dash-truth-footer"):
        assert cls in CSS, f"{cls} missing from styles.css"
    assert "@media (max-width: 1100px)" in CSS
    assert "@media (max-width: 640px)" in CSS
    # Reduced-motion users get no entrance animation.
    assert "prefers-reduced-motion" in CSS


def test_dashboard_suppresses_ad_chrome_and_freshness_strip():
    chrome = _slice(JS, "function applyPageChrome", 1200)
    assert "is-dash-hidden" in chrome
    assert "#data-freshness-bar.is-dash-hidden" in CSS
    # Explanation container stays for the help framework (hidden by default).
    assert 'id="page-explanation-dashboard"' in HTML
