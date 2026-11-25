"""PR-ADS-180A — one lifecycle funnel block on every page (docs/63).

The owner's request (task #13): "One funnel, one design, every page." Every
page that shows the lifecycle funnel — Overview (Lifecycle Activity, Lifecycle
Cohort), Pipeline and Channels — draws it with ONE renderer,
``funnelBlockHtml(model, opts)``, and one CSS block. The block draws and
decides nothing: each page's adapter reads its OWN response into a model.

The guards here:

* every funnel surface calls the one renderer, and no other function emits
  the block's markup classes (``dash-funnel`` / ``funnel-block``);
* the renderer's behaviour on published, zero, withheld, unavailable,
  lower-bound, unknown-from and not-on-this-page stages, evaluated with
  ``node`` on the EXTRACTED block section and nothing else;
* the metric-family label comes from the payload argument;
* no page-global read inside the renderer.

Fail-before evidence: every case fails on 9a09c6d (``funnelBlockHtml`` does not
exist there); the named mutations in docs/63 §6 are exercised in-module by the
``test_*_bites`` cases.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

_BLOCK_START = "// ══ PR-ADS-180A — the ONE lifecycle funnel block (docs/63)"
_BLOCK_END = "// ══ end of the funnel block"

#: The functions that RENDER the block's markup. Everything else in the
#: section is an adapter helper (it builds model pieces, never markup).
_RENDERERS = ("funnelBlockHtml", "funnelBlockFamily", "funnelBlockVerdictHtml",
              "funnelBlockStageHtml", "funnelBlockValueHtml", "funnelBlockConvHtml")

#: Every surface that shows a lifecycle funnel, and the one call it must make.
_SURFACES = ("renderDashLifecycleActivity", "renderDashLifecycleCohort",
             "pipelineFunnelHtml", "renderChannelFunnel")

#: The block's class tokens: `dash-funnel`, `dash-funnel__*`, `funnel-block`,
#: `funnel-block__*` — exact tokens (``dash-funnel-panel`` is not one).
_CLASS_TOKEN = re.compile(r"(?<![\w-])(?:dash-funnel|funnel-block)(?:__[a-z0-9-]+)?(?![\w-])")

_EVENTS = ("lead", "mql", "sql", "opportunity", "customer")
SENTINEL = 7919


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _section(js: str = JS) -> str:
    i = js.index(_BLOCK_START)
    return js[i:js.index(_BLOCK_END, i)]


def _fn(name: str, js: str = JS) -> str:
    m = re.search(r"\n(?:async )?function %s\(" % re.escape(name), js)
    assert m, f"function {name} not found"
    return js[m.start():js.index("\n}\n", m.start()) + 2]


def _js_extract(source: str, head: str) -> str:
    """A top-level `const X = {...};` by brace matching."""
    i = source.index(head)
    j = source.index("{", i)
    depth = 0
    for k in range(j, len(source)):
        depth += {"{": 1, "}": -1}.get(source[k], 0)
        if depth == 0:
            end = k + 1
            return source[i:end + (1 if source[end:end + 1] == ";" else 0)]
    raise AssertionError(head)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# ── the isolated harness: the block section, its verdict renderer, nothing else
#: What the block may legitimately call: the HTML escaper, the count format and
#: the reader-contract verdict renderer (with what IT needs). No page state.
_DEP_FUNCTIONS = ("escapeHtml", "fmtCount", "canonicalMetricHtml", "canonicalBlockingReason",
                  "canonicalReasonLabel", "countryReasonLabel", "crmBlockersHref",
                  "crmBlockerFamily")
_DEP_STRINGS = ("PERIOD_RATIO_LABEL", "PERIOD_SPEND_PER_CUSTOMER_LABEL", "PERIOD_RATIO_DISCLOSURE")
_DEP_CONSTS = ("CANONICAL_REASON_LABELS", "CANONICAL_BASIS_LABELS", "CRM_BLOCKER_FAMILY",
               "GEO_ROW_SPEND_REASONS", "GEO_COVERAGE_REASONS", "COUNTRY_TOTAL_REASONS")


def _isolated(expr: str, *, js: str = JS):
    """Evaluate ``expr`` against the block section ALONE (plus the verdict
    renderer it calls). A block that reached for any page global — a cohort,
    a verdict, a selection, a response — would throw a ReferenceError here."""
    node = shutil.which("node")
    assert node, "node is required: CI runs `node --check static/app.js`"
    parts = ["let _currentUser = null;"]
    parts += [re.search(rf"const {c} =[\s\S]*?\";", js).group(0) for c in _DEP_STRINGS]
    parts += [_js_extract(js, f"const {c} = ") for c in _DEP_CONSTS]
    parts += [_fn(f, js) for f in _DEP_FUNCTIONS]
    parts.append(_section(js))
    script = "\n".join(parts) + f"\nprocess.stdout.write(JSON.stringify({expr}));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def _stage(slot, key, label, value, status="published", reason=None, **extra):
    return {"slot": slot, "key": key, "label": label,
            "metric": {"value": value, "status": status, "reason": reason}, **extra}


def _model(**over):
    m = {"family": {"key": "lifecycle_stage_events", "basis": "stage_entry_date"},
         "verdict": {"status": "published"},
         "stages": [_stage("contacts", "lead", "Entered Lead", 120),
                    _stage("mql", "mql", "Entered MQL", 40),
                    _stage("sql", "sql", "Entered SQL", 11),
                    _stage("opportunity", "opportunity", "Entered Opportunity", 6),
                    _stage("customer", "customer", "Moved to Customer stage in HubSpot", 0)],
         "conversions": [{"kind": "exact", "pct": 33.3, "title": "t"},
                         {"kind": "at_least", "pct": 20, "title": "bound"},
                         {"kind": "unknown", "title": "Conversion unknown: why"},
                         {"kind": "unavailable", "title": "no rate", "basis": "no rate"}]}
    m.update(over)
    return m


def _cells(html: str) -> dict:
    """slot → the visible text of that stage cell."""
    out = {}
    for m in re.finditer(r'<(div|button)[^>]*class="dash-funnel__stage[^"]*"[^>]*'
                         r'data-funnel-slot="([a-z]+)"[^>]*>(.*?)</\1>', html, re.S):
        out[m.group(2)] = _text(m.group(3))
    return out


def _chips(html: str) -> list:
    return re.findall(r'<span class="dash-funnel__conv[^"]*"[^>]*>(.*?)</span>', html)


# ═════════════════════════════════════════════════════════════════════════════
# 1. every funnel surface calls the one renderer
# ═════════════════════════════════════════════════════════════════════════════
def test_01_a_every_funnel_surface_calls_the_one_renderer():
    for name in _SURFACES:
        assert "funnelBlockHtml(" in _strip_comments(_fn(name)), name
    # The pipeline adapter passes its selection as an ARGUMENT.
    assert "pipelineFunnelHtml(d, _leadsView)" in _fn("renderPipelinePage")
    assert "selectable: true, activeKey: activeEvent" in _fn("pipelineFunnelHtml")


def _surface_outputs():
    from tests.test_pr_ads_162i_channels_page_revamp import _payload as channels
    from tests.test_pr_ads_178b_pipeline_one_page import _payload as pipeline
    return _run_app(
        "[renderDashLifecycleActivity({lifecycle_activity: %s}),"
        " renderDashLifecycleCohort({lifecycle_cohort: %s}),"
        " pipelineFunnelHtml(%s, null), renderChannelFunnel(%s, {})]"
        % (json.dumps(_activity()), json.dumps(_cohort()), json.dumps(pipeline()),
           json.dumps({**channels(), "metric_family": "acquisition_cohort_outcomes",
                       "window_basis": "contact_created_at"})))


def test_01_b_every_surface_renders_exactly_one_block_with_the_five_columns_in_order():
    for html in _surface_outputs():
        assert html.count('class="funnel-block"') == 1
        assert re.findall(r'data-funnel-slot="([a-z]+)"', html) == [
            "contacts", "mql", "sql", "opportunity", "customer"]
        assert html.count('class="dash-funnel"') == 1


def _emitters(js: str) -> list:
    """(function, token) for every block class token in app.js CODE that is
    not inside one of the block's own renderer functions."""
    code_lines = js.split("\n")
    out = []
    current = "<top level>"
    allowed = set(_RENDERERS)
    for line in code_lines:
        m = re.match(r"(?:async )?function ([A-Za-z_$][\w$]*)\(", line)
        if m:
            current = m.group(1)
        if re.match(r"\s*(//|\*|/\*)", line):
            continue
        for tok in _CLASS_TOKEN.findall(line):
            if current not in allowed:
                out.append((current, tok))
    return out


