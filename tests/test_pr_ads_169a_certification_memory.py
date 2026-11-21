"""PR-ADS-169A — an audit is started only within the headroom that is free,
and is killed if it exceeds it.

Production (Render, 512Mi instance, idle ~297 MB): the daily certification's
audits were OOM-killed with the whole web server at 2026-11-19T07:39Z and
2026-11-20T07:44Z, and no ``platform_certification_runs`` row was written for
either day. On 2026-11-21 the ``campaign_evidence`` audit died with exit -9.

These cases pin the fix:

* every audit runs alone in a fresh interpreter. Its resident budget is
  derived when it starts (instance limit − instance usage − safety margin);
  below the floor it is not started and is ``crashed`` "insufficient memory
  headroom" — never skipped. The parent's watchdog kills it past the budget
  ("exceeded its memory budget (N MB resident)"). ``RLIMIT_AS`` is only a
  high backstop. Any of these is NOT CERTIFIED, and the parent survives;
* a ``MemoryError`` swallowed inside an audit (which then reports "holds") is
  not a pass when its peak lands within the guard band of the backstop;
* the run row exists from the start (``running``, already NOT CERTIFIED), the
  heartbeat keeps it live, and a ``running`` row whose heartbeat stopped is
  closed NOT CERTIFIED "heartbeat stopped; presumed interrupted" — at
  startup, again after the grace, and before the next run; the System page
  reads both honestly;
* the memory reductions (shared strings, chunked fetch, counts-only
  reconciliation, narrowed classification, one parse of api/server.py) leave
  every value and verdict unchanged.

The PostgreSQL cases must run in CI (PG step + did-run assertion), never skip.
"""
from __future__ import annotations

import ast
import json
import resource
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import audit_platform_certification as apc  # noqa: E402
from services import platform_certification_service as pcs  # noqa: E402

FAKE = "tests.pr_ads_169a_fake_audit"
FAKE_AUDITS = (("fake", FAKE, "a stand-in audit"),)
TINY_MB = 64


def _fake(monkeypatch, mode, log=None, sleep=None):
    monkeypatch.setenv("PR_ADS_169A_FAKE_MODE", mode)
    if log is not None:
        monkeypatch.setenv("PR_ADS_169A_FAKE_LOG", str(log))
    if sleep is not None:
        monkeypatch.setenv("PR_ADS_169A_FAKE_SLEEP", str(sleep))


def _only(report):
    assert len(report["audits"]) == 1
    return report["audits"][0]


def _readers(limit_mb, usage_mb):
    return {"limit": lambda settings: (limit_mb, "cgroup_v2"),
            "usage": lambda source: (usage_mb, source)}


# ── the budget: the watchdog, the derivation, the floor, the backstop ──────
def test_01_the_watchdog_kills_an_audit_whose_resident_memory_passes_its_budget(
        monkeypatch):
    _fake(monkeypatch, "touch")
    before = resource.getrlimit(resource.RLIMIT_AS)
    report = apc.run(audits=FAKE_AUDITS, memory_mb=TINY_MB, timeout=120)
    row = _only(report)
    assert row["outcome"] == apc.CRASHED, row
    assert row["exit_code"] == -9
    assert f"exceeded its memory budget ({TINY_MB} MB resident)" in row["detail"]
    assert "watchdog" in row["detail"]
    assert row["memory"]["watchdog_killed"] is True
    assert row["memory"]["killed_at_rss_mb"] > TINY_MB
    assert report["verdict"] == "NOT CERTIFIED"
    # The parent is alive (we are running) and its own limit never changed.
    assert resource.getrlimit(resource.RLIMIT_AS) == before
    # The same audit with room enough holds: the budget is what decided.
    assert _only(apc.run(audits=FAKE_AUDITS, memory_mb=1024,
                         timeout=120))["outcome"] == apc.HOLDS


def test_02_the_budget_is_derived_at_each_start_and_shrinks_as_the_instance_fills():
    settings = apc.load_settings()
    small = apc.derive_budget(settings, **{
        "limit_reader": lambda s: (512.0, "cgroup_v2"),
        "usage_reader": lambda src: (297.0, src)})
    smaller = apc.derive_budget(settings, **{
        "limit_reader": lambda s: (512.0, "cgroup_v2"),
        "usage_reader": lambda src: (350.0, src)})
    assert small["budget_mb"] == 512 - 297 - settings["safety_margin_mb"] == 151
    assert smaller["budget_mb"] == 98 < small["budget_mb"]
    assert (small["limit_mb"], small["limit_source"], small["usage_mb"],
            small["usage_source"]) == (512.0, "cgroup_v2", 297.0, "cgroup_v2")
    assert apc.derive_budget(settings, limit_reader=lambda s: (512.0, "x"),
                             usage_reader=lambda src: (None, "x"))["budget_mb"] is None


