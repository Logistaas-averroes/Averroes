"""PR-ADS-179B — the independent re-audit's minors on PR-ADS-179A.

PR-ADS-179A made the certification audits build each window once, inside one
pinned ``REPEATABLE READ, READ ONLY`` snapshot (``docs/62``). The re-audit
left five minors; each case below fails on ``9a09c6d`` or under the mutation
``docs/62`` §8 names for it:

1. ``SchemaMigrationBlocked`` named one holder of the schema lock (a
   certification window) when another migration holds it too, and the Meta
   backfill CLI — which runs ``init_db`` — crashed with a traceback (exit 1)
   instead of exiting 2 ("unavailable");
3. ``SELECT 1; COMMIT; BEGIN ISOLATION LEVEL READ COMMITTED`` left the
   connection in a transaction, so the next statement in the same block ran
   outside the snapshot — READ COMMITTED, read-write — and was caught only at
   the block's savepoint release. So did ``ROLLBACK AND CHAIN`` (a NEW
   snapshot with the same modes), a commit through the raw connection, and
   the escape carried in an ``AsIs`` parameter the template does not show;
4. a window that could not take the shared schema lock ran unpinned and
   certified with nothing in the report saying so;
5. the window's session-level shared lock being released before its
   connection goes back to the pool was guarded only indirectly.

(Item 2 is a documentation correction: ``init_db`` still logs and swallows
every error other than the lock-wait bound, as before 179A.)

PR-ADS-179B-F1 (cases 06–08, each failing on ``acb4f66``): a broken pin
never certifies or reads as pinned; the disclosure reaches the stored daily
verdict and its output; ``callproc`` / ``copy_*`` / ``reset()`` cannot run
outside the snapshot.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import live_db  # noqa: E402,F401
from tests.test_pr_ads_179a_certification_runtime import (  # noqa: E402,F401
    _add_sql_contact, _count_contacts_via_reader, _in_another_session, ready,
)

ISOLATION_PROBE = ("SELECT current_setting('transaction_isolation'), "
                   "current_setting('transaction_read_only')")


def _hold_schema_lock(cluster, mode: str):
    """Another session holding ``SCHEMA_LOCK_KEY``: ``exclusive`` as a running
    migration holds it (transaction level), ``shared`` as an open
    certification window holds it (session level)."""
    from db.connection import SCHEMA_LOCK_KEY
    holder = cluster.connect()
    with holder.cursor() as cur:
        if mode == "exclusive":
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
        else:
            cur.execute("SELECT pg_advisory_lock_shared(%s)", (SCHEMA_LOCK_KEY,))
    return holder


def _schema_locks_of(cluster, pid: int) -> list:
    """``pg_locks`` rows for ``SCHEMA_LOCK_KEY`` held by backend ``pid``, read
    by an independent observer session."""
    from db.connection import SCHEMA_LOCK_KEY
    with cluster.connect() as obs, obs.cursor() as cur:
        cur.execute(
            "SELECT mode, granted FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = %s AND classid = %s AND objid = %s AND objsubid = 1",
            (pid, SCHEMA_LOCK_KEY >> 32, SCHEMA_LOCK_KEY & 0xFFFFFFFF))
        rows = cur.fetchall()
        obs.rollback()
    return rows


# ═════════════════════════════════════════════════════════════════════════════
# 1 · SchemaMigrationBlocked: both holders named; the backfill CLI exits 2
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("holder_mode", ["shared", "exclusive"])
def test_01_a_the_backfill_cli_exits_2_when_the_schema_lock_is_held(
        live_db, monkeypatch, holder_mode):
    """``scripts.backfill_meta_ads_spend`` runs ``init_db``. Held past the
    bound — by a certification window (shared) or by another migration
    (exclusive) — the schema lock makes ``init_db`` raise
    ``SchemaMigrationBlocked``. The CLI reports that and exits 2
    ("unavailable"), and never reaches Meta. On ``9a09c6d`` the exception
    escaped ``main`` (a traceback, exit 1)."""
    import db.schema as schema
    from scripts import backfill_meta_ads_spend as script
    from services import meta_ads_spend_service as meta
    monkeypatch.setattr(schema, "SCHEMA_LOCK_WAIT_SECONDS", 1)
    reached_meta = []
    monkeypatch.setattr(meta, "run_backfill",
                        lambda *a, **k: reached_meta.append(a) or {"status": "success"})
    holder = _hold_schema_lock(live_db, holder_mode)
    printed = io.StringIO()
    try:
        with redirect_stdout(printed):
            code = script.main(["--from", "2026-11-01", "--to", "2026-11-17"])
    finally:
        holder.rollback()
        holder.close()
    assert code == 2, printed.getvalue()
    out = json.loads(printed.getvalue())
    assert out["status"] == "failed" and out["reason"] == "schema_migration_blocked", out
    assert reached_meta == [], "the backfill ran without its schema migration"


def test_01_b_schema_migration_blocked_names_both_possible_holders(live_db, monkeypatch):
    """Held by ANOTHER MIGRATION (no certification window open), the error
    must not claim certification windows held the lock: it names both
    possible holders. On ``9a09c6d`` it named windows only."""
    import db.schema as schema
    monkeypatch.setattr(schema, "SCHEMA_LOCK_WAIT_SECONDS", 1)
    holder = _hold_schema_lock(live_db, "exclusive")
    try:
        with pytest.raises(schema.SchemaMigrationBlocked) as raised:
            schema.init_db()
    finally:
        holder.rollback()
        holder.close()
    message = str(raised.value)
    assert "did NOT run" in message
    assert "certification window" in message and "another migration" in message, message
    schema.init_db()                     # lock free: the migration applies


# ═════════════════════════════════════════════════════════════════════════════
# 3 · no statement runs after a reader's statement ends the pinned transaction
# ═════════════════════════════════════════════════════════════════════════════
def _probe_after(escape, seen: list) -> None:
    """In ONE ``get_conn()`` block: run ``escape`` (a reader's statement that
    ends the pinned transaction and opens another), swallow whatever it
    raises, then ask the session which transaction the NEXT statement runs
    in. What that statement saw is appended to ``seen`` the moment it
    returns — before the block's own exit can raise — so a statement that
    ran is never hidden by a later refusal."""
    from db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        try:
            escape(cur)
        except Exception:  # noqa: BLE001 — a reader that swallows everything
            pass
        cur.execute(ISOLATION_PROBE)
        seen.append(cur.fetchone())


def test_03_a_commit_then_begin_read_committed_never_runs_the_next_statement_outside(ready):
    """The re-audit's exact probe: ``SELECT 1; COMMIT; BEGIN ISOLATION LEVEL
    READ COMMITTED`` leaves the connection INTRANS — in a NEW, READ COMMITTED,
    read-write transaction. On ``9a09c6d`` the next statement in the same
    block ran there and reported ``read committed`` / ``off``. It must be
    refused (or report the pinned snapshot's modes), never read committed."""
    from db.connection import PinnedSnapshotError, current_read_snapshot
    from services.snapshot_memo import certification_window
    seen: list = []
    with certification_window() as scope:
        assert scope is not None
        pin = current_read_snapshot()
        try:
            _probe_after(lambda cur: cur.execute(
                "SELECT 1; COMMIT; BEGIN ISOLATION LEVEL READ COMMITTED"), seen)
        except PinnedSnapshotError:
            pass
        assert all(s == ("repeatable read", "on") for s in seen), \
            f"a statement ran outside the snapshot: {seen}"
        assert seen == [], "the statement after the escape must not run at all"
        assert pin.broken and pin.violations, pin.violations
        with pytest.raises(PinnedSnapshotError):
            _count_contacts_via_reader()              # nothing else runs in it


def test_03_b_rollback_and_chain_is_a_new_snapshot_and_is_refused(ready):
    """``ROLLBACK AND CHAIN`` opens a new transaction with the SAME modes
    (repeatable read, read only) — so the modes alone cannot see it — but a
    NEW snapshot: data another session committed after the window opened
    becomes visible. On ``9a09c6d`` the next read saw that row."""
    from db.connection import PinnedSnapshotError, current_read_snapshot, get_conn
    from services.snapshot_memo import certification_window
    seen = []
    with certification_window():
        pin = current_read_snapshot()
        before = _count_contacts_via_reader()
        _in_another_session(_add_sql_contact, "c-179b-chain")
        assert _count_contacts_via_reader() == before      # the snapshot holds

        def chained():
            with get_conn() as conn, conn.cursor() as cur:
                try:
                    cur.execute("SELECT 1; ROLLBACK AND CHAIN")
                except Exception:  # noqa: BLE001 — swallowed
                    pass
                cur.execute("SELECT count(*) FROM hubspot_contact_funnel")
                seen.append(cur.fetchone()[0])
        with pytest.raises(PinnedSnapshotError):
            chained()
        assert seen == [], f"a read ran in a new snapshot: {seen} (window saw {before})"
        assert pin.broken
    # And a bare ROLLBACK AND CHAIN is refused before it is sent.
    with certification_window():
        pin = current_read_snapshot()
        with pytest.raises(PinnedSnapshotError):
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("ROLLBACK AND CHAIN")
        assert not pin.broken and len(pin.violations) == 1
        assert _count_contacts_via_reader() > 0


def test_03_c_a_commit_through_the_raw_connection_is_caught_before_the_next_statement(ready):
    """``cursor.connection`` is psycopg2's own connection: its ``commit()``
    ends the pinned transaction without sending a statement, and psycopg2
    opens a new READ COMMITTED one for the next. On ``9a09c6d`` that next
    statement ran there."""
    from db.connection import PinnedSnapshotError, current_read_snapshot
    from services.snapshot_memo import certification_window
    seen: list = []
    with certification_window():
        pin = current_read_snapshot()
        with pytest.raises(PinnedSnapshotError):
            _probe_after(lambda cur: cur.connection.commit(), seen)
        assert seen == [], seen
        assert pin.broken


def test_03_c2_an_escape_carried_in_a_parameter_is_caught_too(ready):
    """``AsIs`` puts raw SQL into the statement psycopg2 sends, where the
    template shows none of it: the verb check reads what was SENT, not only
    the template. On ``9a09c6d`` the next statement ran outside."""
    from psycopg2.extensions import AsIs
    from db.connection import PinnedSnapshotError, current_read_snapshot
    from services.snapshot_memo import certification_window
    seen: list = []
    with certification_window():
        pin = current_read_snapshot()
        with pytest.raises(PinnedSnapshotError):
            _probe_after(lambda cur: cur.execute(
                "SELECT 1; %s", (AsIs("COMMIT; BEGIN ISOLATION LEVEL READ COMMITTED"),)),
                seen)
        assert seen == [], seen
        assert pin.broken


def test_03_d_a_readers_ordinary_statements_still_run_in_the_snapshot(ready):
    """Not an over-refusal: ``CASE … END``, a reader's own savepoint and
    ``ROLLBACK TO SAVEPOINT``, and its satisfied ``SET TRANSACTION`` requests
    all still run, in the snapshot, with nothing recorded against it."""
    from db.connection import current_read_snapshot, get_conn
    from services.snapshot_memo import certification_window
    with certification_window():
        pin = current_read_snapshot()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT CASE WHEN count(*) > 0 THEN 'commit' END "
                        "FROM hubspot_contact_funnel")
            assert cur.fetchone() == ("commit",)
            cur.execute("SAVEPOINT reader_own")
            cur.execute("SELECT 1")
            cur.execute("ROLLBACK TO SAVEPOINT reader_own")
            cur.execute("RELEASE SAVEPOINT reader_own")
            cur.executemany("SELECT %s", [(1,), (2,)])
            cur.execute(ISOLATION_PROBE)
            assert cur.fetchone() == ("repeatable read", "on")
        assert pin.violations == [] and not pin.broken
        assert _count_contacts_via_reader() > 0