def test_02_a_no_other_function_emits_the_funnel_markup():
    assert _emitters(JS) == []
    # The page shell carries none of it either, and the retired per-page
    # funnel styles are gone (one CSS block).
    assert not _CLASS_TOKEN.search(HTML)
    for gone in (".dash-activity {", ".dash-activity__", ".pipeline-funnel__stage",
                 ".channel-funnel-totals", ".dash-funnel__stage-share",
                 ".dash-funnel__stage-gap", ".dash-funnel__stage-delta",
                 ".leads-customer-figure__help"):
        assert gone not in CSS, gone
    assert ".dash-funnel {" in CSS and ".funnel-block {" in CSS


def test_02_b_the_emitter_scan_bites_a_second_renderer():
    """Mutation M1: a page that draws its own funnel cell again is found."""
    mutated = JS.replace(
        "function renderRevCustomerEfficiency(d) {",
        "function renderRevCustomerEfficiency(d) {\n"
        "  const strip = `<div class=\"dash-funnel__stage\">x</div>`;", 1)
    assert ("renderRevCustomerEfficiency", "dash-funnel__stage") in _emitters(mutated)


def test_02_c_the_old_per_page_renderers_are_deleted():
    for gone in ("function channelFunnelTotalsHtml(", "function renderRevConversionStrip(",
                 "function pipelineFunnelLowerBounds(", "DASH_FUNNEL_RAMP",
                 "dash-activity__", "pipeline-funnel__"):
        assert gone not in JS, gone


