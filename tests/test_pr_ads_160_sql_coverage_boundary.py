"""PR-ADS-160 — prospective SQL coverage boundary, gap prevention, certification.

The two facts this suite is built around
----------------------------------------
**1. Historical recovery is exhausted.** PR-ADS-159's production validation read
every candidate: 1,261 contacts whose lifecycle stage proves they reached SQL,
728 with HubSpot's direct property, 0 recoverable from lifecycle history, 533
with no provable timestamp. All 533 returned VALID history and none of those
histories held a transition into ``salesqualifiedlead``. Their dates are absent
from HubSpot, not merely missing from us. No engineering recovers them.

**2. A proven timestamp could be silently destroyed.** Before this PR, the
contact-funnel upsert refreshed every column from the incoming payload under a
staleness guard only. A later payload that omitted the SQL property therefore
blanked a stored date and REPORTED SUCCESS. Proven against a real PostgreSQL
instance::

    first payload   date_entered_sql = 2026-09-02   -> stored
    later payload   property absent  (NULL)         -> {'ok': True,
                                                        'persisted': 1}
    stored value    date_entered_sql = None         ← silently erased

That is worse than a gap. A gap is visible; this destroyed the evidence that
there had ever been anything to lose. ``test_05`` is that exact sequence, and it
fails on the pre-fix writer.

What these tests will not accept
--------------------------------
No test here passes because a constant is spelled correctly. The boundary tests
run the real service against a real PostgreSQL schema and then read the contact
table back to prove ``date_entered_sql`` is still NULL. The exclusion tests
execute the membership rule rather than asserting its source text. The
idempotence test runs the apply twice and counts rows.

And nothing in this file ever uses creation time, boundary time, a neighbouring
lifecycle date, or the current lifecycle stage as an SQL timestamp —
``test_14`` enforces that over this file's own source.
"""

from __future__ import annotations

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
import services.sql_coverage_boundary_service as boundary_svc  # noqa: E402

from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

