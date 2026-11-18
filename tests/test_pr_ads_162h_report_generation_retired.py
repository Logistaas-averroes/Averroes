"""PR-ADS-162H — report generation and its email are removed; the jobs' data
steps are not.

Owner decision ("delete action queue and reports"). PR-ADS-162G deleted the
Reports page; the weekly and monthly jobs went on building the advisor report
(lead quality → campaign truth → n-grams → markdown) and emailing it by
SendGrid. Nobody received it and no page showed it.

Those two jobs are not only report code. They are the only writers of inputs
live pages read, so this suite has two halves that matter equally:

* what is GONE — the advisor modules, the email module, every call to them,
  the report path on the manual-run response and in the UI, and the legacy
  ``campaigns`` snapshot as a registered freshness dataset;
* what STAYED — every data step in both jobs, proved on the AST of each job
  function and by running each job with every external call stubbed.

The run record matters most. The jobs used to be recorded ``success`` when the
report step was reached; a write that persisted nothing was logged and
forgotten. With the report gone the data steps ARE the job, so a run is
``success`` only when every step landed, ``partial`` when it finished and one
did not, and ``failed`` when it could not continue. ``test_08`` … ``test_10``
drive the real job functions through each of those.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/styles.css").read_text(encoding="utf-8")
SERVER = (ROOT / "api/server.py").read_text(encoding="utf-8")

DELETED_FILES = ("scheduler/delivery.py", "analysis/advisor.py",
                 "analysis/rule_advisor.py")
DELETED_MODULES = ("scheduler.delivery", "analysis.advisor",
                   "analysis.rule_advisor")
PACKAGES = ("analysis", "api", "connectors", "db", "scheduler", "services",
            "scripts")

#: Everything the report was built from and sent with.
REPORT_CALLS = {"run_lead_quality", "run_campaign_truth", "determine_verdict",
                "compute_ngram_findings", "generate_weekly_report",
                "generate_monthly_report", "generate_deterministic_report",
                "deliver_report", "write_campaigns"}

#: The data steps both jobs still own. `record_flag_history` is weekly only —
#: the monthly job has never called it.
KEPT_CALLS = {
    "pull_campaign_performance", "pull_keyword_performance",
    "pull_geo_performance", "pull_paid_search_contacts", "pull_deals_with_gclid",
    "write_run", "write_leads", "write_deals", "write_geo", "write_keywords",
    "sync_keyword_daily_facts", "sync_recent_search_terms",
    "run_waste_detection", "write_waste_terms",
    "run_gclid_match", "write_gclid_attribution",
    "write_gclid_coverage_snapshot",
    "start_run", "finish_run", "update_run",
}
#: Local JSON snapshots that still have a reader (see test_06).
KEPT_SAVES = {"google_ads_save", "hubspot_save", "gclid_save"}
#: Every sync batch the jobs still stamp, as (source, dataset).
KEPT_BATCHES = {("hubspot", "contacts"), ("hubspot", "deals"),
                ("google_ads_api", "geo"), ("google_ads_api", "keywords"),
                ("analysis", "waste_terms"), ("gclid", "matches"),
                ("gclid", "coverage_snapshots")}

JOBS = {"weekly": ("scheduler/weekly.py", "run_weekly_report"),
        "monthly": ("scheduler/monthly.py", "run_monthly_report")}


# ── AST helpers ──────────────────────────────────────────────────────────────
def _imports(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def _production_files():
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _job_function(job: str) -> ast.FunctionDef:
    rel, name = JOBS[job]
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _called(node: ast.AST) -> list:
    """Every called name under ``node``, in SOURCE order (``ast.walk`` is
    breadth-first, which is not the order the code runs in)."""
    found = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name:
                found.append((inner.lineno, inner.col_offset, name))
    return [name for _, _, name in sorted(found)]


def _batches(node: ast.AST) -> set:
    found = set()
    for inner in ast.walk(node):
        if (isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "start_sync_batch"):
            kw = {k.arg: k.value for k in inner.keywords}
            source, dataset = kw.get("source"), kw.get("dataset")
            assert isinstance(source, ast.Constant), "computed sync source"
            assert isinstance(dataset, ast.Constant), "computed sync dataset"
            found.add((source.value, dataset.value))
    return found


# ═════════════════════════════════════════════════════════════════════════════
# 1 — what is gone
# ═════════════════════════════════════════════════════════════════════════════
def test_01_the_report_and_email_modules_are_gone():
    for rel in DELETED_FILES:
        assert not (ROOT / rel).exists(), rel
    import analysis.core as core
    for name in ("run_lead_quality", "run_campaign_truth", "determine_verdict",
                 "_clean_campaign_name", "_is_real_campaign",
                 "_canonicalise_campaign_name", "_CAMPAIGN_CANONICAL",
                 "IN_PROGRESS", "UNKNOWN", "JUNK", "WRONG_FIT"):
        assert not hasattr(core, name), name


def test_02_no_production_module_imports_or_names_a_deleted_module():
    """An import of a deleted module is a crash waiting for its code path. Two
    scripts and the readiness endpoint named these modules as STRINGS (a list
    of modules to import, a list of files that must exist), which an import
    scan cannot see — so string constants are read too."""
    offenders = []
    for path in _production_files():
        rel = str(path.relative_to(ROOT))
        hit = _imports(path) & set(DELETED_MODULES)
        if hit:
            offenders.append((rel, sorted(hit)))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef,
                                        ast.AsyncFunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr)
                      and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                for name in (*DELETED_MODULES, *DELETED_FILES):
                    if name in node.value:
                        offenders.append((rel, node.lineno, name))
    assert offenders == []


def test_03_no_production_code_calls_a_report_function():
    """`scheduler/daily.py` has its own local `deliver_report(result)` — the
    daily pulse's print-to-log step, unrelated to the deleted email module. It
    is the one allowed use of that name, and it is checked to be local."""
    offenders = []
    for path in _production_files():
        rel = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in set(_called(tree)) & (REPORT_CALLS - {"write_campaigns"}):
            if (rel, name) != ("scheduler/daily.py", "deliver_report"):
                offenders.append((rel, name))
    assert offenders == []
    daily = ast.parse((ROOT / "scheduler/daily.py").read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.FunctionDef) and n.name == "deliver_report"
               for n in daily.body)
    assert not {"sendgrid", "scheduler.delivery"} & _imports(
        ROOT / "scheduler/daily.py")
    # The email library has no importer left, and is no longer installed.
    for path in _production_files():
        assert not any(i.split(".")[0] == "sendgrid" for i in _imports(path)), path
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert not re.search(r"^sendgrid", requirements, re.M)


@pytest.mark.parametrize("job", sorted(JOBS))
def test_04_neither_job_builds_a_report_emails_one_or_writes_the_snapshot(job):
    fn = _job_function(job)
    called = set(_called(fn))
    assert not called & REPORT_CALLS, sorted(called & REPORT_CALLS)
    # The `campaigns` snapshot batch went with the step that wrote it.
    assert ("google_ads_api", "campaigns") not in _batches(fn)
    rel = JOBS[job][0]
    assert not {"scheduler.delivery", "analysis.advisor",
                "analysis.rule_advisor"} & _imports(ROOT / rel)
    imported = _imports(ROOT / rel)
    assert not any(name.endswith((".run_lead_quality", ".run_campaign_truth"))
                   for name in imported)


# ═════════════════════════════════════════════════════════════════════════════
# 2 — what stayed
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("job", sorted(JOBS))
def test_05_both_jobs_still_call_every_kept_data_step(job):
    """Read from the AST of the job FUNCTION, not from the file's text: a step
    named only in a comment or the module docstring is not a step."""
    fn = _job_function(job)
    called = _called(fn)
    missing = KEPT_CALLS - set(called)
    if job == "monthly":
        # The monthly job records a run that cannot continue through one
        # helper, which is where its failed-run `update_run` lives too.
        helper = next(n for n in ast.parse(
            (ROOT / JOBS[job][0]).read_text(encoding="utf-8")).body
            if isinstance(n, ast.FunctionDef) and n.name == "_failed")
        assert {"finish_run", "update_run"} <= set(_called(helper))
    assert missing == set(), sorted(missing)
    assert KEPT_SAVES <= set(called), sorted(KEPT_SAVES - set(called))
    assert _batches(fn) == KEPT_BATCHES

    # Each is called exactly as often as before: one pull, one write, one sync.
    for name in ("write_leads", "write_deals", "write_geo", "write_keywords",
                 "sync_keyword_daily_facts", "sync_recent_search_terms",
                 "run_waste_detection", "write_waste_terms", "run_gclid_match",
                 "write_gclid_attribution", "write_gclid_coverage_snapshot",
                 "write_run"):
        assert called.count(name) == 1, (name, called.count(name))

    history = called.count("record_flag_history")
    assert history == (1 if job == "weekly" else 0)
    if job == "weekly":
        # After the annotations land, as before.
        order = [n for n in called
                 if n in ("write_waste_terms", "record_flag_history")]
        assert order == ["write_waste_terms", "record_flag_history"]

    # The run type — what the monitoring cadence is keyed on — is unchanged.
    starts = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "start_run"]
    assert [s.args[0].value for s in starts] == [job]


def test_06_every_kept_json_snapshot_still_has_a_reader():
    """The saves were not report plumbing. Each file the jobs still write is
    read by something that still runs."""
    readers = {
        "ads_search_terms.json": "connectors/gclid_match.py",
        "crm_contacts.json": "connectors/gclid_match.py",
        "crm_deals.json": "connectors/gclid_match.py",
        "data/ads_campaigns.json": "scheduler/daily.py",
    }
    for needle, rel in readers.items():
        assert needle in (ROOT / rel).read_text(encoding="utf-8"), (needle, rel)
    assert "crm_contacts.json" in (ROOT / "analysis/core.py").read_text(
        encoding="utf-8")
    fallback = (ROOT / "services/revenue_attribution_service.py").read_text(
        encoding="utf-8")
    assert "ads_campaigns.json" in fallback and "crm_contacts.json" in fallback
    for rel, _ in JOBS.values():
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "google_ads_save(campaigns, None, keywords, geos)" in src, rel
        assert "google_ads_save(None, search_terms, None, None)" in src, rel
        assert "hubspot_save(contacts, deals, crm_summary)" in src, rel
        assert "gclid_save(gclid_result)" in src, rel


@pytest.mark.parametrize("job", sorted(JOBS))
def test_07_the_printed_step_labels_are_renumbered_consistently(job):
    src = (ROOT / JOBS[job][0]).read_text(encoding="utf-8")
    labels = re.findall(r"Step (\w+)/(\w+)", src)
    assert labels, "no step labels found"
    assert {total for _, total in labels} == {"4"}, sorted(set(labels))
    assert {n for n, _ in labels} == {"1", "2", "3", "4"}
    assert "5b" not in src and "/6" not in src
    # In order of first appearance, with no step out of sequence.
    first_seen = list(dict.fromkeys(n for n, _ in labels))
    assert first_seen == ["1", "2", "3", "4"]


# ═════════════════════════════════════════════════════════════════════════════
# 3 — the jobs, run
# ═════════════════════════════════════════════════════════════════════════════
class _Harness:
    """Every external call the two jobs make, stubbed and recorded.

    Nothing here relabels a production identifier: each stub is installed on
    the real module attribute the job resolves at run time, and each returns
    the shape the real function returns.
    """

    CONTACTS = [{"id": "c1", "properties": {"createdate": "2026-11-10T00:00:00Z"}},
                {"id": "c2", "properties": {"createdate": "2026-11-11T00:00:00Z"}}]
    DEALS = [{"id": "d1", "properties": {}}]
    CAMPAIGNS = [{"campaign": "mature - markets", "spend": 10.0, "date": "2026-11-10"}]
    KEYWORDS = [{"keyword": "tms", "date": "2026-11-10"}]
    GEOS = [{"country": "GB", "date": "2026-11-10"}]
    TERMS = [{"search_term": "free tms", "spend": 9.0},
             {"search_term": "freight software", "spend": 20.0}]
    MATCHED = [{"contact_id": "c1", "gclid": "g1"}]
    COVERAGE = {"total_paid_contacts": 2, "contacts_with_gclid": 1}

    def __init__(self, monkeypatch, tmp_path, job: str):
        import importlib
        self.job = job
        self.calls: list = []
        self.records: list = []
        self.finished_batches: list = []
        self.waste_args = None
        self.overrides: dict = {}
        self.module = importlib.import_module(f"scheduler.{job}")
        self._batch_ids = iter(range(100, 1000))
        self._batch_keys: dict = {}

        # Run in an empty directory: the monthly job checks that the connector
        # snapshots exist on disk, and the real save functions are stubbed.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        for name in ("ads_campaigns.json", "ads_search_terms.json",
                     "crm_contacts.json"):
            (tmp_path / "data" / name).write_text("[]", encoding="utf-8")

        def stub(target: str, name: str, default):
            def call(*args, **kwargs):
                self.calls.append(name)
                behaviour = self.overrides.get(name, default)
                if isinstance(behaviour, BaseException):
                    raise behaviour
                if callable(behaviour):
                    return behaviour(*args, **kwargs)
                return behaviour
            monkeypatch.setattr(target, call)

        ga = "connectors.google_ads_source."
        stub(ga + "pull_campaign_performance", "pull_campaign_performance",
             self.CAMPAIGNS)
        stub(ga + "pull_keyword_performance", "pull_keyword_performance",
             self.KEYWORDS)
        stub(ga + "pull_geo_performance", "pull_geo_performance", self.GEOS)
        stub(ga + "save_output", "google_ads_save", None)
        hs = "connectors.hubspot_pull."
        stub(hs + "pull_paid_search_contacts", "pull_paid_search_contacts",
             self.CONTACTS)
        stub(hs + "pull_deals_with_gclid", "pull_deals_with_gclid", self.DEALS)
        stub(hs + "get_lead_quality_summary", "get_lead_quality_summary", {})
        stub(hs + "save_output", "hubspot_save", None)
        stub("connectors.gclid_match.run_gclid_match", "run_gclid_match",
             {"matched": self.MATCHED, "coverage": self.COVERAGE})
        stub("connectors.gclid_match.save_output", "gclid_save", None)

        stub("services.keyword_sync_service.sync_keyword_daily_facts",
             "sync_keyword_daily_facts", {"ok": True, "written": 3})
        stub("services.search_term_sync_service.sync_recent_search_terms",
             "sync_recent_search_terms",
             {"ok": True, "rows": self.TERMS, "fetched": 2, "written": 2})
        stub("services.search_term_flag_history_service.record_flag_history",
             "record_flag_history",
             {"available": True, "observed": 1, "written": 1, "pages": 1})

        def waste(rows, *, search_term_evidence_available=None):
            self.waste_args = (list(rows), search_term_evidence_available)
            return {"confirmed_waste_items": (
                [{"term": "free tms", "spend_usd": 9.0}]
                if search_term_evidence_available else [])}
        stub("analysis.core.run_waste_detection", "run_waste_detection", waste)

        w = "db.writers."
        stub(w + "write_run", "write_run", 7)
        stub(w + "write_leads", "write_leads", lambda run_id, rows: len(rows))
        stub(w + "write_deals", "write_deals", lambda run_id, rows: len(rows))
        stub(w + "write_geo", "write_geo", lambda run_id, rows: len(rows))
        stub(w + "write_keywords", "write_keywords",
             lambda run_id, rows: len(rows))
        stub(w + "write_waste_terms", "write_waste_terms", None)
        stub(w + "write_gclid_attribution", "write_gclid_attribution",
             lambda **kw: len(kw["matched_rows"]))
        stub(w + "write_gclid_coverage_snapshot",
             "write_gclid_coverage_snapshot", 1)
        stub(w + "write_campaigns", "write_campaigns",
             AssertionError("the campaigns snapshot must not be written"))
        self.updated: list = []
        stub(w + "update_run", "update_run",
             lambda run_id, record: self.updated.append((run_id, dict(record))))

        def start_batch(*, source, dataset, **kwargs):
            batch_id = next(self._batch_ids)
            self._batch_keys[batch_id] = (source, dataset)
            return batch_id
        stub(w + "start_sync_batch", "start_sync_batch", start_batch)

        def finish_batch(*, batch_id, status, **kwargs):
            self.finished_batches.append((self._batch_keys[batch_id], status))
            return True
        stub(w + "finish_sync_batch", "finish_sync_batch", finish_batch)

        # The run record is captured where it would be appended to the JSONL
        # run history, i.e. AFTER the real finish_run has finalised it.
        monkeypatch.setattr("scheduler.run_history.write_run_record",
                            lambda record: self.records.append(dict(record)))

    def run(self):
        return getattr(self.module, JOBS[self.job][1])()

    @property
    def record(self) -> dict:
        assert len(self.records) == 1, "exactly one run record per run"
        return self.records[0]

    def batch_status(self, key) -> list:
        return [status for k, status in self.finished_batches if k == key]


@pytest.fixture
def harness(monkeypatch, tmp_path):
    def make(job: str) -> _Harness:
        return _Harness(monkeypatch, tmp_path, job)
    return make


def _assert_no_report_was_recorded(record: dict):
    """The three report columns stay in the run record (no schema change) and
    say that nothing was produced and nothing was sent."""
    assert record["report_path"] is None
    assert record["delivery_attempted"] is False
    assert record["delivery_success"] is None


@pytest.mark.parametrize("job", sorted(JOBS))
def test_08_a_run_whose_every_step_lands_is_recorded_success(job, harness):
    h = harness(job)
    outcome = h.run()

    assert outcome == {"run_type": job, "status": "success", "failed_steps": []}
    assert h.record["run_type"] == job
    assert h.record["status"] == "success"
    assert h.record["failed_step"] is None and h.record["error_message"] is None
    assert h.record["finished_at"]
    _assert_no_report_was_recorded(h.record)
    # The durable `runs` row is updated with the same record.
    assert [(rid, rec["status"]) for rid, rec in h.updated] == [(7, "success")]
    _assert_no_report_was_recorded(h.updated[0][1])

    # Every kept step ran, in the order the job states.
    expected = ["pull_campaign_performance", "pull_keyword_performance",
                "pull_geo_performance", "pull_paid_search_contacts",
                "pull_deals_with_gclid", "write_run", "write_leads",
                "write_deals", "write_geo", "write_keywords",
                "sync_keyword_daily_facts", "sync_recent_search_terms",
                "run_waste_detection", "write_waste_terms"]
    if job == "weekly":
        expected.append("record_flag_history")
    expected += ["run_gclid_match", "write_gclid_attribution",
                 "write_gclid_coverage_snapshot", "update_run"]
    ran = [c for c in h.calls if c in set(expected)]
    assert ran == expected
    assert "write_campaigns" not in h.calls
    assert h.calls.count("google_ads_save") == 2
    assert h.calls.count("hubspot_save") == h.calls.count("gclid_save") == 1

    # Waste detection received the persisted rows and was told they are real.
    assert h.waste_args == (_Harness.TERMS, True)
    # Every batch the job stamps finished success — and only those batches.
    assert {k for k, _ in h.finished_batches} == KEPT_BATCHES
    assert {status for _, status in h.finished_batches} == {"success"}


def _zero(*args, **kwargs):
    return 0


PARTIAL_CASES = [
    # (id, stub to break, behaviour, step named in the run record, jobs)
    ("leads not persisted", "write_leads", _zero, "write_leads", "both"),
    ("deals not persisted", "write_deals", _zero, "write_deals", "both"),
    ("leads write raises", "write_leads", RuntimeError("db down"),
     "write_leads / write_deals", "both"),
    ("geo not persisted", "write_geo", _zero, "write_geo", "both"),
    ("geo write raises", "write_geo", RuntimeError("db down"), "write_geo", "both"),
    ("keywords not persisted", "write_keywords", _zero, "write_keywords", "both"),
    ("keyword facts not ok", "sync_keyword_daily_facts",
     {"ok": False, "error": "pulled 4 keyword row(s) but wrote 0"},
     "sync_keyword_daily_facts", "both"),
    ("keyword facts raise", "sync_keyword_daily_facts", RuntimeError("boom"),
     "write_keywords / sync_keyword_daily_facts", "both"),
    ("search-term sync not ok", "sync_recent_search_terms",
     {"ok": False, "error": "partial write"}, "sync_recent_search_terms", "both"),
    ("search-term sync raises", "sync_recent_search_terms",
     RuntimeError("boom"), "sync_recent_search_terms", "both"),
    ("waste write raises", "write_waste_terms", RuntimeError("db down"),
     "write_waste_terms", "both"),
    ("flag history unavailable", "record_flag_history",
     {"available": False, "reason": "write_unavailable"},
     "record_flag_history", "weekly"),
    ("flag history raises", "record_flag_history", RuntimeError("boom"),
     "record_flag_history", "weekly"),
    ("gclid match raises", "run_gclid_match", RuntimeError("no file"),
     "run_gclid_match / write_gclid_attribution", "both"),
    ("gclid attribution not persisted", "write_gclid_attribution", _zero,
     "run_gclid_match / write_gclid_attribution", "both"),
    ("coverage snapshot not persisted", "write_gclid_coverage_snapshot", 0,
     "write_gclid_coverage_snapshot", "both"),
]


def _partial_params():
    for case_id, stub, behaviour, step, jobs in PARTIAL_CASES:
        for job in sorted(JOBS):
            if jobs in ("both", job):
                yield pytest.param(job, stub, behaviour, step,
                                   id=f"{job}: {case_id}")


@pytest.mark.parametrize("job, stub, behaviour, step", list(_partial_params()))
def test_09_a_kept_step_that_does_not_land_is_recorded_partial(
        job, stub, behaviour, step, harness):
    """One step broken at a time. The run must reach its end, must NOT be
    recorded success, and the record must name the step.

    Before PR-ADS-162H every one of these ended `success`: the failure was
    logged inside a try/except and the run went on to "report generated"."""
    h = harness(job)
    h.overrides[stub] = behaviour
    outcome = h.run()

    assert outcome["status"] == "partial"
    assert step in outcome["failed_steps"], outcome["failed_steps"]
    assert h.record["status"] == "partial"
    assert step in h.record["failed_step"]
    assert step in h.record["error_message"]
    _assert_no_report_was_recorded(h.record)
    # A partial run still ran to its end: the last step was attempted.
    assert "run_gclid_match" in h.calls
    assert "write_campaigns" not in h.calls
    # The durable `runs` row is never updated to anything but `partial`. (Where
    # the leads/deals write raised and the job dropped its run id there is no
    # row to update, exactly as before this slice; the row keeps the
    # pessimistic `failed` it was inserted with. A run that never got a row at
    # all is `failed` — test_09a.)
    assert {rec["status"] for _, rec in h.updated} <= {"partial"}
    lost_run_id = (
        stub == "write_leads" and isinstance(behaviour, BaseException))
    if not lost_run_id:
        assert [(rid, rec["status"]) for rid, rec in h.updated] == [(7, "partial")]


@pytest.mark.parametrize("job", sorted(JOBS))
def test_09a_a_run_with_no_run_row_is_failed_not_partial(job, harness):
    """`write_run` returns no id — the usual cause is an unreachable database.
    The run still reaches its end, but it has no durable record of itself, so
    it cannot claim that real work landed. The first version of this slice
    recorded `partial`, which monitoring reads as "some datasets were
    incomplete" for a run in which nothing was stored."""
    h = harness(job)
    h.overrides["write_run"] = None
    outcome = h.run()
    assert outcome["status"] == "failed"
    assert "write_run" in outcome["failed_steps"]
    assert h.record["status"] == "failed"
    assert "write_run" in h.record["failed_step"]
    _assert_no_report_was_recorded(h.record)
    # There is no row to update: the only update attempted carries no run id,
    # and it does not say `partial` or `success`.
    assert [rid for rid, _ in h.updated] == [None] * len(h.updated)
    assert {rec["status"] for _, rec in h.updated} <= {"failed"}
    assert "write_campaigns" not in h.calls


