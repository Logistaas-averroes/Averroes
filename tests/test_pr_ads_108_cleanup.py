"""
PR-ADS-108 — ROAS cleanup tests.

Validates:
  - the global 7d/14d/30d/60d ad-window bar is hidden/decoupled on ROAS pages
  - ROAS pages use business windows only (no ad-window option values)
  - legacy /api/reports/roas/* endpoints are explicitly deprecated and not
    competing truth
  - the frontend only consumes /api/revenue-attribution for ROAS pages
  - no active Windsor wording on the ROAS pages
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()


# ── Global ad-window bar hidden on ROAS pages (#5) ───────────────────────────


def _navigate_fn():
    idx = JS.find("function navigate(page, options)")
    assert idx != -1, "navigate(page, options) not found"
    return JS[idx:idx + 2500]


def test_navigate_hides_global_time_range_bar_on_roas_pages():
    body = _navigate_fn()
    assert "time-range-bar" in body, "navigate() must control the global time-range bar"
    assert "roas-campaigns" in body and "roas-countries" in body, (
        "navigate() must decouple the global ad-window bar on ROAS pages"
    )


def test_global_time_range_bar_uses_ad_windows_only():
    """The global bar is the ad-window control (7d/14d/30d/60d) — confirming it is
    a distinct control that must be hidden on business-window ROAS pages."""
    match = re.search(r'<div class="time-range-bar"[^>]*id="time-range-bar".*?</div>', HTML, re.DOTALL)
    assert match, "global time-range-bar not found"
    assert 'data-days="7"' in match.group(0)


def test_roas_window_selects_are_business_windows_not_ad_windows():
    for select_id in ("roas-campaigns-window", "roas-countries-window"):
        m = re.search(rf'<select[^>]*id="{select_id}"[^>]*>.*?</select>', HTML, re.DOTALL)
        assert m, f"{select_id} not found"
        block = m.group(0)
        assert 'value="current_quarter"' in block
        for ad in ('value="7d"', 'value="14d"', 'value="30d"', 'value="60d"'):
            assert ad not in block, f"{select_id} must not contain ad window {ad}"


# ── Legacy endpoints deprecated, not competing truth (#6) ────────────────────


def test_legacy_roas_endpoints_marked_deprecated():
    for fn in ("async def get_roas_campaigns(", "async def get_roas_countries("):
        idx = SERVER.find(fn)
        assert idx != -1, f"{fn} not found"
        region = SERVER[idx:SERVER.find("@app.get", idx + 1)]
        assert '"deprecated": True' in region, f"{fn} must return deprecated=True"
        assert '"superseded_by": "/api/revenue-attribution"' in region


def test_frontend_only_uses_revenue_attribution_for_roas():
    # The new shared endpoint is used...
    assert "/api/revenue-attribution" in JS
    # ...and the legacy ROAS report endpoints are not fetched by the frontend.
    assert "/api/reports/roas/campaigns" not in JS
    assert "/api/reports/roas/countries" not in JS


# ── No active Windsor wording on ROAS pages ──────────────────────────────────


def test_no_windsor_on_roas_pages():
    for page_id in ("page-roas-campaigns", "page-roas-countries"):
        m = re.search(rf'id="{page_id}".*?</section>', HTML, re.DOTALL)
        assert m, f"{page_id} not found"
        assert "windsor" not in m.group(0).lower()


def test_no_windsor_in_revenue_attribution_endpoint():
    idx = SERVER.find("async def get_revenue_attribution(")
    assert idx != -1
    region = SERVER[idx:SERVER.find("@app.get", idx + 1)]
    assert "windsor" not in region.lower()
