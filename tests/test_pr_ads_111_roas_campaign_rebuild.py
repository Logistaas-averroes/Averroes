"""
PR-ADS-111 — Rebuild ROAS by Campaign decision page.

ROAS by Campaign becomes a clean executive decision view that answers one
question: "Which Google Ads campaigns are producing qualified pipeline,
customers, and closed-won revenue?"

It is NOT a diagnostics page. When blocked it shows only the blocked card;
when ready it shows a summary strip, decision filter chips, and a clean
decision table. ROAS by Country must stay untouched in this PR.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()


def _fn(name, span=2600):
    idx = JS.find(name)
    assert idx != -1, f"{name} not found"
    return JS[idx:idx + span]


def _campaign_region():
    """The campaign decision page's implementation block. PR-ADS-177B: the one
    Campaigns page (Campaign Revenue & Spend, rebuilt here in PR-ADS-111, was
    merged into it); its block runs from its banner to the spend-proof modal."""
    start = JS.find("// ── Campaigns — the one campaign page (PR-ADS-177B)")
    assert start != -1, "Campaigns page banner not found"
    end = JS.find("// ── PR-ADS-122: Campaign spend reconciliation drilldown", start)
    assert end != -1, "spend-proof marker not found"
    return JS[start:end]


# PR-ADS-177B: every case below pinned Campaign Revenue & Spend
# (`roas-campaigns`), which is merged into the one Campaigns page. Each is the
# same property on that page's renderers (docs/60 §7.2).

# ── 1. Scope: the campaign page does not reach into the country page ────────


def test_scope_campaign_only_no_country_rebuild():
    region = _campaign_region()
    assert "renderCampaignsPageSummary" in region
    assert "renderCampaignsPageFilters" in region
    assert "data-campaigns-page-filter" in region
    assert "roasCountryFilter" not in region
    assert "data-roas-country-filter" not in region
    assert "renderRoasCountrySummary" not in region


# ── 2. The KPI cards (the summary strip's successor) ─────────────────────────


def test_campaign_summary_strip_exists():
    fn = _fn("function renderCampaignsPageSummary", span=4200)
    # The server's campaign_totals — deals and customers are two cards; the
    # period ratio under its explicit name; nothing labelled ROAS / CAC.
    for label in ('"Campaign Spend"', "COHORT_LEADS_LABEL", "COHORT_SQLS_LABEL",
                  '"Won Deals"', '"Customers"', '"Won Revenue"', "PERIOD_RATIO_LABEL"):
        assert f"label: {label}" in fn, f"KPI cards missing {label}"
    for old in ("ROAS", "CAC"):
        assert old not in fn, f"KPI cards label a period ratio {old}"
    assert "campaign_totals" in fn and "dash-kpi-card" in fn


# ── 3. Decision filter chips ─────────────────────────────────────────────────


def test_campaign_filter_chips_exist():
    assert "campaignsPageFilter" in JS
    fn = _fn("function renderCampaignsPageFilters", span=900)
    for label in ("All", "Winners", "Watch", "Waste", "Learning"):
        assert f'"{label}"' in fn, f"filter chip missing {label}"
    assert 'let campaignsPageFilter = "all"' in JS


def test_filter_chip_event_delegation_scoped_to_campaign():
    assert 'e.target.closest("[data-campaigns-page-filter]")' in JS
    assert "campaignsPageFilter = chip.dataset.campaignsPageFilter" in JS


# ── 4. Sorting is business-value first ───────────────────────────────────────


def test_sorting_is_business_value_first():
    fn = _fn("function sortCampaignsPageRows", span=400)
    order = ["closed_won_deals", "won_revenue", "sqls", "spend"]
    positions = [fn.find(k) for k in order]
    assert all(p != -1 for p in positions), f"missing sort key: {positions}"
    assert positions == sorted(positions), "sort keys not in business-value order"
    assert "BUSINESS_VERDICT_ORDER" not in fn


# ── 5. Blocked state remains first ───────────────────────────────────────────


def test_blocked_state_returns_before_summary_and_table():
    body = _fn("function renderCampaignsPage()", span=3400)
    i_ready = body.find("revenue_decision_ready")
    i_block = body.find("renderRevenueBlockedState")
    i_return = body.find("return", i_block)
    i_summary = body.find("renderCampaignsPageSummary")
    i_table = body.find("renderCampaignsPageTable")
    assert i_ready != -1 and i_block != -1 and i_return != -1
    assert i_ready < i_block < i_return, "must check readiness, render blocked, then return"
    assert i_return < i_summary, "summary must render only after the blocked guard"
    assert i_return < i_table, "table must render only after the blocked guard"
    # PR-ADS-177B: the strict spend gate returns before them too.
    i_gate = body.find("renderCampaignSpendGateState")
    assert i_return < i_gate < i_summary


# ── 6. Safe empty state exists (campaign-specific) ───────────────────────────


def test_safe_empty_state_exists():
    fn = _fn("function renderCampaignsSafeEmptyState", span=600)
    assert "No campaign activity in this window" in fn
    assert "not ready" not in fn.lower()


# ── 7. No diagnostics clutter returns ────────────────────────────────────────


def test_no_diagnostics_clutter_in_campaign_renderer():
    region = _campaign_region()
    for clutter in (
        "View attribution audit",
        "Truth-layer warning",
        "What this page shows",
        "What empty means",
        "Next action",
        "Depends on",
        "Read-only",
    ):
        assert clutter not in region, f"diagnostics clutter leaked back: {clutter}"


# ── 8. Table columns are correct ─────────────────────────────────────────────


def test_table_columns_are_correct():
    fn = _fn("function renderCampaignsPageTable", span=2400)
    for col in ("Campaign", "Spend", "Customers", "Won Revenue", "Won Deals",
                "${PERIOD_RATIO_LABEL}", "Status"):
        assert f">{col}<" in fn, f"table missing column {col}"
    for col in ("Leads", "SQLs"):
        assert f">{col}<span" in fn, f"table missing column {col}"
    for forbidden in (">ROAS<", ">CAC<", ">Confidence<", ">Notes<", ">Junk<",
                      ">${PERIOD_SPEND_PER_CUSTOMER_LABEL}<",
                      ">${CERTIFIED_RATIO_LABELS.cac}<"):
        assert forbidden not in fn, f"forbidden column present: {forbidden}"


# ── Period ratio rendered as a multiple (2.20x), not a percent ───────────────


def test_campaign_period_ratio_uses_multiple_notation():
    assert "function fmtPeriodRatio" in JS
    assert "function fmtRoasMultiple" not in JS
    fn = _fn("function fmtPeriodRatio", span=300)
    assert '.toFixed(2) + "x"' in fn
    assert "Unavailable" in fn
    summary = _fn("function renderCampaignsPageSummary", span=4200)
    assert 'rowMetricHtml(t, "period_revenue_per_ad_spend", fmtPeriodRatio' in summary
    assert '"roas"' not in summary and "fmtRoas(" not in summary
    table = _fn("function renderCampaignsPageTable", span=3600)
    assert 'rowMetricHtml(r, "period_revenue_per_ad_spend", fmtPeriodRatio' in table
    assert '"roas"' not in table and "fmtRoas(" not in table


# ── 9. Campaign page does not depend on Country state ────────────────────────


def test_campaign_page_self_contained():
    region = _campaign_region()
    assert "renderRoasCountryTable" not in region
    assert "renderRoasCountrySummary" not in region
    assert "roasCountryFilter" not in region
    assert "function renderCampaignsPage()" in JS
    assert "function renderRoasCountriesPage" in JS


# ── Header / window selector preserved ───────────────────────────────────────


def test_campaign_header_and_business_window_preserved():
    m = re.search(r'id="page-campaigns".*?</section>', HTML, re.DOTALL)
    assert m
    section = m.group(0)
    # PR-ADS-177B: the one page is titled "Campaigns" (never ROAS).
    assert '<h1 class="page-title">Campaigns</h1>' in section
    assert "ROAS by Campaign" not in section
    for win in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert f'value="{win}"' in section
    for ad in ('value="7d"', 'value="14d"', 'value="30d"', 'value="60d"'):
        assert ad not in section