@pytest.mark.parametrize("job", sorted(JOBS))
def test_09b_a_failed_search_term_sync_withholds_the_waste_analysis(job, harness):
    """The chain PR-ADS-156-F2 built, still intact after the report steps were
    cut out around it: no rows adopted, waste detection told the evidence is
    unavailable, the waste batch finished `failed`, the snapshot left alone."""
    h = harness(job)
    h.overrides["sync_recent_search_terms"] = {"ok": False, "error": "x"}
    outcome = h.run()
    assert h.waste_args == ([], False)
    assert h.batch_status(("analysis", "waste_terms")) == ["failed"]
    assert "write_waste_terms" not in h.calls
    assert h.calls.count("google_ads_save") == 1      # search terms not saved
    assert {"sync_recent_search_terms",
            "run_waste_detection / write_waste_terms"} <= set(outcome["failed_steps"])


FAILED_CASES = [
    ("google ads pull raises", "pull_campaign_performance", "Step 1/4"),
    ("hubspot pull raises", "pull_paid_search_contacts", "Step 2/4"),
    ("waste detection raises", "run_waste_detection", "Step 3/4"),
]


@pytest.mark.parametrize("job", sorted(JOBS))
@pytest.mark.parametrize("case_id, stub, step", FAILED_CASES,
                         ids=[c[0] for c in FAILED_CASES])
