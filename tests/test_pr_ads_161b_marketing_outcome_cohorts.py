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


def _sync_row(**overrides):
    """A contact-funnel sync-state row in the shape
    `fetch_contact_funnel_sync_state` returns — healthy unless overridden."""
    row = {"bootstrap_status": "complete", "last_sync_mode": "incremental",
           "last_incremental_status": "success",
           "last_successful_incremental_at": NOW - timedelta(hours=2),
           "last_incremental_at": NOW - timedelta(hours=2)}
    row.update(overrides)
    return row


def _assess(row=None, *, available=True):
    """Freshness from the REAL assessor — never a hand-built verdict, whose
    shape the real one may not share (PR-ADS-161B review, MAJOR 1)."""
    from analysis import sql_coverage_freshness as freshness_mod
    state = {"available": available, "row": row if row is not None else _sync_row()}
    return freshness_mod.assess(state, now=NOW)


_FRESH = _assess()
_PUBLISHED = (svc.STATUS_PUBLISHED, None)


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
    assert svc.coverage_status(c, publication=_PUBLISHED) == svc.COVERAGE_EVENT_GAPS
    notes = svc.coverage_notes(c, missing_created_at=0, publication=_PUBLISHED)
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
    notes = svc.coverage_notes(c, missing_created_at=1, publication=_PUBLISHED)
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


@pytest.mark.parametrize("source", [None, "", "   ", "SOMETHING_NEW"])
def test_06d_a_blank_or_unrecognised_source_is_excluded_as_unproven_not_as_another_channel(source):
    """PR-ADS-161B review: "not proven Google Ads" is not "proven non-Google".
    Still excluded — nothing proves Google Ads bought it — under its own reason."""
    c = _cohort([_contact("c1", stage="customer", source=source)])
    assert c["google_ads"]["sqls"] == 0
    assert c["excluded_non_google"]["by_reason"] == {svc.REASON_SOURCE_UNCLASSIFIED: 1}


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
# §3 — closed-won deals are NOT published (PR-ADS-161B review round 3)
# ═════════════════════════════════════════════════════════════════════════════
# The first two rounds published closed-won deals placed through the ledger's
# display-only primary contact, without the ledger's own sync coverage, under
# an attribution status derived from CONTACTS. They could not be certified in
# this PR, so they are removed: no ledger read, no field, no card, no column —
# and an explicit declaration that they are not published.

def test_08_the_cohort_reads_no_deal_ledger(monkeypatch):
    import db.deal_ledger_repository as ledger_repo

    def _forbidden(*_a, **_k):
        raise AssertionError("the deal ledger was read by the cohort page")

    monkeypatch.setattr(ledger_repo, "fetch_won_deals", _forbidden)
    p = _page(monkeypatch, contacts=_MIXED)
    assert p["cohort"]["sql_status"] == svc.STATUS_PUBLISHED
    for name in ("build_deal_outcomes", "deal_bucket", "deal_metric_metadata"):
        assert not hasattr(svc, name), name


def test_08b_no_closed_won_field_on_any_row_or_the_summary(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    assert not [k for k in p["summary"] if "closed_won" in k]
    for r in p["campaigns"]:
        assert not [k for k in r if "closed_won" in k], r["campaign_key"]
    assert "deals" not in p["cohort"] and "deal_metadata" not in p["cohort"]


def test_08c_closed_won_is_declared_unpublished_and_never_customers(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    decl = p["cohort"]["closed_won_deals"]
    assert decl["published_on_this_page"] is False
    assert decl["reason"] == "deferred_until_certified"
    assert "never unique customers" in decl["note"]
    # The same declaration on the fallback response.
    from services.campaign_evidence_service import unavailable_response
    assert unavailable_response("30d")["cohort"]["closed_won_deals"] == decl


def test_08d_no_campaign_evidence_surface_renders_closed_won():
    js = _APP_JS.read_text(encoding="utf-8")
    for fn in ("renderCampaignEvidenceKPIs", "renderCampaignDecisionTable",
               "renderCampaignEvidenceRow", "renderCampaignDrawer"):
        src = _js_function(js, fn)
        assert "closed_won" not in src and "Closed-won" not in src, fn
    assert ">Unique customers<" not in js and ">Unique Customers<" not in js


def test_08e_counterfactual_the_audit_goes_red_if_closed_won_is_published(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    p["summary"]["closed_won_deals_google_ads"] = 3
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p, independent=_independent_for(_MIXED))
    assert any("closed_won_not_published" in v for v in a.violations), a.violations
    p = _page(monkeypatch, contacts=_MIXED)
    p["cohort"]["closed_won_deals"]["published_on_this_page"] = True
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p, independent=_independent_for(_MIXED))
    assert any("closed_won_not_published" in v for v in a.violations), a.violations


# ═════════════════════════════════════════════════════════════════════════════
# §4 — CPQL (required case 9)
# ═════════════════════════════════════════════════════════════════════════════

def _cpql(**kw):
    base = dict(publication=_PUBLISHED, spend_available=True, spend_usd=1200.0,
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
    # An unreadable funnel: CPQL inherits the SQL verdict and its reason.
    unreadable = svc.sql_publication(cohort_available=False,
                                     reconciliation_problems=[], freshness=_FRESH)
    assert _cpql(publication=unreadable)[:2] == (
        svc.STATUS_UNAVAILABLE, svc.SQL_REASON_FUNNEL_UNREADABLE)


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


def test_10e_this_pr_is_not_a_new_reader_of_the_boundary_bound(tmp_path, monkeypatch):
    """`audit_sql_coverage_gate` fails on ANY module outside its allow-list
    that names the bound — it does not, and should not, tell a reader from a
    module that forbids it. This PR's audit forbids it, so it takes the name
    from the gate rather than joining the allow-list.

    Found by CI's PostgreSQL step (PR-ADS-160 test_29) after the first commit
    spelled the column in FORBIDDEN_DATE_SOURCES. This case runs the same
    file scan without a database, so it cannot hide behind a PG skip."""
    from scripts import audit_sql_coverage_gate as gate_mod

    # The repository as committed: no new reader, no blending.
    g = gate_mod.Gate()
    result = gate_mod.check_bound_is_not_a_date(g)
    assert result["offenders"] == [], result
    assert result["blenders"] == [], result
    assert g.violations == []
    # The allow-list was not widened to get there.
    assert not any("marketing_outcome" in p or "campaign_evidence" in p
                   for p in gate_mod._BOUND_READERS)

    # Counterfactual: the pre-fix spelling of this file IS a new reader.
    pre_fix = (_ROOT / "scripts" / "audit_marketing_outcome_cohorts.py").read_text(
        encoding="utf-8").replace("    BOUND_COLUMN, ", '    "known_reached_sql_by", ', 1)
    assert '"known_reached_sql_by"' in pre_fix, "mutation did not apply"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "audit_marketing_outcome_cohorts.py").write_text(
        pre_fix, encoding="utf-8")
    monkeypatch.setattr(gate_mod, "_ROOT", tmp_path)
    g2 = gate_mod.Gate()
    red = gate_mod.check_bound_is_not_a_date(g2)
    assert red["offenders"] == ["scripts/audit_marketing_outcome_cohorts.py"]
    assert g2.exit_code == gate_mod.EXIT_VIOLATION


def test_10f_the_contamination_check_still_forbids_the_bound():
    """Sourcing the name from the gate must not drop it from the list: the
    bound stays forbidden in every classification function (test_10b shows a
    function reading it going red)."""
    from scripts import audit_sql_coverage_gate as gate_mod

    assert gate_mod.BOUND_COLUMN == "known_reached_sql_by"
    assert gate_mod.BOUND_COLUMN in audit.FORBIDDEN_DATE_SOURCES


# ═════════════════════════════════════════════════════════════════════════════
# §6 — reconciliation through the real page builder (required case 11)
# ═════════════════════════════════════════════════════════════════════════════

#: Stale from the real assessor: complete bootstrap, last success 48h ago.
_STALE = _assess(_sync_row(last_successful_incremental_at=NOW - timedelta(hours=48),
                           last_incremental_at=NOW - timedelta(hours=48)))


def _patch_page(monkeypatch, *, contacts, fresh=True, freshness=None,
                spend_rows=None, mappings=(), lead_rows=(), identity_available=True):
    import db.crm_funnel_repository as funnel_repo
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
        "available": identity_available, "mappings": list(mappings)})
    monkeypatch.setattr(funnel_repo, "fetch_acquisition_cohort_contacts",
                        lambda s, e: {"available": True, "rows": list(contacts),
                                      "missing_created_at": 0})
    verdict = freshness if freshness is not None else (_FRESH if fresh else _STALE)
    monkeypatch.setattr(svc, "read_freshness", lambda now=None: verdict)
    monkeypatch.setattr(svc, "lifecycle_event_disclosure",
                        lambda: svc.lifecycle_disclosure_skeleton(
                            available=True, reached_sql_by_current_stage=1531,
                            exact_direct_timestamp=863, recovered_timestamp=0,
                            missing_exact_timestamp=668,
                            open_post_boundary_incidents=103))


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
    assert p["cohort"]["metadata"]["as_of"] == (NOW - timedelta(hours=48)).isoformat()
    assert p["summary"]["cohort_cpql_status"] == svc.STATUS_WITHHELD
    assert p["summary"]["cohort_cpql_reason"] == svc.CPQL_REASON_SOURCE_NOT_FRESH
    assert p["summary"]["cohort_cpql_usd"] is None


