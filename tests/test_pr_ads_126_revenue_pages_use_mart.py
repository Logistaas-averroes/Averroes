"""
PR-ADS-126 — Revenue pages read from the canonical Revenue Decision Mart.

PR-ADS-125 created the truth (`/api/revenue-performance` + `/api/revenue-performance/audit`).
PR-ADS-126 makes the Revenue & Attribution UI OBEY it. After this PR the four
business pages (ROAS by Campaign, ROAS by Country, Revenue by Source, Deals) load
the mart as their PRIMARY truth, render summary / spend_truth / readiness / rows /
diagnostics, and never re-derive a spend / FX / ROAS / reconciliation / mapping /
source-spend verdict of their own. Revenue Health gains a compact Revenue Page
Parity panel from the audit endpoint. There is NO silent fallback to the old page
endpoints on business pages, no fake $0, no native GBP relabelled as USD, and no
Google Ads / HubSpot writes.

These are mostly static frontend-contract tests (the established pattern, e.g.
test_pr_ads_123) plus a few backend unit checks that the mart actually nulls USD /
ROAS when FX is incomplete and flags a country geo mismatch.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()
MART = open(os.path.join(ROOT, "services", "revenue_decision_mart.py"), encoding="utf-8").read()


def _slice(marker, span=1500):
    """Return ``span`` chars of app.js starting at ``marker`` (declaration body)."""
    i = JS.find(marker)
    assert i != -1, f"{marker} not found in app.js"
    return JS[i:i + span]


# ── 1. ROAS by Campaign uses the mart ───────────────────────────────────────

def test_campaign_page_uses_mart():
    # The canonical mart URL is built once in the shared loader helper.
    assert "/api/revenue-performance?view=" in JS
    assert "async function fetchRevenuePerformance" in JS
    loader = _slice("async function loadRoasCampaigns")
    assert 'fetchRevenuePerformance("campaign"' in loader
    # The campaign page no longer fetches its own page endpoint as primary truth.
    assert "/api/revenue-attribution?" not in loader
    # Campaign rows come from the mart's rows.
    assert "roasCampaignsData = data.rows" in loader


# ── 2. ROAS by Country uses the mart ────────────────────────────────────────

def test_country_page_uses_mart():
    loader = _slice("async function loadRoasCountries")
    assert 'fetchRevenuePerformance("country"' in loader
    assert "roasCountriesData = data.rows" in loader
    assert "/api/revenue-attribution?" not in loader


# ── 3. Revenue by Source uses the mart ──────────────────────────────────────

def test_source_page_uses_mart():
    loader = _slice("async function loadRevenueBySource")
    assert 'fetchRevenuePerformance("source"' in loader
    assert "/api/revenue-by-source?" not in loader
    page = _slice("function renderRevenueBySourcePage", span=900)
    # Source rows ARE the mart rows; only Google Ads carries spend/ROAS.
    assert "data.rows" in page


# ── 4. Deals uses the mart ──────────────────────────────────────────────────

def test_deals_page_uses_mart():
    loader = _slice("async function loadDeals", span=2000)
    assert 'fetchRevenuePerformance("deal"' in loader
    assert "revenueDealsData = data.rows" in loader
    assert "/api/revenue-deals?" not in loader
    # PR-ADS-127: the deal summary (count / Won Revenue / average / GCLID) all come
    # from the mart's deal-LEDGER summary — the same source as the deal rows — so it
    # reconciles with the table and never mixes the canonical attribution total.
    assert "revenueDealsSummary = data.ledger_summary" in loader
    summary = _slice("function martDealLedgerSummary", span=900)
    assert "data.ledger_summary" in summary
    assert "won_revenue_usd" not in summary


# ── 5. Shared spend-truth / summary / diagnostics across every view ─────────

def test_all_revenue_pages_share_mart_renderers():
    assert "function renderMartSpendTruth" in JS
    assert "function renderMartSummaryStrip" in JS
    assert "function renderMartDiagnostics" in JS
    for page in (
        "function renderRoasCampaignsPage",
        "function renderRoasCountriesPage",
        "function renderRevenueBySourcePage",
        "function renderRevenueDealsPage",
    ):
        body = _slice(page, span=1800)
        assert "renderMartSpendTruth(" in body, f"{page} must render the shared spend truth strip"
        assert "renderMartDiagnostics(" in body, f"{page} must render shared diagnostics"
    # The shared strip reads spend_truth + summary; the summary strip reads summary.
    st = _slice("function renderMartSpendTruth", span=900)
    assert "spend_truth" in st and "usd_spend" in st and "fx_status" in st
    strip = _slice("function renderMartSummaryStrip", span=900)
    assert "data.summary" in strip or "(data && data.summary)" in strip


# ── 6. FX incomplete is respected — Unavailable, never $0 / native-as-USD ────

def test_fx_incomplete_renders_unavailable_not_zero_in_ui():
    st = _slice("function renderMartSpendTruth", span=900)
    # USD reporting + ROAS show "Unavailable" when null — never $0, never GBP-as-USD.
    assert 'st.usd_spend == null ? "Unavailable"' in st
    assert 'roas == null ? "Unavailable"' in st
    # Native spend uses the native-currency formatter (never "$" on GBP).
    assert "fmtNativeMoney(st.native_spend, st.native_currency)" in st
    strip = _slice("function renderMartSummaryStrip", span=900)
    assert 's.spend_usd == null ? "Unavailable"' in strip
    assert 's.roas == null ? "Unavailable"' in strip


def test_fx_incomplete_backend_nulls_usd_and_roas(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    t125._patch_durable(monkeypatch, canonical=t125.CANONICAL_FX_INCOMPLETE)
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = build_revenue_decision_mart(view="campaign", window=t125.WINDOW, now=t125.NOW)
    assert mart["spend_truth"]["usd_spend"] is None
    assert mart["summary"]["spend_usd"] is None
    assert mart["summary"]["roas"] is None
    assert mart["readiness"]["spend_decision_ready"] is False
    # Native spend is preserved only in spend_truth.native_spend.
    assert mart["spend_truth"]["native_spend"] == 10000.0


# ── 7. Country mismatch blocks the trusted ROAS table ───────────────────────

def test_country_mismatch_blocks_trusted_table_in_ui():
    body = _slice("function renderRoasCountriesPage", span=2600)
    # Country readiness is the mart's verdict; mismatch/unavailable blocks the table.
    assert 'country_spend_status !== "verified"' in body
    # PR-ADS-127: the card is now given the cause so its copy matches it.
    assert "renderCountrySpendUnreconciledState(st.country_spend_status)" in body
    card = _slice("function renderCountrySpendUnreconciledState", span=1100)
    assert "Country spend coverage incomplete" in card  # mismatch wording
    assert "Country geo spend unavailable" in card       # no-geo wording (distinct)
    # The mismatch guard returns before any trusted table render.
    i_guard = body.find('country_spend_status !== "verified"')
    i_table = body.find("renderRoasCountryTable")
    assert i_guard != -1 and i_table != -1 and i_guard < i_table


def test_country_mismatch_backend_blocks(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    broken_total = {
        "available": True, "has_rows": True,
        "total_cost_micros": 1000_000_000, "total_spend": 1000.0,
        "rows_counted": 1, "country_count": 1,
        "currency_code": "GBP", "customer_id": "111",
    }
    t125._patch_durable(monkeypatch, geo_total=broken_total)
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = build_revenue_decision_mart(view="country", window=t125.WINDOW, now=t125.NOW)
    assert mart["spend_truth"]["country_spend_status"] == "mismatch"
    assert mart["readiness"]["country_decision_ready"] is False


# ── 8. Revenue Health audit panel ───────────────────────────────────────────

def test_revenue_health_renders_parity_panel():
    health = _slice("async function loadRevenueHealth", span=2400)
    assert "/api/revenue-performance/audit?window=" in health
    assert "renderRevenuePageParity" in health
    panel = _slice("function renderRevenuePageParity", span=1800)
    assert "Revenue Page Parity" in panel
    for col in (">Page<", ">Metric<", ">Current<", ">Mart<", ">Difference<", ">Status<", ">Reason<"):
        assert col in panel, f"parity panel missing column {col}"
    # It renders the audit's page rows (current vs mart vs difference vs status).
    assert "p.current_value" in panel and "p.mart_value" in panel and "p.status" in panel


# ── 9. No silent fallback to old endpoints on business pages ────────────────

def test_no_silent_old_endpoint_fallback_on_business_pages():
    assert "function renderMartUnavailable" in JS
    # Each business loader surfaces mart unavailability — directly via
    # renderMartUnavailable(), or (Deals) by setting the "mart_unavailable" status
    # that renderRevenueDealsPage maps to renderMartUnavailable(). Never a silent
    # call to the legacy page endpoint.
    cases = [
        ("async function loadRoasCampaigns", "/api/revenue-attribution?"),
        ("async function loadRoasCountries", "/api/revenue-attribution?"),
        ("async function loadRevenueBySource", "/api/revenue-by-source?"),
        ("async function loadDeals", "/api/revenue-deals?"),
    ]
    for loader, old_url in cases:
        body = _slice(loader, span=1500)
        surfaces = "renderMartUnavailable()" in body or '"mart_unavailable"' in body
        assert surfaces, f"{loader} must surface mart unavailability"
        assert old_url not in body, f"{loader} must not call the legacy endpoint {old_url}"
    # Deals routes its mart_unavailable status to the canonical unavailable card.
    deals_page = _slice("function renderRevenueDealsPage", span=900)
    assert '"mart_unavailable"' in deals_page and "renderMartUnavailable()" in deals_page


# ── 10. No backend writes (Google Ads / HubSpot / budgets / OCT) ────────────

def test_mart_service_has_no_external_writes():
    lowered = MART.lower()
    for forbidden in (
        "execute_action", "mutate", "upload_offline", "offline_conversion",
        "google_ads_client", "googleads", "hubspot_client",
        "requests.post", "requests.put", "requests.patch", "requests.delete",
        "import requests", "db.writers",
        "set_budget", "set_bid", "pause_campaign", "enable_campaign",
    ):
        assert forbidden not in lowered, f"mart must not reference '{forbidden}'"
    assert "read-only" in lowered


def test_mart_client_uses_get_only():
    # The shared mart loader and the parity fetch are GET reads (no method:/body).
    helper = _slice("async function fetchRevenuePerformance", span=400)
    assert "method:" not in helper and "body:" not in helper
    health = _slice("async function loadRevenueHealth", span=2400)
    audit_idx = health.find("/api/revenue-performance/audit")
    assert audit_idx != -1
    # The audit fetch around that call must not be a mutating request.
    around = health[max(0, audit_idx - 120):audit_idx + 200]
    assert "method:" not in around and "POST" not in around


# ── Mart contract backs the "mart decides" design ───────────────────────────

def test_mart_contract_exposes_readiness_for_ui():
    # The frontend renders blocked/ready states from the mart's readiness block,
    # so the backend must own and expose it (decisions move out of the UI).
    assert "def _readiness_block" in MART
    assert '"readiness": readiness' in MART
    for key in (
        "revenue_decision_ready", "spend_decision_ready", "country_decision_ready",
    ):
        assert key in MART


def test_parity_css_present():
    # The compact parity panel + diagnostics have styling (no unstyled raw dump).
    assert ".revenue-diagnostics" in CSS
    assert ".revenue-page-parity" in CSS
    assert ".parity-status" in CSS