#: A boundary instant, and windows placed either side of it.
BOUNDARY = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
#: A contact created long before the boundary — the shape of all 533.
LEGACY_CREATED = datetime(2020, 3, 1, tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# §4 — sound per-window exclusion. The boundary DISPROVES membership only.
# ═════════════════════════════════════════════════════════════════════════════

def test_01_a_bounded_contact_is_excluded_from_a_window_after_the_boundary():
    """Requirement 1. The whole point of the boundary, executed.

    The contact reached SQL at some unknown instant at or before B. A window
    that OPENS after B therefore cannot contain that transition — not because we
    learned the date, but because we learned it was already over.
    """
    verdict = coverage.membership_verdict(
        LEGACY_CREATED, date(2026, 10, 31),
        known_reached_sql_by=BOUNDARY, window_start=date(2026, 10, 1))

    assert verdict == "proven_outside"

    split = coverage.window_membership(
        [{"contact_id": "c1", "created_at": LEGACY_CREATED,
          "known_reached_sql_by": BOUNDARY}],
        date(2026, 10, 31), date(2026, 10, 1))
    assert split["window_membership_unresolved"] == 0
    assert split["window_membership_proven_outside"] == 1
    # Attributed to the boundary, not to the creation rule — which could not
    # have ruled this contact out, since it was created long before the window.
    assert split["window_membership_excluded_by_boundary"] == 1


def test_02_the_same_contact_stays_unresolved_for_an_overlapping_window():
    """Requirement 2. A window straddling B cannot rule the contact out.

    The unknown transition could have happened before B (inside this window's
    early part) or between B and the window's end. Nothing distinguishes them,
    so membership stays genuinely open. This is the case a careless
    implementation gets wrong by comparing the bound against the window END.
    """
    verdict = coverage.membership_verdict(
        LEGACY_CREATED, date(2026, 9, 30),
        known_reached_sql_by=BOUNDARY, window_start=date(2026, 9, 1))

    assert verdict == "unresolved"

    split = coverage.window_membership(
        [{"contact_id": "c1", "created_at": LEGACY_CREATED,
          "known_reached_sql_by": BOUNDARY}],
        date(2026, 9, 30), date(2026, 9, 1))
    assert split["window_membership_unresolved"] == 1
    assert split["window_membership_excluded_by_boundary"] == 0


@pytest.mark.parametrize("window_start,window_end,expected", [
    # Opens exactly AT the boundary instant: the transition was already over.
    (BOUNDARY, date(2026, 10, 31), "proven_outside"),
    # Opens one second before it: it could have happened inside.
    (BOUNDARY - timedelta(seconds=1), date(2026, 10, 31), "unresolved"),
    # Entirely before the boundary.
    (date(2026, 1, 1), date(2026, 6, 30), "unresolved"),
    # Open-ended (All Time) — nothing can ever be ruled out of it.
    (None, None, "unresolved"),
])
def test_03_the_boundary_comparison_is_against_the_window_start(
        window_start, window_end, expected):
    """The boundary between exclusion and non-exclusion, at the instant itself."""
    assert coverage.membership_verdict(
        LEGACY_CREATED, window_end, BOUNDARY, window_start) == expected


def test_04_a_contact_with_no_boundary_evidence_is_never_excluded():
    """Absence of a bound is not a bound.

    A contact that did not exist at boundary time — or was not captured by it —
    has no upper bound, and no window arithmetic can rule it out.
    """
    assert coverage.membership_verdict(
        LEGACY_CREATED, date(2026, 10, 31),
        known_reached_sql_by=None, window_start=date(2026, 10, 1)) == "unresolved"

    split = coverage.window_membership(
        [{"contact_id": "c1", "created_at": LEGACY_CREATED,
          "known_reached_sql_by": None}],
        date(2026, 10, 31), date(2026, 10, 1))
    assert split["window_membership_unresolved"] == 1
    assert split["window_membership_bounded"] == 0


def test_05_the_creation_lower_bound_still_works_and_is_not_double_counted():
    """PR-ADS-159's rule survives, and the two rules never inflate each other."""
    created_after = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert coverage.membership_verdict(
        created_after, date(2026, 10, 31), None, date(2026, 10, 1)
    ) == "proven_outside"

    # A contact that BOTH rules exclude is counted once as proven_outside, and
    # is NOT attributed to the boundary — the creation rule alone sufficed.
    split = coverage.window_membership(
        [{"contact_id": "c1", "created_at": created_after,
          "known_reached_sql_by": BOUNDARY}],
        date(2026, 10, 31), date(2026, 10, 1))
    assert split["window_membership_proven_outside"] == 1
    assert split["window_membership_excluded_by_boundary"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# §2/§3 — the boundary itself, against a real schema
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def seeded160(pg, monkeypatch):  # noqa: F811
    """Three contacts: two undated at SQL, one with a proven direct date."""
    import db.connection as connection

    monkeypatch.setenv("DATABASE_URL", pg.url)
    connection._pool = None
    connection.init_pool()
    from db.schema import init_db

    init_db()
    from db import writers

    writers.upsert_hubspot_contact_funnel([
        # Reached SQL, no date anywhere — the shape of the 533.
        {"contact_id": "undated_a", "lifecycle_stage": "salesqualifiedlead",
         "created_at": LEGACY_CREATED, "last_modified_at": LEGACY_CREATED},
        # A later stage also proves SQL was reached.
        {"contact_id": "undated_b", "lifecycle_stage": "customer",
         "created_at": LEGACY_CREATED, "last_modified_at": LEGACY_CREATED},
        # Proven date: must never be bounded, and never altered.
        {"contact_id": "dated", "lifecycle_stage": "salesqualifiedlead",
         "created_at": LEGACY_CREATED, "last_modified_at": LEGACY_CREATED,
         "date_entered_sql": datetime(2026, 2, 2, tzinfo=timezone.utc)},
    ])
    pg.connection = connection
    return pg


def _sql_dates(connection, ids):
    with connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT contact_id, date_entered_sql FROM "
                    "hubspot_contact_funnel WHERE contact_id = ANY(%s) "
                    "ORDER BY contact_id", (list(ids),))
        return dict(cur.fetchall())


@_needs_pg
def test_06_pg_the_dry_run_proposes_and_writes_absolutely_nothing(seeded160):
    """Requirement: a dry run must show the real population and change nothing."""
    result = boundary_svc.establish_boundary(apply=False)

    assert result["ok"] is True
    assert result["run_outcome"] == boundary_svc.RUN_OK
    assert result["legacy_undated_bounded"] == 2, "the dated contact is excluded"
    assert result["boundary_written"] is False
    assert result["contacts_written"] == 0
    assert result["hubspot_writes_performed"] is False
    # A dry run has established nothing, so nothing can be certified.
    assert result["certification_can_begin"] is False
    # And the store is untouched.
    assert repo.fetch_active_sql_coverage_boundary()["boundary"] is None


@_needs_pg
def test_07_pg_boundary_evidence_never_becomes_an_sql_event_date(seeded160):
    """Requirement 3. THE invariant of this entire PR.

    After applying a boundary, the bounded contacts must still have NO SQL date
    and NO recovered-history row. The bound lives in its own table under its own
    name, and touches neither of the two places an exact timestamp may live.
    """
    before = _sql_dates(seeded160.connection, ["undated_a", "undated_b", "dated"])
    result = boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    assert result["ok"] and result["boundary_written"]
    after = _sql_dates(seeded160.connection, ["undated_a", "undated_b", "dated"])

    assert after["undated_a"] is None, "a bound must never become an event date"
    assert after["undated_b"] is None
    # The proven date is untouched — a boundary neither creates nor edits dates.
    assert after["dated"] == before["dated"]

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM hubspot_lifecycle_stage_history")
        assert cur.fetchone()[0] == 0, (
            "a boundary must never write recovered-history evidence")
        # The bound IS stored — in its own table, under its own name.
        cur.execute("SELECT contact_id, known_reached_sql_by FROM "
                    "sql_coverage_boundary_contact ORDER BY contact_id")
        bounds = dict(cur.fetchall())
    assert set(bounds) == {"undated_a", "undated_b"}
    assert all(v == BOUNDARY for v in bounds.values())


@_needs_pg
def test_08_pg_current_lifecycle_status_alone_never_becomes_an_event_date(
        seeded160):
    """Requirement 4. ``undated_b`` is a CUSTOMER — the strongest possible
    stage evidence that it passed through SQL — and it still gets no date.

    A stage proves a transition HAPPENED. It says nothing about WHEN, and this
    is the substitution that would be easiest to rationalise and most wrong.
    """
    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT lifecycle_stage, date_entered_sql, "
                    "date_entered_customer, created_at FROM "
                    "hubspot_contact_funnel WHERE contact_id = 'undated_b'")
        stage, sql_date, customer_date, created = cur.fetchone()

    assert stage == "customer"
    assert sql_date is None, "reaching customer does not date the SQL transition"
    assert customer_date is None
    assert created == LEGACY_CREATED
    # Nor did the bound leak into any neighbouring stage column.
    assert BOUNDARY not in (sql_date, customer_date, created)