# ═════════════════════════════════════════════════════════════════════════════
# 3. the renderer's behaviour, evaluated on the extracted block alone
# ═════════════════════════════════════════════════════════════════════════════
def test_03_a_published_counts_render_and_a_published_zero_is_zero():
    html = _isolated("funnelBlockHtml(%s, {})" % json.dumps(_model()))
    cells = _cells(html)
    assert cells["contacts"].startswith("Entered Lead 120")
    assert cells["customer"] == "Moved to Customer stage in HubSpot 0", cells["customer"]
    assert "—" not in cells["customer"] and "Unavailable" not in cells["customer"]


def test_03_b_withheld_and_unavailable_stages_are_the_word_and_the_reason_never_a_dash():
    m = _model()
    m["stages"][1] = _stage("mql", "mql", "Entered MQL", None, "withheld",
                            "the cohort is not proven complete")
    m["stages"][2] = _stage("sql", "sql", "Entered SQL", None, "unavailable",
                            "the funnel sent no count for this stage")
    cells = _cells(_isolated("funnelBlockHtml(%s, {})" % json.dumps(m)))
    assert cells["mql"] == "Entered MQL Withheld the cohort is not proven complete"
    assert cells["sql"] == "Entered SQL Unavailable the funnel sent no count for this stage"
    for slot in ("mql", "sql"):
        assert "—" not in cells[slot] and not re.search(r"\d", cells[slot]), cells[slot]


def test_03_c_a_withheld_funnel_shows_no_figure_even_beside_a_faulty_value():
    """Withheld means absent: under an unpublished verdict the block reads no
    stage figure at all — a value a faulty backend left beside the verdict
    never reaches the page — and every column says the word."""
    m = _model(verdict={"status": "withheld", "reason": "the last contact sync failed"})
    for s in m["stages"]:
        s["metric"]["value"] = SENTINEL
        s["bound_reason"] = "a bound"
    html = _isolated("funnelBlockHtml(%s, {})" % json.dumps(m))
    assert str(SENTINEL) not in html and "at least" not in html
    assert "<strong>Withheld</strong> — the last contact sync failed." in html
    assert list(_cells(html).values()) == [
        "Entered Lead Withheld", "Entered MQL Withheld", "Entered SQL Withheld",
        "Entered Opportunity Withheld", "Moved to Customer stage in HubSpot Withheld"]
    assert _chips(html) == []          # no rate beside withheld counts