def test_03_the_derived_budget_and_its_inputs_are_recorded_per_audit(monkeypatch):
    _fake(monkeypatch, "hold")
    usage = iter([100.0, 300.0])
    readers = {"limit": lambda s: (512.0, "cgroup_v2"),
               "usage": lambda src: (next(usage), src)}
    report = apc.run(audits=(("a", FAKE, "x"), ("b", FAKE, "y")), readers=readers,
                     timeout=120)
    a, b = report["audits"]
    assert (a["outcome"], b["outcome"]) == (apc.HOLDS, apc.HOLDS)
    assert a["memory_budget_mb"] == 512 - 100 - 64
    assert b["memory_budget_mb"] == 512 - 300 - 64        # read again at its start
    assert b["memory"]["usage_mb"] == 300.0 and b["memory"]["limit_source"] == "cgroup_v2"
    assert 0 < a["peak_rss_mb"] <= a["vm_peak_mb"]


def test_04_below_the_headroom_floor_an_audit_is_crashed_not_skipped(monkeypatch, tmp_path):
    log = tmp_path / "ran.log"
    _fake(monkeypatch, "hold", log)
    report = apc.run(audits=FAKE_AUDITS, readers=_readers(512.0, 400.0), timeout=120)
    row = _only(report)
    assert row["outcome"] == apc.CRASHED
    assert row["detail"].startswith("insufficient memory headroom (48 MB available")
    assert report["verdict"] == "NOT CERTIFIED"
    assert not log.exists()                 # not started
    unknown = _only(apc.run(audits=FAKE_AUDITS, readers=_readers(512.0, None)))
    assert unknown["outcome"] == apc.CRASHED
    assert "insufficient memory headroom (unknown available" in unknown["detail"]


def test_05_the_address_space_backstop_still_applies(monkeypatch):
    _fake(monkeypatch, "allocate")         # reserves 256 MB fast, untouched
    row = _only(apc.run(audits=FAKE_AUDITS, memory_mb=1024, address_space_mb=TINY_MB,
                        timeout=120))
    assert row["outcome"] == apc.CRASHED, row
    assert f"exceeded its memory backstop ({TINY_MB} MB of address space)" in row["detail"]
    assert row["memory"]["memory_error"] is True


def test_06_a_memory_error_swallowed_inside_an_audit_is_not_a_pass(monkeypatch):
    """The audit catches the MemoryError and prints "holds" with exit 0 — the
    shape of every read wrapped in ``except Exception``. The guard band of the
    backstop refuses it."""
    _fake(monkeypatch, "swallow")
    report = apc.run(audits=FAKE_AUDITS, memory_mb=1024, address_space_mb=TINY_MB,
                     timeout=120)
    row = _only(report)
    assert row["outcome"] == apc.CRASHED, row
    assert f"reached its memory backstop ({TINY_MB} MB" in row["detail"]
    assert "not trusted" in row["detail"]
    assert report["verdict"] == "NOT CERTIFIED"


def test_07_sigkill_is_named_plainly_as_out_of_memory(monkeypatch):
    _fake(monkeypatch, "sigkill")
    row = _only(apc.run(audits=FAKE_AUDITS, memory_mb=192, timeout=120))
    assert row["outcome"] == apc.CRASHED
    assert row["exit_code"] == -9
    assert "SIGKILL" in row["detail"] and "out of memory" in row["detail"]
    assert "unrecognised exit code" not in row["detail"]


def test_08_audits_run_one_at_a_time_never_in_parallel(monkeypatch, tmp_path):
    log = tmp_path / "order.log"
    _fake(monkeypatch, "hold", log)
    three = tuple((f"fake{i}", FAKE, "x") for i in range(3))
    report = apc.run(audits=three, memory_mb=192, timeout=120)
    assert [r["outcome"] for r in report["audits"]] == [apc.HOLDS] * 3
    lines = [line.split() for line in log.read_text().splitlines()]
    assert [w for w, _, _ in lines] == ["start", "end"] * 3
    pids = [p for _, p, _ in lines]
    assert pids[0::2] == pids[1::2] and len(set(pids)) == 3
    times = [float(t) for _, _, t in lines]
    assert times == sorted(times)


