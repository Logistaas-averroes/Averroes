"""
PR-ADS-135 — Dashboard Revenue & Customers.

Proves the second Dashboard tab (Dashboard → Revenue) and its read-only contract:

  - GET /api/dashboard/revenue exists, is read-only, and uses BUSINESS windows;
    ad windows (30d) are a 400, not a coerced fallback.
  - Revenue is HubSpot closed-won only; Google Ads conversion value is never used.
  - Top-deal amounts stay null when missing (never $0); contact-level proof
    fields (company_id / main_contact / contact_id / deal name / source) are not
    durably stored, so they are null → "Unavailable".
  - The revenue trend buckets by deal_close_date; SQL/customer rates are None
    (never 0%) when a denominator is missing; average deal value is None when
    there are no customers.
  - Campaign / source / country breakdowns PRESERVE unattributed revenue under
    "Unclassified / Needs Review" / "Unattributed" — never dropped, never forced
    into Organic or Google Ads; non-Google source rows never carry ROAS.
  - Deal concentration is computed from real amounts; a disconnected revenue
    integration yields Unavailable, not $0, everywhere; previous-period deltas
    never fake 0%.
  - Frontend: the Revenue tab exists and activates (Overview stays default),
    it calls /api/dashboard/revenue, reuses the PR-134 KPI/chart/card/footer
    system, renders the proof columns, shows Unavailable for missing fields,
    never leaks undefined/null/NaN, and never renders ROAS on non-Google rows.
  - Read-only: no platform-write verbs in the touched files; building the
    revenue contract calls no db.writers write function.

Patches the durable repository boundary (db.revenue_repository) with one
coherent dataset — no real database, no network.
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
    os.path.join(ROOT, "services", "dashboard_revenue_service.py"), encoding="utf-8"
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


# ── Coherent durable dataset (GBP 10,000 native / USD 13,000; one unattributed) ──

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
# Won rows: two mapped campaigns + one deal with NO campaign/country (unattributed).
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
    {"deal_id": "d1", "company": "Acme Logistics", "country": "United States",
     "campaign_name": "Global Competitors", "deal_close_date": "2026-06-14",
     "deal_amount_usd": 18000.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
    {"deal_id": "d2", "company": "Blue Cargo", "country": "United States",
     "campaign_name": "Global Competitors", "deal_close_date": "2026-05-23",
     "deal_amount_usd": 11000.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
    {"deal_id": "d3", "company": "Trans UK", "country": "United Kingdom",
     "campaign_name": "Brand - UK", "deal_close_date": "2026-04-23",
     "deal_amount_usd": 9000.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "utm"},
    {"deal_id": "d4", "company": "Ghost Co", "country": None,
     "campaign_name": None, "deal_close_date": "2026-06-19",
     "deal_amount_usd": 4000.0, "deal_stage_label": "Closed Won",
     "match_status": "unknown", "match_source": None},
]
SOURCE_LEAD_ROWS = (
    [{"acquisition_group": "google_ads", "status_category": "qualified",
      "source_primary_raw": "PAID_SEARCH", "source_detail_raw": "google"}] * 20
    + [{"acquisition_group": "organic", "status_category": "lead",
        "source_primary_raw": "SOCIAL_ORGANIC", "source_detail_raw": "linkedin"}] * 30
)
SOURCE_REV_ROWS = [
    {"acquisition_group": "google_ads", "attribution_status": "attributed",
     "deal_amount_usd": 38000.0, "source_primary_raw": "PAID_SEARCH", "source_detail_raw": "google"},
    {"acquisition_group": "organic", "attribution_status": "attributed",
     "deal_amount_usd": 4000.0, "source_primary_raw": "SOCIAL_ORGANIC", "source_detail_raw": "linkedin"},
]
LEAD_DAILY_ROWS = [
    {"event_date": "2026-02-10", "leads": 50, "sqls": 10},
    {"event_date": "2026-05-10", "leads": 50, "sqls": 10},
]


def _patch_durable(monkeypatch, *, won_rows=None, deal_rows=None, canonical=None,
                   lead_daily=None, source_rev_rows=None):
    import db.revenue_repository as repo

    won = won_rows if won_rows is not None else WON_ROWS
    deals = deal_rows if deal_rows is not None else DEAL_ROWS
    canon = canonical if canonical is not None else CANONICAL
    daily = lead_daily if lead_daily is not None else LEAD_DAILY_ROWS
    src_rev = source_rev_rows if source_rev_rows is not None else SOURCE_REV_ROWS

    monkeypatch.setattr(repo, "fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr(repo, "fetch_campaign_country_spend",
                        lambda s, e: {"available": True, "rows": list(GEO_ROWS),
                                      "coverage_start": "2026-01-01", "coverage_end": "2026-06-22"})
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: {"available": True, "rows": list(LEAD_ROWS),
                                      "event_date_safe": True, "missing_contact_created_at_count": 0,
                                      "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0})
    monkeypatch.setattr(repo, "fetch_won_revenue",
                        lambda s, e: {"available": True, "rows": list(won),
                                      "coverage_start": "2026-01-01", "coverage_end": "2026-06-22"})
    monkeypatch.setattr(repo, "fetch_sync_state", lambda: {"available": True, "datasets": {}})
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e, *_a, **_k: dict(canon))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total", lambda s, e, *_a, **_k: dict(GEO_CANONICAL_TOTAL))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country", lambda s, e: dict(GEO_BY_COUNTRY))
    monkeypatch.setattr(repo, "fetch_spend_coverage",
                        lambda s, e, *_a, **_k: {"available": True, "chunks": list(COVERAGE_COMPLETE)})
    monkeypatch.setattr(repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: True)
    # PR-ADS-153E-B: the closed-won population is the canonical deal ledger, so
    # the same fixture deals are stubbed there instead of on the legacy read.
    patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(deals))
    monkeypatch.setattr(repo, "fetch_source_leads",
                        lambda s, e: {"available": True, "rows": list(SOURCE_LEAD_ROWS)})
    monkeypatch.setattr(repo, "fetch_source_revenue",
                        lambda s, e: {"available": True, "rows": list(src_rev)})
    monkeypatch.setattr(repo, "fetch_lead_daily_series",
                        lambda s, e: {"available": True, "rows": list(daily)})
    import db.writers as db_writers
    monkeypatch.setattr(db_writers, "source_attribution_health_counts",
                        lambda: {"classified_contacts": 50, "attributed_deals": 4,
                                 "ambiguous_deals": 0, "unclassified_deals": 1})


def _revenue(monkeypatch, window=WINDOW, **kw):
    _patch_durable(monkeypatch, **kw)
    from services.dashboard_revenue_service import build_dashboard_revenue
    return build_dashboard_revenue(window, now=NOW)


# ══════════════════ 1. Endpoint + windows ═══════════════════════════════════


def test_endpoint_registered():
    assert '@app.get("/api/dashboard/revenue")' in SERVER
    assert "build_dashboard_revenue" in SERVER
    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/dashboard/revenue" in routes


def test_all_business_windows_accepted(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_revenue_service import build_dashboard_revenue
    for key in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert build_dashboard_revenue(key, now=NOW)["window"]["key"] == key


def test_invalid_window_raises(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_revenue_service import build_dashboard_revenue
    for bad in ("30d", "60d", "", "quarter"):
        with pytest.raises(ValueError):
            build_dashboard_revenue(bad, now=NOW)


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


def test_endpoint_accepts_business_window_rejects_ad_window(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not mint viewer cookie")
    ok = client.get("/api/dashboard/revenue?window=last_quarter", cookies={"ads_session": cookie})
    assert ok.status_code == 200, ok.text
    assert ok.json()["window"]["key"] == "last_quarter"
    bad = client.get("/api/dashboard/revenue?window=30d", cookies={"ads_session": cookie})
    assert bad.status_code == 400, bad.text


# ══════════════════ 2. Revenue truth ════════════════════════════════════════


def test_revenue_from_hubspot_closed_won_only(monkeypatch):
    out = _revenue(monkeypatch)
    k = out["kpis"]
    assert k["closed_won_revenue_usd"] == 42000.0  # 18000+11000+9000+4000
    assert k["customers"] == 4
    assert k["largest_deal_usd"] == 18000.0
    assert out["google_ads_conversion_value_used"] is False
    # PR-ADS-153E-B: closed-won revenue is the canonical deal ledger, read at
    # `all_source` scope through the shared contract.
    assert out["source_truth"] == "hubspot_deal_ledger"
    assert out["revenue_source"] == "hubspot_deal_ledger"
    assert out["revenue_scope"] == "all_source"
    assert out["legacy_fallback_used"] is False
    assert out["read_only"] is True
    assert "conversion value" in SERVICE.lower()
    assert "google_ads_conversion_value_used" in SERVICE


def test_average_deal_value_and_rates(monkeypatch):
    out = _revenue(monkeypatch)
    k = out["kpis"]
    assert k["average_deal_value_usd"] == 10500.0  # 42000 / 4
    assert k["sqls"] == 20
    assert k["sql_to_customer_rate"] == 0.2  # 4 / 20
    assert k["revenue_per_sql_usd"] == 2100.0  # 42000 / 20


def test_rates_unavailable_when_denominator_missing(monkeypatch):
    import db.revenue_repository as repo
    _patch_durable(monkeypatch)
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: {"available": True, "rows": [], "event_date_safe": False,
                                      "missing_contact_created_at_count": 5,
                                      "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0})
    from services.dashboard_revenue_service import build_dashboard_revenue
    out = build_dashboard_revenue(WINDOW, now=NOW)
    k = out["kpis"]
    assert k["sqls"] is None
    assert k["sql_to_customer_rate"] is None and k["sql_to_customer_rate"] != 0
    assert k["revenue_per_sql_usd"] is None


def test_average_deal_value_unavailable_when_no_customers(monkeypatch):
    out = _revenue(monkeypatch, won_rows=[], deal_rows=[])
    k = out["kpis"]
    assert k["average_deal_value_usd"] is None and k["average_deal_value_usd"] != 0
    assert k["largest_deal_usd"] is None


def test_disconnected_integration_never_fabricates_zero(monkeypatch):
    import db.revenue_repository as repo
    _patch_durable(monkeypatch, won_rows=[], deal_rows=[], source_rev_rows=[])
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: False)
    from services.dashboard_revenue_service import build_dashboard_revenue
    out = build_dashboard_revenue(WINDOW, now=NOW)
    k = out["kpis"]
    for field in ("closed_won_revenue_usd", "customers", "average_deal_value_usd",
                  "largest_deal_usd", "sql_to_customer_rate", "revenue_per_sql_usd"):
        assert k[field] is None, f"{field} must be None when integration disconnected"
    assert out["revenue_trend"]["status"] == "unavailable"
    assert out["deal_concentration"] is None
    assert out["top_deals"] == []
    assert out["truth_status"]["revenue"] == "blocked"


# ══════════════════ 3. Trend + concentration ════════════════════════════════


def test_revenue_trend_buckets_by_close_date(monkeypatch):
    out = _revenue(monkeypatch)
    trend = out["revenue_trend"]
    assert trend["status"] == "ready"
    assert trend["bucket"] in ("week", "month")
    # Every datable deal is inside a bucket and buckets reconcile to the total.
    assert sum(p["revenue_usd"] or 0 for p in trend["points"]) == 42000.0
    assert sum(p["deals"] or 0 for p in trend["points"]) == 4
    win = out["window"]
    for p in trend["points"]:
        assert win["start_date"] <= p["period_start"] <= win["end_date"]


def test_customer_trend_has_no_cross_grain_rate(monkeypatch):
    out = _revenue(monkeypatch)
    ct = out["customer_trend"]
    assert ct["customers_status"] == "ready"
    assert ct["sqls_status"] == "ready"
    # Per-bucket points carry the two real counts but NEVER a fabricated rate.
    for p in ct["points"]:
        assert "sql_to_customer_rate" not in p
        assert set(p) >= {"period_start", "customers", "sqls"}


def test_deal_concentration_logic(monkeypatch):
    out = _revenue(monkeypatch)
    c = out["deal_concentration"]
    # amounts 18000/11000/9000/4000 of 42000: top1=0.4286, top3=0.9048 -> high.
    assert c["top_1_deal_share"] == 0.4286
    assert c["top_3_deals_share"] == 0.9048
    assert c["label"] == "Highly concentrated"
    assert c["customers"] == 4


# ── Null-amount doctrine (Copilot + review): a missing amount is never a $0 ──

def _null_amount_dataset():
    """Default dataset but d1's amount is unknown (null) everywhere."""
    won = [dict(WON_ROWS[0], deal_amount_usd=None)] + WON_ROWS[1:]
    deals = [dict(DEAL_ROWS[0], deal_amount_usd=None)] + DEAL_ROWS[1:]
    return won, deals


