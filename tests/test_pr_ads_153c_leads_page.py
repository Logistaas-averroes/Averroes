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
    pairs = [("PAID_SEARCH", "brand-us"), ("ORGANIC_SEARCH", None),
             ("PAID_SOCIAL", "linkedin"), (None, None)]
    google = funnel.resolve_source_pair_allowlist("google_ads", pairs)
    assert google == [("PAID_SEARCH", "brand-us")]
    assert funnel.resolve_source_pair_allowlist(None, pairs) is None

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
    """PR-ADS-153C §3 held this page open FOR PR-ADS-153D, which has now
    consolidated it. Reachability is what mattered, and it survives: the old
    URL resolves to the canonical Flagged view rather than dead-ending."""
    # The standalone page is gone — nav item, section markup and loader.
    assert 'data-page="waste"' not in _INDEX_HTML
    assert 'id="page-waste"' not in _INDEX_HTML
    # But the route still resolves, to Search Terms → Flagged.
    assert 'waste: { page: "search-terms", tab: "flagged" }' in _APP_JS
    # And the API remains as a documented compatibility adapter for 153G.
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


def _strip_js_comments(source: str) -> str:
    """Drop // line comments and /* */ block comments — prose is not code.

    The Leads page legitimately documents Email Marketing as an acquisition
    channel; what must never exist is a code path that reads or renders an email
    ADDRESS. Stripping comments keeps this guard aimed at the executable code.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_blocks, flags=re.M)


def test_leads_ui_does_not_render_an_email_field():
    page = _APP_JS.split("// ── Leads — canonical HubSpot CRM funnel explorer")[1]
    page = page.split("\n// ── ")[0]
    assert "email" not in _strip_js_comments(page).lower()


def test_no_leads_code_path_reads_an_email_field():
    """Belt-and-braces: no email property access anywhere in the page module,
    comment-stripped or not."""
    page = _APP_JS.split("// ── Leads — canonical HubSpot CRM funnel explorer")[1]
    page = page.split("\n// ── ")[0].lower()
    for accessor in (".email", '"email"', "'email'", "[email]", "email:"):
        assert accessor not in page


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


# =============================================================================
# PR-ADS-153C follow-up §1 — ONE population behind the Source selector
# =============================================================================
# The Source selector previously filtered only the contact rows, leaving the
# funnel headline on All Sources. "Source = Organic" above an all-source funnel
# is a lie about which population the numbers describe.

_MIXED_SOURCE_ROWS = [
    _row("g1", source="PAID_SEARCH", sql="2026-07-05"),
    _row("g2", source="PAID_SEARCH", sql="2026-07-06"),
    _row("o1", source="ORGANIC_SEARCH", campaign="google", sql="2026-07-07"),
    _row("s1", source="PAID_SOCIAL", campaign="linkedin", sql="2026-07-08"),
]


def test_funnel_counts_honour_the_selected_acquisition_group():
    counts = _pops(_MIXED_SOURCE_ROWS)["counts"]
    assert counts["sql"][funnel.SCOPE_ALL_SOURCE] == 4

    organic = _pops(_MIXED_SOURCE_ROWS, acquisition_group="organic")
    assert organic["counts"]["sql"][funnel.SCOPE_ALL_SOURCE] == 1
    assert [c["contact_id"] for c in organic["events"]["sql"]] == ["o1"]


def test_funnel_counts_and_contact_rows_use_the_same_selected_source():
    """The aggregate and the row list must agree contact-for-contact."""
    for group, expected in (("google_ads", {"g1", "g2"}),
                            ("organic", {"o1"}),
                            ("other_paid", {"s1"})):
        populations = _pops(_MIXED_SOURCE_ROWS, acquisition_group=group)
        headline = populations["counts"]["sql"][funnel.SCOPE_ALL_SOURCE]
        rows_in_view = {c["contact_id"] for c in populations["events"]["sql"]}
        # The rows the table would page over ARE the rows the headline counted.
        assert rows_in_view == expected
        assert headline == len(expected)


def test_cohort_conversions_are_computed_on_the_filtered_population():
    rows = [
        _row("g", source="PAID_SEARCH", mql_="2026-07-01", sql="2026-07-20"),
        _row("o", source="ORGANIC_SEARCH", campaign="google", mql_="2026-07-02"),
    ]
    all_source = funnel.cohort_conversion(_pops(rows), "mql", "sql")
    assert all_source["cohort_size"] == 2
    assert all_source["rate_pct"] == 50.0

    google_only = funnel.cohort_conversion(
        _pops(rows, acquisition_group="google_ads"), "mql", "sql")
    assert google_only["cohort_size"] == 1
    assert google_only["rate_pct"] == 100.0


def test_coverage_reports_the_filtered_population():
    populations = _pops(_MIXED_SOURCE_ROWS, acquisition_group="google_ads")
    assert populations["coverage"]["contacts_considered"] == 2
    assert populations["coverage"]["acquisition_group"] == "google_ads"


def test_aggregate_contract_accepts_and_validates_acquisition_group():
    import inspect
    signature = inspect.signature(funnel.build)
    assert "acquisition_group" in signature.parameters
    assert "acquisition_group" in inspect.signature(funnel.contacts).parameters
    assert "acquisition_group" in inspect.signature(
        funnel.operational_status_breakdown).parameters
    with pytest.raises(ValueError):
        funnel.build("business", "current_quarter", acquisition_group="nonsense")


def test_aggregate_endpoint_exposes_acquisition_group():
    block = _SERVER_PY.split('@app.get("/api/crm-funnel")')[1].split("@app.")[0]
    assert "acquisition_group" in block
    ops = _SERVER_PY.split('@app.get("/api/crm-funnel/operational-status")')[1]
    assert "acquisition_group" in ops.split("@app.")[0]


def test_frontend_sends_the_source_filter_to_the_funnel_not_just_the_rows():
    load_funnel = _APP_JS.split("async function leadsLoadFunnel(")[1].split("\n}")[0]
    assert "/api/crm-funnel?" in load_funnel
    assert 'params.set("acquisition_group", _leadsSourceGroup)' in load_funnel


def test_source_selector_reloads_the_funnel():
    controls = _APP_JS.split("function leadsRenderControls(")[1].split("\nfunction ")[0]
    source_handler = controls.split('document.getElementById("leads-source")')[1]
    source_handler = source_handler.split("statusSel")[0]
    assert "leadsLoadFunnel()" in source_handler


def test_funnel_strip_discloses_the_active_source_filter():
    strip = _APP_JS.split("function leadsFunnelStripHtml(")[1].split("\nfunction ")[0]
    assert "acquisition_group_label" in strip
    assert "Source:" in strip


# =============================================================================
# PR-ADS-153C follow-up §3 — the FULL canonical source-classification contract
# =============================================================================
# `hs_analytics_source` alone is not the contract. The drill-down
# (`hs_analytics_source_data_1`) is what routes Offline Sources to its real
# group, and dropping it made Leads disagree with Revenue by Source.

@pytest.mark.parametrize("primary,detail,expected", [
    # Offline Sources is ambiguous until its drill-down is read.
    ("Offline Sources", "Events", "other_paid"),
    ("Offline Sources", "SalesNash", "other_paid"),
    ("Offline Sources", "reseller", "organic"),
    ("Offline Sources", "referral", "organic"),
    ("Offline Sources", "direct email", "organic"),
    ("Offline Sources", "CRM migration", "offline"),
    # Unambiguous primaries.
    ("PAID_SOCIAL", "linkedin", "other_paid"),
    ("EMAIL_MARKETING", "hubspot", "other_paid"),
    ("ORGANIC_SEARCH", "google", "organic"),
    ("REFERRALS", "partner.com", "organic"),
    ("PAID_SEARCH", "Brand - US", "google_ads"),
    # Missing / unknown is Unclassified — never defaulted to Organic.
    (None, None, "unclassified"),
    ("", "", "unclassified"),
    ("SOMETHING_NEW", "x", "unclassified"),
])
def test_leads_uses_the_full_primary_plus_detail_classification(
        primary, detail, expected):
    row = _row("1", source=primary, campaign=detail)
    assert funnel.derive_acquisition_group(row) == expected


@pytest.mark.parametrize("primary,detail", [
    ("Offline Sources", "Events"),
    ("Offline Sources", "SalesNash"),
    ("Offline Sources", "reseller"),
    ("Offline Sources", "referral"),
    ("Offline Sources", "direct email"),
    ("PAID_SOCIAL", "linkedin"),
    ("EMAIL_MARKETING", "hubspot"),
    ("ORGANIC_SEARCH", "google"),
    ("PAID_SEARCH", "Brand - US"),
    (None, None),
])
def test_leads_and_revenue_by_source_agree_on_the_same_contact(primary, detail):
    """Two pages, one taxonomy. Same evidence must give the same group."""
    from analysis.source_classification import classify_source
    from services.source_attribution_service import classify_contact_row

    revenue_side = classify_contact_row({
        "id": "1",
        "properties": {"hs_analytics_source": primary,
                       "hs_analytics_source_data_1": detail},
    })["acquisition_group"]
    leads_side = funnel.derive_acquisition_group(
        _row("1", source=primary, campaign=detail))
    assert leads_side == revenue_side == classify_source(primary, detail)


def test_leads_reuses_the_taxonomy_module_rather_than_redefining_it():
    service = (_ROOT / "services" / "canonical_crm_funnel_service.py").read_text()
    assert "from analysis.source_classification import" in service
    assert "classify_source_taxonomy" in service
    taxonomy_fn = service.split("def source_taxonomy(")[1].split("\ndef ")[0]
    # Delegation only — no local rule table.
    assert "classify_source_taxonomy(" in taxonomy_fn
    assert "hs_analytics_source_data_1" in taxonomy_fn


def test_no_classification_call_drops_the_detail_evidence():
    """The `classify_source(primary, None)` shape must not return here."""
    service = (_ROOT / "services" / "canonical_crm_funnel_service.py").read_text()
    assert 'classify_source(row.get("hs_analytics_source"), None)' not in service


def test_source_allowlist_is_resolved_over_evidence_pairs():
    """Collapsing to the primary alone would re-lose the drill-down."""
    pairs = [("Offline Sources", "Events"),
             ("Offline Sources", "reseller"),
             ("Offline Sources", "CRM migration"),
             ("PAID_SEARCH", "Brand - US")]
    assert funnel.resolve_source_pair_allowlist("other_paid", pairs) == [
        ("Offline Sources", "Events")]
    assert funnel.resolve_source_pair_allowlist("organic", pairs) == [
        ("Offline Sources", "reseller")]
    assert funnel.resolve_source_pair_allowlist("offline", pairs) == [
        ("Offline Sources", "CRM migration")]


def test_repository_filters_on_the_pair_not_the_primary_alone():
    repo_src = (_ROOT / "db" / "crm_funnel_repository.py").read_text()
    assert "source_pairs_in" in repo_src
    predicate = repo_src.split("def _append_source_pair_filter(")[1].split("\ndef ")[0]
    assert "unnest(" in predicate
    # NULL sources/details are common; NULL = NULL would silently drop them.
    assert "IS NOT DISTINCT FROM" in predicate
    # An empty allow-list still filters everything out.
    assert "FALSE" in predicate


# =============================================================================
# PR-ADS-153C follow-up §2 — no fabricated Campaign / Keyword
# =============================================================================
# hs_analytics_source_data_1/2 carry Google Ads campaign/keyword semantics ONLY
# for Paid Search contacts. Everywhere else they are drill-down text.

@pytest.mark.parametrize("primary,detail,group", [
    ("ORGANIC_SEARCH", "google", "organic"),
    ("PAID_SOCIAL", "linkedin", "other_paid"),
    ("EMAIL_MARKETING", "hubspot", "other_paid"),
    ("Offline Sources", "Events", "other_paid"),
    ("Offline Sources", "CRM migration", "offline"),
    ("REFERRALS", "partner.com", "organic"),
])
def test_non_google_contacts_never_get_campaign_or_keyword(primary, detail, group):
    row = _row("1", source=primary, campaign=detail, keyword="whatever",
               sql="2026-07-05")
    payload = funnel._contact_row_payload(row, "sql", True)  # noqa: SLF001
    assert payload["acquisition_group"] == group
    assert payload["campaign"] is None
    assert payload["keyword"] is None
    assert payload["campaign_available"] is False
    assert payload["campaign_semantics"] == "not_google_ads_source"
    # The real evidence is still present — as canonical source information.
    assert payload["source_channel"]
    assert payload["source_platform"]
    assert payload["source_detail_raw"] == detail


def test_google_ads_contact_with_proven_identity_keeps_campaign_and_keyword():
    row = _row("1", source="PAID_SEARCH", campaign="Brand - US", keyword="tms",
               sql="2026-07-05")
    payload = funnel._contact_row_payload(  # noqa: SLF001
        row, "sql", True, lambda label: (True, None))
    assert payload["campaign"] == "Brand - US"
    assert payload["keyword"] == "tms"
    assert payload["campaign_available"] is True
    assert payload["campaign_semantics"] == "google_ads_campaign"


def test_google_ads_contact_without_identity_contract_is_unavailable_not_absent():
    row = _row("1", source="PAID_SEARCH", sql="2026-07-05")
    payload = funnel._contact_row_payload(row, "sql", False)  # noqa: SLF001
    assert payload["campaign"] is None
    assert payload["campaign_semantics"] == "campaign_identity_unavailable"


def test_google_ads_contact_whose_label_does_not_resolve_is_unresolved():
    row = _row("1", source="PAID_SEARCH", campaign="(not set)", sql="2026-07-05")
    payload = funnel._contact_row_payload(  # noqa: SLF001
        row, "sql", True, lambda label: (False, "unsafe_campaign"))
    assert payload["campaign"] is None
    assert payload["campaign_semantics"] == "campaign_identity_unresolved"


def test_keyword_requires_a_real_label_not_whitespace():
    row = _row("1", source="PAID_SEARCH", keyword="   ", sql="2026-07-05")
    payload = funnel._contact_row_payload(  # noqa: SLF001
        row, "sql", True, lambda label: (True, None))
    assert payload["keyword"] is None


def test_table_shows_canonical_source_columns_for_non_google_contacts():
    render = _APP_JS.split("function leadsRenderContacts(")[1].split("\nfunction ")[0]
    header = render.split("<thead>")[1].split("</thead>")[0]
    assert "Channel / Platform" in header
    assert "leadsChannelPlatformHtml(r)" in render
    assert "leadsCampaignCellHtml(r, \"campaign\")" in render
    assert "leadsCampaignCellHtml(r, \"keyword\")" in render


def test_campaign_cell_states_why_a_label_is_withheld():
    cell = _APP_JS.split("function leadsCampaignCellHtml(")[1].split("\nfunction ")[0]
    for semantics in ("not_google_ads_source", "campaign_identity_unavailable",
                      "campaign_identity_unresolved"):
        assert semantics in cell
    assert "Not applicable" in cell
    assert "Unavailable" in cell


def test_drawer_surfaces_neutral_source_detail_not_a_fake_campaign():
    drawer = _APP_JS.split("function leadsOpenDrawer(")[1].split("\nfunction ")[0]
    assert "source_detail_raw" in drawer
    assert "Source detail" in drawer
    assert "source_channel_label" in drawer
    assert "source_platform_label" in drawer
    assert "not_google_ads_source" in drawer


# =============================================================================
# PR-ADS-153C follow-up §4 — no lying controls on Disqualified / Other
# =============================================================================
def test_status_and_company_controls_are_hidden_on_the_operational_view():
    fn = _APP_JS.split("function leadsSyncControlVisibility(")[1].split("\n}")[0]
    assert "_leadsView === LEADS_VIEW_OTHER" in fn
    assert "statusControl.hidden = operational" in fn
    assert "searchControl.hidden = operational" in fn


def test_control_visibility_runs_on_every_view_switch():
    fn = _APP_JS.split("async function leadsRenderActiveView(")[1].split("\n}")[0]
    assert "leadsSyncControlVisibility()" in fn


def test_each_leads_filter_is_wrapped_so_it_can_be_hidden_with_its_label():
    section = _INDEX_HTML.split('id="page-leads"')[1].split("</section>")[0]
    for control_id in ("leads-status-control", "leads-search-control"):
        assert control_id in section
    controls = section.split('id="leads-table-controls"')[1].split("</div>")[0]
    # The label travels with its control, so hiding one cannot orphan the other.
    assert controls.index('id="leads-status-control"') < controls.index('for="leads-status"')


def test_source_filter_actually_filters_the_operational_view():
    fn = _APP_JS.split("async function leadsRenderOperational(")[1].split("\nasync function ")[0]
    assert 'params.set("acquisition_group", _leadsSourceGroup)' in fn
    assert "acquisition_group_label" in fn


def test_operational_status_service_applies_the_same_allowlist():
    service = (_ROOT / "services" / "canonical_crm_funnel_service.py").read_text()
    fn = service.split("def operational_status_breakdown(")[1].split("\ndef ")[0]
    # Delegated to the one shared resolver rather than re-derived locally.
    assert "resolve_population_filters(scope, acquisition_group" in fn
    assert 'source_pairs_in=filters["source_pairs_in"]' in fn
    resolver = service.split("def resolve_population_filters(")[1].split("\ndef ")[0]
    assert "resolve_source_pair_allowlist(" in resolver


# =============================================================================
# PR-ADS-153C follow-up §5 — the operational view honours the Scope selector
# =============================================================================
# The Disqualified / Other view keeps Scope visible and says it applies. It must
# therefore consume the SAME canonical scope contract as the funnel strip above
# it, or its counts silently describe a broader population than their headline.

def test_operational_status_accepts_and_validates_scope():
    import inspect
    signature = inspect.signature(funnel.operational_status_breakdown)
    assert "scope" in signature.parameters
    assert signature.parameters["scope"].default == funnel.SCOPE_ALL_SOURCE
    with pytest.raises(ValueError):
        funnel.operational_status_breakdown(
            "business", "current_quarter", scope="not_a_scope")


def test_operational_status_endpoint_exposes_scope():
    block = _SERVER_PY.split('@app.get("/api/crm-funnel/operational-status")')[1]
    block = block.split("@app.")[0]
    assert re.search(r'scope:\s*str\s*=\s*Query\(default="all_source"\)', block)
    assert "scope=scope" in block


def test_frontend_sends_the_scope_to_the_operational_view():
    fn = _APP_JS.split("async function leadsRenderOperational(")[1]
    fn = fn.split("\nasync function ")[0]
    assert "scope: _leadsScope" in fn
    assert "scope_label" in fn


def test_scope_change_reloads_the_operational_view_too():
    controls = _APP_JS.split("function leadsRenderControls(")[1].split("\nfunction ")[0]
    scope_handler = controls.split('document.getElementById("leads-scope")')[1]
    scope_handler = scope_handler.split("sourceSel")[0]
    # loadLeads() reloads the funnel AND re-renders whichever view is active.
    assert "loadLeads()" in scope_handler
    active = _APP_JS.split("async function leadsRenderActiveView(")[1].split("\n}")[0]
    assert "leadsRenderOperational()" in active


def test_operational_view_reports_unavailable_scope_not_zero_counts():
    fn = _APP_JS.split("async function leadsRenderOperational(")[1]
    fn = fn.split("\nasync function ")[0]
    assert "campaign_identity_unavailable" in fn
    assert "not zero" in fn


# ── The shared resolver is the mechanism, not a convention ──────────────────
def test_one_resolver_serves_both_the_contact_page_and_the_operational_view():
    service = (_ROOT / "services" / "canonical_crm_funnel_service.py").read_text()
    contacts_fn = service.split("\ndef contacts(")[1].split("\ndef ")[0]
    operational_fn = service.split(
        "\ndef operational_status_breakdown(")[1].split("\ndef ")[0]
    for fn in (contacts_fn, operational_fn):
        assert "resolve_population_filters(scope, acquisition_group" in fn
    # Neither view re-derives the scope→SQL translation for itself.
    for fn in (contacts_fn, operational_fn):
        assert "resolve_campaign_allowlist(" not in fn


class _FakeRepo:
    """Distinct facets only — the pure part of the population-filter contract."""

    SOURCE_PAIRS = [
        ("PAID_SEARCH", "Brand - US"),
        ("PAID_SEARCH", "(not set)"),
        ("ORGANIC_SEARCH", "google"),
        ("Offline Sources", "Events"),
    ]

    @staticmethod
    def fetch_distinct_facets():
        return {"available": True,
                "source_pairs": list(_FakeRepo.SOURCE_PAIRS),
                "campaigns": ["Brand - US", "(not set)", "google", "Events"]}


def _resolver(label):
    return (label == "Brand - US", None if label == "Brand - US" else "unsafe")


def _filters(scope, group=None):
    return funnel.resolve_population_filters(scope, group, _resolver, _FakeRepo)


def test_all_source_scope_applies_no_scope_constraint():
    filters = _filters(funnel.SCOPE_ALL_SOURCE)
    assert filters["source_pairs_in"] is None
    assert filters["campaigns_in"] is None
    assert filters["require_keyword"] is False


def test_google_ads_scope_restricts_to_the_google_ads_population():
    filters = _filters(funnel.SCOPE_GOOGLE_ADS_SOURCE)
    assert set(filters["source_pairs_in"]) == {
        ("PAID_SEARCH", "Brand - US"), ("PAID_SEARCH", "(not set)")}
    assert filters["campaigns_in"] is None
    assert filters["require_keyword"] is False


def test_campaign_scope_requires_a_proven_campaign_identity():
    filters = _filters(funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE)
    assert set(filters["source_pairs_in"]) == {
        ("PAID_SEARCH", "Brand - US"), ("PAID_SEARCH", "(not set)")}
    assert filters["campaigns_in"] == ["Brand - US"]
    assert filters["require_keyword"] is False


def test_keyword_scope_requires_identity_and_keyword_evidence():
    filters = _filters(funnel.SCOPE_KEYWORD_ATTRIBUTABLE)
    assert filters["campaigns_in"] == ["Brand - US"]
    assert filters["require_keyword"] is True


def test_scope_filters_are_progressively_narrower():
    """all_source ⊇ google_ads_source ⊇ campaign ⊇ keyword, as filters."""
    all_source = _filters(funnel.SCOPE_ALL_SOURCE)
    google = _filters(funnel.SCOPE_GOOGLE_ADS_SOURCE)
    campaign = _filters(funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE)
    keyword = _filters(funnel.SCOPE_KEYWORD_ATTRIBUTABLE)

    assert all_source["source_pairs_in"] is None  # unconstrained = widest
    assert set(campaign["source_pairs_in"]) <= set(google["source_pairs_in"])
    assert set(keyword["source_pairs_in"]) <= set(campaign["source_pairs_in"])
    # Each step adds a constraint and never removes one.
    assert google["campaigns_in"] is None and campaign["campaigns_in"] is not None
    assert keyword["require_keyword"] and not campaign["require_keyword"]


def test_scope_and_source_intersect_rather_than_replace_each_other():
    both = _filters(funnel.SCOPE_GOOGLE_ADS_SOURCE, "google_ads")
    assert set(both["source_pairs_in"]) == {
        ("PAID_SEARCH", "Brand - US"), ("PAID_SEARCH", "(not set)")}
    # A non-Google group under a Google-only scope is a proven zero, not an
    # ignored filter.
    contradiction = _filters(funnel.SCOPE_GOOGLE_ADS_SOURCE, "organic")
    assert contradiction["source_pairs_in"] == []
    # And Source alone still narrows an unconstrained scope.
    organic = _filters(funnel.SCOPE_ALL_SOURCE, "organic")
    assert organic["source_pairs_in"] == [("ORGANIC_SEARCH", "google")]


def test_identity_unavailable_makes_narrow_operational_scopes_unavailable():
    assert funnel._identity_dependent_scope_unavailable(  # noqa: SLF001
        funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE, False) is True
    assert funnel._identity_dependent_scope_unavailable(  # noqa: SLF001
        funnel.SCOPE_KEYWORD_ATTRIBUTABLE, False) is True
    # The broad scopes are unaffected by an attribution outage.
    assert funnel._identity_dependent_scope_unavailable(  # noqa: SLF001
        funnel.SCOPE_ALL_SOURCE, False) is False
    assert funnel._identity_dependent_scope_unavailable(  # noqa: SLF001
        funnel.SCOPE_GOOGLE_ADS_SOURCE, False) is False


def test_operational_repository_takes_the_same_filters_as_the_contact_page():
    import inspect
    from db import crm_funnel_repository as repo

    counts_params = set(inspect.signature(
        repo.fetch_operational_status_counts).parameters)
    page_params = set(inspect.signature(repo.fetch_funnel_contact_page).parameters)
    for shared in ("source_pairs_in", "campaigns_in", "require_keyword"):
        assert shared in counts_params
        assert shared in page_params


def test_both_repository_queries_share_one_predicate_implementation():
    repo_src = (_ROOT / "db" / "crm_funnel_repository.py").read_text()
    for fn_name in ("fetch_funnel_contact_page", "fetch_operational_status_counts"):
        fn = repo_src.split(f"def {fn_name}(")[1].split("\ndef ")[0]
        assert "_append_source_pair_filter(where, params" in fn
        assert "_append_campaign_filter(where, params" in fn
        assert "_KEYWORD_PRESENT_SQL" in fn
