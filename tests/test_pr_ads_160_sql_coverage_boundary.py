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
import re
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
#:
#: PR-ADS-160 §2 removed every operator-facing way to choose this. The boundary
#: time is stamped DATABASE-SIDE inside the write transaction, so tests inject a
#: deterministic SQL clock EXPRESSION through the private `_clock_sql` seam —
#: never a timestamp value, and never through any CLI surface.
BOUNDARY = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_FIXED_CLOCK = "TIMESTAMPTZ '2026-09-14 12:00:00+00'"


def _establish(apply=True):
    """Establish the boundary with a deterministic, database-side instant."""
    return boundary_svc.establish_boundary(
        apply=apply, _clock_sql=_FIXED_CLOCK if apply else None)


#: A contact created long before the boundary — the shape of all 533.
LEGACY_CREATED = datetime(2020, 3, 1, tzinfo=timezone.utc)

#: A freshness verdict good enough to certify against, for the pure-analysis
#: tests. The real contract is exercised against a real sync state in §5.
FRESH = {"fresh": True, "reason": "source_fresh"}


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
    # Opens exactly AT the boundary instant. PR-ADS-160 §7: the comparison is
    # STRICT, so this stays UNRESOLVED. A window start is inclusive, so the
    # transition could have occurred at that very instant; ruling the contact
    # out would need interval semantics this system has not proven.
    (BOUNDARY, date(2026, 10, 31), "unresolved"),
    # One second AFTER the boundary: now strictly later, so it is ruled out.
    (BOUNDARY + timedelta(seconds=1), date(2026, 10, 31), "proven_outside"),
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

    # PR-ADS-160 §5: certification now REQUIRES a fresh contact-funnel source.
    # A fixture that omitted this would silently exercise the stale path.
    #
    # §3: freshness reads the PROVEN successful incremental, not
    # `last_incremental_at` — which a bootstrap stamps too. A fixture that set
    # only the old column would be indistinguishable from a backfill and would
    # fail closed, so it writes the provenance the contract actually reads.
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete",
        last_incremental_at=datetime.now(tz=timezone.utc),
        last_modified_watermark=datetime.now(tz=timezone.utc),
        last_status="success", last_sync_mode="incremental",
        last_incremental_status="success",
        last_successful_incremental_at=datetime.now(tz=timezone.utc),
        last_error=None)

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
    result = _establish()
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
    _establish()

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
def test_09_pg_an_exact_replay_is_a_verified_no_op(seeded160):
    """Requirement 10, under §3's immutability: VERIFIED, never rewritten.

    The first cut used ``ON CONFLICT DO UPDATE``, so a replay silently rewrote
    the stored rows. A completed boundary records an observation that has
    passed; rewriting it would change the meaning of every window verdict
    already derived from it, and nothing would record that it had.

    An identical replay therefore verifies and writes nothing.
    """
    from db import writers

    first = _establish()
    bid = first["boundary"]["boundary_id"]
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT boundary_id, observed_at, contacts_bounded, "
                    "completed_at FROM sql_coverage_boundary")
        before = cur.fetchall()

    second = writers.establish_sql_coverage_boundary(
        {**first["boundary"], "boundary_id": bid}, clock_sql=_FIXED_CLOCK)

    assert second["ok"] is True
    assert second["already_applied"] is True
    assert second["contacts_written"] == first["contacts_written"] == 2

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sql_coverage_boundary")
        assert cur.fetchone()[0] == 1, "a second boundary row would be a second truth"
        cur.execute("SELECT COUNT(*) FROM sql_coverage_boundary_contact")
        assert cur.fetchone()[0] == 2, "bounds are never appended"
        cur.execute("SELECT boundary_id, observed_at, contacts_bounded, "
                    "completed_at FROM sql_coverage_boundary")
        assert cur.fetchall() == before, (
            "an exact replay must leave the stored boundary byte-identical")


@_needs_pg
def test_10_pg_a_second_boundary_is_refused_and_there_is_no_override(seeded160):
    """Two boundaries would be two answers to "when did the guarantee begin".

    PR-ADS-160 §3 removed the replacement path entirely: there is no flag, no
    parameter, and no service argument that establishes a second one.
    """
    _establish()
    again = boundary_svc.establish_boundary(apply=True)

    assert again["ok"] is False
    assert again["run_outcome"] == boundary_svc.BOUNDARY_ALREADY_ESTABLISHED
    assert again["boundary_written"] is False
    assert "no replacement path" in again["detail"]

    # The service exposes no way to ask for a different boundary id.
    import inspect

    params = set(inspect.signature(boundary_svc.establish_boundary).parameters)
    assert "boundary_id" not in params
    assert "observed_at" not in params


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
    monkeypatch.setattr(writers, "establish_sql_coverage_boundary",
                        lambda b, **k: {"ok": False, "error": "disk full",
                                        "contacts_written": 0,
                                        "observed_at": None})

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

    _establish()
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

    _establish()
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

    _establish()
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

    _establish()
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


def test_20_an_unreadable_incident_store_fails_the_run_closed(monkeypatch):
    """Requirement §4. An UNVERIFIED guarantee is not a healthy one.

    The first cut returned ``ok: True`` with a null open-incident count. Every
    consumer that checks only ``ok`` — the scheduler among them — would read
    that as "checked, all clear", when in fact nothing was verified. The run now
    fails, and says what it could not confirm.
    """
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": {
                            "boundary_id": "b1", "observed_at": BOUNDARY}})
    monkeypatch.setattr(repo, "fetch_post_boundary_sql_contacts",
                        lambda *, boundary_id=None, since=None: {
                            "available": True, "rows": []})
    monkeypatch.setattr(repo, "fetch_post_boundary_incidents",
                        lambda *, status=None: {"available": False, "rows": [],
                                                "open_count": None})

    result = boundary_svc.detect_post_boundary_gaps(apply=False)

    assert result["ok"] is False
    assert result["run_outcome"] == boundary_svc.INCIDENT_STORE_UNREADABLE
    assert result["unresolved_post_boundary_incidents"] is None
    assert "unverified" in result["detail"]


def test_21_a_failed_gap_pass_reports_unknown_counts_not_zero(monkeypatch):
    """An aborted pass proves nothing about how many prospective gaps exist."""
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": {
                            "boundary_id": "b1", "observed_at": BOUNDARY}})
    monkeypatch.setattr(repo, "fetch_post_boundary_sql_contacts",
                        lambda *, boundary_id=None, since=None: {"available": False, "rows": []})

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
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=0,
        freshness=FRESH)
    assert clean["certification_status"] == coverage.CERT_ELIGIBLE
    assert clean["certification_eligible"] is True

    blocked = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=5, recovered_sqls=0,
        unresolved_rows=[{"contact_id": "c1", "created_at": LEGACY_CREATED,
                          "known_reached_sql_by": BOUNDARY}],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=1,
        freshness=FRESH)
    assert blocked["certification_status"] == coverage.CERT_POST_BOUNDARY_GAPS
    assert blocked["certification_eligible"] is False
    # §2 (second review). The HISTORICAL half is still complete — every undated
    # legacy contact was ruled out of this window — but an open prospective gap
    # could belong to it, so the WINDOW TOTAL is not complete and no total is
    # publishable. The two halves are reported apart precisely so that a
    # complete historical population can never be read as a complete window.
    assert blocked["historical_membership_complete"] is True
    assert blocked["prospective_membership_complete"] is False
    assert blocked["window_total_complete"] is False
    assert blocked["complete_sql_total"] is None
    assert blocked["cpql_publishable"] is False
    # The confirmed dated subset stays visible under its own qualified name.
    assert blocked["confirmed_sql_subset"] == 5


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
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=0,
        freshness=FRESH)
    assert block["certification_status"] == expected


def test_24_no_boundary_means_no_window_is_certifiable():
    block = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=1, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=None, open_post_boundary_incidents=0,
        freshness=FRESH)
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
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=0,
        freshness=FRESH)

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

    _establish()

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

    _establish()
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

    _establish()

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

    _establish()
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

    _establish()
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


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the OLD contact promoted AFTER the boundary
#
# The central failure mode, and the one the first cut could not see. Both of its
# predicates — `created_at >= boundary` and `effective_date >= boundary` — are
# FALSE for this contact, so it was invisible:
#
#     created long before the boundary;
#     BELOW SQL when the snapshot was taken, so absent from it;
#     promoted to SQL afterwards;
#     no exact SQL timestamp captured.
#
# Creation date cannot classify it. The immutable snapshot can, and is the only
# thing that can: it is the record of who was already historical.
# ═════════════════════════════════════════════════════════════════════════════

def _no_history(monkeypatch):
    """HubSpot answers with real, EMPTY history — so an incident is genuine."""
    monkeypatch.setattr(
        hubspot, "fetch_lifecycle_stage_history",
        lambda ids, client=None: {cid: {"state": hubspot.HISTORY_PRESENT,
                                        "versions": []} for cid in ids})


@_needs_pg
def test_41_pg_an_old_contact_promoted_after_the_boundary_is_a_prospective_gap(
        seeded160, monkeypatch):
    """Created long before B, below SQL at B, promoted after, no timestamp."""
    from db import writers

    # Below SQL when the snapshot is taken — so NOT in it.
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "promoted_later", "lifecycle_stage": "lead",
        "created_at": LEGACY_CREATED, "last_modified_at": LEGACY_CREATED}])

    established = _establish()
    assert established["ok"]
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT contact_id FROM sql_coverage_boundary_contact "
                    "ORDER BY contact_id")
        snapshot = [r[0] for r in cur.fetchall()]
    assert "promoted_later" not in snapshot, (
        "a contact below SQL at boundary time must not be in the snapshot")

    # Now promoted to SQL, with NO exact timestamp. Its creation date is old,
    # and it has no effective date at all — both old predicates are false.
    _no_history(monkeypatch)
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "promoted_later", "lifecycle_stage": "salesqualifiedlead",
        "created_at": LEGACY_CREATED,
        "last_modified_at": BOUNDARY + timedelta(days=3)}])

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["ok"] is True
    assert result["new_undated_sql_gaps"] == 1, (
        "an old contact promoted after the boundary with no SQL timestamp is a "
        "PROSPECTIVE gap, however old its creation date")
    assert result["unresolved_post_boundary_incidents"] == 1

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT contact_id FROM sql_post_boundary_incident")
        assert [r[0] for r in cur.fetchall()] == ["promoted_later"]
        # And no date was invented for it.
        cur.execute("SELECT date_entered_sql FROM hubspot_contact_funnel "
                    "WHERE contact_id = 'promoted_later'")
        assert cur.fetchone()[0] is None


@_needs_pg
def test_42_pg_a_snapshot_contact_stays_historical_and_raises_no_incident(
        seeded160, monkeypatch):
    """The other side of the anti-join: the snapshot IS the classifier.

    ``undated_a`` and ``undated_b`` were undated at SQL when the boundary was
    taken, so they are in the snapshot and are historical forever. They must
    never become prospective incidents, however many times detection runs.
    """
    _establish()
    _no_history(monkeypatch)

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["new_undated_sql_gaps"] == 0
    assert result["unresolved_post_boundary_incidents"] == 0
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sql_post_boundary_incident")
        assert cur.fetchone()[0] == 0, (
            "a contact recorded in the boundary snapshot is historical, and "
            "must never be reported as a prospective gap")


@_needs_pg
def test_43_pg_a_promoted_contacts_incident_blocks_only_relevant_windows(
        seeded160, monkeypatch):
    """§1 + §6 together: the gap blocks certification where it could belong."""
    from db import writers

    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "promoted_later", "lifecycle_stage": "lead",
        "created_at": LEGACY_CREATED, "last_modified_at": LEGACY_CREATED}])
    _establish()
    _no_history(monkeypatch)
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "promoted_later", "lifecycle_stage": "salesqualifiedlead",
        "created_at": LEGACY_CREATED,
        "last_modified_at": BOUNDARY + timedelta(days=3)}])
    boundary_svc.detect_post_boundary_gaps(apply=True)

    incidents = repo.fetch_post_boundary_incidents(status="open")
    assert incidents["available"] and incidents["open_count"] == 1
    rows = incidents["rows"]

    # A window opening AFTER the incident was detected: it could belong.
    detected = rows[0]["detected_at"]
    after = coverage.window_coverage(
        window="after", window_start=detected, window_end=None,
        confirmed_sqls=0, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=rows,
        freshness=FRESH)
    assert after["open_post_boundary_gaps"] == 1

    # A window that CLOSED before the contact was created: it cannot.
    before = coverage.window_coverage(
        window="ancient", window_start=date(2019, 1, 1),
        window_end=date(2019, 12, 31), confirmed_sqls=0, recovered_sqls=0,
        unresolved_rows=[], boundary_observed_at=BOUNDARY,
        open_post_boundary_incidents=rows, freshness=FRESH)
    assert before["open_post_boundary_gaps"] == 0
    assert before["post_boundary_gaps_ruled_out"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §2 — the boundary timestamp is not the operator's to choose
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_44_pg_an_operator_cannot_backdate_the_boundary(seeded160):
    """Requirement §2. There is no surface that accepts a historical instant.

    A backdated boundary would rule contacts out of windows on the strength of
    an observation that never happened at that time — the bound would be a
    fiction, and every window after it would inherit it.
    """
    import inspect
    import os

    # 1. No service parameter accepts one.
    params = inspect.signature(boundary_svc.establish_boundary).parameters
    assert "observed_at" not in params
    assert not any("timestamp" in p or "instant" in p for p in params)

    # 2. No CLI flag exposes one.
    env = {**os.environ, "DATABASE_URL": seeded160.url}
    helped = subprocess.run(
        [sys.executable, "-m", "scripts.establish_sql_coverage_boundary",
         "--help"], capture_output=True, text=True, cwd=str(_ROOT), env=env)
    assert "--observed-at" not in helped.stdout
    assert "--boundary-id" not in helped.stdout

    # 3. The CLI REJECTS one if a user tries anyway.
    tried = subprocess.run(
        [sys.executable, "-m", "scripts.establish_sql_coverage_boundary",
         "--apply", "--observed-at", "2020-01-01T00:00:00Z"],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)
    assert tried.returncode != 0
    assert repo.fetch_active_sql_coverage_boundary()["boundary"] is None


@_needs_pg
def test_45_pg_the_boundary_instant_is_stamped_after_the_population_read(
        seeded160):
    """The instant comes from the database, and never precedes the snapshot.

    Stamped with ``clock_timestamp()`` rather than ``now()``: ``now()`` returns
    TRANSACTION START time, which in this transaction precedes the population
    read — so the boundary would claim to have observed the population at an
    instant before it actually did.
    """
    before = datetime.now(tz=timezone.utc)
    result = boundary_svc.establish_boundary(apply=True)
    after = datetime.now(tz=timezone.utc)

    assert result["ok"] is True
    observed = result["boundary"]["observed_at"]
    assert observed is not None, "an applied boundary must record its instant"
    assert before <= observed <= after, (
        "the instant must fall inside this call, not be chosen by a caller")

    # Every bounded contact carries exactly that instant — never earlier.
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT known_reached_sql_by FROM "
                    "sql_coverage_boundary_contact")
        bounds = [r[0] for r in cur.fetchall()]
    assert bounds == [observed]
    assert all(b >= before for b in bounds), (
        "a contact observed in the snapshot must never receive a bound earlier "
        "than the observation that found it")