#: Every freshness verdict the REAL assessor emits that does not prove the
#: population complete as of a known watermark — each with the sync state that
#: produces it.
#: ``None`` = the sync-state table was read and holds no row.
_NOT_PROVEN = [
    ("source_sync_state_missing", None),
    ("source_bootstrap_incomplete", _sync_row(bootstrap_status="running")),
    ("source_incremental_provenance_missing", _sync_row(
        last_sync_mode=None, last_incremental_status=None,
        last_successful_incremental_at=None)),
    ("source_last_incremental_failed", _sync_row(last_incremental_status="partial")),
    ("source_no_successful_incremental", _sync_row(last_successful_incremental_at=None)),
]


def _assess_state(row):
    from analysis import sql_coverage_freshness as freshness_mod
    return freshness_mod.assess({"available": True, "row": row}, now=NOW)


@pytest.mark.parametrize("reason,row", _NOT_PROVEN, ids=[r for r, _ in _NOT_PROVEN])
def test_11h_a_population_not_proven_complete_withholds_sqls_and_cpql_with_its_own_reason(
        monkeypatch, reason, row):
    """PR-ADS-161B review, MAJOR 1. The assessor returns fresh=False — not None
    — for a funnel never ingested, a bootstrap still arriving, a failed
    incremental. The first commit withheld only on None, so all of these
    PUBLISHED a cohort count over a partial population, and the CPQL beside it
    blamed staleness. Driven by the real `assess`; the reason is passed through."""
    verdict = _assess_state(row)
    assert verdict["reason"] == reason, verdict
    assert verdict["fresh"] is False            # a claim about the pipeline, not None
    p = _page(monkeypatch, contacts=_MIXED, freshness=verdict)
    c = p["cohort"]
    assert c["sql_status"] == svc.STATUS_WITHHELD
    assert c["sql_reason"] == reason
    # CPQL inherits the verdict AND its reason — never "stale" for a bootstrap.
    assert p["summary"]["cohort_cpql_status"] == svc.STATUS_WITHHELD
    assert p["summary"]["cohort_cpql_reason"] == reason
    assert p["summary"]["cohort_cpql_usd"] is None
    # EVERY row — mapped, unmapped label, no-spend — inherits the page verdict
    # and its reason. Row-only refusals never replace it (Copilot round 3: an
    # unmatched row used to say `unmapped_label_has_no_campaign_spend` here).
    assert {r["mapping_status"] for r in p["campaigns"]} >= {"mapped", "unmatched"}
    for r in p["campaigns"]:
        assert r["cohort_sql_status"] == svc.STATUS_WITHHELD
        assert r["cohort_sql_reason"] == reason
        assert r["cohort_sqls"] is None, r["campaign_key"]
        assert r["cohort_sqls_missing_event_timestamp"] is None
        assert r["cohort_cpql_usd"] is None
        assert (r["cohort_cpql_status"], r["cohort_cpql_reason"]) == (svc.STATUS_WITHHELD, reason)
        # The outcome status is never drawn from the withheld count.
        assert r["outcome_status"] not in ("SQL producer", "Spend without SQL proof",
                                           "No outcome evidence")
    # Nothing SQL-derived anywhere: not the parts, not the breakdown, not a note.
    assert audit.withheld_exposures(p) == []
    assert c["breakdown"] is None
    assert c["metadata"]["coverage_status"] == svc.COVERAGE_NOT_PROVEN
    assert any("no SQL count, SQL breakdown or CPQL is published" in n
               for n in c["metadata"]["coverage_notes"])
    assert not any("cohort SQL contact(s)" in n for n in c["metadata"]["coverage_notes"])
    # The funnel-wide reached-SQL counts are read over the same unproven funnel:
    # withheld with the cohort (round 3, truth auditor MAJOR). The incident count
    # is an integrity fact and is never suppressed.
    lc = c["lifecycle_event_coverage"]
    for k in svc.LIFECYCLE_DISCLOSURE_COUNT_FIELDS:
        assert lc[k] is None, k
    assert lc["counts_withheld"] is True and lc["counts_withheld_reason"] == reason
    assert lc["open_post_boundary_incidents"] == 103


def test_11i_positive_control_fresh_and_stale_are_the_only_publishing_verdicts():
    """Without this, test_11h would pass for a gate that withheld everything."""
    for verdict in (_FRESH, _STALE):
        assert svc.sql_publication(cohort_available=True, reconciliation_problems=[],
                                   freshness=verdict) == _PUBLISHED, verdict["reason"]
    from analysis import sql_coverage_freshness as freshness_mod
    published = {r for r in freshness_mod.FRESHNESS_REASONS
                  if r in svc.PUBLISHABLE_FRESHNESS_REASONS}
    assert published == {freshness_mod.FRESH, freshness_mod.STALE}
    # Unknown (could not read) is withheld under its own, different reason.
    unknown = _assess(available=False)
    assert unknown["fresh"] is None
    assert svc.sql_publication(cohort_available=True, reconciliation_problems=[],
                               freshness=unknown) == (svc.STATUS_WITHHELD,
                                                      svc.SQL_REASON_WATERMARK_UNKNOWN)


def test_11j_counterfactual_the_first_commits_none_only_gate_would_have_published(monkeypatch):
    """The pre-fix rule, executed: it would have published a running bootstrap."""
    verdict = _assess(_sync_row(bootstrap_status="running"))
    pre_fix_withholds = verdict.get("fresh") is None
    assert not pre_fix_withholds, "pre-fix rule would already withhold; test proves nothing"
    assert svc.sql_publication(cohort_available=True, reconciliation_problems=[],
                               freshness=verdict)[0] == svc.STATUS_WITHHELD


