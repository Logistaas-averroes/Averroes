"""PR-ADS-166A — the Leads page: contact names, and what "Customers" means.

Owner report: "it says 208 customers for last quarter". Production, Q3 2026:
about 219 contacts ENTERED the HubSpot Customer lifecycle stage (206 with an
OFFLINE original source, 182 created before the quarter — CRM housekeeping)
while 8 deals were closed-won. The page's figure was correct and its word was
wrong: a lifecycle stage move is not a customer won.

What these cases hold:

* the stage-move count is named for what it is, everywhere on the page;
* "Customers won" is a separate figure from the closed-won reader contract —
  distinct companies, ``{value, status, reason}`` — never derived from the
  stage-move count, never a dash or a 0 when it is not published;
* a contact row carries the contact's own HubSpot name, read by the existing
  contact sync, kept through a sparse payload, and reported as missing when it
  is missing — never an email, an id or the company in its place.

The UI cases evaluate the real functions from ``static/app.js`` in ``node``
(the repository has no JS harness). The database cases run the real schema,
contact sync, funnel writer and readers on a throwaway PostgreSQL cluster.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from analysis import closed_won_truth as cwt
from connectors import hubspot_pull as hp
from tests import test_pr_ads_163c_removed_contacts as m163c
from tests.canonical_ledger_fixtures import (
    company_evidence, fresh_sync_state, ledger_row, patch_canonical_ledger,
)
# The module-scoped cluster and the per-test database of PR-ADS-163C.
from tests.test_pr_ads_163c_removed_contacts import (  # noqa: F401
    _cluster, db, needs_pg,
)

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
SERVER_PY = (ROOT / "api" / "server.py").read_text(encoding="utf-8")

#: The day the owner reported it. "last_quarter" is Q3 2026 (1 Jul – 30 Sep).
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
WINDOW = "last_quarter"

STAGE_MOVES = 219          # contacts moved to the Customer stage in Q3 2026
WON_DEALS = 8              # deals closed-won in Q3 2026
WON_COMPANIES = 7          # two of the eight deals belong to one company


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures: the production-shaped quarter
# ═════════════════════════════════════════════════════════════════════════════
def _won_rows():
    return [ledger_row(f"d{i}", deal_close_date=f"2026-08-{10 + i:02d}T09:00:00+00:00")
            for i in range(WON_DEALS)]


def _ledger(monkeypatch, *, companies=True, sync_state=None, available=True,
            scan_age_hours=2):
    """The deal ledger of Q3 2026: eight won deals, seven companies."""
    rows = _won_rows()
    evidence = None
    if companies:
        evidence = company_evidence(
            rows, companies={"d0": ["co-shared"], "d1": ["co-shared"],
                             **(companies if isinstance(companies, dict) else {})},
            scan_started_at=(NOW - timedelta(hours=scan_age_hours)).isoformat())
    patch_canonical_ledger(
        monkeypatch, rows, available=available,
        sync_state=sync_state or fresh_sync_state(NOW),
        company_associations=evidence)
    return rows


def _won(monkeypatch=None, **kwargs):
    from services import canonical_crm_funnel_service as funnel
    return funnel.build_customers_won("business", WINDOW, now=NOW, **kwargs)


def _funnel_payload(count=STAGE_MOVES, window_key=WINDOW):
    """The slice of ``/api/crm-funnel`` the customer figures read."""
    return {"available": True, "window": {"window_key": window_key},
            "reconciliation": {"status": "ok"},
            "events": {"customer": {
                "count": count, "metric_family": "lifecycle_stage_events",
                "window_basis": "date_entered_customer"}}}


# ═════════════════════════════════════════════════════════════════════════════
# The real UI functions, evaluated in node
# ═════════════════════════════════════════════════════════════════════════════
def _js_block(source: str, head: str) -> str:
    """``head`` through the end of its first balanced ``{…}`` / ``[…]``."""
    i = source.index(head)
    opener = min((source.index(c, i) for c in "{[" if c in source[i:]))
    pairs = {"{": "}", "[": "]"}
    stack = []
    for k in range(opener, len(source)):
        ch = source[k]
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                end = k + 1
                return source[i:end + (source[end:end + 1] == ";")]
    raise AssertionError(head)


def _js_definition(source: str, name: str) -> str:
    """The page's own definition of ``name``: a function, or a top-level
    ``const`` (an object / array literal, or a string up to its ``";``)."""
    fn = re.search(rf"^(?:async )?function {re.escape(name)}\(", source, re.M)
    if fn:
        return _js_block(source, source[fn.start():fn.end()])
    const = re.search(rf"^(const|let) {re.escape(name)} =\s*(.)", source, re.M)
    assert const, f"app.js defines no top-level {name}"
    kw = const.group(1)
    if const.group(2) in "{[":
        return _js_block(source, f"{kw} {name} =")
    return re.search(rf"^{kw} {re.escape(name)} =[\s\S]*?[\"\d\]le];", source,
                     re.M).group(0)


def _node(calls: dict, *, role=None, source: str | None = None) -> dict:
    """Run the page's own functions. ``calls`` maps a key to [fn, args].

    Only the functions named in ``calls`` are asked for; everything they
    reference is pulled from ``app.js`` as node reports it missing, so the
    code under test is always the page's, never a copy kept here.
    """
    node = shutil.which("node")
    assert node, "node is a CI dependency (node --check static/app.js)"
    src = APP_JS if source is None else source
    names = list(dict.fromkeys(["LEADS_STAGES"] + [c[0] for c in calls.values()]))
    user = "null" if role is None else json.dumps({"role": role})
    for _ in range(80):
        entries = ", ".join(dict.fromkeys(c[0] for c in calls.values()))
        script = (f"let _currentUser = {user};\n"
                  + "\n".join(_js_definition(src, n) for n in reversed(names))
                  + f"\nconst fns = {{ {entries} }};\n"
                  f"const calls = {json.dumps(calls)};\n"
                  "const out = { __stages: LEADS_STAGES };\n"
                  "for (const [k, [fn, args]] of Object.entries(calls)) {\n"
                  "  out[k] = fns[fn](...args);\n"
                  "}\n"
                  "console.log(JSON.stringify(out));\n")
        res = subprocess.run([node, "-e", script], capture_output=True, text=True)
        if res.returncode == 0:
            return json.loads(res.stdout)
        missing = re.search(r"ReferenceError: (\w+) is not defined", res.stderr)
        if missing and missing.group(1) not in names:
            names.append(missing.group(1))
            continue
        before = re.search(r"ReferenceError: Cannot access '(\w+)' before", res.stderr)
        if before and names[-1] != before.group(1):
            names.remove(before.group(1))
            names.append(before.group(1))
            continue
        raise AssertionError(res.stderr)
    raise AssertionError("could not resolve the page's definitions")


def _text(markup: str) -> str:
    """What a person reads: tags and attributes removed, whitespace folded."""
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _figure(markup: str, which: str) -> str:
    """The visible text of ONE of the two figures (PR-ADS-178B: on the
    Pipeline page — the funnel's Customer-stage cell, and the Customers card
    with the Customers won section)."""
    marker = {"stage_moves": "<!--stage_moves-->",
              "customers_won": "<!--customers_won-->"}[which]
    return _text(markup.split(marker)[1].split("<!--end-->")[0])


def _pipeline_from(funnel, won):
    """PR-ADS-178B — the Pipeline builder's payload for one window, shaped
    from the funnel slice and the customers-won verdicts the builder
    republishes (``pipeline_service._customers_won``: the list only beside a
    published count; withheld = no list, with the reason)."""
    won = won or {}

    def verdict(key):
        m = won.get(key)
        if isinstance(m, dict) and m.get("status") in (
                "published", "withheld", "unavailable", "not_published"):
            return m
        return {"value": None, "status": "unavailable",
                "reason": "closed_won_verdict_missing"}
    cust, deals = verdict("customers"), verdict("closed_won_deals")
    if cust["status"] == "published" and "won_customers" in won:
        cw = {"status": "published", "reason": None, "rows": won["won_customers"]}
    elif cust["status"] == "published":
        cw = {"status": "withheld",
              "reason": won.get("won_customers_omitted") or "closed_won_rows_do_not_match_count"}
    else:
        cw = {"status": cust["status"], "reason": cust.get("reason")}
    return {"window": {"key": WINDOW}, "cohort": None, "funnel": funnel,
            "kpis": {"customers": cust, "closed_won_deals": deals},
            "customers_won": cw}


def _figures_calls(funnel, won, key="p"):
    """The renderers that draw the two figures on the Pipeline page."""
    payload = _pipeline_from(funnel, won)
    return {f"{key}__moves": ["pipelineFunnelHtml", [payload]],
            f"{key}__cards": ["pipelineKpiCardsHtml", [payload, None]],
            f"{key}__list": ["pipelineCustomersWonHtml", [payload]]}


def _figures_markup(out: dict, key="p") -> str:
    """One markup holding the two figures, each between markers: the funnel's
    Customer-stage cell, and the Customers card + the Customers won section."""
    moves = out[f"{key}__moves"]
    cell = moves[moves.index('data-funnel-event="customer"'):]
    cell = cell[:cell.index("</button>")]
    cards = out[f"{key}__cards"]
    card = cards[cards.index('data-kpi="customers"'):]
    card = card[:card.index('data-kpi="revenue"')]
    return ("<!--stage_moves--><x " + cell + "<!--end-->"
            "<!--customers_won--><x " + card + out[f"{key}__list"] + "<!--end-->")


def _figures(funnel, won, *, role=None) -> str:
    return _figures_markup(_node(_figures_calls(funnel, won), role=role))


# ═════════════════════════════════════════════════════════════════════════════
# A — the misleading number
# ═════════════════════════════════════════════════════════════════════════════
def test_01_the_customer_stage_is_named_as_a_stage_move_on_the_strip_tab_and_table():
    stages = {s["event"]: s for s in _node({})["__stages"]}
    assert stages["customer"]["label"] == "Moved to Customer stage in HubSpot"
    # PR-ADS-178B: the stages are the Pipeline funnel's (no tabs); each also
    # names its stage for the list header ("Customer").
    assert stages["customer"]["stage"] == "Customer"
    # No funnel stage is called plain "Customers" any more, on any surface.
    for s in stages.values():
        assert s["label"].strip().lower() != "customers", s
        assert s["stage"].strip().lower() != "customers", s
        assert "lifecycle customers" not in s["label"].lower()


def test_02_the_production_quarter_shows_219_stage_moves_and_customers_won_separately(monkeypatch):
    """219 contacts moved to the Customer stage; 8 deals won by 7 companies."""
    _ledger(monkeypatch)
    won = _won()
    assert won["customers"] == {"value": WON_COMPANIES, "status": "published",
                                "reason": None,
                                "dedup_key": "hubspot_company_id"}
    assert won["closed_won_deals"] == {"value": WON_DEALS,
                                       "status": "published", "reason": None}

    # PR-ADS-178B: on the Pipeline page — the funnel's Customer-stage cell,
    # and the Customers card beside the Customers won list.
    markup = _figures(_funnel_payload(), won)
    moves, customers = _figure(markup, "stage_moves"), _figure(markup, "customers_won")
    assert moves == (
        "Moved to Customer stage in HubSpot 219 — Counts contacts whose HubSpot "
        "lifecycle stage changed to Customer in this period, including old "
        "contacts updated by the team. It is not new customers.")
    assert customers.startswith(
        "Customers 7 Distinct HubSpot companies · never summed HubSpot companies "
        "on closed-won deals Customers won Distinct companies with a closed-won "
        "deal closed in this period. Not the same as contacts moved to the "
        "Customer stage. Customer Closed-won deals Latest close date ")
    # Neither figure carries the other's number.
    assert "219" not in customers and "7" not in re.sub(r"\D219\D", " ", moves)


def test_03_customers_won_is_distinct_companies_never_the_deal_count_or_the_stage_moves(monkeypatch):
    _ledger(monkeypatch)
    won = _won()
    assert won["customers"]["value"] == WON_COMPANIES != WON_DEALS
    assert won["customers"]["value"] != STAGE_MOVES
    # The reader takes no stage-move input and reads nothing from the funnel:
    # no repository, no contact row, no population, no call to `build`.
    import inspect
    from services import canonical_crm_funnel_service as svc
    assert set(inspect.signature(svc.build_customers_won).parameters) == {
        "window_type", "window_key", "scope", "acquisition_group", "now"}
    code = "".join(inspect.getsource(fn) for fn in (
        svc.build_customers_won, svc._build_customers_won, svc._cw_metric,
        svc._cw_envelope, svc._cw_unavailable))
    for forbidden in ("repo.", "crm_funnel_repository", "build_populations",
                      "fetch_funnel", "date_entered", "EVENT_", "counts[",
                      " build(", "contacts("):
        assert forbidden not in code, forbidden
    assert "load_won_deals(" in code and "single_snapshot" in code


def test_04_both_figures_say_which_metric_family_and_window_basis_they_are(monkeypatch):
    _ledger(monkeypatch)
    won = _won()
    assert won["metric_family"] == "closed_won_customers"
    assert won["window_basis"] == "deal_close_date"
    assert won["dedup_key"] == "hubspot_company_id"
    assert won["cohort"] == "closed_won_close_date"
    assert won["window"]["window_key"] == WINDOW
    assert won["window"]["start"].startswith("2026-07-01")
    assert won["window"]["end_exclusive"].startswith("2026-10-01")
    # The stage-move side is asserted against the real funnel in test_23.
    from services import canonical_crm_funnel_service as funnel
    assert funnel.METRIC_FAMILY_STAGE_EVENTS == "lifecycle_stage_events"
    assert funnel.METRIC_FAMILY_STAGE_EVENTS != won["metric_family"]


def test_05_withheld_customers_render_the_word_and_the_reason_never_a_number(monkeypatch):
    """Eight won deals, one of which HubSpot associates with no company."""
    _ledger(monkeypatch, companies={"d3": []})
    won = _won()
    assert won["customers"]["value"] is None
    assert won["customers"]["status"] == cwt.WITHHELD
    assert won["customers"]["reason"] == cwt.R_CUSTOMER_IDENTITY
    # The deal count is a different metric with its own verdict.
    assert won["closed_won_deals"] == {"value": WON_DEALS,
                                       "status": "published", "reason": None}

    markup = _figures(_funnel_payload(), won)
    customers = _figure(markup, "customers_won")
    # PR-ADS-178B: the Customers card and the Customers won section.
    assert customers == (
        "Customers Withheld a won deal has no company, or several Distinct "
        "HubSpot companies · never summed HubSpot companies on closed-won deals "
        "Customers won Distinct companies with a closed-won deal closed in this "
        "period. Not the same as contacts moved to the Customer stage. Withheld "
        "a won deal has no company, or several — no list of customers is shown.")
    value_cell = _text(markup.split('class="dash-kpi-card__value">')[1]
                       .split("dash-kpi-card__sub")[0])
    assert not re.search(r"\d", value_cell), value_cell
    assert "—" not in value_cell
    # The stage-move figure is untouched by the withholding.
    assert _figure(markup, "stage_moves").startswith(
        "Moved to Customer stage in HubSpot 219 ")


def test_06_an_unreadable_or_stale_ledger_is_unavailable_with_its_reason(monkeypatch):
    _ledger(monkeypatch, available=False)
    unreadable = _won()
    assert unreadable["available"] is False
    for key in ("customers", "closed_won_deals"):
        assert unreadable[key]["value"] is None
        assert unreadable[key]["status"] == cwt.UNAVAILABLE
        assert unreadable[key]["reason"] == "canonical_ledger_unreadable"

    _ledger(monkeypatch, sync_state=fresh_sync_state(NOW, hours_ago=72))
    stale = _won()
    for key in ("customers", "closed_won_deals"):
        assert stale[key]["value"] is None
        assert stale[key]["status"] == cwt.UNAVAILABLE
        assert stale[key]["reason"] == cwt.R_LEDGER_STALE

    # Company associations unreadable: the deals are known, the customers
    # are not — and that is "unavailable", not seven and not eight.
    _ledger(monkeypatch, companies=False)
    no_companies = _won()
    assert no_companies["customers"]["value"] is None
    assert no_companies["customers"]["status"] == cwt.UNAVAILABLE
    assert no_companies["customers"]["reason"] == cwt.R_COMPANY_ASSOCIATIONS_UNREADABLE
    assert no_companies["closed_won_deals"]["value"] == WON_DEALS

    out = _node({**_figures_calls(_funnel_payload(), unreadable, "u"),
                 **_figures_calls(_funnel_payload(), stale, "s")})
    out = {k: _figures_markup(out, k) for k in ("u", "s")}
    assert _figure(out["u"], "customers_won").startswith(
        "Customers Unavailable the deal ledger could not be read ")
    assert _figure(out["s"], "customers_won").startswith(
        "Customers Unavailable the deal sync is overdue")
    for markup in out.values():
        assert not re.search(r"\d", _figure(markup, "customers_won"))


def test_07_a_scope_or_source_filter_gets_no_customers_won_figure_and_no_ledger_read(monkeypatch):
    """Scope and Source classify CONTACTS. Showing the all-source customers
    beside a filtered funnel would be the same mistake in a new place."""
    from services import canonical_revenue_service as crs
    from services import canonical_crm_funnel_service as svc

    def boom(*a, **k):
        raise AssertionError("the ledger must not be read for a filtered page")
    monkeypatch.setattr(crs, "load_won_deals", boom)

    for kwargs in ({"scope": "google_ads_source"},
                   {"acquisition_group": "offline"},
                   {"scope": "keyword_attributable", "acquisition_group": "google_ads"}):
        out = svc.build_customers_won("business", WINDOW, now=NOW, **kwargs)
        assert out["available"] is False
        for key in ("customers", "closed_won_deals"):
            assert out[key]["value"] is None
            assert out[key]["status"] == "unavailable"
            assert out[key]["reason"] == svc.REASON_FILTER_NOT_APPLICABLE
        assert out["metric_family"] == "closed_won_customers"

    # PR-ADS-178B: on the Pipeline page a lead filter makes the Customers won
    # LIST unavailable with this reason (pipeline_service._customers_won);
    # the Customers card is a deal figure and is never blanked by it.
    markup = _figures(_funnel_payload(count=206), out)
    assert ("Customers won Distinct companies with a closed-won deal closed in "
            "this period. Not the same as contacts moved to the Customer stage. "
            "Unavailable customers won is not split by contact source — clear "
            "the Scope and Source filters") in _figure(markup, "customers_won")


def test_08_a_window_the_reader_contract_cannot_express_is_refused_not_approximated(monkeypatch):
    from services import canonical_revenue_service as crs
    from services import canonical_crm_funnel_service as svc

    monkeypatch.setattr(crs, "load_won_deals", lambda *a, **k: pytest.fail(
        "an evidence window must not be re-expressed as a business window"))
    out = svc.build_customers_won("evidence", "90d", now=NOW)
    assert out["customers"]["status"] == "unavailable"
    assert out["customers"]["value"] is None
    assert out["customers"]["reason"] == svc.REASON_WINDOW_NOT_EXPRESSIBLE
    assert out["window"] is None

    with pytest.raises(ValueError):
        svc.build_customers_won("business", "not_a_window", now=NOW)


def test_09_a_reader_that_sends_no_usable_verdict_fails_closed(monkeypatch):
    from services import canonical_revenue_service as crs
    from services import canonical_crm_funnel_service as svc

    for base in (
        {"available": True, "deals": [{"deal_id": "1"}] * 8},          # no verdict
        {"available": True, "canonical_metrics": {
            "customers": {"value": 8, "status": "certainly"},          # unknown status
            "closed_won_deals": {"value": None, "status": "published"}}},
    ):
        monkeypatch.setattr(crs, "load_won_deals", lambda *a, _b=base, **k: _b)
        out = svc.build_customers_won("business", WINDOW, now=NOW)
        for key in ("customers", "closed_won_deals"):
            assert out[key]["value"] is None, (key, out[key])
            assert out[key]["status"] == "unavailable"
            assert out[key]["reason"] == svc.REASON_VERDICT_MISSING

    # A withheld metric that still carried a number loses the number.
    monkeypatch.setattr(crs, "load_won_deals", lambda *a, **k: {
        "available": True, "canonical_metrics": {
            "customers": {"value": 8, "status": "withheld",
                          "reason": cwt.R_CUSTOMER_IDENTITY},
            "closed_won_deals": {"value": 8, "status": "published"}}})
    out = svc.build_customers_won("business", WINDOW, now=NOW)
    assert out["customers"] == {"value": None, "status": "withheld",
                                "reason": cwt.R_CUSTOMER_IDENTITY,
                                "dedup_key": "hubspot_company_id"}


def test_10_the_page_never_falls_back_to_the_stage_moves_when_customers_won_is_missing():
    """PR-ADS-178B: the Pipeline page reads ONE response, so the old
    cross-response cases (no customers-won payload, another metric family,
    another window) cannot arise; what remains is the page's own fall-backs:
    no verdict, a published zero, an unknown stage-move count."""
    published = {"metric_family": "closed_won_customers",
                 "requested": {"window_key": WINDOW},
                 "customers": {"value": 7, "status": "published", "reason": None},
                 "closed_won_deals": {"value": 8, "status": "published", "reason": None}}
    raw = _node({
        **_figures_calls(_funnel_payload(), {
            "metric_family": "closed_won_customers",
            "requested": {"window_key": WINDOW}, "customers": {"value": 7}}, "no_verdict"),
        **_figures_calls(_funnel_payload(), None, "no_payload"),
        **_figures_calls(_funnel_payload(count=0), {
            **published,
            "customers": {"value": 0, "status": "published", "reason": None},
            "closed_won_deals": {"value": 0, "status": "published", "reason": None},
            "won_customers": []}, "zero"),
        **_figures_calls({"available": False, "window": {"window_key": WINDOW}},
                         published, "moves_unknown"),
    })
    out = {k: _figures_markup(raw, k) for k in ("no_verdict", "no_payload", "zero",
                                                 "moves_unknown")}
    for key in ("no_verdict", "no_payload"):
        customers = _figure(out[key], "customers_won")
        assert customers.startswith("Customers Unavailable "), (key, customers)
        assert not re.search(r"\d", customers), (key, customers)
        assert "219" in _figure(out[key], "stage_moves")
    # A published zero is "0" on both sides, not a word and not a dash.
    assert _figure(out["zero"], "stage_moves").startswith(
        "Moved to Customer stage in HubSpot 0 ")
    assert _figure(out["zero"], "customers_won").startswith("Customers 0 ")
    assert "No customer was won in this period." in _figure(out["zero"], "customers_won")
    assert "<table" not in out["zero"]            # nothing to list
    # An unknown stage-move count is a word; customers won is unaffected.
    assert _figure(out["moves_unknown"], "stage_moves").startswith(
        "Moved to Customer stage in HubSpot Unavailable ")
    assert _figure(out["moves_unknown"], "customers_won").startswith("Customers 7 ")


def test_11_only_an_admin_gets_the_view_blockers_link_from_the_leads_figure(monkeypatch):
    _ledger(monkeypatch, companies={"d3": []})
    won = _won()
    assert won["customers"]["reason"] == cwt.R_CUSTOMER_IDENTITY   # a CRM finding
    # PR-ADS-178B: the Pipeline page's Customers card and section.
    viewer = _figures(_funnel_payload(), won, role="viewer")
    admin = _figures(_funnel_payload(), won, role="admin")
    assert "View blockers" not in viewer
    assert "View blockers" in admin
    assert "cte_window=business%2Flast_quarter" in admin
    assert "cte_family=customer_identity" in admin


def test_12_the_new_reader_is_inventoried_and_the_audit_fails_without_its_row(monkeypatch):
    from scripts import audit_customer_revenue_reader_cutover as audit

    def inventory_ok():
        a = audit.Audit()
        audit.structural_checks(a)
        return [v for v in a.violations if v.startswith("inventory_complete")]

    assert inventory_ok() == []
    rows = [r for r in audit.INVENTORY
            if r["consumer"] == "services/canonical_crm_funnel_service.py"]
    assert len(rows) == 1 and rows[0]["canonical_via"] == "load_won_deals"
    monkeypatch.setattr(audit, "INVENTORY", tuple(
        r for r in audit.INVENTORY if r not in rows))
    failures = inventory_ok()
    assert failures and "canonical_crm_funnel_service" in failures[0]


def test_13_the_customers_won_route_is_gated_like_the_contact_rows_it_sits_beside():
    def route(path):
        block = SERVER_PY.split(f'@app.get("{path}")')[1].split("\n@app.")[0]
        return block
    for path in ("/api/crm-funnel/customers-won", "/api/crm-funnel/contacts"):
        assert "user: dict = Depends(require_auth)" in route(path), path

    from fastapi.testclient import TestClient
    from api.server import app
    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/api/crm-funnel/customers-won", "/api/crm-funnel/contacts"):
        assert client.get(path).status_code == 401, path


# ═════════════════════════════════════════════════════════════════════════════
# B — names
# ═════════════════════════════════════════════════════════════════════════════
def test_14_the_contact_read_asks_for_first_and_last_name_and_still_never_email():
    assert "firstname" in hp.CONTACT_FUNNEL_PROPERTIES
    assert "lastname" in hp.CONTACT_FUNNEL_PROPERTIES
    assert "email" not in hp.CONTACT_FUNNEL_PROPERTIES
    row = hp.normalize_contact_funnel_row({"id": "1", "properties": {
        "firstname": "  Lina ", "lastname": "Haddad", "company": "Acme Freight",
        "email": "lina@acme.example"}})
    assert (row["first_name"], row["last_name"]) == ("Lina", "Haddad")
    assert "email" not in row
    assert "lina@acme.example" not in json.dumps(row, default=str)
    blank = hp.normalize_contact_funnel_row({"id": "2", "properties": {
        "firstname": "   ", "company": "Acme Freight"}})
    assert blank["first_name"] is None and blank["last_name"] is None


def test_15_a_missing_name_is_reported_missing_never_filled_from_anything_else():
    from services import canonical_crm_funnel_service as funnel

    def payload(**row):
        base = {"contact_id": "9000123", "company": "Acme Freight",
                "lifecycle_stage": "customer"}
        return funnel._contact_row_payload({**base, **row}, "customer", True)

    named = payload(first_name="Lina", last_name="Haddad")
    assert named["contact_name"] == "Lina Haddad"
    assert named["contact_name_status"] == "recorded"
    assert payload(first_name="Lina")["contact_name"] == "Lina"
    assert payload(last_name="Haddad")["contact_name"] == "Haddad"

    for row in ({}, {"first_name": None, "last_name": ""},
                {"first_name": "  ", "last_name": " "}):
        # HubSpot was asked (names_read_at is set) and holds nothing.
        out = payload(**row, names_read_at=NOW)
        assert out["contact_name"] is None, row
        assert out["contact_name_status"] == "not_recorded", row
        # Never asked: the same empty row supports only "not loaded".
        never = payload(**row)
        assert never["contact_name"] is None
        assert never["contact_name_status"] == "not_loaded", row
        assert never["names_read_at"] is None
        # Asked, and HubSpot did not return the contact: a third claim.
        gone = payload(**row, names_last_asked_at=NOW)
        assert gone["contact_name"] is None
        assert gone["contact_name_status"] == "not_returned", row
    # A name field holding an email, an id or no letter is a VALUE that is not
    # shown — not "HubSpot has no name" (review m2).
    for row in ({"first_name": "lina@acme.example"},
                {"first_name": "9000123"},
                {"first_name": "  ", "last_name": "—"}):
        for extra in ({"names_read_at": NOW}, {}, {"names_last_asked_at": NOW}):
            out = payload(**row, **extra)
            assert out["contact_name"] is None, row
            assert out["contact_name_status"] == "not_displayable", (row, extra)
            assert "lina@acme.example" not in json.dumps(
                {k: v for k, v in out.items() if k != "contact_id"}, default=str)
    for out in (payload(**r) for r in ({}, {"first_name": "9000123"})):
        # The company is still the company, in its own field.
        assert out["company"] == "Acme Freight"
        assert out["company_source"] == "hubspot_contact.company"
    # A usable part survives beside an unusable one.
    assert payload(first_name="lina@acme.example",
                   last_name="Haddad")["contact_name"] == "Haddad"
    assert "email" not in " ".join(named)


def test_16_the_row_renders_name_not_recorded_never_blank_a_dash_or_the_company():
    named = {"contact_name": "Lina Haddad", "contact_name_status": "recorded",
             "company": "Acme <Freight>"}
    unnamed = {"contact_name": None, "contact_name_status": "not_recorded",
               "company": "Acme Freight", "contact_id": "9000123"}
    # A payload whose status says "not recorded" but still carries a string.
    inconsistent = {"contact_name": "lina@acme.example",
                    "contact_name_status": "not_recorded", "company": None}
    out = _node({
        "named": ["leadsContactNameHtml", [named]],
        "unnamed": ["leadsContactNameHtml", [unnamed]],
        "inconsistent": ["leadsContactNameHtml", [inconsistent]],
        "company": ["leadsCompanyHtml", [named]],
        "no_company": ["leadsCompanyHtml", [inconsistent]],
        "heading": ["leadsContactHeading", [named]],
        "heading_unnamed": ["leadsContactHeading", [unnamed]],
        "heading_nothing": ["leadsContactHeading", [inconsistent]],
    })
    assert _text(out["named"]) == "Lina Haddad"
    assert _text(out["unnamed"]) == "Name not recorded"
    assert _text(out["inconsistent"]) == "Name not recorded"
    assert "Acme" not in out["unnamed"] and "9000123" not in out["unnamed"]
    assert "@" not in out["inconsistent"]
    assert _text(out["company"]) == "Acme <Freight>"
    assert "<Freight>" not in out["company"]                 # escaped
    assert _text(out["no_company"]) == "Company not recorded"
    assert out["heading"] == "Lina Haddad — Acme <Freight>"
    assert out["heading_unnamed"] == "Acme Freight"
    assert out["heading_nothing"] == "Name not recorded"

    # PR-ADS-178B: the function's own body (sourceGroupLabel precedes it now).
    table = APP_JS.split("async function leadsRenderContacts(")[1].split("\n}\n")[0]
    assert "<td>${leadsContactNameHtml(r)}</td>" in table
    assert "<td>${leadsCompanyHtml(r)}</td>" in table
    assert ">Contact${" in table and ">Company</th>" in table
    assert 'r.company || "—"' not in table


# ═════════════════════════════════════════════════════════════════════════════
# Database cases: the real sync, writer and readers
# ═════════════════════════════════════════════════════════════════════════════
class _HonestHubSpot(m163c._FakeHubSpot):
    """The 163C portal, answering a scan with ONLY the properties asked for
    and only those HubSpot holds a value for — as the real search does."""

    def search(self, req, kwargs):
        resp = super().search(req, kwargs)
        filters = req["filterGroups"][0]["filters"]
        if any(f["operator"] == "IN" for f in filters):
            return resp
        asked = set(req.get("properties") or [])
        resp.results = [m163c._Obj({"id": o.id, "properties": {
            k: v for k, v in o.to_dict()["properties"].items()
            if k in asked and v is not None}}) for o in resp.results]
        return resp


def _q3_contact(n, **props):
    """One contact moved to the Customer stage in Q3 2026."""
    base = {
        "lifecyclestage": "customer",
        "hs_v2_date_entered_customer": f"2026-08-{1 + n % 28:02d}T10:00:00Z",
        # 182 of the 219 were created long before the quarter.
        "createdate": ("2024-03-05T08:00:00Z" if n < 182
                       else "2026-07-20T08:00:00Z"),
        # 206 of the 219 carry an OFFLINE original source.
        "hs_analytics_source": "OFFLINE" if n < 206 else "PAID_SEARCH",
        "hs_analytics_source_data_1": "IMPORT" if n < 206 else "Brand - US",
        "hs_analytics_source_data_2": None,
        "firstname": f"First{n}", "lastname": f"Last{n}",
    }
    base.update(props)
    return m163c._contact(n, **base)


def _contacts_page(**kwargs):
    from services import canonical_crm_funnel_service as funnel
    out = funnel.contacts("business", WINDOW, event="customer", now=NOW, **kwargs)
    assert out["available"] is True, out
    return out


@needs_pg
def test_20_the_real_sync_stores_names_and_the_contact_rows_carry_them(db):
    fake = _HonestHubSpot([
        _q3_contact(0, firstname="Lina", lastname="Haddad", company="Acme Freight"),
        _q3_contact(1, firstname="Omar", lastname=None, company="Bolt Cargo"),
        _q3_contact(2, firstname=None, lastname=None, company="Nameless Ltd"),
        _q3_contact(3, firstname="x@y.example", lastname=None, company=None),
    ])
    m163c._load(fake, NOW - timedelta(hours=3))
    assert fake.calls and set(fake.calls) == {m163c.SEARCH}     # read-only

    stored = {r["contact_id"]: (r["first_name"], r["last_name"]) for r in db.rows(
        "SELECT contact_id, first_name, last_name FROM hubspot_contact_funnel")}
    assert stored == {m163c._cid(0): ("Lina", "Haddad"),
                      m163c._cid(1): ("Omar", None),
                      m163c._cid(2): (None, None),
                      m163c._cid(3): ("x@y.example", None)}   # stored verbatim

    rows = {r["contact_id"]: r for r in _contacts_page()["rows"]}
    assert rows[m163c._cid(0)]["contact_name"] == "Lina Haddad"
    assert rows[m163c._cid(0)]["company"] == "Acme Freight"
    assert rows[m163c._cid(1)]["contact_name"] == "Omar"
    assert rows[m163c._cid(2)]["contact_name"] is None
    assert rows[m163c._cid(2)]["contact_name_status"] == "not_recorded"
    # An email in the first-name field is stored as sent and never shown.
    assert rows[m163c._cid(3)]["contact_name"] is None
    assert rows[m163c._cid(3)]["contact_name_status"] == "not_displayable"
    assert rows[m163c._cid(2)]["company"] == "Nameless Ltd"
    assert rows[m163c._cid(3)]["company"] is None
    assert "email" not in json.dumps(_contacts_page(), default=str).lower() \
        .replace("x@y.example", "")

    # The same search box finds a contact by name, or by company.
    def found(q):
        return {r["contact_id"] for r in _contacts_page(company_query=q)["rows"]}
    assert found("haddad") == {m163c._cid(0)}
    assert found("lina had") == {m163c._cid(0)}
    assert found("bolt") == {m163c._cid(1)}
    assert found("nameless") == {m163c._cid(2)}
    assert found("nobody-by-this-name") == set()
    # ... and a value that is not shown as a name is not searchable as one:
    # typing the address does not find the contact (review m2).
    assert found("x@y.example") == set()
    assert found("x@y") == set() and found("y.example") == set()


@needs_pg
def test_21_a_later_sparse_payload_cannot_blank_a_stored_name(db):
    fake = _HonestHubSpot([_q3_contact(0, firstname="Lina", lastname="Haddad")])
    m163c._load(fake, NOW - timedelta(hours=6))

    def stored():
        (row,) = db.rows("SELECT first_name, last_name, lifecycle_stage, "
                         "last_modified_at FROM hubspot_contact_funnel")
        return row

    before = stored()
    assert (before["first_name"], before["last_name"]) == ("Lina", "Haddad")

    # HubSpot answers a later read of the SAME contact without the names (a
    # property it returned no value for), newer than the stored row.
    props = fake.contacts[m163c._cid(0)]["properties"]
    props.update(firstname=None, lastname=None, lifecyclestage="evangelist",
                 lastmodifieddate=str(m163c.BASE_MS + 3_600_000))
    result = m163c._sync(fake, NOW - timedelta(hours=3))
    assert result["status"] == "success"
    after = stored()
    assert after["last_modified_at"] > before["last_modified_at"]   # it DID write
    assert after["lifecycle_stage"] == "evangelist"                 # latest state
    assert (after["first_name"], after["last_name"]) == ("Lina", "Haddad")

    # A real correction — a different name — still replaces the stored one.
    props.update(firstname="Leena", lastname=None,
                 lastmodifieddate=str(m163c.BASE_MS + 7_200_000))
    m163c._sync(fake, NOW - timedelta(hours=2))
    after = stored()
    assert (after["first_name"], after["last_name"]) == ("Leena", "Haddad")


@needs_pg
def test_22_an_existing_database_gains_the_name_columns_on_both_tables_and_keeps_its_rows(db):
    fake = _HonestHubSpot([_q3_contact(0)])
    m163c._load(fake, NOW - timedelta(hours=3))
    # A database from before this PR: neither table has the columns.
    for table in ("hubspot_contact_funnel", "hubspot_contact_funnel_removed"):
        db.execute(f"ALTER TABLE {table} DROP COLUMN first_name, "
                   "DROP COLUMN last_name, DROP COLUMN names_read_at, "
                   "DROP COLUMN names_last_asked_at")
    db.execute("DROP TABLE hubspot_company_name")
    from db.schema import init_db
    init_db()

    def columns(table):
        return {r["column_name"] for r in db.rows(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (table,))}
    for table in ("hubspot_contact_funnel", "hubspot_contact_funnel_removed"):
        assert {"first_name", "last_name", "names_read_at",
                "names_last_asked_at"} <= columns(table), table
    assert columns("hubspot_company_name") >= {"company_id", "name", "read_at",
                                               "last_asked_at"}
    assert db.funnel_count() == 1
    # Its name has never been read: that is "not loaded", not "HubSpot has none".
    page = _contacts_page()
    (row,) = page["rows"]
    assert row["contact_name"] is None
    assert row["contact_name_status"] == "not_loaded"
    assert page["names_progress"] == {
        "available": True, "read": 0, "not_returned": 0, "never_asked": 1,
        "total": 1, "complete": False}
    assert _text(_node({"n": ["leadsContactNameHtml", [row]]})["n"]) \
        == "Name not loaded yet"


@needs_pg
def test_23_the_production_quarter_over_the_real_funnel_219_moves_beside_8_won_deals(db, monkeypatch):
    fake = _HonestHubSpot([_q3_contact(n) for n in range(STAGE_MOVES)])
    m163c._load(fake, NOW - timedelta(hours=3))

    from services import canonical_crm_funnel_service as funnel
    payload = funnel.build("business", WINDOW, now=NOW)
    assert payload["available"] is True
    block = payload["events"]["customer"]
    assert block["count"] == STAGE_MOVES
    assert block["metric_family"] == "lifecycle_stage_events"
    assert block["window_basis"] == "date_entered_customer"
    for event, other in payload["events"].items():
        assert other["metric_family"] == "lifecycle_stage_events"
        assert other["window_basis"] == other["event_date_column"], event
    # What the count is made of: old, offline contacts moved in the quarter.
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel "
                     "WHERE created_at < '2026-07-01'") == 182
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel "
                     "WHERE hs_analytics_source = 'OFFLINE'") == 206

    _ledger(monkeypatch)
    won = _won()
    assert won["customers"]["value"] == WON_COMPANIES
    assert won["closed_won_deals"]["value"] == WON_DEALS

    markup = _figures(payload, won)
    assert _figure(markup, "stage_moves").startswith(
        "Moved to Customer stage in HubSpot 219 ")
    assert _figure(markup, "customers_won").startswith("Customers 7 ")

    # The page of contacts behind the stage-move count is those 219, named.
    page = _contacts_page(page_size=100)
    assert page["total"] == STAGE_MOVES
    assert all(r["contact_name_status"] == "recorded" for r in page["rows"])


