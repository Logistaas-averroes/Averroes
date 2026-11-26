"""PR-ADS-180C — the Pipeline funnel reads the undated-reach bound; withheld
cohort notes; unknown is not zero (docs/65).

What is guarded here (the PostgreSQL half is
``test_pr_ads_180c_pipeline_undated_bound_pg_integration``):

* **m3.** For the same window and the same contacts, Overview → Lifecycle
  Activity read "Leads at least 10 … 7 contacts reached this stage with no
  entry date" while Pipeline read "Entered Lead 10", exact: the Pipeline
  checked only the window's own rows. The Pipeline builder now reads
  ``fetch_undated_stage_reach`` in its snapshot and publishes, per stage,
  ``undated_contacts_could_belong`` and ``count_is_bound`` — the Activity
  block's fields, from the same two functions — and ``pipelineFunnelModel``
  applies the Activity rule itself (``stageEntryCountBounds``): one rule.
  Under a lead filter the all-source figure is an upper limit, never the
  subset's count: the scope's own gap decides only where nothing lies
  outside the window's rows.
* **Hardening.** A stage the server does not state complete
  (``count_is_bound`` missing) is a bound on the page.
* **Carried minor (a).** A cohort the mart withholds after the fact (a window
  with no settled day) kept the published cohort's coverage notes — "4
  cohort SQL contact(s) are proven by lifecycle stage…", a floor on the SQL
  count it withholds. The notes are rebuilt for the verdict, and the audit
  scans them.
* **Carried minor (b).** The legacy audit's "Included paid leads missing
  dates" read an unread count as 0. It is None, and the page says Unknown.

Every producer runs for real; the repository is replaced at its fetch
boundary only.

PR-ADS-182A (owner decision, docs/67): the Pipeline funnel is the acquisition
cohort (by create date), so it reads no stage-entry count and no undated
reach. The m3 cases are ported: the same contacts now read EXACT on Pipeline
(the cohort needs no stage-entry date) while Overview → Lifecycle Activity
keeps the bound; Pipeline reads identically to Overview's acquisition block;
the one stage-entry rule is Activity's. Removed with the Pipeline lead-filter
narrowing it tested: 01_d, 01_g, 08 (see docs/67 §6).
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date

from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
from tests.test_pr_ads_180b_acquisition_cohort_funnel import (
    JS, NOW, _contract, _f_row, _fn, _outcomes, _text, _window_fetch,
)

_EVENTS = ("lead", "mql", "sql", "opportunity", "customer")
_RANK = {"lead": 1, "marketingqualifiedlead": 2, "salesqualifiedlead": 3,
         "opportunity": 4, "customer": 5, "evangelist": 6}
_EVENT_RANK = {"lead": 1, "mql": 2, "sql": 3, "opportunity": 4, "customer": 5}


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures: the window's rows, and the undated reach over the same table
# ═════════════════════════════════════════════════════════════════════════════
def _reach(rows):
    """``fetch_undated_stage_reach`` over the SAME whole table, by its
    documented rule (the SQL is proven on PostgreSQL in the PG module): a
    contact whose stage implies the event and that has no date for it, ruled
    out only when created after the window AND no date of it falls in the
    window; ``outside_window_rows`` the part with no date in the window."""
    def read(start, end):
        counts = dict.fromkeys(_EVENTS, 0)
        outside = dict.fromkeys(_EVENTS, 0)
        for r in rows:
            dates = [date.fromisoformat(v) for k, v in r.items()
                     if k.startswith("date_entered_") and v]
            in_window = any((start is None or d >= start) and (end is None or d <= end)
                            for d in dates)
            created = date.fromisoformat(r["created_at"]) if r.get("created_at") else None
            if created and end and created > end and not in_window:
                continue
            for e in _EVENTS:
                if _RANK.get(r["lifecycle_stage"], 0) >= _EVENT_RANK[e] \
                        and not r.get(f"date_entered_{e}"):
                    counts[e] += 1
                    outside[e] += 0 if in_window else 1
        return {"available": True, "counts": counts, "outside_window_rows": outside}
    return read


def _outside_rows():
    """The auditor's probe (audit180a/probe_outside.py), window Q3 (last
    quarter at NOW): ten dated Leads; four reached Lead undated and entered
    MQL in OCTOBER; three sit at stage lead with no date at all. None of the
    seven is in Q3's rows."""
    rows = [_f_row(i, "marketingqualifiedlead", created="2026-07-01",
                   lead="2026-07-05", mql="2026-07-10") for i in range(10)]
    rows += [_f_row(50 + i, "marketingqualifiedlead", created="2026-07-01", mql="2026-10-15")
             for i in range(4)]
    rows += [_f_row(80 + i, "lead", created="2026-07-01") for i in range(3)]
    return rows


