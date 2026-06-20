"""
PR-ADS-107A — Frontend foundation tests for the ROAS pages.

Validates:
  - ROAS by Campaign / Country window dropdowns use business-window labels
  - old ad-style windows (7d/14d/30d/60d/90d/365d) are gone from those dropdowns
  - ROAS pages consume the shared /api/revenue-attribution endpoint
  - active Windsor wording is removed from the two ROAS pages
  - source labels (Google Ads API = spend, HubSpot = revenue truth) are present
  - the new revenue columns and confidence/verdict badges are rendered
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()

BUSINESS_LABELS = ["Current Quarter", "Last Quarter", "Last 6 Months", "YTD", "All Time"]
BUSINESS_KEYS = ["current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"]
AD_WINDOW_OPTION_VALUES = [
    'value="7d"', 'value="14d"', 'value="30d"',
    'value="60d"', 'value="90d"', 'value="365d"',
]


def _select(select_id):
    match = re.search(
        rf'<select[^>]*id="{select_id}"[^>]*>.*?</select>', HTML, re.DOTALL
    )
    assert match, f"{select_id} select not found"
    return match.group(0)


def _section(page_id):
    match = re.search(rf'id="{page_id}".*?</section>', HTML, re.DOTALL)
    assert match, f"{page_id} section not found"
    return match.group(0)


# ── Business window dropdowns ────────────────────────────────────────────────


def test_campaign_dropdown_has_business_labels():
    select = _select("roas-campaigns-window")
    for label in BUSINESS_LABELS:
        assert label in select, f"Missing business window label: {label}"
    for key in BUSINESS_KEYS:
        assert f'value="{key}"' in select, f"Missing business window value: {key}"


def test_country_dropdown_has_business_labels():
    select = _select("roas-countries-window")
    for label in BUSINESS_LABELS:
        assert label in select, f"Missing business window label: {label}"
    for key in BUSINESS_KEYS:
        assert f'value="{key}"' in select


def test_campaign_dropdown_has_no_ad_windows():
    select = _select("roas-campaigns-window")
    for value in AD_WINDOW_OPTION_VALUES:
        assert value not in select, f"Old ad window still present on ROAS campaign page: {value}"


def test_country_dropdown_has_no_ad_windows():
    select = _select("roas-countries-window")
    for value in AD_WINDOW_OPTION_VALUES:
        assert value not in select, f"Old ad window still present on ROAS country page: {value}"


def test_default_selected_window_is_current_quarter():
    for select_id in ("roas-campaigns-window", "roas-countries-window"):
        select = _select(select_id)
        match = re.search(r'<option value="([^"]+)"[^>]*selected', select)
        assert match, f"No default selected option in {select_id}"
        assert match.group(1) == "current_quarter"


# ── Shared endpoint usage ────────────────────────────────────────────────────


def test_roas_loaders_use_revenue_attribution_endpoint():
    for fn in ("loadRoasCampaigns", "loadRoasCountries"):
        idx = JS.find(f"function {fn}")
        assert idx != -1, f"{fn} not found"
        body = JS[idx:idx + 900]
        assert "/api/revenue-attribution" in body, f"{fn} must call /api/revenue-attribution"
        assert "/api/reports/roas/campaigns" not in body
        assert "/api/reports/roas/countries" not in body


def test_business_window_config_present():
    assert "BUSINESS_WINDOWS" in JS
    assert "getRoasBusinessWindow" in JS
    for key in BUSINESS_KEYS:
        assert f'"{key}"' in JS, f"Business window key missing from JS config: {key}"


# ── Windsor wording removed from ROAS pages ──────────────────────────────────


def test_no_windsor_in_roas_campaign_section():
    section = _section("page-roas-campaigns")
    assert "windsor" not in section.lower(), "Windsor wording must be removed from ROAS campaign page"


def test_no_windsor_in_roas_country_section():
    section = _section("page-roas-countries")
    assert "windsor" not in section.lower(), "Windsor wording must be removed from ROAS country page"


def test_no_windsor_in_roas_page_explanations():
    # The roas-campaigns / roas-countries PAGE_EXPLANATIONS entries must not
    # mention Windsor anymore.
    assert "Windsor campaigns" not in JS
    assert "Windsor geo" not in JS


# ── Source labels ────────────────────────────────────────────────────────────


def test_source_labels_present_on_roas_pages():
    for page_id in ("page-roas-campaigns", "page-roas-countries"):
        section = _section(page_id)
        assert "Google Ads API = spend/platform evidence" in section
        assert "HubSpot = revenue truth" in section


# ── New revenue columns + badges ─────────────────────────────────────────────


def test_campaign_render_has_business_columns():
    idx = JS.find("function renderRoasCampaignsPage")
    assert idx != -1
    body = JS[idx:idx + 2500]
    for col in ("Leads", "SQLs", "Customers", "Won Revenue", "ROAS", "CAC", "Confidence", "Verdict"):
        assert f">{col}<" in body, f"Missing campaign column header: {col}"


def test_country_render_has_business_columns():
    idx = JS.find("function renderRoasCountriesPage")
    assert idx != -1
    body = JS[idx:idx + 2500]
    for col in ("Leads", "SQLs", "Customers", "Won Revenue", "ROAS", "CAC", "Top Campaign", "Confidence", "Verdict"):
        assert f">{col}<" in body, f"Missing country column header: {col}"


def test_confidence_and_verdict_badge_helpers_exist():
    assert "function getConfidenceBadge" in JS
    assert "function getBusinessVerdictBadge" in JS
    # Business verdict labels rendered by the badge helper.
    for label in ("Winner", "Watch", "Waste", "Learning"):
        assert f">{label}<" in JS, f"Missing business verdict label: {label}"


def test_empty_state_explains_business_window():
    idx = JS.find("function roasEmptyStateHtml")
    assert idx != -1
    body = JS[idx:idx + 800]
    assert "No attributed revenue found" in body
    assert "no HubSpot closed-won attribution matched" in body