# ═════════════════════════════════════════════════════════════════════════════
# Round 2 — names for records already stored, and the won customers' names
# ═════════════════════════════════════════════════════════════════════════════
def test_17_not_loaded_and_not_recorded_are_two_claims_and_the_header_says_how_far_loading_is():
    out = _node({
        "not_loaded": ["leadsContactNameHtml", [{"contact_name_status": "not_loaded"}]],
        "not_recorded": ["leadsContactNameHtml", [{"contact_name_status": "not_recorded"}]],
        # A status this page does not know is not evidence HubSpot has no name.
        "unknown": ["leadsContactNameHtml", [{"contact_name_status": "something_new"}]],
        "no_status": ["leadsContactNameHtml", [{"contact_name": "Lina"}]],
        "head_not_loaded": ["leadsContactHeading", [{"contact_name_status": "not_loaded"}]],
        "not_displayable": ["leadsContactNameHtml",
                            [{"contact_name_status": "not_displayable",
                              "contact_name": "lina@acme.example"}]],
        "not_returned": ["leadsContactNameHtml",
                         [{"contact_name_status": "not_returned"}]],
        "head_not_returned": ["leadsContactHeading",
                              [{"contact_name_status": "not_returned"}]],
        "progress": ["leadsNamesProgressNote",
                     [{"available": True, "read": 3000, "not_returned": 0,
                       "never_asked": 18751, "total": 21751}]],
        "none_yet": ["leadsNamesProgressNote",
                     [{"available": True, "read": 0, "not_returned": 0,
                       "never_asked": 21751, "total": 21751}]],
        "nearly": ["leadsNamesProgressNote",
                   [{"available": True, "read": 21750, "not_returned": 0,
                     "never_asked": 1, "total": 21751}]],
        "complete": ["leadsNamesProgressNote",
                     [{"available": True, "read": 21751, "not_returned": 0,
                       "never_asked": 0, "total": 21751}]],
        # Thirty contacts HubSpot never returns do not keep the note up for
        # ever: every contact has been asked for (review m3).
        "done_with_stuck": ["leadsNamesProgressNote",
                            [{"available": True, "read": 21721, "not_returned": 30,
                              "never_asked": 0, "total": 21751}]],
        "unknown_counts": ["leadsNamesProgressNote",
                           [{"available": False, "read": None, "total": None}]],
        # Counts without `never_asked` cannot say the load is done.
        "old_shape": ["leadsNamesProgressNote",
                      [{"available": True, "read": 5, "total": 5}]],
        "missing": ["leadsNamesProgressNote", [None]],
        "header": ["leadsNamesNoteHtml",
                   [{"available": True, "read": 3000, "not_returned": 0,
                     "never_asked": 18751, "total": 21751}]],
        "header_done": ["leadsNamesNoteHtml",
                        [{"available": True, "read": 5, "not_returned": 0,
                          "never_asked": 0, "total": 5}]],
    })
    assert _text(out["not_displayable"]) == "Name not shown"
    assert "@" not in out["not_displayable"]
    assert ("HubSpot holds a value in the name field that is not shown as a "
            "name") in out["not_displayable"]
    assert _text(out["not_returned"]) == "Not returned by HubSpot"
    assert out["head_not_returned"] == "Not returned by HubSpot"
    assert out["done_with_stuck"] == ""
    assert _text(out["not_loaded"]) == "Name not loaded yet"
    assert _text(out["not_recorded"]) == "Name not recorded"
    assert _text(out["unknown"]) == "Name not loaded yet"
    assert _text(out["no_status"]) == "Name not loaded yet"
    assert out["head_not_loaded"] == "Name not loaded yet"
    assert out["progress"] == ("Names are being loaded from HubSpot: 3,000 of "
                               "21,751 contacts read so far.")
    assert out["none_yet"].endswith(": 0 of 21,751 contacts read so far.")
    # Not hidden once most exist: one unread contact keeps the note.
    assert out["nearly"].endswith(": 21,750 of 21,751 contacts read so far.")
    assert out["complete"] == ""
    # Unknown counts never read as "complete".
    for key in ("unknown_counts", "missing", "old_shape"):
        assert out[key] == ("Names are being loaded from HubSpot: progress is "
                            "unavailable.")
    assert _text(out["header"]) == out["progress"]
    assert out["header_done"] == ""
    # PR-ADS-178B: the function's own body (sourceGroupLabel precedes it now).
    table = APP_JS.split("async function leadsRenderContacts(")[1].split("\n}\n")[0]
    assert ">Contact${leadsNamesNoteHtml(payload.names_progress)}</th>" in table


