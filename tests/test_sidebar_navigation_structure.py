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
    "opportunities",
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
    "In Progress Leads",
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
    "Lead Intelligence",
    "Revenue &amp; Attribution",
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
    assert "Revenue &amp; Attribution" in HTML


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
    """Search Terms page must have tab buttons for search terms and patterns."""
    page_match = re.search(
        r'id="page-search-terms".*?</section>', HTML, re.DOTALL
    )
    assert page_match, "Could not find page-search-terms section"
    page = page_match.group(0)
    assert "search-terms-tab" in page, "Missing tab buttons in Search Terms page"
    assert 'data-tab="search-terms"' in page or "Search Terms" in page
    assert 'data-tab="patterns"' in page or "Patterns" in page


def test_frontend_uses_api_reports_routes():
    """Frontend JS must use /api/reports/... not /reports/..."""
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    # Must contain /api/reports/ references
    assert "/api/reports/roas/campaigns" in js
    assert "/api/reports/roas/countries" in js
    assert "/api/reports/unit-economics" in js
    assert "/api/admin/churn-input" in js
    # Must NOT use bare /reports/ routes for ROAS
    bare_roas = re.findall(r'["\']\/reports\/roas', js)
    assert not bare_roas, f"Found bare /reports/roas routes (should use /api/reports/): {bare_roas}"


def test_country_roas_estimate_warning():
    """Country ROAS page must include the estimate warning copy."""
    page_match = re.search(
        r'id="page-roas-countries".*?</section>', HTML, re.DOTALL
    )
    assert page_match, "Could not find page-roas-countries section"
    page = page_match.group(0)
    assert "Country-level ROAS is an estimate" in page, (
        "Missing country-level estimate warning in ROAS by Country page"
    )


def test_section_labels_are_non_clickable():
    """Section labels should use the sidebar__section-label class."""
    sidebar_match = re.search(
        r'<nav id="sidebar".*?</nav>', HTML, re.DOTALL
    )
    assert sidebar_match
    sidebar = sidebar_match.group(0)
    assert 'class="sidebar__section-label"' in sidebar
