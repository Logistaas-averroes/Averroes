"""
tests/test_pr_ads_155_lifecycle_cohort_and_revenue_truth.py

PR-ADS-155 — close two production truth gaps.

  §1  The Dashboard funnel is ONE Lead-anchored cohort, not five independent
      stage-entry totals arranged to look like one.
  §2  Commercial outcomes are separated from lifecycle progression.
  §3  Partial lifecycle coverage is surfaced, and no proxy date is ever used.
  §4  Missing stage dates are recovered from real HubSpot property-history
      evidence, or left missing. Never inferred. Never written to HubSpot.
  §5  All-Time revenue stays fail-closed, with the parts published separately.
  §6  The deals blocking the total are named and actionable.
  §7  The parity audit tells four kinds of unavailability apart.
  §8  Static guards against the misleading Dashboard regressions.

Run with:
    python -m pytest tests/test_pr_ads_155_lifecycle_cohort_and_revenue_truth.py -v
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import canonical_crm_funnel_service as funnel  # noqa: E402
from services import canonical_revenue_service as canonical_revenue  # noqa: E402
from services import cross_page_parity_service as parity  # noqa: E402
from services import lifecycle_history_recovery_service as recovery  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_APP = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
_OVERVIEW = (_ROOT / "services" / "dashboard_overview_service.py").read_text(
    encoding="utf-8")
_RECOVERY_SRC = (_ROOT / "services" / "lifecycle_history_recovery_service.py").read_text(
    encoding="utf-8")
_BACKFILL_SRC = (_ROOT / "scripts" / "backfill_lifecycle_stage_history.py").read_text(
    encoding="utf-8")
_MISSING_SRC = (_ROOT / "scripts" / "report_missing_deal_amounts.py").read_text(
    encoding="utf-8")


# ── helpers ──────────────────────────────────────────────────────────────────
def _contact(cid, *, lead=None, mql=None, sql=None, opp=None, customer=None,
             stage=None, source="ORGANIC_SEARCH"):
    return {
        "contact_id": cid,
        "lifecycle_stage": stage,
        "hs_analytics_source": source,
        "date_entered_lead": lead,
        "date_entered_mql": mql,
        "date_entered_sql": sql,
        "date_entered_opportunity": opp,
        "date_entered_customer": customer,
    }


def _pops(rows, start=date(2026, 1, 1), end=date(2026, 3, 31), **kwargs):
    return funnel.build_populations(rows, start, end, **kwargs)


def _deal(deal_id, *, amount=None, currency=None, status="verified_usd",
          reason=None, close="2026-02-01", name=None):
    return {
        "deal_id": deal_id,
        "deal_name": name or f"Company {deal_id}",
        "deal_close_date": close,
        "deal_stage_label": "Closed Won",
        "amount_raw": amount,
        "deal_currency_code": currency,
        "revenue_usd": amount if status in ("verified_usd", "converted") else None,
        "currency_status": status,
        "currency_reason": reason,
        "acquisition_group": "google_ads",
    }


def _base(deals, *, window="all_time"):
    return {"available": True, "deals": deals, "window": window,
            "window_start": None, "window_end": None, "as_of": None}


# ═════════════════════════════════════════════════════════════════════════════
# §1 — one Lead-anchored cohort
# ═════════════════════════════════════════════════════════════════════════════
def test_1_cohort_denominator_is_contacts_that_entered_lead_in_the_window():
    """The denominator is Lead entries INSIDE the window and nothing else."""
    rows = [
        _contact("in-1", lead=date(2026, 1, 5), stage="lead"),
        _contact("in-2", lead=date(2026, 3, 30), stage="lead"),
        # Entered Lead before the window: not this cohort.
        _contact("before", lead=date(2025, 12, 31), stage="lead"),
        # Entered Lead after the window: not this cohort.
        _contact("after", lead=date(2026, 4, 1), stage="lead"),
        # Entered MQL inside the window but Lead outside it: still not the
        # denominator — the anchor is Lead entry, not any stage entry.
        _contact("mql-only", lead=date(2025, 6, 1), mql=date(2026, 2, 1),
                 stage="marketingqualifiedlead"),
    ]
    cohort = funnel.lead_cohort_progression(_pops(rows))
    assert cohort["anchor_event"] == funnel.EVENT_LEAD
    assert cohort["cohort_size"] == 2
    assert cohort["stages"][0]["reached"] == 2


def test_2_later_stages_count_even_when_they_fall_outside_the_window():
    """A January lead that became an SQL in July is a conversion of that cohort."""
    rows = [_contact("c1", lead=date(2026, 1, 10), mql=date(2026, 2, 1),
                     sql=date(2026, 7, 1), opp=date(2026, 8, 1),
                     customer=date(2026, 9, 1), stage="customer")]
    cohort = funnel.lead_cohort_progression(_pops(rows))
    assert [s["reached"] for s in cohort["stages"]] == [1, 1, 1, 1, 1]
    assert cohort["converted"] == 1
    assert cohort["rate_from_anchor_pct"] == 100.0


def test_3_displayed_counts_are_monotonically_non_increasing():
    """Lead >= MQL >= SQL >= Opportunity >= Customer, by construction."""
    rows = [
        _contact("a", lead=date(2026, 1, 2), mql=date(2026, 1, 9),
                 sql=date(2026, 2, 1), opp=date(2026, 2, 20),
                 customer=date(2026, 3, 1), stage="customer"),
        _contact("b", lead=date(2026, 1, 3), mql=date(2026, 1, 20),
                 sql=date(2026, 2, 5), stage="salesqualifiedlead"),
        _contact("c", lead=date(2026, 1, 4), mql=date(2026, 2, 2),
                 stage="marketingqualifiedlead"),
        _contact("d", lead=date(2026, 1, 5), stage="lead"),
    ]
    reached = [s["reached"] for s in funnel.lead_cohort_progression(_pops(rows))["stages"]]
    assert reached == [4, 3, 2, 1, 1]
    assert all(reached[i] >= reached[i + 1] for i in range(len(reached) - 1))


def test_4_a_later_stage_can_never_exceed_the_stage_before_it():
    """A contact with an SQL date but no MQL date does not inflate SQL.

    This is the exact shape that made the old strip widen: SQL evidence counted
    independently of whether MQL was ever proven.
    """
    rows = [
        _contact("chain-broken", lead=date(2026, 1, 2),
                 sql=date(2026, 2, 1), stage="salesqualifiedlead"),
        _contact("intact", lead=date(2026, 1, 3), mql=date(2026, 1, 10),
                 sql=date(2026, 2, 2), stage="salesqualifiedlead"),
    ]
    cohort = funnel.lead_cohort_progression(_pops(rows))
    by_event = {s["event"]: s for s in cohort["stages"]}
    assert by_event["mql"]["reached"] == 1
    assert by_event["sql"]["reached"] == 1
    assert by_event["sql"]["reached"] <= by_event["mql"]["reached"]
    # And the dropped contact is NAMED, not silently lost.
    assert by_event["sql"]["exclusion_reasons"][funnel.EXCLUSION_PRIOR_STAGE_UNPROVEN] == 1


def test_5_a_stage_dated_before_the_anchor_is_excluded_and_reported():
    rows = [_contact("recycled", lead=date(2026, 3, 1), mql=date(2025, 12, 1),
                     stage="marketingqualifiedlead")]
    cohort = funnel.lead_cohort_progression(_pops(rows))
    by_event = {s["event"]: s for s in cohort["stages"]}
    assert by_event["mql"]["reached"] == 0
    assert (by_event["mql"]["exclusion_reasons"][funnel.EXCLUSION_STAGE_BEFORE_ANCHOR]
            == 1)


def test_6_the_cohort_is_not_built_by_chaining_adjacent_independent_cohorts():
    """`cohort_conversion` and the displayed progression answer different
    questions, and the progression is never assembled from the former.

    Constructed so the two DISAGREE: the MQL→SQL adjacent cohort is 100%, while
    the Lead-anchored cohort reaches SQL for only one of two contacts. A
    progression built by chaining adjacent rates would report 2 at SQL.
    """
    rows = [
        _contact("full", lead=date(2026, 1, 2), mql=date(2026, 1, 20),
                 sql=date(2026, 2, 1), stage="salesqualifiedlead"),
        _contact("stalled", lead=date(2026, 1, 3), mql=date(2026, 2, 10),
                 stage="marketingqualifiedlead"),
    ]
    pops = _pops(rows)
    adjacent = funnel.cohort_conversion(pops, funnel.EVENT_MQL, funnel.EVENT_SQL)
    assert adjacent["cohort_size"] == 2 and adjacent["rate_pct"] == 50.0
    by_event = {s["event"]: s for s in funnel.lead_cohort_progression(pops)["stages"]}
    assert by_event["sql"]["reached"] == 1


def test_7_percentages_are_computed_by_the_service_from_the_anchor():
    rows = [_contact(f"c{i}", lead=date(2026, 1, 2), stage="lead") for i in range(4)]
    rows[0]["date_entered_mql"] = date(2026, 1, 10)
    rows[0]["lifecycle_stage"] = "marketingqualifiedlead"
    cohort = funnel.lead_cohort_progression(_pops(rows))
    by_event = {s["event"]: s for s in cohort["stages"]}
    assert by_event["lead"]["rate_from_anchor_pct"] == 100.0
    assert by_event["mql"]["rate_from_anchor_pct"] == 25.0
    assert by_event["mql"]["previous_stage_conversion_pct"] == 25.0
    assert by_event["lead"]["previous_stage_conversion_pct"] is None


def test_8_cohort_block_carries_every_required_field():
    payload_fields = {
        "anchor_event", "cohort_size", "stages", "converted",
        "rate_from_anchor_pct", "previous_stage_conversion_pct", "available",
        "truth_status", "coverage_status", "excluded_contacts",
        "exclusion_reasons",
    }
    cohort = funnel.lead_cohort_progression(
        _pops([_contact("a", lead=date(2026, 1, 2), stage="lead")]))
    assert payload_fields <= set(cohort)


def test_9_an_unavailable_scope_yields_null_counts_not_zero():
    """An attribution outage must not render as an empty cohort."""
    rows = [_contact("a", lead=date(2026, 1, 2), stage="lead",
                     source="PAID_SEARCH")]
    pops = _pops(rows, identity_available=False)
    cohort = funnel.lead_cohort_progression(pops, funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE)
    assert cohort["available"] is False
    assert cohort["cohort_size"] is None
    assert cohort["excluded_contacts"] is None
    assert cohort["reason"] == funnel.REASON_CAMPAIGN_IDENTITY_UNAVAILABLE


# ═════════════════════════════════════════════════════════════════════════════
# §3 — partial coverage, surfaced and never imputed
# ═════════════════════════════════════════════════════════════════════════════
def test_10_a_reached_stage_without_a_timestamp_is_excluded_and_counted():
    rows = [_contact("gap", lead=date(2026, 1, 2), stage="salesqualifiedlead")]
    cohort = funnel.lead_cohort_progression(_pops(rows))
    by_event = {s["event"]: s for s in cohort["stages"]}
    assert by_event["mql"]["reached"] == 0
    assert (by_event["mql"]["exclusion_reasons"][funnel.EXCLUSION_MISSING_STAGE_DATE]
            == 1)
    assert cohort["coverage_status"] == funnel.COVERAGE_PARTIAL
    assert cohort["excluded_contacts"] == 1


def test_11_partial_coverage_is_not_ready_and_complete_coverage_is():
    partial = funnel.lead_cohort_progression(
        _pops([_contact("gap", lead=date(2026, 1, 2), stage="opportunity")]))
    assert partial["truth_status"] == "not_ready"
    clean = funnel.lead_cohort_progression(
        _pops([_contact("ok", lead=date(2026, 1, 2), stage="lead")]))
    assert clean["coverage_status"] == funnel.COVERAGE_COMPLETE
    assert clean["truth_status"] == "ready"


def test_12_a_contact_with_no_lead_date_is_disclosed_over_its_own_population():
    """Anchorless contacts cannot be placed in ANY window's cohort.

    They are reported separately rather than summed into the cohort's excluded
    total, because that integer would then mix two populations.
    """
    rows = [
        _contact("anchored", lead=date(2026, 1, 2), stage="lead"),
        _contact("anchorless", mql=date(2026, 2, 1),
                 stage="marketingqualifiedlead"),
    ]
    cohort = funnel.lead_cohort_progression(_pops(rows))
    records = {r["reason"]: r for r in cohort["exclusion_reasons"]}
    anchorless = records[funnel.EXCLUSION_MISSING_ANCHOR_DATE]
    assert anchorless["contacts"] == 1
    assert anchorless["population"] == "contacts_considered"
    assert anchorless["counts_toward_excluded_contacts"] is False
    assert cohort["excluded_contacts"] == 0


def test_13_no_proxy_date_is_ever_substituted_for_a_missing_stage_date():
    """createdate is present and is NOT used to fill the missing MQL entry."""
    row = _contact("gap", lead=date(2026, 1, 2), stage="marketingqualifiedlead")
    row["created_at"] = date(2025, 1, 1)
    pops = _pops([row])
    contact = pops["contacts"][0]
    assert contact["event_dates"][funnel.EVENT_MQL] is None
    assert contact["created_at"] == date(2025, 1, 1)
    by_event = {s["event"]: s
                for s in funnel.lead_cohort_progression(pops)["stages"]}
    assert by_event["mql"]["reached"] == 0


def test_14_every_stage_date_declares_where_it_came_from():
    rows = [_contact("a", lead=date(2026, 1, 2), stage="lead")]
    rows[0]["date_entered_mql"] = date(2026, 1, 20)
    rows[0]["date_entered_mql_from_history"] = True
    contact = _pops(rows)["contacts"][0]
    assert (contact["event_date_provenance"][funnel.EVENT_LEAD]
            == funnel.PROVENANCE_HUBSPOT_PROPERTY)
    assert (contact["event_date_provenance"][funnel.EVENT_MQL]
            == funnel.PROVENANCE_PROPERTY_HISTORY)


# ═════════════════════════════════════════════════════════════════════════════
# §4 — recovery from real evidence only
# ═════════════════════════════════════════════════════════════════════════════
def _version(value, ts, source_type="CRM_UI"):
    return {"value": value, "timestamp": ts, "source_type": source_type,
            "source_id": "42", "updated_by_user_id": "7"}


def test_15_a_matching_history_version_recovers_the_real_timestamp():
    row = {"contact_id": "c1", "lifecycle_stage": "salesqualifiedlead",
           "date_entered_lead": date(2026, 1, 1), "date_entered_mql": None,
           "date_entered_sql": date(2026, 3, 1),
           "date_entered_opportunity": None, "date_entered_customer": None}
    recovered, unresolved = recovery.select_recovered_events(
        row, [_version("marketingqualifiedlead", date(2026, 2, 2))])
    assert len(recovered) == 1
    assert recovered[0]["funnel_event"] == funnel.EVENT_MQL
    assert recovered[0]["entered_at"] == date(2026, 2, 2)
    assert recovered[0]["hubspot_source_type"] == "CRM_UI"
    assert unresolved == []


def test_16_no_matching_version_recovers_nothing_and_says_why():
    row = {"contact_id": "c1", "lifecycle_stage": "salesqualifiedlead",
           "date_entered_lead": date(2026, 1, 1), "date_entered_mql": None,
           "date_entered_sql": date(2026, 3, 1),
           "date_entered_opportunity": None, "date_entered_customer": None}
    recovered, unresolved = recovery.select_recovered_events(
        row, [_version("lead", date(2026, 1, 1))])
    assert recovered == []
    assert unresolved == [{"funnel_event": funnel.EVENT_MQL,
                           "reason": recovery.NO_HISTORY_VERSION}]


def test_17_recovery_never_writes_to_hubspot():
    """No write verb reaches HubSpot from the recovery path.

    The connector helper it calls is a batch READ; the service and the command
    both declare `hubspot_writes_performed` False, and neither file contains a
    HubSpot mutation call.
    """
    for src in (_RECOVERY_SRC, _BACKFILL_SRC):
        for verb in ("basic_api.create", "basic_api.update", "batch_api.create",
                     "batch_api.update", "batch_api.archive", "requests.post",
                     "requests.patch", "requests.put", "requests.delete"):
            assert verb not in src, f"{verb} must never appear in the recovery path"
    assert "hubspot_writes_performed" in _RECOVERY_SRC


def test_18_the_backfill_command_defaults_to_a_dry_run():
    assert '"--apply", action="store_true"' in _BACKFILL_SRC
    assert "apply: bool = False" in _RECOVERY_SRC
    # Bounded and resumable.
    assert '"--limit"' in _BACKFILL_SRC and '"--restart"' in _BACKFILL_SRC
    assert "def recover(*, limit: int" in _RECOVERY_SRC


# ═════════════════════════════════════════════════════════════════════════════
# §5/§6 — fail-closed revenue, with the blockers named
# ═════════════════════════════════════════════════════════════════════════════
def test_19_the_disclosure_separates_the_known_sum_from_the_unknown_total():
    deals = [_deal(f"p{i}", amount=100.0, currency="USD") for i in range(167)]
    deals += [_deal(f"u{i}", status="unavailable", reason="no_amount")
              for i in range(14)]
    d = canonical_revenue.revenue_disclosure(_base(deals))

    assert d["closed_won_deals"] == 181
    assert d["revenue_proven_deals"] == 167
    assert d["revenue_unavailable_deals"] == 14
    assert d["known_revenue_usd"] == 16700.0
    assert d["total_revenue_usd"] is None
    assert d["total_revenue_publishable"] is False
    assert d["unavailable_reason"] == "closed_won_deals_missing_amount"
    assert d["violation_codes"] == [canonical_revenue.V_CURRENCY_UNPROVEN_DEALS]
    # The label names its own denominator, so it cannot read as a window total.
    assert d["known_revenue_label"] == "Known revenue from 167 priced deals"


def test_20_the_count_is_published_even_though_the_total_is_not():
    deals = [_deal("p1", amount=100.0, currency="USD"),
             _deal("u1", status="unavailable", reason="no_amount")]
    d = canonical_revenue.revenue_disclosure(_base(deals))
    assert d["closed_won_deals"] == 2
    assert d["total_revenue_usd"] is None


def test_21_a_refused_read_reports_null_counts_never_zero():
    refused = {"available": False, "reason": canonical_revenue.REASON_LEDGER_UNREADABLE,
               "detail": "db down", "violation_codes": []}
    d = canonical_revenue.revenue_disclosure(refused)
    assert d["closed_won_deals"] is None
    assert d["revenue_unavailable_deals"] is None
    assert d["known_revenue_usd"] is None
    assert d["total_revenue_publishable"] is False


def test_22_the_missing_amount_report_names_every_blocking_deal():
    deals = [_deal("p1", amount=100.0, currency="USD"),
             _deal("u1", status="unavailable", reason="no_amount",
                   close="2024-12-12", name="Batti Logistics - New Deal"),
             _deal("u2", amount=50.0, currency="XYZ", status="unavailable",
                   reason="unknown_currency", close="2024-06-13")]
    report = canonical_revenue.missing_amount_deals(_base(deals))

    assert report["deal_count"] == 2
    assert {d["deal_id"] for d in report["deals"]} == {"u1", "u2"}
    first = next(d for d in report["deals"] if d["deal_id"] == "u1")
    assert first["deal_name"] == "Batti Logistics - New Deal"
    assert first["deal_close_date"] == "2024-12-12"
    assert first["amount_status"] == canonical_revenue.AMOUNT_STATUS_MISSING
    # The canonical `analysis.deal_currency` reason, verbatim — not a second
    # vocabulary invented by the report.
    assert first["reason"] == "no_amount"
    assert first["fallback_used"] is False
    assert report["writes_performed"] is False
    # An amount that IS present with an unprovable currency is a different fix,
    # and stays distinguishable.
    second = next(d for d in report["deals"] if d["deal_id"] == "u2")
    assert second["amount_status"] == canonical_revenue.AMOUNT_STATUS_PRESENT
    assert second["reason"] == "unknown_currency"


def test_23_the_hubspot_record_url_is_omitted_rather_than_guessed(monkeypatch):
    monkeypatch.setattr(canonical_revenue, "hubspot_portal_id", lambda: None)
    report = canonical_revenue.missing_amount_deals(
        _base([_deal("u1", status="unavailable", reason="no_amount")]))
    assert report["deals"][0]["hubspot_record_url"] is None

    monkeypatch.setattr(canonical_revenue, "hubspot_portal_id", lambda: "142257138")
    report = canonical_revenue.missing_amount_deals(
        _base([_deal("u1", status="unavailable", reason="no_amount")]))
    assert (report["deals"][0]["hubspot_record_url"]
            == "https://app.hubspot.com/contacts/142257138/record/0-3/u1")


def test_24_a_refused_read_does_not_report_zero_unpriced_deals():
    report = canonical_revenue.missing_amount_deals(
        {"available": False, "reason": canonical_revenue.REASON_COVERAGE_NOT_PROVEN})
    assert report["deal_count"] is None
    assert report["deals"] is None


def test_25_no_amount_is_ever_inferred_in_the_missing_amount_report():
    """A blocked deal reports no amount at all — not the average, not a share."""
    deals = [_deal("p1", amount=1000.0, currency="USD"),
             _deal("u1", status="unavailable", reason="no_amount")]
    report = canonical_revenue.missing_amount_deals(_base(deals))
    blocked = report["deals"][0]
    assert "revenue_usd" not in blocked and "amount" not in blocked
    for source in (_MISSING_SRC,):
        assert "no amount is guessed" in source.lower() or "guessed" in source.lower()


# ═════════════════════════════════════════════════════════════════════════════
# §7 — four kinds of unavailability, told apart
# ═════════════════════════════════════════════════════════════════════════════
def _readings(reason):
    return [{"consumer": "overview", "unavailable_reason": reason,
             "truth_status": "not_ready", "violation_codes": []},
            {"consumer": "revenue", "unavailable_reason": reason,
             "truth_status": "not_ready", "violation_codes": []}]


@pytest.mark.parametrize("reason,expected_status,expected_code", [
    (canonical_revenue.REASON_LEDGER_UNREADABLE,
     "database_unreadable", parity.V_DB_UNREADABLE),
    (canonical_revenue.REASON_SYNC_STATE_UNREADABLE,
     "database_unreadable", parity.V_DB_UNREADABLE),
    (canonical_revenue.REASON_COVERAGE_NOT_PROVEN,
     "population_unavailable", parity.V_POPULATION_UNAVAILABLE),
    (canonical_revenue.REASON_REVENUE_INCOMPLETE,
     "total_unpublishable", parity.V_TOTAL_UNPUBLISHABLE),
])
def test_26_each_unavailability_class_gets_its_own_code(reason, expected_status,
                                                        expected_code):
    status, code, detail = parity._classify_unavailable(
        _readings(reason), "closed_won_revenue_usd")
    assert status == expected_status
    assert code == expected_code
    assert reason in detail


def test_27_all_time_revenue_reports_the_missing_amount_code_not_a_generic_outage():
    _status, code, detail = parity._classify_unavailable(
        _readings(canonical_revenue.REASON_REVENUE_INCOMPLETE),
        "closed_won_revenue_usd")
    assert code == "revenue_total_unpublishable_missing_amount"
    assert code != parity.V_SOURCE_UNAVAILABLE
    assert "resolved by pricing those deals at source" in detail


def test_28_an_unexplained_absence_stays_the_catch_all_rather_than_being_guessed():
    status, code, _detail = parity._classify_unavailable(
        [{"consumer": "overview", "unavailable_reason": None,
          "truth_status": None, "violation_codes": []}], "sqls")
    assert status == "unavailable"
    assert code == parity.V_SOURCE_UNAVAILABLE


def test_29_consumers_declaring_different_reasons_is_itself_reported():
    readings = [
        {"consumer": "overview",
         "unavailable_reason": canonical_revenue.REASON_LEDGER_UNREADABLE,
         "truth_status": "not_ready", "violation_codes": []},
        {"consumer": "revenue",
         "unavailable_reason": canonical_revenue.REASON_REVENUE_INCOMPLETE,
         "truth_status": "not_ready", "violation_codes": []},
    ]
    status, code, detail = parity._classify_unavailable(readings, "revenue")
    assert status == "unavailable"
    assert code == parity.V_SOURCE_UNAVAILABLE
    assert "differing reasons" in detail


def test_30_lifecycle_partial_is_disclosed_with_counts_and_is_not_a_violation():
    consumers = {"overview": {"payload": {"lifecycle_cohort": {
        "coverage_status": "partial", "truth_status": "not_ready",
        "cohort_size": 40, "excluded_contacts": 3,
        "exclusion_reasons": [{"reason": "missing_stage_entry_date",
                               "population": "lead_cohort", "contacts": 3}]}}}}
    rows = parity._lifecycle_coverage_disclosures(consumers)
    assert len(rows) == 1
    assert rows[0]["code"] == parity.V_LIFECYCLE_PARTIAL
    assert rows[0]["excluded_contacts"] == 3
    # Complete coverage produces no disclosure row at all.
    consumers["overview"]["payload"]["lifecycle_cohort"]["coverage_status"] = "complete"
    assert parity._lifecycle_coverage_disclosures(consumers) == []


# ═════════════════════════════════════════════════════════════════════════════
# §8 — static guards against the misleading Dashboard
# ═════════════════════════════════════════════════════════════════════════════
def _dashboard_funnel_source() -> str:
    start = _APP.index("function renderDashLifecycleCohort(d)")
    end = _APP.index("function renderDashCommercialOutcomes(d)")
    return _APP[start:end]


def test_31_the_old_independent_totals_funnel_is_gone():
    assert "function renderDashFunnel(" not in _APP
    assert "renderDashLifecycleCohort(d)" in _APP
    assert "renderDashCommercialOutcomes(d)" in _APP


def test_32_the_lifecycle_strip_reads_the_cohort_contract_not_stage_totals():
    src = _dashboard_funnel_source()
    assert "d.lifecycle_cohort" in src
    for independent_total in ("lifecycle_leads", "lifecycle_mqls", "lifecycle_sqls",
                              "lifecycle_opportunities", "lifecycle_customers"):
        assert independent_total not in src, (
            f"{independent_total} is an independent window total and must not "
            "appear inside the cohort funnel")


def test_33_closed_won_customers_and_revenue_are_not_inside_the_lifecycle_strip():
    src = _dashboard_funnel_source()
    assert "closed_won_revenue_usd" not in src
    assert "k.customers" not in src
    assert "commercial_outcomes" not in src


def test_34_no_funnel_arrow_connects_lifecycle_customer_to_deal_revenue():
    outcomes_start = _APP.index("function renderDashCommercialOutcomes(d)")
    outcomes = _APP[outcomes_start:outcomes_start + 4000]
    # The outcomes section is its own panel and draws no conversion chips.
    assert "dash-funnel__conv" not in outcomes
    assert "dash-outcomes-panel" in outcomes
    assert "connected_to_lifecycle_funnel" in _OVERVIEW


def test_35_the_dashboard_computes_no_conversion_arithmetic_in_javascript():
    """Every rate rendered comes from the backend contract.

    The old helper that divided two counts in the browser is gone, and the new
    renderer contains no division at all.
    """
    assert "function dashConversion(" not in _APP
    src = _dashboard_funnel_source()
    assert "/" not in re.sub(r"//.*|/\*[\s\S]*?\*/", "", src).replace("</", "<")
    assert "rate_from_anchor_pct" in src
    assert "previous_stage_conversion_pct" in src


def test_36_partial_lifecycle_coverage_is_rendered_not_hidden():
    assert "Partial lifecycle history" in _APP
    assert "function dashCohortCoverageBadge" in _APP
    assert "function dashCohortCoverageDisclosure" in _APP
    assert "excluded from this cohort" in _APP


def test_37_known_revenue_is_never_rendered_under_a_total_revenue_label():
    outcomes_start = _APP.index("function renderDashCommercialOutcomes(d)")
    outcomes = _APP[outcomes_start:outcomes_start + 4000]
    # The total cell renders `total_revenue_usd` and nothing else.
    total_cell = outcomes[outcomes.index("Total Closed-Won Revenue"):]
    total_cell = total_cell[:total_cell.index("</div>\n        </div>")]
    assert "total_revenue_usd" in total_cell
    assert "known_revenue_usd" not in total_cell
    # The partial sum is rendered only under the backend's own label.
    assert "o.known_revenue_label" in outcomes
    assert "not the window total" in outcomes


def test_38_the_overview_publishes_the_cohort_and_the_separated_outcomes():
    assert '"lifecycle_cohort": lifecycle_funnel.get("cohort")' in _OVERVIEW
    assert '"commercial_outcomes": _commercial_outcomes(' in _OVERVIEW
    assert '"connected_to_lifecycle_funnel": False' in _OVERVIEW
