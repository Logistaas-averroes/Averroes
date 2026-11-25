"""PR-ADS-178B-F1 — truth audit round 1 on the one Pipeline page.

MAJOR-1: the working-status ("Disqualified / Other") breakdown called
``leadsCount``, which 178B deleted with the Leads page: the panel stayed on
"Loading…" with an unhandled rejection, and ``loadPipeline`` threw on a window
change with that view selected. Restored, and a static check that every
function ``app.js`` calls is defined.

Minors: m1 customers won is the window's all-source won population whatever
the lead filters (PG ``test_02``); m2 counterfactuals for three guards that
survived mutation (A1 the KPI half of ``pipeline_checks``, A2 the previous
window of ``period_change``, A3 the deal-count agreement of the average);
m3 the average compares the divisor ``deal_count`` and names a mismatch in
its own words; m4 no raw reason codes or HubSpot property names in the
funnel's text; m5 the SQL card says the lead filters do not narrow it; o1 a
stage count under partial evidence is "at least N", never exact-looking.

Fail-before / mutation evidence: ``docs/61`` §11.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_pr_ads_161b_marketing_outcome_cohorts import _VM_HARNESS, _run_app

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
JS = APP_JS.read_text(encoding="utf-8")

_SYNC_TAIL = "process.stdout.write(JSON.stringify(out === undefined ? null : out));"
_ASYNC_HARNESS = _VM_HARNESS.replace(
    _SYNC_TAIL,
    "Promise.resolve(out).then((v) => "
    "process.stdout.write(JSON.stringify(v === undefined ? null : v)));")
assert _ASYNC_HARNESS != _VM_HARNESS, "the 161B harness tail changed"


def _run_async(script: str, tmp_path, *, js: str | None = None):
    """Run ``script`` against the real app.js and AWAIT its final value. An
    unhandled rejection (a ReferenceError inside an async renderer) exits
    node non-zero — a failure, never a silent pass."""
    node = shutil.which("node")
    assert node, "node is required: CI runs `node --check static/app.js`"
    (tmp_path / "harness.js").write_text(_ASYNC_HARNESS, encoding="utf-8")
    (tmp_path / "app.js").write_text(js if js is not None else JS, encoding="utf-8")
    (tmp_path / "probe.js").write_text(script, encoding="utf-8")
    out = subprocess.run([node, str(tmp_path / "harness.js"), str(tmp_path / "app.js"),
                          str(tmp_path / "probe.js")], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-3000:]
    assert out.stdout, "the renderer never finished"
    return json.loads(out.stdout)


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


#: The working-status payload the operational endpoint answers.
_OPERATIONAL = {"available": True, "scope_label": "All Sources",
                "acquisition_group_label": "All sources",
                "counts": {"open_working": 17, "bad_fit": 0, "no_response": None}}


# ── MAJOR-1. the working-status view renders its counts ──────────────────────
def test_01_a_the_working_status_view_renders_its_counts_and_never_hangs(tmp_path):
    keys = re.findall(r"^  ([a-z_]+):", JS.split("const LEADS_STATUS_LABELS = {")[1]
                      .split("};")[0], re.M)
    assert {"open_working", "bad_fit", "no_response"} <= set(keys), keys
    out = _run_async("""
