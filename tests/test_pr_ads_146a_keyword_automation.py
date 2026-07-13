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
from datetime import date, datetime, timezone
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


def test_month_chunks_cover_range_inclusive():
    chunks = kss._month_chunks(date(2026, 1, 10), date(2026, 3, 5))
    assert chunks[0] == (date(2026, 1, 10), date(2026, 1, 31))
    assert chunks[1] == (date(2026, 2, 1), date(2026, 2, 28))
    assert chunks[-1] == (date(2026, 3, 1), date(2026, 3, 5))


# ─────────────────────────────────────────────────────────────────────────────
# §1 — bootstrap resumability / completeness
# ─────────────────────────────────────────────────────────────────────────────
def _bootstrap_writers():
    """Patch the durable-job + sync collaborators the bootstrap uses."""
    return patch.multiple(
        writers,
        get_latest_recovery_job=MagicMock(return_value=None),
        create_recovery_job=MagicMock(return_value=True),
        update_recovery_job=MagicMock(return_value=None),
    )


def test_bootstrap_completes_only_when_every_chunk_succeeds():
    with _bootstrap_writers(), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts",
                      return_value={"ok": True, "fetched": 3, "written": 3,
                                    "skipped_missing_identity": 0, "currency_incomplete_rows": 0}):
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "success"
    assert out["summary"]["total_chunks"] == 3           # May, Jun, Jul
    assert out["summary"]["completed_chunks"] == 3
    assert out["summary"]["failed_chunk"] is None


def test_bootstrap_is_partial_when_a_chunk_fails():
    calls = {"n": 0}

    def flaky(a, b, sync_type):
        calls["n"] += 1
        ok = calls["n"] != 2   # second chunk fails
        return {"ok": ok, "fetched": 3, "written": 3 if ok else 0,
                "skipped_missing_identity": 0, "currency_incomplete_rows": 0,
                "error": None if ok else "boom"}

    with _bootstrap_writers(), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=flaky):
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "partial"
    assert out["summary"]["failed_chunk"] is not None


def test_bootstrap_resumes_without_repeating_completed_chunks():
    existing = {
        "job_id": "kwbs_resume", "status": "partial",
        "date_from": "2026-05-01", "date_to": "2026-07-13",
        "completed_chunks": ["2026-05-01/2026-05-31", "2026-06-01/2026-06-30"],
    }
    seen: list = []

    def record(a, b, sync_type):
        seen.append((a.isoformat(), b.isoformat()))
        return {"ok": True, "fetched": 1, "written": 1,
                "skipped_missing_identity": 0, "currency_incomplete_rows": 0}

    with patch.multiple(writers,
                        get_latest_recovery_job=MagicMock(return_value=existing),
                        create_recovery_job=MagicMock(return_value=True),
                        update_recovery_job=MagicMock(return_value=None)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts", side_effect=record):
        out = kss.run_keyword_bootstrap()
    # Only the un-completed July chunk is (re)synced; May/June are not repeated.
    assert seen == [("2026-07-01", "2026-07-13")]
    assert out["status"] == "success"


def test_bootstrap_does_not_restart_a_completed_covering_job():
    existing = {"job_id": "done", "status": "success",
                "date_from": "2026-05-01", "date_to": "2026-07-13"}
    with patch.object(writers, "get_latest_recovery_job", return_value=existing), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "sync_keyword_daily_facts") as sync:
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "success"
    assert out["reason"] == "already_complete"
    sync.assert_not_called()


def test_bootstrap_fails_closed_when_start_date_unresolved():
    with patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "resolve_history_start_date",
                      side_effect=kss.KeywordBootstrapError("no start")):
        out = kss.run_keyword_bootstrap()
    assert out["status"] == "failed"
    assert out["reason"] == "start_date_unresolved"


# ─────────────────────────────────────────────────────────────────────────────
# §2 — auto bootstrap on deploy
# ─────────────────────────────────────────────────────────────────────────────
def test_deploy_bootstrap_not_needed_when_coverage_complete():
    with patch.object(writers, "get_latest_recovery_job", return_value=None), \
         patch.object(kss, "_bootstrap_needed", return_value=False):
        assert kss.maybe_start_bootstrap_on_deploy() == "not_needed"


def test_deploy_bootstrap_skips_when_already_running():
    with patch.object(writers, "get_latest_recovery_job",
                      return_value={"status": "running"}):
        assert kss.maybe_start_bootstrap_on_deploy() == "already_running"


def test_deploy_bootstrap_starts_when_partial(monkeypatch):
    started = {"v": False}

    class _T:
        def __init__(self, *a, **k):
            pass

        def start(self):
            started["v"] = True

    monkeypatch.setattr(kss.threading, "Thread", _T)
    kss._bootstrap_running = False
    with patch.object(writers, "get_latest_recovery_job", return_value=None), \
         patch.object(kss, "_bootstrap_needed", return_value=True):
        out = kss.maybe_start_bootstrap_on_deploy()
    assert out == "started"
    assert started["v"] is True


def test_bootstrap_needed_true_when_table_empty():
    with patch.object(kss, "_durable_coverage", return_value=(None, None, 0)):
        assert kss._bootstrap_needed() is True


def test_bootstrap_needed_true_when_coverage_starts_late():
    with patch.object(kss, "_durable_coverage",
                      return_value=(date(2026, 1, 15), date(2026, 7, 13), 999)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2024, 1, 1), "env")):
        assert kss._bootstrap_needed() is True


def test_bootstrap_needed_false_when_fully_covered():
    with patch.object(kss, "_durable_coverage",
                      return_value=(date(2024, 1, 1), date(2026, 7, 13), 999)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2024, 1, 1), "env")):
        assert kss._bootstrap_needed() is False


# ─────────────────────────────────────────────────────────────────────────────
# §6 — All-time completeness metadata
# ─────────────────────────────────────────────────────────────────────────────
def test_history_complete_true_only_when_full_and_success():
    job = {"status": "success",
           "completed_chunks": [kss._chunk_key(a, b)
                                for a, b in kss._month_chunks(date(2026, 5, 1), date(2026, 7, 13))]}
    with patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2026, 5, 1), "env")), \
         patch.object(kss, "_durable_coverage",
                      return_value=(date(2026, 5, 1), date(2026, 7, 13), 500)), \
         patch.object(writers, "get_latest_recovery_job", return_value=job):
        out = kss.keyword_history_status()
    assert out["history_complete"] is True
    assert out["missing_date_ranges"] == []
    assert out["history_start_expected"] == "2026-05-01"


def test_history_incomplete_when_coverage_starts_after_expected():
    with patch.object(kss, "_account_today", return_value=date(2026, 7, 13)), \
         patch.object(kss, "resolve_history_start_date", return_value=(date(2024, 1, 1), "env")), \
         patch.object(kss, "_durable_coverage",
                      return_value=(date(2026, 1, 15), date(2026, 7, 13), 500)), \
         patch.object(writers, "get_latest_recovery_job", return_value=None):
        out = kss.keyword_history_status()
    assert out["history_complete"] is False
    assert out["durable_coverage_start"] == "2026-01-15"
    assert out["missing_date_ranges"]     # earlier months are missing


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
