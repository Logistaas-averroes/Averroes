"""
tests/test_pr_ads_154c_f3_country_sql_and_revenue_gate.py

PR-ADS-154C-F3 — the two findings the post-merge production parity audit made.

Finding 1: Dashboard Countries publishes the SQLs it could assign to a REAL
country — 14 against the mart's 16 in the production current quarter — while
declaring them to be the full campaign-attributable population. The missing 2
are not lost; they are in the page's own residual bucket. The registry described
the wrong population, so a correct page and a correct mart were reported as
disagreeing.

Finding 2: over All Time, Overview, Revenue and the mart published
``$878,324.80`` while Channels, Campaigns, Countries and Deals published
"unavailable" — and all-source CUSTOMERS were 181 and identical everywhere. A
gate rejection cannot produce that shape: it blanks the count too. What produces
it is ``summarize_deals`` returning the sum of the deals whose currency WAS
proven as soon as one such deal exists — a partial known-dollar sum, published
as the business total, on the three consumers that read it directly. The other
four each already refused a partial sum under their own rule.

Both are proved here against the REAL builders, not by asserting the fix.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services import canonical_contract as contract_mod  # noqa: E402
from analysis import revenue_scope  # noqa: E402
from services import canonical_revenue_service as canonical_revenue  # noqa: E402
from services import cross_page_parity_service as parity  # noqa: E402
from tests.canonical_ledger_fixtures import (  # noqa: E402
    READY_SYNC_STATE, ledger_row, patch_canonical_ledger,
)
from tests.test_pr_ads_138_dashboard_countries import (  # noqa: E402
    NOW, _patch_durable,
)

#: Where each consumer publishes its own closed-won revenue headline. Overview,
#: Revenue, Channels, Deals and the mart publish the ALL-SOURCE total; Campaigns
#: publishes the campaign-attributable subset and Countries the country-attributed
#: one. Different questions, and every one of them is a sum over the same
#: canonical population — so a deal with no proven amount withholds all seven.
_REVENUE_PATHS = {
    "dashboard/overview": "kpis.closed_won_revenue_usd",
    "dashboard/revenue": "kpis.closed_won_revenue_usd",
    "dashboard/channels": "kpis.closed_won_revenue_usd",
    "dashboard/campaigns": "kpis.won_revenue_usd",
    "dashboard/countries": "kpis.won_revenue_usd",
    "dashboard/deals": "kpis.closed_won_revenue_usd",
    "revenue_decision_mart": "summary.won_revenue_usd",
}
_ALL_CONSUMERS = tuple(_REVENUE_PATHS)

#: The subset of those that publish the ALL-SOURCE business total.
_ALL_SOURCE_CONSUMERS = ("dashboard/overview", "dashboard/revenue",
                         "dashboard/channels", "dashboard/deals",
                         "revenue_decision_mart")


def _deal(idx, *, country="United States", campaign="Global Competitors",
          amount=1000.0):
    """One canonical won deal. ``amount=None`` = currency never proven."""
    proven = amount is not None
    return ledger_row(
        f"d{idx}", amount_raw=amount, revenue_usd=amount,
        currency_status="verified_usd" if proven else "unavailable",
        currency_reason="deal_currency_is_usd" if proven else "no_amount",
        campaign_name_raw=campaign, country_raw=country,
        acquisition_group="google_ads", attribution_status="attributed",
        gclid=f"g{idx}")


def _build_all(monkeypatch, window, rows, *, sync_state=None):
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, rows, sync_state=sync_state)
    return parity._build_consumers(window, NOW)


def _revenue_of(built: dict, consumers=None) -> dict:
    """Each consumer's own closed-won revenue headline."""
    return {name: parity._dig((built[name].get("payload") or {}), _REVENUE_PATHS[name])
            for name in (consumers or _REVENUE_PATHS)}


def _customers_of(built: dict) -> dict:
    out = {}
    for name, entry in built.items():
        node = ((entry.get("payload") or {}).get("summary")
                if name == "revenue_decision_mart"
                else (entry.get("payload") or {}).get("kpis")) or {}
        for key in ("customers", "total_customers", "closed_won_customers"):
            if key in node:
                out[name] = node[key]
                break
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Finding 1 — the Countries SQL identity
# ═════════════════════════════════════════════════════════════════════════════

def test_f3_1_countries_sqls_are_a_country_attributed_identity():
    """Requirement 1. The real-country total is its OWN identity and scope."""
    spec = parity.METRIC_IDENTITIES["country_attributed_sqls"]
    assert spec["scope"] == "country_attributed_sqls"
    # PR-ADS-154C-F3-F1 §3: the SQLs ORIGINATE in the canonical lead population
    # the decision mart aggregates and are then partitioned by country. Geo
    # decides the partition; it does not produce the SQL.
    assert spec["canonical_source"] == contract_mod.SOURCE_REVENUE_DECISION_MART
    assert spec["consumers"] == [("dashboard/countries", "kpis.sqls")]
    # Geo remains a PREREQUISITE, where a prerequisite belongs.
    assert spec["coverage_proof"] == parity.PROOF_COUNTRY_SQLS


def test_f3_2_countries_is_out_of_the_full_campaign_attributable_comparison():
    """Requirement 2. 14 was never a wrong answer to the mart's question — it was
    a right answer to a different one, and comparing them made the page look
    broken while hiding what the difference actually was."""
    consumers = {c for c, _ in
                 parity.METRIC_IDENTITIES["campaign_attributable_sqls"]["consumers"]}
    assert "dashboard/countries" not in consumers
    assert consumers == {"dashboard/overview", "dashboard/revenue",
                         "dashboard/campaigns", "dashboard/deals",
                         "revenue_decision_mart"}


