"""
PR-ADS-069 — Sidebar UX Grouping & Page Rename
Tests that the sidebar HTML has correct structure, groups, labels, and route keys.
"""
from pathlib import Path
import re

HTML = Path("static/index.html").read_text()

# All required data-page route keys (must never change without a migration PR)
REQUIRED_ROUTE_KEYS = [
    "dashboard",
    "action-queue",
    "reports",
    "campaigns",
    "search-terms",
    "ngrams",
    "geo",
    "keywords",
    "leads",
    "deals",
    "gclid-attribution",
    "waste",
    "opportunities",
    "scheduler",
    "health",
    "backfill",
    "historical-intelligence",
]

NEW_VISIBLE_LABELS = [
    "Search Term Universe",
    "Search Pattern Analysis",
    "Country Performance",
    "Keyword Performance",
    "Flagged Waste Terms",
    "Data Runs",
    "System Status",
    "Admin Backfill",
    "Historical Trends",
]

SIDEBAR_GROUPS = [
    "Command Center",
    "Evidence",
    "Review &amp; Quality",
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
    """No data-page value should appear more than once."""
    matches = re.findall(r'data-page="([^"]+)"', HTML)
    seen = {}
    for m in matches:
        seen[m] = seen.get(m, 0) + 1
    duplicates = {k: v for k, v in seen.items() if v > 1}
    assert not duplicates, f"Duplicate data-page keys: {duplicates}"


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


def test_old_confusing_labels_removed_from_sidebar():
    """Old labels should not appear as nav-item labels in the sidebar."""
    # Extract the sidebar section only
    sidebar_match = re.search(
        r'<nav id="sidebar".*?</nav>', HTML, re.DOTALL
    )
    assert sidebar_match, "Could not find sidebar nav element"
    sidebar = sidebar_match.group(0)

    old_labels_in_sidebar = [
        ">Search Terms<",      # replaced by Search Term Universe
        ">N-Grams<",           # replaced by Search Pattern Analysis
        ">Geo<",               # replaced by Country Performance
        ">Keywords<",          # replaced by Keyword Performance
        ">Waste Terms<",       # replaced by Flagged Waste Terms
        ">Scheduler<",         # replaced by Data Runs
        ">System Health<",     # replaced by System Status
        ">Historical Backfill<",  # replaced by Admin Backfill
        ">Historical Intelligence<",  # replaced by Historical Trends
    ]
    for label in old_labels_in_sidebar:
        assert label not in sidebar, (
            f"Old label still in sidebar: {label}"
        )


def test_section_labels_are_non_clickable():
    """Section labels should use the sidebar__section-label class."""
    sidebar_match = re.search(
        r'<nav id="sidebar".*?</nav>', HTML, re.DOTALL
    )
    assert sidebar_match
    sidebar = sidebar_match.group(0)
    assert 'class="sidebar__section-label"' in sidebar
