"""
tests/test_pr_ads_156_f2_stale_analysis_and_durability.py

PR-ADS-156-F2 — the last five false-green paths.

F1 stopped the schedulers overwriting `data/ads_search_terms.json` when the
canonical sync failed, so an outage could not masquerade as a quiet week. Waste
detection then RELOADED that preserved file and published findings from it under
the current run's timestamp: the snapshot was protected from being destroyed and
immediately reused as though it were current, which is the same falsehood from
the other side.

Four more, each a place where something was assumed rather than checked:

  * ``finish_sync_batch`` returns a Boolean and both services ignored it, so a
    run whose CERTIFICATE never persisted still reported the interval covered;
  * ``customer_id`` was declared part of canonical search-term identity but left
    out of the unique index, the writer's conflict target and the audit's
    duplicate key — so the contract said two accounts are distinguishable while
    the index said they are the same row;
  * the legacy guard read literal SQL in two directories, so a service calling
    an imported legacy repository helper passed cleanly;
  * the audit still substituted the UTC date when the account calendar could not
    be resolved — the exact fallback F1 removed from the sync service.

Run with:
    python -m pytest tests/test_pr_ads_156_f2_stale_analysis_and_durability.py -v
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

from analysis import core as waste  # noqa: E402
from analysis import legacy_source_guard as guard  # noqa: E402
from scripts import audit_keyword_search_term_freshness as audit  # noqa: E402
from services import keyword_sync_service as kw_sync  # noqa: E402
from services import search_term_sync_service as st_sync  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)
from tests.test_pr_ads_156_evidence_freshness import (  # noqa: E402
    _FakeWriters, _install_fake_writers, _rows,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

_SCHEDULERS = ("scheduler/weekly.py", "scheduler/monthly.py")
_AUDIT_MODULE = "scripts.audit_keyword_search_term_freshness"

#: A junk term with enough spend to clear the $5 floor, so if a stale snapshot
#: were ever analysed it would produce a visible, attributable finding rather
#: than an empty result that could be mistaken for correct behaviour.
_STALE_SNAPSHOT_ROW = {
    "search_term": "logistics jobs free download",
    "campaign": "stale-campaign", "spend": 999.0,
}


class _UnfinalizableWriters(_FakeWriters):
    """A writer whose batch rows open but never finalize.

    The shape of a database that accepted the INSERT and then lost the
    connection, or a batch id that no longer exists — rare, and precisely the
    case where the DATA is fine and only the proof of it is missing, so nothing
    else in the system would ever notice.
    """

    def finish_sync_batch(self, **kw):
        self.finished.append(kw)
        return False


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch):
    """A data/outputs pair holding a previous run's search-term snapshot."""
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    data.mkdir()
    outputs.mkdir()
    (data / "ads_search_terms.json").write_text(
        json.dumps([_STALE_SNAPSHOT_ROW]), encoding="utf-8")
    (data / "ads_keywords.json").write_text(
        json.dumps([{"keyword": "free logistics jobs", "spend": 888.0,
                     "campaign": "kw-campaign"}]), encoding="utf-8")
    (data / "crm_contacts.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(waste, "DATA_DIR", str(data))
    monkeypatch.setattr(waste, "OUTPUT_DIR", str(outputs))
    return tmp_path


def _report(tmp_path) -> dict:
    return json.loads((tmp_path / "outputs" / "waste_report.json").read_text())


def _parse(rel: str) -> ast.Module:
    return ast.parse((_ROOT / rel).read_text(encoding="utf-8"))


def _run_audit(*args, env_extra=None):
    # PR-ADS-156-F3: the subprocess needs the configured account, or the audit
    # correctly refuses to certify any population at all.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root",
           "PYTHONPATH": str(_ROOT), "GOOGLE_ADS_CUSTOMER_ID": "555"}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", _AUDIT_MODULE, *args],
                          cwd=str(_ROOT), capture_output=True, text=True, env=env)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — waste detection never analyses a stale snapshot
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("rel", _SCHEDULERS)
def test_1_and_2_schedulers_pass_availability_into_waste_detection(rel):
    """Weekly and monthly hand waste detection the canonical rows AND whether
    they are trustworthy. Neither leaves the function to find its own input.

    Checked in the AST because the defect was an absence: `run_waste_detection()`
    with no arguments looks entirely innocent, and reads a file 200 lines away in
    another module.
    """
    tree = _parse(rel)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "run_waste_detection"]
    assert len(calls) == 1, rel
    call = calls[0]
    assert call.args, f"{rel}: waste detection is called with no rows"
    assert {kw.arg for kw in call.keywords} == {"search_term_evidence_available"}, rel


