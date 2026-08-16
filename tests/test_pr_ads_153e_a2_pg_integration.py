"""
tests/test_pr_ads_153e_a2_pg_integration.py

PR-ADS-153E-A2 — PostgreSQL-backed proof of the cutover interlock.

The unit suite proves the DECISION (given this state, does the gate fail?).
This suite proves the STATE ITSELF — that the columns the decision reads are
written correctly by real SQL against a real cluster:

  * a bootstrap run never stamps ``last_incremental_at``;
  * an incremental run never resets a completed bootstrap;
  * the first ``bootstrap_started_at`` survives every retry;
  * ``bootstrap_completed_at`` is written only on a run that PROVED it reached
    the end of the result set;
  * a failed or unproven run cannot mark the bootstrap complete;
  * the audit gate reads all of it and exits accordingly, in both single-window
    and ``--all-windows`` modes.

The suite spins up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user. Every ``sudo`` passes ``-n``: a password-protected sudo
must fail immediately, not block the run on a prompt with no tty to answer it.
If the binaries or that user are unavailable the module is skipped — and CI
fails loudly on a skip, because a skipped database suite is not merge evidence.

Read-only against every external platform; the only writes are local.

Run with:
    python -m pytest tests/test_pr_ads_153e_a2_pg_integration.py -v
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# ── Locate the PostgreSQL server binaries ────────────────────────────────────
_PG_BIN = None
for _cand in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
    if os.path.exists(os.path.join(_cand, "initdb")):
        _PG_BIN = _cand
        break


def _have_postgres() -> bool:
    if not _PG_BIN:
        return False
    try:
        import pwd
        pwd.getpwnam("postgres")
    except (KeyError, ImportError):
        return False
    if shutil.which("sudo") is None:
        return False
    probe = subprocess.run(["sudo", "-n", "-u", "postgres", "true"],
                           capture_output=True, text=True)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


class _PgCluster:
    """A throwaway PostgreSQL cluster (initdb + start), owned by ``postgres``."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pg153ea2_")
        # `postgres` must be able to create data/ and write the socket + log
        # inside this directory. mkdtemp gives us 0700, so widen it — chmod
        # only needs ownership, which we have, and works without root.
        os.chmod(self.tmp, 0o777)
        _run(["sudo", "-n", "chown", "-R", "postgres:postgres", self.tmp])
        self.data = os.path.join(self.tmp, "data")
        self.port = _free_port()
        self.url = None

    def start(self):
        r = _run(["sudo", "-n", "-u", "postgres", os.path.join(_PG_BIN, "initdb"),
                  "-D", self.data, "-A", "trust", "-E", "UTF8"])
        if r.returncode != 0:
            raise RuntimeError(f"initdb failed: {r.stderr}")
        r = _run(["sudo", "-n", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
                  "-D", self.data, "-l", os.path.join(self.tmp, "log"), "-w",
                  "-o", f"-p {self.port} -k {self.tmp} -h 127.0.0.1", "start"])
        if r.returncode != 0:
            raise RuntimeError(f"pg_ctl start failed: {r.stderr}")
        import psycopg2
        for _ in range(20):
            try:
                c = psycopg2.connect(host="127.0.0.1", port=self.port,
                                     user="postgres", dbname="postgres")
                break
            except psycopg2.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError("could not connect to freshly started postgres")
        c.autocommit = True
        c.cursor().execute("CREATE DATABASE app")
        c.close()
        self.url = f"postgresql://postgres@127.0.0.1:{self.port}/app"
        return self

    def stop(self):
        _run(["sudo", "-n", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
              "-D", self.data, "-w", "stop"])
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture()
def pg(monkeypatch):
    """A live cluster with the real schema applied and db.connection pointed at it."""
    cluster = _PgCluster().start()
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)

        import db.connection as connection
        if hasattr(connection, "_pool"):
            monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()

        from db.schema import init_db
        init_db()

        yield cluster
    finally:
        try:
            import db.connection as connection
            if getattr(connection, "_pool", None) is not None:
                connection._pool.closeall()
                connection._pool = None
        except Exception:  # noqa: BLE001
            pass
        cluster.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
_T1 = "2026-07-01T00:00:00+00:00"
_T2 = "2026-08-01T00:00:00+00:00"


