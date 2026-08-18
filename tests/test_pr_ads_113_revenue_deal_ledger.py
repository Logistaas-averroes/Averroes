"""
PR-ADS-113 — Revenue Deal Ledger + Attribution Health tests.

Two connected revenue surfaces built in one PR:
  1. Deals rebuilt into a Closed-Won Revenue Ledger (deal_close_date truth).
  2. An admin-only Revenue Attribution Health diagnostics page.

Backend coverage: the ledger windows by deal_close_date (never run_date),
dedupes to one latest row per deal, counts closed-won only, totals accurately,
and reports database-unavailable distinctly from a safe-empty window.

Frontend coverage: Deals uses business windows (no day windows), drops the old
funnel, distinguishes unavailable / safe-empty / ready, never fakes $0 / 0.00x;
Health is admin-only, read-only against the audit endpoint, and never shows a
SAFE-but-not-wired window as Revenue Ready. Campaign/Country pages are unchanged.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.canonical_ledger_fixtures import (  # noqa: E402
    canonical_ledger_patch, from_legacy_deal_rows,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
REPO = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()

FIXED_NOW = datetime(2026, 6, 21, tzinfo=timezone.utc)


def _fn(name, span=2600):
    idx = JS.find(name)
    assert idx != -1, f"{name} not found"
    return JS[idx:idx + span]


def _deals_region():
    start = JS.find("let revenueDealsData")
    assert start != -1
    end = JS.find("// ── Revenue Attribution Health", start)
    assert end != -1
    return JS[start:end]


def _health_region():
    start = JS.find("// ── Revenue Attribution Health")
    assert start != -1
    end = JS.find("function junkRateBadge", start)
    assert end != -1
    return JS[start:end]


def _section(page_id):
    m = re.search(rf'id="{page_id}".*?</section>', HTML, re.DOTALL)
    assert m, f"{page_id} not found"
    return m.group(0)


def _repo_fn(name, span=2000):
    idx = REPO.find(name)
    assert idx != -1, f"{name} not found"
    return REPO[idx:idx + span]


def _repo_sql(name, span=2000):
    """Repository function body from the first cur.execute (skips the docstring)."""
    fn = _repo_fn(name, span)
    return fn[fn.find("cur.execute"):]


def _load_build_revenue_deals():
    """Import the service, skipping where the DB driver is absent (local sandbox).

    The production/CI environment ships psycopg2; importing the service there
    succeeds and the functional assertions run for real.
    """
    try:
        from services.revenue_attribution_service import build_revenue_deals
        return build_revenue_deals
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable in this environment: {exc}")


def _load_build_audit():
    try:
        from services.revenue_attribution_service import build_revenue_attribution_audit
        return build_revenue_attribution_audit
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable in this environment: {exc}")


def _patched_audit_repo():
    """Patch the durable repository so the audit builds deterministically."""
    grain = {"available": True, "lead_window_safe": True,
             "lead_event_date_field_available": True,
             "missing_contact_created_at_count": 0, "excluded_non_paid_count": 0,
             "excluded_pseudo_campaign_count": 0, "counts_by_source_type": {}}
    pollution = {"available": True, "pseudo_campaign_rows": [],
                 "email_campaign_rows": [], "zero_spend_campaigns_with_leads": []}
    empty_rows = {"available": True, "rows": [], "coverage_start": None, "coverage_end": None}
    leads = {"available": True, "rows": [], "event_date_safe": True,
             "lead_event_date_field_available": True,
             "missing_contact_created_at_count": 0, "excluded_non_paid_count": 0,
             "excluded_pseudo_campaign_count": 0, "coverage_start": None, "coverage_end": None}
    return patch.multiple(
        "db.revenue_repository",
        fetch_lead_date_grain_health=lambda *a, **k: grain,
        fetch_campaign_pollution_report=lambda *a, **k: pollution,
        fetch_won_revenue=lambda *a, **k: empty_rows,
        fetch_campaign_country_spend=lambda *a, **k: empty_rows,
        fetch_lead_quality=lambda *a, **k: leads,
        fetch_sync_state=lambda *a, **k: {"available": True, "datasets": {}},
    )


# ════════════════════════════ BACKEND ════════════════════════════


def test_repo_ledger_uses_close_date_never_run_date():
    sql = _repo_sql("def fetch_revenue_deals")
    assert "deal_close_date" in sql
    assert "run_date" not in sql, "revenue ledger must NOT window by scheduler run_date"


def test_repo_ledger_one_latest_row_per_deal():
    fn = _repo_fn("def fetch_revenue_deals")
    assert "DISTINCT ON (deal_id)" in fn
    assert "ORDER BY deal_id, created_at DESC" in fn


def test_repo_ledger_closed_won_only():
    fn = _repo_fn("def fetch_revenue_deals")
    # Restricts to the won stage id or a "%won%" label — closed-won only.
    assert "WON_DEAL_STAGE_ID" in fn or "deal_stage = %s" in fn
    assert "deal_stage_label ILIKE %s" in fn


def test_repo_ledger_returns_required_fields():
    fn = _repo_fn("def fetch_revenue_deals")
    for field in ("deal_id", "company", "country", "campaign_name", "deal_close_date",
                  "deal_amount_usd", "deal_stage_label", "match_status", "match_source"):
        assert field in fn, f"ledger row missing field: {field}"


def _deals_payload(rows, available=True):
    return {"available": available, "rows": rows, "table": "gclid_attribution"}


def test_service_database_unavailable_distinct_from_safe_empty():
    build_revenue_deals = _load_build_revenue_deals()

    # Database unavailable.
    with canonical_ledger_patch([], available=False):
        unavailable = build_revenue_deals("current_quarter", now=FIXED_NOW)
    # PR-ADS-153E-B: the reason names WHY the canonical read failed.
    assert unavailable["source_health"]["ledger_status"] == "canonical_ledger_unreadable"
    assert unavailable["legacy_fallback_used"] is False
    assert unavailable["summary"]["won_revenue"] is None
    assert unavailable["deals"] == []

    # Ledger available but empty window (safe-empty) — must be a different status.
    with canonical_ledger_patch([]):
        empty = build_revenue_deals("current_quarter", now=FIXED_NOW)
    assert empty["source_health"]["ledger_status"] == "available"
    assert empty["source_health"]["revenue_attribution_status"] == "no_closed_won_deals_in_window"
    assert empty["summary"]["deal_count"] == 0
    # No fake zero revenue when there are no closed-won deals.
    assert empty["summary"]["won_revenue"] is None
    assert empty["summary"]["average_deal_value"] is None


def test_service_summary_totals_accurate():
    build_revenue_deals = _load_build_revenue_deals()

    rows = [
        {"deal_id": "d1", "company": "Acme", "country": "Saudi Arabia",
         "campaign_name": "gulf", "deal_close_date": "2026-05-01",
         "deal_amount_usd": 10000.0, "deal_stage_label": "Won", "match_status": "matched",
         "match_source": "gclid"},
        {"deal_id": "d2", "company": "Beta", "country": "UAE",
         "campaign_name": "gulf", "deal_close_date": "2026-04-15",
         "deal_amount_usd": 20000.0, "deal_stage_label": "Won", "match_status": "matched",
         "match_source": "gclid"},
        {"deal_id": "d3", "company": "Gamma", "country": "Qatar",
         "campaign_name": "north", "deal_close_date": "2026-06-01",
         "deal_amount_usd": 12000.0, "deal_stage_label": "Won", "match_status": "url_fallback",
         "match_source": "crm_field"},
    ]
    with canonical_ledger_patch(from_legacy_deal_rows(rows)):
        out = build_revenue_deals("all_time", now=FIXED_NOW)

    s = out["summary"]
    assert s["deal_count"] == 3
    assert s["won_revenue"] == 42000.0
    assert s["average_deal_value"] == 14000.0
    # PR-ADS-153E-B: the GCLID subset is now the `gclid_attributable` SCOPE, and
    # the fixture's two `match_source == "gclid"` deals carry a GCLID.
    assert s["exact_gclid_count"] == 2
    assert s["gclid_attributable_deals"] == 2
    assert out["source_health"]["ledger_status"] == "available"
    assert out["source_health"]["revenue_attribution_status"] == "hubspot_deal_ledger"
    # Sorted by close date desc, then revenue desc.
    closes = [d["deal_close_date"] for d in out["deals"]]
    assert closes == sorted(closes, reverse=True)


def test_service_business_windows_differ():
    build_revenue_deals = _load_build_revenue_deals()

    with canonical_ledger_patch([]):
        cq = build_revenue_deals("current_quarter", now=FIXED_NOW)["window"]
        ytd = build_revenue_deals("ytd", now=FIXED_NOW)["window"]
        allt = build_revenue_deals("all_time", now=FIXED_NOW)["window"]

    assert cq["start_date"] == "2026-04-01"
    assert ytd["start_date"] == "2026-01-01"
    assert allt["start_date"] is None  # all_time is unbounded below
    assert cq["start_date"] != ytd["start_date"]


def test_service_rejects_invalid_window():
    build_revenue_deals = _load_build_revenue_deals()
    with canonical_ledger_patch([]):
        with pytest.raises(ValueError):
            build_revenue_deals("30d", now=FIXED_NOW)


def test_endpoint_registered_business_window_only():
    assert '"/api/revenue-deals"' in SERVER
    assert "async def get_revenue_deals(" in SERVER
    assert "from services.revenue_attribution_service import build_revenue_deals" in SERVER
    idx = SERVER.find("async def get_revenue_deals(")
    region = SERVER[idx:idx + 400]
    assert 'default="current_quarter"' in region
    assert 'default="60d"' not in region
    assert "_parse_window(" not in region


# ════════════════════════════ FRONTEND — DEALS ════════════════════════════


def test_deals_uses_business_windows_not_day_windows():
    section = _section("page-deals")
    assert "Closed-Won Deals" in section
    assert "HubSpot closed-won revenue attributed to Google Ads, by deal close date." in section
    for win in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert f'value="{win}"' in section
    for ad in ('value="7d"', 'value="14d"', 'value="30d"', 'value="60d"'):
        assert ad not in section
    # Deals is a revenue page so the global ad-window bar is suppressed.
    assert '"deals"' in JS[JS.find("REVENUE_PAGES"):JS.find("REVENUE_PAGES") + 120]


def test_deals_loader_uses_mart_deal_view():
    # PR-ADS-126: the Deals page reads the canonical mart deal view as PRIMARY
    # truth (fetchRevenuePerformance("deal", ...)) — never /api/deals?days=, and
    # no silent fallback to the legacy /api/revenue-deals page endpoint.
    region = _deals_region()
    assert 'fetchRevenuePerformance("deal"' in region
    assert "/api/deals?days=" not in region
    assert "/api/revenue-deals?window=" not in region


def test_old_funnel_removed():
    assert "DEAL_PIPELINE_STAGES" not in JS
    assert "deals-funnel-body" not in JS
    assert "deals-funnel-body" not in HTML
    assert "funnel-stage" not in JS
    region = _deals_region()
    assert "funnel" not in region.lower()


def test_deals_states_differ():
    region = _deals_region()
    assert "function renderRevenueDealsUnavailable" in region
    assert "function renderRevenueDealsSafeEmptyState" in region
    assert "Closed-won revenue is unavailable" in region
    assert "The durable attribution ledger cannot be read right now." in region
    assert "No closed-won deals found for this window" in region
    # Render flow: unavailable -> safe-empty -> table.
    page = _fn("function renderRevenueDealsPage", span=1200)
    i_unavail = page.find("renderRevenueDealsUnavailable")
    i_empty = page.find("renderRevenueDealsSafeEmptyState")
    i_table = page.find("renderRevenueDealsTable")
    assert -1 < i_unavail < i_empty < i_table


def test_deals_no_fake_zero_or_roas():
    region = _deals_region()
    assert "0.00x" not in region
    assert "$0" not in region
    # The ledger has no ROAS column (it is revenue truth, not a ratio page).
    assert ">ROAS<" not in region


def test_deals_table_columns_and_attribution_labels():
    table = _fn("function renderRevenueDealsTable", span=1400)
    for col in ("Company", "Closed", "Country", "Campaign", "Won Revenue", "Attribution"):
        assert f">{col}<" in table, f"deals table missing column {col}"
    for gone in (">Stage<", ">Keyword<", ">Amount<"):
        assert gone not in table
    badge = _fn("function dealAttributionBadge", span=900)
    assert "Exact GCLID" in badge
    assert "CRM Match" in badge
    assert "URL Match" in badge
    assert "Needs Review" in badge


# ════════════════════════════ FRONTEND — HEALTH ════════════════════════════


def test_health_page_is_admin_only():
    assert '"revenue-health"' in JS[JS.find("const PAGES"):JS.find("const PAGES") + 600]
    # Navigation role enforcement.
    nav = _fn("function navigate(page, options)", span=3400)
    assert 'page === "revenue-health"' in nav
    assert 'role !== "admin"' in nav
    # Nav item is hidden for non-admins.
    assert "nav-revenue-health-item" in JS
    assert "nav-revenue-health-item" in HTML
    show = _fn("function showApp(user)", span=1400)
    assert "nav-revenue-health-item" in show


def test_health_uses_audit_endpoint_read_only():
    region = _health_region()
    assert "/api/revenue-attribution/audit?window=" in region
    # The audit/health diagnostics must be read-only: the audit endpoint is only
    # ever fetched (GET), never POSTed to. (PR-ADS-114 adds an admin Revenue
    # Recovery action that POSTs to a DIFFERENT endpoint, /api/revenue-recovery
    # — that is a local-DB recovery trigger, not an external-platform write.)
    audit_idx = region.find("/api/revenue-attribution/audit")
    around = region[max(0, audit_idx - 200):audit_idx + 200]
    assert "POST" not in around
    assert "/api/revenue-attribution/audit" not in region.replace(
        "/api/revenue-attribution/audit?window=", "", 1
    ) or region.count("/api/revenue-attribution/audit") == 1


def test_health_safe_alone_is_not_revenue_ready():
    region = _health_region()
    # Revenue Ready requires a SAFE verdict AND the integration connected (PR-115
    # split contract): SAFE alone is not enough.
    assert 'audit.verdict === "SAFE"' in region
    assert "integrationConnected" in region
    assert "A SAFE audit verdict alone does not mean Revenue Ready." in region


def test_health_shows_checks_and_rails():
    region = _health_region()
    # PR-ADS-115: "Revenue attribution / Wired" split into integration + window.
    for check in ("Lead date grain", "Revenue integration", "Selected window",
                  "Campaign pollution", "Window integrity"):
        assert check in region, f"missing health check: {check}"
    for rail in ("geo.run_date", "leads.contact_created_at", "gclid_attribution.deal_close_date"):
        assert rail in region, f"missing source-date rail: {rail}"
    for count in ("missing dates", "Excluded non-paid rows", "Excluded pseudo campaigns"):
        assert count in region


# ════════════════════ WINDOW INTEGRITY CORRECTION ════════════════════════


def test_audit_includes_window_ranges():
    build_audit = _load_build_audit()
    with _patched_audit_repo():
        audit = build_audit("current_quarter", now=FIXED_NOW)
    wr = audit.get("window_ranges")
    assert isinstance(wr, dict)
    for k in ("current_quarter", "ytd", "all_time"):
        assert k in wr
        assert "start_date" in wr[k] and "end_date" in wr[k]
    # Resolved by date boundaries (2026-06-21 is Q2).
    assert wr["current_quarter"]["start_date"] == "2026-04-01"
    assert wr["ytd"]["start_date"] == "2026-01-01"
    assert wr["all_time"]["start_date"] is None


def test_health_integrity_uses_date_ranges_not_values():
    region = _health_region()
    # Date-boundary based, not value-signature based.
    assert "window_ranges" in region
    assert "rangeSignatures" in region
    assert "new Set(rangeSignatures)" in region
    # The old value-signature integrity test must be gone.
    assert "${s.won_revenue}|${s.leads}" not in region
    assert "anyData" not in region
    # The page states windows differ by date boundaries, values may be equal.
    assert "differ by their date boundaries" in region


def _extract_integrity_snippet():
    region = _health_region()
    m = re.search(
        r"const ranges = audit\.window_ranges.*?const windowIntegrityOk = .*?;",
        region, re.DOTALL,
    )
    assert m, "could not locate window-integrity computation"
    return m.group(0)


def _eval_window_integrity(audit):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to evaluate frontend integrity logic")
    snippet = _extract_integrity_snippet()
    script = (
        f"const audit = {json.dumps(audit)};\n"
        "const dg = audit.date_grain_health || {};\n"
        "const pollutionCount = 0;\n"
        f"{snippet}\n"
        "console.log(JSON.stringify(windowIntegrityOk));"
    )
    out = subprocess.check_output([node, "-e", script]).decode().strip()
    return json.loads(out)


def test_window_integrity_ok_when_values_equal_but_ranges_differ():
    # All three summaries identical, but the resolved date ranges differ.
    identical = {"spend": 1000, "won_revenue": 5000, "leads": 10}
    audit = {
        "revenue_attribution_wired": True,
        "window_ranges": {
            "current_quarter": {"start_date": "2026-04-01", "end_date": "2026-06-21"},
            "ytd": {"start_date": "2026-01-01", "end_date": "2026-06-21"},
            "all_time": {"start_date": None, "end_date": "2026-06-21"},
        },
        "window_comparison": {
            "current_quarter": dict(identical),
            "ytd": dict(identical),
            "all_time": dict(identical),
        },
    }
    assert _eval_window_integrity(audit) is True, (
        "identical totals with differing date ranges must NOT warn"
    )


def test_window_integrity_warns_only_when_ranges_collapse():
    # Genuine bug: windows fail to resolve to distinct date ranges.
    same_range = {"start_date": "2026-04-01", "end_date": "2026-06-21"}
    audit = {
        "window_ranges": {
            "current_quarter": dict(same_range),
            "ytd": dict(same_range),
            "all_time": dict(same_range),
        },
        "window_comparison": {},
    }
    assert _eval_window_integrity(audit) is False


# ════════════════════════════ NON-REGRESSION ════════════════════════════


def test_campaign_and_country_pages_unchanged():
    # ROAS pages still render their multiple-notation tables and are not coupled
    # to the new revenue-deals surfaces.
    camp = _fn("function renderRoasCampaignTable", span=1600)
    country = _fn("function renderRoasCountryTable", span=1600)
    assert "fmtRoasMultiple(r.roas)" in camp
    assert "fmtRoasMultiple(r.roas)" in country
    assert "revenue-deals" not in camp
    assert "revenue-deals" not in country