@_needs_pg
def test_46_pg_the_boundary_records_its_snapshot_provenance(seeded160):
    """§2 — the run the population was read against is recorded, not implied."""
    result = _establish()
    assert result["ok"]

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT source_dataset, source_run_id, run_id, "
                    "population_definition, lifecycle_rule_version "
                    "FROM sql_coverage_boundary")
        dataset, source_run, run_id, definition, rule = cur.fetchone()

    assert dataset == boundary_svc.SOURCE_DATASET
    assert run_id and run_id.startswith("sqlbound_")
    assert definition == boundary_svc.POPULATION_DEFINITION
    assert rule == lifecycle.LIFECYCLE_RULE_VERSION
    # The sync state the population was read against, carried verbatim.
    assert source_run and "contact_funnel_sync" in source_run


# ═════════════════════════════════════════════════════════════════════════════
# §3 — exactly one completed boundary, and it is immutable
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_47_pg_a_second_completed_boundary_is_refused_by_the_database(seeded160):
    """A service check cannot provide this. Two concurrent establishers would
    both read "no boundary exists" and both insert; only a constraint stops the
    second. Proven by attempting the insert directly, bypassing the service."""
    import psycopg2

    _establish()
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO sql_coverage_boundary (boundary_id, observed_at, "
                " lifecycle_rule_version, source_dataset, population_definition,"
                " run_id, status) "
                "VALUES ('rival', now(), 'v', 'd', 'p', 'r', 'complete')")
        c.rollback()

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sql_coverage_boundary "
                    "WHERE status = 'complete'")
        assert cur.fetchone()[0] == 1


@_needs_pg
def test_48_pg_a_completed_boundary_can_never_be_updated_or_deleted(seeded160):
    """Immutability is a property of the TABLES, not a discipline writers keep."""
    import psycopg2

    _establish()
    for statement in (
        "UPDATE sql_coverage_boundary SET observed_at = now()",
        "UPDATE sql_coverage_boundary SET contacts_bounded = 99",
        "DELETE FROM sql_coverage_boundary",
        "UPDATE sql_coverage_boundary_contact SET known_reached_sql_by = now()",
        "DELETE FROM sql_coverage_boundary_contact",
    ):
        with seeded160.connection.get_conn() as c, c.cursor() as cur:
            with pytest.raises(psycopg2.Error):
                cur.execute(statement)
            c.rollback()

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT contacts_bounded FROM sql_coverage_boundary")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT COUNT(*) FROM sql_coverage_boundary_contact")
        assert cur.fetchone()[0] == 2


@_needs_pg
def test_49_pg_a_mutated_replay_is_refused_and_changes_nothing(seeded160):
    """Requirement §3. Same id, different content → fail, database untouched."""
    from db import writers

    first = _establish()
    bid = first["boundary"]["boundary_id"]

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT boundary_id, observed_at, contacts_bounded, "
                    "population_definition FROM sql_coverage_boundary")
        before = cur.fetchall()

    mutated = writers.establish_sql_coverage_boundary(
        {**first["boundary"], "boundary_id": bid,
         "population_definition": "something else entirely"},
        clock_sql=_FIXED_CLOCK)

    assert mutated["ok"] is False
    assert "not identical" in mutated["error"]
    assert "population_definition differs" in mutated["error"]

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT boundary_id, observed_at, contacts_bounded, "
                    "population_definition FROM sql_coverage_boundary")
        assert cur.fetchall() == before, (
            "a refused replay must leave the stored boundary untouched")


@_needs_pg
def test_50_pg_a_replay_with_a_changed_population_is_refused(seeded160):
    """The population is part of what a boundary asserts, so it is verified too."""
    from db import writers

    first = _establish()
    bid = first["boundary"]["boundary_id"]

    # A new undated SQL contact appears, so a replay would bound 3, not 2.
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "undated_c", "lifecycle_stage": "salesqualifiedlead",
        "created_at": LEGACY_CREATED, "last_modified_at": LEGACY_CREATED}])

    replay = writers.establish_sql_coverage_boundary(
        {**first["boundary"], "boundary_id": bid}, clock_sql=_FIXED_CLOCK)

    assert replay["ok"] is False
    assert "population differs" in replay["error"]
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sql_coverage_boundary_contact")
        assert cur.fetchone()[0] == 2, "the stored snapshot is unchanged"


@_needs_pg
def test_51_pg_a_different_boundary_id_is_refused_by_the_service_and_writer(
        seeded160):
    """§3 — the singleton is enforced at both layers, with a readable reason."""
    from db import writers

    _establish()
    rival = writers.establish_sql_coverage_boundary(
        {"boundary_id": "rival_boundary",
         "lifecycle_rule_version": lifecycle.LIFECYCLE_RULE_VERSION,
         "source_dataset": boundary_svc.SOURCE_DATASET,
         "population_definition": boundary_svc.POPULATION_DEFINITION,
         "run_id": "r2"}, clock_sql=_FIXED_CLOCK)

    assert rival["ok"] is False
    assert "a different completed boundary already exists" in rival["error"]
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sql_coverage_boundary")
        assert cur.fetchone()[0] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §4 — fail closed on every required write and verification read
# ═════════════════════════════════════════════════════════════════════════════

def _boundary_stubs(monkeypatch, *, rows):
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": {
                            "boundary_id": "b1", "observed_at": BOUNDARY}})
    monkeypatch.setattr(repo, "fetch_post_boundary_sql_contacts",
                        lambda *, boundary_id=None, since=None: {
                            "available": True, "rows": rows})
    monkeypatch.setattr(repo, "fetch_post_boundary_incidents",
                        lambda *, status=None: {"available": True, "rows": [],
                                                "open_count": 0})


def test_52_a_failed_direct_property_resolution_fails_the_run(monkeypatch):
    """§4. An unchecked resolution would report a clean run that never happened."""
    from db import writers

    _boundary_stubs(monkeypatch, rows=[{
        "contact_id": "has_date", "created_at": BOUNDARY,
        "lifecycle_stage": "salesqualifiedlead",
        "direct_date_entered_sql": BOUNDARY,
        "effective_date_entered_sql": BOUNDARY}])
    monkeypatch.setattr(writers, "resolve_post_boundary_incidents",
                        lambda ids, *, resolved_by: {
                            "ok": False, "error": "deadlock detected",
                            "persisted": 0})

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["ok"] is False
    assert result["run_outcome"] == boundary_svc.BOUNDARY_WRITE_FAILED
    assert "deadlock detected" in result["detail"]


def test_53_a_failed_history_resolution_fails_the_run(monkeypatch):
    """The same, on the other permitted source."""
    from db import writers

    transition = BOUNDARY + timedelta(hours=2)
    _boundary_stubs(monkeypatch, rows=[{
        "contact_id": "needs_history", "created_at": BOUNDARY,
        "lifecycle_stage": "salesqualifiedlead",
        "direct_date_entered_sql": None,
        "effective_date_entered_sql": None}])
    monkeypatch.setattr(
        hubspot, "fetch_lifecycle_stage_history",
        lambda ids, client=None: {cid: {
            "state": hubspot.HISTORY_PRESENT,
            "versions": [{"value": "salesqualifiedlead", "timestamp": transition,
                          "source_type": "CRM_UI", "source_id": "u1",
                          "source_label": None, "updated_by_user_id": None}]}
            for cid in ids})
    monkeypatch.setattr(writers, "upsert_lifecycle_stage_history",
                        lambda rows, *, run_id: {"ok": True,
                                                 "persisted": len(rows)})
    monkeypatch.setattr(writers, "resolve_post_boundary_incidents",
                        lambda ids, *, resolved_by: {
                            "ok": False, "error": "connection lost",
                            "persisted": 0})

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["ok"] is False
    assert result["run_outcome"] == boundary_svc.BOUNDARY_WRITE_FAILED
    assert "connection lost" in result["detail"]
    # Truthful partial-write accounting: the evidence DID land.
    assert result["history_events_persisted"] == 1
    assert result["partial_local_write"] is True


def test_54_resolution_counts_come_from_the_database_not_the_request(monkeypatch):
    """§4. Report what was PERSISTED, never how many ids we asked about.

    ``len(requested)`` would report two resolutions where the database made one
    — and an incident the report calls closed would still be open.
    """
    from db import writers

    _boundary_stubs(monkeypatch, rows=[
        {"contact_id": f"c{i}", "created_at": BOUNDARY,
         "lifecycle_stage": "salesqualifiedlead",
         "direct_date_entered_sql": BOUNDARY,
         "effective_date_entered_sql": BOUNDARY} for i in range(3)])
    # Three ids asked about; the database resolved ONE (the others were already
    # resolved, so the UPDATE's WHERE clause skipped them).
    monkeypatch.setattr(writers, "resolve_post_boundary_incidents",
                        lambda ids, *, resolved_by: {"ok": True, "persisted": 1,
                                                     "error": None})

    result = boundary_svc.detect_post_boundary_gaps(apply=True)

    assert result["ok"] is True
    assert result["incidents_resolved"] == 1, (
        "the count must be what the database persisted, not len(requested)")


def test_55_the_scheduler_records_an_error_when_the_guarantee_is_unverified(
        monkeypatch):
    """§4. A scheduler run must never look healthy on an unverified guarantee."""
    import scheduler.incremental_sync as sync

    monkeypatch.setattr(
        "services.sql_coverage_boundary_service.detect_post_boundary_gaps",
        lambda **k: {"ok": False, "run_outcome": "incident_store_unreadable",
                     "detail": "the post-boundary incident store could not be read",
                     "new_undated_sql_gaps": None,
                     "unresolved_post_boundary_incidents": None})

    errors: list = []
    result = sync._detect_sql_coverage_gaps(run_id="r1", errors=errors)

    assert result["ok"] is False
    assert errors, "an unverified prospective guarantee must be a run error"
    assert "sql_coverage_gaps" in errors[0]


def test_56_the_scheduler_errors_when_open_incidents_cannot_be_counted(
        monkeypatch):
    """The other direction: a completed run that could not count its blockers."""
    import scheduler.incremental_sync as sync

    monkeypatch.setattr(
        "services.sql_coverage_boundary_service.detect_post_boundary_gaps",
        lambda **k: {"ok": True, "boundary_established": True,
                     "new_undated_sql_gaps": 0,
                     "unresolved_post_boundary_incidents": None})

    errors: list = []
    result = sync._detect_sql_coverage_gaps(run_id="r1", errors=errors)

    assert errors and "UNVERIFIED" in errors[0]
    assert result["status"] == "failed", "an error beside `success` is a lie"


def test_57_the_scheduler_errors_on_remaining_open_incidents(monkeypatch):
    """An open prospective gap is the condition this PR exists to surface."""
    import scheduler.incremental_sync as sync

    monkeypatch.setattr(
        "services.sql_coverage_boundary_service.detect_post_boundary_gaps",
        lambda **k: {"ok": True, "boundary_established": True,
                     "new_undated_sql_gaps": 0,
                     "unresolved_post_boundary_incidents": 3})

    errors: list = []
    result = sync._detect_sql_coverage_gaps(run_id="r1", errors=errors)

    assert errors and "3 unresolved" in errors[0]
    assert result["status"] == "failed"


# ═════════════════════════════════════════════════════════════════════════════
# §5 — freshness is a certification PREREQUISITE
#
# A window can be complete — every undated contact ruled out, no prospective gap
# able to belong to it — and still be worthless if its source stopped updating.
# It would be complete with respect to data that has stopped arriving. "Nothing
# is missing from what we have" is not "nothing is missing".
# ═════════════════════════════════════════════════════════════════════════════

import analysis.sql_coverage_freshness as freshness_mod  # noqa: E402

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _sync(**over):
    """A sync state proving a recent, SUCCESSFUL INCREMENTAL run.

    PR-ADS-160 §3: `last_incremental_at` alone proves nothing — the
    contact-funnel sync stamps it on bootstrap runs too. The freshness contract
    reads `last_successful_incremental_at` (advanced only by an incremental that
    succeeded) and `last_incremental_status` (that incremental's own outcome,
    which no later bootstrap overwrites).
    """
    row = {"bootstrap_status": "complete",
           # Stamped by BOTH modes. Present here only to prove the contract does
           # not use it.
           "last_incremental_at": NOW - timedelta(hours=2),
           "last_sync_mode": "incremental",
           "last_status": "success",
           "last_incremental_status": "success",
           "last_successful_incremental_at": NOW - timedelta(hours=2),
           "last_error": None}
    row.update(over)
    return {"available": True, "row": row}


@pytest.mark.parametrize("state,expected_reason,expected_fresh", [
    (_sync(), freshness_mod.FRESH, True),
    (_sync(last_successful_incremental_at=NOW - timedelta(hours=200)),
     freshness_mod.STALE, False),
    (_sync(last_incremental_status="failed",
           last_successful_incremental_at=None),
     freshness_mod.SYNC_FAILED, False),
    (_sync(bootstrap_status="partial"),
     freshness_mod.BOOTSTRAP_INCOMPLETE, False),
    # A completed BOOTSTRAP and nothing else. The row is post-migration (it
    # carries a mode), so the absence of an incremental is a fact about the
    # PIPELINE, not about the record.
    (_sync(last_sync_mode="bootstrap", last_incremental_status=None,
           last_successful_incremental_at=None),
     freshness_mod.NEVER_RUN, False),
    # A LEGACY row, written before the provenance columns existed. It cannot
    # prove a successful incremental either way, so it fails closed — reported
    # apart from NEVER_RUN because the remedy differs: one needs a sync to run,
    # the other needs a sync to record.
    (_sync(last_sync_mode=None, last_incremental_status=None,
           last_successful_incremental_at=None),
     freshness_mod.PROVENANCE_MISSING, False),
    ({"available": True, "row": None}, freshness_mod.STATE_MISSING, False),
    # Unreadable is None, NOT False: False is a claim about the pipeline,
    # None is a statement about us. Both block; only one is the pipeline's fault.
    ({"available": False, "row": None}, freshness_mod.STATE_UNAVAILABLE, None),
])
def test_58_the_freshness_contract_distinguishes_every_state(
        state, expected_reason, expected_fresh):
    verdict = freshness_mod.assess(state, now=NOW)

    assert verdict["reason"] == expected_reason
    assert verdict["fresh"] is expected_fresh
    assert verdict["reason"] in freshness_mod.FRESHNESS_REASONS
    assert verdict["detail"], "every verdict must say WHY"
    # Everything that is not proven fresh blocks certification.
    assert freshness_mod.blocks_certification(verdict) is (expected_fresh is not True)


