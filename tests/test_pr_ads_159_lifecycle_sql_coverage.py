"""PR-ADS-159 — Lifecycle SQL timestamp recovery, coverage bounds, read consistency.

The defect this suite exists for
--------------------------------
A production dry run examined 50 contacts and recovered 0 timestamps, reporting
``history_payload_missing`` 50 times. Read literally, that says the connected
HubSpot portal holds no ``lifecyclestage`` history at all.

It was not saying that. The batch request never asked for history.

``client.crm.contacts.batch_api.read(...)`` was handed a plain **dict** as its
body. The SDK's own ``sanitize_for_serialization`` documents the behaviour —
"If obj is dict, return the dict" — so ``attribute_map`` is applied only to
MODEL instances. The snake_case key ``properties_with_history`` therefore went
out on the wire, HubSpot's batch endpoint does not know that field, ignored it,
and answered with contacts carrying no history container. Every contact parsed
as "HubSpot returned no history", which is indistinguishable from "HubSpot holds
no history" unless something asks the question a second way.

    dict body  -> {'inputs', 'properties', 'properties_with_history'}
    model body -> {'inputs', 'properties', 'propertiesWithHistory'}   ← correct

The connector's docstring claimed the model "serializes properties_with_history
to propertiesWithHistory". True of the model, irrelevant to the code, which
never built one. That is the shape of the failure this suite is built around: a
parameter that exists on an object the call does not use is not a parameter that
was sent, and a docstring is not a wire format.

Three consequences are tested here:

1. the request must serialize the camelCase key — asserted against the SDK's
   real serializer, not against the source text;
2. a second, independent read path must exist, so "we got nothing" can be told
   apart from "there is nothing";
3. every canonical SQL read must share ONE effective stage-entry expression,
   because the headline coalesced recovered dates while the detail page and the
   operational counts did not — two published numbers that disagree by
   construction the moment a single timestamp is recovered.

What these tests will not accept
--------------------------------
No test here passes because a variable is named correctly. The serialization
test runs the SDK serializer. The consistency test parses the repository. The
window-bound tests assert the boundary DAY, because an off-by-one there would
silently rule a contact out of a window it might genuinely belong to.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import tests.conftest as conftest  # noqa: E402,F401  (import-order guard)
import analysis.crm_lifecycle as lifecycle  # noqa: E402
import analysis.lifecycle_sql_coverage as coverage  # noqa: E402
import connectors.hubspot_pull as hubspot  # noqa: E402
import db.crm_funnel_repository as repo  # noqa: E402
import services.lifecycle_history_recovery_service as recovery  # noqa: E402

_REPO_FILE = _ROOT / "db" / "crm_funnel_repository.py"


# ─────────────────────────────────────────────────────────────────────────────
# Fakes — a HubSpot client narrow enough to control, wide enough to be honest
# ─────────────────────────────────────────────────────────────────────────────

class _ApiError(Exception):
    """Stands in for the SDK's ApiException, which carries a `.status`."""

    def __init__(self, status):
        super().__init__(f"status={status}")
        self.status = status


def _version(value, timestamp, *, source_type="CRM_UI"):
    return {"value": value, "timestamp": timestamp, "sourceType": source_type,
            "sourceId": "u1", "sourceLabel": None, "updatedByUserId": None}


def _record(contact_id, versions=None, *, container=True, key="lifecyclestage"):
    """One batch/individual result. ``container=False`` omits history entirely."""
    record = {"id": contact_id, "properties": {"lifecyclestage": "customer"}}
    if container:
        record["properties_with_history"] = {key: list(versions or [])}
    return record


class _Response:
    def __init__(self, results):
        self.results = results


class _FakeBatchApi:
    def __init__(self, outer):
        self._outer = outer

    def read(self, batch_read_input_simple_public_object_id=None, **_kw):
        body = batch_read_input_simple_public_object_id
        self._outer.batch_bodies.append(body)
        if self._outer.batch_error is not None:
            raise self._outer.batch_error
        return _Response(self._outer.batch_results)


class _FakeBasicApi:
    def __init__(self, outer):
        self._outer = outer

    def get_by_id(self, contact_id, properties=None,
                  properties_with_history=None, **_kw):
        self._outer.individual_calls.append(
            (contact_id, tuple(properties_with_history or ())))
        if self._outer.individual_error is not None:
            raise self._outer.individual_error
        record = self._outer.individual_results.get(contact_id)
        if record is None:
            raise _ApiError(404)
        return record


class _FakeContacts:
    def __init__(self, outer):
        self.batch_api = _FakeBatchApi(outer)
        self.basic_api = _FakeBasicApi(outer)


class _FakeCrm:
    def __init__(self, outer):
        self.contacts = _FakeContacts(outer)


class FakeClient:
    """A HubSpot client double wide enough to answer BOTH read paths.

    A double narrower than the interface it stands in for fails on the CALL
    rather than on the behaviour under test, so the assertions never run. Both
    APIs are present here even when a test exercises only one.
    """

    def __init__(self, *, batch_results=None, individual_results=None,
                 batch_error=None, individual_error=None):
        self.batch_results = list(batch_results or [])
        self.individual_results = dict(individual_results or {})
        self.batch_error = batch_error
        self.individual_error = individual_error
        self.batch_bodies: list = []
        self.individual_calls: list = []
        self.crm = _FakeCrm(self)


@pytest.fixture(autouse=True)
def _no_real_api_exception(monkeypatch):
    """Make the connector treat our fake error as the SDK's ApiException."""
    monkeypatch.setattr(hubspot, "ApiException", _ApiError, raising=False)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the request actually asks for history
# ═════════════════════════════════════════════════════════════════════════════

def test_01_batch_body_serializes_the_camelcase_history_key():
    """THE defect, asserted against the SDK's own serializer.

    Not against the source text and not against the model's attribute_map — the
    question is what goes on the wire, and only the serializer answers it.
    """
    from hubspot.crm.contacts.api_client import ApiClient

    body = hubspot._batch_history_body(["1", "2"])
    wire = ApiClient().sanitize_for_serialization(body)

    assert "propertiesWithHistory" in wire, (
        "the batch body must ask for propertiesWithHistory; a snake_case key is "
        "silently ignored by HubSpot and history is never returned")
    assert "properties_with_history" not in wire
    assert wire["propertiesWithHistory"] == ["lifecyclestage"]
    assert [i["id"] for i in wire["inputs"]] == ["1", "2"]


def test_02_a_plain_dict_body_would_not_ask_for_history():
    """The negative control: prove the old form really was broken.

    Without this, test 01 only shows the new code works — not that the old code
    did not, which is the claim the whole PR rests on.
    """
    from hubspot.crm.contacts.api_client import ApiClient

    old_style = {"inputs": [{"id": "1"}], "properties": ["lifecyclestage"],
                 "properties_with_history": ["lifecyclestage"]}
    wire = ApiClient().sanitize_for_serialization(old_style)

    assert "propertiesWithHistory" not in wire
    assert "properties_with_history" in wire


def test_03_the_diagnostic_reports_the_real_request_key():
    """The diagnostic must read the key off the body, not describe intent."""
    assert hubspot._batch_history_request_key() == "propertiesWithHistory"


# ═════════════════════════════════════════════════════════════════════════════
# §1 tests 1–8 — the eight required connector cases
# ═════════════════════════════════════════════════════════════════════════════

def test_04_batch_response_contains_usable_history():
    """Case 1. History present → versions parsed, state PRESENT."""
    ts = datetime(2026, 3, 1, tzinfo=timezone.utc)
    client = FakeClient(batch_results=[
        _record("c1", [_version("salesqualifiedlead", ts)])])

    out = hubspot.fetch_lifecycle_stage_history(["c1"], client=client)

    assert out["c1"]["state"] == hubspot.HISTORY_PRESENT
    assert out["c1"]["via"] == hubspot.HISTORY_VIA_BATCH
    assert out["c1"]["versions"][0]["value"] == "salesqualifiedlead"
    assert out["c1"]["versions"][0]["timestamp"] == ts


def test_05_batch_response_omits_the_history_container():
    """Case 2. Contact returned, no history container → PROPERTY_ABSENT.

    This is the state 50 production contacts landed in. It means "no history
    came back", NOT "no history exists" — the distinction the individual read
    below exists to settle.
    """
    client = FakeClient(batch_results=[_record("c1", container=False)])

    out = hubspot.fetch_lifecycle_stage_history(["c1"], client=client)

    assert out["c1"]["state"] == hubspot.HISTORY_PROPERTY_ABSENT
    assert out["c1"]["versions"] == []


def test_06_individual_read_provides_history_when_batch_does_not():
    """Case 3. The asymmetry that proves the request, not the portal, was wrong."""
    ts = datetime(2026, 4, 2, tzinfo=timezone.utc)
    client = FakeClient(
        batch_results=[_record("c1", container=False)],
        individual_results={"c1": _record("c1", [_version("salesqualifiedlead", ts)])})

    batch = hubspot.fetch_lifecycle_stage_history(["c1"], client=client)
    single = hubspot.fetch_lifecycle_stage_history_single("c1", client=client)

    assert batch["c1"]["state"] == hubspot.HISTORY_PROPERTY_ABSENT
    assert single["state"] == hubspot.HISTORY_PRESENT
    assert single["via"] == hubspot.HISTORY_VIA_INDIVIDUAL
    assert client.individual_calls == [("c1", ("lifecyclestage",))], (
        "the individual read must ask for the history property by name")

    diagnosis = hubspot.diagnose_lifecycle_history_reads(["c1"], client=client)
    assert diagnosis["verdict"] == "individual_only_batch_returns_no_history"


