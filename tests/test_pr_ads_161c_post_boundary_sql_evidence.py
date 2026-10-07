"""PR-ADS-161C — post-boundary SQL evidence forensics and gap prevention.

What this suite will not accept
-------------------------------
No SQL-entry date from anything but HubSpot's direct
``hs_v2_date_entered_salesqualifiedlead`` or a genuine ``salesqualifiedlead``
transition in its lifecycle history. Every PostgreSQL test reads the stored
columns back; none trusts a return value about what was written.

A guard whose absence changes nothing is not a guard, so every protection added
here has a COUNTERFACTUAL beside it (§6): the same scenario run with the
protection removed, shown to produce the defect the protection exists to stop.

The fixture (``seeded160``) and the boundary helper are PR-ADS-160's own, so the
boundary these tests police is established by the real service with a
database-stamped instant — never one a test chose.
"""

from __future__ import annotations

import ast
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import tests.conftest as conftest  # noqa: E402,F401  (import-order guard)
import analysis.post_boundary_sql_forensics as fx  # noqa: E402
import connectors.hubspot_pull as hubspot  # noqa: E402
import db.crm_funnel_repository as repo  # noqa: E402
import services.post_boundary_sql_evidence_service as evidence_svc  # noqa: E402
import services.sql_coverage_boundary_service as boundary_svc  # noqa: E402

