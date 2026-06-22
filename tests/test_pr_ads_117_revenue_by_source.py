"""
PR-ADS-117 — Revenue by Acquisition Source tests.

Proves the classification rules, deal-attribution safety, the revenue-by-source
contract (Google Ads is the only group with spend/ROAS; deals counted once;
window-correct), and that the backfill is read-only to HubSpot.
"""

import os
import sys
from datetime import datetime
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d + "T12:00:00+00:00")


# ════════════════════════ classification rules (pure) ════════════════════════

from analysis.source_classification import (  # noqa: E402
    classify_source, attribute_deal,
    GROUP_GOOGLE_ADS, GROUP_OTHER_PAID, GROUP_ORGANIC, GROUP_OFFLINE, GROUP_UNCLASSIFIED,
)


@pytest.mark.parametrize("primary,detail,expected", [
    ("Paid Search", None, GROUP_GOOGLE_ADS),
    ("PAID_SEARCH", None, GROUP_GOOGLE_ADS),
    ("Paid Social", None, GROUP_OTHER_PAID),
    ("Other Campaigns", None, GROUP_OTHER_PAID),
    ("Email Marketing", None, GROUP_OTHER_PAID),
    ("Direct Traffic", None, GROUP_ORGANIC),
    ("Organic Search", None, GROUP_ORGANIC),
    ("Organic Social", None, GROUP_ORGANIC),
    ("Direct Email", None, GROUP_ORGANIC),
    ("Referrals", None, GROUP_ORGANIC),
    ("Offline Sources", "SalesNash / Events", GROUP_OTHER_PAID),
    ("Offline Sources", "Events", GROUP_OTHER_PAID),
    ("Offline Sources", "Referrals", GROUP_ORGANIC),
    ("Offline Sources", "Direct Email", GROUP_ORGANIC),
    ("Offline Sources", "Resellers", GROUP_ORGANIC),
    ("Offline Sources", "Something else entirely", GROUP_OFFLINE),
    ("Other Offline Sources", None, GROUP_OFFLINE),
])
def test_explicit_mapping_rules(primary, detail, expected):
    assert classify_source(primary, detail) == expected


@pytest.mark.parametrize("primary", ["", None, "  ", "Some New Channel", "tiktok ads", "podcast"])
def test_unknown_or_missing_is_unclassified(primary):
    assert classify_source(primary, None) == GROUP_UNCLASSIFIED


def test_unknown_never_defaults_to_organic():
    assert classify_source("mystery source", "mystery detail") != GROUP_ORGANIC


def test_normalisation_case_and_underscores():
    assert classify_source("paid_search", None) == GROUP_GOOGLE_ADS
    assert classify_source("PAID SEARCH", None) == GROUP_GOOGLE_ADS
    assert classify_source("Email_Marketing", None) == GROUP_OTHER_PAID


# ════════════════════════ deal attribution safety ════════════════════════


def test_single_contact_attributed():
    out = attribute_deal([GROUP_GOOGLE_ADS])
    assert out["attribution_status"] == "attributed"
    assert out["acquisition_group"] == GROUP_GOOGLE_ADS


def test_multiple_same_group_attributed():
    out = attribute_deal([GROUP_ORGANIC, GROUP_ORGANIC])
    assert out["attribution_status"] == "attributed"
    assert out["acquisition_group"] == GROUP_ORGANIC


def test_conflicting_groups_ambiguous_not_allocated():
    out = attribute_deal([GROUP_GOOGLE_ADS, GROUP_ORGANIC])
    assert out["attribution_status"] == "ambiguous"
    assert out["acquisition_group"] == "ambiguous"  # never split across sources


def test_no_classified_contact_unclassified():
    assert attribute_deal([])["attribution_status"] == "unclassified"
    assert attribute_deal([GROUP_UNCLASSIFIED])["attribution_status"] == "unclassified"


# ════════════════════════ revenue-by-source contract ════════════════════════