def test_f3_3_the_residual_sqls_are_registered_and_published(monkeypatch):
    """Requirement 3. The residual is an audited identity, not a footnote."""
    spec = parity.METRIC_IDENTITIES["country_unattributed_residual_sqls"]
    assert spec["consumers"] == [("dashboard/countries", "residual.sqls")]

    built = _build_all(monkeypatch, "current_quarter", [_deal(1)])
    countries = built["dashboard/countries"]["payload"]
    assert "sqls" in (countries.get("residual") or {})
    block = (countries.get(contract_mod.METRIC_TRUTH_KEY) or {})
    assert block["country_attributed_sqls"]["scope"] == "country_attributed_sqls"
    assert (block["country_unattributed_residual_sqls"]["scope"]
            == "country_unattributed_residual_sqls")


def test_f3_4_country_sqls_plus_residual_equal_the_campaign_attributable_total(
        monkeypatch):
    """Requirement 4 — the production arithmetic, 14 + 2 = 16.

    The lead rows below put 14 qualified leads in a real country and 2 with no
    country at all, exactly the shape the production quarter had.
    """
    import db.revenue_repository as repo

    _patch_durable(monkeypatch)
    rows = ([{"campaign_name": "Global Competitors", "country": "United States",
              "status_category": "qualified", "has_gclid": True}] * 14
            + [{"campaign_name": "Global Competitors", "country": None,
                "status_category": "qualified", "has_gclid": True}] * 2)
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: {"available": True, "rows": list(rows),
                                      "event_date_safe": True,
                                      "missing_contact_created_at_count": 0,
                                      "excluded_non_paid_count": 0,
                                      "excluded_pseudo_campaign_count": 0})
    patch_canonical_ledger(monkeypatch, [_deal(1)])

    out = parity.audit_window("current_quarter", now=NOW)
    total = next(m for m in out["metrics"] if m["metric"] == "campaign_attributable_sqls")
    real = next(m for m in out["metrics"] if m["metric"] == "country_attributed_sqls")
    residual = next(m for m in out["metrics"]
                    if m["metric"] == "country_unattributed_residual_sqls")

    assert total["value"] == 16
    assert real["value"] == 14
    assert residual["value"] == 2
    assert real["value"] + residual["value"] == total["value"]

    rule = next(c for c in out["conservation"]
                if c["total"] == "campaign_attributable_sqls")
    assert rule["status"] == "conserved", rule
    assert parity.V_CONSERVATION_BROKEN not in out["violation_codes"]


def test_f3_5_a_broken_conservation_equation_fails_the_audit(monkeypatch):
    """Requirement 5. Registering the split is not enough; the parts must add up.

    Hiding one residual SQL inside the real-country total is the specific way a
    reader would be lied to, and it must not be a way to make the audit green.
    """
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(1)])
    real = parity._build_consumers

    def inflated(window, now):
        built = real(window, now)
        c = built["dashboard/countries"]["payload"]
        c["kpis"]["sqls"] = (c["kpis"]["sqls"] or 0) + 1     # swallow the residual
        return built
    monkeypatch.setattr(parity, "_build_consumers", inflated)

    out = parity.audit_window("current_quarter", now=NOW)
    assert out["ok"] is False
    assert parity.V_CONSERVATION_BROKEN in out["violation_codes"]
    rule = next(c for c in out["conservation"]
                if c["total"] == "campaign_attributable_sqls")
    assert rule["status"] == "broken"