def _state() -> dict:
    from db import deal_ledger_repository as repo

    res = repo.fetch_sync_state()
    assert res["available"], res
    return res.get("row") or {}


def _bootstrap(status="success", *, proved=True, watermark=_T1, **kw):
    from db import deal_ledger_repository as repo

    return repo.record_sync_state(sync_mode=repo.SYNC_MODE_BOOTSTRAP,
                                  status=status, proved_complete=proved,
                                  watermark=watermark, **kw)


def _incremental(status="success", *, watermark=_T2, **kw):
    from db import deal_ledger_repository as repo

    return repo.record_sync_state(sync_mode=repo.SYNC_MODE_INCREMENTAL,
                                  status=status, watermark=watermark, **kw)


def _ledger_row(deal_id="D1", *, won=True, amount=1000.0, revenue_usd=1000.0,
                gclid=None, close_date="2026-07-10T00:00:00+00:00"):
    return {
        "deal_id": deal_id, "deal_name": f"Deal {deal_id}",
        "pipeline_id": "default", "deal_stage_id": "326093516",
        "deal_stage_label": "Deal Won / Payment Received",
        "hs_is_closed": True, "hs_is_closed_won": won,
        "deal_created_at": "2026-05-01T00:00:00+00:00",
        "deal_close_date": close_date, "hubspot_lastmodified_at": _T1,
        "amount_raw": amount, "deal_currency_code": "USD",
        "amount_in_home_currency": amount, "home_currency_code": "USD",
        "revenue_usd": revenue_usd, "currency_status": "verified_usd",
        "currency_reason": "deal_currency_is_usd",
        "primary_contact_id": "C1", "association_count": 1,
        "association_status": "resolved",
        "association_reason": "single_associated_contact",
        "gclid": gclid, "campaign_name_raw": "Brand - UK", "keyword_raw": None,
        "country_raw": "AE", "source_primary_raw": "PAID_SEARCH",
        "source_detail_raw": "Brand - UK", "acquisition_group": "google_ads",
        "attribution_status": "attributed", "attribution_reason": "single_contact",
        "sync_batch_id": None,
        "source_fetched_at": "2026-08-16T00:00:00+00:00",
    }


def _assoc(contact_id="C1"):
    return {"contact_id": contact_id, "association_type_id": "4",
            "association_label": "Primary", "is_primary": True,
            "primary_selection_reason": "single_associated_contact",
            "gclid": None, "campaign_name_raw": "Brand - UK",
            "keyword_raw": None, "country_raw": "AE",
            "source_primary_raw": "PAID_SEARCH",
            "source_detail_raw": "Brand - UK",
            "acquisition_group": "google_ads"}


def _run_audit(*argv):
    """Invoke the real CLI in-process and return its exit code."""
    import importlib

    audit = importlib.import_module("scripts.audit_canonical_revenue_truth")
    importlib.reload(audit)
    old = sys.argv
    sys.argv = ["audit", *argv]
    try:
        return audit.main()
    finally:
        sys.argv = old


# ═════════════════════════════════════════════════════════════════════════════
# Bootstrap vs incremental write different columns
# ═════════════════════════════════════════════════════════════════════════════
def test_a_bootstrap_run_never_stamps_last_incremental_at(pg):
    """The load-bearing one. 153E-A stamped it on every run, so it could not
    answer the question the gate asks: did an incremental succeed AFTER the
    bootstrap?"""
    # INSERT path — the very first bootstrap creates the row.
    _bootstrap(status="partial", proved=False, watermark_is_checkpoint=True)
    assert _state()["last_incremental_at"] is None, (
        "the first bootstrap stamped last_incremental_at on insert")

    # UPDATE path — every later bootstrap pass hits ON CONFLICT, which is where
    # 153E-A unconditionally set NOW().
    _bootstrap()
    state = _state()

    assert state["bootstrap_status"] == "complete"
    assert state["bootstrap_started_at"] is not None
    assert state["bootstrap_completed_at"] is not None
    assert state["last_incremental_at"] is None, (
        "a bootstrap stamped last_incremental_at and can now masquerade as the "
        "post-bootstrap incremental")