globalThis.__urls = [];
fetchJSON = async (url) => { __urls.push(url); return %s; };
_leadsView = LEADS_VIEW_OTHER;
(async () => {
  await leadsRenderActiveView();
  return { body: __els["leads-table-body"].innerHTML, urls: __urls };
})()""" % json.dumps(_OPERATIONAL), tmp_path)
    body = out["body"]
    assert any("/api/crm-funnel/operational-status?" in u for u in out["urls"])
    assert "Loading" not in body
    assert "<table" in body and "Working status" in body
    cells = dict(re.findall(r"<code>([a-z_]+)</code></td>\s*<td>(.*?)</td>", body, re.S))
    assert cells["open_working"] == "17"
    # A published zero is 0; a null count is the word, never 0 and never a dash.
    assert cells["bad_fit"] == "0"
    assert "Unavailable" in cells["no_response"] and "0" not in _text(cells["no_response"])
    # A status the endpoint does not list is a zero it counted (unchanged since 153C).
    assert cells["reseller"] == "0"


def test_01_b_a_window_change_with_the_working_status_view_selected_resolves(tmp_path):
    """The path the audit named: the view stays selected and the window (or a
    lead filter) changes — ``loadPipeline`` awaits the view's renderer."""
    out = _run_async("""
fetchJSON = async (url) => (url.indexOf("/api/pipeline?") === 0
  ? { page: "pipeline", window: { key: getRoasBusinessWindow() }, kpis: {},
      filters: {}, funnel: { available: false, events: {} } }
  : %s);
_leadsView = LEADS_VIEW_OTHER;
(async () => { await loadPipeline(); return __els["leads-table-body"].innerHTML; })()
""" % json.dumps(_OPERATIONAL), tmp_path)
    assert "<table" in out and "Loading" not in out


def test_01_c_leads_count_renders_unknown_as_the_word_never_zero():
    out = _run_app("[leadsCount(null), leadsCount(undefined), leadsCount(0), leadsCount(1234)]")
    assert "Unavailable" in out[0] and "Unavailable" in out[1]
    assert out[2] == "0" and out[3] == "1,234"


# ── MAJOR-1. every function app.js calls is defined ─────────────────────────
#: Called names a regex over the source finds that are not app functions:
#: browser globals the vm harness does not provide, and CSS / prose inside
#: strings (``var(--x)``, ``rotate(``, ``:not(``, "contact(s)", "date(s)").
_NOT_APP_CALLS = {"Blob", "Event", "var", "rotate", "not", "contact", "date"}
_KEYWORDS = set("if for while switch catch function return typeof new await async "
                "delete void in of do else case super import".split())