from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)
from tests.test_pr_ads_160_sql_coverage_boundary import (  # noqa: E402,F401
    BOUNDARY, _establish, seeded160,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

#: HubSpot's own version instants, all after the boundary.
T_LEAD = BOUNDARY + timedelta(days=2)
T_SQL = BOUNDARY + timedelta(days=3)
T_OPP = BOUNDARY + timedelta(days=5)
CREATED = BOUNDARY + timedelta(days=1)

_PII_KEYS = {"email", "firstname", "lastname", "name", "phone", "company",
             "hs_analytics_first_url", "ip_country", "gclid"}


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _v(stage, ts, *, raw=None):
    """One history version in the connector's normalised shape."""
    return {"value": stage, "timestamp": ts,
            "timestamp_raw": (ts.isoformat() if ts is not None else raw),
            "source_type": "CRM_UI", "source_id": "u1", "source_label": None,
            "updated_by_user_id": None}


def _entry(versions=(), *, state=hubspot.HISTORY_PRESENT, direct=None,
           direct_raw=None):
    """One contact's answer from the (stubbed) READ-ONLY history read."""
    if direct is not None:
        d = {"state": "present", "value": direct, "raw_present": True}
    elif direct_raw is not None:
        d = {"state": "unparseable", "value": None, "raw_present": True}
    else:
        d = {"state": "absent", "value": None, "raw_present": False}
    return {"state": state, "versions": list(versions), "direct_sql_entry": d}


JUMP = [_v("lead", T_LEAD), _v("opportunity", T_OPP)]


def _serve(monkeypatch, mapping):
    """Replace the READ-ONLY batch history read with a controlled answer."""
    calls: list = []

    def fake(ids, client=None):
        calls.append(list(ids))
        for cid in ids:
            if isinstance(mapping.get(cid), Exception):
                raise mapping[cid]
        return {cid: mapping[cid] for cid in ids if cid in mapping}

    monkeypatch.setattr(hubspot, "fetch_lifecycle_stage_history", fake)
    return calls


def _contact(cid, stage, *, created=CREATED, modified=None, sql=None):
    from db import writers
    res = writers.upsert_hubspot_contact_funnel([{
        "contact_id": cid, "lifecycle_stage": stage, "created_at": created,
        "last_modified_at": modified or created, "date_entered_sql": sql}])
    assert res["ok"] is True


def _q(sql, params=()):
    from db.connection import get_conn
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        try:
            return cur.fetchall()
        except Exception:  # noqa: BLE001 — statements without a result set
            return None


def _incident(cid):
    from db.connection import get_conn
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM sql_post_boundary_incident "
                    "WHERE contact_id = %s", (cid,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    assert len(rows) <= 1, "one incident per contact, ever"
    return rows[0] if rows else None


def _sql_date(cid):
    rows = _q("SELECT date_entered_sql FROM hubspot_contact_funnel "
              "WHERE contact_id = %s", (cid,))
    return rows[0][0] if rows else None


def _history_rows(cid):
    return _q("SELECT funnel_event, entered_at, hubspot_property, "
              "recovery_run_id FROM hubspot_lifecycle_stage_history "
              "WHERE contact_id = %s", (cid,))


_SNAPSHOT_TABLES = ("hubspot_contact_funnel", "sql_post_boundary_incident",
                    "hubspot_lifecycle_stage_history", "sql_coverage_boundary",
                    "sql_coverage_boundary_contact",
                    "hubspot_contact_funnel_sync_state")


def _snapshot():
    """A content fingerprint of every table the audit or repair could touch."""
    out = {}
    for table in _SNAPSHOT_TABLES:
        out[table] = _q(f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' "
                        f"ORDER BY t::text), '')) FROM {table} t")[0]
    return out


def _detect(run_id):
    return boundary_svc.detect_post_boundary_gaps(apply=True, run_id=run_id)


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the pure forensic vocabulary
# ═════════════════════════════════════════════════════════════════════════════

def test_01_a_stage_jump_is_proven_from_history_and_its_bounds_are_not_a_date():
    """lead → opportunity with no SQL version: HubSpot never had the stage."""
    shape = fx.history_shape(JUMP)

    assert shape["stage_jump_skipped_sql"] is True
    assert shape["has_sql_version"] is False
    assert shape["stage_path"] == "lead>opportunity"
    assert shape["last_known_below_sql_at"] == T_LEAD
    # The upper bound IS the opportunity instant — which is exactly why it is
    # a bound and never an SQL date (an opportunity timestamp is a forbidden
    # substitute by name).
    assert shape["first_observed_at_or_above_sql"] == T_OPP
    assert shape["bounds_basis"] == fx.BOUNDS_BASIS_HISTORY
    assert "sql_entered_at" not in shape and "date_entered_sql" not in shape


def test_02_an_sql_version_is_never_a_stage_jump():
    shape = fx.history_shape([_v("lead", T_LEAD),
                              _v("salesqualifiedlead", T_SQL),
                              _v("opportunity", T_OPP)])
    assert shape["stage_jump_skipped_sql"] is False
    assert shape["sql_version_dated"] is True
    # The current at-or-above run starts AT the SQL version here.
    assert shape["first_observed_at_or_above_sql"] == T_SQL
    assert shape["last_known_below_sql_at"] == T_LEAD


def test_03_an_unranked_predecessor_leaves_the_lower_bound_unknown():
    """'other' proves nothing about SQL. Unknown stays unknown."""
    shape = fx.history_shape([_v("other", T_LEAD), _v("opportunity", T_OPP)])
    assert shape["last_known_below_sql_at"] is None
    assert shape["stage_jump_skipped_sql"] is None
    assert shape["first_observed_at_or_above_sql"] == T_OPP


def test_04_a_contact_created_at_opportunity_has_no_lower_bound():
    shape = fx.history_shape([_v("opportunity", T_OPP)])
    assert shape["last_known_below_sql_at"] is None
    assert shape["stage_jump_skipped_sql"] is None


def test_05_an_unparseable_sql_version_and_an_undated_one_are_different():
    """One is our parser failing on a value HubSpot sent; one is HubSpot."""
    unparseable = fx.history_shape([_v("salesqualifiedlead", None,
                                       raw="not-a-time")])
    undated = fx.history_shape([_v("salesqualifiedlead", None, raw=None)])

    assert unparseable["sql_version_unparseable"] is True
    assert unparseable["sql_version_undated"] is False
    assert undated["sql_version_undated"] is True
    assert undated["sql_version_unparseable"] is False


def test_06_every_incident_reason_the_detector_can_emit_is_mapped():
    """An unmapped reason would fall to cause_unresolved silently."""
    assert set(boundary_svc.INCIDENT_REASONS) == set(fx.MAPPED_INCIDENT_REASONS)
    assert len(set(boundary_svc.INCIDENT_REASONS)) == len(
        boundary_svc.INCIDENT_REASONS)


def test_07_the_classification_vocabulary_partitions_and_fails_closed():
    groups = (fx.CODE_OWNED, fx.SOURCE_UNRESOLVABLE, fx.NOT_DETERMINED)
    flat = [c for g in groups for c in g]
    assert len(flat) == len(set(flat)) == len(fx.CLASSIFICATIONS)
    assert set(fx.CLASSIFICATIONS) <= set(fx.FACTS)
    assert fx.owner_of("something_new") == fx.OWNER_UNKNOWN, (
        "an unknown classification must never be called anybody's gap")


@pytest.mark.parametrize("reason,jump,expected", [
    ("post_boundary_history_has_no_sql_transition", True, fx.C_STAGE_JUMP),
    ("post_boundary_history_has_no_sql_transition", None, fx.C_HISTORY_NO_SQL),
    ("post_boundary_history_request_failed", None, fx.C_HISTORY_FAILED),
    ("post_boundary_history_payload_absent", None, fx.C_HISTORY_ABSENT),
    ("post_boundary_no_direct_sql_date", None, fx.C_HISTORY_NOT_CONSULTED),
    ("post_boundary_history_sql_timestamp_unparseable", None,
     fx.C_HISTORY_SQL_UNPARSEABLE),
    ("post_boundary_history_sql_version_undated", None,
     fx.C_HISTORY_SQL_UNDATED),
    ("a_reason_nobody_declared", None, fx.C_CAUSE_UNRESOLVED),
])
def test_08_local_classification_follows_the_recorded_evidence(
        reason, jump, expected):
    out = fx.classify_local({"reason": reason, "stage_jump_skipped_sql": jump,
                             "current_lifecycle_stage": "opportunity",
                             "funnel_row_present": True})
    assert out["classification"] == expected


def test_09_stored_evidence_with_an_open_incident_outranks_every_reason():
    out = fx.classify_local({
        "reason": "post_boundary_history_has_no_sql_transition",
        "stage_jump_skipped_sql": True, "direct_sql_entry_at": T_SQL,
        "current_lifecycle_stage": "opportunity", "funnel_row_present": True})
    assert out["classification"] == fx.C_STORED_EVIDENCE_OPEN
    assert out["owner"] == fx.OWNER_CODE


def _src(**kw):
    base = {"request_failed": False, "returned": True,
            "direct_state": "absent", "direct_sql_entry_at": None,
            "direct_sql_entry_set_at": None, "last_modified_at": None,
            "history_state": "history_payload_present",
            "history_shape": fx.history_shape(JUMP)}
    base.update(kw)
    return base


LOCAL = {"reason": "post_boundary_history_has_no_sql_transition",
         "current_lifecycle_stage": "opportunity", "funnel_row_present": True,
         "last_modified_at": T_OPP, "last_ingested_at": T_OPP}


@pytest.mark.parametrize("source,expected", [
    # HubSpot changed after our stored copy: the sync never re-read it.
    (_src(direct_state="present", direct_sql_entry_at=T_SQL,
          last_modified_at=T_OPP + timedelta(hours=1)),
     fx.C_CANDIDATE_NOT_REFRESHED),
    # Same version, but HubSpot set the property after we ingested it.
    (_src(direct_state="present", direct_sql_entry_at=T_SQL,
          last_modified_at=T_OPP,
          direct_sql_entry_set_at=T_OPP + timedelta(hours=2)),
     fx.C_LATE_PROPERTY),
    # Set before we ingested that version: the payload carried it; we lost it.
    (_src(direct_state="present", direct_sql_entry_at=T_SQL,
          last_modified_at=T_OPP,
          direct_sql_entry_set_at=T_OPP - timedelta(hours=2)),
     fx.C_WRITER_DROPPED),
    # No instant to decide with: not picked, reported unresolved.
    (_src(direct_state="present", direct_sql_entry_at=T_SQL,
          last_modified_at=T_OPP), fx.C_CAUSE_UNRESOLVED),
    (_src(direct_state="unparseable"), fx.C_DIRECT_UNPARSEABLE),
    (_src(history_shape=fx.history_shape(
        [_v("lead", T_LEAD), _v("salesqualifiedlead", T_SQL)])),
     fx.C_HISTORY_EXACT_NOT_STORED),
    (_src(), fx.C_STAGE_JUMP),
    (_src(history_shape=fx.history_shape([_v("opportunity", T_OPP)])),
     fx.C_HISTORY_NO_SQL),
    (_src(history_state="history_payload_missing"), fx.C_HISTORY_ABSENT),
    (_src(request_failed=True), fx.C_HISTORY_FAILED),
    (_src(returned=False), fx.C_CAUSE_UNRESOLVED),
])
def test_10_the_comparison_names_where_the_evidence_disappeared(
        source, expected):
    assert fx.classify_with_source(LOCAL, source)["classification"] == expected


# ═════════════════════════════════════════════════════════════════════════════
# §2 — the connector reads the direct property, on the same request
# ═════════════════════════════════════════════════════════════════════════════

def test_11_the_history_read_carries_the_direct_property_as_a_current_value():
    from hubspot.crm.contacts.api_client import ApiClient

    wire = ApiClient().sanitize_for_serialization(
        hubspot._batch_history_body(["1"]))
    assert hubspot.HUBSPOT_SQL_ENTRY_PROPERTY in wire["properties"]
    # History is still requested for the stage ONLY — nothing else changed.
    assert wire["propertiesWithHistory"] == ["lifecyclestage"]
    from analysis.crm_lifecycle import EVENT_HUBSPOT_PROPERTY, EVENT_SQL
    assert hubspot.HUBSPOT_SQL_ENTRY_PROPERTY == EVENT_HUBSPOT_PROPERTY[EVENT_SQL]


@pytest.mark.parametrize("raw,state", [
    ("2026-09-17T12:00:00Z", "present"),
    ("1789646400000", "present"),
    ("", "absent"),
    (None, "absent"),
    ("seventeenth of september", "unparseable"),
])
def test_12_the_direct_property_states_are_told_apart(raw, state):
    record = {"id": "1", "properties": {"lifecyclestage": "opportunity",
                                        hubspot.HUBSPOT_SQL_ENTRY_PROPERTY: raw},
              "propertiesWithHistory": {"lifecyclestage": []}}
    out = hubspot._history_from_record(record)["direct_sql_entry"]
    assert out["state"] == state
    assert (out["value"] is not None) is (state == "present")


def test_13_a_record_without_properties_says_nothing_about_the_direct_date():
    out = hubspot._history_from_record({"id": "1"})["direct_sql_entry"]
    assert out["state"] == "not_read", "absence of a container is not absence"


def test_14_the_comparison_read_asks_for_no_personal_property():
    asked = set(hubspot.COMPARISON_CURRENT_PROPERTIES) | set(
        hubspot.COMPARISON_HISTORY_PROPERTIES)
    assert asked == {"lifecyclestage", hubspot.HUBSPOT_SQL_ENTRY_PROPERTY,
                     "lastmodifieddate"}


class _Resp:
    def __init__(self, results):
        self.results = results


class _Batch:
    def __init__(self, results):
        self.calls = []
        self._results = results

    def read(self, batch_read_input_simple_public_object_id):
        self.calls.append(batch_read_input_simple_public_object_id)
        return _Resp(self._results)


class _Client:
    def __init__(self, results):
        batch = _Batch(results)
        self.batch = batch
        self.crm = type("crm", (), {"contacts": type(
            "contacts", (), {"batch_api": batch})()})()


def test_15_the_comparison_read_reports_when_hubspot_set_the_direct_date():
    client = _Client([{
        "id": "1",
        "properties": {"lifecyclestage": "opportunity",
                       "lastmodifieddate": "2026-09-20T00:00:00Z",
                       hubspot.HUBSPOT_SQL_ENTRY_PROPERTY: "2026-09-17T00:00:00Z"},
        "propertiesWithHistory": {
            "lifecyclestage": [{"value": "opportunity",
                                "timestamp": "2026-09-19T00:00:00Z"}],
            hubspot.HUBSPOT_SQL_ENTRY_PROPERTY: [
                {"value": "2026-09-17T00:00:00Z",
                 "timestamp": "2026-09-19T06:00:00Z"}]}}])

    out = hubspot.compare_sql_entry_evidence(["1", "2"], client=client)

    assert out["1"]["direct_sql_entry"]["state"] == "present"
    assert out["1"]["direct_sql_entry_set_at"] == datetime(
        2026, 9, 19, 6, tzinfo=timezone.utc)
    assert out["2"]["returned"] is False, "not returned is not 'no evidence'"
    assert len(client.batch.calls) == 1


# ═════════════════════════════════════════════════════════════════════════════
# §3 — the detector, against a real schema (§8.1–§8.8)
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_20_pg_a_late_direct_property_is_persisted_and_resolves_its_incident(
        seeded160, monkeypatch):
    """§8.1 + §8.7. The evidence path PR-ADS-160 never re-read."""
    _establish()
    _contact("late", "opportunity")
    _serve(monkeypatch, {"late": _entry(JUMP)})
    first = _detect("run1")
    opened = _incident("late")
    assert first["ok"] is True and opened["status"] == "open"
    assert opened["stage_jump_skipped_sql"] is True

    # HubSpot now holds the direct property; our stored copy does not.
    _serve(monkeypatch, {"late": _entry(JUMP, direct=T_SQL)})
    second = _detect("run2")

    assert second["ok"] is True
    assert second["direct_sql_timestamps_refreshed"] == 1
    assert _sql_date("late") == T_SQL, "HubSpot's own value, unchanged"
    closed = _incident("late")
    assert closed["status"] == "resolved"
    assert closed["resolved_by"] == "direct_property"
    assert closed["resolved_by_run_id"] == "run2"
    assert closed["resolution_evidence_at"] == T_SQL
    # §8.7 — the trail stays visible on the resolved row.
    assert closed["detected_at"] == opened["detected_at"]
    assert closed["reason"] == boundary_svc.INCIDENT_HISTORY_NO_SQL
    assert closed["history_stage_path"] == "lead>opportunity"
    assert second["unresolved_post_boundary_incidents"] == 0

    # Rerun: idempotent. Nothing rewritten, nothing duplicated.
    third = _detect("run3")
    assert third["direct_sql_timestamps_refreshed"] == 0
    assert _incident("late")["resolved_by_run_id"] == "run2"
    assert _q("SELECT count(*) FROM sql_post_boundary_incident")[0][0] == 1


@_needs_pg
def test_21_pg_a_history_transition_is_persisted_with_lineage(
        seeded160, monkeypatch):
    """§8.2. Direct property absent; history holds the exact transition."""
    _establish()
    _contact("hist", "opportunity")
    _serve(monkeypatch, {"hist": _entry([_v("lead", T_LEAD)])})
    _detect("run1")
    assert _incident("hist")["status"] == "open"

    _serve(monkeypatch, {"hist": _entry([_v("lead", T_LEAD),
                                         _v("salesqualifiedlead", T_SQL),
                                         _v("opportunity", T_OPP)])})
    result = _detect("run2")

    assert result["history_events_persisted"] == 1
    assert _sql_date("hist") is None, "history never writes the direct column"
    assert _history_rows("hist") == [("sql", T_SQL, "lifecyclestage", "run2")]
    closed = _incident("hist")
    assert closed["status"] == "resolved" and closed["resolved_by"] == "history"
    assert closed["resolution_evidence_at"] == T_SQL

    _detect("run3")
    assert len(_history_rows("hist")) == 1, "rerun appends nothing"


@_needs_pg
def test_22_pg_no_transition_stays_null_open_and_explained(
        seeded160, monkeypatch):
    """§8.3. Nothing is substituted — not creation, sync, boundary, bounds."""
    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    result = _detect("run1")

    inc = _incident("jump")
    assert inc["status"] == "open"
    assert inc["reason"] == boundary_svc.INCIDENT_HISTORY_NO_SQL
    assert inc["direct_property_state"] == "absent"
    assert inc["stage_jump_skipped_sql"] is True
    assert inc["last_known_below_sql_at"] == T_LEAD
    assert inc["first_observed_at_or_above_sql"] == T_OPP
    assert inc["observation_bounds_basis"] == fx.BOUNDS_BASIS_HISTORY
    assert result["new_undated_sql_gaps"] == 1

    assert _sql_date("jump") is None
    assert _history_rows("jump") == []
    rows = repo.fetch_post_boundary_sql_contacts(
        boundary_id=repo.fetch_active_sql_coverage_boundary()["boundary"][
            "boundary_id"], since=BOUNDARY)["rows"]
    row = next(r for r in rows if r["contact_id"] == "jump")
    assert row["effective_date_entered_sql"] is None, (
        "no bound, observation or creation instant leaks into the canonical "
        "effective SQL date")


@_needs_pg
def test_23_pg_a_stage_jump_keeps_lifecycle_events_withheld_and_the_cohort_valid(
        seeded160, monkeypatch):
    """§8.4 + §8.10. Two definitions; one incident affects only one of them."""
    from scripts import audit_sql_coverage_gate as gate_mod
    from services import marketing_outcome_cohort_service as cohort_svc

    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    _detect("run1")

    g, report = gate_mod.run()
    assert g.exit_code == gate_mod.EXIT_VIOLATION
    assert any("no_open_post_boundary_gaps" in v for v in g.violations)
    assert report["post_boundary_gaps"]["open"] == 1

    out = cohort_svc.build_window_outcomes(
        BOUNDARY.date(), (BOUNDARY + timedelta(days=30)).date(),
        resolve_label=lambda label: ("unmatched", label))
    assert out["sql_publication"] == (cohort_svc.STATUS_PUBLISHED, None), (
        "an open lifecycle-event incident must not withhold the acquisition "
        "cohort, which never asks for an SQL-entry date")
    assert out["cohort"]["all_sources"]["sqls"] == 1
    assert out["cohort"]["all_sources"]["sqls_missing_event_timestamp"] == 1


@_needs_pg
def test_24_pg_request_failed_payload_absent_and_no_transition_stay_apart(
        seeded160, monkeypatch):
    """§8.5. Three different facts, three reasons, three classifications."""
    _establish()
    for cid in ("absent", "nosql", "unread"):
        _contact(cid, "opportunity")
    _serve(monkeypatch, {
        "absent": _entry(state=hubspot.HISTORY_PROPERTY_ABSENT),
        "nosql": _entry([_v("opportunity", T_OPP)]),
        # "unread" is not answered at all.
    })
    _detect("run1")

    reasons = {cid: _incident(cid)["reason"]
               for cid in ("absent", "nosql", "unread")}
    assert reasons == {
        "absent": boundary_svc.INCIDENT_HISTORY_ABSENT,
        "nosql": boundary_svc.INCIDENT_HISTORY_NO_SQL,
        "unread": boundary_svc.INCIDENT_HISTORY_UNREADABLE}

    report = evidence_svc.audit()
    by_id = {i["contact_id"]: i["classification"] for i in report["incidents"]}
    assert by_id == {"absent": fx.C_HISTORY_ABSENT,
                     "nosql": fx.C_HISTORY_NO_SQL,
                     "unread": fx.C_HISTORY_FAILED}
    assert report["root_cause"]["not_determined"] == 2


@_needs_pg
def test_25_pg_an_exact_timestamp_survives_absence_and_never_reopens(
        seeded160, monkeypatch):
    """§8.6. Absence is not a correction — anywhere on the path."""
    from db import writers

    _establish()
    _contact("kept", "opportunity")
    _serve(monkeypatch, {"kept": _entry(JUMP)})
    _detect("run1")
    _contact("kept", "opportunity", modified=T_OPP, sql=T_SQL)
    _detect("run2")
    assert _incident("kept")["status"] == "resolved"

    # A later, NEWER payload omits the property.
    _contact("kept", "opportunity", modified=T_OPP + timedelta(days=1), sql=None)
    assert _sql_date("kept") == T_SQL
    _detect("run3")
    inc = _incident("kept")
    assert inc["status"] == "resolved" and inc["resolved_by"] == "direct_property"

    # A repair never overwrites a stored exact date either.
    res = writers.apply_post_boundary_sql_evidence(
        [{"contact_id": "kept", "date_entered_sql": T_LEAD}], [], run_id="r")
    assert res["ok"] is True and res["direct_unchanged"] == 1
    assert _sql_date("kept") == T_SQL


_FAIL_ON_INCIDENT_UPDATE = """
CREATE OR REPLACE FUNCTION _fail_161c() RETURNS TRIGGER AS $$
BEGIN RAISE EXCEPTION 'injected failure'; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER _fail_161c BEFORE UPDATE ON sql_post_boundary_incident
  FOR EACH ROW EXECUTE FUNCTION _fail_161c();
"""


@_needs_pg
def test_26_pg_a_writer_failure_rolls_back_and_never_reports_success(
        seeded160, monkeypatch):
    """§8.8. Evidence and its incident cannot disagree, even on failure."""
    import scheduler.incremental_sync as sync

    _establish()
    _contact("late", "opportunity")
    _serve(monkeypatch, {"late": _entry(JUMP)})
    _detect("run1")
    _q(_FAIL_ON_INCIDENT_UPDATE)

    _serve(monkeypatch, {"late": _entry(JUMP, direct=T_SQL)})
    result = _detect("run2")
    assert result["ok"] is False
    assert result["run_outcome"] == boundary_svc.BOUNDARY_WRITE_FAILED
    assert _sql_date("late") is None, "the direct write rolled back with it"
    assert _incident("late")["status"] == "open"

    errors: list = []
    dataset = sync._detect_sql_coverage_gaps(run_id="run3", errors=errors)
    assert dataset["status"] == "failed" and errors


@_needs_pg
def test_27_pg_a_stranded_incident_closes_only_on_stored_evidence(
        seeded160, monkeypatch):
    """Incidents whose contact left the detector's population.

    'pre': its direct date arrived and PRECEDES the boundary, so it is no
    longer "post-boundary" by the population rule — PR-ADS-160 never looked at
    it again and its incident stayed open forever. 'fell': its stage fell below
    SQL and it has no evidence — it must stay open, and be counted.
    """
    _establish()
    _contact("pre", "opportunity")
    _contact("fell", "opportunity")
    _serve(monkeypatch, {"pre": _entry(JUMP), "fell": _entry(JUMP)})
    _detect("run1")

    _contact("pre", "opportunity", modified=T_OPP,
             sql=BOUNDARY - timedelta(days=10))
    _contact("fell", "lead", modified=T_OPP)
    result = _detect("run2")

    assert result["stranded_incidents_examined"] == 2
    assert result["stranded_incidents_resolved"] == 1
    assert _incident("pre")["status"] == "resolved"
    assert _incident("fell")["status"] == "open"
    assert result["open_incidents_outside_population"] == 1
    assert result["unresolved_post_boundary_incidents"] == 1


@_needs_pg
def test_28_pg_no_resolver_closes_an_incident_without_evidence(seeded160,
                                                              monkeypatch):
    """Both resolvers ask the DATABASE whether the evidence exists."""
    from db import writers

    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    _detect("run1")

    a = writers.apply_post_boundary_sql_evidence(
        [], [], run_id="r", resolve_contact_ids=["jump"])
    b = writers.resolve_post_boundary_incidents(["jump"], resolved_by="history")
    assert a["incidents_resolved"] == 0 and b["persisted"] == 0
    assert _incident("jump")["status"] == "open"


@_needs_pg
def test_29_pg_reruns_never_duplicate_an_incident_or_an_event(
        seeded160, monkeypatch):
    _establish()
    _contact("jump", "opportunity")
    _contact("hist", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP),
                         "hist": _entry([_v("salesqualifiedlead", T_SQL),
                                         _v("opportunity", T_OPP)])})
    for n in range(3):
        assert _detect(f"run{n}")["ok"] is True
    counts = dict(_q("SELECT contact_id, count(*) FROM sql_post_boundary_incident "
                     "GROUP BY contact_id"))
    assert counts == {"jump": 1}
    assert len(_history_rows("hist")) == 1


