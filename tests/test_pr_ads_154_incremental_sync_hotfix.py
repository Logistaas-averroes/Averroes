"""
PR-ADS-154 — production incremental-sync hotfix.

What went wrong in production
─────────────────────────────
The historical geo bootstrap succeeded (32 chunks verified, 34,068 rows, exit
0). The very next command, ``python -m scheduler.incremental_sync``, reported
``partial`` with nine dataset failures, repeated ``database_unavailable``,
Windsor jobs still executing — and exited **0**.

The cause was one line that was never written. The CLI entry point called
``run_daily_incremental_sync()`` without ``db.connection.init_pool()``, so a
fresh process began with ``_pool = None``. Every persistence call received
``conn is None`` and degraded quietly to a no-op: HubSpot rows were pulled and
none written, the deal ledger reported ``database_unavailable``, canonical spend
wrote nothing, the geo lease store was unreachable. The run spent real Google
Ads and HubSpot quota to produce nothing, then told the operator it was fine.

Each test below is a statement about one of those defects being structurally
impossible now:

  §1  the pool is initialized AND probed before any external connector is called
  §2  Windsor is gone from active orchestration
  §3  Google Ads work uses the ONE canonical platform-evidence source
  §4  every active (source, dataset) pair is registered
  §5  the CLI exit code agrees with the reported status
  §6  no dataset can report success after a persistence failure

Deterministic and synthetic: no production identifiers, no network. The two
process-level cases spawn a real subprocess with no ``DATABASE_URL``, because
"a fresh process initializes its own pool" is a claim about process startup that
an in-process test cannot make.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import scheduler.incremental_sync as sync  # noqa: E402
from services import dataset_keys  # noqa: E402

_SYNC_SRC = (_ROOT / "scheduler" / "incremental_sync.py").read_text()


def _run_module(env_overrides: dict, code: str | None = None) -> subprocess.CompletedProcess:
    """Run a child Python process with a controlled environment."""
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.update(env_overrides)
    env["PYTHONPATH"] = str(_ROOT)
    args = ([sys.executable, "-c", code] if code
            else [sys.executable, "-m", "scheduler.incremental_sync"])
    return subprocess.run(args, cwd=_ROOT, env=env, capture_output=True,
                          text=True, timeout=180)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the pool is initialized and PROVEN before any external pull
# ═════════════════════════════════════════════════════════════════════════════

def test_1_a_fresh_cli_process_initializes_the_database_pool():
    """A standalone process is not the Flask app and must set up its own pool.

    Asserted in a real subprocess: the claim is about what a NEW interpreter
    does at startup, and an in-process test inherits a pool the test session
    already built. The child confirms `_pool is None` on entry — the exact
    production starting state — and that the readiness path calls `init_pool`.
    """
    code = """
import db.connection as conn
assert conn._pool is None, "a fresh process must start with no pool"

calls = []
_real = conn.init_pool
def _spy():
    calls.append(1)
    return _real()
conn.init_pool = _spy

