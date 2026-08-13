"""
tests/test_pr_ads_153c_leads_page.py

PR-ADS-153C — Canonical Leads Experience & Lead Intelligence Retirement.

Covers the required suites:
  §34  canonical page contract — each stage uses its OWN stage-entry date,
       historical cohorts survive later transitions, no createdate fallback;
  §35  scope algebra and unavailable-never-zero;
  §36  navigation — Lead Intelligence removed, Leads under CRM & Revenue,
       retired routes redirect, no dead Action Queue links;
  §37  operational status is a working dimension, never a funnel definition;
  §38  frontend truth states (reconciled / partial / mismatch / unavailable);
  §39  privacy — no emails, no MDR free text.

Run with:
    python -m pytest tests/test_pr_ads_153c_leads_page.py -v
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import crm_lifecycle as lifecycle  # noqa: E402
from analysis import mql_status_taxonomy as mql  # noqa: E402
from services import canonical_crm_funnel_service as funnel  # noqa: E402

_APP_JS = (_ROOT / "static" / "app.js").read_text()
_INDEX_HTML = (_ROOT / "static" / "index.html").read_text()
_SERVER_PY = (_ROOT / "api" / "server.py").read_text()


def _row(contact_id="1", *, stage=None, status=None, created="2026-01-05",
         lead=None, mql_=None, sql=None, opportunity=None, customer=None,
         source="PAID_SEARCH", campaign="Brand - US", keyword="tms"):
    def _d(v):
        return date.fromisoformat(v) if v else None
    return {
        "contact_id": contact_id,
        "company": f"Co {contact_id}",
        "created_at": _d(created),
        "lifecycle_stage": stage,
        "mql_status": status,
        "mql_status_category": mql.classify_mql_status(status),
        "date_entered_lead": _d(lead),
        "date_entered_mql": _d(mql_),
        "date_entered_sql": _d(sql),
        "date_entered_opportunity": _d(opportunity),
        "date_entered_customer": _d(customer),
        "hs_analytics_source": source,
        "hs_analytics_source_data_1": campaign,
        "hs_analytics_source_data_2": keyword,
        "ip_country": "AE", "country": None, "owner_id": "42", "has_gclid": True,
    }


_Q3_START, _Q3_END = date(2026, 7, 1), date(2026, 9, 30)


def _pops(rows, start=_Q3_START, end=_Q3_END, **kw):
    return funnel.build_populations(rows, start, end, **kw)


# =============================================================================
# §34 — Canonical page contract: each stage on its OWN event date
# =============================================================================
@pytest.mark.parametrize("event,kwargs,prop", [
    ("lead", {"lead": "2026-07-05"}, "hs_v2_date_entered_lead"),
    ("mql", {"mql_": "2026-07-05"}, "hs_v2_date_entered_marketingqualifiedlead"),
    ("sql", {"sql": "2026-07-05"}, "hs_v2_date_entered_salesqualifiedlead"),
    ("opportunity", {"opportunity": "2026-07-05"}, "hs_v2_date_entered_opportunity"),
    ("customer", {"customer": "2026-07-05"}, "hs_v2_date_entered_customer"),
])
def test_each_stage_card_uses_its_own_entry_date(event, kwargs, prop):
    populations = _pops([_row("1", **kwargs)])
    assert populations["counts"][event][funnel.SCOPE_ALL_SOURCE] == 1
    assert funnel.event_definition(event)["event_date_property"] == prop
    # And no OTHER stage is credited by that one date.
    for other in lifecycle.FUNNEL_EVENTS:
        if other != event:
            assert populations["counts"][other][funnel.SCOPE_ALL_SOURCE] == 0


def test_january_created_august_qualified_appears_in_august_sql_view():
    """The headline behaviour: acquisition date and qualification date differ."""
    row = _row("1", created="2026-01-05", sql="2026-08-14")
    q3 = funnel.build_populations([row], date(2026, 7, 1), date(2026, 9, 30))
    q1 = funnel.build_populations([row], date(2026, 1, 1), date(2026, 3, 31))
    assert q3["counts"]["sql"][funnel.SCOPE_ALL_SOURCE] == 1
    assert q1["counts"]["sql"][funnel.SCOPE_ALL_SOURCE] == 0


def test_current_customer_remains_visible_in_its_historical_sql_view():
    rows = [_row("1", stage="customer", sql="2026-07-10", customer="2026-11-02")]
    populations = _pops(rows)
    assert populations["counts"]["sql"][funnel.SCOPE_ALL_SOURCE] == 1
    sql_population = populations["events"]["sql"]
    assert sql_population[0]["lifecycle_stage"] == "customer"


def test_stage_views_are_not_mutually_exclusive_by_current_stage():
    """One contact legitimately appears in several stage cohorts."""
    rows = [_row("1", stage="customer", lead="2026-07-01", mql_="2026-07-05",
                 sql="2026-07-10", opportunity="2026-07-20",
                 customer="2026-08-01")]
    counts = _pops(rows)["counts"]
    for event in lifecycle.FUNNEL_EVENTS:
        assert counts[event][funnel.SCOPE_ALL_SOURCE] == 1


def test_no_createdate_fallback_for_any_stage():
    rows = [_row("1", created="2026-07-05")]  # in-window creation, no stage dates
    counts = _pops(rows)["counts"]
    for event in lifecycle.FUNNEL_EVENTS:
        assert counts[event][funnel.SCOPE_ALL_SOURCE] == 0


def test_contact_row_payload_exposes_all_stage_dates_and_selected_event():
    row = _row("1", stage="customer", sql="2026-07-10", customer="2026-08-01")
    payload = funnel._contact_row_payload(row, "sql", True)  # noqa: SLF001
    assert payload["event_date"] == "2026-07-10"
    assert payload["stage_dates"]["customer"] == "2026-08-01"
    # Current lifecycle is reported separately from the selected event.
    assert payload["lifecycle_stage"] == "customer"


def test_missing_stage_date_is_none_not_fabricated():
    payload = funnel._contact_row_payload(_row("1", sql="2026-07-10"), "sql", True)  # noqa: SLF001
    assert payload["stage_dates"]["customer"] is None


# =============================================================================
# §35 — Scope algebra
# =============================================================================
def test_all_sources_is_the_leads_page_default():
    assert 'let _leadsScope = "all_source";' in _APP_JS
    assert re.search(r'scope:\s*str\s*=\s*Query\(default="all_source"\)', _SERVER_PY)


@pytest.mark.parametrize("source", ["ORGANIC_SEARCH", "PAID_SOCIAL", "OFFLINE"])
def test_non_google_sqls_appear_in_all_sources(source):
    counts = _pops([_row("1", sql="2026-07-05", source=source)])["counts"]["sql"]
    assert counts[funnel.SCOPE_ALL_SOURCE] == 1
    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 0


def test_google_ads_sql_appears_in_both_all_sources_and_google_scope():
    counts = _pops([_row("1", sql="2026-07-05", source="PAID_SEARCH")])["counts"]["sql"]
    assert counts[funnel.SCOPE_ALL_SOURCE] == 1
    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 1


def test_scope_nesting_invariant_holds_for_every_stage():
    rows = [
        _row("1", sql="2026-07-05", source="PAID_SEARCH", campaign="Brand - US", keyword="tms"),
        _row("2", sql="2026-07-06", source="PAID_SEARCH", campaign="Brand - US", keyword=None),
        _row("3", sql="2026-07-07", source="PAID_SEARCH", campaign=None),
        _row("4", sql="2026-07-08", source="ORGANIC_SEARCH"),
    ]
    counts = _pops(rows)["counts"]["sql"]
    assert (counts[funnel.SCOPE_KEYWORD_ATTRIBUTABLE]
            <= counts[funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE]
            <= counts[funnel.SCOPE_GOOGLE_ADS_SOURCE]
            <= counts[funnel.SCOPE_ALL_SOURCE])


def test_identity_unavailable_makes_narrow_scopes_unavailable_not_zero():
    rows = [_row(str(i), sql="2026-07-05") for i in range(1, 6)]
    counts = _pops(rows, identity_available=False)["counts"]["sql"]
    assert counts[funnel.SCOPE_ALL_SOURCE] == 5
    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 5
    assert counts[funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE] is None
    assert counts[funnel.SCOPE_KEYWORD_ATTRIBUTABLE] is None


def test_contacts_endpoint_withholds_a_narrow_scope_without_identity(monkeypatch):
    """A narrow scope with no identity contract must be UNAVAILABLE — never an
    empty page, which a reader would take as a proven zero."""
    monkeypatch.setattr(funnel, "_build_campaign_resolver",
                        lambda *a, **kw: (lambda c: (False, "x"), False))
    payload = funnel.contacts("business", "current_quarter",
                              event="sql", scope=funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE)
    assert payload["available"] is False
    assert payload["reason"] == funnel.REASON_CAMPAIGN_IDENTITY_UNAVAILABLE
    assert payload["total"] is None


def test_scope_allowlists_are_resolved_from_the_canonical_classifiers():
    """Scope filtering happens server-side via pre-resolved allow-lists, so the
    taxonomy is never re-implemented in SQL."""
    raw = ["PAID_SEARCH", "ORGANIC_SEARCH", "PAID_SOCIAL", None]
    google = funnel.resolve_source_allowlist("google_ads", raw)
    assert google == ["PAID_SEARCH"]
    assert funnel.resolve_source_allowlist(None, raw) is None

    resolver = lambda label: (label == "Brand - US", None)  # noqa: E731
    assert funnel.resolve_campaign_allowlist(
        ["Brand - US", "Other"], resolver) == ["Brand - US"]


def test_frontend_renders_unavailable_scope_as_dash_never_zero():
    assert "leadsCount" in _APP_JS
    # The count helper maps null/undefined to an em dash, never 0.
    helper = _APP_JS.split("function leadsCount(")[1].split("}")[0]
    assert '"—"' in helper
    assert "|| 0" not in helper


# =============================================================================
# §36 — Navigation
# =============================================================================
def test_lead_intelligence_section_is_removed():
    assert "Lead Intelligence" not in _INDEX_HTML


def test_leads_nav_item_exists_under_crm_and_revenue():
    assert "CRM &amp; Revenue" in _INDEX_HTML
    crm_section = _INDEX_HTML.split("CRM &amp; Revenue")[1].split("<!-- Admin -->")[0]
    assert 'data-page="leads"' in crm_section
    assert ">Leads<" in crm_section


def test_lead_quality_and_in_progress_nav_items_are_gone():
    assert ">Lead Quality<" not in _INDEX_HTML
    assert ">In Progress Leads<" not in _INDEX_HTML
    assert 'data-page="opportunities"' not in _INDEX_HTML


def test_in_progress_page_markup_is_removed():
    assert 'id="page-opportunities"' not in _INDEX_HTML
    assert 'id="opps-body"' not in _INDEX_HTML


def test_retired_route_redirects_to_leads_with_filter_intent():
    assert "RETIRED_PAGE_REDIRECTS" in _APP_JS
    block = _APP_JS.split("const RETIRED_PAGE_REDIRECTS = {")[1].split("};")[0]
    assert 'opportunities:' in block
    assert '"leads"' in block
    assert '"open_working"' in block


def test_old_lead_quality_url_lands_on_the_canonical_leads_page():
    """The `leads` route key is retained, so #/leads resolves to the new page."""
    assert '"leads"' in _APP_JS.split("const PAGES = [")[1].split("]")[0]
    assert "case \"leads\":         loadLeads();" in _APP_JS