# ═════════════════════════════════════════════════════════════════════════════
# 4 · an unpinned window is disclosed in every audit's report
# ═════════════════════════════════════════════════════════════════════════════
def test_04_an_unpinned_fallback_is_disclosed_per_window_in_all_three_audits(
        ready, monkeypatch):
    """A migration holds the schema lock, so every window gives up waiting
    and runs unpinned (as ``08_c`` forces it). Each audit still exits as it
    would have — an unpinned window is the pre-179A behaviour, not a failed
    check — but its report now says, per window, ``pinned: false`` and why,
    with a summary note. On ``9a09c6d`` nothing in any report said so."""
    import db.connection as connection
    from analysis.business_windows import WINDOW_KEYS
    from scripts import audit_campaigns_page_certification as camp
    from scripts import audit_customer_revenue_reader_cutover as reader
    from scripts import audit_marketing_outcome_cohorts as cohorts
    monkeypatch.setattr(connection, "WINDOW_SCHEMA_LOCK_WAIT_SECONDS", 0.2)
    now = datetime.now(tz=timezone.utc)

    def unpinned(entry, name):
        snap = entry.get("snapshot")
        assert snap is not None, f"{name}: no snapshot disclosure"
        assert snap["pinned"] is False, (name, snap)
        assert snap["reason"] == "schema_lock_wait_exceeded", (name, snap)
        assert snap["detail"], (name, snap)

    def summarised(summary, names):
        assert summary["all_pinned"] is False
        assert summary["windows"] == len(names) and summary["pinned"] == 0
        assert [u["window"] for u in summary["unpinned"]] == list(names)
        assert "UNPINNED" in summary["note"] and "snapshot-consistent" in summary["note"]

    holder = _hold_schema_lock(ready, "exclusive")
    try:
        a, rep = cohorts.run(now)
        _, rrep = reader.run(now)
        f, per_window = camp.run(tuple(WINDOW_KEYS), now=now)
    finally:
        holder.rollback()
        holder.close()

    # The exit codes are unchanged: everything else on the fixture holds.
    assert a.exit_code == 0, (a.violations, a.unavailable)
    assert rrep["exit_code"] == 0, rrep["pages_not_run"]
    assert f.exit_code == 0, (f.violations, f.unavailable)

    for entry in rep["business_windows"]:
        unpinned(entry, f"cohorts/{entry['window']}")
    summarised(rep["snapshot_pinning"], WINDOW_KEYS)

    for entry in rrep["windows"] + rrep["evidence_windows"]:
        unpinned(entry, f"reader_cutover/{entry['window']}")
    summarised(rrep["snapshot_pinning"],
               [*WINDOW_KEYS, *(f"evidence/{k}" for k in reader.EVIDENCE_PASS_WINDOWS)])

    for w in WINDOW_KEYS:
        unpinned(per_window[w], f"campaigns/{w}")
    summarised(f.snapshot_pinning, WINDOW_KEYS)

    # With the lock free, the same audit discloses a pinned window.
    f2, pw2 = camp.run(("ytd",), now=now)
    assert pw2["ytd"]["snapshot"] == {"pinned": True, "reason": None, "detail": None}
    assert f2.snapshot_pinning["all_pinned"] is True and f2.snapshot_pinning["unpinned"] == []