_WON_LIST = [
    {"company_id": "c1", "company_name": "Shared <Logistics>",
     "company_name_status": "named", "closed_won_deals": 2,
     "latest_close_date": "2026-08-11T09:00:00+00:00"},
    {"company_id": "c2", "company_name": None,
     "company_name_status": "no_name_in_hubspot", "closed_won_deals": 1,
     "latest_close_date": "2026-08-12T09:00:00+00:00"},
    {"company_id": "c3", "company_name": None,
     "company_name_status": "not_loaded", "closed_won_deals": 1,
     "latest_close_date": None},
]


def _won_payload(customers, rows=None):
    payload = {"metric_family": "closed_won_customers",
               "requested": {"window_key": WINDOW}, "customers": customers,
               "closed_won_deals": {"value": 4, "status": "published", "reason": None}}
    if rows is not None:
        payload["won_customers"] = rows
    return payload


def test_18_the_won_customers_list_is_shown_only_for_a_published_count_that_it_matches():
    published = {"value": 3, "status": "published", "reason": None}
    withheld = {"value": None, "status": "withheld",
                "reason": cwt.R_CUSTOMER_IDENTITY}
    cases = {
        "published": _won_payload(published, _WON_LIST),
        # A withheld count that (wrongly) still carried a list: nothing shown.
        "withheld": _won_payload(withheld, _WON_LIST),
        "withheld_no_list": _won_payload(withheld),
        # A list that disagrees with its own headline is not shown beside it.
        "mismatch": _won_payload(published, _WON_LIST[:2]),
        "no_list": _won_payload(published),
    }
    raw = _node({k: v for key, won in cases.items()
                 for k, v in _figures_calls(_funnel_payload(), won, key).items()})
    out = {key: _figures_markup(raw, key) for key in cases}
    shown = _figure(out["published"], "customers_won")
    # PR-ADS-178B: the Customers card, then the Customers won section's list.
    assert shown == (
        "Customers 3 Distinct HubSpot companies · never summed HubSpot companies "
        "on closed-won deals Customers won Distinct companies with a closed-won "
        "deal closed in this period. Not the same as contacts moved to the "
        "Customer stage. Customer Closed-won deals Latest close date "
        "Shared <Logistics> 2 2026-08-11 "
        "Company has no name in HubSpot 1 2026-08-12 "
        "Company name not loaded yet 1 Unavailable")
    assert "<Logistics>" not in out["published"]                 # escaped
    assert "<table" in out["published"]
    for key in ("withheld", "withheld_no_list", "mismatch", "no_list"):
        assert "<table" not in out[key], key
        assert "Shared" not in out[key], key
        assert "Company name not loaded yet" not in out[key], key
    # A published count with no list that matches it says so (the list's own
    # verdict: the rows do not match the published count).
    for key in ("mismatch", "no_list"):
        assert _figure(out[key], "customers_won").endswith(
            "Withheld the deal rows received do not match the published count "
            "— no list of customers is shown."), key
    assert _figure(out["withheld"], "customers_won").startswith(
        "Customers Withheld a won deal has no company, or several ")
    assert _figure(out["mismatch"], "customers_won").startswith("Customers 3 ")