def test_retired_loader_is_gone():
    assert "loadOpportunities" not in _APP_JS
    assert "ACTIVE_MDR_STATUSES" not in _APP_JS


def test_action_queue_has_no_links_to_retired_pages():
    """Action Queue's server-side primary_link pages must not reference a
    retired page (they never did — this locks it in)."""
    for retired in ('"page": "opportunities"', '"page": "leads"'):
        assert retired not in _SERVER_PY
    assert 'data-navigate="opportunities"' not in _APP_JS


def test_leads_uses_business_windows_not_evidence_windows():
    revenue_pages = _APP_JS.split("const REVENUE_PAGES = [")[1].split("]")[0]
    assert '"leads"' in revenue_pages
    evidence_pages = _APP_JS.split("const EVIDENCE_PAGES = [")[1].split("]")[0]
    assert '"leads"' not in evidence_pages
    assert '"opportunities"' not in evidence_pages


def test_flagged_waste_terms_remains_reachable():
    """§3 — the page keeps its route and backend for PR-ADS-153D."""
    assert 'data-page="waste"' in _INDEX_HTML
    assert 'id="page-waste"' in _INDEX_HTML
    assert '@app.get("/api/waste")' in _SERVER_PY


# =============================================================================
# §37 — Operational status is not a funnel definition
# =============================================================================
@pytest.mark.parametrize("raw", [
    "OPEN - Connecting", "OPEN - Pending Meeting", "OPEN - Meeting Booked"])