def test_null_amount_makes_bucket_revenue_unavailable_not_zero(monkeypatch):
    won, deals = _null_amount_dataset()
    out = _revenue(monkeypatch, won_rows=won, deal_rows=deals)
    trend = out["revenue_trend"]
    # The bucket holding d1 (null amount) reports revenue_usd=None, but still
    # counts the deal/customer — never a fabricated $0 from the unknown amount.
    null_pts = [p for p in trend["points"] if p["revenue_usd"] is None and p["deals"] > 0]
    assert null_pts, "expected a bucket with a null amount to report null revenue"
    assert all(p["customers"] >= 1 for p in null_pts)
    # A bucket that CONTAINS deals never fabricates $0 from an unknown amount:
    # it is either a real known sum or None. (An empty bucket — no deals — is a
    # genuine verified $0 inside a ready series, which is allowed.)
    for p in trend["points"]:
        if p["deals"] > 0:
            assert p["revenue_usd"] is None or p["revenue_usd"] > 0


def test_null_amount_makes_group_revenue_unavailable(monkeypatch):
    won, deals = _null_amount_dataset()
    out = _revenue(monkeypatch, won_rows=won, deal_rows=deals)
    # d1 is Global Competitors / United States — those groups have an unknown
    # amount, so their revenue total is Unavailable (None), counts preserved.
    camp = next(r for r in out["breakdowns"]["by_campaign"] if r["campaign"] == "Global Competitors")
    assert camp["revenue_usd"] is None
    assert camp["deals"] >= 1 and camp["customers"] >= 1
    country = next(r for r in out["breakdowns"]["by_country"] if r["country"] == "United States")
    assert country["revenue_usd"] is None


