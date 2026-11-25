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


def _fn(marker):
    """The whole top-level app.js function starting at ``marker`` (to its
    closing ``}`` at column 0) — robust to a body that grows."""
    i = JS.find(marker)
    assert i != -1, f"{marker} not found in app.js"
    j = JS.find("\n}\n", i)
    assert j != -1, f"end of {marker} not found in app.js"
    return JS[i:j + 3]


# ── 1. ROAS by Campaign uses the mart ───────────────────────────────────────

def test_campaign_page_uses_mart():
    # The canonical mart URL is built once in the shared loader helper.
    assert "/api/revenue-performance?view=" in JS
    assert "async function fetchRevenuePerformance" in JS
    # PR-ADS-177B: the one Campaigns page reads the mart's SETTLED campaign
    # view (owner decision 2026-11-24, 2) — still the mart, never a page
    # endpoint, and its rows are the mart's rows.
    loader = _slice("async function loadCampaignsPage", span=2400)
    assert "/api/revenue-performance?view=campaign&settled=true" in loader
    assert "/api/revenue-attribution?" not in loader
    assert "campaignsPageMart = data" in loader
    page = _slice("function renderCampaignsPage()", span=4000)
    assert "data.rows" in page


# ── 2. ROAS by Country uses the mart ────────────────────────────────────────

def test_country_page_uses_mart():
    loader = _slice("async function loadRoasCountries")
    assert 'fetchRevenuePerformance("country"' in loader
    assert "roasCountriesData = data.rows" in loader
    assert "/api/revenue-attribution?" not in loader


# ── 3. Revenue by Source uses the mart ──────────────────────────────────────

# ── 4. Deals uses the mart ──────────────────────────────────────────────────

def test_deals_page_uses_mart():
    # PR-ADS-178B: the deal list is the Pipeline page's; it reads ONE builder,
    # whose deal rows are the mart deal view's rows.
    loader = _slice("async function loadPipeline", span=2000)
    assert "/api/pipeline?" in loader
    assert "/api/revenue-deals?" not in loader
    svc = open(os.path.join(ROOT, "services", "pipeline_service.py"), encoding="utf-8").read()
    assert 'build_revenue_decision_mart(view="deal", window=window, now=now)' in svc
    assert '_deal_list(mart.get("rows") or [], deals_m,' in svc
    # PR-ADS-127: the deal summary reconciles with the table and never mixes the
    # canonical ATTRIBUTION total. PR-ADS-178B: the count, Won Revenue and
    # customers are the mart deal view's all-source summary metrics; the list
    # is shown only when it numbers that count, and the average only when the
    # deal-LEDGER summary (the same source as the rows) counted the same deals.
    assert 'deals_m = _verdict(summary_metrics.get("closed_won_deals"))' in svc
    assert 'ledger = mart.get("ledger_summary") or {}' in svc
    assert "attributed" not in svc


# ── 5. Shared spend-truth / summary / diagnostics across every view ─────────

def test_all_revenue_pages_share_mart_renderers():
    assert "function renderMartSpendTruth" in JS
    assert "function renderMartSummaryStrip" in JS
    assert "function renderMartDiagnostics" in JS
    for page in (
        "function renderCampaignsPage()",
        "function renderRoasCountriesPage",
        # PR-ADS-168B: renderRevenueBySourcePage is deleted with its page.
        # PR-ADS-178B: renderRevenueDealsPage is deleted with its page; the
        # Pipeline page shows no spend (asserted below).
    ):
        body = _slice(page, span=3000)
        if page == "function renderCampaignsPage()":
            # PR-ADS-177B: the one Campaigns page, in the Dashboard's language
            # (the Countries precedent): KPI cards from the mart's
            # campaign_totals, a STRICT spend gate instead of the diagnostic
            # spend card, and a truth footer from the mart's spend_truth.
            own = JS[JS.find(page):JS.find("\n}\n", JS.find(page)) + 3]
            assert "renderCampaignsTruthFooter(data)" in own
            footer = _slice("function renderCampaignsTruthFooter", span=900)
            assert "data.spend_truth" in footer
            assert "renderCampaignsPageSummary(data)" in own
            assert "campaign_totals" in _slice("function renderCampaignsPageSummary", span=600)
            assert "renderCampaignSpendGateState(data)" in own
            continue
        if page == "function renderRoasCountriesPage":
            # PR-ADS-176B: the one Countries page, in the Dashboard's language.
            # The spend card and the summary strip are the account's
            # CAMPAIGN-attributable figures, and the mart's diagnostics are
            # campaign/legacy codes — none describes the country rows. Its
            # truth footer reads the mart's own spend_truth statuses; its
            # totals are the mart's country_totals. Still only mart fields.
            own = JS[JS.find(page):JS.find("\n}\n", JS.find(page)) + 3]
            assert "renderCountriesTruthFooter(data)" in own
            footer = _slice("function renderCountriesTruthFooter", span=900)
            assert "data.spend_truth" in footer
            assert "renderRoasCountrySummary(data)" in own
            continue
        if page == "function renderRevenueDealsPage":
            # PR-ADS-164A: the Deals page lists ALL-SOURCE won deals, so it no
            # longer shows the Google Ads spend card above them (spend lives
            # on the Google Ads pages). Sliced to the function's own end so
            # the assertion is about this renderer, not its neighbours.
            own = JS[JS.find(page):JS.find("\n}\n", JS.find(page)) + 3]
            assert "renderMartSpendTruth(" not in own
        else:
            assert "renderMartSpendTruth(" in body, f"{page} must render the shared spend truth strip"
        assert "renderMartDiagnostics(" in body, f"{page} must render shared diagnostics"
    # The shared strip reads spend_truth + summary; the summary strip reads summary.
    pipeline = JS[JS.find("function renderPipelinePage()"):
                  JS.find("\n}\n", JS.find("function renderPipelinePage()")) + 3]
    assert "renderMartSpendTruth(" not in pipeline and "spend" not in pipeline.lower()
    st = _slice("function renderMartSpendTruth", span=900)
    assert "spend_truth" in st and "usd_spend" in st and "fx_status" in st
    strip = _slice("function renderMartSummaryStrip", span=900)
    assert "data.summary" in strip or "(data && data.summary)" in strip