import scheduler.incremental_sync as sync
ready, detail = sync.ensure_database_ready()
assert calls, "ensure_database_ready must call init_pool"
assert ready is False, "with no DATABASE_URL the probe must fail"
assert detail, "a failed readiness check must say why"
print("OK")
"""
    proc = _run_module({}, code=code)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_1b_readiness_requires_a_real_query_not_just_a_pool(monkeypatch):
    """`init_pool()` succeeding is not evidence that the database answers.

    It swallows its own failure and leaves `_pool = None`, and even a pool that
    exists can front an unreachable server. The probe is `SELECT 1`.
    """
    import db.connection as conn

    monkeypatch.setattr(conn, "init_pool", lambda: None)

    class _NullCtx:
        def __enter__(self): return None
        def __exit__(self, *a): return False

    monkeypatch.setattr(conn, "get_conn", lambda: _NullCtx())
    ready, detail = sync.ensure_database_ready()
    assert ready is False
    assert "pool is not available" in detail

    # A connection whose probe raises is also not ready.
    class _BoomCursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): raise RuntimeError("server closed the connection")

    class _BoomConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _BoomCursor()

    monkeypatch.setattr(conn, "get_conn", lambda: _BoomConn())
    ready, detail = sync.ensure_database_ready()
    assert ready is False
    assert "probe failed" in detail


def test_2_database_unavailability_fails_before_any_external_connector(monkeypatch):
    """The decisive test: ZERO external calls when persistence is unavailable.

    A pull with no database is quota spent to produce nothing, and the resulting
    per-dataset report describes work whose results were discarded — which is
    precisely what production saw.
    """
    calls: list[str] = []

    import connectors.hubspot_pull as hubspot
    for name in ("pull_paid_search_contacts_in_range", "pull_all_contacts_in_range",
                 "pull_closed_won_deals_in_range"):
        monkeypatch.setattr(hubspot, name,
                            lambda *a, _n=name, **k: calls.append(_n) or [])
    import services.google_ads_spend_service as spend
    monkeypatch.setattr(spend, "fetch_daily_spend",
                        lambda *a, **k: calls.append("google_ads") or {"rows": []})

    monkeypatch.setattr(sync, "ensure_database_ready",
                        lambda: (False, "connection pool is not available"))
    # write_run must not even be attempted — the abort comes first.
    monkeypatch.setattr(sync.db_writers, "write_run",
                        lambda *a, **k: calls.append("write_run") or 1)

    out = sync.run_daily_incremental_sync(run_reason="test")

    assert calls == [], f"external work was attempted with no database: {calls}"
    assert out["status"] == "failed"
    assert out["reason"] == sync.DB_UNAVAILABLE_REASON
    assert out["datasets"] == {}, "nothing ran, so no dataset may claim a status"
    assert out["run_id"] is None
    assert out["errors"]


def test_2b_a_missing_run_record_is_also_a_database_failure(monkeypatch):
    """No run row means nothing written can be attributed to this run."""
    monkeypatch.setattr(sync, "ensure_database_ready", lambda: (True, None))
    monkeypatch.setattr(sync.db_writers, "write_run", lambda *a, **k: 0)

    out = sync.run_daily_incremental_sync(run_reason="test")
    assert out["status"] == "failed"
    assert out["reason"] == sync.DB_UNAVAILABLE_REASON
    assert out["datasets"] == {}


def test_2c_readiness_is_checked_before_anything_else_in_the_run():
    """Asserted on the AST: the gate is the FIRST call, not merely present.

    A readiness check placed after the first pull would be a check that runs
    too late to prevent anything.
    """
    tree = ast.parse(_SYNC_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_daily_incremental_sync")
    called: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name:
                called.append(name)
    assert "ensure_database_ready" in called
    assert called.index("ensure_database_ready") < called.index("write_run"), (
        "the readiness gate must precede the first durable write")


# ═════════════════════════════════════════════════════════════════════════════
# §2 — Windsor is gone from active orchestration
# ═════════════════════════════════════════════════════════════════════════════

def test_3_windsor_connectors_are_never_invoked_by_incremental_sync():
    """Asserted on the AST, not on the word "windsor".

    The module still NAMES Windsor — in the retired-dataset records, which is
    the point: a dataset that silently disappears from the report is
    indistinguishable from one that was forgotten. What must be gone is the
    calls and the imports.
    """
    tree = ast.parse(_SYNC_SRC)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and "windsor" in (node.module or ""):
            imported.append(node.module)
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names if "windsor" in a.name]
    assert imported == [], f"incremental sync still imports Windsor: {imported}"

    called = {getattr(n.func, "id", getattr(n.func, "attr", "")) or ""
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    windsor_calls = {c for c in called if "windsor" in c.lower()}
    assert windsor_calls == set(), f"Windsor still called: {windsor_calls}"

    # And no Windsor sync helper survives.
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not {d for d in defined if "windsor" in d.lower()}


def test_3b_the_retired_datasets_are_recorded_not_silently_dropped(monkeypatch):
    """Each retired dataset says what replaced it, or that nothing did."""
    assert set(sync.RETIRED_DATASETS) == {
        "windsor/campaigns", "windsor/keywords", "windsor/geo", "windsor/search_terms"}
    for name, record in sync.RETIRED_DATASETS.items():
        assert record["status"] == "retired", name
        assert record["note"], name
        # `replaced_by` is present for every entry — None is an explicit answer
        # ("no canonical replacement"), not a missing one.
        assert "replaced_by" in record, name

    # keywords has NO canonical replacement, and says so rather than pretending.
    assert sync.RETIRED_DATASETS["windsor/keywords"]["replaced_by"] is None
    assert "no canonical" in sync.RETIRED_DATASETS["windsor/keywords"]["note"].lower()


def test_3c_retired_never_counts_as_success_or_failure():
    """A dataset that never runs cannot vote on whether the run worked."""
    assert "retired" in sync.NON_VOTING_STATUSES
    assert "skipped" in sync.NON_VOTING_STATUSES
    only_retired = {k: dict(v) for k, v in sync.RETIRED_DATASETS.items()}
    assert sync._overall_status(only_retired) == "success"
    # ...but it must not mask a real failure either.
    mixed = dict(only_retired, real={"status": "failed"})
    assert sync._overall_status(mixed) == "failed"


def test_3d_an_unrecognised_dataset_status_counts_as_a_failure():
    """Defaulting the unknown case to "fine" is how a new outcome string
    silently turns a broken run green."""
    assert sync._overall_status({"a": {"status": "success"},
                                 "b": {"status": "who_knows"}}) == "partial"
    assert sync._overall_status({"b": {"status": None}}) == "failed"


# ═════════════════════════════════════════════════════════════════════════════
# §3 / §4 — one canonical platform source; every active pair is registered
# ═════════════════════════════════════════════════════════════════════════════

def test_4_active_google_ads_work_uses_the_canonical_platform_source():
    """`google_ads` and `google_ads_api` named one source; keeping both is what
    let the writer and the freshness config drift until neither matched."""
    assert dataset_keys.PLATFORM_EVIDENCE_SOURCE == "google_ads_api"
    assert dataset_keys.CANONICAL_SPEND_SOURCE == "google_ads_api"
    assert dataset_keys.CANONICAL_GEO_SOURCE == "google_ads_api"

    # The superseded spelling is NORMALIZED, not merely banned: production rows
    # already carry it, and dropping the spelling would orphan that history —
    # the same defect wearing the opposite mask.
    assert dataset_keys.canonical_source("google_ads") == "google_ads_api"
    assert dataset_keys.canonical_source("GOOGLE_ADS ") == "google_ads_api"
    assert dataset_keys.canonical_source("google_ads_api") == "google_ads_api"

    # The scheduler stamps the canonical key, never a literal.
    assert 'source="google_ads"' not in _SYNC_SRC


def test_4b_the_writer_canonicalizes_before_stamping(monkeypatch):
    """One key is written, whichever spelling the caller passed."""
    import db.writers as w

    seen: list[tuple] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            if params and "INSERT INTO sync_batches" in sql:
                seen.append(params)
        def fetchone(self): return (42,)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    monkeypatch.setattr(w, "get_conn", lambda: _Conn())
    w.start_sync_batch("google_ads", "canonical_spend", "daily")
    assert seen and seen[0][1] == "google_ads_api"


def test_5_every_active_scheduler_pair_is_registered(caplog):
    """The contract test the production run needed.

    Seven pairs logged "unknown source"/"unknown dataset". The warning was the
    least of it: an unregistered pair is a key the freshness configuration does
    not read, so the dataset reports "never run" forever while its table fills
    up normally. Nothing fails; nothing shows.
    """
    assert sync.ACTIVE_SYNC_PAIRS, "the scheduler must declare its pairs"
    unregistered = [(s, d) for s, d in sync.ACTIVE_SYNC_PAIRS
                    if not dataset_keys.is_registered_pair(s, d)]
    assert unregistered == [], f"unregistered scheduler pairs: {unregistered}"

    # The exact pairs the production run complained about.
    for pair in (("google_ads", "canonical_spend"), ("fx", "daily_rates"),
                 ("hubspot", "deal_ledger"), ("hubspot", "source_classification"),
                 ("google_ads", "canonical_geo")):
        assert dataset_keys.is_registered_pair(*pair), pair


def test_5b_the_scheduler_pairs_match_what_it_actually_stamps():
    """A declared list that drifts from the calls proves nothing.

    Every `start_sync_batch` in this module is read off the AST and its
    source/dataset arguments matched against the declared set, so a new dataset
    cannot be added without appearing in the contract.
    """
    tree = ast.parse(_SYNC_SRC)
    declared = {(s, d) for s, d in sync.ACTIVE_SYNC_PAIRS}
    resolved = {
        "CANONICAL_SPEND_SOURCE": dataset_keys.CANONICAL_SPEND_SOURCE,
        "CANONICAL_SPEND_DATASET": dataset_keys.CANONICAL_SPEND_DATASET,
        "CANONICAL_GEO_SOURCE": dataset_keys.CANONICAL_GEO_SOURCE,
        "CANONICAL_GEO_DATASET": dataset_keys.CANONICAL_GEO_DATASET,
        "GEO_SYNC_SOURCE": dataset_keys.CANONICAL_GEO_SOURCE,
        "GEO_SYNC_DATASET": dataset_keys.CANONICAL_GEO_DATASET,
        "FX_SOURCE": dataset_keys.FX_SOURCE,
        "FX_DAILY_RATES_DATASET": dataset_keys.FX_DAILY_RATES_DATASET,
        "DEAL_LEDGER_SOURCE": dataset_keys.DEAL_LEDGER_SOURCE,
        "DEAL_LEDGER_DATASET": dataset_keys.DEAL_LEDGER_DATASET,
        "SOURCE_CLASSIFICATION_SOURCE": dataset_keys.SOURCE_CLASSIFICATION_SOURCE,
        "SOURCE_CLASSIFICATION_DATASET": dataset_keys.SOURCE_CLASSIFICATION_DATASET,
        "GCLID_SOURCE": dataset_keys.GCLID_SOURCE,
        "GCLID_MATCHES_DATASET": dataset_keys.GCLID_MATCHES_DATASET,
    }

    def _value(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return resolved.get(node.id)
        return None

    stamped = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "start_sync_batch"):
            continue
        kw = {k.arg: _value(k.value) for k in node.keywords}
        pair = (kw.get("source"), kw.get("dataset"))
        assert all(pair), f"unresolvable start_sync_batch keys: {kw}"
        stamped.add(pair)

    assert stamped == declared, (
        f"declared-vs-stamped mismatch — only stamped: {stamped - declared}; "
        f"only declared: {declared - stamped}")


def test_5c_the_registry_is_the_single_definition(caplog):
    """`db.writers` re-exports the registry rather than keeping a second copy."""
    import db.writers as w

    assert w.VALID_SYNC_SOURCES is dataset_keys.VALID_SYNC_SOURCES
    assert w.VALID_SYNC_DATASETS is dataset_keys.VALID_SYNC_DATASETS
    # The freshness config's keys come from the same module, so neither side
    # can move alone.
    from services.freshness_service import DATASET_FRESHNESS_CONFIG

    spend = DATASET_FRESHNESS_CONFIG["canonical_spend"]
    assert (spend["source"], spend["dataset"]) == (
        dataset_keys.CANONICAL_SPEND_SOURCE, dataset_keys.CANONICAL_SPEND_DATASET)
    geo = DATASET_FRESHNESS_CONFIG["canonical_geo"]
    assert (geo["source"], geo["dataset"]) == (
        dataset_keys.CANONICAL_GEO_SOURCE, dataset_keys.CANONICAL_GEO_DATASET)
    # gclid_attribution's writer key was `(hubspot, gclid_matches)` while this
    # config read `(gclid, matches)` — the same drift, found by the same audit.
    gclid = DATASET_FRESHNESS_CONFIG["gclid_attribution"]
    assert (gclid["source"], gclid["dataset"]) == (
        dataset_keys.GCLID_SOURCE, dataset_keys.GCLID_MATCHES_DATASET)


def test_5d_the_historical_source_spelling_is_relabelled_not_orphaned():
    """The migration folds `google_ads` onto the canonical key, idempotently.

    Writers canonicalize from now on, but production already holds rows under
    the old spelling — including the successful geo bootstrap's batches. Left
    behind, those datasets would report "never run" again.
    """
    ddl = (_ROOT / "db" / "schema.py").read_text()
    assert "UPDATE sync_state SET source = 'google_ads_api'" in ddl
    assert "UPDATE sync_batches SET source = 'google_ads_api'" in ddl
    # Collision-safe: sync_state is UNIQUE(source, dataset), so a dataset
    # holding both spellings must be resolved rather than crash the migration.
    assert "DELETE FROM sync_state stale" in ddl
    assert "last_successful_sync_at" in ddl
    # Evidence tables are NOT touched — the geo bootstrap's data is preserved.
    migration = ddl[ddl.index("fold the superseded `google_ads` source"):]
    migration = migration[:migration.index('"""')]
    for evidence in ("google_ads_geo_daily_spend", "google_ads_geo_coverage",
                     "google_ads_campaign_daily_spend"):
        assert f"UPDATE {evidence}" not in migration
        assert f"DELETE FROM {evidence}" not in migration