def test_1b_a_failed_sync_never_analyses_the_preserved_snapshot(snapshot_dir):
    """The precise F1 gap: the file survives, and is not read.

    The snapshot on disk holds a junk term with $999 of spend. If it were
    analysed it would appear in the findings, so an empty result here is
    evidence rather than an absence of evidence.
    """
    out = waste.run_waste_detection([], search_term_evidence_available=False)

    assert out["search_term_evidence_available"] is False
    assert out["unavailable_reason"] == waste.WASTE_SEARCH_TERMS_UNAVAILABLE
    assert out["confirmed_waste_items"] == []
    assert out["suspected_waste_items"] == []
    # Not zero — absent. A zero would read as "we looked and found no waste".
    assert out["confirmed_waste_usd"] is None
    assert out["total_spend_analysed"] is None
    assert _STALE_SNAPSHOT_ROW["search_term"] not in json.dumps(out)

    # The snapshot itself is untouched: this run declined to read it, and did
    # not destroy it either.
    preserved = json.loads(
        (snapshot_dir / "data" / "ads_search_terms.json").read_text())
    assert preserved == [_STALE_SNAPSHOT_ROW]

    # And the written report says unavailable, rather than being left stale for
    # the next reader to mistake for current.
    assert _report(snapshot_dir)["search_term_evidence_available"] is False


def test_3_the_persisted_rows_are_exactly_what_is_analysed(snapshot_dir):
    """Not the snapshot, not a superset — the rows the caller supplied."""
    persisted = [
        {"search_term": "free logistics software", "spend": 40.0,
         "campaign": "current-campaign"},
        {"search_term": "logistics pricing", "spend": 60.0,
         "campaign": "current-campaign"},
    ]
    out = waste.run_waste_detection(persisted,
                                    search_term_evidence_available=True)

    assert out["search_term_evidence_available"] is True
    assert out["data_source"] == "canonical_search_terms"
    assert out["search_term_population"] == 2
    assert out["total_spend_analysed"] == 100.0
    analysed = {i["term"] for i in out["confirmed_waste_items"]
                + out["suspected_waste_items"]}
    assert analysed <= {r["search_term"] for r in persisted}
    # The stale snapshot's high-spend junk term is nowhere near this result.
    assert _STALE_SNAPSHOT_ROW["search_term"] not in analysed


def test_4_verified_empty_is_analysed_as_empty_with_no_keyword_fallback(snapshot_dir):
    """Asked, answered, nothing there.

    A verified-empty interval is a measurement OF SEARCH TERMS. The old code
    silently analysed keyword rows instead, which answers a different question
    under the same heading — and the keyword file in this fixture holds an $888
    junk keyword that would have shown up.
    """
    out = waste.run_waste_detection([], search_term_evidence_available=True)

    assert out["search_term_evidence_available"] is True
    assert out["data_source"] == "canonical_search_terms"
    assert out["search_term_population"] == 0
    assert out["confirmed_waste_items"] == []
    assert out["suspected_waste_items"] == []
    # Zero, not None: this run DID measure, and measured nothing.
    assert out["confirmed_waste_usd"] == 0
    assert out["total_spend_analysed"] == 0
    assert "free logistics jobs" not in json.dumps(out)

    # The fallback is gone from the code, not merely unreachable on this path.
    src = guard.code_only(_ROOT / "analysis" / "core.py")
    assert "keywords_fallback" not in src
    assert "ads_keywords.json" not in src


