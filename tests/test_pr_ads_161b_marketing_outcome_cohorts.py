"""
PR-ADS-161B — canonical marketing outcome cohorts and the Campaign Evidence
migration.

The brief's twelve required cases are ``test_01`` … ``test_12``. Every guard
this PR adds also has a counterfactual: a test proving the guard goes red when
the property it protects is broken. A guard whose absence changes nothing is
not a guard.

Where a test's subject is production behaviour it drives the real producer —
the real repository read against a real PostgreSQL (``test_2x``), or the real
JavaScript gate run in ``node`` (``test_15x``) — rather than a hand-built
intermediate structure.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import tests.conftest as conftest  # noqa: F401  (import-order guard)
import scripts.audit_marketing_outcome_cohorts as audit
import services.marketing_outcome_cohort_service as svc
from tests.test_pr_ads_153e_a_pg_integration import _have_postgres, pg  # noqa: F401

_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "static" / "app.js"

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

#: A fixed "now", and the 30d evidence window it resolves to (account-local
#: Europe/London days, inclusive): 2026-09-04 … 2026-10-03.
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
START, END = date(2026, 9, 4), date(2026, 10, 3)
START_AT, END_BEFORE = svc.window_instants(START, END)
INSIDE = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
BEFORE = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
SQL_DATE = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)

_ids = itertools.count(1)


def _contact(cid, *, created=INSIDE, stage="lead", direct=None, recovered=None,
             source="PAID_SEARCH", label="Brand - UK", gclid=None, row_id=None):
    """One row in the shape `fetch_acquisition_cohort_contacts` returns."""
    return {"funnel_row_id": row_id if row_id is not None else next(_ids),
            "contact_id": cid, "created_at": created, "lifecycle_stage": stage,
            "sql_entered_direct": direct, "sql_entered_recovered": recovered,
            "hs_analytics_source": source, "hs_analytics_source_data_1": label,
            "gclid": gclid}


def _resolver(spend=(("1", "Brand - UK"), ("2", "Gulf")), mappings=()):
    """Campaign Evidence's REAL resolver over a given spend/identity index."""
    from services.campaign_evidence_service import (
        _assign_lead, _identity_index, _spend_by_campaign_id)
    _, norm_to_ids = _spend_by_campaign_id(
        {"rows": [{"campaign_id": cid, "campaign_name": n} for cid, n in spend]})
    by_label, _ = _identity_index({"mappings": list(mappings)})
    return lambda label: _assign_lead(label, by_label, norm_to_ids)


def _cohort(rows, resolver=None):
    cohort = svc.build_cohort(rows, resolve_label=resolver or _resolver(),
                              start_at=START_AT, end_before=END_BEFORE)
    assert svc.reconcile_cohort(cohort) == [], "every test cohort must reconcile"
    return cohort


# ═════════════════════════════════════════════════════════════════════════════
# §1 — SQL proof and cohort membership (required cases 1–4)
# ═════════════════════════════════════════════════════════════════════════════

def test_01_a_contact_created_in_the_window_with_an_exact_sql_date_is_counted_once():
    c = _cohort([_contact("c1", stage="salesqualifiedlead", direct=SQL_DATE)])
    assert c["all_sources"]["sqls"] == 1
    assert c["by_campaign"]["1"]["sqls"] == 1
    assert c["proof_counts"][svc.PROOF_DIRECT] == 1
    assert c["all_sources"]["sqls_missing_event_timestamp"] == 0


def test_01b_a_recovered_lifecycle_transition_proves_sql():
    c = _cohort([_contact("c1", stage="lead", recovered=SQL_DATE)])
    assert c["all_sources"]["sqls"] == 1
    assert c["proof_counts"][svc.PROOF_RECOVERED] == 1
    assert c["all_sources"]["sqls_missing_event_timestamp"] == 0


def test_02_an_undated_salesqualifiedlead_is_counted_once_and_disclosed_without_a_date():
    """The 668. Reached-SQL is proven; WHEN is unknown; it is counted anyway,
    in the cohort of the period it was CREATED in — and no date is attached."""
    c = _cohort([_contact("c1", stage="salesqualifiedlead")])
    assert c["all_sources"]["sqls"] == 1
    assert c["by_campaign"]["1"]["sqls"] == 1
    assert c["all_sources"]["sqls_missing_event_timestamp"] == 1
    assert c["proof_counts"][svc.PROOF_STAGE] == 1
    # No timestamp invented: the classified contact carries no date of any kind.
    record = c["sql_identities"]["c1"]
    assert set(record) == {"proof", "bucket", "campaign_key", "reason"}
    assert svc.coverage_status(c) == svc.COVERAGE_EVENT_GAPS
    notes = svc.coverage_notes(c, missing_created_at=0)
    assert any("no exact SQL-entry timestamp" in n for n in notes)


@pytest.mark.parametrize("stage", ["opportunity", "customer", "evangelist",
                                   "Customer ", "OPPORTUNITY"])
def test_03_an_undated_later_stage_contact_is_counted_once(stage):
    c = _cohort([_contact("c1", stage=stage)])
    assert c["all_sources"]["sqls"] == 1
    assert c["all_sources"]["sqls_missing_event_timestamp"] == 1


@pytest.mark.parametrize("stage", ["lead", "marketingqualifiedlead", "subscriber",
                                   "other", "370543605", "377714653", None, ""])
def test_03b_negative_control_a_stage_below_sql_with_no_evidence_is_not_an_sql(stage):
    """Without this, test_03 would pass for a rule that called everyone an SQL."""
    c = _cohort([_contact("c1", stage=stage)])
    assert c["all_sources"]["contacts_acquired"] == 1
    assert c["all_sources"]["sqls"] == 0


def test_03c_the_stage_rule_is_the_repositorys_one_rule_not_a_copy():
    """The brief forbids a second competing stage ranking."""
    from analysis.crm_lifecycle import EVENT_SQL, stages_implying_event
    assert svc._STAGES_IMPLYING_SQL == frozenset(stages_implying_event(EVENT_SQL))
    assert svc._STAGES_IMPLYING_SQL == {
        "salesqualifiedlead", "opportunity", "customer", "evangelist"}


def test_04_a_contact_created_before_the_window_is_excluded_even_if_it_entered_sql_inside():
    """Created in August, entered SQL on 25 September. It is in AUGUST's
    acquisition cohort, not September's — and it is a September lifecycle event
    (test_23 proves that through the real lifecycle reader)."""
    c = _cohort([_contact("old", created=BEFORE, stage="salesqualifiedlead",
                          direct=SQL_DATE)])
    assert c["all_sources"]["contacts_acquired"] == 0
    assert c["all_sources"]["sqls"] == 0
    assert c["rows_outside_window"] == 1