def test_03_d_a_lower_bound_is_at_least_n_with_its_reason():
    m = _model()
    m["stages"][2]["bound_reason"] = "3 contacts reached this stage with no entry date"
    html = _isolated("funnelBlockHtml(%s, {})" % json.dumps(m))
    cell = _cells(html)["sql"]
    assert cell.startswith("Entered SQL at least 11 Lower bound: 3 contacts"), cell
    assert 'data-lower-bound="sql"' in html
    assert re.search(r"(?<!at least )\b11\b", _text(html)) is None


def test_03_e_conversions_exact_at_least_unknown_unavailable_and_none():
    """178B-F2 exactly: into a lower-bound stage "at least X%"; out of a
    lower-bound or unknown stage "—" with the reason in its title; a missing
    rate is the word, never 0%; a page that publishes no rate draws a plain
    connector."""
    html = _isolated("funnelBlockHtml(%s, {})" % json.dumps(_model()))
    assert _chips(html) == ["33.3%", "at least 20%", "—", "Unavailable"]
    assert 'data-conv-bound="unknown" title="Conversion unknown: why">—<' in html
    # An exact chip whose rate is missing is unavailable, never "0%".
    m = _model(conversions=[{"kind": "exact", "pct": None}, {"kind": "at_least"},
                            {"kind": "none"}, {}])
    html = _isolated("funnelBlockHtml(%s, {})" % json.dumps(m))
    assert _chips(html) == ["Unavailable", "Unavailable"]
    assert "0%" not in html
    assert html.count("dash-funnel__link dash-funnel__link--plain") == 2


def test_03_f_a_column_the_page_does_not_carry_is_not_available_on_this_page():
    m = _model(stages=_model()["stages"][:3])        # no Opportunity, no Customer stage
    html = _isolated("funnelBlockHtml(%s, {})" % json.dumps(m))
    cells = _cells(html)
    for slot, caption in (("opportunity", "Opportunity"), ("customer", "Customer stage")):
        assert cells[slot].startswith(f"{caption} Not available on this page"), cells[slot]
        assert not re.search(r"\d", cells[slot]) and "—" not in cells[slot]


def test_03_g_a_model_with_no_verdict_fails_closed():
    m = _model()
    del m["verdict"]
    html = _isolated("funnelBlockHtml(%s, {})" % json.dumps(m))
    assert not re.search(r"\b(120|40|11|6)\b", _text(html))
    assert "<strong>Unavailable</strong>" in html


def test_03_h_the_renderer_computes_no_rate():
    """Every percentage on the block is the adapter's, read from the payload.
    The renderer functions contain no division (closing tags aside)."""
    for name in _RENDERERS:
        body = _strip_comments(_fn(name)).replace("</", "<")
        assert "/" not in body, name


def test_03_i_a_selectable_block_marks_the_stage_it_is_given():
    html = _isolated("funnelBlockHtml(%s, {selectable: true, activeKey: 'sql'})"
                     % json.dumps(_model()))
    assert re.findall(r'data-funnel-event="([a-z]+)" aria-pressed="(true|false)"', html) == [
        ("lead", "false"), ("mql", "false"), ("sql", "true"), ("opportunity", "false"),
        ("customer", "false")]