def test_open_statuses_map_to_open_working(raw):
    assert mql.classify_mql_status(raw) == mql.CATEGORY_OPEN_WORKING


def test_sales_qualified_signal_does_not_define_canonical_sql():
    """A CLOSED - Sales Qualified status with NO lifecycle SQL entry is not an SQL."""
    rows = [_row("1", status="CLOSED - Sales Qualified", sql=None, lead="2026-07-01")]
    assert _pops(rows)["counts"]["sql"][funnel.SCOPE_ALL_SOURCE] == 0


def test_deal_created_signal_does_not_define_lifecycle_opportunity():
    rows = [_row("1", status="CLOSED - Deal Created", opportunity=None,
                 lead="2026-07-01")]
    assert _pops(rows)["counts"]["opportunity"][funnel.SCOPE_ALL_SOURCE] == 0


def test_null_status_is_no_verdict_and_unknown_is_unmapped():
    assert mql.classify_mql_status(None) == mql.CATEGORY_NO_VERDICT
    assert mql.classify_mql_status("CLOSED - Brand New") == mql.CATEGORY_UNMAPPED


@pytest.mark.parametrize("raw,category", [
    ("CLOSED - Bad Product Fit", mql.CATEGORY_BAD_FIT),
    ("CLOSED - Sales Disqualified", mql.CATEGORY_DISQUALIFIED),
    ("CLOSED - Job Seeker", mql.CATEGORY_CONTACT_QUALITY),
    ("CLOSED - Bad Contact", mql.CATEGORY_CONTACT_QUALITY),
    ("CLOSED - No Response", mql.CATEGORY_NO_RESPONSE),
    ("DICARDED", mql.CATEGORY_DISCARDED),
    ("RESELLER", mql.CATEGORY_RESELLER),
])
def test_disqualified_other_statuses_are_represented_honestly(raw, category):
    assert mql.classify_mql_status(raw) == category