@pytest.mark.parametrize("spend_usd", [0.0, 0])
def test_11k_zero_window_spend_is_never_a_zero_dollar_cpql(monkeypatch, spend_usd):
    """PR-ADS-161B review, MAJOR 2. `fetch_canonical_campaign_spend` reports
    0.0 for a window with no spend rows; 0 / N would have published $0.00 — a
    free SQL. Page level AND row level."""
    assert _cpql(spend_usd=spend_usd) == (svc.STATUS_NOT_APPLICABLE,
                                          svc.CPQL_REASON_ZERO_SPEND, None)
    p = _page(monkeypatch, contacts=_MIXED, spend_rows=[
        {"campaign_id": "1", "campaign_name": "Brand - UK", "spend": 0.0,
         "spend_usd": 0.0, "fx_complete": True}])
    assert p["cohort"]["sql_status"] == svc.STATUS_PUBLISHED      # SQLs still publish
    assert p["summary"]["cohort_sqls_google_ads"] == 5
    assert p["summary"]["cohort_cpql_usd"] is None
    assert p["summary"]["cohort_cpql_status"] == svc.STATUS_NOT_APPLICABLE
    assert p["summary"]["cohort_cpql_reason"] == svc.CPQL_REASON_ZERO_SPEND
    brand = next(r for r in p["campaigns"] if r["campaign_id"] == "1")
    assert brand["cohort_sqls"] == 2
    assert brand["cohort_cpql_usd"] is None
    assert brand["cohort_cpql_reason"] == svc.CPQL_REASON_ZERO_SPEND


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
# §6b — ONE publication verdict on every surface, every state (round 3)
# ═════════════════════════════════════════════════════════════════════════════
# Summary SQL, row SQL, page CPQL, row CPQL, row outcome, the cohort block, and
# the campaign-detail endpoint (the drawer) — built by the REAL page builder and
# the REAL `_build_campaign_detail` — must state the same verdict for the same
# evidence. Row-only refusals may narrow a row's CPQL; nothing may widen it.

_NO_SQL_CONTACTS = [_contact("z1", stage="lead"), _contact("z2", stage="lead", label="Gulf")]


def _scenario(name, monkeypatch):
    """(patch kwargs, expected (sql_status, sql_reason), expected page CPQL
    (status, reason)) for one evidence state."""
    import db.crm_funnel_repository as funnel_repo
    kw = {"contacts": _MIXED}
    if name == "fresh":
        exp = ("published", None), ("published", None)
    elif name == "stale":
        kw["fresh"] = False
        exp = ("published", None), ("withheld", "source_not_fresh")
    elif name in ("missing_sync_state", "incomplete_bootstrap", "failed_incremental"):
        row, reason = {
            "missing_sync_state": (None, "source_sync_state_missing"),
            "incomplete_bootstrap": (_sync_row(bootstrap_status="running"),
                                     "source_bootstrap_incomplete"),
            "failed_incremental": (_sync_row(last_incremental_status="failed"),
                                   "source_last_incremental_failed"),
        }[name]
        from analysis import sql_coverage_freshness as freshness_mod
        kw["freshness"] = freshness_mod.assess({"available": True, "row": row}, now=NOW)
        exp = ("withheld", reason), ("withheld", reason)
    elif name == "unknown_watermark":
        kw["freshness"] = _assess(available=False)
        exp = ("withheld", "data_watermark_unknown"), ("withheld", "data_watermark_unknown")
    elif name == "reconciliation_failure":
        real = svc.reconcile_cohort
        monkeypatch.setattr(svc, "reconcile_cohort", lambda c: ["forced"] + real(c))
        exp = (("withheld", "cohort_reconciliation_failed"),
               ("withheld", "cohort_reconciliation_failed"))
    elif name == "unavailable_funnel":
        kw["contacts"] = []
        exp = (("unavailable", "canonical_funnel_unreadable"),
               ("unavailable", "canonical_funnel_unreadable"))
    elif name == "missing_campaign_identity":
        kw["identity_available"] = False
        exp = ("published", None), ("published", None)
    elif name == "zero_spend":
        kw["spend_rows"] = [{"campaign_id": "1", "campaign_name": "Brand - UK",
                             "spend": 0.0, "spend_usd": 0.0, "fx_complete": True}]
        exp = ("published", None), ("not_applicable", "zero_window_spend")
    elif name == "zero_sqls":
        kw["contacts"] = _NO_SQL_CONTACTS
        exp = ("published", None), ("not_applicable", "zero_cohort_sqls")
    else:                                                  # pragma: no cover
        raise AssertionError(name)
    _patch_page(monkeypatch, **kw)
    if name == "unavailable_funnel":
        monkeypatch.setattr(funnel_repo, "fetch_acquisition_cohort_contacts",
                            lambda s, e: {"available": False, "rows": [],
                                          "missing_created_at": None})
    return exp


_SCENARIOS = ["fresh", "stale", "missing_sync_state", "incomplete_bootstrap",
              "failed_incremental", "unknown_watermark", "reconciliation_failure",
              "unavailable_funnel", "missing_campaign_identity", "zero_spend", "zero_sqls"]


def _detail(monkeypatch, campaign_key):
    """The REAL /api/campaign-detail builder, with only its two unrelated
    previews and the drawer's lead-detail read stubbed."""
    import api.server as server
    import db.revenue_repository as rev_repo
    monkeypatch.setattr(server, "_campaign_keyword_preview",
                        lambda w, k: {"available": False, "rows": []})
    monkeypatch.setattr(server, "_campaign_flagged_preview",
                        lambda w, k: {"available": False, "rows": []})
    monkeypatch.setattr(rev_repo, "fetch_campaign_lead_detail",
                        lambda s, e: {"available": True, "rows": []})
    return server._build_campaign_detail("ignored", 30, window_key="all_time",
                                         campaign_key=campaign_key)


@pytest.mark.parametrize("name", _SCENARIOS)
def test_17_one_publication_verdict_on_every_surface(monkeypatch, name):
    from services.campaign_evidence_service import (COHORT_ROW_FIELDS,
                                                    build_campaign_evidence)
    (sql_exp, cpql_exp) = _scenario(name, monkeypatch)
    p = build_campaign_evidence("all_time", now=NOW)
    c, s = p["cohort"], p["summary"]
    verdict = (c["sql_status"], c["sql_reason"])
    assert verdict == sql_exp, (name, verdict)
    assert (c["cpql_status"], c["cpql_reason"]) == cpql_exp, name
    # The summary states the same verdict and the same CPQL.
    assert (s["cohort_sql_status"], s["cohort_sql_reason"]) == verdict
    assert (s["cohort_cpql_status"], s["cohort_cpql_reason"]) == cpql_exp
    published = verdict[0] == "published"
    for r in p["campaigns"]:
        # Every row carries the page verdict, verbatim.
        assert (r["cohort_sql_status"], r["cohort_sql_reason"]) == verdict, r["campaign_key"]
        if not published:
            # Withheld: no count, and the CPQL inherits the PAGE verdict exactly.
            assert r["cohort_sqls"] is None
            assert (r["cohort_cpql_status"], r["cohort_cpql_reason"]) == verdict
            assert r["outcome_status"] not in ("SQL producer", "Spend without SQL proof",
                                               "No outcome evidence")
        # A row can narrow, never widen: no published row CPQL over a page that
        # does not publish CPQL for a page-level reason.
        if r["cohort_cpql_status"] == "published":
            assert published and cpql_exp[0] == "published", (name, r)
    if not published:
        assert audit.withheld_exposures(p) == [], name
    if name == "missing_campaign_identity":
        mapped = [r for r in p["campaigns"] if r["mapping_status"] == "mapped" and r["spend_usd"]]
        assert mapped and all(r["cohort_cpql_reason"] == "campaign_attribution_unavailable"
                              for r in mapped)

    # The detail endpoint: same verdict, same row fields, never legacy SQL.
    target = next((r for r in p["campaigns"] if r["mapping_status"] == "mapped"), None)
    d = _detail(monkeypatch, target["campaign_key"] if target else "1")
    assert (d["cohort"]["sql_status"], d["cohort"]["sql_reason"]) == verdict, name
    assert (d["cohort"]["cpql_status"], d["cohort"]["cpql_reason"]) == cpql_exp, name
    if target is not None:
        card = d["campaign"]
        assert card is not None, name
        for k in COHORT_ROW_FIELDS:
            assert card[k] == target[k], (name, k, card[k], target[k])
        assert "confirmed_sqls" not in card and "cpql_usd" not in card
        assert card["legacy_lead_status"]["published_as_sql"] is False


