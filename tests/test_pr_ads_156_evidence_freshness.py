"""
tests/test_pr_ads_156_evidence_freshness.py

PR-ADS-156 — Keyword and Search-Term evidence: fresh, canonical, self-auditing.

The contradiction this closes
------------------------------
`keyword_daily_facts` and `search_terms` are the canonical Platform Evidence
tables. Both already had direct Google Ads API services. Neither was refreshed
by the primary production command, and the retired-dataset registry said so in
terms that had stopped being true — "search terms … refreshed by the weekly
scheduler, not by this incremental run", and "NO canonical Google Ads API
incremental persistence path exists for keywords today".

Search terms were also written by three schedulers, each with its own inline
copy of pull → open batch → write → judge → finish. Three copies meant three
definitions of success, and they had drifted: a zero-row pull was plain
`success` daily, `success` with the message "evidence pipeline unavailable"
weekly, and fatal only monthly.

Run with:
    python -m pytest tests/test_pr_ads_156_evidence_freshness.py -v
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import legacy_source_guard  # noqa: E402
from scheduler import incremental_sync as sync  # noqa: E402
from services import dataset_keys  # noqa: E402
from services import freshness_service  # noqa: E402
from services import search_term_sync_service as st_sync  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

_SYNC_SRC = (_ROOT / "scheduler" / "incremental_sync.py").read_text(encoding="utf-8")
_AUDIT = "scripts.audit_keyword_search_term_freshness"

_SCHEDULERS = ("scheduler/daily.py", "scheduler/weekly.py", "scheduler/monthly.py")


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse(rel: str) -> ast.Module:
    return ast.parse((_ROOT / rel).read_text(encoding="utf-8"))


def _code_only(rel: str) -> str:
    """Source with comments AND docstrings removed.

    A guard that greps raw text fails on the paragraph explaining what was
    removed, which is both wrong and a strong incentive to delete the
    explanation. The check is about what the code DOES.

    PR-ADS-156-F1 §3: delegates to the shared guard so this suite and the audit
    command strip prose the same way.
    """
    return legacy_source_guard.code_only(_ROOT / rel)


def _called_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _run_audit(*args, env_extra=None):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root",
           "PYTHONPATH": str(_ROOT)}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", _AUDIT, *args],
                          cwd=str(_ROOT), capture_output=True, text=True, env=env)


def _patch_evidence_services(monkeypatch, *, keyword=None, search=None):
    monkeypatch.setattr("services.keyword_sync_service.sync_recent_keyword_facts",
                        keyword or (lambda *a, **kw: {
                            "ok": True, "source": "google_ads_api",
                            "dataset": "keyword_facts", "batch_id": 1,
                            "date_from": "2026-01-01", "date_to": "2026-01-30",
                            "fetched": 5, "prepared": 5, "written": 5,
                            "verified_empty": False, "error": None}))
    monkeypatch.setattr("services.search_term_sync_service.sync_recent_search_terms",
                        search or (lambda *a, **kw: {
                            "ok": True, "source": "google_ads_api",
                            "dataset": "search_terms", "batch_id": 2,
                            "date_from": "2026-01-17", "date_to": "2026-01-30",
                            "fetched": 9, "prepared": 9, "written": 9,
                            "rejected": 0, "rejected_reasons": {},
                            "verified_empty": False, "error": None,
                            "latest_source_date": "2026-01-30"}))


class _FakeWriters:
    """A minimal stand-in for db.writers that records what it was asked to do."""

    def __init__(self, *, batch_id=7, written=None):
        self.batch_id = batch_id
        self.written = written
        self.finished = []

    def start_sync_batch(self, **kw):
        self.started = kw
        return self.batch_id

    def finish_sync_batch(self, **kw):
        self.finished.append(kw)
        return True

    def write_search_terms(self, run_id, rows, sync_batch_id=None):
        return len(rows) if self.written is None else self.written


def _install_fake_writers(monkeypatch, fake):
    import db.writers as real

    for name in ("start_sync_batch", "finish_sync_batch", "write_search_terms"):
        monkeypatch.setattr(real, name, getattr(fake, name))


def _sync_fixture(**overrides) -> dict:
    """A healthy, current canonical sync record in the F1 shape.

    Built here rather than spelled out at each call site so a case states only
    the ONE fact it is about — an old coverage date, a missing counter — instead
    of restating a dozen healthy fields around it.
    """
    facts = {
        "sync_status": "success",
        "latest_successful_sync": "2026-03-01T06:00:00+00:00",
        "sync_error": None,
        "latest_requested_interval": {"date_from": "2026-02-16",
                                      "date_to": "2026-03-01"},
        "latest_batch_status": "success",
        "latest_batch_row_count": 3,
        "latest_batch_fetched_count": 3,
        "latest_batch_prepared_count": 3,
        "latest_batch_rejected_count": 0,
        "latest_batch_verified_empty": False,
        "latest_proven_source_date": "2026-03-01",
        "coverage_through": "2026-03-01",
        "certified_interval": {"date_from": "2026-02-16", "date_to": "2026-03-01"},
        "verified_empty": False,
        "verified_empty_intervals": 0,
        "unproven_empty_intervals": 0,
        "failed_batches": 0,
    }
    facts.update(overrides)
    return facts


def _data_fixture(*, current=None, historical=None, **overrides) -> dict:
    """Table facts in the F1 certified/historical split."""
    clean = {"row_count": 0, "distinct_source_dates": 0,
             "min_source_date": None, "max_source_date": None,
             "rows_missing_identity": 0, "rows_missing_currency_provenance": 0,
             "duplicate_natural_key_groups": 0}
    facts = {
        "row_count": 0, "min_source_date": None, "max_source_date": None,
        "data_last_seen": None,
        "current": {**clean, **(current or {})},
        "historical": {**clean, "rows_inside_interval_non_canonical": 0,
                       "by_source_system": [], **(historical or {})},
    }
    facts.update(overrides)
    facts.setdefault("data_last_seen", facts.get("max_source_date"))
    if "max_source_date" in overrides and "data_last_seen" not in overrides:
        facts["data_last_seen"] = overrides["max_source_date"]
    return facts


def _rows(n, *, day="2026-02-10", term="widget", **overrides):
    """Fully-identified canonical search-term rows.

    PR-ADS-156-F1 §5 added account/campaign/ad-group identity to what the
    service accepts, so the fixture carries it: a row without those is no longer
    a canonical row, and tests about persistence should not be quietly testing
    rejection instead. ``overrides`` lets a case strip one field deliberately.
    """
    base = {"source_date": day,
            "customer_id": "555", "campaign_name": "C", "campaign_id": "111",
            "ad_group": "AG", "ad_group_id": "222",
            "keyword": "kw", "match_type": "EXACT", "cost_micros": 1000,
            "currency_code": "GBP", "clicks": 1, "impressions": 10,
            "conversions": 0, "source": "google_ads_api"}
    rows = []
    for i in range(n):
        # Distinct terms when several are asked for (the natural key needs
        # them); the exact term when one is. Overrides land LAST so a case can
        # blank a field deliberately without the suffix rescuing it.
        row = {**base, "search_term": f"{term}-{i}" if n > 1 else term}
        row.update(overrides)
        rows.append(row)
    return rows


# ═════════════════════════════════════════════════════════════════════════════
# 1-3 — the primary run refreshes both, under registered keys
# ═════════════════════════════════════════════════════════════════════════════
def test_1_the_primary_incremental_sync_refreshes_keyword_facts(monkeypatch):
    """The dataset the weekly scheduler used to own now runs every day."""
    _patch_evidence_services(monkeypatch)
    errors: list = []
    result = sync._sync_keyword_facts(run_id=1, errors=errors)

    assert result["status"] == "success"
    assert result["dataset"] == "keyword_facts"
    assert result["rows_written"] == 5
    assert errors == []
    # And it is wired into the run, not merely callable.
    assert "datasets[LABEL_KEYWORD_FACTS] = _sync_keyword_facts(" in _SYNC_SRC


def test_2_the_primary_incremental_sync_refreshes_search_terms(monkeypatch):
    _patch_evidence_services(monkeypatch)
    errors: list = []
    result = sync._sync_search_terms(run_id=1, errors=errors)

    assert result["status"] == "success"
    assert result["dataset"] == "search_terms"
    assert result["rows_written"] == 9
    assert result["latest_source_date"] == "2026-01-30"
    assert errors == []
    assert "datasets[LABEL_SEARCH_TERMS] = _sync_search_terms(" in _SYNC_SRC


def test_3_both_pairs_are_registered_and_spelled_once():
    """A key the freshness config does not read is a dataset that reports
    'never run' forever while its table fills up normally."""
    for source, dataset in (
        (dataset_keys.KEYWORD_FACTS_SOURCE, dataset_keys.KEYWORD_FACTS_DATASET),
        (dataset_keys.SEARCH_TERMS_SOURCE, dataset_keys.SEARCH_TERMS_DATASET),
    ):
        assert dataset_keys.is_registered_pair(source, dataset), (source, dataset)

    config = freshness_service.DATASET_FRESHNESS_CONFIG
    assert config["keyword_facts"]["source"] == dataset_keys.KEYWORD_FACTS_SOURCE
    assert config["keyword_facts"]["dataset"] == dataset_keys.KEYWORD_FACTS_DATASET
    assert config["keyword_facts"]["table"] == "keyword_daily_facts"
    assert config["search_terms"]["source"] == dataset_keys.SEARCH_TERMS_SOURCE
    assert config["search_terms"]["dataset"] == dataset_keys.SEARCH_TERMS_DATASET
    assert config["search_terms"]["table"] == "search_terms"

    # Spelled once: the owning services import the keys rather than restating them.
    for module in ("services/keyword_sync_service.py",
                   "services/search_term_sync_service.py"):
        src = (_ROOT / module).read_text(encoding="utf-8")
        assert "from services.dataset_keys import" in src, module

    # Declared for enumeration, and separately from the pairs this module stamps.
    assert set(sync.SERVICE_OWNED_SYNC_PAIRS) == {
        (dataset_keys.KEYWORD_FACTS_SOURCE, dataset_keys.KEYWORD_FACTS_DATASET),
        (dataset_keys.SEARCH_TERMS_SOURCE, dataset_keys.SEARCH_TERMS_DATASET)}


# ═════════════════════════════════════════════════════════════════════════════
# 4-5 — one service each, called from everywhere
# ═════════════════════════════════════════════════════════════════════════════
def test_4_keyword_sync_reuses_the_existing_canonical_service():
    """No second keyword writer. The incremental run calls the same function the
    weekly, monthly, bootstrap and admin-refresh paths already call."""
    tree = _parse("scheduler/incremental_sync.py")
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_sync_keyword_facts")
    imported = {alias.name for node in ast.walk(fn)
                if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "sync_recent_keyword_facts" in imported
    # It writes no keyword facts of its own.
    assert "write_keyword_daily_facts" not in ast.dump(fn)


def test_5_every_scheduled_search_term_write_goes_through_the_one_service():
    """The three inline copies are gone.

    Checked in the AST rather than by reading: `write_search_terms` is still a
    perfectly good function, and the point is not that it disappeared but that
    no SCHEDULER calls it directly any more — because a caller that combines its
    own pull with its own write is a fourth definition of what success means.
    """
    for rel in _SCHEDULERS:
        called = _called_names(_parse(rel))
        assert "write_search_terms" not in called, rel
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "sync_recent_search_terms" in src, rel

    # And the incremental run uses it too.
    assert "sync_recent_search_terms" in _SYNC_SRC


# ═════════════════════════════════════════════════════════════════════════════
# 8-11 — persistence and the verified-empty distinction
# ═════════════════════════════════════════════════════════════════════════════
def test_8_a_non_empty_pull_that_writes_nothing_fails(monkeypatch):
    fake = _FakeWriters(written=0)
    _install_fake_writers(monkeypatch, fake)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range",
                        lambda *a, **kw: _rows(4))

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is False
    assert out["fetched"] == 4 and out["written"] == 0
    assert "persistence failed" in out["error"]
    assert out["verified_empty"] is False
    # The failed batch must NOT advance the proven-coverage watermark.
    assert fake.finished[-1]["status"] == "failed"
    assert fake.finished[-1].get("last_source_date") is None


def test_9_partial_persistence_fails(monkeypatch):
    fake = _FakeWriters(written=2)
    _install_fake_writers(monkeypatch, fake)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range",
                        lambda *a, **kw: _rows(5))

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is False
    assert out["written"] == 2 and out["prepared"] == 5
    assert "partial persistence" in out["error"]
    assert fake.finished[-1]["status"] == "failed"


def test_10_a_successful_empty_response_is_explicitly_verified_empty(monkeypatch):
    """Asked, answered, nothing there — a measurement, and a success."""
    fake = _FakeWriters()
    _install_fake_writers(monkeypatch, fake)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range",
                        lambda *a, **kw: [])

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is True
    assert out["verified_empty"] is True
    assert out["fetched"] == 0 and out["written"] == 0
    # The watermark advances: this interval IS now proven.
    assert fake.finished[-1]["status"] == "success"
    assert fake.finished[-1]["last_source_date"] == date(2026, 2, 14)


def test_11_a_failed_pull_cannot_masquerade_as_verified_empty(monkeypatch):
    """A failure returns no rows too. That is the whole reason the flag exists."""
    fake = _FakeWriters()
    _install_fake_writers(monkeypatch, fake)

    def _boom(*a, **kw):
        raise RuntimeError("Google Ads API unavailable")

    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range", _boom)

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is False
    assert out["verified_empty"] is False
    assert out["fetched"] == 0
    assert "Google Ads API unavailable" in out["error"]
    assert fake.finished[-1]["status"] == "failed"
    assert fake.finished[-1].get("last_source_date") is None

    # The keyword service draws the same line.
    from services import keyword_sync_service as kw

    monkeypatch.setattr("db.writers.start_sync_batch", lambda **kw_: 3)
    monkeypatch.setattr("db.writers.finish_sync_batch", lambda **kw_: True)
    monkeypatch.setattr(
        "connectors.google_ads_source.pull_keyword_performance_range", _boom)
    kw_out = kw.sync_keyword_daily_facts(date(2026, 2, 1), date(2026, 2, 14), "daily")
    assert kw_out["ok"] is False and kw_out["verified_empty"] is False


# ═════════════════════════════════════════════════════════════════════════════
# 12-14, 18-19 — freshness reads canonical state, never legacy
# ═════════════════════════════════════════════════════════════════════════════
def test_12_search_term_freshness_is_canonical_not_windsor():
    """The verdict endpoint and the verification script both read the canonical
    pair. Windsor rows survive as history, under a key that says so."""
    api_src = (_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    verdict = api_src[api_src.index("def _build_search_terms_verdict"):]
    verdict = verdict[:verdict.index("\ndef ", 10)]
    assert "SEARCH_TERMS_SOURCE" in verdict and "SEARCH_TERMS_DATASET" in verdict
    assert "legacy_sync_state" in verdict
    assert "historical only" in verdict

    verifier = _code_only("scripts/verify_search_terms_pipeline.py")
    assert "SEARCH_TERMS_SOURCE" in verifier
    assert "connectors.windsor_pull" not in verifier
    assert "from connectors.google_ads_source import pull_search_terms" in verifier


def test_13_keyword_freshness_reads_the_durable_facts_not_the_snapshot():
    config = freshness_service.DATASET_FRESHNESS_CONFIG
    assert config["keyword_facts"]["table"] == "keyword_daily_facts"
    assert config["keyword_facts"]["page"] == "keywords"
    # The legacy snapshot keeps a freshness entry — it still has live
    # non-evidence consumers — but it is NOT a page dependency.
    assert config["keywords"]["table"] == "keywords"
    assert config["keywords"]["page"] is None


def test_14_stale_canonical_data_is_reported_stale_even_with_rows():
    """An old table full of rows is the case an "are there rows" check calls
    healthy, and it is the case this command exists for."""
    from scripts.audit_keyword_search_term_freshness import _assess, _stale

    today = date(2026, 3, 1)
    assert _stale("2026-01-01", 8, today) is True
    assert _stale(today.isoformat(), 8, today) is False
    # Never covered is `canonical_sync_never_run`, not stale — one problem, one
    # code.
    assert _stale(None, 8, today) is False

    verdict = _assess(
        "search_terms", "search_terms", "google_ads_api", "search_terms",
        _sync_fixture(latest_successful_sync="2026-01-02",
                      latest_batch_row_count=400, latest_batch_fetched_count=400,
                      latest_batch_prepared_count=400,
                      coverage_through="2026-01-01",
                      certified_interval={"date_from": "2025-12-19",
                                          "date_to": "2026-01-01"},
                      latest_proven_source_date="2026-01-01"),
        _data_fixture(row_count=400, max_source_date="2026-01-01",
                      min_source_date="2025-12-02",
                      current={"row_count": 400, "distinct_source_dates": 30}),
        stale_after_days=8, today=today, registered=True)

    assert verdict["stale"] is True
    assert "canonical_source_stale" in verdict["violation_codes"]
    assert verdict["ok"] is False


def test_18_and_19_evidence_never_falls_back_to_legacy_sources():
    """Static guard (§10). Keyword Evidence reads durable facts; Search Term
    Evidence reads the canonical table. Neither falls back."""
    kw_evidence = _code_only("services/keyword_evidence_service.py")
    assert "keyword_daily_facts" in kw_evidence
    assert "FROM keywords" not in kw_evidence
    assert "windsor" not in kw_evidence.lower()

    # The search-term evidence service names Windsor only in prose, describing
    # legacy rows whose provenance is unknown — which is a disclosure, not a
    # read. The guard is over CODE.
    st_evidence = _code_only("services/search_term_evidence_service.py")
    assert "windsor" not in st_evidence.lower()
    assert "ads_search_terms.json" not in st_evidence
    assert "FROM search_terms" in st_evidence or "search_terms" in st_evidence


# ═════════════════════════════════════════════════════════════════════════════
# 15-17 — failing closed
# ═════════════════════════════════════════════════════════════════════════════
def test_15_missing_database_access_fails_closed(monkeypatch):
    """No batch, no pull. An untracked sync cannot be proven later, so Google
    Ads is not called at all."""
    called = {"pull": 0}

    def _count(*a, **kw):
        called["pull"] += 1
        return _rows(3)

    monkeypatch.setattr("db.writers.start_sync_batch", lambda **kw: None)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range", _count)

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is False
    assert out["db_unavailable"] is True
    assert out["verified_empty"] is False
    assert called["pull"] == 0, "no external call without a tracking record"

    # And the audit reports nothing measured rather than zero violations.
    from scripts import audit_keyword_search_term_freshness as audit

    monkeypatch.setattr("db.connection.get_conn",
                        lambda: __import__("contextlib").nullcontext(None))
    report = audit.run_audit()
    assert report["ok"] is False
    assert audit.V_TABLE_UNAVAILABLE in report["violation_codes"]
    assert report["datasets"] == []


def test_16_missing_source_identity_fails_closed():
    """A row with no search term cannot be stored under the natural key, and is
    reported rather than dropped."""
    prepared, rejected = st_sync.classify_rows([
        *_rows(1, term="ok"),
        *_rows(1, term="blank", search_term="   "),
        *_rows(1, term="no-date", source_date=None),
    ])
    assert len(prepared) == 1
    assert rejected[st_sync.REJECT_BLANK_SEARCH_TERM] == 1
    assert rejected[st_sync.REJECT_UNPARSEABLE_DATE] == 1

    from scripts.audit_keyword_search_term_freshness import _assess

    verdict = _assess(
        "keyword_facts", "keyword_daily_facts", "google_ads_api", "keyword_facts",
        _sync_fixture(latest_batch_row_count=10, latest_batch_fetched_count=10,
                      latest_batch_prepared_count=10),
        _data_fixture(row_count=10, min_source_date="2026-02-27",
                      max_source_date="2026-03-01",
                      current={"row_count": 10, "distinct_source_dates": 3,
                               "rows_missing_identity": 4}),
        stale_after_days=8, today=date(2026, 3, 1), registered=True)
    assert "missing_identity" in verdict["violation_codes"]


def test_17_missing_currency_lineage_is_disclosed_and_excluded():
    from scripts.audit_keyword_search_term_freshness import _assess

    verdict = _assess(
        "search_terms", "search_terms", "google_ads_api", "search_terms",
        _sync_fixture(latest_batch_row_count=10, latest_batch_fetched_count=10,
                      latest_batch_prepared_count=10),
        _data_fixture(row_count=10, min_source_date="2026-02-27",
                      max_source_date="2026-03-01",
                      current={"row_count": 10, "distinct_source_dates": 3,
                               "rows_missing_currency_provenance": 3}),
        stale_after_days=8, today=date(2026, 3, 1), registered=True)

    assert "unproven_currency_lineage" in verdict["violation_codes"]
    detail = next(v["detail"] for v in verdict["violations"]
                  if v["code"] == "unproven_currency_lineage")
    assert "excluded from verified monetary totals" in detail


# ═════════════════════════════════════════════════════════════════════════════
# 20-21 — evidence status is separate from executive truth
# ═════════════════════════════════════════════════════════════════════════════
def test_20_executive_truth_semantics_are_unchanged():
    """An evidence outage must not make the revenue ledger look unusable.

    `build_truth_block` reads the geo reconciliation and nothing else — the two
    evidence datasets are not among its inputs, and adding them would have made
    a search-term outage report `truth_status: not_ready` on the figure the
    business steers by.
    """
    datasets = {
        "google_ads_api/geo_reconciliation": {
            "available": True, "campaign_coverage_complete": True,
            "fx_coverage_complete": True, "geo_coverage_complete": True,
            "comparison_like_for_like": True, "reconciled": True,
            "geo_ready": True, "geo_gap_codes": []},
        "google_ads_api/keyword_facts": {"status": "failed"},
        "google_ads_api/search_terms": {"status": "failed"},
    }
    truth = sync.build_truth_block(datasets)
    assert truth["truth_status"] == sync.TRUTH_READY
    assert truth["geo_ready"] is True
    assert truth["gap_codes"] == []
    # Not one evidence code leaked into the executive vocabulary.
    assert not any("keyword" in c or "search_term" in c for c in truth["gap_codes"])


def test_21_evidence_failures_produce_explicit_evidence_gap_codes():
    healthy = sync.build_evidence_block({
        "google_ads_api/keyword_facts": {"status": "success"},
        "google_ads_api/search_terms": {"status": "success"}})
    assert healthy["evidence_status"] == sync.EVIDENCE_READY
    assert healthy["evidence_gap_codes"] == []

    partial = sync.build_evidence_block({
        "google_ads_api/keyword_facts": {"status": "success"},
        "google_ads_api/search_terms": {"status": "failed"}})
    assert partial["evidence_status"] == sync.EVIDENCE_PARTIAL
    assert partial["evidence_gap_codes"] == [sync.EVIDENCE_GAP_SEARCH_TERMS]

    down = sync.build_evidence_block({
        "google_ads_api/keyword_facts": {"status": "failed"},
        "google_ads_api/search_terms": {"status": "failed"}})
    assert down["evidence_status"] == sync.EVIDENCE_NOT_READY
    assert set(down["evidence_gap_codes"]) == {
        sync.EVIDENCE_GAP_KEYWORD_FACTS, sync.EVIDENCE_GAP_SEARCH_TERMS}

    # A run that never reached the datasets says so, rather than reporting a
    # failure nobody observed.
    absent = sync.build_evidence_block({})
    assert absent["evidence_status"] == sync.EVIDENCE_NOT_READY
    assert absent["evidence_gap_codes"] == [sync.EVIDENCE_GAP_NOT_RUN]

    # A failed evidence refresh is never suppressed because executive truth is fine.
    assert "evidence_status" in _SYNC_SRC
    assert "**evidence," in _SYNC_SRC


# ═════════════════════════════════════════════════════════════════════════════
# 22-25 — the audit command
# ═════════════════════════════════════════════════════════════════════════════
def test_22_the_audit_cli_initializes_its_own_database_connection():
    """Run as `python -m`, nothing calls `init_pool`. Without this the command
    reports a database problem that does not exist."""
    src = (_ROOT / "scripts" / "audit_keyword_search_term_freshness.py").read_text(
        encoding="utf-8")
    assert "ensure_database_ready" in src

    res = _run_audit("--json", env_extra={"DATABASE_URL": ""})
    assert res.returncode == 2, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert payload["ok"] is False
    assert payload["violation_codes"] == ["canonical_table_unavailable"]
    # Nothing was measured, so nothing is reported as zero.
    assert payload["datasets"] == []


@pytest.mark.parametrize("args", [("--json",), ()])
def test_23_both_audit_modes_survive_unavailable_data(args):
    """The moment an operator most needs this command is the moment least is
    available, so neither mode may raise on a partially-proven report."""
    res = _run_audit(*args, env_extra={"DATABASE_URL": ""})
    assert res.returncode == 2
    assert "Traceback" not in res.stderr
    assert res.stdout.strip()


@_needs_pg
def test_24_the_audit_exits_nonzero_for_deliberate_violations(pg):  # noqa: F811
    """A never-run dataset is a violation, and the exit code says so."""
    res = _run_audit("--json", env_extra={"DATABASE_URL": pg.url})
    assert res.returncode == 1, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert payload["database_available"] is True
    assert payload["ok"] is False
    assert "canonical_sync_never_run" in payload["violation_codes"]
    # Both datasets were inspected, not just the first failing one.
    assert {d["dataset"] for d in payload["datasets"]} == {
        "keyword_facts", "search_terms"}
    assert payload["external_writes_performed"] is False


@_needs_pg
def test_25_a_healthy_current_window_fixture_exits_zero(pg):  # noqa: F811
    """The positive control: seed a proven, current, deduplicated fixture and
    require exit 0. Without it, every assertion above could be satisfied by a
    command that fails on everything."""
    _seed_healthy(date.today())
    res = _run_audit("--json", env_extra={"DATABASE_URL": pg.url})
    assert res.returncode == 0, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert payload["ok"] is True
    assert payload["violation_codes"] == []
    by_name = {d["dataset"]: d for d in payload["datasets"]}
    assert by_name["search_terms"]["current"]["duplicate_natural_key_groups"] == 0
    assert by_name["keyword_facts"]["current"]["duplicate_natural_key_groups"] == 0
    # F1 §1: coverage is what freshness was judged on, and the newest stored row
    # is reported beside it rather than instead of it.
    for ds in payload["datasets"]:
        assert ds["coverage_through"] is not None
        assert ds["stale"] is False
        assert "data_last_seen" in ds
    # All-time history is DISCLOSED, never claimed complete.
    assert any(d["code"] == "history_coverage_unproven"
               for d in payload["disclosures"])


def test_26_no_code_path_performs_a_google_ads_mutation():
    """Read-only against Google Ads, proved over the paths this PR touches."""
    mutating = ("mutate", "MutateGoogleAds", "campaign_criterion_service",
                "add_negative", "update_budget", "set_bid")
    for rel in (*_SCHEDULERS, "scheduler/incremental_sync.py",
                "services/search_term_sync_service.py",
                "services/keyword_sync_service.py",
                "scripts/audit_keyword_search_term_freshness.py",
                "scripts/verify_search_terms_pipeline.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))
        for verb in mutating:
            assert verb not in code, f"{rel} mentions {verb}"

    # And every evidence result states it.
    assert '"external_writes_performed": False' in _SYNC_SRC


def test_27_the_existing_contract_surfaces_are_intact():
    """The functions other suites and production call are still exported."""
    from services import keyword_sync_service as kw

    for name in ("sync_keyword_daily_facts", "sync_recent_keyword_facts",
                 "run_keyword_bootstrap", "keyword_history_status",
                 "maybe_start_bootstrap_on_deploy"):
        assert callable(getattr(kw, name)), name
    for name in ("sync_search_terms", "sync_recent_search_terms", "classify_rows"):
        assert callable(getattr(st_sync, name)), name
    assert st_sync.DEFAULT_LOOKBACK_DAYS >= 14
    # `write_search_terms` still exists — it is the writer the service calls.
    from db import writers

    assert callable(writers.write_search_terms)


# ═════════════════════════════════════════════════════════════════════════════
# §10 — static legacy-source guard
# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-156-F1 §3: the allowlist and the scan moved into
# `analysis/legacy_source_guard`, so the audit command and this suite execute
# the SAME function. They used to be two implementations of one rule — and the
# audit's `legacy_source_active` code was declared with nothing able to raise
# it, which in JSON reads as a check that ran and passed.
_LEGACY_ACCESS_ALLOWLIST = legacy_source_guard.LEGACY_ACCESS_ALLOWLIST
_PRODUCTION_DIRS = legacy_source_guard.PRODUCTION_DIRS


def test_28_no_production_path_newly_reads_a_retired_source():
    """§10. Fails if scheduler or service code imports Windsor, writes
    production Keyword Evidence into the legacy snapshot, or silently falls back
    from canonical facts to a legacy table."""
    offenders = legacy_source_guard.scan_legacy_sources()
    assert offenders == [], [f["detail"] for f in offenders]


def test_28b_the_allowlist_is_narrow_and_every_entry_is_justified():
    """An allowlist nobody maintains is a hole with documentation attached."""
    for rel, reason in _LEGACY_ACCESS_ALLOWLIST.items():
        assert (_ROOT / rel).exists(), f"{rel} is allowlisted but does not exist"
        assert reason and len(reason) > 10, rel
        assert any(word in reason for word in
                   ("audit", "reconcil", "migration", "historical", "history",
                    "diagnostic", "legacy", "retired", "writer")), (rel, reason)


def test_28c_the_legacy_keyword_snapshot_keeps_its_live_consumers():
    """§5 required an inspection before stopping the scheduled legacy writes.

    Four live consumers read `keywords`, and none of them is Keyword Evidence:
    the campaign drill-down preview, the aggregated keyword endpoint, the
    keyword-review action queue, and the keyword-theme snapshot behind the
    Campaigns page. Stopping the writes would have starved all four, so the
    writes stay — documented, non-authoritative, and out of Keyword Evidence.
    """
    api_src = (_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    assert api_src.count("FROM keywords") >= 3
    revenue_repo = (_ROOT / "db" / "revenue_repository.py").read_text(encoding="utf-8")
    assert "fetch_keyword_theme_snapshot" in revenue_repo

    # Keyword EVIDENCE is not one of them.
    kw_evidence = (_ROOT / "services" / "keyword_evidence_service.py").read_text(
        encoding="utf-8")
    assert "FROM keywords" not in kw_evidence

    # The weekly/monthly schedulers still write it, and still label it legacy.
    for rel in ("scheduler/weekly.py", "scheduler/monthly.py"):
        assert "write_keywords(" in (_ROOT / rel).read_text(encoding="utf-8"), rel


# ═════════════════════════════════════════════════════════════════════════════
# §12 — PostgreSQL-backed persistence certification
# ═════════════════════════════════════════════════════════════════════════════
def _exec(sql, params=()):
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def _scalar(sql, params=()):
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


_ST_INSERT = """
    INSERT INTO search_terms
        (source_date, campaign_name, campaign_id, ad_group, keyword, match_type,
         search_term, cost_micros, currency_code, customer_id, source_system,
         clicks, impressions, conversions)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '555', 'google_ads_api', 1, 10, 0)
    ON CONFLICT (source_date, COALESCE(campaign_name, ''), COALESCE(campaign_id, ''),
                 COALESCE(ad_group, ''), COALESCE(keyword, ''),
                 COALESCE(match_type, ''), search_term)
    DO UPDATE SET clicks = EXCLUDED.clicks, updated_at = NOW()