def test_09_each_audit_is_a_fresh_interpreter_without_preexec_fn(monkeypatch):
    """No ``preexec_fn``: it would fork() the 300 MB multi-threaded server
    and run Python in the child before exec (unsafe with threads, and it
    turns off the vfork path). The child sets its own backstop."""
    seen = {}

    class _Proc:
        returncode = 2
        pid = 0

        def __init__(self, cmd, **kw):
            seen.update(cmd=cmd, kw=kw)
            kw["stdout"].write(json.dumps({"exit_code": 2}).encode())
            kw["stderr"].write(b"CERTIFICATION_CHILD memory_mb=320 exit_code=2 "
                               b"vm_peak_kb=40000 vm_hwm_kb=30000\n")

        def wait(self, timeout=None):
            return 2

    monkeypatch.setattr(apc.subprocess, "Popen", _Proc)
    report = apc.run(audits=FAKE_AUDITS, memory_mb=192, timeout=5)
    assert _only(report)["outcome"] == apc.UNAVAILABLE
    assert "preexec_fn" not in seen["kw"]
    assert seen["cmd"][1:] == ["-m", "scripts.certification_child", "--memory-mb",
                               "320", FAKE, "--json"]
    assert seen["cmd"][0] == sys.executable


def test_09a_the_memory_settings_are_configured():
    import yaml
    pc = yaml.safe_load((ROOT / "config" / "thresholds.yaml").read_text())[
        "platform_certification"]
    s = apc.load_settings()
    for key in ("instance_memory_mb", "safety_margin_mb", "min_audit_budget_mb",
                "audit_address_space_mb"):
        assert s[key] == pc[key]
    assert (pc["instance_memory_mb"], pc["safety_margin_mb"], pc["min_audit_budget_mb"],
            pc["audit_address_space_mb"]) == (512, 64, 96, 320)
    assert pc["interrupted_after_minutes"] * 60 > 2 * pc["heartbeat_seconds"]


@pytest.mark.parametrize("memory,rc,expected", [
    ({"budget_mb": 150, "address_space_mb": 320, "memory_error": True},
     3, "exceeded its memory backstop (320 MB of address space)"),
    ({"budget_mb": 150, "address_space_mb": 320, "watchdog_killed": True,
      "killed_at_rss_mb": 151.0}, -9, "exceeded its memory budget (150 MB resident)"),
    ({"budget_mb": 150, "address_space_mb": 320, "vm_peak_mb": 200.0,
      "peak_rss_mb": 160.0}, 0, "exceeded its memory budget (150 MB resident)"),
    ({"budget_mb": 150, "address_space_mb": 320, "vm_peak_mb": 310.0,
      "peak_rss_mb": 140.0}, 0, "reached its memory backstop (320 MB"),
    ({"budget_mb": 150, "address_space_mb": 320, "vm_peak_mb": None,
      "peak_rss_mb": None}, 0, "memory budget (150 MB resident) is not proven held"),
    (None, -9, "SIGKILL"),
    (None, -11, "SIGSEGV"),
])
def test_09b_memory_evidence_is_read_before_the_exit_code(memory, rc, expected):
    res = apc.classify(rc, json.dumps({"exit_code": rc}), "", None, memory)
    assert res["outcome"] == apc.CRASHED
    assert expected in res["detail"]


def test_09c_the_stored_summary_keeps_each_audits_memory():
    summary = pcs.summarize({"verdict": "NOT CERTIFIED", "exit_code": 2, "audits": [
        {"key": "campaign_evidence", "outcome": "crashed", "detail": "exceeded its "
         "memory budget (150 MB resident)", "memory_budget_mb": 150,
         "peak_rss_mb": 150.1, "vm_peak_mb": 191.0,
         "memory": {"limit_mb": 512.0, "usage_mb": 298.0}, "report": {"x": 1}}]})
    a = summary["audits"][0]
    assert (a["memory_budget_mb"], a["peak_rss_mb"], a["vm_peak_mb"]) == (150, 150.1, 191.0)
    assert a["memory"] == {"limit_mb": 512.0, "usage_mb": 298.0}
    assert "report" not in a