def test_10_a_step_the_run_cannot_continue_past_is_recorded_failed(
        job, case_id, stub, step, harness):
    """Unchanged by this slice: these three ended `failed` before it, and
    still do. The weekly job raises; the monthly job returns the outcome."""
    h = harness(job)
    h.overrides[stub] = RuntimeError("upstream down")
    if job == "weekly":
        with pytest.raises(RuntimeError, match="upstream down"):
            h.run()
    else:
        outcome = h.run()
        assert outcome["status"] == "failed"
        assert outcome["failed_steps"] == [h.record["failed_step"]]
        assert step in h.record["failed_step"]
    assert h.record["status"] == "failed"
    assert "upstream down" in h.record["error_message"]
    _assert_no_report_was_recorded(h.record)
    assert "run_gclid_match" not in h.calls


def test_10b_the_monthly_job_still_fails_on_a_missing_connector_snapshot(
        harness, tmp_path):
    h = harness("monthly")
    (tmp_path / "data" / "crm_contacts.json").unlink()
    outcome = h.run()
    assert outcome["status"] == h.record["status"] == "failed"
    assert h.record["failed_step"] == "pre-analysis data validation"
    assert "data/crm_contacts.json" in h.record["error_message"]
    assert "run_waste_detection" not in h.calls


def test_10c_a_missing_report_can_no_longer_fail_the_monthly_run():
    """It was recorded `failed` when the advisor returned no path or the file
    was not on disk. A run that makes no report has nothing of the kind to
    check."""
    src = (ROOT / "scheduler/monthly.py").read_text(encoding="utf-8")
    fn = _job_function("monthly")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "report_path" not in names
    for gone in ("Advisor returned no report path", "Report file missing",
                 "Report file not found"):
        assert gone not in src, gone


