"""PR-ADS-182A — the Pipeline page's funnel counts by contact CREATE DATE
(owner decision, ``docs/67``).

The Pipeline funnel was the lifecycle stage-ENTRY funnel ("Entered stage in
window") with the PR-ADS-180C undated-reach bound: with ~9,000 production Lead
contacts that have no Lead date, most stages read "at least N" for good. It
is now the ACQUISITION COHORT — contacts created in the window, and how far
they have got so far — drawn by the one shared block (``acquisitionFunnelModel``
→ ``funnelBlockHtml``) from the one block producer the Overview uses
(``dashboard_overview_service.acquisition_funnel_block``), over the mart
deal view's own cohort (the build the SQL card reads).

What is guarded here (the PostgreSQL half is
``test_pr_ads_182a_pipeline_cohort_funnel_pg_integration``):

1. **One funnel, one producer.** Pipeline's unfiltered funnel IS the
   Overview's block; the parity audit compares them column for column.
2. **Filters.** Source (and Scope Google Ads-source) read the cohort's own
   partition by source group — the Source filter's own rule, drill-down
   included. Campaign- / Keyword-attributable cannot be honoured on the
   cohort basis: unavailable with the reason, never the unfiltered cohort.
3. **Withheld means absent.** The cohort's one freshness verdict (the real
   ``assess``); no count anywhere in the payload or the DOM; unreadable is
   unavailable, never 0.
4. **Lower bounds.** A contact with no created date is in no window: while
   any exist (or the response does not say) every column is "at least N".
5. **Metadata and copy.** The block carries the cohort's ``metric_family`` /
   ``window_basis``; the page says what it counts; the contact list (stage
   entries) says it is a different count.
6. **Audits.** The cohort audit certifies the Pipeline block against its
   cohort; reconciliation conserves the source-group partition.
7. **Dead plumbing.** What only Pipeline's stage-entry funnel used is gone;
   what Overview → Lifecycle Activity still uses stays.

Every producer runs for real; the contact repository is replaced at its
fetch boundary only, and the mart deal view's ``cohort`` block is the real
cohort contract over those rows (no database here). Freshness goes through
the real ``analysis.sql_coverage_freshness.assess``.
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest  # noqa: F401

from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
START, END = date(2026, 11, 1), date(2026, 11, 19)
SENTINEL = 7919
#: PR-ADS-182A-F1 (m3): the sentinel in ANY rendering ("7919", "7,919",
#: "7.919", "7 919", "$7,919.00") — a plain substring check could not fail
#: once the page formats the figure.
_SENTINEL_ANY = re.compile(r"7[,.\s\u202f\u00a0]?919")
COLUMNS = ("contacts", "reached_mql", "reached_sql", "reached_opportunity",
           "reached_customer_stage")
#: block ``totals`` key → cohort slot field.
SLOT_FIELD = {"contacts": "contacts_acquired", "reached_mql": "reached_mql",
              "reached_sql": "sqls", "reached_opportunity": "reached_opportunity",
              "reached_customer_stage": "reached_customer_stage"}


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════
def row(i, stage, *, src="PAID_SEARCH", label="brand - uk",
        created="2026-11-10T10:00:00Z", **dates):
    """One ``fetch_acquisition_cohort_contacts`` row (180B's shape)."""
    r = {"funnel_row_id": i, "contact_id": f"c{i}", "created_at": created,
         "lifecycle_stage": stage, "hs_analytics_source": src,
         "hs_analytics_source_data_1": label, "gclid": None, "ip_country": None,
         "country": None}
    for e in ("mql", "sql", "opportunity", "customer"):
        r[f"{e}_entered_direct"] = dates.get(e)
        r[f"{e}_entered_recovered"] = dates.get(f"{e}_recovered")
    return r


def rows():
    """Nine contacts created in the window, five source groups:

    * c0 Google Ads, lead · c1 Google Ads, MQL · c2 Google Ads, customer
    * c3 Organic, opportunity · c4 Organic, lead with an SQL date
    * c5 Offline Sources / SalesNash, customer — Other Paid BY ITS DRILL-DOWN
    * c6 Paid Social, MQL (Other Paid) · c7 no source, lead (Unclassified)
    * c8 Offline Sources / IMPORT, SQL (Offline)
    """
    return [
        row(0, "lead"), row(1, "marketingqualifiedlead"), row(2, "customer"),
        row(3, "opportunity", src="ORGANIC_SEARCH", label=None),
        row(4, "lead", src="ORGANIC_SEARCH", label=None, sql="2026-11-12"),
        row(5, "customer", src="OFFLINE_SOURCES", label="SalesNash"),
        row(6, "marketingqualifiedlead", src="PAID_SOCIAL", label=None),
        row(7, "lead", src=None, label=None),
        row(8, "salesqualifiedlead", src="OFFLINE_SOURCES", label="IMPORT"),
    ]


#: The expected counts per population (contacts, MQL, SQL, Opportunity, Customer).
ALL = (9, 6, 5, 3, 2)
BY_GROUP = {"google_ads": (3, 2, 1, 1, 1), "organic": (2, 1, 2, 1, 0),
            "other_paid": (2, 2, 1, 1, 1), "offline": (1, 1, 1, 0, 0),
            "unclassified": (1, 0, 0, 0, 0)}


def _resolver(label):
    if (label or "").strip().lower() == "brand - uk":
        return ("google_ads", "brand - uk")
    # PR-ADS-182A-F1 (m2): an approved "not Google Ads" label (the mart's
    # excluded_label_keys) — a Paid Search contact the SQL card leaves out.
    if (label or "").strip().lower() == "partner newsletter":
        return ("not_google_ads", None)
    return ("unmatched", str(label))


def _sync_state(*, fresh=True):
    if not fresh:
        return {"available": True, "row": {"bootstrap_status": "running",
                                           "last_sync_mode": "bootstrap",
                                           "last_incremental_status": None,
                                           "last_successful_incremental_at": None}}
    return {"available": True, "row": {
        "bootstrap_status": "complete", "last_sync_mode": "incremental",
        "last_incremental_status": "success",
        "last_successful_incremental_at": (NOW - timedelta(hours=3)).isoformat()}}


def cohort_contract(monkeypatch, contact_rows=None, *, fresh=True, missing_created_at=0,
                    readable=True, start=START, end=END, now=NOW):
    """The mart's REAL cohort contract (``_cohort_contract_block``) over the
    REAL cohort build (``build_window_outcomes``) of ``contact_rows``, with
    the REAL freshness assessor. Only the repository read is replaced."""
    from analysis import sql_coverage_freshness as FR
    from db import crm_funnel_repository as repo
    from services import marketing_outcome_cohort_service as M
    from services.revenue_attribution_service import _cohort_contract_block, _cohort_slots
    data = list(rows() if contact_rows is None else contact_rows)
    monkeypatch.setattr(repo, "fetch_acquisition_cohort_contacts", lambda a, b: (
        {"available": True, "rows": data, "missing_created_at": missing_created_at}
        if readable else {"available": False, "rows": [], "missing_created_at": None}))
    outcomes = M.build_window_outcomes(start, end, resolve_label=_resolver,
                                       freshness=FR.assess(_sync_state(fresh=fresh), now=now),
                                       now=now)
    slots, _ = _cohort_slots(outcomes)
    return json.loads(json.dumps(_cohort_contract_block(
        outcomes, slots, start_date=start, end_date=end), default=str))


def pipeline(monkeypatch, cohort=None, *, window="current_quarter", now=NOW, **filters):
    """The REAL Pipeline builder. Its one substituted input is the mart deal
    view's ``cohort`` block — the real contract above — in place of the
    database-backed one (no database here); every other mart figure is the
    real build's."""
    from services import pipeline_service as ps
    contract = cohort_contract(monkeypatch) if cohort is None else cohort
    real = ps.build_revenue_decision_mart

    def mart(**kw):
        return {**real(**kw), "cohort": contract}
    monkeypatch.setattr(ps, "build_revenue_decision_mart", mart)
    return json.loads(json.dumps(ps.build_pipeline(window, now=now, **filters), default=str))


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def cells(html: str) -> dict:
    out = {}
    for m in re.finditer(r'<(div|button)[^>]*class="dash-funnel__stage[^"]*"[^>]*'
                         r'data-funnel-slot="([a-z]+)"[^>]*>(.*?)</\1>', html, re.S):
        out[m.group(2)] = _text(m.group(3))
    return out


def page(payload, *, js=None) -> str:
    """The Pipeline page's summary (KPI cards + funnel panel), rendered by the
    real ``renderPipelinePage`` in node."""
    return _run_app(
        '_currentUser = {"username": "u", "role": "viewer"}; '
        "pipelineData = %s; pipelineStatus = 'ok'; renderPipelinePage(); "
        '__els["pipeline-summary"].innerHTML' % json.dumps(payload), js=js)


def funnel_panel(summary_html: str) -> str:
    return summary_html[summary_html.index('id="pipeline-funnel"'):]


def _totals(t):
    return tuple(t[c] for c in COLUMNS)


def _slot(slot):
    return tuple(slot[SLOT_FIELD[c]] for c in COLUMNS)


# ═════════════════════════════════════════════════════════════════════════════
# 1. one funnel, one producer
# ═════════════════════════════════════════════════════════════════════════════
def test_01_a_the_pipeline_funnel_is_the_cohorts_all_sources_slot_under_its_verdict(monkeypatch):
    contract = cohort_contract(monkeypatch)
    p = pipeline(monkeypatch, contract)
    f = p["funnel"]
    assert f["publication"] == {"status": "published", "reason": None}
    assert (f["metric_family"], f["window_basis"]) == ("acquisition_cohort_outcomes",
                                                       "contact_created_at")
    assert _totals(f["totals"]) == ALL
    assert _totals(f["totals"]) == _slot(contract["breakdown"]["all_sources"])
    assert f["label"] == "Acquisition cohort"
    assert f["description"] == ("Contacts created in these days, and how far they have "
                                "got so far.")
    assert f["window"] == {"start_date": "2026-11-01", "end_date": "2026-11-19"}
    # No stage-entry figure is published on the page any more.
    dumped = json.dumps(f)
    for gone in ("lifecycle_stage_events", "events", "conversions", "undated_reach",
                 "count_is_bound", "Entered stage in window"):
        assert gone not in dumped, gone
    # The cohort block declares the other family NOT published here, with no
    # count (PR-ADS-177C) — the one place its name appears.
    lc = p["cohort"]["lifecycle_event_coverage"]
    assert lc["counts_withheld"] is True
    assert "lifecycle_stage_events" not in json.dumps({k: v for k, v in p.items()
                                                       if k != "cohort"})
    # The SQL card and the funnel are one cohort build: its Reached SQL over
    # Google Ads is the card's cohort total.
    assert contract["totals"]["sqls"] == BY_GROUP["google_ads"][2]


def test_01_b_pipeline_and_overview_draw_one_block_from_one_producer(monkeypatch):
    from services import pipeline_service as ps
    from services.dashboard_overview_service import _acquisition_funnel_block
    contract = cohort_contract(monkeypatch, missing_created_at=2)
    overview = json.loads(json.dumps(_acquisition_funnel_block(contract), default=str))
    mine = json.loads(json.dumps(ps._funnel(contract, "all_source", None), default=str))
    own_keys = {"label", "description", "lead_filters", "truth_scope"}
    assert {k: v for k, v in mine.items() if k not in own_keys} == overview
    # One producer, no second read: the Pipeline builder calls no funnel
    # build, no undated reach and takes no database snapshot of its own.
    src = inspect.getsource(ps)
    assert "acquisition_funnel_block(cohort, source_group=source_group" in src
    for gone in ("crm_funnel.build(", "undated_stage_reach", "undated_reach_for_population",
                 "one_read_snapshot", "stage_count_is_bound", "_funnel_coverage"):
        assert gone not in src, gone
    # One UI block: the Pipeline adapter is the shared acquisition adapter.
    model = JS[JS.index("function pipelineFunnelModel("):]
    model = model[:model.index("\n}\n")]
    assert "acquisitionFunnelModel(f," in model and "shares: false" in model
    assert "funnelBlockHtml(pipelineFunnelModel(f)" in JS


def test_01_c_the_parity_audit_holds_pipeline_to_the_overview_column_for_column(monkeypatch):
    from services import cross_page_parity_service as parity
    from tests.test_pr_ads_154c_cross_page_parity import _audit_with, _payloads
    idents = {k: v for k, v in parity.METRIC_IDENTITIES.items()
              if k.startswith("all_source_acquisition_cohort_")}
    assert sorted(idents) == sorted(f"all_source_acquisition_cohort_{c}" for c in COLUMNS)
    for col in COLUMNS:
        spec = idents[f"all_source_acquisition_cohort_{col}"]
        assert spec["consumers"] == [
            ("dashboard/overview", f"lifecycle_acquisition_funnel.totals.{col}"),
            (parity.CONSUMER_PIPELINE, f"funnel.totals.{col}")]
        assert spec["contract_metric"] == "lifecycle_acquisition_funnel"
        assert spec["scope"] == "all_source_acquisition_cohort"
    ok = _audit_with(monkeypatch, _payloads())
    assert {m["metric"]: m["status"] for m in ok["metrics"]
            if m["metric"] in idents} == dict.fromkeys(idents, "identical")
    # Pipeline publishing another number for the same column is a mismatch.
    payloads = _payloads()
    payloads["pipeline"]["funnel"]["totals"]["reached_sql"] -= 1
    bad = _audit_with(monkeypatch, payloads)
    assert parity.V_VALUE_MISMATCH in bad["violation_codes"]
    assert next(m for m in bad["metrics"] if m["metric"]
                == "all_source_acquisition_cohort_reached_sql")["status"] == "mismatch"
    # The real builder files its provenance under that key, with the scope the
    # audit expects only when the funnel IS the all-source one.
    p = pipeline(monkeypatch)
    c = p["metric_truth"]["lifecycle_acquisition_funnel"]
    assert (c["scope"], c["truth_status"], c["data_source"]) == (
        "all_source_acquisition_cohort", "ready", "canonical.revenue_decision_mart")
    filtered = pipeline(monkeypatch, acquisition_group="organic")
    assert filtered["metric_truth"]["lifecycle_acquisition_funnel"]["scope"] \
        != "all_source_acquisition_cohort"


# ═════════════════════════════════════════════════════════════════════════════
# 2. the lead filters on the cohort basis
# ═════════════════════════════════════════════════════════════════════════════
def test_02_a_a_source_filter_reads_the_cohorts_own_partition_by_the_filters_rule(monkeypatch):
    from services import canonical_crm_funnel_service as crm_funnel
    from services import marketing_outcome_cohort_service as M
    contract = cohort_contract(monkeypatch)
    groups = contract["breakdown"]["by_source_group"]
    assert set(groups) == set(crm_funnel.ACQUISITION_GROUPS)
    for g, expected in BY_GROUP.items():
        f = pipeline(monkeypatch, contract, acquisition_group=g)["funnel"]
        assert f["publication"]["status"] == "published", g
        assert f["source_group"] == g and f["lead_filters"]["honoured"] is True
        assert _totals(f["totals"]) == expected == _slot(groups[g]), g
        assert f["population"] == f"source_group_contacts_acquired_in_window:{g}"
    # The partition is the whole cohort, every field.
    for field in M.SLOT_COUNT_FIELDS:
        assert sum(s[field] for s in groups.values()) \
            == contract["breakdown"]["all_sources"][field], field
    # The Source filter's own rule, drill-down included: SalesNash under
    # Offline Sources is Other Paid, exactly as the contact list classifies it.
    for r in rows():
        own = crm_funnel.derive_acquisition_group(r)
        assert own in BY_GROUP, (r["contact_id"], own)
    assert crm_funnel.derive_acquisition_group(rows()[5]) == "other_paid"


def test_02_b_scope_google_ads_source_is_the_google_ads_source_group(monkeypatch):
    contract = cohort_contract(monkeypatch)
    scoped = pipeline(monkeypatch, contract, scope="google_ads_source")["funnel"]
    grouped = pipeline(monkeypatch, contract, acquisition_group="google_ads")["funnel"]
    assert _totals(scoped["totals"]) == _totals(grouped["totals"]) == BY_GROUP["google_ads"]
    both = pipeline(monkeypatch, contract, scope="google_ads_source",
                    acquisition_group="google_ads")["funnel"]
    assert _totals(both["totals"]) == BY_GROUP["google_ads"]
    # Google Ads-source with another Source selects no contact together: said
    # as such, no count.
    none = pipeline(monkeypatch, contract, scope="google_ads_source",
                    acquisition_group="organic")["funnel"]
    assert none["publication"] == {"status": "unavailable",
                                   "reason": "lead_filters_select_no_contact"}
    assert none["totals"] is None and none["lead_filters"]["honoured"] is False


def test_02_c_a_scope_the_cohort_is_not_split_by_is_unavailable_never_the_whole_cohort(monkeypatch):
    contract = cohort_contract(monkeypatch)
    for scope in ("campaign_attributable", "keyword_attributable"):
        for group in (None, "google_ads"):
            p = pipeline(monkeypatch, contract, scope=scope, acquisition_group=group)
            f = p["funnel"]
            assert f["publication"] == {"status": "unavailable",
                                        "reason": "lead_scope_not_on_cohort_basis"}, scope
            assert f["totals"] is None
            assert f["lead_filters"] == {
                "scope": scope, "scope_label": f["lead_filters"]["scope_label"],
                "acquisition_group": group,
                "acquisition_group_label": f["lead_filters"]["acquisition_group_label"],
                "source_group": None, "honoured": False,
                "unavailable_reason": "lead_scope_not_on_cohort_basis"}
            assert p["metric_truth"]["lifecycle_acquisition_funnel"]["truth_status"] \
                == "not_ready"
            panel = funnel_panel(page(p))
            text = _text(panel)
            assert "is not split by campaign or keyword attribution" in text
            assert "the contact list below still applies this Scope" in text
            for slot, cell in cells(panel).items():
                assert not re.search(r"\d", cell), (slot, cell)
                assert "Unavailable" in cell


def test_02_d_the_page_names_the_population_it_counts(monkeypatch):
    contract = cohort_contract(monkeypatch)
    allp = _text(funnel_panel(page(pipeline(monkeypatch, contract))))
    assert "All sources. The SQL card above is the Google Ads acquisition cohort only" in allp
    org_panel = funnel_panel(page(pipeline(monkeypatch, contract, acquisition_group="organic")))
    org = _text(org_panel)
    assert "Source group: Organic (HubSpot Original Source and its drill-down)" in org
    assert "Lead filters — Scope: All sources · Source: Organic" in org
    c = cells(org_panel)
    assert c["contacts"].startswith("Contacts acquired 2 created in this window · Organic")
    assert c["sql"].startswith("Reached SQL 2 ")


def test_02_e_a_google_ads_funnel_names_why_the_sql_card_can_be_lower(monkeypatch):
    """PR-ADS-182A-F1 (m2): two Google Ads SQL figures on one page. A Paid
    Search contact at SQL whose campaign label is an approved "not Google Ads"
    label is in the funnel's Google Ads source group (original source) but
    not in the SQL card's Google Ads acquisition cohort. The page says so."""
    extra = row(9, "salesqualifiedlead", label="partner newsletter")
    contract = cohort_contract(monkeypatch, rows() + [extra])
    for filters in ({"scope": "google_ads_source"}, {"acquisition_group": "google_ads"}):
        p = pipeline(monkeypatch, contract, **filters)
        card = contract["totals"]["sqls"]          # what the SQL card republishes
        funnel = p["funnel"]["totals"]["reached_sql"]
        assert (card, funnel) == (1, 2), (card, funnel)
        text = _text(funnel_panel(page(p)))
        assert "every Paid Search contact by HubSpot Original Source" in text, filters
        assert ("also leaves out Paid Search contacts whose campaign label is not a Google "
                "Ads campaign, so the card can be lower") in text, filters
    # Another source group does not carry the sentence.
    org = _text(funnel_panel(page(pipeline(monkeypatch, contract, acquisition_group="organic"))))
    assert "so the card can be lower" not in org


def test_02_f_an_unhonoured_filter_is_unavailable_on_the_page_whatever_the_server_sends(
        monkeypatch):
    """PR-ADS-182A-F1 (o1), defence in depth: a payload whose lead filter is
    not honoured but which (wrongly) says published with totals draws no
    figure — Unavailable with the server's reason, or the scope reason."""
    p = pipeline(monkeypatch, scope="campaign_attributable")
    p["funnel"]["publication"] = {"status": "published", "reason": None}
    p["funnel"]["totals"] = {"contacts": 12, "reached_mql": 11, "reached_sql": 10,
                             "reached_opportunity": 9, "reached_customer_stage": 8}
    panel = funnel_panel(page(p))
    for slot, cell in cells(panel).items():
        assert "Unavailable" in cell and not re.search(r"\d", cell), (slot, cell)
    assert "is not split by campaign or keyword attribution" in _text(panel)
    # No reason in the filter block: the scope reason, never a figure.
    p["funnel"]["lead_filters"]["unavailable_reason"] = None
    panel = funnel_panel(page(p))
    assert "is not split by campaign or keyword attribution" in _text(panel)
    assert all("Unavailable" in c for c in cells(panel).values())


# ═════════════════════════════════════════════════════════════════════════════
# 3. withheld means absent; unreadable is unavailable
# ═════════════════════════════════════════════════════════════════════════════
def test_03_a_a_withheld_cohort_leaves_no_count_in_the_payload_or_the_page(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    contract = cohort_contract(monkeypatch, fresh=False, missing_created_at=3)
    assert contract["sql_status"] == "withheld"
    for filters in ({}, {"acquisition_group": "organic"}, {"scope": "google_ads_source"}):
        p = pipeline(monkeypatch, contract, **filters)
        f = p["funnel"]
        assert f["publication"] == {"status": "withheld",
                                    "reason": "source_bootstrap_incomplete"}, filters
        assert f["totals"] is None
        # The table-wide disclosure is republished whatever the verdict.
        assert f["contacts_without_created_at"] == 3
        assert audit.mart_withheld_exposures(p) == [], audit.mart_withheld_exposures(p)
        assert audit.acquisition_funnel_problems(p, "funnel") == []
        panel = funnel_panel(page(p))
        assert "Withheld" in panel and "backfill is still running" in _text(panel)
        for slot, cell in cells(panel).items():
            assert not re.search(r"\d", cell), (slot, cell)
    # A leak is named by the audit.
    leaked = pipeline(monkeypatch, contract)
    leaked["funnel"]["totals"] = {"contacts": SENTINEL}
    assert "funnel.totals.contacts" in audit.mart_withheld_exposures(leaked)
    assert audit.acquisition_funnel_problems(leaked, "funnel")


def test_03_b_an_unreadable_cohort_is_unavailable_never_zero(monkeypatch):
    contract = cohort_contract(monkeypatch, readable=False)
    p = pipeline(monkeypatch, contract)
    f = p["funnel"]
    assert f["publication"] == {"status": "unavailable", "reason": "canonical_funnel_unreadable"}
    assert f["totals"] is None and f["contacts_without_created_at"] is None
    panel = funnel_panel(page(p))
    for slot, cell in cells(panel).items():
        assert "Unavailable" in cell and not re.search(r"\d", cell), (slot, cell)
    # No cohort block at all: unavailable too, never zeros.
    from services import pipeline_service as ps
    bare = ps._funnel({}, "all_source", None)
    assert bare["publication"]["status"] == "unavailable" and bare["totals"] is None


def test_03_c_a_published_cohort_without_the_selected_slot_is_unavailable_never_partial(
        monkeypatch):
    from services import pipeline_service as ps
    contract = cohort_contract(monkeypatch)
    no_part = json.loads(json.dumps(contract))
    del no_part["breakdown"]["by_source_group"]
    f = ps._funnel(no_part, "all_source", "organic")
    assert f["publication"] == {"status": "unavailable",
                                "reason": "acquisition_funnel_counts_missing"}
    assert f["totals"] is None
    short = json.loads(json.dumps(contract))
    del short["breakdown"]["by_source_group"]["organic"]["reached_opportunity"]
    g = ps._funnel(short, "all_source", "organic")
    assert g["publication"]["status"] == "unavailable" and g["totals"] is None
    # The unfiltered funnel is unaffected by the partition's absence.
    assert ps._funnel(no_part, "all_source", None)["publication"]["status"] == "published"


def test_03_d_a_faulty_servers_totals_beside_a_withheld_verdict_are_never_drawn(monkeypatch):
    p = pipeline(monkeypatch, cohort_contract(monkeypatch, fresh=False))
    p["funnel"]["totals"] = dict.fromkeys(COLUMNS, SENTINEL)
    panel = funnel_panel(page(p))
    # PR-ADS-182A-F1 (m3): in ANY format — "7919" never renders as such
    # (fmtCount prints "7,919"), so a plain substring check could not fail.
    assert _SENTINEL_ANY.search(r"<b>7,919</b> 7919 7.919") is not None  # the guard bites
    assert _SENTINEL_ANY.search(panel) is None, panel
    assert "Withheld" in panel
    # The same under an unhonoured filter (o1): totals sent, none drawn.
    q = pipeline(monkeypatch, cohort_contract(monkeypatch), scope="keyword_attributable")
    q["funnel"]["totals"] = dict.fromkeys(COLUMNS, SENTINEL)
    assert _SENTINEL_ANY.search(funnel_panel(page(q))) is None


# ═════════════════════════════════════════════════════════════════════════════
# 4. contacts with no created date make every column a lower bound
# ═════════════════════════════════════════════════════════════════════════════
def test_04_a_a_contact_with_no_created_date_bounds_every_column(monkeypatch):
    exact = cells(funnel_panel(page(pipeline(
        monkeypatch, cohort_contract(monkeypatch, missing_created_at=0)))))
    assert exact["contacts"].startswith("Contacts acquired 9 ")
    assert "at least" not in " ".join(exact.values())
    p = pipeline(monkeypatch, cohort_contract(monkeypatch, missing_created_at=4))
    assert p["funnel"]["contacts_without_created_at"] == 4
    bound = cells(funnel_panel(page(p)))
    for slot, n in zip(("contacts", "mql", "sql", "opportunity", "customer"), ALL):
        assert f"at least {n}" in bound[slot], (slot, bound[slot])
        assert "4 contacts have no created date in HubSpot" in bound[slot], slot
    # Not stated is not zero: a response without the disclosure is a bound.
    p["funnel"].pop("contacts_without_created_at")
    unknown = cells(funnel_panel(page(p)))
    assert all("at least" in c for c in unknown.values()), unknown
    # A Source filter keeps the table-wide disclosure: it only widens.
    org = pipeline(monkeypatch, cohort_contract(monkeypatch, missing_created_at=4),
                   acquisition_group="organic")
    assert all("at least" in c for c in cells(funnel_panel(page(org))).values())


# ═════════════════════════════════════════════════════════════════════════════
# 5. metadata and copy
# ═════════════════════════════════════════════════════════════════════════════
def test_05_a_the_block_carries_the_cohorts_family_and_says_what_it_counts(monkeypatch):
    panel = funnel_panel(page(pipeline(monkeypatch)))
    assert 'data-metric-family="acquisition_cohort_outcomes"' in panel
    assert 'data-window-basis="contact_created_at"' in panel
    text = _text(panel)
    assert "Lifecycle funnel · Acquisition cohort" in text
    assert "Contacts created in these days, and how far they have got so far." in text
    assert ("Acquisition cohort: contacts created in the window; each stage counts every "
            "contact that has reached it so far") in text
    assert "windowed on the contact's creation date" in text
    # The stage-entry family is never this block's label.
    assert "contacts that entered each stage in the window" not in text
    assert "Entered stage in window" not in text
    assert [c.split(" ")[0] for c in cells(panel).values()] == [
        "Contacts", "Reached", "Reached", "Reached", "Reached"]
    assert "never companies, never deals" in text
    # PR-ADS-182A-F1 (m1): no other Pipeline copy describes the funnel as
    # stage entries — the page subtitle, the page explanation, the API doc.
    stage_entry = re.compile(r"entered each|enter(ed)? (a|each) (lifecycle )?stage in the "
                             r"window|Entered stage in window|stage-entry", re.I)
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    section = html[html.index('<section id="page-pipeline"'):]
    subtitle = re.search(r'<p class="page-subtitle">(.*?)</p>', section, re.S).group(1)
    assert not stage_entry.search(subtitle), subtitle
    assert "contacts created in the window, and how far they have got so far" in subtitle
    expl = _run_app("PAGE_EXPLANATIONS.pipeline")
    for key in ("purpose", "emptyMeans"):
        assert not stage_entry.search(expl[key]), (key, expl[key])
    assert "contacts created in the window" in expl["purpose"]
    server = (ROOT / "api" / "server.py").read_text(encoding="utf-8")
    route = server[server.index('@app.get("/api/pipeline")'):]
    route = route[:route.index('"""', route.index('"""') + 3)]
    assert not stage_entry.search(route), route


def test_05_b_the_contact_list_is_a_different_count_and_says_so(monkeypatch):
    panel = funnel_panel(page(pipeline(monkeypatch)))
    # The funnel's columns are not buttons onto a list of another population.
    assert "data-funnel-event" not in panel
    assert "dash-funnel__stage--selectable" not in panel
    picker = panel[panel.index('class="pipeline-list-picker"'):]
    assert re.findall(r'data-leads-stage="([a-z]+)"', picker) == [
        "lead", "mql", "sql", "opportunity", "customer"]
    assert "a different count from the funnel above" in _text(picker)
    assert "contacts that ENTERED a stage in this window" in _text(picker)
    # The stage-move list keeps its "not new customers" disclosure.
    assert "It is not new customers." in picker
    wire = JS[JS.index("function pipelineWireFunnel("):]
    wire = wire[:wire.index("\n}\n")]
    assert '_leadsView = btn.getAttribute("data-leads-stage")' in wire
    assert "data-funnel-event" not in wire


def test_05_c_the_stage_entry_list_keeps_its_own_truth_notes():
    """PR-ADS-182A-F1 (o3): the retired funnel banner warned the stage-entry
    list when campaign identity was unavailable or the contact sync was
    incomplete. The list says it again, from its own response."""
    out = _run_app(
        "[leadsListTruthNotesHtml({campaign_identity_available: false,"
        "   sync: {bootstrap_status: 'running'}, window: {window_key: 'all_time'}}),"
        " leadsListTruthNotesHtml({campaign_identity_available: true,"
        "   sync: {bootstrap_status: 'running'}, window: {window_key: 'last_quarter'}}),"
        " leadsListTruthNotesHtml({campaign_identity_available: true,"
        "   sync: {bootstrap_status: 'complete'}, window: {window_key: 'all_time'}})]")
    both, running, none = (_text(h) for h in out)
    assert ("Google Ads campaign identity unavailable — campaign- and keyword-attributable "
            "scopes are unavailable (not zero).") in both
    assert "Historical CRM sync in progress (bootstrap: running). All Time does not yet " \
           "represent complete history." in both
    assert running == "Historical CRM sync in progress (bootstrap: running)."
    assert none == ""
    contacts = JS[JS.index("async function leadsRenderContacts("):]
    contacts = contacts[:contacts.index("\n}\n")]
    assert "const notes = leadsListTruthNotesHtml(payload);" in contacts
    assert contacts.count("${notes}") == 2            # the empty list and the table


# ═════════════════════════════════════════════════════════════════════════════
# 6. the audits certify the Pipeline block against its cohort
# ═════════════════════════════════════════════════════════════════════════════
def test_06_a_the_cohort_audit_certifies_the_pipeline_funnel_against_its_slot(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    contract = cohort_contract(monkeypatch)
    for filters in ({}, {"acquisition_group": "other_paid"}, {"scope": "google_ads_source"},
                    {"scope": "keyword_attributable"}):
        p = pipeline(monkeypatch, contract, **filters)
        assert audit.acquisition_funnel_problems(p, "funnel") == [], filters
        assert audit.created_at_disclosure_problems(p) == [], filters
    wrong = pipeline(monkeypatch, contract, acquisition_group="other_paid")
    wrong["funnel"]["totals"]["reached_mql"] += 1
    assert any("by_source_group[other_paid].reached_mql" in x
               for x in audit.acquisition_funnel_problems(wrong, "funnel"))
    # A filter the cohort cannot honour may not publish.
    sneaky = pipeline(monkeypatch, contract, scope="campaign_attributable")
    sneaky["funnel"]["publication"] = {"status": "published", "reason": None}
    sneaky["funnel"]["totals"] = dict.fromkeys(COLUMNS, 1)
    assert audit.acquisition_funnel_problems(sneaky, "funnel")
    # The audit runs it on the Pipeline and Dashboard → Deals payloads.
    a = audit.Audit()
    audit.audit_mart_window(a, window="current_quarter",
                            payloads={"pipeline": wrong, "dashboard/deals": wrong})
    assert any("pipeline acquisition_funnel" in v for v in a.violations), a.violations
    assert any("dashboard/deals acquisition_funnel" in v for v in a.violations)


def test_06_b_the_source_group_partition_is_conserved_or_the_cohort_withholds(monkeypatch):
    from scripts import audit_marketing_outcome_cohorts as audit
    from services import marketing_outcome_cohort_service as M
    from db import crm_funnel_repository as repo
    data = rows()
    monkeypatch.setattr(repo, "fetch_acquisition_cohort_contacts", lambda a, b: {
        "available": True, "rows": data, "missing_created_at": 0})
    cohort = M.build_cohort(data, resolve_label=_resolver,
                            start_at=M.window_instants(START, END)[0],
                            end_before=M.window_instants(START, END)[1])
    assert M.reconcile_cohort(cohort) == []
    cohort["by_source_group"]["organic"]["sqls"] += 1
    assert any("source groups" in p for p in M.reconcile_cohort(cohort))
    contract = cohort_contract(monkeypatch)
    assert audit.cohort_stage_problems(contract) == []
    contract["breakdown"]["by_source_group"]["offline"]["contacts_acquired"] += 1
    assert any("by_source_group contacts_acquired" in p
               for p in audit.cohort_stage_problems(contract))


# ═════════════════════════════════════════════════════════════════════════════
# 7. what only Pipeline's stage-entry funnel used is gone; Activity's stays
# ═════════════════════════════════════════════════════════════════════════════
def test_07_a_the_lead_filter_narrowing_is_gone_and_the_activity_bound_stays(monkeypatch):
    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as crm_funnel
    from services import dashboard_overview_service as ovw
    assert not hasattr(crm_funnel, "undated_reach_for_population")
    for fn in (repo.fetch_undated_stage_reach, crm_funnel.undated_stage_reach):
        code = inspect.getsource(fn).split('"""')[-1]      # the body, not the docstring
        assert "outside_window_rows" not in code and "__outside" not in code, fn.__name__
    bounds = JS[JS.index("function stageEntryCountBounds("):]
    bounds = bounds[:bounds.index("\n}\n")]
    assert "undated_contacts_all_sources" not in bounds          # the code, not a comment
    assert "undated_contacts_all_sources" not in JS.replace(
        "(`undated_contacts_all_sources`, Pipeline's) went with it.", "")
    # Overview → Lifecycle Activity keeps its bound, its rule and its snapshot.
    for name in ("undated_stage_reach", "stage_count_is_bound", "one_read_snapshot"):
        assert callable(getattr(crm_funnel, name)), name
    src = inspect.getsource(ovw)
    assert src.count("_funnel.one_read_snapshot()") == 2
    assert "_funnel.undated_stage_reach(window)" in src
    assert "_funnel.stage_count_is_bound(event, coverage, undated)" in src
    assert "stageEntryCountBounds(a, stages)" in JS
    # The reach still rules out by the SAME window clause the rows use.
    assert "any_stage_date_in_window(" in inspect.getsource(repo.fetch_undated_stage_reach)
    assert "any_stage_date_in_window(" in inspect.getsource(repo.fetch_funnel_contacts)
