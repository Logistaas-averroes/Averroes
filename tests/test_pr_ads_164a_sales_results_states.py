"""PR-ADS-164A — the Deals and Revenue by Source pages say what they count, and
never read "unavailable" as "none".

PR-ADS-168B deleted the Revenue by Source page, its drawer route and the mart
`source` view. The cases about that page's UI and drawer are deleted with it
(test_08, test_13, test_14, test_16, test_17 and the drawer/source halves of
the mixed UI cases). The `source` surface in the server cases is now
``build_revenue_by_source`` itself — the reader the mart view passed through
unchanged, which still feeds the Dashboard source breakdowns.

Both pages read the SAME canonical won-deal population
(``canonical_revenue_service.load_won_deals``, all-source scope). They looked
different because of presentation: the Deals page called an all-source list
"attributed to Google Ads", carried two dead columns and a Google Ads spend
card, rendered withheld revenue as a dash — and both pages rendered a stale or
unreadable ledger as "no deals / no activity".

Every UI case here drives the REAL ``static/app.js`` (the whole file, in a node
``vm``, through the real loaders) with payloads produced by the REAL server
builders over a stubbed ledger. No payload is hand-written; the fail-closed
cases mutate a real one and say so.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests import test_pr_ads_125_revenue_decision_mart as t125
from tests.canonical_ledger_fixtures import (
    UNPROVEN_SYNC_STATE, fresh_sync_state, ledger_row, patch_canonical_ledger,
)
from tests.test_pr_ads_161b_marketing_outcome_cohorts import _VM_HARNESS

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
WINDOW = "current_quarter"          # 2026-10-01 .. 2026-12-31 at NOW

# The canonical verdict's two withholding reasons these states produce, with
# the plain words app.js gives them (CANONICAL_REASON_LABELS).
STALE_WORDS = "the deal sync is overdue, so these figures may be out of date"
UNREADABLE_WORDS = "the deal ledger could not be read"
UNPROVEN_WORDS = "deal-ledger coverage is not proven"
UNDATED_WORDS = "a won deal has no close date"
NO_AMOUNT_WORDS = "a won deal has no proven USD amount"
NO_VERDICT_WORDS = "the server sent no usable publication verdict for these deals"

#: Text that claims the population is empty. Allowed only beside a published
#: zero; never in a withheld or unavailable state.
NONE_CLAIMS = ("No closed-won deals", "No attributed source activity",
               "No attributed closed-won client detail", "No leads/SQLs found",
               "No attributed activity")

GOOGLE = ledger_row(
    "d1", deal_name="Acme renewal", deal_close_date="2026-10-20T00:00:00+00:00",
    amount_raw=9000.0, revenue_usd=9000.0, acquisition_group="google_ads",
    attribution_status="attributed", campaign_name_raw="Brand - UK",
    gclid="gclid-d1", country_raw="United Kingdom",
    source_primary_raw="PAID_SEARCH", primary_contact_id="c1")
ORGANIC = ledger_row(
    "d2", deal_name="Beta expansion", deal_close_date="2026-11-02T00:00:00+00:00",
    amount_raw=4000.0, revenue_usd=4000.0, acquisition_group="organic",
    attribution_status="attributed", source_primary_raw="ORGANIC_SEARCH")
UNPRICED = ledger_row(
    "d3", deal_name="Gamma pilot", deal_close_date="2026-11-05T00:00:00+00:00",
    amount_raw=None, revenue_usd=None, currency_status="unavailable",
    currency_reason="no_amount")
#: Won, but closed two quarters before the window: the ledger is not empty,
#: the WINDOW is.
OUT_OF_WINDOW = ledger_row(
    "d0", deal_name="Old deal", deal_close_date="2026-05-10T00:00:00+00:00",
    amount_raw=7777.0, revenue_usd=7777.0)
UNDATED = ledger_row("d9", deal_name="Undated deal", deal_close_date=None)

DEAL_NAMES = ("Acme renewal", "Beta expansion", "Gamma pilot", "Old deal",
              "Undated deal")

#: state -> (ledger rows, patch_canonical_ledger keyword arguments)
STATES = {
    # A current ledger with deals in the window.
    "deals": ([GOOGLE, ORGANIC, OUT_OF_WINDOW], {}),
    # A current ledger with no deal in the window.
    "empty": ([OUT_OF_WINDOW], {}),
    # The deals are there, but the last successful sync is 72h old (limit 36h).
    "stale": ([GOOGLE, ORGANIC], {"stale": True}),
    # The ledger cannot be read at all.
    "unreadable": ([GOOGLE, ORGANIC], {"available": False}),
    # Readable, but its historical bootstrap never completed.
    "unproven": ([GOOGLE, ORGANIC], {"unproven": True}),
    # A won deal with no close date could belong to this window.
    "undated": ([GOOGLE, ORGANIC, UNDATED], {}),
    # Every deal is known, one has no amount: the count publishes, revenue not.
    "unpriced": ([GOOGLE, ORGANIC, UNPRICED], {}),
}

#: state -> (status, reason, plain words) for the states that publish no list.
BLOCKED = {
    "stale": ("unavailable", "canonical_ledger_stale", STALE_WORDS),
    "unreadable": ("unavailable", "canonical_ledger_unreadable", UNREADABLE_WORDS),
    "unproven": ("unavailable", "canonical_coverage_not_proven", UNPROVEN_WORDS),
    "undated": ("withheld", "won_deals_without_close_date", UNDATED_WORDS),
}


def _patch(monkeypatch, state, *, leads=True):
    """One coherent durable dataset (PR-ADS-125's), with the ledger in ``state``."""
    import db.revenue_repository as repo

    t125._patch_durable(monkeypatch)
    rows, opts = STATES[state]
    kwargs = {"sync_state": fresh_sync_state(NOW)}        # current, by default
    if opts.get("stale"):
        kwargs["sync_state"] = fresh_sync_state(NOW, hours_ago=72.0)
    if opts.get("unproven"):
        kwargs["sync_state"] = dict(UNPROVEN_SYNC_STATE)
    if opts.get("available") is False:
        kwargs["available"] = False
    patch_canonical_ledger(monkeypatch, rows, **kwargs)
    if not leads:
        monkeypatch.setattr(repo, "fetch_source_leads",
                            lambda s, e: {"available": True, "rows": []})


def _mart(view):
    from services.revenue_decision_mart import build_revenue_decision_mart
    return build_revenue_decision_mart(view=view, window=WINDOW, now=NOW)


def _source():
    """The per-source groups and their verdict, exactly as the deleted mart
    `source` view passed them through (PR-ADS-168B)."""
    from services.source_attribution_service import build_revenue_by_source
    src = build_revenue_by_source(WINDOW, now=NOW)
    return {"rows": src.get("groups") or [],
            "closed_won_population": src.get("closed_won_population")}


def _payloads(monkeypatch, state, **kw):
    """What the Deals endpoint and the source reader really send for ``state``.
    PR-ADS-178B: plus the Pipeline builder's payload, which the deal list (the
    Closed-Won Deals page's successor) renders."""
    _patch(monkeypatch, state, **kw)
    from services.pipeline_service import build_pipeline
    return {"deal": _mart("deal"), "source": _source(),
            "pipeline": build_pipeline(WINDOW, now=NOW)}


def _verdict(payload):
    """The canonical verdict a payload carries for its won-deal list, or None."""
    return payload.get("closed_won_population")


def _numbers(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _numbers(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _numbers(v, f"{path}[{i}]")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield path, value


# ═════════════════════════════════════════════════════════════════════════════
# Server — the payload tells genuinely-empty from not-read
# ═════════════════════════════════════════════════════════════════════════════

def test_01_a_current_ledger_with_deals_publishes_the_count_beside_the_rows(
        monkeypatch):
    p = _payloads(monkeypatch, "deals")
    published_two = {"value": 2, "status": "published", "reason": None}
    assert _verdict(p["deal"]) == published_two
    assert _verdict(p["source"]) == published_two
    assert [r["deal_name"] for r in p["deal"]["rows"]] == ["Beta expansion",
                                                          "Acme renewal"]


def test_02_both_pages_count_the_same_population(monkeypatch):
    """The owner's report: the two pages "show different data". They do not."""
    p = _payloads(monkeypatch, "deals")
    groups = p["source"]["rows"]
    assert sum(g["closed_won_deals"] for g in groups) == len(p["deal"]["rows"]) \
        == (_verdict(p["deal"]) or {}).get("value") \
        == (_verdict(p["source"]) or {}).get("value") == 2
    assert sum(g["won_revenue"] for g in groups) == \
        p["deal"]["ledger_summary"]["won_revenue"] == 13000.0
    # Every listed deal names the Revenue by Source group it is counted in.
    by_group = {g["group"]: g["closed_won_deals"] for g in groups}
    for row in p["deal"]["rows"]:
        assert by_group[row["acquisition_group"]] >= 1


def test_03_a_current_ledger_with_no_deal_in_the_window_is_a_published_zero(
        monkeypatch):
    p = _payloads(monkeypatch, "empty")
    zero = {"value": 0, "status": "published", "reason": None}
    assert _verdict(p["deal"]) == zero
    assert _verdict(p["source"]) == zero
    assert p["deal"]["rows"] == []
    assert all(g["closed_won_deals"] == 0 for g in p["source"]["rows"])


@pytest.mark.parametrize("state", sorted(BLOCKED))
def test_04_a_population_that_was_not_released_says_so_and_carries_no_count(
        monkeypatch, state):
    status, reason, _ = BLOCKED[state]
    p = _payloads(monkeypatch, state)
    verdict = {"value": None, "status": status, "reason": reason}
    for surface in ("deal", "source"):
        assert _verdict(p[surface]) == verdict, surface
    # The lists are empty in EXACTLY the same way as test_03's — the verdict
    # is the only thing that tells the two apart.
    assert p["deal"]["rows"] == [] and p["source"]["rows"] == []
    # Withheld means absent: no deal count, revenue or customer figure.
    assert list(_numbers({k: p["deal"][k] for k in
                          ("rows", "ledger_summary", "closed_won_population")})) == []


@pytest.mark.parametrize("state", sorted(STATES))
def test_05_the_verdict_is_the_canonical_one_passed_through(monkeypatch, state):
    """A page republishes ``{value, status, reason}``; it never decides it."""
    from services import canonical_revenue_service as canonical_revenue
    from services.revenue_attribution_service import build_revenue_deals
    from services.source_attribution_service import build_revenue_by_source

    p = _payloads(monkeypatch, state)
    canonical = canonical_revenue.load_won_deals(WINDOW, now=NOW)
    truth = canonical["canonical_metrics"]["closed_won_deals"]
    assert _verdict(p["deal"]) == truth
    assert _verdict(p["source"]) == truth
    # The mart passes the page builders' own verdicts through unchanged.
    assert _verdict(build_revenue_deals(WINDOW, now=NOW)) == truth
    assert _verdict(build_revenue_by_source(WINDOW, now=NOW)) == truth
    # Where the population is released, revenue stays the canonical revenue
    # verdict: nothing in this PR re-gates it.
    if truth["status"] == "published":
        assert p["deal"]["ledger_summary"]["metrics"]["revenue_usd"] == \
            canonical["canonical_metrics"]["revenue_usd"]


def test_06_revenue_withheld_for_a_deal_with_no_amount_still_lists_every_deal(
        monkeypatch):
    p = _payloads(monkeypatch, "unpriced")
    assert _verdict(p["deal"]) == {
        "value": 3, "status": "published", "reason": None}
    assert len(p["deal"]["rows"]) == 3
    revenue = p["deal"]["ledger_summary"]["metrics"]["revenue_usd"]
    assert revenue == {"value": None, "status": "withheld",
                       "reason": "closed_won_deals_missing_proven_usd_amount"}
    # Never the priced part: no total, no average.
    assert p["deal"]["ledger_summary"]["won_revenue"] is None
    assert p["deal"]["ledger_summary"]["average_deal_value"] is None


def test_07_views_that_list_no_deals_carry_no_population_verdict(monkeypatch):
    _patch(monkeypatch, "deals")
    for view in ("campaign", "country"):
        mart = _mart(view)
        # The key is part of every view's contract; it is None where there is
        # no deal list for it to describe.
        assert "closed_won_population" in mart, view
        assert mart["closed_won_population"] is None, view


# ═════════════════════════════════════════════════════════════════════════════
# UI — the whole real app.js, through the real loaders
# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-161B's `vm` harness (every helper and formatter app.js really uses,
# behind a minimal DOM stub), awaiting the probe so the async loaders run.

_SYNC_TAIL = "process.stdout.write(JSON.stringify(out === undefined ? null : out));"
_ASYNC_HARNESS = _VM_HARNESS.replace(
    _SYNC_TAIL,
    "Promise.resolve(out).then((v) => "
    "process.stdout.write(JSON.stringify(v === undefined ? null : v)));")
assert _ASYNC_HARNESS != _VM_HARNESS, "the 161B harness tail changed"

# PR-ADS-178B: the deal list is the Pipeline page's; its real loader is
# driven with the real Pipeline payload. "deals" is the deal-list panel,
# "page" the whole page (KPI cards, funnel, deals, customers, footer).
_PROBE = """
const P = %(payloads)s;
_currentUser = %(user)s;
fetchJSON = async (url) => P.pipeline;
(async () => {
  await loadPipeline();
  const outcomes = __els["pipeline-outcomes"].innerHTML;
  const i = outcomes.indexOf('id="pipeline-deals"');
  const j = outcomes.indexOf('id="pipeline-customers"');
  return { deals: outcomes.slice(i, j),
           page: __els["pipeline-summary"].innerHTML + outcomes };
})()
"""


def _ui(payloads, tmp_path, *, js=None, user=None):
    """Render the Deals page from ``payloads`` in the real app.js (or a
    mutation of it). Returns its HTML. (Revenue by Source and its drawer were
    rendered here too until PR-ADS-168B deleted them.)"""
    node = shutil.which("node")
    assert node, "node is required: CI runs `node --check static/app.js`"
    (tmp_path / "harness.js").write_text(_ASYNC_HARNESS, encoding="utf-8")
    (tmp_path / "app.js").write_text(
        js if js is not None else APP_JS.read_text(encoding="utf-8"),
        encoding="utf-8")
    (tmp_path / "probe.js").write_text(
        _PROBE % {"payloads": json.dumps(payloads, default=str),
                  "user": json.dumps(user)}, encoding="utf-8")
    out = subprocess.run(
        [node, str(tmp_path / "harness.js"), str(tmp_path / "app.js"),
         str(tmp_path / "probe.js")], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-3000:]
    assert out.stdout, "the loaders never finished"
    return json.loads(out.stdout)


def _text(html):
    """What a reader sees: the markup's text, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _assert_says_only_why(html, word, words):
    """The withholding word and its reason — and no row, figure or 'none'."""
    text = _text(html)
    assert f">{word}</span>" in html, text
    assert words in text, text
    for claim in NONE_CLAIMS:
        assert claim not in text, text
    assert "<table" not in html and "<tr" not in html, text
    for name in DEAL_NAMES + ("Acme Ltd",):
        assert name not in text, text
    assert not re.search(r"[0-9$£€]", text), f"a figure is shown: {text}"
    assert "—" not in text, f"a dash stands in for the reason: {text}"


def test_10_the_deals_page_lists_every_source_deal_with_the_fields_it_carries(
        monkeypatch, tmp_path):
    out = _ui(_payloads(monkeypatch, "deals"), tmp_path)
    html, page = out["deals"], _text(out["page"])
    text = _text(html)
    for column in ("Deal", "Closed", "Country", "Campaign", "Source",
                   "Won Revenue"):
        assert f"<th>{column}</th>" in html, column
    assert html.count("<th>") == 6
    # Both deals, the organic one included, each with its own name and source.
    assert "Acme renewal" in text and "Beta expansion" in text
    assert "Brand - UK" in text and "United Kingdom" in text
    assert ">Google Ads</td>" in html and ">Organic</td>" in html
    assert "$9.0k" in text and "$4.0k" in text
    # PR-ADS-178B: the KPI cards (the strip's successor): the count, the
    # total and the average. The GCLID subset card is not one of the five
    # (owner brief: SQLs, deals, customers, revenue, average).
    assert "Closed-won deals 2 " in page and "Won revenue $13.0k" in page
    assert "Average deal value $6.5k" in page
    assert "GCLID-attributable" not in page
    assert "from every source" in text
    # A deal that closed outside the window is not in it.
    assert "Old deal" not in text
    # Nothing the payload does not carry, and no Google Ads spend.
    for gone in ("Company", "Attribution", "Needs Review", "Exact GCLID",
                 "attributed to Google Ads", "Canonical Spend Truth",
                 "Native Spend", "USD Reporting", "£"):
        assert gone not in text, gone


def test_11_a_window_with_no_deal_says_none_closed_only_because_it_was_read(
        monkeypatch, tmp_path):
    payloads = _payloads(monkeypatch, "empty")
    html = _ui(payloads, tmp_path)["deals"]
    text = _text(html)
    assert "No closed-won deals closed in this period" in text
    assert "The deal ledger was read and is current." in text
    assert "<table" not in html and "Unavailable" not in text
    assert "Withheld" not in text
    # …and it may say so only because the verdict says the ledger was read.
    assert _verdict(payloads["deal"]) == {"value": 0, "status": "published",
                                          "reason": None}


@pytest.mark.parametrize("state", sorted(BLOCKED))
def test_12_the_deals_page_never_reads_a_withheld_ledger_as_no_deals(
        monkeypatch, tmp_path, state):
    status, _, words = BLOCKED[state]
    html = _ui(_payloads(monkeypatch, state), tmp_path)["deals"]
    _assert_says_only_why(html, status.capitalize(), words)
    assert "This does not mean that no deals closed." in _text(html)


def test_15_withheld_revenue_reads_withheld_with_its_reason_never_a_dash(
        monkeypatch, tmp_path):
    payloads = _payloads(monkeypatch, "unpriced")
    out = _ui(payloads, tmp_path)
    html, page = out["deals"], out["page"]
    text = _text(html)
    # The count is published; the total and the average are not.
    # PR-ADS-178B: on the KPI cards (the strip's successor).
    cards = page[page.index("pipeline-kpi-grid"):page.index('id="pipeline-funnel"')]
    assert "Closed-won deals 3 " in _text(cards)
    assert f"Won revenue Withheld {NO_AMOUNT_WORDS}" in _text(cards)
    assert f"Average deal value Withheld {NO_AMOUNT_WORDS}" in _text(cards)
    assert "—" not in _text(cards), "a dash stands in for a withheld total"
    # Never the priced part presented as the total.
    assert "$13.0k" not in _text(page) and "$6.5k" not in _text(page)
    # Every deal is still listed; the unpriced one says why it has no amount.
    assert "Gamma pilot" in text and "$9.0k" in text and "$4.0k" in text
    assert "Unavailable no proven USD amount" in text
    assert ">Unclassified</td>" in html
    # A viewer gets the reason but no link; an admin can open the blockers.
    assert "View blockers" not in text
    assert "View blockers" not in _text(page)
    admin = _ui(payloads, tmp_path, user={"role": "admin"})["page"]
    assert "View blockers" in _text(admin)
    assert "cte_family=revenue_proof" in admin


def _mutated(payloads, **verdict):
    """A REAL payload set whose verdict alone is replaced (``None`` removes
    it) — the rows, summaries and every other key stay as the server sent."""
    out = json.loads(json.dumps(payloads, default=str))
    # PR-ADS-178B: the Pipeline payload carries the same verdict for its list.
    for surface in ("deal", "source", "pipeline"):
        if verdict.get("drop"):
            out[surface].pop("closed_won_population", None)
        else:
            out[surface]["closed_won_population"] = dict(verdict)
    return out


@pytest.mark.parametrize("verdict,words", [
    # No verdict at all (an older server, a dropped key).
    ({"drop": True}, NO_VERDICT_WORDS),
    # A status this client does not know is not evidence of publication.
    ({"value": 0, "status": "ok", "reason": None}, NO_VERDICT_WORDS),
    ({"value": 0, "status": "not_published", "reason": "canonical_ledger_stale"},
     STALE_WORDS),
    # "published" with no numeric count is not a published zero.
    ({"value": None, "status": "published", "reason": None},
     "the deal rows received do not match the published count"),
    # A published count the (empty) list contradicts is not "none" either.
    ({"value": 4, "status": "published", "reason": None},
     "the deal rows received do not match the published count"),
])
def test_18_an_unrecognised_or_missing_verdict_fails_closed_on_every_surface(
        monkeypatch, tmp_path, verdict, words):
    """Mutations of the real EMPTY-window payloads: each list is truly empty,
    so only the verdict stands between the page and "there are none"."""
    out = _ui(_mutated(_payloads(monkeypatch, "empty", leads=False), **verdict),
              tmp_path)
    _assert_says_only_why(out["deals"], "Unavailable", words)


def test_19_rows_sent_beside_a_withheld_verdict_are_not_rendered(monkeypatch,
                                                                 tmp_path):
    """The second wall: the UI holds even if a server sent the rows anyway."""
    leaked = _mutated(_payloads(monkeypatch, "deals"), value=None,
                      status="unavailable", reason="canonical_ledger_stale")
    assert leaked["deal"]["rows"]
    out = _ui(leaked, tmp_path)
    _assert_says_only_why(out["deals"], "Unavailable", STALE_WORDS)


def test_20_the_guard_is_load_bearing_without_it_stale_reads_as_none(
        monkeypatch, tmp_path):
    """A guard whose absence changes nothing is not a guard."""
    js = APP_JS.read_text(encoding="utf-8")
    guard = ('function closedWonPopulationState(verdict, dealCount) {\n'
             '  if (!closedWonPopulationPublished(verdict)) return "blocked";\n')
    assert js.count(guard) == 1
    # The pre-164A rule: an empty array is "empty", whatever the verdict says.
    bypassed = js.replace(guard, (
        'function closedWonPopulationState(verdict, dealCount) {\n'
        '  if (!closedWonPopulationPublished(verdict)) '
        'return dealCount > 0 ? "rows" : "empty";\n'))
    payloads = _payloads(monkeypatch, "stale")
    # PR-ADS-178B: the Pipeline builder withholds the list itself as well
    # (deals_status, and the KPI card's own verdict), so the client guard is
    # shown load-bearing against a faulty server that sends neither: the list
    # "published" and empty, its count card claiming publication, and only
    # the population verdict saying stale.
    payloads["pipeline"] = {**payloads["pipeline"], "deals": [],
                            "deals_status": "published", "deals_reason": None,
                            "kpis": {**payloads["pipeline"]["kpis"],
                                     "closed_won_deals": {"value": None,
                                                          "status": "published",
                                                          "reason": None}}}
    out = _ui(payloads, tmp_path, js=bypassed)
    assert "No closed-won deals closed in this period" in _text(out["deals"])
    # …and with the guard in place the same payloads say why.
    out = _ui(payloads, tmp_path)
    _assert_says_only_why(out["deals"], "Unavailable", STALE_WORDS)


# ═════════════════════════════════════════════════════════════════════════════
# Structural — the Deals page says what it counts
# ═════════════════════════════════════════════════════════════════════════════

def _deals_markup():
    # PR-ADS-178B: the deals are on the Pipeline page.
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<section id="page-pipeline".*?\n      </section>', html, re.S)
    assert m, "the Pipeline page section is missing"
    return m.group(0)


def _deals_js():
    """The deal list's own region of app.js (PR-ADS-178B: the Pipeline
    page's), with the KPI cards that state its count, comments stripped."""
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("// ── KPI cards: one source each")
    end = js.index("// ── Customers won", start)
    return re.sub(r"^\s*//.*$", "", js[start:end], flags=re.M)


def test_21_the_deals_page_markup_says_all_sources_by_close_date():
    section = _deals_markup()
    assert "Every HubSpot closed-won deal, from every source" in section
    assert "by deal close date" in section
    for gone in ("attributed to Google Ads", "Company", "Needs Review",
                 "Exact GCLID", "Spend Truth"):
        assert gone not in section, gone


def test_22_the_deals_page_code_has_no_dead_column_no_spend_card_no_old_label():
    region = _deals_js()
    for gone in ("attributed to Google Ads", "Company", "Needs Review",
                 "Exact GCLID", "renderMartSpendTruth", "dealAttributionBadge",
                 "match_source", "exact_gclid_count", "r.company",
                 '"database_unavailable"', '|| "available"',
                 "No closed-won deals found"):
        assert gone not in region, gone
    # The canonical metrics go through the shared helpers, not a formatter
    # that turns "withheld" into a dash (PR-ADS-178B: the KPI cards).
    assert "canonicalMetricHtml(k.won_revenue_usd, fmtMoney" in region
    assert "canonicalMetricHtml(k.closed_won_deals, fmtCount" in region
    assert "fmtMoney(k.won_revenue_usd" not in region
    assert "fmtCount(k.closed_won_deals" not in region


def test_23_no_roas_or_cac_wording_reached_either_page():
    """Audit check 18 stays green: no period ratio is named ROAS or CAC."""
    from scripts.audit_customer_revenue_reader_cutover import (
        ui_ratio_label_violations,
    )
    assert ui_ratio_label_violations(APP_JS.read_text(encoding="utf-8"),
                                     INDEX_HTML.read_text(encoding="utf-8")) == []
    region = _deals_js() + _deals_markup()
    assert not re.search(r"\b(ROAS|CAC)\b", region)