@_needs_pg
def test_30_pg_a_failed_read_never_erases_the_shape_a_good_read_recorded(
        seeded160, monkeypatch):
    """A failed request is not evidence, so it cannot overwrite evidence."""
    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    _detect("run1")
    _serve(monkeypatch, {"jump": RuntimeError("HubSpot 503")})
    _detect("run2")

    inc = _incident("jump")
    assert inc["reason"] == boundary_svc.INCIDENT_HISTORY_UNREADABLE
    assert inc["stage_jump_skipped_sql"] is True
    assert inc["history_stage_path"] == "lead>opportunity"
    assert inc["last_checked_by_run_id"] == "run2"


# ═════════════════════════════════════════════════════════════════════════════
# §4 — the forensic audit (§4.2, §4.3, §8.9)
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_40_pg_the_audit_classifies_and_writes_nothing(seeded160, monkeypatch):
    """§8.9. Read-only proven by content fingerprint, and no PII emitted."""
    _establish()
    _contact("jump", "opportunity")
    _contact("stored", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP), "stored": _entry(JUMP)})
    _detect("run1")
    # Exact evidence lands, but the incident's closure is lost.
    _q("UPDATE hubspot_contact_funnel SET date_entered_sql = %s "
       "WHERE contact_id = 'stored'", (T_SQL,))

    def _no_hubspot(*a, **k):
        raise AssertionError("the local audit must not call HubSpot")

    monkeypatch.setattr(hubspot, "fetch_lifecycle_stage_history", _no_hubspot)
    monkeypatch.setattr(hubspot, "compare_sql_entry_evidence", _no_hubspot)
    monkeypatch.setattr(hubspot, "get_client", _no_hubspot)

    before = _snapshot()
    report = evidence_svc.audit()
    assert _snapshot() == before, "the audit changed the database"

    assert report["audit_complete"] is True
    assert report["hubspot_calls_performed"] == 0
    assert report["external_writes_performed"] == 0
    assert report["database_writes_performed"] == 0
    assert report["open_incidents"] == 2 and report["resolved_incidents"] == 0
    by_id = {i["contact_id"]: i for i in report["incidents"]}
    assert by_id["jump"]["classification"] == fx.C_STAGE_JUMP
    assert by_id["stored"]["classification"] == fx.C_STORED_EVIDENCE_OPEN
    assert report["root_cause"]["code_owned_losses"] == 1
    assert report["verdict"] == evidence_svc.V_CODE_LOSS
    assert report["exit_code"] == evidence_svc.EXIT_VIOLATION
    assert report["source_fresh"] is True
    assert report["lifecycle_event_publication"]["status"] == "withheld"
    assert report["acquisition_cohort_publication"]["freshness_gate"] == "passes"
    assert not (_PII_KEYS & set(_walk_keys(report)))
    json.dumps(report, default=str)