@_needs_pg
def test_09_pg_applying_the_boundary_twice_is_idempotent(seeded160):
    """Requirement 10. Re-running rewrites; it never appends or double-counts."""
    first = boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    bid = first["boundary"]["boundary_id"]

    second = boundary_svc.establish_boundary(
        apply=True, observed_at=BOUNDARY, boundary_id=bid)

    assert second["ok"] is True
    assert second["already_applied"] is True
    assert second["contacts_written"] == first["contacts_written"] == 2

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sql_coverage_boundary")
        assert cur.fetchone()[0] == 1, "a second boundary row would be a second truth"
        cur.execute("SELECT COUNT(*) FROM sql_coverage_boundary_contact")
        assert cur.fetchone()[0] == 2, "bounds were rewritten, not appended"


@_needs_pg
def test_10_pg_a_second_boundary_is_refused_without_an_explicit_id(seeded160):
    """Two boundaries would be two answers to "when did the guarantee begin"."""
    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    again = boundary_svc.establish_boundary(apply=True)

    assert again["ok"] is False
    assert again["run_outcome"] == boundary_svc.BOUNDARY_ALREADY_ESTABLISHED
    assert again["boundary_written"] is False
    assert "two answers" in again["detail"]


def test_11_a_failed_population_read_cannot_establish_a_boundary(monkeypatch):
    """Requirement 11. Fail-closed, and never over an unknown population.

    A boundary established over an unreadable population would bound NOBODY
    while looking established — and would then silently fail to exclude anything
    from any window, for the rest of the system's life.
    """
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": None})
    monkeypatch.setattr(repo, "fetch_boundary_candidate_population",
                        lambda: {"available": False, "rows": []})

    result = boundary_svc.establish_boundary(apply=True)

    assert result["ok"] is False
    assert result["run_outcome"] == boundary_svc.POPULATION_UNREADABLE
    assert result["boundary_written"] is False
    assert result["contacts_written"] == 0
    # Unknown, never zero — "0 contacts to bound" is a claim this run cannot make.
    assert result["legacy_undated_bounded"] is None
    assert result["contacts_examined"] is None
    assert result["hubspot_writes_performed"] is False


def test_12_a_failed_local_write_cannot_report_success(monkeypatch):
    """Requirement 11. A refused write is never `ok`, and never resumable-looking."""
    from db import writers

    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": None})
    monkeypatch.setattr(repo, "fetch_boundary_candidate_population",
                        lambda: {"available": True, "rows": [
                            {"contact_id": "c1", "created_at": LEGACY_CREATED,
                             "lifecycle_stage": "salesqualifiedlead"}]})
    monkeypatch.setattr(writers, "apply_sql_coverage_boundary",
                        lambda b, c: {"ok": False, "error": "disk full",
                                      "contacts_written": 0})

    result = boundary_svc.establish_boundary(apply=True)

    assert result["ok"] is False
    assert result["run_outcome"] == boundary_svc.BOUNDARY_WRITE_FAILED
    assert "disk full" in result["detail"]
    assert result["boundary_written"] is False
    assert result["certification_can_begin"] is False


def test_13_the_boundary_store_being_unreadable_fails_closed(monkeypatch):
    """Unknown is not "no boundary". The two lead to opposite behaviour."""
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": False, "boundary": None})

    result = boundary_svc.establish_boundary(apply=True)

    assert result["ok"] is False
    assert result["run_outcome"] == boundary_svc.BOUNDARY_STORE_UNREADABLE
    assert result["boundary_written"] is False


# ═════════════════════════════════════════════════════════════════════════════
# §5 — prospective gap prevention
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_14_pg_a_sparse_payload_cannot_erase_a_proven_sql_timestamp(seeded160):
    """Requirement 5. The defect, reproduced exactly, now prevented.

    Proven against this same schema BEFORE the fix:

        first payload   date_entered_sql = 2026-09-02  -> stored
        later payload   property absent (NULL)         -> ok: True, persisted: 1
        stored value    None                           ← silently erased

    A write that REPORTS SUCCESS while destroying the only evidence a contact
    entered SQL is worse than a gap: it destroys the proof that anything was
    lost. A real correction must still get through, which is what separates this
    from simply never updating the column.
    """
    from db import writers

    proven = datetime(2026, 9, 2, tzinfo=timezone.utc)
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "prospective", "lifecycle_stage": "salesqualifiedlead",
        "created_at": LEGACY_CREATED, "last_modified_at": LEGACY_CREATED,
        "date_entered_sql": proven}])
    assert _sql_dates(seeded160.connection, ["prospective"])["prospective"] == proven

    # A LATER payload (so the staleness guard admits it) that omits the property.
    result = writers.upsert_hubspot_contact_funnel([{
        "contact_id": "prospective", "lifecycle_stage": "salesqualifiedlead",
        "created_at": LEGACY_CREATED,
        "last_modified_at": LEGACY_CREATED + timedelta(days=30)}])

    assert result["ok"] is True and result["persisted"] == 1
    assert _sql_dates(seeded160.connection, ["prospective"])["prospective"] == proven, (
        "a sparse payload must not erase a proven stage-entry timestamp")

    # A genuine correction — a DIFFERENT non-null date — must still win.
    corrected = datetime(2026, 9, 3, tzinfo=timezone.utc)
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "prospective", "lifecycle_stage": "salesqualifiedlead",
        "created_at": LEGACY_CREATED,
        "last_modified_at": LEGACY_CREATED + timedelta(days=31),
        "date_entered_sql": corrected}])
    assert _sql_dates(seeded160.connection, ["prospective"])["prospective"] == corrected