def _patch(monkeypatch, rows, undated):
    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as F
    monkeypatch.setattr(repo, "fetch_funnel_contacts", _window_fetch(rows))
    monkeypatch.setattr(repo, "fetch_undated_stage_reach", undated)
    monkeypatch.setattr(F, "_build_campaign_resolver",
                        lambda s, e: (F.default_campaign_resolver, True))
    monkeypatch.setattr(F, "_sync_state_block", lambda: {"available": True})


def _cohort_rows(funnel_rows):
    """The same contacts as ``fetch_acquisition_cohort_contacts`` rows (the
    cohort read), PR-ADS-182A: created date, stage, source, and the MQL / SQL /
    Opportunity / Customer entry dates as direct evidence."""
    from tests.test_pr_ads_182a_pipeline_cohort_funnel import row
    out = []
    for i, r in enumerate(funnel_rows):
        dates = {e: r.get(f"date_entered_{e}") for e in ("mql", "sql", "opportunity", "customer")
                 if r.get(f"date_entered_{e}")}
        out.append({**row(i, r["lifecycle_stage"], src=r.get("hs_analytics_source"),
                          label=r.get("hs_analytics_source_data_1"),
                          created=f"{r['created_at']}T10:00:00Z", **dates),
                    "contact_id": r["contact_id"]})
    return out


def _pipeline_cohort(monkeypatch, funnel_rows, *, missing_created_at=0):
    """PR-ADS-182A: the REAL Pipeline builder over the same contacts — its
    funnel is the acquisition cohort of Q3 (last quarter at NOW)."""
    from tests.test_pr_ads_182a_pipeline_cohort_funnel import cohort_contract, pipeline
    contract = cohort_contract(monkeypatch, _cohort_rows(funnel_rows),
                               missing_created_at=missing_created_at,
                               start=date(2026, 7, 1), end=date(2026, 9, 30), now=NOW)
    return contract, pipeline(monkeypatch, contract, window="last_quarter", now=NOW)


def _activity(monkeypatch, rows, *, undated=None, window="last_quarter"):
    from services import dashboard_overview_service as O
    _patch(monkeypatch, rows, undated if undated is not None else _reach(rows))
    funnel = O._lifecycle_funnel_block(window, now=NOW)
    return json.loads(json.dumps(O._lifecycle_activity_block(funnel, window, NOW), default=str))


def _stage_html(html):
    """slot → (value text, its "Lower bound: …" note or None)."""
    out = {}
    for m in re.finditer(r'<(div|button)[^>]*class="dash-funnel__stage[^"]*"[^>]*'
                         r'data-funnel-slot="([a-z]+)"[^>]*>(.*?)</\1>', html, re.S):
        body = m.group(3)
        value = re.search(r'dash-funnel__stage-value">(.*?)</span>\s*<span', body, re.S)
        note = re.search(r'dash-funnel__note--bound">(.*?)</span>', body, re.S)
        out[m.group(2)] = (_text(value.group(1)) if value else None,
                           _text(note.group(1)) if note else None)
    return out


def _render_pipeline(funnel):
    return _run_app("pipelineFunnelHtml(%s, null)" % json.dumps({"funnel": funnel}))


def _render_overview_acquisition(block):
    return _run_app("renderDashAcquisitionFunnel(%s)"
                    % json.dumps({"lifecycle_acquisition_funnel": block}))