def test_null_amount_makes_avg_concentration_and_largest_unavailable(monkeypatch):
    # Review + follow-up: with an unknown deal amount, average deal value, deal
    # concentration AND largest deal are all Unavailable (the unknown deal could
    # be the largest). Deal/customer counts are still preserved.
    won, deals = _null_amount_dataset()
    out = _revenue(monkeypatch, won_rows=won, deal_rows=deals)
    assert out["kpis"]["average_deal_value_usd"] is None
    assert out["deal_concentration"] is None
    assert out["kpis"]["largest_deal_usd"] is None  # strict: unknown could be largest
    # Counts still include every closed-won deal.
    assert out["kpis"]["customers"] == 4


def _card_strings(out):
    """All human-facing decision-card text (headlines + bodies)."""
    parts = []
    for c in out["decision_cards"]:
        parts.append(c.get("headline") or "")
        parts.append(c.get("body") or "")
    return " || ".join(parts)


def test_decision_card_copy_never_fabricates_zero_for_unknown_revenue(monkeypatch):
    # A closed-won deal with a null amount in an unattributed campaign/country:
    # the Investigate card references that revenue but must say Unavailable, not $0.
    unattributed_null = [
        {"deal_id": "d1", "company": "Ghost Co", "country": None, "campaign_name": None,
         "deal_close_date": "2026-06-14", "deal_amount_usd": None,
         "deal_stage_label": "Closed Won", "match_status": "unknown", "match_source": None},
        {"deal_id": "d2", "company": "Blue Cargo", "country": "United States",
         "campaign_name": "Global Competitors", "deal_close_date": "2026-05-23",
         "deal_amount_usd": 11000.0, "deal_stage_label": "Closed Won",
         "match_status": "matched", "match_source": "gclid"},
    ]
    won = [
        {"campaign_name": None, "country": None, "deal_id": "d1",
         "deal_amount_usd": None, "match_status": "unknown"},
        {"campaign_name": "Global Competitors", "country": "United States",
         "deal_id": "d2", "deal_amount_usd": 11000.0, "match_status": "matched"},
    ]
    out = _revenue(monkeypatch, won_rows=won, deal_rows=unattributed_null)
    # The unattributed group's revenue is unknown -> its breakdown revenue is None.
    unc = next(r for r in out["breakdowns"]["by_campaign"] if r["attribution_status"] == "unattributed")
    assert unc["revenue_usd"] is None
    # Decision-card copy must not fabricate a $0 for that unknown revenue.
    text = _card_strings(out)
    assert "$0" not in text
    assert "Unavailable" in text
    # The Investigate card specifically references the unattributed revenue.
    investigate = next(c for c in out["decision_cards"] if c["type"] == "investigate")
    assert "$0" not in investigate["body"]
    assert "Unavailable" in investigate["body"]