def test_07_both_paths_provide_no_history():
    """Case 4. Then the portal really does hold nothing for this contact."""
    client = FakeClient(batch_results=[_record("c1", [])],
                        individual_results={"c1": _record("c1", [])})

    diagnosis = hubspot.diagnose_lifecycle_history_reads(["c1"], client=client)

    assert diagnosis["verdict"] == "neither_path_returns_history"
    assert diagnosis["batch"]["states"] == {hubspot.HISTORY_EMPTY: 1}
    assert diagnosis["individual"]["states"] == {hubspot.HISTORY_EMPTY: 1}


def test_08_contact_is_not_returned():
    """Case 5. Asked and got no record — an identity question, not retention."""
    client = FakeClient(batch_results=[])

    out = hubspot.fetch_lifecycle_stage_history(["c1", "c2"], client=client)

    assert out["c1"]["state"] == hubspot.HISTORY_CONTACT_ABSENT
    assert out["c2"]["state"] == hubspot.HISTORY_CONTACT_ABSENT

    single = hubspot.fetch_lifecycle_stage_history_single("missing", client=client)
    assert single["state"] == hubspot.HISTORY_CONTACT_ABSENT, (
        "a 404 is HubSpot answering, and the answer is that the contact is gone")


def test_09_http_request_fails():
    """Case 6. A failure is raised, never returned as an empty result."""
    client = FakeClient(batch_error=_ApiError(500))

    with pytest.raises(hubspot.HubSpotRetryableError):
        hubspot.fetch_lifecycle_stage_history(["c1"], client=client)

    diagnosis = hubspot.diagnose_lifecycle_history_reads(["c1"], client=client)
    assert diagnosis["batch"]["outcome"] == "request_failed"
    assert "states" not in diagnosis["batch"], (
        "a failed request must publish no per-state counts — it proved nothing")


def test_10_history_exists_but_contains_no_sql_transition():
    """Case 7. Real evidence that the transition was never recorded."""
    row = {"contact_id": "c1", "lifecycle_stage": "customer",
           "date_entered_sql": None}
    versions = [_version("marketingqualifiedlead",
                         datetime(2026, 1, 1, tzinfo=timezone.utc))]

    found, unresolved = recovery.select_recovered_events(row, versions,
                                                         (lifecycle.EVENT_SQL,))

    assert found == []
    assert unresolved[0]["reason"] == recovery.HISTORY_PRESENT_NO_SQL_STAGE


def test_11_sql_history_version_has_no_valid_timestamp():
    """Case 8, split in two — the states must not be collapsed.

    A version carrying NO timestamp and a version carrying an unparseable one
    are different defects: the first is HubSpot recording a change without a
    time, the second is us failing to read a value that is there.
    """
    row = {"contact_id": "c1", "lifecycle_stage": "customer",
           "date_entered_sql": None}

    absent = [{"value": "salesqualifiedlead", "timestamp": None,
               "timestamp_raw": None}]
    _, unresolved = recovery.select_recovered_events(row, absent,
                                                     (lifecycle.EVENT_SQL,))
    assert unresolved[0]["reason"] == recovery.HISTORY_SQL_VERSION_NO_TIMESTAMP

    malformed = [{"value": "salesqualifiedlead", "timestamp": None,
                  "timestamp_raw": "not-a-date"}]
    _, unresolved = recovery.select_recovered_events(row, malformed,
                                                     (lifecycle.EVENT_SQL,))
    assert unresolved[0]["reason"] == recovery.HISTORY_SQL_TIMESTAMP_INVALID


def test_12_a_recovered_sql_timestamp_is_hubspots_own_value():
    """Nothing is computed, interpolated or rounded on the way through."""
    ts = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    row = {"contact_id": "c1", "lifecycle_stage": "opportunity",
           "date_entered_sql": None}

    found, unresolved = recovery.select_recovered_events(
        row, [_version("salesqualifiedlead", ts)], (lifecycle.EVENT_SQL,))

    assert unresolved == []
    assert found[0]["entered_at"] == ts
    assert found[0]["evidence_state"] == recovery.HISTORY_SQL_TIMESTAMP_RECOVERED
    assert found[0]["hubspot_property"] == "lifecyclestage"


# ═════════════════════════════════════════════════════════════════════════════
# §2 — the evidence vocabulary
# ═════════════════════════════════════════════════════════════════════════════


def test_14_contact_not_returned_no_longer_collapses_into_payload_missing():
    """The two were one number. They are two different follow-ups."""
    mapping = recovery._PAYLOAD_STATE_REASON

    assert mapping[hubspot.HISTORY_CONTACT_ABSENT] == \
        recovery.HISTORY_CONTACT_NOT_RETURNED
    assert mapping[hubspot.HISTORY_PROPERTY_ABSENT] == \
        recovery.HISTORY_PAYLOAD_MISSING
    assert mapping[hubspot.HISTORY_CONTACT_ABSENT] != \
        mapping[hubspot.HISTORY_PROPERTY_ABSENT]


def test_15_an_unknown_payload_state_is_reported_as_itself():
    """Never folded into the nearest familiar reason."""
    assert recovery._PAYLOAD_STATE_REASON.get("something_new") is None
    assert recovery.evidence_state("sql", "something_new") == "something_new"


def test_16_non_sql_events_keep_the_generic_vocabulary():
    """SQL mode renames SQL states only; the all-stage run is unchanged."""
    assert recovery.evidence_state("mql", recovery.NO_HISTORY_VERSION) == \
        recovery.NO_HISTORY_VERSION
    assert recovery.evidence_state("sql", recovery.NO_HISTORY_VERSION) == \
        recovery.HISTORY_PRESENT_NO_SQL_STAGE


# ═════════════════════════════════════════════════════════════════════════════
# §3 — SQL-specific candidates
# ═════════════════════════════════════════════════════════════════════════════

def test_17_sql_mode_ignores_gaps_in_other_stages():
    """A contact missing only its lead date is not an SQL candidate.

    Fetching it would spend a HubSpot request that cannot resolve an SQL
    question, and its outcome would be counted in an SQL coverage report.
    """
    row = {"contact_id": "c1", "lifecycle_stage": "customer",
           "date_entered_lead": None, "date_entered_mql": None,
           "date_entered_sql": datetime(2026, 1, 1, tzinfo=timezone.utc),
           "date_entered_opportunity": None, "date_entered_customer": None}

    assert recovery.missing_events(row, (lifecycle.EVENT_SQL,)) == []
    assert "lead" in recovery.missing_events(row)


def test_18_stages_implying_sql_are_derived_from_the_rank_doctrine():
    """One source for "reached SQL", used in Python and in SQL alike."""
    stages = lifecycle.stages_implying_event(lifecycle.EVENT_SQL)

    assert "salesqualifiedlead" in stages
    assert "customer" in stages and "opportunity" in stages
    assert "lead" not in stages and "marketingqualifiedlead" not in stages
    assert "subscriber" not in stages




# ═════════════════════════════════════════════════════════════════════════════
# §4 — bounded fallback and safety
# ═════════════════════════════════════════════════════════════════════════════

def test_21_an_authentication_failure_stops_the_run():
    """403 must not be counted as N contacts without history.

    That is exactly the false conclusion PR-ADS-159 §1 was built on, arriving
    through a different door.
    """
    assert recovery._is_permanent_auth_failure(_ApiError(403))
    assert recovery._is_permanent_auth_failure(_ApiError(401))
    assert recovery._is_permanent_auth_failure(
        hubspot.HubSpotRetryableError("read failed (status=401)"))
    assert not recovery._is_permanent_auth_failure(_ApiError(500))
    assert not recovery._is_permanent_auth_failure(_ApiError(429))


