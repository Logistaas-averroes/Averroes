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
