"""
tests/test_pr_ads_153b_canonical_funnel.py

PR-ADS-153B — Canonical CRM Funnel Truth. Database-free unit tests for the
lifecycle taxonomy, the single mql_status mapping, contact normalisation, the
canonical funnel service (events, scopes, cohort-safe conversions), the
legacy-vs-lifecycle reconciliation, sync orchestration, and governance.

Covers the required suites §32 (ingestion contract), §33 (lifecycle events),
§34 (mql_status mapping), §36 (all-source scope) and §37 (reconciliation).
Latest-state ordering (§35) and upsert idempotency are proven against a real
PostgreSQL cluster in tests/test_pr_ads_153b_pg_integration.py.

Run with:
    python -m pytest tests/test_pr_ads_153b_canonical_funnel.py -v
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import crm_lifecycle as lifecycle  # noqa: E402
from analysis import mql_status_taxonomy as mql  # noqa: E402
from connectors.hubspot_pull import (  # noqa: E402
    CONTACT_FUNNEL_PROPERTIES,
    normalize_contact_funnel_row,
)
from services import canonical_crm_funnel_service as funnel  # noqa: E402
from services import crm_funnel_reconciliation_service as recon  # noqa: E402
from services import hubspot_contact_funnel_sync_service as sync  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _hs_contact(contact_id="1", *, lifecyclestage=None, mql_status=None,
                created="2026-01-05T00:00:00Z", modified="2026-07-01T00:00:00Z",
                entered_lead=None, entered_mql=None, entered_sql=None,
                entered_opportunity=None, entered_customer=None,
                source="PAID_SEARCH", campaign="Brand - US", keyword="tms",
                extra=None):
    """A raw HubSpot contact dict as the search API returns it."""
    props = {
        "lifecyclestage": lifecyclestage,
        "mql_status": mql_status,
        "createdate": created,
        "lastmodifieddate": modified,
        "hs_v2_date_entered_lead": entered_lead,
        "hs_v2_date_entered_marketingqualifiedlead": entered_mql,
        "hs_v2_date_entered_salesqualifiedlead": entered_sql,
        "hs_v2_date_entered_opportunity": entered_opportunity,
        "hs_v2_date_entered_customer": entered_customer,
        "hs_analytics_source": source,
        "hs_analytics_source_data_1": campaign,
        "hs_analytics_source_data_2": keyword,
        "company": f"Co {contact_id}",
    }
    props.update(extra or {})
    return {"id": contact_id, "properties": props}


def _row(contact_id="1", *, stage=None, status=None, created="2026-01-05",
         lead=None, mql_=None, sql=None, opportunity=None, customer=None,
         source="PAID_SEARCH", campaign="Brand - US", keyword="tms"):
    """A canonical funnel row as the repository returns it (dates already dates)."""
    def _d(value):
        return date.fromisoformat(value) if value else None

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
        "ip_country": "AE",
        "country": None,
        "has_gclid": True,
    }


def _legacy(contact_id="1", *, status="qualified", created="2026-01-05",
            mql_status="CLOSED - Sales Qualified", company=None):
    return {
        "contact_key": contact_id or "id:9",
        "contact_id": contact_id,
        "status_category": status,
        "contact_created_at": date.fromisoformat(created) if created else None,
        "mql_status": mql_status,
        "company": company or f"Co {contact_id}",
    }


_Q3_START = date(2026, 7, 1)
_Q3_END = date(2026, 9, 30)


def _pops(rows, start=_Q3_START, end=_Q3_END):
    return funnel.build_populations(rows, start, end)


# =============================================================================
# §32 — Ingestion contract
# =============================================================================
def test_lifecycle_property_is_fetched():
    assert "lifecyclestage" in CONTACT_FUNNEL_PROPERTIES


def test_all_five_stage_entry_timestamps_are_fetched():
    for event in lifecycle.FUNNEL_EVENTS:
        assert lifecycle.EVENT_HUBSPOT_PROPERTY[event] in CONTACT_FUNNEL_PROPERTIES


def test_lastmodifieddate_is_fetched():
    """The watermark property must be requested or incremental sync is blind."""
    assert "lastmodifieddate" in CONTACT_FUNNEL_PROPERTIES


def test_mdr_comments_property_is_never_fetched_for_the_funnel():
    """§15 — free text must not be able to reach the typed status property."""
    assert "mql___mdr_comments" not in CONTACT_FUNNEL_PROPERTIES


def test_email_is_never_fetched_for_the_funnel():
    assert "email" not in CONTACT_FUNNEL_PROPERTIES


def test_normalises_an_all_source_contact():
    """An organic contact is ingested — the canonical store is not paid-search only."""
    row = normalize_contact_funnel_row(
        _hs_contact("55", source="ORGANIC_SEARCH", lifecyclestage="lead"))
    assert row["contact_id"] == "55"
    assert row["hs_analytics_source"] == "ORGANIC_SEARCH"
    assert row["lifecycle_stage"] == "lead"


def test_contact_without_identity_is_rejected_not_synthesised():
    assert normalize_contact_funnel_row({"id": "", "properties": {}}) is None
    assert normalize_contact_funnel_row({"properties": {}}) is None


def test_missing_lifecycle_is_preserved_as_null():
    row = normalize_contact_funnel_row(_hs_contact("7", lifecyclestage=None))
    assert row["lifecycle_stage"] is None


def test_unknown_lifecycle_is_preserved_not_guessed():
    row = normalize_contact_funnel_row(
        _hs_contact("8", lifecyclestage="brandNewPortalStage"))
    assert row["lifecycle_stage"] == "brandnewportalstage"
    assert lifecycle.is_known_stage(row["lifecycle_stage"]) is False


def test_missing_stage_dates_stay_null_never_createdate():
    """§3 — createdate must NEVER stand in for a missing funnel event date."""
    row = normalize_contact_funnel_row(
        _hs_contact("9", created="2026-01-05T00:00:00Z", entered_sql=None))
    assert row["created_at"] is not None
    for event in lifecycle.FUNNEL_EVENTS:
        assert row[lifecycle.EVENT_DATE_COLUMN[event]] is None


def test_latest_stage_entry_is_the_max_of_supplied_stage_dates():
    row = normalize_contact_funnel_row(_hs_contact(
        "10", entered_lead="2026-01-05T00:00:00Z",
        entered_mql="2026-03-05T00:00:00Z", entered_sql="2026-07-05T00:00:00Z"))
    assert row["latest_stage_entry_at"] == datetime(2026, 7, 5, tzinfo=timezone.utc)


def test_latest_stage_entry_is_null_without_any_stage_evidence():
    row = normalize_contact_funnel_row(_hs_contact("11"))
    assert row["latest_stage_entry_at"] is None


def test_epoch_millisecond_timestamps_are_parsed():
    """HubSpot returns epoch-ms on some endpoints; both shapes must parse."""
    row = normalize_contact_funnel_row(
        _hs_contact("12", entered_sql="1751673600000"))
    assert row["date_entered_sql"].year == 2025


# =============================================================================
# §34 — mql_status mapping
# =============================================================================
@pytest.mark.parametrize("raw,expected", [
    ("OPEN - Connecting", mql.CATEGORY_OPEN_WORKING),
    ("OPEN - Pending Meeting", mql.CATEGORY_OPEN_WORKING),
    ("OPEN - Meeting Booked", mql.CATEGORY_OPEN_WORKING),
    ("Open", mql.CATEGORY_OPEN_WORKING),
    ("CLOSED - Job Seeker", mql.CATEGORY_CONTACT_QUALITY),
    ("CLOSED - Bad Contact", mql.CATEGORY_CONTACT_QUALITY),
    ("CLOSED - Bad Product Fit", mql.CATEGORY_BAD_FIT),
    ("CLOSED - No Response", mql.CATEGORY_NO_RESPONSE),
    ("CLOSED - Sales Qualified", mql.CATEGORY_SALES_QUALIFIED_SIGNAL),
    ("CLOSED - Sales Disqualified", mql.CATEGORY_DISQUALIFIED),
    ("CLOSED - Deal Created", mql.CATEGORY_DEAL_CREATED_SIGNAL),
    ("DICARDED", mql.CATEGORY_DISCARDED),
    ("RESELLER", mql.CATEGORY_RESELLER),
])
def test_every_live_mql_status_value_is_mapped(raw, expected):
    """Every value the live portal can emit must classify — none may silently
    collapse into `unknown` as they did before PR-ADS-153B."""
    assert mql.classify_mql_status(raw) == expected
    assert mql.is_mapped(raw) is True


def test_dicarded_one_r_spelling_is_the_canonical_internal_value():
    """The HubSpot INTERNAL value is one-R; the label is DISCARDED."""
    assert "DICARDED" in mql.KNOWN_MQL_STATUS_VALUES
    assert mql.classify_mql_status("DICARDED") == mql.CATEGORY_DISCARDED


def test_null_status_is_no_verdict():
    assert mql.classify_mql_status(None) == mql.CATEGORY_NO_VERDICT
    assert mql.classify_mql_status("") == mql.CATEGORY_NO_VERDICT
    assert mql.classify_mql_status("   ") == mql.CATEGORY_NO_VERDICT


def test_unknown_non_null_status_is_unmapped_not_no_verdict():
    """§17 — the two absences are distinct; a NEW production value must surface."""
    assert mql.classify_mql_status("CLOSED - Brand New Value") == mql.CATEGORY_UNMAPPED
    assert mql.classify_mql_status("CLOSED - Brand New Value") != mql.CATEGORY_NO_VERDICT


def test_no_verdict_and_unmapped_are_different_categories():
    assert mql.CATEGORY_NO_VERDICT != mql.CATEGORY_UNMAPPED


def test_mdr_free_text_is_detected_as_pollution():
    text = "Spoke to the prospect, they will revert next week."
    assert mql.classify_mql_status(text) == mql.CATEGORY_UNMAPPED
    assert mql.looks_like_free_text(text) is True


def test_known_status_is_never_flagged_as_free_text():
    for value in mql.KNOWN_MQL_STATUS_VALUES:
        assert mql.looks_like_free_text(value) is False


def test_status_mapping_is_case_insensitive_on_lookup():
    assert mql.classify_mql_status("closed - sales qualified") == (
        mql.CATEGORY_SALES_QUALIFIED_SIGNAL)
    assert mql.canonical_mql_status("closed - sales qualified") == (
        "CLOSED - Sales Qualified")


def test_mdr_comments_never_populate_mql_status():
    """§15 — the removed `mql_status or mql___mdr_comments` fallback must be gone."""
    contact = _hs_contact("20", mql_status=None, extra={
        "mql___mdr_comments": "Called twice, no answer."})
    row = normalize_contact_funnel_row(contact)
    assert row["mql_status"] is None
    assert row["mql_status_category"] == mql.CATEGORY_NO_VERDICT


def test_writers_no_longer_carry_the_comment_fallback_for_the_funnel():
    """The canonical normaliser must not read MDR comments at all."""
    import inspect

    body = inspect.getsource(normalize_contact_funnel_row)
    assert "mql___mdr_comments" not in body


# =============================================================================
# §33 — Lifecycle events
# =============================================================================
@pytest.mark.parametrize("event,kwargs", [
    (lifecycle.EVENT_LEAD, {"lead": "2026-07-05"}),
    (lifecycle.EVENT_MQL, {"mql_": "2026-07-05"}),
    (lifecycle.EVENT_SQL, {"sql": "2026-07-05"}),
    (lifecycle.EVENT_OPPORTUNITY, {"opportunity": "2026-07-05"}),
    (lifecycle.EVENT_CUSTOMER, {"customer": "2026-07-05"}),
])
def test_event_counted_on_its_own_stage_entry_date(event, kwargs):
    populations = _pops([_row("1", **kwargs)])
    assert populations["counts"][event][funnel.SCOPE_ALL_SOURCE] == 1


def test_event_outside_window_is_not_counted():
    populations = _pops([_row("1", sql="2026-02-05")])
    assert populations["counts"][lifecycle.EVENT_SQL][funnel.SCOPE_ALL_SOURCE] == 0


def test_current_customer_retains_its_historical_sql_cohort():
    """A contact now at Customer STILL counts in the SQL cohort of the window in
    which it entered Sales Qualified Lead. Funnel counts are never made mutually
    exclusive by current stage."""
    rows = [_row("1", stage="customer", sql="2026-07-10", customer="2026-09-20")]
    populations = _pops(rows)
    assert populations["counts"][lifecycle.EVENT_SQL][funnel.SCOPE_ALL_SOURCE] == 1
    assert populations["counts"][lifecycle.EVENT_CUSTOMER][funnel.SCOPE_ALL_SOURCE] == 1


def test_current_opportunity_retains_historical_mql_and_sql():
    rows = [_row("1", stage="opportunity", mql_="2026-07-02", sql="2026-07-15",
                 opportunity="2026-08-01")]
    populations = _pops(rows)
    counts = populations["counts"]
    assert counts[lifecycle.EVENT_MQL][funnel.SCOPE_ALL_SOURCE] == 1
    assert counts[lifecycle.EVENT_SQL][funnel.SCOPE_ALL_SOURCE] == 1
    assert counts[lifecycle.EVENT_OPPORTUNITY][funnel.SCOPE_ALL_SOURCE] == 1


def test_createdate_never_substitutes_for_a_missing_sql_event_date():
    """A contact created inside the window with NO SQL entry date is not an SQL."""
    rows = [_row("1", created="2026-07-05", sql=None, stage="lead")]
    populations = _pops(rows)
    assert populations["counts"][lifecycle.EVENT_SQL][funnel.SCOPE_ALL_SOURCE] == 0


def test_contact_created_in_january_qualified_in_august_is_an_august_sql():
    """The headline behaviour change: acquisition date and qualification date are
    different events and land in different windows."""
    row = _row("1", created="2026-01-05", sql="2026-08-14")
    q3 = funnel.build_populations([row], date(2026, 7, 1), date(2026, 9, 30))
    q1 = funnel.build_populations([row], date(2026, 1, 1), date(2026, 3, 31))
    assert q3["counts"][lifecycle.EVENT_SQL][funnel.SCOPE_ALL_SOURCE] == 1
    assert q1["counts"][lifecycle.EVENT_SQL][funnel.SCOPE_ALL_SOURCE] == 0
    assert q1["counts"][lifecycle.EVENT_LEAD][funnel.SCOPE_ALL_SOURCE] == 0


def test_missing_stage_date_for_a_reached_stage_is_reported_as_coverage_gap():
    """Lifecycle says customer, but no customer entry date — a gap, never repaired."""
    rows = [_row("1", stage="customer", customer=None)]
    populations = _pops(rows)
    gaps = populations["coverage"]["stage_reached_without_entry_date"]
    assert gaps[lifecycle.EVENT_CUSTOMER] == 1
    status = funnel.reconciliation_status(populations, available=True)
    assert status["status"] == "partial"
    assert funnel.REASON_MISSING_STAGE_DATE in status["reasons"]


def test_unknown_lifecycle_stage_surfaces_in_coverage():
    rows = [_row("1", stage="brandnewstage", sql="2026-07-05")]
    populations = _pops(rows)
    assert populations["coverage"]["unknown_lifecycle_stage_contacts"] == 1


def test_every_funnel_event_names_its_hubspot_property():
    definitions = funnel.funnel_definitions()
    for event in lifecycle.FUNNEL_EVENTS:
        assert definitions[event]["event_date_property"].startswith("hs_v2_date_entered")
        assert definitions[event]["canonical_source"] == "hubspot_lifecycle"


def test_sql_definition_is_lifecycle_not_mql_status():
    definition = funnel.event_definition(lifecycle.EVENT_SQL)
    assert "salesqualifiedlead" in definition["definition"]
    assert "mql_status" not in definition["definition"]
    assert definition["event_date_property"] == "hs_v2_date_entered_salesqualifiedlead"


def test_sql_is_not_defined_by_current_stage_alone():
    """A contact currently AT salesqualifiedlead but with no entry date is not a
    windowed SQL — the event needs evidence, not a current-state guess."""
    populations = _pops([_row("1", stage="salesqualifiedlead", sql=None)])
    assert populations["counts"][lifecycle.EVENT_SQL][funnel.SCOPE_ALL_SOURCE] == 0


# =============================================================================
# §36 — All-source scope algebra
# =============================================================================
def test_organic_sql_is_included_in_all_source():
    rows = [_row("1", sql="2026-07-05", source="ORGANIC_SEARCH")]
    counts = _pops(rows)["counts"][lifecycle.EVENT_SQL]
    assert counts[funnel.SCOPE_ALL_SOURCE] == 1
    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 0


def test_paid_social_sql_is_included_in_all_source():
    rows = [_row("1", sql="2026-07-05", source="PAID_SOCIAL")]
    counts = _pops(rows)["counts"][lifecycle.EVENT_SQL]
    assert counts[funnel.SCOPE_ALL_SOURCE] == 1
    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 0


def test_google_ads_sql_is_included_in_all_source_and_google_scope():
    rows = [_row("1", sql="2026-07-05", source="PAID_SEARCH")]
    counts = _pops(rows)["counts"][lifecycle.EVENT_SQL]
    assert counts[funnel.SCOPE_ALL_SOURCE] == 1
    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 1


def test_scopes_are_strictly_nested_for_every_event():
    rows = [
        _row("1", sql="2026-07-05", source="PAID_SEARCH", campaign="Brand - US",
             keyword="tms"),
        _row("2", sql="2026-07-06", source="PAID_SEARCH", campaign="Brand - US",
             keyword=None),
        _row("3", sql="2026-07-07", source="PAID_SEARCH", campaign=None),
        _row("4", sql="2026-07-08", source="ORGANIC_SEARCH"),
        _row("5", mql_="2026-07-09", source="PAID_SEARCH", campaign="Brand - US"),
    ]
    populations = _pops(rows)
    for event in lifecycle.FUNNEL_EVENTS:
        assert funnel.scopes_are_nested(populations["events"][event]), event
    counts = populations["counts"][lifecycle.EVENT_SQL]
    assert (counts[funnel.SCOPE_KEYWORD_ATTRIBUTABLE]
            <= counts[funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE]
            <= counts[funnel.SCOPE_GOOGLE_ADS_SOURCE]
            <= counts[funnel.SCOPE_ALL_SOURCE])


def test_all_source_exceeds_google_ads_when_other_sources_qualify():
    """The defect PR-ADS-153A found: `all_source` reading a paid-search-only table
    could never exceed the Google Ads scope. Here it must."""
    rows = [
        _row("1", sql="2026-07-05", source="PAID_SEARCH"),
        _row("2", sql="2026-07-06", source="ORGANIC_SEARCH"),
        _row("3", sql="2026-07-07", source="OFFLINE"),
    ]
    counts = _pops(rows)["counts"][lifecycle.EVENT_SQL]
    assert counts[funnel.SCOPE_ALL_SOURCE] == 3
    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 1


def test_keyword_scope_requires_campaign_attribution():
    """A keyword without a resolvable campaign can never be keyword-attributable."""
    rows = [_row("1", sql="2026-07-05", campaign=None, keyword="tms")]
    counts = _pops(rows)["counts"][lifecycle.EVENT_SQL]
    assert counts[funnel.SCOPE_KEYWORD_ATTRIBUTABLE] == 0


def test_scope_is_a_subset_not_a_redefinition():
    """MVT rule 6 — attribution creates subsets; the underlying event is unchanged."""
    rows = [_row("1", sql="2026-07-05", source="ORGANIC_SEARCH")]
    populations = _pops(rows)
    all_keys = funnel.scope_keys(
        populations["events"][lifecycle.EVENT_SQL], funnel.SCOPE_ALL_SOURCE)
    google_keys = funnel.scope_keys(
        populations["events"][lifecycle.EVENT_SQL], funnel.SCOPE_GOOGLE_ADS_SOURCE)
    assert all_keys == {"1"}
    assert google_keys == set()


# =============================================================================
# Cohort-safe conversions (§26)
# =============================================================================
def test_conversion_is_cohort_based_not_period_ratio():
    """Denominator = contacts entering MQL in the window; numerator = that SAME
    cohort which later entered SQL. Unrelated period totals are never divided."""
    rows = [
        _row("1", mql_="2026-07-02", sql="2026-08-10"),
        _row("2", mql_="2026-07-03", sql=None),
        # An SQL from an older MQL cohort: it must NOT inflate this window's rate.
        _row("3", mql_="2026-02-01", sql="2026-07-20"),
    ]
    populations = _pops(rows)
    conversion = funnel.cohort_conversion(
        populations, lifecycle.EVENT_MQL, lifecycle.EVENT_SQL)
    assert conversion["cohort_size"] == 2
    assert conversion["converted"] == 1
    assert conversion["rate_pct"] == 50.0
    assert conversion["basis"] == funnel.BASIS_COHORT


def test_conversion_counts_progression_outside_the_window():
    """The cohort is fixed by the window; its later progression counts whenever
    it happened — that is what makes the rate cohort-safe."""
    rows = [_row("1", mql_="2026-09-30", sql="2026-11-15")]
    conversion = funnel.cohort_conversion(
        _pops(rows), lifecycle.EVENT_MQL, lifecycle.EVENT_SQL)
    assert conversion["cohort_size"] == 1
    assert conversion["converted"] == 1


def test_conversion_is_unavailable_for_an_empty_cohort_never_zero_percent():
    conversion = funnel.cohort_conversion(
        _pops([]), lifecycle.EVENT_MQL, lifecycle.EVENT_SQL)
    assert conversion["available"] is False
    assert conversion["rate_pct"] is None
    assert conversion["basis"] == funnel.BASIS_UNAVAILABLE


def test_backwards_dated_transition_is_not_counted_as_conversion():
    rows = [_row("1", mql_="2026-07-20", sql="2026-07-01")]
    conversion = funnel.cohort_conversion(
        _pops(rows), lifecycle.EVENT_MQL, lifecycle.EVENT_SQL)
    assert conversion["converted"] == 0


def test_all_adjacent_conversions_are_produced():
    conversions = funnel.build_conversions(_pops([_row("1", lead="2026-07-01")]))
    pairs = {(c["from_event"], c["to_event"]) for c in conversions}
    assert pairs == set(lifecycle.FUNNEL_PROGRESSION)


# =============================================================================
# §37 — Legacy vs lifecycle reconciliation
# =============================================================================
def test_lifecycle_sql_with_legacy_not_qualified_is_flagged():
    result = recon.reconcile_contacts(
        [_row("1", sql="2026-07-05")], [_legacy("1", status="in_progress")])
    assert result["counts"][recon.MISMATCH_LIFECYCLE_SQL_LEGACY_NOT_QUALIFIED] == 1


def test_legacy_qualified_never_entered_sql_is_flagged():
    result = recon.reconcile_contacts(
        [_row("1", sql=None)], [_legacy("1", status="qualified")])
    assert result["counts"][recon.MISMATCH_LEGACY_QUALIFIED_NEVER_ENTERED_SQL] == 1


def test_lifecycle_opportunity_with_legacy_in_progress_is_flagged():
    result = recon.reconcile_contacts(
        [_row("1", stage="opportunity", opportunity="2026-07-05")],
        [_legacy("1", status="in_progress", mql_status="OPEN - Meeting Booked")])
    assert result["counts"][
        recon.MISMATCH_LIFECYCLE_OPPORTUNITY_LEGACY_IN_PROGRESS] == 1


def test_lifecycle_customer_without_customer_date_is_flagged():
    result = recon.reconcile_contacts(
        [_row("1", stage="customer", customer=None)], [_legacy("1")])
    assert result["counts"][recon.MISMATCH_LIFECYCLE_CUSTOMER_NO_CUSTOMER_DATE] == 1


def test_deal_created_status_without_opportunity_lifecycle_is_flagged():
    result = recon.reconcile_contacts(
        [_row("1", stage="lead", opportunity=None)],
        [_legacy("1", status="qualified", mql_status="CLOSED - Deal Created")])
    assert result["counts"][
        recon.MISMATCH_DEAL_CREATED_STATUS_NOT_OPPORTUNITY] == 1


def test_sales_qualified_status_without_sql_lifecycle_is_flagged():
    result = recon.reconcile_contacts(
        [_row("1", stage="lead", sql=None)],
        [_legacy("1", status="qualified", mql_status="CLOSED - Sales Qualified")])
    assert result["counts"][recon.MISMATCH_SALES_QUALIFIED_STATUS_NOT_SQL] == 1


def test_unmapped_and_no_verdict_are_reported_separately():
    result = recon.reconcile_contacts(
        [_row("1"), _row("2")],
        [_legacy("1", mql_status="CLOSED - Something New"),
         _legacy("2", mql_status=None)])
    assert result["counts"][recon.MISMATCH_UNMAPPED_MQL_STATUS] == 1
    assert result["counts"][recon.MISMATCH_NO_VERDICT] == 1


def test_legacy_row_without_hubspot_identity_is_reported():
    orphan = _legacy(None, status="qualified")
    orphan["contact_key"] = "id:42"
    result = recon.reconcile_contacts([], [orphan])
    assert result["counts"][recon.MISMATCH_LEGACY_WITHOUT_HUBSPOT_IDENTITY] == 1


def test_reconciliation_never_returns_an_email():
    result = recon.reconcile_contacts(
        [_row("1", sql="2026-07-05")], [_legacy("1", status="junk")])
    for contact in result["contacts"]:
        assert "email" not in contact
        assert not any("@" in str(v) for v in contact.values() if isinstance(v, str))


def test_excluded_contacts_are_skipped_in_reconciliation():
    result = recon.reconcile_contacts(
        [], [_legacy("1", mql_status=None)], exclusions={"1"})
    assert result["counts"][recon.MISMATCH_NO_VERDICT] == 0


# ── §23 before/after SQL comparison ──────────────────────────────────────────
def test_before_after_splits_date_shift_from_population_change():
    funnel_rows = [
        # Same contact, SQL under both doctrines — but qualification happened in
        # Q3 while acquisition was Q1: a pure DATE SHIFT.
        _row("1", created="2026-01-05", sql="2026-07-10"),
        # Lifecycle-only: HubSpot marked it SQL, legacy never did.
        _row("2", created="2026-07-02", sql="2026-07-20"),
        # Legacy-qualified but HubSpot has no SQL entry: POPULATION difference.
        _row("3", created="2026-07-03", sql=None),
    ]
    legacy_rows = [
        _legacy("1", status="qualified", created="2026-01-05"),
        _legacy("2", status="in_progress", created="2026-07-02"),
        _legacy("3", status="qualified", created="2026-07-03"),
    ]
    comparison = recon.compare_sql_counts(
        funnel_rows, legacy_rows, set(), _Q3_START, _Q3_END)

    assert comparison["legacy_sql_count"] == 1      # contact 3 only
    assert comparison["lifecycle_sql_count"] == 2   # contacts 1 and 2
    assert comparison["date_shifted_contacts"] == 1          # contact 1
    assert comparison["missing_sql_event_date_contacts"] == 1  # contact 3
    assert comparison["sets"]["date_shifted"] == ["1"]
    assert comparison["sets"]["missing_sql_event_date"] == ["3"]


def test_before_after_reports_both_definitions_explicitly():
    comparison = recon.compare_sql_counts([], [], set(), _Q3_START, _Q3_END)
    assert "status_category" in comparison["legacy_definition"]
    assert "salesqualifiedlead" in comparison["canonical_definition"]


def test_before_after_exposes_attribution_coverage_by_scope():
    funnel_rows = [
        _row("1", sql="2026-07-10", source="PAID_SEARCH", campaign="Brand - US"),
        _row("2", sql="2026-07-11", source="ORGANIC_SEARCH"),
    ]
    comparison = recon.compare_sql_counts(
        funnel_rows, [], set(), _Q3_START, _Q3_END)
    coverage = comparison["attribution_coverage"]
    assert coverage[funnel.SCOPE_ALL_SOURCE] == 2
    assert coverage[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 1


def test_lifecycle_customer_and_revenue_customer_stay_separate():
    """§13/§40 — this PR defines the LIFECYCLE customer only. The revenue customer
    (closed-won deal truth) is explicitly deferred to PR-ADS-153E."""
    definition = funnel.event_definition(lifecycle.EVENT_CUSTOMER)
    assert definition["definition"].endswith("'customer'")
    assert "deal" not in definition["definition"].lower()

    # The service must state the distinction, and must not define revenue here.
    module_doc = funnel.__doc__.lower()
    assert "lifecycle customer" in module_doc
    assert "pr-ads-153e" in module_doc


# =============================================================================
# Reconciliation status / fail-closed behaviour
# =============================================================================
def test_unavailable_source_is_never_reported_as_zero():
    status = funnel.reconciliation_status(_pops([]), available=False)
    assert status["status"] == "unavailable"


def test_broken_scope_nesting_is_a_mismatch():
    populations = _pops([_row("1", sql="2026-07-05")])
    # Force an impossible membership: narrower scope true, broader false.
    contact = populations["events"][lifecycle.EVENT_SQL][0]
    contact["scopes"][funnel.SCOPE_GOOGLE_ADS_SOURCE] = False
    contact["scopes"][funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE] = True
    status = funnel.reconciliation_status(populations, available=True)
    assert status["status"] == "mismatch"


def test_clean_population_reconciles():
    populations = _pops([_row("1", stage="salesqualifiedlead", lead="2026-07-01",
                              mql_="2026-07-03", sql="2026-07-05")])
    assert funnel.reconciliation_status(
        populations, available=True)["status"] == "reconciled"


# =============================================================================
# Sync orchestration (§7) — no HubSpot, no database
# =============================================================================
class _FakeWriters:
    """Records every write so orchestration can be asserted without a database."""

    def __init__(self, state=None):
        self.state = dict(state or {})
        self.upserts: list[list[dict]] = []
        self.state_updates: list[dict] = []
        self.batches: list[dict] = []
        self.finished: list[dict] = []
        # Failure injection for the fail-closed contract.
        self.upsert_ok = True
        self.batch_ok = True
        self.checkpoint_ok = True
        self.fail_checkpoint_after = None  # fail the Nth+ state write

    def get_contact_funnel_sync_state(self, scope="contacts"):
        return dict(self.state) if self.state else None

    def update_contact_funnel_sync_state(self, scope="contacts", **fields):
        self.state_updates.append(dict(fields))
        if self.checkpoint_ok is False:
            return False
        if (self.fail_checkpoint_after is not None
                and len(self.state_updates) > self.fail_checkpoint_after):
            return False
        self.state.update(fields)
        return True

    def upsert_hubspot_contact_funnel(self, rows, *, sync_batch_id=None):
        self.upserts.append(list(rows))
        if self.upsert_ok is False:
            return {"ok": False, "attempted": len(rows), "persisted": 0,
                    "error": "database_unavailable"}
        return {"ok": True, "attempted": len(rows),
                "persisted": len(rows), "error": None}

    def start_sync_batch(self, **kwargs):
        self.batches.append(kwargs)
        if self.batch_ok is False:
            return 0
        return len(self.batches)

    def finish_sync_batch(self, **kwargs):
        self.finished.append(kwargs)


@pytest.fixture()
def fake_writers(monkeypatch):
    writers = _FakeWriters()
    monkeypatch.setattr(sync, "db_writers", writers)
    return writers


def _page(*contacts, complete=True):
    """Fake iterator. Emits the explicit end-of-result-set sentinel by default,
    mirroring the real connector; pass ``complete=False`` for a scan that stops
    without proving it finished."""
    def iterator(since, max_pages=None):
        for index, page in enumerate(contacts):
            yield page, {"watermark_ms": 0, "page_index": index, "complete": False}
        if complete:
            yield [], {"watermark_ms": 0, "complete": True}
    return iterator


def test_sync_checkpoints_the_watermark_after_every_page(fake_writers):
    pages = _page(
        [_hs_contact("1", modified="2026-07-01T00:00:00Z")],
        [_hs_contact("2", modified="2026-07-02T00:00:00Z")],
    )
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=pages,
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert result["status"] == "success"
    assert result["pages"] == 2
    watermarks = [u["last_modified_watermark"] for u in fake_writers.state_updates
                  if "last_modified_watermark" in u]
    # One checkpoint per page (plus the final state write) — never only at the end.
    assert len(watermarks) >= 2
    assert watermarks[0] == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_sync_failure_keeps_the_last_proven_watermark(fake_writers):
    def exploding(since, max_pages=None):
        yield [_hs_contact("1", modified="2026-07-01T00:00:00Z")], {"watermark_ms": 0}
        raise RuntimeError("HubSpot 500")

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=exploding,
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert result["watermark"] == "2026-07-01T00:00:00+00:00"
    assert any(f["status"] == "failed" for f in fake_writers.finished)


def test_partial_sync_is_never_reported_as_a_complete_bootstrap(fake_writers):
    pages = _page([_hs_contact("1")])
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=pages, max_pages=1,
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))
    assert result["truncated"] is True
    assert result["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL


def test_completed_bootstrap_is_marked_complete(fake_writers):
    pages = _page([_hs_contact("1")])
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=pages,
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))
    assert result["bootstrap_status"] == sync.BOOTSTRAP_COMPLETE


def test_incremental_without_a_watermark_degrades_to_full_scan():
    """Never silently sync only recent contacts when the watermark is missing."""
    since = sync.resolve_since({}, sync.MODE_INCREMENTAL, overlap_minutes=15)
    assert since.year == 1970


def test_incremental_resumes_from_the_watermark_with_overlap():
    state = {"last_modified_watermark": datetime(2026, 7, 1, 12, 0,
                                                 tzinfo=timezone.utc)}
    since = sync.resolve_since(state, sync.MODE_INCREMENTAL, overlap_minutes=15)
    assert since == datetime(2026, 7, 1, 11, 45, tzinfo=timezone.utc)


def test_first_ever_bootstrap_starts_at_the_epoch():
    since = sync.resolve_since({}, sync.MODE_BOOTSTRAP, overlap_minutes=15)
    assert since.year == 1970


def test_interrupted_bootstrap_resumes_from_its_durable_watermark():
    """A bootstrap must NOT rescan the portal from the epoch on every retry —
    that would make a large backlog impossible to finish."""
    state = {"last_modified_watermark": datetime(2026, 7, 1, 12, 0,
                                                 tzinfo=timezone.utc)}
    since = sync.resolve_since(state, sync.MODE_BOOTSTRAP, overlap_minutes=15)
    assert since == datetime(2026, 7, 1, 11, 45, tzinfo=timezone.utc)


def test_bootstrap_and_incremental_resume_from_the_same_point():
    """Both modes share one resume rule; only the no-watermark case differs."""
    state = {"last_modified_watermark": datetime(2026, 7, 1, 12, 0,
                                                 tzinfo=timezone.utc)}
    assert (sync.resolve_since(state, sync.MODE_BOOTSTRAP, overlap_minutes=15)
            == sync.resolve_since(state, sync.MODE_INCREMENTAL, overlap_minutes=15))


def test_restart_from_epoch_forces_a_full_rebuild():
    """Operator-only escape hatch — never normal bootstrap behaviour."""
    state = {"last_modified_watermark": datetime(2026, 7, 1, tzinfo=timezone.utc)}
    since = sync.resolve_since(state, sync.MODE_BOOTSTRAP, overlap_minutes=15,
                               restart_from_epoch=True)
    assert since.year == 1970


def test_contacts_without_identity_are_rejected_not_written(fake_writers):
    pages = _page([_hs_contact("1"), {"id": "", "properties": {}}])
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=pages,
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))
    assert result["rejected_no_identity"] == 1
    assert result["contacts_written"] == 1


def test_sync_records_both_freshness_datasets(fake_writers):
    pages = _page([_hs_contact("1", entered_sql="2026-07-01T00:00:00Z")])
    sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=pages,
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))
    datasets = {b["dataset"] for b in fake_writers.batches}
    assert datasets == {sync.DATASET_CONTACT_FUNNEL, sync.DATASET_LIFECYCLE_EVENTS}


# =============================================================================
# Truth-safety §2 — fail closed on persistence / checkpoint failure
# =============================================================================
def test_batch_creation_failure_means_hubspot_is_never_called(fake_writers):
    """Without durable batch state a failure would be invisible, so the run must
    refuse to read HubSpot at all."""
    fake_writers.batch_ok = False
    called = {"value": False}

    def iterator(since, max_pages=None):
        called["value"] = True
        yield [_hs_contact("1")], {"watermark_ms": 0, "complete": False}

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=iterator,
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert called["value"] is False
    assert fake_writers.upserts == []


def test_contact_persistence_failure_does_not_advance_the_watermark(fake_writers):
    fake_writers.upsert_ok = False
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL,
        page_iterator=_page([_hs_contact("1", modified="2026-07-01T00:00:00Z")]),
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    advanced = [u for u in fake_writers.state_updates
                if u.get("last_modified_watermark")]
    assert advanced == []
    assert fake_writers.state.get("last_modified_watermark") is None


def test_page_checkpoint_failure_is_failed_not_success(fake_writers):
    fake_writers.checkpoint_ok = False
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=_page([_hs_contact("1")]),
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert any(f["status"] == "failed" for f in fake_writers.finished)


def test_final_completion_checkpoint_failure_is_failed_not_complete(fake_writers):
    """The last durable write must be proven before a bootstrap may claim it
    finished — otherwise the next run would rescan from scratch."""
    # Allow the running-state write and the per-page checkpoint; fail the final one.
    fake_writers.fail_checkpoint_after = 2
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=_page([_hs_contact("1")]),
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert result.get("bootstrap_status") != sync.BOOTSTRAP_COMPLETE
    assert fake_writers.state.get("bootstrap_status") != sync.BOOTSTRAP_COMPLETE


def test_successful_durable_path_still_reports_success(fake_writers):
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=_page([_hs_contact("1")]),
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))
    assert result["status"] == "success"
    assert result["bootstrap_status"] == sync.BOOTSTRAP_COMPLETE


def test_idempotent_stale_row_no_op_is_not_treated_as_failure(fake_writers, monkeypatch):
    """`persisted < attempted` is the latest-state guard working, NOT a failure."""
    def _stale_noop(rows, *, sync_batch_id=None):
        return {"ok": True, "attempted": len(rows), "persisted": 0, "error": None}
    monkeypatch.setattr(fake_writers, "upsert_hubspot_contact_funnel", _stale_noop)

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=_page([_hs_contact("1")]),
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))
    assert result["status"] == "success"
    assert result["contacts_written"] == 0
    assert result["contacts_attempted"] == 1


# =============================================================================
# Writer contracts — legacy writers keep their integer contract; the new
# canonical writer always returns the structured result
# =============================================================================
class _NullConnCM:
    """A `get_conn()` stand-in for an unavailable database."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def db_unavailable(monkeypatch):
    from db import writers as db_writers

    monkeypatch.setattr(db_writers, "get_conn", lambda: _NullConnCM())
    return db_writers