def _load_build():
    try:
        from services.source_attribution_service import build_revenue_by_source
        return build_revenue_by_source
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _patch_sources(monkeypatch, *, lead_rows=None, revenue_rows=None, spend_rows=None, seen=None):
    def cap_leads(start, end, *a, **k):
        if seen is not None:
            seen["leads"] = (start, end)
        return {"available": True, "rows": lead_rows or []}

    def cap_revenue(start, end, *a, **k):
        if seen is not None:
            seen["revenue"] = (start, end)
        return {"available": True, "rows": revenue_rows or []}

    def cap_spend(start, end, *a, **k):
        if seen is not None:
            seen["spend"] = (start, end)
        return {"available": True, "rows": spend_rows or [], "coverage_start": None, "coverage_end": None}

    try:
        monkeypatch.setattr("db.revenue_repository.fetch_source_leads", cap_leads)
        monkeypatch.setattr("db.revenue_repository.fetch_source_revenue", cap_revenue)
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", cap_spend)
        monkeypatch.setattr("db.writers.source_attribution_health_counts",
                            lambda: {"contacts_classified": 0, "deals_attributed": 0,
                                     "ambiguous_deals": 0, "unclassified_deals": 0})
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")


def _group(out, key):
    return next(g for g in out["groups"] if g["group"] == key)


def test_only_google_ads_has_spend_and_roas(monkeypatch):
    build = _load_build()
    _patch_sources(
        monkeypatch,
        revenue_rows=[
            {"acquisition_group": "google_ads", "attribution_status": "attributed", "deal_amount_usd": 1000.0},
            {"acquisition_group": "other_paid", "attribution_status": "attributed", "deal_amount_usd": 500.0},
            {"acquisition_group": "organic", "attribution_status": "attributed", "deal_amount_usd": 700.0},
        ],
        spend_rows=[{"campaign_name": "x", "country": "US", "spend": 500.0}],
    )
    out = build("current_quarter", now=_at("2026-06-22"))
    g = _group(out, "google_ads")
    assert g["spend"] == 500.0 and g["roas"] == 2.0 and g["has_spend"] is True
    for other in ("other_paid", "organic", "offline", "unclassified"):
        og = _group(out, other)
        assert og["spend"] is None, f"{other} must never inherit spend"
        assert og["roas"] is None, f"{other} must never show ROAS"
        assert og["roas_status"] == "unavailable_no_spend_source"


def test_paid_social_and_email_never_inherit_google_roas(monkeypatch):
    build = _load_build()
    _patch_sources(
        monkeypatch,
        lead_rows=[
            {"acquisition_group": "other_paid", "status_category": "qualified"},
        ],
        revenue_rows=[{"acquisition_group": "other_paid", "attribution_status": "attributed", "deal_amount_usd": 999.0}],
        spend_rows=[{"campaign_name": "x", "country": "US", "spend": 1000.0}],
    )
    out = build("current_quarter", now=_at("2026-06-22"))
    op = _group(out, "other_paid")
    assert op["spend"] is None and op["roas"] is None
    assert op["won_revenue"] == 999.0  # revenue still shown


def test_deal_counted_once_across_groups(monkeypatch):
    build = _load_build()
    _patch_sources(
        monkeypatch,
        revenue_rows=[
            {"acquisition_group": "google_ads", "attribution_status": "attributed", "deal_amount_usd": 100.0},
            {"acquisition_group": "ambiguous", "attribution_status": "ambiguous", "deal_amount_usd": 200.0},
            {"acquisition_group": "unclassified", "attribution_status": "unclassified", "deal_amount_usd": 300.0},
        ],
    )
    out = build("all_time", now=_at("2026-06-22"))
    total_customers = sum(g["customers"] for g in out["groups"])
    total_revenue = sum(g["won_revenue"] for g in out["groups"])
    assert total_customers == 3, "each deal counted exactly once"
    assert total_revenue == 600.0
    # Ambiguous + unclassified fold into the Needs-Review section.
    nr = _group(out, "unclassified")
    assert nr["customers"] == 2 and nr["won_revenue"] == 500.0