@pytest.mark.parametrize("state", [
    _sync(last_successful_incremental_at=NOW - timedelta(hours=200)),
    _sync(last_incremental_status="failed"),
    _sync(bootstrap_status="partial"),
    _sync(last_sync_mode=None, last_incremental_status=None,
          last_successful_incremental_at=None),
    {"available": False, "row": None},
])
def test_59_no_window_certifies_on_a_source_that_is_not_fresh(state):
    """Requirement §5. The four blocking states, each proven to block.

    The window below is otherwise perfect: it opens after the boundary, every
    historical contact is ruled out, and no prospective gap can belong to it.
    Only freshness stops it — which is the whole point.
    """
    verdict = freshness_mod.assess(state, now=NOW)
    block = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=5, recovered_sqls=0,
        unresolved_rows=[{"contact_id": "c1", "created_at": LEGACY_CREATED,
                          "known_reached_sql_by": BOUNDARY}],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=0,
        freshness=verdict)

    # Complete, and still not certifiable.
    assert block["window_total_complete"] is True
    assert block["certification_eligible"] is False
    assert block["certification_status"] == coverage.CERT_STALE_SOURCE
    assert verdict["reason"] in block["certification_explanation"]


def test_60_a_fresh_source_lets_an_otherwise_clean_window_certify():
    """The positive control: freshness is the ONLY thing that was blocking."""
    block = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=5, recovered_sqls=0,
        unresolved_rows=[{"contact_id": "c1", "created_at": LEGACY_CREATED,
                          "known_reached_sql_by": BOUNDARY}],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=0,
        freshness=freshness_mod.assess(_sync(), now=NOW))

    assert block["certification_status"] == coverage.CERT_ELIGIBLE
    assert block["source_fresh"] is True


def test_61_freshness_uses_the_proven_successful_incremental_not_a_proxy():
    """Which timestamp, and why the two obvious ones are both wrong.

    ``latest_modified_at`` is the newest contact modification the sync happened
    to see; it goes stale on its own whenever HubSpot is quiet, and a quiet CRM
    is not a broken pipeline.

    ``last_incremental_at`` is stamped by BOTH modes, so a successful bootstrap
    is indistinguishable from a live incremental feed — a window could certify
    against a source whose incremental pipeline had died.
    """
    quiet_crm = _sync(last_successful_incremental_at=NOW - timedelta(hours=1))
    quiet_crm["row"]["latest_modified_at"] = NOW - timedelta(days=30)
    assert freshness_mod.assess(quiet_crm, now=NOW)["fresh"] is True

    dead_pipeline = _sync(
        last_successful_incremental_at=NOW - timedelta(days=30))
    dead_pipeline["row"]["latest_modified_at"] = NOW - timedelta(minutes=1)
    assert freshness_mod.assess(dead_pipeline, now=NOW)["fresh"] is False

    # A BOOTSTRAP that ran a minute ago does not make the source fresh.
    bootstrap_only = _sync(
        last_sync_mode="bootstrap", last_incremental_status=None,
        last_successful_incremental_at=None,
        last_incremental_at=NOW - timedelta(minutes=1))
    verdict = freshness_mod.assess(bootstrap_only, now=NOW)
    assert verdict["fresh"] is False
    assert verdict["reason"] == freshness_mod.NEVER_RUN

    # And a bootstrap AFTER a failed incremental does not clear the failure.
    masked = _sync(last_sync_mode="bootstrap", last_status="success",
                   last_incremental_status="failed")
    assert freshness_mod.assess(masked, now=NOW)["reason"] == \
        freshness_mod.SYNC_FAILED


@_needs_pg
def test_62_pg_the_audit_and_the_gate_share_one_freshness_contract(
        seeded160, monkeypatch):
    """Both surfaces expose it, and neither restates the rule.

    A second copy that agreed would prove nothing, and one that disagreed would
    report the gate's bug as the pipeline's.
    """
    from scripts import audit_lifecycle_sql_coverage as audit
    from scripts import audit_sql_coverage_gate as gate_mod

    _establish()

    f = audit.Findings()
    audit_verdict = audit.audit_source_freshness(f)
    g = gate_mod.Gate()
    gate_verdict = gate_mod.check_source_freshness(g)

    assert audit_verdict["reason"] == gate_verdict["reason"]
    assert audit_verdict["fresh"] is gate_verdict["fresh"] is True
    assert audit_verdict["reason"] in freshness_mod.FRESHNESS_REASONS


@_needs_pg
def test_63_pg_a_stale_source_breaks_the_gate_and_blocks_every_window(
        seeded160, monkeypatch):
    """End to end: stale ingestion, red gate, zero certified windows."""
    from db import writers
    from scripts import audit_lifecycle_sql_coverage as audit
    from scripts import audit_sql_coverage_gate as gate_mod

    _establish()
    # Genuinely stale, not merely unprovable: the last incremental SUCCEEDED,
    # it simply succeeded two weeks ago. That distinction is the point — an
    # unreadable provenance and a dead scheduler have different remedies.
    stale_at = datetime.now(tz=timezone.utc) - timedelta(days=14)
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete",
        last_incremental_at=stale_at, last_status="success",
        last_sync_mode="incremental", last_incremental_status="success",
        last_successful_incremental_at=stale_at, last_error=None)

    g, gate_report = gate_mod.run()
    assert any("source_freshness" in v for v in g.violations)
    assert g.exit_code == gate_mod.EXIT_VIOLATION
    assert gate_report["source_freshness"]["reason"] == freshness_mod.STALE

    findings, report = audit.run()
    assert report["source_freshness"]["fresh"] is False
    assert report["certification"]["windows_certified"] == 0
    assert report["certification"]["source_fresh"] is False
    # A stale pipeline is a DATA finding for the coverage audit, not a contract
    # violation and not an audit outage — the audit still ran perfectly.
    assert findings.violations == []


# ═════════════════════════════════════════════════════════════════════════════
# §6 — incidents are resolved PER WINDOW, not applied globally
# ═════════════════════════════════════════════════════════════════════════════

def _incident(created, detected):
    return {"contact_id": "g1", "contact_created_at": created,
            "detected_at": detected}


def test_64_an_incident_blocks_only_the_windows_it_could_belong_to():
    """Requirement §6. One global count blocking everything is the defect.

    The incident's contact was created in December. A window that CLOSED in
    October cannot contain a transition by a contact that did not exist — and
    must certify normally.
    """
    december = _incident(datetime(2026, 12, 1, tzinfo=timezone.utc),
                         datetime(2026, 12, 5, tzinfo=timezone.utc))

    october = coverage.incident_membership(
        [december], date(2026, 10, 1), date(2026, 10, 31))
    assert october["open_post_boundary_gaps"] == 0
    assert october["post_boundary_gaps_ruled_out"] == 1

    late_december = coverage.incident_membership(
        [december], date(2026, 12, 1), date(2026, 12, 31))
    assert late_december["open_post_boundary_gaps"] == 1
    assert late_december["post_boundary_gaps_ruled_out"] == 0


@pytest.mark.parametrize("start,end,blocks", [
    # Window entirely BEFORE the contact existed → ruled out by the creation
    # lower bound.
    (date(2026, 1, 1), date(2026, 1, 31), 0),
    # Window OVERLAPPING the detection → could belong. The transition happened
    # somewhere at or before 15 September and after 1 August; part of that
    # interval lies inside this window.
    (date(2026, 9, 1), date(2026, 9, 30), 1),
    # Window opening AFTER detection → ruled out by the detection UPPER bound.
    # "Already at SQL by 15 September" means the transition was over before
    # October opened. This is the same one-directional rule §7 applies to the
    # boundary bound, and it is why detection is worth recording at all.
    (date(2026, 10, 1), date(2026, 10, 31), 0),
])
def test_65_incident_membership_before_overlapping_and_after(start, end, blocks):
    incident = _incident(datetime(2026, 8, 1, tzinfo=timezone.utc),
                         datetime(2026, 9, 15, tzinfo=timezone.utc))
    split = coverage.incident_membership([incident], start, end)
    assert split["open_post_boundary_gaps"] == blocks


def test_66_an_incident_detected_before_a_window_opened_is_ruled_out():
    """The detection UPPER bound, used the only sound way it can be.

    Detection means "we first saw it already at SQL, with no date". If that was
    strictly before a window opened, the transition was over before the window
    began — the same strict comparison §7 applies to the boundary bound.
    """
    early = _incident(datetime(2026, 1, 1, tzinfo=timezone.utc),
                      datetime(2026, 2, 1, tzinfo=timezone.utc))
    split = coverage.incident_membership(
        [early], date(2026, 6, 1), date(2026, 6, 30))
    assert split["open_post_boundary_gaps"] == 0
    assert split["post_boundary_gaps_ruled_out"] == 1


def test_67_an_incident_with_no_usable_bounds_blocks_every_window():
    """Nothing rules it out, so it rules nothing out. Unknown blocks."""
    blind = {"contact_id": "g1", "contact_created_at": None,
             "detected_at": None}
    for start, end in [(date(2026, 1, 1), date(2026, 1, 31)),
                       (date(2026, 10, 1), date(2026, 10, 31)),
                       (None, None)]:
        split = coverage.incident_membership([blind], start, end)
        assert split["open_post_boundary_gaps"] == 1


def test_68_an_unreadable_incident_store_blocks_certification(monkeypatch):
    """§4 + §6. A null count is not zero gaps — unknown blockers must block."""
    block = coverage.window_coverage(
        window="oct", window_end=date(2026, 10, 31), window_start=date(2026, 10, 1),
        confirmed_sqls=5, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=BOUNDARY, open_post_boundary_incidents=None,
        freshness=FRESH)

    assert block["certification_eligible"] is False
    assert block["certification_status"] == coverage.CERT_UNAVAILABLE
    assert block["open_post_boundary_gaps"] is None
    assert "unknown blockers must block" in block["certification_explanation"]


def test_69_incident_membership_never_produces_a_date():
    """It returns counts. It has no way to return a timestamp, and never does."""
    incident = _incident(datetime(2026, 8, 1, tzinfo=timezone.utc),
                         datetime(2026, 9, 15, tzinfo=timezone.utc))
    split = coverage.incident_membership(
        [incident], date(2026, 9, 1), date(2026, 9, 30))

    assert set(split) == {"open_post_boundary_gaps",
                          "post_boundary_gaps_ruled_out",
                          "post_boundary_gaps_global"}
    assert all(isinstance(v, int) for v in split.values())


# ═════════════════════════════════════════════════════════════════════════════
# §4 — no shadowed definitions
#
# Six functions existed TWICE in the funnel repository, byte-identical, the
# second silently overriding the first. That is benign only while the copies
# agree: the moment one is edited, every caller gets whichever Python bound
# last, and the edit appears to have no effect for reasons nothing explains.
# This already bit once in this PR — an updated `fetch_post_boundary_sql_contacts`
# was shadowed by its stale twin and raised `unexpected keyword argument`.
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("module_path", [
    "db/crm_funnel_repository.py",
    "db/writers.py",
    "services/sql_coverage_boundary_service.py",
    "analysis/lifecycle_sql_coverage.py",
    "analysis/sql_coverage_freshness.py",
    "scripts/audit_lifecycle_sql_coverage.py",
    "scripts/audit_sql_coverage_gate.py",
    "scripts/establish_sql_coverage_boundary.py",
])
def test_70_no_module_defines_the_same_top_level_function_twice(module_path):
    """A later definition silently replaces an earlier one. Never acceptable."""
    import ast as _ast
    import collections

    source = (_ROOT / module_path).read_text(encoding="utf-8")
    tree = _ast.parse(source)
    counts = collections.Counter(
        node.name for node in tree.body
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)))
    duplicates = {name: n for name, n in counts.items() if n > 1}

    assert not duplicates, (
        f"{module_path} defines {sorted(duplicates)} more than once; the later "
        f"definition silently overrides the earlier, so an edit to the wrong "
        f"copy has no effect and nothing explains why")


def test_71_the_duplicate_guard_actually_fires():
    """The guard's own negative control.

    A check that cannot fail is not a check. This proves the AST walk detects a
    shadowed definition rather than merely finding none in files that have none.
    """
    import ast as _ast
    import collections

    shadowed = "def f():\n    return 1\n\n\ndef f():\n    return 2\n"
    tree = _ast.parse(shadowed)
    counts = collections.Counter(
        node.name for node in tree.body
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)))
    assert {n: c for n, c in counts.items() if c > 1} == {"f": 2}


# ═════════════════════════════════════════════════════════════════════════════
# §1 (second review) — the scheduler dataset must carry a TRUTHFUL status
#
# `detect_post_boundary_gaps()` speaks in `ok` plus counts. `_overall_status()`
# speaks in status strings, and deliberately treats an ABSENT status as a
# failure — an unrecognised outcome is not evidence of success. Returning the
# service payload unchanged therefore marked EVERY real sync `partial`,
# including a perfectly clean check, and including the pre-boundary state where
# this dataset has nothing to say at all.
#
# These tests run the REAL wrapper — `_detect_sql_coverage_gaps` calling the
# real service against a real database — and feed its real return value into
# the real `_overall_status`. Mocking the wrapper to return an invented status
# would test the invention, not the mapping.
# ═════════════════════════════════════════════════════════════════════════════

def _sched_run(seeded, monkeypatch):
    """Run the real scheduler wrapper and score it with the real `_overall_status`."""
    import scheduler.incremental_sync as sync

    errors: list = []
    block = sync._detect_sql_coverage_gaps(run_id="sched_test", errors=errors)
    overall = sync._overall_status({"hubspot/sql_coverage_gaps": block})
    return block, errors, overall


@_needs_pg
def test_72_pg_no_boundary_makes_the_dataset_skipped_and_non_voting(
        seeded160, monkeypatch):
    """Case 1. Nothing to police yet is a STATE, not a success and not a failure.

    Before the boundary exists there is no prospective period, so this dataset
    cannot vote: calling it `success` would claim a guarantee that does not
    exist, and calling it `failed` would make every pre-boundary run red for
    doing exactly the right thing.
    """
    import scheduler.incremental_sync as sync

    # Deliberately NOT establishing a boundary.
    block, errors, overall = _sched_run(seeded160, monkeypatch)

    assert block["status"] == "skipped"
    assert block["skip_reason"] == "no_sql_coverage_boundary_established"
    assert block["detail"], "a skip must say why, or it is indistinguishable from a bug"
    assert block["status"] in sync.NON_VOTING_STATUSES
    assert errors == [], "having nothing to police is not a run error"
    # Non-voting: it neither greens nor reds the run.
    assert overall == "success"


@_needs_pg
def test_73_pg_a_clean_established_boundary_reports_success(
        seeded160, monkeypatch):
    """Case 2. The one green case — and the regression this blocker was about.

    Before the fix this returned no status at all, so a flawless prospective
    check made the whole sync `partial` forever.
    """
    _establish()

    block, errors, overall = _sched_run(seeded160, monkeypatch)

    assert block["ok"] is True
    assert block["status"] == "success"
    assert errors == [], "a `success` beside a populated errors list is a lie"
    assert overall == "success"