def test_17b_counterfactual_a_row_refusal_evaluated_first_would_contradict_the_page(
        monkeypatch):
    """Copilot round 3: row-only refusals used to run BEFORE the page verdict,
    so during an incomplete bootstrap an unmatched row said
    `unmapped_label_has_no_campaign_spend`. Asserts the shipped behaviour on
    the unmatched rows, and pins the ordering in source; test_15n-style
    mutation of the order is covered by test_17's per-row reason equality."""
    from services import campaign_evidence_service as ces
    _scenario("incomplete_bootstrap", monkeypatch)
    p = ces.build_campaign_evidence("all_time", now=NOW)
    un = [r for r in p["campaigns"] if r["mapping_status"] == "unmatched"]
    assert un and all(r["cohort_cpql_reason"] == "source_bootstrap_incomplete" for r in un)
    src = Path(ces.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _cohort_row_fields"):src.index("def _publication(")]
    assert body.index("if not published:") < body.index('elif kind == "unmatched":'), \
        "the page verdict must be evaluated before every row-only refusal"


def test_17c_the_fallback_response_has_exactly_the_live_shape(monkeypatch):
    """Copilot round 3: the fallback set `cohort.metadata` to null and omitted
    blocks the live contract always carries. Key-for-key equal now, every
    unknown value null — never 0."""
    from services.campaign_evidence_service import unavailable_response
    live = _page(monkeypatch, contacts=_MIXED)
    down = unavailable_response("30d", now=NOW)

    def paths(d, prefix=""):
        out = set()
        for k, v in d.items():
            out.add(prefix + k)
            # `cohort.breakdown` is documented as null unless published, and
            # by_label keys are data, not schema.
            if isinstance(v, dict) and prefix + k != "cohort.breakdown":
                out |= paths(v, prefix + k + ".")
        return out

    live_paths, down_paths = paths(live), paths(down)
    assert live_paths - down_paths == set(), sorted(live_paths - down_paths)
    # The only extra key the fallback may carry is the outage flag itself.
    assert down_paths - live_paths <= {"db_unavailable"}, sorted(down_paths - live_paths)
    for k in ("metric_family", "cohort", "legacy_sql", "summary", "campaigns"):
        assert k in down, k
    assert down["metric_family"] == "acquisition_cohort_outcomes"
    assert down["legacy_sql"]["published_on_this_page"] is False
    c = down["cohort"]
    assert (c["sql_status"], c["sql_reason"]) == ("unavailable", "request_failed")
    assert (c["cpql_status"], c["cpql_reason"]) == ("unavailable", "request_failed")
    assert c["metadata"]["metric_family"] == "acquisition_cohort_outcomes"
    for k in ("mapped_count", "unattributed_count", "excluded_non_google_count", "as_of"):
        assert c["metadata"][k] is None, k
    assert all(v is None for k, v in c["reconciliation"].items()
               if k not in ("status", "identities"))
    s = down["summary"]
    assert all(s[k] is None for k in s if k.startswith("cohort_sqls"))
    assert s["cohort_cpql_usd"] is None
    assert audit.withheld_exposures(down) == []


def test_17d_the_db_down_response_states_one_verdict(monkeypatch):
    """The early db-unavailable return used to pair the real verdict with an
    empty summary saying `request_failed`."""
    import db.crm_funnel_repository as funnel_repo
    import db.revenue_repository as rev_repo
    _patch_page(monkeypatch, contacts=[])
    monkeypatch.setattr(rev_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *a, **k: {"available": False, "rows": []})
    monkeypatch.setattr(rev_repo, "fetch_lead_quality",
                        lambda s, e: {"available": False, "rows": []})
    monkeypatch.setattr(funnel_repo, "fetch_acquisition_cohort_contacts",
                        lambda s, e: {"available": False, "rows": [], "missing_created_at": None})
    from services.campaign_evidence_service import build_campaign_evidence
    p = build_campaign_evidence("30d", now=NOW)
    assert p["db_unavailable"] is True
    assert p["metric_family"] == "acquisition_cohort_outcomes"
    assert (p["summary"]["cohort_sql_status"], p["summary"]["cohort_sql_reason"]) == \
        (p["cohort"]["sql_status"], p["cohort"]["sql_reason"]) == \
        ("unavailable", "canonical_funnel_unreadable")
    assert p["summary"]["cohort_cpql_reason"] == p["cohort"]["cpql_reason"]


# ═════════════════════════════════════════════════════════════════════════════
# §7 — the API metric contract
# ═════════════════════════════════════════════════════════════════════════════

_REQUIRED_META = ("metric_family", "window_basis", "outcome_basis", "dedup_key",
                  "as_of", "source_freshness", "attribution_status", "mapped_count",
                  "unattributed_count", "excluded_non_google_count",
                  "coverage_status", "coverage_notes")


def test_13_every_sql_response_carries_the_metric_contract(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    md = p["cohort"]["metadata"]
    for key in _REQUIRED_META:
        assert key in md, f"SQL metadata lacks {key}"
    assert md["metric_family"] == "acquisition_cohort_outcomes"
    assert md["window_basis"] == "contact_created_at"
    assert md["outcome_basis"] == "latest_canonical_lifecycle_evidence"
    assert md["dedup_key"] == "contact_id"
    assert (md["mapped_count"], md["unattributed_count"],
            md["excluded_non_google_count"]) == (3, 2, 1)
    assert md["coverage_status"] == svc.COVERAGE_EVENT_GAPS
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

    def paid_search(c):
        # Re-stated, not imported — the audit's own SQL restates it too.
        return " ".join(str(c["hs_analytics_source"] or "").replace("_", " ")
                        .lower().split()) == "paid search"

    return {"contacts_acquired": len(inside), "sqls": sum(map(proven, inside)),
            "stage_only_sqls": sum(map(stage_only, inside)),
            "distinct_sql_contact_ids": len({c["contact_id"] for c in inside if proven(c)}),
            "paid_search_sourced_sqls": sum(1 for c in inside if proven(c) and paid_search(c))}


def test_14_the_audit_passes_a_coherent_page(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    a = audit.Audit()
    out = audit.audit_window(a, window="30d", payload=p,
                             independent=_independent_for(_MIXED))
    assert a.violations == [], a.violations
    assert out["cohort"]["google_ads_sqls"] == 5


def test_14i_counterfactual_an_sql_moved_from_google_ads_to_excluded_is_caught(monkeypatch):
    """PR-ADS-161B review: check 4 proves only that the buckets add up to each
    other — a Google Ads SQL relabelled as excluded still adds up. The
    independent Paid Search count is what catches it."""
    p = _page(monkeypatch, contacts=_MIXED)
    s, br = p["summary"], p["cohort"]["breakdown"]
    s["cohort_sqls_google_ads"] -= 1
    s["cohort_sqls_unattributed"] -= 1
    s["cohort_sqls_excluded_non_google"] += 1
    br["excluded_non_google"]["by_reason"]["non_google_source"] += 1
    br["unattributed"]["without_label_row"]["sqls"] -= 1
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED))
    assert not any("bucket_reconciliation" in v for v in a.violations), a.violations
    assert any("google_ads_split" in v for v in a.violations), a.violations


def test_14j_an_approved_not_google_ads_mapping_is_the_one_sanctioned_move(monkeypatch):
    """Positive control for 14i: a Paid Search SQL excluded by an approved
    mapping is NOT a split violation."""
    contacts = _MIXED + [_contact("m1", stage="customer", label="Bing Brand")]
    p = _page(monkeypatch, contacts=contacts, mappings=[
        {"external_campaign_label": "bing brand", "campaign_id": None,
         "match_method": "not_google_ads"}])
    by_reason = p["cohort"]["breakdown"]["excluded_non_google"]["by_reason"]
    assert by_reason[svc.REASON_LABEL_NOT_GOOGLE_ADS] == 1     # the move happened
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(contacts))
    assert not any("google_ads_split" in v for v in a.violations), a.violations


def test_14k_counterfactual_a_cpql_published_over_a_withheld_count_or_zero_spend_is_caught(
        monkeypatch):
    # A payload that says "withheld" but still carries its SQL counts and a
    # published CPQL: the audit names every place they leak.
    p = _page(monkeypatch, contacts=_MIXED)
    p["cohort"]["sql_status"] = "withheld"
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED))
    leak = [v for v in a.violations if "withheld_not_exposed" in v]
    assert leak, a.violations
    exposed = audit.withheld_exposures(p)
    assert "summary.cohort_cpql_usd" in exposed and "cpql_status=published" in exposed
    assert "summary.cohort_sqls_google_ads" in exposed and "cohort.breakdown" in exposed

    p = _page(monkeypatch, contacts=_MIXED)
    brand = next(r for r in p["campaigns"] if r["campaign_id"] == "1")
    brand["spend_usd"] = 0.0
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED))
    assert any("over zero spend" in v for v in a.violations), a.violations