@_needs_pg
def test_15_pg_a_cleared_lifecycle_stage_still_propagates(seeded160):
    """The protection is narrow ON PURPOSE, and this proves the edge of it.

    Stage-entry evidence is append-only in HubSpot, so absence there means "not
    sent". Lifecycle STAGE is different: a cleared value is itself a real fact
    and must propagate, or the funnel would freeze at whatever stage it first
    saw. Coalescing everything would have been the easy over-correction.
    """
    from db import writers

    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "undated_a", "lifecycle_stage": None,
        "created_at": LEGACY_CREATED,
        "last_modified_at": LEGACY_CREATED + timedelta(days=10)}])

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT lifecycle_stage FROM hubspot_contact_funnel "
                    "WHERE contact_id = 'undated_a'")
        assert cur.fetchone()[0] is None, (
            "a cleared lifecycle stage is a fact and must propagate")


class _FakeHistoryClient:
    """A HubSpot client double for the history read. Records every call."""

    def __init__(self, history=None):
        self._history = history or {}
        self.calls: list = []


def _stub_history(monkeypatch, mapping):
    """Replace the READ-ONLY batch history call with a controlled answer."""
    calls: list = []

    def fake(ids, client=None):
        calls.append(list(ids))
        return {cid: mapping[cid] for cid in ids if cid in mapping}

    monkeypatch.setattr(hubspot, "fetch_lifecycle_stage_history", fake)
    return calls


@_needs_pg
def test_16_pg_a_post_boundary_contact_with_the_direct_property_is_counted_once(
        seeded160, monkeypatch):
    """Requirement 6. The happy path creates no incident and no duplicate."""
    from db import writers

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    calls = _stub_history(monkeypatch, {})

    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "new_ok", "lifecycle_stage": "salesqualifiedlead",
        "created_at": BOUNDARY + timedelta(days=1),
        "last_modified_at": BOUNDARY + timedelta(days=1),
        "date_entered_sql": BOUNDARY + timedelta(days=1)}])

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["ok"] is True
    assert result["new_sql_transitions_observed"] == 1
    assert result["direct_sql_timestamps_present"] == 1
    assert result["new_undated_sql_gaps"] == 0
    assert result["unresolved_post_boundary_incidents"] == 0
    assert calls == [], "a contact with a direct date needs no HubSpot read"
    assert result["hubspot_writes_performed"] is False

    # Counted once: exactly one row, and no incident.
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sql_post_boundary_incident")
        assert cur.fetchone()[0] == 0


@_needs_pg
def test_17_pg_a_post_boundary_contact_is_recovered_from_history_exactly_once(
        seeded160, monkeypatch):
    """Requirement 7. No direct property, but HubSpot holds a real transition."""
    from db import writers

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    transition = BOUNDARY + timedelta(days=2)
    _stub_history(monkeypatch, {"new_hist": {
        "state": hubspot.HISTORY_PRESENT,
        "versions": [{"value": "salesqualifiedlead", "timestamp": transition,
                      "source_type": "CRM_UI", "source_id": "u1",
                      "source_label": None, "updated_by_user_id": None}]}})

    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "new_hist", "lifecycle_stage": "salesqualifiedlead",
        "created_at": BOUNDARY + timedelta(days=1),
        "last_modified_at": BOUNDARY + timedelta(days=1)}])

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["ok"] is True
    assert result["history_timestamps_recovered"] == 1
    assert result["new_undated_sql_gaps"] == 0
    assert result["hubspot_writes_performed"] is False

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT contact_id, funnel_event, entered_at FROM "
                    "hubspot_lifecycle_stage_history")
        rows = cur.fetchall()
    assert len(rows) == 1, "exactly one recovered row, never a duplicate"
    assert rows[0][0] == "new_hist"
    assert rows[0][1] == lifecycle.EVENT_SQL
    # HubSpot's OWN timestamp, carried through unchanged — not the boundary,
    # not the creation time, not the detection time.
    assert rows[0][2] == transition


@_needs_pg
def test_18_pg_a_post_boundary_contact_with_neither_source_raises_an_incident(
        seeded160, monkeypatch):
    """Requirement 8. The gap becomes visible instead of joining the 533."""
    from db import writers

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    _stub_history(monkeypatch, {"new_gap": {
        "state": hubspot.HISTORY_PRESENT,
        # Real history, but no SQL transition in it.
        "versions": [{"value": "lead", "timestamp": BOUNDARY,
                      "source_type": "FORM", "source_id": "f1",
                      "source_label": None, "updated_by_user_id": None}]}})

    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "new_gap", "lifecycle_stage": "salesqualifiedlead",
        "created_at": BOUNDARY + timedelta(days=1),
        "last_modified_at": BOUNDARY + timedelta(days=1)}])

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["ok"] is True
    assert result["new_undated_sql_gaps"] == 1
    assert result["unresolved_post_boundary_incidents"] == 1
    assert result["incident_reasons"] == {
        boundary_svc.INCIDENT_HISTORY_NO_SQL: 1}

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT contact_id, reason, status, history_checked FROM "
                    "sql_post_boundary_incident")
        row = cur.fetchone()
    assert row[0] == "new_gap"
    assert row[1] == boundary_svc.INCIDENT_HISTORY_NO_SQL
    assert row[2] == "open"
    # "We looked and found nothing" is recorded as such, never as "we did not
    # look" and never as a silent zero.
    assert row[3] is True

    # And critically: no date was invented for it.
    assert _sql_dates(seeded160.connection, ["new_gap"])["new_gap"] is None


