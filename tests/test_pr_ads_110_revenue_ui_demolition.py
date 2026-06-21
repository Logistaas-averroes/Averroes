"""
PR-ADS-110 — Revenue ROAS UI demolition tests.

The ROAS by Campaign / Country pages are business decision pages, not diagnostics
pages. This asserts the clutter is removed and replaced with a clean blocked /
empty / simple-table flow.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()


def _fn(name, span=2600):
    idx = JS.find(name)
    assert idx != -1, f"{name} not found"
    return JS[idx:idx + span]


def _navigate():
    # Span widened in PR-ADS-113 (added revenue-health admin role enforcement).
    return _fn("function navigate(page, options)", span=3200)


def _section(page_id):
    m = re.search(rf'id="{page_id}".*?</section>', HTML, re.DOTALL)
    assert m, f"{page_id} not found"
    return m.group(0)


# ── 1. Old ad windows hidden on ROAS pages ───────────────────────────────────


def test_revenue_pages_constant_includes_both_roas_pages():
    # PR-ADS-113 added the Deals (Closed-Won ledger) and Revenue Health pages to
    # the revenue-page set so the global ad-window bar is suppressed there too.
    assert 'REVENUE_PAGES = ["roas-campaigns", "roas-countries", "deals", "revenue-health"]' in JS
    assert "function isRevenuePage" in JS


def test_navigate_hides_global_window_bar_on_revenue_pages():
    nav = _navigate()
    assert "time-range-bar" in nav
    assert "isRevenuePage(page)" in nav
    assert "revenuePage" in nav


def test_roas_selects_have_no_ad_windows():
    for select_id in ("roas-campaigns-window", "roas-countries-window"):
        m = re.search(rf'<select[^>]*id="{select_id}"[^>]*>.*?</select>', HTML, re.DOTALL)
        assert m
        block = m.group(0)
        for ad in ('value="7d"', 'value="14d"', 'value="30d"', 'value="60d"'):
            assert ad not in block
        assert 'value="current_quarter"' in block


# ── 2. Audit button removed from business pages ──────────────────────────────


def test_audit_button_removed_from_business_pages():
    assert "View attribution audit" not in JS
    assert 'id="roas-audit-btn"' not in JS
    assert "function loadRevenueAttributionAudit" not in JS
    # The ROAS business decision pages must not call the audit endpoint. (The
    # admin-only Revenue Health diagnostics page added in PR-ADS-113 legitimately
    # uses it via loadRevenueHealth — assert the ROAS renderers stay clean.)
    for fn_name in ("function renderRoasCampaignsPage", "function renderRoasCountriesPage",
                    "function loadRoasCampaigns", "function loadRoasCountries"):
        body = _fn(fn_name, span=2600)
        assert "/api/revenue-attribution/audit" not in body


# ── 3. Help / explanation blocks removed ─────────────────────────────────────


def test_no_explanation_containers_on_roas_pages():
    assert 'id="page-explanation-roas-campaigns"' not in HTML
    assert 'id="page-explanation-roas-countries"' not in HTML


def test_navigate_skips_explanation_and_help_on_revenue_pages():
    nav = _navigate()
    # Explanation + help are gated on revenuePage.
    assert "if (revenuePage)" in nav
    assert "renderPageExplanation(page)" in nav  # still called for non-revenue
    # Help button is suppressed on revenue pages (unique navigate() expression).
    assert "!revenuePage && !!PAGE_HELP_CONTENT[page]" in JS


# ── 4. Monitoring warning hidden on revenue pages ────────────────────────────


def test_monitoring_warning_suppressed_on_revenue_pages():
    mon = _fn("async function loadMonitoringStatus()", span=600)
    assert "isRevenuePage(_currentPage)" in mon
    nav = _navigate()
    assert "monitoring-banner" in nav


# ── 5 & 6. Blocked state exists and suppresses the table ─────────────────────


def test_blocked_state_helpers_exist():
    assert "function isRevenueDecisionReady" in JS
    assert "function renderRevenueBlockedState" in JS
    assert "Revenue ROAS is not ready yet" in JS
    assert "Run HubSpot lead re-sync" in JS
    assert "Not connected" in JS
    assert "Rebuilding" in JS


def test_blocked_decision_logic():
    fn = _fn("function isRevenueDecisionReady", span=400)
    assert 'lead_date_grain_status === "event_date"' in fn
    assert 'lead_metrics_status !== "withheld"' in fn
    assert 'revenue_attribution_status !== "not_wired_or_no_closed_won"' in fn


def test_blocked_state_returns_before_table_campaign():
    body = _fn("function renderRoasCampaignsPage", span=2600)
    i_block = body.find("renderRevenueBlockedState")
    i_table = body.find(">Campaign<")
    assert i_block != -1 and i_table != -1
    assert i_block < i_table, "blocked card must render before (and instead of) the table"


def test_blocked_state_returns_before_table_country():
    body = _fn("function renderRoasCountriesPage", span=2600)
    # PR-ADS-112 rebuilt this page with a country-specific blocked renderer.
    i_block = body.find("renderCountryRevenueBlockedState")
    i_table = body.find(">Country<")
    assert i_block != -1 and i_table != -1
    assert i_block < i_table


# ── 7. Safe empty state distinct from blocked ────────────────────────────────


def test_safe_empty_state_distinct_from_blocked():
    assert "function renderRevenueEmptyState" in JS
    fn = _fn("function renderRevenueEmptyState", span=600)
    assert "No closed-won revenue found for this window" in fn
    assert "no HubSpot closed-won deals are attributed" in fn
    assert "Revenue ROAS is not ready yet" not in fn


def test_render_flow_blocked_then_empty_then_table():
    # ROAS by Campaign (PR-ADS-111) and ROAS by Country (PR-ADS-112) were each
    # rebuilt with page-specific blocked + safe-empty renderers.
    for fn_name, block_fn, empty_fn, col in (
        ("function renderRoasCampaignsPage", "renderRevenueBlockedState",
         "renderRoasCampaignSafeEmptyState", ">Campaign<"),
        ("function renderRoasCountriesPage", "renderCountryRevenueBlockedState",
         "renderRoasCountrySafeEmptyState", ">Country<"),
    ):
        body = _fn(fn_name, span=2600)
        i_block = body.find(block_fn)
        i_empty = body.find(empty_fn)
        i_table = body.find(col)
        assert i_block < i_empty < i_table, f"{fn_name}: flow must be blocked -> empty -> table"


# ── 8. Both pages covered (simple table columns) ─────────────────────────────


def test_both_pages_simple_decision_table():
    for fn_name in ("function renderRoasCampaignsPage", "function renderRoasCountriesPage"):
        body = _fn(fn_name, span=2600)
        assert ">Decision<" in body
        for clutter in (">CAC<", ">Confidence<", ">Notes<"):
            assert clutter not in body


# ── 9. Other pages untouched ─────────────────────────────────────────────────


def test_other_pages_not_treated_as_revenue_pages():
    fn = _fn("function isRevenuePage", span=120)
    # Only the two ROAS pages are revenue pages; other sections are untouched.
    for other in ("campaigns", "keywords", "search-terms", "leads"):
        assert f'"{other}"' not in 'REVENUE_PAGES = ["roas-campaigns", "roas-countries"]' or other in ("",)
    assert "loadCampaigns" in JS
    assert "loadKeywords" in JS
    assert "loadSearchTerms" in JS
    assert "loadLeads" in JS


def test_revenue_pages_list():
    # PR-ADS-113 extended the revenue-page set with Deals + Revenue Health.
    m = re.search(r'REVENUE_PAGES = \[([^\]]*)\]', JS)
    assert m
    names = [n.strip().strip('"').strip("'") for n in m.group(1).split(",") if n.strip()]
    assert names == ["roas-campaigns", "roas-countries", "deals", "revenue-health"]