def test_14l_counterfactual_withheld_lifecycle_counts_are_an_exposure(monkeypatch):
    """Round 3, truth auditor MAJOR: the first withheld-exposure scan never
    looked at the lifecycle-event disclosure, which carried 1,531 / 863 / 668
    beside "no SQL count is published"."""
    _patch_page(monkeypatch, contacts=_MIXED,
                freshness=_assess(_sync_row(bootstrap_status="running")))
    from services.campaign_evidence_service import build_campaign_evidence
    p = build_campaign_evidence("30d", now=NOW)
    assert p["cohort"]["sql_status"] == svc.STATUS_WITHHELD
    assert audit.withheld_exposures(p) == []
    # Put the counts back, as the pre-fix service did: the scan must name them.
    p["cohort"]["lifecycle_event_coverage"]["reached_sql_by_current_stage"] = 1531
    p["cohort"]["lifecycle_event_coverage"]["missing_exact_timestamp"] = 668
    exposed = audit.withheld_exposures(p)
    assert "cohort.lifecycle_event_coverage.reached_sql_by_current_stage" in exposed
    assert "cohort.lifecycle_event_coverage.missing_exact_timestamp" in exposed


def test_14m_a_published_cohort_keeps_the_lifecycle_counts(monkeypatch):
    """Positive control for 11h / 14l: the counts are withheld WITH the cohort,
    not always."""
    p = _page(monkeypatch, contacts=_MIXED)
    lc = p["cohort"]["lifecycle_event_coverage"]
    assert (lc["reached_sql_by_current_stage"], lc["missing_exact_timestamp"]) == (1531, 668)
    assert lc["counts_withheld"] is False


def test_14b_counterfactual_the_audit_fails_when_the_page_drops_an_unattributed_sql(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    p["summary"]["cohort_sqls_unattributed"] -= 1
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED))
    assert a.exit_code == audit.EXIT_VIOLATION
    assert any("bucket_reconciliation" in v for v in a.violations)


def test_14c_counterfactual_the_audit_fails_when_the_page_disagrees_with_sql(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    independent = _independent_for(_MIXED)
    independent["sqls"] += 1                  # canonical evidence proves one more
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p, independent=independent)
    assert any("sql_proof" in v for v in a.violations)


def test_14d_lifecycle_gaps_alone_do_not_fail_the_audit(monkeypatch):
    """Required: incomplete event timestamps, correctly disclosed, are NOT a
    violation — every SQL here is undated."""
    contacts = [_contact(f"s{i}", stage="customer") for i in range(4)]
    p = _page(monkeypatch, contacts=contacts)
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(contacts))
    assert a.exit_code == audit.EXIT_OK, a.violations


def test_14e_counterfactual_undisclosed_undated_sqls_fail_the_audit(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    p["cohort"]["breakdown"]["all_sources"]["sqls_missing_event_timestamp"] = 0
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED))
    assert any("lifecycle_gaps_disclosed" in v for v in a.violations)


def test_14f_counterfactual_a_cpql_not_drawn_from_cohort_sqls_fails(monkeypatch):
    p = _page(monkeypatch, contacts=_MIXED)
    p["summary"]["cohort_cpql_usd"] = p["summary"]["overall_cpql_usd"] or 1.0
    a = audit.Audit()
    audit.audit_window(a, window="30d", payload=p,
                       independent=_independent_for(_MIXED))
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
# §9 — the frontend, executed: the WHOLE real app.js in a node `vm`
# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-161B review round 3 (Copilot HIGH: "raw SQL exposed when the cohort is
# withheld"). These tests load the production app.js unmodified — every helper,
# formatter and threshold it really uses — behind a minimal DOM stub, and render
# each Campaign Evidence surface. They are ADVERSARIAL: the payload handed to
# the UI carries a sentinel SQL count and CPQL even though its verdict withholds
# them (the backend no longer sends them; the UI is the second wall, and must
# hold on its own). Each surface must not show the sentinel. Every guard is
# then shown red against a mutated app.js that bypasses it.