# ═════════════════════════════════════════════════════════════════════════════
# 5 · the shared schema lock never rides a pooled connection
# ═════════════════════════════════════════════════════════════════════════════
def test_05_the_windows_shared_lock_is_released_before_its_connection_is_pooled(ready):
    """A window takes ``SCHEMA_LOCK_KEY`` SHARED at session level; a session
    lock outlives the transaction, so a connection pooled still holding it
    would block every later migration for as long as the pool keeps it. Read
    through ``pg_locks`` on the window connection's own backend pid: held
    during the window, gone once it closes — on that same connection, which
    the pool then hands out again. Fails when ``_release_connection`` skips
    ``pg_advisory_unlock_shared``."""
    from db.connection import get_conn
    from services.snapshot_memo import certification_window
    with certification_window() as scope:
        assert scope is not None
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        held = _schema_locks_of(ready, pid)
        assert held == [("ShareLock", True)], held        # the check can see it
    assert _schema_locks_of(ready, pid) == [], \
        f"backend {pid} still holds the shared schema lock after its window closed"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        assert cur.fetchone()[0] == pid, "the window's connection was not pooled"


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-179B-F1 — truth audit round 1
# ═════════════════════════════════════════════════════════════════════════════
def _raw_cursor_escape_then_raise(seen: list) -> None:
    """The auditor's probe: a reader takes psycopg2's OWN cursor
    (``cursor.connection.cursor()`` — the disclosed bypass of the view's
    per-statement checks), ends the pinned transaction, reads in the new one,
    then fails; whatever called it swallows the failure."""
    from db.connection import get_conn
    try:
        with get_conn() as conn, conn.cursor() as cur:
            raw = cur.connection.cursor()
            raw.execute("COMMIT; BEGIN ISOLATION LEVEL READ COMMITTED")
            raw.execute(ISOLATION_PROBE)
            seen.append(raw.fetchone())
            raise RuntimeError("the reader fails after its escape")
    except Exception:  # noqa: BLE001 — swallowed, as a careless caller would
        pass