def test_legacy_write_campaigns_keeps_its_integer_contract(db_unavailable):
    """`write_campaigns` is annotated `-> int` and its callers expect an int.
    The structured result introduced for the canonical funnel writer must NOT
    leak into legacy writers."""
    result = db_unavailable.write_campaigns(1, [{
        "campaign_name": "Brand - US", "spend_usd": 10.0, "clicks": 5,
        "impressions": 100, "conversions": 1, "total_leads": 2,
        "confirmed_sqls": 1, "junk_count": 0, "junk_rate_pct": 0.0,
        "cpql_usd": 10.0, "verdict": "HOLD", "verdict_reason": "test",
    }])
    assert isinstance(result, int)
    assert not isinstance(result, bool)
    assert result == 0


def test_no_legacy_writer_returns_the_structured_result(db_unavailable):
    """The sibling legacy writers must be untouched by the new contract.

    Their exact pre-PR-ADS-153B return values are preserved as-is — including
    quirks such as ``write_waste_terms`` returning None for empty input, which
    is out of scope for this PR. The invariant asserted here is only that none
    of them leaked the structured funnel result.
    """
    rows = [{
        "campaign_name": "Brand - US", "spend_usd": 1.0,
        "search_term": "tms", "country": "AE", "contact_id": "1",
    }]
    for name in ("write_campaigns", "write_leads", "write_waste_terms",
                 "write_geo", "write_deals"):
        for payload in ([], rows):
            result = getattr(db_unavailable, name)(1, payload)
            assert not isinstance(result, dict), (
                f"{name} leaked the structured result: {result!r}")
            assert result is None or isinstance(result, int), (
                f"{name} returned an unexpected type: {result!r}")