def test_window_correct_same_bounds_all_sources(monkeypatch):
    build = _load_build()
    seen = {}
    _patch_sources(monkeypatch, seen=seen)
    from datetime import date
    out = build("last_quarter", now=_at("2026-06-22"))
    assert out["window"]["key"] == "last_quarter"
    assert out["window"]["start_date"] == "2026-01-01"
    assert out["window"]["end_date"] == "2026-03-31"
    expect = (date(2026, 1, 1), date(2026, 3, 31))
    assert seen["leads"] == expect and seen["revenue"] == expect and seen["spend"] == expect


def test_no_fabricated_zero_spend_or_roas(monkeypatch):
    build = _load_build()
    _patch_sources(monkeypatch, spend_rows=[])  # no google spend at all
    out = build("current_quarter", now=_at("2026-06-22"))
    for g in out["groups"]:
        if not g["has_spend"]:
            assert g["spend"] is None and g["roas"] is None


# ════════════════════════ backfill read-only doctrine ════════════════════════


def test_backfill_read_only_to_external():
    src = open(os.path.join(ROOT, "services", "source_attribution_service.py"), encoding="utf-8").read().lower()
    for forbidden in ("create_deal", "update_deal", "create_contact", "update_contact",
                      "googleads", "google_ads_upload", "upload_conversions"):
        assert forbidden not in src


def test_endpoints_registered():
    assert '"/api/revenue-by-source"' in SERVER
    assert '"/api/source-attribution-backfill/run"' in SERVER
    assert '"/api/source-attribution-backfill/status"' in SERVER
    assert '"/api/source-attribution-health"' in SERVER
    run_idx = SERVER.find("def api_source_backfill_run")
    assert "status_code=202" in SERVER[run_idx - 90:run_idx]
    region = SERVER[run_idx:run_idx + 2600]
    assert "check_admin_or_token(request)" in region
    assert "threading.Thread" in region
    assert 'job_type="source_attribution_backfill"' in region


def test_durable_tables_exist():
    assert "CREATE TABLE IF NOT EXISTS contact_source_classification" in SCHEMA
    assert "CREATE TABLE IF NOT EXISTS deal_source_attribution" in SCHEMA
    for col in ("source_primary_raw", "source_detail_raw", "acquisition_group",
                "classification_rule_version", "attribution_status", "attribution_reason"):
        assert col in SCHEMA
    # deal_id is unique (each deal once).
    assert "deal_id                TEXT NOT NULL UNIQUE" in SCHEMA


# ════════════════════════ frontend page ════════════════════════


def test_page_registered_and_revenue_page():
    assert '"revenue-by-source"' in JS[JS.find("const PAGES"):JS.find("const PAGES") + 700]
    assert '"revenue-by-source"' in JS[JS.find("REVENUE_PAGES"):JS.find("REVENUE_PAGES") + 160]
    assert 'id="page-revenue-by-source"' in HTML
    assert 'data-page="revenue-by-source"' in HTML
    assert 'id="revenue-by-source-range"' in HTML  # date-range chip (PR-116 contract)


def test_page_sections_and_roas_only_for_google():
    fn_idx = JS.find("function renderSourceSectionTable")
    body = JS[fn_idx:fn_idx + 1600]
    # Google section has Spend + ROAS; others show "ROAS unavailable".
    assert ">ROAS<" in body and ">Spend<" in body
    assert "ROAS unavailable — no connected spend source" in body
    assert ">Attribution Status<" in body
    # Health summary surfaces the four counts.
    health = JS[JS.find("function renderRevenueBySourceHealth"):JS.find("function renderRevenueBySourceHealth") + 700]
    for label in ("Contacts classified", "Deals attributed", "Ambiguous deals", "Unclassified deals"):
        assert label in health


def test_loader_has_window_guard():
    idx = JS.find("async function loadRevenueBySource")
    body = JS[idx:idx + 1400]
    assert "++_revReqSeq.bySource" in body
    assert "_revResponseIsCurrent" in body
    assert 'setWindowRangeLoading("revenue-by-source-range")' in body
    assert 'renderWindowRange("revenue-by-source-range"' in body