_VM_HARNESS = r"""const fs = require("fs"); const vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const script = fs.readFileSync(process.argv[3], "utf8");
const H = { get: (t, p) => (p in t ? t[p] : STUB), apply: () => STUB, construct: () => STUB };
const STUB = new Proxy(function () {}, H);
const els = {};
function mkEl(id) {
  const o = { id, innerHTML: "", textContent: "", value: "", style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    hasAttribute() { return false; }, addEventListener() {}, removeEventListener() {},
    querySelectorAll() { return []; }, querySelector() { return null; },
    appendChild(c) { return c; }, append() {}, prepend() {}, remove() {},
    insertAdjacentHTML(pos, html) { this.innerHTML += html; }, focus() {}, blur() {},
    closest() { return null; }, scrollTo() {}, contains() { return false; },
    getBoundingClientRect() { return {}; } };
  return new Proxy(o, { get: (t, p) => (p in t ? t[p] : STUB),
                        set: (t, p, v) => { t[p] = v; return true; } });
}
const document = new Proxy({
  getElementById: (id) => els[id] || (els[id] = mkEl(id)),
  querySelector: (s) => els[s] || (els[s] = mkEl(s)), querySelectorAll: () => [],
  createElement: (t) => mkEl(t), addEventListener() {}, removeEventListener() {},
  body: mkEl("body"), documentElement: mkEl("html"), readyState: "loading" }, H);
const ctx = { console, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0,
  clearInterval() {}, Promise, URLSearchParams, Intl, Date, Math, JSON, document,
  localStorage: STUB, sessionStorage: STUB, navigator: STUB, location: STUB, history: STUB,
  fetch: () => new Promise(() => {}), addEventListener() {}, removeEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  requestAnimationFrame: () => 0, Plotly: STUB, CustomEvent: function () {}, __els: els };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx, { filename: "app.js" });
const out = vm.runInContext(script, ctx, { filename: "probe.js" });
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""

#: A sentinel no legitimate rendering produces.
SENTINEL = 7919


def _js_function(js: str, name: str) -> str:
    """One top-level function, from its declaration to its own closing brace.

    Top-level functions in app.js close with `}` at column 0. `node` parsing
    the result is the check that the slice is whole.
    """
    i = js.find(f"\nfunction {name}(")
    assert i != -1, f"function {name} not found"
    j = js.find("\n}\n", i)
    assert j != -1, f"function {name} has no column-0 closing brace"
    return js[i:j + 2]


def _run_app(script: str, *, js: str | None = None, tmp=None):
    """Evaluate ``script`` in the context of the real app.js (or a mutation of
    it) and return its final expression, JSON-decoded. Node is a hard
    requirement here, as it is in CI — a missing binary fails, never skips."""
    node = shutil.which("node")
    assert node, "node is required: CI runs `node --check static/app.js`"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "harness.js").write_text(_VM_HARNESS, encoding="utf-8")
        (d / "app.js").write_text(js if js is not None else _APP_JS.read_text(encoding="utf-8"),
                                  encoding="utf-8")
        (d / "probe.js").write_text(script, encoding="utf-8")
        out = subprocess.run([node, str(d / "harness.js"), str(d / "app.js"),
                              str(d / "probe.js")], capture_output=True, text=True,
                             timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def _cohort_js(status, reason=None, cpql_status=None, cpql_reason=None,
               coverage="cohort_complete_event_timestamps_incomplete",
               as_of="2026-10-03T06:00:00+00:00"):
    # The lifecycle block is ADVERSARIAL too: sentinel reached-SQL counts and
    # `counts_withheld: false`, as a faulty backend might send them. The UI must
    # gate them on the verdict, not on the backend's flag.
    return {"sql_status": status, "sql_reason": reason,
            "cpql_status": cpql_status or ("published" if status == "published" else status),
            "cpql_reason": cpql_reason if cpql_reason is not None else reason,
            "metadata": {"as_of": as_of, "coverage_status": coverage},
            "lifecycle_event_coverage": {
                "metric_family": "lifecycle_stage_events", "published_on_this_page": False,
                "reached_sql_by_current_stage": SENTINEL, "exact_direct_timestamp": SENTINEL,
                "recovered_timestamp": SENTINEL, "missing_exact_timestamp": SENTINEL,
                "open_post_boundary_incidents": 103, "counts_withheld": False}}


#: Every verdict under which no SQL-derived value may be visible — the page
#: (or drawer) verdicts the backend emits, each with its real reason code.
_WITHHOLDING = [
    _cohort_js("withheld", "source_sync_state_missing", coverage="cohort_population_not_proven"),
    _cohort_js("withheld", "source_bootstrap_incomplete", coverage="cohort_population_not_proven"),
    _cohort_js("withheld", "source_last_incremental_failed", coverage="cohort_population_not_proven"),
    _cohort_js("withheld", "data_watermark_unknown", coverage="cohort_population_not_proven"),
    _cohort_js("withheld", "cohort_reconciliation_failed", coverage="cohort_population_not_proven"),
    _cohort_js("unavailable", "canonical_funnel_unreadable", coverage="unavailable"),
    None,                                                     # no cohort block at all
]
_WITHHOLDING_IDS = ["missing_sync_state", "bootstrap_incomplete", "failed_incremental",
                    "unknown_watermark", "reconciliation_failed", "unavailable_funnel",
                    "no_cohort_block"]
_PUBLISHED_JS = _cohort_js("published")


def _leaky_row(key="1", name="Brand - UK", spend=100.0, sqls=SENTINEL, status="SQL producer"):
    """A row as a FAULTY backend might send it under a withheld verdict: the
    raw count, a published-looking CPQL and its own copy of the verdict saying
    published. The UI must not trust any of that."""
    return {"campaign_key": key, "campaign_id": key, "campaign_name": name,
            "spend_usd": spend, "spend_native": spend, "spend_currency": "GBP",
            "cohort_contacts_acquired": 12, "cohort_sql_status": "published",
            "cohort_sqls": sqls, "cohort_sqls_missing_event_timestamp": SENTINEL,
            "cohort_cpql_usd": SENTINEL, "cohort_cpql_status": "published",
            "confirmed_junk": 1, "junk_rate_pct": 10.0, "outcome_status": status,
            "mapping_status": "mapped", "aliases": []}


_LEAKY_SUMMARY = {"cohort_sql_status": "published", "cohort_sqls_google_ads": SENTINEL,
                  "cohort_sqls_mapped": SENTINEL, "cohort_sqls_unattributed": SENTINEL,
                  "cohort_sqls_excluded_non_google": SENTINEL,
                  "cohort_sqls_all_sources": SENTINEL,
                  "cohort_sqls_missing_event_timestamp": SENTINEL,
                  "cohort_cpql_usd": SENTINEL, "cohort_cpql_status": "published",
                  "spend_usd": 1000.0, "spend_native": 800.0, "campaigns": 1}


def _render_surfaces(page_cohort, *, drawer_cohort="same", js=None) -> dict:
    """Render every Campaign Evidence surface through the real app.js."""
    drawer = page_cohort if drawer_cohort == "same" else drawer_cohort
    script = textwrap.dedent(f"""
        _campaignCohort = {json.dumps(page_cohort)};
        _campaignSummary = {json.dumps(_LEAKY_SUMMARY)};
        _campaignEvidence = [{json.dumps(_leaky_row())}];
        const out = {{}};
        out.kpis = renderCampaignEvidenceKPIs();
        out.disclosure = renderCampaignSqlReconciliation();
        out.row = renderCampaignEvidenceRow(_campaignEvidence[0]);
        renderCampaignDrawer({{
          campaign_name: "Brand - UK", cohort: {json.dumps(drawer)},
          campaign: Object.assign({{}}, _campaignEvidence[0], {{
            window: "30d", total_leads: 3, in_progress: 0, wrong_fit: 0, unknown: 0,
            verdicted_leads: 3, legacy_lead_status: {{qualified: {SENTINEL}}} }}),
          lead_quality: null,
          countries: [{{country: "UK", total_leads: 3, confirmed_sqls: {SENTINEL},
                        in_progress: 0, confirmed_junk: 1, wrong_fit: 0, unknown: 0,
                        junk_rate_pct: 10.0}}],
          keywords: [], waste_terms: [], recent_leads: [], label_set: []
        }});
        out.drawer = __els["campaign-drawer-body"].innerHTML;
        out.sentinel = [String({SENTINEL}), fmtCount({SENTINEL}), fmtDollar({SENTINEL})];
        out
    """)
    return _run_app(script, js=js)


def _exposed(out: dict, surface: str) -> bool:
    return any(token in out[surface] for token in out["sentinel"])


@pytest.mark.parametrize("cohort", _WITHHOLDING, ids=_WITHHOLDING_IDS)
@pytest.mark.parametrize("surface", ["kpis", "disclosure", "row", "drawer"])
def test_15_no_surface_shows_a_withheld_sql_count_or_cpql(cohort, surface):
    out = _render_surfaces(cohort)
    assert not _exposed(out, surface), out[surface][:1500]
    # And it says why, in words — never a blank, never a zero.
    assert ("Withheld" in out[surface] or "Unavailable" in out[surface]
            or "withheld" in out[surface] or "unavailable" in out[surface])


def test_15b_positive_control_a_published_verdict_does_render_the_count():
    """Without this, test_15 passes for a UI that renders nothing."""
    out = _render_surfaces(_PUBLISHED_JS)
    for surface in ("kpis", "disclosure", "row", "drawer"):
        assert _exposed(out, surface), surface


def test_15c_the_mobile_label_cell_shows_the_withholding_state():
    """Narrow screens label each cell from `data-label` (styles.css
    `attr(data-label)`), so the SQL and CPQL cells must carry the withholding
    word themselves — not a number beside a "not published" suffix."""
    import re as _re
    out = _render_surfaces(_cohort_js("withheld", "source_bootstrap_incomplete"))
    cells = dict(_re.findall(r'<td[^>]*data-label="([^"]*)"[^>]*>(.*?)</td>', out["row"], _re.S))
    assert ">Withheld<" in cells["Cohort SQLs"], cells["Cohort SQLs"]
    assert ">Withheld<" in cells["CPQL"], cells["CPQL"]
    assert "not published</span>" not in cells["Cohort SQLs"]


def test_15d_a_sql_dependent_status_is_not_asserted_under_a_withheld_verdict():
    out = _render_surfaces(_cohort_js("withheld", "data_watermark_unknown"))
    assert ">SQL count not published<" in out["row"]
    assert ">SQL producer<" not in out["row"] and ">SQL producer<" not in out["drawer"]


def _filter_sort(page_cohort, *, outcome="all", sort="spend", js=None):
    rows = [_leaky_row("a", "A", spend=10.0, sqls=SENTINEL),
            _leaky_row("b", "B", spend=99.0, sqls=0),
            _leaky_row("c", "C", spend=50.0, sqls=3)]
    for r in rows:
        r["cohort_cpql_usd"] = (r["spend_usd"] / r["cohort_sqls"]) if r["cohort_sqls"] else None
    script = textwrap.dedent(f"""
        _campaignCohort = {json.dumps(page_cohort)};
        _campaignFilters.outcome = {json.dumps(outcome)};
        _campaignFilters.sort = {json.dumps(sort)};
        _campaignFilters.status = "all";
        sortCampaignEvidence(filterCampaignEvidence({json.dumps(rows)})).map(r => r.campaign_key)
    """)
    return _run_app(script, js=js)


@pytest.mark.parametrize("cohort", _WITHHOLDING, ids=_WITHHOLDING_IDS)
def test_15e_filters_and_sorts_never_classify_or_rank_by_a_withheld_count(cohort):
    # has_sql / no_sql classify nothing: every row stays.
    assert sorted(_filter_sort(cohort, outcome="has_sql")) == ["a", "b", "c"]
    assert sorted(_filter_sort(cohort, outcome="no_sql")) == ["a", "b", "c"]
    # SQL and CPQL sorts fall back to spend, never the hidden ordering.
    assert _filter_sort(cohort, sort="sqls") == ["b", "c", "a"]
    assert _filter_sort(cohort, sort="cpql") == ["b", "c", "a"]


def test_15f_positive_control_filters_and_sorts_use_a_published_count():
    assert _filter_sort(_PUBLISHED_JS, outcome="has_sql") == ["c", "a"]
    assert _filter_sort(_PUBLISHED_JS, sort="sqls") == ["a", "c", "b"]


def test_15g_the_drawer_gates_on_its_own_response_not_page_state():
    """Copilot: the drawer read the global cohort from the last Campaign-page
    load. It is opened from the Action Queue too, and a later detail request
    can carry a different verdict."""
    # Page published, drawer response withheld → the drawer withholds.
    out = _render_surfaces(_PUBLISHED_JS,
                           drawer_cohort=_cohort_js("withheld", "source_bootstrap_incomplete"))
    assert not _exposed(out, "drawer")
    # Page state absent (Action Queue), drawer response published → it publishes.
    out = _render_surfaces(None, drawer_cohort=_PUBLISHED_JS)
    assert _exposed(out, "drawer")


def test_15h_a_row_withheld_by_its_own_verdict_stays_withheld_under_a_published_page():
    script = textwrap.dedent(f"""
        _campaignCohort = {json.dumps(_PUBLISHED_JS)};
        const r = {json.dumps(_leaky_row())};
        r.cohort_sql_status = "withheld"; r.cohort_sql_reason = "cohort_reconciliation_failed";
        renderCampaignEvidenceRow(r)
    """)
    html = _run_app(script)
    assert "7,919" not in html and "7919" not in html
    assert ">Withheld<" in html


def test_15i_the_gate_has_no_global_fallback():
    js = _APP_JS.read_text(encoding="utf-8")
    gate = _js_function(js, "campaignSqlPublication")
    assert "function campaignSqlPublication(cohort)" in gate
    assert "_campaignCohort" not in gate
    import re as _re
    assert not _re.search(r"campaign(?:Row)?SqlPublication\(\s*\)", js), \
        "a caller invokes the gate with no cohort"
    for fn in ("renderCampaignDrawer", "_appendDrawerEvidenceSections"):
        assert "_campaignCohort" not in _js_function(js, fn), fn
    # Missing argument = unavailable, never permission.
    r = _run_app("campaignSqlPublication(undefined)")
    assert r["publish"] is False and r["state"] == "unavailable"


@pytest.mark.parametrize("status", ["withheld", "unavailable", "anything-else"])
def test_15j_cpql_never_publishes_without_the_sql_count(status):
    r = _run_app(f"campaignSqlPublication({json.dumps(_cohort_js(status, 'x', cpql_status='published'))})")
    assert r["publish"] is False and r["cpql"] is False


def test_15k_the_drawer_declares_its_gate_before_using_it():
    """The temporal-dead-zone defect PR-ADS-158's registry recorded."""
    fn = _js_function(_APP_JS.read_text(), "renderCampaignDrawer")
    declared = fn.index("const drawerSqlPub = campaignRowSqlPublication(drawerCohort, camp);")
    first_use = fn.index("drawerSqlPub.")
    assert declared < first_use