def test_11_the_run_outcome_helper_has_exactly_three_answers():
    from scheduler.sync_utils import note_step_failure, run_outcome
    assert run_outcome([]) == ("success", None, None)
    failures: list = []
    note_step_failure(failures, "write_geo", "wrote 0 of 4 fetched rows")
    note_step_failure(failures, "record_flag_history", RuntimeError("x" * 900))
    status, steps, message = run_outcome(failures)
    assert status == "partial"
    assert steps == "write_geo; record_flag_history"
    assert message.startswith("write_geo: wrote 0 of 4 fetched rows; ")
    assert len(message) < 700                     # each detail is kept short
    # `failed` is for a run that did not reach its end (recorded by the job
    # where it stops) — and for one that reached its end with NO run row.
    # Partial claims real work landed; a run with no durable record of itself
    # cannot show that. With the database unreachable every step fails and the
    # first version of this helper still answered `partial`.
    assert "failed" not in {run_outcome(f)[0] for f in ([], failures)}
    no_row: list = []
    note_step_failure(no_row, "write_run", "no run id returned")
    note_step_failure(no_row, "sync_recent_search_terms", "db unavailable")
    status, steps, _message = run_outcome(no_row)
    assert status == "failed"
    assert steps == "write_run; sync_recent_search_terms"