@_needs_pg
def test_74_pg_an_open_incident_makes_the_dataset_failed(
        seeded160, monkeypatch):
    """Case 3. A post-boundary contact with no exact timestamp is the condition
    this PR exists to surface, so it reds the dataset AND the run."""
    from db import writers

    _establish()
    # HubSpot history holds no SQL transition either: neither permitted source
    # can supply a date, so this is a genuine prospective gap.
    _stub_history(monkeypatch, {"late_gap": {"state": hubspot.HISTORY_PRESENT,
                                             "versions": []}})
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "late_gap", "lifecycle_stage": "salesqualifiedlead",
        "created_at": BOUNDARY + timedelta(days=2),
        "last_modified_at": BOUNDARY + timedelta(days=2)}])

    block, errors, overall = _sched_run(seeded160, monkeypatch)

    assert block["status"] == "failed"
    assert errors, "an open prospective gap must be a run error"
    assert any("sql_coverage_gaps" in e for e in errors)
    assert overall != "success"
    assert overall == "failed"


@_needs_pg
def test_75_pg_an_unreadable_incident_store_makes_the_dataset_failed(
        seeded160, monkeypatch):
    """Case 4. A run that completed but could not count its own blockers has
    not verified anything. Unverified is not healthy."""
    _establish()
    monkeypatch.setattr(repo, "fetch_post_boundary_incidents",
                        lambda **k: {"available": False, "rows": []})

    block, errors, overall = _sched_run(seeded160, monkeypatch)

    assert block["status"] == "failed"
    assert errors and any("UNVERIFIED" in e or "could not" in e for e in errors)
    assert overall != "success"
    # And the count is unknown, never zero — a pass that could not read the
    # store proves nothing about how many blockers remain.
    assert block["unresolved_post_boundary_incidents"] is None


# ═════════════════════════════════════════════════════════════════════════════
# §2 (second review) — an incomplete SQL total and its CPQL must never publish
#
# Completeness has TWO halves and they fail for different reasons:
#
#   historical   every undated legacy contact ruled out of this window
#   prospective  no open post-boundary gap could belong to this window
#
# The first cut computed only the historical half and called the result
# `complete`, so a window with an open prospective gap published a total that
# was provably missing rows, and a CPQL computed from it. The halves are now
# reported apart, `complete_sql_total` is null unless BOTH hold, and
# `cpql_publishable` additionally requires certification.
#
# The confirmed dated subset stays visible throughout — under its own
# explicitly qualified name, never as "the total".
# ═════════════════════════════════════════════════════════════════════════════

def _window(**over):
    """A window whose HISTORICAL half is complete, varied one factor at a time."""
    kwargs = {
        "window": "oct", "window_end": date(2026, 10, 31),
        "window_start": date(2026, 10, 1),
        "confirmed_sqls": 42, "recovered_sqls": 0,
        # Bounded below the window start: ruled out, so the historical half is
        # complete in every case here. Only the PROSPECTIVE half varies.
        "unresolved_rows": [{"contact_id": "c1", "created_at": LEGACY_CREATED,
                             "known_reached_sql_by": BOUNDARY}],
        "boundary_observed_at": BOUNDARY,
        "open_post_boundary_incidents": 0,
        "freshness": FRESH,
    }
    kwargs.update(over)
    return coverage.window_coverage(**kwargs)


def _certify(*windows):
    """Run the same windows through the LAST gate, with every global input good.

    Only the per-window verdict varies, so whatever the gate withholds, it
    withholds for a window-local reason and nothing else.
    """
    from scripts import audit_lifecycle_sql_coverage as audit

    blocks = [{**w, "window_type": "monthly"} for w in windows]
    report = audit.audit_certification(
        audit.Findings(), blocks,
        {"available": True, "post_boundary_incidents_available": True,
         "boundary": {"boundary_id": "b1", "observed_at": BOUNDARY}},
        {"reconciliation_complete": True},
        freshness=FRESH)
    return blocks, report


def test_76_a_window_publishes_a_complete_total_only_when_both_halves_hold():
    """The five cases, each failing for a different reason, on the same window.

    1. historical resolved, one open prospective gap that could belong
    2. historical resolved, the incident store unreadable
    3. both halves complete, but the source is stale
    4. both halves complete and fresh, but the window straddles the boundary
    5. everything clean — the only case that may publish
    """
    # ── 1. an open prospective gap that could belong to this window ──────────
    case1 = _window(open_post_boundary_incidents=1)
    # ── 2. the incident store could not be read: unknown, not zero ───────────
    case2 = _window(open_post_boundary_incidents=None)
    # ── 3. complete, but certifying against a source that stopped updating ───
    case3 = _window(freshness={"fresh": False,
                               "reason": freshness_mod.STALE})
    # ── 4. complete and fresh, but the window straddles the boundary, so the
    #      guaranteed period does not cover all of it ─────────────────────────
    case4 = _window(window_start=date(2026, 9, 1), window_end=date(2026, 9, 30))
    # ── 5. nothing wrong anywhere ───────────────────────────────────────────
    case5 = _window()

    # ── the membership layer: the two halves are computed and reported APART ─
    #
    # Cases 1 and 2 fail the PROSPECTIVE half while the historical half holds.
    # Before this fix only the historical half was computed and its verdict was
    # called `complete`, so both published a total provably missing rows.
    for block in (case1, case2):
        assert block["historical_membership_complete"] is True
        assert block["prospective_membership_complete"] is False
        assert block["window_total_complete"] is False
        assert block["complete_sql_total"] is None
    assert case1["reason"] == coverage.COVERAGE_POST_BOUNDARY_GAPS
    assert case2["reason"] == coverage.COVERAGE_INCIDENTS_UNREADABLE

    # Case 3 is genuinely COMPLETE — every row IS accounted for — and still may
    # not publish. Completeness and certification are different questions: a
    # complete window over a source that stopped updating describes data that
    # stopped arriving. Collapsing the two would make "complete" mean
    # "trustworthy", which is the substitution this whole PR exists to refuse.
    assert case3["window_total_complete"] is True
    assert case3["certification_eligible"] is False
    assert case3["certification_status"] == coverage.CERT_STALE_SOURCE

    # Case 4 straddles the boundary, and so fails BOTH questions at once: an
    # upper bound of 14 Sep cannot rule a contact out of a window that opens on
    # 1 Sep, so the historical half is unresolved, and only part of the window
    # lies in the guaranteed period.
    assert case4["historical_membership_complete"] is False
    assert case4["window_total_complete"] is False
    assert case4["certification_status"] == coverage.CERT_OVERLAPS_BOUNDARY

    # ── the certification layer: the LAST gate, which takes the total back ───
    blocks, report = _certify(case1, case2, case3, case4, case5)
    failing, publishable = blocks[:4], blocks[4]

    for i, block in enumerate(failing, start=1):
        assert block["certified"] is False, f"case {i} certified"
        assert block["cpql_publishable"] is False, f"case {i} published CPQL"
        assert block["complete_sql_total"] is None, (
            f"case {i} left a complete total on the report; whoever reads the "
            f"number rather than the flag gets an incomplete total")
        # The confirmed dated subset stays visible — under a name that says
        # what it is, and never AS the total.
        assert block["confirmed_sql_subset"] == 42
        assert block["complete_sql_total"] != block["confirmed_sql_subset"]

    # ── the only publishable case ───────────────────────────────────────────
    assert publishable["historical_membership_complete"] is True
    assert publishable["prospective_membership_complete"] is True
    assert publishable["window_total_complete"] is True
    assert publishable["certified"] is True
    assert publishable["cpql_publishable"] is True
    assert publishable["complete_sql_total"] == 42
    assert report["windows_certified"] == 1
    assert len(report["blocked_windows"]) == 4


def test_77_a_global_prerequisite_takes_back_a_locally_publishable_total():
    """The gate must WITHHOLD, not merely annotate.

    A window can be locally flawless and still uncertifiable for a reason that
    lives outside it. A report that says `certified: false` beside
    `complete_sql_total: 42` is read as a total by anyone who reads the number
    rather than the flag, so certification takes both back.
    """
    from scripts import audit_lifecycle_sql_coverage as audit

    clean = _window()
    assert clean["cpql_publishable"] is True, "control: it starts publishable"

    blocks = [{**clean, "window_type": "monthly"}]
    report = audit.audit_certification(
        audit.Findings(), blocks,
        {"available": True, "post_boundary_incidents_available": True,
         "boundary": {"boundary_id": "b1", "observed_at": BOUNDARY}},
        # The 44 canonical reader combinations did NOT reconcile — nothing to
        # do with this window, and fatal to any claim made about it.
        {"reconciliation_complete": False}, freshness=FRESH)

    assert report["windows_certified"] == 0
    assert report["blocked_windows"][0]["reason"] == \
        "canonical_readers_did_not_reconcile"
    assert blocks[0]["certified"] is False
    assert blocks[0]["cpql_publishable"] is False
    assert blocks[0]["complete_sql_total"] is None
    assert blocks[0]["confirmed_sql_subset"] == 42


# ═════════════════════════════════════════════════════════════════════════════
# §5 (second review) — provenance is bound to THE snapshot, or there is none
#
# The service used to read the ingestion provenance before calling the writer,
# so a sync landing between the two made `source_run_id` describe a state older
# than the population actually snapshotted — a boundary documented as traceable
# to a run it does not correspond to. It is now read inside the SAME
# transaction, immediately after the population, and its absence REFUSES
# establishment rather than recording a best-effort NULL.
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_78_pg_a_boundary_without_snapshot_provenance_is_refused(
        seeded160, monkeypatch):
    """No provable provenance, no boundary. Not a NULL, not best-effort."""
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM hubspot_contact_funnel_sync_state")
        c.commit()

    result = _establish()

    assert result["ok"] is False
    assert "provenance" in result["detail"] or "traced" in result["detail"]
    # And nothing was recorded: a refusal that left a row behind would be the
    # best-effort boundary this blocker forbids.
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM sql_coverage_boundary")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM sql_coverage_boundary_contact")
        assert cur.fetchone()[0] == 0


@_needs_pg
def test_79_pg_the_provenance_describes_the_state_the_snapshot_was_read_from(
        seeded160):
    """The recorded provenance must match the sync state AT snapshot time.

    Read outside the transaction, a sync landing in between would make this
    describe a different run than the one that produced the population.
    """
    from db import writers

    batch_id = writers.start_sync_batch(
        "hubspot", "hubspot/contact_funnel", "incremental")
    assert batch_id, "the fixture needs a real batch to trace back to"
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete", last_batch_id=batch_id,
        last_status="success", last_sync_mode="incremental",
        last_incremental_status="success",
        last_successful_incremental_at=datetime.now(tz=timezone.utc),
        last_error=None)

    result = _establish()
    assert result["ok"] is True

    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT source_run_id FROM sql_coverage_boundary")
        source_run_id = cur.fetchone()[0]

    assert f"batch={batch_id}" in source_run_id
    assert "mode=incremental" in source_run_id
    # The service's own report agrees with what the database recorded, so a
    # reader of either surface reaches the same run.
    assert result["boundary"]["source_run_id"] == source_run_id


def test_80_the_prospective_question_is_vacuous_before_a_boundary_exists():
    """The one place an unknown gap count does NOT block, and why.

    A post-boundary gap is defined relative to a boundary. Where none is
    established there is no prospective period and nothing can belong to it —
    the question is vacuous, not unknown. Treating it as unknown would make
    every window incomplete the moment PR-ADS-160 shipped, including windows
    PR-ADS-159 correctly published, which is a consumer change this
    foundational PR must not make.

    The moment a boundary exists the same missing count IS a real unknown, and
    blocks. That is the whole difference between "not asked" and "asked, and we
    could not look".
    """
    common = {"window": "2024", "window_end": date(2024, 12, 31),
              "window_start": date(2024, 1, 1), "confirmed_sqls": 11,
              "recovered_sqls": 0, "unresolved_rows": [],
              "open_post_boundary_incidents": None}

    # No boundary: vacuous. PR-ADS-159's contract, unchanged.
    before = coverage.window_coverage(**common, boundary_observed_at=None)
    assert before["prospective_membership_complete"] is True
    assert before["window_total_complete"] is True
    assert before["complete_sql_total"] == 11
    assert before["reason"] == coverage.COVERAGE_COMPLETE
    # Complete is still not CERTIFIED — the stronger claim needs a boundary.
    assert before["certification_status"] == coverage.CERT_NO_BOUNDARY
    assert before["certification_eligible"] is False

    # A boundary exists and the store could not be read: a real unknown.
    after = coverage.window_coverage(**common, boundary_observed_at=BOUNDARY,
                                     freshness=FRESH)
    assert after["prospective_membership_complete"] is False
    assert after["window_total_complete"] is False
    assert after["complete_sql_total"] is None
    assert after["cpql_publishable"] is False
    assert after["reason"] == coverage.COVERAGE_INCIDENTS_UNREADABLE
    assert "could not be read" in after["explanation"]


# ═════════════════════════════════════════════════════════════════════════════
# §1 (third review) — ONE PostgreSQL snapshot for the population AND the
# provenance, proven with two concurrent connections
#
# Putting both reads in one transaction was necessary and not sufficient. Under
# the connection's default READ COMMITTED isolation, PostgreSQL takes a FRESH
# snapshot at the start of EVERY statement, so a contact-funnel sync committing
# between the population SELECT and the provenance SELECT is invisible to the
# first and visible to the second. The boundary then records a population
# describing one instant beside a `source_run_id` describing a later one — the
# mixed snapshot §5 of the second review set out to make impossible, one layer
# further down than that fix reached.
#
# test_79 cannot see this: with nothing committing concurrently, both isolation
# levels agree. Only a second connection committing INSIDE the transaction's
# read window can tell them apart.
# ═════════════════════════════════════════════════════════════════════════════

class _GatedCursor:
    """A real cursor that fires a callback after each ``execute``.

    The callback is what lets the test stop the establishing transaction at an
    exact statement boundary — mid-transaction, with its snapshot already taken
    — rather than racing it with a sleep.
    """

    def __init__(self, cur, after_execute):
        self._cur = cur
        self._after = after_execute
        self._n = 0

    def execute(self, sql, params=None):
        result = (self._cur.execute(sql) if params is None
                  else self._cur.execute(sql, params))
        self._n += 1
        self._after(self._n, sql)
        return result

    def __getattr__(self, name):        # executemany, fetchone, description…
        return getattr(self._cur, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)


class _GatedConn:
    def __init__(self, conn, after_execute):
        self._conn = conn
        self._after = after_execute

    def cursor(self, *a, **k):
        return _GatedCursor(self._conn.cursor(*a, **k), self._after)

    def __getattr__(self, name):        # commit, rollback, closed…
        return getattr(self._conn, name)