def test_19_a_name_step_that_is_partial_stays_partial_on_the_run(monkeypatch):
    from scheduler import incremental_sync as sync
    from services import hubspot_name_backfill_service as names

    def result(status, **extra):
        return {"status": status, "error": "1 batch failed", "selected": 300,
                "batches_requested": 3, "batches_answered": 2, **extra}

    for runner, label in (("run_contact_name_backfill", sync.LABEL_CONTACT_NAMES),
                          ("run_company_name_sync", sync.LABEL_COMPANY_NAMES)):
        monkeypatch.setattr(names, runner, lambda: result("partial"))
        errors: list = []
        out = sync._sync_names(label=label, runner=runner, errors=errors)
        assert out["status"] == "partial"                 # not failed, not success
        assert len(errors) == 1 and "'partial'" in errors[0] and label in errors[0]
        assert "answered 2 of 3" in errors[0]
        # The run it belongs to is partial, with other datasets green.
        assert sync._overall_status({"a": {"status": "success"}, label: out}) == "partial"

        monkeypatch.setattr(names, runner, lambda: result("success", error=None))
        errors = []
        assert sync._sync_names(label=label, runner=runner, errors=errors)["status"] == "success"
        assert errors == []

        # An unrecognised outcome is not evidence of success.
        monkeypatch.setattr(names, runner, lambda: result("done"))
        errors = []
        assert sync._sync_names(label=label, runner=runner, errors=errors)["status"] == "failed"
        assert errors

        def boom():
            raise RuntimeError("service exploded")
        monkeypatch.setattr(names, runner, boom)
        errors = []
        out = sync._sync_names(label=label, runner=runner, errors=errors)
        assert out["status"] == "failed" and out["written"] is None
        assert errors

    import inspect
    body = inspect.getsource(sync.run_daily_incremental_sync)
    funnel_at = body.index('datasets["hubspot/contact_funnel"]')
    names_at = body.index("datasets[LABEL_CONTACT_NAMES]")
    assoc_at = body.index("datasets[LABEL_DEAL_COMPANY_ASSOCIATIONS]")
    companies_at = body.index("datasets[LABEL_COMPANY_NAMES]")
    assert funnel_at < names_at and assoc_at < companies_at
    step = inspect.getsource(sync._sync_names)
    assert 'if status == "success" else "failed"' not in step
    # The cap is the configured one.
    assert names.load_config() == {"max_contacts_per_run": 3000,
                                   "max_companies_per_run": 1000,
                                   "company_reread_days": 7.0,
                                   "not_returned_retry_days": 7.0}
    assert names._status(0, 0, 0) == "success"
    assert names._status(3, 3, 0) == "success"
    assert names._status(3, 2, 0) == "partial"
    assert names._status(3, 3, 1) == "partial"
    assert names._status(3, 0, 0) == "failed"


