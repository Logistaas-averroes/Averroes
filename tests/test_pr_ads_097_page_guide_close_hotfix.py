"""
PR-ADS-098 — Page Guide Drawer Close Hotfix
Tests that:
- closeHelpDrawer hides drawer even when _helpDrawerOpen is false (idempotent).
- close button handler calls closeHelpDrawer with e.preventDefault/stopPropagation.
- Escape handler calls closeHelpDrawer regardless of _helpDrawerOpen state.
- overlay click calls closeHelpDrawer only when target is overlay.
- closeHelpDrawer resets help-drawer-body to placeholder text.
- navigate() calls closeHelpDrawer.
- Cache busting version is v=098.
- Help drawer hidden rule is present in CSS.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")


# ── Idempotent closeHelpDrawer ─────────────────────────────────────────────

def test_close_help_drawer_does_not_return_early_on_closed():
    """closeHelpDrawer must NOT have an early return when _helpDrawerOpen is false."""
    # The early-return guard must not be present inside closeHelpDrawer
    fn_start = JS.find("function closeHelpDrawer(")
    assert fn_start != -1, "closeHelpDrawer not found"
    # Find function body
    body_start = JS.find("{", fn_start)
    depth = 0
    body_end = -1
    for idx, ch in enumerate(JS[body_start:], start=body_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = idx
                break
    fn_body = JS[body_start:body_end + 1]
    assert "if (!_helpDrawerOpen) return" not in fn_body, (
        "closeHelpDrawer must not return early when _helpDrawerOpen is false"
    )


def test_close_help_drawer_sets_overlay_hidden():
    """closeHelpDrawer must set overlay.hidden = true unconditionally."""
    fn_start = JS.find("function closeHelpDrawer(")
    body_start = JS.find("{", fn_start)
    depth = 0
    body_end = -1
    for idx, ch in enumerate(JS[body_start:], start=body_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = idx
                break
    fn_body = JS[body_start:body_end + 1]
    assert "overlay.hidden = true" in fn_body
    assert 'overlay.setAttribute("aria-hidden", "true")' in fn_body
    assert "drawer.hidden = true" in fn_body
    assert "_helpDrawerOpen = false" in fn_body


def test_close_help_drawer_resets_body():
    """closeHelpDrawer must reset help-drawer-body to placeholder text."""
    fn_start = JS.find("function closeHelpDrawer(")
    body_start = JS.find("{", fn_start)
    depth = 0
    body_end = -1
    for idx, ch in enumerate(JS[body_start:], start=body_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = idx
                break
    fn_body = JS[body_start:body_end + 1]
    assert "Select a page to view its help guide." in fn_body, (
        "closeHelpDrawer must reset body to placeholder text"
    )
    assert '<p class="empty-state">Select a page to view its help guide.</p>' in fn_body, (
        "closeHelpDrawer must use empty-state class on placeholder paragraph"
    )


# ── Close button handler ───────────────────────────────────────────────────

def test_close_button_handler_calls_prevent_default():
    """Close button handler must call e.preventDefault()."""
    close_btn_block_match = re.search(
        r'helpCloseBtn\.addEventListener\("click".*?\}\s*\)', JS, re.DOTALL
    )
    assert close_btn_block_match, "helpCloseBtn click listener not found"
    block = close_btn_block_match.group(0)
    assert "e.preventDefault()" in block, (
        "Close button handler must call e.preventDefault()"
    )


def test_close_button_handler_calls_stop_propagation():
    """Close button handler must call e.stopPropagation()."""
    close_btn_block_match = re.search(
        r'helpCloseBtn\.addEventListener\("click".*?\}\s*\)', JS, re.DOTALL
    )
    assert close_btn_block_match, "helpCloseBtn click listener not found"
    block = close_btn_block_match.group(0)
    assert "e.stopPropagation()" in block, (
        "Close button handler must call e.stopPropagation()"
    )


def test_close_button_handler_calls_close_help_drawer():
    """Close button handler must call closeHelpDrawer()."""
    close_btn_block_match = re.search(
        r'helpCloseBtn\.addEventListener\("click".*?\}\s*\)', JS, re.DOTALL
    )
    assert close_btn_block_match, "helpCloseBtn click listener not found"
    block = close_btn_block_match.group(0)
    assert "closeHelpDrawer()" in block, (
        "Close button handler must call closeHelpDrawer()"
    )


# ── Escape key handler ─────────────────────────────────────────────────────

def test_escape_handler_does_not_guard_on_help_drawer_open():
    """Escape handler must call closeHelpDrawer without checking _helpDrawerOpen first."""
    fn_start = JS.find("function _helpDrawerKeyHandler(")
    assert fn_start != -1, "_helpDrawerKeyHandler not found"
    body_start = JS.find("{", fn_start)
    depth = 0
    body_end = -1
    for idx, ch in enumerate(JS[body_start:], start=body_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = idx
                break
    fn_body = JS[body_start:body_end + 1]
    # Must NOT have the old "if (!_helpDrawerOpen) return" guard before Escape
    assert 'if (!_helpDrawerOpen) return' not in fn_body, (
        "_helpDrawerKeyHandler must not return early when _helpDrawerOpen is false"
    )
    # Must still call closeHelpDrawer on Escape
    escape_match = re.search(
        r'"Escape".*?closeHelpDrawer\(\)', fn_body, re.DOTALL
    )
    assert escape_match, "Escape key must call closeHelpDrawer()"


# ── Overlay click guard ────────────────────────────────────────────────────

def test_overlay_click_only_closes_on_target_match():
    """Overlay click listener must only call closeHelpDrawer when e.target === helpOverlay."""
    overlay_block_match = re.search(
        r'helpOverlay\.addEventListener\("click".*?\}\s*\)', JS, re.DOTALL
    )
    assert overlay_block_match, "helpOverlay click listener not found"
    block = overlay_block_match.group(0)
    assert "e.target === helpOverlay" in block, (
        "Overlay click must guard: if (e.target === helpOverlay)"
    )


# ── navigate() calls closeHelpDrawer ──────────────────────────────────────

def test_navigate_calls_close_help_drawer():
    """navigate() must call closeHelpDrawer() on every page change."""
    fn_start = JS.find("function navigate(")
    assert fn_start != -1, "navigate function not found"
    body_start = JS.find("{", fn_start)
    depth = 0
    body_end = -1
    for idx, ch in enumerate(JS[body_start:], start=body_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = idx
                break
    fn_body = JS[body_start:body_end + 1]
    assert "closeHelpDrawer()" in fn_body, (
        "navigate() must call closeHelpDrawer()"
    )


# ── Force-close on startup ─────────────────────────────────────────────────

def test_startup_force_closes_help_drawer():
    """App startup must call closeHelpDrawer() to ensure clean initial state."""
    dom_start = JS.find('document.addEventListener("DOMContentLoaded", async () => {')
    assert dom_start != -1, "Init DOMContentLoaded handler not found"

    close_idx = JS.find("closeHelpDrawer()", dom_start)
    assert close_idx != -1, "App startup must force-close the help drawer"

    check_idx = JS.find("await checkAuth()", dom_start)
    assert check_idx != -1, "checkAuth() call not found in startup handler"
    assert close_idx < check_idx, "closeHelpDrawer() must run before checkAuth()"


# ── Cache busting ──────────────────────────────────────────────────────────

def test_cache_busting_version_is_098():
    """index.html must reference app.js?v=098."""
    assert 'app.js?v=098' in HTML, (
        "Cache busting version must be updated to v=098"
    )


def test_help_drawer_hidden_rule_exists():
    """Help drawer must have an explicit [hidden] display override."""
    assert ".help-drawer[hidden]" in CSS, "Missing .help-drawer[hidden] selector"
    assert re.search(r"\.help-drawer\[hidden\]\s*\{[^}]*display\s*:\s*none\s*;", CSS), (
        "Missing display:none in .help-drawer[hidden] rule"
    )
