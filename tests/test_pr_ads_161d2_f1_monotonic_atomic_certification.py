"""
tests/test_pr_ads_161d2_f1_monotonic_atomic_certification.py

PR-ADS-161D2-F1 — monotonic and atomic customer certification.

§1  run state is ordered by run START, never commit order (PG)
§2  the coverage audit fails unless the complete scan covers the CURRENT won
    population, set for set (PG)
§3  backfill completion is proven inside its own transaction, with the deal
    ledger locked against writes (PG, including a real concurrent writer)
§4  CRM findings report exact counts separately from capped samples
§5  CI wiring

Every guard here was shown red against the code before its fix; see the PR.
"""

from __future__ import annotations

import itertools
import threading
import time
from datetime import timedelta

import psycopg2
import pytest

import tests.conftest as conftest  # noqa: F401  (import-order guard)
import services.canonical_customer_revenue_service as svc
from tests.canonical_ledger_fixtures import ledger_row
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: F401
    _have_postgres, pg,
)
from tests.test_pr_ads_161d2_deal_company_associations import (  # noqa: F401
    NOW, Q4, _ROOT, _coverage_audit, _deal, _fake_reads, _ok_obs, _q, _t,
    _universe, _won_ids, ledger,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")


def _state():
    from db import deal_company_association_repository as repo
    return repo.fetch_sync_state()["row"]


def _run(status, minutes, *, error=None):
    """Record a FULL-population run that started at ``_t(minutes)``."""
    from db import deal_company_association_repository as repo
    assert repo.record_sync_run(
        mode="incremental", status=status, started_at=_t(minutes),
        complete=status == "success", full_population=True,
        error=error)["available"]


_RUN_FIELDS = ("last_run_status", "last_run_started_at", "last_error",
               "last_full_run_status", "last_full_run_started_at",
               "last_full_run_complete")


# ═════════════════════════════════════════════════════════════════════════════
# §1 — run state is monotonic by run start
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_01_an_older_success_finishing_late_cannot_hide_a_newer_failure(ledger):
    _run("failed", 10, error="newer scan failed")
    _run("success", 5)                       # older run, commits second
    s = _state()
    assert (s["last_run_status"], s["last_full_run_status"],
            s["last_full_run_complete"]) == ("failed", "failed", False)
    assert svc._parse(s["last_full_run_started_at"]) == _t(10)
    assert s["last_error"] == "newer scan failed"
    # So the customer gate stays shut.
    assert svc.association_scan_current(s, _t(11), 48)["current"] is False


@_needs_pg
def test_02_an_older_failure_finishing_late_cannot_replace_a_newer_success(
        ledger):
    _run("success", 10)
    _run("failed", 5, error="older scan failed")   # older run, commits second
    s = _state()
    assert (s["last_run_status"], s["last_full_run_status"],
            s["last_full_run_complete"]) == ("success", "success", True)
    assert svc._parse(s["last_run_started_at"]) == _t(10)
    assert s["last_error"] is None


_RUNS = (("success", 5, None), ("partial", 7, "partial"),
         ("failed", 10, "newest"))


@_needs_pg
@pytest.mark.parametrize("order", list(itertools.permutations(_RUNS)),
                         ids=lambda o: "-".join(r[0] for r in o))
def test_03_commit_order_never_decides_which_run_is_latest(ledger, order):
    for status, minutes, error in order:
        _run(status, minutes, error=error)
    s = _state()
    assert {k: s[k] for k in _RUN_FIELDS} == {
        "last_run_status": "failed",
        "last_run_started_at": _t(10).isoformat(),
        "last_error": "newest",
        "last_full_run_status": "failed",
        "last_full_run_started_at": _t(10).isoformat(),
        "last_full_run_complete": False,
    }


# ═════════════════════════════════════════════════════════════════════════════
# §2 — the complete scan must cover the CURRENT won population
# ═════════════════════════════════════════════════════════════════════════════

def _scan(monkeypatch, mapping):
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _fake_reads(monkeypatch, mapping)
    out = sync_deal_company_associations(mode="incremental")
    assert out["complete"], out
    return out


@_needs_pg
def test_10_a_complete_scan_records_the_exact_set_it_certified(
        ledger, monkeypatch):
    for d in ("1", "2"):
        _deal(ledger, d)
    _deal(ledger, "9", hs_is_closed_won=False, deal_stage_id="x")
    _scan(monkeypatch, {"1": [901], "2": [902]})
    s = _state()
    assert sorted(s["last_complete_scan_deal_ids"]) == ["1", "2"]
    assert s["last_complete_scan_population"] == 2
    code, out = _coverage_audit()
    assert code == 0, out["violations"]
    assert out["checks"]["complete_scan_covers_current_won_population"] is True


@_needs_pg
def test_11_a_won_deal_with_no_successful_observation_fails_the_audit(
        ledger, monkeypatch):
    for d in ("1", "2"):
        _deal(ledger, d)
    _scan(monkeypatch, {"1": [901], "2": [902]})
    _deal(ledger, "3")                       # wins after the scan, never read
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["complete_scan_covers_current_won_population"] is False
    cov = out["coverage"]
    assert cov["won_deals_without_successful_observation"] == 1
    assert cov["won_deals_not_certified_by_complete_scan"] == 1
    assert cov["coverage_gap_deal_ids_sample"] == ["3"]


@_needs_pg
def test_12_a_won_deal_observed_only_before_the_claimed_scan_fails_the_audit(
        ledger, monkeypatch):
    from db import deal_company_association_repository as repo
    for d in ("1", "2"):
        _deal(ledger, d)
    _scan(monkeypatch, {"1": [901], "2": [902]})
    # A later run CLAIMS a complete scan of both deals, but deal 2's only
    # successful observation predates it.
    later = repo.db_now()["now"] + timedelta(minutes=5)
    assert repo.record_observation_success(
        "1", [{"company_id": "901", "association_category": "HUBSPOT_DEFINED",
               "association_type_id": "5", "association_label": None,
               "association_path_key": "HUBSPOT_DEFINED:5"}],
        observation_started_at=later + timedelta(seconds=1))["applied"]
    assert repo.record_sync_run(
        mode="incremental", status="success", started_at=later,
        population=2, complete=True, full_population=True,
        scanned_deal_ids=["1", "2"])["available"]
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["complete_scan_covers_current_won_population"] is False
    assert out["coverage"]["won_deals_behind_complete_scan"] == 1
    assert out["coverage"]["coverage_gap_deal_ids_sample"] == ["2"]


@_needs_pg
def test_13_a_deal_that_won_after_the_scan_fails_even_once_observed(
        ledger, monkeypatch):
    """The pre-F1 check passed this: deal 3 HAS a success after the scan's
    start (a subset pass read it), so nothing was 'behind' — yet the scan never
    certified it. Only the exact-set proof sees the population moved."""
    from db import deal_company_association_repository as repo
    for d in ("1", "2"):
        _deal(ledger, d)
    _scan(monkeypatch, {"1": [901], "2": [902]})
    _deal(ledger, "3")
    _fake_reads(monkeypatch, {"3": [903]})
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    subset = sync_deal_company_associations(mode="backfill", deal_ids=["3"])
    assert subset["complete"] and not subset["full_population"]
    assert sorted(_state()["last_complete_scan_deal_ids"]) == ["1", "2"], \
        "a subset pass must never certify a scan"
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["complete_scan_covers_current_won_population"] is False
    cov = out["coverage"]
    assert (cov["won_deals_without_successful_observation"],
            cov["won_deals_behind_complete_scan"],
            cov["won_deals_not_certified_by_complete_scan"]) == (0, 0, 1)
    # The next full scan certifies the new population and the audit holds.
    _scan(monkeypatch, {"1": [901], "2": [902], "3": [903]})
    assert repo.fetch_sync_state()["row"]["last_complete_scan_population"] == 3
    assert _coverage_audit()[0] == 0


@_needs_pg
def test_14_a_certified_deal_that_is_no_longer_won_fails_the_audit(
        ledger, monkeypatch):
    for d in ("1", "2"):
        _deal(ledger, d)
    _scan(monkeypatch, {"1": [901], "2": [902]})
    _deal(ledger, "2", hs_is_closed_won=False, deal_stage_id="x")
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["complete_scan_covers_current_won_population"] is False
    assert out["coverage"]["certified_deals_no_longer_won"] == 1


@_needs_pg
def test_15_a_recorded_population_that_disagrees_with_its_set_fails(
        ledger, monkeypatch):
    from db.connection import get_conn
    _deal(ledger, "1")
    _scan(monkeypatch, {"1": [901]})
    assert _coverage_audit()[0] == 0
    with get_conn() as c, c.cursor() as cur:
        cur.execute("UPDATE hubspot_deal_company_sync_state "
                    "SET last_complete_scan_population = 5")
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["complete_scan_covers_current_won_population"] is False


@_needs_pg
def test_16_a_completeness_claim_that_names_no_deals_proves_nothing(ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    assert repo.record_sync_run(
        mode="incremental", status="success", started_at=_t(0),
        population=1, complete=True, full_population=True)["available"]
    s = _state()
    assert s["last_complete_scan_started_at"] is None
    assert s["last_complete_scan_deal_ids"] is None
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["complete_scan_covers_current_won_population"] is False


# ═════════════════════════════════════════════════════════════════════════════
# §3 — backfill completion is proven inside its own transaction
# ═════════════════════════════════════════════════════════════════════════════

def _generation_with_every_deal_observed(deals):
    from db import deal_company_association_repository as repo
    gen = repo.db_now()["now"]
    assert repo.start_backfill_generation(gen)["available"]
    for d in deals:
        assert repo.record_observation_success(
            d, [], observation_started_at=repo.db_now()["now"])["applied"]
    progress = repo.fetch_backfill_progress(gen)
    assert progress["remaining"] == [], "precondition: progress says done"
    return gen


@_needs_pg
def test_20_completion_rereads_the_population_instead_of_trusting_progress(
        ledger):
    """A deal wins between the progress read and completion."""
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    _deal(ledger, "9", hs_is_closed_won=False, deal_stage_id="x")
    gen = _generation_with_every_deal_observed(["1"])
    _deal(ledger, "9")                                   # now won, unobserved
    done = repo.complete_backfill_generation(gen)
    assert done["available"] and done["completed"] is False
    assert (done["reason"], done["population"], done["remaining"]) == \
        ("won_deals_lack_observation_in_generation", 2, 1)
    s = _state()
    assert s["backfill_status"] == "in_progress"
    assert s["last_complete_scan_started_at"] is None


@_needs_pg
def test_21_completion_waits_out_a_concurrent_ledger_write_and_sees_it(
        ledger, pg):
    """A real concurrent writer: deal 9 is being made won in an open
    transaction when completion starts. Completion must block on the ledger
    lock, see the committed change, and refuse — not race past it."""
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    _deal(ledger, "9", hs_is_closed_won=False, deal_stage_id="x")
    gen = _generation_with_every_deal_observed(["1"])

    writer = psycopg2.connect(pg.url)
    try:
        with writer.cursor() as cur:
            cur.execute("UPDATE hubspot_deal_ledger SET hs_is_closed_won = TRUE "
                        "WHERE deal_id = '9'")          # uncommitted
        result = {}
        t = threading.Thread(
            target=lambda: result.update(repo.complete_backfill_generation(gen)))
        t.start()
        time.sleep(1.0)
        assert t.is_alive(), \
            "completion did not wait for the in-flight ledger write"
        writer.commit()
        t.join(timeout=30)
        assert not t.is_alive()
    finally:
        writer.close()
    assert result["completed"] is False
    assert result["remaining"] == 1
    assert _state()["backfill_status"] == "in_progress"


@_needs_pg
def test_22_completion_derives_the_certified_set_from_the_database(ledger):
    from db import deal_company_association_repository as repo
    for d in ("1", "2"):
        _deal(ledger, d)
    gen = _generation_with_every_deal_observed(["1", "2"])
    done = repo.complete_backfill_generation(gen)
    assert (done["completed"], done["population"]) == (True, 2)
    s = _state()
    assert sorted(s["last_complete_scan_deal_ids"]) == ["1", "2"]
    assert s["last_complete_scan_population"] == 2
    import inspect
    params = inspect.signature(repo.complete_backfill_generation).parameters
    assert list(params) == ["generation_started_at"], \
        "completion must not accept a caller-provided population"


@_needs_pg
def test_23_the_backfill_command_reports_a_refused_completion(
        ledger, monkeypatch):
    from db import deal_company_association_repository as repo
    from tests.test_pr_ads_161d2_deal_company_associations import _backfill
    _deal(ledger, "1")
    _deal(ledger, "9", hs_is_closed_won=False, deal_stage_id="x")
    _fake_reads(monkeypatch, {"1": [901], "9": [909]})
    real = repo.complete_backfill_generation

    def deal_wins_first(gen):
        _deal(ledger, "9")                  # wins after the progress read
        return real(gen)

    monkeypatch.setattr(repo, "complete_backfill_generation", deal_wins_first)
    code, out = _backfill(["--apply"])
    assert code == 1
    assert out["reason"].startswith("completion refused: 1 won deal(s)")
    assert out["completion"]["completed"] is False
    monkeypatch.setattr(repo, "complete_backfill_generation", real)
    code, out = _backfill(["--resume", "--apply"])
    assert code == 0 and out["reason"] == "backfill_generation_complete"
    assert sorted(_state()["last_complete_scan_deal_ids"]) == ["1", "9"]


# ═════════════════════════════════════════════════════════════════════════════
# §4 — exact counts, capped samples
# ═════════════════════════════════════════════════════════════════════════════

def test_30_crm_finding_counts_are_exact_beyond_the_sample_cap(monkeypatch):
    from scripts import audit_deal_company_association_coverage as audit
    from services import canonical_revenue_service as crs
    no_company = [str(1000 + i) for i in range(130)]
    several = [str(5000 + i) for i in range(105)]
    rows = [ledger_row(d, deal_close_date=Q4) for d in no_company + several]
    obs = ([_ok_obs(d, 0, 0) for d in no_company]
           + [_ok_obs(d, 2, 2) for d in several])
    paths = [{"deal_id": d, "company_id": c, "association_category":
              "HUBSPOT_DEFINED", "association_type_id": "5",
              "association_path_key": "HUBSPOT_DEFINED:5"}
             for d in several for c in ("901", "902")]
    monkeypatch.setattr(crs, "load_closed_won_universe",
                        lambda *_a, **_k: _universe(rows, obs, paths))
    _, report = audit.run(now=NOW)
    crm = report["crm_findings"]
    assert crm["no_company_count"] == 130
    assert crm["multiple_company_count"] == 105
    assert len(crm["no_company_deal_ids_sample"]) == 100
    assert len(crm["multiple_company_deal_ids_sample"]) == 100
    assert crm["sample_limit"] == 100
    assert not any(k.endswith("_deal_ids") for k in crm), \
        "an id list must be named a sample"


# ═════════════════════════════════════════════════════════════════════════════
# §5 — CI
# ═════════════════════════════════════════════════════════════════════════════

def test_40_ci_runs_this_suite_in_the_postgresql_step_and_asserts_it_ran():
    wf = (_ROOT / ".github/workflows/pr-ads-153d-checks.yml").read_text(
        encoding="utf-8")
    assert "tests/test_pr_ads_161d2_f1_monotonic_atomic_certification.py" in wf
    assert '"tests.test_pr_ads_161d2_f1_monotonic_atomic_certification"' in wf