@pytest.mark.parametrize("rel", _SCHEDULERS)
def test_5_unavailable_evidence_cannot_finish_a_successful_waste_batch(rel):
    """A `success` on the waste_terms batch advances a freshness watermark.

    Doing that from evidence that does not exist reports the dataset current on
    the strength of an interval nobody measured — so the successful finish must
    sit on the branch where the evidence IS available, and the unavailable
    branch must finish `failed`.
    """
    tree = _parse(rel)

    guards = [n for n in ast.walk(tree)
              if isinstance(n, ast.If)
              and isinstance(n.test, ast.UnaryOp)
              and isinstance(n.test.op, ast.Not)
              and getattr(n.test.operand, "id", None) == "search_terms_available"]
    assert guards, f"{rel}: no `not search_terms_available` guard around the batch"

    def _finishes(nodes, status):
        found = []
        for node in nodes:
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and getattr(inner.func, "attr", None) == "finish_sync_batch"):
                    for kw in inner.keywords:
                        if (kw.arg == "status" and isinstance(kw.value, ast.Constant)
                                and kw.value.value == status):
                            found.append(inner)
        return found

    waste_guard = next(
        (g for g in guards
         if _finishes(g.body, "failed") and _finishes(g.orelse, "success")), None)
    assert waste_guard is not None, (
        f"{rel}: the waste_terms batch is not gated on search-term availability")

    # And the failure names the reason, so the batch row is self-explaining.
    src = (_ROOT / rel).read_text(encoding="utf-8")
    assert "canonical search-term evidence unavailable" in src, rel


# ═════════════════════════════════════════════════════════════════════════════
# §2 — an unfinalized batch is not a covered interval
# ═════════════════════════════════════════════════════════════════════════════
def test_6_search_term_batch_finalization_failure_returns_not_ok(monkeypatch):
    fake = _UnfinalizableWriters()
    _install_fake_writers(monkeypatch, fake)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range",
                        lambda *a, **kw: _rows(3))

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is False
    assert st_sync.BATCH_FINALIZATION_FAILED in out["error"]
    assert out["batch_finalized"] is False


def test_7_keyword_batch_finalization_failure_returns_not_ok(monkeypatch):
    monkeypatch.setattr("db.writers.start_sync_batch", lambda **kw: 5)
    monkeypatch.setattr("db.writers.finish_sync_batch", lambda **kw: False)
    monkeypatch.setattr("db.writers.write_keyword_daily_facts",
                        lambda **kw: {"fetched": 4, "prepared": 4, "written": 4,
                                      "skipped_missing_identity": 0,
                                      "skipped_no_date": 0})
    monkeypatch.setattr(
        "connectors.google_ads_source.pull_keyword_performance_range",
        lambda *a, **kw: [{"date": "2026-02-10"}] * 4)

    out = kw_sync.sync_keyword_daily_facts(date(2026, 2, 1), date(2026, 2, 14),
                                           "daily")

    assert out["ok"] is False
    assert kw_sync.BATCH_FINALIZATION_FAILED in out["error"]
    assert out["batch_finalized"] is False


def test_8_verified_empty_is_withheld_when_its_proof_did_not_persist(monkeypatch):
    """Verified-empty that was not written down is not verified anything.

    The claim only means something because it is durable — every reader
    downstream works from the batch column, not from this return value.
    """
    fake = _UnfinalizableWriters()
    _install_fake_writers(monkeypatch, fake)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range",
                        lambda *a, **kw: [])

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is False
    assert out["verified_empty"] is False
    assert st_sync.BATCH_FINALIZATION_FAILED in out["error"]
    # The service still TRIED to record it — the claim was made and refused by
    # circumstance, not quietly skipped.
    assert fake.finished[-1]["verified_empty"] is True


def test_9_written_rows_with_a_failed_finalization_are_disclosed_not_certified(
        monkeypatch):
    """Both facts, separately: rows may be in the table, and this run certifies
    nothing. Reporting only the first would be a false green; reporting only the
    second would send someone hunting for data that is already there."""
    fake = _UnfinalizableWriters()
    _install_fake_writers(monkeypatch, fake)
    monkeypatch.setattr("connectors.google_ads_source.pull_search_terms_range",
                        lambda *a, **kw: _rows(6))

    out = st_sync.sync_search_terms(date(2026, 2, 1), date(2026, 2, 14), "daily")

    assert out["ok"] is False
    assert out["written"] == 0             # certified
    assert out["rows_possibly_written"] == 6   # disclosed
    assert out["latest_source_date"] is None   # coverage did NOT advance
    assert "not certified" in out["error"]

    # And the run report carries both, so `evidence_status` cannot read ready.
    from scheduler import incremental_sync as sync

    entry = sync._evidence_dataset_result(
        "search terms", out, requested_from="2026-02-01",
        requested_to="2026-02-14", errors=[])
    assert entry["status"] == "failed"
    assert entry["rows_written"] == 0
    assert entry["rows_possibly_written"] == 6
    assert entry["batch_finalized"] is False
    evidence = sync.build_evidence_block({
        "google_ads_api/keyword_facts": {"status": "success"},
        "google_ads_api/search_terms": entry})
    assert evidence["evidence_status"] != sync.EVIDENCE_READY


