"""
PR-ADS-161B — canonical marketing outcome cohorts and the Campaign Evidence
migration.

PR-ADS-177C retired Campaign Evidence (``/api/campaigns``, its drawer, its
renderers and ``services/campaign_evidence_service``). The cases that pinned
that payload or UI are removed; each property is asserted on the live
Campaigns surface (``tests/test_pr_ads_177c_campaign_feed_port_pg_integration.py``,
the port table in ``docs/60`` §9.3). This module keeps the cohort service's own
cases, the gate's (``campaignSqlPublication``), and the audit, whose
per-window certification now reads the settled campaign view.

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
    """The REAL campaign-ID resolver over a given spend/identity index —
    Campaign Evidence's, moved unchanged to services.campaign_label_resolver
    by PR-ADS-177C (the page is retired; the rule is not)."""
    from services.campaign_label_resolver import build_resolver
    return build_resolver(
        {"rows": [{"campaign_id": cid, "campaign_name": n} for cid, n in spend]},
        {"mappings": list(mappings)})


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


_MIXED = [
    _contact("a1", stage="salesqualifiedlead", direct=SQL_DATE, label="Brand - UK"),
    _contact("a2", stage="customer", label="Brand - UK"),                      # undated
    _contact("a3", stage="lead", label="Brand - UK"),
    _contact("g1", stage="opportunity", label="Gulf"),                         # undated
    _contact("u1", stage="salesqualifiedlead", label="Mystery Campaign"),      # unmapped
    _contact("u2", stage="customer", label=None),                              # no label
    _contact("x1", stage="customer", source="ORGANIC_SEARCH", label=None),     # excluded
]


def test_11c_counterfactual_reconcile_cohort_detects_a_leaking_bucket():
    c = _cohort(list(_MIXED))
    c["unattributed"]["sqls"] -= 1            # an SQL silently disappears
    problems = svc.reconcile_cohort(c)
    assert any("google_ads.sqls" in p for p in problems)


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


# ═════════════════════════════════════════════════════════════════════════════
# §6b — ONE publication verdict on every surface, every state (round 3)
# ═════════════════════════════════════════════════════════════════════════════
# Summary SQL, row SQL, page CPQL, row CPQL, row outcome, the cohort block, and
# the campaign-detail endpoint (the drawer) — built by the REAL page builder and
# the REAL `_build_campaign_detail` — must state the same verdict for the same
# evidence. Row-only refusals may narrow a row's CPQL; nothing may widen it.

_NO_SQL_CONTACTS = [_contact("z1", stage="lead"), _contact("z2", stage="lead", label="Gulf")]


_SCENARIOS = ["fresh", "stale", "missing_sync_state", "incomplete_bootstrap",
              "failed_incremental", "unknown_watermark", "reconciliation_failure",
              "unavailable_funnel", "missing_campaign_identity", "zero_spend", "zero_sqls"]


# ═════════════════════════════════════════════════════════════════════════════
# §7 — the API metric contract
# ═════════════════════════════════════════════════════════════════════════════

_REQUIRED_META = ("metric_family", "window_basis", "outcome_basis", "dedup_key",
                  "as_of", "source_freshness", "attribution_status", "mapped_count",
                  "unattributed_count", "excluded_non_google_count",
                  "coverage_status", "coverage_notes")


def test_13d_the_cohort_reads_no_event_time_coverage_layer():
    """Forbidden by PR-ADS-161A-1's AST guard, re-asserted for this module."""
    src = (_ROOT / "services" / "marketing_outcome_cohort_service.py").read_text()
    for name in ("cpql_publishable", "complete_sql_total", "lifecycle_sql_coverage"):
        assert name not in src


# ═════════════════════════════════════════════════════════════════════════════
# §8 — the audit command
# ═════════════════════════════════════════════════════════════════════════════


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


@pytest.mark.parametrize("status", ["withheld", "unavailable", "anything-else"])
def test_15j_cpql_never_publishes_without_the_sql_count(status):
    r = _run_app(f"campaignSqlPublication({json.dumps(_cohort_js(status, 'x', cpql_status='published'))})")
    assert r["publish"] is False and r["cpql"] is False