def _batch(label_date):
    """A real sync_batches row, so `last_batch_id`'s foreign key is satisfied."""
    from db import writers

    return writers.start_sync_batch("hubspot", "contact_funnel", "daily",
                                    date_to=label_date)


def _run_establish_gated(pg_cluster, monkeypatch, *, gate_after, on_pause):
    """Establish a boundary on a dedicated connection, paused at one statement.

    ``gate_after`` is the 1-based index of the ``execute`` to pause AFTER, and
    ``on_pause`` runs on the MAIN thread while the establishing transaction sits
    open with its snapshot already taken.

    Returns ``(result, gated_sql)`` — the statement that was gated, so the test
    fails loudly if a refactor moves it rather than silently gating the wrong
    read.
    """
    import contextlib
    import threading

    import psycopg2
    from db import writers

    paused, resume = threading.Event(), threading.Event()
    gated_sql: list = []

    def after_execute(n, sql):
        if n == gate_after:
            gated_sql.append(sql)
            paused.set()
            assert resume.wait(timeout=30), "the concurrent writer never released the gate"

    establishing = psycopg2.connect(pg_cluster.url)

    @contextlib.contextmanager
    def fake_get_conn():
        yield _GatedConn(establishing, after_execute)

    monkeypatch.setattr(writers, "get_conn", fake_get_conn)

    box: dict = {}

    def establish():
        try:
            box["result"] = _establish()
        except BaseException as exc:            # noqa: BLE001
            box["error"] = exc
            paused.set()

    worker = threading.Thread(target=establish, daemon=True)
    worker.start()
    try:
        assert paused.wait(timeout=30), "establishment never reached the gate"
        if "error" in box:
            raise box["error"]
        on_pause()
    finally:
        resume.set()
        worker.join(timeout=60)
        establishing.close()

    if "error" in box:
        raise box["error"]
    return box["result"], (gated_sql[0] if gated_sql else "")


def _recorded(connection):
    """What the committed boundary actually says: its population and provenance."""
    with connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT source_run_id FROM sql_coverage_boundary")
        row = cur.fetchone()
        cur.execute("SELECT contact_id FROM sql_coverage_boundary_contact "
                    "ORDER BY contact_id")
        contacts = [r[0] for r in cur.fetchall()]
    return (row[0] if row else None), contacts


@_needs_pg
@pytest.mark.parametrize("isolation,mixes", [
    ("REPEATABLE READ", False),   # the shipped default
    ("READ COMMITTED", True),     # the negative control — proves what it buys
])
def test_81_pg_population_and_provenance_come_from_one_snapshot(
        seeded160, monkeypatch, isolation, mixes):
    """Two connections. A sync commits between the two reads. Do they agree?

    The gate fires AFTER the population read and BEFORE the provenance read, so
    the concurrent commit lands exactly in the window that isolation closes:

      REPEATABLE READ  population OLD + provenance OLD  → one snapshot
      READ COMMITTED   population OLD + provenance NEW  → MIXED

    The second parametrisation is a negative control. A guard whose absence
    changes nothing is not a guard, and without it this test would pass on the
    unfixed code.
    """
    import psycopg2
    from db import writers

    monkeypatch.setattr(writers, "_BOUNDARY_ISOLATION", isolation)

    before_batch = _batch(date(2026, 9, 1))
    after_batch = _batch(date(2026, 9, 13))
    assert before_batch and after_batch and before_batch != after_batch
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete", last_batch_id=before_batch,
        last_status="success", last_sync_mode="incremental",
        last_incremental_status="success",
        last_successful_incremental_at=datetime.now(tz=timezone.utc),
        last_error=None)

    other = psycopg2.connect(seeded160.url)

    def concurrent_sync():
        """A contact-funnel sync landing mid-transaction, on its OWN connection."""
        with other, other.cursor() as cur:
            cur.execute(
                "INSERT INTO hubspot_contact_funnel "
                "(contact_id, lifecycle_stage, created_at, last_modified_at) "
                "VALUES ('promoted_mid_transaction', 'salesqualifiedlead', %s, %s)",
                (LEGACY_CREATED, LEGACY_CREATED))
            cur.execute(
                "UPDATE hubspot_contact_funnel_sync_state SET last_batch_id = %s "
                " WHERE scope = 'contacts'", (after_batch,))
        # `with other` commits; the establishing transaction is still open.

    try:
        # 1 = SET TRANSACTION, 2 = the singleton check, 3 = the population.
        result, gated = _run_establish_gated(
            seeded160, monkeypatch, gate_after=3, on_pause=concurrent_sync)
    finally:
        other.close()

    assert "hubspot_contact_funnel" in gated and "lifecycle_stage" in gated, (
        "the gate no longer lands on the population read — the statement order "
        f"changed, so this test is no longer proving anything. Gated: {gated!r}")
    assert result["ok"] is True, result.get("detail")

    source_run_id, contacts = _recorded(seeded160.connection)

    # The population half is OLD under both isolation levels: that statement ran
    # before the concurrent commit. It is the PROVENANCE half that moves.
    assert "promoted_mid_transaction" not in contacts, (
        "the population must describe the snapshot, not a later commit")

    if mixes:
        # Without the isolation guard the two halves describe different
        # instants, and nothing in the recorded boundary says so.
        assert f"batch={after_batch}" in source_run_id, (
            "control failed: READ COMMITTED should have seen the newer sync "
            "state, which is the whole defect being guarded against")
        assert f"batch={before_batch}" not in source_run_id
    else:
        assert f"batch={before_batch}" in source_run_id, (
            f"provenance describes a different snapshot than the population: "
            f"{source_run_id!r}")
        assert f"batch={after_batch}" not in source_run_id


@_needs_pg
def test_82_pg_the_snapshot_is_pinned_before_the_first_read(
        seeded160, monkeypatch):
    """The other side of the same guarantee.

    Gating BEFORE the population read — a commit landing while only the
    singleton check has run — proves the snapshot is fixed at the transaction's
    first read rather than at whichever statement happens to be last. Under
    REPEATABLE READ both later reads still describe the pre-commit state; a
    per-statement snapshot would have let BOTH move.
    """
    import psycopg2
    from db import writers

    before_batch = _batch(date(2026, 9, 1))
    after_batch = _batch(date(2026, 9, 13))
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete", last_batch_id=before_batch,
        last_status="success", last_sync_mode="incremental",
        last_incremental_status="success",
        last_successful_incremental_at=datetime.now(tz=timezone.utc),
        last_error=None)

    other = psycopg2.connect(seeded160.url)

    def concurrent_sync():
        with other, other.cursor() as cur:
            cur.execute(
                "INSERT INTO hubspot_contact_funnel "
                "(contact_id, lifecycle_stage, created_at, last_modified_at) "
                "VALUES ('arrived_after_snapshot', 'salesqualifiedlead', %s, %s)",
                (LEGACY_CREATED, LEGACY_CREATED))
            cur.execute(
                "UPDATE hubspot_contact_funnel_sync_state SET last_batch_id = %s "
                " WHERE scope = 'contacts'", (after_batch,))

    try:
        # Gate after statement 2 — the singleton check, which is what takes the
        # transaction's snapshot. The population has not been read yet.
        result, gated = _run_establish_gated(
            seeded160, monkeypatch, gate_after=2, on_pause=concurrent_sync)
    finally:
        other.close()

    assert "sql_coverage_boundary" in gated, gated
    assert result["ok"] is True, result.get("detail")

    source_run_id, contacts = _recorded(seeded160.connection)

    assert "arrived_after_snapshot" not in contacts, (
        "a contact committed after the transaction's snapshot was bounded by "
        "it — the boundary would claim to have observed a population it never "
        "saw, and that contact would be wrongly ruled out of every later window")
    assert f"batch={before_batch}" in source_run_id
    assert f"batch={after_batch}" not in source_run_id
    # Both halves moved together — or rather, neither moved. That is the claim.
    assert sorted(contacts) == ["undated_a", "undated_b"]


# ═════════════════════════════════════════════════════════════════════════════
# §2 (third review) — a truncated contact-funnel run is never a success
#
# `run_status` was computed correctly — "partial" whenever the scan did not
# reach the end of the result set — and then thrown away. Both sync batches were
# finished with status="success" and the returned dict carried a hardcoded
# "status": "success", so:
#
#   * the batch history said the interval was covered when the scan stopped short;
#   * `last_source_date` advanced, moving a proven-coverage watermark past data
#     that was never read;
#   * the scheduler saw a green dataset and the daily run reported clean.
#
# The run's own verdict is now what gets written down and returned.
# ═════════════════════════════════════════════════════════════════════════════

def _pages(n, *, complete):
    """A page iterator. Omitting the sentinel is what `truncated` means.

    The service proves completion from an explicit empty `{"complete": True}`
    page, never from running out of pages — a capped run stops iterating too.
    """
    def iterator(since, max_pages=None):
        for i in range(n):
            yield ([{"id": f"trunc_{i}",
                     "properties": {"hs_object_id": f"trunc_{i}",
                                    "email": f"t{i}@example.com",
                                    "lifecyclestage": "lead",
                                    "createdate": "2026-09-01T00:00:00Z",
                                    "lastmodifieddate": "2026-09-02T00:00:00Z"}}],
                   {"complete": False})
        if complete:
            yield ([], {"complete": True})
    return iterator


def _sync_state(connection):
    with connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT last_status, last_sync_mode, last_incremental_status, "
                    "       last_successful_incremental_at "
                    "  FROM hubspot_contact_funnel_sync_state WHERE scope = 'contacts'")
        row = cur.fetchone()
    return dict(zip(("last_status", "last_sync_mode", "last_incremental_status",
                     "last_successful_incremental_at"), row))


def _batch_rows(connection):
    with connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT dataset, status FROM sync_batches "
                    " WHERE finished_at IS NOT NULL ORDER BY id")
        return cur.fetchall()


@_needs_pg
def test_83_pg_a_truncated_incremental_reports_partial_everywhere(seeded160):
    """The real service, a real database, and every durable surface checked.

    A truncated run must not look successful in ANY of the four places it is
    recorded: the returned status, the sync batches, the incremental provenance,
    or the proven-successful-incremental timestamp.
    """
    from services import hubspot_contact_funnel_sync_service as svc

    # The fixture records an earlier PROVEN incremental. The question is whether
    # a truncated run moves it — an absent timestamp would prove nothing, since
    # it would be absent either way.
    proven_before = _sync_state(seeded160.connection)["last_successful_incremental_at"]
    assert proven_before is not None, "control: something to advance must exist"

    result = svc.run_contact_funnel_sync(
        mode=svc.MODE_INCREMENTAL, page_iterator=_pages(2, complete=False))

    assert result["truncated"] is True
    assert result["scan_complete"] is False
    assert result["status"] == "partial", "a truncated run reported success"

    # Both durable batches agree with the run's own verdict.
    finished = _batch_rows(seeded160.connection)
    assert finished, "the run recorded no finished batch at all"
    assert {status for _, status in finished} == {"partial"}, finished

    state = _sync_state(seeded160.connection)
    assert state["last_status"] == "partial"
    assert state["last_sync_mode"] == svc.MODE_INCREMENTAL
    assert state["last_incremental_status"] == "partial"
    assert state["last_successful_incremental_at"] == proven_before, (
        "a truncated run advanced the proven-successful-incremental timestamp; "
        "certification would then treat a short scan as a healthy feed")

    # And the coverage watermark did NOT advance: a partial pull did not cover
    # its interval, so claiming it did is the same lie as a failed run claiming it.
    with seeded160.connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT status, last_source_date FROM sync_state "
                    " WHERE dataset = %s", (svc.DATASET_CONTACT_FUNNEL,))
        row = cur.fetchone()
    assert row and row[0] == "partial"
    assert row[1] is None, "a truncated run advanced the coverage watermark"


@_needs_pg
def test_84_pg_a_truncated_run_makes_the_scheduler_run_non_green(
        seeded160, monkeypatch):
    """End to end through the REAL scheduler wrapper and REAL `_overall_status`.

    Nothing is mocked to return an invented status: the service runs for real
    against a real database, the scheduler's own wrapper classifies it, and the
    scheduler's own aggregator scores it.
    """
    import scheduler.incremental_sync as sync
    from services import hubspot_contact_funnel_sync_service as svc

    # The ONLY substitution is HubSpot's pages — the seam the service exposes
    # for exactly this. Everything downstream of it is the real orchestration:
    # real batches, real checkpoints, real status derivation.
    real_sync = svc.run_contact_funnel_sync
    monkeypatch.setattr(svc, "get_bootstrap_mode", lambda: svc.MODE_INCREMENTAL)
    monkeypatch.setattr(
        svc, "run_contact_funnel_sync",
        lambda **kw: real_sync(**kw, page_iterator=_pages(2, complete=False)))

    errors: list = []
    block = sync._sync_contact_funnel(run_id=None, errors=errors)
    overall = sync._overall_status({"hubspot/contact_funnel": block})

    assert block["status"] == "partial"
    assert block["status"] not in sync.NON_VOTING_STATUSES, (
        "a truncated run must VOTE — a non-voting status would let it pass")
    assert overall != "success"
    assert errors and "contact_funnel" in errors[0], (
        "a dataset that votes the run down must say why")


@_needs_pg
def test_85_pg_a_complete_incremental_still_reports_success(seeded160):
    """The control. The fix must not turn healthy runs partial.

    Same service, same database, same page count — the ONLY difference is the
    completion sentinel that proves the scan reached the end.
    """
    from services import hubspot_contact_funnel_sync_service as svc

    result = svc.run_contact_funnel_sync(
        mode=svc.MODE_INCREMENTAL, page_iterator=_pages(2, complete=True))

    assert result["truncated"] is False
    assert result["status"] == "success"
    assert {status for _, status in _batch_rows(seeded160.connection)} == {"success"}

    state = _sync_state(seeded160.connection)
    assert state["last_status"] == "success"
    assert state["last_incremental_status"] == "success"
    assert state["last_successful_incremental_at"] is not None

    import scheduler.incremental_sync as sync
    assert sync._overall_status({"hubspot/contact_funnel": result}) == "success"


# ═════════════════════════════════════════════════════════════════════════════
# §3 (third review) — the SUMMARY must follow certification, not membership
#
# `run()` computed `coverage_complete` — a pure MEMBERSHIP verdict — and then
# published it twice more, as `complete_sql_total_publishable` and
# `cpql_publishable`. Membership knows nothing about whether the source is still
# being fed, whether the boundary and its incidents could be read, or whether
# the 44 canonical reads agree. So the summary could announce a publishable CPQL
# while the `certification` block directly beneath it reported zero certified
# windows and had already stripped the total from every one of them.
#
# Whoever read the summary rather than the per-window detail got the opposite of
# the truth. The flags are now DERIVED from what survived the final gate.
# ═════════════════════════════════════════════════════════════════════════════