def test_every_operational_category_is_offered_in_the_ui():
    labels = _APP_JS.split("const LEADS_STATUS_LABELS = {")[1].split("};")[0]
    for category in mql.ALL_CATEGORIES:
        assert category in labels, category


def test_disqualified_view_is_visually_distinct_from_funnel_stages():
    assert "LEADS_VIEW_OTHER" in _APP_JS
    assert "search-terms-tab--muted" in _APP_JS


# =============================================================================
# §38 — Frontend truth states
# =============================================================================
def test_mismatch_withholds_the_count():
    populations = _pops([_row("1", sql="2026-07-05")])
    contact = populations["events"]["sql"][0]
    contact["scopes"][funnel.SCOPE_GOOGLE_ADS_SOURCE] = False
    contact["scopes"][funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE] = True
    assert funnel.reconciliation_status(
        populations, available=True)["status"] == "mismatch"


def test_frontend_withholds_counts_on_mismatch():
    strip = _APP_JS.split("function leadsFunnelStripHtml(")[1].split("\nfunction ")[0]
    assert 'reconciliation || {}).status === "mismatch"' in strip
    assert "(!available || mismatch) ? null : block.count" in strip


def test_frontend_surfaces_partial_and_unavailable_states():
    truth = _APP_JS.split("function leadsTruthStateHtml(")[1].split("\nfunction ")[0]
    assert '"mismatch"' in truth and '"partial"' in truth
    assert "withheld" in truth
    assert "not zero" in truth


def test_incomplete_bootstrap_is_surfaced_and_all_time_is_qualified():
    truth = _APP_JS.split("function leadsTruthStateHtml(")[1].split("\nfunction ")[0]
    assert "Historical CRM sync in progress" in truth
    assert "all_time" in truth
    assert "does not yet represent complete history" in truth


def test_conversions_render_only_when_cohort_safe():
    strip = _APP_JS.split("function leadsFunnelStripHtml(")[1].split("\nfunction ")[0]
    assert 'conv.basis === "cohort"' in strip
    assert "conv.available" in strip


