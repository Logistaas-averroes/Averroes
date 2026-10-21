"""
tests/test_pr_ads_191_crm_truth_exceptions.py

PR-ADS-191 — the CRM Truth Exceptions workbench: which HubSpot won deals stop a
customer or revenue total from publishing, and why.

§1  customer identity rows (PG, real ledger writer, association store, sync)
§2  revenue proof rows
§3  scan-level states: stale, uncertified, population moved, corrupted
§4  unavailable evidence is never zero
§5  windows, pagination, CSV parity, the HTTP routes and access control
§6  read-only guarantee, publication regression, the audit and its
    counterfactuals, CI wiring

No production totals are hardcoded: every number comes from the fixtures here.
"""

from __future__ import annotations

import csv
import io
import json
from contextlib import redirect_stdout
from datetime import timedelta

import pytest

import tests.conftest as conftest  # noqa: F401  (import-order guard)
import analysis.closed_won_truth as cwt
import connectors.hubspot_pull as hubspot
import services.canonical_customer_revenue_service as svc
from services import crm_truth_exceptions_service as cte
from tests.canonical_ledger_fixtures import ledger_row
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: F401
    _have_postgres, pg,
)
from tests.test_pr_ads_161d2_deal_company_associations import (  # noqa: F401
    _ROOT, _deal, _fake_reads, _ok_obs, _q, _snapshot, _universe, ledger,
    NOW, Q4,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

WINDOW = "business/all_time"
QUARTER = "business/current_quarter"


def _now():
    from db import deal_company_association_repository as repo
    return repo.db_now()["now"]


def _scan(monkeypatch, mapping, fail=()):
    """A full-population association scan through the real sync service."""
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _fake_reads(monkeypatch, mapping, fail=set(fail))
    return sync_deal_company_associations(mode="incremental")


def _model(now=None):
    return cte.build_crm_truth_exceptions(now=now or _now())


def _rows(model, family=None, reason=None, deal=None):
    return [r for r in model["rows"]
            if (family is None or r["exception_family"] == family)
            and (reason is None or r["reason_code"] == reason)
            and (deal is None or r["deal_id"] == deal)]


def _window(model, wid):
    return next(w for w in model["windows"] if w["window"] == wid)


def _truth_window(now, wid):
    wtype, wkey = wid.split("/")
    return svc.get_closed_won_truth([(wtype, wkey)], now=now, resolver=None,
                                    resolver_detail="t")["windows"][0]


def _unpriced(deal_id, reason="unknown_currency", **kw):
    return dict(currency_status="unavailable", currency_reason=reason,
                revenue_usd=None, **kw)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — customer identity
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_01_a_no_company_won_deal_is_a_customer_identity_blocker(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _deal(ledger, "2")
    _scan(monkeypatch, {"1": [901], "2": []})
    now = _now()
    m = _model(now)
    rows = _rows(m, cte.FAMILY_IDENTITY, deal="2")
    assert [r["reason_code"] for r in rows] == [cwt.CUSTOMER_NO_COMPANY]
    assert rows[0]["affected_metrics"] == ["customers"]
    assert {WINDOW, QUARTER} <= set(rows[0]["affected_windows"])
    for wid in (WINDOW, QUARTER):
        pub = _truth_window(now, wid)["publication"]["customers"]
        assert (pub["status"], pub["reason"]) == (cwt.WITHHELD,
                                                  cwt.R_CUSTOMER_IDENTITY)
        assert _window(m, wid)["explanation"]["customers"] == {
            "explained": True, "by": "rows",
            "reason": cwt.R_CUSTOMER_IDENTITY}
    assert m["status"] == cte.STATUS_CERTIFIED_WITH_FINDINGS


@_needs_pg
def test_02_a_multi_company_deal_discloses_every_company_and_picks_none(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _scan(monkeypatch, {"1": [902, 901]})
    m = _model()
    [row] = _rows(m, cte.FAMILY_IDENTITY)
    assert row["reason_code"] == cwt.CUSTOMER_AMBIGUOUS
    assert row["associated_company_ids"] == ["901", "902"]
    assert row["resolved_company_id"] is None
    assert row["associated_company_names"] is None
    assert _window(m, WINDOW)["publication"]["customers"]["status"] == \
        cwt.WITHHELD


@_needs_pg
def test_06_a_successful_zero_company_lookup_is_a_proven_crm_finding(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _scan(monkeypatch, {"1": []})
    [row] = _rows(_model(), cte.FAMILY_IDENTITY)
    assert (row["reason_code"], row["latest_attempt_status"],
            row["associated_company_ids"], row["latest_failure_reason"]) == \
        (cwt.CUSTOMER_NO_COMPANY, "success", [], None)


@_needs_pg
def test_07_a_failed_lookup_is_never_read_as_no_company(ledger, monkeypatch):
    """Deal 1 never succeeded: unavailable. Deal 2 succeeded, then failed:
    its stored set stands, flagged stale. Neither is 'no company'."""
    _deal(ledger, "1")
    _deal(ledger, "2")
    _scan(monkeypatch, {"2": [902]}, fail={"1"})
    _scan(monkeypatch, {}, fail={"1", "2"})
    m = _model()
    one = _rows(m, deal="1")
    assert [r["reason_code"] for r in one] == [
        cwt.CUSTOMER_ASSOCIATION_UNAVAILABLE]
    assert one[0]["associated_company_ids"] is None
    assert one[0]["latest_failure_reason"] == "timeout"
    two = _rows(m, deal="2")
    assert [r["reason_code"] for r in two] == [cwt.STALE_LATEST_ATTEMPT_FAILED]
    assert two[0]["associated_company_ids"] == ["902"]
    assert not _rows(m, reason=cwt.CUSTOMER_NO_COMPANY)


def test_07_b_stale_evidence_names_its_cause_and_the_stale_set_is_unchanged():
    """The refactor inside the canonical owner: the stale SET the publication
    gate reads is exactly the keys of the new per-cause map."""
    evidence = {"available": True, "paths": [], "observations": [
        {**_ok_obs("1", 0, 0), "last_attempt_status": "failed"},
        {**_ok_obs("2", 0, 0),
         "deal_lastmodified_at": "2026-10-08T00:00:00+00:00"},
        _ok_obs("3", 0, 0)]}
    out = svc.company_identity_inputs(evidence)
    assert out["stale_reasons"] == {
        "1": [cwt.STALE_LATEST_ATTEMPT_FAILED],
        "2": [cwt.STALE_DEAL_MODIFIED_AFTER_OBSERVATION]}
    assert out["stale"] == {"1", "2"}


# ═════════════════════════════════════════════════════════════════════════════
# §2 — revenue proof
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_03_an_unproven_currency_is_a_revenue_blocker_with_no_usd(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _deal(ledger, "2", **_unpriced("2", deal_currency_code=None))
    _scan(monkeypatch, {"1": [901], "2": [902]})
    now = _now()
    m = _model(now)
    [row] = _rows(m, cte.FAMILY_REVENUE)
    assert (row["deal_id"], row["reason_code"], row["revenue_usd"],
            row["currency_proof_status"]) == ("2", "unknown_currency", None,
                                              cwt.CURRENCY_MISSING)
    pub = _truth_window(now, QUARTER)["publication"]["revenue_usd"]
    assert (pub["status"], pub["reason"]) == (cwt.WITHHELD,
                                              cwt.R_REVENUE_UNPROVEN)
    assert _window(m, QUARTER)["explanation"]["revenue_usd"]["explained"]


@_needs_pg
def test_04_one_deal_with_two_blockers_is_two_rows_and_one_deal(
        ledger, monkeypatch):
    _deal(ledger, "1", **_unpriced("1"))
    _deal(ledger, "2")
    _scan(monkeypatch, {"1": [], "2": [902]})
    m = _model()
    assert [r["row_id"] for r in _rows(m, deal="1")] == [
        "1:customer_identity:unresolved_no_company",
        "1:revenue_proof:unknown_currency"]
    assert m["won_deal_population"] == 2
    assert m["totals"]["deals_with_any_exception"] == 1
    assert m["totals"]["rows"] == 2


@_needs_pg
def test_05_a_resolved_priced_deal_is_no_exception_and_totals_publish(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _deal(ledger, "2")
    _scan(monkeypatch, {"1": [901], "2": [902]})
    now = _now()
    m = _model(now)
    assert m["rows"] == [] and m["status"] == cte.STATUS_CERTIFIED_NO_FINDINGS
    w = _truth_window(now, QUARTER)
    assert (w["publication"]["customers"]["status"],
            w["outcomes"]["customers"]) == (cwt.PUBLISHED, 2)
    assert w["publication"]["revenue_usd"]["status"] == cwt.PUBLISHED
    assert all(e["explained"] for e in
               _window(m, QUARTER)["explanation"].values())


@_needs_pg
def test_05_b_zero_revenue_is_proven_not_missing(ledger, monkeypatch):
    _deal(ledger, "1", amount_raw=0.0, revenue_usd=0.0)
    _scan(monkeypatch, {"1": [901]})
    assert _rows(_model(), cte.FAMILY_REVENUE) == []


# ═════════════════════════════════════════════════════════════════════════════
# §3 — scan-level states
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_08_a_stale_scan_is_reported_and_never_reads_as_zero_findings(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _deal(ledger, "2")
    _scan(monkeypatch, {"1": [901], "2": [902]})
    later = _now() + timedelta(hours=svc.association_scan_max_age_hours() + 1)
    m = _model(later)
    assert m["status"] == cte.STATUS_SOURCE_STALE
    assert [b["code"] for b in m["dataset_blockers"]] == [
        cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT]
    pub = _truth_window(later, WINDOW)["publication"]["customers"]
    assert pub["reason"] == cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT
    assert _window(m, WINDOW)["explanation"]["customers"] == {
        "explained": True, "by": "dataset",
        "reason": cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT}


@_needs_pg
def test_09_a_deal_won_after_the_complete_scan_is_uncertified(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _scan(monkeypatch, {"1": [901]})
    _deal(ledger, "2")
    m = _model()
    assert m["status"] == cte.STATUS_SCAN_NOT_CURRENT
    reasons = {r["reason_code"] for r in _rows(m, deal="2")}
    assert reasons == {cwt.CUSTOMER_ASSOCIATION_UNAVAILABLE,
                       cwt.NOT_CERTIFIED_BY_COMPLETE_SCAN}
    assert _window(m, WINDOW)["publication"]["customers"]["status"] != \
        cwt.PUBLISHED


@_needs_pg
def test_10_a_deal_leaving_the_won_population_is_disclosed_not_counted(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _deal(ledger, "2")
    _scan(monkeypatch, {"1": [901], "2": [902]})
    _deal(ledger, "2", hs_is_closed_won=False, deal_stage_id="x")
    m = _model()
    assert m["won_deal_population"] == 1
    [blocker] = m["dataset_blockers"]
    assert blocker["certified_no_longer_won_deal_ids"] == ["2"]
    assert not _rows(m, family=cte.FAMILY_IDENTITY, deal="2")
    assert m["association_scan"]["scan_current"]["reason"] == \
        "certified_set_differs_from_current_won_population"


@_needs_pg
def test_11_a_corrupted_scan_population_fails_the_audit(ledger, monkeypatch):
    from db.connection import get_conn
    from scripts import audit_crm_truth_exceptions as audit
    _deal(ledger, "1")
    _scan(monkeypatch, {"1": [901]})
    with get_conn() as c, c.cursor() as cur:
        cur.execute("UPDATE hubspot_deal_company_sync_state "
                    "SET last_complete_scan_population = 2")
    m = _model()
    assert m["status"] == cte.STATUS_SCAN_NOT_CURRENT
    _, report = audit.run(now=_now())
    assert report["exit_code"] == audit.EXIT_VIOLATION
    assert report["checks"]["deal_company_association_coverage_audit_holds"] \
        is False


# ═════════════════════════════════════════════════════════════════════════════
# §4 — unavailable is never zero
# ═════════════════════════════════════════════════════════════════════════════

def test_12_an_unreadable_ledger_is_explicitly_unavailable():
    m = cte.build_crm_truth_exceptions(
        now=NOW, universe={"available": False, "error": "boom"})
    assert (m["available"], m["status"], m["rows"], m["totals"],
            m["won_deal_population"]) == (
        False, cte.STATUS_EVIDENCE_UNAVAILABLE, None, None, None)
    assert cte.filter_rows(m) is None
    page = cte.query(m)
    assert page["rows"] is None and page["pagination"]["total"] is None


def test_12_b_unreadable_identity_evidence_is_unavailable_not_zero():
    rows = [ledger_row("1", deal_close_date=Q4),
            ledger_row("2", deal_close_date=Q4, **_unpriced("2"))]
    u = _universe(rows, [], [])
    u["company_associations"] = {"available": False, "error": "boom"}
    m = cte.build_crm_truth_exceptions(now=NOW, universe=u)
    assert m["status"] == cte.STATUS_EVIDENCE_UNAVAILABLE
    fam = m["totals"]["by_family"]
    assert (fam["customer_identity"]["rows"],
            fam["customer_identity"]["deals"]) == (None, None)
    # Revenue evidence is readable and still listed.
    assert [r["deal_id"] for r in _rows(m, cte.FAMILY_REVENUE)] == ["2"]
    assert [b["code"] for b in m["dataset_blockers"]] == [
        cwt.R_COMPANY_ASSOCIATIONS_UNREADABLE]


def test_12_c_unproven_ledger_coverage_lists_nothing_as_complete():
    rows = [ledger_row("1", deal_close_date=Q4)]
    u = _universe(rows, [], [])
    u["coverage_findings"] = [{"code": "x", "message": "not proven"}]
    m = cte.build_crm_truth_exceptions(now=NOW, universe=u)
    assert (m["status"], m["rows"]) == (cte.STATUS_EVIDENCE_UNAVAILABLE, None)
    assert m["dataset_blockers"][0]["code"] == cwt.R_COVERAGE_NOT_PROVEN


def test_12_d_the_ui_never_renders_unavailable_as_zero_issues():
    src = (_ROOT / "static/app.js").read_text(encoding="utf-8")
    body = src.split("function renderCrmTruthRows(data)")[1].split(
        "\nasync function ")[0]
    assert "data.rows === null" in body and "This is not \"0 issues\"" in body
    assert "function _cteCount(value)" in src
    assert 'return value === null || value === undefined ? "Unavailable"' \
        in src
    loader = src.split("async function loadCrmTruthExceptions()")[1].split(
        "\nasync function ")[0]
    assert "Could not load" in loader
    html = (_ROOT / "static/index.html").read_text(encoding="utf-8")
    assert 'id="crm-truth-exceptions-panel"' in html
    assert "CRM Truth Exceptions" in html
    # On System Status, which is admin-only.
    health = html.split('<section id="page-health"')[1].split("</section>")[0]
    assert 'id="crm-truth-exceptions-panel"' in health
    assert 'page === "health" && (!_currentUser || _currentUser.role !== ' \
           '"admin")' in src


# ═════════════════════════════════════════════════════════════════════════════
# §5 — windows, pagination, CSV, HTTP
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_13_affected_windows_are_the_truth_services_own_membership(
        ledger, monkeypatch):
    now = _now()
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=120)).isoformat()
    _deal(ledger, "1", deal_close_date=recent)
    _deal(ledger, "2", deal_close_date=old)
    _scan(monkeypatch, {"1": [], "2": []})
    m = _model(now)
    truth = svc.get_closed_won_truth(now=now, resolver=None,
                                     resolver_detail="t")
    for deal in ("1", "2"):
        [row] = _rows(m, deal=deal)
        expected = sorted(
            f"{w['window']['window_type']}/{w['window']['window_key']}"
            for w in truth["windows"]
            if deal in (w["membership"]["deal_ids"] or []))
        assert row["affected_windows"] == expected
    assert "evidence/7d" in _rows(m, deal="1")[0]["affected_windows"]
    assert "evidence/7d" not in _rows(m, deal="2")[0]["affected_windows"]
    # Filtering reproduces exactly the window's members with a blocker.
    page = cte.query(m, window="evidence/7d")
    assert [r["deal_id"] for r in page["rows"]] == ["1"]
    for w in m["windows"]:
        b, c = w["blocker_deals"], w["canonical_counts"]
        assert b["customer_identity_unresolved"] == \
            c["unresolved_customer_identity"]


@_needs_pg
def test_14_pagination_is_stable_with_no_gap_or_duplicate(ledger, monkeypatch):
    ids = [str(n) for n in range(1, 13)]
    for d in ids:
        _deal(ledger, d)
    _scan(monkeypatch, {d: [] for d in ids})
    m = _model()
    pages, offset = [], 0
    while True:
        page = cte.query(m, limit=5, offset=offset)
        if not page["rows"]:
            break
        assert page["pagination"]["total"] == 12
        pages.extend(r["row_id"] for r in page["rows"])
        offset += 5
    assert pages == [r["row_id"] for r in m["rows"]] and len(pages) == 12
    again = cte.query(_model(), limit=5, offset=5)
    assert [r["row_id"] for r in again["rows"]] == pages[5:10]


def _client(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    return TestClient(server.app)


_AUTH = {"Authorization": "Bearer secret-token"}


@_needs_pg
def test_15_csv_is_the_same_population_injection_safe_and_utf8(
        ledger, monkeypatch):
    _deal(ledger, "1", deal_name="=HYPERLINK(\"http://x\",\"pay\")")
    _deal(ledger, "2", deal_name="Società Ünïcode ✓")
    _deal(ledger, "3", deal_name="+cmd|' /C calc'!A0")
    _scan(monkeypatch, {"1": [], "2": [901, 902], "3": []})
    client = _client(monkeypatch)
    for params in ({}, {"family": "customer_identity"},
                   {"reason": "unresolved_no_company"},
                   {"window_type": "business", "window_key": "all_time"}):
        js = client.get("/api/audit/crm-truth-exceptions",
                        params={**params, "limit": 1000}, headers=_AUTH)
        cs = client.get("/api/audit/crm-truth-exceptions.csv",
                        params=params, headers=_AUTH)
        assert js.status_code == cs.status_code == 200
        assert cs.headers["content-type"].startswith("text/csv")
        text = cs.content.decode("utf-8")
        parsed = list(csv.DictReader(io.StringIO(text)))
        assert [r["row_id"] for r in parsed] == \
            [r["row_id"] for r in js.json()["rows"]]
        assert tuple(csv.DictReader(io.StringIO(text)).fieldnames) == \
            cte.CSV_COLUMNS
    names = {r["deal_id"]: r["deal_name"] for r in parsed}
    assert names["1"].startswith("'=")
    assert names["3"].startswith("'+")
    assert names["2"] == "Società Ünïcode ✓"
    assert cte._cell("-1+2") == "'-1+2" and cte._cell("@SUM(A1)") == \
        "'@SUM(A1)"


def test_16_access_is_admin_only(monkeypatch):
    client = _client(monkeypatch)
    for path in ("/api/audit/crm-truth-exceptions",
                 "/api/audit/crm-truth-exceptions.csv"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={
            "Authorization": "Bearer wrong"}).status_code == 401


def test_16_b_unknown_filters_are_4xx_never_an_empty_list(monkeypatch):
    model = cte.build_crm_truth_exceptions(
        now=NOW, universe=_universe([ledger_row("1", deal_close_date=Q4)],
                                    [_ok_obs("1", 0, 0)], []))
    monkeypatch.setattr(cte, "build_crm_truth_exceptions", lambda: model)
    client = _client(monkeypatch)
    for params in ({"window_type": "business", "window_key": "nope"},
                   {"window_type": "business"},
                   {"family": "nope"}, {"reason": "nope"},
                   {"family": "revenue_proof",
                    "reason": "unresolved_no_company"},
                   {"limit": 0}, {"limit": 100000}, {"offset": -1}):
        for path in ("/api/audit/crm-truth-exceptions",
                     "/api/audit/crm-truth-exceptions.csv"):
            if path.endswith(".csv") and ("limit" in params
                                          or "offset" in params):
                continue
            resp = client.get(path, params=params, headers=_AUTH)
            assert resp.status_code == 400, (path, params, resp.text)


def test_16_c_unavailable_evidence_is_explicit_on_both_routes(monkeypatch):
    model = cte.build_crm_truth_exceptions(
        now=NOW, universe={"available": False, "error": "boom"})
    monkeypatch.setattr(cte, "build_crm_truth_exceptions", lambda: model)
    client = _client(monkeypatch)
    js = client.get("/api/audit/crm-truth-exceptions", headers=_AUTH)
    assert js.status_code == 200
    body = js.json()
    assert (body["available"], body["status"], body["rows"],
            body["pagination"]["total"]) == (
        False, cte.STATUS_EVIDENCE_UNAVAILABLE, None, None)
    # An empty CSV would read as "no blockers": refuse instead.
    assert client.get("/api/audit/crm-truth-exceptions.csv",
                      headers=_AUTH).status_code == 503


# ═════════════════════════════════════════════════════════════════════════════
# §6 — read-only, publication regression, the audit
# ═════════════════════════════════════════════════════════════════════════════

@_needs_pg
def test_17_reads_and_the_audit_write_nothing_and_call_nothing(
        ledger, monkeypatch):
    from scripts import audit_crm_truth_exceptions as audit
    _deal(ledger, "1")
    _deal(ledger, "2", **_unpriced("2"))
    _scan(monkeypatch, {"1": [], "2": [901, 902]})

    def forbidden(*_a, **_k):
        raise AssertionError("an external call was made")

    for name in dir(hubspot):
        if name.startswith(("fetch_", "get_", "pull_")) and callable(
                getattr(hubspot, name)):
            monkeypatch.setattr(hubspot, name, forbidden)
    before = _snapshot()
    _model()
    client = _client(monkeypatch)
    assert client.get("/api/audit/crm-truth-exceptions",
                      headers=_AUTH).status_code == 200
    assert client.get("/api/audit/crm-truth-exceptions.csv",
                      headers=_AUTH).status_code == 200
    audit.run(now=_now())
    assert _snapshot() == before, "a read changed the database"
    a = audit.Audit()
    audit.structural_checks(a)
    assert a.violations == []


@_needs_pg
def test_18_publication_is_unchanged_and_the_audit_passes_with_findings(
        ledger, monkeypatch):
    """CRM findings exist, totals are withheld exactly as the truth service
    says, and the audit exits 0: the evidence is represented truthfully."""
    from scripts import audit_crm_truth_exceptions as audit
    _deal(ledger, "1")
    _deal(ledger, "2", **_unpriced("2"))
    _deal(ledger, "3")
    _scan(monkeypatch, {"1": [901], "2": [902], "3": [903, 904]})
    now = _now()
    m = _model(now)
    truth = svc.get_closed_won_truth(now=now, resolver=None,
                                     resolver_detail="t")
    for w in truth["windows"]:
        wid = f"{w['window']['window_type']}/{w['window']['window_key']}"
        mine = _window(m, wid)["publication"]
        for metric in ("closed_won_deals", "customers", "revenue_usd"):
            assert mine[metric] == w["publication"][metric]
    code_buf = io.StringIO()
    with redirect_stdout(code_buf):
        code = audit.main(["--json"])
    report = json.loads(code_buf.getvalue())
    assert code == 0, report["violations"]
    assert report["verdict"] == "represents_the_evidence_truthfully"
    assert report["totals"]["by_family"]["customer_identity"]["deals"] == 1
    assert report["totals"]["by_family"]["revenue_proof"]["deals"] == 1
    assert report["embedded_audits"]["customer_closed_won_truth"] == 0


@_needs_pg
def test_19_the_audit_catches_a_blocker_lost_by_the_workbench(
        ledger, monkeypatch):
    """Implementation mutation: revenue reasons vanish. The withheld revenue
    verdict is no longer explained, the counts disagree with the truth service,
    and PR-ADS-155's report names a deal the workbench lost."""
    from scripts import audit_crm_truth_exceptions as audit
    _deal(ledger, "1")
    _deal(ledger, "2", **_unpriced("2"))
    _scan(monkeypatch, {"1": [901], "2": [902]})
    assert audit.run(now=_now(), with_embedded=False)[1]["exit_code"] == 0
    monkeypatch.setattr(cte, "_revenue_row_reasons", lambda row: [])
    _, report = audit.run(now=_now(), with_embedded=False)
    assert report["exit_code"] == audit.EXIT_VIOLATION
    for check in ("withheld_verdict_is_explained_and_published_total_is_clean",
                  "window_counts_reconcile_with_truth_service",
                  "revenue_rows_contain_every_missing_amount_deal"):
        assert report["checks"][check] is False, check


@_needs_pg
def test_19_b_the_audit_catches_identity_rows_that_disagree(
        ledger, monkeypatch):
    """Output mutation: the workbench reports an ambiguous deal as no-company
    (the classifier module is shared with the truth service, so the mutation is
    applied to the workbench's own output). The coverage audit's independent
    derivation from the raw association rows refuses it."""
    from scripts import audit_crm_truth_exceptions as audit
    _deal(ledger, "1")
    _scan(monkeypatch, {"1": [901, 902]})
    assert audit.run(now=_now(), with_embedded=False)[1]["exit_code"] == 0
    real = cte.build_crm_truth_exceptions

    def misreads(**kw):
        model = real(**kw)
        for r in model["rows"] or []:
            if r["reason_code"] == cwt.CUSTOMER_AMBIGUOUS:
                r["reason_code"] = cwt.CUSTOMER_NO_COMPANY
                r["row_id"] = f"{r['deal_id']}:customer_identity:" \
                              f"{cwt.CUSTOMER_NO_COMPANY}"
        return model

    monkeypatch.setattr(cte, "build_crm_truth_exceptions", misreads)
    _, report = audit.run(now=_now(), with_embedded=False)
    assert report["exit_code"] == audit.EXIT_VIOLATION
    assert report["checks"]["identity_rows_match_coverage_audit"] is False


def test_20_the_workbench_republishes_no_total():
    rows = [ledger_row("1", deal_close_date=Q4)]
    m = cte.build_crm_truth_exceptions(
        now=NOW, universe=_universe(rows, [_ok_obs("1", 1, 1)],
                                    [{"deal_id": "1", "company_id": "901"}]))
    for w in m["windows"]:
        assert not {"outcomes", "attribution", "membership"} & set(w)
    flat = json.dumps({k: v for k, v in m.items() if k != "rows"})
    assert '"customers": 1' not in flat and "revenue_usd\": 1000" not in flat


def test_21_rows_use_only_canonical_reason_codes():
    vocab = cte.reason_vocabulary()
    canonical = set(cwt.CUSTOMER_IDENTITY_STATES) | set(
        cwt.STALE_EVIDENCE_REASONS) | {cwt.NOT_CERTIFIED_BY_COMPLETE_SCAN} | \
        set(cwt.AMOUNT_STATES) | set(cwt.CLOSE_DATE_STATES) | set(
        cwt.WON_DEFINITION_STATES)
    from analysis import deal_currency as dc
    canonical |= {getattr(dc, n) for n in dir(dc) if n.startswith("REASON_")}
    canonical.add("currency_reason_not_recorded")   # PR-ADS-155's constant
    for family, reasons in vocab.items():
        assert set(reasons) <= canonical, family
        for r in reasons:
            assert cte.EXPLANATIONS.get(r), r


def test_22_ci_runs_this_suite_in_the_postgresql_step_and_asserts_it_ran():
    wf = (_ROOT / ".github/workflows/pr-ads-153d-checks.yml").read_text(
        encoding="utf-8")
    assert "tests/test_pr_ads_191_crm_truth_exceptions.py" in wf
    assert '"tests.test_pr_ads_191_crm_truth_exceptions"' in wf