@_needs_pg
def test_19_pg_an_incident_resolves_only_when_a_real_timestamp_arrives(
        seeded160, monkeypatch):
    """Resolution is attributable to evidence, never to the passage of time."""
    from db import writers

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    _stub_history(monkeypatch, {"late": {"state": hubspot.HISTORY_PRESENT,
                                         "versions": []}})
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "late", "lifecycle_stage": "salesqualifiedlead",
        "created_at": BOUNDARY + timedelta(days=1),
        "last_modified_at": BOUNDARY + timedelta(days=1)}])
    first = boundary_svc.detect_post_boundary_gaps(apply=True)
    assert first["new_undated_sql_gaps"] == 1

    # HubSpot later supplies the direct property.
    real = BOUNDARY + timedelta(days=3)
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "late", "lifecycle_stage": "salesqualifiedlead",
        "created_at": BOUNDARY + timedelta(days=1),
        "last_modified_at": BOUNDARY + timedelta(days=5),
        "date_entered_sql": real}])

    second = boundary_svc.detect_post_boundary_gaps(apply=True)
    assert second["unresolved_post_boundary_incidents"] == 0

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT status, resolved_by FROM sql_post_boundary_incident "
                    "WHERE contact_id = 'late'")
        status, resolved_by = cur.fetchone()
    assert status == "resolved"
    assert resolved_by == "direct_property"


def test_20_an_unreadable_incident_store_reports_null_not_zero(monkeypatch):
    """A window must not certify because an outage made its blockers invisible."""
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": {
                            "boundary_id": "b1", "observed_at": BOUNDARY}})
    monkeypatch.setattr(repo, "fetch_post_boundary_sql_contacts",
                        lambda *, since: {"available": True, "rows": []})
    monkeypatch.setattr(repo, "fetch_post_boundary_incidents",
                        lambda *, status=None: {"available": False, "rows": [],
                                                "open_count": None})

    result = boundary_svc.detect_post_boundary_gaps(apply=False)

    assert result["ok"] is True
    assert result["unresolved_post_boundary_incidents"] is None
    assert result["incident_store_available"] is False


def test_21_a_failed_gap_pass_reports_unknown_counts_not_zero(monkeypatch):
    """An aborted pass proves nothing about how many prospective gaps exist."""
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": {
                            "boundary_id": "b1", "observed_at": BOUNDARY}})
    monkeypatch.setattr(repo, "fetch_post_boundary_sql_contacts",
                        lambda *, since: {"available": False, "rows": []})

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["ok"] is False
    assert result["run_outcome"] == boundary_svc.POPULATION_UNREADABLE
    for field in ("new_sql_transitions_observed", "new_undated_sql_gaps",
                  "unresolved_post_boundary_incidents"):
        assert result[field] is None, f"{field} became a number it never read"


# ═════════════════════════════════════════════════════════════════════════════
# §6 — certification
# ═════════════════════════════════════════════════════════════════════════════

def test_22_an_open_incident_blocks_certification_of_affected_windows():
    """Requirement 9. One unresolved prospective gap withholds certification."""
    clean = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=5, recovered_sqls=0,
        unresolved_rows=[{"contact_id": "c1", "created_at": LEGACY_CREATED,
                          "known_reached_sql_by": BOUNDARY}],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=0)
    assert clean["certification_status"] == coverage.CERT_ELIGIBLE
    assert clean["certification_eligible"] is True

    blocked = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=5, recovered_sqls=0,
        unresolved_rows=[{"contact_id": "c1", "created_at": LEGACY_CREATED,
                          "known_reached_sql_by": BOUNDARY}],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=1)
    assert blocked["certification_status"] == coverage.CERT_POST_BOUNDARY_GAPS
    assert blocked["certification_eligible"] is False
    # The window is still COMPLETE for the historical population — completeness
    # and certification are different questions and must not collapse.
    assert blocked["window_total_complete"] is True


@pytest.mark.parametrize("start,end,expected", [
    (date(2026, 10, 1), date(2026, 10, 31), coverage.CERT_ELIGIBLE),
    (date(2026, 9, 1), date(2026, 9, 30), coverage.CERT_OVERLAPS_BOUNDARY),
    (date(2026, 1, 1), date(2026, 6, 30), coverage.CERT_PRE_BOUNDARY),
    (None, None, coverage.CERT_PRE_BOUNDARY),
])
def test_23_certification_depends_on_where_the_window_sits(start, end, expected):
    block = coverage.window_coverage(
        window="w", window_end=end, window_start=start,
        confirmed_sqls=1, recovered_sqls=0,
        unresolved_rows=[{"contact_id": "c1", "created_at": LEGACY_CREATED,
                          "known_reached_sql_by": BOUNDARY}],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=0)
    assert block["certification_status"] == expected


def test_24_no_boundary_means_no_window_is_certifiable():
    block = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=1, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=None, open_post_boundary_incidents=0)
    assert block["certification_status"] == coverage.CERT_NO_BOUNDARY
    assert block["certification_eligible"] is False