def test_dashboard_conversion_helper_requires_cohort_basis():
    helper = _APP_JS.split("function dashCohortConversion(")[1].split("\n}")[0]
    assert 'found.basis !== "cohort"' in helper
    assert "found.available" in helper


# =============================================================================
# §21/§22 — Dashboard migration
# =============================================================================
def test_dashboard_funnel_uses_canonical_lifecycle_counts():
    block = _APP_JS.split("function renderDashFunnel(")[1].split("\n}")[0]
    assert "k.lifecycle_leads" in block
    assert "k.lifecycle_mqls" in block
    assert "k.lifecycle_sqls" in block
    # The naked campaign-attributable "SQLs" is gone from the funnel strip.
    assert "k.sqls" not in block


def test_dashboard_keeps_revenue_customers_on_the_revenue_contract():
    """§14 — lifecycle customers must NEVER silently replace revenue customers."""
    block = _APP_JS.split("function renderDashFunnel(")[1].split("\n}")[0]
    assert "k.customers" in block
    assert "k.lifecycle_customers" not in block
    assert "Closed-won deals" in block


def test_dashboard_payload_exposes_lifecycle_and_revenue_separately():
    source = (_ROOT / "services" / "dashboard_overview_service.py").read_text()
    assert '"lifecycle_customers": lifecycle_funnel.get("customer")' in source
    assert '"customers": customers,' in source
    assert '"lifecycle_sqls"' in source


def test_lifecycle_block_fails_closed_without_a_database():
    from services import dashboard_overview_service as dash

    block = dash._lifecycle_funnel_block("current_quarter")  # noqa: SLF001
    assert block["available"] is False
    for event in ("lead", "mql", "sql", "opportunity", "customer"):
        assert block[event] is None


def test_dashboard_lifecycle_scope_is_all_source():
    source = (_ROOT / "services" / "dashboard_overview_service.py").read_text()
    funnel_block = source.split("def _lifecycle_funnel_block(")[1]
    assert "SCOPE_ALL_SOURCE" in funnel_block


def test_campaign_attributable_sqls_stay_explicitly_named():
    """§22 — a narrower subset may never sit under a bare "SQLs" label."""
    source = (_ROOT / "services" / "dashboard_overview_service.py").read_text()
    assert '"sqls_scope": _canon.SCOPE_CAMPAIGN_ATTRIBUTABLE' in source
    assert '"google_ads_source_sqls"' in source


# =============================================================================
# §39 — Privacy
# =============================================================================
def test_contact_row_payload_never_contains_an_email():
    payload = funnel._contact_row_payload(_row("1", sql="2026-07-10"), "sql", True)  # noqa: SLF001
    assert "email" not in payload
    assert not any("@" in str(v) for v in payload.values() if isinstance(v, str))


def test_contact_page_columns_exclude_email_and_free_text():
    from db.crm_funnel_repository import _CONTACT_PAGE_COLUMNS

    for column in _CONTACT_PAGE_COLUMNS:
        assert "email" not in column.lower()
        assert "comment" not in column.lower()


def test_leads_ui_does_not_render_an_email_field():
    page = _APP_JS.split("// ── Leads — canonical HubSpot CRM funnel explorer")[1]
    page = page.split("\n// ── ")[0]
    assert "email" not in page.lower()


def test_contact_id_is_available_but_not_a_prominent_table_column():
    page = _APP_JS.split("function leadsRenderContacts(")[1].split("\nfunction ")[0]
    # The table header row does not expose the raw HubSpot id...
    header = page.split("<thead>")[1].split("</thead>")[0]
    assert "contact_id" not in header
    # ...but the drawer keeps it for admin/debugging.
    drawer = _APP_JS.split("function leadsOpenDrawer(")[1].split("\nfunction ")[0]
    assert "contact_id" in drawer


# =============================================================================
# §29/§30 — Performance and ordering
# =============================================================================
def test_contact_page_is_server_side_paginated_and_bounded():
    from db import crm_funnel_repository as repo

    assert repo.MAX_CONTACT_PAGE_SIZE == 100
    source = (_ROOT / "db" / "crm_funnel_repository.py").read_text()
    page_fn = source.split("def fetch_funnel_contact_page(")[1].split("\ndef ")[0]
    assert "LIMIT %s OFFSET %s" in page_fn
    assert "COUNT(*)" in page_fn