def test_11b_the_jobs_record_a_missing_run_row_under_the_name_the_helper_reads():
    """The rule above keys on one step name. If a job recorded the missing run
    row under another name, the run would quietly go back to `partial`."""
    from scheduler import sync_utils
    for rel in ("scheduler/weekly.py", "scheduler/monthly.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert re.search(
            r'note_step_failure\(\s*step_failures,\s*"write_run"', src), rel
    assert sync_utils.RUN_ROW_STEP == "write_run"


def test_11c_the_kept_steps_keep_their_windows():
    """A truth review changed three literals in the kept steps (the search-term
    window, the keyword-facts window, a batch's sync type) and every test
    still passed. The windows are part of what "kept" means."""
    weekly = (ROOT / "scheduler/weekly.py").read_text(encoding="utf-8")
    monthly = (ROOT / "scheduler/monthly.py").read_text(encoding="utf-8")
    assert 'sync_recent_search_terms("weekly", days=60' in weekly
    assert 'sync_recent_search_terms("monthly", days=30' in monthly
    for name, src in (("weekly", weekly), ("monthly", monthly)):
        assert re.search(r"sync_keyword_daily_facts\(\s*run_date - "
                         r"timedelta\(days=29\)", src), name
        assert src.count("timedelta(days=29)") == 1, name
        # every sync batch this job opens is stamped with the job's own type
        types = set(re.findall(r'sync_type="([a-z_]+)"', src))
        assert types == {name}, (name, types)