# ═════════════════════════════════════════════════════════════════════════════
# §3 — the account belongs in the natural key
# ═════════════════════════════════════════════════════════════════════════════
def test_11_index_writer_and_audit_share_one_key_including_the_account():
    """One key, spelled the same in three places.

    A conflict target that does not match a unique index is a runtime error; one
    that matches the WRONG index silently merges two accounts into one row; and
    an audit grouping on a narrower key reports correct rows as duplicates. The
    three have to agree, so the test reads all three.
    """
    schema = (_ROOT / "db" / "schema.py").read_text(encoding="utf-8")
    # The LAST definition wins: `db/schema.py` is executed top to bottom in one
    # statement, and PR-ADS-144's guarded migration also rebuilds this index
    # earlier in the script. Reading the first occurrence would test a
    # definition that is superseded before the script finishes.
    index = schema[schema.rindex("CREATE UNIQUE INDEX idx_search_terms_unique_fact"):]
    index = index[:index.index(");")]
    assert "COALESCE(customer_id" in index
    assert schema.rindex("CREATE UNIQUE INDEX idx_search_terms_unique_fact") > \
        schema.index("ADD COLUMN IF NOT EXISTS customer_id"), (
            "the index is rebuilt before the column it references is added")
    for column in ("source_date", "campaign_name", "campaign_id", "ad_group",
                   "keyword", "match_type", "search_term"):
        assert column in index, column

    writers = (_ROOT / "db" / "writers.py").read_text(encoding="utf-8")
    conflict = writers[writers.index("ON CONFLICT ("):]
    conflict = conflict[:conflict.index(") DO UPDATE SET")]
    assert "COALESCE(customer_id" in conflict

    assert "COALESCE(customer_id, '')" in audit._SEARCH_TERM_DUPLICATES

    # The migration is guarded on the DEFINITION, so a redeploy is a no-op
    # rather than a repeated index rebuild.
    assert "indexdef LIKE '%customer_id%'" in schema


@_needs_pg
def test_10_two_customers_identical_in_every_other_field_stay_distinct(pg):  # noqa: F811
    """The contract said customer identity distinguishes two accounts. Until F2
    the index disagreed, and the second write silently overwrote the first."""
    import db.writers as w

    day = date(2026, 3, 1)
    for customer in ("555", "666"):
        rows = _rows(1, day=day.isoformat(), term="same term",
                     customer_id=customer)
        assert w.write_search_terms(None, rows) == 1

    assert _scalar("SELECT COUNT(*) FROM search_terms") == 2
    assert _scalar("SELECT COUNT(DISTINCT customer_id) FROM search_terms") == 2

    # Re-writing the same observation upserts rather than duplicating, so the
    # wider key has not simply turned every write into an insert.
    w.write_search_terms(None, _rows(1, day=day.isoformat(), term="same term",
                                     customer_id="555"))
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 2

    # The audit agrees: the two rows are not a duplicate group.
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        facts = audit._search_term_data_facts(cur, day - timedelta(days=13), day)
    assert facts["current"]["row_count"] == 2
    assert facts["current"]["duplicate_natural_key_groups"] == 0


@_needs_pg
def test_11b_the_deployed_index_definition_carries_the_account(pg):  # noqa: F811
    """Read from the live catalog, not from the DDL string — the migration has
    to have actually run."""
    definition = _scalar("SELECT indexdef FROM pg_indexes "
                         "WHERE indexname = 'idx_search_terms_unique_fact'")
    assert definition, "the natural-key index is missing"
    assert "customer_id" in definition
    assert "UNIQUE" in definition

    # Re-running the schema is a no-op, and leaves exactly one such index.
    from db.schema import init_db

    init_db()
    assert _scalar("SELECT COUNT(*) FROM pg_indexes "
                   "WHERE indexname = 'idx_search_terms_unique_fact'") == 1