def test_09d_the_heartbeat_beats_while_an_audit_runs(monkeypatch):
    _fake(monkeypatch, "sleep", sleep=1.5)
    beats = []
    rc, out, _err, error, _mem = apc._run_capped(
        FAKE, 60, memory_mb=192, heartbeat=lambda: beats.append(time.monotonic()),
        heartbeat_seconds=0.2)
    assert (rc, error) == (0, None) and json.loads(out)["mode"] == "sleep"
    assert len(beats) >= 5, beats
    assert all(b - a >= 0.19 for a, b in zip(beats, beats[1:]))


def test_09e_cgroup_readers_prefer_v2_then_v1_then_config(monkeypatch, tmp_path):
    settings = apc.load_settings()
    v2max, v2cur = tmp_path / "max", tmp_path / "current"
    v1lim, v1use = tmp_path / "limit", tmp_path / "usage"
    monkeypatch.setattr(apc, "CGROUP_V2_MAX", v2max)
    monkeypatch.setattr(apc, "CGROUP_V2_CURRENT", v2cur)
    monkeypatch.setattr(apc, "CGROUP_V1_LIMIT", v1lim)
    monkeypatch.setattr(apc, "CGROUP_V1_USAGE", v1use)
    v2max.write_text(f"{512 * 2**20}\n")
    v2cur.write_text(f"{300 * 2**20}\n")
    assert apc.derive_budget(settings)["budget_mb"] == 512 - 300 - 64
    v2max.write_text("max\n")                       # no v2 limit
    v1lim.write_text(f"{1024 * 2**20}\n")
    v1use.write_text(f"{500 * 2**20}\n")
    b = apc.derive_budget(settings)
    assert (b["limit_source"], b["budget_mb"]) == ("cgroup_v1", 1024 - 500 - 64)
    v1lim.write_text("9223372036854771712\n")       # v1's "no limit"
    b = apc.derive_budget(settings)
    assert (b["limit_source"], b["usage_source"]) == ("config", "parent_vmrss")
    assert b["limit_mb"] == 512.0 and b["usage_mb"] > 0


# ── verdict equivalence of the memory reductions (pure) ─────────────────────
def _old_function_code(path: Path, name: str):
    """The pre-PR-ADS-169A implementation, verbatim, for comparison."""
    try:
        src = path.read_text()
        tree = ast.parse(src)
    except Exception:  # noqa: BLE001
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            if not seg:
                return None
            fn = ast.parse(seg).body[0]
            body = list(getattr(fn, "body", []))
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            fn.body = body or [ast.Pass()]
            return ast.unparse(fn)
    return None