"""

_KW_INSERT = """
    INSERT INTO keyword_daily_facts
        (source_date, customer_id, campaign_id, ad_group_id, criterion_id,
         campaign_name, ad_group_name, keyword_text, match_type,
         cost_micros, currency_code, source_system, clicks, impressions,
         conversions)
    VALUES (%s, %s, %s, %s, %s, %s, 'AG', 'kw', 'EXACT', 1000, 'GBP',
            'google_ads_api', 1, 10, 0)
    ON CONFLICT (source_date, customer_id, campaign_id, ad_group_id, criterion_id)
    DO UPDATE SET clicks = EXCLUDED.clicks, updated_at = NOW()
"""


def _seed_healthy(today: date) -> None:
    """A proven, current, deduplicated fixture for both datasets."""
    for offset in range(3):
        day = today - timedelta(days=offset)
        _exec(_ST_INSERT, (day, "Camp A", "111", "AG", "kw", "EXACT",
                           f"term-{offset}", 1000, "GBP"))
        _exec(_KW_INSERT, (day, "555", "111", "222", f"333{offset}", "Camp A"))
    for source, dataset in (("google_ads_api", "search_terms"),
                            ("google_ads_api", "keyword_facts")):
        # F1 §2/§3: the durable counters are part of a healthy batch now. A
        # batch that recorded only `row_count` cannot distinguish "wrote 3 of 3"
        # from "wrote 3 of 40", so the fixture states all four.
        _exec("INSERT INTO sync_batches (source, dataset, sync_type, status, "
              "row_count, fetched_count, prepared_count, rejected_count, "
              "verified_empty, date_from, date_to, started_at, finished_at) "
              "VALUES (%s, %s, 'daily', 'success', 3, 3, 3, 0, FALSE, %s, %s, "
              "NOW(), NOW())",
              (source, dataset, today - timedelta(days=13), today))
        _exec("INSERT INTO sync_state (source, dataset, status, "
              "last_successful_sync_at, last_source_date) "
              "VALUES (%s, %s, 'success', NOW(), %s) "
              "ON CONFLICT (source, dataset) DO UPDATE SET status = 'success', "
              "last_successful_sync_at = NOW(), last_source_date = EXCLUDED.last_source_date",
              (source, dataset, today))


@_needs_pg
def test_6_and_7_overlapping_runs_upsert_rather_than_duplicate(pg):  # noqa: F811
    """§12. The same interval synced twice must leave one row per natural key."""
    today = date(2026, 3, 1)
    for _ in range(3):                      # three overlapping runs
        _seed_healthy(today)

    assert _scalar("SELECT COUNT(*) FROM search_terms") == 3
    assert _scalar("SELECT COUNT(*) FROM keyword_daily_facts") == 3
    assert _scalar("""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM search_terms
            GROUP BY source_date, COALESCE(campaign_name,''), COALESCE(campaign_id,''),
                     COALESCE(ad_group,''), COALESCE(keyword,''),
                     COALESCE(match_type,''), search_term
            HAVING COUNT(*) > 1) d""") == 0
    assert _scalar("""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM keyword_daily_facts
            GROUP BY source_date, customer_id, campaign_id, ad_group_id, criterion_id
            HAVING COUNT(*) > 1) d""") == 0


@_needs_pg
def test_12b_identities_stay_separate_including_shared_display_names(pg):  # noqa: F811
    """§12. Two campaigns can share a display name; they are different campaigns.

    The natural key carries the ID for exactly this reason — a key built on the
    label would silently merge two accounts' worth of spend into one row.
    """
    day = date(2026, 3, 1)
    _exec(_ST_INSERT, (day, "Same Name", "111", "AG", "kw", "EXACT", "t", 1000, "GBP"))
    _exec(_ST_INSERT, (day, "Same Name", "222", "AG", "kw", "EXACT", "t", 1000, "GBP"))
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 2

    _exec(_KW_INSERT, (day, "555", "111", "222", "333", "Same Name"))
    _exec(_KW_INSERT, (day, "555", "111", "999", "333", "Same Name"))
    _exec(_KW_INSERT, (day, "666", "111", "222", "333", "Same Name"))
    assert _scalar("SELECT COUNT(*) FROM keyword_daily_facts") == 3


@_needs_pg
def test_12c_only_successful_batches_advance_proven_coverage(pg):  # noqa: F811
    """§12. A failed batch is an attempt, not coverage — and a legacy row can
    never override a newer canonical one."""
    from scripts.audit_keyword_search_term_freshness import _sync_facts
    from db.connection import get_conn

    _exec("INSERT INTO sync_batches (source, dataset, sync_type, status, row_count, "
          "date_from, date_to, started_at) VALUES "
          "('google_ads_api','search_terms','daily','failed',0,'2026-02-01',"
          "'2026-02-14', NOW() - INTERVAL '2 hours')")
    _exec("INSERT INTO sync_batches (source, dataset, sync_type, status, row_count, "
          "date_from, date_to, started_at) VALUES "
          "('google_ads_api','search_terms','daily','success',0,'2026-02-01',"
          "'2026-02-14', NOW())")
    # Only the SUCCESSFUL batch advanced the watermark.
    _exec("INSERT INTO sync_state (source, dataset, status, last_successful_sync_at, "
          "last_source_date) VALUES ('google_ads_api','search_terms','success', "
          "NOW(), '2026-02-14')")
    # A legacy Windsor row, newer-looking by nothing but its presence.
    _exec("INSERT INTO sync_state (source, dataset, status, last_successful_sync_at) "
          "VALUES ('windsor','search_terms','success', NOW() + INTERVAL '1 day')")

    with get_conn() as conn, conn.cursor() as cur:
        facts = _sync_facts(cur, "google_ads_api", "search_terms")

    # The latest CANONICAL batch is the successful one, and proven coverage
    # comes from it.
    assert facts["latest_batch_status"] == "success"
    assert facts["latest_proven_source_date"] == "2026-02-14"
    assert facts["coverage_through"] == "2026-02-14"
    # F1 §2: it is NOT counted as verified-empty. These raw inserts carry no
    # durable marker, which is precisely the shape of the historical batches
    # recorded while the evidence pipeline was unavailable — a successful
    # zero-row row that proves nothing about what the source returned.
    assert facts["verified_empty_intervals"] == 0
    assert facts["unproven_empty_intervals"] == 1
    assert facts["verified_empty"] is False
    assert facts["failed_batches"] == 1
    # The canonical state is the one read; the legacy Windsor row — dated in the
    # FUTURE here, to make the point — cannot displace it.
    assert facts["sync_status"] == "success"