# ═════════════════════════════════════════════════════════════════════════════
# 4. the metric family is the payload's, labelled — never relabelled
# ═════════════════════════════════════════════════════════════════════════════
def test_04_a_the_family_label_comes_from_the_model_argument():
    out = _isolated("[%s].map((k) => funnelBlockHtml({...%s, family: {key: k, basis: 'contact_created_at'}}, {}))"
                    % (", ".join(json.dumps(k) for k in (
                        "lifecycle_stage_events", "acquisition_cohort_outcomes", None, "made_up")),
                       json.dumps(_model())))
    events, cohort, missing, unknown = out
    assert 'data-metric-family="lifecycle_stage_events"' in events
    assert "Lifecycle stage events: contacts that entered each stage" in events
    assert 'data-metric-family="acquisition_cohort_outcomes"' in cohort
    assert "Acquisition cohort: contacts created in the window" in cohort
    assert "Lifecycle stage events" not in cohort and "Acquisition cohort" not in events
    for html in (missing, unknown):        # never guessed
        assert "Metric family not stated by this response" in html
        assert "Lifecycle stage events" not in html and "Acquisition cohort" not in html


def test_04_b_each_surface_reads_its_family_from_its_own_payload():
    """The adapters copy the payload's metadata. Mutating the payload's
    family changes the label: nothing is hard-coded per page."""
    from tests.test_pr_ads_162i_channels_page_revamp import _payload as channels
    from tests.test_pr_ads_178b_pipeline_one_page import _payload as pipeline
    p = pipeline()
    swapped = dict(p, funnel={**p["funnel"], "metric_family": "acquisition_cohort_outcomes"})
    ch = {**channels(), "metric_family": "acquisition_cohort_outcomes",
          "window_basis": "contact_created_at"}
    out = _run_app(
        "[pipelineFunnelHtml(%s), pipelineFunnelHtml(%s), renderChannelFunnel(%s, {}),"
        " renderChannelFunnel(%s, {}), renderDashLifecycleActivity({lifecycle_activity: %s}),"
        " renderDashLifecycleActivity({lifecycle_activity: %s}),"
        " renderDashLifecycleCohort({lifecycle_cohort: %s}),"
        " renderDashLifecycleCohort({lifecycle_cohort: %s})]"
        % (json.dumps(p), json.dumps(swapped), json.dumps(ch),
           json.dumps({**ch, "metric_family": None}), json.dumps(_activity()),
           json.dumps({**_activity(), "basis": None}), json.dumps(_cohort()),
           json.dumps({**_cohort(), "anchor_event": "mql"})))
    fam = [re.search(r'data-metric-family="([a-z_]+)" data-window-basis="([a-z_]+)"', h).groups()
           for h in out]
    assert fam == [("lifecycle_stage_events", "stage_entry_date"),
                   ("acquisition_cohort_outcomes", "stage_entry_date"),
                   ("acquisition_cohort_outcomes", "contact_created_at"),
                   ("not_stated", "contact_created_at"),
                   ("stage_entry_events", "stage_entry_date"),
                   ("not_stated", "stage_entry_date"),
                   ("lead_entry_cohort", "date_entered_lead"),
                   ("not_stated", "date_entered_lead")]


def test_04_c_two_pages_two_families_side_by_side():
    """Overview's activity block and the Channels block are different SQL
    families; each says which, and neither borrows the other's words."""
    activity, _cohort_html, pipeline, channels = _surface_outputs()
    assert "Lifecycle stage events" in _text(activity) and "Acquisition cohort" not in _text(activity)
    assert "Acquisition cohort" in _text(channels) and "Lifecycle stage events" not in _text(channels)
    assert "Lifecycle stage events" in _text(pipeline)


def test_04_d_the_family_scan_bites_a_hard_coded_label():
    """Mutation M3: a block that prints one family whatever the argument says
    is caught by 04_a."""
    mutated = JS.replace('label: FUNNEL_FAMILY_LABELS[key] || "Metric family not stated by this response",',
                         'label: FUNNEL_FAMILY_LABELS.lifecycle_stage_events,', 1)
    assert mutated != JS
    html = _isolated("funnelBlockHtml({...%s, family: {key: 'acquisition_cohort_outcomes'}}, {})"
                     % json.dumps(_model()), js=mutated)
    assert "Acquisition cohort" not in html       # the guard in 04_a would fail