def test_10_one_parse_of_a_file_returns_exactly_what_the_per_call_walk_did(tmp_path):
    import scripts.audit_campaign_evidence_certification as cert
    files = [ROOT / "api" / "server.py",
             ROOT / "db" / "keyword_repository.py",
             ROOT / "services" / "keyword_evidence_service.py"]
    dup = tmp_path / "dup.py"
    dup.write_text("def f():\n    return 1\n\nclass A:\n    def f(self):\n"
                   "        '''doc'''\n        return 2\n\nasync def g():\n    pass\n")
    asked = {"_build_campaign_detail", "_campaign_keyword_preview",
             "_campaign_flagged_preview", "build_campaign_keyword_preview",
             "fetch_keyword_aggregates"}
    for path in files + [dup]:
        names = sorted({n.name for n in ast.walk(ast.parse(path.read_text()))
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
        if len(names) > 60:          # api/server.py: what the audit asks + a sample
            names = sorted(asked | set(names[::12]))
        for name in names + ["no_such_function"]:
            assert cert._function_code(path, name) == _old_function_code(path, name), \
                (path, name)
    assert cert._function_code(tmp_path / "missing.py", "f") is None


def _lead(key, *, status="qualified", created="2026-10-05", source="PAID_SEARCH",
          stype="paid_search", campaign="Brand", run_date="2026-11-01", row_id=1):
    return {"contact_key": key, "contact_id": key, "row_id": row_id,
            "run_date": run_date, "contact_created_at": created,
            "status_category": status, "source_type": stype,
            "hs_analytics_source": source, "campaign_name": campaign,
            "keyword": "kw", "country": "GB", "company": "Co", "has_gclid": True}


def _population_fixture():
    leads = [
        _lead("a"), _lead("b", campaign=None), _lead("c", source="ORGANIC_SEARCH",
                                                     stype="organic"),
        _lead("d", status="junk"), _lead("e", created=None),
        _lead("f", created="2025-01-01"), _lead("g"), _lead("h", campaign="Unknown"),
        _lead("a", status="junk", run_date="2026-10-01", row_id=0),   # older snapshot
        _lead("x"),                                                   # excluded
        _lead("y", status="junk"),                                    # excluded non-SQL
        _lead("o", source="OFFLINE", stype="offline"),
    ]
    classification = [
        {"contact_key": "a", "contact_id": "a", "acquisition_group": "google_ads",
         "status_category": "qualified", "contact_created_at": "2026-10-05",
         "source_primary_raw": "PAID_SEARCH", "source_detail_raw": "Brand",
         "classification_rule_version": "v1", "classified_at": "2026-11-01",
         "updated_at": "2026-11-01"},
        {"contact_key": "b", "acquisition_group": "google_ads",
         "status_category": "junk", "contact_created_at": "2026-10-05"},     # stale
        {"contact_key": "c", "acquisition_group": "google_ads",
         "status_category": "qualified", "contact_created_at": "2026-10-05"},  # stale group
        {"contact_key": "o", "acquisition_group": "other_paid",
         "status_category": "qualified", "contact_created_at": "2026-10-05"},
        {"contact_key": None, "acquisition_group": "x"},
    ]   # g, h, d, e missing
    return leads, {"x", "y"}, classification


def _resolver(name):
    if not name:
        return False, "missing_campaign"
    return (True, None) if name == "Brand" else (False, "unresolved_campaign")


@pytest.mark.parametrize("window", [(date(2026, 10, 1), date(2026, 10, 31)),
                                    (None, date(2026, 11, 20))])
def test_11_counts_only_population_equals_the_kept_population_counts(window):
    from services import canonical_contact_outcome_service as c
    leads, exclusions, classification = _population_fixture()
    kept = c.build_populations(leads, exclusions, classification, *window,
                               campaign_resolver=_resolver)
    lean = c.build_populations(leads, exclusions, classification, *window,
                               campaign_resolver=_resolver, keep_contacts=False)
    assert lean["counts"] == kept["counts"]
    assert kept["counts"]["total_all_source_sqls"] > 0
    assert kept["counts"]["stale_classification_contacts"] > 0
    assert kept["counts"]["missing_classification_contacts"] > 0
    # Not computed is None — never an empty population standing in for it.
    assert lean["contacts"] is None and lean["excluded"] is None


@pytest.mark.parametrize("window", [(date(2026, 10, 1), date(2026, 10, 31)),
                                    (None, date(2026, 11, 20))])
def test_12_the_narrowed_classification_builds_the_identical_population(window):
    from services import canonical_contact_outcome_service as c
    leads, exclusions, classification = _population_fixture()
    full = c.build_populations(leads, exclusions, [dict(r) for r in classification],
                               *window, campaign_resolver=_resolver)
    narrowed = c.build_populations(
        leads, exclusions,
        c._classification_fields_read([dict(r) for r in classification]),
        *window, campaign_resolver=_resolver)
    assert narrowed == full


def test_13_shared_strings_change_no_value_type_or_order():
    from db.row_compaction import rows_as_dicts

    class _Cur:
        description = [("a",), ("b",), ("c",), ("d",)]

        def fetchall(self):
            when = datetime(2026, 1, 1, tzinfo=timezone.utc)
            return [("x" + "y", 1, when, None), ("xy", 2.5, when, b"z"),
                    ("".join(["x", "y"]), True, date(2026, 1, 1), "q")]

    expected = [dict(zip([d[0] for d in _Cur.description], r))
                for r in _Cur().fetchall()]
    got = rows_as_dicts(_Cur())
    assert got == expected
    assert [[type(v) for v in r.values()] for r in got] == \
        [[type(v) for v in r.values()] for r in expected]
    assert got[0]["a"] is got[1]["a"] is got[2]["a"]       # one object, equal text


# ── PostgreSQL ──────────────────────────────────────────────────────────────
@pytest.fixture()
def live(monkeypatch):
    from tests import test_pr_ads_153b_pg_integration as pgmod
    if not pgmod._have_postgres():
        pytest.skip("PostgreSQL not available")
    cluster = pgmod._PgCluster().start()
    import db.connection as connection
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)
        monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()
        from db.schema import init_db
        init_db()
        yield cluster
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None
        cluster.stop()


def _rows(cluster, sql="SELECT id, status, verdict, exit_code, finished_at, "
                       "summary, heartbeat_at FROM platform_certification_runs "
                       "ORDER BY id"):
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


REPORT = {"verdict": "CERTIFIED", "exit_code": 0, "generated_at": "x",
          "audits": [{"key": "closed_won_truth", "outcome": "holds", "exit_code": 0}],
          "crm_data_to_fix": None}


def test_14_pg_the_row_exists_while_the_run_is_running_and_is_finalized_in_place(live):
    seen = {}

    def runner():
        seen["during"] = _rows(live)
        return REPORT
    out = pcs.run_and_record(runner=runner)
    assert out["recorded"] is True and out["verdict"] == "CERTIFIED"
    (rid, status, verdict, code, finished, summary, beat), = seen["during"]
    assert (status, verdict, code, finished) == ("running", "NOT CERTIFIED", 2, None)
    assert summary["status"] == "running" and beat is not None
    after = _rows(live)
    assert len(after) == 1 and after[0][0] == rid
    assert after[0][1:4] == ("finished", "CERTIFIED", 0)
    assert after[0][4] is not None


def test_15_pg_the_heartbeat_moves_and_names_the_audit_being_run(live, monkeypatch):
    seen = []
    real_run = apc.run

    def fake_runner(module, timeout):
        seen.append(_rows(live, "SELECT summary->>'current_audit', heartbeat_at "
                                "FROM platform_certification_runs")[0])
        return 0, json.dumps({"exit_code": 0}), "", None
    monkeypatch.setattr(apc, "run", lambda **kw: real_run(
        audits=(("first", "m1", "x"), ("second", "m2", "y")), runner=fake_runner, **kw))
    out = pcs.run_and_record()
    assert out["verdict"] == "CERTIFIED" and out["recorded"]
    assert [a for a, _ in seen] == ["first", "second"]
    assert seen[0][1] <= seen[1][1]


def test_16_pg_a_stale_running_row_is_closed_on_startup_and_a_live_one_is_not(live):
    from api import scheduler
    stale = pcs.open_run(datetime.now(timezone.utc) - timedelta(minutes=20))
    live_id = pcs.open_run(datetime.now(timezone.utc))
    with live.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE platform_certification_runs SET heartbeat_at = "
                    "clock_timestamp() - interval '9 minutes' WHERE id = %s", (stale,))
        conn.commit()
    scheduler._close_interrupted_certification_runs()
    rows = {r[0]: r for r in _rows(live)}
    _, status, verdict, code, finished, summary, _ = rows[stale]
    assert (status, verdict, code) == ("interrupted", "NOT CERTIFIED", 2)
    assert finished is not None
    assert summary["error"] == "heartbeat stopped; presumed interrupted"
    assert rows[live_id][1] == "running"