def test_fmt_usd_short_none_is_unavailable():
    from services.dashboard_revenue_service import _fmt_usd_short
    assert _fmt_usd_short(None) == "Unavailable"
    assert _fmt_usd_short(18000) == "$18.0k"
    assert _fmt_usd_short(2_500_000) == "$2.5M"


def test_scale_card_says_unavailable_when_top_group_revenue_unknown(monkeypatch):
    # A multi-customer, attributed campaign whose total is Unavailable (one deal
    # amount unknown): the Scale card names the customers but never a $0 total.
    deals = [
        {"deal_id": "d1", "company": "Acme", "country": "United States",
         "campaign_name": "Global Competitors", "deal_close_date": "2026-06-14",
         "deal_amount_usd": None, "deal_stage_label": "Closed Won",
         "match_status": "matched", "match_source": "gclid"},
        {"deal_id": "d2", "company": "Blue", "country": "United States",
         "campaign_name": "Global Competitors", "deal_close_date": "2026-05-23",
         "deal_amount_usd": 11000.0, "deal_stage_label": "Closed Won",
         "match_status": "matched", "match_source": "gclid"},
    ]
    won = [
        {"campaign_name": "Global Competitors", "country": "United States",
         "deal_id": "d1", "deal_amount_usd": None, "match_status": "matched"},
        {"campaign_name": "Global Competitors", "country": "United States",
         "deal_id": "d2", "deal_amount_usd": 11000.0, "match_status": "matched"},
    ]
    out = _revenue(monkeypatch, won_rows=won, deal_rows=deals)
    scale = next(c for c in out["decision_cards"] if c["type"] == "scale")
    assert "$0" not in scale["body"]
    assert "Unavailable" in scale["body"]
    assert "Global Competitors" in scale["headline"]