def test_15l_the_basis_label_names_the_cohort_and_the_funnel_watermark():
    # PR-ADS-177C: the basis label (`CAMPAIGN_COHORT_BASIS`) was Campaign
    # Evidence's and is retired with it; every live leads / SQLs label names
    # the cohort ("contacts acquired in window") and a stale count says
    # "measured as of" its watermark (`cohortAsOfNote`).
    js = _APP_JS.read_text(encoding="utf-8")
    assert 'COHORT_SQLS_LABEL = "SQLs · contacts acquired in window"' in js
    assert "as of the contact-funnel watermark" in js
    assert "Contacts that entered SQL during this period" not in js
    assert _run_app('campaignCohortAsOf({metadata: {}})') == "an unknown data watermark"
    stale = {"sql_status": "published", "metadata": {
        "as_of": "2026-10-03T06:00:00+00:00", "source_freshness": {"reason": "source_stale"}}}
    assert "measured as of" in _run_app("cohortAsOfNote(%s)" % json.dumps(stale))


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
def test_21_pg_the_audit_reconciles_every_supported_window(seeded):
    a, report = audit.run()
    assert a.violations == [], a.violations
    assert a.unavailable == [], a.unavailable
    assert a.exit_code == audit.EXIT_OK
    # PR-ADS-177C: the per-window certification is the Campaigns surface's
    # (the settled campaign view), over its business windows.
    from analysis.business_windows import WINDOW_KEYS
    assert [w["window"] for w in report["windows"]] == list(WINDOW_KEYS)
    assert any(w.get("published") for w in report["windows"])
    assert report["external_writes_performed"] is False
    assert report["database_writes_performed"] is False
    split = report["population_split"]
    assert split["reached_sql_by_current_stage"] == 5   # dated, undated, unmapped, organic, old
    assert split["sql_dated_but_stage_now_below_sql"] == 1   # recovered (stage: lead)


#: Original-source spellings at the edges of `normalize_source`, each paired
#: with what `classify_source` makes of it. The tab / newline / NBSP cases are
#: the ones the audit's first SQL normalisation disagreed on (re-review MINOR 1).
_SOURCE_EDGES = [
    "\tPaid Search", "Paid Search\n", "\nPAID_SEARCH\n", " Paid Search",
    "Paid Search", "Paid Search ", "Paid Search", "  paid   SEARCH ",
    "PAID_SEARCH", "paid-search", "", None, "Paid Searches",
]


@_needs_pg
def test_22_pg_counterfactual_the_audit_goes_red_when_the_campaigns_surface_drops_an_sql(
        seeded, monkeypatch):
    """PR-ADS-177C — the port of the Campaign Evidence counterfactual: the
    Campaigns surface (the settled campaign view) loses an SQL from its
    published breakdown, and the audit's independent SQL catches it."""
    import services.revenue_attribution_service as ras
    real = ras._cohort_contract_block

    def leaky(*args, **kwargs):
        out = real(*args, **kwargs)
        b = out.get("breakdown")
        if b and b["all_sources"]["sqls"]:
            b["all_sources"] = {**b["all_sources"], "sqls": b["all_sources"]["sqls"] - 1}
        return out

    monkeypatch.setattr(ras, "_cohort_contract_block", leaky)
    a, _ = audit.run()
    assert a.exit_code == audit.EXIT_VIOLATION
    assert any("sql_proof" in v for v in a.violations), a.violations


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


def test_18_no_document_or_label_overclaims_recency_or_customers():
    """Copilot round 3 (three LOW findings): the cohort is measured as of the
    canonical contact-funnel watermark — not "now", not "today" — and this PR
    publishes SQL contacts only; it never claims to preserve or count customers."""
    import ast as _ast
    doc44 = (_ROOT / "docs" / "44_MARKETING_OUTCOME_COHORTS.md").read_text(encoding="utf-8")
    svc_doc = _ast.get_docstring(_ast.parse(
        (_ROOT / "services" / "marketing_outcome_cohort_service.py").read_text(encoding="utf-8")))
    js = _APP_JS.read_text(encoding="utf-8")
    # PR-ADS-177C: Campaign Evidence's renderers are retired; the same claim
    # is held over the live Campaigns renderers that print the cohort.
    campaign_js = "\n".join(_js_function(js, fn) for fn in (
        "renderCampaignsPageSummary", "renderCampaignsPageTable",
        "renderCampaignsResidualRow", "renderCampaignsPageDrawer"))
    for name, text in (("docs/44", doc44), ("cohort service docstring", svc_doc),
                       ("Campaigns UI", campaign_js)):
        low = text.lower()
        assert "as of now" not in low, name
        assert "as of today" not in low and "*today*" not in low, name
        assert "current data" not in low, name
    assert "canonical contact-funnel watermark" in doc44
    assert "canonical contact-funnel watermark" in svc_doc
    # Customers: defined as NOT published, never claimed.
    assert "real SQLs and customers" not in doc44
    assert "**Customer** — not published" in doc44
    assert "unique customers" not in campaign_js.lower()
