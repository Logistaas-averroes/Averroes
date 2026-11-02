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
    """The ROAS-by-Campaign implementation block (state -> table renderer)."""
    start = JS.find("let roasCampaignFilter")
    assert start != -1, "roasCampaignFilter state not found"
    # PR-ADS-161E: the country page is "Country Revenue & Spend" (a close-date
    # ratio is never called ROAS).
    end = JS.find("// ── Country Revenue & Spend", start)
    assert end != -1, "Country Revenue & Spend marker not found"
    return JS[start:end]


# ── 1. Scope: campaign page rebuilt, country page not rebuilt ─────────────────


def test_scope_campaign_only_no_country_rebuild():
    region = _campaign_region()
    # Campaign decision view exists and is self-contained.
    assert "renderRoasCampaignSummary" in region
    assert "renderRoasCampaignFilters" in region
    assert "data-roas-campaign-filter" in region
    # The Campaign page must not reach into Country state/renderers. (ROAS by
    # Country was rebuilt separately in PR-ADS-112.)
    assert "roasCountryFilter" not in region
    assert "data-roas-country-filter" not in region
    assert "renderRoasCountrySummary" not in region


# ── 2. Campaign summary strip exists ─────────────────────────────────────────


def test_campaign_summary_strip_exists():
    assert "function renderRoasCampaignSummary" in JS
    # span widened in PR-ADS-119: a native/USD currency block now precedes the strip.
    fn = _fn("function renderRoasCampaignSummary", span=3600)
    # PR-ADS-161E: closed-won deals and customers are two cells. The close-date
    # ratios are shown under their explicit period names; certified CAC is shown
    # under its cohort label (always Unavailable). Neither is labelled ROAS/CAC.
    for label in ("Spend", "Leads", "SQLs", "Closed-Won Deals", "Customers",
                  "Won Revenue", "${PERIOD_RATIO_LABEL}",
                  "${PERIOD_SPEND_PER_CUSTOMER_LABEL}",
                  "${CERTIFIED_RATIO_LABELS.cac}"):
        assert f">{label}<" in fn, f"summary strip missing {label}"
    for old in (">ROAS<", ">CAC<"):
        assert old not in fn, f"summary strip still labels a period ratio {old}"
    assert "revenue-summary-strip" in fn


# ── 3. Campaign decision filter chips exist ──────────────────────────────────


def test_campaign_filter_chips_exist():
    assert "roasCampaignFilter" in JS
    assert "data-roas-campaign-filter" in JS
    fn = _fn("function renderRoasCampaignFilters", span=900)
    for label in ("All", "Winners", "Watch", "Waste", "Learning"):
        assert f'"{label}"' in fn, f"filter chip missing {label}"
    # Default selection is "all".
    assert 'roasCampaignFilter = "all"' in JS


def test_filter_chip_event_delegation_scoped_to_campaign():
    # Delegation keys off the campaign data attribute, so country is unaffected.
    assert 'e.target.closest("[data-roas-campaign-filter]")' in JS
    assert "roasCampaignFilter = btn.dataset.roasCampaignFilter" in JS


# ── 4. Sorting is business-value first ───────────────────────────────────────


def test_sorting_is_business_value_first():
    fn = _fn("function sortRoasCampaignRows", span=400)
    # The sort keys must appear in this exact priority order. PR-ADS-161E: the
    # outcome key is the won-DEAL count (a row's customers can be withheld and
    # are not a deal count).
    order = ["closed_won_deals", "won_revenue", "sqls", "spend"]
    positions = [fn.find(k) for k in order]
    assert all(p != -1 for p in positions), f"missing sort key: {positions}"
    assert positions == sorted(positions), "sort keys not in business-value order"
    # Verdict bucket must NOT drive primary sort.
    assert "BUSINESS_VERDICT_ORDER" not in fn


# ── 5. Blocked state remains first ───────────────────────────────────────────


