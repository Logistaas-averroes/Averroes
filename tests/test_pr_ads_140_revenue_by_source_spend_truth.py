"""
PR-ADS-140 — Revenue by Source Spend Truth Audit + Canonical Spend Fix.

Proves the exact production mismatch is fixed: Revenue by Source now shows the
Google Ads spend from the CANONICAL campaign-daily spend truth (the same number
the Revenue Decision Mart shows at the top), NOT the geo/country table. Geo spend
stays diagnostic only.

Reproduces the reported bug: canonical top spend ~$97.3k USD (£72,281.92 GBP)
while the Google Ads source section showed only ~$20.1k (geo-derived). After this
PR the Google Ads source spend equals the canonical mart spend, the audit passes
with no `different_source_table` reason, and ROAS/GBP/USD safety is never loosened.

These tests patch the durable repository boundary and the shared spend-truth
helper — no real database, no network.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
WINDOW = "all_time"

# The exact production numbers from the bug report.
CANONICAL_NATIVE_GBP = 72281.92
CANONICAL_USD = 97300.0
GEO_USD = 20100.0  # the WRONG geo-derived number that must NEVER be used


def _at(d):
    return datetime.fromisoformat(d + "T12:00:00+00:00")


# ── Spend-truth blocks for each business state ──────────────────────────────

def _truth_verified():
    return {
        "state": "verified",
        "google_ads_spend_source": "canonical_campaign_daily_spend",
        "native_currency": "GBP",
        "native_spend": CANONICAL_NATIVE_GBP,
        "usd_spend": CANONICAL_USD,
        "fx_status": "verified",
        "spend_coverage_status": "verified",
        "roas_available": True,
        "geo_spend_used": False,
        "geo_spend_note": "Geo spend is diagnostic and is not used as the source-level "
                          "Google Ads denominator.",
    }


def _truth_fx_incomplete():
    return {
        "state": "fx_incomplete",
        "google_ads_spend_source": "canonical_campaign_daily_spend",
        "native_currency": "GBP",
        "native_spend": CANONICAL_NATIVE_GBP,   # native GBP evidence still present
        "usd_spend": None,
        "fx_status": "incomplete",
        "spend_coverage_status": "verified",
        "roas_available": False,
        "geo_spend_used": False,
        "geo_spend_note": "Geo spend is diagnostic.",
    }


def _truth_coverage_incomplete():
    return {
        "state": "coverage_incomplete",
        "google_ads_spend_source": "canonical_campaign_daily_spend",
        "native_currency": "GBP",
        "native_spend": None,
        "usd_spend": None,
        "fx_status": "verified",
        "spend_coverage_status": "incomplete",
        "roas_available": False,
        "geo_spend_used": False,
        "geo_spend_note": "Geo spend is diagnostic.",
    }


def _patch_source_service(monkeypatch, *, truth, lead_rows=None, revenue_rows=None,
                          geo_usd=GEO_USD):
    """Patch the Revenue by Source dependencies with a coherent dataset.

    ``geo_usd`` is the (wrong) geo/country spend — patched into
    fetch_campaign_country_spend so the test proves it is NEVER used.
    """
    import db.revenue_repository as repo
    import db.writers as writers
    import services.revenue_spend_truth_service as truth_svc

    monkeypatch.setattr(repo, "fetch_source_leads",
                        lambda s, e: {"available": True, "rows": lead_rows or []})
    monkeypatch.setattr(repo, "fetch_source_revenue",
                        lambda s, e: {"available": True, "rows": revenue_rows or []})
    # Geo spend still callable (diagnostic), but the source page must not use it.
    monkeypatch.setattr(
        repo, "fetch_campaign_country_spend",
        lambda s, e: {"available": True, "rows": [{"campaign_name": "x",
                      "country": "US", "spend": geo_usd}]})
    monkeypatch.setattr(truth_svc, "build_google_ads_spend_truth",
                        lambda window, now=None: dict(truth))
    monkeypatch.setattr(writers, "source_attribution_health_counts",
                        lambda: {"contacts_classified": 0, "deals_attributed": 0,
                                 "ambiguous_deals": 0, "unclassified_deals": 0})


def _google(out):
    return next(g for g in out["groups"] if g["group"] == "google_ads")


def _load_build():
    from services.source_attribution_service import build_revenue_by_source
    return build_revenue_by_source


# ════════════════ 1. Canonical spend, not geo spend ════════════════


def test_source_uses_canonical_spend_not_geo(monkeypatch):
    build = _load_build()
    _patch_source_service(
        monkeypatch,
        truth=_truth_verified(),
        revenue_rows=[{"acquisition_group": "google_ads",
                       "attribution_status": "attributed",
                       "deal_amount_usd": 194600.0}],   # -> ROAS 2.0 on 97300
    )
    out = build(WINDOW, now=NOW)
    g = _google(out)
    # The exact production fix: canonical USD, never the geo $20.1k.
    assert g["spend"] == CANONICAL_USD == 97300.0
    assert g["spend"] != GEO_USD
    assert g["spend"] != 20100.0
    assert g["roas"] == 2.0
    # Proof block: canonical source, geo spend explicitly NOT used.
    proof = out["source_spend_truth"]
    assert proof["google_ads_spend_source"] == "canonical_campaign_daily_spend"
    assert proof["geo_spend_used"] is False
    assert proof["native_spend"] == CANONICAL_NATIVE_GBP
    assert proof["usd_spend"] == CANONICAL_USD


def test_source_spend_truth_block_shape(monkeypatch):
    build = _load_build()
    _patch_source_service(monkeypatch, truth=_truth_verified())
    out = build(WINDOW, now=NOW)
    proof = out["source_spend_truth"]
    for key in ("google_ads_spend_source", "native_currency", "native_spend",
                "usd_spend", "fx_status", "spend_coverage_status", "geo_spend_used",
                "geo_spend_note"):
        assert key in proof
    assert proof["native_currency"] == "GBP"
    assert proof["fx_status"] == "verified"
    assert "diagnostic" in proof["geo_spend_note"].lower()


# ════════════════ 2. Audit passes against the mart ════════════════


# Coherent durable dataset so the mart's canonical spend == source spend.
CANONICAL = {
    "available": True, "customer_id": "111", "currency_code": "GBP",
    "reporting_currency": "USD", "fx_complete": True, "fx_missing_days": 0,
    "campaign_count": 1, "total_spend": CANONICAL_NATIVE_GBP,
    "total_spend_usd": CANONICAL_USD,
    "rows": [{"campaign_name": "Brand - UK", "spend": CANONICAL_NATIVE_GBP,
              "spend_usd": CANONICAL_USD}],
}
GEO_TOTAL = {"available": True, "has_rows": True, "total_spend": CANONICAL_NATIVE_GBP,
             "currency_code": "GBP", "customer_id": "111"}
GEO_BY_COUNTRY = {
    "available": True, "has_rows": True, "total_spend": CANONICAL_NATIVE_GBP,
    "total_spend_usd": CANONICAL_USD, "fx_complete": True, "currency_code": "GBP",
    "customer_id": "111", "reporting_currency": "USD",
    "rows": [{"country_criterion_id": "2826", "country_code": "GB",
              "country_name": "United Kingdom", "cost_micros": 72281_920000,
              "spend": CANONICAL_NATIVE_GBP, "spend_usd": CANONICAL_USD,
              "fx_complete": True}],
}
COVERAGE_COMPLETE = [{"chunk_start": "2018-01-01", "chunk_end": "2026-12-31",
                      "status": "verified"}]
WON = [{"campaign_name": "Brand - UK", "country": "United Kingdom",
        "deal_id": "d1", "deal_amount_usd": 194600.0, "match_status": "matched"}]
SOURCE_REV = [{"acquisition_group": "google_ads", "attribution_status": "attributed",
               "deal_amount_usd": 194600.0}]


def _patch_full_durable(monkeypatch):
    """Patch the WHOLE durable boundary so the real mart + real spend-truth helper
    run end-to-end against one coherent dataset (canonical USD == source USD)."""
    import db.revenue_repository as repo
    import db.writers as writers

    monkeypatch.setattr(repo, "fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr(repo, "fetch_campaign_country_spend",
                        lambda s, e: {"available": True, "rows": [
                            {"campaign_name": "Brand - UK", "country": "United Kingdom",
                             "spend": GEO_USD}],  # geo says 20,100 — must be ignored
                            "coverage_start": "2018-01-01", "coverage_end": "2026-06-22"})
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: {"available": True, "rows": [], "event_date_safe": True,
                                      "missing_contact_created_at_count": 0,
                                      "excluded_non_paid_count": 0,
                                      "excluded_pseudo_campaign_count": 0})
    monkeypatch.setattr(repo, "fetch_won_revenue",
                        lambda s, e: {"available": True, "rows": list(WON)})
    monkeypatch.setattr(repo, "fetch_sync_state", lambda: {"available": True, "datasets": {}})
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e: dict(CANONICAL))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total", lambda s, e: dict(GEO_TOTAL))
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_by_country",
                        lambda s, e: dict(GEO_BY_COUNTRY))
    monkeypatch.setattr(repo, "fetch_spend_coverage",
                        lambda s, e: {"available": True, "chunks": list(COVERAGE_COMPLETE)})
    monkeypatch.setattr(repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})
    monkeypatch.setattr(repo, "revenue_integration_connected", lambda: True)
    monkeypatch.setattr(repo, "fetch_source_leads", lambda s, e: {"available": True, "rows": []})
    monkeypatch.setattr(repo, "fetch_source_revenue",
                        lambda s, e: {"available": True, "rows": list(SOURCE_REV)})
    monkeypatch.setattr(writers, "source_attribution_health_counts",
                        lambda: {"contacts_classified": 0, "deals_attributed": 0,
                                 "ambiguous_deals": 0, "unclassified_deals": 0})


def test_audit_revenue_by_source_passes(monkeypatch):
    _patch_full_durable(monkeypatch)
    from services.revenue_decision_mart import build_revenue_performance_audit

    audit = build_revenue_performance_audit(WINDOW, now=NOW)
    by_key = {p["page_key"]: p for p in audit["pages"]}
    src = by_key["revenue_by_source"]
    # Revenue by Source spend equals the mart canonical spend, and passes.
    assert src["current_value"] == src["mart_value"] == CANONICAL_USD
    assert src["current_value"] != GEO_USD
    assert src["status"] == "pass"
    # No different_source_table reason after the fix.
    assert "different_source_table" not in src["reasons"]


def test_source_google_spend_equals_mart_top(monkeypatch):
    _patch_full_durable(monkeypatch)
    from services.revenue_decision_mart import build_revenue_decision_mart

    mart = build_revenue_decision_mart(view="source", window=WINDOW, now=NOW)
    google = next(g for g in mart["rows"] if g["group"] == "google_ads")
    # The Google Ads section spend == the canonical top-line USD spend.
    assert google["spend"] == mart["spend_truth"]["usd_spend"] == CANONICAL_USD
    assert google["spend"] != GEO_USD
    # The mart source view carries the spend-truth proof block.
    assert mart["source_spend_truth"]["geo_spend_used"] is False


def test_audit_no_source_table_reason_even_when_fx_differs(monkeypatch):
    # Even if a source page differs, PR-ADS-140 removes the different_source_table
    # reason (both read the same canonical table now).
    from services.revenue_decision_mart import _difference_reasons
    st = {"fx_status": "incomplete", "campaign_spend_status": "verified",
          "country_spend_status": "verified"}
    reasons = _difference_reasons("revenue_by_source", st, status="fail")
    assert "different_source_table" not in reasons
    assert "different_fx_coverage" in reasons


# ════════════════ 3. FX incomplete case ════════════════


def test_fx_incomplete_withholds_usd_keeps_native(monkeypatch):
    build = _load_build()
    _patch_source_service(
        monkeypatch,
        truth=_truth_fx_incomplete(),
        revenue_rows=[{"acquisition_group": "google_ads",
                       "attribution_status": "attributed", "deal_amount_usd": 5000.0}],
    )
    out = build(WINDOW, now=NOW)
    g = _google(out)
    assert g["spend"] is None            # USD withheld
    assert g["roas"] is None             # ROAS withheld
    # Native GBP still present in the proof block.
    assert out["source_spend_truth"]["native_spend"] == CANONICAL_NATIVE_GBP
    # Status does NOT say "no connected spend source".
    assert g["spend_status_label"] == "FX unavailable — USD ROAS withheld"
    assert "no connected spend source" not in g["spend_status_label"].lower()
    assert g["roas_status"] == "unavailable_fx"


# ════════════════ 4. Spend coverage incomplete case ════════════════


def test_coverage_incomplete_unavailable_no_fake_zero(monkeypatch):
    build = _load_build()
    _patch_source_service(
        monkeypatch,
        truth=_truth_coverage_incomplete(),
        revenue_rows=[{"acquisition_group": "google_ads",
                       "attribution_status": "attributed", "deal_amount_usd": 5000.0}],
    )
    out = build(WINDOW, now=NOW)
    g = _google(out)
    assert g["spend"] is None and g["spend"] != 0    # no fake $0
    assert g["roas"] is None
    assert g["spend_status_label"] == "Canonical spend coverage incomplete"
    assert g["roas_status"] == "unavailable_coverage"


def test_no_google_spend_source_unavailable(monkeypatch):
    build = _load_build()
    _patch_source_service(
        monkeypatch,
        truth={"state": "source_unavailable",
               "google_ads_spend_source": "unavailable", "native_currency": "GBP",
               "native_spend": None, "usd_spend": None, "fx_status": "unavailable",
               "spend_coverage_status": "unavailable", "roas_available": False,
               "geo_spend_used": False, "geo_spend_note": "diagnostic"},
        revenue_rows=[{"acquisition_group": "google_ads",
                       "attribution_status": "attributed", "deal_amount_usd": 5000.0}],
    )
    out = build(WINDOW, now=NOW)
    g = _google(out)
    assert g["spend"] is None and g["roas"] is None
    assert g["spend_status_label"] == "Google Ads spend source unavailable"
    assert g["roas_status"] == "unavailable_no_spend_source"


# ════════════════ 5. Non-Google safety ════════════════


def test_non_google_sources_remain_revenue_only(monkeypatch):
    build = _load_build()
    _patch_source_service(
        monkeypatch,
        truth=_truth_verified(),
        lead_rows=[{"acquisition_group": "other_paid", "status_category": "qualified"},
                   {"acquisition_group": "organic", "status_category": "lead"}],
        revenue_rows=[
            {"acquisition_group": "other_paid", "attribution_status": "attributed",
             "deal_amount_usd": 1000.0},
            {"acquisition_group": "organic", "attribution_status": "attributed",
             "deal_amount_usd": 2000.0},
            {"acquisition_group": "offline", "attribution_status": "attributed",
             "deal_amount_usd": 3000.0},
        ],
    )
    out = build(WINDOW, now=NOW)
    for key in ("other_paid", "organic", "offline", "unclassified"):
        g = next(x for x in out["groups"] if x["group"] == key)
        assert g["spend"] is None, f"{key} must never carry spend"
        assert g["roas"] is None, f"{key} must never show ROAS"
        assert g["roas_status"] == "unavailable_no_spend_source"
    # Revenue is still shown for the non-Google groups.
    assert next(x for x in out["groups"] if x["group"] == "other_paid")["won_revenue"] == 1000.0


# ════════════════ 6. No circular import ════════════════


def test_no_circular_import():
    # Importing the source service AND the mart AND the spend-truth helper together
    # must not raise (revenue_spend_truth_service imports the attribution contract,
    # NEVER the mart, so there is no cycle).
    import importlib
    for mod in ("services.revenue_spend_truth_service",
                "services.source_attribution_service",
                "services.revenue_decision_mart"):
        importlib.import_module(mod)
    import services.source_attribution_service as s
    import services.revenue_decision_mart as m
    # The source service does NOT import the mart (that would be circular).
    src = open(os.path.join(ROOT, "services", "source_attribution_service.py"),
               encoding="utf-8").read()
    assert "revenue_decision_mart" not in src
    truth_src = open(os.path.join(ROOT, "services", "revenue_spend_truth_service.py"),
                     encoding="utf-8").read()
    assert "revenue_decision_mart" not in truth_src
    assert callable(s.build_revenue_by_source) and callable(m.build_revenue_decision_mart)


# ════════════════ 7. Read-only doctrine ════════════════


def test_touched_backend_files_are_read_only():
    forbidden = ("requests.post", "requests.put", "requests.patch", "requests.delete",
                 "upload_offline", "upload_conversions", "create_deal", "update_deal",
                 "create_contact", "update_contact", "set_budget", "set_bid",
                 "pause_campaign", "enable_campaign")
    for rel in ("services/revenue_spend_truth_service.py",
                "services/source_attribution_service.py",
                "services/revenue_decision_mart.py"):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
        for token in forbidden:
            assert token not in text, f"{rel} contains forbidden write '{token}'"


# ════════════════ 8. Frontend ════════════════


def test_frontend_renders_canonical_spend_with_proof_chip():
    # Google Ads section spend reads the canonical group spend + native GBP subline.
    fn = JS[JS.find("function renderSourceGroupSection"):
            JS.find("function renderSourceGroupSection") + 2200]
    assert "source_spend_truth" in JS
    assert "renderSourceSpendProofChip" in fn
    assert "source-spend-native" in fn or "native" in fn
    # The proof chip states the canonical campaign spend source.
    chip = JS[JS.find("function renderSourceSpendProofChip"):
              JS.find("function renderSourceSpendProofChip") + 500]
    assert "canonical campaign spend" in chip
    assert "diagnostic" in chip or "geo_spend_note" in chip


def test_frontend_status_label_covers_all_states():
    fn = JS[JS.find("function sourceStatusLabel"):
            JS.find("function sourceStatusLabel") + 900]
    # Google states use the backend spend_status_label; non-google stays revenue-only.
    assert "spend_status_label" in fn
    assert "Revenue-only — no connected spend source" in fn


def test_frontend_page_passes_spend_truth_to_sections():
    page = JS[JS.find("function renderRevenueBySourcePage"):
              JS.find("function renderRevenueBySourcePage") + 1400]
    assert "source_spend_truth" in page
    assert "renderSourceGroupSection(g, gi, spendTruth)" in page


def test_frontend_no_fake_zero_or_fabricated_values():
    # No fabricated $0 / 0.00x in the Google spend chip: null renders "Unavailable".
    fn = JS[JS.find("function renderSourceGroupSection"):
            JS.find("function renderSourceGroupSection") + 2200]
    assert 'group.spend === null' in fn
    assert '"Unavailable"' in fn