@_needs_pg
def test_12_historical_null_customer_ids_are_disclosed_not_invented(pg):  # noqa: F811
    """A back-filled guess would be inventing provenance. They stay NULL, they
    stay outside the certified population, and they are counted."""
    import db.writers as w

    day = date(2026, 3, 1)
    _exec("INSERT INTO search_terms (source_date, search_term, campaign_name, "
          "source_system) VALUES ('2024-05-05', 'legacy term', 'Old', 'windsor')")
    w.write_search_terms(None, _rows(2, day=day.isoformat()))

    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 1

    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        facts = audit._search_term_data_facts(cur, day - timedelta(days=13), day)

    assert facts["current"]["row_count"] == 2
    assert facts["current"]["rows_missing_identity"] == 0
    assert facts["historical"]["row_count"] == 1
    assert facts["historical"]["rows_missing_identity"] == 1
    assert {e["source_system"] for e in facts["historical"]["by_source_system"]} == {
        "windsor", "google_ads_api"}

    # Nothing wrote an account id onto the historical row.
    assert _scalar("SELECT customer_id FROM search_terms "
                   "WHERE search_term = 'legacy term'") is None


# ═════════════════════════════════════════════════════════════════════════════
# §4 — indirect legacy reads
# ═════════════════════════════════════════════════════════════════════════════
def _tree(tmp_path, rel: str, source: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_13_an_indirect_legacy_repository_read_is_detected(tmp_path):
    """The case the literal-SQL scan cleared.

    A service that imports a repository module and calls
    `repo.fetch_keyword_theme_snapshot(...)` contains no legacy SQL at all — the
    SQL is in the repository. Names are what cross a module boundary, so names
    are what the guard reads.
    """
    _tree(tmp_path, "services/rogue_evidence.py",
          "from db import revenue_repository as repo\n"
          "def themes():\n"
          "    return repo.fetch_keyword_theme_snapshot(30)\n")
    _tree(tmp_path, "services/also_rogue.py",
          "from connectors.windsor_pull import pull_search_terms\n"
          "def go():\n    return pull_search_terms()\n")
    _tree(tmp_path, "analysis/clean.py", "VALUE = 1\n")

    findings = guard.scan_legacy_sources(root=tmp_path)
    by_path = {f["path"]: f["reason"] for f in findings}

    assert by_path.get("services/rogue_evidence.py") == \
        guard.REASON_LEGACY_REPOSITORY_CALL
    assert by_path.get("services/also_rogue.py") == \
        guard.REASON_RETIRED_PROVIDER_IMPORT
    assert "analysis/clean.py" not in by_path

    # `analysis/` and `api/` are production paths too — a legacy read there
    # reaches the page by a different route, not a less real one.
    for directory in ("analysis", "api", "db"):
        assert directory in guard.PRODUCTION_DIRS


def test_14_an_old_json_snapshot_cannot_be_current_canonical_evidence(tmp_path):
    _tree(tmp_path, "analysis/rogue_analysis.py",
          "import json\n"
          "def load():\n"
          "    return json.load(open('data/ads_search_terms.json'))\n")
    _tree(tmp_path, "services/rogue_fallback.py",
          "def pick(search_terms, keywords):\n"
          "    return keywords if not search_terms else search_terms  "
          "# keywords_fallback\n")

    reasons = {f["path"]: f["reason"] for f in guard.scan_legacy_sources(root=tmp_path)}
    assert reasons.get("analysis/rogue_analysis.py") == \
        guard.REASON_LEGACY_SNAPSHOT_FILE
    assert reasons.get("services/rogue_fallback.py") == \
        guard.REASON_KEYWORD_FALLBACK

    # And the real waste-detection path no longer does either.
    core = guard.code_only(_ROOT / "analysis" / "core.py")
    assert "ads_search_terms.json" not in core
    assert "keywords_fallback" not in core


def test_13b_the_real_repository_is_clean_and_the_audit_emits_the_code(monkeypatch):
    """The live assertion, and the proof that a finding reaches the CLI."""
    assert guard.scan_legacy_sources() == []

    monkeypatch.setattr(audit, "scan_legacy_sources",
                        lambda *a, **kw: [{"path": "services/rogue.py",
                                           "reason": guard.REASON_LEGACY_REPOSITORY_CALL,
                                           "detail": "rogue calls a legacy helper"}])
    violations = audit._legacy_source_violations()
    assert [v["code"] for v in violations] == [audit.V_LEGACY_SOURCE_ACTIVE]

    for rel, reason in guard.LEGACY_ACCESS_ALLOWLIST.items():
        assert (_ROOT / rel).exists(), f"{rel} is allowlisted but does not exist"
        assert len(reason) > 10, rel


# ═════════════════════════════════════════════════════════════════════════════
# §5 — the audit answers on the account's calendar or not at all
# ═════════════════════════════════════════════════════════════════════════════
def test_15_account_calendar_failure_produces_a_controlled_unavailable(monkeypatch):
    """Freshness is a comparison against the ACCOUNT's today.

    Around midnight in British Summer Time the account day and the UTC day
    differ, so substituting one for the other moves the staleness boundary by a
    full day — silently. An audit that refuses to answer is visible; a wrong
    date is not.
    """
    def _boom(*a, **kw):
        raise RuntimeError("tzdata unavailable")

    monkeypatch.setattr("analysis.account_time.account_today", _boom)

    report = audit.run_audit()

    assert report["ok"] is False
    assert report["violation_codes"] == [audit.V_ACCOUNT_CALENDAR_UNRESOLVED]
    assert report["datasets"] == []
    detail = report["violations"][0]["detail"]
    assert "will not" in detail and "UTC" in detail

    # A controlled nonzero exit, not an exception.
    assert report["database_available"] is False

    # Neither mode raises on it.
    audit._print_human(report)


def test_15b_no_utc_substitute_survives_in_the_audit():
    """Checked in the AST: the old code caught any resolution error and used
    `(now or datetime.now(utc)).date()`."""
    tree = ast.parse(
        (_ROOT / "scripts" / "audit_keyword_search_term_freshness.py").read_text(
            encoding="utf-8"))
    run_audit = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "run_audit")
    dumped = ast.dump(run_audit)
    # `today` is resolved by the shared helper and nowhere else.
    assert "_account_calendar" in dumped
    assert "resolve_canonical_window" not in dumped

    calendar = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_account_calendar")
    # It reports a reason; it never returns a substitute date.
    assert "account_today" in ast.dump(calendar)
    assert "utcnow" not in ast.dump(calendar)