def _one_certifiable_window(monkeypatch, start, end):
    """Restrict the audit's window inventory to ONE window after the boundary.

    The production inventory contains `all_time`, which is permanently
    incomplete and permanently uncertifiable — correctly, since it contains the
    unknowable historical period. With it present no report can ever publish
    anything, so a positive control would be indistinguishable from a flag that
    is simply hard-wired false. Narrowing the inventory is what makes the clean
    case genuinely reachable, and therefore what makes the blocked cases mean
    something.
    """
    import scripts.audit_sql_doctrine_inventory as inventory

    monkeypatch.setattr(inventory, "resolve_all_windows", lambda canon, now: [
        {"window_key": "post_boundary", "window_type": "business",
         "start": start, "end": end},
    ])


@pytest.fixture()
def certifiable(seeded160, monkeypatch):
    """A database whose one window is complete, fresh, reconciled and certified.

    Every undated legacy contact is bounded BELOW the window start, so the
    historical half is resolved; no incident exists, so the prospective half is
    too; and the sync state proves a recent successful incremental.
    """
    _establish()
    _one_certifiable_window(monkeypatch, date(2026, 9, 20), date(2026, 9, 30))
    return seeded160


def _report(monkeypatch=None):
    from scripts import audit_lifecycle_sql_coverage as audit

    return audit.run(now=datetime(2026, 10, 1, tzinfo=timezone.utc))


def _assert_withheld(findings, report, *, why):
    """Every surface a consumer might read must agree that nothing publishes."""
    cert = report["certification"]
    assert cert["windows_certified"] == 0, why
    assert report["cpql_publishable"] is False, f"{why}: CPQL published anyway"
    assert report["complete_sql_total_publishable"] is False, (
        f"{why}: a complete total was presented as publishable")

    for block in report["windows"]:
        assert block["cpql_publishable"] is False, (
            f"{why}: window {block['window']} still publishes CPQL")
        assert block["complete_sql_total"] is None, (
            f"{why}: window {block['window']} kept a complete total")
        # The confirmed dated subset is never taken away — it is a true number
        # about contacts that carry a proven date, and withholding it would
        # replace an overstatement with a different kind of lie.
        assert block["confirmed_sql_subset"] is not None
        assert block["confirmed_sqls"] == block["confirmed_sql_subset"]

    # The summary never contradicts the gate directly beneath it.
    assert "publication_gate" in [c["check"] for c in findings.checks]
    assert not [v for v in findings.violations if "publication_gate" in v]


@_needs_pg
def test_86_pg_a_stale_source_withholds_publication_from_a_complete_report(
        certifiable, monkeypatch):
    """Case 1. Membership complete, pipeline dead.

    "Nothing is missing from what we have" is not "nothing is missing". A
    complete window over a source that stopped updating describes data that
    stopped arriving.
    """
    from db import writers

    stale = datetime.now(tz=timezone.utc) - timedelta(days=14)
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete", last_incremental_at=stale,
        last_status="success", last_sync_mode="incremental",
        last_incremental_status="success",
        last_successful_incremental_at=stale, last_error=None)

    findings, report = _report()

    assert report["source_freshness"]["fresh"] is False
    assert report["coverage_complete"] is True, (
        "control: MEMBERSHIP is complete — that is exactly why the old code "
        "published, and why this case is the one that mattered")
    assert report["publication_withheld_by_certification"] is True
    _assert_withheld(findings, report, why="stale source")


@_needs_pg
def test_87_pg_a_reader_disagreement_withholds_publication(
        certifiable, monkeypatch):
    """Case 2. Membership complete, the canonical reads do not agree.

    Three reads of the same population that disagree mean at least one published
    number is wrong, and nothing in a per-window membership verdict can see it.
    """
    from scripts import audit_lifecycle_sql_coverage as audit

    real = audit.audit_read_reconciliation
    monkeypatch.setattr(audit, "audit_read_reconciliation",
                        lambda f, now: {**real(f, now),
                                        "reconciliation_complete": False})

    findings, report = _report()

    assert report["coverage_complete"] is True, "control: membership is complete"
    assert report["read_reconciliation"]["reconciliation_complete"] is False
    assert [b["reason"] for b in report["certification"]["blocked_windows"]] == \
        ["canonical_readers_did_not_reconcile"]
    _assert_withheld(findings, report, why="readers did not reconcile")


@_needs_pg
def test_88_pg_an_unreadable_certification_input_withholds_publication(
        certifiable, monkeypatch):
    """Case 3. Membership complete, and we could not check the blockers.

    Unknown is not clear. An incident store that cannot be read is not evidence
    of zero incidents, and publishing on it would turn an outage into a number.
    """
    monkeypatch.setattr(repo, "fetch_post_boundary_incidents",
                        lambda **k: {"available": False, "rows": [],
                                     "open_count": None})

    findings, report = _report()

    assert report["boundary"]["post_boundary_incidents_available"] is False
    _assert_withheld(findings, report, why="incident store unreadable")
    # An outage is an audit UNAVAILABILITY, not a contract violation: the audit
    # could not look, which is a different finding from the audit finding a lie.
    assert any("post_boundary_incidents" in u for u in findings.unavailable)
    assert findings.violations == []


@_needs_pg
def test_89_pg_a_fully_clean_report_does_publish(certifiable):
    """Case 4, the positive control.

    Without this, every assertion above is satisfied by a flag hard-wired false.
    Same code path, same window, nothing blocking — and the summary publishes.
    """
    findings, report = _report()

    assert report["source_freshness"]["fresh"] is True
    assert report["read_reconciliation"]["reconciliation_complete"] is True
    assert report["coverage_complete"] is True
    assert report["certification"]["windows_certified"] == \
        report["certification"]["windows_assessed"] > 0
    assert report["cpql_publishable"] is True
    assert report["complete_sql_total_publishable"] is True
    assert report["publication_withheld_by_certification"] is False

    for block in report["windows"]:
        assert block["cpql_publishable"] is True
        assert block["complete_sql_total"] is not None
    assert findings.violations == []


@_needs_pg
def test_90_pg_the_summary_can_never_publish_with_zero_certified_windows(
        certifiable, monkeypatch):
    """The invariant itself, checked by the audit rather than only by this test.

    A guard that lives only in a test protects only the cases the test thought
    of. `run()` raises a `publication_gate` violation if the summary ever claims
    publishable while nothing is certified, so a future edit that reintroduces
    the defect fails the audit's own contract.
    """
    from scripts import audit_lifecycle_sql_coverage as audit

    # Force the exact contradiction: certification blocks everything, while the
    # membership-only flags would have said publish.
    monkeypatch.setattr(audit, "audit_certification",
                        lambda f, w, b, r, fr=None: {
                            "certified_windows": [], "blocked_windows": [],
                            "windows_certified": 0, "windows_assessed": 0,
                            "readers_reconciled": True, "boundary_readable": True,
                            "incidents_readable": True, "source_fresh": True,
                            "source_freshness_reason": "source_fresh"})

    findings, report = _report()

    assert report["coverage_complete"] is True
    assert report["cpql_publishable"] is False
    assert report["complete_sql_total_publishable"] is False
    assert findings.violations == [], (
        "withholding correctly is not a violation — the violation fires only "
        "if the summary PUBLISHES with nothing certified")


# ═════════════════════════════════════════════════════════════════════════════
# §1 (fourth review) — the DURABLE run record must carry the true final status
#
# `_overall_status` was already correct, the returned summary was already
# correct, and the CLI exit code was already correct. The lie lived in exactly
# one line — and it was the line production reads:
#
#     "status": "success" if overall_status in ("success", "partial") else "failed"
#
# `/api/runs`, the "Latest recorded run" banner, per-page run metadata and Data
# Runs all consume the `runs` table. A truncated contact-funnel sync could
# therefore leave every one of those surfaces reporting a clean run over a
# contact population that was never finished.
# ═════════════════════════════════════════════════════════════════════════════

def _all_other_datasets_succeed(monkeypatch, sync):
    """Let every voting dataset EXCEPT the contact funnel report success.

    Discovered by introspection rather than listed by hand: a dataset added
    later would otherwise fail this test for a reason that has nothing to do
    with what it is testing, and the fix would be to edit a list nobody reads.
    """
    for name in dir(sync):
        if name == "_sync_contact_funnel":
            continue                      # the one under test — runs for real
        if name.startswith(("_sync_", "_publish_", "_detect_")):
            if callable(getattr(sync, name)):
                monkeypatch.setattr(sync, name,
                                    lambda **kw: {"status": "success"})


def _runs_row(connection, run_id):
    with connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT status, error_message, finished_at FROM runs "
                    " WHERE id = %s", (run_id,))
        row = cur.fetchone()
    return dict(zip(("status", "error_message", "finished_at"), row)) if row else None


@pytest.fixture()
def truncated_funnel(seeded160, monkeypatch):
    """A real, genuinely truncated contact-funnel run inside a real scheduler run."""
    import scheduler.incremental_sync as sync
    from services import hubspot_contact_funnel_sync_service as svc

    real_sync = svc.run_contact_funnel_sync
    monkeypatch.setattr(svc, "get_bootstrap_mode", lambda: svc.MODE_INCREMENTAL)
    monkeypatch.setattr(
        svc, "run_contact_funnel_sync",
        lambda **kw: real_sync(**kw, page_iterator=_pages(2, complete=False)))
    _all_other_datasets_succeed(monkeypatch, sync)
    return seeded160


@_needs_pg
def test_91_pg_a_partial_run_is_recorded_as_partial_not_success(truncated_funnel):
    """End to end: real service, real scheduler, real `runs` row.

    Nothing here is mocked to return an invented status. The contact-funnel
    dataset runs the real service over a scan that does not reach the end of its
    result set; every other dataset succeeds; and the durable record is read
    back out of PostgreSQL.
    """
    import scheduler.incremental_sync as sync

    result = sync.run_daily_incremental_sync(run_reason="test_fourth_review")

    # The dataset itself, unchanged from the third review.
    funnel = result["datasets"]["hubspot/contact_funnel"]
    assert funnel["status"] == "partial"
    assert funnel["truncated"] is True

    # The summary — both names for the same verdict.
    assert result["status"] == "partial"
    assert result["execution_status"] == "partial"

    # The DURABLE record. This is the assertion the blocker is about.
    row = _runs_row(truncated_funnel.connection, result["run_id"])
    assert row is not None, "the run was never recorded"
    assert row["status"] == "partial", (
        "a partial run was persisted as success; every monitoring surface reads "
        "this column, so production would show a clean run over an incomplete "
        "contact population")
    assert row["finished_at"] is not None

    # And it says WHY, durably — a bare `partial` in a table is a verdict
    # nobody can act on.
    assert row["error_message"], "a partial run recorded no explanation"
    assert "contact_funnel" in row["error_message"]
    assert "did not reach the end of the result set" in row["error_message"]


@_needs_pg
def test_92_pg_the_cli_exit_code_stays_non_zero_for_a_partial_run(
        truncated_funnel, capsys):
    """`echo $?` must not say everything worked.

    Already true before this fix, and asserted here so the durable-status change
    cannot be "fixed" later by relaxing the exit code to match it.
    """
    import scheduler.incremental_sync as sync

    exit_code = sync.main()
    capsys.readouterr()                    # the JSON summary, not under test

    assert exit_code != 0
    assert exit_code == 1


@_needs_pg
def test_93_pg_a_clean_run_is_still_recorded_as_success(seeded160, monkeypatch):
    """The positive control.

    Same scheduler, same datasets, the ONLY difference being the completion
    sentinel that proves the contact scan reached the end. Without this, the
    test above is satisfied by a scheduler that records every run as partial.
    """
    import scheduler.incremental_sync as sync
    from services import hubspot_contact_funnel_sync_service as svc

    real_sync = svc.run_contact_funnel_sync
    monkeypatch.setattr(svc, "get_bootstrap_mode", lambda: svc.MODE_INCREMENTAL)
    monkeypatch.setattr(
        svc, "run_contact_funnel_sync",
        lambda **kw: real_sync(**kw, page_iterator=_pages(2, complete=True)))
    _all_other_datasets_succeed(monkeypatch, sync)

    result = sync.run_daily_incremental_sync(run_reason="test_fourth_review_ok")

    assert result["datasets"]["hubspot/contact_funnel"]["status"] == "success"
    assert result["status"] == "success"
    assert result["execution_status"] == "success"

    row = _runs_row(seeded160.connection, result["run_id"])
    assert row["status"] == "success"
    assert row["error_message"] is None
    assert sync.main() == 0


@_needs_pg
def test_94_pg_a_failed_run_is_still_recorded_as_failed(seeded160, monkeypatch):
    """The other control. Three outcomes must stay three."""
    import scheduler.incremental_sync as sync

    _all_other_datasets_succeed(monkeypatch, sync)
    monkeypatch.setattr(sync, "_sync_contact_funnel",
                        lambda **kw: {"status": "failed", "error": "boom"})

    result = sync.run_daily_incremental_sync(run_reason="test_fourth_review_fail")

    assert result["status"] in ("partial", "failed")
    row = _runs_row(seeded160.connection, result["run_id"])
    # Every other dataset succeeded, so the run is `partial` overall — and the
    # durable record must say exactly that rather than rounding it to either end.
    assert row["status"] == result["status"]


@_needs_pg
def test_95_pg_the_durable_status_never_disagrees_with_the_summary(
        seeded160, monkeypatch):
    """The invariant, over all three outcomes, through the real scheduler.

    Persisting a status that contradicts the returned one is the whole defect,
    so it is checked as a property rather than only in the truncated case.
    """
    import scheduler.incremental_sync as sync

    def every_dataset(block):
        """Every voting dataset reports the SAME outcome, so the RUN does too."""
        for name in dir(sync):
            if name.startswith(("_sync_", "_publish_", "_detect_")) \
                    and callable(getattr(sync, name)):
                monkeypatch.setattr(sync, name, (lambda b: lambda **kw: b)(block))

    seen = {}
    # `_overall_status` is a vote: one failing dataset among successes is a
    # PARTIAL run, not a failed one. So a run-level `failed` needs every voting
    # dataset to fail — which is what makes these three run outcomes, rather
    # than three dataset outcomes wearing the run's name.
    for label, setup in (
            ("success", lambda: every_dataset({"status": "success"})),
            ("partial", lambda: (_all_other_datasets_succeed(monkeypatch, sync),
                                 monkeypatch.setattr(
                                     sync, "_sync_contact_funnel",
                                     lambda **kw: {"status": "partial",
                                                   "pages": 2,
                                                   "scan_complete": False}))),
            ("failed", lambda: every_dataset({"status": "failed",
                                              "error": "boom"}))):
        setup()
        result = sync.run_daily_incremental_sync(run_reason=f"prop_{label}")
        row = _runs_row(seeded160.connection, result["run_id"])

        assert row["status"] == result["status"], (
            f"{label}: durable {row['status']!r} != returned {result['status']!r}")
        assert row["status"] in ("success", "partial", "failed")
        seen[label] = row["status"]

    assert seen == {"success": "success", "partial": "partial",
                    "failed": "failed"}, seen
    assert len(set(seen.values())) == 3, (
        "two of the three outcomes are indistinguishable in the runs table")