def _rollback_and_chain_swallowed(seen: list) -> None:
    from db.connection import get_conn
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1; ROLLBACK AND CHAIN")
    except Exception:  # noqa: BLE001
        pass


@pytest.mark.parametrize("escape", [_raw_cursor_escape_then_raise,
                                    _rollback_and_chain_swallowed],
                         ids=["raw_cursor_escape", "rollback_and_chain"])
def test_06_a_broken_window_never_certifies_or_reads_as_pinned(ready, monkeypatch, escape):
    """F1 item 1. The escape is the LAST thing the campaigns audit's window
    reads, so no later read trips over the broken snapshot. On ``acb4f66``:
    with the raw-cursor escape ``_Pin._undo`` set ``broken`` but recorded no
    refusal, so the window certified with exit 0 and ``pinned: true`` after
    reading ``('read committed', 'off')``; with ``ROLLBACK AND CHAIN`` the
    refusal made it exit 2 while it still read ``pinned: true`` and the note
    called it a snapshot-consistent build."""
    from db.connection import current_read_snapshot
    from scripts import audit_campaigns_page_certification as camp
    from services.snapshot_memo import current_scope, refused_reads
    real = camp.audit_window
    seen: list = []
    refused: list = []

    def last_read_escapes(w, f, *, now=None):
        out = real(w, f, now=now)
        assert current_read_snapshot() is not None
        escape(seen)
        refused.append(refused_reads(current_scope()))
        return out
    monkeypatch.setattr(camp, "audit_window", last_read_escapes)
    f, per_window = camp.run(("ytd",), now=datetime.now(tz=timezone.utc))
    if escape is _raw_cursor_escape_then_raise:
        assert seen == [("read committed", "off")], seen   # the probe did escape
    assert refused and refused[0], "the broken snapshot recorded no refusal"
    assert f.exit_code == 2 and f.violations == [], (f.violations, f.unavailable)
    assert any(u.startswith("pinned_snapshot[ytd]") for u in f.unavailable), f.unavailable
    snap = per_window["ytd"]["snapshot"]
    assert snap["pinned"] is False and snap["reason"] == "snapshot_broken", snap
    summary = f.snapshot_pinning
    assert summary["all_pinned"] is False and summary["pinned"] == 0, summary
    assert [u["window"] for u in summary["unpinned"]] == ["ytd"]
    assert not summary["note"].startswith("every window"), summary["note"]
    assert "NOT a snapshot-consistent build" in summary["note"], summary["note"]