@_needs_pg
def test_41_pg_the_forensic_read_is_read_only_by_postgresql(seeded160):
    """The guarantee is the transaction mode, not the SQL after it."""
    import psycopg2

    from db.connection import get_conn
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        with get_conn() as c, c.cursor() as cur:
            cur.execute(repo.FORENSIC_TRANSACTION_MODE)
            cur.execute("DELETE FROM sql_post_boundary_incident")


def test_42_an_unreadable_store_is_unknown_never_zero(monkeypatch):
    monkeypatch.setattr(evidence_svc, "_source_freshness",
                        lambda: {"fresh": None, "reason": "x"})
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": {
                            "boundary_id": "b1", "observed_at": BOUNDARY}})
    monkeypatch.setattr(repo, "fetch_post_boundary_incident_forensics",
                        lambda: {"available": False, "rows": []})
    report = evidence_svc.audit()
    assert report["open_incidents"] is None
    assert report["exit_code"] == evidence_svc.EXIT_UNAVAILABLE

    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": False, "boundary": None})
    report = evidence_svc.audit()
    assert report["boundary_id"] is None and report["open_incidents"] is None
    assert report["exit_code"] == evidence_svc.EXIT_UNAVAILABLE


@_needs_pg
def test_43_pg_a_resolved_incident_without_evidence_is_a_contradiction(
        seeded160, monkeypatch):
    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    _detect("run1")
    _q("UPDATE sql_post_boundary_incident SET status = 'resolved' "
       "WHERE contact_id = 'jump'")

    report = evidence_svc.audit()
    assert report["integrity"]["resolved_without_stored_evidence"] == 1
    assert report["verdict"] == evidence_svc.V_INTEGRITY
    assert report["exit_code"] == evidence_svc.EXIT_VIOLATION