def test_blocked_state_returns_before_summary_and_table():
    # PR-ADS-126: readiness is the MART's verdict (data.readiness.revenue_decision_ready),
    # and the blocked guard still returns before any summary strip or table render.
    body = _fn("function renderRoasCampaignsPage", span=2400)
    i_ready = body.find("revenue_decision_ready")
    i_block = body.find("renderRevenueBlockedState")
    i_return = body.find("return", i_block)
    i_summary = body.find("renderMartSummaryStrip")
    i_table = body.find("renderRoasCampaignTable")
    assert i_ready != -1 and i_block != -1 and i_return != -1
    assert i_ready < i_block < i_return, "must check readiness, render blocked, then return"
    assert i_return < i_summary, "summary must render only after the blocked guard"
    assert i_return < i_table, "table must render only after the blocked guard"


# ── 6. Safe empty state exists (campaign-specific) ───────────────────────────


def test_safe_empty_state_exists():
    assert "function renderRoasCampaignSafeEmptyState" in JS
    fn = _fn("function renderRoasCampaignSafeEmptyState", span=600)
    assert "No campaign revenue found for this window" in fn
    # Must not regress to "not ready" language.
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
    fn = _fn("function renderRoasCampaignTable", span=1600)
    # PR-ADS-161E: the ratio column is the period ratio, never labelled ROAS.
    for col in ("Campaign", "Spend", "Leads", "SQLs", "Customers",
                "Won Revenue", "${PERIOD_RATIO_LABEL}", "Decision"):
        assert f">{col}<" in fn, f"table missing column {col}"
    for forbidden in (">ROAS<", ">CAC<", ">Confidence<", ">Notes<",
                      ">${PERIOD_SPEND_PER_CUSTOMER_LABEL}<",
                      ">${CERTIFIED_RATIO_LABELS.cac}<"):
        assert forbidden not in fn, f"forbidden column present: {forbidden}"


# ── Period ratio rendered as a multiple (2.20x), not a percent ───────────────


def test_campaign_period_ratio_uses_multiple_notation():
    # Campaign-specific formatter renders a multiple and "Unavailable" when null.
    # PR-ADS-161E: renamed fmtRoasMultiple -> fmtPeriodRatio (never called ROAS).
    assert "function fmtPeriodRatio" in JS
    assert "function fmtRoasMultiple" not in JS
    fn = _fn("function fmtPeriodRatio", span=300)
    assert '.toFixed(2) + "x"' in fn
    assert "Unavailable" in fn
    # Summary strip and campaign table use the multiple formatter, not fmtRoas.
    # PR-ADS-161E: through the canonical adapter, with the multiple formatter —
    # a withheld period ratio then names its reason instead of a bare "Unavailable".
    summary = _fn("function renderRoasCampaignSummary", span=3600)
    assert 'rowMetricHtml(s, "period_revenue_per_ad_spend", fmtPeriodRatio' in summary
    assert 'rowMetricHtml(s, "roas"' not in summary
    assert "fmtRoas(s.roas)" not in summary
    table = _fn("function renderRoasCampaignTable", span=3600)
    assert 'rowMetricHtml(r, "period_revenue_per_ad_spend", fmtPeriodRatio' in table
    assert 'rowMetricHtml(r, "roas"' not in table
    assert "fmtRoas(r.roas)" not in table


# ── 9. Campaign page does not depend on Country state ────────────────────────


def test_campaign_page_self_contained():
    # The Campaign decision flow must not reference Country renderers/state, so
    # the two pages stay independent (Country was rebuilt in PR-ADS-112).
    region = _campaign_region()
    assert "renderRoasCountryTable" not in region
    assert "renderRoasCountrySummary" not in region
    assert "roasCountryFilter" not in region
    # Both pages exist.
    assert "function renderRoasCampaignsPage" in JS
    assert "function renderRoasCountriesPage" in JS


# ── Header / window selector preserved ───────────────────────────────────────


def test_campaign_header_and_business_window_preserved():
    m = re.search(r'id="page-roas-campaigns".*?</section>', HTML, re.DOTALL)
    assert m
    section = m.group(0)
    # PR-ADS-161E: renamed from "ROAS by Campaign".
    assert "Campaign Revenue &amp; Spend" in section
    assert "ROAS by Campaign" not in section
    assert "Campaign-level Google Ads spend compared with HubSpot closed-won revenue." in section
    for win in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert f'value="{win}"' in section
    # No ad-style day windows on this business page.
    for ad in ('value="7d"', 'value="14d"', 'value="30d"', 'value="60d"'):
        assert ad not in section