def test_bucket_label_is_portable(monkeypatch):
    # No glibc-only "%-d" — a weekly label renders with no leading zero.
    out = _revenue(monkeypatch)
    labels = [p["period_label"] for p in out["revenue_trend"]["points"]]
    assert labels, "expected trend points"
    import re
    for lbl in labels:
        assert re.match(r"^\d{1,2} [A-Z][a-z]{2}$", lbl), f"unexpected label {lbl!r}"


def test_revenue_and_customer_trend_share_one_grain(monkeypatch):
    # Both trends must resolve the same (start, end, bucket) so the customer
    # series never disagrees with the revenue series on time grain.
    out = _revenue(monkeypatch)
    rev, cust = out["revenue_trend"], out["customer_trend"]
    assert rev["bucket"] == cust["bucket"]
    assert [p["period_start"] for p in rev["points"]] == [p["period_start"] for p in cust["points"]]


# ══════════════════ 4. Breakdowns preserve unattributed ═════════════════════


def test_campaign_breakdown_preserves_unattributed(monkeypatch):
    out = _revenue(monkeypatch)
    rows = out["breakdowns"]["by_campaign"]
    labels = [r["campaign"] for r in rows]
    assert "Unclassified / Needs Review" in labels
    unc = next(r for r in rows if r["campaign"] == "Unclassified / Needs Review")
    assert unc["revenue_usd"] == 4000.0  # revenue kept, never dropped
    assert unc["attribution_status"] == "unattributed"
    # Total revenue across breakdown rows reconciles to the KPI.
    assert sum(r["revenue_usd"] for r in rows) == 42000.0