def test_22_a_rate_limited_individual_read_is_retried_then_succeeds(monkeypatch):
    """429 is transient; the backoff must not be a busy loop."""
    slept: list = []
    monkeypatch.setattr(hubspot.time, "sleep", slept.append)

    calls = {"n": 0}
    ts = datetime(2026, 2, 2, tzinfo=timezone.utc)

    class _Flaky(FakeClient):
        pass

    client = _Flaky(individual_results={
        "c1": _record("c1", [_version("salesqualifiedlead", ts)])})
    original = client.crm.contacts.basic_api.get_by_id

    def _get(contact_id, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ApiError(429)
        return original(contact_id, **kw)

    client.crm.contacts.basic_api.get_by_id = _get

    out = hubspot.fetch_lifecycle_stage_history_single("c1", client=client)

    assert out["state"] == hubspot.HISTORY_PRESENT
    assert calls["n"] == 2
    assert slept and slept[0] > 0, "a retry must actually back off"


def test_23_a_permanent_individual_failure_is_not_retried_forever(monkeypatch):
    monkeypatch.setattr(hubspot.time, "sleep", lambda *_a: None)
    client = FakeClient(individual_error=_ApiError(403))

    with pytest.raises(hubspot.HubSpotRetryableError):
        hubspot.fetch_lifecycle_stage_history_single("c1", client=client)

    assert len(client.individual_calls) == 1, (
        "a permission failure must not be retried — it cannot start working")



def test_25_the_error_message_carries_no_contact_identifier():
    """A raised connector error must not leak the record it was reading."""
    client = FakeClient(individual_error=_ApiError(500))

    with pytest.raises(hubspot.HubSpotRetryableError) as caught:
        hubspot.fetch_lifecycle_stage_history_single("contact-secret-42",
                                                     client=client)

    assert "contact-secret-42" not in str(caught.value)


# ═════════════════════════════════════════════════════════════════════════════
# §5 — one effective SQL-entry expression
# ═════════════════════════════════════════════════════════════════════════════

def _function_source(name: str, *, module: str = "repo") -> str:
    path = (_REPO_FILE if module == "repo"
            else _ROOT / "services" / "lifecycle_history_recovery_service.py")
    src = path.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in {path.name}")


#: Every canonical read that decides which contacts are in a window.
_CANONICAL_READS = (
    "fetch_funnel_contacts",
    "fetch_funnel_contact_page",
    "fetch_operational_status_counts",
    "fetch_sql_recovery_candidates",
    "fetch_sql_coverage_population",
    "fetch_unresolved_sql_created_at_bounds",
)


@pytest.mark.parametrize("name", _CANONICAL_READS)
def test_26_every_canonical_read_uses_the_shared_effective_expression(name):
    """The drift guard.

    ``fetch_funnel_contact_page`` and ``fetch_operational_status_counts`` used
    the BARE column while the headline coalesced recovered history. A contact
    recovered from lifecycle history was counted in the headline and missing
    from the page that is supposed to enumerate it.
    """
    src = _function_source(name)
    uses_shared = ("effective_date_sql(" in src
                   or "_effective_date_sql(" in src)
    assert uses_shared, (
        f"{name} must filter on the shared effective stage-entry expression")


def test_27_no_canonical_read_builds_its_own_coalesce():
    """One expression, not several that happen to agree today."""
    for name in _CANONICAL_READS:
        src = _function_source(name)
        assert "COALESCE(f.date_entered" not in src, (
            f"{name} spells out its own COALESCE instead of using the helper")


def test_28_the_precedence_is_direct_then_recovered_then_null():
    expr = repo.effective_date_sql(lifecycle.EVENT_SQL)

    assert expr == ("COALESCE(f.date_entered_sql, h.recovered_date_entered_sql)")
    assert repo.direct_date_sql(lifecycle.EVENT_SQL) == "f.date_entered_sql"
    assert repo.recovered_date_sql(lifecycle.EVENT_SQL) == \
        "h.recovered_date_entered_sql"
    assert expr.index("f.date_entered_sql") < expr.index("h.recovered"), (
        "the direct HubSpot property must win; recovery fills a gap and never "
        "overrides a fact")


def test_29_the_effective_expression_refuses_an_unknown_event():
    for fn in (repo.effective_date_sql, repo.direct_date_sql,
               repo.recovered_date_sql):
        with pytest.raises(ValueError):
            fn("not_a_funnel_event")


def test_30_the_contact_page_projects_the_recovered_date_and_flags_it():
    """A page that filters on the effective date must also SHOW it.

    Otherwise a recovered contact appears in a window with a blank date beside
    it, and the row looks like the bug rather than the fix.
    """
    select = repo._contact_page_select()

    assert "COALESCE(f.date_entered_sql, h.recovered_date_entered_sql) " \
           "AS date_entered_sql" in select
    assert "AS date_entered_sql_from_history" in select


# ═════════════════════════════════════════════════════════════════════════════
# §6 — global gaps versus per-window membership
# ═════════════════════════════════════════════════════════════════════════════

def _undated(contact_id, created_at):
    return {"contact_id": contact_id, "created_at": created_at}


def test_31_creation_after_a_window_disproves_membership():
    """The ONE sound implication. A contact cannot enter SQL before it exists."""
    created = datetime(2026, 3, 1, tzinfo=timezone.utc)

    assert coverage.membership_verdict(created, date(2026, 1, 31)) == \
        "proven_outside"
    assert coverage.membership_verdict(created, date(2026, 12, 31)) == \
        "unresolved"


def test_32_the_window_end_day_is_inclusive():
    """Off by one here would rule a contact out of a window it may belong to.

    The repository's own predicate is ``< end + INTERVAL '1 day'``, so a contact
    created at any point ON the end date is still inside the window.
    """
    end = date(2026, 1, 31)

    last_moment = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)
    first_after = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)

    assert coverage.membership_verdict(last_moment, end) == "unresolved"
    assert coverage.membership_verdict(first_after, end) == "proven_outside"


def test_33_an_unknown_creation_time_rules_the_contact_out_of_nothing():
    assert coverage.membership_verdict(None, date(2020, 1, 1)) == "unresolved"


def test_34_an_open_ended_window_can_never_rule_anything_out():
    """All Time has no end, so no undated contact can be excluded from it."""
    created = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert coverage.membership_verdict(created, None) == "unresolved"


def test_35_creation_before_the_window_end_proves_nothing_either_way():
    """It does not put the contact IN the window; it only fails to rule it out."""
    rows = [_undated("a", datetime(2020, 1, 1, tzinfo=timezone.utc))]

    block = coverage.window_coverage(
        window="q", window_end=date(2026, 6, 30), confirmed_sqls=5,
        recovered_sqls=0, unresolved_rows=rows)

    assert block["window_membership_unresolved"] == 1
    assert block["confirmed_sqls"] == 5, (
        "the undated contact is NOT added to the confirmed subset")
    assert block["complete_sql_total"] is None


def test_36_the_global_gap_is_reported_once_not_once_per_window():
    """The 525 were counted against all eleven windows. One population, one count."""
    rows = [_undated(str(i), datetime(2026, 9, 1, tzinfo=timezone.utc))
            for i in range(525)]

    early = coverage.window_coverage(window="2024", window_end=date(2024, 12, 31),
                                     confirmed_sqls=10, recovered_sqls=0,
                                     unresolved_rows=rows)
    late = coverage.window_coverage(window="all_time", window_end=None,
                                    confirmed_sqls=99, recovered_sqls=0,
                                    unresolved_rows=rows)

    assert early["global_missing_sql_entry_date"] == 525
    assert late["global_missing_sql_entry_date"] == 525
    # …but they affect the two windows completely differently.
    assert early["window_membership_unresolved"] == 0
    assert early["window_membership_proven_outside"] == 525
    assert late["window_membership_unresolved"] == 525


def test_37_every_window_explains_why_completeness_is_or_is_not_proven():
    rows = [_undated("a", datetime(2026, 9, 1, tzinfo=timezone.utc))]

    complete = coverage.window_coverage(window="2024", window_end=date(2024, 12, 31),
                                        confirmed_sqls=3, recovered_sqls=1,
                                        unresolved_rows=rows)
    incomplete = coverage.window_coverage(window="ytd", window_end=date(2026, 12, 31),
                                          confirmed_sqls=3, recovered_sqls=1,
                                          unresolved_rows=rows)

    for block in (complete, incomplete):
        assert block["explanation"].strip(), "a window must always say why"
        assert block["reason"] in (coverage.COVERAGE_COMPLETE,
                                   coverage.COVERAGE_UNRESOLVED_MEMBERSHIP)
    assert "created after this window ended" in complete["explanation"]
    assert "cannot be disproven" in incomplete["explanation"]


def test_38_no_monotonic_stage_ordering_is_used_anywhere():
    """"It reached opportunity, so it must have reached SQL earlier" is not used.

    It is a real implication only in a portal that never skips stages and never
    back-fills, and this repository holds no tested evidence that this is such a
    portal. Until it does, the inference is unavailable.
    """
    src = (_ROOT / "analysis" / "lifecycle_sql_coverage.py").read_text()
    code = ast.unparse(ast.parse(src))

    for forbidden in ("date_entered_opportunity", "date_entered_customer",
                      "date_entered_mql", "STAGE_RANK"):
        assert forbidden not in code, (
            f"{forbidden} appears in executable code — window membership must "
            "not be inferred from a neighbouring stage's date")


def test_39_contact_creation_never_becomes_the_sql_event_date():
    """Creation is a lower bound used to DISPROVE membership. Nothing else."""
    rows = [_undated("a", datetime(2026, 1, 15, tzinfo=timezone.utc))]

    block = coverage.window_coverage(window="jan", window_end=date(2026, 1, 31),
                                     confirmed_sqls=7, recovered_sqls=0,
                                     unresolved_rows=rows)

    assert block["confirmed_sqls"] == 7
    assert block["complete_sql_total"] is None
    assert block["window_membership_unresolved"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §7 — publication stays fail-closed
# ═════════════════════════════════════════════════════════════════════════════

def test_40_an_unproven_window_withholds_the_total_and_cpql():
    rows = [_undated("a", datetime(2026, 1, 1, tzinfo=timezone.utc))]

    block = coverage.window_coverage(window="ytd", window_end=date(2026, 12, 31),
                                     confirmed_sqls=42, recovered_sqls=2,
                                     unresolved_rows=rows)

    assert block["complete_sql_total"] is None, "never zero, never the subset"
    assert block["cpql_publishable"] is False
    assert block["window_total_complete"] is False
    assert block["confirmed_sqls"] == 42, (
        "the confirmed subset may still be published AS a subset")


def test_41_a_proven_window_publishes_the_total_and_cpql():
    rows = [_undated("a", datetime(2026, 9, 1, tzinfo=timezone.utc))]

    block = coverage.window_coverage(window="2024", window_end=date(2024, 12, 31),
                                     confirmed_sqls=11, recovered_sqls=0,
                                     unresolved_rows=rows)

    assert block["window_total_complete"] is True
    assert block["complete_sql_total"] == 11
    assert block["cpql_publishable"] is True


def test_42_an_unavailable_population_is_not_a_complete_window():
    """A population that could not be read is unknown, not empty."""
    block = coverage.window_coverage(window="30d", window_end=date(2026, 1, 31),
                                     confirmed_sqls=0, recovered_sqls=0,
                                     unresolved_rows=[],
                                     population_available=False)

    assert block["window_total_complete"] is False
    assert block["complete_sql_total"] is None
    assert block["cpql_publishable"] is False
    assert block["global_missing_sql_entry_date"] is None, (
        "an unreadable population must not report zero missing dates")
    assert block["reason"] == coverage.COVERAGE_POPULATION_UNAVAILABLE


def test_43_zero_undated_contacts_is_a_complete_window():
    block = coverage.window_coverage(window="30d", window_end=date(2026, 1, 31),
                                     confirmed_sqls=8, recovered_sqls=3,
                                     unresolved_rows=[])

    assert block["window_total_complete"] is True
    assert block["complete_sql_total"] == 8
    assert "complete" in block["explanation"]


# ═════════════════════════════════════════════════════════════════════════════
# §8 — the coverage audit command
# ═════════════════════════════════════════════════════════════════════════════

def _run_audit(env_extra=None, args=("--json",)):
    """Run the audit as a SUBPROCESS.

    In-process, this test's own pool and imports would decide the outcome. The
    command's contract is that it initialises its own pool and exits with a code
    an operator can act on.
    """
    import os

    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "scripts.audit_lifecycle_sql_coverage", *args],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)