# ═════════════════════════════════════════════════════════════════════════════
# §5 — the CLI exit code agrees with the reported status
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status,expected", [
    ("success", 0),
    ("partial", 1),
    ("failed", 1),
])
def test_8_9_cli_exit_code_matches_the_reported_status(monkeypatch, capsys,
                                                       status, expected):
    """Exit 0 means, and only means, that every dataset that ran succeeded.

    The entry point previously fell off the end of the module at 0, so a run
    reporting `partial` with nine failures was indistinguishable to any caller
    from a clean one — and an operator reading `echo $?` was told it worked.
    """
    monkeypatch.setattr(sync, "run_daily_incremental_sync",
                        lambda **k: {"status": status, "datasets": {}})
    assert sync.main() == expected
    # The JSON is still printed, whatever the outcome.
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_8b_a_real_process_with_no_database_exits_nonzero():
    """End-to-end, in a real subprocess: the production scenario, inverted.

    Same command, same missing pool — but it now stops before touching Google
    Ads or HubSpot, says `database_unavailable`, and exits 1.
    """
    proc = _run_module({})
    assert proc.returncode != 0, (
        f"a run with no database must not exit 0\nstdout={proc.stdout}")
    payload = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert payload["status"] == "failed"
    assert payload["reason"] == "database_unavailable"
    assert payload["datasets"] == {}