def _undefined_calls(source: str) -> list:
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    calls = set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\(", code)) - _KEYWORDS
    # Every name the source declares anywhere (a local, a parameter, an arrow
    # parameter) — over-approximated: a name declared ONLY as a local is a
    # local call, not a missing app function.
    local = set()
    for pat in (r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
                r"\bfunction\s+([A-Za-z_$][\w$]*)", r"([A-Za-z_$][\w$]*)\s*=>"):
        local |= set(re.findall(pat, code))
    for m in re.finditer(r"(?:function\s*[\w$]*\s*|(?<![\w$]))\(([^()]*)\)\s*(?:=>|\{)", code):
        local |= set(re.findall(r"[A-Za-z_$][\w$]*", m.group(1)))
    probe = ("(%s).filter((n) => { try { return eval('typeof ' + n) === 'undefined'; }"
             " catch (e) { return true; } })" % json.dumps(sorted(calls)))
    undefined_globally = _run_app(probe, js=source)
    return sorted(n for n in undefined_globally if n not in local and n not in _NOT_APP_CALLS)


def test_02_every_function_app_js_calls_is_defined():
    assert _undefined_calls(JS) == []


def test_02_b_the_scan_bites_a_deleted_function():
    """The scan is a guard: deleting a called function is found by name."""
    start = JS.index("\nfunction leadsCount(")
    end = JS.index("\n}\n", start) + 3
    assert "leadsCount" in _undefined_calls(JS[:start] + "\n" + JS[end:])


# ── m2 A1. the KPI half of pipeline_checks bites ─────────────────────────────
def _audit_inputs():
    pub = {"closed_won_deals": {"value": 2, "status": "published", "reason": None},
           "customers": {"value": 1, "status": "published", "reason": None},
           "revenue_usd": {"value": 5000.0, "status": "published", "reason": None}}
    truth = {"outcomes": {"closed_won_deals": 2, "customers": 1, "revenue_usd": 5000.0},
             "membership": {"deal_ids": ["d-1", "d-2"]}}
    payload = {"kpis": {"closed_won_deals": {"value": 2, "status": "published", "reason": None},
                        "customers": {"value": 1, "status": "published", "reason": None},
                        "won_revenue_usd": {"value": 5000.0, "status": "published",
                                            "reason": None}},
               "deals": [{"deal_id": "d-1"}, {"deal_id": "d-2"}]}
    return pub, truth, payload


def _violations(pub, truth, payload):
    from scripts import audit_customer_revenue_reader_cutover as rc
    a = rc.Audit()
    rc.pipeline_checks(a, "current_quarter", "pipeline", pub, truth, payload)
    return a.violations


@pytest.mark.parametrize("field", ["closed_won_deals", "customers", "won_revenue_usd"])
def test_03_a_a_published_kpi_that_is_not_the_canonical_value_is_a_violation(field):
    pub, truth, payload = _audit_inputs()
    assert _violations(pub, truth, payload) == []
    payload["kpis"][field] = {**payload["kpis"][field], "value": 3}
    assert any(f"kpis.{field}=3" in v for v in _violations(pub, truth, payload))
    # Published by the page while the canonical verdict is withheld: also caught.
    pub2, truth2, payload2 = _audit_inputs()
    canon = {"closed_won_deals": "closed_won_deals", "customers": "customers",
             "won_revenue_usd": "revenue_usd"}[field]
    pub2[canon] = {"value": None, "status": "withheld", "reason": "customer_identity_unresolved"}
    assert any(f"kpis.{field}" in v for v in _violations(pub2, truth2, payload2))


def test_03_b_a_withheld_kpi_with_another_reason_is_a_violation():
    pub, truth, payload = _audit_inputs()
    pub["customers"] = {"value": None, "status": "withheld",
                        "reason": "customer_identity_unresolved"}
    payload["kpis"]["customers"] = {"value": None, "status": "withheld",
                                    "reason": "canonical_ledger_stale"}
    assert any("kpis.customers withheld/canonical_ledger_stale" in v
               for v in _violations(pub, truth, payload))
    payload["kpis"]["customers"]["reason"] = "customer_identity_unresolved"
    assert _violations(pub, truth, payload) == []


# ── m2 A2. period_change reads the PREVIOUS window, like for like ────────────
#: now = 2026-11-25: each window's previous period, resolved by the canonical
#: resolver at the shifted reference (dashboard_overview_service).
_PREVIOUS = {
    "current_quarter": ("last_quarter", "2026-07-01", "2026-09-30"),
    "last_quarter": ("last_quarter", "2026-04-01", "2026-06-30"),
    "last_6_months": ("last_6_months", "2025-11-24", "2026-05-24"),
    "ytd": ("ytd", "2025-01-01", "2025-11-25"),
}


@pytest.mark.parametrize("window", sorted(_PREVIOUS))
def test_04_period_change_builds_the_previous_window_like_for_like(monkeypatch, window):
    from services import dashboard_deals_service as dds
    from services.canonical_contract import resolve_canonical_window
    now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
    seen = []

    def fake_build(key, now=None, **_):
        w = resolve_canonical_window(key, now=now)
        seen.append((key, w["start_date"], w["end_date"]))
        return {"kpis": {"closed_won_deals": {"value": 4, "status": "published"},
                         "sqls": {"value": None, "status": "withheld"}}}

    monkeypatch.setattr(dds, "build_pipeline", fake_build)
    cur = resolve_canonical_window(window, now=now)
    kpis = {"closed_won_deals": {"value": 6, "status": "published"}}
    pc = dds._build_period_change(window, now, kpis)
    assert seen == [_PREVIOUS[window]]
    key, start, end = seen[0]
    assert end < cur["start_date"], "the previous period ends before this one starts"
    if window != "current_quarter":   # a quarter in progress compares to the full previous one
        span = lambda a, b: (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
        assert abs(span(start, end) - span(cur["start_date"], cur["end_date"])) <= 3
    m = pc["metrics"]["closed_won_deals"]
    assert (m["current"], m["previous"]) == (6, 4)
    assert pc["metrics"]["sqls"]["status"] == "no_comparison"


# ── m2 A3 / m3. the average is over the published deals, or not shown ────────
def _avg(ledger, deals=3, revenue=900.0):
    from analysis import closed_won_truth as cwt
    from services.pipeline_service import _average_deal_value
    return _average_deal_value(ledger, cwt.metric(deals, cwt.PUBLISHED),
                               cwt.metric(revenue, cwt.PUBLISHED))


def test_05_the_average_deal_value_divides_by_the_published_deals_only():
    from services import pipeline_service as ps
    good = {"deal_count": 3, "closed_won_deals": 3, "won_revenue": 900.0,
            "average_deal_value": 300.0}
    assert _avg(good)["value"] == 300.0 and _avg(good)["status"] == "published"
    for bad in ({**good, "deal_count": 4, "average_deal_value": 225.0},   # the divisor
                {**good, "closed_won_deals": 4},
                {**good, "deal_count": None},
                {**good, "won_revenue": 1200.0}):
        m = _avg(bad)
        assert (m["value"], m["status"], m["reason"]) == (
            None, "unavailable", ps.REASON_AVERAGE_POPULATION_MISMATCH), bad
    m = _avg({**good, "average_deal_value": None})
    assert (m["status"], m["reason"]) == ("unavailable", ps.REASON_VERDICT_MISSING)
    assert ps.REASON_AVERAGE_POPULATION_MISMATCH != ps.REASON_VERDICT_MISSING


def test_05_b_every_pipeline_reason_has_plain_words_on_the_page():
    from services import pipeline_service as ps
    codes = [ps.REASON_AVERAGE_POPULATION_MISMATCH, ps.REASON_NO_DEALS_TO_AVERAGE,
             ps.REASON_VERDICT_MISSING, ps.REASON_DEAL_LIST_MISMATCH,
             ps.REASON_FUNNEL_UNREADABLE]
    labels = _run_app("%s.map((c) => [c, CANONICAL_REASON_LABELS[c] || null])"
                      % json.dumps(codes))
    for code, label in labels:
        assert label and "_" not in label, code


# ── the Pipeline page renderers ─────────────────────────────────────────────
from tests.test_pr_ads_178b_pipeline_one_page import _page, _payload  # noqa: E402


def _funnel(**over):
    f = _payload()["funnel"]
    return {**f, **over}


def _funnel_html(payload):
    summary, _ = _page(payload)
    return summary[summary.index('id="pipeline-funnel"'):]


# ── m4. no raw codes or HubSpot property names in the funnel's text ──────────
def test_06_the_funnel_says_reasons_in_words_and_keeps_property_names_in_titles():
    f = _funnel(reconciliation={"status": "partial", "reasons": [
        "missing_stage_entry_date", "unknown_lifecycle_stage", "campaign_identity_unavailable"]},
        coverage={"stage_reached_without_entry_date": {e: 0 for e in (
            "lead", "mql", "sql", "opportunity", "customer")}})
    html = _funnel_html(_payload(funnel=f))
    visible = _text(re.sub(r'\s(title|aria-label|data-[a-z-]+)="[^"]*"', "", html))
    assert not re.search(r"\b[a-z]+(_[a-z0-9]+)+\b", visible), visible
    assert "hs_v2_date_entered" not in visible
    assert 'title="HubSpot property: hs_v2_date_entered_sql"' in html
    assert "no entry date recorded in HubSpot" in visible
    assert "does not recognise" in visible


# ── m5. the SQL card is not narrowed by the lead filters, and says so ────────
def test_07_the_sql_card_says_the_lead_filters_do_not_narrow_it():
    note = lambda s: re.search(r'data-kpi="sqls">(.*?)data-kpi="deals"', s, re.S).group(1)
    plain, _ = _page(_payload())
    assert "data-kpi-note" not in note(plain)
    filt = _payload()
    filt["filters"] = {"scope": "google_ads_source", "acquisition_group": None,
                       "lead_filtered": True}
    summary, _ = _page(filt)
    assert "not narrowed by the lead filters" in _text(note(summary))
    assert "different population (all sources" in _text(_funnel_html(filt))


# ── o1. a partial stage count is a lower bound, never exact-looking ──────────
def test_08_a_partial_stage_count_is_at_least_n_with_the_reason():
    zero = {e: 0 for e in ("lead", "mql", "sql", "opportunity", "customer")}
    part = _funnel(reconciliation={"status": "partial", "reasons": ["missing_stage_entry_date"]},
                   coverage={"stage_reached_without_entry_date": {**zero, "sql": 3}})
    html = _funnel_html(_payload(funnel=part))
    stages = dict(re.findall(r'data-funnel-event="([a-z]+)".*?dash-funnel__stage-value">(.*?)</span>\s*<span',
                             html, re.S))
    assert "at least 11" in _text(stages["sql"])
    assert "3 contacts reached this stage with no entry date" in html
    for e in ("lead", "mql", "opportunity", "customer"):
        assert "at least" not in stages[e], e
    # The payload cannot say which stage: every stage is a lower bound.
    unknown = _funnel(reconciliation={"status": "partial", "reasons": ["missing_stage_entry_date"]})
    unknown.pop("coverage", None)
    html2 = _funnel_html(_payload(funnel=unknown))
    assert _text(html2).count("at least") == 5
    # Reconciled evidence: exact counts.
    assert "at least" not in _text(_funnel_html(_payload()))


# ── round 2. a conversion % beside a lower-bound stage is never exact ────────
def _conv(frm, to, pct):
    return {"from_event": frm, "to_event": to, "available": True,
            "basis": "cohort", "rate_pct": pct}


def _chips(html):
    return re.findall(r'<span class="dash-funnel__conv[^"]*"[^>]*>(.*?)</span>', html)


def test_09_a_conversion_beside_a_lower_bound_stage_is_never_shown_as_exact():
    """Truth audit round 2 (figure-affecting). ``cohort_conversion`` counts a
    contact as converted only when it carries a TO-stage date, so an undated
    TO stage undercounts the numerator: the rate is "at least X%". An undated
    FROM stage leaves the cohort itself incomplete: the rate is unknown ("—").
    On 2904d7f the chips read a bare "0%" / "40%" beside "at least N"."""
    zero = {e: 0 for e in ("lead", "mql", "sql", "opportunity", "customer")}
    convs = [_conv("lead", "mql", 50), _conv("mql", "sql", 40),
             _conv("sql", "opportunity", 0), _conv("opportunity", "customer", 25)]
    part = _funnel(conversions=convs,
                   reconciliation={"status": "partial", "reasons": ["missing_stage_entry_date"]},
                   coverage={"stage_reached_without_entry_date": {**zero, "opportunity": 5}})
    chips = _chips(_funnel_html(_payload(funnel=part)))
    # lead→mql, mql→sql: neither side is a bound — exact.
    assert chips[0] == "50%" and chips[1] == "40%", chips
    # sql→opportunity: the TO stage is a bound — "at least".
    assert chips[2] == "at least 0%", chips
    # opportunity→customer: the FROM stage is a bound — unknown.
    assert chips[3] == "—", chips


def test_09_b_reconciled_evidence_keeps_exact_rates():
    convs = [_conv("lead", "mql", 50), _conv("mql", "sql", 40),
             _conv("sql", "opportunity", 10), _conv("opportunity", "customer", 25)]
    chips = _chips(_funnel_html(_payload(funnel=_funnel(conversions=convs))))
    assert chips == ["50%", "40%", "10%", "25%"], chips