def test_legacy_writers_never_return_the_structured_result():
    """Static guard: only the canonical funnel writer carries the new contract."""
    import ast

    source = (_ROOT / "db" / "writers.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        annotation = ast.unparse(node.returns) if node.returns else None
        returns_dict = any(
            isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)
            for n in ast.walk(node))
        if returns_dict:
            assert annotation not in ("int", "bool"), (
                f"{node.name} is annotated -> {annotation} but returns a dict")


def test_funnel_writer_always_returns_the_structured_result(db_unavailable):
    """Every exit path — including the empty-input short circuit — is structured."""
    for rows in ([], [None], [{"contact_id": "1",
                               "last_modified_at": "2026-07-01T00:00:00+00:00"}]):
        result = db_unavailable.upsert_hubspot_contact_funnel(rows)
        assert isinstance(result, dict), rows
        assert set(result) == {"ok", "attempted", "persisted", "error"}, rows
        assert isinstance(result["ok"], bool)
        assert isinstance(result["attempted"], int)
        assert isinstance(result["persisted"], int)


def test_funnel_writer_reports_unavailable_database_as_not_ok(db_unavailable):
    """An unavailable DB is ok=False — never a bare 0, and never a silent
    "wrote nothing" that the caller would treat as success."""
    result = db_unavailable.upsert_hubspot_contact_funnel([{
        "contact_id": "1", "last_modified_at": "2026-07-01T00:00:00+00:00"}])
    assert result == {"ok": False, "attempted": 1, "persisted": 0,
                      "error": "database_unavailable"}