def test_04b_window_edges_are_account_local_midnight_not_utc():
    """During BST, London midnight is 23:00 UTC the day before. Spend days are
    London days, so cohort days must be too — or CPQL's two halves disagree."""
    assert START_AT == datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc)
    assert END_BEFORE == datetime(2026, 10, 3, 23, 0, tzinfo=timezone.utc)
    just_in = datetime(2026, 9, 3, 23, 30, tzinfo=timezone.utc)      # 00:30 London, 4 Sep
    just_out = datetime(2026, 9, 3, 22, 30, tzinfo=timezone.utc)     # 23:30 London, 3 Sep
    assert svc.in_window(just_in, START_AT, END_BEFORE)
    assert not svc.in_window(just_out, START_AT, END_BEFORE)
    late = datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc)        # 00:30 London, 4 Oct
    assert not svc.in_window(late, START_AT, END_BEFORE)


def test_04c_a_contact_with_no_created_at_is_in_no_window_all_time_included():
    _, all_time_end = svc.window_instants(None, END)
    assert not svc.in_window(None, None, all_time_end)
    c = svc.build_cohort([_contact("c1", created=None, stage="customer")],
                         resolve_label=_resolver(), start_at=None,
                         end_before=all_time_end)
    assert c["all_sources"]["contacts_acquired"] == 0
    notes = svc.coverage_notes(c, missing_created_at=1)
    assert any("no created date" in n for n in notes)


# ═════════════════════════════════════════════════════════════════════════════
# §2 — attribution: three buckets, nothing dropped (required cases 5–7)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("label,reason", [
    ("Unknown Campaign 9", svc.REASON_UNMAPPED_LABEL),
    (None, "missing_campaign"),
    ("", "missing_campaign"),
    ("(direct)", "pseudo_campaign"),
])
def test_05_a_google_ads_sql_with_no_campaign_mapping_is_unattributed_not_dropped(label, reason):
    c = _cohort([_contact("c1", stage="salesqualifiedlead", label=label)])
    assert c["google_ads"]["sqls"] == 1, "the SQL left the Google Ads total"
    assert c["unattributed"]["sqls"] == 1
    assert c["unattributed"]["by_reason"] == {reason: 1}
    assert sum(s["sqls"] for s in c["by_campaign"].values()) == 0


def test_05b_an_unmapped_label_gets_its_own_mapping_review_key():
    c = _cohort([_contact("c1", stage="customer", label="Unknown Campaign 9")])
    assert list(c["unattributed"]["by_label"]) == ["unmatched:unknown campaign 9"]
    assert c["unattributed"]["without_label_row"]["sqls"] == 0


def test_05c_alias_mapping_places_the_sql_on_its_campaign():
    mappings = [{"external_campaign_label": "brand uk old name", "campaign_id": "1",
                 "match_method": "manual"}]
    c = _cohort([_contact("c1", stage="salesqualifiedlead", label="Brand UK old name")],
                resolver=_resolver(mappings=mappings))
    assert c["by_campaign"]["1"]["sqls"] == 1
    assert c["unattributed"]["sqls"] == 0


def test_06_a_non_google_sql_is_excluded_from_the_google_ads_total_and_counted():
    c = _cohort([_contact("c1", stage="salesqualifiedlead", source="ORGANIC_SEARCH")])
    assert c["google_ads"]["sqls"] == 0
    assert c["excluded_non_google"]["sqls"] == 1
    assert c["excluded_non_google"]["by_reason"] == {svc.REASON_NON_GOOGLE_SOURCE: 1}
    assert c["all_sources"]["sqls"] == 1


def test_06b_an_approved_not_google_ads_label_is_excluded_with_its_own_reason():
    """HubSpot files Microsoft Ads under PAID_SEARCH too; an approved
    not_google_ads mapping says which labels those are."""
    mappings = [{"external_campaign_label": "bing brand", "campaign_id": None,
                 "match_method": "not_google_ads"}]
    c = _cohort([_contact("c1", stage="salesqualifiedlead", label="Bing Brand")],
                resolver=_resolver(mappings=mappings))
    assert c["google_ads"]["sqls"] == 0
    assert c["excluded_non_google"]["by_reason"] == {svc.REASON_LABEL_NOT_GOOGLE_ADS: 1}


def test_06c_a_gclid_on_a_non_google_source_is_disclosed_not_hidden():
    c = _cohort([_contact("c1", stage="customer", source="OFFLINE", gclid="Cj0abc")])
    assert c["excluded_non_google"]["sqls_with_gclid"] == 1


def test_07_duplicate_rows_for_one_contact_count_once():
    rows = [_contact("dup", stage="lead"),
            _contact("dup", stage="salesqualifiedlead", direct=SQL_DATE)]
    c = _cohort(rows)
    assert c["all_sources"]["contacts_acquired"] == 1
    assert c["all_sources"]["sqls"] == 1
    assert c["dedup"]["duplicate_rows"] == 1
    # The strongest proof across the rows wins — any canonical row proves it.
    assert c["proof_counts"][svc.PROOF_DIRECT] == 1


def test_07b_duplicate_rows_that_disagree_on_campaign_are_unattributed_not_guessed():
    rows = [_contact("dup", stage="customer", label="Brand - UK"),
            _contact("dup", stage="customer", label="Gulf")]
    c = _cohort(rows)
    assert c["unattributed"]["by_reason"] == {svc.REASON_CONFLICTING_ROWS: 1}
    assert sum(s["sqls"] for s in c["by_campaign"].values()) == 0


def test_07c_a_blank_contact_id_uses_the_reported_fallback_identity():
    c = _cohort([_contact("", stage="customer", row_id=77),
                 _contact("  ", stage="customer", row_id=78)])
    assert c["all_sources"]["sqls"] == 2
    assert c["dedup"]["fallback_identities"] == 2
    assert set(c["sql_identities"]) == {"funnel_row:77", "funnel_row:78"}


# ═════════════════════════════════════════════════════════════════════════════
# §3 — closed-won deals (required case 8)
# ═════════════════════════════════════════════════════════════════════════════

def _deal(deal_id, *, contact="c1", gclid=None, campaign="Brand - UK",
          group="google_ads", status="attributed", revenue=1000.0):
    return {"deal_id": deal_id, "primary_contact_id": contact, "gclid": gclid,
            "campaign_name_raw": campaign, "acquisition_group": group,
            "attribution_status": status, "revenue_usd": revenue}


