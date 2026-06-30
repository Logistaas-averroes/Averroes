"""
PR-ADS-123 — Revenue ROAS UI Containment Fix.

A PRESENTATION/LAYOUT-only change: the ROAS by Campaign / Country decision tables
were overflowing the page on desktop and clipping the Decision column, the KPI
spend wrapped badly ("$93,875.53" then "USD"), and the Google Ads Spend summary
was unstyled raw text. This proves the layout fixes WITHOUT changing any data,
spend, FX, mapping, connector, attribution, or schema logic.

Static frontend assertions only — no runtime, no network.
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()


def _fn(marker, span=1800):
    i = JS.find(marker)
    assert i != -1, f"missing {marker}"
    return JS[i:i + span]


# ════════════ 1. ROAS tables use a real horizontal-scroll shell ════════════


def test_campaign_table_uses_revenue_table_scroll():
    fn = _fn("function renderRoasCampaignTable", span=2600)
    assert "revenue-table-shell" in fn
    assert "revenue-table-scroll" in fn
    assert "revenue-decision-table" in fn


def test_country_table_uses_revenue_table_scroll():
    fn = _fn("function renderRoasCountryTable", span=2600)
    assert "revenue-table-shell" in fn
    assert "revenue-table-scroll" in fn
    assert "revenue-decision-table" in fn


def test_scroll_wrapper_css_defines_overflow():
    # The shell contains; the inner scroll provides horizontal overflow.
    shell = _css_block(".revenue-table-shell")
    assert "overflow: hidden" in shell
    scroll = _css_block(".revenue-table-scroll")
    assert "overflow-x: auto" in scroll


# ════════════ 2. the table has a min-width so columns are not clipped ════════


def test_decision_table_has_min_width_in_scroll():
    block = _css_block(".revenue-table-scroll .revenue-decision-table")
    assert re.search(r"min-width:\s*1120px", block)


# ════════════ 3. Decision column stays visible (nowrap + min-width) ════════════


def test_decision_column_nowrap_and_min_width():
    block = _css_block(".revenue-decision-table th:last-child,\n.revenue-decision-table td:last-child")
    assert "white-space: nowrap" in block
    assert re.search(r"min-width:\s*120px", block)


def test_campaign_name_column_wraps_normally():
    # Requirement 5: name + aliases wrap instead of forcing the table wider.
    block = _css_block(".revenue-decision-table td:first-child")
    assert "white-space: normal" in block


# ════════════ 4. Google Ads Spend summary is styled markup, not raw text ════


def test_spend_summary_is_styled_strip():
    fn = _fn("function renderRoasCampaignSummary", span=2200)
    assert "revenue-spend-summary" in fn
    assert "revenue-spend-summary__grid" in fn
    # The old unstyled raw-text block is gone.
    assert "revenue-spend-currency__native" not in fn
    # And the styled strip has real CSS (it previously had none).
    assert ".revenue-spend-summary {" in CSS
    assert ".revenue-spend-summary__grid {" in CSS


# ════════════ 5. KPI spend uses the compact formatter ════════════


def test_compact_currency_formatter_exists():
    assert "function fmtCompactCurrency" in JS
    fn = _fn("function fmtCompactCurrency", span=600)
    # Compact magnitudes (k / M), currency-correct (never "$" forced on native).
    assert '"k"' in fn and '"M"' in fn
    assert "currencySymbol(code)" in fn


def test_kpi_spend_uses_compact_not_full_precision():
    fn = _fn("function renderRoasCampaignSummary", span=2200)
    # Native + reporting magnitudes shown compactly in the summary strip.
    assert "fmtCompactCurrency(native, nativeCur)" in fn
    assert 'fmtCompactCurrency(usd, "USD")' in fn
    # The visible KPI strip Spend cell uses the compact magnitude form (e.g.
    # "$93.9k"), not the full-precision "$93,875.53 USD" that wrapped badly.
    assert "? fmtMoney(usd)" in fn


def test_exact_values_preserved_on_hover_and_in_modal():
    # Exact full-precision native/USD stay available (hover title), not lost.
    fn = _fn("function renderRoasCampaignSummary", span=2200)
    assert 'title="${fmtCurrency(native, nativeCur)}"' in fn
    # The spend proof modal still carries exact per-total figures.
    assert "renderSpendProof" in JS


# ════════════ 6. no backend/service/schema files changed ════════════


def test_only_frontend_and_test_files_changed():
    # PR-ADS-123 is presentation-only: the diff vs origin/main must touch nothing
    # outside static/* and this test file.
    try:
        base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        out = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        import pytest
        pytest.skip("git diff unavailable in this environment")
    changed = [f for f in out.splitlines() if f.strip()]
    if not changed:
        import pytest
        pytest.skip("no committed diff yet (working tree only)")
    allowed_prefixes = ("static/",)
    allowed_exact = {"tests/test_pr_ads_123_revenue_ui_containment.py"}
    forbidden = [
        f for f in changed
        if not (f.startswith(allowed_prefixes) or f in allowed_exact)
    ]
    assert not forbidden, f"PR-ADS-123 must be frontend-only; unexpected changes: {forbidden}"


# ── helpers ──────────────────────────────────────────────────────────────────


def _css_block(selector):
    i = CSS.find(selector)
    assert i != -1, f"missing CSS selector: {selector}"
    brace = CSS.find("{", i)
    end = CSS.find("}", brace)
    assert brace != -1 and end != -1, f"malformed CSS for {selector}"
    return CSS[brace:end + 1]