def _forensic_row(cid, **kw):
    row = {"contact_id": cid, "status": "open",
           "reason": boundary_svc.INCIDENT_HISTORY_NO_SQL,
           "detected_at": T_OPP, "contact_created_at": CREATED,
           "current_lifecycle_stage": "opportunity",
           "lifecycle_stage_at_detection": "opportunity",
           "funnel_row_present": True, "last_modified_at": T_OPP,
           "last_ingested_at": T_OPP, "direct_sql_entry_at": None,
           "recovered_sql_entry_at": None}
    row.update(kw)
    return row


def _stub_store(monkeypatch, rows):
    monkeypatch.setattr(evidence_svc, "_source_freshness",
                        lambda: {"fresh": True, "reason": "source_fresh"})
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": {
                            "boundary_id": "b1", "observed_at": BOUNDARY}})
    monkeypatch.setattr(repo, "fetch_post_boundary_incident_forensics",
                        lambda: {"available": True, "rows": rows})


def _cmp(direct=None, *, modified=T_OPP, set_at=None, versions=JUMP):
    return {"returned": True, "lifecycle_stage": "opportunity",
            "last_modified_at": modified,
            "direct_sql_entry": ({"state": "present", "value": direct}
                                 if direct else {"state": "absent",
                                                 "value": None}),
            "direct_sql_entry_set_at": set_at,
            "history_state": hubspot.HISTORY_PRESENT,
            "versions": list(versions)}