# ── 6. FX incomplete is respected — Unavailable, never $0 / native-as-USD ────

def test_fx_incomplete_renders_unavailable_not_zero_in_ui():
    st = _slice("function renderMartSpendTruth", span=900)
    # USD reporting + the period ratio basis show "Unavailable" when null — never
    # $0, never GBP-as-USD. (PR-ADS-161E: the ratio is period revenue / ad spend.)
    assert 'st.usd_spend == null ? "Unavailable"' in st
    assert 's.period_revenue_per_ad_spend == null ? "Unavailable"' in st
    # Native spend uses the native-currency formatter (never "$" on GBP).
    assert "fmtNativeMoney(st.native_spend, st.native_currency)" in st
    strip = _fn("function renderMartSummaryStrip")
    assert 's.spend_usd == null ? "Unavailable"' in strip
    # PR-ADS-161E: the period ratio through the mart's canonical metric — a null
    # renders Withheld / Unavailable with the reason, never $0 or 0.00x. The
    # certified ROAS renders through the same helper (always Unavailable).
    assert 'martMetricHtml(s, "period_revenue_per_ad_spend", fmtPeriodRatio)' in strip
    assert 'martMetricHtml(s, "roas", fmtPeriodRatio)' in strip
    helper = _slice("function martMetricHtml", span=600)
    assert "canonicalMetricHtml(m, formatter" in helper
    assert "dashValue(" in helper


def test_fx_incomplete_backend_nulls_usd_and_roas(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    t125._patch_durable(monkeypatch, canonical=t125.CANONICAL_FX_INCOMPLETE)
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = build_revenue_decision_mart(view="campaign", window=t125.WINDOW, now=t125.NOW)
    assert mart["spend_truth"]["usd_spend"] is None
    assert mart["summary"]["spend_usd"] is None
    assert mart["summary"]["period_revenue_per_ad_spend"] is None
    assert mart["summary"]["roas"] is None
    assert mart["readiness"]["spend_decision_ready"] is False
    # Native spend is preserved only in spend_truth.native_spend.
    assert mart["spend_truth"]["native_spend"] == 10000.0


# ── 7. Country mismatch blocks the trusted ROAS table ───────────────────────

def test_country_mismatch_blocks_trusted_table_in_ui():
    body = _slice("function renderRoasCountriesPage", span=3400)
    # Country readiness is the mart's verdict; mismatch/unavailable blocks the table.
    assert 'country_spend_status !== "verified"' in body
    # PR-ADS-128: the card is given the whole spend_truth so its copy + status
    # chips match the cause.
    assert "renderCountrySpendUnreconciledState(st)" in body
    card = _fn("function renderCountrySpendUnreconciledState")
    # PR-ADS-161E: the country ratio is period revenue / ad spend, never ROAS.
    assert "Country Period Revenue / Ad Spend is blocked" in card
    assert "Country ROAS is blocked" not in card
    assert "does not reconcile" in card                 # mismatch wording
    assert "no canonical Google Ads geo spend" in card  # no-geo wording (distinct)
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
    panel = _slice("function renderRevenuePageParity", span=2800)
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
        ("async function loadCampaignsPage", "/api/revenue-attribution?"),
        ("async function loadRoasCountries", "/api/revenue-attribution?"),
        # PR-ADS-168B: loadRevenueBySource is deleted with its page.
    ]
    for loader, old_url in cases:
        body = _slice(loader, span=1500)
        surfaces = "renderMartUnavailable()" in body or '"mart_unavailable"' in body
        assert surfaces, f"{loader} must surface mart unavailability"
        assert old_url not in body, f"{loader} must not call the legacy endpoint {old_url}"
    # PR-ADS-178B: the Pipeline loader (the deal list's) never calls the legacy
    # endpoint, and a failed read renders the unavailable card, no figure.
    loader = _slice("async function loadPipeline", span=1500)
    assert "/api/revenue-deals?" not in loader
    assert 'pipelineStatus = data ? "ok" : "error";' in loader
    page = _slice("function renderPipelinePage()", span=900)
    assert 'if (pipelineStatus !== "ok" || !pipelineData)' in page
    assert "Pipeline unavailable" in page and 'outcomes.innerHTML = "";' in page


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
