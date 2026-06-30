"""
PR-ADS-128 — Revenue Mart UI Hardening + Deployment QA.

The mart is the truth (PR-ADS-125/126/127). This PR makes the Revenue &
Attribution section obey one visual + truth contract — UI hardening ONLY:

  1-3. Revenue pages hide the global day-window bar, the global Help button, and
       the global monitoring warning (shared chrome guard), while keeping their
       own business-window selector.
  4.   One shared, clean "Canonical Spend Truth" card across all four pages.
  5.   Revenue tables scroll horizontally instead of clipping the Decision column.
  6.   The Country blocked state explains the exact spend blocker (mismatch vs
       unavailable) with status chips + a matching next step.
  7.   Revenue Health shows a scannable parity summary above the audit table.
  8.   A mart-endpoint failure renders an honest "mart unavailable" card — never a
       silent fallback to the old page endpoints.

No truth/math/sync/schema changes; no Google Ads / HubSpot writes.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()


def _slice(marker, span=1800):
    i = JS.find(marker)
    assert i != -1, f"{marker} not found in app.js"
    return JS[i:i + span]


REVENUE_PAGES_MIN = ["roas-campaigns", "roas-countries", "revenue-by-source", "deals", "revenue-health"]


# ── Test 1 — Revenue pages hide the global day-window bar ────────────────────

def test_revenue_pages_hide_day_windows():
    # The shared revenue-page list drives chrome suppression.
    assert "const REVENUE_PAGES" in JS
    assert "function isRevenuePage" in JS
    decl = _slice("const REVENUE_PAGES", span=300)
    for page in REVENUE_PAGES_MIN:
        assert f'"{page}"' in decl, f"{page} must be a revenue page (chrome suppressed)"
    # The global ad day-window bar is gated behind the revenue-page check.
    chrome = _slice("function applyPageChrome", span=700)
    assert "isRevenuePage(page)" in chrome
    assert "time-range-bar" in chrome
    assert "revenuePage" in chrome  # bar hidden when on a revenue page
    # The business-window selector is NOT removed (revenue pages keep their own).
    assert "VALID_BUSINESS_WINDOWS" in JS
    assert "getRoasBusinessWindow" in JS


# ── Test 2 — Revenue pages hide the global Help button ──────────────────────

def test_revenue_pages_hide_help():
    chrome = _slice("function applyPageChrome", span=700)
    assert "page-help-btn" in chrome
    # Help is hidden specifically when on a revenue page.
    assert re.search(r"helpBtn\s*&&\s*revenuePage", chrome) or "revenuePage" in chrome


# ── Test 3 — Revenue pages hide the global monitoring warning ───────────────

def test_revenue_pages_hide_monitoring_warning():
    chrome = _slice("function applyPageChrome", span=700)
    assert "monitoring-banner" in chrome
    assert re.search(r"monitoringBanner\s*&&\s*revenuePage", chrome) or "revenuePage" in chrome
    # A CSS failsafe also hides chrome on revenue pages even if a JS hook is missed.
    assert "is-revenue-page" in JS


# ── Test 4 — Shared, clean Canonical Spend Truth card ───────────────────────

def test_shared_clean_spend_truth_card():
    assert "function renderMartSpendTruth" in JS
    card = _slice("function renderMartSpendTruth", span=1100)
    for label in ("Canonical Spend Truth", "Native Spend", "USD Reporting",
                  "FX Coverage", "ROAS Basis"):
        assert label in card, f"spend card missing '{label}'"
    # All four business page renderers use the SAME component.
    for page in ("function renderRoasCampaignsPage", "function renderRoasCountriesPage",
                 "function renderRevenueBySourcePage", "function renderRevenueDealsPage"):
        body = _slice(page, span=1800)
        assert "renderMartSpendTruth(" in body, f"{page} must use the shared spend card"


# ── Test 5 — No native-as-USD; Unavailable, never $0 ────────────────────────

def test_no_native_labelled_as_usd():
    card = _slice("function renderMartSpendTruth", span=1100)
    # Native spend uses the native-currency formatter (never "$" on GBP).
    assert "fmtNativeMoney(st.native_spend, st.native_currency)" in card
    # USD reporting is Unavailable when null (never $0, never native).
    assert 'st.usd_spend == null ? "Unavailable"' in card
    # ROAS basis is Unavailable when ROAS is null.
    assert 's.roas == null ? "Unavailable"' in card
    # The card never formats native spend with the USD formatter.
    assert "fmtMoney(st.native_spend" not in card
    # fmtNativeMoney itself routes through the currency formatter, not fmtMoney.
    helper = _slice("function fmtNativeMoney", span=200)
    assert "fmtCurrency(" in helper
    assert "fmtMoney(" not in helper


# ── Test 6 — Revenue table clipping protection ──────────────────────────────

def test_revenue_table_clipping_protection():
    assert "overflow-x: auto" in CSS
    assert ".revenue-decision-table" in CSS
    assert "min-width" in CSS
    # The Decision column (last) has explicit width AND right padding.
    block = _css_block(".revenue-decision-table th:last-child")
    assert re.search(r"min-width:\s*150px", block)
    assert re.search(r"padding-right:\s*24px", block)


def _css_block(selector_fragment):
    i = CSS.find(selector_fragment)
    assert i != -1, f"CSS selector not found: {selector_fragment}"
    brace = CSS.find("{", i)
    end = CSS.find("}", brace)
    return CSS[brace:end + 1]


# ── Test 7 — Country blocked state branches by status ───────────────────────

def test_country_blocked_state_branches_by_status():
    fn = _slice("function renderCountrySpendUnreconciledState", span=1900)
    assert "function renderCountrySpendUnreconciledState(spendTruth)" in JS
    assert 'status === "mismatch"' in fn
    assert "Country ROAS is blocked" in fn
    assert "Country geo spend" in fn          # status chip label
    # Distinct cause copy for the two blockers.
    assert "does not reconcile" in fn         # mismatch
    assert "no canonical Google Ads geo spend" in fn  # unavailable
    # Status chips show the campaign-spend and FX gates too.
    assert "Campaign spend" in fn and "FX coverage" in fn
    # Never a country ROAS table from a blocked state — the guard returns first.
    page = _slice("function renderRoasCountriesPage", span=2600)
    i_guard = page.find('country_spend_status !== "verified"')
    i_table = page.find("renderRoasCountryTable")
    assert i_guard != -1 and i_table != -1 and i_guard < i_table


# ── Test 8 — No old-endpoint fallback on business pages ─────────────────────

def test_no_old_endpoint_fallback():
    cases = [
        ("async function loadRoasCampaigns", "campaign", "/api/revenue-attribution?"),
        ("async function loadRoasCountries", "country", "/api/revenue-attribution?"),
        ("async function loadRevenueBySource", "source", "/api/revenue-by-source?"),
        ("async function loadDeals", "deal", "/api/revenue-deals?"),
    ]
    for loader, view, old_url in cases:
        body = _slice(loader, span=2000)
        assert f'fetchRevenuePerformance("{view}"' in body, f"{loader} must read the mart"
        assert old_url not in body, f"{loader} must not call the legacy endpoint {old_url}"
    assert "function renderMartUnavailable" in JS


# ── Test 9 — Revenue Health parity summary ──────────────────────────────────

def test_revenue_health_parity_summary():
    panel = _slice("function renderRevenuePageParity", span=2800)
    assert "Revenue Page Parity" in panel
    assert "All revenue pages agree with mart" in panel
    assert "Pages with drift" in panel
    for col in (">Page<", ">Metric<", ">Current<", ">Mart<", ">Difference<", ">Status<", ">Reason<"):
        assert col in panel, f"parity table missing column {col}"
    # Driven solely by the audit response — failed rows are not hidden.
    assert "audit.pages" in panel
    assert "all_pages_agree" in panel
    health = _slice("async function loadRevenueHealth", span=2400)
    assert "/api/revenue-performance/audit?window=" in health


# ── Test 10 — Read-only safety ──────────────────────────────────────────────

def test_no_writes_introduced():
    lowered = JS.lower()
    # Platform-write verbs / call shapes that must never appear in the read-only UI.
    # (".mutate(" is the Google Ads write-call form — the bare word "mutate" is a
    # common English term used in benign comments, so we match the call, not the word.)
    for forbidden in (
        ".mutate(", "upload_offline", "offline_conversion",
        "google_ads_client", "hubspot_client",
        "requests.post", "requests.put", "requests.patch", "requests.delete",
        "set_budget", "set_bid", "pause_campaign", "enable_campaign",
    ):
        assert forbidden not in lowered, f"UI must not reference '{forbidden}'"


# ── Mart-unavailable card copy (Change 8) ───────────────────────────────────

def test_mart_unavailable_card_copy():
    card = _slice("function renderMartUnavailable", span=600)
    assert "Canonical revenue mart unavailable" in card
    assert "will not render old page-specific numbers" in card
    assert "Check Revenue Health and System Status" in card