class _Refuse:
    """Any call that is not a batch READ is recorded and refused."""

    def __init__(self, fake, path):
        self._fake, self._path = fake, path

    def __getattr__(self, name):
        return _Refuse(self._fake, f"{self._path}.{name}")

    def __call__(self, *args, **kwargs):
        self._fake.forbidden.append(self._path)
        raise AssertionError(f"forbidden HubSpot call: {self._path}")


class _BatchApi(_Refuse):
    def __init__(self, fake, kind):
        super().__init__(fake, f"crm.{kind}.batch_api")
        self._kind = kind

    def read(self, batch_read_input_simple_public_object_id=None, **kwargs):
        return self._fake.read(self._kind, batch_read_input_simple_public_object_id)


class _Objects(_Refuse):
    def __init__(self, fake, kind):
        super().__init__(fake, f"crm.{kind}")
        self.batch_api = _BatchApi(fake, kind)


class _BatchHubSpot:
    """A HubSpot that answers batch reads by id — and nothing else.

    ``objects[kind]`` maps an id to its properties; an id that is not there
    is one HubSpot does not return (a 404 inside a batch). A property whose
    value is None is not returned. A batch containing an id in ``fail_ids``
    fails as a whole with HTTP 500, for as long as the id stays there.
    """

    def __init__(self, contacts=None, companies=None):
        self.objects = {"contacts": dict(contacts or {}),
                        "companies": dict(companies or {})}
        self.calls: list = []        # (kind, [ids asked], [properties asked])
        self.forbidden: list = []
        self.fail_ids: set = set()
        self.fail_all = False
        crm = _Refuse(self, "crm")
        crm.contacts = _Objects(self, "contacts")
        crm.companies = _Objects(self, "companies")
        self.client = _Refuse(self, "client")
        self.client.crm = crm

    def read(self, kind, body):
        from hubspot.crm.contacts import ApiException
        inputs = body["inputs"] if isinstance(body, dict) else body.inputs
        ids = [str(i["id"] if isinstance(i, dict) else i.id) for i in inputs]
        props = list(body["properties"] if isinstance(body, dict)
                     else body.properties)
        self.calls.append((kind, ids, props))
        assert len(ids) <= 100, "HubSpot refuses a batch of more than 100"
        if self.fail_all or self.fail_ids & set(ids):
            raise ApiException(status=500, reason="Internal Server Error")
        store = self.objects[kind]
        results = [m163c._Obj({"id": i, "properties": {
            k: v for k, v in store[i].items() if k in props and v is not None}})
            for i in ids if i in store]
        return type("Resp", (), {"results": results})()


def _unread(db):
    return {r["contact_id"] for r in db.rows(
        "SELECT contact_id FROM hubspot_contact_funnel WHERE names_read_at IS NULL")}


def _stored_names(db):
    return {r["contact_id"]: (r["first_name"], r["last_name"]) for r in db.rows(
        "SELECT contact_id, first_name, last_name FROM hubspot_contact_funnel")}


@pytest.fixture()
def _no_pacing(monkeypatch):
    monkeypatch.setattr(hp, "NAME_READ_PACING_SECONDS", 0)
    monkeypatch.setattr(hp, "INITIAL_BACKOFF_SECONDS", 0)   # retries, no waiting


@needs_pg
def test_24_the_backfill_reads_stored_contacts_newest_first_and_stamps_only_what_hubspot_answered(db, _no_pacing):
    from services import hubspot_name_backfill_service as names

    cid = m163c._cid
    portal = _HonestHubSpot([m163c._contact(
        n, firstname=f"First{n}", lastname=f"Last{n}",
        hs_v2_date_entered_lead="2026-10-29T10:00:00Z") for n in range(10)])
    m163c._load(portal, NOW - timedelta(hours=30))
    # The modified-since sync asked for names, so it stamps every contact.
    assert _unread(db) == set()
    # A database from before this PR: contacts stored, names never read.
    db.execute("UPDATE hubspot_contact_funnel SET names_read_at = NULL, "
               "first_name = NULL, last_name = NULL")
    db.execute("UPDATE hubspot_contact_funnel SET first_name = 'Kept' "
               "WHERE contact_id = %s", (cid(3),))
    assert names.contact_name_progress() == {
        "available": True, "read": 0, "not_returned": 0, "never_asked": 10,
        "total": 10, "complete": False}

    fake = _BatchHubSpot(contacts={
        cid(n): {"firstname": f"First{n}", "lastname": f"Last{n}",
                 "email": f"p{n}@x.example"} for n in range(10)})
    del fake.objects["contacts"][cid(8)]                       # 404 inside a batch
    fake.objects["contacts"][cid(7)] = {"firstname": "  ", "lastname": None}
    fake.objects["contacts"][cid(3)] = {"firstname": None, "lastname": "Last3"}
    fake.fail_ids = {cid(5)}                                   # one batch fails

    out = names.run_contact_name_backfill(now=NOW, client=fake.client,
                                          limit=9, batch_size=3)
    # Newest contact first (createdate rises with n), capped at nine, in
    # batches of three, asking for the two name properties and nothing else.
    # The failing batch is retried with backoff before it is given up on.
    asked = list(dict.fromkeys(tuple(c[1]) for c in fake.calls))
    assert asked == [(cid(9), cid(8), cid(7)), (cid(6), cid(5), cid(4)),
                     (cid(3), cid(2), cid(1))]
    assert len(fake.calls) == 1 + hp.MAX_RETRIES + 1
    assert {tuple(c[2]) for c in fake.calls} == {("firstname", "lastname")}
    assert {c[0] for c in fake.calls} == {"contacts"} and fake.forbidden == []

    assert out["status"] == "partial"                 # one of three batches failed
    assert (out["batches_requested"], out["batches_answered"],
            out["batches_failed"]) == (3, 2, 1)
    assert out["selected"] == 9 and out["max_per_run"] == 9
    assert out["returned"] == 5 and out["not_returned"] == 1
    assert out["with_name"] == 4 and out["without_name"] == 1
    assert out["written"] == 5
    assert out["hubspot_writes_performed"] is False
    assert "500" in out["error"]

    # Stamped as READ: exactly the ids HubSpot returned. Not the 404, not the
    # failed batch, not the one over the cap.
    assert _unread(db) == {cid(8), cid(6), cid(5), cid(4), cid(0)}
    # Stamped as ASKED-AND-NOT-RETURNED: only the 404 — not the failed batch,
    # which was never answered.
    asked_stamp = {r["contact_id"]: r["names_last_asked_at"] for r in db.rows(
        "SELECT contact_id, names_last_asked_at FROM hubspot_contact_funnel")}
    assert {c for c, at in asked_stamp.items() if at is not None} == {cid(8)}
    assert asked_stamp[cid(8)] == NOW
    stored = _stored_names(db)
    assert stored[cid(9)] == ("First9", "Last9")
    assert stored[cid(7)] == (None, None)             # read; HubSpot has none
    assert stored[cid(3)] == ("Kept", "Last3")        # empty answer keeps a name
    assert stored[cid(5)] == (None, None) and stored[cid(8)] == (None, None)
    assert "x.example" not in json.dumps(stored)
    assert out["progress"] == {"available": True, "read": 5, "not_returned": 1,
                               "never_asked": 4, "total": 10, "complete": False}

    from services import canonical_crm_funnel_service as funnel
    page = funnel.contacts("business", "all_time", event="lead", now=NOW)
    rows = {r["contact_id"]: r for r in page["rows"]}
    assert rows[cid(9)]["contact_name"] == "First9 Last9"
    assert rows[cid(7)]["contact_name_status"] == "not_recorded"
    assert rows[cid(8)]["contact_name_status"] == "not_returned"  # 404 ≠ no name
    assert rows[cid(5)]["contact_name_status"] == "not_loaded"   # failed batch
    assert rows[cid(0)]["contact_name_status"] == "not_loaded"   # over the cap
    assert page["names_progress"]["read"] == 5
    rendered = _node({
        "note": ["leadsNamesNoteHtml", [page["names_progress"]]],
        "read_none": ["leadsContactNameHtml", [rows[cid(7)]]],
        "missing": ["leadsContactNameHtml", [rows[cid(8)]]],
        "unread": ["leadsContactNameHtml", [rows[cid(5)]]]})
    assert _text(rendered["note"]) == ("Names are being loaded from HubSpot: "
                                       "5 of 10 contacts read so far.")
    assert _text(rendered["read_none"]) == "Name not recorded"
    assert _text(rendered["missing"]) == "Not returned by HubSpot"
    assert _text(rendered["unread"]) == "Name not loaded yet"

    # Next run: the failed batch and the one over the cap are read. The 404
    # was asked yesterday and is NOT asked again today.
    fake.fail_ids, fake.calls = set(), []
    out = names.run_contact_name_backfill(now=NOW + timedelta(days=1),
                                          client=fake.client, limit=9,
                                          batch_size=3)
    assert [c[1] for c in fake.calls] == [[cid(6), cid(5), cid(4)], [cid(0)]]
    assert out["status"] == "success" and out["not_returned"] == 0
    assert _unread(db) == {cid(8)}
    # Every contact has now been asked for: the load is done, and the note
    # clears although one contact was never returned.
    assert out["progress"] == {"available": True, "read": 9, "not_returned": 1,
                               "never_asked": 0, "total": 10, "complete": True}
    assert _node({"n": ["leadsNamesNoteHtml", [out["progress"]]]})["n"] == ""
    stamps = {r["contact_id"]: r["names_read_at"] for r in db.rows(
        "SELECT contact_id, names_read_at FROM hubspot_contact_funnel")}
    assert stamps[cid(9)] == NOW and stamps[cid(5)] == NOW + timedelta(days=1)

    # Six days on nothing is due: zero calls, success.
    fake.calls = []
    out = names.run_contact_name_backfill(now=NOW + timedelta(days=6),
                                          client=fake.client)
    assert fake.calls == [] and out["status"] == "success"
    assert out["batches_requested"] == 0 and out["max_per_run"] == 3000

    # After a week the not-returned contact is asked once more. Nothing
    # answers at all: failed, and its last-asked stamp does not move.
    day8 = NOW + timedelta(days=7, hours=1)
    fake.fail_all, fake.calls = True, []
    out = names.run_contact_name_backfill(now=day8, client=fake.client)
    assert {tuple(c[1]) for c in fake.calls} == {(cid(8),)}
    assert out["status"] == "failed" and out["batches_answered"] == 0
    assert db.scalar("SELECT names_last_asked_at FROM hubspot_contact_funnel "
                     "WHERE contact_id = %s", (cid(8),)) == NOW

    # HubSpot answers and still does not return it: answered, stamped again.
    fake.fail_all, fake.calls = False, []
    out = names.run_contact_name_backfill(now=day8, client=fake.client)
    assert out["status"] == "success" and out["not_returned"] == 1
    assert db.scalar("SELECT names_last_asked_at FROM hubspot_contact_funnel "
                     "WHERE contact_id = %s", (cid(8),)) == day8

    # A week later it is back in HubSpot: read like any other contact.
    fake.objects["contacts"][cid(8)] = {"firstname": "Back", "lastname": None}
    out = names.run_contact_name_backfill(now=day8 + timedelta(days=8),
                                          client=fake.client)
    assert out["status"] == "success" and _unread(db) == set()
    assert _stored_names(db)[cid(8)] == ("Back", None)
    assert out["progress"] == {"available": True, "read": 10, "not_returned": 0,
                               "never_asked": 0, "total": 10, "complete": True}