def _viewer_client():
    """A TestClient plus a viewer cookie — `/api/runs` requires auth."""
    import os

    try:
        from fastapi.testclient import TestClient
    except ImportError:                              # pragma: no cover
        pytest.skip("fastapi[testclient] not available")
    from starlette.responses import Response as StarletteResponse

    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    try:
        from api.auth import set_session
        from api.server import app
    except Exception as exc:                         # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")

    r = StarletteResponse()
    set_session(r, "testviewer", "viewer")
    cookies = {}
    for part in r.headers.get("set-cookie", "").split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            cookies["ads_session"] = part.split("=", 1)[1]
    return TestClient(app, raise_server_exceptions=False), cookies


@_needs_pg
def test_96_pg_the_api_serves_partial_rather_than_a_rounded_status(
        truncated_funnel):
    """`/api/runs` is what the dashboard reads. It must see `partial` too.

    A durable `partial` that the API rounds on the way out would move the defect
    one layer rather than fix it, so the endpoint is exercised rather than
    inspected.
    """
    import scheduler.incremental_sync as sync

    result = sync.run_daily_incremental_sync(run_reason="test_api_partial")
    assert result["status"] == "partial"

    client, cookies = _viewer_client()
    response = client.get("/api/runs?days=30", cookies=cookies)
    assert response.status_code == 200, response.text

    runs = response.json()["runs"]
    assert runs, "the API returned no runs at all"
    latest = runs[0]

    assert latest["status"] == "partial", (
        f"the API rounded the durable status to {latest['status']!r}; the "
        f"dashboard reads this field")
    assert latest["status"] not in ("success", "failed")
    assert latest["finished_at"] is not None


@_needs_pg
def _runs_rows_verbatim(connection):
    """Every finished run row, with its run_type EXACTLY as persisted.

    PR-ADS-160-F1 §2. The earlier version of this helper read `run_type` from
    PostgreSQL and then substituted the literal `"daily"` before handing the
    rows to monitoring. That rewrote the one field the production path gets
    wrong, so the test passed while real `daily_incremental_sync` rows were
    being discarded by `compute_monitoring_status` — a test adapting production
    data to its assertion instead of the other way round.

    Nothing is reshaped here beyond serialising the timestamps.
    """
    with connection.get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT run_type, status, started_at, finished_at FROM runs "
                    " ORDER BY started_at DESC")
        return [{"run_type": r[0], "status": r[1],
                 "started_at": r[2].isoformat(), "finished_at": r[3].isoformat()}
                for r in cur.fetchall() if r[3] is not None]


@_needs_pg
def test_97_pg_a_partial_run_does_not_make_monitoring_green(truncated_funnel):
    """The monitoring severity over a REAL partial run, end to end.

    Two defects meet here. `api/monitoring.py` counted partial as a successful
    run for both of its measurements, so a pipeline producing nothing but
    partial runs reported itself healthy. And it grouped by the cadence NAMES,
    so the real run type — `daily_incremental_sync` — never reached that logic
    at all.

    The row flows in with the run type the scheduler actually persisted.
    """
    import scheduler.incremental_sync as sync
    from api.monitoring import compute_monitoring_status

    result = sync.run_daily_incremental_sync(run_reason="test_monitoring_partial")
    assert result["status"] == "partial"

    rows = _runs_rows_verbatim(truncated_funnel.connection)

    assert rows and rows[0]["status"] == "partial"
    assert rows[0]["run_type"] == sync.RUN_TYPE == "daily_incremental_sync", (
        "this test is only meaningful over the run type production writes")

    verdict = compute_monitoring_status(
        rows, {"daily": 2, "weekly": 8, "monthly": 35}, 2)
    daily = verdict["latest_runs"]["daily"]

    assert daily["last_status"] == "partial", (
        "the real run type never reached the daily monitoring bucket")
    assert daily["latest_partial"] is True
    assert verdict["severity"] != "green", (
        "a partial run reset the system to healthy")
    assert verdict["severity"] == "yellow", "and it is not an outage either"
    assert any("partially" in w or "incomplete" in w for w in verdict["warnings"]), \
        verdict["warnings"]
    # ...and not the "nothing ran" complaint the grouping defect produced.
    assert not any("No daily run found" in w for w in verdict["warnings"]), \
        verdict["warnings"]
    # The proven-complete coverage claim was NOT advanced by this run.
    assert daily["last_success_at"] is None
    assert daily["last_completed_at"] is not None


@_needs_pg
def test_97b_pg_the_pre_fix_grouping_would_have_discarded_this_run(
        truncated_funnel):
    """The negative control for the grouping defect.

    `test_97` above can only prove the mapping works; it cannot show that the
    mapping is what makes it work. This replays the SAME persisted rows through
    the PRE-FIX grouping — match the cadence names literally — and asserts the
    run disappears.

    A guard whose absence changes nothing is not a guard.
    """
    import scheduler.incremental_sync as sync
    from api.monitoring import compute_monitoring_status

    result = sync.run_daily_incremental_sync(run_reason="test_monitoring_control")
    assert result["status"] == "partial"

    rows = _runs_rows_verbatim(truncated_funnel.connection)
    assert rows[0]["run_type"] == "daily_incremental_sync"

    # The pre-fix behaviour, reproduced exactly: only rows whose run_type IS a
    # cadence name were grouped.
    pre_fix_rows = [r for r in rows if r["run_type"] in ("daily", "weekly", "monthly")]
    assert pre_fix_rows == [], (
        "the real run type would have survived the old grouping, so this "
        "control proves nothing")

    before = compute_monitoring_status(
        pre_fix_rows, {"daily": 2, "weekly": 8, "monthly": 35}, 2)
    after = compute_monitoring_status(
        rows, {"daily": 2, "weekly": 8, "monthly": 35}, 2)

    # Pre-fix: the partial run is invisible, and the daily bucket complains
    # about ABSENCE while a real daily run had just finished partial.
    assert before["latest_runs"]["daily"]["last_status"] is None
    assert before["latest_runs"]["daily"]["latest_partial"] is False
    assert any("No daily run found" in w for w in before["warnings"])

    # Post-fix: the same rows, the same function, the run is seen.
    assert after["latest_runs"]["daily"]["last_status"] == "partial"
    assert after["latest_runs"]["daily"]["latest_partial"] is True


def test_98_the_presentation_layer_never_calls_a_partial_run_fresh():
    """The banner and the per-page strip, checked against the shipped JS.

    These are the two surfaces the blocker names, and neither is reachable from
    Python. Asserting on the source is weaker than driving a browser and far
    stronger than asserting nothing: the defect was a MISSING branch, and a
    missing branch is exactly what a structural check can see.
    """
    source = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    # `normalizeRunStatus` must pass `partial` through rather than fold it into
    # success — everything below depends on that.
    assert 'if (raw === "success") return "success";' in source
    assert "return raw || \"unknown\";" in source

    # Both surfaces branch on it explicitly.
    assert source.count('status === "partial"') >= 2, (
        "a partial run still falls through to a generic branch on at least one "
        "of the two run-status surfaces")

    banner = source[source.index("Latest recorded run failed"):]
    banner = banner[:banner.index("// ── Monitoring status banner")]
    assert "Latest run partial" in banner
    assert "some datasets were incomplete" in banner
    # Warning, not OK and not error: work landed, but not all of it.
    partial_branch = banner[banner.index('status === "partial"'):]
    partial_branch = partial_branch[:partial_branch.index("} else if")]
    assert "freshness-warning" in partial_branch
    assert "freshness-ok" not in partial_branch
    assert "freshness-error" not in partial_branch

    meta = source[source.index("function renderRunMeta"):]
    meta = meta[:meta.index("// ── Per-page dataset-level freshness strip")]
    meta_partial = meta[meta.index('status === "partial"'):]
    meta_partial = meta_partial[:meta_partial.index("} else if")]
    assert "Latest run partial" in meta_partial
    assert "is-fresh" not in meta_partial, (
        "per-page run metadata still describes a partial run as Fresh")
    assert "· Fresh`" not in meta_partial


# ═════════════════════════════════════════════════════════════════════════════
# §1 (fifth review) — canonical dataset freshness must not paint `partial` green
#
# The durable run status, the global banner, the per-page run strip and
# monitoring severity were all corrected in the fourth review. Canonical dataset
# freshness was the surface left behind: `compute_canonical_freshness` branched
# on `running` and on `failed`, and had no branch for `partial` — so a truncated
# sync fell straight through to the staleness test and, being recent and having
# rows, came out FRESH_WITH_DATA at `ok` severity.
#
# It is deliberately NOT folded into `failed`. A failed sync may have written
# nothing; a partial one wrote everything it read. The two have different
# remedies, and different meanings for the rows already on screen.
# ═════════════════════════════════════════════════════════════════════════════

import services.freshness_service as freshness_svc  # noqa: E402

_FRESHNESS_BASE = {
    "dataset": "contact_funnel",
    "rows_in_window": 10,
    "latest_source_date": None,          # filled per call — "today"
    "sync_status": "success",
    "latest_batch_status": "success",
    "latest_batch_row_count": 10,
    "last_successful_sync_at": None,     # filled per call — "now"
    "stale_threshold_days": 2,
    "row_count_supported": True,
}


def _freshness(**over):
    """The canonical verdict for a recent, populated dataset, varied one factor."""
    kwargs = {**_FRESHNESS_BASE, **over}
    kwargs.setdefault("latest_source_date", date.today())
    if kwargs.get("last_successful_sync_at") is None \
            and "last_successful_sync_at" not in over:
        kwargs["last_successful_sync_at"] = datetime.now(tz=timezone.utc)
    return freshness_svc.compute_canonical_freshness(**kwargs)


def test_99_a_partial_sync_is_never_canonically_fresh():
    """The exact reproduction from the review, and its controls.

    A recent, populated dataset whose latest sync ended `partial` must not
    report `fresh_with_data` at `ok` severity — the rows are real, but the
    population behind them is incomplete.
    """
    status = freshness_svc.CanonicalFreshnessStatus

    partial = _freshness(sync_status="partial", latest_batch_status="partial",
                         latest_batch_row_count=10)

    assert partial["canonical_status"] != status.FRESH_WITH_DATA
    assert partial["severity"] != "ok", "a truncated sync was painted green"
    assert partial["canonical_status"] == status.DATA_AVAILABLE_LATEST_SYNC_PARTIAL
    assert partial["severity"] == "warning", (
        "and it is not an error either — real work landed")
    assert "PARTIAL" in partial["reason"] or "partial" in partial["reason"]
    assert partial["next_action"].strip(), "a warning must say what to do"

    # The control that makes the assertion mean something: the SAME dataset,
    # the same recency, the same rows — only the sync outcome differs.
    clean = _freshness()
    assert clean["canonical_status"] == status.FRESH_WITH_DATA
    assert clean["severity"] == "ok"


def test_100_partial_is_distinguished_from_failed_and_from_success():
    """Three outcomes, three verdicts, at this layer too.

    Folding `partial` into `failed` would be the easy fix and the wrong one: a
    failed sync may have written nothing, a partial one wrote everything it
    read, and the remedies differ.
    """
    status = freshness_svc.CanonicalFreshnessStatus

    seen = {
        name: _freshness(sync_status=name, latest_batch_status=name)
        for name in ("success", "partial", "failed")
    }

    assert seen["success"]["canonical_status"] == status.FRESH_WITH_DATA
    assert seen["partial"]["canonical_status"] == \
        status.DATA_AVAILABLE_LATEST_SYNC_PARTIAL
    assert seen["failed"]["canonical_status"] == \
        status.DATA_AVAILABLE_LATEST_SYNC_FAILED

    assert len({v["canonical_status"] for v in seen.values()}) == 3
    assert [seen[k]["severity"] for k in ("success", "partial", "failed")] == \
        ["ok", "warning", "warning"]


@pytest.mark.parametrize("field", ["sync_status", "latest_batch_status"])
def test_101_partial_from_either_source_is_enough(field):
    """`sync_state` and the latest batch can disagree; either one blocks green.

    The `failed` branch already reads both with `or`. Reading only one would
    leave a hole exactly where the two records disagree — which is precisely
    when something has gone wrong.
    """
    status = freshness_svc.CanonicalFreshnessStatus

    verdict = _freshness(**{field: "partial"})
    assert verdict["canonical_status"] == status.DATA_AVAILABLE_LATEST_SYNC_PARTIAL
    assert verdict["severity"] == "warning"


def test_102_a_partial_sync_with_no_rows_does_not_claim_an_empty_window():
    """"Nothing arrived" and "nothing exists" are different facts.

    A truncated sync that produced no rows leaves a window that was never fully
    read. Reporting it as a clean empty would turn an unfinished scan into a
    measured zero.
    """
    status = freshness_svc.CanonicalFreshnessStatus

    verdict = _freshness(sync_status="partial", latest_batch_status="partial",
                         rows_in_window=0, latest_batch_row_count=0)

    assert verdict["canonical_status"] == status.PARTIAL_NO_DATA
    assert verdict["severity"] == "error"
    assert verdict["canonical_status"] not in (
        status.FRESH_BUT_EMPTY, status.EMPTY_SUCCESS), (
        "a truncated scan was reported as a proven-empty window")
    assert "NOT proven empty" in verdict["reason"]

    # Control: the same zero rows after a SUCCESSFUL sync IS a clean empty.
    clean_empty = _freshness(rows_in_window=0, latest_batch_row_count=0)
    assert clean_empty["canonical_status"] == status.EMPTY_SUCCESS


def test_103_a_partial_sync_never_claims_a_row_count_it_did_not_measure():
    """An unmeasured row count stays unmeasured — but still says `partial`.

    Returning PARTIAL_NO_DATA here would assert an emptiness nobody looked for.
    Returning a bare neutral "row count unavailable" would read as a tooling
    gap rather than an incomplete population, so the partial fact is carried
    into the reason.
    """
    status = freshness_svc.CanonicalFreshnessStatus

    unknown = _freshness(sync_status="partial", latest_batch_status="partial",
                         rows_in_window=None)
    assert unknown["canonical_status"] == status.UNKNOWN_ROW_COUNT
    assert unknown["canonical_status"] != status.PARTIAL_NO_DATA
    assert "partial" in unknown["reason"].lower()

    not_enabled = _freshness(sync_status="partial", latest_batch_status="partial",
                             rows_in_window=None, row_count_supported=False)
    assert not_enabled["canonical_status"] == status.ROW_COUNT_NOT_ENABLED
    assert "partial" in not_enabled["reason"].lower()


