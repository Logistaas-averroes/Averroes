"""
PR-ADS-071/083A — Page Help Drawer & Revenue Snapshot History UI
Tests that:
- Help drawer exists in HTML
- Help drawer has content for all page keys
- ROAS help copy mentions HubSpot revenue, not Google Ads conversion value
- Country ROAS help says estimate until GCLID is wired
- GCLID page help says readiness/confidence audit only
- Snapshot History UI calls correct API endpoints
- No bare non-api snapshot routes
- Existing menu/nav tests still pass (covered by test_sidebar_navigation_structure.py)
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")


# ── Help Drawer Structure ──────────────────────────────────────────────────

def test_help_drawer_exists_in_html():
    """Help drawer element must exist in the HTML."""
    assert 'id="help-drawer"' in HTML
    assert 'id="help-drawer-overlay"' in HTML
    assert 'id="help-drawer-body"' in HTML
    assert 'id="help-drawer-close-btn"' in HTML


def test_help_button_exists():
    """Page help button must exist in the time-range-bar area."""
    assert 'id="page-help-btn"' in HTML


def test_help_drawer_css_exists():
    """Help drawer CSS classes must be defined."""
    assert ".help-drawer" in CSS
    assert ".help-drawer__header" in CSS
    assert ".help-drawer__body" in CSS
    assert ".help-drawer__section" in CSS


# ── Help Content Coverage ──────────────────────────────────────────────────

REQUIRED_HELP_PAGES = [
    "dashboard",
    # PR-ADS-162G: "action-queue" and "reports" were deleted (pages, routes and
    # endpoints); see tests/test_pr_ads_162g_action_queue_reports_retired.py.
    "campaigns",
    "search-terms",
    # PR-ADS-177C: "keywords" is the Keywords tab of the one Keywords & Search
    # Terms page; #/keywords redirects there and its help lives under
    # "search-terms" (above).
    # PR-ADS-176B: "geo" was deleted with its help entry; Countries is
    # "roas-countries" (below).
    # PR-ADS-178B: "leads", "opportunities" and "deals" are one page,
    # "pipeline" (each old key redirects to it).
    "pipeline",
    # PR-ADS-177B: "roas-campaigns" was merged into "campaigns" (above).
    "roas-countries",
    "gclid-attribution",
    # PR-ADS-165A: "unit-economics" was deleted with its page.
    "scheduler",
    "health",
    "backfill",
]


def test_page_help_content_covers_all_pages():
    """PAGE_HELP_CONTENT must have entries for all required pages."""
    content_start = JS.find("const PAGE_HELP_CONTENT = {")
    assert content_start != -1, "Could not find PAGE_HELP_CONTENT declaration"
    object_start = JS.find("{", content_start)
    assert object_start != -1, "Could not find PAGE_HELP_CONTENT object start"

    depth = 0
    object_end = -1
    for idx, ch in enumerate(JS[object_start:], start=object_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                object_end = idx
                break
    assert object_end != -1, "Could not find PAGE_HELP_CONTENT object end"

    help_block = JS[object_start + 1:object_end]

    for key in REQUIRED_HELP_PAGES:
        if re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", key):
            key_pattern = rf"^\s*{re.escape(key)}\s*:"
        else:
            key_pattern = rf'^\s*["\']{re.escape(key)}["\']\s*:'
        assert re.search(key_pattern, help_block, re.MULTILINE), (
            f"Missing PAGE_HELP_CONTENT entry for page: {key}"
        )


def test_help_content_has_all_fields():
    """Each PAGE_HELP_CONTENT entry must have all 5 required fields."""
    required_fields = ["what", "source", "howToUse", "doNotAssume", "checkNext"]
    # PR-ADS-153D retired the standalone Flagged Waste Terms page, so there is
    # one fewer help page. The count is derived from the page list this file
    # already asserts rather than being a hand-maintained magic number.
    expected = len(REQUIRED_HELP_PAGES)
    for field in required_fields:
        count = JS.count(f"{field}:")
        assert count >= expected, (
            f"Field '{field}' appears only {count} times — expected at least "
            f"{expected} (one per help page)"
        )


# ── Critical Help Copy Assertions ──────────────────────────────────────────

def test_roas_campaigns_help_mentions_hubspot_revenue():
    """The campaign revenue help must mention HubSpot revenue. PR-ADS-177B:
    Campaign Revenue & Spend's help is the one Campaigns page's (`campaigns`)."""
    assert "HubSpot" in JS
    help_block = JS[JS.find("const PAGE_HELP_CONTENT"):]
    roas_start = help_block.find("\n  campaigns: {")
    assert roas_start != -1, "campaigns key not found in PAGE_HELP_CONTENT"
    roas_section = help_block[roas_start:]
    roas_end = roas_section.find('"search-terms"')
    if roas_end != -1:
        roas_section = roas_section[:roas_end]
    assert "HubSpot" in roas_section, "ROAS by Campaign help must mention HubSpot"
    assert "does NOT use Google Ads conversion value" in roas_section or \
           "not Google Ads conversion" in roas_section, \
        "ROAS by Campaign help must state it does not use Google Ads conversion value"


def test_roas_countries_help_says_estimate():
    """Country ROAS help must say estimate until GCLID is wired."""
    roas_countries_start = JS.find('"roas-countries"', JS.find("PAGE_HELP_CONTENT"))
    assert roas_countries_start != -1, "roas-countries key not found in PAGE_HELP_CONTENT"
    roas_countries_section = JS[roas_countries_start:roas_countries_start + 1500]
    assert "estimate" in roas_countries_section.lower(), \
        "ROAS by Country help must mention 'estimate'"
    assert "GCLID" in roas_countries_section, \
        "ROAS by Country help must mention GCLID coverage"


def test_gclid_help_says_readiness_audit():
    """GCLID Attribution help must say readiness/confidence audit only."""
    gclid_start = JS.find('"gclid-attribution"', JS.find("PAGE_HELP_CONTENT"))
    assert gclid_start != -1, "gclid-attribution key not found in PAGE_HELP_CONTENT"
    gclid_section = JS[gclid_start:gclid_start + 1500]
    assert "readiness" in gclid_section.lower() or "confidence audit" in gclid_section.lower(), \
        "GCLID Attribution help must mention readiness/confidence audit"
    assert "does NOT implement" in gclid_section or "does not implement" in gclid_section.lower(), \
        "GCLID help must clarify it does not implement actual bridge"


def test_churn_input_help_is_retired_with_its_page():
    """PR-ADS-162D retired the Churn Input page: no current figure read the
    churn rate it stored. Its help entry must not outlive it."""
    help_block = JS[JS.find("PAGE_HELP_CONTENT"):JS.find("PAGE_HELP_CONTENT") + 20000]
    assert '"churn-input"' not in help_block


# ── Revenue Snapshot History UI — retired (PR-ADS-162E) ────────────────────
# The section showed persisted local-JSON ROAS snapshots. Since PR-ADS-161E
# every figure in them was withheld as non-canonical, so the block displayed
# dates and the word "Withheld". PR-ADS-162E deleted it with its routes, its
# daily job and the legacy chain behind it. These assertions replace the ones
# that pinned its existence.

def test_snapshot_history_is_retired():
    assert 'id="snapshot-history-container"' not in HTML
    assert 'id="snapshot-history-section"' not in HTML
    assert "loadRevenueSnapshotHistory" not in JS
    assert "/api/reports/roas/snapshots" not in JS
    assert ".snapshot-history-section" not in CSS
    assert ".snapshot-latest-card" not in CSS


def test_no_legacy_verdict_or_ltv_fields_are_rendered():
    """The snapshot block was the last reader of the legacy SCALE/HOLD/FIX/CUT
    counts and of LTV/CAC from local JSON."""
    for field in ("scale_count", "hold_count", "fix_count", "cut_count",
                  "insufficient_data_count", "ltv_cac_ratio"):
        assert field not in JS, field


def test_no_bare_snapshot_routes():
    """No fetch of any snapshot route remains, with or without /api/."""
    assert re.findall(r'fetch\(["\']([^"\']*snapshot[^"\']*)', JS) == []


# ── Open/Close Functions Exist ─────────────────────────────────────────────

def test_open_help_drawer_function_exists():
    """openHelpDrawer function must exist."""
    assert "function openHelpDrawer(" in JS


def test_close_help_drawer_function_exists():
    """closeHelpDrawer function must exist."""
    assert "function closeHelpDrawer(" in JS