def test_f3_6_a_withheld_component_is_not_a_conservation_failure(monkeypatch):
    """An honest outage must not be reported as broken arithmetic."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(1)])
    real = parity._build_consumers

    def withheld(window, now):
        built = real(window, now)
        built["dashboard/countries"]["payload"]["kpis"]["sqls"] = None
        return built
    monkeypatch.setattr(parity, "_build_consumers", withheld)

    out = parity.audit_window("current_quarter", now=NOW)
    rule = next(c for c in out["conservation"]
                if c["total"] == "campaign_attributable_sqls")
    assert rule["status"] == "not_evaluable"
    assert parity.V_CONSERVATION_BROKEN not in out["violation_codes"]


@pytest.mark.parametrize("other", ["lifecycle_sqls", "source_group_sqls"])
def test_f3_7_country_sqls_are_never_compared_with_the_other_populations(other):
    """Requirements 6 and 7. Five SQL populations, five questions."""
    pairs = {frozenset((d["left"], d["right"])) for d in parity.DISTINCT_BY_DESIGN}
    assert frozenset(("country_attributed_sqls", other)) in pairs

    scopes = {k: parity.METRIC_IDENTITIES[k]["scope"] for k in
              ("campaign_attributable_sqls", "country_attributed_sqls",
               "country_unattributed_residual_sqls", "source_group_sqls",
               "lifecycle_sqls")}
    assert len(set(scopes.values())) == 5, scopes


def test_f3_8_no_sql_is_assigned_to_a_guessed_country(monkeypatch):
    """Requirement 8. A country-less SQL stays in the residual, labelled."""
    import db.revenue_repository as repo

    _patch_durable(monkeypatch)
    rows = ([{"campaign_name": "Global Competitors", "country": "United States",
              "status_category": "qualified", "has_gclid": True}] * 14
            + [{"campaign_name": "Global Competitors", "country": None,
                "status_category": "qualified", "has_gclid": True}] * 2)
    monkeypatch.setattr(repo, "fetch_lead_quality",
                        lambda s, e: {"available": True, "rows": list(rows),
                                      "event_date_safe": True,
                                      "missing_contact_created_at_count": 0,
                                      "excluded_non_paid_count": 0,
                                      "excluded_pseudo_campaign_count": 0})
    patch_canonical_ledger(monkeypatch, [_deal(1)])

    from services.dashboard_countries_service import build_dashboard_countries
    page = build_dashboard_countries("current_quarter", now=NOW)

    assert page["residual"]["sqls"] == 2
    assert page["kpis"]["sqls"] == 14
    assert page["kpis"]["sqls_scope"] == "country_attributed_sqls"
    assert page["kpis"]["sqls_residual"] == 2
    # Every real country row is a real country; none absorbed the residual.
    named = [c for c in page["countries"] if not c.get("is_residual")]
    assert sum((c.get("sqls") or 0) for c in named) == 14
    assert all(c.get("country_code") for c in named)


def test_f3_9_bounded_windows_keep_their_existing_values(monkeypatch):
    """Requirement 9. The identity correction changes labels, not arithmetic."""
    built = _build_all(monkeypatch, "current_quarter",
                       [_deal(1), _deal(2), _deal(3, country=None, campaign=None)])
    mart = built["revenue_decision_mart"]["payload"]["summary"]
    assert mart["sqls"] == 25 and mart["leads"] == 105
    assert built["dashboard/countries"]["payload"]["kpis"]["sqls"] == 25


# ═════════════════════════════════════════════════════════════════════════════
# Finding 2 — one canonical revenue availability decision
# ═════════════════════════════════════════════════════════════════════════════

def test_f3_10_a_strict_rejection_stops_every_consumer_publishing_revenue(
        monkeypatch):
    """Requirement 10. And it blanks customers too — which is why a rejection
    CANNOT be what produced the production signature, where customers were 181
    and identical on every page."""
    broken = dict(READY_SYNC_STATE, bootstrap_status="in_progress")
    built = _build_all(monkeypatch, "all_time",
                       [_deal(i) for i in range(3)], sync_state=broken)

    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    assert base["available"] is False
    assert base["reason"] == canonical_revenue.REASON_COVERAGE_NOT_PROVEN

    assert all(v is None for v in _revenue_of(built).values()), _revenue_of(built)
    assert all(v is None for v in _customers_of(built).values()), _customers_of(built)


def test_f3_11_the_zero_counts_in_a_rejected_read_are_not_measurements(monkeypatch):
    """`TOTAL_WON_DEALS=0, UNSUMMABLE_DEALS=0` beside `LEDGER_AVAILABLE=False`
    proves nothing about the deals: a rejected read carries no population at all,
    so a diagnostic counting its rows counts an empty list."""
    _patch_durable(monkeypatch)
    broken = dict(READY_SYNC_STATE, bootstrap_status="in_progress")
    patch_canonical_ledger(monkeypatch, [_deal(i) for i in range(3)],
                           sync_state=broken)
    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    assert base["available"] is False
    assert len(base.get("deals") or []) == 0     # three real rows exist
    verdict = canonical_revenue.revenue_total_publishable(base)
    assert verdict["publishable"] is False
    assert verdict["currency_unavailable_deals"] is None   # unknown, not zero


@pytest.mark.parametrize("consumer", _ALL_CONSUMERS)
def test_f3_12_one_unproven_amount_withholds_the_total_on_every_consumer(
        monkeypatch, consumer):
    """Requirements 11, 12 and 13 — the production signature, reproduced.

    Three deals with proven amounts and one without. Before this PR the mart,
    Overview and Revenue published the sum of the three; Channels, Campaigns,
    Countries and Deals published nothing. Now nobody publishes a partial sum,
    and the COUNT — which is complete whatever the amounts are — survives.
    """
    rows = [_deal(0), _deal(1), _deal(2), _deal(3, amount=None)]
    built = _build_all(monkeypatch, "all_time", rows)

    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    assert base["available"] is True                 # the population IS readable
    verdict = canonical_revenue.revenue_total_publishable(base)
    assert verdict["publishable"] is False
    assert verdict["reason"] == canonical_revenue.REASON_REVENUE_INCOMPLETE
    assert verdict["currency_unavailable_deals"] == 1

    assert _revenue_of(built)[consumer] is None, _revenue_of(built)
    assert _customers_of(built)[consumer] == 4, _customers_of(built)


def test_f3_13_the_mart_no_longer_publishes_a_partial_known_dollar_sum(monkeypatch):
    """The exact defect: `summarize_deals` still reports the partial sum as a
    diagnostic, and the mart no longer promotes it to the business total."""
    rows = [_deal(0), _deal(1), _deal(2), _deal(3, amount=None)]
    built = _build_all(monkeypatch, "all_time", rows)

    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    raw = canonical_revenue.summarize_deals(base["deals"], "all_source")
    assert raw["known_revenue_usd"] == 3000.0      # the proven sum still exists
    assert raw["revenue_usd"] is None              # but it is not the total
    assert raw["currency_unavailable_deals"] == 1
    assert raw["currency_complete"] is False

    summary = built["revenue_decision_mart"]["payload"]["summary"]
    assert summary["won_revenue_usd"] is None      # but is not the total
    assert summary["customers"] == 4
    assert summary["revenue_available"] is True    # population readable
    assert summary["revenue_total_available"] is False
    assert (summary["revenue_total_unavailable_reason"]
            == canonical_revenue.REASON_REVENUE_INCOMPLETE)
    assert summary["currency_unavailable_deals"] == 1


def test_f3_14_the_attributed_subset_follows_the_same_rule(monkeypatch):
    """A partial sum is not the subset's total either — the ROAS numerator
    included."""
    rows = [_deal(0), _deal(1), _deal(2), _deal(3, amount=None)]
    built = _build_all(monkeypatch, "all_time", rows)
    summary = built["revenue_decision_mart"]["payload"]["summary"]
    assert summary["attributed_won_revenue_usd"] is None
    assert summary["attributed_revenue_total_available"] is False
    assert summary["attributed_customers"] is not None


def test_f3_15_an_unavailable_contract_cannot_carry_a_numeric_value(monkeypatch):
    """Requirement 16. Every consumer's revenue contract must agree with its own
    published value."""
    rows = [_deal(0), _deal(1), _deal(3, amount=None)]
    built = _build_all(monkeypatch, "all_time", rows)
    for name, entry in built.items():
        payload = entry.get("payload") or {}
        blocks = payload.get(contract_mod.METRIC_TRUTH_KEY) or {}
        for key, block in blocks.items():
            node = payload.get("summary") if name == "revenue_decision_mart" \
                else payload.get("kpis") or {}
            if block.get("truth_status") == contract_mod.TRUTH_READY:
                continue
            # A not-ready contract must not sit beside a published figure for
            # the identity it describes.
            spec = parity.METRIC_IDENTITIES.get(key)
            if not spec:
                continue
            path = next((p for c, p in spec["consumers"] if c == name), None)
            if path is None:
                continue
            assert parity._dig(payload, path) is None, (name, key)


def test_f3_16_publishing_over_an_unavailable_source_is_its_own_violation(
        monkeypatch):
    """Requirement 17. Two consumers can AGREE perfectly and both be publishing a
    figure neither is entitled to — which is what $878,324.80 did on three pages.
    Reading that as a value mismatch is how it survived."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(1)])
    real = parity._build_consumers

    def bypassing(window, now):
        built = real(window, now)
        for name in ("dashboard/overview", "revenue_decision_mart"):
            p = built[name]["payload"]
            p[contract_mod.METRIC_TRUTH_KEY]["closed_won_revenue_usd"][
                "truth_status"] = contract_mod.TRUTH_UNAVAILABLE
            p[contract_mod.METRIC_TRUTH_KEY]["closed_won_revenue_usd"][
                "unavailable_reason"] = canonical_revenue.REASON_REVENUE_INCOMPLETE
        return built
    monkeypatch.setattr(parity, "_build_consumers", bypassing)

    out = parity.audit_window("current_quarter", now=NOW)
    assert out["ok"] is False
    assert parity.V_VALUE_OVER_UNAVAILABLE_SOURCE in out["violation_codes"]
    # NOT collapsed into a generic mismatch: every consumer still agrees.
    assert parity.V_VALUE_MISMATCH not in out["violation_codes"]
    entry = next(m for m in out["metrics"] if m["metric"] == "closed_won_revenue_usd")
    assert entry["status"] == "published_over_unavailable_source"
    detail = " ".join(v.get("detail", "") for v in out["violations"])
    assert canonical_revenue.REASON_REVENUE_INCOMPLETE in detail