def test_15l_the_basis_label_names_the_cohort_and_the_funnel_watermark():
    js = _APP_JS.read_text(encoding="utf-8")
    assert 'CAMPAIGN_COHORT_BASIS = "SQLs from contacts created during this period"' in js
    assert "measured as of the canonical contact-funnel watermark" in js
    assert "Contacts that entered SQL during this period" not in js
    assert _run_app('campaignCohortAsOf({metadata: {}})') == "an unknown data watermark"


def test_15m_the_legacy_qualified_columns_are_labelled_as_lead_status():
    js = _APP_JS.read_text(encoding="utf-8")
    sections = _js_function(js, "_appendDrawerEvidenceSections")
    assert sections.count("CAMPAIGN_LEGACY_QUALIFIED_LABEL") == 2
    assert "CAMPAIGN_SQL_SCOPE_SHORT" not in sections


#: Each mutation bypasses ONE guard in app.js. The check named beside it must
#: then catch the sentinel — proving the guard is load-bearing, not decorative.
_UI_MUTATIONS = [
    ("kpi_gate", "  const sqlKpi = pub.publish\n", "  const sqlKpi = true\n", "kpis"),
    ("row_gate", "const sqls = !pub.publish ? campaignSqlWithheld(pub, { compact: true })",
     "const sqls = false ? campaignSqlWithheld(pub, { compact: true })", "row"),
    ("drawer_gate", "${drawerSqlPub.publish ? cnt(camp.cohort_sqls)", "${true ? cnt(camp.cohort_sqls)",
     "drawer"),
    ("drawer_reads_page_state", "  const drawerCohort = data.cohort || null;",
     "  const drawerCohort = _campaignCohort;", "drawer_isolation"),
    ("row_narrowing", '  if (!row || row.cohort_sql_status !== "published") {',
     "  if (false) {", "row_narrowing"),
    ("sort_gate", '!campaignSqlPublication(_campaignCohort).publish) by = "spend";',
     'false) by = "spend";', "sort"),
    ("filter_gate", "&& !campaignRowSqlPublication(_campaignCohort, c).publish) return true;",
     "&& false) return true;", "filter"),
    ("country_split_gate", "countrySqlPub.publish ? r.confirmed_sqls", "true ? r.confirmed_sqls",
     "drawer"),
    # Round 3 (truth auditor MAJOR): the lifecycle disclosure's reached-SQL
    # counts are gated on the verdict.
    ("lifecycle_counts_gate", "const lcCountsShown = pub.publish && !lc.counts_withheld;",
     "const lcCountsShown = true;", "disclosure"),
]


@pytest.mark.parametrize("name,old,new,check", _UI_MUTATIONS, ids=[m[0] for m in _UI_MUTATIONS])
def test_15n_every_ui_guard_is_load_bearing(name, old, new, check):
    js = _APP_JS.read_text(encoding="utf-8")
    assert js.count(old) == 1, f"mutation anchor for {name} missing or ambiguous"
    mutated = js.replace(old, new, 1)
    withheld = _cohort_js("withheld", "source_bootstrap_incomplete",
                          coverage="cohort_population_not_proven")
    if check in ("kpis", "row", "drawer", "disclosure"):
        out = _render_surfaces(withheld, js=mutated)
        assert _exposed(out, check), f"{name}: bypassing the guard did not expose the count"
    elif check == "drawer_isolation":
        out = _render_surfaces(_PUBLISHED_JS, drawer_cohort=withheld, js=mutated)
        assert _exposed(out, "drawer"), name
    elif check == "row_narrowing":
        script = textwrap.dedent(f"""
            _campaignCohort = {json.dumps(_PUBLISHED_JS)};
            const r = {json.dumps(_leaky_row())};
            r.cohort_sql_status = "withheld";
            renderCampaignEvidenceRow(r)
        """)
        assert "7,919" in _run_app(script, js=mutated), name
    elif check == "sort":
        assert _filter_sort(withheld, sort="sqls", js=mutated) == ["a", "c", "b"], name
    elif check == "filter":
        assert sorted(_filter_sort(withheld, outcome="has_sql", js=mutated)) == ["a", "c"], name