def test_15c_the_cli_exits_two_on_an_unresolvable_calendar():
    """End to end. `ACCOUNT_TZ` is patched to a name no tz database contains, so
    the resolver genuinely cannot answer."""
    res = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.');\n"
         "import analysis.account_time as at;\n"
         "at.ACCOUNT_TZ = 'Not/AZone';\n"
         "from scripts import audit_keyword_search_term_freshness as a;\n"
         "sys.exit(a.main())"],
        cwd=str(_ROOT), capture_output=True, text=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root",
             "PYTHONPATH": str(_ROOT), "DATABASE_URL": ""})
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Traceback" not in res.stderr


# ═════════════════════════════════════════════════════════════════════════════
# 16 — the F1 suite is still a gate, and still green
# ═════════════════════════════════════════════════════════════════════════════
def test_16_the_156_and_f1_suites_remain_collected_and_gated():
    """F2 changes a shared writer, the schema's natural key and the audit's
    calendar. The suites that would notice are named in the blocking CI step, so
    "still green" is enforced by the gate rather than asserted here."""
    workflow = (_ROOT / ".github" / "workflows" / "pr-ads-153d-checks.yml").read_text(
        encoding="utf-8")
    for suite in ("test_pr_ads_156_evidence_freshness",
                  "test_pr_ads_156_f1_freshness_truth",
                  "test_pr_ads_156_f2_stale_analysis_and_durability",
                  "test_rule_advisor_doctrine",
                  "test_pr_ads_144_pg_integration"):
        assert suite in workflow, suite

    # And the earlier modules still hold the cases they claim to — a suite named
    # in the workflow but emptied of tests is a gate that passes on nothing.
    # Counted as test FUNCTIONS; the collected case count is higher because
    # several are parametrized.
    for module, minimum in (("test_pr_ads_156_f1_freshness_truth", 24),
                            ("test_pr_ads_156_evidence_freshness", 28)):
        tree = ast.parse((_ROOT / "tests" / f"{module}.py").read_text(
            encoding="utf-8"))
        cases = [n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
        assert len(cases) >= minimum, (module, len(cases))


# ── PostgreSQL helpers ───────────────────────────────────────────────────────
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