def test_25_unavailable_inputs_make_certification_unknown_not_refused():
    block = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=None, recovered_sqls=None, unresolved_rows=[],
        population_available=False, boundary_observed_at=BOUNDARY)
    assert block["certification_status"] == coverage.CERT_UNAVAILABLE
    assert block["window_after_boundary"] is None
    assert block["open_post_boundary_gaps"] is None


def test_26_all_time_coverage_stays_incomplete_forever():
    """Requirement 13. The boundary does not, and cannot, complete All Time.

    All Time necessarily contains the historical period. No amount of bounding
    changes that a transition happened at an unknown instant inside it.
    """
    block = coverage.window_coverage(
        window="all_time", window_end=None, window_start=None,
        confirmed_sqls=728, recovered_sqls=0,
        unresolved_rows=[{"contact_id": f"c{i}", "created_at": LEGACY_CREATED,
                          "known_reached_sql_by": BOUNDARY} for i in range(533)],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=0)

    assert block["window_total_complete"] is False
    assert block["window_membership_unresolved"] == 533
    assert block["complete_sql_total"] is None
    assert block["cpql_publishable"] is False
    assert block["certification_eligible"] is False


# ═════════════════════════════════════════════════════════════════════════════
# §6/§7 — the audit and the gate, executed
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_27_pg_all_44_combinations_still_reconcile_with_a_boundary(seeded160):
    """Requirement 12. The boundary changes membership, never the read paths."""
    from scripts import audit_lifecycle_sql_coverage as audit

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)

    f = audit.Findings()
    block = audit.audit_read_reconciliation(f, datetime.now(tz=timezone.utc))

    assert block["combinations_expected"] == 44
    assert block["combinations_compared"] == 44
    assert block["combinations_execution_unavailable"] == 0
    assert block["reconciliation_complete"] is True
    assert f.violations == []


@_needs_pg
def test_28_pg_the_audit_reports_historical_and_prospective_gaps_separately(
        seeded160, monkeypatch):
    """The two kinds of gap must never be added under one heading.

    "A date HubSpot does not hold" and "a date we failed to capture" have
    different remedies. Summing them would hide the second inside the first —
    which is exactly how the prospective gap would go unnoticed.
    """
    import os

    from scripts import audit_lifecycle_sql_coverage as audit

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    _stub_history(monkeypatch, {"gap": {"state": hubspot.HISTORY_PRESENT,
                                        "versions": []}})
    from db import writers

    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "gap", "lifecycle_stage": "salesqualifiedlead",
        "created_at": BOUNDARY + timedelta(days=1),
        "last_modified_at": BOUNDARY + timedelta(days=1)}])
    boundary_svc.detect_post_boundary_gaps(apply=True)

    monkeypatch.setenv("DATABASE_URL", seeded160.url)
    monkeypatch.setattr(sys, "argv", ["audit", "--json"])
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        audit.main()
    report = json.loads(buf.getvalue())

    bound = report["boundary"]
    assert bound["boundary_established"] is True
    assert bound["legacy_undated_bounded"] == 2      # the historical side
    assert bound["open_post_boundary_incidents"] == 1  # the prospective side
    assert bound["post_boundary_incident_reasons"] == {
        boundary_svc.INCIDENT_HISTORY_NO_SQL: 1}
    # Two separate numbers under two separate headings, never one total.
    assert bound["legacy_undated_bounded"] != bound["open_post_boundary_incidents"]


@_needs_pg
def test_29_pg_the_gate_holds_on_a_clean_system(seeded160):
    """Every guarantee is checked by execution, over a real schema."""
    from scripts import audit_sql_coverage_gate as gate_mod

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)

    g, report = gate_mod.run()

    assert g.violations == [], g.violations
    assert report["stage_dates_not_erasable"]["protected"] is True
    assert report["boundary_in_event_dates"]["contaminated"] == 0
    assert report["post_boundary_gaps"]["open"] == 0
    assert report["external_writes_performed"] is False
    assert report["database_writes_performed"] is False


@_needs_pg
def test_30_pg_the_gate_fails_when_a_post_boundary_gap_is_open(
        seeded160, monkeypatch):
    """§7.1 — the gate must go red the moment a prospective gap appears."""
    from db import writers
    from scripts import audit_sql_coverage_gate as gate_mod

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    _stub_history(monkeypatch, {"gap": {"state": hubspot.HISTORY_PRESENT,
                                        "versions": []}})
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "gap", "lifecycle_stage": "salesqualifiedlead",
        "created_at": BOUNDARY + timedelta(days=1),
        "last_modified_at": BOUNDARY + timedelta(days=1)}])
    boundary_svc.detect_post_boundary_gaps(apply=True)

    g, report = gate_mod.run()

    assert any("no_open_post_boundary_gaps" in v for v in g.violations)
    assert g.exit_code == gate_mod.EXIT_VIOLATION
    assert report["post_boundary_gaps"]["open"] == 1


@_needs_pg
def test_31_pg_the_gate_detects_a_boundary_instant_used_as_an_event_date(
        seeded160):
    """§7.3 — the contamination signature, planted and then detected.

    This is the failure the whole design exists to make impossible. The test
    creates it deliberately, by direct SQL, to prove the gate would catch it if
    some future code path ever did.
    """
    from scripts import audit_sql_coverage_gate as gate_mod

    boundary_svc.establish_boundary(apply=True, observed_at=BOUNDARY)
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("UPDATE hubspot_contact_funnel SET date_entered_sql = %s "
                    "WHERE contact_id = 'undated_a'", (BOUNDARY,))
        c.commit()

    g, report = gate_mod.run()

    assert report["boundary_in_event_dates"]["contaminated"] == 1
    assert any("no_boundary_timestamp_in_event_dates" in v for v in g.violations)
    assert g.exit_code == gate_mod.EXIT_VIOLATION