def _deals(rows, created=None):
    created = created if created is not None else {"c1": INSIDE, "c2": INSIDE}
    return svc.build_deal_outcomes(rows, created_at_by_contact=created,
                                   resolve_label=_resolver(),
                                   start_at=START_AT, end_before=END_BEFORE)


def test_08_one_closed_won_deal_with_several_contacts_counts_once():
    """The ledger names ONE primary contact per deal, and repeated ledger rows
    for the same deal_id are collapsed — never one deal per associated contact."""
    d = _deals([_deal("D1", contact="c1"), _deal("D1", contact="c2"),
                _deal("D1", contact="c1")])
    assert d["buckets"][svc.BUCKET_CAMPAIGN]["deals"] == 1
    assert d["dedup"]["duplicate_rows"] == 2
    assert d["dedup"]["distinct_deals_examined"] == 1
    assert d["by_campaign"]["1"]["revenue_usd_known"] == 1000.0


def test_08b_deal_buckets_follow_the_scope_lattice():
    d = _deals([
        _deal("campaign"),
        _deal("no_campaign", campaign=None),
        _deal("ambiguous", group="google_ads", status="ambiguous"),
        _deal("organic", group="organic", status="attributed"),
        _deal("gclid_only", group="organic", status="ambiguous", gclid="Cj0x"),
    ])
    b = d["buckets"]
    assert b[svc.BUCKET_CAMPAIGN]["deals"] == 2         # campaign + gclid_only
    assert b[svc.BUCKET_UNATTRIBUTED]["deals"] == 1     # no_campaign
    assert b[svc.BUCKET_AMBIGUOUS]["deals"] == 1        # contacts disagree
    assert b[svc.BUCKET_EXCLUDED]["deals"] == 1         # organic
    assert d["google_ads"]["deals"] == 3


def test_08c_a_deal_that_cannot_be_placed_in_time_is_disclosed_not_windowed():
    d = _deals([_deal("A", contact=""), _deal("B", contact="ghost"),
                _deal("C", contact="c9")], created={"c9": None})
    assert d["unplaceable"] == {svc.UNPLACEABLE_NO_PRIMARY_CONTACT: 1,
                                svc.UNPLACEABLE_CONTACT_NOT_IN_FUNNEL: 1,
                                svc.UNPLACEABLE_CONTACT_NO_CREATED_AT: 1}
    assert sum(x["deals"] for x in d["buckets"].values()) == 0


def test_08d_a_deal_whose_contact_was_acquired_outside_the_window_is_not_in_it():
    d = _deals([_deal("A", contact="c1")], created={"c1": BEFORE})
    assert sum(x["deals"] for x in d["buckets"].values()) == 0
    assert d["unplaceable_total"] == 0


def test_08e_closed_won_deals_are_never_labelled_unique_customers():
    meta = svc.deal_metric_metadata(deals=_deals([_deal("A")]), freshness={},
                                    attribution_status="complete")
    assert meta["dedup_key"] == "deal_id"
    assert "not unique customers" in meta["label"]
    js = _APP_JS.read_text(encoding="utf-8")
    assert ">Unique customers<" not in js and ">Unique Customers<" not in js