def test_17_pg_the_next_run_sweeps_first_and_a_late_finish_still_records_its_verdict(live):
    orphan = pcs.open_run(datetime.now(timezone.utc) - timedelta(hours=1))
    with live.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE platform_certification_runs SET heartbeat_at = NULL")
        conn.commit()
    out = pcs.run_and_record(runner=lambda: REPORT)
    rows = {r[0]: r for r in _rows(live)}
    assert rows[orphan][1] == "interrupted"
    assert [r[1] for r in rows.values()].count("finished") == 1
    # A run swept while it was in fact still going finalizes with its verdict.
    late = pcs.open_run(datetime.now(timezone.utc))
    assert pcs.close_interrupted_runs(older_than_minutes=0) >= 1
    assert pcs.record(datetime.now(timezone.utc), datetime.now(timezone.utc), 0,
                      "CERTIFIED", {"audits": []}, run_id=late) is True
    assert {r[0]: r for r in _rows(live)}[late][1:4] == ("finished", "CERTIFIED", 0)
    assert out["recorded"]


def test_18_pg_the_system_page_reads_running_and_interrupted_rows_honestly(live):
    from services import system_overview_service as so
    now = datetime.now(timezone.utc)
    rid = pcs.open_run(now)
    block = so.certification_block(so.read_latest_certification(), now)
    assert block["state"] == so.STATE_RUNNING
    assert block["state"] != so.STATE_CERTIFIED and block["audits"] == []
    with live.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE platform_certification_runs SET heartbeat_at = "
                    "clock_timestamp() - interval '30 minutes' WHERE id = %s", (rid,))
        conn.commit()
    block = so.certification_block(so.read_latest_certification(), now)
    assert (block["state"], block["interrupted"], block["could_not_run"]) == \
        (so.STATE_NOT_CERTIFIED, True, True)
    pcs.close_interrupted_runs()
    block = so.certification_block(so.read_latest_certification(), now)
    assert (block["state"], block["run_status"], block["interrupted"]) == \
        (so.STATE_NOT_CERTIFIED, "interrupted", True)
    assert block["could_not_run"] is True