def _render_activity(act):
    return _run_app("renderDashLifecycleActivity(%s)" % json.dumps({"lifecycle_activity": act}))


# ═════════════════════════════════════════════════════════════════════════════
# m3. Pipeline reads the same undated-reach bound as Overview Activity
# ═════════════════════════════════════════════════════════════════════════════
def test_01_a_the_pipeline_counts_the_seven_undated_contacts_exactly_where_activity_bounds(
        monkeypatch):
    """On 7cd5e22 the Pipeline read "Entered Lead 10", exact, while Activity
    read "at least 10" (seven contacts reached Lead with no Lead date and lie
    outside the window's rows); 180C made Pipeline "at least 10" too.
    PR-ADS-182A: the Pipeline funnel counts by create date, which needs no
    stage-entry date — the same seventeen contacts are counted EXACTLY there,
    while the stage-entry Activity count keeps its bound."""
    act = _activity(monkeypatch, _outside_rows())
    cells = _stage_html(_render_activity(act))
    assert cells["contacts"] == (
        "at least 10", "Lower bound: 7 contacts reached this stage with no entry date "
        "recorded in HubSpot and could belong to this window"), cells["contacts"]
    _contract, p = _pipeline_cohort(monkeypatch, _outside_rows())
    f = p["funnel"]
    assert f["publication"]["status"] == "published"
    assert (f["totals"]["contacts"], f["totals"]["reached_mql"]) == (17, 14)
    pipe = _stage_html(_render_pipeline(f))
    assert pipe["contacts"] == ("17", None), pipe
    assert pipe["mql"] == ("14", None), pipe
    assert all(note is None for _v, note in pipe.values()), pipe


def test_01_b_the_same_cohort_reads_identically_on_pipeline_and_overview(monkeypatch):
    """The one rule, now for the acquisition cohort: every column's value
    and bound reason, Overview's block against Pipeline's, over the same
    cohort — exact, and under a no-created-date bound. Its counterpart on
    the real table is PG case 01."""
    from services.dashboard_overview_service import _acquisition_funnel_block
    rows = _outside_rows() + [
        _f_row(300, "salesqualifiedlead", created="2026-07-01", lead="2026-07-08",
               mql="2026-07-09"),
        _f_row(301, "opportunity", created="2026-08-01")]
    for missing in (0, 6):
        contract, p = _pipeline_cohort(monkeypatch, rows, missing_created_at=missing)
        overview = _stage_html(_render_overview_acquisition(
            json.loads(json.dumps(_acquisition_funnel_block(contract), default=str))))
        pipeline = _stage_html(_render_pipeline(p["funnel"]))
        assert overview == pipeline, (missing, overview, pipeline)
        assert pipeline["contacts"][0] == ("19" if not missing else "at least 19")


def test_01_c_an_unreadable_reach_is_a_bound_on_every_activity_stage(monkeypatch):
    """PR-ADS-182A: the undated reach is Activity's alone now. Unread, every
    Activity stage is a bound — never exact."""
    act = _activity(monkeypatch, _outside_rows(),
                    undated=lambda s, e: {"available": False, "counts": None})
    cells = _stage_html(_render_activity(act))
    for slot, (value, note) in cells.items():
        assert value.startswith("at least"), (slot, value)
        assert "could not all be counted" in note, (slot, note)
    for st in act["stages"]:
        assert st["undated_contacts_could_belong"] is None, st["event"]
        assert st["count_is_bound"] is True, st["event"]

    def raises(s, e):
        raise RuntimeError("connection reset")
    act = _activity(monkeypatch, _outside_rows(), undated=raises)
    assert all(st["count_is_bound"] is True for st in act["stages"])