def test_08f_missing_usd_revenue_is_a_known_subset_not_zero():
    d = _deals([_deal("A", revenue=500.0), _deal("B", revenue=None)])
    slot = d["buckets"][svc.BUCKET_CAMPAIGN]
    assert slot["revenue_usd_known"] == 500.0 and slot["revenue_usd_missing"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §4 — CPQL (required case 9)
# ═════════════════════════════════════════════════════════════════════════════

def _cpql(**kw):
    base = dict(cohort_available=True, spend_available=True, spend_usd=1200.0,
                cohort_sqls=4, source_fresh=True)
    base.update(kw)
    return svc.cpql_decision(**base)


@pytest.mark.parametrize("sqls", [0, None])
def test_09_zero_cohort_sqls_gives_no_cpql_never_zero_or_infinity(sqls):
    status, reason, value = _cpql(cohort_sqls=sqls)
    assert value is None
    assert status == svc.STATUS_NOT_APPLICABLE and reason == svc.CPQL_REASON_ZERO_SQLS


def test_09b_cpql_divides_window_spend_by_cohort_sqls():
    assert _cpql() == (svc.STATUS_PUBLISHED, None, 300.0)


def test_09c_cpql_is_never_blocked_by_missing_sql_entry_dates():
    """Every SQL here is stage-proven and undated; CPQL still publishes."""
    c = _cohort([_contact(f"c{i}", stage="customer") for i in range(3)])
    assert c["google_ads"]["sqls_missing_event_timestamp"] == 3
    assert _cpql(cohort_sqls=c["google_ads"]["sqls"]) == (svc.STATUS_PUBLISHED, None, 400.0)


def test_09d_a_stale_source_and_an_unknown_source_withhold_cpql_with_different_reasons():
    stale = _cpql(source_fresh=False)
    unknown = _cpql(source_fresh=None)
    assert stale[0] == unknown[0] == svc.STATUS_WITHHELD
    assert stale[1] == svc.CPQL_REASON_SOURCE_NOT_FRESH
    assert unknown[1] == svc.CPQL_REASON_SOURCE_FRESHNESS_UNKNOWN
    assert stale[2] is None and unknown[2] is None


def test_09e_no_spend_or_incomplete_fx_makes_cpql_unavailable():
    assert _cpql(spend_available=False)[:2] == (svc.STATUS_UNAVAILABLE,
                                                svc.CPQL_REASON_SPEND_UNAVAILABLE)
    assert _cpql(spend_usd=None)[:2] == (svc.STATUS_UNAVAILABLE,
                                         svc.CPQL_REASON_FX_INCOMPLETE)
    assert _cpql(cohort_available=False)[:2] == (svc.STATUS_UNAVAILABLE,
                                                 svc.CPQL_REASON_COHORT_UNAVAILABLE)


# ═════════════════════════════════════════════════════════════════════════════
# §5 — no date is invented (required case 10)
# ═════════════════════════════════════════════════════════════════════════════

def test_10_a_missing_sql_timestamp_never_becomes_another_date():
    """Not contact_created_at, not the boundary, not a sync time.

    Behaviourally: the undated contact's membership is decided by created_at
    (its acquisition) and its SQL proof by stage — and no field of the output
    carries a date for it at all. Structurally: no classification function reads
    a boundary, sync or ingestion stamp (counterfactual in test_10b).
    """
    c = _cohort([_contact("c1", stage="customer", created=INSIDE)])
    serialised = json.dumps(svc._jsonable_cohort(c), default=str)
    assert INSIDE.isoformat()[:10] not in serialised
    assert "2026-09" not in serialised
    a = audit.Audit()
    result = audit.check_no_date_contamination(a)
    assert result == {"forbidden_references": {}, "missing_functions": []}
    assert a.exit_code == audit.EXIT_OK


def test_10b_counterfactual_the_contamination_check_fails_on_a_forbidden_timestamp(tmp_path):
    src = _ROOT / "services" / "marketing_outcome_cohort_service.py"
    tampered = src.read_text(encoding="utf-8").replace(
        'if row.get("sql_entered_direct") is not None:',
        'if (row.get("sql_entered_direct") or row.get("known_reached_sql_by")) is not None:', 1)
    assert tampered != src.read_text(encoding="utf-8"), "mutation did not apply"
    path = tmp_path / "svc.py"
    path.write_text(tampered, encoding="utf-8")
    a = audit.Audit()
    result = audit.check_no_date_contamination(a, path=path)
    assert result["forbidden_references"] == {"sql_proof": ["known_reached_sql_by"]}
    assert a.exit_code == audit.EXIT_VIOLATION


def test_10c_counterfactual_a_rename_cannot_quietly_empty_the_contamination_check():
    a = audit.Audit()
    audit.check_no_date_contamination(a, functions=("sql_proof", "renamed_away"))
    assert a.exit_code == audit.EXIT_UNAVAILABLE
    assert "renamed_away" in a.unavailable[0]


def test_10d_the_disclosure_may_report_the_boundary_because_it_is_not_classification():
    """The precise scope of test_10's check: `lifecycle_event_disclosure`
    reports `boundary_observed_at` as metadata and is deliberately outside it."""
    assert "lifecycle_event_disclosure" not in audit.CLASSIFICATION_FUNCTIONS
    assert "boundary_observed_at" in (
        _ROOT / "services" / "marketing_outcome_cohort_service.py").read_text()


# ═════════════════════════════════════════════════════════════════════════════
# §6 — reconciliation through the real page builder (required case 11)
# ═════════════════════════════════════════════════════════════════════════════

def _patch_page(monkeypatch, *, contacts, deals=(), deal_created=None,
                fresh=True, spend_rows=None, mappings=(), lead_rows=()):
    import db.crm_funnel_repository as funnel_repo
    import db.deal_ledger_repository as ledger_repo
    import db.revenue_repository as rev_repo

    spend_rows = spend_rows if spend_rows is not None else [
        {"campaign_id": "1", "campaign_name": "Brand - UK", "spend": 1000.0,
         "spend_usd": 1260.0, "fx_complete": True},
        {"campaign_id": "2", "campaign_name": "Gulf", "spend": 500.0,
         "spend_usd": 630.0, "fx_complete": True}]
    spend = {"available": True, "currency_code": "GBP", "reporting_currency": "USD",
             "fx_complete": True, "fx_missing_days": 0, "customer_id": "123",
             "total_spend": sum(r["spend"] for r in spend_rows),
             "total_spend_usd": sum(r["spend_usd"] for r in spend_rows),
             "rows": spend_rows}
    monkeypatch.setattr(rev_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *a, **k: spend)
    monkeypatch.setattr(rev_repo, "fetch_lead_quality", lambda s, e: {
        "available": True, "rows": list(lead_rows), "event_date_safe": True})
    monkeypatch.setattr(rev_repo, "fetch_campaign_identity", lambda customer_id=None: {
        "available": True, "mappings": list(mappings)})
    monkeypatch.setattr(funnel_repo, "fetch_acquisition_cohort_contacts",
                        lambda s, e: {"available": True, "rows": list(contacts),
                                      "missing_created_at": 0})
    monkeypatch.setattr(funnel_repo, "fetch_contacts_created_at", lambda ids: {
        "available": True, "created_at": dict(deal_created or {})})
    monkeypatch.setattr(ledger_repo, "fetch_won_deals", lambda s=None, e=None: {
        "available": True, "rows": list(deals)})
    monkeypatch.setattr(svc, "read_freshness", lambda now=None: {
        "fresh": fresh, "reason": "source_fresh" if fresh else "source_stale",
        "last_successful_incremental_at": "2026-10-03T06:00:00+00:00",
        "age_hours": 6.0, "detail": ""})
    monkeypatch.setattr(svc, "lifecycle_event_disclosure", lambda: {
        "metric_family": svc.METRIC_FAMILY_LIFECYCLE_EVENTS,
        "window_basis": svc.WINDOW_BASIS_LIFECYCLE_EVENTS,
        "published_on_this_page": False, "reached_sql_by_current_stage": 1531,
        "exact_direct_timestamp": 863, "recovered_timestamp": 0,
        "missing_exact_timestamp": 668, "open_post_boundary_incidents": 103})


def _page(monkeypatch, **kw):
    from services.campaign_evidence_service import build_campaign_evidence
    _patch_page(monkeypatch, **kw)
    return build_campaign_evidence("30d", now=NOW)


_MIXED = [
    _contact("a1", stage="salesqualifiedlead", direct=SQL_DATE, label="Brand - UK"),
    _contact("a2", stage="customer", label="Brand - UK"),                      # undated
    _contact("a3", stage="lead", label="Brand - UK"),
    _contact("g1", stage="opportunity", label="Gulf"),                         # undated
    _contact("u1", stage="salesqualifiedlead", label="Mystery Campaign"),      # unmapped
    _contact("u2", stage="customer", label=None),                              # no label
    _contact("x1", stage="customer", source="ORGANIC_SEARCH", label=None),     # excluded
]


def test_11_campaign_rows_plus_unattributed_equal_the_google_ads_summary(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    s = p["summary"]
    rows = p["campaigns"]
    mapped = sum(r["cohort_sqls"] or 0 for r in rows if r["mapping_status"] == "mapped")
    review = sum(r["cohort_sqls"] or 0 for r in rows if r["mapping_status"] == "unmatched")
    assert s["cohort_sqls_google_ads"] == 5
    assert s["cohort_sqls_mapped"] == mapped == 3
    assert s["cohort_sqls_unattributed"] == 2
    assert mapped + s["cohort_sqls_unattributed"] == s["cohort_sqls_google_ads"]
    assert review + p["cohort"]["breakdown"]["unattributed"]["without_label_row"]["sqls"] \
        == s["cohort_sqls_unattributed"]
    assert s["cohort_sqls_excluded_non_google"] == 1
    assert s["cohort_sqls_all_sources"] == 6
    assert p["cohort"]["reconciliation"]["status"] == "reconciled"
    # An unmapped label surfaces as its own Mapping Review row.
    assert any(r["campaign_key"] == "unmatched:mystery campaign" for r in rows)


def test_11b_the_page_publishes_cohort_cpql_over_the_same_window(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    s = p["summary"]
    assert s["cohort_cpql_status"] == svc.STATUS_PUBLISHED
    assert s["cohort_cpql_usd"] == round(1890.0 / 5, 2)
    brand = next(r for r in p["campaigns"] if r["campaign_id"] == "1")
    assert brand["cohort_contacts_acquired"] == 3
    assert brand["cohort_sqls"] == 2
    assert brand["cohort_sqls_missing_event_timestamp"] == 1
    assert brand["cohort_cpql_usd"] == round(1260.0 / 2, 2)
    assert brand["outcome_status"] == "SQL producer"


def test_11c_counterfactual_reconcile_cohort_detects_a_leaking_bucket():
    c = _cohort(list(_MIXED))
    c["unattributed"]["sqls"] -= 1            # an SQL silently disappears
    problems = svc.reconcile_cohort(c)
    assert any("google_ads.sqls" in p for p in problems)


def test_11d_a_reconciliation_failure_withholds_the_page(monkeypatch):
    real = svc.reconcile_cohort
    monkeypatch.setattr(svc, "reconcile_cohort", lambda c: ["forced"] + real(c))
    p = _page(monkeypatch, contacts=_MIXED)
    assert p["cohort"]["sql_status"] == svc.STATUS_WITHHELD
    assert p["cohort"]["sql_reason"] == "cohort_reconciliation_failed"


def test_11e_a_stale_source_publishes_sqls_as_of_its_watermark_but_withholds_cpql(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED, fresh=False)
    assert p["cohort"]["sql_status"] == svc.STATUS_PUBLISHED
    assert p["cohort"]["metadata"]["as_of"] == "2026-10-03T06:00:00+00:00"
    assert p["summary"]["cohort_cpql_status"] == svc.STATUS_WITHHELD
    assert p["summary"]["cohort_cpql_usd"] is None


def test_11f_an_unreadable_funnel_is_unavailable_never_zero(monkeypatch):
    import db.crm_funnel_repository as funnel_repo
    _patch_page(monkeypatch, contacts=[])
    monkeypatch.setattr(funnel_repo, "fetch_acquisition_cohort_contacts",
                        lambda s, e: {"available": False, "rows": [],
                                      "missing_created_at": None})
    from services.campaign_evidence_service import build_campaign_evidence
    p = build_campaign_evidence("30d", now=NOW)
    assert p["cohort"]["sql_status"] == svc.STATUS_UNAVAILABLE
    assert p["summary"]["cohort_sqls_google_ads"] is None
    assert all(r["cohort_sqls"] is None for r in p["campaigns"])


def test_11g_legacy_fields_are_kept_and_declared_legacy(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    assert p["legacy_sql"]["published_on_this_page"] is False
    assert "summary.confirmed_sqls_total" in p["legacy_sql"]["fields"]
    assert "confirmed_sqls_total" in p["summary"]


# ═════════════════════════════════════════════════════════════════════════════
# §7 — the API metric contract
# ═════════════════════════════════════════════════════════════════════════════

_REQUIRED_META = ("metric_family", "window_basis", "outcome_basis", "dedup_key",
                  "as_of", "source_freshness", "attribution_status", "mapped_count",
                  "unattributed_count", "excluded_non_google_count",
                  "coverage_status", "coverage_notes")


def test_13_every_sql_and_deal_response_carries_the_metric_contract(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED,
              deals=[_deal("D1", contact="a2")], deal_created={"a2": INSIDE})
    md, dmd = p["cohort"]["metadata"], p["cohort"]["deal_metadata"]
    for key in _REQUIRED_META:
        assert key in md, f"SQL metadata lacks {key}"
        assert key in dmd, f"deal metadata lacks {key}"
    assert md["metric_family"] == "acquisition_cohort_outcomes"
    assert md["window_basis"] == "contact_created_at"
    assert md["outcome_basis"] == "latest_canonical_lifecycle_evidence"
    assert md["dedup_key"] == "contact_id"
    assert (md["mapped_count"], md["unattributed_count"],
            md["excluded_non_google_count"]) == (3, 2, 1)
    assert md["coverage_status"] == svc.COVERAGE_EVENT_GAPS
    assert dmd["dedup_key"] == "deal_id"
    assert dmd["window_basis"] == "primary_contact.contact_created_at"
    assert p["summary"]["closed_won_deals_google_ads"] == 1
    assert p["metric_family"] == "acquisition_cohort_outcomes"


def test_13b_the_lifecycle_event_family_is_declared_distinct_and_unpublished(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    lc = p["cohort"]["lifecycle_event_coverage"]
    assert lc["metric_family"] == "lifecycle_stage_events"
    assert lc["window_basis"] == "date_entered_sql"
    assert lc["published_on_this_page"] is False
    assert lc["open_post_boundary_incidents"] == 103


def test_13c_the_unavailable_fallback_has_the_same_contract_shape():
    from services.campaign_evidence_service import unavailable_response
    p = unavailable_response("30d", now=NOW)
    assert p["cohort"]["sql_status"] == "unavailable"
    assert p["summary"]["cohort_sqls_google_ads"] is None
    assert p["summary"]["cohort_cpql_status"] == "unavailable"


def test_13d_the_cohort_reads_no_event_time_coverage_layer():
    """Forbidden by PR-ADS-161A-1's AST guard, re-asserted for this module."""
    src = (_ROOT / "services" / "marketing_outcome_cohort_service.py").read_text()
    for name in ("cpql_publishable", "complete_sql_total", "lifecycle_sql_coverage"):
        assert name not in src


# ═════════════════════════════════════════════════════════════════════════════
# §8 — the audit command
# ═════════════════════════════════════════════════════════════════════════════

def _independent_for(contacts):
    stages = svc._STAGES_IMPLYING_SQL
    inside = [c for c in contacts if svc.in_window(c["created_at"], START_AT, END_BEFORE)]

    def proven(c):
        return (c["sql_entered_direct"] is not None or c["sql_entered_recovered"] is not None
                or (c["lifecycle_stage"] or "").strip().lower() in stages)

    def stage_only(c):
        return (c["sql_entered_direct"] is None and c["sql_entered_recovered"] is None
                and (c["lifecycle_stage"] or "").strip().lower() in stages)

    return {"contacts_acquired": len(inside), "sqls": sum(map(proven, inside)),
            "stage_only_sqls": sum(map(stage_only, inside)),
            "distinct_sql_contact_ids": len({c["contact_id"] for c in inside if proven(c)})}


def test_14_the_audit_passes_a_coherent_page(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    a = audit.Audit()
    out = audit.audit_window(a, window="30d", payload=p,
                             independent=_independent_for(_MIXED), won_deal_ids=[])
    assert a.violations == [], a.violations
    assert out["cohort"]["google_ads_sqls"] == 5


def test_14b_counterfactual_the_audit_fails_when_the_page_drops_an_unattributed_sql(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    p["summary"]["cohort_sqls_unattributed"] -= 1
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED), won_deal_ids=[])
    assert a.exit_code == audit.EXIT_VIOLATION
    assert any("bucket_reconciliation" in v for v in a.violations)


def test_14c_counterfactual_the_audit_fails_when_the_page_disagrees_with_sql(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    independent = _independent_for(_MIXED)
    independent["sqls"] += 1                  # canonical evidence proves one more
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p, independent=independent,
                       won_deal_ids=[])
    assert any("sql_proof" in v for v in a.violations)


def test_14d_lifecycle_gaps_alone_do_not_fail_the_audit(monkeypatch):
    """Required: incomplete event timestamps, correctly disclosed, are NOT a
    violation — every SQL here is undated."""
    contacts = [_contact(f"s{i}", stage="customer") for i in range(4)]
    p = _page(monkeypatch, contacts=contacts)
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(contacts), won_deal_ids=[])
    assert a.exit_code == audit.EXIT_OK, a.violations


def test_14e_counterfactual_undisclosed_undated_sqls_fail_the_audit(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    p["cohort"]["breakdown"]["all_sources"]["sqls_missing_event_timestamp"] = 0
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED), won_deal_ids=[])
    assert any("lifecycle_gaps_disclosed" in v for v in a.violations)


def test_14f_counterfactual_a_cpql_not_drawn_from_cohort_sqls_fails(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    p["summary"]["cohort_cpql_usd"] = p["summary"]["overall_cpql_usd"] or 1.0
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED), won_deal_ids=[])
    assert any("cpql_uses_cohort_sqls" in v for v in a.violations)


def test_14g_counterfactual_the_write_path_check_fails_on_a_connector_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import connectors.hubspot_pull\n", encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text('"""Mentions HubSpot in a docstring only."""\nX = 1\n')
    a = audit.Audit()
    monkey_root = audit._ROOT
    try:
        audit._ROOT = tmp_path
        found = audit.check_no_write_paths(a, paths=(bad, good))
    finally:
        audit._ROOT = monkey_root
    assert list(found["write_markers"]) == ["bad.py"]
    assert a.exit_code == audit.EXIT_VIOLATION


def test_14h_the_audit_covers_every_supported_campaign_evidence_window():
    from analysis.evidence_windows import EVIDENCE_WINDOWS
    assert EVIDENCE_WINDOWS == ("7d", "14d", "30d", "60d", "180d", "all_time")
    src = (_ROOT / "scripts" / "audit_marketing_outcome_cohorts.py").read_text()
    assert "for window in EVIDENCE_WINDOWS:" in src


# ═════════════════════════════════════════════════════════════════════════════
# §9 — the frontend gate, executed in node
# ═════════════════════════════════════════════════════════════════════════════

def _js_function(js: str, name: str) -> str:
    """One top-level function, from its declaration to its own closing brace.

    Top-level functions in app.js close with `}` at column 0. Slicing to the
    next `function` instead would drag in whatever top-level declarations sit
    between two functions. `node` parsing the result is the check that the
    slice is whole — a truncated function is a syntax error, not a silent pass.
    """
    i = js.find(f"\nfunction {name}(")
    assert i != -1, f"function {name} not found"
    j = js.find("\n}\n", i)
    assert j != -1, f"function {name} has no column-0 closing brace"
    return js[i:j + 2]


def _run_gate(cohort, summary=None) -> dict:
    node = shutil.which("node")
    if not node:                                          # pragma: no cover
        pytest.skip("node is unavailable")
    from tests.test_pr_ads_160_sql_coverage_boundary import _js_object_literal
    js = _APP_JS.read_text(encoding="utf-8")
    program = "\n".join([
        "function escapeHtml(s){return String(s);}",
        "function fmtDate(s){return 'DATE(' + s + ')';}",
        f"let _campaignCohort = {json.dumps(cohort)};",
        f"let _campaignSummary = {json.dumps(summary or {})};",
        f"const CAMPAIGN_COHORT_REASONS = {_js_object_literal(js, 'CAMPAIGN_COHORT_REASONS')};",
        _js_function(js, "campaignCohortAsOf"),
        _js_function(js, "campaignCohortReason"),
        _js_function(js, "campaignSqlPublication"),
        _js_function(js, "campaignSqlWithheld"),
        _js_function(js, "campaignCpqlNotPublished"),
        textwrap.dedent("""
            const pub = campaignSqlPublication();
            console.log(JSON.stringify({
              pub, asOf: campaignCohortAsOf(),
              withheld: campaignSqlWithheld(pub),
              cpql: campaignCpqlNotPublished(pub),
            }));
        """),
    ])
    out = subprocess.run([node, "-e", program], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_15_the_gate_reads_the_cohort_not_the_legacy_reconciliation():
    published = _run_gate({"sql_status": "published", "cpql_status": "published",
                           "metadata": {"as_of": "2026-10-03T06:00:00Z"}})
    assert published["pub"]["publish"] is True and published["pub"]["cpql"] is True
    assert published["asOf"] == "DATE(2026-10-03T06:00:00Z)"
    # A missing cohort block is unavailable — never permission. The legacy
    # reconciliation is not consulted at all: the gate has no path to it.
    missing = _run_gate(None)
    assert missing["pub"]["publish"] is False
    assert missing["pub"]["state"] == "unavailable"
    assert "_campaignSqlReconciliation" not in _js_function(
        _APP_JS.read_text(), "campaignSqlPublication")


def test_15b_cpql_never_publishes_without_the_sql_count():
    for status in ("withheld", "unavailable", "anything-else"):
        r = _run_gate({"sql_status": status, "cpql_status": "published",
                       "sql_reason": "data_watermark_unknown"})
        assert r["pub"]["publish"] is False
        assert r["pub"]["cpql"] is False, f"CPQL published over a {status} SQL count"


def test_15c_withheld_and_unpublished_are_words_never_zero():
    for cohort in ({"sql_status": "withheld", "sql_reason": "data_watermark_unknown"},
                   {"sql_status": "unavailable", "sql_reason": "canonical_funnel_unreadable"},
                   {"sql_status": "published", "cpql_status": "withheld",
                    "cpql_reason": "source_not_fresh"},
                   {"sql_status": "published", "cpql_status": "not_applicable",
                    "cpql_reason": "zero_cohort_sqls"}):
        r = _run_gate(cohort)
        for html in (r["withheld"], r["cpql"]):
            assert ">0<" not in html and "$0" not in html and "Infinity" not in html
    zero = _run_gate({"sql_status": "published", "cpql_status": "not_applicable"})
    assert ">N/A<" in zero["cpql"]
    stale = _run_gate({"sql_status": "published", "cpql_status": "withheld",
                       "cpql_reason": "source_not_fresh"})
    assert ">Withheld<" in stale["cpql"] and "stale" in stale["cpql"]


def test_15d_a_missing_timestamp_no_longer_renders_reconciliation_required():
    """The brief's headline symptom. The old gate printed this whenever the
    legacy scope failed to reconcile; the cohort never asks for a timestamp."""
    region = "\n".join(_js_function(_APP_JS.read_text(), fn) for fn in (
        "campaignSqlWithheld", "renderCampaignEvidenceKPIs",
        "renderCampaignEvidenceRow", "renderCampaignDrawer"))
    assert "Reconciliation required" not in region


def test_15e_the_legacy_qualified_columns_are_labelled_as_lead_status():
    js = _APP_JS.read_text(encoding="utf-8")
    sections = _js_function(js, "_appendDrawerEvidenceSections")
    assert sections.count("CAMPAIGN_LEGACY_QUALIFIED_LABEL") == 2
    assert "CAMPAIGN_SQL_SCOPE_SHORT" not in sections, (
        "a legacy lead-status count is labelled as the cohort SQL count")


def test_15f_the_drawer_declares_its_gate_before_using_it():
    """The temporal-dead-zone defect PR-ADS-158's registry recorded."""
    fn = _js_function(_APP_JS.read_text(), "renderCampaignDrawer")
    declared = fn.index("const drawerSqlPub = campaignSqlPublication();")
    first_use = fn.index("drawerSqlPub.")
    assert declared < first_use


def test_15g_the_basis_label_names_the_cohort_and_its_watermark():
    js = _APP_JS.read_text(encoding="utf-8")
    assert 'CAMPAIGN_COHORT_BASIS = "SQLs from contacts created during this period"' in js
    assert "measured as of ${escapeHtml(campaignCohortAsOf())}" in js
    assert "Contacts that entered SQL during this period" not in js


@pytest.mark.parametrize("mutation,expected", [
    # The gate stops reading the cohort block (still stored, never consulted).
    (("let _campaignCohort = null;", "let _campaignCohort = null; let _unused = null;"),
     None),
    (("  const c = _campaignCohort;\n  if (!c || !c.sql_status) {",
      "  const c = null;\n  if (!c || !c.sql_status) {"),
     "does not read the cohort block"),
    (("_campaignCohort = data.cohort || null;", "_campaignCohort = null;"),
     "not carried from /api/campaigns into state"),
    (('const label = pub.state === "unavailable" ? "Unavailable" : "Withheld";',
      'const label = "";'),
     "no Withheld / Unavailable rendering"),
    (('const CAMPAIGN_SQL_SCOPE_LABEL = "Cohort SQLs";',
      'const CAMPAIGN_SQL_SCOPE_LABEL = "SQLs";'),
     "not named 'Cohort SQLs'"),
])
def test_16_the_retargeted_pr_ads_157_gate_still_goes_red(tmp_path, monkeypatch,
                                                         mutation, expected):
    """PR-ADS-161B retargeted three of PR-ADS-157's certification checks at the
    cohort contract. Each must still fail when the property it guards breaks —
    otherwise retargeting would have been weakening. The first case is the
    positive control: an irrelevant edit leaves the gate green."""
    import scripts.audit_campaign_evidence_certification as cert
    old, new = mutation
    js = _APP_JS.read_text(encoding="utf-8")
    assert old in js, f"mutation anchor missing: {old!r}"
    mutated = tmp_path / "app.js"
    mutated.write_text(js.replace(old, new, 1), encoding="utf-8")
    monkeypatch.setattr(cert, "_APP_JS", mutated)
    findings = cert.Findings()
    cert.check_frontend_gates(findings)
    if expected is None:
        assert findings.violations == [], findings.violations
    else:
        assert any(expected in v for v in findings.violations), findings.violations


# ═════════════════════════════════════════════════════════════════════════════
# §10 — end to end against a real PostgreSQL
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def seeded(pg, monkeypatch):  # noqa: F811
    """A real schema with every input Campaign Evidence reads, seeded through
    the production writers. Times are relative to the real clock, because the
    page resolves its window from today."""
    from db import writers
    import db.deal_ledger_repository as ledger
    from tests.test_pr_ads_153e_a_pg_integration import _ledger_row

    now = datetime.now(timezone.utc)
    inside = now - timedelta(days=3)
    before = now - timedelta(days=60)
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete", last_incremental_at=now,
        last_modified_watermark=now, last_status="success",
        last_sync_mode="incremental", last_incremental_status="success",
        last_successful_incremental_at=now, last_error=None)
    writers.upsert_hubspot_contact_funnel([
        {"contact_id": "dated", "lifecycle_stage": "salesqualifiedlead",
         "created_at": inside, "last_modified_at": inside, "date_entered_sql": inside,
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "Brand - UK"},
        {"contact_id": "undated", "lifecycle_stage": "customer",
         "created_at": inside, "last_modified_at": inside,
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "Brand - UK"},
        {"contact_id": "recovered", "lifecycle_stage": "lead",
         "created_at": inside, "last_modified_at": inside,
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "Gulf"},
        {"contact_id": "plain_lead", "lifecycle_stage": "lead",
         "created_at": inside, "last_modified_at": inside,
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "Gulf"},
        {"contact_id": "unmapped", "lifecycle_stage": "opportunity",
         "created_at": inside, "last_modified_at": inside,
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "Mystery"},
        {"contact_id": "organic", "lifecycle_stage": "customer",
         "created_at": inside, "last_modified_at": inside,
         "hs_analytics_source": "ORGANIC_SEARCH"},
        {"contact_id": "old_entered_recently", "lifecycle_stage": "salesqualifiedlead",
         "created_at": before, "last_modified_at": inside, "date_entered_sql": inside,
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "Brand - UK"},
    ])
    writers.upsert_lifecycle_stage_history(
        [{"contact_id": "recovered", "funnel_event": "sql", "entered_at": inside,
          "hubspot_value": "salesqualifiedlead"}], run_id="t161b")
    spend_day = (now - timedelta(days=2)).date()
    writers.upsert_campaign_daily_spend([
        {"customer_id": "123", "currency_code": "GBP", "campaign_id": "1",
         "campaign_name": "Brand - UK", "spend_date": spend_day, "cost_micros": 900_000_000},
        {"customer_id": "123", "currency_code": "GBP", "campaign_id": "2",
         "campaign_name": "Gulf", "spend_date": spend_day, "cost_micros": 300_000_000},
    ])
    writers.upsert_fx_rates([{"rate_date": spend_day, "base_currency": "GBP",
                              "quote_currency": "USD", "rate": 1.25,
                              "provider": "test", "source_version": "t"}])
    # One won deal associated with THREE contacts — counted once.
    ledger.upsert_deal(_ledger_row("D-1", primary_contact_id="dated",
                                   campaign="Brand - UK", association_count=3),
                       associations=[{"contact_id": c} for c in ("dated", "undated", "unmapped")])
    return {"inside": inside, "before": before}


@_needs_pg
def test_20_pg_the_page_counts_proven_sqls_from_the_real_funnel(seeded):
    from services.campaign_evidence_service import build_campaign_evidence
    p = build_campaign_evidence("30d")
    s = p["summary"]
    assert p["cohort"]["sql_status"] == "published", p["cohort"]
    # dated + undated (Brand), recovered (Gulf), unmapped → 4 Google Ads SQLs.
    assert s["cohort_sqls_google_ads"] == 4
    assert s["cohort_sqls_mapped"] == 3
    assert s["cohort_sqls_unattributed"] == 1
    assert s["cohort_sqls_excluded_non_google"] == 1
    assert s["cohort_sqls_missing_event_timestamp"] == 2      # undated, unmapped
    assert p["cohort"]["breakdown"]["proof_counts"] == {
        svc.PROOF_DIRECT: 1, svc.PROOF_RECOVERED: 1, svc.PROOF_STAGE: 3}
    assert s["cohort_cpql_status"] == "published"
    assert s["cohort_cpql_usd"] == round(1500.0 / 4, 2)       # (900+300) GBP × 1.25
    assert s["closed_won_deals_google_ads"] == 1
    assert p["cohort"]["reconciliation"]["status"] == "reconciled"


@_needs_pg
def test_21_pg_the_audit_reconciles_every_supported_window(seeded):
    a, report = audit.run()
    assert a.violations == [], a.violations
    assert a.unavailable == [], a.unavailable
    assert a.exit_code == audit.EXIT_OK
    assert [w["window"] for w in report["windows"]] == [
        "7d", "14d", "30d", "60d", "180d", "all_time"]
    assert report["external_writes_performed"] is False
    assert report["database_writes_performed"] is False
    split = report["population_split"]
    assert split["reached_sql_by_current_stage"] == 5   # dated, undated, unmapped, organic, old
    assert split["sql_dated_but_stage_now_below_sql"] == 1   # recovered (stage: lead)


@_needs_pg
def test_22_pg_counterfactual_the_audit_goes_red_when_the_page_drops_an_sql(seeded, monkeypatch):
    import services.campaign_evidence_service as ces
    real = ces._cohort_summary_fields

    def leaky(*args, **kwargs):
        out = real(*args, **kwargs)
        if out["cohort_sqls_unattributed"]:
            out["cohort_sqls_unattributed"] -= 1
        return out

    monkeypatch.setattr(ces, "_cohort_summary_fields", leaky)
    a, _ = audit.run()
    assert a.exit_code == audit.EXIT_VIOLATION


@_needs_pg
def test_23_pg_created_before_the_window_is_a_lifecycle_event_not_a_cohort_member(seeded):
    """Required case 4, through BOTH real readers."""
    import db.crm_funnel_repository as repo
    from services.campaign_evidence_service import _window_bounds
    start, end, _ = _window_bounds("30d", None)
    start_at, end_before = svc.window_instants(start, end)
    cohort_ids = {r["contact_id"] for r in
                  repo.fetch_acquisition_cohort_contacts(start_at, end_before)["rows"]}
    assert "old_entered_recently" not in cohort_ids
    assert "dated" in cohort_ids
    events = repo.fetch_funnel_contacts(start, end)
    assert "old_entered_recently" in {r["contact_id"] for r in events["rows"]}


@_needs_pg
def test_24_pg_nothing_is_written_and_no_date_is_produced(seeded):
    """Building the page must leave every stage-entry date exactly as it was."""
    from services.campaign_evidence_service import build_campaign_evidence
    import db.connection as connection

    def snapshot():
        with connection.get_conn() as c, c.cursor() as cur:
            cur.execute("SELECT contact_id, date_entered_sql FROM hubspot_contact_funnel "
                        "ORDER BY contact_id")
            funnel = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM hubspot_lifecycle_stage_history")
            history = cur.fetchone()[0]
        return funnel, history

    before = snapshot()
    build_campaign_evidence("all_time")
    assert snapshot() == before
    funnel = dict(before[0])
    assert funnel["undated"] is None and funnel["unmapped"] is None


@_needs_pg
def test_12_pg_the_coverage_gate_still_reports_open_post_boundary_incidents(seeded):
    """Required case 12: the lifecycle gate is neither weakened nor bypassed.
    A real open incident goes red in the real gate, beside a published cohort."""
    from db import writers
    from scripts import audit_sql_coverage_gate as gate
    from services.campaign_evidence_service import build_campaign_evidence

    writers.record_post_boundary_incidents([{
        "contact_id": "undated", "boundary_id": "boundary_test",
        "reason": "post_boundary_no_direct_sql_date", "lifecycle_stage": "customer",
        "contact_created_at": seeded["inside"], "history_checked": True,
        "history_state": "no_sql_transition"}], run_id="t161b")
    g, report = gate.run()
    assert report["post_boundary_gaps"] == {"available": True, "open": 1}
    assert any(v.startswith("no_open_post_boundary_gaps") for v in g.violations)
    assert g.exit_code == gate.EXIT_VIOLATION

    p = build_campaign_evidence("30d")
    assert p["cohort"]["sql_status"] == "published"
    assert p["cohort"]["lifecycle_event_coverage"]["open_post_boundary_incidents"] == 1
