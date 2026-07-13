"""
tests/test_pr_ads_146a_keyword_automation.py

PR-ADS-146A — Keyword Evidence Production Automation & UI Completion.

Covers the automation contract so operators never need a Render Shell:

  §9  google_ads_api is a valid sync source (no false unknown-source warning);
      a keyword-facts backfill creates + finishes a batch and writes sync_state.
  §3  ONE shared sync_keyword_daily_facts used by every scheduler; immutable
      upsert; fetched-but-zero-written fails; skipped-identity fails; a verified
      empty range still advances the watermark and reports ok.
  §1  Resumable full-history bootstrap in monthly chunks — fail-closed start
      date, resume without repeating a succeeded chunk, partial on a failed
      chunk, complete only when every chunk succeeds, durable state persisted.
  §2  Auto bootstrap on deploy — started / already_running / not_needed.
  §6  keyword_history_status All-time completeness metadata.
  Governance — strictly read-only vs Google Ads (pull only, no mutation words).

Run with:
    python -m pytest tests/test_pr_ads_146a_keyword_automation.py -v
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.writers as writers  # noqa: E402
from services import keyword_sync_service as kss  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _kw_rows(n: int, *, currency: str | None = "GBP") -> list[dict]:
    return [{
        "source_date": "2026-06-01", "customer_id": "123", "campaign_id": f"c{i}",
        "ad_group_id": f"a{i}", "criterion_id": f"k{i}", "currency_code": currency,
        "cost_micros": 1_000_000, "clicks": 3,
    } for i in range(n)]


def _stats(fetched=3, prepared=3, written=3, skip_id=0, skip_date=0, db_unavailable=False):
    return {
        "fetched": fetched, "prepared": prepared, "written": written,
        "skipped_missing_identity": skip_id, "skipped_no_date": skip_date,
        "db_unavailable": db_unavailable,
    }


@pytest.fixture
def sync_env():
    """Patch the three collaborators sync_keyword_daily_facts imports at runtime:
    start/finish sync batch, the keyword writer, and the direct GAQL pull."""
    with patch.object(writers, "start_sync_batch", return_value=77) as start_b, \
         patch.object(writers, "finish_sync_batch", return_value=True) as finish_b, \
         patch.object(writers, "write_keyword_daily_facts", return_value=_stats()) as write_f, \
         patch("connectors.google_ads_source.pull_keyword_performance_range",
               return_value=_kw_rows(3)) as pull:
        yield {"start": start_b, "finish": finish_b, "write": write_f, "pull": pull}


# ─────────────────────────────────────────────────────────────────────────────
# §9 — valid sync source + batch lifecycle
# ─────────────────────────────────────────────────────────────────────────────
def test_google_ads_api_is_a_valid_sync_source():
    assert "google_ads_api" in writers.VALID_SYNC_SOURCES
    assert "keyword_facts" in writers.VALID_SYNC_DATASETS
    assert "backfill" in writers.VALID_SYNC_TYPES


def test_backfill_source_emits_no_unknown_source_warning(caplog):
    """A keyword-facts backfill must not log the false unknown-source warning."""
    with patch.object(writers, "get_conn", return_value=MagicMock()):
        with caplog.at_level("WARNING"):
            writers.start_sync_batch(
                source="google_ads_api", dataset="keyword_facts",
                sync_type="backfill", date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    assert "unknown source" not in caplog.text


def test_sync_creates_and_finishes_keyword_facts_batch(sync_env):
    res = kss.sync_keyword_daily_facts(date(2026, 6, 1), date(2026, 6, 30), "backfill")
    assert res["ok"] is True
    sync_env["start"].assert_called_once()
    src = sync_env["start"].call_args.kwargs.get("source")
    ds = sync_env["start"].call_args.kwargs.get("dataset")
    assert (src, ds) == ("google_ads_api", "keyword_facts")
    # Finished as success with a watermark advanced to date_to.
    fk = sync_env["finish"].call_args.kwargs
    assert fk["status"] == "success"
    assert fk["last_source_date"] == date(2026, 6, 30)


# ─────────────────────────────────────────────────────────────────────────────
# §3 — shared sync semantics
# ─────────────────────────────────────────────────────────────────────────────
def test_fetched_but_zero_written_marks_failed(sync_env):
    sync_env["write"].return_value = _stats(fetched=5, prepared=5, written=0)
    res = kss.sync_keyword_daily_facts(date(2026, 6, 1), date(2026, 6, 30), "daily")
    assert res["ok"] is False
    assert sync_env["finish"].call_args.kwargs["status"] == "failed"
    # A partial-persistence failure must carry a human-readable reason so the
    # admin refresh UI never falls back to "Unknown error".
    assert res["error"] and "wrote 0" in res["error"]


def test_skipped_identity_marks_failed(sync_env):
    sync_env["write"].return_value = _stats(fetched=3, prepared=2, written=2, skip_id=1)
    res = kss.sync_keyword_daily_facts(date(2026, 6, 1), date(2026, 6, 30), "daily")
    assert res["ok"] is False
    assert res["error"] and "rejected" in res["error"]


def test_successful_sync_has_no_error_string(sync_env):
    res = kss.sync_keyword_daily_facts(date(2026, 6, 1), date(2026, 6, 30), "daily")
    assert res["ok"] is True
    assert res["error"] is None


def test_verified_empty_range_is_ok_and_advances_watermark(sync_env):
    """Source returns nothing → nothing written → still a clean success, and the
    watermark advances so the window is provably synced (§8 verified-empty)."""
    sync_env["pull"].return_value = []
    sync_env["write"].return_value = _stats(fetched=0, prepared=0, written=0)
    res = kss.sync_keyword_daily_facts(date(2026, 6, 1), date(2026, 6, 30), "daily")
    assert res["ok"] is True
    assert sync_env["finish"].call_args.kwargs["last_source_date"] == date(2026, 6, 30)


def test_pull_failure_marks_failed_without_writing(sync_env):
    sync_env["pull"].side_effect = RuntimeError("google ads timeout")
    res = kss.sync_keyword_daily_facts(date(2026, 6, 1), date(2026, 6, 30), "daily")
    assert res["ok"] is False
    sync_env["write"].assert_not_called()
    assert sync_env["finish"].call_args.kwargs["status"] == "failed"


def test_recent_incremental_uses_rolling_account_local_window(sync_env):
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    with patch.object(kss, "_account_today", return_value=date(2026, 7, 13)):
        kss.sync_recent_keyword_facts("daily", days=30, now=now)
    a, b = sync_env["pull"].call_args.args
    assert b == "2026-07-13"
    assert a == "2026-06-14"   # today + previous 29 days inclusive


@pytest.mark.parametrize("module_name", ["scheduler.daily", "scheduler.weekly", "scheduler.monthly"])
def test_all_schedulers_route_through_shared_sync(module_name):
    """§3 — no competing keyword-fact sync paths: every scheduler imports the
    shared service, never a bespoke write_keyword_daily_facts block."""
    src = Path(module_name.replace(".", "/") + ".py").read_text()
    assert "keyword_sync_service" in src
    assert "sync_keyword_daily_facts" in src or "sync_recent_keyword_facts" in src


# ─────────────────────────────────────────────────────────────────────────────
# §1 — start-date resolution (fail closed)
# ─────────────────────────────────────────────────────────────────────────────
def test_start_date_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_HISTORY_START_DATE", "2023-05-10")
    start, label = kss.resolve_history_start_date()
    assert start == date(2023, 5, 10)
    assert "configured" in label


def test_start_date_falls_back_to_canonical_spend(monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_HISTORY_START_DATE", raising=False)
    with patch.object(kss, "_earliest_canonical_spend_date", return_value=date(2024, 1, 15)):
        start, label = kss.resolve_history_start_date()
    assert start == date(2024, 1, 15)
    assert "canonical" in label


def test_start_date_fails_closed_when_unknown(monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_HISTORY_START_DATE", raising=False)
    with patch.object(kss, "_earliest_canonical_spend_date", return_value=None):
        with pytest.raises(kss.KeywordBootstrapError):
            kss.resolve_history_start_date()


def test_stable_month_keys_and_required_excludes_current_month():
    # §4 — closed months carry a stable YYYY-MM key; the current open month is
    # excluded from historical completeness (the daily incremental proves it).
    assert kss._required_month_keys(date(2026, 5, 1), date(2026, 7, 13)) == ["2026-05", "2026-06"]
    chunks = kss._bootstrap_chunks(date(2026, 5, 10), date(2026, 7, 13))
    assert chunks[0] == ("2026-05", date(2026, 5, 10), date(2026, 5, 31))
    assert chunks[1] == ("2026-06", date(2026, 6, 1), date(2026, 6, 30))
    assert chunks[-1] == ("2026-07", date(2026, 7, 1), date(2026, 7, 13))


# ─────────────────────────────────────────────────────────────────────────────
# §1/§2 — DB-backed lease: fake store modelling the durable claim + heartbeat
# ─────────────────────────────────────────────────────────────────────────────
class FakeLeaseStore:
    """In-memory model of the durable lease writers, honouring the same contract
    the partial unique index enforces in Postgres: at most one LIVE lease."""

    def __init__(self, initial=None):
        self.job = dict(initial) if initial else None
        self.released = None

    def acquire(self, job_type, token, ttl, *, date_from, date_to, chunk_months=1,
                seed_completed_chunks=None, stale_takeover=True):
        j = self.job
        if j and j.get("status") == "running" and j.get("_live"):
            return {"claimed": False, "reason": "active_lease", "job": j}
        if j and j.get("status") in ("running", "partial", "failed", "queued"):
            j = dict(j)
            j.update(status="running", lease_token=token, _live=True)
            j.setdefault("errors", [])
            self.job = j
            return {"claimed": True, "reason": "resumed", "job": j}
        j = {"job_id": f"kwbs_{date_to}_{token[:8]}", "status": "running",
             "lease_token": token, "_live": True,
             "completed_chunks": list(seed_completed_chunks or []), "errors": []}
        self.job = j
        return {"claimed": True, "reason": "created", "job": j}

    def renew(self, job_id, token, ttl, *, completed_chunks=None, current_chunk=None, errors=None):
        if not self.job or self.job.get("lease_token") != token:
            return False
        if completed_chunks is not None:
            self.job["completed_chunks"] = list(completed_chunks)
        if errors is not None:
            self.job["errors"] = list(errors)
        return True

    def release(self, job_id, token, *, status, summary=None, completed_chunks=None, finished_at=None):
        if not self.job or self.job.get("lease_token") != token:
            return False
        self.job["status"] = status
        self.job["_live"] = False
        if completed_chunks is not None:
            self.job["completed_chunks"] = list(completed_chunks)
        if summary is not None:
            self.job["summary"] = summary
        self.released = status
        return True


def _patch_lease(store, latest=None):
    return patch.multiple(
        writers,
        get_latest_recovery_job=MagicMock(return_value=latest),
        acquire_recovery_lease=MagicMock(side_effect=store.acquire),
        renew_recovery_lease=MagicMock(side_effect=store.renew),
        release_recovery_lease=MagicMock(side_effect=store.release),
    )


def _ok_sync(*_a, **_k):
    return {"ok": True, "fetched": 3, "written": 3,
            "skipped_missing_identity": 0, "currency_incomplete_rows": 0}


# ─────────────────────────────────────────────────────────────────────────────
# §1 — bootstrap resumability / completeness / atomic claim
# ─────────────────────────────────────────────────────────────────────────────
def test_only_one_worker_acquires_the_bootstrap_lease():
    # §1 — the atomic claim contract: a second worker never launches while a live
    # lease exists (Postgres enforces this via uq_recovery_running_keyword_bootstrap).
    store = FakeLeaseStore()
    a = store.acquire("keyword_bootstrap", "tokenA", 900,
                      date_from=date(2026, 5, 1), date_to=date(2026, 7, 13))
    b = store.acquire("keyword_bootstrap", "tokenB", 900,
                      date_from=date(2026, 5, 1), date_to=date(2026, 7, 13))
    assert a["claimed"] is True
    assert b["claimed"] is False and b["reason"] == "active_lease"


def test_bootstrap_yields_to_an_active_lease():
    with patch.object(writers, "get_latest_recovery_job", return_value={"status": "running", "job_id": "owner"}), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(writers, "acquire_recovery_lease",
                      return_value={"claimed": False, "reason": "active_lease", "job": {"job_id": "owner"}}), \
         patch.object(kss, "sync_keyword_daily_facts") as sync:
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "running" and out["reason"] == "already_running"
    sync.assert_not_called()


def test_bootstrap_completes_only_when_every_chunk_succeeds():
    store = FakeLeaseStore()
    with _patch_lease(store), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=_ok_sync):
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "success"
    assert out["summary"]["total_chunks"] == 3           # May, Jun, Jul
    assert out["summary"]["completed_chunks"] == 3
    assert out["summary"]["required_chunks"] == 2         # May, Jun (Jul is current)
    assert out["summary"]["failed_chunk"] is None
    assert store.released == "success"


def test_bootstrap_is_partial_when_a_chunk_fails():
    def flaky(cs, ce, sync_type):
        ok = kss._month_key(cs) != "2026-06"   # June fails
        return {"ok": ok, "fetched": 3, "written": 3 if ok else 0,
                "skipped_missing_identity": 0, "currency_incomplete_rows": 0,
                "error": None if ok else "boom"}

    store = FakeLeaseStore()
    with _patch_lease(store), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=flaky):
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "partial"
    assert out["summary"]["failed_chunk"] == "2026-06"


def test_bootstrap_resumes_without_repeating_completed_chunks():
    existing = {"job_id": "kwbs_resume", "status": "partial",
                "date_from": "2026-05-01", "date_to": "2026-07-13",
                "completed_chunks": ["2026-05", "2026-06"]}
    seen: list = []

    def record(cs, ce, sync_type):
        seen.append(kss._month_key(cs))
        return _ok_sync()

    store = FakeLeaseStore(initial=existing)
    with _patch_lease(store, latest=existing), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=record):
        out = kss.run_keyword_bootstrap()
    assert seen == ["2026-07"]           # only the un-completed current month
    assert out["status"] == "success"


def test_bootstrap_does_not_restart_a_completed_covering_job():
    required = kss._required_month_keys(date(2026, 5, 1), date(2026, 7, 13))
    existing = {"job_id": "done", "status": "success",
                "completed_chunks": required + ["2026-07"]}
    with patch.object(writers, "get_latest_recovery_job", return_value=existing), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(writers, "acquire_recovery_lease") as acq, \
         patch.object(kss, "sync_keyword_daily_facts") as sync:
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "success" and out["reason"] == "already_complete"
    acq.assert_not_called()
    sync.assert_not_called()


def test_bootstrap_fails_closed_when_start_date_unresolved():
    with patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "resolve_history_start_date",
                      side_effect=kss.KeywordBootstrapError("no start")):
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "failed"
    assert out["reason"] == "start_date_unresolved"


# §5 — never run a resumable bootstrap without a durable checkpoint.
def test_bootstrap_fails_closed_when_durable_job_unavailable():
    with patch.object(writers, "get_latest_recovery_job", return_value=None), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(writers, "acquire_recovery_lease",
                      return_value={"claimed": False, "reason": "db_unavailable", "job": None}), \
         patch.object(kss, "sync_keyword_daily_facts") as sync:
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "failed"
    assert out["reason"] == "durable_job_unavailable"
    sync.assert_not_called()   # aborted BEFORE any Google Ads pull


def test_bootstrap_aborts_when_checkpoint_cannot_persist():
    # §5 — a failed checkpoint renew stops the run rather than continue undurably.
    store = FakeLeaseStore()

    def failing_renew(*_a, **_k):
        return False

    with patch.multiple(writers,
                        get_latest_recovery_job=MagicMock(return_value=None),
                        acquire_recovery_lease=MagicMock(side_effect=store.acquire),
                        renew_recovery_lease=MagicMock(side_effect=failing_renew),
                        release_recovery_lease=MagicMock(side_effect=store.release)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts") as sync:
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "interrupted"
    assert out["reason"] == "lease_lost"
    sync.assert_not_called()   # lost the lease before the first pull


# §2 — recover a stale running job (crashed worker) and resume its ledger.
def test_stale_running_job_is_recovered_and_resumes():
    existing = {"job_id": "stale", "status": "running", "_live": False,
                "completed_chunks": ["2026-05"], "errors": []}
    seen: list = []

    def record(cs, ce, sync_type):
        seen.append(kss._month_key(cs))
        return _ok_sync()

    store = FakeLeaseStore(initial=existing)
    with _patch_lease(store, latest=existing), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=record):
        out = kss.run_keyword_bootstrap()
    assert "2026-05" not in seen               # already completed, not repeated
    assert set(seen) == {"2026-06", "2026-07"}  # resumes the missing chunks
    assert out["status"] == "success"


# §3 — the spec's Jan/Feb/Mar scenario end-to-end.
def test_february_gap_then_restart_resumes_february():
    store = FakeLeaseStore()

    def sync1(cs, ce, sync_type):
        ok = kss._month_key(cs) != "2026-02"
        return {"ok": ok, "fetched": 2, "written": 2 if ok else 0,
                "skipped_missing_identity": 0, "currency_incomplete_rows": 0,
                "error": None if ok else "feb boom"}

    common = dict(resolve=(date(2026, 1, 1), "env"), today=date(2026, 4, 15))
    with _patch_lease(store), \
         patch.object(kss, "resolve_history_start_date", return_value=common["resolve"]), \
         patch.object(kss, "_account_today", return_value=common["today"]), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=sync1):
        out1 = kss.run_keyword_bootstrap()
    assert out1["status"] == "partial"
    assert "2026-02" not in store.job["completed_chunks"]   # Feb failed
    assert {"2026-01", "2026-03"}.issubset(store.job["completed_chunks"])

    order: list = []

    def sync2(cs, ce, sync_type):
        order.append(kss._month_key(cs))
        return _ok_sync()

    with _patch_lease(store, latest=store.job), \
         patch.object(kss, "resolve_history_start_date", return_value=common["resolve"]), \
         patch.object(kss, "_account_today", return_value=common["today"]), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=sync2):
        out2 = kss.run_keyword_bootstrap()
    assert order == ["2026-02"]              # ONLY February re-synced
    assert out2["status"] == "success"


# ─────────────────────────────────────────────────────────────────────────────
# §2/§3 — auto bootstrap on deploy + gap-aware need
# ─────────────────────────────────────────────────────────────────────────────
def test_deploy_bootstrap_not_needed_when_coverage_complete():
    with patch.object(kss, "_bootstrap_needed", return_value=False):
        assert kss.maybe_start_bootstrap_on_deploy() == "not_needed"


def test_deploy_bootstrap_starts_when_needed(monkeypatch):
    started = {"v": False}

    class _T:
        def __init__(self, *a, **k):
            pass

        def start(self):
            started["v"] = True

    monkeypatch.setattr(kss.threading, "Thread", _T)
    kss._bootstrap_running = False
    with patch.object(kss, "_bootstrap_needed", return_value=True):
        out = kss.maybe_start_bootstrap_on_deploy()
    assert out == "started"
    assert started["v"] is True


def test_bootstrap_needed_true_when_table_empty():
    with patch.object(kss, "_durable_coverage", return_value=(None, None, 0)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 1, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(writers, "get_latest_recovery_job", return_value=None):
        assert kss._bootstrap_needed() is True


def test_bootstrap_needed_true_when_ledger_has_gap_despite_correct_min():
    # §3 — a successful first month must not hide a failed later month even when
    # MIN(source_date) already equals the expected start.
    job = {"status": "partial", "completed_chunks": ["2026-01", "2026-03"]}   # Feb missing
    with patch.object(kss, "_durable_coverage", return_value=(date(2026, 1, 1), date(2026, 4, 10), 50)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 1, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 4, 15)), \
         patch.object(writers, "get_latest_recovery_job", return_value=job):
        assert kss._bootstrap_needed() is True


def test_bootstrap_needed_false_when_required_months_complete():
    job = {"status": "partial", "completed_chunks": ["2026-01", "2026-02", "2026-03"]}
    with patch.object(kss, "_durable_coverage", return_value=(date(2026, 1, 1), date(2026, 4, 10), 99)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 1, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 4, 15)), \
         patch.object(writers, "get_latest_recovery_job", return_value=job):
        assert kss._bootstrap_needed() is False


def test_bootstrap_needed_true_for_stale_running_job():
    stale = {"status": "running", "lease_expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}
    with patch.object(kss, "_durable_coverage", return_value=(date(2026, 1, 1), date(2026, 7, 13), 9)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 1, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(writers, "get_latest_recovery_job", return_value=stale):
        assert kss._bootstrap_needed() is True


def test_bootstrap_not_needed_for_live_running_job():
    live = {"status": "running",
            "lease_expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}
    with patch.object(kss, "_durable_coverage", return_value=(date(2026, 1, 1), date(2026, 7, 13), 9)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 1, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(writers, "get_latest_recovery_job", return_value=live):
        assert kss._bootstrap_needed() is False


# ─────────────────────────────────────────────────────────────────────────────
# §4/§6 — All-time completeness metadata
# ─────────────────────────────────────────────────────────────────────────────
def test_history_complete_when_required_done_and_recent_current():
    required = kss._required_month_keys(date(2026, 5, 1), date(2026, 7, 13))
    job = {"status": "success", "completed_chunks": required + ["2026-07"]}
    with patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_durable_coverage", return_value=(date(2026, 5, 1), date(2026, 7, 13), 500)), \
         patch.object(kss, "_latest_successful_sync_date", return_value=date(2026, 7, 13)), \
         patch.object(writers, "get_latest_recovery_job", return_value=job):
        out = kss.keyword_history_status()
    assert out["history_complete"] is True
    assert out["missing_date_ranges"] == []
    assert out["latest_successful_sync_date"] == "2026-07-13"


def test_history_complete_stays_true_on_next_calendar_day():
    # §4 — bootstrap complete through the last closed month + a fresh daily sync
    # keeps All-time complete the next day; the current month is never "missing".
    required = kss._required_month_keys(date(2026, 1, 1), date(2026, 7, 14))
    job = {"status": "success", "completed_chunks": required + ["2026-07"]}
    with patch.object(kss, "_account_today", return_value=date(2026, 7, 14)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 1, 1), "env")), \
         patch.object(kss, "_durable_coverage", return_value=(date(2026, 1, 1), date(2026, 7, 13), 9000)), \
         patch.object(kss, "_latest_successful_sync_date", return_value=date(2026, 7, 14)), \
         patch.object(writers, "get_latest_recovery_job", return_value=job):
        out = kss.keyword_history_status()
    assert out["history_complete"] is True
    assert out["missing_date_ranges"] == []


def test_history_incomplete_when_required_month_missing():
    with patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2024, 1, 1), "env")), \
         patch.object(kss, "_durable_coverage", return_value=(date(2026, 1, 15), date(2026, 7, 13), 500)), \
         patch.object(kss, "_latest_successful_sync_date", return_value=date(2026, 7, 13)), \
         patch.object(writers, "get_latest_recovery_job", return_value=None):
        out = kss.keyword_history_status()
    assert out["history_complete"] is False
    assert out["missing_date_ranges"]     # 2024/2025 months are missing


# ─────────────────────────────────────────────────────────────────────────────
# §6 — selected-window coverage proof
# ─────────────────────────────────────────────────────────────────────────────
def test_recent_window_not_synced_is_not_fully_synced():
    # No successful batch covers the recent 7-day window -> not proven synced.
    with patch.object(kss, "_keyword_success_intervals", return_value=[]), \
         patch.object(kss, "_latest_successful_sync_date", return_value=date(2026, 7, 6)):
        cov = kss.selected_window_coverage(date(2026, 7, 7), date(2026, 7, 13), is_all_time=False)
    assert cov["selected_window_fully_synced"] is False
    assert cov["selected_window_coverage_status"] == "not_synced"
    assert cov["missing_window_ranges"]


def test_recent_window_fully_synced_allows_zero_complete():
    # A successful batch covers the whole window though it has no rows -> verified empty.
    with patch.object(kss, "_keyword_success_intervals",
                      return_value=[(date(2026, 6, 14), date(2026, 7, 13))]), \
         patch.object(kss, "_latest_successful_sync_date", return_value=date(2026, 7, 13)):
        cov = kss.selected_window_coverage(date(2026, 7, 7), date(2026, 7, 13), is_all_time=False)
    assert cov["selected_window_fully_synced"] is True
    assert cov["missing_window_ranges"] == []
    assert cov["successful_coverage_ranges"] == [{"start": "2026-07-07", "end": "2026-07-13"}]


def test_never_synced_window_is_not_synced():
    with patch.object(kss, "_durable_coverage", return_value=(None, None, 0)), \
         patch.object(kss, "_latest_successful_sync_date", return_value=None):
        cov = kss.selected_window_coverage(date(2026, 7, 7), date(2026, 7, 13), is_all_time=False)
    assert cov["selected_window_fully_synced"] is False
    assert cov["selected_window_coverage_status"] == "not_synced"


def test_all_time_coverage_defers_to_history_complete():
    with patch.object(kss, "keyword_history_status",
                      return_value={"history_complete": True, "history_start_expected": "2026-01-01",
                                    "missing_date_ranges": []}), \
         patch.object(kss, "_durable_coverage", return_value=(date(2026, 1, 1), date(2026, 7, 13), 9000)), \
         patch.object(kss, "_latest_successful_sync_date", return_value=date(2026, 7, 13)):
        cov = kss.selected_window_coverage(None, date(2026, 7, 13), is_all_time=True)
    assert cov["selected_window_fully_synced"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Governance — strictly read-only vs Google Ads
# ─────────────────────────────────────────────────────────────────────────────
def test_service_source_has_no_google_ads_mutation_calls():
    src = Path("services/keyword_sync_service.py").read_text().lower()
    for banned in ("mutate", "create_negative", "add_keyword", "pause_keyword",
                   "update_bid", "upload_conversion"):
        assert banned not in src, f"unexpected mutation reference: {banned}"
    # The legacy snapshot table must never be touched by the automation path.
    assert "delete from keywords" not in src


def test_refresh_endpoint_declares_read_only_external():
    src = Path("api/server.py").read_text()
    assert '"read_only_external": True' in src or "'read_only_external': True" in src