def test_32_the_gate_reports_unavailable_rather_than_passing_when_blind(
        monkeypatch):
    """A gate that cannot look must never report that everything is fine."""
    from scripts import audit_sql_coverage_gate as gate_mod

    monkeypatch.setattr(repo, "fetch_post_boundary_incidents",
                        lambda *, status=None: {"available": False, "rows": [],
                                                "open_count": None})
    g = gate_mod.Gate()
    out = gate_mod.check_no_open_post_boundary_gaps(g)

    assert out["open"] is None, "an unreadable store is never 0 open incidents"
    assert g.unavailable and not g.violations
    assert g.exit_code == gate_mod.EXIT_UNAVAILABLE


# ═════════════════════════════════════════════════════════════════════════════
# §3 — the CLI, through a real process
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_33_pg_the_cli_dry_run_writes_nothing_and_says_so(seeded160):
    """A subprocess, so the standalone init path is exercised for real."""
    import os

    env = {**os.environ, "DATABASE_URL": seeded160.url}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.establish_sql_coverage_boundary",
         "--json"],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)

    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["mode"] == "dry_run"
    assert payload["boundary_written"] is False
    assert payload["hubspot_writes_performed"] is False
    assert payload["legacy_undated_bounded"] == 2
    assert repo.fetch_active_sql_coverage_boundary()["boundary"] is None


def test_34_the_cli_fails_closed_without_a_database(monkeypatch):
    """No database means no boundary — never an empty-population boundary."""
    import os

    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.establish_sql_coverage_boundary",
         "--apply", "--json"],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["boundary_written"] is False
    assert payload["hubspot_writes_performed"] is False


# ═════════════════════════════════════════════════════════════════════════════
# Requirement 14 — the doctrine, enforced over this suite's own source
# ═════════════════════════════════════════════════════════════════════════════

def test_35_no_test_here_uses_a_forbidden_substitute_as_an_sql_timestamp():
    """Requirement 14, checked against this file rather than promised.

    A suite that proved the production code never substitutes a date, while
    itself asserting that some substitute IS the date, would have encoded the
    very confusion it exists to prevent. So the rule is applied here too: no
    assertion in this file may equate a stage-entry date with creation time, the
    boundary instant, a neighbouring stage date, or the current stage.
    """
    import ast as _ast

    source = Path(__file__).read_text(encoding="utf-8")

    # This function necessarily NAMES every pattern it forbids, so scanning the
    # whole file would make the check fail on its own definition. Excise it
    # first; a guard that cannot survive its own rule is not a guard.
    tree = _ast.parse(source)
    lines = source.splitlines()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name.startswith(
                "test_35_no_test_here"):
            for n in range(node.lineno - 1, (node.end_lineno or node.lineno)):
                lines[n] = ""
    body = "\n".join(lines)

    # Built from parts, so the forbidden strings never appear literally above.
    date_field = "date_entered_" + "sql"
    substitutes = {
        "LEGACY_CREATED": "contact creation time",
        "BOUNDARY": "the boundary observation instant",
        "created": "contact creation time",
        "customer_date": "a neighbouring stage date",
        "stage": "the current lifecycle stage",
    }
    subscripts = [f'{date_field}"] == ', f"{date_field}'] == ", "sql_date == "]

    for prefix in subscripts:
        for name, why in substitutes.items():
            needle = prefix + name
            assert needle not in body, (
                f"this suite asserts {why} IS the SQL entry date — the exact "
                f"substitution the whole PR exists to forbid")

    # The positive control: the suite DOES assert the bounded contacts keep a
    # NULL SQL date, which is the only correct outcome for all 533.
    assert 'after["undated_a"] is None' in body
    assert "sql_date is None" in body


def test_36_the_boundary_service_has_no_hubspot_write_path():
    """Executed against the module source: no SDK write entry point exists."""
    source = (_ROOT / "services" / "sql_coverage_boundary_service.py").read_text(
        encoding="utf-8")
    for forbidden in ("basic_api.update", "batch_api.update", "basic_api.create",
                      "batch_api.create", "basic_api.archive", ".update(",
                      "_api.create("):
        if forbidden == ".update(":
            # A local dict.update is fine; an SDK client call is not.
            assert "client.crm" not in source or "_api.update(" not in source
            continue
        assert forbidden not in source, (
            f"{forbidden} would be a HubSpot write from a module that "
            f"guarantees it performs none")

    # And every return path states it explicitly.
    assert source.count('"hubspot_writes_performed": False') >= 3


def test_37_every_run_names_exactly_one_outcome_from_its_vocabulary(monkeypatch):
    """The vocabulary is reachable and single-valued, proven by executing it."""
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": False, "boundary": None})
    stopped = boundary_svc.establish_boundary(apply=False)
    assert stopped["run_outcome"] in boundary_svc.RUN_OUTCOMES

    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": None})
    monkeypatch.setattr(repo, "fetch_boundary_candidate_population",
                        lambda: {"available": True, "rows": []})
    ok = boundary_svc.establish_boundary(apply=False)
    assert ok["run_outcome"] == boundary_svc.RUN_OK

    # The two vocabularies count different things and must never share a value.
    run_values = set(boundary_svc.RUN_OUTCOMES)
    incident_values = set(boundary_svc.INCIDENT_REASONS)
    assert not (run_values & incident_values), (
        "a value in both vocabularies lets a per-run count and a per-contact "
        "count be added under one heading")