def test_44_the_comparison_classifies_against_hubspot_and_counts_its_calls(
        monkeypatch):
    rows = [_forensic_row(c) for c in ("cand", "late", "drop", "hist", "jump")]
    _stub_store(monkeypatch, rows)
    answers = {
        "cand": _cmp(T_SQL, modified=T_OPP + timedelta(hours=1)),
        "late": _cmp(T_SQL, set_at=T_OPP + timedelta(hours=1)),
        "drop": _cmp(T_SQL, set_at=T_OPP - timedelta(hours=1)),
        "hist": _cmp(versions=[_v("lead", T_LEAD),
                               _v("salesqualifiedlead", T_SQL)]),
        "jump": _cmp(),
    }
    calls: list = []

    def fake(ids, client=None):
        calls.append(list(ids))
        return {cid: answers[cid] for cid in ids}

    monkeypatch.setattr(hubspot, "compare_sql_entry_evidence", fake)
    report = evidence_svc.audit(compare_hubspot=True, client=object())

    by_id = {i["contact_id"]: i["classification"] for i in report["incidents"]}
    assert by_id == {"cand": fx.C_CANDIDATE_NOT_REFRESHED,
                     "late": fx.C_LATE_PROPERTY, "drop": fx.C_WRITER_DROPPED,
                     "hist": fx.C_HISTORY_EXACT_NOT_STORED,
                     "jump": fx.C_STAGE_JUMP}
    assert report["hubspot_calls_performed"] == len(calls) == 1
    assert report["root_cause"]["code_owned_losses"] == 4
    assert report["exit_code"] == evidence_svc.EXIT_VIOLATION
    comparison = next(i for i in report["incidents"]
                      if i["contact_id"] == "late")["comparison"]
    assert comparison["hubspot_direct_sql_entry_at"] == T_SQL.isoformat()


def test_45_a_sample_is_compared_as_a_sample_and_never_extrapolated(
        monkeypatch):
    rows = [_forensic_row(f"c{i}") for i in range(4)]
    _stub_store(monkeypatch, rows)
    monkeypatch.setattr(hubspot, "compare_sql_entry_evidence",
                        lambda ids, client=None: {c: _cmp() for c in ids})
    report = evidence_svc.audit(compare_hubspot=True, sample=2, client=object())
    assert report["compared_incidents"] == 2
    assert report["incidents_listed"] == 2
    assert "2 of 4" in report["root_cause_hubspot_comparison"]["denominator"]
    assert sum(report["root_cause"]["by_owner"].values()) == 4


def test_46_a_failed_comparison_is_unavailable_not_a_finding(monkeypatch):
    _stub_store(monkeypatch, [_forensic_row("c1")])

    def boom(ids, client=None):
        raise RuntimeError("HubSpot 503")

    monkeypatch.setattr(hubspot, "compare_sql_entry_evidence", boom)
    report = evidence_svc.audit(compare_hubspot=True, client=object())
    assert report["exit_code"] == evidence_svc.EXIT_UNAVAILABLE
    assert report["hubspot_calls_failed"] == 1