def test_44_audit_exits_2_when_the_database_is_unavailable():
    """Unavailable is exit 2 — never 1, and never 0.

    "The data is unreachable" and "the code contradicts its doctrine" lead an
    operator to opposite actions.
    """
    result = _run_audit({"DATABASE_URL": ""})

    assert result.returncode == 2, result.stdout[-2000:]
    report = json.loads(result.stdout)
    assert report["violations"] == []
    assert report["audit_complete"] is False
    assert report["coverage_complete"] is False


def test_45_audit_never_reports_writes():
    result = _run_audit({"DATABASE_URL": ""})
    report = json.loads(result.stdout)

    assert report["external_writes_performed"] is False
    assert report["hubspot_calls_performed"] is False


def test_46_audit_complete_and_coverage_complete_are_different_questions():
    """The audit must not require complete data in order to produce a report."""
    src = (_ROOT / "scripts" / "audit_lifecycle_sql_coverage.py").read_text()

    assert '"coverage_complete"' in src and '"audit_complete"' in src
    assert "EXIT_COVERAGE_INCOMPLETE = 3" in src
    # Strict mode is the ONLY thing that turns incomplete coverage into a
    # non-zero exit; the default report must still be produced.
    assert "args.strict and not report[\"coverage_complete\"]" in src


def test_47_the_audit_makes_no_external_api_call():
    """Read-only over the local database. Nothing here contacts HubSpot."""
    src = (_ROOT / "scripts" / "audit_lifecycle_sql_coverage.py").read_text()
    code = ast.unparse(ast.parse(src))

    # Narrowed by PR-ADS-159-R5: the audit now imports the canonical funnel
    # service, whose SCOPE CONSTANTS legitimately contain "google_ads"
    # (`SCOPE_GOOGLE_ADS_SOURCE`). A guard that fires on a constant's NAME stops
    # being read as a guard. What must be absent are the client entry points.
    for forbidden in ("hubspot_pull", "get_client", "google_ads_client",
                      "googleads", "mailchimp_client", "requests.get",
                      "requests.post", "httpx."):
        assert forbidden not in code, (
            f"{forbidden} is reachable from the audit — it must read only the "
            "local database")


def test_48_the_audit_lists_the_readers_it_certifies():
    """A hand-maintained list that silently shrinks stops being a guard."""
    from scripts import audit_lifecycle_sql_coverage as audit

    listed = set(audit._EFFECTIVE_DATE_READERS)
    assert {"fetch_funnel_contact_page", "fetch_operational_status_counts"} <= listed, (
        "the two reads that actually drifted must be certified by name")
    for name in listed:
        assert _function_source(name), f"{name} is listed but does not exist"


def test_49_the_doctrine_inventory_still_discloses_incomplete_migration():
    """PR-ADS-159 does not migrate consumers and must not claim to."""
    src = (_ROOT / "scripts" / "audit_sql_doctrine_inventory.py").read_text()
    registry = (_ROOT / "analysis" / "sql_doctrine_registry.py").read_text()

    assert "legacy" in src.lower()
    assert "migration" in registry.lower() or "migrated" in registry.lower()


# ═════════════════════════════════════════════════════════════════════════════
# §9 — no HubSpot write path exists anywhere in the recovery surface
# ═════════════════════════════════════════════════════════════════════════════

def test_50_the_recovery_service_declares_and_performs_no_hubspot_write():
    src = (_ROOT / "services" / "lifecycle_history_recovery_service.py").read_text()
    code = ast.unparse(ast.parse(src))

    for forbidden in ("basic_api.update", "batch_api.update", "batch_api.create",
                      "basic_api.create", "basic_api.archive"):
        assert forbidden not in code, f"{forbidden} is a HubSpot WRITE"


#: HubSpot SDK write verbs, scoped to the SDK's own API objects.
#:
#: The pattern is `_api.<verb>(`, not `.<verb>(`. An unscoped `.update(` also
#: matches `some_local_dict.update(...)`, which is not a HubSpot call at all —
#: a guard that fires on a local dictionary mutation stops being read as a
#: guard, and then a real write slips past it.
_HUBSPOT_WRITE_CALLS = ("_api.update(", "_api.create(", "_api.archive(",
                        "_api.merge(", "_api.update_batch(")


def test_51_the_connector_history_reads_are_reads():
    src = (_ROOT / "connectors" / "hubspot_pull.py").read_text()
    for name in ("fetch_lifecycle_stage_history",
                 "fetch_lifecycle_stage_history_single",
                 "diagnose_lifecycle_history_reads"):
        fn = None
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                fn = ast.unparse(node)
        assert fn is not None, f"{name} not found"
        for forbidden in _HUBSPOT_WRITE_CALLS:
            assert forbidden not in fn, f"{name} contains {forbidden}"


def test_52_the_write_guard_actually_fires():
    """A guard nobody has seen fail is not known to be a guard."""
    sample = "def f():\n    client.crm.contacts.basic_api.update(x)\n"
    fn = ast.unparse(ast.parse(sample))

    assert any(w in fn for w in _HUBSPOT_WRITE_CALLS), (
        "the write-call patterns must match a real SDK write")
    # …and must NOT match an ordinary dictionary mutation.
    benign = ast.unparse(ast.parse("def f():\n    out['a'].update(b=1)\n"))
    assert not any(w in benign for w in _HUBSPOT_WRITE_CALLS)


# ═════════════════════════════════════════════════════════════════════════════
# §10 — against a real PostgreSQL server
# ═════════════════════════════════════════════════════════════════════════════
#
# Everything above reasons about expressions and pure functions. The drift this
# PR fixes is only OBSERVABLE once a timestamp is actually recovered: until a
# row exists in the recovery table, the bare column and the coalesced
# expression return identical results and the two reads agree by accident.

from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

#: Inside every window from 7d up.
RECENT = date.today() - timedelta(days=2)
#: Long before any bounded window, so it lands only in the open-ended ones.
OLD = date.today() - timedelta(days=900)


@pytest.fixture()
def seeded159(pg, monkeypatch):  # noqa: F811
    """Four contacts covering every SQL evidence provenance at once.

      ``direct``    a direct HubSpot SQL date inside the recent windows
      ``recovered`` NO direct date, but a recovered lifecycle-history date in
                    the same recent windows — the contact that made the
                    headline and the detail page disagree
      ``undated``   reached SQL, no date anywhere, created recently
      ``old_undated`` reached SQL, no date anywhere, created long ago
    """
    import db.connection as connection

    monkeypatch.setenv("DATABASE_URL", pg.url)
    connection._pool = None
    connection.init_pool()
    from db.schema import init_db

    init_db()

    def _exec(sql, params=()):
        with connection.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)

    def _contact(contact_id, stage, entered_sql, created):
        _exec(
            """INSERT INTO hubspot_contact_funnel
                   (contact_id, created_at, last_modified_at, lifecycle_stage,
                    mql_status_category, date_entered_sql, company,
                    hs_analytics_source, hs_analytics_source_data_1,
                    hs_analytics_source_data_2)
               VALUES (%s,%s,NOW(),%s,'qualified',%s,'Acme',
                       'PAID_SEARCH','camp','kw')""",
            (contact_id, created, stage, entered_sql))

    _contact("direct", "customer", RECENT, RECENT)
    _contact("recovered", "customer", None, RECENT)
    _contact("undated", "salesqualifiedlead", None, RECENT)
    _contact("old_undated", "opportunity", None, OLD)

    # The recovered date lives in its OWN table — the contact sync owns
    # `date_entered_sql` and would erase anything written there.
    _exec(
        """INSERT INTO hubspot_lifecycle_stage_history
               (contact_id, funnel_event, entered_at, hubspot_property,
                hubspot_value, hubspot_source_type, lifecycle_rule_version,
                recovery_run_id)
           VALUES ('recovered','sql',%s,'lifecyclestage',
                   'salesqualifiedlead','CRM_UI','v1','pr-ads-159-fixture')""",
        (RECENT,))
    yield pg