# ═════════════════════════════════════════════════════════════════════════════
# 5. no page-global reads inside the renderer
# ═════════════════════════════════════════════════════════════════════════════
def _page_state_names(js: str) -> set:
    """Mutable page state: every top-level `let`, and every top-level const
    object the code mutates in place (`X.y =` / `X[y] =`)."""
    names = set(re.findall(r"^let\s+([A-Za-z_$][\w$]*)", js, re.M))
    for const in re.findall(r"^const\s+([A-Za-z_$][\w$]*)\s*=\s*\{", js, re.M):
        if re.search(r"(?<![\w$.])%s(?:\.[\w$]+|\[[^\]]+\])\s*=[^=]" % re.escape(const), js):
            names.add(const)
    return names


def _global_reads(js: str) -> set:
    code = _strip_comments(_section(js))
    used = set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)", code))
    return used & _page_state_names(js)


def test_05_a_the_block_reads_no_page_state():
    assert {"_leadsView", "pipelineData", "dashOverview", "_channelsData",
            "_channelsView"} <= _page_state_names(JS)
    assert _global_reads(JS) == set()


def test_05_b_the_scan_bites_a_block_that_reads_the_selection():
    """Mutation M2: the block reads the Pipeline page's selection itself."""
    mutated = JS.replace(
        "const active = o.activeKey !== null && o.activeKey !== undefined && o.activeKey === s.key;",
        "const active = _leadsView === s.key;", 1)
    assert mutated != JS
    assert _global_reads(mutated) == {"_leadsView"}
    with pytest.raises(AssertionError):           # and it cannot run in isolation
        _isolated("funnelBlockHtml(%s, {selectable: true})" % json.dumps(_model()), js=mutated)


def test_05_c_page_state_does_not_change_the_block():
    """The same model renders the same block whatever the page globals hold."""
    model = json.dumps(_model())
    out = _run_app(
        "const a = funnelBlockHtml(%s, {selectable: true, activeKey: 'mql'});"
        "_leadsView = 'sql'; pipelineData = {funnel: {metric_family: 'x'}}; dashOverview = {};"
        "_channelsData = {metric_family: 'acquisition_cohort_outcomes'}; pipelineStatus = 'ok';"
        "[a, funnelBlockHtml(%s, {selectable: true, activeKey: 'mql'})]" % (model, model))
    assert out[0] == out[1]
    assert 'data-funnel-event="mql" aria-pressed="true"' in out[0]


def test_05_d_the_pipeline_selection_is_the_argument_not_the_global():
    from tests.test_pr_ads_178b_pipeline_one_page import _payload
    out = _run_app("_leadsView = 'mql'; [pipelineFunnelHtml(%s, 'sql'), pipelineFunnelHtml(%s, 'other')]"
                   % (json.dumps(_payload()), json.dumps(_payload())))
    pressed = re.findall(r'data-funnel-event="([a-z]+)" aria-pressed="true"', out[0])
    assert pressed == ["sql"]
    assert re.findall(r'aria-pressed="true"', out[1]) == []
    assert 'search-terms-tab--muted active"' in out[1]


# ═════════════════════════════════════════════════════════════════════════════
# 6. per-page truth the shared design must not erase
# ═════════════════════════════════════════════════════════════════════════════
def _activity(**over):
    stages = []
    for e, label, n in (("lead", "Leads", 352), ("mql", "MQLs", 553), ("sql", "SQLs", 42),
                        ("opportunity", "Opportunities", 46),
                        ("customer", "Moved to Customer stage", 17)):
        stages.append({"event": e, "label": label, "entered": n, "available": True,
                       "activity_label": f"{label} entered during this period",
                       "event_date_property": f"hs_v2_date_entered_{e}",
                       "missing_entry_date_contacts": 0,
                       "previous_period": {"status": "no_comparison"}})
    conv = [{"from_event": a, "to_event": b, "available": True, "basis": "cohort",
             "rate_pct": r, "cohort_size": c, "converted": k, "cohort_label": f"in the {lab} cohort",
             "from_label": lab, "to_label": to}
            for a, b, r, c, k, lab, to in (
                ("lead", "mql", 82.95, 352, 292, "Leads", "MQLs"),
                ("mql", "sql", 6.69, 553, 37, "MQLs", "SQLs"),
                ("sql", "opportunity", 11.9, 42, 5, "SQLs", "Opportunities"),
                ("opportunity", "customer", 2.17, 46, 1, "Opportunities", "Customer stage"))]
    a = {"available": True, "reason": None, "basis": "stage_entry_events",
         "monotonic_expected": False, "stages": stages, "conversions": conv,
         "reconciliation": {"status": "reconciled", "reasons": []},
         "coverage": {"stage_reached_without_entry_date": {e: 0 for e in _EVENTS}},
         "previous_period": {"label": "vs Last Quarter"}}
    a.update(over)
    return a