def test_country_breakdown_preserves_unattributed(monkeypatch):
    out = _revenue(monkeypatch)
    rows = out["breakdowns"]["by_country"]
    assert "Unattributed" in [r["country"] for r in rows]
    unc = next(r for r in rows if r["country"] == "Unattributed")
    assert unc["revenue_usd"] == 4000.0
    assert unc["attribution_status"] == "unattributed"


def test_source_breakdown_has_no_roas_and_marks_spend_connection(monkeypatch):
    out = _revenue(monkeypatch)
    rows = out["breakdowns"]["by_source"]
    assert rows, "expected source rows"
    for r in rows:
        assert "roas" not in r, "source rows must never carry ROAS"
    google = next((r for r in rows if r["source_group"] == "Google Ads"), None)
    assert google and google["spend_connected"] is True
    others = [r for r in rows if r["source_group"] != "Google Ads"]
    for r in others:
        assert r["spend_connected"] is False
        assert r["status"] == "Revenue-only"


# ══════════════════ 5. Top deals proof + unavailable ════════════════════════


def test_top_deals_amount_null_stays_null_not_zero(monkeypatch):
    deal_rows = [dict(DEAL_ROWS[0], deal_amount_usd=None)] + DEAL_ROWS[1:]
    won_rows = [dict(WON_ROWS[0], deal_amount_usd=None)] + WON_ROWS[1:]
    out = _revenue(monkeypatch, deal_rows=deal_rows, won_rows=won_rows)
    d = next(x for x in out["top_deals"] if x["deal_id"] == "d1")
    assert d["amount_usd"] is None and d["amount_usd"] != 0


def test_top_deals_contact_fields_are_unavailable_not_fabricated(monkeypatch):
    out = _revenue(monkeypatch)
    d = out["top_deals"][0]
    # Not durably stored on the canonical ledger -> null -> UI shows Unavailable.
    # PR-ADS-153E-B moved `deal` (the deal's own name) into the STORED set and
    # `company` (the associated contact's employer) out of it: the ledger is
    # keyed on the deal and holds no company record at all.
    for field in ("company", "company_id", "main_contact"):
        assert d[field] is None, f"{field} must be null (Unavailable), not fabricated"
    assert d["deal_name"], "the canonical ledger stores the deal's own name"
    # Real fields are present.
    assert d["deal_id"] and d["amount_usd"] is not None and d["close_date"]
    reasons = {u["metric"] for u in out["unavailable"]}
    assert "main_contact" in reasons and "company_id" in reasons