def test_8c_the_entry_point_raises_systemexit_with_the_code():
    """`main()` returning a code is useless if `__main__` discards it."""
    tail = _SYNC_SRC[_SYNC_SRC.index('if __name__ == "__main__":'):]
    assert "raise SystemExit(main())" in tail
    body = _SYNC_SRC[_SYNC_SRC.index("def main()"):]
    body = body[:body.index('\nif __name__')]
    assert 'return 0 if result.get("status") == "success" else 1' in body


# ═════════════════════════════════════════════════════════════════════════════
# §6 — no dataset reports success after a persistence failure
# ═════════════════════════════════════════════════════════════════════════════

def _no_batch(monkeypatch):
    """Simulate `start_sync_batch` returning 0 (its DB-unavailable answer)."""
    monkeypatch.setattr(sync.db_writers, "start_sync_batch", lambda **k: 0)
    monkeypatch.setattr(sync.db_writers, "finish_sync_batch", lambda **k: True)


def test_6_pulled_positive_written_zero_is_a_failure(monkeypatch):
    """Preparing a row in memory is not evidence that anything was stored.

    `hubspot/source_classification` reported `success` with
    `contacts_classified` set to the number of rows PREPARED, so a run that
    classified 900 contacts and persisted none looked identical to one that
    persisted all of them.
    """
    import connectors.hubspot_pull as hubspot
    import services.source_attribution_service as attribution

    monkeypatch.setattr(hubspot, "pull_all_contacts_in_range",
                        lambda **k: [{"id": "1"}, {"id": "2"}])
    monkeypatch.setattr(hubspot, "pull_closed_won_deals_with_sources_in_range",
                        lambda **k: [])
    monkeypatch.setattr(attribution, "classify_contact_row", lambda c: dict(c))
    monkeypatch.setattr(attribution, "attribute_deal_row", lambda d: dict(d))
    monkeypatch.setattr(sync.db_writers, "start_sync_batch", lambda **k: 5)
    monkeypatch.setattr(sync.db_writers, "finish_sync_batch", lambda **k: True)
    monkeypatch.setattr(sync.db_writers, "upsert_deal_source_attribution", lambda r: 0)

    from datetime import date
    errors: list = []

    # Zero written from two pulled → failed.
    monkeypatch.setattr(sync.db_writers, "upsert_contact_source_classification",
                        lambda r: 0)
    out = sync._sync_source_classification(
        run_id=1, date_from=date(2026, 1, 1), date_to=date(2026, 1, 2), errors=errors)
    assert out["status"] == "failed"
    assert errors

    # Both written → success, and the reported figure is what LANDED.
    monkeypatch.setattr(sync.db_writers, "upsert_contact_source_classification",
                        lambda r: 2)
    out = sync._sync_source_classification(
        run_id=1, date_from=date(2026, 1, 1), date_to=date(2026, 1, 2), errors=[])
    assert out["status"] == "success"
    assert out["contacts_classified"] == 2      # persisted
    assert out["contacts_pulled"] == 2