def test_a_later_bootstrap_does_not_move_last_incremental_at(pg):
    """The dangerous direction. If a bootstrap bumped this column it would move
    FORWARD past its own completion, manufacturing the ordering the gate
    requires out of a run that was never an incremental at all."""
    _bootstrap()
    _incremental()
    stamped = _state()["last_incremental_at"]
    assert stamped is not None

    time.sleep(0.05)
    _bootstrap()

    assert _state()["last_incremental_at"] == stamped


def test_an_incremental_run_stamps_last_incremental_at(pg):
    _incremental()
    assert _state()["last_incremental_at"] is not None


def test_an_incremental_never_resets_a_completed_bootstrap(pg):
    _bootstrap()
    completed_at = _state()["bootstrap_completed_at"]

    _incremental()
    _incremental(status="partial", error="association_lookup_cap_reached")
    _incremental(status="failed", error="pull_failed")

    state = _state()
    assert state["bootstrap_status"] == "complete"
    assert state["bootstrap_completed_at"] == completed_at
    assert state["bootstrap_started_at"] is not None


def test_an_incremental_before_any_bootstrap_leaves_it_not_started(pg):
    """The 153E-A hole, at the storage layer: a successful incremental must not
    imply any historical coverage."""
    _incremental()
    state = _state()
    assert state["bootstrap_status"] == "not_started"
    assert state["bootstrap_started_at"] is None
    assert state["bootstrap_completed_at"] is None
    assert state["last_status"] == "success"
    assert state["last_sync_mode"] == "incremental"


# ═════════════════════════════════════════════════════════════════════════════
# Bootstrap timestamps
# ═════════════════════════════════════════════════════════════════════════════
def test_the_first_bootstrap_start_timestamp_survives_retries(pg):
    _bootstrap(status="partial", proved=False, watermark_is_checkpoint=True)
    first_start = _state()["bootstrap_started_at"]
    assert first_start is not None
    assert _state()["bootstrap_status"] == "in_progress"

    time.sleep(0.05)
    _bootstrap(status="partial", proved=False, watermark_is_checkpoint=True)
    time.sleep(0.05)
    _bootstrap()

    state = _state()
    assert state["bootstrap_started_at"] == first_start, (
        "a retry restarted the clock, so the elapsed bootstrap is unknowable")
    assert state["bootstrap_status"] == "complete"


def test_completion_is_written_only_after_proven_end_of_results(pg):
    """`status == success` is not enough. A capped run also 'succeeds' at what
    it attempted; only reaching the end of the result set proves coverage."""
    _bootstrap(status="success", proved=False)
    state = _state()
    assert state["bootstrap_status"] == "in_progress"
    assert state["bootstrap_completed_at"] is None

    _bootstrap(status="success", proved=True)
    assert _state()["bootstrap_status"] == "complete"
    assert _state()["bootstrap_completed_at"] is not None


def test_a_failed_run_cannot_mark_the_bootstrap_complete(pg):
    """Even claiming end-of-results: a failed run proved nothing."""
    _bootstrap(status="failed", proved=True, error="pull_failed: 503")
    state = _state()
    assert state["bootstrap_status"] == "in_progress"
    assert state["bootstrap_completed_at"] is None
    assert state["last_status"] == "failed"


def test_a_partial_bootstrap_keeps_in_progress_and_no_completion(pg):
    _bootstrap(status="partial", proved=False, watermark_is_checkpoint=True,
               error="association_lookup_cap_reached")
    state = _state()
    assert state["bootstrap_status"] == "in_progress"
    assert state["bootstrap_completed_at"] is None
    assert state["last_status"] == "partial"
    # The clean-prefix checkpoint still advanced, so the next pass resumes.
    assert state["last_modified_watermark"] is not None


def test_completion_is_ordered_after_the_start(pg):
    from datetime import datetime

    _bootstrap(status="partial", proved=False, watermark_is_checkpoint=True)
    time.sleep(0.05)
    _bootstrap()
    state = _state()

    started = datetime.fromisoformat(state["bootstrap_started_at"])
    completed = datetime.fromisoformat(state["bootstrap_completed_at"])
    assert completed >= started