@pytest.mark.parametrize("mutation,expected", [
    # The positive control: an irrelevant edit leaves the certification green.
    (("let _campaignCohort = null;", "let _campaignCohort = null; let _unused = null;"),
     None),
    # The gate reads page-global state instead of its argument.
    (("function campaignSqlPublication(cohort) {\n  const c = cohort;",
      "function campaignSqlPublication(cohort) {\n  const c = _campaignCohort;"),
     "reads page-global cohort state"),
    # A caller gates on nothing.
    (("const pub = campaignSqlPublication(_campaignCohort);\n  // PR-ADS-157 §2 — a status",
      "const pub = campaignSqlPublication();\n  // PR-ADS-157 §2 — a status"),
     "with no cohort"),
    # The drawer gates on the page's state, not its own response.
    (("  const drawerCohort = data.cohort || null;", "  const drawerCohort = _campaignCohort;"),
     "does not gate on its own /api/campaign-detail cohort"),
    (("_campaignCohort = data.cohort || null;", "_campaignCohort = null;"),
     "not carried from /api/campaigns into state"),
    (('const label = pub.state === "unavailable" ? "Unavailable" : "Withheld";',
      'const label = "";'),
     "no Withheld / Unavailable rendering"),
    (('const CAMPAIGN_SQL_SCOPE_LABEL = "Cohort SQLs";',
      'const CAMPAIGN_SQL_SCOPE_LABEL = "SQLs";'),
     "not named 'Cohort SQLs'"),
    # Round 3: a surface reading a legacy SQL / CPQL field (present while the
    # cohort is withheld) is caught.
    (("  const junk  = c.confirmed_junk == null",
      "  const legacy = c.confirmed_sqls;\n  const junk  = c.confirmed_junk == null"),
     "reads legacy SQL / CPQL fields"),
    (("  const s = _campaignSummary || {};\n  const cur",
      "  const s = _campaignSummary || {};\n  const _o = s.overall_cpql_usd;\n  const cur"),
     "reads legacy SQL / CPQL fields"),
    # Round 3: a literal is not a response's cohort.
    (("const sqlPub = campaignSqlPublication(_campaignCohort);",
      'const sqlPub = campaignSqlPublication({sql_status: "published"});'),
     "a literal instead of a response's cohort"),
])
def test_16_the_pr_ads_157_certification_still_goes_red(tmp_path, monkeypatch,
                                                       mutation, expected):
    """PR-ADS-157's certification checks, retargeted at the cohort contract
    and tightened in round 3. Each must fail when the property it guards breaks."""
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
    # A real won deal IS in the ledger. Round 3 removed closed-won publication,
    # so the page must say nothing about it (test_20) — the absence is proven
    # against present data, not against an empty table.
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
    assert not [k for k in s if "closed_won" in k]
    assert all(not [k for k in r if "closed_won" in k] for r in p["campaigns"])
    assert p["cohort"]["closed_won_deals"]["published_on_this_page"] is False
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


#: Original-source spellings at the edges of `normalize_source`, each paired
#: with what `classify_source` makes of it. The tab / newline / NBSP cases are
#: the ones the audit's first SQL normalisation disagreed on (re-review MINOR 1).
_SOURCE_EDGES = [
    "\tPaid Search", "Paid Search\n", "\nPAID_SEARCH\n", " Paid Search",
    "Paid Search", "Paid Search ", "Paid Search", "  paid   SEARCH ",
    "PAID_SEARCH", "paid-search", "", None, "Paid Searches",
]


@_needs_pg
def test_25_pg_the_audits_paid_search_rule_agrees_with_classify_source(seeded):
    """The `google_ads_split` check is only independent if its SQL restates the
    Python rule EXACTLY. Every edge spelling goes through the production writer
    into PostgreSQL; the audit's count must equal classify_source's."""
    from analysis.source_classification import GROUP_GOOGLE_ADS, classify_source
    from db import writers

    inside = seeded["inside"]
    writers.upsert_hubspot_contact_funnel([
        {"contact_id": f"edge{i}", "lifecycle_stage": "customer",
         "created_at": inside, "last_modified_at": inside,
         "hs_analytics_source": src}
        for i, src in enumerate(_SOURCE_EDGES)])
    expected_edge = sum(1 for src in _SOURCE_EDGES
                        if classify_source(src, None) == GROUP_GOOGLE_ADS)
    assert expected_edge == 9          # the fixture really exercises both sides

    _, end_before = svc.window_instants(None, datetime.now(timezone.utc).date())
    independent = audit.independent_counts(None, end_before)
    # The seeded funnel's own Paid Search SQLs: dated, undated, recovered,
    # unmapped, old_entered_recently.
    assert independent["paid_search_sourced_sqls"] == 5 + expected_edge

    # And the full audit agrees with the page on every window.
    a, _ = audit.run()
    assert not any("google_ads_split" in v for v in a.violations), a.violations
    assert not any("google_ads_split" in u for u in a.unavailable), a.unavailable


@_needs_pg
def test_25b_pg_counterfactual_the_first_sql_normalisation_disagreed(seeded, monkeypatch):
    """The pre-fix expression (btrim before collapsing, PostgreSQL \\s) run over
    the same rows: it must miss the tab / newline / NBSP spellings, or test_25
    proves nothing about them."""
    from db.connection import get_conn
    from db import writers

    inside = seeded["inside"]
    writers.upsert_hubspot_contact_funnel([
        {"contact_id": f"edge{i}", "lifecycle_stage": "customer",
         "created_at": inside, "last_modified_at": inside,
         "hs_analytics_source": src}
        for i, src in enumerate(_SOURCE_EDGES)])
    pre_fix = ("regexp_replace(btrim(lower(replace(coalesce(hs_analytics_source, ''), "
               "'_', ' '))), '\\s+', ' ', 'g') = 'paid search'")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FILTER (WHERE {pre_fix}) "
                        f"FROM hubspot_contact_funnel WHERE contact_id LIKE 'edge%%'")
            pre_fix_count = cur.fetchone()[0]
        conn.rollback()
    assert pre_fix_count < 9, "the pre-fix SQL already agreed; the edges test nothing"


@_needs_pg
def test_26_pg_an_incomplete_bootstrap_withholds_everywhere_and_exposes_nothing(seeded):
    """The round-3 HIGH finding end to end: the real sync-state row says the
    bootstrap is still running. The page, every row, the detail endpoint and
    the audit must agree it is withheld — and no SQL count may be anywhere."""
    from db import writers
    from services.campaign_evidence_service import build_campaign_evidence
    import api.server as server

    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="running", last_status="partial")
    p = build_campaign_evidence("30d")
    assert (p["cohort"]["sql_status"], p["cohort"]["sql_reason"]) == \
        ("withheld", "source_bootstrap_incomplete"), p["cohort"]
    assert audit.withheld_exposures(p) == []
    assert all(r["cohort_sqls"] is None and r["cohort_sql_status"] == "withheld"
               for r in p["campaigns"])

    d = server._build_campaign_detail("Brand - UK", 30, window_key="30d", campaign_key="1")
    assert (d["cohort"]["sql_status"], d["cohort"]["sql_reason"]) == \
        ("withheld", "source_bootstrap_incomplete")
    assert d["campaign"]["cohort_sqls"] is None and d["campaign"]["cohort_cpql_usd"] is None

    a, report = audit.run()
    assert a.violations == [], a.violations
    assert all(w["sql_status"] == "withheld" for w in report["windows"])
    assert any(c["check"].endswith("withheld_not_exposed") and c["ok"] for c in a.checks)