def test_01_e_one_rule_not_a_copy():
    """The stage-entry adapter calls ONE function, and the window-row rule
    is reached only through it. PR-ADS-182A: that adapter is Activity's;
    the Pipeline adapter is the acquisition adapter and reads no stage-entry
    rule at all."""
    rule = _fn("stageEntryCountBounds")
    assert "stageEntryLowerBounds(" in rule and "undated_contacts_could_belong" in rule
    assert "count_is_bound" in rule
    body = _fn("dashActivityFunnelModel")
    assert "stageEntryCountBounds(" in body
    assert "stageEntryLowerBounds(" not in body
    assert "undated_contacts_could_belong" not in body
    pipe = _fn("pipelineFunnelModel")
    assert "acquisitionFunnelModel(" in pipe
    for gone in ("stageEntryCountBounds(", "stageEntryLowerBounds(", "count_is_bound",
                 "undated_contacts_could_belong"):
        assert gone not in pipe, gone
    callers = [m.start() for m in re.finditer(r"stageEntryLowerBounds\(", JS)]
    # The definition and the one call inside stageEntryCountBounds.
    assert len(callers) == 2, len(callers)
    assert "function dashActivityLowerBounds(" not in JS
    # The backend: one rule in the funnel service; Overview calls it, and
    # Pipeline reads neither it nor the reach.
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    overview = (root / "services" / "dashboard_overview_service.py").read_text()
    pipeline = (root / "services" / "pipeline_service.py").read_text()
    assert "_funnel.stage_count_is_bound(" in overview
    assert "_funnel.undated_stage_reach(" in overview
    assert "stage_count_is_bound(" not in pipeline
    assert "undated_stage_reach(" not in pipeline
    for src in (overview, pipeline):
        assert "fetch_undated_stage_reach(" not in src


def test_01_f_a_stage_the_server_does_not_state_complete_is_a_bound():
    """Hardening (180B-F1 re-review): every other piece of evidence says the
    stage is exact — reconciled, coverage 0, undated reach measured 0 — but
    the stage carries no `count_is_bound`. On 7cd5e22 it read "MQLs 12"."""
    stage = {"event": "mql", "label": "MQLs", "entered": 12, "available": True,
             "missing_entry_date_contacts": 0, "undated_contacts_could_belong": 0,
             "previous_period": {"status": "no_comparison"}}
    act = {"available": True, "basis": "stage_entry_events", "stages": [stage],
           "conversions": [], "reconciliation": {"status": "reconciled", "reasons": []},
           "coverage": {"stage_reached_without_entry_date": {"mql": 0}},
           "undated_reach": {"available": True, "counts": {"mql": 0}}, "previous_period": {}}
    cells = _stage_html(_render_activity(act))
    assert cells["mql"][0] == "at least 12", cells
    assert "does not state this count complete" in cells["mql"][1]
    # Stated complete: exact.
    cells = _stage_html(_render_activity(
        {**act, "stages": [{**stage, "count_is_bound": False}]}))
    assert cells["mql"] == ("12", None)
    # Defence in depth: a stage whose undated reach is not measured (None) is
    # a bound even beside a server verdict that says complete. (PR-ADS-182A:
    # asserted on Activity, the one stage-entry funnel; the Pipeline half of
    # this case went with Pipeline's stage-entry funnel.)
    unmeasured = {**stage, "count_is_bound": False, "undated_contacts_could_belong": None}
    value, note = _stage_html(_render_activity({**act, "stages": [unmeasured]}))["mql"]
    assert value == "at least 12" and "could not all be counted" in note


# ═════════════════════════════════════════════════════════════════════════════
# Carried minor (a): withheld means absent — the coverage notes too
# ═════════════════════════════════════════════════════════════════════════════
_SQL_NOTE = re.compile(r"^\d+ cohort SQL contact")


def test_02_a_a_window_with_no_settled_day_keeps_no_cohort_sql_note(monkeypatch):
    """The published cohort's notes state its SQL gap (4 here). On 7cd5e22
    the withheld block carried that note on: a floor on the SQL count it
    withholds."""
    from scripts import audit_marketing_outcome_cohorts as audit
    from services.revenue_decision_mart import _withhold_cohort
    published = _contract(_outcomes(monkeypatch, missing_created_at=2))
    assert any(_SQL_NOTE.match(n) for n in published["metadata"]["coverage_notes"])
    c = _withhold_cohort(published, "no_settled_day_in_window")
    notes = c["metadata"]["coverage_notes"]
    assert not any(_SQL_NOTE.match(n) for n in notes), notes
    assert not any(re.match(r"^\d", n) and "no created date" not in n for n in notes), notes
    # The reason, and the table-wide disclosure, survive.
    assert notes[0].startswith("the cohort SQL count is unavailable (no_settled_day_in_window)")
    assert notes[1].startswith("2 canonical contact(s) have no created date")
    assert c["metadata"]["coverage_status"] == "cohort_population_not_proven"
    payload = {"cohort": c, "readiness": {"missing_contact_created_at_count": 2}}
    assert audit.mart_withheld_exposures(payload) == []
    assert audit.created_at_disclosure_problems(payload) == []