@_needs_pg
def test_47_pg_the_audit_cli_emits_json_and_its_exit_code(seeded160,
                                                         monkeypatch):
    from scripts import audit_post_boundary_sql_incidents as cli

    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    _detect("run1")

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["--json", "--sample", "1"])
    out = json.loads(buf.getvalue())
    assert code == cli.EXIT_OK == out["exit_code"]
    assert out["open_incidents"] == 1
    assert out["root_cause"]["source_unresolvable"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §5 — the repair command (§5.7)
# ═════════════════════════════════════════════════════════════════════════════

def _repair_world(monkeypatch):
    """Three open incidents; HubSpot later answers each differently."""
    _establish()
    for cid in ("direct", "hist", "jump"):
        _contact(cid, "opportunity")
    _serve(monkeypatch, {c: _entry(JUMP) for c in ("direct", "hist", "jump")})
    _detect("run1")
    _serve(monkeypatch, {
        "direct": _entry(JUMP, direct=T_SQL),
        "hist": _entry([_v("lead", T_LEAD), _v("salesqualifiedlead", T_SQL),
                        _v("opportunity", T_OPP)]),
        "jump": _entry(JUMP)})


@_needs_pg
def test_50_pg_the_dry_run_reports_and_writes_nothing(seeded160, monkeypatch):
    _repair_world(monkeypatch)
    before = _snapshot()
    report = evidence_svc.repair(client=object())
    assert _snapshot() == before
    assert report["mode"] == evidence_svc.MODE_DRY_RUN
    assert (report["examined"], report["recoverable_direct_property"],
            report["recoverable_lifecycle_history"], report["unresolved"]) == (
        3, 1, 1, 1)
    assert report["written"] == 0 and report["database_writes_performed"] == 0
    assert report["hubspot_writes_performed"] is False


@_needs_pg
def test_51_pg_apply_persists_with_provenance_and_is_idempotent(
        seeded160, monkeypatch):
    _repair_world(monkeypatch)
    report = evidence_svc.repair(apply=True, client=object(), run_id="rep1")

    assert report["status"] == evidence_svc.R_COMPLETE
    assert report["written"] == 2 and report["incidents_resolved"] == 2
    assert report["resolved_by"] == {"direct_property": 1, "history": 1}
    assert (report["open_before"], report["open_after"]) == (3, 1)
    assert _sql_date("direct") == T_SQL
    assert _history_rows("hist") == [("sql", T_SQL, "lifecyclestage", "rep1")]
    for cid in ("direct", "hist"):
        assert _incident(cid)["resolved_by_run_id"] == "rep1"
        assert _incident(cid)["resolution_evidence_at"] == T_SQL
    assert _sql_date("jump") is None and _incident("jump")["status"] == "open"

    again = evidence_svc.repair(apply=True, client=object(), run_id="rep2")
    assert (again["examined"], again["written"], again["incidents_resolved"]) \
        == (1, 0, 0)
    assert _incident("direct")["resolved_by_run_id"] == "rep1"


@_needs_pg
def test_52_pg_nothing_recoverable_is_reported_truthfully(seeded160,
                                                         monkeypatch):
    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    _detect("run1")
    report = evidence_svc.repair(apply=True, client=object())
    assert (report["recoverable_direct_property"],
            report["recoverable_lifecycle_history"], report["unresolved"],
            report["written"]) == (0, 0, 1, 0)
    assert report["exit_code"] == evidence_svc.EXIT_OK
    assert _sql_date("jump") is None


@_needs_pg
def test_53_pg_a_failed_apply_rolls_back_completely(seeded160, monkeypatch):
    _repair_world(monkeypatch)
    _q(_FAIL_ON_INCIDENT_UPDATE)
    before = _snapshot()
    report = evidence_svc.repair(apply=True, client=object())
    assert report["status"] == evidence_svc.R_FAILED
    assert report["written"] == 0 and report["incidents_resolved"] == 0
    assert report["exit_code"] == evidence_svc.EXIT_VIOLATION
    assert _snapshot() == before, "a failed apply left a partial write"


@_needs_pg
def test_54_pg_an_unreadable_contact_makes_the_run_partial_not_complete(
        seeded160, monkeypatch):
    _repair_world(monkeypatch)
    _serve(monkeypatch, {"direct": RuntimeError("HubSpot 503")})
    report = evidence_svc.repair(client=object())
    assert report["status"] == evidence_svc.R_PARTIAL
    assert report["contacts_unread"] == 3
    assert report["exit_code"] == evidence_svc.EXIT_VIOLATION


@_needs_pg
def test_55_pg_the_repair_cli_is_a_dry_run_unless_told_otherwise(
        seeded160, monkeypatch):
    from scripts import repair_post_boundary_sql_evidence as cli

    _repair_world(monkeypatch)
    monkeypatch.setattr(hubspot, "get_client", lambda: object())
    before = _snapshot()
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["--json"])
    out = json.loads(buf.getvalue())
    assert out["mode"] == "dry_run" and code == cli.EXIT_OK
    assert _snapshot() == before


# ═════════════════════════════════════════════════════════════════════════════
# §6 — counterfactuals: each protection, removed, lets its defect through
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_60_pg_a_substituted_sql_date_is_caught(seeded160, monkeypatch):
    """§9.1–9.4. The invariant holds on the real path and fails on each lie.

    Each mutation makes the detector treat a forbidden instant as the direct
    property. The guard — a stage-jump contact never acquires any SQL date —
    must hold on the real code and must fail under every mutation.
    """
    _establish()
    original = boundary_svc._consult_history

    def guard(cid):
        return _sql_date(cid) is None and _history_rows(cid) == []

    _contact("real", "opportunity")
    _serve(monkeypatch, {"real": _entry(JUMP)})
    _detect("real_run")
    assert guard("real"), "the real path must never date a stage jump"

    substitutes = {
        "contact_created_at": lambda r: CREATED,
        "boundary_time": lambda r: BOUNDARY,
        "sync_time": lambda r: _q(
            "SELECT last_ingested_at FROM hubspot_contact_funnel "
            "WHERE contact_id = %s", (r["contact_id"],))[0][0],
        "first_observed": lambda r: T_OPP,
    }
    for name, pick in substitutes.items():
        cid = f"m_{name}"
        _contact(cid, "opportunity")
        _serve(monkeypatch, {cid: _entry(JUMP)})

        def mutated(rows, *, direct_out=None, _pick=pick, **kw):
            recovered, incidents, n = original(rows, direct_out=direct_out,
                                               **kw)
            for row in rows:
                if direct_out is not None:
                    direct_out.append({"contact_id": row["contact_id"],
                                       "date_entered_sql": _pick(row)})
            return recovered, [], n

        monkeypatch.setattr(boundary_svc, "_consult_history", mutated)
        _detect(f"mut_{name}")
        monkeypatch.setattr(boundary_svc, "_consult_history", original)
        assert not guard(cid), f"substituting {name} went undetected"


@_needs_pg
def test_61_pg_erasing_a_stored_timestamp_is_caught(seeded160, monkeypatch):
    """§9.5. Plain latest-state semantics would blank the evidence."""
    from db import writers

    _contact("kept", "opportunity", sql=T_SQL)
    _contact("kept", "opportunity", modified=T_OPP, sql=None)
    assert _sql_date("kept") == T_SQL

    erasing = ",\n".join(f"{c} = EXCLUDED.{c}"
                         for c in writers._CONTACT_FUNNEL_COLUMNS
                         if c != "contact_id")
    monkeypatch.setattr(writers, "_CONTACT_FUNNEL_UPDATE_SET", erasing)
    _contact("kept", "opportunity", modified=T_OPP + timedelta(days=1), sql=None)
    assert _sql_date("kept") is None, "the mutation must reproduce the erasure"


@_needs_pg
def test_62_pg_closing_an_incident_without_evidence_is_caught(seeded160,
                                                             monkeypatch):
    """§9.6. Drop the evidence predicate and the audit sees the lie."""
    from db import writers

    _establish()
    _contact("fell", "opportunity")
    _serve(monkeypatch, {"fell": _entry(JUMP)})
    _detect("run1")
    _contact("fell", "lead", modified=T_OPP)
    _detect("run2")
    assert evidence_svc.audit()["integrity"][
        "resolved_without_stored_evidence"] == 0

    lax = writers._RESOLVE_WITH_STORED_EVIDENCE_SQL.replace(
        "AND COALESCE(f.date_entered_sql, h.entered_at) IS NOT NULL", "")
    assert lax != writers._RESOLVE_WITH_STORED_EVIDENCE_SQL
    monkeypatch.setattr(writers, "_RESOLVE_WITH_STORED_EVIDENCE_SQL", lax)
    _detect("run3")
    audit = evidence_svc.audit()
    assert audit["integrity"]["resolved_without_stored_evidence"] == 1
    assert audit["exit_code"] == evidence_svc.EXIT_VIOLATION


