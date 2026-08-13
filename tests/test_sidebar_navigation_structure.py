"""
PR-ADS-080B — Revenue-First Menu Restructure & ROAS Frontend
Tests that the sidebar HTML has correct structure, groups, labels, and route keys.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

# All required data-page route keys (must never change without a migration PR)
REQUIRED_ROUTE_KEYS = [
    "dashboard",
    "action-queue",
    "reports",
    "campaigns",
    "search-terms",
    "geo",
    "keywords",
    "leads",
    "deals",
    "gclid-attribution",
    "waste",
        "roas-campaigns",
    "roas-countries",
    "unit-economics",
    "churn-input",
    "scheduler",
    "health",
    "backfill",
]

NEW_VISIBLE_LABELS = [
    "Keywords",
    "Countries",
    "Search Terms",
    "Flagged Waste Terms",
    "Data Runs",
    "System Status",
    "Admin Backfill",
    "ROAS by Campaign",
    "ROAS by Country",
    "Unit Economics",
    "Churn Input",
]

SIDEBAR_GROUPS = [
    "Command Center",
    "Platform Evidence",
    "CRM &amp; Revenue",
    "Admin",
]

REQUIRED_ADMIN_IDS = [
    "nav-health-item",
    "nav-backfill-item",
]


def test_all_route_keys_exist():
    """Every required data-page key must exist in the HTML."""
    for key in REQUIRED_ROUTE_KEYS:
        assert f'data-page="{key}"' in HTML, f"Missing data-page key: {key}"


def test_no_duplicate_route_keys():
    """No data-page value should appear more than once in the sidebar nav."""
    sidebar_match = re.search(
        r'<nav id="sidebar".*?</nav>', HTML, re.DOTALL
    )
    assert sidebar_match, "Could not find sidebar nav element"
    sidebar = sidebar_match.group(0)
    matches = re.findall(r'data-page="([^"]+)"', sidebar)
    seen = {}
    for m in matches:
        seen[m] = seen.get(m, 0) + 1
    duplicates = {k: v for k, v in seen.items() if v > 1}
    assert not duplicates, f"Duplicate data-page keys in sidebar: {duplicates}"


def test_sidebar_groups_exist():
    """Section group labels must exist in the sidebar."""
    for label in SIDEBAR_GROUPS:
        assert label in HTML, f"Missing sidebar group label: {label}"


def test_new_visible_labels_exist():
    """New renamed visible labels must appear in the HTML."""
    for label in NEW_VISIBLE_LABELS:
        assert label in HTML, f"Missing new visible label: {label}"


def test_admin_ids_still_exist():
    """Admin element IDs must remain for role-visibility logic."""
    for id_val in REQUIRED_ADMIN_IDS:
        assert f'id="{id_val}"' in HTML, f"Missing admin ID: {id_val}"


def test_old_duplicate_menu_items_removed():
    """Old separate Search Term Universe / Search Pattern Analysis menu items must not exist as distinct nav items."""
    sidebar_match = re.search(
        r'<nav id="sidebar".*?</nav>', HTML, re.DOTALL
    )
    assert sidebar_match, "Could not find sidebar nav element"
    sidebar = sidebar_match.group(0)

    old_labels_in_sidebar = [
        ">Search Term Universe<",
        ">Search Pattern Analysis<",
        ">Country Performance<",
        ">Keyword Performance<",
    ]
    for label in old_labels_in_sidebar:
        assert label not in sidebar, (
            f"Old label still in sidebar: {label}"
        )


def test_revenue_attribution_section_exists():
    """Revenue & Attribution section must exist in the sidebar."""
    assert "CRM &amp; Revenue" in HTML


def test_roas_pages_exist():
    """ROAS by Campaign and ROAS by Country pages must have containers."""
    assert 'id="page-roas-campaigns"' in HTML
    assert 'id="page-roas-countries"' in HTML


def test_unit_economics_page_exists():
    """Unit Economics page must have a container."""
    assert 'id="page-unit-economics"' in HTML


def test_churn_input_page_exists():
    """Churn Input page must have a container in Admin."""
    assert 'id="page-churn-input"' in HTML


def test_search_terms_has_tabs():
    """Search Terms page must offer Terms + Patterns tabs.

    PR-ADS-144: the page is rendered entirely by JS into #search-terms-shell,
    so the tab buttons live in the renderSearchTermsShell template in app.js
    rather than static HTML.
    """
    page_match = re.search(
        r'id="page-search-terms".*?</section>', HTML, re.DOTALL
    )
    assert page_match, "Could not find page-search-terms section"
    assert 'id="search-terms-shell"' in page_match.group(0)
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    start = js.find("function renderSearchTermsShell")
    assert start != -1, "renderSearchTermsShell not found in app.js"
    end = js.find("function stActivateTab", start)
    assert end != -1, "stActivateTab not found after renderSearchTermsShell"
    shell = js[start:end]
    assert 'id="tab-btn-terms"' in shell, "Missing Terms tab button"
    assert 'id="tab-btn-patterns"' in shell, "Missing Patterns tab button"
    assert 'data-tab="patterns"' in shell


def test_frontend_uses_api_reports_routes():
    """Frontend JS must use /api/-prefixed routes, not bare /reports/...

    PR-ADS-107A: the ROAS by Campaign / Country pages now consume the shared
    /api/revenue-attribution endpoint (business windows). Unit economics and
    churn-input continue to use their /api/ routes.
    """
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    # ROAS pages use the shared revenue-attribution contract.
    assert "/api/revenue-attribution" in js
    assert "/api/reports/unit-economics" in js
    assert "/api/admin/churn-input" in js
    # Must NOT use bare /reports/ routes for ROAS
    bare_roas = re.findall(r'["\']\/reports\/roas', js)
    assert not bare_roas, f"Found bare /reports/roas routes (should use /api/-prefixed): {bare_roas}"


def test_country_roas_estimate_warning_removed():
    """PR-ADS-110: the big yellow country-level estimate warning is demolished —
    ROAS pages are clean business decision pages."""
    page_match = re.search(
        r'id="page-roas-countries".*?</section>', HTML, re.DOTALL
    )
    assert page_match, "Could not find page-roas-countries section"
    page = page_match.group(0)
    assert "Country-level ROAS is an estimate" not in page
    assert "roas-estimate-warning" not in page


def test_section_labels_are_non_clickable():
    """Section labels should use the sidebar__section-label class."""
    sidebar_match = re.search(
        r'<nav id="sidebar".*?</nav>', HTML, re.DOTALL
    )
    assert sidebar_match
    sidebar = sidebar_match.group(0)
    assert 'class="sidebar__section-label"' in sidebar
