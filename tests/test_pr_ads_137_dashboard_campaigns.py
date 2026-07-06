"""
PR-ADS-137 — Dashboard Campaigns & Keywords.

Proves the fourth Dashboard tab (Dashboard → Campaigns) and its read-only contract:

  - GET /api/dashboard/campaigns exists, is auth-gated, read-only, and uses
    BUSINESS windows; ad windows (30d) are a 400, not a coerced fallback.
  - It composes the canonical Revenue Decision Mart (view="campaign"), the
    per-campaign native-GBP + FX-gated-USD spend, and the closed-won deal ledger
    — no new business math, no new taxonomy. The Google Ads conversion value is
    never used as revenue.
  - Google Ads spend is native GBP and is never labelled USD. USD spend + ROAS
    are Unavailable when FX is incomplete, but native GBP spend is still present.
  - Campaign ROAS appears only when spend + FX + revenue are safe (and is never a
    misleading 0.00x for a zero-revenue campaign).
  - No fake $0 / 0.00x / 0%: a missing deal amount makes that campaign's revenue
    (and ROAS) Unavailable, never a lowered $0; a missing prior period yields
    "No comparison".
  - Revenue that maps to no Google Ads campaign is preserved under "Unattributed
    / Needs Review", never dropped, never forced onto a campaign, and never
    counted in the ROAS numerator.
  - Keyword themes carry NO outcome attribution and NO ROAS (durable data has
    none); search-term panels present the waste-analysis classification only.
  - Frontend: the Campaigns tab exists and activates (Overview stays default),
    is hash-linkable, calls /api/dashboard/campaigns, reuses the PR-134/135/136
    system, renders native GBP (never "$"), never a ROAS on a non-safe row, never
    leaks undefined/null/NaN, and its keyword/search panels state read-only.
  - Read-only: no platform-write verbs in the touched files; the new repository
    fetchers are read-only; building the contract calls no db.writers write fn.

Patches the durable repository boundary (db.revenue_repository) with one coherent
dataset — no real database, no network.
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
    os.path.join(ROOT, "services", "dashboard_campaigns_service.py"), encoding="utf-8"
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


# ── Coherent durable dataset (GBP 10,000 / USD 13,000; 2 mapped campaigns) ────

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
KEYWORD_SNAPSHOT = {"available": True, "run_date": "2026-06-20", "rows": [
    {"campaign_name": "Global Competitors", "ad_group": "comp", "keyword": "competitor alternative",
     "match_type": "phrase", "spend_usd": 1200.0, "clicks": 300, "impressions": 5000, "conversions": 4.0},
    {"campaign_name": "Global Competitors", "ad_group": "sw", "keyword": "freight forwarding software",
     "match_type": "phrase", "spend_usd": 900.0, "clicks": 210, "impressions": 4000, "conversions": 3.0},
]}
SEARCH_SIGNALS = {"available": True, "rows": [
    {"search_term": "freight software pricing", "campaign_name": "Global Competitors",
     "spend_usd": 400.0, "clicks": 60, "conversions": 2.0, "is_flagged_waste": False, "junk_category": None},
    {"search_term": "free logistics jobs", "campaign_name": "Global Competitors",
     "spend_usd": 220.0, "clicks": 90, "conversions": 0.0, "is_flagged_waste": True, "junk_category": "jobs"},
    {"search_term": "logistics meaning", "campaign_name": "Brand - UK",
     "spend_usd": 80.0, "clicks": 30, "conversions": 0.0, "is_flagged_waste": None, "junk_category": None},
]}


def _patch_durable(monkeypatch, *, canonical=None, deals=None, keyword_snapshot=None,
                   search_signals=None, revenue_connected=True):
    import db.revenue_repository as repo

    canon = canonical if canonical is not None else CANONICAL
    dl = deals if deals is not None else DEAL_ROWS
    ks = keyword_snapshot if keyword_snapshot is not None else KEYWORD_SNAPSHOT
    ss = search_signals if search_signals is not None else SEARCH_SIGNALS

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
    monkeypatch.setattr(repo, "fetch_campaign_identity", lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: revenue_connected)
    monkeypatch.setattr(repo, "fetch_revenue_deals", lambda s, e: {"available": True, "rows": list(dl)})
    monkeypatch.setattr(repo, "fetch_keyword_theme_snapshot", lambda: dict(ks))
    monkeypatch.setattr(repo, "fetch_search_term_signals", lambda s, e: dict(ss))
    import db.writers as db_writers
    monkeypatch.setattr(db_writers, "source_attribution_health_counts",
                        lambda: {"classified_contacts": 25, "attributed_deals": 2,
                                 "ambiguous_deals": 0, "unclassified_deals": 1})


def _campaigns(monkeypatch, window=WINDOW, **kw):
    _patch_durable(monkeypatch, **kw)
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    return build_dashboard_campaigns(window, now=NOW)


def _camp(out, name):
    return next((c for c in out["campaigns"] if c["campaign_name"] == name), None)


# ══════════════════ 1. Endpoint + windows + auth ════════════════════════════


def test_endpoint_registered():
    assert '@app.get("/api/dashboard/campaigns")' in SERVER
    assert "build_dashboard_campaigns" in SERVER
    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/dashboard/campaigns" in routes


def test_all_business_windows_accepted(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    for key in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert build_dashboard_campaigns(key, now=NOW)["window"]["key"] == key


def test_invalid_window_raises(monkeypatch):
    _patch_durable(monkeypatch)
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    for bad in ("30d", "60d", "7d", "14d", "", "quarter"):
        with pytest.raises(ValueError):
            build_dashboard_campaigns(bad, now=NOW)


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
    r = client.get("/api/dashboard/campaigns?window=ytd")
    assert r.status_code in (401, 403), r.text


def test_endpoint_accepts_business_window_rejects_ad_window(monkeypatch):
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    _patch_durable(monkeypatch)
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not mint viewer cookie")
    ok = client.get("/api/dashboard/campaigns?window=last_quarter", cookies={"ads_session": cookie})
    assert ok.status_code == 200, ok.text
    assert ok.json()["window"]["key"] == "last_quarter"
    bad = client.get("/api/dashboard/campaigns?window=30d", cookies={"ads_session": cookie})
    assert bad.status_code == 400, bad.text


# ══════════════════ 2. Composition + truth ══════════════════════════════════


def test_read_only_and_conversion_value_never_used(monkeypatch):
    out = _campaigns(monkeypatch)
    assert out["read_only"] is True
    assert out["google_ads_conversion_value_used"] is False
    assert out["source_truth"] == "revenue_decision_mart_campaign_view"
    assert "conversion value" in SERVICE.lower()


def test_kpis_from_google_attributed_truth(monkeypatch):
    out = _campaigns(monkeypatch)
    k = out["kpis"]
    # Native GBP spend prominent, USD only when FX-safe.
    assert k["verified_spend_native"] == {"amount": 10000.0, "currency": "GBP"}
    assert k["verified_spend_usd"] == 13000.0
    assert k["sqls"] == 25  # 20 + 5
    assert k["customers"] == 2  # Google-attributed only (Ghost Co unattributed excluded)
    assert k["won_revenue_usd"] == 29000.0  # Google-attributed only
    assert k["roas"] == round(29000.0 / 13000.0, 2)  # 2.23 — reconciles with canonical mart


def test_kpis_reconcile_with_google_campaign_rows(monkeypatch):
    out = _campaigns(monkeypatch)
    k = out["kpis"]
    google = [c for c in out["campaigns"] if c["attribution_status"] != "unattributed"]
    assert k["sqls"] == sum(c["sqls"] for c in google)
    assert k["customers"] == sum(c["customers"] for c in google)
    assert k["won_revenue_usd"] == round(sum(c["won_revenue_usd"] for c in google), 2)


def test_native_gbp_never_labelled_usd(monkeypatch):
    out = _campaigns(monkeypatch)
    assert out["kpis"]["verified_spend_native"]["currency"] == "GBP"
    for c in out["campaigns"]:
        # native_currency is always GBP; the spend_native value is never a USD figure.
        assert c["native_currency"] == "GBP"


# ══════════════════ 3. Spend / FX / ROAS truth ══════════════════════════════


def test_fx_incomplete_keeps_native_gbp_but_withholds_usd_and_roas(monkeypatch):
    canon = dict(CANONICAL, fx_complete=False, fx_missing_days=5, total_spend_usd=None,
                 rows=[dict(r, spend_usd=None, fx_complete=False) for r in CANONICAL["rows"]])
    out = _campaigns(monkeypatch, canonical=canon)
    k = out["kpis"]
    assert k["verified_spend_native"]["amount"] == 10000.0  # native GBP still present
    assert k["verified_spend_usd"] is None  # USD Unavailable
    assert k["roas"] is None  # ROAS Unavailable
    g = _camp(out, "Global Competitors")
    assert g["spend_native"] == 6000.0 and g["spend_native"] != 0
    assert g["spend_usd"] is None
    assert g["roas"] is None and g["roas_available"] is False


def test_campaign_roas_only_when_spend_and_revenue_safe(monkeypatch):
    out = _campaigns(monkeypatch)
    g = _camp(out, "Global Competitors")
    assert g["roas"] == round(29000.0 / 8000.0, 2) and g["roas_available"] is True
    # A campaign with verified spend but ZERO revenue shows no misleading 0.00x —
    # its status conveys the outcome.
    brand = _camp(out, "Brand - UK")
    assert brand["roas"] is None and brand["roas_available"] is False
    assert brand["status"] == "SQL producer"


def test_no_roas_from_conversion_value(monkeypatch):
    # ROAS is revenue/spend only; the service documents that the Google Ads
    # conversion value is never used, and never reports it as used.
    assert "conversion value" in SERVICE.lower()
    out = _campaigns(monkeypatch)
    assert out["google_ads_conversion_value_used"] is False
    assert out["kpis"]["roas"] == round(29000.0 / 13000.0, 2)  # revenue ÷ spend only


# ══════════════════ 4. No fake $0 / preserve unattributed ════════════════════


def test_null_amount_makes_campaign_revenue_and_roas_unavailable(monkeypatch):
    # A Global Competitors deal with an unknown amount → that campaign's revenue
    # (and ROAS) Unavailable, never a lowered $0. KPI revenue also Unavailable.
    deals = [dict(DEAL_ROWS[0], deal_amount_usd=None)] + DEAL_ROWS[1:]
    out = _campaigns(monkeypatch, deals=deals)
    g = _camp(out, "Global Competitors")
    assert g["won_revenue_usd"] is None and g["won_revenue_usd"] != 0
    assert g["roas"] is None
    assert out["kpis"]["won_revenue_usd"] is None
    assert out["kpis"]["roas"] is None


def test_unattributed_revenue_preserved_and_excluded_from_roas(monkeypatch):
    out = _campaigns(monkeypatch)
    unatt = _camp(out, "Unattributed / Needs Review")
    assert unatt is not None
    assert unatt["won_revenue_usd"] == 4000.0  # revenue kept, never dropped
    assert unatt["attribution_status"] == "unattributed"
    assert unatt["spend_native"] is None and unatt["roas"] is None
    # It is NOT counted in the Google-Ads ROAS numerator.
    assert out["kpis"]["won_revenue_usd"] == 29000.0
    assert out["kpis"]["roas"] == round(29000.0 / 13000.0, 2)


def test_disconnected_integration_blanks_customers_and_revenue(monkeypatch):
    # A disconnected revenue integration must render Unavailable customers/revenue,
    # never a verified $0 / 0-customer proof. Spend + SQLs survive (independent).
    out = _campaigns(monkeypatch, revenue_connected=False)
    k = out["kpis"]
    assert k["customers"] is None
    assert k["won_revenue_usd"] is None
    assert k["roas"] is None
    assert k["verified_spend_native"]["amount"] == 10000.0  # spend survives
    assert k["sqls"] == 25  # SQLs come from contacts and survive
    for c in out["campaigns"]:
        assert c["customers"] is None, f"{c['campaign_name']} customers must be None"
        assert c["won_revenue_usd"] is None and c["roas"] is None
    assert out["truth_status"]["revenue"] == "blocked"


def test_no_fake_deltas_when_previous_baseline_missing(monkeypatch):
    out = _campaigns(monkeypatch, window="all_time")
    pc = out["period_change"]
    assert pc["available"] is False
    assert pc["metrics"] == {}
    assert "period_change" in {u["metric"] for u in out["unavailable"]}


def test_previous_period_unknown_amount_withholds_revenue_delta(monkeypatch):
    # The previous-period revenue baseline counts a null-amount deal as $0 in the
    # mart summary; if the previous window had an unknown amount, the revenue delta
    # must be withheld (No comparison), never measured against a lowered $0.
    import db.revenue_repository as repo
    _patch_durable(monkeypatch)

    def deals_by_window(start, end):
        prev = start is not None and getattr(start, "year", 9999) < 2026
        rows = ([dict(DEAL_ROWS[0], deal_amount_usd=None)] if prev else list(DEAL_ROWS))
        return {"available": True, "rows": rows}

    monkeypatch.setattr(repo, "fetch_revenue_deals", deals_by_window)
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    out = build_dashboard_campaigns("ytd", now=NOW)
    rev = out["period_change"]["metrics"]["won_revenue_usd"]
    assert rev["status"] != "ok"  # baseline withheld → no fabricated growth %


def test_attribution_status_preserves_mapping_distinction(monkeypatch):
    # "matched" → "mapped"; "unavailable" → "mapping_unavailable" (never collapsed
    # to a single "unmapped"); the unattributed callout is its own status.
    out = _campaigns(monkeypatch)
    assert _camp(out, "Global Competitors")["attribution_status"] == "mapped"
    assert _camp(out, "Unattributed / Needs Review")["attribution_status"] == "unattributed"


def test_unavailable_card_copy_accurate_for_healthy_window(monkeypatch):
    # Healthy window (FX verified, revenue complete, ROAS present): the Unavailable
    # card must NOT claim FX/revenue is incomplete — it flags keyword attribution.
    out = _campaigns(monkeypatch)
    card = next(c for c in out["decision_cards"] if c["type"] == "unavailable")
    assert "keyword" in card["body"].lower()
    assert "fx coverage is incomplete" not in card["body"].lower()


def test_fmt_usd_short_none_is_unavailable():
    from services.dashboard_campaigns_service import _fmt_usd_short
    assert _fmt_usd_short(None) == "Unavailable"
    assert _fmt_usd_short(29000) == "$29.0k"
    assert _fmt_usd_short(2_500_000) == "$2.5M"


# ══════════════════ 5. Keyword themes + search terms (read-only, no attr) ════


def test_keyword_themes_never_fake_outcomes_or_roas(monkeypatch):
    out = _campaigns(monkeypatch)
    kt = out["keyword_themes"]
    assert kt["status"] == "ready"
    assert kt["window_scoped"] is False  # honest: recent snapshot, not window-scoped
    assert kt["themes"], "expected keyword themes"
    for t in kt["themes"]:
        assert t["sqls"] is None
        assert t["customers"] is None
        assert t["won_revenue_usd"] is None
        assert t["roas"] is None
        assert t["spend_usd"] is not None  # spend/clicks evidence is real


def test_keyword_snapshot_unavailable_degrades(monkeypatch):
    out = _campaigns(monkeypatch, keyword_snapshot={"available": False, "run_date": None, "rows": []})
    assert out["keyword_themes"]["status"] == "unavailable"
    assert out["keyword_themes"]["themes"] == []
    assert out["truth_status"]["keyword_attribution"] == "unavailable"


def test_search_term_signals_bucketed_by_waste_state(monkeypatch):
    out = _campaigns(monkeypatch)
    s = out["search_term_signals"]
    assert s["status"] == "ready"
    assert [t["search_term"] for t in s["value_terms"]] == ["freight software pricing"]
    assert [t["search_term"] for t in s["waste_terms"]] == ["free logistics jobs"]
    assert [t["search_term"] for t in s["needs_review"]] == ["logistics meaning"]
    # No term claims an outcome/revenue field.
    for bucket in ("value_terms", "waste_terms", "needs_review"):
        for t in s[bucket]:
            assert "won_revenue_usd" not in t and "roas" not in t and "customers" not in t


def test_decision_cards_shape_and_safety(monkeypatch):
    out = _campaigns(monkeypatch)
    types = [c["type"] for c in out["decision_cards"]]
    assert types == ["scale", "watch", "investigate", "unavailable"]
    for c in out["decision_cards"]:
        assert c["headline"] and c["body"]
        assert "$0" not in c["body"]


def test_truth_status_keys(monkeypatch):
    out = _campaigns(monkeypatch)
    ts = out["truth_status"]
    for key in ("spend", "fx", "revenue", "campaign_attribution", "keyword_attribution", "search_terms"):
        assert key in ts
    assert ts["keyword_attribution"] == "unavailable"  # no keyword→outcome attribution
    assert ts["campaign_attribution"] == "partial"  # unattributed revenue present


# ══════════════════ 6. Read-only safety ═════════════════════════════════════


def test_new_repo_fetchers_are_read_only():
    for name in ("fetch_keyword_theme_snapshot", "fetch_search_term_signals"):
        body = _repo_fn_body(name).lower()
        for forbidden in ("insert", "update ", "delete", "upsert", "drop", "alter",
                          "requests.post", "requests.patch", "requests.put"):
            assert forbidden not in body, f"{name} must be read-only ('{forbidden}')"


def test_no_platform_writes_in_touched_files():
    files = [
        "services/dashboard_campaigns_service.py",
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


def test_building_campaigns_invokes_no_write_functions(monkeypatch):
    import db.writers as db_writers
    for name in dir(db_writers):
        if name.startswith(("write_", "upsert_", "record_", "update_", "insert_")):
            attr = getattr(db_writers, name)
            if callable(attr):
                def _boom(*a, _n=name, **k):
                    raise AssertionError(f"unexpected write call: db.writers.{_n}")
                monkeypatch.setattr(db_writers, name, _boom, raising=False)
    _patch_durable(monkeypatch)
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    for key in ("current_quarter", "last_quarter", "ytd", "all_time"):
        build_dashboard_campaigns(key, now=NOW)


# ══════════════════ 7. Frontend: Campaigns tab ══════════════════════════════


def _dashboard_section():
    import re
    m = re.search(r'<section id="page-dashboard".*?</section>', HTML, re.DOTALL)
    assert m, "page-dashboard section missing"
    return m.group(0)


def test_campaigns_tab_enabled_overview_default():
    section = _dashboard_section()
    assert 'data-dash-tab="campaigns"' in section
    assert 'class="dashboard-tab is-active" data-dash-tab="overview"' in section
    import re
    ch = re.search(r'<button[^>]*data-dash-tab="campaigns"[^>]*>', section).group(0)
    assert "disabled" not in ch
    assert 'id="dashboard-campaigns-root"' in section


def test_campaigns_tab_is_hash_linkable():
    assert '"campaigns"' in _slice(JS, "const DASHBOARD_TABS", 120)
    fn = _slice(JS, "function activateDashboardTab", 1500)
    assert "dashboard-campaigns-root" in fn
    assert "loadDashboardCampaigns" in fn


def test_campaigns_tab_calls_its_endpoint():
    loader = _slice(JS, "async function loadDashboardCampaigns", 1400)
    assert "/api/dashboard/campaigns?window=" in loader
    assert "getRoasBusinessWindow()" in loader
    assert "_revReqSeq.dashCampaigns" in loader


def test_campaigns_kpi_cards_render_five_metrics():
    fn = _slice(JS, "function renderCampKpiRow", 4000)
    for label in ("Verified Google Ads Spend", "SQLs from Google Ads", "Customers",
                  "Won Revenue", "Google Ads ROAS"):
        assert label in fn
    assert "dash-kpi-card" in fn


def test_campaigns_reuses_pr134_design_system():
    start = JS.find("// ── Dashboard — Campaigns & Keywords tab")
    end = JS.find("// ── Campaigns page")
    assert start != -1 and end > start, "campaigns block not found"
    block = JS[start:end]
    for cls in ("dash-kpi-card", "dash-panel", "dash-truth-footer",
                "dash-combo-chart", "dash-chart-hit"):
        assert cls in block, f"campaigns tab should reuse PR-134 class {cls}"
    assert "renderDashDecisionCards(" in block
    assert "renderDashSkeleton()" in block
    for lib in ("chart.js", "Chart(", "d3.", "highcharts", "recharts", "echarts"):
        assert lib not in JS


def test_native_gbp_rendered_never_dollar():
    block = JS[JS.find("// ── Dashboard — Campaigns & Keywords tab"):JS.find("// ── Campaigns page")]
    # Native spend uses fmtCompactCurrency / a GBP symbol path, never fmtMoney (USD $).
    assert "fmtCompactCurrency" in block
    assert "campNativeBig" in block
    # The spend cell shows USD only as a secondary, gated on spend_usd presence.
    fn = _slice(JS, "function campSpendCell", 600)
    assert "spend_native" in fn and "spend_usd" in fn


def test_campaign_roas_cell_gated_on_availability():
    fn = _slice(JS, "function campRoasCell", 500)
    assert "roas_available" in fn
    # A non-safe ROAS falls back to the status, never a fabricated multiple.
    assert "camp-status" in fn


def test_campaigns_block_has_no_undefined_or_null_leaks():
    block = JS[JS.find("// ── Dashboard — Campaigns & Keywords tab"):JS.find("// ── Campaigns page")]
    assert ">undefined<" not in block
    assert ">null<" not in block
    assert ">NaN<" not in block


def test_keyword_and_search_panels_state_read_only():
    block = JS[JS.find("// ── Dashboard — Campaigns & Keywords tab"):JS.find("// ── Campaigns page")]
    assert "read-only" in block.lower()
    assert "no platform write" in block.lower()
    # Keyword theme outcome cells render Unavailable, never a number.
    fn = _slice(JS, "function renderCampKeywordThemes", 1600)
    assert "Unavailable" in fn


def test_drawer_renders_proof_columns():
    block = JS[JS.find("const CAMP_DEAL_COLUMNS"):JS.find("const CAMP_DEAL_COLUMNS") + 500]
    for label in ("Company", "Company ID", "Main Contact", "Contact ID", "Deal",
                  "Deal ID", "Amount", "Close Date", "Attribution"):
        assert label in block
    fn = _slice(JS, "function campDealCell", 400)
    assert "dashValue(deal.amount_usd, fmtMoney)" in fn  # null → Unavailable, never $0


def test_campaigns_admin_gated_links_respect_role():
    footer = _slice(JS, "function renderCampTruthFooter", 1200)
    assert "dashCanNavigate(" in footer
    drawer = _slice(JS, "function wireCampDrawers", 3000)
    assert "dashCanNavigate(" in drawer


def test_campaigns_responsive_classes_exist():
    for cls in (".camp-table", ".camp-scroll", ".camp-theme-list", ".camp-signal-grid",
                ".camp-bubble", ".camp-drawer", ".dash-tab-intro"):
        assert cls in CSS, f"{cls} missing from styles.css"
    assert "@media (max-width: 1100px)" in CSS
    assert "@media (max-width: 640px)" in CSS


def test_campaigns_error_and_loading_states_exist():
    assert "Campaigns &amp; Keywords unavailable" in JS
    loader = _slice(JS, "async function loadDashboardCampaigns", 1400)
    assert "renderDashSkeleton()" in loader
    assert "is-refreshing" in loader