def test_63_treating_absence_as_no_transition_is_caught(monkeypatch):
    """§9.7. Collapse the reasons and the three-way distinction disappears."""
    rows = [{"contact_id": c, "created_at": CREATED,
             "lifecycle_stage": "opportunity"} for c in ("a", "b", "c")]
    answers = {"a": _entry(state=hubspot.HISTORY_PROPERTY_ABSENT),
               "b": _entry([_v("opportunity", T_OPP)])}
    _serve(monkeypatch, answers)

    def reasons():
        _, incidents, _ = boundary_svc._consult_history(
            rows, boundary_id="b1", client=None, budget=10, direct_out=[])
        return {i["contact_id"]: i["reason"] for i in incidents}

    assert len(set(reasons().values())) == 3
    monkeypatch.setattr(boundary_svc, "INCIDENT_HISTORY_ABSENT",
                        boundary_svc.INCIDENT_HISTORY_NO_SQL)
    assert len(set(reasons().values())) == 2, "the collapse must be visible"


@_needs_pg
def test_64_pg_skipping_the_late_refresh_is_caught(seeded160, monkeypatch):
    """§9.8. Without the direct read, a date HubSpot holds is never fetched."""
    _establish()
    original = boundary_svc._consult_history
    monkeypatch.setattr(
        boundary_svc, "_consult_history",
        lambda rows, *, direct_out=None, **kw: original(rows, direct_out=None,
                                                        **kw))
    _contact("late", "opportunity")
    _serve(monkeypatch, {"late": _entry(JUMP, direct=T_SQL)})
    _detect("run1")
    assert _sql_date("late") is None and _incident("late")["status"] == "open"

    monkeypatch.setattr(boundary_svc, "_consult_history", original)
    _detect("run2")
    assert _sql_date("late") == T_SQL and _incident("late")["status"] == \
        "resolved"


@_needs_pg
def test_65_pg_publishing_lifecycle_events_over_an_open_incident_is_caught(
        seeded160, monkeypatch):
    """§9.9. A reader blind to incidents turns the gate green."""
    from scripts import audit_sql_coverage_gate as gate_mod

    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    _detect("run1")

    def withheld():
        g, _ = gate_mod.run()
        return any("no_open_post_boundary_gaps" in v for v in g.violations)

    assert withheld()
    monkeypatch.setattr(repo, "fetch_post_boundary_incidents",
                        lambda **k: {"available": True, "rows": [],
                                     "open_count": 0})
    assert not withheld(), "the mutation must hide the incident"


@_needs_pg
def test_66_pg_withholding_the_cohort_for_an_incident_is_caught(
        seeded160, monkeypatch):
    """§9.10. The cohort's verdict must not read lifecycle-event incidents."""
    from services import marketing_outcome_cohort_service as cohort_svc

    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    _detect("run1")

    def published():
        out = cohort_svc.build_window_outcomes(
            BOUNDARY.date(), (BOUNDARY + timedelta(days=30)).date(),
            resolve_label=lambda label: ("unmatched", label))
        return out["sql_publication"][0] == cohort_svc.STATUS_PUBLISHED

    assert published()
    real = cohort_svc.sql_publication

    def global_withhold(**kw):
        if repo.fetch_post_boundary_incidents(status="open")["open_count"]:
            return cohort_svc.STATUS_WITHHELD, "open_post_boundary_incidents"
        return real(**kw)

    monkeypatch.setattr(cohort_svc, "sql_publication", global_withhold)
    assert not published(), "the mutation must withhold the cohort"

    # And structurally: the real verdict function never names an incident.
    src = Path(cohort_svc.__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "sql_publication")
    assert "incident" not in ast.get_source_segment(src, fn)


@_needs_pg
def test_67_pg_a_duplicating_writer_is_caught(seeded160, monkeypatch):
    """§9.11. An append-only incident writer cannot survive a rerun."""
    from db import writers
    from db.connection import get_conn

    _establish()
    _contact("jump", "opportunity")
    _serve(monkeypatch, {"jump": _entry(JUMP)})
    assert _detect("run1")["ok"] is True

    def appending(incidents, *, run_id):
        try:
            with get_conn() as c, c.cursor() as cur:
                for i in incidents:
                    cur.execute("INSERT INTO sql_post_boundary_incident "
                                "(contact_id, boundary_id, reason) "
                                "VALUES (%s, %s, %s)",
                                (i["contact_id"], i["boundary_id"],
                                 i["reason"]))
            return {"ok": True, "persisted": len(incidents)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "persisted": 0, "error": str(exc)}

    monkeypatch.setattr(writers, "record_post_boundary_incidents", appending)
    rerun = _detect("run2")
    assert rerun["ok"] is False, "the duplicate must be refused, not absorbed"
    assert _q("SELECT count(*) FROM sql_post_boundary_incident "
              "WHERE contact_id = 'jump'")[0][0] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §7 — existing contracts, and the wiring that proves these tests ran
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_70_pg_the_gate_still_holds_on_a_clean_system(seeded160):
    """§8.10 clean fixture — none of this PR's code trips the gate."""
    from scripts import audit_sql_coverage_gate as gate_mod

    _establish()
    g, report = gate_mod.run()
    assert g.violations == [], g.violations
    assert report["post_boundary_gaps"]["open"] == 0


def test_71_no_new_module_reads_the_boundary_bound():
    """`known_reached_sql_by` stays inside the gate's allow-list."""
    from scripts.audit_sql_coverage_gate import BOUND_COLUMN

    for rel in ("analysis/post_boundary_sql_forensics.py",
                "services/post_boundary_sql_evidence_service.py",
                "scripts/audit_post_boundary_sql_incidents.py",
                "scripts/repair_post_boundary_sql_evidence.py"):
        assert BOUND_COLUMN not in (_ROOT / rel).read_text(encoding="utf-8")


def test_72_the_audit_has_no_write_path():
    """Structural: the audit module never imports the writers."""
    src = (_ROOT / "services/post_boundary_sql_evidence_service.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    audit_fn = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "audit")
    names = {getattr(n, "module", None) for n in ast.walk(audit_fn)
             if isinstance(n, ast.ImportFrom)}
    imported = {a.name for n in ast.walk(audit_fn)
                if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "writers" not in imported and "db.writers" not in names


def test_73_ci_runs_this_suite_in_the_postgresql_step_and_asserts_it_ran():
    wf = (_ROOT / ".github/workflows/pr-ads-153d-checks.yml").read_text(
        encoding="utf-8")
    assert "tests/test_pr_ads_161c_post_boundary_sql_evidence.py" in wf
    assert '"tests.test_pr_ads_161c_post_boundary_sql_evidence"' in wf