def _cohort(**over):
    stages = []
    prev = None
    for i, (e, label, n) in enumerate((("lead", "Leads", 100), ("mql", "MQLs", 50),
                                       ("sql", "SQLs", 10), ("opportunity", "Opportunities", 0),
                                       ("customer", "Moved to Customer stage", 0))):
        stages.append({"event": e, "label": label, "reached": n, "stage_index": i,
                       "rate_from_anchor_pct": float(n),
                       "previous_stage_conversion_pct": (
                           None if prev is None else (None if not prev else round(n * 100 / prev, 2))),
                       "event_date_column": f"date_entered_{e}",
                       "event_date_property": f"hs_v2_date_entered_{e}",
                       "excluded_contacts": 0, "exclusion_reasons": {}, "basis": "cohort"})
        prev = n
    c = {"available": True, "reason": None, "anchor_event": "lead", "anchor_label": "Leads",
         "anchor_definition": "Contacts whose Lead date falls in the window.",
         "basis": "cohort", "coverage_status": "complete", "excluded_contacts": 0,
         "exclusion_reasons": [], "stages": stages}
    c.update(over)
    return c


def test_06_a_overview_activity_keeps_its_canonical_rates_and_trend_inside_the_stage():
    html = _run_app("renderDashLifecycleActivity({lifecycle_activity: %s})" % json.dumps(_activity(
        stages=[{**s, "previous_period": {"status": "ok", "delta_pct": 0.1, "direction": "up"}}
                for s in _activity()["stages"]])))
    assert _chips(html) == ["82.95%", "6.69%", "11.9%", "2.17%"]
    assert "292 of 352 in the Leads cohort" in _text(html)
    # The trend is a note INSIDE each stage, never a connector.
    cells = _cells(html)
    assert all("+10.0% vs Last Quarter" in c for c in cells.values()), cells
    for link in re.findall(r'<div class="dash-funnel__link[^"]*"[^>]*>(.*?)</div>', html, re.S):
        assert "vs Last Quarter" not in link


def test_06_b_overview_activity_marks_a_lower_bound_like_pipeline():
    """PR-ADS-180A: the activity block shares the Pipeline's lower-bound rule
    (stageEntryLowerBounds) — the same stage-entry count on the same all-source
    scope no longer reads "42" on one page and "at least 42" on the other."""
    cov = {e: 0 for e in _EVENTS}
    cov["sql"] = 12
    html = _run_app("renderDashLifecycleActivity({lifecycle_activity: %s})" % json.dumps(_activity(
        reconciliation={"status": "partial", "reasons": ["missing_stage_entry_date"]},
        coverage={"stage_reached_without_entry_date": cov})))
    cells = _cells(html)
    assert cells["sql"].startswith("SQLs at least 42 "), cells["sql"]
    assert "Lower bound: 12 contacts reached this stage with no entry date" in cells["sql"]
    assert "at least" not in cells["mql"] and "at least" not in cells["opportunity"]
    # Into the bound: at least; out of it: unknown, with the reason.
    assert _chips(html) == ["82.95%", "at least 6.69%", "—", "2.17%"]


def test_06_c_overview_cohort_keeps_the_service_rates_and_the_anchor_share():
    html = _run_app("renderDashLifecycleCohort({lifecycle_cohort: %s})" % json.dumps(_cohort()))
    # previous_stage_conversion_pct whole; a null (empty previous stage) is the
    # word, never 0%.
    assert _chips(html) == ["50%", "20%", "0%", "Unavailable"]
    assert "50% of Lead cohort" in _cells(html)["mql"]