def test_19_pg_an_audit_over_its_cap_is_recorded_not_certified_with_the_memory_detail(
        live, monkeypatch):
    _fake(monkeypatch, "touch")
    real_run = apc.run
    monkeypatch.setattr(apc, "run", lambda **kw: real_run(
        audits=FAKE_AUDITS, memory_mb=TINY_MB, timeout=120, **kw))
    out = pcs.run_and_record()
    assert out["verdict"] == "NOT CERTIFIED" and out["recorded"]
    (_, status, verdict, code, _, summary, _), = _rows(live)
    assert (status, verdict, code) == ("finished", "NOT CERTIFIED", 2)
    a = summary["audits"][0]
    assert a["outcome"] == "crashed"
    assert f"exceeded its memory budget ({TINY_MB} MB resident)" in a["detail"]
    assert a["memory_budget_mb"] == TINY_MB


def test_20_pg_chunked_shared_reads_return_the_same_rows_as_fetchall(live, monkeypatch):
    import db.row_compaction as rc
    monkeypatch.setattr(rc, "CHUNK_ROWS", 3)
    sql = ("SELECT g, 'v' || (g % 3), now() - g * interval '1 hour', "
           "CASE WHEN g % 2 = 0 THEN NULL ELSE 1.5::numeric END, current_date - g "
           "FROM generate_series(1, 10) g ORDER BY g")
    with live.connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        expected = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute(sql)
        got = rc.rows_as_dicts(cur)
    assert got == expected and len(got) == 10
    assert [type(v) for r in got for v in r.values()] == \
        [type(v) for r in expected for v in r.values()]


def test_21_pg_counts_only_reconciliation_equals_the_full_build(live, monkeypatch):
    """Through the real reads: page_reconciliation (counts only now) states
    what the full population build states."""
    from db import writers
    from services import canonical_contact_outcome_service as c
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "1234567890")
    run_id = writers.write_run({"run_type": "daily", "status": "success",
                                "started_at": datetime.now(timezone.utc)})
    today = datetime.now(timezone.utc)
    contacts = [{"id": f"k{i}", "mql_status": ("CLOSED - Sales Qualified" if i % 2 else "DICARDED"),
                 "campaign_name": ("Brand" if i % 3 else None),
                 "hs_analytics_source": ("PAID_SEARCH" if i % 4 else "ORGANIC_SEARCH"),
                 "createdate": (today - timedelta(days=i)).isoformat()}
                for i in range(1, 40)]
    assert writers.write_leads(run_id, contacts) > 0
    for window in ("7d", "30d", "all_time"):
        for scope in (c.SCOPE_ALL_SOURCE, c.SCOPE_GOOGLE_ADS_SOURCE,
                      c.SCOPE_CAMPAIGN_ATTRIBUTABLE):
            full = c.build(c.WINDOW_EVIDENCE, window)
            expected = c.reconciliation_metadata(
                {"counts": full["counts"]}, scope, available=full["available"])
            expected["window"] = full["window"]
            got = c.page_reconciliation(c.WINDOW_EVIDENCE, window, scope)
            assert got == expected, (window, scope)
    assert c.build(c.WINDOW_EVIDENCE, "all_time")["counts"]["total_all_source_sqls"] > 0


