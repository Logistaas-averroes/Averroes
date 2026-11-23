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
    # PR-ADS-162G: "action-queue" and "reports" were deleted (pages, routes and
    # endpoints); see tests/test_pr_ads_162g_action_queue_reports_retired.py.
    "campaigns",
    "search-terms",
    # PR-ADS-176B: "geo" (the Countries evidence page) was deleted; #/geo
    # redirects to "roas-countries", the one Countries page. See
    # tests/test_pr_ads_176b_countries_one_page.py.
    "keywords",
    "leads",
    "deals",
        "roas-campaigns",
    "roas-countries",
    # PR-ADS-165A: "unit-economics" was deleted (page, route, endpoint and
    # service); see tests/test_pr_ads_165a_weekly_job_and_unit_economics_retired.py.
    "scheduler",
    "health",
    "backfill",
    # PR-ADS-153D: GCLID Attribution is forensic/admin, not an everyday
    # CRM & Revenue destination, so it now sits with the other diagnostics.
    "gclid-attribution",
]

NEW_VISIBLE_LABELS = [
    "Keywords",
    "Countries",
    "Search Terms",
    "Data Runs",
    "System Status",
    "Admin Backfill",
    # PR-ADS-161E: renamed from "ROAS by Campaign" / "ROAS by Country" — a
    # close-date ratio is never called ROAS.
    "Campaign Revenue &amp; Spend",
    # PR-ADS-176B: "Country Revenue &amp; Spend" is merged into the one page
    # titled "Countries" (already listed above).
]

# PR-ADS-162F: the groups were renamed to say what they hold. "Command
# Center", "Platform Evidence" and "CRM & Revenue" became "Overview",
# "Google Ads" and "Sales Results" (the exact membership and order are pinned
# in tests/test_pr_ads_162f_sidebar_groups.py).
# PR-ADS-174A: the four groups were replaced by the six-item menu (pinned in
# tests/test_pr_ads_174a_six_item_menu.py). These are its item labels.
SIDEBAR_GROUPS = [
    "Overview",
    "Channels",
    "Campaigns",
    "Countries",
    "Pipeline",
    "System",
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
    """The menu item labels must exist in the sidebar, in order."""
    sidebar = re.search(r'<nav id="sidebar".*?</nav>', HTML, re.DOTALL).group(0)
    labels = re.findall(r'<span class="nav-item__label">([^<]+)</span>', sidebar)
    assert labels[:len(SIDEBAR_GROUPS)] == SIDEBAR_GROUPS, labels


def test_new_visible_labels_exist():
    """New renamed visible labels must appear in the HTML."""
    for label in NEW_VISIBLE_LABELS:
        assert label in HTML, f"Missing new visible label: {label}"
    # PR-ADS-161E: the old ROAS page labels must not reappear.
    for old in ("ROAS by Campaign", "ROAS by Country"):
        assert old not in HTML, f"Old ROAS page label still visible: {old}"


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
    """The revenue pages must be reachable from the menu. PR-ADS-162F labelled
    the group "Sales Results" (it was "CRM & Revenue"); PR-ADS-174A put each
    Revenue & Spend page in the strip of the item it is about. PR-ADS-176B:
    the country one IS the Countries item — one page, no strip."""
    assert 'data-section="campaigns"' in HTML
    assert 'data-page="roas-campaigns"' in HTML.split('data-section="campaigns"')[1].split("</nav>")[0]
    assert 'data-section="countries"' not in HTML
    assert 'data-page="roas-countries" data-nav-item="countries"' in HTML


def test_roas_pages_exist():
    """ROAS by Campaign and ROAS by Country pages must have containers."""
    assert 'id="page-roas-campaigns"' in HTML
    assert 'id="page-roas-countries"' in HTML


def test_unit_economics_page_is_retired():
    """PR-ADS-165A: the Unit Economics page is deleted by owner decision —
    the section and its sidebar entry are gone, not hidden. (This case used
    to assert the container existed.)"""
    assert 'id="page-unit-economics"' not in HTML
    assert 'data-page="unit-economics"' not in HTML


def test_churn_input_page_is_retired():
    """PR-ADS-162D: the Churn Input page stored a rate no current figure
    read. The page and its sidebar entry are deleted, not hidden."""
    assert 'id="page-churn-input"' not in HTML
    assert 'data-page="churn-input"' not in HTML


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
    /api/revenue-attribution endpoint (business windows). (Churn Input was
    retired in PR-ADS-162D and Unit Economics in PR-ADS-165A.)
    """
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    # ROAS pages use the shared revenue-attribution contract.
    assert "/api/revenue-attribution" in js
    assert "/api/reports/unit-economics" not in js
    assert "/api/admin/churn-input" not in js
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
    """PR-ADS-174A: the six-item menu has no section labels (it had four,
    using the sidebar__section-label class); every sidebar entry is a link."""
    sidebar_match = re.search(
        r'<nav id="sidebar".*?</nav>', HTML, re.DOTALL
    )
    assert sidebar_match
    sidebar = sidebar_match.group(0)
    assert 'class="sidebar__section-label"' not in sidebar
    items = sidebar[sidebar.index('<ul class="sidebar__nav"'):sidebar.index("</ul>")]
    assert items.count("<li>") == 6 == items.count('<a class="nav-item"')