def test_06_b_the_window_entry_reads_the_pin_itself(ready):
    """``window_snapshot`` decides from the pin, not only from the scope's
    existence: a pin with a recorded refusal, or a broken pin, is reported
    ``snapshot_broken`` (on ``acb4f66``: ``pinned: true``)."""
    from db.connection import PinnedSnapshotError, current_read_snapshot, get_conn
    from services.snapshot_memo import certification_window, window_snapshot
    disclosure: dict = {}
    with certification_window(disclosure) as scope:
        pin = current_read_snapshot()
        assert window_snapshot(scope, disclosure)["pinned"] is True
        with pytest.raises(PinnedSnapshotError):
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        assert pin.violations and not pin.broken
    assert window_snapshot(scope, disclosure) == {
        "pinned": False, "reason": "snapshot_broken",
        "detail": window_snapshot(scope, disclosure)["detail"]}
    assert "refused" in window_snapshot(scope, disclosure)["detail"]


def _platform_report(pinning: dict) -> dict:
    return {"verdict": "CERTIFIED", "exit_code": 0,
            "generated_at": "2026-11-25T07:30:00+00:00",
            "audits": [{"key": "campaigns", "module": "scripts.audit_campaigns_page_certification",
                        "proves": "the Campaigns page", "outcome": "holds",
                        "exit_code": 0, "seconds": 15.0, "violations": [],
                        "unavailable": [],
                        "report": {"certified": True, "exit_code": 0,
                                   "snapshot_pinning": pinning}},
                       {"key": "closed_won_truth", "module": "m", "proves": "p",
                        "outcome": "holds", "exit_code": 0, "seconds": 2.0,
                        "violations": [], "unavailable": [], "report": {}}],
            "crm_data_to_fix": None}