def test_6b_a_batch_that_could_not_be_opened_is_a_failure(monkeypatch):
    """A dataset with no durable batch publishes no freshness.

    `start_sync_batch` returns 0 when the database is unavailable, and the code
    then skipped `finish_sync_batch` while still returning success — the row
    count was real, the evidence that anything was written was not.
    """
    from datetime import date

    _no_batch(monkeypatch)
    pulled: list = []
    import connectors.hubspot_pull as hubspot
    monkeypatch.setattr(hubspot, "pull_paid_search_contacts_in_range",
                        lambda **k: pulled.append(1) or [])

    out = sync._sync_hubspot_contacts(
        run_id=1, date_from=date(2026, 1, 1), date_to=date(2026, 1, 2), errors=[])
    assert out["status"] == "failed"
    assert "batch" in out["error"].lower()
    # It failed BEFORE the pull, not after: there is no point spending quota on
    # a result that cannot be recorded.
    assert pulled == []


def test_6c_fx_distinguishes_nothing_missing_from_nothing_persisted(monkeypatch):
    """Both arrive as rows_written == 0 with an empty `failed` list.

    `upsert_fx_rates` returns 0 on an unavailable database rather than raising,
    so the fetch loop records no failure. `fetched` separates them.
    """
    from datetime import date
    import services.fx_service as fx

    monkeypatch.setattr(sync.db_writers, "start_sync_batch", lambda **k: 5)
    monkeypatch.setattr(sync.db_writers, "finish_sync_batch", lambda **k: True)

    # Nothing was missing — a genuinely idle refresh.
    monkeypatch.setattr(fx, "ensure_fx_rates",
                        lambda *a, **k: {"rows_written": 0, "fetched": 0, "failed": []})
    out = sync._sync_fx_rates(run_id=1, date_to=date(2026, 1, 2), errors=[])
    assert out["status"] == "success"
    assert out["already_current"] is True

    # Seven rates fetched and none stored — a persistence failure wearing the
    # same zero.
    errors: list = []
    monkeypatch.setattr(fx, "ensure_fx_rates",
                        lambda *a, **k: {"rows_written": 0, "fetched": 7, "failed": []})
    out = sync._sync_fx_rates(run_id=1, date_to=date(2026, 1, 2), errors=errors)
    assert out["status"] == "failed"
    assert "persisted none" in out["error"]
    assert errors