@needs_pg
def test_25_a_later_sync_that_omits_the_names_keeps_the_record_that_they_were_read(db):
    portal = _HonestHubSpot([_q3_contact(0, firstname=None, lastname=None)])
    m163c._load(portal, NOW - timedelta(hours=6))
    (first,) = db.rows("SELECT names_read_at FROM hubspot_contact_funnel")
    assert first["names_read_at"] is not None       # read, and HubSpot has none
    (row,) = _contacts_page()["rows"]
    assert row["contact_name_status"] == "not_recorded"

    # Another writer of the same row that never read the names (no key at
    # all) must not erase the record that they were read.
    from db import writers as db_writers
    normalised = hp.normalize_contact_funnel_row({
        "id": m163c._cid(0), "properties": {
            **portal.contacts[m163c._cid(0)]["properties"],
            "lastmodifieddate": str(m163c.BASE_MS + 9_000_000)}})
    assert "names_read_at" not in normalised
    result = db_writers.upsert_hubspot_contact_funnel([normalised])
    assert result["ok"] and result["persisted"] == 1
    (after,) = db.rows("SELECT names_read_at FROM hubspot_contact_funnel")
    assert after["names_read_at"] == first["names_read_at"]


def _associate(db, pairs):
    for deal_id, company_id in pairs:
        db.execute(
            "INSERT INTO hubspot_deal_company_association (deal_id, company_id, "
            "association_category, association_type_id, association_path_key, "
            "last_observed_at) VALUES (%s, %s, 'HUBSPOT_DEFINED', '5', "
            "'HUBSPOT_DEFINED:5', %s)", (deal_id, company_id, NOW))


def _company_rows(db):
    return {r["company_id"]: (r["name"], r["read_at"]) for r in db.rows(
        "SELECT company_id, name, read_at FROM hubspot_company_name")}


@needs_pg
def test_26_company_names_are_read_by_id_and_a_failed_or_empty_read_keeps_the_last_good_name(db, _no_pacing):
    from services import hubspot_name_backfill_service as names

    # Two association paths to one company are still one company to read.
    _associate(db, [("d1", "c1"), ("d2", "c2"), ("d3", "c3"), ("d4", "c4"),
                    ("d5", "c5"), ("d6", "c1")])
    fake = _BatchHubSpot(companies={
        "c1": {"name": "Acme Freight", "domain": "acme.example"},
        "c2": {"name": "   "},                 # a company with no name
        "c4": {"name": "Delta"}, "c5": {"name": "Echo"}})   # c3: not returned
    fake.fail_ids = {"c5"}

    out = names.run_company_name_sync(now=NOW, client=fake.client, batch_size=2)
    asked = list(dict.fromkeys(tuple(c[1]) for c in fake.calls))
    assert asked == [("c1", "c2"), ("c3", "c4"), ("c5",)]
    assert {tuple(c[2]) for c in fake.calls} == {("name",)}
    assert {c[0] for c in fake.calls} == {"companies"} and fake.forbidden == []
    assert out["status"] == "partial"
    assert (out["batches_requested"], out["batches_answered"],
            out["batches_failed"]) == (3, 2, 1)
    assert out["returned"] == 3 and out["not_returned"] == 1
    assert out["written"] == 3 and out["hubspot_writes_performed"] is False
    # c3 was asked for and not returned: a row that says so, with no read. The
    # failed batch (c5) left nothing at all.
    assert _company_rows(db) == {"c1": ("Acme Freight", NOW), "c2": (None, NOW),
                                 "c3": (None, None), "c4": ("Delta", NOW)}
    assert db.scalar("SELECT last_asked_at FROM hubspot_company_name "
                     "WHERE company_id = 'c3'") == NOW
    from db import hubspot_name_repository as repo
    assert repo.fetch_company_names(["c1", "c2", "c3", "c5"]) == {
        "available": True, "names": {
            "c1": {"name": "Acme Freight", "read": True},
            "c2": {"name": None, "read": True},
            "c3": {"name": None, "read": False}}}

    # Next day: only the never-asked id is due. c3 was asked yesterday.
    day2 = NOW + timedelta(days=1)
    fake.fail_ids, fake.calls = set(), []
    out = names.run_company_name_sync(now=day2, client=fake.client, batch_size=2)
    assert [c[1] for c in fake.calls] == [["c5"]]
    assert out["status"] == "success" and out["not_returned"] == 0
    assert _company_rows(db)["c5"] == ("Echo", day2)

    # A week on everything is due again — and every read fails.
    day9 = NOW + timedelta(days=8, hours=1)
    fake.fail_all, fake.calls = True, []
    before = _company_rows(db)
    out = names.run_company_name_sync(now=day9, client=fake.client, batch_size=2)
    assert list(dict.fromkeys(tuple(c[1]) for c in fake.calls)) == [
        ("c1", "c2"), ("c3", "c4"), ("c5",)]
    assert out["status"] == "failed" and out["written"] == 0
    assert _company_rows(db) == before               # the last good names stand

    # Then HubSpot answers with an EMPTY name for one and a NEW name for another.
    day10 = day9 + timedelta(days=1)
    fake.fail_all, fake.calls = False, []
    fake.objects["companies"]["c1"] = {"name": None}
    fake.objects["companies"]["c4"] = {"name": "Delta Ltd"}
    out = names.run_company_name_sync(now=day10, client=fake.client, batch_size=2)
    assert out["status"] == "success" and out["not_returned"] == 1
    rows = _company_rows(db)
    assert rows["c1"] == ("Acme Freight", day10)     # not blanked by absence
    assert rows["c4"] == ("Delta Ltd", day10)        # a real correction lands
    assert rows["c3"] == (None, None)
    # A named company HubSpot later stops returning keeps its name and its
    # read stamp; only "last asked" moves.
    del fake.objects["companies"]["c4"]
    day18 = day10 + timedelta(days=8)
    names.run_company_name_sync(now=day18, client=fake.client, batch_size=2)
    assert _company_rows(db)["c4"] == ("Delta Ltd", day10)
    assert db.scalar("SELECT last_asked_at FROM hubspot_company_name "
                     "WHERE company_id = 'c4'") == day18
    # An older read arriving late never overwrites a newer one.
    from db import writers as db_writers
    db_writers.upsert_company_names([{"company_id": "c4", "name": "Stale"}],
                                    read_at=NOW)
    assert _company_rows(db)["c4"] == ("Delta Ltd", day10)


@needs_pg
def test_27_the_customers_won_payload_lists_the_won_customers_only_when_they_are_published(db, monkeypatch):
    from db import writers as db_writers
    db_writers.upsert_company_names(
        [{"company_id": "co-shared", "name": "Shared Logistics"},
         {"company_id": "co-d2", "name": None},
         {"company_id": "co-never-won", "name": "Not In This Window"}],
        read_at=NOW)
    db_writers.mark_company_names_not_returned(["co-d3"], asked_at=NOW)

    _ledger(monkeypatch)
    won = _won()
    assert won["customers"]["value"] == WON_COMPANIES
    listed = won["won_customers"]
    assert len(listed) == WON_COMPANIES
    assert sum(r["closed_won_deals"] for r in listed) == WON_DEALS
    by_id = {r["company_id"]: r for r in listed}
    assert "co-never-won" not in by_id
    assert by_id["co-shared"] == {
        "company_id": "co-shared", "company_name": "Shared Logistics",
        "company_name_status": "named", "closed_won_deals": 2,
        "latest_close_date": "2026-08-11T09:00:00+00:00"}
    assert by_id["co-d2"]["company_name_status"] == "no_name_in_hubspot"
    assert by_id["co-d2"]["company_name"] is None
    assert by_id["co-d7"]["company_name_status"] == "not_loaded"
    assert by_id["co-d3"]["company_name_status"] == "not_returned"
    assert by_id["co-d3"]["company_name"] is None
    # Newest close first.
    assert [r["company_id"] for r in listed][0] == "co-d7"

    markup = _figures(_funnel_payload(), won)
    text = _figure(markup, "customers_won")
    # PR-ADS-178B: the Pipeline page's Customers won table (no fold).
    assert text.startswith("Customers 7 ") and markup.count("<tr>") == 1 + 7
    assert "Shared Logistics 2 2026-08-11" in text
    assert "Company has no name in HubSpot 1 2026-08-12" in text
    assert text.count("Company name not loaded yet") == 4
    assert "Company not returned by HubSpot 1 2026-08-13" in text

    # Withheld: one won deal has no company. No list, no key, no name.
    _ledger(monkeypatch, companies={"d3": []})
    withheld = _won()
    assert withheld["customers"]["status"] == cwt.WITHHELD
    assert "won_customers" not in withheld
    assert "Shared Logistics" not in json.dumps(withheld, default=str)
    assert "co-shared" not in json.dumps(withheld, default=str)
    markup = _figures(_funnel_payload(), withheld)
    assert "<table" not in markup and "Shared Logistics" not in markup

    # Withheld with every deal still resolving to one company (the last full
    # association scan is five days old). The list COULD be built — and is not.
    _ledger(monkeypatch, scan_age_hours=120)
    stale = _won()
    assert stale["customers"]["value"] is None
    assert stale["customers"]["status"] == cwt.WITHHELD
    assert stale["customers"]["reason"] == cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT
    assert "won_customers" not in stale and "won_customers_omitted" not in stale
    assert "Shared Logistics" not in json.dumps(stale, default=str)

    # Unreadable ledger or a filter: no list either.
    _ledger(monkeypatch, available=False)
    assert "won_customers" not in _won()
    from services import canonical_crm_funnel_service as funnel
    assert "won_customers" not in funnel.build_customers_won(
        "business", WINDOW, scope="google_ads_source", now=NOW)