def test_f3_17_a_complete_healthy_population_certifies_all_time(monkeypatch):
    """Requirement 18. The gate is not weakened: a genuinely complete bootstrap
    with a healthy incremental and every amount proven publishes All Time."""
    built = _build_all(monkeypatch, "all_time", [_deal(i) for i in range(3)])
    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    assert base["available"] is True
    assert canonical_revenue.revenue_total_publishable(base)["publishable"] is True
    published = _revenue_of(built, _ALL_SOURCE_CONSUMERS)
    assert all(v == 3000.0 for v in published.values()), published


@pytest.mark.parametrize("field,value", [
    ("bootstrap_status", "in_progress"),          # requirement 19
    ("bootstrap_completed_at", None),
    ("last_status", "failed"),                    # requirement 20
    ("last_sync_mode", "bootstrap"),
])
def test_f3_18_an_unproven_sync_state_remains_blocked(monkeypatch, field, value):
    """Requirements 19 and 20. Nothing here was loosened."""
    _patch_durable(monkeypatch)
    state = dict(READY_SYNC_STATE)
    state[field] = value
    patch_canonical_ledger(monkeypatch, [_deal(1)], sync_state=state)
    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    assert base["available"] is False
    assert base["reason"] == canonical_revenue.REASON_COVERAGE_NOT_PROVEN
    assert base.get("violation_codes")


def test_f3_19_the_coverage_gate_does_not_look_at_the_window(monkeypatch):
    """Part 4's first branch, ruled out by inspection AND by behaviour.

    `check_sync_coverage` reads only the sync-state row. It has no window
    parameter and no notion of a lower bound, so All Time cannot be rejected for
    being unbounded — it is accepted or rejected on exactly the same evidence as
    every other window. That is why the fix is not in the gate.
    """
    from services.revenue_reconciliation_service import check_sync_coverage
    import inspect

    params = list(inspect.signature(check_sync_coverage).parameters)
    assert params == ["sync_res"], params

    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(1)])
    verdicts = {w: canonical_revenue.load_won_deals(w, now=NOW)["available"]
                for w in ("current_quarter", "last_quarter", "last_6_months",
                          "ytd", "all_time")}
    assert set(verdicts.values()) == {True}, verdicts

    broken = dict(READY_SYNC_STATE, bootstrap_status="in_progress")
    patch_canonical_ledger(monkeypatch, [_deal(1)], sync_state=broken)
    verdicts = {w: canonical_revenue.load_won_deals(w, now=NOW)["available"]
                for w in ("current_quarter", "last_quarter", "last_6_months",
                          "ytd", "all_time")}
    assert set(verdicts.values()) == {False}, verdicts


# ═════════════════════════════════════════════════════════════════════════════
# Static guards
# ═════════════════════════════════════════════════════════════════════════════

_PRODUCTION_DIRS = ("services", "api", "scheduler", "analysis", "db")

#: Modules allowed to bypass the readiness gate. Read-only diagnostics only, and
#: each must SAY it is unenforced in its own output. A page never appears here.
_REQUIRE_READY_ALLOWLIST = {
    "scripts/audit_canonical_revenue_shadow.py",
}


def _production_files():
    for d in _PRODUCTION_DIRS:
        for f in sorted((_ROOT / d).rglob("*.py")):
            yield f


def test_f3_20_no_production_consumer_disables_the_readiness_gate():
    """Requirement 14. `require_ready=False` is how a page would quietly read a
    population the merge gate would have rejected."""
    offenders = []
    for f in _production_files():
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "load_won_deals":
                continue
            for kw in node.keywords:
                if kw.arg == "require_ready" and not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is True):
                    rel = str(f.relative_to(_ROOT))
                    if rel not in _REQUIRE_READY_ALLOWLIST:
                        offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "production code must not weaken the canonical readiness gate: "
        + ", ".join(offenders))