def test_22_pg_shared_dates_leave_every_funnel_row_unchanged(live):
    from db import crm_funnel_repository as repo
    from db import writers
    now = datetime.now(timezone.utc)
    writers.upsert_hubspot_contact_funnel([
        {"contact_id": f"f{i}", "lifecycle_stage": "salesqualifiedlead",
         "created_at": now - timedelta(days=i % 3), "last_modified_at": now,
         "date_entered_sql": now - timedelta(days=i % 2)} for i in range(12)])
    got = repo.fetch_all_funnel_contacts()["rows"]
    with live.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {repo._funnel_select()} FROM {repo.FUNNEL_TABLE} f "
                    f"{repo._recovery_join()}")
        cols = [d[0] for d in cur.description]
        expected = [repo._normalise_row(dict(zip(cols, r))) for r in cur.fetchall()]
    key = lambda r: r["contact_id"]  # noqa: E731
    assert sorted(got, key=key) == sorted(expected, key=key) and len(got) == 12
    by_day = {}
    for r in got:
        by_day.setdefault(r["created_at"], set()).add(id(r["created_at"]))
    assert all(len(ids) == 1 for ids in by_day.values())   # one object per date


def test_23_startup_sweeps_now_and_again_after_the_heartbeat_grace(monkeypatch):
    from api import scheduler
    calls, timers = [], []

    class _Timer:
        def __init__(self, interval, fn):
            timers.append((interval, fn))
            self.daemon = False

        def start(self):
            timers.append("started")

    monkeypatch.setattr(scheduler, "_close_interrupted_certification_runs",
                        lambda: calls.append("swept"))
    monkeypatch.setattr(scheduler.threading, "Timer", _Timer)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    scheduler.start_scheduler()
    try:
        import time
        for _ in range(50):
            if calls:
                break
            time.sleep(0.02)
        assert calls == ["swept"]
        (interval, fn), started = timers
        assert started == "started"
        assert interval == pcs.interrupted_after_minutes() * 60 + 30
        assert fn() is None and calls == ["swept", "swept"]
    finally:
        scheduler._scheduler.shutdown(wait=False)
        monkeypatch.setattr(scheduler, "_scheduler", None)


def test_24_pg_the_heartbeat_keeps_a_long_audit_running_on_the_system_page(
        live, monkeypatch):
    """With a 1.2 s staleness limit and a 2 s audit, the run would read as
    interrupted half-way without the periodic heartbeat."""
    from services import system_overview_service as so
    _fake(monkeypatch, "sleep", sleep=2.0)
    monkeypatch.setattr(pcs, "interrupted_after_minutes", lambda: 0.02)   # 1.2 s
    real_settings = apc.load_settings
    monkeypatch.setattr(apc, "load_settings",
                        lambda: {**real_settings(), "heartbeat_seconds": 0.2})
    real_run = apc.run
    monkeypatch.setattr(apc, "run", lambda **kw: real_run(
        audits=FAKE_AUDITS, memory_mb=192, timeout=60, **kw))
    states, done = [], threading.Event()

    def watch():
        while not done.is_set():
            block = so.certification_block(so.read_latest_certification(),
                                           datetime.now(timezone.utc))
            states.append((block["state"], block["run_status"]))
            time.sleep(0.1)
    t = threading.Thread(target=watch)
    t.start()
    try:
        out = pcs.run_and_record()
    finally:
        done.set()
        t.join()
    assert out["verdict"] == "CERTIFIED"
    during = [st for st in states if st[1] != "finished"]
    assert len(during) >= 10, states
    assert set(during) <= {(so.STATE_RUNNING, "running"), (so.STATE_NOT_CHECKED, None)}, states
    assert (so.STATE_RUNNING, "running") in during


def test_25_pg_the_system_page_shows_the_last_finished_crm_data_while_a_run_is_live(live):
    from services import system_overview_service as so
    fix = {"deals_with_any_exception": 7, "by_family": {}, "dataset_blockers": [],
           "windows": []}
    assert pcs.record(datetime.now(timezone.utc) - timedelta(days=1),
                      datetime.now(timezone.utc) - timedelta(days=1), 2,
                      "NOT CERTIFIED", {"audits": [], "crm_data_to_fix": fix})
    pcs.open_run(datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    block = so.certification_block(so.read_latest_certification(), now)
    assert block["state"] == so.STATE_RUNNING
    assert block["crm_data_to_fix"] == fix and block["crm_data_to_fix_from"]
    pcs.close_interrupted_runs(older_than_minutes=0)
    block = so.certification_block(so.read_latest_certification(), now)
    assert block["interrupted"] and block["crm_data_to_fix"] == fix
    assert block["crm_data_to_fix_from"]