@_needs_pg
def test_53_pg_headline_detail_and_operational_all_include_a_recovered_contact(
        seeded159):
    """The drift, executed.

    Before PR-ADS-159 §5 the headline coalesced the recovered date while the
    page and the operational counts filtered on the bare column, so this
    contact was in one number and absent from the other two.
    """
    page = repo.fetch_funnel_contact_page(lifecycle.EVENT_SQL, None, None,
                                          page_size=50)
    ops = repo.fetch_operational_status_counts(lifecycle.EVENT_SQL, None, None)
    contacts = repo.fetch_funnel_contacts(None, None)

    assert page["available"] and ops["available"] and contacts["available"]

    ids = {r["contact_id"] for r in page["rows"]}
    assert ids == {"direct", "recovered"}, (
        "the page must contain the recovered contact and exclude the undated ones")

    headline = sum(1 for r in contacts["rows"] if r.get("date_entered_sql"))
    operational = sum(int(v) for v in ops["counts"].values())
    assert headline == page["total"] == operational == 2, (
        f"headline={headline} detail={page['total']} operational={operational}")


@_needs_pg
def test_54_pg_the_recovered_date_is_shown_and_flagged_as_recovered(seeded159):
    page = repo.fetch_funnel_contact_page(lifecycle.EVENT_SQL, None, None,
                                          page_size=50)
    rows = {r["contact_id"]: r for r in page["rows"]}

    assert rows["recovered"]["date_entered_sql"] == RECENT
    assert rows["recovered"]["date_entered_sql_from_history"] is True
    assert rows["direct"]["date_entered_sql"] == RECENT
    assert rows["direct"]["date_entered_sql_from_history"] is False, (
        "a directly-read date must never be labelled as recovered")


@_needs_pg
def test_55_pg_the_recovered_contact_is_no_longer_a_candidate(seeded159):
    """A contact recovered on an earlier run must not be re-asked of HubSpot."""
    candidates = repo.fetch_sql_recovery_candidates(limit=100)

    assert candidates["available"]
    ids = {r["contact_id"] for r in candidates["rows"]}
    assert ids == {"undated", "old_undated"}, (
        "only contacts with NO effective SQL date are candidates")


