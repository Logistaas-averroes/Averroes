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
    end = JS.find("// ── ROAS by Country", start)
    assert end != -1, "ROAS by Country marker not found"
    return JS[start:end]


# ── 1. Scope: campaign page rebuilt, country page not rebuilt ─────────────────


def test_scope_campaign_only_no_country_rebuild():
    region = _campaign_region()
    # Campaign decision view exists.
    assert "renderRoasCampaignSummary" in region
    assert "renderRoasCampaignFilters" in region
    assert "data-roas-campaign-filter" in region
    # Country must NOT get a parallel filter/summary rebuild in this PR.
    assert "roasCountryFilter" not in JS
    assert "data-roas-country-filter" not in JS
    assert "renderRoasCountrySummary" not in JS
    assert "renderRoasCountryFilters" not in JS


# ── 2. Campaign summary strip exists ─────────────────────────────────────────


def test_campaign_summary_strip_exists():
    assert "function renderRoasCampaignSummary" in JS
    fn = _fn("function renderRoasCampaignSummary", span=900)
    for label in ("Spend", "Leads", "SQLs", "Customers", "Won Revenue", "ROAS"):
        assert f">{label}<" in fn, f"summary strip missing {label}"
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
    # The sort keys must appear in this exact priority order.
    order = ["customers", "won_revenue", "sqls", "spend"]
    positions = [fn.find(k) for k in order]
    assert all(p != -1 for p in positions), f"missing sort key: {positions}"
    assert positions == sorted(positions), "sort keys not in business-value order"
    # Verdict bucket must NOT drive primary sort.
    assert "BUSINESS_VERDICT_ORDER" not in fn


# ── 5. Blocked state remains first ───────────────────────────────────────────


def test_blocked_state_returns_before_summary_and_table():
    body = _fn("function renderRoasCampaignsPage", span=1600)
    i_ready = body.find("isRevenueDecisionReady")
    i_block = body.find("renderRevenueBlockedState")
    i_return = body.find("return", i_block)
    i_summary = body.find("renderRoasCampaignSummary")
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
    for col in ("Campaign", "Spend", "Leads", "SQLs", "Customers",
                "Won Revenue", "ROAS", "Decision"):
        assert f">{col}<" in fn, f"table missing column {col}"
    for forbidden in (">CAC<", ">Confidence<", ">Notes<"):
        assert forbidden not in fn, f"forbidden column present: {forbidden}"


# ── 9. ROAS by Country untouched ─────────────────────────────────────────────


def test_country_page_untouched():
    assert "roasCountryFilter" not in JS
    assert "data-roas-country-filter" not in JS
    assert "renderRoasCountrySummary" not in JS
    # Country page still exists and still renders.
    assert "function renderRoasCountriesPage" in JS
    assert "renderRoasCountrySafeEmptyState" not in JS


# ── Header / window selector preserved ───────────────────────────────────────


def test_campaign_header_and_business_window_preserved():
    m = re.search(r'id="page-roas-campaigns".*?</section>', HTML, re.DOTALL)
    assert m
    section = m.group(0)
    assert "ROAS by Campaign" in section
    assert "Campaign-level Google Ads spend compared with HubSpot closed-won revenue." in section
    for win in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert f'value="{win}"' in section
    # No ad-style day windows on this business page.
    for ad in ('value="7d"', 'value="14d"', 'value="30d"', 'value="60d"'):
        assert ad not in section