def test_28_a_list_that_cannot_be_reconciled_with_the_published_count_is_left_out(monkeypatch):
    from services import canonical_crm_funnel_service as funnel
    from services import canonical_revenue_service as crs

    real = crs.load_won_deals
    _ledger(monkeypatch)

    def tampered(*a, **k):
        base = real(*a, **k)
        base["company_ids_by_deal"] = {**base["company_ids_by_deal"],
                                       "d5": ["co-d5", "co-other"]}
        return base
    monkeypatch.setattr(crs, "load_won_deals", tampered)
    out = funnel.build_customers_won("business", WINDOW, now=NOW)
    assert out["customers"]["value"] == WON_COMPANIES
    assert "won_customers" not in out
    assert out["won_customers_omitted"] == "closed_won_rows_do_not_match_count"
    markup = _figures(_funnel_payload(), out)
    # PR-ADS-178B: the Customers card keeps its published 7; the section
    # says why there is no list (the rows do not match the count).
    assert _figure(markup, "customers_won").startswith("Customers 7 ")
    assert _figure(markup, "customers_won").endswith(
        "Withheld the deal rows received do not match the published count "
        "— no list of customers is shown.")
    assert "<table" not in markup

    # With no database the names are "unavailable" — never "not loaded".
    monkeypatch.setattr(crs, "load_won_deals", real)
    from db import hubspot_name_repository as repo
    monkeypatch.setattr(repo, "fetch_company_names",
                        lambda ids: {"available": False, "names": {}})
    out = funnel.build_customers_won("business", WINDOW, now=NOW)
    assert {r["company_name_status"] for r in out["won_customers"]} == {"unavailable"}
    markup = _figures(_funnel_payload(), out)
    assert _figure(markup, "customers_won").count("Company name unavailable") == 7


# ═════════════════════════════════════════════════════════════════════════════
# Review round 1
# ═════════════════════════════════════════════════════════════════════════════
class _FixtureMisuse(BaseException):
    """The fake was used in a way the real SDK cannot behave. A BaseException
    so that no ``except Exception`` in the code under test can swallow it."""


class _ScriptedBatchApi:
    """Answers ``read`` from a script: an exception instance is raised, any
    other value is returned as the SDK would return it.

    ``kind`` is the SDK package this API belongs to. An SDK exception from any
    OTHER package is refused: ``hubspot.crm.companies`` and
    ``hubspot.crm.contacts`` each have their own ``ApiException`` with no
    common base, and a fake that raised the contacts class from the companies
    API hid exactly the bug review round 2 found.
    """

    def __init__(self, script, kind):
        self.script, self.calls, self.kind = list(script), 0, kind

    def read(self, batch_read_input_simple_public_object_id=None, **kwargs):
        self.calls += 1
        if not self.script:
            raise _FixtureMisuse("the code under test made a call the script "
                                 "did not expect")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            module = type(step).__module__
            if module.startswith("hubspot.") and not module.startswith(
                    f"hubspot.crm.{self.kind}."):
                raise _FixtureMisuse(
                    f"the {self.kind} API never raises "
                    f"{module}.{type(step).__name__}")
            raise step
        if isinstance(step, (list, type(None))):
            return type("Resp", (), {"results": step})()
        return step


def _scripted_client(script, kind="contacts"):
    """A client that has ONLY the ``kind`` API: reaching for the other one is
    an AttributeError, not a silent answer."""
    api = _ScriptedBatchApi(script, kind)
    objects = type("O", (), {"batch_api": api})()
    crm = type("C", (), {kind: objects})()
    return type("Client", (), {"crm": crm})(), api


def _api_error(status, reason, body=None, kind="contacts"):
    """The exception the REAL SDK raises for ``status`` from the ``kind``
    package (``rest.py``: 401 / 403 / 404 / 5xx have their own subclasses)."""
    import importlib
    exceptions = importlib.import_module(f"hubspot.crm.{kind}.exceptions")
    cls = {401: exceptions.UnauthorizedException,
           403: exceptions.ForbiddenException,
           404: exceptions.NotFoundException}.get(status)
    if cls is None:
        cls = (exceptions.ServiceException if 500 <= status <= 599
               else exceptions.ApiException)
    exc = cls(status=status, reason=reason)
    exc.body = json.dumps(body) if body is not None else None
    return exc


def _sdk_207(kind, body):
    """A 207 answer exactly as the SDK hands it back: deserialized by the
    package's own ApiClient into BatchResponseSimplePublicObjectWithErrors.
    The SDK raises only outside 200–299, so a 207 is RETURNED, never raised."""
    import importlib
    client = importlib.import_module(f"hubspot.crm.{kind}.api_client").ApiClient()
    return client._ApiClient__deserialize(
        body, "BatchResponseSimplePublicObjectWithErrors")


_READS = (("contacts", "read_contact_names"), ("companies", "read_company_names"))


def test_29_a_name_read_retries_what_can_recover_and_tells_missing_ids_from_a_refusal(monkeypatch):
    """Every case runs for contacts AND for companies, each raising the
    exception classes of its OWN SDK package."""
    import hubspot.crm.companies.exceptions as companies_exc
    import hubspot.crm.contacts.exceptions as contacts_exc

    monkeypatch.setattr(hp, "INITIAL_BACKOFF_SECONDS", 0)
    # The premise of review round 2: two unrelated classes.
    assert companies_exc.ApiException is not contacts_exc.ApiException
    assert not issubclass(companies_exc.ForbiddenException, contacts_exc.ApiException)
    assert set(hp.HUBSPOT_API_EXCEPTIONS) >= {contacts_exc.ApiException,
                                              companies_exc.ApiException}

    for kind, reader in _READS:
        read = getattr(hp, reader)
        props = ({"firstname": "Lina"} if kind == "contacts" else {"name": "Acme"})
        found = m163c._Obj({"id": "1", "properties": props})
        ok = ({"1": {"first_name": "Lina", "last_name": None}}
              if kind == "contacts" else {"1": "Acme"})

        def err(status, reason, body=None, _kind=kind):
            return _api_error(status, reason, body, kind=_kind)

        # 5xx, 429, a timeout and a transport error are retried with backoff.
        for failures in ([err(500, "Internal Server Error")] * 2,
                         [err(503, "Service Unavailable"), TimeoutError("read timed out")],
                         [ConnectionError("connection reset")] * 2,
                         [err(429, "Too Many Requests")] * 2):
            client, api = _scripted_client([*failures, [found]], kind)
            assert read(["1", "2"], client=client) == ok, kind
            assert api.calls == 3, kind

        # ... and given up on after MAX_RETRIES, as a FAILED read with status.
        client, api = _scripted_client([err(502, "Bad Gateway")] * 5, kind)
        with pytest.raises(hp.HubSpotRetryableError) as failed:
            read(["1"], client=client)
        assert api.calls == hp.MAX_RETRIES and "502" in str(failed.value)
        assert not isinstance(failed.value, hp.HubSpotNameReadRefused)
        client, api = _scripted_client([TimeoutError("read timed out")] * 5, kind)
        with pytest.raises(hp.HubSpotRetryableError):
            read(["1"], client=client)
        assert api.calls == hp.MAX_RETRIES

        # A 404 that NAMES the requested ids as not found is an ANSWER: those
        # ids were not returned. One call; not a failed batch; not retried.
        not_found = {"status": "error", "category": "OBJECT_NOT_FOUND",
                     "message": "Could not get some objects",
                     "context": {"ids": ["1", "2"]}}
        client, api = _scripted_client([err(404, "Not Found", not_found)], kind)
        assert read(["1", "2"], client=client) == {}, kind
        assert api.calls == 1, kind

        # A 207 is not raised by the SDK at all: it comes back as a result
        # list beside its errors. The found id is returned; the other is not.
        answer = _sdk_207(kind, {
            "status": "COMPLETE",
            "results": [{"id": "1", "properties": props,
                         "createdAt": "2026-01-01T00:00:00Z",
                         "updatedAt": "2026-01-01T00:00:00Z", "archived": False}],
            "numErrors": 1,
            "errors": [{"status": "error", "category": "OBJECT_NOT_FOUND",
                        "message": "not found", "context": {"ids": ["2"]},
                        "links": {}, "errors": []}],
            "startedAt": "2026-01-01T00:00:00Z",
            "completedAt": "2026-01-01T00:00:01Z"})
        assert type(answer).__name__ == "BatchResponseSimplePublicObjectWithErrors"
        assert type(answer).__module__.startswith(f"hubspot.crm.{kind}.")
        client, api = _scripted_client([answer], kind)
        assert read(["1", "2"], client=client) == ok, kind
        assert api.calls == 1

        # A 404 that names NO requested id is not an answer about them.
        for body in (None, {"message": "Not Found"},
                     {"category": "OBJECT_NOT_FOUND", "context": {"ids": ["777"]}}):
            client, api = _scripted_client([err(404, "Not Found", body)], kind)
            with pytest.raises(hp.HubSpotNameReadRefused) as refused:
                read(["1", "2"], client=client)
            assert refused.value.status == 404 and api.calls == 1, kind

        # Any other 4xx — a missing scope above all — is refused at once, with
        # the status in the message, and never retried.
        for status, reason in ((403, "Forbidden"), (401, "Unauthorized"),
                               (400, "Bad Request")):
            client, api = _scripted_client([err(status, reason)] * 3, kind)
            with pytest.raises(hp.HubSpotNameReadRefused) as refused:
                read(["1"], client=client)
            assert refused.value.status == status, kind
            assert api.calls == 1, (kind, status)
            assert f"HTTP {status}" in str(refused.value)

        # An answer that is not a result list is a failed read, never "no names".
        client, api = _scripted_client([None], kind)
        with pytest.raises(hp.HubSpotRetryableError):
            read(["1"], client=client)

    # The fixture's own guard: the companies API cannot raise a contacts
    # exception here any more — the substitution that hid the defect.
    client, api = _scripted_client([_api_error(403, "Forbidden", kind="contacts")],
                                   "companies")
    with pytest.raises(_FixtureMisuse, match="never raises"):
        hp.read_company_names(["c1"], client=client)