def test_decision_cards_degrade_safely(monkeypatch):
    out = _revenue(monkeypatch, won_rows=[], deal_rows=[], source_rev_rows=[])
    types = [c["type"] for c in out["decision_cards"]]
    assert types == ["scale", "watch", "investigate", "unavailable"]
    for c in out["decision_cards"]:
        assert c["headline"] and c["body"]


def test_no_fake_deltas_when_previous_baseline_missing(monkeypatch):
    out = _revenue(monkeypatch, window="all_time")
    pc = out["period_change"]
    assert pc["available"] is False
    assert pc["metrics"] == {}
    assert "period_change" in {u["metric"] for u in out["unavailable"]}


def test_zero_baseline_delta_is_no_comparison(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_overview_service import _delta_metric
    m = _delta_metric(5, 0)
    assert m["status"] == "no_comparison" and m["delta_pct"] is None


# ══════════════════ 6. Read-only safety ═════════════════════════════════════


def test_new_repo_fetcher_is_read_only():
    body = _repo_fn_body("fetch_lead_daily_series").lower()
    for forbidden in ("insert", "update ", "delete", "upsert", "drop", "alter",
                      "requests.post", "requests.patch", "requests.put"):
        assert forbidden not in body, f"fetch_lead_daily_series must be read-only ('{forbidden}')"


def test_no_platform_writes_in_touched_files():
    files = [
        "services/dashboard_revenue_service.py",
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


def test_building_revenue_invokes_no_write_functions(monkeypatch):
    import db.writers as db_writers
    for name in dir(db_writers):
        if name.startswith(("write_", "upsert_", "record_", "update_", "insert_")):
            attr = getattr(db_writers, name)
            if callable(attr):
                def _boom(*a, _n=name, **k):
                    raise AssertionError(f"unexpected write call: db.writers.{_n}")
                monkeypatch.setattr(db_writers, name, _boom, raising=False)
    _patch_durable(monkeypatch)
    from services.dashboard_revenue_service import build_dashboard_revenue
    for key in ("current_quarter", "last_quarter", "ytd", "all_time"):
        build_dashboard_revenue(key, now=NOW)


# ══════════════════ 7. Frontend: Revenue tab ════════════════════════════════


def _dashboard_section():
    import re
    m = re.search(r'<section id="page-dashboard".*?</section>', HTML, re.DOTALL)
    assert m, "page-dashboard section missing"
    return m.group(0)


def test_revenue_tab_enabled_overview_default():
    section = _dashboard_section()
    assert 'data-dash-tab="revenue"' in section
    assert 'data-dash-tab="overview"' in section
    # Overview keeps the active class by default; Revenue is NOT disabled.
    assert 'class="dashboard-tab is-active" data-dash-tab="overview"' in section
    import re
    rev = re.search(r'<button[^>]*data-dash-tab="revenue"[^>]*>', section).group(0)
    assert "disabled" not in rev
    # A dedicated revenue root exists, hidden by default.
    assert 'id="dashboard-revenue-root"' in section
    assert 'id="dashboard-overview-root"' in section


def test_revenue_tab_is_hash_linkable():
    assert "hashToDashTab" in JS
    assert "?tab=" in JS
    # The shared tab controller grows as tabs are enabled (134→137); widen the
    # window so the Revenue root + loader are still covered.
    fn = _slice(JS, "function activateDashboardTab", 1800)
    assert "dashboard-revenue-root" in fn
    assert "loadDashboardRevenue" in fn


def test_revenue_tab_calls_its_endpoint():
    loader = _slice(JS, "async function loadDashboardRevenue", 1400)
    assert "/api/dashboard/revenue?window=" in loader
    assert "getRoasBusinessWindow()" in loader
    assert "_revReqSeq.dashRevenue" in loader


def test_revenue_kpi_cards_render_five_metrics():
    fn = _slice(JS, "function renderRevKpiRow", 4000)
    for label in ("Closed-Won Revenue", "Customers", "Average Deal Value",
                  "Largest Deal", "SQL → Customer Rate"):
        assert label in fn
    # Reuses the PR-134 KPI card system and Unavailable doctrine.
    assert "dash-kpi-card" in fn
    assert "dash-unavailable" in fn or "dashValue(" in fn


def test_revenue_reuses_pr134_design_system():
    start = JS.find("// ── Dashboard — Revenue & Customers")
    end = JS.find("// ── Campaigns page")
    assert start != -1 and end > start, "revenue block not found"
    block = JS[start:end]
    # Reuses PR-134 classes directly...
    for cls in ("dash-kpi-card", "dash-panel", "dash-truth-footer",
                "dash-combo-chart", "dash-chart-hit"):
        assert cls in block, f"revenue tab should reuse PR-134 class {cls}"
    # ...and the shared PR-134 decision-card renderer (not a re-implementation).
    assert "renderDashDecisionCards(" in block
    assert "renderDashSkeleton()" in block
    # No third-party chart libraries.
    for lib in ("chart.js", "Chart(", "d3.", "highcharts", "recharts", "echarts"):
        assert lib not in JS


def test_breakdown_panels_and_proof_columns_render():
    breakdown = _slice(JS, "function renderRevBreakdowns", 1200)
    assert "Revenue by Campaign" in breakdown
    assert "Revenue by Source" in breakdown
    assert "Revenue by Country" in breakdown
    cols = JS[JS.find("const REV_DEAL_COLUMNS"):JS.find("const REV_DEAL_COLUMNS") + 600]
    for label in ("Company", "Company ID", "Main Contact", "Contact ID", "Deal",
                  "Deal ID", "Amount", "Close Date", "Campaign", "Source", "Country", "Attribution"):
        assert label in cols


def test_amount_null_never_renders_zero():
    fn = _slice(JS, "function revDealCell", 400)
    # Amount cell routes through dashValue -> Unavailable, never fmtMoney(0).
    assert "dashValue(deal.amount_usd, fmtMoney)" in fn
    assert "null → Unavailable" in fn or "never $0" in fn


def test_revenue_block_has_no_undefined_or_null_leaks():
    block = JS[JS.find("// ── Dashboard — Revenue & Customers"):JS.find("// ── Campaigns page")]
    assert ">undefined<" not in block
    assert ">null<" not in block
    assert ">NaN<" not in block


def test_source_breakdown_frontend_never_shows_roas():
    fn = _slice(JS, "function renderRevBreakdownPanel", 2000)
    # No ROAS formatter and no ROAS field on any breakdown row (target_page
    # values like "roas-campaigns" are route ids, not displayed ROAS).
    assert "fmtRoasMultiple" not in fn
    assert "r.roas" not in fn
    assert ".roas" not in fn


def test_admin_gated_links_respect_role():
    footer = _slice(JS, "function renderRevTruthFooter", 1400)
    assert "dashCanNavigate(" in footer
    breakdown = _slice(JS, "function renderRevBreakdownPanel", 2000)
    assert "dashCanNavigate(" in breakdown


def test_revenue_responsive_classes_exist():
    for cls in (".rev-breakdown-grid", ".rev-deal-scroll", ".rev-conc",
                ".rev-attr", ".dash-tab-intro"):
        assert cls in CSS, f"{cls} missing from styles.css"
    assert "@media (max-width: 1100px)" in CSS
    assert "@media (max-width: 640px)" in CSS


def test_error_and_loading_states_exist():
    # The title is HTML-escaped in the error card template.
    assert "Revenue &amp; Customers unavailable" in JS
    assert "renderDashSkeleton()" in _slice(JS, "async function loadDashboardRevenue", 1400)
    assert "is-refreshing" in _slice(JS, "async function loadDashboardRevenue", 1400)