# ═════════════════════════════════════════════════════════════════════════════
# 4 — the callers republish the outcome
# ═════════════════════════════════════════════════════════════════════════════
def _client(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    return TestClient(server.app), {"Authorization": "Bearer secret-token"}


OUTCOMES = [
    ({"run_type": "x", "status": "success", "failed_steps": []}, "success", []),
    ({"run_type": "x", "status": "partial", "failed_steps": ["write_geo"]},
     "partial", ["write_geo"]),
    ({"run_type": "x", "status": "failed",
      "failed_steps": ["Step 2/4: HubSpot pull"]}, "failed",
     ["Step 2/4: HubSpot pull"]),
    # Fail closed: not raising is not evidence of success.
    (None, "failed", ["run outcome missing"]),
    ("outputs/weekly_report_2026-11-16.md", "failed", ["run outcome missing"]),
    ({"status": "done"}, "failed", ["run outcome missing"]),
]


@pytest.mark.parametrize("job", sorted(JOBS))
@pytest.mark.parametrize("outcome, status, steps", OUTCOMES,
                         ids=["success", "partial", "failed", "none",
                              "a report path", "unknown status"])
def test_12_the_manual_run_endpoint_reports_the_runs_own_outcome(
        job, outcome, status, steps, monkeypatch):
    """`/run/monthly` answered `success` for a run that had recorded itself
    `failed` and returned None — "did not raise" was the whole test."""
    client, auth = _client(monkeypatch)
    monkeypatch.setattr(f"scheduler.{job}.{JOBS[job][1]}", lambda: outcome)
    body = client.post(f"/run/{job}", headers=auth).json()
    assert body["status"] == status
    assert body["job"] == job
    assert body["result"] == {"failed_steps": steps}
    assert "report_path" not in json.dumps(body)
    assert ("error" in body) == (status == "failed")


@pytest.mark.parametrize("job", sorted(JOBS))
def test_12b_a_run_that_raises_is_still_reported_failed(job, monkeypatch):
    client, auth = _client(monkeypatch)

    def boom():
        raise RuntimeError("upstream down")
    monkeypatch.setattr(f"scheduler.{job}.{JOBS[job][1]}", boom)
    body = client.post(f"/run/{job}", headers=auth).json()
    assert body["status"] == "failed"
    assert body["error"] == "RuntimeError: scheduler execution failed"


@pytest.mark.parametrize("job", sorted(JOBS))
def test_13_the_scheduled_wrapper_does_not_log_a_non_success_as_succeeded(
        job, monkeypatch, caplog):
    import logging
    import api.scheduler as scheduler
    wrapper = getattr(scheduler, f"_run_{job}_scheduled")
    for outcome, level, text in (
            ({"status": "success", "failed_steps": []}, logging.INFO, "succeeded"),
            ({"status": "partial", "failed_steps": ["write_geo"]},
             logging.WARNING, "finished partially — did not land: write_geo"),
            ({"status": "failed", "failed_steps": ["Step 1/4"]},
             logging.ERROR, "failed at: Step 1/4"),
            (None, logging.ERROR, "returned no recognisable outcome")):
        caplog.clear()
        monkeypatch.setattr(f"scheduler.{job}.{JOBS[job][1]}", lambda o=outcome: o)
        with caplog.at_level(logging.INFO, logger="api.scheduler"):
            wrapper()
        lines = [(r.levelno, r.getMessage()) for r in caplog.records
                 if f"[scheduler/{job}]" in r.getMessage()
                 and "started" not in r.getMessage()]
        assert lines == [(level, f"[scheduler/{job}] {text}")] or (
            outcome is None and lines[0][0] == level and text in lines[0][1])
        if not (isinstance(outcome, dict) and outcome["status"] == "success"):
            assert not any("succeeded" in m for _, m in lines)
        assert scheduler._job_state[job] is False             # noqa: SLF001
    # Job ids and schedules are untouched: monitoring is keyed on them.
    assert {"weekly", "monthly"} <= set(scheduler.JOB_IDS)
    assert scheduler._SCHEDULE_DESCRIPTIONS["weekly"].startswith("Monday 07:00")   # noqa: SLF001
    assert scheduler._SCHEDULE_DESCRIPTIONS["monthly"].startswith("1st of month 08:00")  # noqa: SLF001


def test_14_the_weekly_and_monthly_cadences_are_still_monitored():
    """The jobs were kept partly so these two cadences do not warn forever.
    A success run is green; a partial latest run is yellow and does not move
    `last_success_at` — the rule monitoring already applies everywhere."""
    from api.monitoring import (MONITORING_CADENCES, RUN_TYPE_CADENCE,
                                compute_monitoring_status, monitoring_cadence)
    assert {"weekly", "monthly"} <= set(MONITORING_CADENCES)
    assert RUN_TYPE_CADENCE["weekly"] == "weekly"
    assert monitoring_cadence("monthly") == "monthly"

    now = datetime.now(timezone.utc)

    def run(run_type, status, days_ago):
        ts = (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        return {"run_type": run_type, "status": status, "started_at": ts,
                "finished_at": ts}
    base = [run("daily", "success", 0), run("daily_incremental_sync", "success", 0)]
    thresholds = {"daily": 2, "daily_incremental_sync": 2, "weekly": 8,
                  "monthly": 35}
    healthy = compute_monitoring_status(
        base + [run("weekly", "success", 1), run("monthly", "success", 10)],
        thresholds, 2)
    assert healthy["severity"] == "green" and healthy["warnings"] == []

    partial = compute_monitoring_status(
        base + [run("weekly", "partial", 1), run("weekly", "success", 6),
                run("monthly", "success", 10)], thresholds, 2)
    assert partial["severity"] == "yellow"
    weekly = partial["latest_runs"]["weekly"]
    assert weekly["latest_partial"] is True
    assert weekly["consecutive_failures"] == 0
    assert weekly["last_success_at"] == run("weekly", "success", 6)["finished_at"]
    assert "Weekly run completed partially" in " ".join(partial["warnings"])


def test_15_the_scheduler_page_prints_no_report_path_and_names_a_partial_run():
    assert "report_path" not in JS
    assert "Report:" not in JS.split("async function triggerRun")[1].split(
        "\nfunction ")[0]
    trigger = JS.split("async function triggerRun")[1].split("\nfunction ")[0]
    assert 'data.status === "success"' in trigger
    assert 'data.status === "partial"' in trigger
    assert "data.result.failed_steps" in trigger
    assert 'showSchedFeedback("warning"' in trigger
    assert ".run-feedback--warning" in CSS
    for word in ("delivery", "emailed", "sendgrid"):
        assert word not in trigger.lower(), word
    # The endpoint no longer builds one either (the daily pulse still returns
    # the path of its own JSON result; that is not the advisor report).
    for job in sorted(JOBS):
        handler = SERVER.split(f'@app.post("/run/{job}")')[1].split("@app.")[0]
        assert "report_path" not in handler
        assert "_data_refresh_response" in handler


def test_16_readiness_and_preflight_no_longer_require_the_deleted_modules():
    import api.server as server
    assert not {"analysis.advisor", "analysis.rule_advisor",
                "scheduler.delivery"} & set(server._REQUIRED_MODULES)   # noqa: SLF001
    import importlib
    for name in server._REQUIRED_MODULES:                               # noqa: SLF001
        importlib.import_module(name)
    for rel in ("scripts/healthcheck.py", "scripts/phase1_readiness.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        strings = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for key in ("SENDGRID_API_KEY", "REPORT_SENDER_EMAIL",
                    "REPORT_RECIPIENT_EMAIL", "ANTHROPIC_API_KEY", "ADVISOR_MODE"):
            assert key not in strings, (rel, key)
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert not {"check_sendgrid", "check_advisor_mode",
                    "check_deterministic_advisor"} & defined, rel
    # Every file the readiness audit requires exists.
    import scripts.phase1_readiness as readiness
    for group in (readiness._REQUIRED_SCHEDULER_FILES,                  # noqa: SLF001
                  readiness._REQUIRED_ANALYSIS_FILES,                   # noqa: SLF001
                  readiness._REQUIRED_CONNECTOR_FILES,                  # noqa: SLF001
                  readiness._REQUIRED_API_FILES):                       # noqa: SLF001
        for rel in group:
            assert (ROOT / rel).exists(), rel
    import yaml
    render = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    keys = {e["key"] for e in render["services"][0]["envVars"]}
    assert not {"SENDGRID_API_KEY", "REPORT_SENDER_EMAIL",
                "REPORT_RECIPIENT_EMAIL"} & keys
    assert {"HUBSPOT_API_KEY", "ADMIN_API_TOKEN", "MAILCHIMP_API_KEY"} <= keys


# ═════════════════════════════════════════════════════════════════════════════
# 5 — the `campaigns` snapshot is no longer a dataset
# ═════════════════════════════════════════════════════════════════════════════
RETIRED = ("google_ads_api", "campaigns")


def test_17_the_campaigns_snapshot_is_not_a_freshness_dataset_and_spend_is():
    from services import freshness_service as fresh
    from api.server import _known_dataset_pairs
    config = fresh.DATASET_FRESHNESS_CONFIG
    assert "campaigns" not in config
    assert not [k for k, c in config.items()
                if (c.get("source"), c.get("dataset")) == RETIRED]
    assert not [k for k, c in config.items() if c.get("table") == "campaigns"]
    assert RETIRED not in _known_dataset_pairs()
    assert fresh.is_retired_dataset(*RETIRED) is True
    assert set(fresh.RETIRED_DATASETS) == {RETIRED}

    # The dataset the Campaigns page reads is untouched and still reported.
    spend = config["canonical_spend"]
    assert spend["table"] == "google_ads_campaign_daily_spend"
    assert spend["date_column"] == "spend_date"
    assert (spend["source"], spend["dataset"]) == ("google_ads_api",
                                                   "canonical_spend")
    assert spend["page"] == "campaigns" and spend["stale_threshold_days"] == 8
    assert ("google_ads_api", "canonical_spend") in _known_dataset_pairs()

    # Mailchimp stamps a dataset that is also spelled "campaigns". It is a
    # different key and must not be swept up with the retired one.
    assert config["mailchimp_campaigns"]["dataset"] == "campaigns"
    assert fresh.is_retired_dataset("mailchimp", "campaigns") is False
    assert ("mailchimp", "campaigns") in _known_dataset_pairs()
    # The other legacy snapshots these jobs still write stay registered.
    for key in ("keywords", "geo", "leads", "deals", "waste_terms",
                "gclid_attribution", "gclid_coverage_snapshots", "keyword_facts",
                "search_terms"):
        assert key in config, key


def test_18_system_status_names_no_dataset_the_freshness_config_lacks():
    """The general rule the retired entry broke: every dataset System Status
    reasons about must be one the freshness config can produce a status for.
    A core dataset with no status is silently ignored; one with a status and
    no writer is permanently stale."""
    from services import system_status_service as status
    from services.freshness_service import DATASET_FRESHNESS_CONFIG as config
    assert "campaigns" not in status.CORE_DATASETS
    assert "campaigns" not in status.PIPELINE_DEPENDENCIES
    assert "campaigns" not in status.SOURCE_DEFINITIONS["google_ads_api"]["datasets"]
    assert status.PAGE_PIPELINE_IMPACT["campaigns"] == ["canonical_spend"]
    assert "campaigns" not in status.PAGE_PIPELINE_IMPACT["dashboard"]
    assert "canonical_spend" in status.CORE_DATASETS
    assert "canonical_spend" in status.PIPELINE_DEPENDENCIES

    named = set(status.CORE_DATASETS) | set(status.DERIVED_DATASETS)
    named |= set(status.PIPELINE_DEPENDENCIES)
    for definition in status.SOURCE_DEFINITIONS.values():
        named |= set(definition["datasets"])
    for deps in status.PAGE_PIPELINE_IMPACT.values():
        named |= set(deps) - {"all", "admin"}
    for pipeline in status.PIPELINE_DEPENDENCIES.values():
        named |= set(pipeline.get("depends_on") or [])
        named |= set(pipeline.get("blocks") or [])
    assert named - set(config) == set(), sorted(named - set(config))


def test_19_the_ui_depends_on_no_dataset_the_endpoint_no_longer_reports():
    assert "google_ads_api/campaigns" not in JS
    deps = JS.split("const PAGE_DEPENDENCIES = {")[1].split("\n};")[0]
    for page in ("dashboard", '"roas-campaigns"', '"unit-economics"', "campaigns"):
        line = next(ln for ln in deps.splitlines()
                    if ln.strip().startswith(f"{page}:"))
        assert '"google_ads_api/canonical_spend"' in line, line
    assert '"google_ads_api/canonical_spend": { page: "campaigns"' in JS
    assert '"google_ads_api/canonical_spend": "Campaign spend data"' in JS
    # The page map the Campaigns freshness strip reads is unchanged.
    assert ('campaigns:         ["google_ads_api/canonical_spend", '
            '"hubspot/contacts"]') in JS


class _FakeCursor:
    """A cursor over one fixed production-shaped state: the retired snapshot's
    sync_state row is 40 days old, its table is empty, and every registered
    dataset synced an hour ago with rows."""

    def __init__(self, state):
        self.state = state
        self.description = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        lower = text.lower()
        self.state["executed"].append(text)
        if "from sync_state" in lower:
            self.description = [(c,) for c in (
                "source", "dataset", "status", "last_successful_sync_at",
                "last_source_date", "last_batch_id", "error_message",
                "updated_at")]
            self._rows = list(self.state["sync_rows"])
        elif "from sync_batches" in lower:
            self.description = [(c,) for c in ("source", "dataset", "status",
                                               "row_count")]
            self._rows = []
        elif "count(*)" in lower:
            table = re.search(r"Identifier\('([^']+)'\)", text).group(1)
            self.state["counted"].append(table)
            self.description = [("count",)]
            self._rows = [(0 if table == "campaigns" else 5,)]
        else:
            self.description = [("status",), ("started_at",), ("finished_at",)]
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self.state)


@pytest.fixture
def production_shaped_db(monkeypatch):
    from services.freshness_service import DATASET_FRESHNESS_CONFIG
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(hours=1)
    old = now - timedelta(days=40)
    rows = [(c["source"], c["dataset"], "success", fresh, date.today(), 1,
             None, fresh) for c in DATASET_FRESHNESS_CONFIG.values()]
    rows.append((*RETIRED, "success", old, old.date(), 2, None, old))
    state = {"sync_rows": rows, "counted": [], "executed": [], "old": old}
    monkeypatch.setattr("db.connection.get_conn", lambda: _FakeConn(state))
    import api.server as server
    monkeypatch.setattr(server, "_war_room_cache", {})
    return state


def test_20_system_status_raises_no_stale_campaigns_blocker(
        production_shaped_db, monkeypatch):
    """The state production is in the day this ships: a `(google_ads_api,
    campaigns)` sync row that will only get older, and — taking the worst case
    for the old code — an empty `campaigns` table."""
    client, auth = _client(monkeypatch)
    body = client.get("/api/system/status-war-room?days=60", headers=auth).json()

    blockers = body["critical_blockers"]
    assert not [b for b in blockers if b["id"].startswith("campaigns_")], blockers
    assert not [b for b in blockers if "Campaigns data is stale" in b["title"]]
    assert "campaigns" not in {p["key"] for p in body["pipelines"]}
    google = next(s for s in body["sources"] if s["source"] == "google_ads_api")
    assert "campaigns" not in google["datasets"]
    assert "canonical_spend" in google["datasets"]
    assert not [i for i in body["page_impact"] if i["blocked_by"] == "campaigns"]
    # Nothing counted the retired table, and nothing else is wrong in this
    # state — so the status is clean, not merely free of one blocker.
    assert "campaigns" not in production_shaped_db["counted"]
    assert "google_ads_campaign_daily_spend" in production_shaped_db["counted"]
    assert body["overall_status"] == "ok", (body["overall_label"], blockers)
    assert blockers == []
    # The retired row does not date the source either: Google Ads last synced
    # an hour ago, not 40 days ago.
    assert google["last_successful_sync_at"] != production_shaped_db["old"].isoformat()

    # The dataset the Campaigns page reads is reported, and healthy.
    spend = next(p for p in body["pipelines"] if p["key"] == "canonical_spend")
    assert spend["canonical_status"] == "fresh_with_data"
    assert spend["rows_in_window"] == 5

    import api.server as server
    freshness = server.api_datasets_freshness(user={"username": "t"}, days=60)
    keys = [d["dataset_key"] for d in freshness["datasets"]]
    assert "google_ads_api/campaigns" not in keys
    assert "google_ads_api/canonical_spend" in keys
    assert [r["dataset_key"] for r in freshness["retired_datasets"]] == [
        "google_ads_api/campaigns"]
    assert not [d for d in freshness["datasets"]
                if d["severity"] in ("error", "warning")], freshness["datasets"]


def test_21_the_blocker_guard_goes_red_on_the_pre_change_registration(
        production_shaped_db, monkeypatch):
    """Counterfactual for test_20. Register the snapshot again exactly as it
    was, over the same database state, and the alarm this slice removes comes
    back — so test_20 passes because of the unregistration, not by accident."""
    from services import freshness_service as fresh
    from services import system_status_service as status
    before = {"campaigns": {
        "table": "campaigns", "date_column": "run_date",
        "source": "google_ads_api", "dataset": "campaigns",
        "stale_threshold_days": 8, "depends_on": [], "page": "campaigns"}}
    monkeypatch.setattr(fresh, "DATASET_FRESHNESS_CONFIG",
                        {**before, **fresh.DATASET_FRESHNESS_CONFIG})
    monkeypatch.setattr(fresh, "RETIRED_DATASETS", {})
    monkeypatch.setattr(status, "CORE_DATASETS",
                        frozenset({*status.CORE_DATASETS, "campaigns"}))
    client, auth = _client(monkeypatch)
    body = client.get("/api/system/status-war-room?days=60", headers=auth).json()
    assert "campaigns" in production_shaped_db["counted"]
    assert body["overall_status"] == "error"
    assert "core dataset unavailable or failed" in body["overall_label"]
    import api.server as server
    freshness = server.api_datasets_freshness(user={"username": "t"}, days=60)
    row = next(d for d in freshness["datasets"]
               if d["dataset_key"] == "google_ads_api/campaigns")
    assert row["canonical_status"] == "stale_and_empty"

    # And with rows still in the window — the first week after the last write —
    # it is the exact blocker named in the brief.
    from services.freshness_service import CanonicalFreshnessStatus
    blockers = status.compute_critical_blockers(
        {"campaigns": CanonicalFreshnessStatus.STALE_WITH_DATA})
    assert [(b["id"], b["title"]) for b in blockers] == [
        ("campaigns_stale", "Campaigns data is stale")]


def test_22_the_table_and_its_manual_writer_are_not_dropped():
    """Unregistering is not deleting. The DDL stays, and the admin backfill —
    the one remaining caller — still reaches the writer."""
    schema = (ROOT / "db/schema.py").read_text(encoding="utf-8")
    assert re.search(r"CREATE TABLE IF NOT EXISTS campaigns\b", schema)
    assert "DROP TABLE" not in schema.split("CREATE TABLE IF NOT EXISTS campaigns")[1][:2000]
    import db.writers as writers
    assert callable(writers.write_campaigns)
    backfill = (ROOT / "scripts/backfill_windsor.py").read_text(encoding="utf-8")
    assert "from db.writers import write_campaigns" in backfill
    callers = [str(p.relative_to(ROOT)) for p in _production_files()
               if "write_campaigns" in _called(ast.parse(
                   p.read_text(encoding="utf-8")))]
    assert callers == ["scripts/backfill_windsor.py"]
    # "campaigns" stays a valid sync dataset KEY: Mailchimp and the manual
    # backfill both stamp it.
    from services.dataset_keys import VALID_SYNC_DATASETS
    assert "campaigns" in VALID_SYNC_DATASETS


# ═════════════════════════════════════════════════════════════════════════════
# 6 — the doctrine registry
# ═════════════════════════════════════════════════════════════════════════════
def test_23_the_doctrine_inventory_is_clean_and_holds_no_report_entry():
    from analysis import sql_doctrine_registry as registry
    from scripts import audit_sql_doctrine_inventory as inventory
    static = inventory.build_static(ROOT)
    assert static["unclassified_occurrences"] == []
    assert static["registry_problems"] == []

    assert not hasattr(registry, "_REPORT") and not hasattr(registry, "_CLAUDE")
    consumers = {c["consumer"]: c for c in registry.CONSUMERS}
    assert not [n for n in consumers if "report pipeline" in n
                or "advisor" in n.lower()]
    rule_ids = {r["id"] for r in registry.RULES}
    assert not {i for i in rule_ids if i.startswith("report.")}
    assert "api.module" not in rule_ids
    # No rule, consumer, CPQL entry, decision surface or known conflict points
    # at code that no longer exists.
    for rule in registry.RULES:
        assert (ROOT / rule["path"]).exists(), rule["id"]
        assert rule["path"] not in DELETED_FILES, rule["id"]
    gone = ("determine_verdict", "run_campaign_truth", "run_lead_quality",
            "rule_advisor", "analysis/advisor.py")
    for entry in (*registry.CONSUMERS, *registry.CPQL_CONSUMERS,
                  *registry.DECISION_SURFACES,
                  *registry.KNOWN_CONTRACT_CONFLICTS):
        location = entry.get("code_location", "")
        assert not any(g in location for g in gone), location
        assert (ROOT / location.split(":")[0]).exists(), location
    assert not [d for d in registry.DECISION_SURFACES
                if "weekly" in d["surface"].lower()
                or "report" in d["surface"].lower()]
    assert "doctrine.third_snapshot_population" not in {
        k["id"] for k in registry.KNOWN_CONTRACT_CONFLICTS}

    # The snapshot writer is still in db/writers.py, so it is still registered
    # — as what it now is: reachable from the manual backfill only.
    writer = consumers["campaigns snapshot writer (db.writers.write_campaigns)"]
    assert "backfill" in writer["api_endpoint"]
    assert "scheduler" not in writer["api_endpoint"]
    assert "writers.campaigns" in rule_ids
    bound = [o for o in static["occurrences"]
             if o.get("rule_id") == "writers.campaigns"]
    assert bound and {o["symbol"] for o in bound} == {"write_campaigns"}
    # Nothing in analysis/core.py needs a binding any more.
    assert not [o for o in static["occurrences"]
                if o["path"] == "analysis/core.py"]


def test_24_no_ui_text_names_a_period_ratio_roas_or_cac():
    """The reader audit's structural checks, including check 18, on this
    tree: the reworded Scheduler feedback introduced neither word."""
    from scripts import audit_customer_revenue_reader_cutover as audit
    a = audit.Audit()
    audit.structural_checks(a)
    assert a.violations == [], a.violations
    assert a.checks and all(a.checks.values())
    trigger = JS.split("async function triggerRun")[1].split("\nfunction ")[0]
    assert not re.search(r"\bROAS\b|\bCAC\b", trigger)
