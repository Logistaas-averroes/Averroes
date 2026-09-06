"""
tests/test_pr_ads_156_f1_freshness_truth.py

PR-ADS-156-F1 — closing the false-green freshness paths.

PR-ADS-156 made the two Platform Evidence datasets refresh automatically and
gave them an audit command. The audit could still certify a dataset that was
stale or empty for reasons nobody had proven, in five distinct ways:

  1. freshness was derived from ``MAX(source_date)`` — the newest ROW — so a
     dataset with no recent rows looked stale even when its interval had been
     queried and come back genuinely empty, and a dataset whose syncs had
     stopped looked fresh for as long as its old rows sat there;
  2. "verified empty" was INFERRED from ``status = 'success' AND row_count = 0``,
     which is also the shape of historical batches recorded while the evidence
     pipeline was down;
  3. the persistence and legacy-source violation codes were declared and never
     emitted — in JSON that reads as a check that ran and passed;
  4. identity/currency checks scanned the whole table, so quarantined
     Windsor-era rows would have blocked the new pipeline permanently;
  5. a search term counted as "identified" if its text was non-empty, with no
     account, campaign or ad group required.

Run with:
    python -m pytest tests/test_pr_ads_156_f1_freshness_truth.py -v
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

from analysis import account_time  # noqa: E402
from analysis import legacy_source_guard as guard  # noqa: E402
from scripts import audit_keyword_search_term_freshness as audit  # noqa: E402
from services import keyword_sync_service as kw_sync  # noqa: E402
from services import search_term_sync_service as st_sync  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)
from tests.test_pr_ads_156_evidence_freshness import (  # noqa: E402
    _FakeWriters, _data_fixture, _install_fake_writers, _rows, _sync_fixture,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

_SCHEDULERS = ("scheduler/daily.py", "scheduler/weekly.py", "scheduler/monthly.py")
_AUDIT_MODULE = "scripts.audit_keyword_search_term_freshness"

TODAY = date(2026, 3, 1)


def _assess(sync: dict, data: dict, *, today: date = TODAY,
            dataset: str = "search_terms", table: str = "search_terms") -> dict:
    return audit._assess(dataset, table, "google_ads_api", dataset, sync, data,
                         stale_after_days=8, today=today, registered=True)


def _run_audit(*args, env_extra=None):
    # PR-ADS-156-F3: the subprocess needs the configured account, or the audit
    # correctly refuses to certify any population at all.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root",
           "PYTHONPATH": str(_ROOT), "GOOGLE_ADS_CUSTOMER_ID": "555"}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", _AUDIT_MODULE, *args],
                          cwd=str(_ROOT), capture_output=True, text=True, env=env)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — freshness from coverage, not from the newest row
# ═════════════════════════════════════════════════════════════════════════════
def test_1_an_old_successful_empty_batch_is_stale():
    """The exact false green: a verified-empty interval, correctly recorded, and
    then nothing for two months.

    It leaves no row behind, so a `MAX(source_date)` check reads whatever ran
    before it — or nothing at all — and reports the dataset as fine. Coverage
    knows better: the last thing anyone proved ended on 1 January.
    """
    verdict = _assess(
        _sync_fixture(latest_batch_row_count=0, latest_batch_fetched_count=0,
                      latest_batch_prepared_count=0,
                      latest_batch_verified_empty=True, verified_empty=True,
                      verified_empty_intervals=1,
                      coverage_through="2026-01-01",
                      certified_interval={"date_from": "2025-12-19",
                                          "date_to": "2026-01-01"}),
        _data_fixture())

    assert verdict["stale"] is True
    assert audit.V_SOURCE_STALE in verdict["violation_codes"]
    assert verdict["ok"] is False
    detail = next(v["detail"] for v in verdict["violations"]
                  if v["code"] == audit.V_SOURCE_STALE)
    assert "2026-01-01" in detail


def test_2_a_recent_verified_empty_interval_passes_with_no_rows():
    """Asked, answered, nothing there — and nothing stored, because there was
    nothing to store. That is a healthy dataset, not a broken one."""
    verdict = _assess(
        _sync_fixture(latest_batch_row_count=0, latest_batch_fetched_count=0,
                      latest_batch_prepared_count=0,
                      latest_batch_verified_empty=True, verified_empty=True,
                      verified_empty_intervals=1),
        _data_fixture())

    assert verdict["ok"] is True, verdict["violations"]
    assert verdict["violation_codes"] == []
    assert verdict["stale"] is False
    assert verdict["data_last_seen"] is None


def test_3_a_recent_verified_empty_interval_passes_when_data_is_older():
    """Rows from a busier fortnight, then a quiet one that was genuinely
    queried. Freshness follows the query, not the rows."""
    verdict = _assess(
        _sync_fixture(latest_batch_row_count=0, latest_batch_fetched_count=0,
                      latest_batch_prepared_count=0,
                      latest_batch_verified_empty=True, verified_empty=True,
                      verified_empty_intervals=1),
        _data_fixture(row_count=120, min_source_date="2026-01-05",
                      max_source_date="2026-02-10"))

    assert verdict["ok"] is True, verdict["violations"]
    assert verdict["stale"] is False
    # Both quantities are published, and they disagree — which is the point.
    assert verdict["data_last_seen"] == "2026-02-10"
    assert verdict["coverage_through"] == "2026-03-01"


# ═════════════════════════════════════════════════════════════════════════════
# §2 — verified-empty is durable evidence, never an inference
# ═════════════════════════════════════════════════════════════════════════════
def test_4_success_with_zero_rows_is_not_proof_of_emptiness():
    """`status = 'success' AND row_count = 0` is the shape of the historical
    batches recorded while the evidence pipeline was unavailable. Without the
    durable marker it proves that a batch finished, and nothing else."""
    verdict = _assess(
        _sync_fixture(latest_batch_row_count=0, latest_batch_fetched_count=None,
                      latest_batch_prepared_count=None,
                      latest_batch_rejected_count=None,
                      latest_batch_verified_empty=False, verified_empty=False,
                      unproven_empty_intervals=1),
        _data_fixture())

    assert audit.V_UNPROVEN_EMPTY in verdict["violation_codes"]
    assert verdict["ok"] is False
    assert verdict["verified_empty"] is False


def test_5_a_failed_pull_never_creates_verified_empty_evidence(monkeypatch):
    """Three layers, because one of them alone is a promise rather than a
    guarantee: the service does not claim it, the writer refuses the claim, and
    a claim without counters is refused too."""
    fake = _FakeWriters()
    _install_fake_writers(monkeypatch, fake)

    def _boom(*a, **kw):
        raise RuntimeError("Google Ads API unavailable")

    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range", _boom)
    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is False and out["verified_empty"] is False
    assert fake.finished[-1]["status"] == "failed"
    assert fake.finished[-1]["verified_empty"] is False
    # The counters stay unstated. A failed pull measured nothing, and a stored
    # zero would read downstream as "we looked and there was nothing".
    assert fake.finished[-1].get("fetched_count") is None

    from db.writers import _honour_verified_empty as honour

    # A failed batch cannot be verified empty, whatever it claims.
    assert honour(1, True, "failed", 0, 0, 0, 0) is False
    # Nor can a successful one that wrote rows, or fetched them.
    assert honour(1, True, "success", 3, 3, 3, 0) is False
    assert honour(1, True, "success", 0, 5, 0, 5) is False
    # Nor one that did not report its counts at all — an unmeasured pull is not
    # evidence, and defaulting the counters to zero here would manufacture
    # exactly the certainty the column exists to withhold.
    assert honour(1, True, "success", 0, None, None, None) is False
    # The one shape that IS verified empty.
    assert honour(1, True, "success", 0, 0, 0, 0) is True


def test_5b_the_services_state_the_counters_they_measured(monkeypatch):
    """A verified-empty claim travels WITH its evidence.

    The writer refuses a claim whose counts were never stated, so a service that
    passed `verified_empty=True` alone would silently record FALSE and the
    interval would read as unproven forever. Both canonical services therefore
    report all four counts, on the empty path and the non-empty one.
    """
    fake = _FakeWriters()
    _install_fake_writers(monkeypatch, fake)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range",
                        lambda *a, **kw: [])

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")
    assert out["verified_empty"] is True
    finished = fake.finished[-1]
    assert finished["verified_empty"] is True
    assert (finished["fetched_count"], finished["prepared_count"],
            finished["rejected_count"]) == (0, 0, 0)

    # A pull that returned rows records the counts and claims nothing.
    fake = _FakeWriters()
    _install_fake_writers(monkeypatch, fake)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range",
                        lambda *a, **kw: _rows(4))
    st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")
    finished = fake.finished[-1]
    assert finished["verified_empty"] is False
    assert finished["fetched_count"] == 4 and finished["prepared_count"] == 4

    # The keyword service draws the same line, through its own writer.
    seen = {}
    monkeypatch.setattr("db.writers.start_sync_batch", lambda **kw: 11)
    monkeypatch.setattr("db.writers.finish_sync_batch",
                        lambda **kw: seen.update(kw) or True)
    monkeypatch.setattr("db.writers.write_keyword_daily_facts",
                        lambda **kw: {"fetched": 0, "prepared": 0, "written": 0,
                                      "skipped_missing_identity": 0,
                                      "skipped_no_date": 0})
    monkeypatch.setattr(
        "connectors.google_ads_source.pull_keyword_performance_range",
        lambda *a, **kw: [])
    kw_out = kw_sync.sync_keyword_daily_facts(date(2026, 2, 1), date(2026, 2, 14),
                                              "daily")
    assert kw_out["verified_empty"] is True
    assert seen["verified_empty"] is True
    assert (seen["fetched_count"], seen["prepared_count"],
            seen["rejected_count"]) == (0, 0, 0)


# ═════════════════════════════════════════════════════════════════════════════
# §3 — persistence violations are reachable and specific
# ═════════════════════════════════════════════════════════════════════════════
def test_6_fetched_rows_with_nothing_written_is_reported():
    verdict = _assess(
        _sync_fixture(latest_batch_fetched_count=40, latest_batch_prepared_count=40,
                      latest_batch_row_count=0),
        _data_fixture())

    assert audit.V_FETCHED_NOT_PERSISTED in verdict["violation_codes"]
    detail = next(v["detail"] for v in verdict["violations"]
                  if v["code"] == audit.V_FETCHED_NOT_PERSISTED)
    assert "40" in detail
    # And it is NOT reported as an unproven empty interval — the batch is not
    # empty, it is unstored. One problem, one code.
    assert audit.V_UNPROVEN_EMPTY not in verdict["violation_codes"]


def test_7_partial_persistence_is_reported_from_the_durable_counters():
    mismatch = _assess(
        _sync_fixture(latest_batch_fetched_count=40, latest_batch_prepared_count=40,
                      latest_batch_row_count=31),
        _data_fixture(row_count=31, current={"row_count": 31}))
    assert audit.V_PARTIAL_PERSISTENCE in mismatch["violation_codes"]

    rejected = _assess(
        _sync_fixture(latest_batch_fetched_count=40, latest_batch_prepared_count=37,
                      latest_batch_row_count=37, latest_batch_rejected_count=3),
        _data_fixture(row_count=37, current={"row_count": 37}))
    assert audit.V_PARTIAL_PERSISTENCE in rejected["violation_codes"]
    detail = next(v["detail"] for v in rejected["violations"]
                  if v["code"] == audit.V_PARTIAL_PERSISTENCE)
    assert "rejected 3" in detail

    # A failed latest batch is its own violation, separately from the counters.
    failed = _assess(_sync_fixture(latest_batch_status="failed",
                                   sync_status="failed"),
                     _data_fixture())
    assert audit.V_SYNC_FAILED in failed["violation_codes"]


# ═════════════════════════════════════════════════════════════════════════════
# §4/§5 — certified interval blocks; history discloses
# ═════════════════════════════════════════════════════════════════════════════
def test_8_missing_identity_on_a_current_canonical_row_blocks_certification():
    verdict = _assess(
        _sync_fixture(),
        _data_fixture(row_count=9, max_source_date="2026-03-01",
                      current={"row_count": 9, "rows_missing_identity": 2}))

    assert audit.V_MISSING_IDENTITY in verdict["violation_codes"]
    assert verdict["ok"] is False
    detail = next(v["detail"] for v in verdict["violations"]
                  if v["code"] == audit.V_MISSING_IDENTITY)
    # The sentence names what identity MEANS, so an operator reading the JSON
    # knows which field to go and look at.
    assert "account, campaign, ad group, term" in detail


def test_9_historical_legacy_rows_are_disclosed_and_do_not_fail_freshness():
    """Quarantined Windsor-era rows will never be repaired. Blocking on them
    would make this command permanently red for a dataset whose current window
    is perfectly healthy — and a check that is always red is a check nobody
    reads."""
    verdict = _assess(
        _sync_fixture(),
        _data_fixture(row_count=1_240, min_source_date="2024-01-01",
                      max_source_date="2026-03-01",
                      current={"row_count": 40, "distinct_source_dates": 14},
                      historical={"row_count": 1_200,
                                  "rows_missing_identity": 1_200,
                                  "rows_missing_currency_provenance": 900,
                                  "by_source_system": [
                                      {"source_system": "windsor", "rows": 1_100},
                                      {"source_system": "unknown", "rows": 100}]}))

    assert verdict["ok"] is True, verdict["violations"]
    assert verdict["violation_codes"] == []

    disclosure = next(d for d in verdict["disclosures"]
                      if d["code"] == audit.D_LEGACY_ROWS)
    # Counted, labelled by source, and explicitly not relabelled canonical.
    assert "1200 row(s)" in disclosure["detail"]
    assert "windsor=1100" in disclosure["detail"]
    assert "not relabelled canonical" in disclosure["detail"]


def test_10_missing_currency_proof_on_current_rows_limits_monetary_certification():
    verdict = _assess(
        _sync_fixture(),
        _data_fixture(row_count=9, max_source_date="2026-03-01",
                      current={"row_count": 9,
                               "rows_missing_currency_provenance": 4}))

    assert audit.V_UNPROVEN_CURRENCY in verdict["violation_codes"]
    detail = next(v["detail"] for v in verdict["violations"]
                  if v["code"] == audit.V_UNPROVEN_CURRENCY)
    assert "excluded from verified monetary totals" in detail


def test_11_an_unauthorized_legacy_read_emits_legacy_source_active(tmp_path,
                                                                  monkeypatch):
    """The declared-but-unreachable violation, made reachable.

    The scan runs over a synthetic tree so the case proves the RULE rather than
    the current state of the repository — a test that passes only because
    nothing is broken today cannot tell you when something breaks tomorrow.
    """
    (tmp_path / "services").mkdir(parents=True)
    (tmp_path / "services" / "rogue_evidence.py").write_text(
        "from connectors.windsor_pull import pull_search_terms\n"
        "def go():\n    return pull_search_terms()\n", encoding="utf-8")
    (tmp_path / "services" / "innocent.py").write_text(
        '"""Explains at length that windsor_pull is retired and unused."""\n'
        "VALUE = 1\n", encoding="utf-8")

    findings = guard.scan_legacy_sources(root=tmp_path)
    paths = {f["path"] for f in findings}
    assert "services/rogue_evidence.py" in paths
    # Prose describing the retired provider is not a read of it. Otherwise the
    # cheapest way to pass this guard would be to delete the explanation.
    assert "services/innocent.py" not in paths

    # The audit emits the shared helper's findings under the declared code.
    monkeypatch.setattr(audit, "scan_legacy_sources",
                        lambda *a, **kw: [{"path": "services/rogue_evidence.py",
                                           "reason": guard.REASON_RETIRED_PROVIDER_IMPORT,
                                           "detail": "rogue reads Windsor"}])
    violations = audit._legacy_source_violations()
    assert [v["code"] for v in violations] == [audit.V_LEGACY_SOURCE_ACTIVE]
    assert "allowlist" in violations[0]["detail"]

    # And the CLI and the suite execute the SAME function, so they cannot drift.
    src = (_ROOT / "scripts" / "audit_keyword_search_term_freshness.py").read_text(
        encoding="utf-8")
    assert "from analysis.legacy_source_guard import scan_legacy_sources" in src


def test_11b_the_real_repository_has_no_unauthorized_legacy_read():
    """The live assertion, over the actual tree, through the shared helper."""
    assert guard.scan_legacy_sources() == []
    for rel, reason in guard.LEGACY_ACCESS_ALLOWLIST.items():
        assert (_ROOT / rel).exists(), f"{rel} is allowlisted but does not exist"
        assert any(word in reason for word in
                   ("audit", "reconcil", "migration", "historical", "history",
                    "diagnostic", "legacy", "retired", "writer")), (rel, reason)


# ═════════════════════════════════════════════════════════════════════════════
# §6 — one pull per scheduler execution, adopted only when persisted
# ═════════════════════════════════════════════════════════════════════════════
def _calls(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
    return names


@pytest.mark.parametrize("rel", _SCHEDULERS)
def test_12_each_scheduler_pulls_search_terms_exactly_once(rel):
    """Two pulls of one dataset per run is two answers nothing reconciles.

    Weekly and monthly used to call `pull_search_terms(days_back=…)` at step 1
    and then let the canonical service pull the same window again ~150 lines
    later. The window each trigger asks for is unchanged; the number of times it
    asks is not.
    """
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    called = _calls(tree)

    assert "pull_search_terms" not in called, rel
    assert "pull_search_terms_range" not in called, rel
    assert called.count("sync_recent_search_terms") == 1, rel

    # And the rows it returns are the ones analysed — no second source.
    src = (_ROOT / rel).read_text(encoding="utf-8")
    assert "include_rows=True" in src, rel


@pytest.mark.parametrize("rel", _SCHEDULERS)
def test_13_rows_are_adopted_only_after_the_ok_check(rel):
    """Structural, not textual: the assignment that feeds analysis must sit
    INSIDE the branch that checked the sync succeeded.

    `rows` holds what the pull PREPARED. On a partial write that is not what the
    database holds, so analysing it draws conclusions from rows nobody can look
    up afterwards — the shape of an audit trail with nothing behind it.
    """
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))

    ok_guarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "ok" not in ast.dump(node.test):
            continue
        for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(inner, ast.Assign):
                targets = [t.id for t in inner.targets if isinstance(t, ast.Name)]
                if "search_terms" in targets and "rows" in ast.dump(inner.value):
                    ok_guarded.append(inner)
    assert ok_guarded, f"{rel}: rows are adopted outside an ok check"

    # Nothing assigns the rows unconditionally elsewhere.
    unguarded = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "search_terms"
                         for t in n.targets)
                 and "rows" in ast.dump(n.value)
                 and n not in ok_guarded]
    assert unguarded == [], f"{rel}: {len(unguarded)} unguarded row adoption(s)"


def test_13b_failed_persistence_disables_downstream_analysis():
    """Junk detection and n-grams are gated on the same availability flag, and
    the report is told 'unavailable' rather than 'nothing found'."""
    daily = (_ROOT / "scheduler" / "daily.py").read_text(encoding="utf-8")
    assert "detect_junk_terms(search_terms) if search_terms_available else []" in daily
    assert '"new_junk_terms_available": search_terms_available' in daily

    for rel in ("scheduler/weekly.py", "scheduler/monthly.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "if search_terms_available else None" in src, rel

    # `None` is the advisor's "unavailable"; `{}` would read as "no findings".
    advisor = (_ROOT / "analysis" / "advisor.py").read_text(encoding="utf-8")
    assert "ngram_data: dict | None = None" in advisor


def test_13c_a_failed_pull_never_overwrites_the_previous_snapshot():
    """`save_output` distinguishes 'not measured' from 'measured as empty'.

    Rewriting data/ads_search_terms.json with `[]` because this run's pull
    failed would destroy the last surviving copy of the previous observation and
    make an outage look like a quiet week.
    """
    from connectors.google_ads_source import save_output

    import inspect

    src = inspect.getsource(save_output)
    assert "if rows is None:" in src and "continue" in src

    for rel in ("scheduler/weekly.py", "scheduler/monthly.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "google_ads_save(campaigns, None, keywords, geos)" in src, rel
        assert "google_ads_save(None, search_terms, None, None)" in src, rel


# ═════════════════════════════════════════════════════════════════════════════
# §7 — one account calendar, no silent UTC fallback
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("moment,expected", [
    # British Summer Time: 23:30 UTC is already tomorrow in the account's
    # calendar. The old fallback returned the UTC date here, shifting the
    # requested interval by a day and recording the wrong day as covered.
    (datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc), date(2026, 7, 16)),
    (datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc), date(2026, 7, 15)),
    # GMT: the account day and the UTC day coincide again.
    (datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc), date(2026, 1, 15)),
    # The BST transition itself (last Sunday in March 2026 = 29 March).
    (datetime(2026, 3, 29, 23, 30, tzinfo=timezone.utc), date(2026, 3, 30)),
])
def test_14_account_timezone_boundaries_have_no_silent_utc_fallback(moment, expected):
    assert st_sync._account_today(moment) == expected
    # Keyword and search-term intervals resolve through ONE function, so they
    # can never disagree about which day they are asking Google Ads about.
    assert kw_sync._account_today(moment) == st_sync._account_today(moment)
    assert account_time.account_today(moment) == expected


def test_14b_the_service_no_longer_carries_its_own_calendar_fallback():
    """Checked in the AST, so the paragraph explaining the removed fallback does
    not itself trip the guard — otherwise the cheapest way to pass would be to
    delete the explanation."""
    path = _ROOT / "services" / "search_term_sync_service.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_account_today")

    # No exception handler: there is nothing left to fall back FROM.
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    dumped = ast.dump(fn)
    assert "utcnow" not in dumped
    assert "resolve_canonical_window" not in dumped
    assert "account_today" in dumped
    assert "from analysis.account_time import" in src


# ═════════════════════════════════════════════════════════════════════════════
# §2/§12 — the migration and backward compatibility, against real PostgreSQL
# ═════════════════════════════════════════════════════════════════════════════
def _exec(sql, params=()):
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def _row(sql, params=()):
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _start(source="google_ads_api", dataset="search_terms", **kw) -> int:
    import db.writers as w

    return w.start_sync_batch(source=source, dataset=dataset,
                              sync_type=kw.pop("sync_type", "daily"), **kw)


@_needs_pg
def test_15_the_migration_defaults_existing_batches_to_not_verified_empty(pg):  # noqa: F811
    """The default is the whole point.

    Every batch already in production predates the marker, and a historical
    successful zero-row batch is not evidence that Google returned no data —
    some of them were recorded while the evidence pipeline was unavailable.
    """
    columns = {r[0]: r[1] for r in _rows_of(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'sync_batches'")}
    for name in ("verified_empty", "fetched_count", "prepared_count",
                 "rejected_count"):
        assert name in columns, name
    assert columns["verified_empty"] == "NO"          # NOT NULL

    # A row written the old way — no marker mentioned anywhere.
    _exec("INSERT INTO sync_batches (source, dataset, sync_type, status, "
          "row_count, date_from, date_to, started_at) VALUES "
          "('google_ads_api','search_terms','daily','success',0,"
          "'2026-02-01','2026-02-14', NOW())")
    row = _row("SELECT verified_empty, fetched_count, prepared_count, "
               "rejected_count FROM sync_batches ORDER BY id DESC LIMIT 1")
    assert row[0] is False
    # And the counters are NULL, not 0: nothing was measured, and a stored zero
    # would read as "we looked and there was nothing".
    assert row[1] is None and row[2] is None and row[3] is None

    # Re-running the schema is a no-op, not a second ALTER that fails.
    from db.schema import init_db

    init_db()
    assert _row("SELECT COUNT(*) FROM sync_batches")[0] >= 1


def _rows_of(sql, params=()):
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() or []


@_needs_pg
def test_16_existing_finish_sync_batch_callers_are_unaffected(pg):  # noqa: F811
    """Dozens of call sites pass the original five arguments, several of them
    positionally. They must keep working, and must keep recording nothing they
    did not measure."""
    import db.writers as w

    batch_id = _start(date_from=date(2026, 2, 1), date_to=date(2026, 2, 14))
    assert batch_id

    # The pre-F1 signature, positionally, exactly as legacy callers spell it.
    assert w.finish_sync_batch(batch_id, "success", 12, None,
                               date(2026, 2, 14)) is True

    row = _row("SELECT status, row_count, verified_empty, fetched_count, "
               "prepared_count, rejected_count FROM sync_batches WHERE id = %s",
               (batch_id,))
    assert row[0] == "success" and row[1] == 12
    assert row[2] is False                      # never silently claimed
    assert row[3] is None and row[4] is None and row[5] is None

    # And the watermark still advances the way it always did.
    state = _row("SELECT status, last_source_date FROM sync_state "
                 "WHERE source = 'google_ads_api' AND dataset = 'search_terms'")
    assert state[0] == "success" and str(state[1]) == "2026-02-14"


@_needs_pg
def test_16b_a_verified_empty_claim_is_stored_only_when_it_is_consistent(pg):  # noqa: F811
    import db.writers as w

    honest = _start(date_from=date(2026, 2, 1), date_to=date(2026, 2, 14))
    w.finish_sync_batch(batch_id=honest, status="success", row_count=0,
                        last_source_date=date(2026, 2, 14), verified_empty=True,
                        fetched_count=0, prepared_count=0, rejected_count=0)
    assert _row("SELECT verified_empty FROM sync_batches WHERE id = %s",
                (honest,))[0] is True

    # The same claim over a batch that actually wrote rows is refused at the
    # writer, so no caller — canonical or not — can record an unproven interval
    # as proven.
    dishonest = _start(date_from=date(2026, 2, 1), date_to=date(2026, 2, 14))
    w.finish_sync_batch(batch_id=dishonest, status="success", row_count=9,
                        verified_empty=True, fetched_count=9, prepared_count=9,
                        rejected_count=0)
    assert _row("SELECT verified_empty FROM sync_batches WHERE id = %s",
                (dishonest,))[0] is False


@_needs_pg
def test_17_canonical_rows_round_trip_with_full_identity(pg):  # noqa: F811
    """§12 — against the real database, not a mocked repository.

    The service prepares, the writer stores, and the audit's own SQL reads the
    row back inside the certified interval with its identity intact.
    """
    import db.writers as w

    day = date(2026, 3, 1)
    batch_id = _start(date_from=day - timedelta(days=13), date_to=day)
    written = w.write_search_terms(None, _rows(3, day=day.isoformat()),
                                   sync_batch_id=batch_id)
    assert written == 3
    w.finish_sync_batch(batch_id=batch_id, status="success", row_count=written,
                        last_source_date=day, fetched_count=3, prepared_count=3,
                        rejected_count=0)

    # The account identity reached the column, which is what makes the identity
    # check something other than a string-length test.
    assert _row("SELECT COUNT(*) FROM search_terms WHERE customer_id = '555'")[0] == 3

    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        sync = audit._sync_facts(cur, "google_ads_api", "search_terms")
        cert_from, cert_to = audit._certified_bounds(sync)
        data = audit._search_term_data_facts(cur, cert_from, cert_to)

    assert sync["coverage_through"] == day.isoformat()
    assert sync["latest_batch_fetched_count"] == 3
    assert data["current"]["row_count"] == 3
    assert data["current"]["rows_missing_identity"] == 0
    assert data["current"]["duplicate_natural_key_groups"] == 0
    assert data["historical"]["row_count"] == 0

    verdict = _assess(sync, data, today=day)
    assert verdict["ok"] is True, verdict["violations"]


@_needs_pg
def test_17b_a_row_outside_the_interval_is_history_not_a_current_failure(pg):  # noqa: F811
    """A Windsor-era row with no account, no campaign and no currency sits in
    the same table. It is counted, labelled, and does not fail today."""
    import db.writers as w

    day = date(2026, 3, 1)
    _exec("INSERT INTO search_terms (source_date, search_term, source_system) "
          "VALUES ('2024-05-05', 'legacy term', 'windsor')")

    batch_id = _start(date_from=day - timedelta(days=13), date_to=day)
    w.write_search_terms(None, _rows(2, day=day.isoformat()),
                         sync_batch_id=batch_id)
    w.finish_sync_batch(batch_id=batch_id, status="success", row_count=2,
                        last_source_date=day, fetched_count=2, prepared_count=2,
                        rejected_count=0)

    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        sync = audit._sync_facts(cur, "google_ads_api", "search_terms")
        data = audit._search_term_data_facts(cur, *audit._certified_bounds(sync))

    assert data["current"]["row_count"] == 2
    assert data["current"]["rows_missing_identity"] == 0
    assert data["historical"]["row_count"] == 1
    assert data["historical"]["rows_missing_identity"] == 1
    assert {e["source_system"] for e in data["historical"]["by_source_system"]} == {
        "windsor", "google_ads_api"}

    verdict = _assess(sync, data, today=day)
    assert verdict["ok"] is True, verdict["violations"]
    assert any(d["code"] == audit.D_LEGACY_ROWS for d in verdict["disclosures"])


@_needs_pg
def test_17c_the_audit_cli_exits_nonzero_on_an_unproven_empty_interval(pg):  # noqa: F811
    """End to end, with real exit codes: a controlled 1, not an exception."""
    for dataset in ("search_terms", "keyword_facts"):
        _exec("INSERT INTO sync_batches (source, dataset, sync_type, status, "
              "row_count, date_from, date_to, started_at, finished_at) VALUES "
              "('google_ads_api', %s, 'daily', 'success', 0, %s, %s, NOW(), NOW())",
              (dataset, date.today() - timedelta(days=13), date.today()))
        _exec("INSERT INTO sync_state (source, dataset, status, "
              "last_successful_sync_at, last_source_date) "
              "VALUES ('google_ads_api', %s, 'success', NOW(), %s) "
              "ON CONFLICT (source, dataset) DO UPDATE SET status = 'success'",
              (dataset, date.today()))

    res = _run_audit("--json", env_extra={"DATABASE_URL": pg.url})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "Traceback" not in res.stderr
    payload = json.loads(res.stdout)
    assert audit.V_UNPROVEN_EMPTY in payload["violation_codes"]
    for ds in payload["datasets"]:
        assert ds["verified_empty"] is False
        assert ds["unproven_empty_intervals"] == 1

    # Human mode prints the same verdict without raising.
    human = _run_audit(env_extra={"DATABASE_URL": pg.url})
    assert human.returncode == 1
    assert "Traceback" not in human.stderr
    assert "coverage through" in human.stdout


# ═════════════════════════════════════════════════════════════════════════════
# 18 — the surrounding contract suites stay in the blocking gate
# ═════════════════════════════════════════════════════════════════════════════
def test_18_the_neighbouring_contract_suites_remain_in_the_blocking_gate():
    """F1 changes a shared writer, a shared connector and three schedulers. The
    suites that would notice a regression are named in the blocking CI step, so
    "still green" is enforced by the gate rather than asserted here."""
    workflow = (_ROOT / ".github" / "workflows" / "pr-ads-153d-checks.yml").read_text(
        encoding="utf-8")
    for suite in ("test_pr_ads_156_evidence_freshness",
                  "test_pr_ads_156_f1_freshness_truth",
                  "test_pr_ads_155_f2_lifecycle_activity_truth",
                  "test_scheduler_google_ads_cutover",
                  "test_pr_ads_145_search_terms_completion"):
        assert suite in workflow, suite