def test_06_d_unavailable_overview_blocks_say_why_and_show_no_count():
    out = _run_app(
        "[renderDashLifecycleActivity({lifecycle_activity: {available: false,"
        " reason: 'canonical_funnel_mismatch', basis: 'stage_entry_events', stages: []}}),"
        " renderDashLifecycleCohort({lifecycle_cohort: {available: false,"
        " reason: 'funnel_invariant_mismatch', anchor_event: 'lead', stages: []}})]")
    for html, why in zip(out, ("a funnel invariant broke, so no count is rendered",
                               "a funnel invariant broke, so no count is rendered")):
        assert f"<strong>Unavailable</strong> — {why}." in html
        assert list(_cells(html).values()) == [
            "Contacts Unavailable", "MQL Unavailable", "SQL Unavailable",
            "Opportunity Unavailable", "Customer stage Unavailable"]


def test_06_e_the_customer_column_is_the_lifecycle_stage_never_companies():
    """The fifth column counts CONTACTS moved to the Customer stage. Customers
    are distinct companies from closed-won deals — another population, by
    close date — and stay outside the funnel (PR-ADS-155 §2, 166A). No adapter
    reads a customer, deal or company figure, and no column is labelled as
    customers."""
    adapters = ("dashActivityFunnelModel", "dashCohortFunnelModel", "pipelineFunnelModel",
                "channelFunnelModel")
    for name in adapters:
        body = _strip_comments(_fn(name))
        for figure in (".customers", "customers_won", "closed_won", "company"):
            assert figure not in body, (name, figure)
    for html in _surface_outputs():
        label = re.search(r'data-funnel-slot="customer"[^>]*>\s*<span class="dash-funnel__stage-label">'
                          r'(.*?)</span>', html, re.S).group(1)
        assert "stage" in label.lower(), label
        assert label.strip().lower() not in ("customers", "customers won", "customer")


def test_06_f_channels_withheld_is_the_block_with_the_word_and_no_figure():
    from tests.test_pr_ads_162i_channels_page_revamp import _payload
    p = _payload(publication={"status": "withheld", "reason": "source_last_incremental_failed"})
    p["totals"] = {**p["totals"], "contacts": SENTINEL}
    html = _run_app("renderChannelFunnel(%s, {})" % json.dumps(p))
    assert str(SENTINEL) not in html
    assert html.count('class="funnel-block"') == 1
    assert "<strong>Withheld</strong> — the last contact sync failed." in html
    assert all(c.endswith("Withheld") for c in _cells(html).values())


def test_06_g_the_revenue_tab_strip_is_no_longer_a_funnel_and_keeps_every_figure():
    """Customer Efficiency joins the acquisition cohort (SQLs) to close-date
    outcomes. It is outcome cells now — no arrow, no funnel markup — with the
    cohort gated on THIS response's cohort and the two cross-cohort ratios as
    labelled cells, unavailable with their reasons."""
    src = _strip_comments(_fn("renderRevCustomerEfficiency"))
    assert "cohortCountHtml(k.sqls, d.cohort, null)" in src
    assert "→" not in src and not _CLASS_TOKEN.search(src)
    d = {"window": {"key": "current_quarter"},
         "cohort": {"sql_status": "withheld", "sql_reason": "source_bootstrap_incomplete"},
         "kpis": {"sqls": SENTINEL, "closed_won_deals": 0, "average_deal_value_usd": None,
                  "sql_to_customer_rate": None, "sql_to_customer_rate_status": "unavailable",
                  "sql_to_customer_rate_reason": "sql_to_customer_not_one_cohort",
                  "revenue_per_sql_usd": None, "revenue_per_sql_status": "unavailable",
                  "revenue_per_sql_reason": "sql_to_customer_not_one_cohort"}}
    html = _run_app('_currentUser = {"role": "viewer"}; renderRevCustomerEfficiency(%s)' % json.dumps(d))
    text = _text(html)
    assert str(SENTINEL) not in html and "Withheld" in text
    assert "Closed-Won Deals 0 By close date" in text
    assert "SQL-to-customer rate Unavailable" in text and "Revenue per SQL Unavailable" in text
    assert "&amp;amp;" not in html