def test_02_b_the_audit_scans_the_notes_of_a_withheld_cohort(monkeypatch):
    """`mart_withheld_exposures` reads the notes: a withheld block quoting a
    cohort count there is an exposure. On 7cd5e22 it reported []."""
    from scripts import audit_marketing_outcome_cohorts as audit
    from services.revenue_decision_mart import _withhold_cohort
    published = _contract(_outcomes(monkeypatch, missing_created_at=2))
    c = _withhold_cohort(published, "no_settled_day_in_window")
    leaked = {**c, "metadata": {**c["metadata"],
                                "coverage_notes": published["metadata"]["coverage_notes"]}}
    found = audit.mart_withheld_exposures({"cohort": leaked})
    assert found == ["cohort.metadata.coverage_notes[0]"], found
    # Any other count-led note is one too; the no-created-date one is not.
    other = {**c, "metadata": {**c["metadata"], "coverage_notes": [
        "3 duplicate row(s) were collapsed to one contact each; …",
        "2 canonical contact(s) have no created date and belong to no acquisition window."]}}
    assert audit.mart_withheld_exposures({"cohort": other}) == [
        "cohort.metadata.coverage_notes[0]"]
    # Published: the notes may state the cohort's figures.
    assert audit.mart_withheld_exposures({"cohort": published}) == []


def test_02_c_the_cohorts_own_unpublished_notes_are_the_same_function(monkeypatch):
    """One function for both unpublished paths: the cohort's own verdict and
    the mart's after-the-fact withholding (`unpublished_coverage_notes`)."""
    from services import marketing_outcome_cohort_service as M
    from services.revenue_decision_mart import _withhold_cohort
    c = _contract(_outcomes(monkeypatch, fresh=False, missing_created_at=4))
    assert c["sql_status"] == "withheld"
    notes = c["metadata"]["coverage_notes"]
    assert notes == M.unpublished_coverage_notes(
        missing_created_at=4, publication=("withheld", "source_bootstrap_incomplete"))
    assert notes[0].startswith("the cohort SQL count is withheld (source_bootstrap_incomplete)")
    assert notes[1].startswith("4 canonical contact(s) have no created date")
    assert len(notes) == 2
    withheld = _withhold_cohort(_contract(_outcomes(monkeypatch, missing_created_at=4)),
                                "no_settled_day_in_window")
    assert withheld["metadata"]["coverage_notes"] == M.unpublished_coverage_notes(
        missing_created_at=4, publication=("unavailable", "no_settled_day_in_window"))
    # Unread: no number is stated (unknown is not zero, and not a note).
    assert M.unpublished_coverage_notes(
        missing_created_at=None, publication=("unavailable", "x")) == [
        "the cohort SQL count is unavailable (x), so no SQL count, SQL breakdown or CPQL "
        "is published."]


# ═════════════════════════════════════════════════════════════════════════════
# Carried minor (b): unknown is not zero
# ═════════════════════════════════════════════════════════════════════════════
@contextmanager
def _no_conn():
    yield None


def test_03_a_an_unread_missing_date_count_is_none_not_zero(monkeypatch):
    from db import revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: repo._unavailable("leads", {"event_date_safe": False}))
    monkeypatch.setattr(repo, "get_conn", _no_conn)
    grain = repo.fetch_lead_date_grain_health(None, date(2026, 11, 19))
    assert grain["available"] is False
    assert grain["missing_contact_created_at_count"] is None