def test_page_size_is_capped_at_the_api_boundary():
    assert "page_size: int = Query(default=50, ge=1, le=100)" in _SERVER_PY


def test_default_ordering_is_newest_event_first_and_deterministic():
    source = (_ROOT / "db" / "crm_funnel_repository.py").read_text()
    page_fn = source.split("def fetch_funnel_contact_page(")[1].split("\ndef ")[0]
    assert "ORDER BY {date_column} DESC, contact_id ASC" in page_fn


def test_ordering_uses_the_selected_event_date_not_createdate():
    source = (_ROOT / "db" / "crm_funnel_repository.py").read_text()
    page_fn = source.split("def fetch_funnel_contact_page(")[1].split("\ndef ")[0]
    assert "date_column = EVENT_DATE_COLUMN[event]" in page_fn
    assert "created_at DESC" not in page_fn


# =============================================================================
# §15 — One canonical API contract
# =============================================================================
def test_no_competing_lead_api_was_introduced():
    for forbidden in ("/api/leads-v2", "/api/new-leads", "/api/leads/v2"):
        assert forbidden not in _SERVER_PY


def test_leads_page_consumes_only_the_canonical_crm_funnel_family():
    page = _APP_JS.split("// ── Leads — canonical HubSpot CRM funnel explorer")[1]
    page = page.split("\n// ── ")[0]
    endpoints = set(re.findall(r"/api/[a-z0-9/\-]+", page))
    assert endpoints <= {
        "/api/crm-funnel", "/api/crm-funnel/contacts",
        "/api/crm-funnel/operational-status",
    }, endpoints


def test_legacy_leads_endpoint_is_retained_for_other_consumers():
    """§33 — /api/leads still has consumers (country summary/legacy surfaces),
    so it is NOT deleted here; PR-ADS-153G retires it."""
    assert '@app.get("/api/leads")' in _SERVER_PY


# =============================================================================
# §13 — Window contract
# =============================================================================
def test_leads_reuses_the_shared_business_window_resolver():
    service = (_ROOT / "services" / "canonical_crm_funnel_service.py").read_text()
    contacts_fn = service.split("def contacts(")[1].split("\ndef ")[0]
    assert "resolve_window_contract(window_type, window_key" in contacts_fn
    # No bespoke date arithmetic in the new page path.
    assert "timedelta" not in contacts_fn


def test_leads_window_selector_offers_the_business_vocabulary():
    section = _INDEX_HTML.split('id="page-leads"')[1].split("</section>")[0]
    for key in ("current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"):
        assert key in section
    assert "data-business-window-select" in section


def test_leads_window_selector_is_actually_wired_to_the_shared_handler():
    """A window control that does not change the window is a lying control.

    ``data-business-window-select`` only keeps the select's *value* in sync
    when some other surface changes the window; on its own it never listens.
    """
    assert 'const leadsWindow = document.getElementById("leads-window");' in _APP_JS
    block = _APP_JS.split('const leadsWindow = document.getElementById("leads-window");')[1]
    block = block.split("\n\n")[0]
    assert "leadsWindow.value = getRoasBusinessWindow();" in block
    assert 'leadsWindow.addEventListener("change", handleBusinessWindowSelectChange);' in block


def test_business_window_change_reloads_leads_not_the_default_branch():
    """Leads must not fall through to the switch's loadRoasCampaigns default."""
    handler = _APP_JS.split("function handleBusinessWindowSelectChange(")[1].split("\n}")[0]
    assert 'case "leads":' in handler
    leads_case = handler.split('case "leads":')[1].split("break;")[0]
    assert "loadLeads()" in leads_case
    # A new window is a new cohort — paging must restart, never carry an
    # offset that belonged to the previous window.
    assert "_leadsPage = 1" in leads_case


def test_no_bespoke_date_control_was_added():
    section = _INDEX_HTML.split('id="page-leads"')[1].split("</section>")[0]
    assert 'type="date"' not in section


# =============================================================================
# §28 — No vanity metrics
# =============================================================================
def test_no_vanity_metrics_on_the_leads_page():
    page = _APP_JS.split("// ── Leads — canonical HubSpot CRM funnel explorer")[1]
    page = page.split("\n// ── ")[0].lower()
    for banned in ("engagement score", "health score", "lead score",
                   "propensity", "ai score"):
        assert banned not in page