def test_re_running_a_completed_bootstrap_keeps_the_first_completion(pg):
    """Re-proving the same coverage must not restamp completion — that would
    invalidate the incremental-after-bootstrap ordering and silently revoke a
    passing gate until the next daily sync."""
    _bootstrap()
    first_completion = _state()["bootstrap_completed_at"]

    _incremental()
    time.sleep(0.05)
    _bootstrap()

    state = _state()
    assert state["bootstrap_completed_at"] == first_completion
    assert state["bootstrap_status"] == "complete"
    # The gate still passes, because completion did not jump ahead of the
    # incremental that already ran.
    assert state["last_incremental_at"] is not None


def test_an_invalid_sync_mode_writes_nothing(pg):
    from db import deal_ledger_repository as repo

    out = repo.record_sync_state(status="success", sync_mode="sideways")
    assert out["available"] is False
    assert _state() == {}


# ═════════════════════════════════════════════════════════════════════════════
# The audit gate reads it all
# ═════════════════════════════════════════════════════════════════════════════
def _seed_reconciled_ledger():
    """One canonical won deal, present in the legacy source ledger."""
    from db import deal_ledger_repository as repo
    from db.connection import get_conn

    repo.upsert_deal(_ledger_row("D1"), associations=[_assoc("C1")])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO deal_source_attribution (deal_id, "
                " acquisition_group, attribution_status, deal_close_date, "
                " deal_amount_usd) VALUES ('D1', 'google_ads', 'attributed', "
                " '2026-07-10T00:00:00+00:00', 1000)")
        conn.commit()


def test_audit_fails_with_no_sync_state_at_all(pg):
    _seed_reconciled_ledger()
    assert _run_audit("--window", "all_time") == 1


