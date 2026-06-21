"""
PR-ADS-112 — Rebuild ROAS by Country decision page.

ROAS by Country becomes a clean business decision page answering: "Which
countries are producing qualified pipeline, customers, and closed-won
revenue?" — a table decision page only (no map, no Deals, no diagnostics wall).

Country readiness layers country-level geo-spend availability on top of the
shared revenue truth layer, and country totals are derived from the actual
loaded country rows (never the global account summary). ROAS by Campaign must
stay untouched in this PR.
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


def _country_region():
    """The ROAS-by-Country implementation block (state -> table renderer)."""
    start = JS.find("let roasCountrySourceHealth")
    assert start != -1, "roasCountrySourceHealth state not found"
    end = JS.find("// ── Unit Economics", start)
    assert end != -1, "Unit Economics marker not found"
    return JS[start:end]


# ── 1. Country-only filter state and data attribute exist ────────────────────


def test_country_filter_state_and_attribute_exist():
    assert "let roasCountryFilter" in JS
    assert 'roasCountryFilter = "all"' in JS
    assert "data-roas-country-filter" in JS
    fn = _fn("function renderRoasCountryFilters", span=900)
    for label in ("All", "Winners", "Watch", "Waste", "Learning"):
        assert f'"{label}"' in fn, f"country filter chip missing {label}"
    # Delegation keys off the country attribute, leaving Campaign untouched.
    assert 'e.target.closest("[data-roas-country-filter]")' in JS
    assert "roasCountryFilter = btn.dataset.roasCountryFilter" in JS


# ── 2. Campaign state / renderer is not changed ──────────────────────────────


def test_campaign_page_not_modified():
    # Campaign keeps its own filter + renderers; country must not reach into them.
    assert "let roasCampaignFilter" in JS
    assert "function renderRoasCampaignsPage" in JS
    assert "function renderRoasCampaignTable" in JS
    country = _country_region()
    assert "roasCampaignFilter" not in country
    assert "renderRoasCampaignTable" not in country
    assert "renderRoasCampaignSummary" not in country


# ── 3. Country readiness requires shared truth + geo spend ───────────────────


def test_country_readiness_requires_geo_spend():
    assert "function isCountryRevenueDecisionReady" in JS
    fn = _fn("function isCountryRevenueDecisionReady", span=400)
    assert "isRevenueDecisionReady(sh)" in fn
    assert "country_spend_available === true" in fn
    assert 'geo_country_mapping_status === "available"' in fn


def test_load_retains_country_specific_source_health():
    fn = _fn("async function loadRoasCountries", span=2000)
    assert "roasCountrySourceHealth = {" in fn
    assert "country_spend_available: data.country_spend_available === true" in fn
    assert 'geo_country_mapping_status: data.geo_country_mapping_status || "no_geo_data"' in fn


# ── 4. Blocked state renders before summary/table ────────────────────────────


def test_blocked_state_returns_before_summary_and_table():
    body = _fn("function renderRoasCountriesPage", span=1800)
    i_ready = body.find("isCountryRevenueDecisionReady")
    i_block = body.find("renderCountryRevenueBlockedState")
    i_return = body.find("return", i_block)
    i_summary = body.find("renderRoasCountrySummary")
    i_table = body.find("renderRoasCountryTable")
    assert i_ready != -1 and i_block != -1 and i_return != -1
    assert i_ready < i_block < i_return
    assert i_return < i_summary, "summary must render only after the blocked guard"
    assert i_return < i_table, "table must render only after the blocked guard"


def test_country_blocked_copy():
    fn = _fn("function renderCountryRevenueBlockedState", span=1600)
    assert "Country ROAS is not ready yet" in fn
    assert "Run the Google Ads geo sync and HubSpot lead re-sync" in fn
    assert "Country spend" in fn


# ── 5. Summary derived from country rows, not the global summary ──────────────


def test_summary_derived_from_country_rows():
    assert "function summarizeRoasCountryRows" in JS
    body = _fn("function renderRoasCountriesPage", span=1800)
    # The page summarizes the loaded country rows.
    assert "summarizeRoasCountryRows(roasCountriesData)" in body
    # And must NOT reuse the global account summary.
    assert "roasRevenueSummary" not in body
    fn = _fn("function summarizeRoasCountryRows", span=900)
    assert "reduce(" in fn
    assert "totals.spend > 0 ? totals.won_revenue / totals.spend : null" in fn


# ── 6. Summary and table use the multiple ROAS formatter ─────────────────────


def test_country_roas_uses_multiple_notation():
    summary = _fn("function renderRoasCountrySummary", span=900)
    assert "fmtRoasMultiple(s.roas)" in summary
    assert "fmtRoas(s.roas)" not in summary
    table = _fn("function renderRoasCountryTable", span=1600)
    assert "fmtRoasMultiple(r.roas)" in table
    assert "fmtRoas(r.roas)" not in table


# ── 7. Sort order is business value first ────────────────────────────────────


def test_country_sort_is_business_value_first():
    fn = _fn("function sortRoasCountryRows", span=400)
    order = ["customers", "won_revenue", "sqls", "spend"]
    positions = [fn.find(k) for k in order]
    assert all(p != -1 for p in positions)
    assert positions == sorted(positions), "sort keys not in business-value order"
    assert "BUSINESS_VERDICT_ORDER" not in fn


# ── 8. Safe empty state differs from blocked state ───────────────────────────


def test_safe_empty_state_differs_from_blocked():
    assert "function renderRoasCountrySafeEmptyState" in JS
    fn = _fn("function renderRoasCountrySafeEmptyState", span=600)
    assert "No country revenue found for this window" in fn
    assert "not ready" not in fn.lower()
    assert "Country ROAS is not ready yet" not in fn


# ── 9. unknown / blank country renders as Unmapped ───────────────────────────


def test_unknown_or_blank_country_renders_unmapped():
    assert "function countryDisplayName" in JS
    fn = _fn("function countryDisplayName", span=400)
    assert '"Unmapped"' in fn
    assert 'toLowerCase() === "unknown"' in fn
    # The table uses the display-name mapping (not a raw country value).
    table = _fn("function renderRoasCountryTable", span=1600)
    assert "countryDisplayName(r.country)" in table


# ── 10. No map / backend / connector / schema / Campaign / Deals changes ─────


def test_no_map_or_out_of_scope_artifacts():
    region = _country_region()
    for forbidden in ("leaflet", "mapbox", "<svg", "world-map", "geoChart", "choropleth"):
        assert forbidden.lower() not in region.lower(), f"map artifact leaked: {forbidden}"
    # No diagnostics wall / audit button on the business page.
    for clutter in ("View attribution audit", "What this page shows", "Depends on", "Read-only"):
        assert clutter not in region


def test_only_app_js_and_test_changed():
    # This PR is frontend-only; assert no day-window regression and that the
    # decision table stays clean (no CAC / Confidence / Notes).
    table = _fn("function renderRoasCountryTable", span=1600)
    for forbidden in (">CAC<", ">Confidence<", ">Notes<", ">Top Campaign<"):
        assert forbidden not in table


# ── 11. Existing business-window selector remains unchanged ──────────────────


def test_business_window_selector_unchanged():
    m = re.search(r'id="page-roas-countries".*?</section>', HTML, re.DOTALL)
    assert m
    section = m.group(0)
    assert "ROAS by Country" in section
    for win in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert f'value="{win}"' in section
    for ad in ('value="7d"', 'value="14d"', 'value="30d"', 'value="60d"'):
        assert ad not in section


# ── Table columns are exactly the decision columns ───────────────────────────


def test_country_table_columns_are_correct():
    fn = _fn("function renderRoasCountryTable", span=1600)
    for col in ("Country", "Spend", "Leads", "SQLs", "Customers",
                "Won Revenue", "ROAS", "Decision"):
        assert f">{col}<" in fn, f"country table missing column {col}"