def test_7_geo_reconciliation_unavailable_is_not_a_success(monkeypatch):
    """A comparison that could not be performed is not an answer.

    `mismatch` stays successful — the step ran and disagreed, which is a
    truthful result about the data. `unavailable` and `no_geo_data` mean the
    step could not run at all, and both used to return the same `success`.
    """
    import services.google_ads_geo_sync_service as geo

    for status in ("unavailable", "no_geo_data"):
        errors: list = []
        monkeypatch.setattr(geo, "build_geo_reconciliation",
                            lambda w, _s=status: {"status": _s, "reconciled": False,
                                                  "geo_gap_codes": ["geo_rows_unreadable"]})
        out = sync._publish_geo_reconciliation(errors=errors)
        assert out["status"] == "failed", status
        assert out["available"] is False
        assert errors, status

    # A performed comparison is a success whatever its verdict.
    for status, reconciled in (("reconciled", True), ("mismatch", False)):
        errors = []
        monkeypatch.setattr(geo, "build_geo_reconciliation",
                            lambda w, _s=status, _r=reconciled: {
                                "status": _s, "reconciled": _r, "geo_gap_codes": []})
        out = sync._publish_geo_reconciliation(errors=errors)
        assert out["status"] == "success", status
        assert out["available"] is True
        assert out["reconciled"] is reconciled
        assert errors == []


def test_7b_the_lease_store_outage_still_fails_the_geo_step():
    """PR-ADS-153F's distinction survives this refactor: `held` is benign,
    an unreachable lease store is a failure with a failed batch."""
    body = _SYNC_SRC[_SYNC_SRC.index("def _sync_canonical_geo"):]
    body = body[:body.index("\ndef ", 1)]
    assert 'lease_store_unavailable' in body
    assert '_batch("failed", error_message="lease_store_unavailable")' in body
    assert '"status": "skipped"' in body       # `held` stays benign


# ═════════════════════════════════════════════════════════════════════════════
# Governance — the hotfix stays read-only
# ═════════════════════════════════════════════════════════════════════════════

def test_governance_the_scheduler_reaches_no_external_mutation():
    body = "\n".join(line for line in _SYNC_SRC.splitlines()
                     if not line.strip().startswith("#"))
    for marker in ("requests.post", "requests.put", "requests.patch",
                   "requests.delete", "mutate_", "MutateOperation",
                   "upload_click_conversions", "OfflineUserDataJob"):
        assert marker not in body, f"incremental sync must stay read-only ({marker})"