def test_f3_21_all_time_cannot_be_dropped_from_the_audited_windows():
    """Requirement 21. Making an audit green by not asking is not an answer."""
    from analysis.business_windows import WINDOW_KEYS
    assert "all_time" in WINDOW_KEYS

    src = (_ROOT / "scripts" / "audit_cross_page_canonical_parity.py").read_text()
    assert "list(WINDOW_KEYS)" in src
    tree = ast.parse((_ROOT / "services" / "cross_page_parity_service.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "audit_all_windows")
    names = {getattr(n, "id", None) for n in ast.walk(fn)}
    assert "WINDOW_KEYS" in names


def test_f3_22_production_revenue_reads_no_retired_ledger():
    """Requirement 15. GCLID, deal-source and local-JSON revenue may not stand in
    for an unavailable canonical population."""
    banned = ("fetch_revenue_deals", "deal_source_attribution",
              "gclid_attribution", "campaign_performance.json")
    offenders = []
    for name in ("dashboard_overview_service", "dashboard_revenue_service",
                 "dashboard_channels_service", "dashboard_campaigns_service",
                 "dashboard_countries_service", "dashboard_deals_service",
                 "revenue_decision_mart", "canonical_revenue_service"):
        f = _ROOT / "services" / f"{name}.py"
        tree = ast.parse(f.read_text())
        docstrings = {n for scope in ast.walk(tree)
                      if isinstance(scope, (ast.Module, ast.FunctionDef,
                                            ast.AsyncFunctionDef, ast.ClassDef))
                      and scope.body and isinstance(scope.body[0], ast.Expr)
                      and isinstance(scope.body[0].value, ast.Constant)
                      and isinstance(scope.body[0].value.value, str)
                      for n in [scope.body[0].value]}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node not in docstrings:
                if any(b in node.value for b in banned):
                    offenders.append(f"{name}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in banned:
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, offenders


def test_f3_23_the_revenue_decision_is_made_in_exactly_one_place():
    """One implementation, so four private copies cannot drift apart again."""
    assert hasattr(canonical_revenue, "revenue_total_publishable")
    assert "revenue_total_publishable" in canonical_revenue.__all__
    verdict = canonical_revenue.revenue_total_publishable(
        {"available": False, "reason": "x", "violation_codes": ["c"]})
    assert verdict == {"publishable": False, "reason": "x", "detail": None,
                       "violation_codes": ["c"], "currency_unavailable_deals": None}


# ═════════════════════════════════════════════════════════════════════════════
# Reporting
# ═════════════════════════════════════════════════════════════════════════════

def test_f3_24_customers_survive_a_withheld_revenue_total(monkeypatch):
    """Requirement 22. `won_deals` is complete whatever the amounts are, so
    blanking the count would be its own fabrication."""
    built = _build_all(monkeypatch, "all_time",
                       [_deal(0), _deal(1), _deal(2, amount=None)])
    customers = _customers_of(built)
    assert set(customers.values()) == {3}, customers
    assert set(_revenue_of(built).values()) == {None}, _revenue_of(built)


def test_f3_25_the_pending_redesign_pages_stay_visible_and_uncertified(monkeypatch):
    """Requirement 24."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(1)])
    out = parity.audit_window("current_quarter", now=NOW)
    named = {u["consumer"] for u in out["uncertified_consumers"]}
    assert named == {"platform_evidence", "lead_intelligence"}
    assert not (named & parity.CERTIFIED_CONSUMERS)


def test_f3_26_both_output_formats_report_the_same_findings(monkeypatch, capsys):
    """Requirement 25. A finding that only one format shows is a finding the
    reader of the other format does not have."""
    import json
    import scripts.audit_cross_page_canonical_parity as cli

    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(1)])
    real = parity._build_consumers

    def broken(window, now):
        built = real(window, now)
        c = built["dashboard/countries"]["payload"]
        c["kpis"]["sqls"] = (c["kpis"]["sqls"] or 0) + 1
        return built
    monkeypatch.setattr(parity, "_build_consumers", broken)
    monkeypatch.setattr("db.connection.init_pool", lambda: None)

    monkeypatch.setattr(sys, "argv", ["p", "--window", "current_quarter"])
    rc_text = cli.main()
    text = capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["p", "--window", "current_quarter", "--json"])
    rc_json = cli.main()
    payload = json.loads(capsys.readouterr().out)

    assert rc_text == rc_json == 1
    assert parity.V_CONSERVATION_BROKEN in payload["violation_codes"]
    assert parity.V_CONSERVATION_BROKEN in text
    assert "conservation" in text
    assert set(payload["violation_codes"]) <= set(
        c for c in payload["violation_codes"] if c in text), \
        "a code in the JSON that the human-readable output never shows"


# ═════════════════════════════════════════════════════════════════════════════
# Both CLI formats, against the six fixtures PR-ADS-154C-F3 names
# ═════════════════════════════════════════════════════════════════════════════

_ORIGINAL_BUILD = parity._build_consumers

#: A canonical contact funnel that HAS been synced. The reference fixture leaves
#: it unwired, which legitimately makes the five lifecycle identities
#: `canonical_source_unavailable`; scenario 1 supplies it so "fully healthy"
#: means exit 0 rather than "green apart from a known gap".
def _with_synced_lifecycle(built):
    ov = built["dashboard/overview"]["payload"]
    ov["kpis"].update({f"lifecycle_{s}": n for s, n in
                       (("leads", 105), ("mqls", 60), ("sqls", 30),
                        ("opportunities", 8), ("customers", 4))})
    ov["kpis"]["lifecycle_available"] = True
    ov["lifecycle_funnel"] = {"available": True, "status": "reconciled",
                              "sync": {"available": True,
                                       "bootstrap_status": "complete"}}
    for stage in parity.LIFECYCLE_STAGES:
        ov[contract_mod.METRIC_TRUTH_KEY][f"lifecycle_{stage}"]["truth_status"] = \
            contract_mod.TRUTH_READY
    return built


def _cli_both_formats(monkeypatch, capsys, label, *, rows=None, sync_state=None,
         leads=None, mutate=None):
    import json
    import scripts.audit_cross_page_canonical_parity as cli
    import db.revenue_repository as repo

    _patch_durable(monkeypatch)
    if leads is not None:
        monkeypatch.setattr(repo, "fetch_lead_quality",
                            lambda s, e: {"available": True, "rows": list(leads),
                                          "event_date_safe": True,
                                          "missing_contact_created_at_count": 0,
                                          "excluded_non_paid_count": 0,
                                          "excluded_pseudo_campaign_count": 0})
    patch_canonical_ledger(monkeypatch, rows if rows is not None else [_deal(1)],
                           sync_state=sync_state)
    monkeypatch.setattr("db.connection.init_pool", lambda: None)
    # Capture the ORIGINAL builder, not whatever a previous scenario in this
    # same test patched in — chained mutations would attribute one
    # scenario's violation to the next.
    pristine = _ORIGINAL_BUILD
    monkeypatch.setattr(parity, "_build_consumers",
                        (lambda w, n: mutate(pristine(w, n))) if mutate
                        else pristine)
    monkeypatch.setattr(sys, "argv", ["p", "--window", "current_quarter"])
    rc_t = cli.main(); text = capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["p", "--window", "current_quarter", "--json"])
    rc_j = cli.main(); payload = json.loads(capsys.readouterr().out)
    # The two formats are two renderings of one audit; an exit code that
    # depended on which one you asked for would be its own parity defect.
    assert rc_t == rc_j, (label, rc_t, rc_j)
    assert (rc_j == 0) is bool(payload["ok"]), (label, rc_j, payload["ok"])
    return payload, text


_QUALIFIED_REAL = [{"campaign_name": "Global Competitors", "country": "United States",
                    "status_category": "qualified", "has_gclid": True}] * 14
_QUALIFIED_NONE = [{"campaign_name": "Global Competitors", "country": None,
                    "status_category": "qualified", "has_gclid": True}] * 2


def test_f3_27_both_formats_against_the_six_required_fixtures(monkeypatch, capsys):
    # 1. fully healthy — the only scenario that may exit 0.
    p, t = _cli_both_formats(monkeypatch, capsys, "1. fully healthy",
                             mutate=_with_synced_lifecycle)
    assert p["ok"] is True, p["violation_codes"]
    assert "EXPLICITLY UNCERTIFIED" in t

    # 2. country SQL residual
    p, t = _cli_both_formats(
        monkeypatch, capsys, "2. country SQL residual (14 + 2 = 16)",
        leads=_QUALIFIED_REAL + _QUALIFIED_NONE, mutate=_with_synced_lifecycle)
    c = next(x for x in p["results"][0]["conservation"])
    assert (c["total_value"], c["part_values"]) == (16, [14, 2]), c
    assert c["status"] == "conserved"
    assert "conservation" in t
    assert p["ok"] is True, p["violation_codes"]

    # 3. strict All-Time canonical rejection
    p, t = _cli_both_formats(monkeypatch, capsys, "3. strict canonical rejection",
                sync_state=dict(READY_SYNC_STATE, bootstrap_status="in_progress"))
    assert "canonical_source_unavailable" in p["violation_codes"]

    # 4. completed bootstrap, every amount proven
    p, t = _cli_both_formats(monkeypatch, capsys, "4. completed bootstrap, amounts proven",
                rows=[_deal(0), _deal(1)])
    assert "value_published_while_source_unavailable" not in p["violation_codes"]

    # 5. deliberate consumer bypass
    def bypass(built):
        from services import canonical_contract as cc
        p_ = built["dashboard/overview"]["payload"]
        p_[cc.METRIC_TRUTH_KEY]["closed_won_revenue_usd"]["truth_status"] = "unavailable"
        return built
    p, t = _cli_both_formats(monkeypatch, capsys, "5. deliberate consumer bypass", mutate=bypass)
    assert "value_published_while_source_unavailable" in p["violation_codes"]
    assert "value_published_while_source_unavailable" in t

    # 6. deliberate fallback attempt
    def fallback(built):
        built["dashboard/deals"]["payload"]["legacy_fallback_used"] = True
        return built
    p, t = _cli_both_formats(monkeypatch, capsys, "6. deliberate fallback attempt", mutate=fallback)
    assert "legacy_source_supplied_production_total" in p["violation_codes"]
    assert "legacy_source_supplied_production_total" in t


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-154C-F3-F1 — the shared decision is the PRODUCTION decision
#
# The first cut of F3 added `revenue_total_publishable` and then did not call
# it: `get_scope_ladder` never consulted it, and the mart re-derived the same
# condition in a private `_revenue_complete`. A shared rule nobody calls is a
# fifth copy of the rule, not a consolidation of the other four — and drift
# between copies is what let the mart diverge from Channels, Campaigns,
# Countries and Deals in the first place.
# ═════════════════════════════════════════════════════════════════════════════

def test_f3f1_1_the_scope_ladder_carries_the_publishable_verdict(monkeypatch):
    """Every scope states whether its total may be published, and why not."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(0), _deal(1), _deal(2, amount=None)])

    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    ladder = canonical_revenue.get_scope_ladder(base=base)
    assert ladder["available"] is True

    for scope, block in ladder["scopes"].items():
        assert "revenue_total_available" in block, scope
        # The verdict must equal what the public function says for that scope —
        # one rule, not two that happen to agree today.
        expected = canonical_revenue.revenue_total_publishable(base, scope)
        assert block["revenue_total_available"] == expected["publishable"], scope
        assert block["revenue_total_unavailable_reason"] == expected["reason"], scope
        assert block["revenue_total_violation_codes"] == expected["violation_codes"], scope

    all_source = ladder["scopes"][revenue_scope.SCOPE_ALL_SOURCE]
    assert all_source["revenue_total_available"] is False
    assert (all_source["revenue_total_unavailable_reason"]
            == canonical_revenue.REASON_REVENUE_INCOMPLETE)
    assert all_source["revenue_total_violation_codes"] == [
        canonical_revenue.V_CURRENCY_UNPROVEN_DEALS]
    # The arithmetic is untouched: the proven sum is still reported beside it,
    # under a name that cannot be mistaken for a complete total.
    assert all_source["known_revenue_usd"] == 2000.0
    assert all_source["revenue_usd"] is None
    assert all_source["currency_unavailable_deals"] == 1


def test_f3f1_2_a_complete_population_publishes_on_every_scope(monkeypatch):
    """The control — no scope withholds when every amount is proven."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(0), _deal(1)])
    ladder = canonical_revenue.get_scope_ladder(
        base=canonical_revenue.load_won_deals("all_time", now=NOW))
    assert all(b["revenue_total_available"] is True
               for b in ladder["scopes"].values()), ladder["scopes"]
    assert all(b["revenue_total_unavailable_reason"] is None
               for b in ladder["scopes"].values())


def test_f3f1_3_the_mart_reads_the_verdict_rather_than_re_deriving_it(monkeypatch):
    """Flip ONLY the ladder's verdict and the mart must follow it.

    The arithmetic is left saying the total is fine, so a mart that recomputed
    completeness from `currency_unavailable_deals` would publish the figure and
    fail here. This is the test the private `_revenue_complete` would not pass.
    """
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(0), _deal(1)])

    real_ladder = canonical_revenue.get_scope_ladder

    def withheld(*a, **kw):
        ladder = real_ladder(*a, **kw)
        for block in (ladder.get("scopes") or {}).values():
            block["revenue_total_available"] = False
            block["revenue_total_unavailable_reason"] = "a_reason_only_the_ladder_knows"
            # Deliberately left consistent-looking: zero unproven amounts.
            block["currency_unavailable_deals"] = 0
        return ladder
    monkeypatch.setattr(canonical_revenue, "get_scope_ladder", withheld)

    from services.revenue_decision_mart import build_revenue_decision_mart
    summary = build_revenue_decision_mart(
        window="all_time", view="campaign", now=NOW)["summary"]

    assert summary["won_revenue_usd"] is None
    assert summary["attributed_won_revenue_usd"] is None
    assert summary["revenue_total_available"] is False
    assert summary["revenue_total_unavailable_reason"] == "a_reason_only_the_ladder_knows"
    assert summary["customers"] == 2          # the count is not governed by it


#: Production modules that read `currency_unavailable_deals`, and what each does
#: with it. DISCLOSURE means it copies the count into a response so a reader can
#: see how big the gap is; DECISION means it decides whether a revenue total may
#: be published. Exactly one module is allowed to make the decision.
#:
#: A new reader has to be classified here, which is the point: the mart's private
#: `_revenue_complete` was a DECISION that nobody had to declare.
_COUNT_READERS = {
    "services/canonical_revenue_service.py": "decision",
    "services/revenue_decision_mart.py": "disclosure",
    "services/canonical_unit_economics_service.py": "disclosure",
    "services/revenue_attribution_service.py": "disclosure",
    "services/source_attribution_service.py": "disclosure",
}


def test_f3f1_4_only_one_module_decides_revenue_completeness():
    """The static guard, anchored on the field the rule is made of.

    Copying the count into a response is fine and several pages do it. Deciding
    from it whether to publish a total is what may happen in exactly one place,
    and a module that starts doing so has to be reclassified here first.
    """
    unregistered = []
    for f in _production_files():
        rel = str(f.relative_to(_ROOT))
        tree = ast.parse(f.read_text())
        if any(isinstance(n, ast.Constant)
               and n.value == "currency_unavailable_deals" for n in ast.walk(tree)):
            if rel not in _COUNT_READERS:
                unregistered.append(rel)
    assert not unregistered, (
        "these modules read `currency_unavailable_deals` without being "
        "classified as disclosure or decision: " + ", ".join(unregistered))
    assert [k for k, v in _COUNT_READERS.items() if v == "decision"] == [
        "services/canonical_revenue_service.py"]

    # Every module classified `disclosure` must be exactly that: no branch may
    # test the raw count. This is what the mart's `_revenue_complete` did.
    for rel, kind in _COUNT_READERS.items():
        if kind != "disclosure":
            continue
        tree = ast.parse((_ROOT / rel).read_text())
        for node in ast.walk(tree):
            # Only the CONDITION, never the branch bodies. `x.get("currency_
            # unavailable_deals") if available else None` is disclosure guarded
            # by availability, which is exactly what a page should do.
            if isinstance(node, (ast.If, ast.IfExp)):
                tests = [node.test]
            elif isinstance(node, ast.BoolOp):
                tests = list(node.values)
            else:
                continue
            names = {n.value for t in tests for n in ast.walk(t)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            assert "currency_unavailable_deals" not in names, (
                f"{rel}:{node.lineno} branches on the raw count instead of "
                "reading the ladder's `revenue_total_publishable` verdict")


def test_f3f1_5_the_public_function_and_the_ladder_cannot_drift():
    """One implementation, structurally: both paths reach `_revenue_total_verdict`."""
    tree = ast.parse((_ROOT / "services" / "canonical_revenue_service.py").read_text())
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    called_by_public = {getattr(c.func, "id", getattr(c.func, "attr", None))
                        for c in ast.walk(fns["revenue_total_publishable"])
                        if isinstance(c, ast.Call)}
    assert "_revenue_total_verdict" in called_by_public

    called_by_ladder = {getattr(c.func, "id", getattr(c.func, "attr", None))
                        for c in ast.walk(fns["get_scope_ladder"])
                        if isinstance(c, ast.Call)}
    assert "revenue_total_publishable" in called_by_ladder, (
        "get_scope_ladder must consult the shared decision, not describe it")


def test_f3f1_6_the_diagnostic_sum_no_longer_occupies_the_totals_field(monkeypatch):
    """§2. `known_revenue_usd` is the proven sum; `revenue_usd` is the TOTAL.

    Two names, because one name for two facts is how a partial figure came to be
    consumed as a complete one.
    """
    rows = [_deal(0), _deal(1), _deal(2, amount=None)]
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, rows)
    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    summary = canonical_revenue.summarize_deals(base["deals"], "all_source")

    assert summary["known_revenue_usd"] == 2000.0
    assert summary["revenue_usd"] is None
    assert summary["currency_complete"] is False
    assert summary["currency_unavailable_deals"] == 1
    assert summary["won_deals"] == 3          # the count is untouched


def test_f3f1_7_a_complete_population_reports_the_same_figure_in_both(monkeypatch):
    """When nothing is unproven the two agree — the split adds a distinction,
    not a discrepancy."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(0), _deal(1)])
    base = canonical_revenue.load_won_deals("all_time", now=NOW)
    summary = canonical_revenue.summarize_deals(base["deals"], "all_source")
    assert summary["known_revenue_usd"] == summary["revenue_usd"] == 2000.0
    assert summary["currency_complete"] is True


@pytest.mark.parametrize("consumer,path", list(_REVENUE_PATHS.items()))
def test_f3f1_8_no_executive_kpi_can_consume_the_diagnostic_sum(
        monkeypatch, consumer, path):
    """§2's acceptance test: trace every executive revenue KPI through the
    canonical publishability decision.

    The proven sum is 2000.0 and is reachable in the payload; no headline may be
    equal to it, because the total over this population is unknown.
    """
    built = _build_all(monkeypatch, "all_time",
                       [_deal(0), _deal(1), _deal(2, amount=None)])
    value = parity._dig(built[consumer].get("payload") or {}, path)
    assert value is None, f"{consumer}.{path} published {value!r}"
    assert value != 2000.0


def test_f3f1_9_every_withheld_revenue_contract_carries_reason_and_codes(
        monkeypatch):
    """§4. The reason and the machine-readable codes come from the canonical
    helper, so six pages cannot describe one refusal six ways."""
    built = _build_all(monkeypatch, "all_time",
                       [_deal(0), _deal(1), _deal(2, amount=None)])

    checked = 0
    for name in ("dashboard/overview", "dashboard/revenue", "dashboard/deals",
                 "revenue_decision_mart"):
        blocks = ((built[name].get("payload") or {})
                  .get(contract_mod.METRIC_TRUTH_KEY) or {})
        block = blocks.get("closed_won_revenue_usd")
        assert block, name
        assert block["truth_status"] == contract_mod.TRUTH_NOT_READY, name
        assert (block.get("unavailable_reason")
                == canonical_revenue.REASON_REVENUE_INCOMPLETE), name
        assert (block.get("violation_codes")
                == [canonical_revenue.V_CURRENCY_UNPROVEN_DEALS]), name
        checked += 1
    assert checked == 4


def test_f3f1_10_the_contract_helper_normalizes_violation_codes():
    """§4. Deterministic, de-duplicated, and a bare string is one code."""
    resolved = {"key": "ytd", "start_date": "2026-01-01",
                "end_date": "2026-06-01", "timezone": "Europe/London"}

    block = contract_mod.metric_contract(
        metric="m", data_source="x", scope="s", resolved=resolved,
        truth_status=contract_mod.TRUTH_NOT_READY, unavailable_reason="why",
        violation_codes=["b", "a", "b", None, ""])
    assert block["violation_codes"] == ["a", "b"]

    assert contract_mod.metric_contract(
        metric="m", data_source="x", scope="s", resolved=resolved,
        violation_codes="solo")["violation_codes"] == ["solo"]

    # Absent rather than an empty list, so a ready contract stays clean.
    assert "violation_codes" not in contract_mod.metric_contract(
        metric="m", data_source="x", scope="s", resolved=resolved)


def test_f3f1_11_revenue_incomplete_is_a_registered_unavailable_reason():
    """§4. A reason the product can return must be a reason the product names."""
    assert (canonical_revenue.REASON_REVENUE_INCOMPLETE
            in canonical_revenue.ALL_UNAVAILABLE_REASONS)


def test_f3f1_12_the_audit_reports_the_contracts_own_violation_codes(monkeypatch):
    """§4. The parity report surfaces the exact codes the contract published,
    rather than a description the audit invented."""
    built = _build_all(monkeypatch, "all_time",
                       [_deal(0), _deal(1), _deal(2, amount=None)])
    monkeypatch.setattr(parity, "_build_consumers", lambda w, n: built)
    out = parity.audit_window("all_time", now=NOW)

    entry = next(m for m in out["metrics"] if m["metric"] == "closed_won_revenue_usd")
    for reading in entry["readings"]:
        if reading["value"] is None and reading.get("truth_status"):
            assert reading["violation_codes"] == [
                canonical_revenue.V_CURRENCY_UNPROVEN_DEALS], reading["consumer"]
            assert (reading["unavailable_reason"]
                    == canonical_revenue.REASON_REVENUE_INCOMPLETE)


def test_f3f1_13_country_sql_readiness_needs_lead_proof_and_country_coverage(
        monkeypatch):
    """§3. Two dependencies, and naming only one would certify the figure on
    half its evidence."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, [_deal(1)])
    spec = parity.METRIC_IDENTITIES["country_attributed_sqls"]

    # Both proven.
    consumers = parity._build_consumers("current_quarter", NOW)
    assert parity._coverage_proven(consumers, spec)[0] is True

    # Lead population unproven → not proven, whatever geo says.
    mart = consumers["revenue_decision_mart"]["payload"]
    mart["readiness"]["lead_metrics_status"] = "db_empty"
    proven, detail = parity._coverage_proven(consumers, spec)
    assert proven is False and "lead population" in detail

    # Lead population proven, country reconciliation not → still not proven.
    mart["readiness"]["lead_metrics_status"] = "db"
    mart["spend_truth"]["country_spend_status"] = "mismatch"
    proven, detail = parity._coverage_proven(consumers, spec)
    assert proven is False and "country reconciliation" in detail


def test_f3f1_14_the_focused_ci_step_runs_the_f3_suite():
    """§5. The full suite passing is not the requested focused gate."""
    import yaml

    workflows = sorted((_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflow files found"

    suite = "tests/test_pr_ads_154c_f3_country_sql_and_revenue_gate.py"
    found = []
    for wf in workflows:
        doc = yaml.safe_load(wf.read_text())
        for job in (doc.get("jobs") or {}).values():
            for step in (job.get("steps") or []):
                run = step.get("run") or ""
                name = step.get("name") or ""
                if suite in run and "Contract tests" in name:
                    found.append((wf.name, name))
    assert found, (
        f"{suite} is not executed by any focused 'Contract tests' step")
    # And the step says so, so a reader of the CI log knows what ran.
    assert any("154C-F3" in name for _, name in found), found