@_needs_pg
def test_56_pg_sql_candidates_exclude_contacts_that_never_reached_sql(seeded159):
    import db.connection as connection

    with connection.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO hubspot_contact_funnel
                   (contact_id, created_at, last_modified_at, lifecycle_stage)
               VALUES ('just_a_lead', %s, NOW(), 'lead')""", (RECENT,))

    ids = {r["contact_id"]
           for r in repo.fetch_sql_recovery_candidates(limit=100)["rows"]}
    assert "just_a_lead" not in ids, (
        "a contact whose stage never implies SQL has no SQL transition to recover")


@_needs_pg
def test_57_pg_the_population_splits_into_direct_recovered_and_unresolved(
        seeded159):
    population = repo.fetch_sql_coverage_population()

    assert population["available"]
    assert population["candidates"] == 4
    assert population["direct"] == 1
    assert population["recovered"] == 1
    assert population["unresolved"] == 2
    assert population["direct"] + population["recovered"] \
        + population["unresolved"] == population["candidates"]


@_needs_pg
def test_58_pg_unresolved_bounds_carry_creation_times_for_window_reasoning(
        seeded159):
    rows = repo.fetch_unresolved_sql_created_at_bounds()

    assert rows["available"]
    by_id = {r["contact_id"]: r["created_at"] for r in rows["rows"]}
    assert set(by_id) == {"undated", "old_undated"}
    assert all(v is not None for v in by_id.values())


@_needs_pg
def test_59_pg_an_old_window_rules_out_the_recently_created_undated_contact(
        seeded159):
    """The per-window split, on real rows.

    A window that closed before a contact existed cannot contain that contact's
    SQL entry — so it is publishable even while the global gap is non-zero.
    """
    rows = repo.fetch_unresolved_sql_created_at_bounds()["rows"]
    closed_long_ago = OLD - timedelta(days=30)

    block = coverage.window_coverage(
        window="ancient", window_end=closed_long_ago, confirmed_sqls=0,
        recovered_sqls=0, unresolved_rows=rows)

    assert block["global_missing_sql_entry_date"] == 2
    assert block["window_membership_proven_outside"] == 2
    assert block["window_total_complete"] is True
    assert block["complete_sql_total"] == 0, (
        "a proven-empty window publishes 0 — that is a measured zero, not a "
        "withheld one")


@_needs_pg
def test_60_pg_the_audit_runs_and_reports_incomplete_coverage(seeded159):
    """The packaged command, against real rows, as a subprocess."""
    import os

    env = {**os.environ, "DATABASE_URL": seeded159.url}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.audit_lifecycle_sql_coverage", "--json"],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)

    report = json.loads(result.stdout)
    assert report["violations"] == [], report["violations"]
    assert report["unavailable"] == [], report["unavailable"]
    assert result.returncode == 0, result.stdout[-2000:]
    assert report["audit_complete"] is True
    assert report["coverage_complete"] is False, (
        "two undated lifecycle-SQL contacts exist, so coverage is incomplete")
    assert report["complete_sql_total_publishable"] is False
    assert report["cpql_publishable"] is False
    assert report["population"]["unresolved"] == 2

    # PR-ADS-159-R5: every window × every canonical scope, not just all_time.
    recon = report["read_reconciliation"]
    assert recon["combinations"] == 11 * 4, (
        f"expected 11 windows x 4 scopes, got {recon['combinations']}")
    assert recon["scopes"] == ["all_source", "google_ads_source",
                              "campaign_attributable", "keyword_attributable"]
    assert recon["reconciled"] >= 1, recon
    # Every compared pair agrees; every other pair is UNAVAILABLE with a reason,
    # never a silent zero.
    for block in recon["results"]:
        if block["available"]:
            assert block["headline"] == block["detail_total"] \
                == block["operational_total"], block
        else:
            assert block["reason"], "an unavailable pair must say why"
            assert block["headline"] is None
            assert block["detail_total"] is None
            assert block["operational_total"] is None


@_needs_pg
def test_61_pg_strict_mode_exits_nonzero_on_incomplete_coverage(seeded159):
    """Same data, same audit, different question — and a different exit."""
    import os

    env = {**os.environ, "DATABASE_URL": seeded159.url}
    lenient, strict = (
        subprocess.run(
            [sys.executable, "-m", "scripts.audit_lifecycle_sql_coverage",
             "--json", *extra],
            capture_output=True, text=True, cwd=str(_ROOT), env=env)
        for extra in ((), ("--strict",)))

    assert lenient.returncode == 0, lenient.stdout[-2000:]
    assert strict.returncode == 3, strict.stdout[-2000:]
    assert json.loads(strict.stdout)["violations"] == [], (
        "strict mode reports a DATA finding, not a contract violation")


@_needs_pg
def test_62_pg_the_audit_output_carries_no_contact_identifier(seeded159):
    """Contacts appear as counts. The audit is not a contact export."""
    import os

    env = {**os.environ, "DATABASE_URL": seeded159.url}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.audit_lifecycle_sql_coverage", "--json"],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)

    for identifier in ("direct", "recovered", "undated", "old_undated", "Acme"):
        # `recovered` and `direct` are legitimate FIELD names in the report, so
        # only the contact-shaped values are forbidden.
        assert f'"{identifier}"' not in result.stdout or identifier in (
            "direct", "recovered"), f"{identifier} leaked into the audit output"
    assert "Acme" not in result.stdout


# ═════════════════════════════════════════════════════════════════════════════
# §11 — PR-ADS-159-R1/R2/R3: checkpoints, cursor safety, exact remaining work
# ═════════════════════════════════════════════════════════════════════════════
#
# These drive `recover()` end to end against a fake repository. The previous
# versions of three of these tests asserted SOURCE STRINGS — that the file
# contained `len(rows) >= int(limit)`, that it mentioned a budget comparison.
# A string assertion cannot tell whether the cursor actually stops, and the
# cursor actually stopping is the entire guarantee.

class FakeRepo:
    """A funnel repository double: candidates in, checkpoints out.

    Wide enough for `recover()` to run its real loop — the point is to observe
    what the loop DOES with the cursor, not to re-describe it.
    """

    def __init__(self, candidates, *, state_available=True):
        self.candidates = list(candidates)
        self.state_available = state_available
        #: scope -> saved checkpoint row. Separate keys prove separation.
        self.saved: dict = {}
        self.reads: list = []

    # ── checkpoint ──────────────────────────────────────────────────────────
    def fetch_lifecycle_recovery_state(self, *, scope=None):
        self.reads.append(scope)
        if not self.state_available:
            return {"available": False, "row": None, "scope": scope}
        return {"available": True, "row": self.saved.get(scope), "scope": scope}

    def save_lifecycle_recovery_state(self, state, *, scope=None):
        self.saved[scope] = dict(state)
        return {"ok": True, "error": None}

    # ── candidates ──────────────────────────────────────────────────────────
    def _page(self, after_contact_id, limit):
        rows = [r for r in self.candidates
                if after_contact_id is None or r["contact_id"] > after_contact_id]
        return {"available": True, "rows": rows[:limit]}

    def fetch_sql_recovery_candidates(self, *, after_contact_id=None, limit=200):
        return self._page(after_contact_id, limit)

    def fetch_contacts_missing_stage_dates(self, *, after_contact_id=None,
                                           limit=200):
        return self._page(after_contact_id, limit)


def _candidate(contact_id, stage="customer"):
    """One SQL-recovery candidate row as the repository returns it."""
    return {"contact_id": contact_id, "lifecycle_stage": stage,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "date_entered_sql": None}


def _run(monkeypatch, fake_repo, client, *, stub_writer=True, **kwargs):
    """Run `recover()` against the fake repository.

    The local writer is stubbed by default: these tests are about the LOOP's
    cursor and vocabulary decisions, and a real write attempt with no database
    would fail the run before any of that could be observed.
    """
    import db.crm_funnel_repository as real_repo

    for name in ("fetch_lifecycle_recovery_state", "save_lifecycle_recovery_state",
                 "fetch_sql_recovery_candidates",
                 "fetch_contacts_missing_stage_dates"):
        monkeypatch.setattr(real_repo, name, getattr(fake_repo, name))
    if stub_writer:
        import db.writers as real_writers
        monkeypatch.setattr(
            real_writers, "upsert_lifecycle_stage_history",
            lambda rows, *, run_id: {"ok": True, "persisted": len(rows)})
    kwargs.setdefault("event", lifecycle.EVENT_SQL)
    return recovery.recover(client=client, **kwargs)


def _present_client(contact_ids, ts=None):
    """A client whose BATCH read answers every contact with an SQL version."""
    ts = ts or datetime(2026, 3, 3, tzinfo=timezone.utc)
    return FakeClient(batch_results=[
        _record(cid, [_version("salesqualifiedlead", ts)]) for cid in contact_ids])


def _batch_blind_client(contact_ids, *, individual=None):
    """A client whose batch returns no history — the production shape."""
    return FakeClient(
        batch_results=[_record(cid, container=False) for cid in contact_ids],
        individual_results=dict(individual or {}))


# ── R1 · independent checkpoints ─────────────────────────────────────────────

def test_63_each_candidate_mode_has_its_own_checkpoint_scope():
    assert recovery.checkpoint_scope(None) == "lifecycle_stage_history"
    assert recovery.checkpoint_scope(lifecycle.EVENT_SQL) == \
        "lifecycle_stage_history:sql"
    assert recovery.checkpoint_scope(None) != \
        recovery.checkpoint_scope(lifecycle.EVENT_SQL)


def test_64_an_unknown_mode_does_not_inherit_the_general_cursor():
    """A new population must not silently resume from another's cursor.

    That is the exact defect: SQL-only inheriting the all-stage cursor and
    skipping every candidate below it.
    """
    assert recovery.checkpoint_scope("opportunity") == \
        "lifecycle_stage_history:opportunity"
    assert recovery.checkpoint_scope("opportunity") != \
        recovery.checkpoint_scope(None)


def test_65_sql_recovery_reads_and_writes_only_its_own_checkpoint(monkeypatch):
    """Behavioural: run SQL-only with a HIGH general-mode cursor in place.

    Under the shared scope this run would have resumed at "c9" and found
    nothing. It must resume from its own (absent) cursor and process c1.
    """
    fake = FakeRepo([_candidate("c1"), _candidate("c2")])
    fake.saved["lifecycle_stage_history"] = {"last_contact_id": "c9"}

    result = _run(monkeypatch, fake, _present_client(["c1", "c2"]),
                  limit=10, apply=True)

    assert result["ok"], result
    assert result["checkpoint_scope"] == "lifecycle_stage_history:sql"
    assert result["resume_from"] is None, (
        "the SQL run must not inherit the all-stage cursor")
    assert result["contacts_examined"] == 2
    # The general checkpoint is untouched.
    assert fake.saved["lifecycle_stage_history"] == {"last_contact_id": "c9"}
    assert fake.saved["lifecycle_stage_history:sql"]["last_contact_id"] == "c2"


def test_66_general_recovery_is_unaffected_by_a_high_sql_cursor(monkeypatch):
    fake = FakeRepo([_candidate("c1"), _candidate("c2")])
    fake.saved["lifecycle_stage_history:sql"] = {"last_contact_id": "c9"}

    result = _run(monkeypatch, fake, _present_client(["c1", "c2"]),
                  limit=10, apply=True, event=None)

    assert result["checkpoint_scope"] == "lifecycle_stage_history"
    assert result["resume_from"] is None
    assert result["contacts_examined"] == 2
    assert fake.saved["lifecycle_stage_history:sql"] == {"last_contact_id": "c9"}


def test_67_each_mode_resumes_from_its_own_cursor(monkeypatch):
    fake = FakeRepo([_candidate(f"c{i}") for i in range(1, 6)])
    fake.saved["lifecycle_stage_history:sql"] = {"last_contact_id": "c3"}
    fake.saved["lifecycle_stage_history"] = {"last_contact_id": "c1"}

    sql = _run(monkeypatch, fake, _present_client(["c4", "c5"]), limit=10)
    assert sql["resume_from"] == "c3"
    assert sql["contacts_examined"] == 2

    general = _run(monkeypatch, fake, _present_client(["c2", "c3", "c4", "c5"]),
                   limit=10, event=None)
    assert general["resume_from"] == "c1"
    assert general["contacts_examined"] == 4


def test_68_applying_one_mode_never_overwrites_the_others_checkpoint(monkeypatch):
    fake = FakeRepo([_candidate("c1")])
    fake.saved["lifecycle_stage_history"] = {"last_contact_id": "zzz",
                                             "contacts_examined": 999}

    _run(monkeypatch, fake, _present_client(["c1"]), limit=10, apply=True)

    assert fake.saved["lifecycle_stage_history"]["last_contact_id"] == "zzz"
    assert fake.saved["lifecycle_stage_history"]["contacts_examined"] == 999
    assert set(fake.saved) == {"lifecycle_stage_history",
                               "lifecycle_stage_history:sql"}


# ── R2 · the cursor never passes an unadjudicated contact ────────────────────

def test_69_the_cursor_stops_before_the_first_budget_deferred_contact(monkeypatch):
    """limit 5, budget 2: three contacts need a fallback, only two may have one.

    Previously the loop set the cursor on EVERY row it looked at, so c3, c4 and
    c5 were passed over with no individual read and skipped for good on the
    next resumed run.
    """
    ids = [f"c{i}" for i in range(1, 6)]
    fake = FakeRepo([_candidate(c) for c in ids])
    client = _batch_blind_client(ids)   # every contact needs the fallback

    result = _run(monkeypatch, fake, client, limit=5, apply=True,
                  individual_request_budget=2)

    assert result["ok"], result
    assert result["individual_requests"] == 2
    assert result["deferred_at_contact"] == "c3"
    assert result["next_cursor"] == "c2", (
        "the cursor must stop at the last FULLY adjudicated contact")
    assert result["contacts_examined"] == 2
    assert result["contacts_deferred_by_budget"] == 3
    assert fake.saved["lifecycle_stage_history:sql"]["last_contact_id"] == "c2"


def test_70_a_resumed_run_processes_the_deferred_contact(monkeypatch):
    """The other half of the guarantee: deferred means LATER, not lost."""
    ids = [f"c{i}" for i in range(1, 6)]
    fake = FakeRepo([_candidate(c) for c in ids])

    first = _run(monkeypatch, fake, _batch_blind_client(ids), limit=5,
                 apply=True, individual_request_budget=2)
    assert first["next_cursor"] == "c2"

    second = _run(monkeypatch, fake, _batch_blind_client(["c3", "c4", "c5"]),
                  limit=5, apply=True, individual_request_budget=10)

    assert second["resume_from"] == "c2"
    assert second["contacts_examined"] == 3
    assert second["next_cursor"] == "c5"


def test_71_no_contact_is_skipped_or_processed_twice_across_resumes(monkeypatch):
    """Walk the whole population two contacts at a time; account for every id."""
    ids = [f"c{i}" for i in range(1, 8)]
    fake = FakeRepo([_candidate(c) for c in ids])

    seen: list = []
    guard = 0
    while guard < 20:
        guard += 1
        result = _run(monkeypatch, fake, _batch_blind_client(ids), limit=2,
                      apply=True, individual_request_budget=2)
        assert result["ok"], result
        seen.extend(r["contact_id"] for r in result["unresolved"]
                    if r.get("adjudicated"))
        if not result["more_candidates_remain"]:
            break

    assert seen == ids, f"expected every contact exactly once, got {seen}"
    assert len(seen) == len(set(seen)), "a contact was processed twice"


def test_72_a_deferred_contact_is_not_classified_unrecoverable(monkeypatch):
    ids = ["c1", "c2", "c3"]
    fake = FakeRepo([_candidate(c) for c in ids])

    result = _run(monkeypatch, fake, _batch_blind_client(ids), limit=3,
                  individual_request_budget=1)

    deferred = [r for r in result["unresolved"] if not r.get("adjudicated")]
    assert deferred, "the deferred contacts must appear in the report"
    for row in deferred:
        assert row["reason"] == recovery.SQL_DEFERRED_BY_BUDGET
        assert row["reason"] != recovery.UNRECOVERABLE_NO_EVIDENCE
        assert row["payload_state"] == recovery.PAYLOAD_FALLBACK_DEFERRED


def test_73_a_dry_run_writes_no_checkpoint(monkeypatch):
    fake = FakeRepo([_candidate("c1"), _candidate("c2")])

    result = _run(monkeypatch, fake, _present_client(["c1", "c2"]), limit=10)

    assert result["mode"] == "dry_run"
    assert result["apply"] is False
    assert fake.saved == {}, "a dry run must persist nothing, anywhere"


def test_74_apply_advances_only_through_fully_adjudicated_contacts(monkeypatch):
    """Budget covers c1 only; the checkpoint must stop there, not at c3."""
    ids = ["c1", "c2", "c3"]
    fake = FakeRepo([_candidate(c) for c in ids])

    result = _run(monkeypatch, fake, _batch_blind_client(ids), limit=3,
                  apply=True, individual_request_budget=1)

    assert result["next_cursor"] == "c1"
    assert fake.saved["lifecycle_stage_history:sql"]["last_contact_id"] == "c1"


# ── R3 · exact remaining work ────────────────────────────────────────────────

def test_75_fewer_candidates_than_the_limit_means_no_more_remain(monkeypatch):
    fake = FakeRepo([_candidate("c1"), _candidate("c2")])
    result = _run(monkeypatch, fake, _present_client(["c1", "c2"]), limit=10)

    assert result["contacts_examined"] == 2
    assert result["more_candidates_remain"] is False


def test_76_exactly_the_limit_with_nothing_behind_it_means_no_more_remain(
        monkeypatch):
    """The case `len(rows) >= limit` got WRONG — it reported more work forever."""
    fake = FakeRepo([_candidate("c1"), _candidate("c2")])
    result = _run(monkeypatch, fake, _present_client(["c1", "c2"]), limit=2)

    assert result["contacts_examined"] == 2
    assert result["more_candidates_remain"] is False, (
        "exactly the last page is not evidence of another page")


def test_77_exactly_the_limit_with_another_row_behind_it_means_more_remain(
        monkeypatch):
    fake = FakeRepo([_candidate("c1"), _candidate("c2"), _candidate("c3")])
    result = _run(monkeypatch, fake, _present_client(["c1", "c2"]), limit=2)

    assert result["contacts_examined"] == 2
    assert result["more_candidates_remain"] is True


def test_78_budget_exhaustion_before_the_limit_still_means_more_remain(
        monkeypatch):
    """Deferred candidates are remaining work even when the page was not full."""
    ids = ["c1", "c2", "c3"]
    fake = FakeRepo([_candidate(c) for c in ids])

    result = _run(monkeypatch, fake, _batch_blind_client(ids), limit=10,
                  individual_request_budget=1)

    assert result["more_candidates_remain"] is True
    assert result["deferred_at_contact"] == "c2"


def test_79_an_empty_population_reports_no_remaining_work(monkeypatch):
    fake = FakeRepo([])
    result = _run(monkeypatch, fake, FakeClient(), limit=10)

    assert result["ok"] is True
    assert result["contacts_examined"] == 0
    assert result["more_candidates_remain"] is False


def test_80_a_failed_pass_reports_remaining_work_as_unknown(monkeypatch):
    """An aborted pass proves nothing about what is left — not even "more"."""
    fake = FakeRepo([_candidate("c1")], state_available=False)
    result = _run(monkeypatch, fake, FakeClient(), limit=10)

    assert result["ok"] is False
    assert result["more_candidates_remain"] is None


# ── R4 · the emitted vocabulary, executed ────────────────────────────────────

def test_81_each_vocabulary_is_bound_to_one_denominator():
    names = set(recovery.VOCABULARIES)
    assert names == {"request", "per_contact_payload", "per_sql_gap",
                     "per_stage_gap"}
    request = set(recovery.VOCABULARIES["request"])
    gaps = (set(recovery.VOCABULARIES["per_sql_gap"])
            | set(recovery.VOCABULARIES["per_stage_gap"]))
    assert not (request & gaps), (
        "a value in both vocabularies lets two different denominators be added")


def test_82_the_parameter_dropped_verdict_lives_only_in_the_diagnosis_vocabulary():
    """It is a statement about the REQUEST; no contact's payload can make it.

    Declaring it as a per-contact evidence state is exactly what made it
    unreachable — emitted by nothing, asserted by a subset check that passed.
    """
    assert hubspot.HISTORY_PARAMETER_UNSUPPORTED in hubspot.DIAGNOSIS_VERDICTS
    for name, states in recovery.VOCABULARIES.items():
        assert hubspot.HISTORY_PARAMETER_UNSUPPORTED not in states, name


@pytest.mark.parametrize("verdict", hubspot.DIAGNOSIS_VERDICTS)
def test_83_every_diagnosis_verdict_is_reachable(verdict, monkeypatch):
    """Each verdict is produced by executing the code, not asserted to exist."""
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    present = [_record("c1", [_version("salesqualifiedlead", ts)])]
    empty = [_record("c1", [])]
    present_single = {"c1": _record("c1", [_version("salesqualifiedlead", ts)])}
    empty_single = {"c1": _record("c1", [])}

    if verdict == hubspot.HISTORY_PARAMETER_UNSUPPORTED:
        # Force the structural defect: a body that will not carry the key.
        monkeypatch.setattr(hubspot, "_batch_history_body",
                            lambda ids: {"inputs": [], "properties": [],
                                         "properties_with_history": []})
        client = FakeClient(batch_results=empty, individual_results=empty_single)
    elif verdict == hubspot.VERDICT_BOTH:
        client = FakeClient(batch_results=present, individual_results=present_single)
    elif verdict == hubspot.VERDICT_INDIVIDUAL_ONLY:
        client = FakeClient(batch_results=[_record("c1", container=False)],
                            individual_results=present_single)
    elif verdict == hubspot.VERDICT_BATCH_ONLY:
        client = FakeClient(batch_results=present, individual_results=empty_single)
    elif verdict == hubspot.VERDICT_NEITHER:
        client = FakeClient(batch_results=empty, individual_results=empty_single)
    elif verdict == hubspot.VERDICT_BATCH_FAILED:
        client = FakeClient(batch_error=_ApiError(500),
                            individual_results=present_single)
    elif verdict == hubspot.VERDICT_INDIVIDUAL_FAILED:
        client = FakeClient(batch_results=empty, individual_error=_ApiError(500))
    else:  # VERDICT_BOTH_FAILED
        client = FakeClient(batch_error=_ApiError(500),
                            individual_error=_ApiError(500))

    out = hubspot.diagnose_lifecycle_history_reads(["c1"], client=client)
    assert out["verdict"] == verdict, out


def test_84_a_failed_batch_is_never_reported_as_the_portal_holding_nothing():
    """The strongest claim about HubSpot must not come from an unanswered call."""
    client = FakeClient(batch_error=_ApiError(500),
                        individual_results={"c1": _record("c1", [])})

    out = hubspot.diagnose_lifecycle_history_reads(["c1"], client=client)

    assert out["verdict"] == hubspot.VERDICT_BATCH_FAILED
    assert out["verdict"] != hubspot.VERDICT_NEITHER
    assert out["batch"]["outcome"] == "request_failed"
    # …and the individual read's own outcome survives beside it.
    assert out["individual"]["outcome"] == "ok"
    assert out["individual"]["states"] == {hubspot.HISTORY_EMPTY: 1}


def test_85_both_outcomes_survive_when_the_paths_disagree():
    """Neither read produces history, but for DIFFERENT reasons. Keep both."""
    client = FakeClient(batch_results=[_record("c1", container=False)],
                        individual_results={"c1": _record("c1", [])})

    out = hubspot.diagnose_lifecycle_history_reads(["c1"], client=client)

    assert out["batch"]["states"] == {hubspot.HISTORY_PROPERTY_ABSENT: 1}
    assert out["individual"]["states"] == {hubspot.HISTORY_EMPTY: 1}
    assert out["paths_agree"] is False
    assert out["verdict"] == hubspot.VERDICT_NEITHER


def test_86_unrecoverable_requires_both_paths_to_answer_definitively(monkeypatch):
    """The proof, executed. An EMPTY history from both reads — and only that."""
    fake = FakeRepo([_candidate("c1")])
    client = FakeClient(batch_results=[_record("c1", [])],
                        individual_results={"c1": _record("c1", [])})

    result = _run(monkeypatch, fake, client, limit=10)

    reasons = {r["reason"] for r in result["unresolved"]}
    assert reasons == {recovery.UNRECOVERABLE_NO_EVIDENCE}
    assert all(r["both_paths_attempted"] for r in result["unresolved"])


def test_87_a_missing_payload_is_never_called_unrecoverable(monkeypatch):
    """A payload we may simply not have asked for correctly proves nothing."""
    fake = FakeRepo([_candidate("c1")])
    client = FakeClient(batch_results=[_record("c1", container=False)],
                        individual_results={"c1": _record("c1", container=False)})

    result = _run(monkeypatch, fake, client, limit=10)

    reasons = {r["reason"] for r in result["unresolved"]}
    assert reasons == {recovery.PAYLOAD_MISSING}
    assert recovery.UNRECOVERABLE_NO_EVIDENCE not in reasons


def test_88_without_the_fallback_nothing_is_ever_called_unrecoverable(monkeypatch):
    """One path cannot prove absence. The budget/flag must gate the claim."""
    fake = FakeRepo([_candidate("c1")])
    client = FakeClient(batch_results=[_record("c1", [])])

    result = _run(monkeypatch, fake, client, limit=10,
                  individual_fallback=False)

    reasons = {r["reason"] for r in result["unresolved"]}
    assert reasons == {recovery.PAYLOAD_EMPTY}
    assert recovery.UNRECOVERABLE_NO_EVIDENCE not in reasons


def test_89_a_successful_sql_recovery_is_summarised_as_the_sql_state(monkeypatch):
    """The summary must be built from the ROWS, not from a generic constant.

    The first cut added `matching_stage_version_recovered` unconditionally, so
    an SQL run whose rows all said `history_sql_timestamp_recovered` was
    summarised under a name that appeared nowhere in its own output.
    """
    fake = FakeRepo([_candidate("c1")])
    result = _run(monkeypatch, fake, _present_client(["c1"]), limit=10)

    per_gap = result["evidence_states"]["per_stage_gap_reason"]
    assert per_gap == {recovery.HISTORY_SQL_TIMESTAMP_RECOVERED: 1}
    assert recovery.MATCHING_VERSION_RECOVERED not in per_gap
    assert result["evidence_states"]["per_stage_gap_vocabulary"] == "per_sql_gap"


def test_90_a_non_sql_stage_keeps_the_generic_recovered_state(monkeypatch):
    """The SQL vocabulary is keyed on the EVENT, not on the run mode.

    An all-stage run recovering a LEAD transition reports the generic state; the
    same run recovering an SQL transition reports the SQL one. Both are correct,
    and the report names which vocabulary its gap counts belong to.
    """
    ts = datetime(2026, 4, 4, tzinfo=timezone.utc)
    fake = FakeRepo([_candidate("c1")])
    client = FakeClient(batch_results=[_record("c1", [_version("lead", ts)])])

    result = _run(monkeypatch, fake, client, limit=10, event=None)

    per_gap = result["evidence_states"]["per_stage_gap_reason"]
    assert recovery.MATCHING_VERSION_RECOVERED in per_gap, per_gap
    assert result["evidence_states"]["per_stage_gap_vocabulary"] == "per_stage_gap"
    for state in per_gap:
        assert state in recovery.VOCABULARIES["per_stage_gap"], state


def test_91_every_emitted_state_belongs_to_its_own_vocabulary(monkeypatch):
    """Executed over four different shapes, not asserted over a constant list."""
    ts = datetime(2026, 2, 2, tzinfo=timezone.utc)
    shapes = {
        "recovered": FakeClient(
            batch_results=[_record("c1", [_version("salesqualifiedlead", ts)])]),
        "no_sql_stage": FakeClient(
            batch_results=[_record("c1", [_version("lead", ts)])]),
        "empty": FakeClient(batch_results=[_record("c1", [])],
                            individual_results={"c1": _record("c1", [])}),
        "not_returned": FakeClient(batch_results=[]),
    }
    sql_vocab = set(recovery.VOCABULARIES["per_sql_gap"])
    payload_vocab = set(recovery.VOCABULARIES["per_contact_payload"])

    for name, client in shapes.items():
        fake = FakeRepo([_candidate("c1")])
        result = _run(monkeypatch, fake, client, limit=10)
        states = result["evidence_states"]
        for gap_state in states["per_stage_gap_reason"]:
            assert gap_state in sql_vocab, f"{name}: {gap_state} is not a SQL-gap state"
        for payload_state in states["per_contact_payload_state"]:
            assert payload_state in payload_vocab, (
                f"{name}: {payload_state} is not a payload state")


def test_92_an_authorization_failure_stops_the_run_under_its_own_reason(
        monkeypatch):
    fake = FakeRepo([_candidate("c1")])
    client = FakeClient(batch_error=_ApiError(403))

    result = _run(monkeypatch, fake, client, limit=10, apply=True)

    assert result["ok"] is False
    assert result["reason"] == recovery.HUBSPOT_AUTHORIZATION_FAILED
    assert result["reason"] in recovery.VOCABULARIES["request"]
    assert fake.saved == {}, "a stopped run must not advance any checkpoint"


def test_93_local_apply_is_idempotent(monkeypatch):
    """Re-running the same recovery rewrites rather than duplicates.

    The write path is keyed on (contact_id, funnel_event); this proves the
    SERVICE hands the writer the same key set on a re-run rather than growing it.
    """
    writes: list = []

    class _Writer:
        @staticmethod
        def upsert_lifecycle_stage_history(rows, *, run_id):
            writes.append([(r["contact_id"], r["funnel_event"]) for r in rows])
            return {"ok": True, "persisted": len(rows)}

    import db.writers as real_writers
    monkeypatch.setattr(real_writers, "upsert_lifecycle_stage_history",
                        _Writer.upsert_lifecycle_stage_history)

    for _ in range(2):
        fake = FakeRepo([_candidate("c1")])
        _run(monkeypatch, fake, _present_client(["c1"]), limit=10, apply=True,
             stub_writer=False)

    assert writes == [[("c1", "sql")], [("c1", "sql")]], writes
    assert len({tuple(w) for w in writes}) == 1, (
        "the same contact/event key must be rewritten, never appended to")


# ═════════════════════════════════════════════════════════════════════════════
# §12 — PR-ADS-159-R1/R5 against a real PostgreSQL server
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_94_pg_the_two_checkpoint_scopes_are_separate_rows(seeded159):
    """The separation, in the durable store rather than in a fake.

    A shared row is the whole defect: one scope's cursor overwriting the other's
    is what let an SQL-only run resume from an all-stage position.
    """
    repo.save_lifecycle_recovery_state(
        {"last_contact_id": "zzz-general", "contacts_examined": 1,
         "contacts_recovered": 0, "contacts_without_history": 0,
         "events_recovered": 0, "last_run_id": "r1", "last_run_mode": "apply",
         "last_error": None},
        scope="lifecycle_stage_history")
    repo.save_lifecycle_recovery_state(
        {"last_contact_id": "aaa-sql", "contacts_examined": 1,
         "contacts_recovered": 0, "contacts_without_history": 0,
         "events_recovered": 0, "last_run_id": "r2", "last_run_mode": "apply",
         "last_error": None},
        scope="lifecycle_stage_history:sql")

    general = repo.fetch_lifecycle_recovery_state(scope="lifecycle_stage_history")
    sql = repo.fetch_lifecycle_recovery_state(scope="lifecycle_stage_history:sql")

    assert general["available"] and sql["available"]
    assert general["row"]["last_contact_id"] == "zzz-general"
    assert sql["row"]["last_contact_id"] == "aaa-sql", (
        "writing the general scope must not move the SQL cursor")


@_needs_pg
def test_95_pg_the_default_scope_is_the_original_general_one(seeded159):
    """Backward compatibility: the existing production checkpoint still resumes."""
    repo.save_lifecycle_recovery_state(
        {"last_contact_id": "legacy", "contacts_examined": 0,
         "contacts_recovered": 0, "contacts_without_history": 0,
         "events_recovered": 0, "last_run_id": "r", "last_run_mode": "apply",
         "last_error": None})

    unscoped = repo.fetch_lifecycle_recovery_state()
    explicit = repo.fetch_lifecycle_recovery_state(scope="lifecycle_stage_history")

    assert unscoped["scope"] == "lifecycle_stage_history"
    assert unscoped["row"]["last_contact_id"] == "legacy"
    assert explicit["row"]["last_contact_id"] == "legacy"


@_needs_pg
def test_96_pg_an_sql_run_does_not_resume_from_the_general_cursor(seeded159):
    """End to end, on real rows: the exact skip this correction prevents.

    The general cursor is set past every candidate. Under the shared scope the
    SQL run would find nothing and report success; it must instead find both
    undated contacts.
    """
    repo.save_lifecycle_recovery_state(
        {"last_contact_id": "zzzzzz", "contacts_examined": 0,
         "contacts_recovered": 0, "contacts_without_history": 0,
         "events_recovered": 0, "last_run_id": "r", "last_run_mode": "apply",
         "last_error": None},
        scope="lifecycle_stage_history")

    sql_state = repo.fetch_lifecycle_recovery_state(
        scope=recovery.checkpoint_scope(lifecycle.EVENT_SQL))
    assert sql_state["row"] is None, "the SQL scope has its own, empty checkpoint"

    candidates = repo.fetch_sql_recovery_candidates(
        after_contact_id=(sql_state["row"] or {}).get("last_contact_id"),
        limit=100)
    assert {r["contact_id"] for r in candidates["rows"]} == {"undated",
                                                             "old_undated"}


@_needs_pg
def test_97_pg_the_audit_reconciles_every_window_and_scope(seeded159):
    """44 combinations, each compared or explicitly unavailable — never zero."""
    import os

    env = {**os.environ, "DATABASE_URL": seeded159.url}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.audit_lifecycle_sql_coverage", "--json"],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)
    report = json.loads(result.stdout)
    recon = report["read_reconciliation"]

    assert report["violations"] == [], report["violations"]
    assert recon["combinations"] == 44
    seen = {(b["window_type"], b["window"], b["scope"]) for b in recon["results"]}
    assert len(seen) == 44, "every window/scope pair must be reported separately"
    assert recon["effective_date_basis"] == repo.EFFECTIVE_DATE_DOCTRINE


@_needs_pg
def test_98_pg_an_unavailable_scoped_reader_is_unavailable_not_zero(
        seeded159, monkeypatch):
    """Fail-closed: a reader that cannot answer must not read as a proven 0."""
    from scripts import audit_lifecycle_sql_coverage as audit

    monkeypatch.setattr(
        repo, "fetch_operational_status_counts",
        lambda *a, **k: {"available": False, "reason": "database_unavailable",
                         "counts": {}})

    f = audit.Findings()
    block = audit.audit_read_reconciliation(f, datetime.now(tz=timezone.utc))

    assert block["reconciled"] == 0
    assert block["unavailable"] == 44
    for pair in block["results"]:
        assert pair["available"] is False
        assert pair["operational_total"] is None, (
            "an unreadable operational count must never become 0")
    assert f.violations == [], (
        "an outage is unavailability, not a contract violation")