def test_104_the_new_states_are_wired_into_every_registry():
    """A status the rest of the system does not know about is worse than none.

    `ALL` drives the display-label test; `SEVERITY_MAP` drives every badge;
    `HAS_DATA_STATES` decides whether a derived dataset can be built; and
    `BLOCKING_STATES` decides whether it is blocked. Missing from any of them,
    a new status silently degrades to a neutral "unknown" somewhere.
    """
    status = freshness_svc.CanonicalFreshnessStatus
    partial_with_data = status.DATA_AVAILABLE_LATEST_SYNC_PARTIAL
    partial_no_data = status.PARTIAL_NO_DATA

    for s in (partial_with_data, partial_no_data):
        assert s in status.ALL
        assert s in freshness_svc.SEVERITY_MAP
        assert freshness_svc.canonical_status_display_label(s) != "Unknown"

    # Rows exist → a dependant can still be derived, and is NOT blocked.
    assert partial_with_data in freshness_svc.HAS_DATA_STATES
    assert partial_with_data not in freshness_svc.BLOCKING_STATES
    # Nothing usable arrived → nothing downstream can be derived.
    assert partial_no_data not in freshness_svc.HAS_DATA_STATES
    assert partial_no_data in freshness_svc.BLOCKING_STATES

    # The same wiring, on the two mirrored `failed` states — so this test is
    # asserting a shape the module already holds, not one invented for it.
    assert status.DATA_AVAILABLE_LATEST_SYNC_FAILED in freshness_svc.HAS_DATA_STATES
    assert status.FAILED_NO_DATA in freshness_svc.BLOCKING_STATES


def test_105_the_dataset_freshness_ui_never_labels_a_partial_sync_fresh():
    """The shipped JS, checked structurally.

    A canonical status absent from the label and class maps falls back to
    `run-meta` with the raw key as its text — no badge, no styling, no signal.
    That is exactly how a new state goes unnoticed.
    """
    source = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    for key in ("data_available_latest_sync_partial", "partial_no_data"):
        assert f"{key}:" in source, f"{key} is missing from the JS status maps"
        assert source.count(f'"{key}"') + source.count(f"{key}:") >= 2, (
            f"{key} is not wired into both the label/class maps and the "
            f"severity ordering")

    labels = source[source.index("const _csLabels"):]
    labels = labels[:labels.index("};")]
    assert "Data available, latest sync partial" in labels

    classes = source[source.index("const _csClasses"):]
    classes = classes[:classes.index("};")]
    partial_line = [ln for ln in classes.splitlines()
                    if "data_available_latest_sync_partial" in ln][0]
    assert "is-canonical-warning" in partial_line
    assert "is-fresh" not in partial_line, (
        "canonical freshness still styles a partial sync as fresh")

    # ── the single-dataset strip counts it as a warning, not as fresh ───────
    tally = source[source.index("const freshCount"):]
    tally = tally[:tally.index("const runningCount")]
    fresh_line = tally[:tally.index("const warningCount")]
    warning_line = tally[tally.index("const warningCount"):tally.index("const errorCount")]
    error_line = tally[tally.index("const errorCount"):]

    assert "data_available_latest_sync_partial" not in fresh_line
    assert "partial_no_data" not in fresh_line, (
        "a partial sync is being counted toward the fresh tally")
    assert "data_available_latest_sync_partial" in warning_line
    assert "partial_no_data" in error_line

    # ── the MULTI-dataset strip's per-dataset detail ────────────────────────
    #
    # `_shortLabels[status] || "Unknown"` is the fallback, so a status missing
    # here renders "Contact funnel: Unknown" — beside a summary that correctly
    # says "1 warning". The tally and the label then disagree in one line, and
    # the label is both the more specific and the more wrong of the two. The
    # Leads page, which draws several datasets at once, is where this shows.
    short = source[source.index("const _shortLabels"):]
    short = short[:short.index("};")]

    expected_short = {
        "data_available_latest_sync_partial": "Partial",
        "partial_no_data": "Partial, no data",
    }
    for key, label in expected_short.items():
        matching = [ln for ln in short.splitlines() if f"{key}:" in ln]
        assert matching, (
            f"{key} is missing from _shortLabels, so the multi-dataset strip "
            f"renders it as 'Unknown' beside a correct warning summary")
        assert f'"{label}"' in matching[0], (
            f"{key} should read {label!r} in the multi-dataset strip, got: "
            f"{matching[0].strip()}")
        assert '"Unknown"' not in matching[0]
        assert '"Fresh"' not in matching[0], (
            f"{key} is labelled Fresh in the multi-dataset strip")

    # The two labels are distinct, so a reader can tell which case they have.
    assert expected_short["data_available_latest_sync_partial"] != \
        expected_short["partial_no_data"]

    # And the fallback is still the only thing that produces "Unknown" — if it
    # were removed, the assertions above would pass over a different mechanism.
    assert '_shortLabels[statuses[i]] || "Unknown"' in source


def _js_object_literal(source: str, name: str) -> str:
    """Extract one `const <name> = { ... };` object literal, brace-balanced.

    PR-ADS-160-F1 §7. The first version sliced from the next `{` to the next
    `};`, which is only correct while the literal stays flat and contains no
    `};` inside a string. Either would truncate it silently, and the test would
    then evaluate a PARTIAL map and still pass — the failure mode a structural
    test exists to avoid.

    This walks the braces instead, skipping string literals and comments, so the
    slice is the whole object or the helper raises.
    """
    decl = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*{{", source)
    assert decl, f"{name} is not declared as a const object literal"

    i = decl.end() - 1                      # the opening brace
    depth, in_str, quote, esc, in_line, in_block = 0, False, "", False, False, False
    for j in range(i, len(source)):
        ch, nxt = source[j], source[j + 1:j + 2]
        if in_line:
            if ch == "\n":
                in_line = False
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'`":
            in_str, quote = True, ch
        elif ch == "/" and nxt == "/":
            in_line = True
        elif ch == "/" and nxt == "*":
            in_block = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[i:j + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def test_106_the_multi_dataset_label_lookup_is_evaluated_not_just_matched():
    """The real lookup expression, run in `node`, over the real map.

    This repository has no JS test harness — no `package.json`, no
    `node_modules`, no jsdom — and two labels do not justify introducing a
    frontend testing framework. But the `node` binary is already a CI
    dependency (`node --check static/app.js`), so the `_shortLabels` literal can
    be extracted and the ACTUAL expression evaluated against it:

        _shortLabels[statuses[i]] || "Unknown"

    That is a behavioural check of the lookup rather than a search for a
    substring, and it is what catches the real failure mode: a key that is
    present in the file but spelled differently from the status the backend
    emits still renders "Unknown".
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:                                     # pragma: no cover
        pytest.skip("node is unavailable; the structural checks still apply")

    source = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    literal = _js_object_literal(source, "_shortLabels")

    # Exactly the statuses the BACKEND emits, taken from the service itself —
    # not retyped here, so a rename on either side fails this test rather than
    # silently agreeing with a stale copy.
    status = freshness_svc.CanonicalFreshnessStatus
    probes = {
        "partial_with_data": status.DATA_AVAILABLE_LATEST_SYNC_PARTIAL,
        "partial_no_data": status.PARTIAL_NO_DATA,
        "fresh": status.FRESH_WITH_DATA,
        "failed_with_data": status.DATA_AVAILABLE_LATEST_SYNC_FAILED,
    }

    script = (
        f"const _shortLabels = {literal};\n"
        f"const probes = {json.dumps(probes)};\n"
        "const out = {};\n"
        "for (const [name, st] of Object.entries(probes)) {\n"
        "  out[name] = _shortLabels[st] || 'Unknown';\n"   # the real expression
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)

    assert rendered["partial_with_data"] == "Partial", rendered
    assert rendered["partial_no_data"] == "Partial, no data", rendered
    assert rendered["partial_with_data"] != "Unknown"
    assert rendered["partial_no_data"] != "Unknown"
    assert rendered["partial_with_data"] != rendered["partial_no_data"]
    # Never the word the whole review series is about.
    assert "Fresh" not in (rendered["partial_with_data"], rendered["partial_no_data"])

    # Controls: the states either side of the two new ones still render as
    # before, so this proves a gap was filled rather than the map rewritten.
    assert rendered["fresh"] == "Fresh"
    assert rendered["failed_with_data"] == "Degraded"


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-160-F1 §4 — direct evidence outranks inherited evidence
#
# `compute_canonical_freshness` checks the DEPENDENCY first, so a dataset whose
# OWN sync failed or stopped short had that fact replaced by
# `blocked_by_dependency`. Both statements are true; only one directs the right
# action. Fixing the upstream does not fix a dataset whose own sync is broken,
# so the operator repairs the dependency, re-checks, and finds this dataset
# still broken for a reason nothing told them.
#
# The real composition this reproduces: `lifecycle_events` depends on
# `contact_funnel` (services/freshness_service.py DATASET_FRESHNESS_CONFIG), and
# ONE truncated contact-funnel run finishes BOTH datasets' batches partial —
# see `_fail_batches` / the dual `finish_sync_batch` calls in
# `hubspot_contact_funnel_sync_service`. So "upstream blocked AND this dataset
# partial" is not a contrived pairing; it is what a single truncated sync
# produces.
# ═════════════════════════════════════════════════════════════════════════════

def _dependency_status_for(upstream_verdict):
    """How `api/server.py` composes `dependency_status` — blocking states win."""
    return (upstream_verdict["canonical_status"]
            if upstream_verdict["canonical_status"] in freshness_svc.BLOCKING_STATES
            else None)


def test_107_a_datasets_own_partial_state_survives_a_blocked_dependency():
    """The cascade, reproduced through the real pair and the real composition."""
    status = freshness_svc.CanonicalFreshnessStatus

    upstream = _freshness(dataset="contact_funnel", sync_status="partial",
                          latest_batch_status="partial", rows_in_window=0,
                          latest_batch_row_count=0)
    assert upstream["canonical_status"] == status.PARTIAL_NO_DATA
    assert upstream["canonical_status"] in freshness_svc.BLOCKING_STATES, (
        "control: the upstream must actually block, or there is no cascade")

    downstream = _freshness(dataset="lifecycle_events", sync_status="partial",
                            latest_batch_status="partial", rows_in_window=0,
                            latest_batch_row_count=0,
                            dependency_status=_dependency_status_for(upstream))

    assert downstream["canonical_status"] == status.PARTIAL_NO_DATA, (
        "the dataset's own partial state was replaced by the inherited one; "
        "the operator is sent upstream to fix something that will not fix this")
    assert downstream["canonical_status"] != status.BLOCKED_BY_DEPENDENCY

    # The dependency is NAMED rather than dropped — nothing is hidden either way.
    assert "upstream dependency" in downstream["reason"]
    assert "will not resolve this dataset" in downstream["reason"]


def test_108_an_inherited_state_still_wins_where_there_is_no_direct_evidence():
    """The other half of the precedence, and the reason it is not a reorder.

    `blocked_by_dependency` is exactly right when this dataset has nothing
    adverse of its own to say. Only DIRECT adverse evidence displaces it.
    """
    status = freshness_svc.CanonicalFreshnessStatus
    blocked = status.PARTIAL_NO_DATA

    # No evidence at all — the dependency IS the most specific thing known.
    never_ran = _freshness(dataset="lifecycle_events", sync_status=None,
                           latest_batch_status=None, rows_in_window=0,
                           dependency_status=blocked)
    assert never_ran["canonical_status"] == status.BLOCKED_BY_DEPENDENCY

    # Its own sync is FINE; only the upstream is broken.
    healthy = _freshness(dataset="lifecycle_events", sync_status="success",
                         latest_batch_status="success", rows_in_window=5,
                         dependency_status=blocked)
    assert healthy["canonical_status"] == status.BLOCKED_BY_DEPENDENCY

    # In progress is not adverse either.
    running = _freshness(dataset="lifecycle_events", sync_status="running",
                         latest_batch_status="running", rows_in_window=0,
                         dependency_status=blocked)
    assert running["canonical_status"] == status.BLOCKED_BY_DEPENDENCY


def test_109_the_precedence_is_the_same_for_failed_and_is_not_dataset_specific():
    """Deliberate, not accidental, and applied uniformly.

    The same rule governs the pre-existing `failed` states and the other real
    derived pairs. It is stated here so a future reader sees that
    `partial` was not given a private exemption.
    """
    status = freshness_svc.CanonicalFreshnessStatus

    own_failed = _freshness(dataset="lifecycle_events", sync_status="failed",
                            latest_batch_status="failed", rows_in_window=0,
                            dependency_status=status.FAILED_NO_DATA)
    assert own_failed["canonical_status"] == status.FAILED_NO_DATA
    assert "upstream dependency" in own_failed["reason"]

    # The other two configured dependency pairs behave identically.
    for dataset, upstream in (("canonical_geo", "canonical_spend"),
                              ("waste_terms", "search_terms")):
        deps = freshness_svc.DATASET_FRESHNESS_CONFIG[dataset]["depends_on"]
        assert deps == [upstream], f"{dataset} dependency config changed: {deps}"

        own = _freshness(dataset=dataset, sync_status="failed",
                         latest_batch_status="failed", rows_in_window=0,
                         dependency_status=status.FAILED_NO_DATA)
        assert own["canonical_status"] == status.FAILED_NO_DATA, dataset

        inherited = _freshness(dataset=dataset, sync_status=None,
                               latest_batch_status=None, rows_in_window=0,
                               dependency_status=status.FAILED_NO_DATA)
        assert inherited["canonical_status"] in (
            status.BLOCKED_BY_DEPENDENCY, status.NOT_RUN_NO_UPSTREAM_DATA), dataset


def test_110_the_label_extractor_is_brace_balanced_not_index_based():
    """The extractor's own negative control (PR-ADS-160-F1 §7).

    The previous slice ran from the next `{` to the next `};` — string- and
    comment-blind. It returns a TRUNCATED literal, losing every entry after the
    cut. Where the cut lands decides whether `test_106` then dies with a `node`
    syntax error or quietly evaluates a partial map; neither is acceptable, and
    only the second is detectable by reading the test's output.
    """
    # Nesting alone does NOT defeat the old slice — an inner `}` is followed by
    # a comma or newline, never `};` — so it is asserted here only as something
    # the new extractor must still get right, not as a case it rescues.
    nested = (
        'const _shortLabels = {\n'
        '  fresh_with_data: "Fresh",\n'
        '  meta: { note: "a nested object" },\n'
        '  partial_no_data: "Partial, no data",\n'
        '};\n'
    )
    got = _js_object_literal(nested, "_shortLabels")
    assert got.count("{") == got.count("}") == 2
    assert got.endswith("}")
    assert "partial_no_data" in got

    # A `};` inside a STRING is what actually breaks the old slice.
    awkward = (
        'const _shortLabels = {\n'
        '  quirk: "literally };",\n'
        '  partial_no_data: "Partial, no data",\n'
        '};\n'
    )
    got = _js_object_literal(awkward, "_shortLabels")
    assert "partial_no_data" in got

    start = awkward.index("const _shortLabels")
    naive = awkward[awkward.index("{", start):awkward.index("};", start) + 1]
    assert "partial_no_data" not in naive, (
        "the old slice survives this input, so this control proves nothing")

    # And it refuses rather than guessing when the literal is malformed.
    with pytest.raises(AssertionError):
        _js_object_literal("const somethingElse = {};", "_shortLabels")