def test_38_history_reads_are_chunked_to_hubspots_batch_limit(monkeypatch):
    """More than 50 contacts must still be READ, not silently marked unreadable.

    ``fetch_lifecycle_stage_history`` REFUSES more than 50 contacts rather than
    truncating. Handing it the whole request budget in one call would raise, the
    surrounding except would swallow it, and every contact would be reported as
    "history unreadable" — a request that never asks, reported as an answer.

    That is precisely the PR-ADS-159 §1 defect (a dict body meant history was
    never requested, and 50 contacts read as "HubSpot holds nothing"), and this
    test exists so it cannot reappear one layer up.
    """
    limit = hubspot.HUBSPOT_HISTORY_BATCH_LIMIT
    count = limit * 2 + 7
    rows = [{"contact_id": f"c{i:03d}", "created_at": BOUNDARY,
             "lifecycle_stage": "salesqualifiedlead"} for i in range(count)]

    seen_chunks: list = []
    transition = BOUNDARY + timedelta(hours=1)

    def fake(ids, client=None):
        assert len(ids) <= limit, (
            f"{len(ids)} contacts sent to a batch endpoint that accepts {limit}")
        seen_chunks.append(list(ids))
        return {cid: {"state": hubspot.HISTORY_PRESENT,
                      "versions": [{"value": "salesqualifiedlead",
                                    "timestamp": transition,
                                    "source_type": "CRM_UI", "source_id": "u1",
                                    "source_label": None,
                                    "updated_by_user_id": None}]}
                for cid in ids}

    monkeypatch.setattr(hubspot, "fetch_lifecycle_stage_history", fake)

    recovered, incidents, requests = boundary_svc._consult_history(
        rows, boundary_id="b1", client=None, budget=1000)

    assert len(seen_chunks) == 3, "the population must be chunked, not truncated"
    assert sum(len(c) for c in seen_chunks) == count, "every contact was read"
    assert len(recovered) == count, "every contact's real transition recovered"
    assert incidents == [], "a successful read produces no incident"
    assert requests == 3


def test_39_one_failed_chunk_does_not_condemn_the_others(monkeypatch):
    """A failed request is not evidence of absence — and only for its own chunk.

    The contacts in a chunk that raised are ``history_request_failed``: we could
    not look. The contacts in chunks that succeeded keep their real answers. The
    tempting shortcut — one exception, empty history for everybody — would
    publish "HubSpot holds no SQL transition for these contacts" on the strength
    of a call that never returned.
    """
    limit = hubspot.HUBSPOT_HISTORY_BATCH_LIMIT
    rows = [{"contact_id": f"c{i:03d}", "created_at": BOUNDARY,
             "lifecycle_stage": "salesqualifiedlead"}
            for i in range(limit + 3)]
    transition = BOUNDARY + timedelta(hours=1)
    calls = {"n": 0}

    def fake(ids, client=None):
        calls["n"] += 1
        if calls["n"] == 2:          # the second chunk fails
            raise RuntimeError("HubSpot 503")
        return {cid: {"state": hubspot.HISTORY_PRESENT,
                      "versions": [{"value": "salesqualifiedlead",
                                    "timestamp": transition,
                                    "source_type": "CRM_UI", "source_id": "u1",
                                    "source_label": None,
                                    "updated_by_user_id": None}]}
                for cid in ids}

    monkeypatch.setattr(hubspot, "fetch_lifecycle_stage_history", fake)

    recovered, incidents, _requests = boundary_svc._consult_history(
        rows, boundary_id="b1", client=None, budget=1000)

    assert len(recovered) == limit, "the successful chunk still recovered"
    assert len(incidents) == 3, "only the failed chunk's contacts are incidents"
    assert {i["reason"] for i in incidents} == {
        boundary_svc.INCIDENT_HISTORY_UNREADABLE}
    # And they are marked as LOOKED-AT-BUT-UNANSWERED, never as "no history".
    assert all(i["history_checked"] is True for i in incidents)
    assert all(i["reason"] != boundary_svc.INCIDENT_HISTORY_NO_SQL
               for i in incidents)


def test_40_a_contact_beyond_the_request_budget_is_unattempted_not_absent(
        monkeypatch):
    """The budget is a ceiling on work, never a verdict about evidence."""
    rows = [{"contact_id": f"c{i}", "created_at": BOUNDARY,
             "lifecycle_stage": "salesqualifiedlead"} for i in range(5)]

    monkeypatch.setattr(hubspot, "fetch_lifecycle_stage_history",
                        lambda ids, client=None: {
                            cid: {"state": hubspot.HISTORY_PRESENT,
                                  "versions": []} for cid in ids})

    recovered, incidents, _requests = boundary_svc._consult_history(
        rows, boundary_id="b1", client=None, budget=2)

    assert recovered == []
    by_reason = {}
    for i in incidents:
        by_reason[i["reason"]] = by_reason.get(i["reason"], 0) + 1
    # 2 were read and genuinely hold no SQL transition; 3 were never funded.
    assert by_reason == {boundary_svc.INCIDENT_HISTORY_NO_SQL: 2,
                         boundary_svc.INCIDENT_NO_DIRECT_DATE: 3}
    unfunded = [i for i in incidents
                if i["reason"] == boundary_svc.INCIDENT_NO_DIRECT_DATE]
    assert all(i["history_checked"] is False for i in unfunded), (
        "an unfunded contact must never look like one we checked")