def test_audit_fails_on_a_successful_incremental_with_no_bootstrap(pg):
    """End to end, the exact scenario 153E-A passed."""
    from services.revenue_reconciliation_service import (
        V_BOOTSTRAP_NOT_COMPLETE, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _incremental()

    report = build_revenue_reconciliation("all_time")
    # The reconciliation itself is spotless — that was never the problem.
    diff = next(d for d in report["legacy_diffs"]
                if d["ledger"] == "deal_source_attribution")
    assert diff["legacy_only"] == []
    assert diff["canonical_only"] == []
    # And the gate still refuses, on coverage.
    assert report["ok"] is False
    assert V_BOOTSTRAP_NOT_COMPLETE in report["violation_codes"]
    assert _run_audit("--window", "all_time") == 1


def test_audit_fails_on_a_bootstrap_with_no_incremental_after_it(pg):
    from services.revenue_reconciliation_service import (
        V_POST_BOOTSTRAP_INCREMENTAL_MISSING, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()

    report = build_revenue_reconciliation("all_time")
    assert report["ok"] is False
    assert V_POST_BOOTSTRAP_INCREMENTAL_MISSING in report["violation_codes"]
    assert _run_audit("--window", "all_time") == 1


def test_audit_passes_on_a_complete_bootstrap_plus_incremental(pg):
    """The one state that authorises PR-ADS-153E-B to proceed."""
    from services.revenue_reconciliation_service import (
        build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()

    report = build_revenue_reconciliation("all_time")
    assert report["violation_codes"] == [], report["violations"]
    assert report["ok"] is True
    assert _run_audit("--window", "all_time") == 0

    # Still shadow mode. This proves readiness; it authorises nothing.
    assert report["governance"]["shadow_mode"] is True
    assert report["governance"]["external_writes"] is False


def test_audit_fails_when_the_last_sync_was_partial(pg):
    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()
    assert _run_audit("--window", "all_time") == 0

    _incremental(status="partial", error="association_lookup_cap_reached")
    assert _run_audit("--window", "all_time") == 1


def test_audit_fails_when_success_is_recorded_with_an_error(pg):
    from services.revenue_reconciliation_service import (
        V_LAST_SYNC_SUCCESS_WITH_ERROR, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental(error="association_lookup_cap_reached")

    report = build_revenue_reconciliation("all_time")
    assert V_LAST_SYNC_SUCCESS_WITH_ERROR in report["violation_codes"]
    assert _run_audit("--window", "all_time") == 1


def test_audit_fails_when_stage_coverage_is_unreadable(pg, monkeypatch):
    """Fault-injected at the repository function rather than by breaking the
    schema: no column is unique to `fetch_stage_breakdown`, so any DDL that
    breaks it also breaks the summary and rows reads, and the run would fail for
    the wrong reason. Everything else here is the real database.
    """
    import db.deal_ledger_repository as repo
    from services.revenue_reconciliation_service import (
        V_STAGE_BREAKDOWN_UNAVAILABLE, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()
    assert _run_audit("--window", "all_time") == 0

    monkeypatch.setattr(repo, "fetch_stage_breakdown",
                        lambda: {"available": False,
                                 "reason": "database_unavailable", "rows": []})

    report = build_revenue_reconciliation("all_time")
    assert V_STAGE_BREAKDOWN_UNAVAILABLE in report["violation_codes"]
    # NULL, not [] — an unreadable breakdown must not render as "no deals yet".
    assert report["stage_breakdown"] is None
    assert report["stage_breakdown_available"] is False
    assert report["ok"] is False


def test_unreadable_legacy_tables_still_fail(pg):
    """A 153E-A protection this PR must not have weakened."""
    from db.connection import get_conn
    from services.revenue_reconciliation_service import (
        V_LEGACY_LEDGER_UNAVAILABLE, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()
    assert _run_audit("--window", "all_time") == 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE gclid_attribution CASCADE")
        conn.commit()

    report = build_revenue_reconciliation("all_time")
    assert V_LEGACY_LEDGER_UNAVAILABLE in report["violation_codes"]
    assert _run_audit("--window", "all_time") == 1


def test_unexplained_deal_differences_still_fail(pg):
    """Another preserved protection: a won deal absent from the deal-keyed
    legacy ledger has no structural excuse."""
    from db import deal_ledger_repository as repo
    from services.revenue_reconciliation_service import (
        V_UNEXPLAINED_DIFFERENCE, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    repo.upsert_deal(_ledger_row("D_ORPHAN"), associations=[_assoc("C2")])
    _bootstrap()
    _incremental()

    report = build_revenue_reconciliation("all_time")
    assert V_UNEXPLAINED_DIFFERENCE in report["violation_codes"]
    assert _run_audit("--window", "all_time") == 1


def test_expected_non_gclid_difference_remains_explained(pg):
    """And the designed-for difference is still not a failure."""
    from services.revenue_reconciliation_service import (
        REASON_NON_GCLID_EXCLUDED, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()

    report = build_revenue_reconciliation("all_time")
    diff = next(d for d in report["legacy_diffs"]
                if d["ledger"] == "gclid_attribution")
    item, = diff["canonical_only"]
    assert item["reason"] == REASON_NON_GCLID_EXCLUDED
    assert item["expected"] is True
    assert report["ok"] is True


# ═════════════════════════════════════════════════════════════════════════════
# The all-window gate, against real data
# ═════════════════════════════════════════════════════════════════════════════
def test_all_windows_passes_on_a_healthy_ledger(pg):
    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()
    assert _run_audit("--all-windows") == 0
    assert _run_audit("--all-windows", "--json") == 0


def test_all_windows_fails_when_coverage_is_incomplete(pg):
    _seed_reconciled_ledger()
    _incremental()      # no bootstrap
    assert _run_audit("--all-windows") == 1


def test_all_windows_json_reports_one_result_per_window(pg, capsys):
    from scripts.audit_canonical_revenue_truth import GATE_WINDOWS

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()

    assert _run_audit("--all-windows", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert set(payload["results"]) == set(GATE_WINDOWS)
    assert payload["failing_windows"] == []
    for window, result in payload["results"].items():
        assert result["window"] == window
        assert result["ok"] is True


def test_all_windows_and_window_are_mutually_exclusive(pg):
    assert _run_audit("--all-windows", "--window", "ytd") == 2


def test_all_window_output_carries_no_pii(pg, capsys):
    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()

    _run_audit("--all-windows", "--json")
    blob = capsys.readouterr().out.lower()
    for banned in ("email", "@", "firstname", "lastname"):
        assert banned not in blob, banned


# ═════════════════════════════════════════════════════════════════════════════
# The backfill CLI against a real database
# ═════════════════════════════════════════════════════════════════════════════
def test_backfill_cli_proves_completion_from_durable_state(pg, monkeypatch):
    """The CLI's success criterion is the DURABLE state, not a pass's opinion."""
    import services.hubspot_deal_sync_service as svc
    from scripts import backfill_canonical_deal_ledger as cli

    calls = {"n": 0}

    def _fake_backfill(*, restart=False, max_association_lookups=None):
        calls["n"] += 1
        if calls["n"] == 1:
            _bootstrap(status="partial", proved=False,
                       watermark_is_checkpoint=True,
                       error="association_lookup_cap_reached")
            return {"status": "partial", "complete": False,
                    "watermark_is_checkpoint": True, "deals_seen": 10,
                    "written": 10, "association_failures": 0,
                    "write_failures": 0, "pages": 1, "watermark": _T1,
                    "error": "association_lookup_cap_reached"}
        _bootstrap(status="success", proved=True, watermark=_T2)
        return {"status": "success", "complete": True,
                "watermark_is_checkpoint": False, "deals_seen": 5,
                "written": 5, "association_failures": 0, "write_failures": 0,
                "pages": 1, "watermark": _T2, "error": None}

    monkeypatch.setattr(svc, "backfill_deals", _fake_backfill)
    out = cli.run(max_passes=5, max_association_lookups=10, restart=False)

    assert out["ok"] is True
    assert out["passes_run"] == 2
    assert out["bootstrap_status"] == "complete"
    assert out["bootstrap_started_at"] is not None
    assert out["bootstrap_completed_at"] is not None
    # The bootstrap alone is not enough for the audit — an incremental is
    # still required, which is exactly what the runbook says to do next.
    _seed_reconciled_ledger()
    assert _run_audit("--window", "all_time") == 1
    _incremental()
    assert _run_audit("--window", "all_time") == 0


def test_backfill_cli_reports_failure_when_state_disagrees(pg, monkeypatch):
    import services.hubspot_deal_sync_service as svc
    from scripts import backfill_canonical_deal_ledger as cli

    def _lying_backfill(*, restart=False, max_association_lookups=None):
        # Claims completion but records nothing durable.
        return {"status": "success", "complete": True,
                "watermark_is_checkpoint": False, "deals_seen": 5,
                "written": 5, "association_failures": 0, "write_failures": 0,
                "pages": 1, "watermark": _T2, "error": None}

    monkeypatch.setattr(svc, "backfill_deals", _lying_backfill)
    out = cli.run(max_passes=3, max_association_lookups=10, restart=False)

    assert out["ok"] is False
    assert "durable sync state" in out["reason"]


# ═════════════════════════════════════════════════════════════════════════════
# `last_sync_mode` — which mode wrote `last_status`
# ═════════════════════════════════════════════════════════════════════════════
def test_the_mode_is_recorded_on_every_write(pg):
    _bootstrap(status="partial", proved=False, watermark_is_checkpoint=True)
    assert _state()["last_sync_mode"] == "bootstrap"

    _incremental(status="failed", error="pull_failed")
    assert _state()["last_sync_mode"] == "incremental"

    _bootstrap()
    assert _state()["last_sync_mode"] == "bootstrap"


def test_a_bootstrap_rerun_cannot_masquerade_as_post_bootstrap_proof(pg):
    """The reachable sequence that passed before `last_sync_mode` existed.

    Bootstrap completes at T0. The incremental FAILS at T1 — which still stamps
    `last_incremental_at`, because the attempt happened. A bootstrap then reruns
    successfully at T2: it preserves T0 and T1 and overwrites `last_status` with
    `success`. The audit saw T1 > T0 and a success, and passed a history with no
    successful incremental after the bootstrap anywhere in it.
    """
    from services.revenue_reconciliation_service import (
        V_LAST_SYNC_NOT_INCREMENTAL, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()

    _bootstrap()                                            # T0
    completed_at = _state()["bootstrap_completed_at"]
    time.sleep(0.05)
    _incremental(status="failed", error="pull_failed: 503")  # T1, FAILED
    incremental_at = _state()["last_incremental_at"]
    time.sleep(0.05)
    _bootstrap()                                            # T2, succeeds

    state = _state()
    # Every precondition the OLD gate checked is now satisfied...
    assert state["bootstrap_status"] == "complete"
    assert state["bootstrap_completed_at"] == completed_at
    assert state["last_incremental_at"] == incremental_at
    assert incremental_at > completed_at
    assert state["last_status"] == "success"
    # ...and the mode is what gives it away.
    assert state["last_sync_mode"] == "bootstrap"

    report = build_revenue_reconciliation("all_time")
    assert report["ok"] is False
    assert V_LAST_SYNC_NOT_INCREMENTAL in report["violation_codes"]
    assert _run_audit("--window", "all_time") == 1


def test_the_sequence_passes_only_after_another_successful_incremental(pg):
    """The remedy, proven: run one more incremental and the gate opens."""
    _seed_reconciled_ledger()
    _bootstrap()
    _incremental(status="failed", error="pull_failed: 503")
    _bootstrap()
    assert _run_audit("--window", "all_time") == 1

    _incremental()
    state = _state()
    assert state["last_sync_mode"] == "incremental"
    assert state["last_status"] == "success"
    assert _run_audit("--window", "all_time") == 0
    assert _run_audit("--all-windows") == 0


def test_a_partial_incremental_fails(pg):
    _seed_reconciled_ledger()
    _bootstrap()
    _incremental(status="partial", error="association_lookup_cap_reached")
    assert _state()["last_sync_mode"] == "incremental"
    assert _run_audit("--window", "all_time") == 1


def test_an_incremental_with_success_and_an_error_fails(pg):
    from services.revenue_reconciliation_service import (
        V_LAST_SYNC_SUCCESS_WITH_ERROR, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental(error="association_lookup_cap_reached")

    report = build_revenue_reconciliation("all_time")
    assert V_LAST_SYNC_SUCCESS_WITH_ERROR in report["violation_codes"]
    assert _run_audit("--window", "all_time") == 1


def test_a_null_sync_mode_on_an_existing_row_fails_closed(pg):
    """Rows written before the column existed. NULL is not permission — the
    gate stays shut until a real sync records the mode."""
    from db.connection import get_conn
    from services.revenue_reconciliation_service import (
        V_LAST_SYNC_NOT_INCREMENTAL, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()
    assert _run_audit("--window", "all_time") == 0

    # Simulate a pre-migration row.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE hubspot_deal_sync_state "
                        "SET last_sync_mode = NULL")
        conn.commit()

    assert _state()["last_sync_mode"] is None
    report = build_revenue_reconciliation("all_time")
    assert V_LAST_SYNC_NOT_INCREMENTAL in report["violation_codes"]
    assert _run_audit("--window", "all_time") == 1

    # And one real incremental re-opens it.
    _incremental()
    assert _run_audit("--window", "all_time") == 0


def test_an_unknown_sync_mode_value_fails(pg):
    from db.connection import get_conn
    from services.revenue_reconciliation_service import (
        V_LAST_SYNC_NOT_INCREMENTAL, build_revenue_reconciliation,
    )

    _seed_reconciled_ledger()
    _bootstrap()
    _incremental()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE hubspot_deal_sync_state "
                        "SET last_sync_mode = 'sideways'")
        conn.commit()

    report = build_revenue_reconciliation("all_time")
    assert V_LAST_SYNC_NOT_INCREMENTAL in report["violation_codes"]


def test_the_migration_adds_the_column_to_an_existing_table(pg):
    """An existing database predating the column: the additive ALTER must add
    it, leave the row's data intact, and leave the value NULL."""
    from db.connection import get_conn
    from db.schema import init_db

    _bootstrap()
    _incremental()
    before = _state()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE hubspot_deal_sync_state "
                        "DROP COLUMN last_sync_mode")
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'hubspot_deal_sync_state'")
            cols = {r[0] for r in cur.fetchall()}
        conn.commit()
    assert "last_sync_mode" not in cols

    init_db()   # idempotent; runs the ADD COLUMN IF NOT EXISTS migration

    after = _state()
    assert after["last_sync_mode"] is None, "the migration invented a value"
    # Nothing else was disturbed.
    for key in ("bootstrap_status", "bootstrap_started_at",
                "bootstrap_completed_at", "last_incremental_at", "last_status"):
        assert after[key] == before[key], key

    # Running it twice more is still a no-op.
    init_db()
    init_db()
    assert _state()["last_sync_mode"] is None