def test_funnel_writer_empty_input_is_ok_not_a_failure(db_unavailable):
    """Nothing to write is success with zero attempted — distinct from a failure."""
    result = db_unavailable.upsert_hubspot_contact_funnel([])
    assert result["ok"] is True
    assert result["attempted"] == 0
    assert result["error"] is None


def test_sync_fails_closed_cleanly_when_the_database_is_unavailable(monkeypatch):
    """The real writer's unavailable path, inside the real sync orchestration.

    With a bare-`0` return this raised AttributeError ("int has no attribute
    'get'"). It must instead fail closed: status=failed, watermark untouched,
    bootstrap never marked complete.
    """
    from db import writers as real_writers

    monkeypatch.setattr(real_writers, "get_conn", lambda: _NullConnCM())

    class _Writers(_FakeWriters):
        """Durable batch/state calls succeed so the run reaches the REAL upsert."""

        def upsert_hubspot_contact_funnel(self, rows, *, sync_batch_id=None):
            return real_writers.upsert_hubspot_contact_funnel(
                rows, sync_batch_id=sync_batch_id)

    writers = _Writers()
    monkeypatch.setattr(sync, "db_writers", writers)

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP,
        page_iterator=_page([_hs_contact("1", modified="2026-07-01T00:00:00Z")]),
        now=datetime(2026, 7, 5, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert "database_unavailable" in (result["error"] or "")
    assert result["scan_complete"] is False
    assert writers.state.get("bootstrap_status") == sync.BOOTSTRAP_PARTIAL
    assert writers.state.get("last_modified_watermark") is None


# =============================================================================
# Truth-safety §4 — completion is proven, never assumed
# =============================================================================
def test_scan_without_a_completion_sentinel_cannot_mark_bootstrap_complete(fake_writers):
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP,
        page_iterator=_page([_hs_contact("1")], complete=False),
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))
    assert result["scan_complete"] is False
    assert result["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL


def test_stalled_scan_at_the_10k_boundary_cannot_mark_complete(fake_writers):
    """Synthetic 10,000-result boundary: every contact shares one modification
    timestamp, so the real iterator raises rather than returning normally."""
    from connectors.hubspot_pull import HubSpotSearchStalledError

    def stalled(since, max_pages=None):
        yield ([_hs_contact(str(i), modified="2026-07-01T00:00:00Z")
                for i in range(100)], {"watermark_ms": 0, "complete": False})
        raise HubSpotSearchStalledError("stalled at the 10,000-result boundary")

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=stalled,
        now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert result["scan_complete"] is False
    assert fake_writers.state.get("bootstrap_status") == sync.BOOTSTRAP_PARTIAL


def test_real_iterator_raises_rather_than_returning_on_a_stalled_watermark():
    """Prove the connector itself refuses to end normally when >1 full page
    shares a boundary timestamp — the condition that would otherwise let a
    bootstrap be marked complete on an incomplete scan."""
    from connectors.hubspot_pull import (
        HUBSPOT_SEARCH_RESULT_CAP, HubSpotSearchStalledError,
        iter_contacts_modified_since,
    )

    boundary_ms = "1751328000000"
    page_size = 100
    pages_to_cap = HUBSPOT_SEARCH_RESULT_CAP // page_size

    class _Obj:
        def __init__(self, payload):
            self._payload = payload

        def to_dict(self):
            return self._payload

    class _Resp:
        def __init__(self, results, after):
            self.results = results
            self.paging = type("P", (), {"next": type("N", (), {"after": after})()})()

    class _FakeSearch:
        """10,000 contacts that all share one lastmodifieddate.

        Deterministic on the paging cursor, exactly like the real API: re-issuing
        the query returns the SAME contacts, so re-anchoring the watermark cannot
        make progress and the scan is provably incomplete.
        """

        def do_search(self, public_object_search_request=None):
            after = (public_object_search_request or {}).get("after")
            offset = int(after) if after else 0
            results = [_Obj({"id": f"c{offset + i}", "properties": {
                "lastmodifieddate": boundary_ms}}) for i in range(page_size)]
            return _Resp(results, after=str(offset + page_size))

    class _FakeClient:
        def __init__(self):
            self.crm = type("C", (), {})()
            self.crm.contacts = type("K", (), {})()
            self.crm.contacts.search_api = _FakeSearch()

    iterator = iter_contacts_modified_since(0, client=_FakeClient())
    saw_completion = False
    with pytest.raises(HubSpotSearchStalledError):
        for _page_rows, meta in iterator:
            if meta.get("complete"):
                saw_completion = True
            if meta.get("page_index", 0) > pages_to_cap + 5:
                break  # safety net; the raise should arrive first
    assert saw_completion is False


def test_real_iterator_emits_a_completion_sentinel_at_the_true_end():
    from connectors.hubspot_pull import iter_contacts_modified_since

    class _Obj:
        def __init__(self, payload):
            self._payload = payload

        def to_dict(self):
            return self._payload

    class _Resp:
        def __init__(self, results):
            self.results = results
            self.paging = None

    class _FakeSearch:
        def do_search(self, public_object_search_request=None):
            return _Resp([_Obj({"id": "1", "properties": {
                "lastmodifieddate": "1751328000000"}})])

    class _FakeClient:
        def __init__(self):
            self.crm = type("C", (), {})()
            self.crm.contacts = type("K", (), {})()
            self.crm.contacts.search_api = _FakeSearch()

    metas = [meta for _rows, meta
             in iter_contacts_modified_since(0, client=_FakeClient())]
    assert metas[-1]["complete"] is True
    assert all(m["complete"] is False for m in metas[:-1])


# =============================================================================
# Truth-safety §3 — campaign-identity availability is propagated, never zeroed
# =============================================================================
def test_identity_unavailable_withholds_narrow_scopes_but_keeps_broad_ones():
    rows = [_row(str(i), sql="2026-07-05", source="PAID_SEARCH",
                 campaign="Brand - US", keyword="tms") for i in range(1, 6)]
    populations = funnel.build_populations(
        rows, _Q3_START, _Q3_END, identity_available=False)
    counts = populations["counts"][lifecycle.EVENT_SQL]

    assert counts[funnel.SCOPE_ALL_SOURCE] == 5
    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 5
    assert counts[funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE] is None
    assert counts[funnel.SCOPE_KEYWORD_ATTRIBUTABLE] is None

    status = funnel.reconciliation_status(populations, available=True)
    assert status["status"] != "reconciled"
    assert funnel.REASON_CAMPAIGN_IDENTITY_UNAVAILABLE in status["reasons"]


def test_identity_unavailable_is_never_rendered_as_false_membership():
    rows = [_row("1", sql="2026-07-05")]
    populations = funnel.build_populations(
        rows, _Q3_START, _Q3_END, identity_available=False)
    scopes = populations["contacts"][0]["scopes"]
    assert scopes[funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE] is None
    assert scopes[funnel.SCOPE_KEYWORD_ATTRIBUTABLE] is None


def test_unavailable_scope_keys_are_none_not_an_empty_set():
    rows = [_row("1", sql="2026-07-05")]
    populations = funnel.build_populations(
        rows, _Q3_START, _Q3_END, identity_available=False)
    population = populations["events"][lifecycle.EVENT_SQL]
    assert funnel.scope_keys(population, funnel.SCOPE_ALL_SOURCE) == {"1"}
    assert funnel.scope_keys(population, funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE) is None


def test_unavailable_scopes_do_not_break_the_nesting_invariant():
    """An unknown subset cannot violate nesting — it must not become a mismatch."""
    rows = [_row("1", sql="2026-07-05"), _row("2", sql="2026-07-06")]
    populations = funnel.build_populations(
        rows, _Q3_START, _Q3_END, identity_available=False)
    assert funnel.scopes_are_nested(
        populations["events"][lifecycle.EVENT_SQL]) is True
    assert funnel.reconciliation_status(
        populations, available=True)["status"] == "partial"


def test_consulted_identity_with_zero_mapped_campaigns_may_report_zero():
    """A SUCCESSFULLY consulted contract that maps nothing is a real, provable
    zero — that must still be allowed."""
    def _maps_nothing(_campaign_name):
        return False, "unresolved_campaign_mapping"

    rows = [_row("1", sql="2026-07-05", source="PAID_SEARCH")]
    populations = funnel.build_populations(
        rows, _Q3_START, _Q3_END, campaign_resolver=_maps_nothing,
        identity_available=True)
    counts = populations["counts"][lifecycle.EVENT_SQL]

    assert counts[funnel.SCOPE_GOOGLE_ADS_SOURCE] == 1
    assert counts[funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE] == 0
    assert counts[funnel.SCOPE_KEYWORD_ATTRIBUTABLE] == 0


def test_reconciliation_scope_coverage_withholds_unavailable_scopes():
    funnel_rows = [_row("1", sql="2026-07-10", source="PAID_SEARCH")]
    comparison = recon.compare_sql_counts(
        funnel_rows, [], set(), _Q3_START, _Q3_END, identity_available=False)
    coverage = comparison["attribution_coverage"]
    assert coverage[funnel.SCOPE_ALL_SOURCE] == 1
    assert coverage[funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE] is None


def test_build_campaign_resolver_returns_explicit_availability():
    resolver, available = funnel._build_campaign_resolver(_Q3_START, _Q3_END)
    assert callable(resolver)
    assert isinstance(available, bool)


# =============================================================================
# Freshness registry parity (§29) — the google_ads vs google_ads_api defect class
# =============================================================================
def test_writer_keys_match_the_freshness_registry():
    """The keys the ingestion service stamps on sync batches MUST equal the keys
    DATASET_FRESHNESS_CONFIG expects, or the dataset has no freshness signal."""
    from services.freshness_service import DATASET_FRESHNESS_CONFIG

    for dataset in (sync.DATASET_CONTACT_FUNNEL, sync.DATASET_LIFECYCLE_EVENTS):
        config = DATASET_FRESHNESS_CONFIG[dataset]
        assert config["source"] == sync.SYNC_SOURCE
        assert config["dataset"] == dataset


def test_new_datasets_are_accepted_by_the_sync_batch_validator():
    from db.writers import VALID_SYNC_DATASETS, VALID_SYNC_SOURCES

    assert sync.SYNC_SOURCE in VALID_SYNC_SOURCES
    assert sync.DATASET_CONTACT_FUNNEL in VALID_SYNC_DATASETS
    assert sync.DATASET_LIFECYCLE_EVENTS in VALID_SYNC_DATASETS


def test_freshness_config_columns_exist_in_the_schema():
    """A freshness entry pointing at a nonexistent column silently reports
    UNKNOWN forever — three such entries already exist in the codebase."""
    from db.schema import _DDL
    from services.freshness_service import DATASET_FRESHNESS_CONFIG

    for dataset in (sync.DATASET_CONTACT_FUNNEL, sync.DATASET_LIFECYCLE_EVENTS):
        config = DATASET_FRESHNESS_CONFIG[dataset]
        assert config["table"] == "hubspot_contact_funnel"
        assert config["date_column"] in _DDL


def test_new_datasets_are_listed_as_known_for_placeholder_freshness():
    source = (_ROOT / "api" / "server.py").read_text()
    assert '("hubspot", "contact_funnel")' in source
    assert '("hubspot", "lifecycle_events")' in source


# =============================================================================
# Governance
# =============================================================================
def test_no_hubspot_write_verbs_in_the_new_modules():
    """Phase 1 stays read-only externally."""
    for path in (
        _ROOT / "services" / "canonical_crm_funnel_service.py",
        _ROOT / "services" / "hubspot_contact_funnel_sync_service.py",
        _ROOT / "services" / "crm_funnel_reconciliation_service.py",
        _ROOT / "db" / "crm_funnel_repository.py",
    ):
        source = path.read_text().lower()
        for forbidden in ("basic_api.update", "basic_api.create", "basic_api.archive",
                          "contacts.batch_api.update", "deals.basic_api.update"):
            assert forbidden not in source, f"{path.name} contains {forbidden}"


def test_repository_never_selects_an_email_column():
    from db.crm_funnel_repository import _FUNNEL_COLUMNS

    assert not any("email" in column.lower() for column in _FUNNEL_COLUMNS)
    source = (_ROOT / "db" / "crm_funnel_repository.py").read_text().lower()
    # The word may appear in a doc line promising no emails; a SELECT of one may not.
    assert "select email" not in source
    assert ", email" not in source


def test_canonical_store_has_no_email_column():
    from db.schema import _DDL

    table = _DDL.split("CREATE TABLE IF NOT EXISTS hubspot_contact_funnel")[1]
    table = table.split(");")[0]
    assert "email" not in table.lower()


def test_no_deletion_of_historical_evidence_in_new_modules():
    for path in (
        _ROOT / "services" / "hubspot_contact_funnel_sync_service.py",
        _ROOT / "services" / "crm_funnel_reconciliation_service.py",
        _ROOT / "db" / "crm_funnel_repository.py",
    ):
        source = path.read_text().upper()
        assert "DELETE FROM" not in source
        assert "DROP TABLE" not in source
        assert "TRUNCATE TABLE" not in source


def test_legacy_leads_pipeline_is_untouched():
    """§38 — existing pages must keep working until PR-ADS-153C migrates them."""
    from db.writers import _map_status_category

    assert _map_status_category("CLOSED - Sales Qualified") == "qualified"
    assert _map_status_category("OPEN - Meeting Booked") == "in_progress"


def test_legacy_api_and_pages_are_not_removed_in_this_pr():
    source = (_ROOT / "api" / "server.py").read_text()
    assert '@app.get("/api/leads")' in source
    app_js = (_ROOT / "static" / "app.js").read_text()
    assert '"leads"' in app_js
    assert '"opportunities"' in app_js


def test_window_vocabularies_are_reused_not_reinvented():
    """§25 — no third rolling implementation of an existing visible label."""
    source = (_ROOT / "services" / "canonical_crm_funnel_service.py").read_text()
    assert "resolve_window_contract" in source
    assert "INTERVAL" not in source.upper()