@needs_pg
def test_30_a_missing_scope_fails_the_step_with_its_http_status_and_stuck_ids_do_not(db, _no_pacing, monkeypatch):
    from scheduler import incremental_sync as sync
    from services import hubspot_name_backfill_service as names

    monkeypatch.setattr(hp, "INITIAL_BACKOFF_SECONDS", 0)
    cid = m163c._cid
    portal = _HonestHubSpot([m163c._contact(n) for n in range(6)])
    m163c._load(portal, NOW - timedelta(hours=30))
    db.execute("UPDATE hubspot_contact_funnel SET names_read_at = NULL")
    _associate(db, [("d1", "c1"), ("d2", "c2"), ("d3", "c3")])

    # The token may not read these objects: HubSpot refuses every batch, with
    # each API's OWN ForbiddenException.
    client, api = _scripted_client(
        [_api_error(403, "Forbidden", kind="contacts")] * 9, "contacts")
    out = names.run_contact_name_backfill(now=NOW, client=client, batch_size=2)
    assert out["status"] == "failed"
    assert "HTTP 403" in out["error"] and out["refused_http_status"] == 403
    # One call, although three batches were due: the rest would be refused too.
    assert api.calls == 1 and out["batches_requested"] == 1
    assert len(_unread(db)) == 6
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel "
                     "WHERE names_last_asked_at IS NOT NULL") == 0

    client, api = _scripted_client(
        [_api_error(403, "Forbidden", kind="companies")] * 9, "companies")
    out = names.run_company_name_sync(now=NOW, client=client, batch_size=2)
    assert out["status"] == "failed" and "HTTP 403" in out["error"]
    assert out["refused_http_status"] == 403
    # ONE call: not retried, and the second batch is not asked.
    assert api.calls == 1 and out["batches_requested"] == 1
    assert db.scalar("SELECT COUNT(*) FROM hubspot_company_name") == 0
    # What the operator reads in the run's error message names the status.
    errors: list = []
    with monkeypatch.context() as patched:
        patched.setattr(names, "run_company_name_sync", lambda: out)
        sync._sync_names(label=sync.LABEL_COMPANY_NAMES,
                         runner="run_company_name_sync", errors=errors)
    assert "'failed'" in errors[0] and "HTTP 403 (Forbidden)" in errors[0]

    # A batch made only of ids HubSpot does not have is ANSWERED (a 404 that
    # names them): the step is green, they are marked, and they are not asked
    # again tomorrow — a batch of stuck ids must not fail every run.
    def stuck(ids, kind):
        return _api_error(404, "Not Found", {
            "status": "error", "category": "OBJECT_NOT_FOUND",
            "message": "not found", "context": {"ids": list(ids)}}, kind=kind)
    queue = [cid(n) for n in (5, 4, 3, 2, 1, 0)]
    client, api = _scripted_client(
        [stuck(queue[i:i + 2], "contacts") for i in (0, 2, 4)], "contacts")
    out = names.run_contact_name_backfill(now=NOW, client=client, batch_size=2)
    assert out["status"] == "success" and api.calls == 3
    assert (out["batches_answered"], out["batches_failed"]) == (3, 0)
    assert out["returned"] == 0 and out["not_returned"] == 6
    assert out["progress"] == {"available": True, "read": 0, "not_returned": 6,
                               "never_asked": 0, "total": 6, "complete": True}
    client, api = _scripted_client([], "contacts")
    out = names.run_contact_name_backfill(now=NOW + timedelta(days=1),
                                          client=client, batch_size=2)
    assert api.calls == 0 and out["status"] == "success"

    # The same for companies, with the companies package's NotFoundException:
    # one call per batch, answered, and each id stamped as asked-not-returned.
    client, api = _scripted_client(
        [stuck(["c1", "c2"], "companies"), stuck(["c3"], "companies")], "companies")
    out = names.run_company_name_sync(now=NOW, client=client, batch_size=2)
    assert out["status"] == "success" and api.calls == 2
    assert (out["batches_answered"], out["batches_failed"]) == (2, 0)
    assert out["returned"] == 0 and out["not_returned"] == 3
    assert {r["company_id"]: (r["name"], r["read_at"], r["last_asked_at"])
            for r in db.rows("SELECT * FROM hubspot_company_name")} == {
        c: (None, None, NOW) for c in ("c1", "c2", "c3")}
    client, api = _scripted_client([], "companies")
    out = names.run_company_name_sync(now=NOW + timedelta(days=1), client=client,
                                      batch_size=2)
    assert api.calls == 0 and out["status"] == "success"

    # A mixed 207 from the companies API: the found company is written, the
    # other becomes not-returned.
    db.execute("DELETE FROM hubspot_company_name")
    answer = _sdk_207("companies", {
        "status": "COMPLETE",
        "results": [{"id": "c1", "properties": {"name": "Acme Freight"},
                     "createdAt": "2026-01-01T00:00:00Z",
                     "updatedAt": "2026-01-01T00:00:00Z", "archived": False}],
        "numErrors": 1,
        "errors": [{"status": "error", "category": "OBJECT_NOT_FOUND",
                    "message": "not found", "context": {"ids": ["c2"]},
                    "links": {}, "errors": []}],
        "startedAt": "2026-01-01T00:00:00Z",
        "completedAt": "2026-01-01T00:00:01Z"})
    client, api = _scripted_client([answer, [m163c._Obj(
        {"id": "c3", "properties": {"name": "Gamma"}})]], "companies")
    out = names.run_company_name_sync(now=NOW, client=client, batch_size=2)
    assert out["status"] == "success" and api.calls == 2
    assert out["returned"] == 2 and out["not_returned"] == 1
    assert {r["company_id"]: (r["name"], r["read_at"], r["last_asked_at"])
            for r in db.rows("SELECT * FROM hubspot_company_name")} == {
        "c1": ("Acme Freight", NOW, None), "c2": (None, None, NOW),
        "c3": ("Gamma", NOW, None)}


@needs_pg
def test_31_the_leads_customers_won_block_is_a_registered_parity_consumer_of_the_deals_page_figures(db, monkeypatch):
    """Review m4: the Leads block and the Deals page must show one number.

    Both are built over ONE ledger fixture for the same window. They read the
    same canonical verdict, so customers and closed-won deals agree in every
    state — published, withheld, unavailable.
    """
    from services import canonical_contract
    from services import cross_page_parity_service as parity
    from services import dashboard_deals_service as deals_page

    # No spend is stored here: the account time zone every window block carries is
    # read from stored spend, so it is supplied.
    monkeypatch.setattr(canonical_contract, "configured_account_time_zone",
                        lambda: "Europe/London")
    consumer = parity.CONSUMER_LEADS_CUSTOMERS_WON
    assert (consumer, "customers.value") in parity.METRIC_IDENTITIES["customers"]["consumers"]
    assert (consumer, "closed_won_deals.value") in \
        parity.METRIC_IDENTITIES["closed_won_deals"]["consumers"]
    assert consumer in parity.CERTIFIED_CONSUMERS

    for kwargs in ({}, {"companies": {"d3": []}}, {"scan_age_hours": 24 * 30},
                   {"companies": False},
                   {"sync_state": fresh_sync_state(NOW, hours_ago=120)}):
        _ledger(monkeypatch, **kwargs)
        won = _won()
        # PR-ADS-178B: Dashboard → Deals publishes {value, status, reason}
        # (the Pipeline builder's KPIs).
        kpis = deals_page.build_dashboard_deals(WINDOW, now=NOW)["kpis"]
        assert won["customers"]["value"] == kpis["customers"]["value"], kwargs
        assert won["customers"]["status"] == kpis["customers"]["status"], kwargs
        assert won["customers"]["reason"] == kpis["customers"]["reason"], kwargs
        assert won["closed_won_deals"]["value"] == kpis["closed_won_deals"]["value"], kwargs
    _ledger(monkeypatch)
    won = _won()
    assert (won["customers"]["value"], won["closed_won_deals"]["value"]) == (
        WON_COMPANIES, WON_DEALS)

    # The provenance the parity audit checks, against the registry.
    for metric in ("customers", "closed_won_deals"):
        spec = parity.METRIC_IDENTITIES[metric]
        contract = won["metric_truth"][metric]
        signature = parity._window_signature(won, WINDOW)[0]
        assert signature is not None
        assert parity._contract_problem(contract, spec, metric, signature,
                                        consumer=consumer, window=WINDOW) is None
        assert parity._dig(won, dict(spec["consumers"])[consumer]) == \
            won[metric]["value"]
    # And the audit itself, end to end: it builds the Leads block beside the
    # Deals page, reads the same two numbers from both, and finds nothing
    # wrong with the Leads consumer.
    outcome = parity.audit_window(WINDOW, now=NOW)
    by_metric = {m["metric"]: m for m in outcome["metrics"]}
    for metric, expected in (("customers", WON_COMPANIES),
                             ("closed_won_deals", WON_DEALS)):
        readings = {r["consumer"]: r for r in by_metric[metric]["readings"]}
        assert readings[consumer]["value"] == expected
        assert readings[consumer]["contract_problem"] is None
        assert readings["dashboard/deals"]["value"] == expected
    assert not [v for v in outcome["violations"]
                if v.get("consumer") == consumer], outcome["violations"]

    # A withheld customer count declares itself not ready, with the reason.
    _ledger(monkeypatch, companies={"d3": []})
    withheld = _won()
    assert withheld["metric_truth"]["customers"]["truth_status"] == "not_ready"
    assert withheld["metric_truth"]["customers"]["unavailable_reason"] == \
        cwt.R_CUSTOMER_IDENTITY
    assert withheld["metric_truth"]["closed_won_deals"]["truth_status"] == "ready"


_STAGE_QUALIFIED = re.compile(r"customer stage", re.I)
_CUSTOMER_WORD = re.compile(r"\bcustomers?\b", re.I)


def _names_a_customer(text) -> bool:
    """Does ``text`` say "customer(s)" other than as "Customer stage"?"""
    return bool(_CUSTOMER_WORD.search(_STAGE_QUALIFIED.sub("", str(text or ""))))


@needs_pg
def test_32_the_overview_lifecycle_panels_never_call_the_customer_stage_customers(db):
    """Review M1. The Overview's Lifecycle Activity and Lifecycle Cohort
    panels render the same stage event the Leads page does. Built from a real
    overview payload over the real funnel, and rendered by the page's own
    functions: no stage label, conversion target or chip may read as
    "customers" unless it says "Customer stage"."""
    def moved(n):
        day = 1 + n % 20
        return m163c._contact(n, **{
            "lifecyclestage": "customer",
            "createdate": "2026-07-01T08:00:00Z",
            "hs_v2_date_entered_lead": f"2026-07-{day:02d}T10:00:00Z",
            "hs_v2_date_entered_marketingqualifiedlead": f"2026-07-{day:02d}T11:00:00Z",
            "hs_v2_date_entered_salesqualifiedlead": f"2026-08-{day:02d}T10:00:00Z",
            "hs_v2_date_entered_opportunity": f"2026-08-{day:02d}T12:00:00Z",
            "hs_v2_date_entered_customer": (
                f"2026-09-{day:02d}T10:00:00Z" if n % 2 == 0 else None)})
    portal = _HonestHubSpot([moved(n) for n in range(12)])
    m163c._load(portal, NOW - timedelta(hours=3))

    from analysis import crm_lifecycle
    from services.dashboard_overview_service import build_dashboard_overview
    assert crm_lifecycle.EVENT_LABELS[crm_lifecycle.EVENT_CUSTOMER] == \
        "Moved to Customer stage"

    payload = build_dashboard_overview(window=WINDOW, now=NOW)
    activity, cohort = payload["lifecycle_activity"], payload["lifecycle_cohort"]
    assert activity["available"] is True and cohort["available"] is True
    by_event = {s["event"]: s for s in activity["stages"]}
    assert by_event["customer"]["entered"] == 6
    assert by_event["customer"]["label"] == "Moved to Customer stage"
    assert by_event["customer"]["activity_label"] == \
        "Contacts moved to the Customer stage during this period"
    into = [c for c in activity["conversions"] if c["to_event"] == "customer"]
    assert into and into[0]["available"] is True
    assert into[0]["to_label"] == "Customer stage"
    assert [s["label"] for s in cohort["stages"]][-1] == "Moved to Customer stage"

    # Every label the two blocks publish.
    labels = []
    for stage in activity["stages"]:
        labels += [stage["label"], stage["activity_label"]]
    for conversion in activity["conversions"]:
        labels += [conversion["from_label"], conversion["to_label"],
                   conversion["cohort_label"]]
    labels += [s["label"] for s in cohort["stages"]]
    labels += [cohort.get("anchor_label"), cohort.get("anchor_definition")]
    offenders = [text for text in labels if _names_a_customer(text)]
    assert offenders == [], offenders

    # ... and everything the page renders from them, tooltips included.
    out = _node({"activity": ["renderDashLifecycleActivity", [payload]],
                 "cohort": ["renderDashLifecycleCohort", [payload]]})
    for panel in ("activity", "cohort"):
        markup = out[panel]
        visible = _text(markup)
        assert "Moved to Customer stage" in visible, panel
        assert not _names_a_customer(visible), (panel, visible)
        for title in re.findall(r'title="([^"]*)"', markup):
            assert not _names_a_customer(html_lib.unescape(title)), (panel, title)
    assert "Contacts moved to the Customer stage during this period" in \
        _text(out["activity"])
    assert "went on to enter Customer stage." in html_lib.unescape(out["activity"])
    # The guard itself: the old label is caught, the new one is not.
    assert _names_a_customer("Lifecycle Customers")
    assert _names_a_customer("Opportunities → Customers")
    assert not _names_a_customer("Moved to Customer stage")
    assert not _names_a_customer("→ Customer stage")