def test_03_b_the_audit_and_revenue_health_say_unknown(monkeypatch):
    import tests.test_revenue_attribution_audit as t
    unread = {**t._grain(True), "available": False, "missing_contact_created_at_count": None}
    a = json.loads(json.dumps(t._audit(grain=unread), default=str))
    assert a["missing_contact_created_at_count"] is None
    absent = {k: v for k, v in t._grain(True).items() if k != "missing_contact_created_at_count"}
    a2 = json.loads(json.dumps(t._audit(grain=absent), default=str))
    assert a2["missing_contact_created_at_count"] is None

    def strip(audit):
        html = _run_app('_currentUser = {"username": "u", "role": "admin"}; '
                        "renderRevenueHealth(%s)" % json.dumps(audit))
        m = re.search(r"Included paid leads missing dates</span><strong>(.*?)</strong>", html, re.S)
        return _text(m.group(1)).strip()
    assert strip(a) == "Unknown"
    # A read zero is a measured 0.
    read = json.loads(json.dumps(t._audit(), default=str))
    assert read["missing_contact_created_at_count"] == 0
    assert strip(read) == "0"


def test_03_c_a_database_outage_leaves_every_revenue_health_count_unknown(monkeypatch):
    """PR-ADS-180C-F1 (minor 3): through a simulated outage (no connection
    anywhere), every legacy count the audit reports is None and Revenue
    Health says "Unknown" in each cell. On 98cd829 the excluded counts, the
    legacy exclusions and the per-source counts read 0 — "0" on the page."""
    import db.writers as writers
    from db import revenue_repository as repo
    import tests.test_revenue_attribution_audit as t
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: repo._unavailable("leads", {"event_date_safe": False}))
    monkeypatch.setattr(repo, "get_conn", _no_conn)
    monkeypatch.setattr(writers, "get_conn", _no_conn)
    grain = repo.fetch_lead_date_grain_health(None, date(2026, 11, 19))
    for key in ("missing_contact_created_at_count", "excluded_non_paid_count",
                "excluded_pseudo_campaign_count", "counts_by_source_type"):
        assert grain[key] is None, key
    assert writers.count_lead_exclusions() is None
    a = json.loads(json.dumps(t._audit(grain=grain), default=str))
    for key in ("missing_contact_created_at_count", "excluded_non_paid_count",
                "excluded_pseudo_campaign_count", "legacy_excluded_count",
                "counts_by_source_type"):
        assert a[key] is None, key
    html = _run_app('_currentUser = {"username": "u", "role": "admin"}; '
                    "renderRevenueHealth(%s)" % json.dumps(a))
    for label in ("Included paid leads missing dates",
                  "Legacy paid rows excluded from revenue truth",
                  "Excluded non-paid rows", "Excluded pseudo campaigns"):
        m = re.search(re.escape(label) + r"</span><strong>(.*?)</strong>", html, re.S)
        assert _text(m.group(1)).strip() == "Unknown", (label, m.group(1))

    # A count lookup that RAISES is unknown too, not 0.
    def boom():
        raise RuntimeError("connection reset")
    monkeypatch.setattr(writers, "count_lead_exclusions", boom)
    assert json.loads(json.dumps(t._audit(grain=grain), default=str))[
        "legacy_excluded_count"] is None


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-180C-F2 — the effective-date audit follows the shared window helper
# ═════════════════════════════════════════════════════════════════════════════
_FAKE_REPO = '''
EVENT_DATE_COLUMN = {}
FUNNEL_EVENTS = ()


def effective_date_sql(event):
    return event


def any_stage_date_in_window(date_exprs, start, end):
    return " OR ".join(%(helper_expr)s for e in FUNNEL_EVENTS), []


def fetch_funnel_contacts(start, end):
    window_clause, params = any_stage_date_in_window(None, start, end)
    return window_clause
'''