def test_07_an_unpinned_window_reaches_the_stored_daily_verdict_and_its_output(capsys):
    """F1 item 2. ``summarize()`` builds the row stored in
    ``platform_certification_runs``; on ``acb4f66`` it dropped every audit's
    ``snapshot_pinning``, and ``_render`` printed nothing about it, so a
    CERTIFIED day built on unpinned windows looked like any other. (No
    database needed.)"""
    from scripts import audit_platform_certification as apc
    from services import platform_certification_service as pcs
    from services.snapshot_memo import pinning_summary
    pinning = pinning_summary([
        ("current_quarter", {"pinned": True, "reason": None, "detail": None}),
        ("ytd", {"pinned": False, "reason": "schema_lock_wait_exceeded",
                 "detail": "the shared schema lock was not granted within 60s"})])
    report = _platform_report(pinning)

    stored = json.loads(json.dumps(pcs.summarize(report)))
    camp = next(a for a in stored["audits"] if a["key"] == "campaigns")
    assert camp["snapshot_pinning"]["all_pinned"] is False
    assert [u["window"] for u in camp["snapshot_pinning"]["unpinned"]] == ["ytd"]
    assert camp["snapshot_pinning"]["unpinned"][0]["reason"] == "schema_lock_wait_exceeded"
    assert "NOT a snapshot-consistent build" in camp["snapshot_pinning"]["note"]
    other = next(a for a in stored["audits"] if a["key"] == "closed_won_truth")
    assert "snapshot_pinning" not in other         # it reads no window

    apc._render(report)
    out = capsys.readouterr().out
    assert "ytd" in out and "NOT a snapshot-consistent build" in out, out

    # Every window pinned: stored, and nothing extra printed.
    report = _platform_report(pinning_summary(
        [("ytd", {"pinned": True, "reason": None, "detail": None})]))
    camp = next(a for a in pcs.summarize(report)["audits"] if a["key"] == "campaigns")
    assert camp["snapshot_pinning"]["all_pinned"] is True
    apc._render(report)
    assert "snapshot-consistent" not in capsys.readouterr().out


def test_08_copy_callproc_and_reset_cannot_run_outside_the_snapshot(ready):
    """F1 item 3. ``callproc`` and ``copy_*`` passed straight through the
    pinned cursor, and ``reset()`` through the pinned view. On ``acb4f66``,
    after ``cursor.connection.commit()``, ``copy_expert`` read
    ``read committed\\toff``."""
    from db.connection import PinnedSnapshotError, current_read_snapshot, get_conn
    from services.snapshot_memo import certification_window
    copy_probe = ("COPY (SELECT current_setting('transaction_isolation'), "
                  "current_setting('transaction_read_only')) TO STDOUT")
    probes = {
        "copy_expert": lambda cur, buf: cur.copy_expert(copy_probe, buf),
        "copy_to": lambda cur, buf: cur.copy_to(buf, "hubspot_contact_funnel"),
        "callproc": lambda cur, buf: buf.write(str(
            cur.callproc("current_setting", ("transaction_isolation",))
            and cur.fetchone())),
    }
    for name, probe in probes.items():
        buf = io.StringIO()
        with certification_window():
            pin = current_read_snapshot()
            with pytest.raises(PinnedSnapshotError):
                with get_conn() as conn, conn.cursor() as cur:
                    cur.connection.commit()
                    probe(cur, buf)
            assert buf.getvalue() == "", (name, buf.getvalue())
            assert pin.broken and pin.violations, name
    # In an intact snapshot they still run, in it.
    with certification_window():
        pin = current_read_snapshot()
        buf = io.StringIO()
        with get_conn() as conn, conn.cursor() as cur:
            cur.copy_expert(copy_probe, buf)
            cur.callproc("current_setting", ("transaction_read_only",))
            assert cur.fetchone() == ("on",)
        assert buf.getvalue() == "repeatable read\ton\n"
        assert pin.violations == [] and not pin.broken
    # reset() would end the transaction: refused, recorded, and nothing ran.
    with certification_window():
        pin = current_read_snapshot()
        with pytest.raises(PinnedSnapshotError):
            with get_conn() as conn:
                conn.reset()
        assert pin.violations and not pin.broken
        assert _count_contacts_via_reader() > 0          # the snapshot is intact
