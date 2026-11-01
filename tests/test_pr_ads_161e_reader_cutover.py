"""
tests/test_pr_ads_161e_reader_cutover.py

PR-ADS-161E — production readers consume the canonical customer and
closed-won truth.

§1  customers are distinct proven companies (PG: real ledger writer, real
    association sync, real reader contract and Revenue Decision Mart)
§2  revenue, customers and deals withhold independently
§3  derived metrics: ROAS, CAC, SQL-to-customer
§4  the won predicate, won/stage conflicts, undated and future-dated deals,
    Europe/London evidence windows through DST
§5  the UI adapters, evaluated in node: 0, Withheld, Unavailable, View blockers
§6  the cutover audit: inventory, no writes, CRM findings are not failures,
    and its counterfactual on the base revision

No production total is hardcoded: every number comes from the fixtures here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import tests.conftest as conftest  # noqa: F401  (import-order guard)
import analysis.closed_won_truth as cwt
import connectors.hubspot_pull as hubspot
import services.canonical_customer_revenue_service as svc
from services import canonical_revenue_service as crs
from scripts import audit_customer_revenue_reader_cutover as audit
from tests.canonical_ledger_fixtures import (
    company_evidence, ledger_row, patch_canonical_ledger,
)
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: F401
    _have_postgres, pg,
)
from tests.test_pr_ads_161d2_deal_company_associations import (  # noqa: F401
    _ROOT, _deal, _fake_reads, _snapshot, ledger,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

BASE_REVISION = "028effc1aa1172bab001162f09910c03ffb30120"


def _now():
    from db import deal_company_association_repository as repo
    return repo.db_now()["now"]


def _scan(monkeypatch, mapping):
    """A full-population association scan through the real sync service, with
    HubSpot replaced by a dict — no external call."""
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _fake_reads(monkeypatch, mapping)
    return sync_deal_company_associations(mode="incremental")


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _this_year(now, days_ago=1):
    """A close date inside this calendar year (YTD) and this quarter."""
    return _iso(now - timedelta(days=days_ago))


def _last_year(now):
    return _iso(datetime(now.year - 1, 5, 1, 12, tzinfo=timezone.utc))


def _reader(window, now):
    with crs.one_snapshot():
        base = crs.load_won_deals(window, now=now)
        return base, crs.reader_row(base, base.get("deals") or [],
                                    spend_usd=100.0)


def _mart(window, now):
    from services.revenue_decision_mart import build_revenue_decision_mart
    return build_revenue_decision_mart(view="campaign", window=window, now=now)


def _truth(now, key):
    return svc.get_closed_won_truth([("business", key)], now=now)["windows"][0]


# ═════════════════════════════════════════════════════════════════════════════
# §1 — customers are distinct proven companies
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_01_a_published_customer_count_is_the_distinct_proven_company_ids(
        ledger, monkeypatch):
    now = _now()
    for d in ("1", "2", "3"):
        _deal(ledger, d, deal_close_date=_this_year(now))
    _scan(monkeypatch, {"1": [901], "2": [902], "3": [903]})
    base, row = _reader("ytd", now)
    assert base["publication"]["customers"]["status"] == cwt.PUBLISHED
    ids = {c for d in base["deals"]
           for c in base["company_ids_by_deal"][str(d["deal_id"])]}
    assert row["customers"] == len(ids) == 3
    s = _mart("ytd", now)["summary"]
    assert (s["closed_won_deals"], s["customers"], s["customers_status"]) == \
        (3, 3, cwt.PUBLISHED)


@_needs_pg
def test_02_two_won_deals_for_one_company_are_two_deals_and_one_customer(
        ledger, monkeypatch):
    now = _now()
    for d in ("1", "2"):
        _deal(ledger, d, deal_close_date=_this_year(now))
    _scan(monkeypatch, {"1": [901], "2": [901]})
    _, row = _reader("ytd", now)
    assert (row["closed_won_deals"], row["customers"]) == (2, 1)
    s = _mart("ytd", now)["summary"]
    assert (s["closed_won_deals"], s["customers"]) == (2, 1)
    # A subset holding both deals is still one customer — and two subsets of
    # one company are not additive (each says 1, the window says 1).
    with crs.one_snapshot():
        base = crs.load_won_deals("ytd", now=now)
        a = crs.subset_metrics(base, base["deals"][:1])["customers"]["value"]
        b = crs.subset_metrics(base, base["deals"][1:])["customers"]["value"]
    assert (a, b) == (1, 1)


@_needs_pg
def test_03_a_no_company_won_deal_withholds_customers_not_deals(
        ledger, monkeypatch):
    now = _now()
    for d in ("1", "2"):
        _deal(ledger, d, deal_close_date=_this_year(now))
    _scan(monkeypatch, {"1": [901], "2": []})
    base, row = _reader("ytd", now)
    assert (row["closed_won_deals"], row["customers"]) == (2, None)
    assert row["metrics"]["customers"]["status"] == cwt.WITHHELD
    assert row["metrics"]["customers"]["reason"] == cwt.R_CUSTOMER_IDENTITY
    s = _mart("ytd", now)["summary"]
    assert (s["closed_won_deals"], s["customers"], s["customers_status"],
            s["customers_reason"]) == (2, None, cwt.WITHHELD,
                                       cwt.R_CUSTOMER_IDENTITY)


@_needs_pg
def test_04_a_multiple_company_deal_withholds_customers(ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    _scan(monkeypatch, {"1": [901, 902]})
    _, row = _reader("ytd", now)
    assert row["customers"] is None
    assert (row["metrics"]["customers"]["status"],
            row["metrics"]["customers"]["reason"]) == (cwt.WITHHELD,
                                                      cwt.R_CUSTOMER_IDENTITY)
    # Never the "confirmed" lower bound under the customer name.
    s = _mart("ytd", now)["summary"]
    assert s["customers"] is None and s["attributed_customers"] is None


# ═════════════════════════════════════════════════════════════════════════════
# §2 — deals, revenue and customers withhold independently
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_05_a_missing_amount_withholds_revenue_but_not_customers(
        ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    _deal(ledger, "2", deal_close_date=_this_year(now), amount_raw=None,
          revenue_usd=None, currency_status="unavailable",
          currency_reason="no_amount")
    _scan(monkeypatch, {"1": [901], "2": [902]})
    _, row = _reader("ytd", now)
    assert row["won_revenue"] is None
    assert row["metrics"]["revenue_usd"]["status"] == cwt.WITHHELD
    assert row["customers"] == 2
    assert row["metrics"]["customers"]["status"] == cwt.PUBLISHED


@_needs_pg
def test_06_ytd_publishes_revenue_while_withholding_customers(
        ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now), revenue_usd=1200.0,
          amount_raw=1200.0)
    _deal(ledger, "2", deal_close_date=_this_year(now), revenue_usd=800.0,
          amount_raw=800.0)
    _scan(monkeypatch, {"1": [901], "2": []})
    s = _mart("ytd", now)["summary"]
    assert s["won_revenue_usd"] == 2000.0
    assert s["metrics"]["revenue_usd"]["status"] == cwt.PUBLISHED
    assert (s["customers"], s["customers_status"]) == (None, cwt.WITHHELD)


@_needs_pg
def test_07_all_time_withholds_both_for_different_reasons(ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    _deal(ledger, "2", deal_close_date=_last_year(now), amount_raw=None,
          revenue_usd=None, currency_status="unavailable",
          currency_reason="no_amount")
    _deal(ledger, "3", deal_close_date=_last_year(now))
    _scan(monkeypatch, {"1": [901], "2": [902], "3": []})
    t = _truth(now, "all_time")["publication"]
    assert (t["revenue_usd"]["status"], t["customers"]["status"]) == \
        (cwt.WITHHELD, cwt.WITHHELD)
    assert t["revenue_usd"]["reason"] != t["customers"]["reason"]
    s = _mart("all_time", now)["summary"]
    assert s["won_revenue_usd"] is None and s["customers"] is None
    assert s["metrics"]["revenue_usd"]["reason"] == t["revenue_usd"]["reason"]
    assert s["customers_reason"] == t["customers"]["reason"]
    # YTD holds only deal 1 — both publish there.
    y = _mart("ytd", now)["summary"]
    assert (y["customers"], y["metrics"]["revenue_usd"]["status"]) == \
        (1, cwt.PUBLISHED)


def test_10_a_customer_lower_bound_is_never_published_as_the_total():
    """The canonical service discloses ``confirmed_customer_lower_bound`` when
    customers are withheld. No reader republishes it: the reader contract and
    every page read only ``outcomes.customers`` / the canonical metric."""
    for rel in ("services/canonical_revenue_service.py",
                "services/revenue_decision_mart.py",
                "services/revenue_attribution_service.py",
                "services/dashboard_overview_service.py",
                "services/dashboard_channels_service.py",
                "services/dashboard_campaigns_service.py",
                "services/dashboard_deals_service.py",
                "services/canonical_unit_economics_service.py"):
        assert "lower_bound" not in (_ROOT / rel).read_text(encoding="utf-8"), rel
    js = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "lower_bound" not in js
    base = {"available": True, "deals": [ledger_row("1"), ledger_row("2")],
            "publication": {m: {"status": cwt.PUBLISHED, "reason": None}
                            for m in ("closed_won_deals", "revenue_usd")}
            | {"customers": {"status": cwt.WITHHELD,
                             "reason": cwt.R_CUSTOMER_IDENTITY}},
            "company_ids_by_deal": {"1": ["901"], "2": []}}
    row = crs.reader_row(base, base["deals"])
    assert row["customers"] is None   # never 1 (the companies we do know)


# ═════════════════════════════════════════════════════════════════════════════
# §3 — derived metrics
# ═════════════════════════════════════════════════════════════════════════════

def _base(*, revenue=cwt.PUBLISHED, customers=cwt.PUBLISHED, companies=None):
    deals = [ledger_row("1", revenue_usd=600.0), ledger_row("2", revenue_usd=400.0)]
    return {"available": True, "deals": deals,
            "publication": {
                "closed_won_deals": {"status": cwt.PUBLISHED, "reason": None},
                "revenue_usd": {"status": revenue, "reason": None
                                if revenue == cwt.PUBLISHED else cwt.R_REVENUE_UNPROVEN},
                "customers": {"status": customers, "reason": None
                              if customers == cwt.PUBLISHED else cwt.R_CUSTOMER_IDENTITY}},
            "company_ids_by_deal": companies or {"1": ["901"], "2": ["901"]}}


def test_11_roas_is_withheld_when_revenue_is_withheld():
    base = _base(revenue=cwt.WITHHELD)
    row = crs.reader_row(base, base["deals"], spend_usd=500.0)
    assert row["roas"] is None
    assert row["metrics"]["roas"]["status"] == cwt.WITHHELD
    assert row["metrics"]["roas"]["blocked_by"]["reason"] == cwt.R_REVENUE_UNPROVEN
    ok = crs.reader_row(_base(), _base()["deals"], spend_usd=500.0)
    assert ok["roas"] == 2.0
    assert ok["metrics"]["roas"]["cohort_basis"] == cwt.PERIOD_COHORT_BASIS
    # Spend unknown: unavailable, with the spend reason — never 0.
    no_spend = crs.reader_row(_base(), _base()["deals"], spend_usd=None,
                              spend_reason=cwt.R_SPEND_UNAVAILABLE)
    assert (no_spend["roas"], no_spend["metrics"]["roas"]["reason"]) == \
        (None, cwt.R_SPEND_UNAVAILABLE)


def test_12_cac_divides_by_canonical_customers_never_won_deals():
    base = _base()                        # two deals, one company
    row = crs.reader_row(base, base["deals"], spend_usd=500.0)
    assert (row["closed_won_deals"], row["customers"]) == (2, 1)
    assert row["cac"] == 500.0            # not 250.0 (spend ÷ deals)
    held = _base(customers=cwt.WITHHELD)
    row = crs.reader_row(held, held["deals"], spend_usd=500.0)
    assert row["cac"] is None
    assert row["metrics"]["cac"]["status"] == cwt.WITHHELD
    assert row["metrics"]["cac"]["blocked_by"]["reason"] == cwt.R_CUSTOMER_IDENTITY


def test_13_sql_to_customer_rate_is_unavailable_across_two_cohorts():
    """SQLs are an acquisition cohort (contact creation) at campaign scope; won
    deals a close-date cohort at all-source scope. No canonical service
    supplies both over one cohort and scope, so the rate is unavailable with
    the reason — on every page that used to compute it."""
    from services import dashboard_deals_service as deals_svc
    from services import dashboard_revenue_service as rev_svc
    summary = {"sqls": 40, "closed_won_deals": 4, "customers": 3,
               "customers_status": cwt.PUBLISHED, "won_revenue_usd": 1000.0,
               "revenue_total_available": True, "metrics": {}}
    d = deals_svc._build_kpis(summary, True, 0, False)
    r = rev_svc._build_kpis(summary, [], revenue_connected=True,
                            amounts_complete=True)
    for kpis in (d, r):
        assert kpis["sql_to_customer_rate"] is None
        assert kpis["sql_to_customer_rate_reason"] == \
            cwt.R_SQL_TO_CUSTOMER_INCOMPATIBLE
    a = audit.Audit()
    audit.structural_checks(a)
    assert a.checks["sql_to_customer_not_mixed"] is True
    rate = cwt.incompatible_rate(cwt.R_SQL_TO_CUSTOMER_INCOMPATIBLE)
    assert (rate["value"], rate["status"]) == (None, cwt.UNAVAILABLE)


# ═════════════════════════════════════════════════════════════════════════════
# §4 — won predicate, conflicts, undated / future-dated, DST
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_14_the_won_predicate_is_the_won_flag(ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    # In a non-won stage but flagged won: counted (the flag is the predicate;
    # the stage disagreement is reported separately, test_15).
    _deal(ledger, "2", deal_close_date=_last_year(now), hs_is_closed_won=False,
          deal_stage_id="closedlost")
    _scan(monkeypatch, {"1": [901]})
    base, row = _reader("ytd", now)
    assert [str(d["deal_id"]) for d in base["deals"]] == ["1"]
    assert row["closed_won_deals"] == 1


@_needs_pg
def test_15_a_won_flag_stage_conflict_withholds_the_affected_totals(
        ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    _deal(ledger, "2", deal_close_date=_this_year(now), hs_is_closed_won=False)
    _scan(monkeypatch, {"1": [901]})
    base, row = _reader("ytd", now)
    assert base["available"] is False
    assert base["publication"]["closed_won_deals"] == {
        "status": cwt.WITHHELD, "reason": cwt.R_WON_DEFINITION_CONFLICT}
    assert row["closed_won_deals"] is None and row["won_revenue"] is None
    s = _mart("ytd", now)["summary"]
    assert s["closed_won_deals"] is None and s["won_revenue_usd"] is None
    assert s["metrics"]["closed_won_deals"]["status"] == cwt.WITHHELD


@_needs_pg
def test_16_undated_won_deals_are_not_lost_from_all_time(ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    _deal(ledger, "2", deal_close_date=None)
    _scan(monkeypatch, {"1": [901], "2": [902]})
    base, row = _reader("all_time", now)
    assert base["available"] is True
    assert sorted(str(d["deal_id"]) for d in base["deals"]) == ["1", "2"]
    assert base["undated_won_deals_included"] == 1
    assert (row["closed_won_deals"], row["customers"]) == (2, 2)
    # A finite window cannot place it, so it withholds rather than drop it.
    ytd, _ = _reader("ytd", now)
    assert ytd["publication"]["closed_won_deals"] == {
        "status": cwt.WITHHELD, "reason": cwt.R_UNDATED_WON_DEALS}


@_needs_pg
def test_17_future_dated_closes_are_in_no_window(ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    _deal(ledger, "2", deal_close_date=_iso(now + timedelta(hours=3)))
    _scan(monkeypatch, {"1": [901], "2": [902]})
    for key in ("ytd", "all_time"):
        base, row = _reader(key, now)
        assert [str(d["deal_id"]) for d in base["deals"]] == ["1"], key
        assert row["closed_won_deals"] == 1
    mem = _truth(now, "all_time")["membership"]
    assert mem["future_dated_won_deals"] == 1


def test_18_europe_london_evidence_windows_hold_through_dst(monkeypatch):
    """BST ends 2026-10-25. A 7-day evidence window on 2026-10-27 runs from
    London midnight on 21 October (BST, 23:00Z on the 20th) to London midnight
    on 28 October (GMT, 00:00Z). A deal at 23:30Z on the 20th is inside it; a
    UTC-day window would have dropped it."""
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    rows = [ledger_row("in", deal_close_date="2026-10-20T23:30:00+00:00"),
            ledger_row("out", deal_close_date="2026-10-20T22:30:00+00:00"),
            ledger_row("edge", deal_close_date="2026-10-27T23:59:00+00:00")]
    patch_canonical_ledger(monkeypatch, rows,
                           company_associations=company_evidence(rows))
    b = svc.window_bounds("evidence", "7d", now)
    assert b["start"] == datetime(2026, 10, 20, 23, tzinfo=timezone.utc)
    assert b["end"] == datetime(2026, 10, 28, 0, tzinfo=timezone.utc)
    truth = svc.get_closed_won_truth([("evidence", "7d")], now=now)["windows"][0]
    ids = set(truth["membership"]["deal_ids"])
    assert "in" in ids and "out" not in ids
    assert "edge" not in ids                   # later today: not yet closed
    # The reader contract evaluates the same bounds by the same membership.
    base = crs.load_won_deals(start=b["start"], end=b["end"], now=now)
    assert {str(d["deal_id"]) for d in base["deals"]} == ids


# ═════════════════════════════════════════════════════════════════════════════
# §5 — the UI adapters, evaluated in node
# ═════════════════════════════════════════════════════════════════════════════

_JS_PIECES = ("escapeHtml", "dashValue", "canonicalReasonLabel",
              "crmBlockerFamily", "crmBlockersHref", "canonicalBlockingReason",
              "canonicalMetricHtml", "rowMetricHtml", "_ctePendingFilters")
_JS_CONSTS = ("CANONICAL_REASON_LABELS", "CRM_BLOCKER_FAMILY", "ROW_METRIC_KEYS")


def _js_extract(source: str, head: str) -> str:
    i = source.index(head)
    j = source.index("{", i)
    depth = 0
    for k in range(j, len(source)):
        depth += {"{": 1, "}": -1}.get(source[k], 0)
        if depth == 0:
            end = k + 1
            if source[end:end + 1] == ";":
                end += 1
            return source[i:end]
    raise AssertionError(head)


def _node(calls: dict, *, role=None, hash_="") -> dict:
    node = shutil.which("node")
    assert node, "node is a CI dependency (node --check static/app.js)"
    src = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    parts = [_js_extract(src, f"const {c} = ") for c in _JS_CONSTS]
    parts += [_js_extract(src, f"function {f}(") for f in _JS_PIECES]
    user = "null" if role is None else json.dumps({"role": role})
    script = (f"let _currentUser = {user};\n"
              f"const window = {{ location: {{ hash: {json.dumps(hash_)} }} }};\n"
              + "\n".join(parts) + "\n"
              f"const calls = {json.dumps(calls)};\n"
              "const out = {};\n"
              "for (const [k, [fn, args]] of Object.entries(calls)) {\n"
              "  const f = { canonicalMetricHtml, rowMetricHtml, crmBlockersHref,\n"
              "              _ctePendingFilters }[fn];\n"
              "  const real = args.map((a) => a === '__fmt' ? ((v) => String(v)) : a);\n"
              "  out[k] = f(...real);\n"
              "}\n"
              "console.log(JSON.stringify(out));\n")
    res = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_08_a_published_zero_is_displayed_as_zero():
    out = _node({
        "metric": ["canonicalMetricHtml", [{"value": 0, "status": "published"}, "__fmt"]],
        "row": ["rowMetricHtml", [{"customers": 0, "metrics": {"customers": {
            "value": 0, "status": "published", "reason": None}}}, "customers", "__fmt"]],
        "plain": ["rowMetricHtml", [{"closed_won_deals": 0}, "closed_won_deals", "__fmt"]],
    })
    assert out == {"metric": "0", "row": "0", "plain": "0"}


def test_09_withheld_and_unavailable_never_display_as_zero_or_a_dash():
    out = _node({
        "withheld": ["canonicalMetricHtml", [{"value": None, "status": "withheld",
                                              "reason": cwt.R_CUSTOMER_IDENTITY}, "__fmt"]],
        "unavailable": ["canonicalMetricHtml", [{"value": None, "status": "unavailable",
                                                 "reason": "canonical_ledger_unreadable"},
                                                "__fmt"]],
        # A withheld metric that still (wrongly) carried a number renders the
        # word — the renderer trusts the status, not the value.
        "leaky": ["canonicalMetricHtml", [{"value": 7, "status": "withheld",
                                           "reason": cwt.R_REVENUE_UNPROVEN}, "__fmt"]],
        "row": ["rowMetricHtml", [{"customers": None, "metrics": {"customers": {
            "value": None, "status": "withheld",
            "reason": cwt.R_CUSTOMER_IDENTITY}}}, "customers", "__fmt"]],
    }, role="viewer")
    for k, html in out.items():
        assert ">0<" not in html and "—" not in html and ">7<" not in html, k
    assert "Withheld" in out["withheld"] and "no company, or several" in out["withheld"]
    assert "Unavailable" in out["unavailable"] and "could not be read" in out["unavailable"]
    assert "Withheld" in out["leaky"] and "proven USD amount" in out["leaky"]
    assert "Withheld" in out["row"]


def test_19_api_output_and_ui_adapters_preserve_status_and_reason():
    base = _base(customers=cwt.WITHHELD)
    row = crs.reader_row(base, base["deals"], spend_usd=500.0)
    payload = json.loads(json.dumps(row))          # what the API serializes
    for m in ("customers", "cac"):
        assert payload["metrics"][m]["status"] == cwt.WITHHELD
    assert payload["metrics"]["customers"]["reason"] == cwt.R_CUSTOMER_IDENTITY
    out = _node({"customers": ["rowMetricHtml", [payload, "customers", "__fmt"]],
                 "cac": ["rowMetricHtml", [payload, "cac", "__fmt"]]})
    assert "Withheld" in out["customers"] and "no company, or several" in out["customers"]
    # CAC names the input that blocked it, not a generic word.
    assert "Withheld" in out["cac"] and "no company, or several" in out["cac"]


def test_20_admin_view_blockers_select_the_family_and_window():
    withheld = {"value": None, "status": "withheld", "reason": cwt.R_CUSTOMER_IDENTITY}
    opts = {"windowType": "business", "windowKey": "ytd"}
    out = _node({
        "html": ["canonicalMetricHtml", [withheld, "__fmt", opts]],
        "identity": ["crmBlockersHref", [{**opts, "reason": cwt.R_CUSTOMER_IDENTITY}]],
        "revenue": ["crmBlockersHref", [{**opts, "reason": cwt.R_REVENUE_UNPROVEN}]],
        "membership": ["crmBlockersHref", [{**opts, "reason": cwt.R_UNDATED_WON_DEALS}]],
        "conflict": ["crmBlockersHref", [{**opts, "reason": cwt.R_WON_DEFINITION_CONFLICT}]],
        # An outage has no CRM record to fix: no link.
        "outage": ["crmBlockersHref", [{**opts, "reason": "canonical_ledger_unreadable"}]],
    }, role="admin")
    assert out["identity"] == "#/health?cte_window=business%2Fytd&cte_family=customer_identity"
    assert out["revenue"].endswith("cte_family=revenue_proof")
    assert out["membership"].endswith("cte_family=won_deal_membership")
    assert out["conflict"].endswith("cte_family=won_deal_membership")
    assert out["outage"] is None
    assert "View blockers" in out["html"]
    assert f'href="{out["identity"].replace("&", "&amp;")}"' in out["html"]
    # The workbench reads exactly those filters back from the hash, and its
    # window ids use the same "type/key" form (docs/47).
    parsed = _node({"f": ["_ctePendingFilters", []]}, role="admin",
                   hash_=out["identity"][1:])["f"]
    assert parsed == {"window": "business/ytd", "family": "customer_identity",
                      "reason": ""}
    from services import crm_truth_exceptions_service as cte
    assert {cte.FAMILY_IDENTITY, cte.FAMILY_REVENUE, cte.FAMILY_MEMBERSHIP} == \
        {"customer_identity", "revenue_proof", "won_deal_membership"}


def test_21_non_admin_output_has_no_workbench_link():
    withheld = {"value": None, "status": "withheld", "reason": cwt.R_CUSTOMER_IDENTITY}
    opts = {"windowType": "business", "windowKey": "ytd"}
    for role in ("viewer", None):
        out = _node({"html": ["canonicalMetricHtml", [withheld, "__fmt", opts]],
                     "href": ["crmBlockersHref", [{**opts, "family": "customer_identity"}]]},
                    role=role)
        assert out["href"] is None
        assert "View blockers" not in out["html"] and "#/health" not in out["html"]
        # The viewer still sees the truthful status and reason.
        assert "Withheld" in out["html"] and "no company, or several" in out["html"]


# ═════════════════════════════════════════════════════════════════════════════
# §6 — the cutover audit
# ═════════════════════════════════════════════════════════════════════════════

def test_22_every_inventoried_reader_is_covered_by_the_audit(monkeypatch):
    a = audit.Audit()
    found = audit.structural_checks(a)
    assert all(a.checks.values()), a.violations
    inventoried = {r["consumer"] for r in audit.INVENTORY}
    assert set(found["readers"]) <= inventoried
    for row in audit.INVENTORY:
        assert set(row) >= {"consumer", "existing_source", "existing_meaning",
                            "canonical_replacement", "window_cohort", "ui_effect",
                            "status"}, row["consumer"]
        assert row["status"] in ("migrated", "withheld", "removed", "deferred")
        if row["status"] == "deferred":
            assert not row["families"], f"{row['consumer']} defers an outcome"
    # The guard fires: drop one migrated reader from the inventory.
    trimmed = tuple(r for r in audit.INVENTORY
                    if r["consumer"] != "services/source_attribution_service.py")
    monkeypatch.setattr(audit, "INVENTORY", trimmed)
    b = audit.Audit()
    audit.structural_checks(b)
    assert b.checks["inventory_complete"] is False


@_needs_pg
def test_23_the_audit_makes_no_external_call_and_no_database_write(
        ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    _scan(monkeypatch, {"1": [901]})

    def _refuse(*a, **k):
        raise AssertionError("external call during the audit")
    for name in dir(hubspot):
        if name.startswith(("fetch_", "search_", "batch_")):
            monkeypatch.setattr(hubspot, name, _refuse)
    import requests
    monkeypatch.setattr(requests.Session, "request", _refuse)

    from tests.test_pr_ads_161d2_deal_company_associations import _q
    def _ledger_fp():
        return _q("SELECT count(*) AS n, md5(coalesce(string_agg(x::text, '|' "
                  "ORDER BY x::text), '')) AS h FROM hubspot_deal_ledger x")[0]
    before = (_snapshot(), _ledger_fp())
    _, report = audit.run(now=now)
    assert (_snapshot(), _ledger_fp()) == before
    assert report["database_writes_performed"] is False
    assert report["hubspot_calls_performed"] is False
    assert report["checks"]["no_external_calls_no_writes"] is True
    assert report["exit_code"] == audit.EXIT_OK, report["violations"]


@_needs_pg
def test_24_crm_findings_stay_visible_and_are_not_software_failures(
        ledger, monkeypatch):
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now))
    _deal(ledger, "2", deal_close_date=_this_year(now), amount_raw=None,
          revenue_usd=None, currency_status="unavailable",
          currency_reason="no_amount")
    _deal(ledger, "3", deal_close_date=None)
    _scan(monkeypatch, {"1": [901], "2": [], "3": [903, 904]})
    _, report = audit.run(now=now)
    assert report["exit_code"] == audit.EXIT_OK, report["violations"]
    assert all(report["checks"].values())
    by = {w["window"]: w for w in report["windows"]}
    # The findings are reported, not hidden.
    assert by["all_time"]["publication"]["customers"]["status"] == cwt.WITHHELD
    assert by["all_time"]["publication"]["revenue_usd"]["status"] == cwt.WITHHELD
    assert by["ytd"]["publication"]["closed_won_deals"]["reason"] == \
        cwt.R_UNDATED_WON_DEALS
    assert by["all_time"]["undated_won_deals"] == 1
    # The page agrees with the canonical verdict for every window.
    for key, w in by.items():
        assert w["page"]["customers_status"] == w["publication"]["customers"]["status"], key


@_needs_pg
def test_25_the_audit_goes_red_when_a_page_republishes_a_withheld_total(
        ledger, monkeypatch):
    """Counterfactual for the live checks: a mart that puts the deal count back
    under ``customers`` (the pre-161E behaviour) breaks the contract."""
    now = _now()
    for d in ("1", "2"):
        _deal(ledger, d, deal_close_date=_this_year(now))
    _scan(monkeypatch, {"1": [901], "2": []})
    from services import revenue_decision_mart as mart_mod
    real = mart_mod.build_revenue_decision_mart

    def legacy(*a, **k):
        out = real(*a, **k)
        s = out["summary"]
        s["customers"] = s.get("closed_won_deals")          # won deals as customers
        return out
    monkeypatch.setattr(mart_mod, "build_revenue_decision_mart", legacy)
    _, report = audit.run(now=now)
    assert report["exit_code"] == audit.EXIT_VIOLATION
    assert report["checks"]["withheld_values_absent"] is False
    assert report["checks"]["withheld_not_reconstructable"] is False


def test_26_counterfactual_the_audit_fails_on_the_base_revision(tmp_path):
    """On the base revision the legacy readers still exist: the structural
    checks go red there and are green here."""
    git = shutil.which("git")
    assert git
    have = subprocess.run([git, "-C", str(_ROOT), "cat-file", "-e",
                           f"{BASE_REVISION}^{{commit}}"], capture_output=True)
    assert have.returncode == 0, "CI checks out full history (fetch-depth: 0)"
    archive = subprocess.run([git, "-C", str(_ROOT), "archive", BASE_REVISION],
                             capture_output=True, check=True).stdout
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path)
    # The audit itself is new in this PR: evaluate the base tree with it.
    (tmp_path / audit.AUDIT_FILE).write_text(
        (_ROOT / audit.AUDIT_FILE).read_text(encoding="utf-8"), encoding="utf-8")
    import importlib
    base_audit = importlib.reload(audit)
    try:
        base_audit._ROOT = tmp_path
        a = base_audit.Audit()
        base_audit.structural_checks(a)
        for name in ("no_won_deal_as_customer_label", "no_legacy_table_gate",
                     "sql_to_customer_not_mixed", "admin_links_resolve",
                     "non_admin_has_no_link", "customer_readers_canonical"):
            assert a.checks.get(name) is False, (name, a.checks)
    finally:
        importlib.reload(audit)
    here = audit.Audit()
    audit.structural_checks(here)
    assert all(here.checks.values()), here.violations


def test_27_ci_runs_this_suite_with_postgres_and_asserts_it_ran():
    wf = (_ROOT / ".github" / "workflows" / "pr-ads-153d-checks.yml").read_text()
    pg_step = wf[wf.index("- name: PostgreSQL integration tests"):
                 wf.index("- name: Assert the PostgreSQL tests actually ran")]
    assert "tests/test_pr_ads_161e_reader_cutover.py" in pg_step
    did_run = wf[wf.index("- name: Assert the PostgreSQL tests actually ran"):]
    assert '"tests.test_pr_ads_161e_reader_cutover"' in did_run


# ═════════════════════════════════════════════════════════════════════════════
# §7 — independent truth review: each case was executed against the first
#      cut of this PR and reproduced a defect; each passes here.
# ═════════════════════════════════════════════════════════════════════════════

_UNPLACED_GOOGLE_DEAL = {
    "deal_id": "d9", "company": "Beta", "country": "Germany",
    "campaign_name": None, "acquisition_group": "google_ads",
    "attribution_status": "attributed", "deal_close_date": "2026-05-02",
    "deal_amount_usd": 1000.0, "match_source": "gclid"}


def _gated_campaigns(monkeypatch, **kw):
    """The Campaigns tab fixture plus one Google Ads won deal on no campaign —
    the canonical placement verdict withholds every per-campaign figure."""
    from tests import test_pr_ads_137_dashboard_campaigns as t137
    deals = [dict(d) for d in t137.DEAL_ROWS] + [dict(_UNPLACED_GOOGLE_DEAL)]
    t137._patch_durable(monkeypatch, deals=deals, **kw)
    return t137, deals


#: Claims that are false while a count or total is withheld, and the wrong
#: reasons the first cut gave for a withholding.
_FALSE_CLAIMS = ("won no deals", "no campaign has a closed-won deal",
                 "spend without won-deal proof", "spend without sql / customer proof",
                 "produced at least one", "maps to a campaign this window",
                 "maps to a country this window", "could not be read",
                 "unknown deal amount", "integration is disconnected")


def _card_text(payload):
    return " ".join(f"{c.get('headline')} {c.get('body')}"
                    for c in payload.get("decision_cards") or []).lower()


def test_28_a_a_withheld_campaign_deal_count_is_never_waste_or_no_won_deals(
        monkeypatch):
    t137, _ = _gated_campaigns(monkeypatch)
    from services.revenue_attribution_service import (
        VERDICT_OUTCOME_WITHHELD, build_revenue_attribution,
    )
    from services.dashboard_campaigns_service import (
        STATUS_OUTCOME_WITHHELD, build_dashboard_campaigns,
    )
    ra = build_revenue_attribution("ytd", now=t137.NOW)
    assert ra["closed_won_truth"]["campaign_placement_gate"]["reason"] == \
        cwt.R_CAMPAIGN_UNPLACED
    for c in ra["campaigns"]:
        assert c["closed_won_deals"] is None and c["won_revenue"] is None
        assert c["verdict"] in (VERDICT_OUTCOME_WITHHELD, "mapping_required"), c
    # The campaign-attributable SCOPE total is exact for its scope: not gated.
    assert ra["summary"]["closed_won_deals"] == 2
    out = build_dashboard_campaigns("ytd", now=t137.NOW)
    for c in out["campaigns"]:
        if c["campaign_name"] == "Unattributed / Needs Review":
            continue
        assert c["status"] == STATUS_OUTCOME_WITHHELD
        assert c["deals"] is None                  # no proof list beside it
    text = _card_text(out)
    assert not any(p in text for p in _FALSE_CLAIMS), text


def test_28_b_the_overview_never_claims_no_producers_over_withheld_counts(
        monkeypatch):
    from tests import test_pr_ads_134_dashboard_overview as t134
    rows = [t134.WON_ROW_MAPPED | {"deal_close_date": "2026-05-01"},
            dict(_UNPLACED_GOOGLE_DEAL)]
    leads = [dict(r, status_category="lead") for r in t134.LEAD_ROWS]
    t134._patch_durable(monkeypatch, won_rows=rows, lead_rows=leads)
    from tests.canonical_ledger_fixtures import from_legacy_deal_rows
    ledger_rows = from_legacy_deal_rows(rows)
    patch_canonical_ledger(monkeypatch, ledger_rows,
                           company_associations=company_evidence(ledger_rows))
    from services.dashboard_overview_service import build_dashboard_overview
    ov = build_dashboard_overview("ytd", now=t134.NOW)
    text = _card_text(ov)
    assert "no campaign has a closed-won deal" not in text
    assert "withheld" in text
    waste = (ov.get("top_signals") or {}).get("biggest_waste")
    assert waste is None, waste


def test_28_c_an_undated_won_deal_never_reads_as_no_deals_or_an_outage(
        monkeypatch):
    from tests import test_pr_ads_138_dashboard_countries as t138
    from tests.canonical_ledger_fixtures import from_legacy_deal_rows
    deals = [dict(d) for d in t138.DEAL_ROWS] + [
        {"deal_id": "d99", "company": "Undated", "country": "Germany",
         "campaign_name": None, "deal_close_date": None, "deal_amount_usd": 10.0}]
    t138._patch_durable(monkeypatch, deals=deals)
    rows = from_legacy_deal_rows(deals)
    rows[-1]["deal_close_date"] = None
    patch_canonical_ledger(monkeypatch, rows,
                           company_associations=company_evidence(rows))
    from services.dashboard_countries_service import build_dashboard_countries
    out = build_dashboard_countries("ytd", now=t138.NOW)
    assert out["kpis"]["countries_with_closed_won_deals"] is None
    assert out["kpis"]["deals_metric"]["reason"] == cwt.R_UNDATED_WON_DEALS
    text = _card_text(out) + " " + " ".join(
        u.get("reason") or "" for u in out.get("unavailable") or []).lower()
    assert not any(p in text for p in _FALSE_CLAIMS), text


def test_29_per_campaign_placement_is_the_canonical_verdict(monkeypatch):
    """An unmapped campaign label: the canonical resolver places the deal on no
    campaign, so the canonical ``campaign_revenue`` is withheld — and the page
    republishes exactly that, instead of a label-presence approximation."""
    from tests import test_pr_ads_137_dashboard_campaigns as t137
    from tests.canonical_ledger_fixtures import from_legacy_deal_rows, label_resolver
    deals = [dict(d) for d in t137.DEAL_ROWS[:2]] + [
        {"deal_id": "d5", "company": "Renamed", "country": "United States",
         "campaign_name": "Global Competitors (old name)",
         "deal_close_date": "2026-06-10", "deal_amount_usd": 7000.0,
         "match_status": "matched", "match_source": "gclid"}]
    t137._patch_durable(monkeypatch, deals=deals)
    rows = from_legacy_deal_rows(deals)
    patch_canonical_ledger(
        monkeypatch, rows, company_associations=company_evidence(rows),
        resolver=label_resolver(unmapped=["Global Competitors (old name)"]))
    canon = svc.get_closed_won_truth([("business", "ytd")],
                                     now=t137.NOW)["windows"][0]
    assert canon["publication"]["campaign_revenue"]["reason"] == cwt.R_CAMPAIGN_UNPLACED
    from services.revenue_attribution_service import build_revenue_attribution
    ra = build_revenue_attribution("ytd", now=t137.NOW)
    assert ra["closed_won_truth"]["campaign_placement_gate"]["reason"] == \
        cwt.R_CAMPAIGN_UNPLACED
    gc = next(c for c in ra["campaigns"] if c["campaign_name"] == "Global Competitors")
    assert gc["won_revenue"] is None and gc["roas"] is None
    assert gc["verdict"] != "winner"
    # Control: the same label mapped → placed → published.
    patch_canonical_ledger(monkeypatch, rows,
                           company_associations=company_evidence(rows))
    ra = build_revenue_attribution("ytd", now=t137.NOW)
    assert ra["closed_won_truth"]["campaign_placement_gate"] is None


def test_30_a_negative_amount_is_never_netted_into_any_total(monkeypatch):
    from tests import test_pr_ads_137_dashboard_campaigns as t137
    from tests.canonical_ledger_fixtures import from_legacy_deal_rows
    deals = [dict(d) for d in t137.DEAL_ROWS]
    t137._patch_durable(monkeypatch, deals=deals)
    rows = from_legacy_deal_rows(deals)
    rows[2] = dict(rows[2], amount_raw=-500.0, revenue_usd=-500.0)
    patch_canonical_ledger(monkeypatch, rows,
                           company_associations=company_evidence(rows))
    netted = 18000.0 + 11000.0 - 500.0
    from services.revenue_decision_mart import build_revenue_decision_mart
    from services.revenue_attribution_service import build_revenue_attribution
    from services.canonical_unit_economics_service import build_unit_economics
    from services.source_attribution_service import build_revenue_by_source
    pages = {"mart": build_revenue_decision_mart(view="campaign", window="ytd",
                                                 now=t137.NOW),
             "attribution": build_revenue_attribution("ytd", now=t137.NOW),
             "unit_economics": build_unit_economics("ytd", now=t137.NOW),
             "by_source": build_revenue_by_source("ytd", now=t137.NOW)}
    assert pages["mart"]["summary"]["metrics"]["revenue_usd"]["status"] == cwt.WITHHELD
    for name, payload in pages.items():
        hits = [p for p, d in audit._walk(payload) for k, v in d.items()
                if isinstance(v, float) and abs(v - netted) < 0.01]
        assert not hits, (name, hits[:5])
    # The invalid deal is counted as unproven, never as a priced deal.
    assert crs.summarize_deals(rows, "all_source")["currency_unavailable_deals"] == 1


def test_31_roas_and_cac_need_a_scope_google_ads_spend_can_have_produced(
        monkeypatch):
    from tests import test_pr_ads_137_dashboard_campaigns as t137
    t137._patch_durable(monkeypatch)
    from tests.canonical_ledger_fixtures import from_legacy_deal_rows
    rows = from_legacy_deal_rows(t137.DEAL_ROWS)
    patch_canonical_ledger(monkeypatch, rows,
                           company_associations=company_evidence(rows))
    from services.canonical_unit_economics_service import build_unit_economics
    allsrc = build_unit_economics("ytd", scope="all_source", now=t137.NOW)["advertising"]
    for m in ("roas", "cac"):
        assert allsrc[m] is None
        assert allsrc["metrics"][m]["reason"] == cwt.R_SCOPE_INCOMPATIBLE
    camp = build_unit_economics("ytd", scope="campaign_attributable",
                                now=t137.NOW)["advertising"]
    assert camp["metrics"]["roas"]["status"] == cwt.PUBLISHED
    assert camp["metrics"]["roas"]["cohort_basis"] == cwt.PERIOD_COHORT_BASIS


def test_32_legacy_outcome_payloads_withhold_every_recoverable_figure():
    import api.server as server
    mailchimp = {"recipients_inspected": 40, "customer_contacts": 3,
                 "contacts_with_closed_won_revenue": 2,
                 "reconciliation": {"attributed_customers": 3,
                                    "attributed_closed_won_revenue_usd": 900.0,
                                    "durable_customer_population": 7,
                                    "customers_is_subset_of_durable": True,
                                    "definitions": {"customer": "text kept"}}}
    out = server._withhold_legacy_closed_won_outcomes(mailchimp)
    assert out["recipients_inspected"] == 40                 # coverage kept
    for k in ("customer_contacts", "contacts_with_closed_won_revenue"):
        assert out[k] is None
    rec = out["reconciliation"]
    assert all(rec[k] is None for k in ("attributed_customers",
                                        "attributed_closed_won_revenue_usd",
                                        "durable_customer_population",
                                        "customers_is_subset_of_durable"))
    assert rec["definitions"]["customer"] == "text kept"
    assert out["closed_won_outcomes_status"]["status"] == "withheld"
    src = (_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    i = src.index("async def get_gclid_readiness(")
    region = src[i:src.index("@app.", i + 1)]
    for key in ("won_deals", "deals_with_direct_gclid", "deals_with_contact_gclid",
                "deals_without_gclid",
                # The three joinability tiers partition the same local won
                # deals, so their sum would rebuild the withheld count.
                "tier_1_possible_matches", "tier_2_source_tag_matches",
                "tier_3_estimated_required"):
        assert f'"{key}"' in region
    js = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    for key in ("tier_1_possible_matches", "tier_2_source_tag_matches",
                "tier_3_estimated_required"):
        assert f"summary.{key} || 0}}" not in js, key


@_needs_pg
def test_33_the_audit_goes_red_when_a_page_keeps_proof_beside_a_withheld_count(
        ledger, monkeypatch):
    """Counterfactual for the page-wide leak walk: the Campaigns tab putting
    its proof-deal list back beside a withheld count (the first cut's
    behaviour) is caught, on any page, not only the Mart."""
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now), acquisition_group="google_ads",
          attribution_status="attributed", gclid="g1")
    _scan(monkeypatch, {"1": [901]})
    _, clean = audit.run(now=now)
    assert clean["exit_code"] == audit.EXIT_OK, clean["violations"]

    from services import dashboard_campaigns_service as camp
    real = camp._build_campaign_rows

    def leaky(*a, **k):
        rows = real(*a, **k)
        for r in rows:
            r["closed_won_deals"] = None
            r["metrics"] = {"closed_won_deals": cwt.metric(
                None, cwt.WITHHELD, cwt.R_CAMPAIGN_UNPLACED)}
            r["deals"] = [{"deal_id": "1", "amount_usd": 1000.0}]
        return rows or [{"campaign_name": "x", "closed_won_deals": None,
                         "metrics": {"closed_won_deals": cwt.metric(
                             None, cwt.WITHHELD, cwt.R_CAMPAIGN_UNPLACED)},
                         "deals": [{"deal_id": "1"}]}]
    monkeypatch.setattr(camp, "_build_campaign_rows", leaky)
    _, report = audit.run(now=now)
    assert report["exit_code"] == audit.EXIT_VIOLATION
    assert report["checks"]["withheld_not_reconstructable"] is False
    assert any("dashboard_campaigns" in v for v in report["violations"])


# ═════════════════════════════════════════════════════════════════════════════
# §8 — PR review (Copilot, round 1): each case reproduced the finding first
# ═════════════════════════════════════════════════════════════════════════════

def _revenue_tab(monkeypatch, deals):
    from tests import test_pr_ads_135_dashboard_revenue as t135
    from tests.canonical_ledger_fixtures import from_legacy_deal_rows
    t135._patch_durable(monkeypatch, deal_rows=deals)
    rows = from_legacy_deal_rows(deals)
    patch_canonical_ledger(monkeypatch, rows,
                           company_associations=company_evidence(rows))
    from services.dashboard_revenue_service import build_dashboard_revenue
    return build_dashboard_revenue("ytd", now=t135.NOW)


def test_34_revenue_tab_campaign_breakdown_follows_the_placement_verdict(
        monkeypatch):
    from tests import test_pr_ads_135_dashboard_revenue as t135
    deals = [dict(d) for d in t135.DEAL_ROWS[:2]] + [dict(_UNPLACED_GOOGLE_DEAL)]
    out = _revenue_tab(monkeypatch, deals)
    rows = out["breakdowns"]["by_campaign"]
    assert rows, "rows stay, with their verdict"
    for r in rows:
        assert r["deals"] is None and r["revenue_usd"] is None
        assert r["metrics"]["closed_won_deals"]["reason"] == cwt.R_CAMPAIGN_UNPLACED
    scale = next(c for c in out["decision_cards"] if c["type"] == "scale")
    assert "withheld" in scale["headline"].lower()
    assert "won" not in scale["headline"].lower() or "withheld" in scale["headline"].lower()
    # The account total is a scope figure, not per-campaign: still published.
    assert out["kpis"]["closed_won_deals"] == 3


def test_35_a_withheld_source_revenue_keeps_its_groups_and_says_why(monkeypatch):
    from tests import test_pr_ads_135_dashboard_revenue as t135
    deals = [dict(d) for d in t135.DEAL_ROWS[:3]] + [
        dict(t135.DEAL_ROWS[0], deal_id="d8", deal_amount_usd=None)]
    out = _revenue_tab(monkeypatch, deals)
    src = out["breakdowns"]["by_source"]
    assert src, "withheld groups are kept, never an empty 'no revenue' list"
    for r in src:
        assert r["revenue_usd"] is None
        assert r["metrics"]["revenue_usd"]["status"] == cwt.WITHHELD
    js = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    panel = _js_extract(js, "function renderRevBreakdownPanel(")
    assert "windowDeals.status !== \"published\"" in panel


def test_36_roas_available_is_said_only_when_canonical_roas_publishes(
        monkeypatch):
    from services import source_attribution_service as sas
    withheld = {"metrics": {"roas": cwt.metric(None, cwt.WITHHELD,
                                               cwt.R_REVENUE_WITHHELD)}}
    status, label, reason = sas._roas_publication(
        withheld, "available", sas.STATUS_ROAS_AVAILABLE)
    assert (status, label, reason) == (cwt.WITHHELD, sas.STATUS_ROAS_WITHHELD,
                                       cwt.R_REVENUE_WITHHELD)
    published = {"metrics": {"roas": cwt.metric(2.0, cwt.PUBLISHED)}}
    assert sas._roas_publication(published, "available",
                                 sas.STATUS_ROAS_AVAILABLE)[1] == sas.STATUS_ROAS_AVAILABLE
    # Spend not ready keeps its own honest label (FX withheld, coverage …).
    assert sas._roas_publication(withheld, "unavailable_fx",
                                 sas.STATUS_FX_WITHHELD)[1] == sas.STATUS_FX_WITHHELD
    # End to end: an unpriced deal anywhere withholds the window's revenue, so
    # the Google Ads group's ROAS is withheld and never labelled available.
    from tests import test_pr_ads_135_dashboard_revenue as t135
    from tests.canonical_ledger_fixtures import from_legacy_deal_rows
    deals = [dict(d) for d in t135.DEAL_ROWS[:3]] + [
        dict(t135.DEAL_ROWS[3], deal_amount_usd=None)]
    t135._patch_durable(monkeypatch, deal_rows=deals)
    rows = from_legacy_deal_rows(deals)
    patch_canonical_ledger(monkeypatch, rows,
                           company_associations=company_evidence(rows))
    out = sas.build_revenue_by_source("ytd", now=t135.NOW)
    for g in out["groups"]:
        if g.get("roas") is None:
            assert g.get("spend_status_label") != sas.STATUS_ROAS_AVAILABLE, g["group"]
        for ch in g.get("channels") or []:
            for pf in ch.get("platforms") or []:
                if pf.get("roas") is None:
                    assert pf.get("status") != sas.STATUS_ROAS_AVAILABLE, pf


@_needs_pg
def test_37_the_audit_goes_red_on_a_per_campaign_figure_under_the_placement_gate(
        ledger, monkeypatch):
    """Counterfactual for the audit's placement rule: the Revenue tab's
    campaign breakdown without the canonical verdict (the code Copilot
    flagged) is caught."""
    now = _now()
    _deal(ledger, "1", deal_close_date=_this_year(now), acquisition_group="google_ads",
          attribution_status="attributed", gclid="g1", campaign_name_raw="Brand")
    _deal(ledger, "2", deal_close_date=_this_year(now), acquisition_group="google_ads",
          attribution_status="attributed", gclid="g2", campaign_name_raw=None)
    _scan(monkeypatch, {"1": [901], "2": [902]})
    _, clean = audit.run(now=now)
    assert clean["exit_code"] == audit.EXIT_OK, clean["violations"]
    from services import dashboard_revenue_service as rev
    monkeypatch.setattr(rev, "_attach_verdicts", lambda rows, canonical, gate=None: rows)
    _, report = audit.run(now=now)
    assert report["exit_code"] == audit.EXIT_VIOLATION
    assert any("dashboard_revenue" in v and "per-campaign placement" in v
               for v in report["violations"]), report["violations"]
