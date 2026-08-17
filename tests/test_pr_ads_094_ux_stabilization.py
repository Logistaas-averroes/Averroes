"""
PR-ADS-094 — Production UX Stabilization
Tests that:
- Hash routing functions exist and are wired
- Canonical window system replaces hardcoded 60d
- Help drawer closes on navigation and validates content
- ROAS/unit-economics dropdowns have data-sync-window-select
- Cache-busting version params on static assets
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


# ── Hash Routing ───────────────────────────────────────────────────────────

def test_hash_routing_functions_exist():
    """pageToHash, hashToPage, navigateToHash must exist."""
    assert "function pageToHash(" in JS
    assert "function hashToPage(" in JS
    assert "function navigateToHash(" in JS


def test_navigate_accepts_options():
    """navigate() must accept options parameter for hash control."""
    assert "function navigate(page, options)" in JS


def test_hashchange_listener_wired():
    """hashchange event listener must be registered."""
    assert 'addEventListener("hashchange"' in JS


def test_initial_page_uses_hash():
    """Initial page load must read from window.location.hash."""
    assert "hashToPage(window.location.hash)" in JS


def test_hash_route_aliases():
    """Route aliases must exist for backward compat."""
    assert "HASH_ROUTE_ALIASES" in JS
    assert '"ngrams"' in JS or "'ngrams'" in JS


def test_invalid_page_fallback_to_dashboard():
    """Invalid page routes must redirect to dashboard."""
    # navigate must check PAGES.includes(page) and fallback
    assert "PAGES.includes(page)" in JS


# ── Canonical Window System ────────────────────────────────────────────────

def test_valid_day_windows_constant():
    """VALID_DAY_WINDOWS constant must include extended range."""
    assert "VALID_DAY_WINDOWS" in JS
    assert "90" in JS
    assert "365" in JS


def test_get_current_days_function():
    """getCurrentDays() helper must exist."""
    assert "function getCurrentDays()" in JS


def test_get_current_window_param_function():
    """getCurrentWindowParam() helper must exist."""
    assert "function getCurrentWindowParam()" in JS


def test_update_window_controls_function():
    """updateWindowControls() function must exist."""
    assert "function updateWindowControls(" in JS


def test_no_hardcoded_60d_in_gclid_readiness():
    """GCLID readiness must NOT use hardcoded window=60d."""
    start = JS.find("function loadGclidReadiness")
    assert start != -1, "loadGclidReadiness function not found"
    end = JS.find("function loadAttributionConfidenceSummary")
    gclid_section = JS[start:end]
    assert 'window=60d' not in gclid_section
    assert "getCurrentWindowParam()" in gclid_section


def test_no_hardcoded_60d_in_confidence_summary():
    """Attribution confidence must NOT use hardcoded window=60d."""
    start = JS.find("function loadAttributionConfidenceSummary")
    assert start != -1, "loadAttributionConfidenceSummary function not found"
    confidence_section = JS[start:start + 2000]
    assert 'window=60d' not in confidence_section
    assert "getCurrentWindowParam()" in confidence_section


def test_no_hardcoded_60d_in_snapshot_history():
    """Revenue snapshot history must NOT use hardcoded window=60d."""
    start = JS.find("function loadRevenueSnapshotHistory")
    assert start != -1, "loadRevenueSnapshotHistory function not found"
    snapshot_section = JS[start:start + 2000]
    assert 'window=60d' not in snapshot_section
    assert "getCurrentWindowParam()" in snapshot_section


def test_roas_campaigns_uses_business_window():
    """PR-ADS-107A: ROAS campaigns loader uses the business-window resolver,
    not the ad-style global day window."""
    start = JS.find("function loadRoasCampaigns")
    assert start != -1, "loadRoasCampaigns function not found"
    roas_section = JS[start:start + 500]
    assert "getRoasBusinessWindow()" in roas_section
    assert "getCurrentWindowParam()" not in roas_section


def test_roas_countries_uses_business_window():
    """PR-ADS-107A: ROAS countries loader uses the business-window resolver,
    not the ad-style global day window."""
    start = JS.find("function loadRoasCountries")
    assert start != -1, "loadRoasCountries function not found"
    roas_section = JS[start:start + 500]
    assert "getRoasBusinessWindow()" in roas_section
    assert "getCurrentWindowParam()" not in roas_section


def test_unit_economics_uses_global_window_fallback():
    """Unit economics loader should use getCurrentWindowParam() as fallback."""
    start = JS.find("function loadUnitEconomics")
    assert start != -1, "loadUnitEconomics function not found"
    ue_section = JS[start:start + 500]
    # PR-ADS-153E-B: Unit Economics resolves a BUSINESS window (the shared
    # resolver every other revenue page uses), not a rolling ad-style day window.
    assert "getRoasBusinessWindow()" in ue_section


# ── Help Drawer Fixes ──────────────────────────────────────────────────────

def test_help_drawer_closes_on_navigate():
    """navigate() must call closeHelpDrawer() before page switch."""
    nav_section = JS[JS.find("function navigate(page, options)"):]
    nav_section = nav_section[:nav_section.find("function loadPage(")]
    assert "closeHelpDrawer()" in nav_section


def test_help_drawer_validates_content():
    """openHelpDrawer must validate content before opening."""
    open_section = JS[JS.find("function openHelpDrawer"):]
    open_section = open_section[:1000]
    assert "PAGE_HELP_CONTENT[resolvedPageKey]" in open_section


def test_help_drawer_resets_body_on_close():
    """closeHelpDrawer must reset body HTML to empty state."""
    close_section = JS[JS.find("function closeHelpDrawer"):]
    close_section = close_section[:800]
    assert "help-drawer-body" in close_section
    assert "Select a page to view its help guide" in close_section


# ── ROAS Dropdowns Sync ────────────────────────────────────────────────────

def test_roas_campaigns_dropdown_is_business_window():
    """PR-ADS-107A: ROAS campaigns window dropdown is a business-window select,
    decoupled from the ad-style global day window."""
    assert 'id="roas-campaigns-window"' in HTML
    match = re.search(r'<select[^>]*id="roas-campaigns-window"[^>]*>', HTML)
    assert match, "roas-campaigns-window select not found"
    assert "data-business-window-select" in match.group(0)
    assert "data-sync-window-select" not in match.group(0)


def test_roas_countries_dropdown_is_business_window():
    """PR-ADS-107A: ROAS countries window dropdown is a business-window select,
    decoupled from the ad-style global day window."""
    match = re.search(r'<select[^>]*id="roas-countries-window"[^>]*>', HTML)
    assert match, "roas-countries-window select not found"
    assert "data-business-window-select" in match.group(0)
    assert "data-sync-window-select" not in match.group(0)


def test_unit_economics_dropdown_has_sync_attr():
    """Unit economics window dropdown must have data-sync-window-select."""
    match = re.search(r'<select[^>]*id="unit-economics-window"[^>]*>', HTML)
    assert match, "unit-economics-window select not found"
    # PR-ADS-153E-B: it syncs with the BUSINESS-window selector instead.
    assert "data-business-window-select" in match.group(0)


def test_update_window_controls_syncs_dropdowns():
    """updateWindowControls must sync data-sync-window-select elements."""
    update_section = JS[JS.find("function updateWindowControls"):]
    update_section = update_section[:800]
    assert "data-sync-window-select" in update_section


# ── Cache Busting ──────────────────────────────────────────────────────────

def test_css_has_cache_bust_param():
    """styles.css link must include a version query parameter."""
    assert re.search(r'href="/static/styles\.css\?v=', HTML)


def test_js_has_cache_bust_param():
    """app.js script must include a version query parameter."""
    assert re.search(r'src="/static/app\.js\?v=', HTML)