def _audit_fake_repo(tmp_path, helper_expr):
    from scripts import audit_lifecycle_sql_coverage as A
    repo_file = tmp_path / "crm_funnel_repository.py"
    repo_file.write_text(_FAKE_REPO % {"helper_expr": helper_expr})
    f = A.Findings()
    out = A.check_effective_date_consistency(f, repo_file=repo_file,
                                             readers=("fetch_funnel_contacts",))
    return out["readers"]["fetch_funnel_contacts"], [
        v for v in f.violations if v.startswith("effective_date_consistency")]


def test_05_a_the_audit_follows_a_helper_only_when_the_helper_is_shared(tmp_path):
    """The reader names no expression itself and delegates its window to the
    helper. It counts as shared ONLY when the helper's own source uses the
    shared expression and builds no bare column. A naive "any helper call is
    shared" rule passes the bare helper — this case fails it."""
    state, problems = _audit_fake_repo(tmp_path, 'f"f.{EVENT_DATE_COLUMN[event]}"')
    assert problems, "a helper building the bare column must not certify its reader"
    assert state == "bare"
    state, problems = _audit_fake_repo(tmp_path, "effective_date_sql(e)")
    assert (state, problems) == ("shared", [])
    # A helper that names the shared expression AND a bare column certifies
    # nothing: still a violation.
    state, problems = _audit_fake_repo(
        tmp_path, 'effective_date_sql(e) + f"{EVENT_DATE_COLUMN[event]}"')
    assert state == "bare" and problems
    # A helper that names neither: not shared.
    state, problems = _audit_fake_repo(tmp_path, "'f.created_at'")
    assert state == "bare" and problems


def test_05_b_the_real_readers_pass_and_the_helper_refuses_anything_but_shared():
    from db import crm_funnel_repository as repo
    from scripts import audit_lifecycle_sql_coverage as A
    f = A.Findings()
    out = A.check_effective_date_consistency(f)
    assert set(out["readers"].values()) == {"shared"}, out["readers"]
    assert not [v for v in f.violations if v.startswith("effective_date_consistency")]
    shared = [repo.effective_date_sql(e) for e in repo.FUNNEL_EVENTS]
    sql, params = repo.any_stage_date_in_window(shared, date(2026, 7, 1), date(2026, 9, 30))
    assert len(params) == 3 * len(repo.FUNNEL_EVENTS) and shared[0] in sql
    import pytest
    for wrong in ([f"f.{repo.EVENT_DATE_COLUMN[e]}" for e in repo.FUNNEL_EVENTS],
                  list(reversed(shared)), shared[:-1]):
        with pytest.raises(ValueError):
            repo.any_stage_date_in_window(wrong, None, None)


# ── F2 minor 2: a web request's snapshot waits briefly for the schema lock ──
def test_06_a_web_snapshots_wait_briefly_certification_keeps_its_wait(monkeypatch):
    import db.connection as C
    from services import canonical_crm_funnel_service as F
    assert C.WINDOW_SCHEMA_LOCK_WAIT_SECONDS == 60
    assert C.WEB_SNAPSHOT_LOCK_WAIT_SECONDS == 5
    seen = []
    monkeypatch.setattr(C, "pinned_read_snapshot",
                        lambda **kw: seen.append(kw) or __import__("contextlib").nullcontext())
    with F.one_read_snapshot():
        pass
    assert seen == [{"lock_wait_seconds": 5}]
    # Certification opens its window with the default (60 s) wait.
    from pathlib import Path
    memo = (Path(__file__).resolve().parent.parent / "services" / "snapshot_memo.py").read_text()
    assert "stack.enter_context(pinned_read_snapshot())" in memo


# ── F2 minor 3: the reconciliation summary's unread exclusion count ─────────
def test_07_the_reconciliation_summary_says_unknown_for_an_unread_exclusion_count():
    def cell(value):
        html = _run_app("renderReconResult(%s)" % json.dumps(
            {"status": "success", "dry_run": True,
             "summary": {"missing_event_dates": 3, "remaining_unresolved": 0,
                         "already_excluded_legacy": value}}))
        m = re.search(r"Already excluded legacy rows</span><strong>(.*?)</strong>", html, re.S)
        return _text(m.group(1)).strip()
    assert cell(None) == "Unknown"
    assert cell(0) == "0" and cell(3276) == "3,276"
