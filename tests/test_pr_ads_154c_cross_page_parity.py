"""
tests/test_pr_ads_154c_cross_page_parity.py

PR-ADS-154C — cross-page canonical parity, and the prohibition on silent legacy
fallback.

What this suite holds
─────────────────────
  §1   one window anchor: the same window key resolves to the same range for
       every consumer, including across the account's midnight;
  §2   same metric + same window + same filters ⇒ identical values;
  §3   total business revenue and attributed revenue stay separate;
  §4   missing canonical data fails closed;
  §5   a legacy fallback cannot activate silently;
  §6   Windsor cannot reach a production metric consumer;
  §7   GCLID-only ledgers cannot determine total revenue;
  §8   legacy HubSpot association failures cannot move canonical truth readiness;
  §9   `reconciled_with_residual` remains an accepted geo-ready state;
  §10  the audit exits non-zero on a deliberate violation;
  §11  the static guard pins every legacy read a production module performs.

The static guard (§11) is the part that keeps this true after the PR. It carries
a classified registry of every legacy-table read in production code; a new one
fails the test until it is classified, and none may be classified as supplying a
production total.

Read-only: no external platform and no database is touched by any test here.

Run with:
    python -m pytest tests/test_pr_ads_154c_cross_page_parity.py -v
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis.business_windows import WINDOW_KEYS, resolve_window, resolve_window_in_zone  # noqa: E402
from services import canonical_contract as contract  # noqa: E402
from services import canonical_contract as contract_mod  # noqa: E402
from services import cross_page_parity_service as parity  # noqa: E402
import services.google_ads_geo_sync_service as geo  # noqa: E402
import scheduler.incremental_sync as sched  # noqa: E402


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — one window anchor
# ═════════════════════════════════════════════════════════════════════════════

#: 23:30 UTC on 30 June, during British Summer Time: the account day is already
#: 1 July, and 1 July is in a different QUARTER. The single instant at which
#: every anchoring mistake becomes visible at once.
_MIDNIGHT_CROSSING = _at("2026-06-30T23:30:00")


#: What each window resolves to once the ACCOUNT day has rolled to 1 July while
#: UTC is still on 30 June. Spelled out per window rather than asserted as one
#: blanket value: `last_quarter` ends the day BEFORE the current quarter starts,
#: so its end date moves to 30 June exactly when the others move to 1 July, and a
#: single expected value would have been wrong for it.
_ACCOUNT_ANCHORED = {
    "current_quarter": ("2026-07-01", "2026-07-01"),   # rolled into Q3
    "last_quarter":    ("2026-04-01", "2026-06-30"),   # Q2, now complete
    "last_6_months":   ("2026-01-01", "2026-07-01"),
    "ytd":             ("2026-01-01", "2026-07-01"),
    "all_time":        (None, "2026-07-01"),
}


@pytest.mark.parametrize("window", WINDOW_KEYS)
def test_1_every_window_resolves_to_the_account_day_not_utc(window):
    """The account day wins, because spend is denominated in it.

    Google Ads reports a day's cost against the account's local calendar, so a
    business window anchored on UTC asks about a range the spend data does not
    have. Every window key moves at this instant — not only the quarterly ones.
    """
    utc_anchored = resolve_window(window, now=_MIDNIGHT_CROSSING)
    canonical = contract.resolve_canonical_window(
        window, now=_MIDNIGHT_CROSSING, account_time_zone="Europe/London")

    expected_start, expected_end = _ACCOUNT_ANCHORED[window]
    assert canonical["start_date"] == expected_start
    assert canonical["end_date"] == expected_end
    assert canonical["timezone"] == "Europe/London"
    # ...and the UTC anchor genuinely disagrees, which is the defect.
    assert (utc_anchored["start_date"], utc_anchored["end_date"]) != \
           (canonical["start_date"], canonical["end_date"])


def test_1b_the_quarter_boundary_is_where_this_mattered_most():
    """`current_quarter` named two different quarters under the two anchors."""
    utc_anchored = resolve_window("current_quarter", now=_MIDNIGHT_CROSSING)
    canonical = contract.resolve_canonical_window(
        "current_quarter", now=_MIDNIGHT_CROSSING, account_time_zone="Europe/London")
    assert utc_anchored["start_date"] == "2026-04-01"     # Q2
    assert canonical["start_date"] == "2026-07-01"        # Q3
    assert utc_anchored["start_date"] != canonical["start_date"]


def test_1c_an_unknown_or_missing_zone_falls_back_to_the_account_not_utc():
    """Falling back to UTC is what produced the divergence in the first place.

    A missing database row is not a reason to change which day it is.
    """
    from analysis.account_time import ACCOUNT_TZ
    for zone in (None, "", "Not/AZone"):
        resolved = resolve_window_in_zone("ytd", zone, now=_MIDNIGHT_CROSSING)
        assert resolved["end_date"] == "2026-07-01", f"zone={zone!r} fell back to UTC"
    assert resolve_window_in_zone("ytd", None, now=_MIDNIGHT_CROSSING)["timezone"] == ACCOUNT_TZ


def test_1d_every_production_consumer_uses_the_canonical_resolver():
    """Asserted on the AST across the dashboard and composed services.

    A page-local `resolve_window(window, now=now)` is the shape that drifted;
    a text search would trip on the comments explaining the migration, so the
    call graph is walked instead.
    """
    migrated = [
        "services/dashboard_overview_service.py", "services/dashboard_revenue_service.py",
        "services/dashboard_channels_service.py", "services/dashboard_campaigns_service.py",
        "services/dashboard_countries_service.py", "services/dashboard_deals_service.py",
        "services/revenue_decision_mart.py", "services/source_attribution_service.py",
    ]
    for rel in migrated:
        tree = ast.parse((_ROOT / rel).read_text())
        called = {getattr(n.func, "attr", getattr(n.func, "id", None))
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "resolve_canonical_window" in called, f"{rel} does not use the anchor"
        assert "resolve_window" not in called, (
            f"{rel} still resolves a window on its own clock")


# ═════════════════════════════════════════════════════════════════════════════
# §2/§3 — value parity within an identity; separation between identities
# ═════════════════════════════════════════════════════════════════════════════

#: Sentinel for an override that REMOVES a key rather than setting it.
_DELETE = object()

_WIN = {"key": "current_quarter", "label": "Current Quarter",
        "start_date": "2026-04-01", "end_date": "2026-06-22",
        "timezone": "Europe/London", "bounds": "inclusive_start_exclusive_end_utc"}


def _contract(metric, source, scope, **over):
    """A per-metric provenance block in the shape the services publish."""
    block = {
        "metric": metric, "data_source": source, "scope": scope,
        "truth_status": "ready",
        "window": _WIN["key"], "window_start": _WIN["start_date"],
        "window_end": _WIN["end_date"], "timezone": _WIN["timezone"],
        "customer_id": None, "currency": "USD", "fallback_used": False,
        "generated_at": "2026-06-22T12:00:00+00:00",
    }
    block.update(over)
    return block


def _payloads(**overrides):
    """Consumer payloads in the EXACT shape production publishes.

    PR-ADS-154C-F1 §1 asks for this specifically, and the reason is that the
    previous synthetic fixture hid a live defect: real dashboards expose
    `legacy_fallback_used` as a TOP-LEVEL boolean and `source_truth` as a
    STRING, while the fixture nested them as dictionaries. The audit's fallback
    guard checked `isinstance(block, dict)`, so it passed the fixture and could
    never fire on a real payload. A fixture shaped like the thing it stands in
    for is the only kind that can catch that.
    """
    _MART = contract_mod.SOURCE_REVENUE_DECISION_MART
    _BY_SOURCE = contract_mod.SOURCE_REVENUE_BY_SOURCE
    _SPEND = contract_mod.SOURCE_CANONICAL_SPEND
    _GEO = contract_mod.SOURCE_CANONICAL_GEO
    _FUNNEL = contract_mod.SOURCE_CANONICAL_FUNNEL

    def _mt(*specs):
        return {m: _contract(m, src, scope) for m, src, scope in specs}

    # The figures are the ones the reference fixture actually produces (see
    # tests/test_pr_ads_138_dashboard_countries.py): 13000 spend, 33000 all-source
    # revenue over 3 customers, 29000 over 2 for both the campaign- and
    # country-attributed subsets, 25 campaign-attributable SQLs against 0
    # source-group SQLs, 105 leads.
    base = {
        "dashboard/overview": {
            "window": dict(_WIN), "read_only": True,
            "source_truth": "revenue_decision_mart",       # a STRING, as in production
            "legacy_fallback_used": False,                 # TOP-LEVEL, as in production
            "google_ads_conversion_value_used": False,
            "kpis": {"google_ads_spend_usd": 13000.0,
                     "closed_won_revenue_usd": 33000.0,
                     "customers": 3, "sqls": 25, "leads": 105,
                     "lifecycle_leads": 105, "lifecycle_mqls": 60,
                     "lifecycle_sqls": 30, "lifecycle_opportunities": 8,
                     "lifecycle_customers": 4, "lifecycle_available": True},
            # The funnel reports `available` against an empty schema too, so the
            # proof is its own sync block, not the availability flag.
            "lifecycle_funnel": {"available": True, "status": "reconciled",
                                 "sync": {"available": True,
                                          "bootstrap_status": "complete"}},
            contract_mod.METRIC_TRUTH_KEY: _mt(
                ("google_ads_spend_usd", _SPEND, "google_ads_campaign_spend"),
                ("closed_won_revenue_usd", _MART, "all_source_business_revenue"),
                ("customers", _MART, "all_source_business_revenue"),
                ("campaign_attributable_sqls", _MART, "campaign_attributable_sqls"),
                ("campaign_attributable_leads", _MART, "campaign_attributable_leads"),
                *[(f"lifecycle_{s}", _FUNNEL, f"lifecycle_{s}")
                  for s in ("leads", "mqls", "sqls", "opportunities", "customers")])},
        "dashboard/revenue": {
            "window": dict(_WIN), "read_only": True,
            "source_truth": "hubspot_deal_ledger", "legacy_fallback_used": False,
            "kpis": {"closed_won_revenue_usd": 33000.0, "customers": 3, "sqls": 25},
            contract_mod.METRIC_TRUTH_KEY: _mt(
                ("closed_won_revenue_usd", _MART, "all_source_business_revenue"),
                ("customers", _MART, "all_source_business_revenue"),
                ("campaign_attributable_sqls", _MART, "campaign_attributable_sqls"))},
        "dashboard/channels": {
            "window": dict(_WIN), "read_only": True,
            "source_truth": "revenue_by_source_taxonomy",
            "legacy_fallback_used": False,
            # Channels renders the all-source totals from the source-group
            # taxonomy, so it names THAT canonical authority — and its SQL count
            # is a different population, registered under its own identity.
            "kpis": {"closed_won_revenue_usd": 33000.0, "total_customers": 3,
                     "total_sqls": 0},
            contract_mod.METRIC_TRUTH_KEY: _mt(
                ("closed_won_revenue_usd", _BY_SOURCE, "all_source_business_revenue"),
                ("customers", _BY_SOURCE, "all_source_business_revenue"),
                ("source_group_sqls", _BY_SOURCE, "source_group_sqls"))},
        "dashboard/campaigns": {
            "window": dict(_WIN), "read_only": True,
            "source_truth": "revenue_decision_mart_campaign_view",
            "legacy_fallback_used": False,
            "kpis": {"verified_spend_usd": 13000.0, "won_revenue_usd": 29000.0,
                     "customers": 2, "sqls": 25},
            contract_mod.METRIC_TRUTH_KEY: _mt(
                ("google_ads_spend_usd", _SPEND, "google_ads_campaign_spend"),
                ("campaign_attributed_won_revenue_usd", _MART,
                 "campaign_attributable_revenue"),
                ("campaign_attributed_customers", _MART,
                 "campaign_attributable_revenue"),
                ("campaign_attributable_sqls", _MART, "campaign_attributable_sqls"))},
        "dashboard/countries": {
            "window": dict(_WIN), "read_only": True,
            "source_truth": "revenue_decision_mart_country_view",
            "legacy_fallback_used": False,
            # `verified_spend_usd` here sums the per-country rows — the
            # country-ATTRIBUTED denominator, not the full-account one, even when
            # the two happen to agree.
            "kpis": {"won_revenue_usd": 29000.0, "customers": 2,
                     "verified_spend_usd": 13000.0, "sqls": 25},
            contract_mod.METRIC_TRUTH_KEY: _mt(
                ("country_attributed_won_revenue_usd", _GEO,
                 "country_attributed_revenue"),
                ("country_attributed_customers", _GEO, "country_attributed_revenue"),
                ("country_attributed_spend_usd", _GEO, "country_attributed_spend"),
                ("campaign_attributable_sqls", _MART, "campaign_attributable_sqls"))},
        "dashboard/deals": {
            "window": dict(_WIN), "read_only": True,
            "source_truth": "hubspot_deal_ledger", "legacy_fallback_used": False,
            "kpis": {"closed_won_revenue_usd": 33000.0, "closed_won_customers": 3,
                     "sqls": 25},
            contract_mod.METRIC_TRUTH_KEY: _mt(
                ("closed_won_revenue_usd", _MART, "all_source_business_revenue"),
                ("customers", _MART, "all_source_business_revenue"),
                ("campaign_attributable_sqls", _MART, "campaign_attributable_sqls"))},
        "revenue_decision_mart": {
            "window": dict(_WIN),
            # Coverage proof is per metric: campaign spend needs campaign AND FX
            # coverage; country spend needs an accepted reconciliation; country
            # revenue needs that AND the deal ledger; revenue needs the ledger.
            "spend_truth": {"campaign_spend_status": "verified",
                            "fx_status": "verified",
                            "country_spend_status": "reconciled_with_residual"},
            # `db` = the lead query returned rows. `db_empty` is an unmeasured
            # window, not a quarter with no leads, and is not proof of either.
            "readiness": {"lead_metrics_ready": True, "lead_metrics_status": "db"},
            "summary": {"spend_usd": 13000.0, "won_revenue_usd": 33000.0,
                        "customers": 3, "revenue_available": True,
                        "attributed_won_revenue_usd": 29000.0,
                        "attributed_customers": 2, "sqls": 25, "leads": 105},
            contract_mod.METRIC_TRUTH_KEY: _mt(
                ("google_ads_spend_usd", _SPEND, "google_ads_campaign_spend"),
                ("closed_won_revenue_usd", _MART, "all_source_business_revenue"),
                ("customers", _MART, "all_source_business_revenue"),
                ("campaign_attributed_won_revenue_usd", _MART,
                 "campaign_attributable_revenue"),
                ("campaign_attributed_customers", _MART,
                 "campaign_attributable_revenue"),
                ("campaign_attributable_sqls", _MART, "campaign_attributable_sqls"),
                ("campaign_attributable_leads", _MART,
                 "campaign_attributable_leads"))},
    }
    for name, patch in overrides.items():
        target = base[name.replace("__", "/")]
        for path, value in patch.items():
            node = target
            *parents, leaf = path.split(".")
            for p in parents:
                node = node.setdefault(p, {})
            if value is _DELETE:
                node.pop(leaf, None)
            else:
                node[leaf] = value
    return base




def _audit_with(monkeypatch, payloads):
    monkeypatch.setattr(parity, "_build_consumers",
                        lambda window, now: {k: {"payload": v, "error": None}
                                             for k, v in payloads.items()})
    monkeypatch.setattr(parity, "resolve_canonical_window",
                        lambda w, now=None: {"key": w, "label": w,
                                             "start_date": "2026-04-01",
                                             "end_date": "2026-06-22",
                                             "timezone": "Europe/London"})
    return parity.audit_window("current_quarter")


def test_2_identical_values_across_consumers_pass(monkeypatch):
    out = _audit_with(monkeypatch, _payloads())
    assert out["ok"] is True
    assert out["violations"] == []
    assert {m["status"] for m in out["metrics"]} == {"identical"}


def test_2b_one_consumer_disagreeing_is_a_violation(monkeypatch):
    """The whole point: a page publishing a different number for the same
    question fails, with both readings named."""
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={"kpis.closed_won_revenue_usd": 31000.0}))
    assert out["ok"] is False
    assert parity.V_VALUE_MISMATCH in out["violation_codes"]
    mismatch = next(m for m in out["metrics"] if m["metric"] == "closed_won_revenue_usd")
    assert mismatch["status"] == "mismatch"
    assert {r["value"] for r in mismatch["readings"]} == {33000.0, 31000.0}


def test_2c_parity_is_exact_not_within_a_tolerance(monkeypatch):
    """A penny apart is still two answers to one question.

    A tolerance here answers "are these close enough to ignore", which is how
    disagreements survive. Reconciliation between two DIFFERENT sources has a
    tolerance; two renderings of the SAME canonical figure do not.
    """
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={"kpis.closed_won_revenue_usd": 33000.01}))
    assert out["ok"] is False
    assert parity.V_VALUE_MISMATCH in out["violation_codes"]


def test_2d_unanimous_agreement_over_unproven_coverage_is_not_parity(monkeypatch):
    """The failure mode where an audit passes because everything is equally wrong.

    On a window with no verified coverage the canonical spend query returns zero
    rows and every consumer renders the same 0.0 — perfect agreement about a
    number nobody measured. Zero rows over an unproven range is the absence of a
    measurement, not a measured zero, exactly as PR-ADS-153F established for geo.
    """
    out = _audit_with(monkeypatch, _payloads(
        revenue_decision_mart={"spend_truth.campaign_spend_status": "incomplete",
                               "summary.spend_usd": 0.0},
        dashboard__overview={"kpis.google_ads_spend_usd": 0.0},
        dashboard__campaigns={"kpis.verified_spend_usd": 0.0}))
    assert out["ok"] is False
    assert parity.V_AGREEMENT_ON_UNPROVEN_COVERAGE in out["violation_codes"]
    spend = next(m for m in out["metrics"] if m["metric"] == "google_ads_spend_usd")
    assert spend["status"] == "unproven"
    # Revenue is sourced from the deal ledger, not from spend coverage, so it is
    # unaffected — the rule applies where it is relevant, not everywhere.
    revenue = next(m for m in out["metrics"] if m["metric"] == "closed_won_revenue_usd")
    assert revenue["status"] == "identical"


def test_3_total_revenue_and_country_attributed_revenue_are_separate_identities(monkeypatch):
    """33000 total vs 29000 country-attributed is information, not a defect.

    They are registered as distinct by design and never compared, so the audit
    passes while both numbers stay visible and differently labelled.
    """
    out = _audit_with(monkeypatch, _payloads())
    assert out["ok"] is True
    total = next(m for m in out["metrics"] if m["metric"] == "closed_won_revenue_usd")
    country = next(m for m in out["metrics"]
                   if m["metric"] == "country_attributed_won_revenue_usd")
    assert total["value"] == 33000.0
    assert country["value"] == 29000.0
    assert total["scope"] != country["scope"]
    pairs = {(d["left"], d["right"]) for d in out["distinct_by_design"]}
    assert ("closed_won_revenue_usd", "country_attributed_won_revenue_usd") in pairs


def test_3b_every_distinct_pair_names_both_sides_and_a_reason():
    known = set(parity.METRIC_IDENTITIES)
    for entry in parity.DISTINCT_BY_DESIGN:
        assert entry["reason"].strip(), entry
        # At least one side must be a metric this audit actually reads; the other
        # may be a named identity the registry does not yet compare.
        assert entry["left"] in known or entry["right"] in known, entry


def test_3c_labels_distinguish_attributed_revenue_from_total_revenue():
    """"Total Revenue" must never label the Google Ads-attributed subset."""
    for key, spec in parity.METRIC_IDENTITIES.items():
        label = spec["label"].lower()
        if spec["scope"] == "all_source_business_revenue":
            assert "all sources" in label or "all-source" in label, key
        if spec["scope"] == "country_attributed_revenue":
            assert "country" in label, key
            assert not label.startswith("total"), key


# ═════════════════════════════════════════════════════════════════════════════
# §1/§4/§5 — window drift, fail-closed, and no silent fallback
# ═════════════════════════════════════════════════════════════════════════════

def test_1e_consumers_resolving_different_ranges_is_a_violation(monkeypatch):
    """Same window name, different dates — agreement about a different question."""
    out = _audit_with(monkeypatch, _payloads(
        dashboard__countries={"window.end_date": "2026-06-23"}))
    assert out["ok"] is False
    assert parity.V_WINDOW_MISMATCH in out["violation_codes"]


def test_4_a_metric_no_consumer_can_publish_is_reported_unavailable(monkeypatch):
    """Fail closed: absence is stated, never filled in from somewhere else."""
    payloads = _payloads()
    payloads["dashboard/countries"]["kpis"].pop("won_revenue_usd")
    out = _audit_with(monkeypatch, payloads)
    assert out["ok"] is False
    assert parity.V_SOURCE_UNAVAILABLE in out["violation_codes"]
    country = next(m for m in out["metrics"]
                   if m["metric"] == "country_attributed_won_revenue_usd")
    assert country["status"] == "unavailable"
    assert country["value"] is None      # never a fabricated 0


def test_4b_a_consumer_that_raises_is_a_violation_not_a_silent_skip(monkeypatch):
    monkeypatch.setattr(parity, "_build_consumers", lambda window, now: {
        "dashboard/overview": {"payload": None, "error": "RuntimeError: boom"}})
    monkeypatch.setattr(parity, "resolve_canonical_window",
                        lambda w, now=None: {"key": w, "start_date": None,
                                             "end_date": None, "timezone": "Europe/London"})
    out = parity.audit_window("ytd")
    assert out["ok"] is False
    assert parity.V_CONSUMER_FAILED in out["violation_codes"]


def test_5_a_declared_fallback_is_a_violation(monkeypatch):
    """`fallback_used` is a claim. Publishing True is honest AND blocking."""
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={"truth_contract": {"fallback_used": True}}))
    assert out["ok"] is False
    assert parity.V_FALLBACK_USED in out["violation_codes"]


def test_5b_a_legacy_fallback_flag_is_a_violation(monkeypatch):
    out = _audit_with(monkeypatch, _payloads(
        dashboard__countries={"disclosure": {"legacy_fallback_used": True}}))
    assert out["ok"] is False
    assert parity.V_LEGACY_READ in out["violation_codes"]


def test_5c_the_truth_contract_states_fallback_explicitly():
    """Not a default: a canonical block asserts False, and says its source."""
    block = contract.truth_contract(
        data_source=contract.SOURCE_REVENUE_DECISION_MART,
        window="ytd", account_time_zone="Europe/London", now=_at("2026-06-22T12:00:00"))
    for key in ("data_source", "truth_status", "window", "window_start", "window_end",
                "currency", "generated_at", "fallback_used", "customer_id"):
        assert key in block, key
    assert block["fallback_used"] is False
    assert contract.is_canonical(block) is True


def test_5d_an_unavailable_contract_carries_no_figures_and_is_not_canonical():
    block = contract.unavailable_contract(
        data_source=contract.SOURCE_CANONICAL_SPEND, window="ytd",
        reason="coverage_incomplete", account_time_zone="Europe/London")
    assert block["truth_status"] == contract.TRUTH_UNAVAILABLE
    assert block["unavailable_reason"] == "coverage_incomplete"
    assert contract.is_canonical(block) is False


# ═════════════════════════════════════════════════════════════════════════════
# §6/§7/§11 — the static guard over legacy and retired providers
# ═════════════════════════════════════════════════════════════════════════════

#: Every legacy-table read a PRODUCTION module performs, with its classification.
#:
#: `detail_rows`  — row-level detail or a trend series beside a canonical total
#: `diagnostic`   — reconciliation / audit / mapping; never rendered as a total
#: `readiness`    — a boolean probe, carries no figure
#:
#: There is deliberately no "authoritative" classification. A production TOTAL
#: may not come from a legacy table, so an entry that needed one would be a
#: violation to fix, not a category to add.
LEGACY_READ_REGISTRY = {
    ("services/campaign_evidence_service.py", "fetch_campaign_lead_detail"): "detail_rows",
    ("services/campaign_evidence_service.py", "fetch_lead_quality"): "detail_rows",
    ("services/campaign_identity_service.py", "fetch_lead_quality"): "diagnostic",
    ("services/dashboard_deals_service.py", "fetch_sql_lead_details"): "detail_rows",
    ("services/dashboard_revenue_service.py", "fetch_lead_daily_series"): "detail_rows",
    ("services/google_ads_spend_service.py", "fetch_geo_spend_total"): "diagnostic",
    ("services/lead_reconciliation_service.py", "fetch_missing_event_date_leads"): "diagnostic",
    ("services/revenue_attribution_service.py", "fetch_campaign_country_spend"): "diagnostic",
    ("services/revenue_attribution_service.py", "fetch_campaign_pollution_report"): "diagnostic",
    ("services/revenue_attribution_service.py", "fetch_lead_date_grain_health"): "diagnostic",
    ("services/revenue_attribution_service.py", "fetch_lead_quality"): "detail_rows",
    ("services/revenue_attribution_service.py", "revenue_integration_connected"): "readiness",
    ("services/source_attribution_service.py", "fetch_source_contact_details"): "detail_rows",
}

#: Repository functions that read a legacy truth table (`leads`, `geo`,
#: `gclid_attribution`). Derived from the repository source, not hand-listed, so
#: a new legacy-reading helper is picked up automatically.
def _legacy_reader_names() -> set:
    src = (_ROOT / "db" / "revenue_repository.py").read_text()
    markers = ("FROM leads", "JOIN leads", "FROM gclid_attribution",
               "JOIN gclid_attribution", "FROM geo\n", "FROM geo ")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef):
            seg = ast.get_source_segment(src, node) or ""
            if any(m in seg for m in markers):
                names.add(node.name)
    return names


def _production_legacy_calls() -> dict:
    readers = _legacy_reader_names()
    calls: dict = {}
    for root in ("services", "api", "scheduler", "analysis"):
        for f in (_ROOT / root).rglob("*.py"):
            try:
                tree = ast.parse(f.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", getattr(node.func, "id", None))
                    if name in readers:
                        calls.setdefault(str(f.relative_to(_ROOT)), set()).add(name)
    return calls


def test_11_every_production_legacy_read_is_classified():
    """A NEW legacy read fails here until someone classifies it.

    This is the guard that keeps the rest of the suite true after the PR: the
    registry is derived from the repository's own SQL, so adding a legacy-reading
    helper and calling it from a page cannot pass unnoticed.
    """
    actual = {(mod, fn) for mod, fns in _production_legacy_calls().items() for fn in fns}
    unregistered = actual - set(LEGACY_READ_REGISTRY)
    assert not unregistered, (
        "unclassified legacy reads in production code — classify each in "
        f"LEGACY_READ_REGISTRY or migrate it to a canonical source: {sorted(unregistered)}")


def test_11b_the_registry_has_no_stale_entries():
    """A removed read must leave the registry, or the guard rots into fiction."""
    actual = {(mod, fn) for mod, fns in _production_legacy_calls().items() for fn in fns}
    stale = set(LEGACY_READ_REGISTRY) - actual
    assert not stale, f"registry lists reads that no longer happen: {sorted(stale)}"


def test_11c_no_legacy_read_is_classified_as_supplying_a_total():
    allowed = {"detail_rows", "diagnostic", "readiness"}
    for key, classification in LEGACY_READ_REGISTRY.items():
        assert classification in allowed, (
            f"{key} is classified {classification!r}; a production total may not "
            "come from a legacy table")


def test_6_no_production_module_imports_a_retired_provider():
    """Windsor is retired. Scripts may still reference it for migration evidence;
    services, api, scheduler and analysis may not.

    Walked on the AST rather than grepped, because the modules that discuss the
    retirement in prose are exactly the ones a text search would flag.
    """
    offenders = []
    for root in ("services", "api", "scheduler", "analysis"):
        for f in (_ROOT / root).rglob("*.py"):
            try:
                tree = ast.parse(f.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [a.name for a in node.names]
                if any("windsor" in (n or "").lower() for n in names):
                    offenders.append(f"{f.relative_to(_ROOT)}: {names}")
    assert not offenders, f"production modules importing a retired provider: {offenders}"


def test_7_gclid_attribution_cannot_determine_all_source_revenue():
    """Total business revenue reads the canonical ledger, not the GCLID subset.

    `gclid_attribution` holds only deals with a GCLID, so using it as the
    business total silently drops every deal from every other source.
    """
    tree = ast.parse((_ROOT / "services" / "dashboard_revenue_service.py").read_text())
    called = {getattr(n.func, "attr", getattr(n.func, "id", None))
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "fetch_revenue_deals" not in called, (
        "the Revenue page reads the GCLID-only ledger for its revenue total")
    spec = parity.METRIC_IDENTITIES["closed_won_revenue_usd"]
    assert spec["scope"] == "all_source_business_revenue"
    assert spec["canonical_source"] == contract.SOURCE_REVENUE_DECISION_MART


# ═════════════════════════════════════════════════════════════════════════════
# §8/§9 — canonical truth readiness is unmoved by legacy failures
# ═════════════════════════════════════════════════════════════════════════════

def _recon(**kw):
    base = {"status": "success", "available": True, "reconciled": True,
            "country_spend_status": geo.GEO_STATUS_VERIFIED, "geo_ready": True,
            "geo_gap_codes": [], "campaign_coverage_complete": True,
            "fx_coverage_complete": True, "geo_coverage_complete": True,
            "comparison_like_for_like": True}
    base.update(kw)
    return base


def test_8_legacy_hubspot_association_failures_do_not_change_truth_readiness():
    """The production run that motivated this PR had HubSpot 429s in the LEGACY
    `hubspot/deals` contact-association scan while the canonical
    `hubspot/deal_ledger` completed with zero association and write failures.

    Truth readiness is derived from the canonical reconciliation dataset alone,
    so a degraded legacy scan beside it cannot move the verdict.
    """
    healthy = {sched.LABEL_GEO_RECONCILIATION: _recon()}
    with_legacy_failure = {
        sched.LABEL_GEO_RECONCILIATION: _recon(),
        "hubspot/deals": {"status": "failed", "error": "429 Too Many Requests",
                          "authoritative": False},
        "hubspot/deal_ledger": {"status": "success", "association_failures": 0,
                                "write_failures": 0},
    }
    a = sched.build_truth_block(healthy)
    b = sched.build_truth_block(with_legacy_failure)
    assert a == b, "a legacy scan failure changed canonical truth readiness"
    assert b["truth_status"] == sched.TRUTH_READY
    assert b["geo_ready"] is True


def test_8b_the_legacy_deals_scan_is_marked_non_authoritative():
    """It may stay for migration evidence, but it must say what it is."""
    assert sched.LEGACY_NON_AUTHORITATIVE_DATASETS, "no non-authoritative register"
    assert "hubspot/deals" in sched.LEGACY_NON_AUTHORITATIVE_DATASETS


def test_9_reconciled_with_residual_remains_an_accepted_geo_ready_state():
    """PR-ADS-154B-F1, re-asserted from this layer so a parity change cannot
    quietly reintroduce an exact-reconciliation requirement."""
    assert geo.country_geo_ready(geo.GEO_STATUS_RECONCILED_WITH_RESIDUAL) is True
    out = sched.build_truth_block({sched.LABEL_GEO_RECONCILIATION: _recon(
        reconciled=False,
        country_spend_status=geo.GEO_STATUS_RECONCILED_WITH_RESIDUAL)})
    assert out["truth_status"] == sched.TRUTH_READY
    assert out["geo_ready"] is True
    assert out["geo_reconciled"] is False


# ═════════════════════════════════════════════════════════════════════════════
# §10 — the audit command's exit contract
# ═════════════════════════════════════════════════════════════════════════════

def test_10_the_audit_exits_zero_only_on_full_parity(monkeypatch):
    clean = _audit_with(monkeypatch, _payloads())
    assert clean["ok"] is True

    dirty = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={"kpis.closed_won_revenue_usd": 1.0}))
    assert dirty["ok"] is False


def test_10b_one_failing_window_fails_the_whole_audit(monkeypatch):
    calls = {"n": 0}

    def _fake(window, now=None):
        calls["n"] += 1
        return {"window": window, "ok": window != "ytd",
                "violations": ([] if window != "ytd"
                               else [{"code": parity.V_VALUE_MISMATCH}])}

    monkeypatch.setattr(parity, "audit_window", _fake)
    out = parity.audit_all_windows(["current_quarter", "ytd"])
    assert out["ok"] is False
    assert calls["n"] == 2, "every window is audited even after one fails"
    assert out["violations"][0]["window"] == "ytd"


def test_10c_the_cli_exit_code_follows_ok():
    src = (_ROOT / "scripts" / "audit_cross_page_canonical_parity.py").read_text()
    assert 'return 0 if outcome["ok"] else 1' in src


def test_10d_the_cli_never_interpolates_a_raw_exception():
    """PR-ADS-154A: a connection failure carries the DSN, and a DSN carries the
    password."""
    src = (_ROOT / "scripts" / "audit_cross_page_canonical_parity.py").read_text()
    assert "safe_db_error(exc)" in src
    assert "{exc}" not in src.replace("safe_db_error(exc)", "")


def test_10e_the_audit_covers_every_required_window():
    assert set(WINDOW_KEYS) == {"current_quarter", "last_quarter", "last_6_months",
                                "ytd", "all_time"}


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-154C-F1 — the false-negative paths the first version left open
#
# Every test below fails against the pre-F1 audit. Together they close the gap
# between "the audit reported parity" and "parity was actually proved".
# ═════════════════════════════════════════════════════════════════════════════

def test_f1_1_production_shaped_top_level_legacy_fallback_fails(monkeypatch):
    """The defect: `legacy_fallback_used` is a TOP-LEVEL bool in production.

    The original guard checked `isinstance(block, dict)` on keys named
    `truth_contract`, `disclosure` and `source_truth`. In a real dashboard
    payload `source_truth` is a STRING and the fallback flags sit at the top
    level, so the check was False for every production shape — a guard that
    could not fire on the thing it was written for.
    """
    out = _audit_with(monkeypatch, _payloads(
        dashboard__countries={"legacy_fallback_used": True}))
    assert out["ok"] is False
    assert parity.V_LEGACY_READ in out["violation_codes"]
    assert any("top-level" in v.get("detail", "") for v in out["violations"])


def test_f1_1b_production_shaped_top_level_fallback_used_fails(monkeypatch):
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={"fallback_used": True}))
    assert out["ok"] is False
    assert parity.V_FALLBACK_USED in out["violation_codes"]


def test_f1_1c_a_string_source_truth_does_not_crash_the_guard(monkeypatch):
    """Production publishes `source_truth` as a string; the guard must cope."""
    payloads = _payloads()
    assert isinstance(payloads["dashboard/overview"]["source_truth"], str)
    out = _audit_with(monkeypatch, payloads)
    assert out["ok"] is True


def test_f1_2_a_missing_metric_contract_fails(monkeypatch):
    """Silence is not proof that the right source was used."""
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={f"{contract_mod.METRIC_TRUTH_KEY}.closed_won_revenue_usd": _DELETE}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]


def test_f1_2b_a_wrong_canonical_source_fails(monkeypatch):
    """A page reading somewhere else can no longer be certified by the registry."""
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={
            f"{contract_mod.METRIC_TRUTH_KEY}.closed_won_revenue_usd.data_source":
                "legacy.gclid_attribution"}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]
    assert any("legacy.gclid_attribution" in v.get("detail", "")
               for v in out["violations"])


def test_f1_2c_a_wrong_metric_scope_fails(monkeypatch):
    """The GCLID subset published under an all-source label is the whole point."""
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={
            f"{contract_mod.METRIC_TRUTH_KEY}.closed_won_revenue_usd.scope":
                "google_ads_attributed_revenue"}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]


@pytest.mark.parametrize("status", ["not_ready", "unavailable"])
def test_f1_2d_a_non_ready_contract_fails(monkeypatch, status):
    out = _audit_with(monkeypatch, _payloads(
        dashboard__overview={
            f"{contract_mod.METRIC_TRUTH_KEY}.google_ads_spend_usd.truth_status": status}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]


def test_f1_2e_a_contract_window_disagreeing_with_the_payload_fails(monkeypatch):
    """A contract describing a different range than the page computed over."""
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={
            f"{contract_mod.METRIC_TRUTH_KEY}.customers.window_end": "2026-06-23"}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]


def test_f1_2f_inconsistent_currency_across_consumers_fails(monkeypatch):
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={
            f"{contract_mod.METRIC_TRUTH_KEY}.closed_won_revenue_usd.currency": "GBP"}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INCONSISTENT in out["violation_codes"]


def test_f1_3_one_missing_consumer_value_fails_even_when_the_others_agree(monkeypatch):
    """Comparing only what is present let a dropped metric look like agreement.

    Two consumers agreeing while a third declines to answer is not three
    consumers agreeing.
    """
    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={"kpis.closed_won_revenue_usd": None}))
    assert out["ok"] is False
    assert parity.V_CONSUMER_METRIC_MISSING in out["violation_codes"]
    metric = next(m for m in out["metrics"] if m["metric"] == "closed_won_revenue_usd")
    assert metric["status"] != "identical"


def test_f1_3b_a_removed_dotted_path_fails_the_same_way(monkeypatch):
    out = _audit_with(monkeypatch, _payloads(
        dashboard__overview={"kpis.closed_won_revenue_usd": _DELETE}))
    assert out["ok"] is False
    assert parity.V_CONSUMER_METRIC_MISSING in out["violation_codes"]


def test_f1_4_a_missing_consumer_window_fails(monkeypatch):
    """One valid signature is not unanimity when another page published none."""
    out = _audit_with(monkeypatch, _payloads(
        dashboard__deals={"window": _DELETE}))
    assert out["ok"] is False
    assert parity.V_WINDOW_MISSING in out["violation_codes"]


@pytest.mark.parametrize("field", ["end_date", "timezone", "key"])
def test_f1_4b_an_incomplete_consumer_window_fails(monkeypatch, field):
    out = _audit_with(monkeypatch, _payloads(
        dashboard__channels={f"window.{field}": _DELETE}))
    assert out["ok"] is False
    assert parity.V_WINDOW_MISSING in out["violation_codes"]


def test_f1_4c_a_null_start_is_allowed_only_for_all_time(monkeypatch):
    """`all_time` has a genuinely open lower bound; every other window does not."""
    monkeypatch.setattr(parity, "resolve_canonical_window",
                        lambda w, now=None: {"key": w, "label": w,
                                             "start_date": None,
                                             "end_date": "2026-06-22",
                                             "timezone": "Europe/London"})
    payloads = _payloads(dashboard__overview={"window.start_date": None})
    monkeypatch.setattr(parity, "_build_consumers",
                        lambda window, now: {k: {"payload": v, "error": None}
                                             for k, v in payloads.items()})
    blocked = parity.audit_window("current_quarter")
    assert parity.V_WINDOW_MISSING in blocked["violation_codes"]

    allowed = parity.audit_window("all_time")
    assert parity.V_WINDOW_MISSING not in allowed["violation_codes"]


def test_f1_5_campaign_coverage_cannot_substitute_for_geo_coverage(monkeypatch):
    """Each authority is asked about ITSELF.

    Campaign spend coverage was previously the universal proof, so a country
    metric could be certified by evidence about campaign spend — evidence about
    a different table entirely.
    """
    out = _audit_with(monkeypatch, _payloads(
        revenue_decision_mart={
            "spend_truth.campaign_spend_status": "verified",
            "spend_truth.fx_status": "verified",
            "spend_truth.country_spend_status": "mismatch"}))
    assert out["ok"] is False
    assert parity.V_AGREEMENT_ON_UNPROVEN_COVERAGE in out["violation_codes"]

    country = next(m for m in out["metrics"]
                   if m["metric"] == "country_attributed_won_revenue_usd")
    assert country["status"] == "unproven"
    # ...while campaign spend, whose own coverage IS proven, stays identical.
    spend = next(m for m in out["metrics"] if m["metric"] == "google_ads_spend_usd")
    assert spend["status"] == "identical"


def test_f1_5b_incomplete_fx_blocks_campaign_spend_even_with_campaign_coverage(monkeypatch):
    """Spend needs BOTH its coverage and FX; one is not the other."""
    out = _audit_with(monkeypatch, _payloads(
        revenue_decision_mart={"spend_truth.fx_status": "incomplete"}))
    assert out["ok"] is False
    spend = next(m for m in out["metrics"] if m["metric"] == "google_ads_spend_usd")
    assert spend["status"] == "unproven"


def test_f1_5c_unavailable_revenue_blocks_revenue_metrics(monkeypatch):
    out = _audit_with(monkeypatch, _payloads(
        revenue_decision_mart={"summary.revenue_available": False}))
    assert out["ok"] is False
    revenue = next(m for m in out["metrics"] if m["metric"] == "closed_won_revenue_usd")
    assert revenue["status"] == "unproven"


def test_f1_5d_reconciled_with_residual_remains_accepted(monkeypatch):
    """PR-ADS-131 / PR-ADS-154B-F1, re-asserted at the parity layer."""
    assert "reconciled_with_residual" in parity.ACCEPTED_COUNTRY_STATES
    out = _audit_with(monkeypatch, _payloads(
        revenue_decision_mart={
            "spend_truth.country_spend_status": "reconciled_with_residual"}))
    assert out["ok"] is True
    country = next(m for m in out["metrics"]
                   if m["metric"] == "country_attributed_won_revenue_usd")
    assert country["status"] == "identical"


def test_f1_6_the_human_readable_renderer_handles_unproven(capsys, monkeypatch):
    """`_render` indexed a three-entry map and raised KeyError on `unproven` —
    crashing on exactly the failure it exists to explain."""
    import scripts.audit_cross_page_canonical_parity as cli

    out = _audit_with(monkeypatch, _payloads(
        revenue_decision_mart={"spend_truth.fx_status": "incomplete"}))
    assert any(m["status"] == "unproven" for m in out["metrics"])

    cli._render({"ok": False, "results": [out],
                 "violations": out["violations"],
                 "violation_codes": out["violation_codes"]})
    printed = capsys.readouterr().out
    assert "unproven" in printed
    assert "agreement_on_unproven_coverage" in printed


def test_f1_6b_an_unknown_future_status_renders_rather_than_raising(capsys):
    """A status this renderer has not been taught about must still print."""
    import scripts.audit_cross_page_canonical_parity as cli

    cli._render({"ok": False, "violations": [], "violation_codes": [],
                 "results": [{
                     "window": "ytd", "window_start": "2026-01-01",
                     "window_end": "2026-06-22", "timezone": "Europe/London",
                     "ok": False, "consumers_inspected": ["x"],
                     "consumer_windows": [], "violations": [],
                     "metrics": [{"metric": "m", "status": "a_status_from_the_future",
                                  "value": 1, "readings": []}]}]})
    assert "a_status_from_the_future" in capsys.readouterr().out


@pytest.mark.parametrize("zone,expected", [
    (None, "Europe/London"), ("", "Europe/London"),
    ("Not/AZone", "Europe/London"), ("America/Los_Angeles", "America/Los_Angeles"),
])
def test_f1_7_the_reported_timezone_is_the_one_actually_used(zone, expected):
    """Returning the REQUESTED zone after falling back described dates as having
    been computed somewhere they were not — the same class of defect as the
    anchoring bug this resolver exists to fix."""
    resolved = resolve_window_in_zone("ytd", zone, now=_MIDNIGHT_CROSSING)
    assert resolved["timezone"] == expected
    assert resolved["timezone_requested"] == (zone or "Europe/London")


def test_f1_7b_the_dates_match_the_reported_timezone():
    """The reported zone and the computed dates must tell the same story."""
    invalid = resolve_window_in_zone("current_quarter", "Not/AZone",
                                     now=_MIDNIGHT_CROSSING)
    account = resolve_window_in_zone("current_quarter", "Europe/London",
                                     now=_MIDNIGHT_CROSSING)
    assert invalid["timezone"] == account["timezone"] == "Europe/London"
    assert invalid["start_date"] == account["start_date"] == "2026-07-01"

    la = resolve_window_in_zone("current_quarter", "America/Los_Angeles",
                                now=_MIDNIGHT_CROSSING)
    assert la["timezone"] == "America/Los_Angeles"
    assert la["start_date"] == "2026-04-01"    # still Q2 in Los Angeles


def test_f1_8_comparison_is_exact_not_rounded(monkeypatch):
    """`_norm` rounded to six decimals while the command claimed exactness.

    Rounding is a tolerance wearing different clothes. Two values differing at
    the seventh decimal are two answers to one question.
    """
    from decimal import Decimal
    assert parity._norm(1.00000001) != parity._norm(1.0)
    assert parity._norm(2.0) == parity._norm(2) == Decimal("2.0")

    out = _audit_with(monkeypatch, _payloads(
        dashboard__revenue={"kpis.closed_won_revenue_usd": 33000.00000001}))
    assert out["ok"] is False
    assert parity.V_VALUE_MISMATCH in out["violation_codes"]


def test_f1_9_every_real_audited_payload_publishes_its_metric_contracts():
    """The registry's paths and contracts checked against REAL service output.

    A registry that drifts from the services reports "unavailable" forever and
    looks like a data problem. This binds the two together.

    Every registered consumer must publish the CONTRACT unconditionally — an
    outage is something a page states, not something it goes quiet about — and
    must publish the VALUE whenever its own contract calls the metric ready. The
    reference fixture leaves the canonical contact funnel unwired, so the five
    lifecycle identities are legitimately `not_ready` here and are checked on the
    first rule only.
    """
    import tests.test_pr_ads_138_dashboard_countries as t138
    from services.cross_page_parity_service import METRIC_IDENTITIES, expected_source

    mp = pytest.MonkeyPatch()
    t138._patch_durable(mp)
    try:
        built = parity._build_consumers("current_quarter", t138.NOW)
    finally:
        mp.undo()

    for metric_key, spec in METRIC_IDENTITIES.items():
        for consumer_name, path in spec["consumers"]:
            entry = built.get(consumer_name) or {}
            assert entry.get("error") is None, f"{consumer_name}: {entry.get('error')}"
            payload = entry["payload"]
            block = (payload.get(contract_mod.METRIC_TRUTH_KEY) or {}).get(metric_key)
            assert isinstance(block, dict), (
                f"{consumer_name} publishes no contract for {metric_key}")
            assert block["metric"] == metric_key
            assert block["data_source"] == expected_source(spec, consumer_name)
            assert block["scope"] == spec["scope"]
            assert block["fallback_used"] is False
            if block["truth_status"] == contract_mod.TRUTH_READY:
                assert parity._dig(payload, path) is not None, (
                    f"{consumer_name}.{path} declares {metric_key} ready and "
                    "publishes nothing — the registry has drifted from the service")


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-154C-F2 — complete the decision-metric registry
#
# The gap this closes: the audit built SEVEN consumers and certified FOUR metric
# identities, then reported all seven as "inspected". Three production pages
# passed by having nothing checked about them — the agreement-shaped failure the
# command exists to catch, occurring inside the command itself.
# ═════════════════════════════════════════════════════════════════════════════


def test_f2_1_every_executive_page_contributes_a_certified_identity():
    """No production page is registered as a consumer of nothing."""
    from services.cross_page_parity_service import (
        CERTIFIED_CONSUMERS, METRIC_IDENTITIES)

    for page in ("dashboard/overview", "dashboard/revenue", "dashboard/channels",
                 "dashboard/campaigns", "dashboard/countries", "dashboard/deals",
                 "revenue_decision_mart"):
        assert page in CERTIFIED_CONSUMERS, f"{page} certifies no metric identity"
        registered = [k for k, s in METRIC_IDENTITIES.items()
                      if any(c == page for c, _ in s["consumers"])]
        assert registered, f"{page} is in CERTIFIED_CONSUMERS but registers nothing"


def test_f2_1b_a_page_that_certifies_nothing_is_a_violation(monkeypatch):
    """Building successfully is not certification.

    Channels published no audited identity before this PR and the audit still
    counted it among the consumers inspected. Stripping its contracts must now
    fail rather than pass quietly.
    """
    payloads = _payloads()
    payloads["dashboard/channels"].pop(contract_mod.METRIC_TRUTH_KEY)
    out = _audit_with(monkeypatch, payloads)
    assert out["ok"] is False
    assert parity.V_CONSUMER_UNCERTIFIED in out["violation_codes"]
    row = next(c for c in out["consumer_certification"]
               if c["consumer"] == "dashboard/channels")
    assert row["certified"] is False
    assert row["identities_certified"] == 0
    assert row["identities_registered"] > 0


def test_f2_1c_a_registered_consumer_that_is_never_built_is_a_violation(monkeypatch):
    """A page dropped from `_build_consumers` must not vanish from the report."""
    payloads = _payloads()
    payloads.pop("dashboard/deals")
    out = _audit_with(monkeypatch, payloads)
    assert out["ok"] is False
    assert parity.V_CONSUMER_NOT_BUILT in out["violation_codes"]


def test_f2_2_lifecycle_stages_are_five_identities_from_the_canonical_funnel():
    """Each stage keeps its OWN event-date semantics, so each is its own question."""
    from services.cross_page_parity_service import (
        LIFECYCLE_STAGES, METRIC_IDENTITIES)

    assert LIFECYCLE_STAGES == ("leads", "mqls", "sqls", "opportunities", "customers")
    for stage in LIFECYCLE_STAGES:
        spec = METRIC_IDENTITIES[f"lifecycle_{stage}"]
        assert spec["canonical_source"] == contract_mod.SOURCE_CANONICAL_FUNNEL
        assert spec["scope"] == f"lifecycle_{stage}"
        assert spec["consumers"] == [("dashboard/overview", f"kpis.lifecycle_{stage}")]


def test_f2_2b_the_three_sql_populations_are_never_compared():
    """Lifecycle SQLs, campaign-attributable SQLs and source-group SQLs are three
    questions. Forcing them to match would file the answer as a bug."""
    from services.cross_page_parity_service import DISTINCT_BY_DESIGN, METRIC_IDENTITIES

    scopes = {k: METRIC_IDENTITIES[k]["scope"] for k in
              ("lifecycle_sqls", "campaign_attributable_sqls", "source_group_sqls")}
    assert len(set(scopes.values())) == 3, scopes

    pairs = {frozenset((d["left"], d["right"])) for d in DISTINCT_BY_DESIGN}
    assert frozenset(("campaign_attributable_sqls", "source_group_sqls")) in pairs
    assert frozenset(("campaign_attributable_sqls", "lifecycle_sqls")) in pairs
    # And no identity puts two of them under one roof.
    for key, spec in METRIC_IDENTITIES.items():
        paths = {p for _, p in spec["consumers"]}
        assert not ({"kpis.sqls", "kpis.total_sqls"} <= paths), key


def test_f2_2c_lifecycle_customers_is_not_the_revenue_customer_count():
    """PR-ADS-153C kept these apart; the registry must not quietly rejoin them."""
    from services.cross_page_parity_service import DISTINCT_BY_DESIGN, METRIC_IDENTITIES

    assert (METRIC_IDENTITIES["lifecycle_customers"]["canonical_source"]
            != METRIC_IDENTITIES["customers"]["canonical_source"])
    pairs = {frozenset((d["left"], d["right"])) for d in DISTINCT_BY_DESIGN}
    assert frozenset(("lifecycle_customers", "customers")) in pairs


def test_f2_2d_all_source_outcomes_span_every_page_that_publishes_them(monkeypatch):
    """33000 over 3 customers is one number on five surfaces, and the audit now
    checks all five rather than two."""
    from services.cross_page_parity_service import METRIC_IDENTITIES

    for key in ("closed_won_revenue_usd", "customers"):
        consumers = {c for c, _ in METRIC_IDENTITIES[key]["consumers"]}
        assert consumers == {"dashboard/overview", "dashboard/revenue",
                             "dashboard/channels", "dashboard/deals",
                             "revenue_decision_mart"}, key

    out = _audit_with(monkeypatch, _payloads(
        dashboard__deals={"kpis.closed_won_customers": 4}))
    assert out["ok"] is False
    assert parity.V_VALUE_MISMATCH in out["violation_codes"]


def test_f2_2e_country_spend_is_not_the_full_account_denominator():
    """Countries sums the per-country rows. geographic_view does not place
    location-less spend in any of them, so the two are equal only by accident."""
    from services.cross_page_parity_service import DISTINCT_BY_DESIGN, METRIC_IDENTITIES

    full = {c for c, _ in METRIC_IDENTITIES["google_ads_spend_usd"]["consumers"]}
    assert "dashboard/countries" not in full
    country = METRIC_IDENTITIES["country_attributed_spend_usd"]
    assert country["consumers"] == [("dashboard/countries", "kpis.verified_spend_usd")]
    assert country["canonical_source"] == contract_mod.SOURCE_CANONICAL_GEO
    pairs = {frozenset((d["left"], d["right"])) for d in DISTINCT_BY_DESIGN}
    assert frozenset(("google_ads_spend_usd", "country_attributed_spend_usd")) in pairs


def test_f2_3_a_contract_filed_under_the_wrong_metric_fails(monkeypatch):
    """A block keyed `customers` that names itself something else describes a
    different question; reading it as provenance for this one is the mistake."""
    out = _audit_with(monkeypatch, _payloads(dashboard__deals={
        f"{contract_mod.METRIC_TRUTH_KEY}.customers.metric": "attributed_customers"}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]
    detail = " ".join(v.get("detail", "") for v in out["violations"])
    assert "contract.metric" in detail


def test_f2_3b_a_contract_naming_a_different_window_key_fails(monkeypatch):
    """Internally consistent and answering a different question.

    F1 compared the contract's dates with the payload's, which a page that
    resolved the wrong window satisfies perfectly — both halves are wrong
    together. The requested key is now checked too.
    """
    out = _audit_with(monkeypatch, _payloads(dashboard__campaigns={
        f"{contract_mod.METRIC_TRUTH_KEY}.campaign_attributable_sqls.window": "ytd"}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]
    detail = " ".join(v.get("detail", "") for v in out["violations"])
    assert "requested window" in detail


@pytest.mark.parametrize("field", [
    "data_source", "scope", "truth_status", "window", "window_end",
    "timezone", "currency", "fallback_used", "customer_id",
])
def test_f2_3c_an_omitted_contract_field_fails_rather_than_comparing_equal(
        monkeypatch, field):
    """Presence is checked BEFORE consistency, because two missing values compare
    equal. A contract that omits its window used to satisfy the window check by
    saying nothing at all."""
    out = _audit_with(monkeypatch, _payloads(dashboard__revenue={
        f"{contract_mod.METRIC_TRUTH_KEY}.customers.{field}": _DELETE}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]
    detail = " ".join(v.get("detail", "") for v in out["violations"])
    assert "omits required field" in detail and field in detail


def test_f2_3d_each_consumer_must_name_the_authority_it_actually_reads(monkeypatch):
    """Channels renders the all-source totals from the source-group taxonomy, so
    that is what it must declare — and it may not claim the mart instead."""
    from services.cross_page_parity_service import METRIC_IDENTITIES, expected_source

    spec = METRIC_IDENTITIES["closed_won_revenue_usd"]
    assert expected_source(spec, "dashboard/channels") == contract_mod.SOURCE_REVENUE_BY_SOURCE
    assert expected_source(spec, "dashboard/deals") == contract_mod.SOURCE_REVENUE_DECISION_MART

    out = _audit_with(monkeypatch, _payloads(dashboard__channels={
        f"{contract_mod.METRIC_TRUTH_KEY}.closed_won_revenue_usd.data_source":
            contract_mod.SOURCE_REVENUE_DECISION_MART}))
    assert out["ok"] is False
    assert parity.V_CONTRACT_INVALID in out["violation_codes"]


def test_f2_4_country_revenue_is_not_ready_on_geo_coverage_alone(monkeypatch):
    """The §4 correction. Geo coverage says the SPEND side is placed; it is silent
    on whether the closed-won deals behind the revenue were readable at all.

    Country SPEND stays proven in the same run — the two depend on different
    things being true, which is the whole point of separating them.
    """
    out = _audit_with(monkeypatch, _payloads(
        revenue_decision_mart={"summary.revenue_available": False}))
    assert out["ok"] is False

    for key in ("country_attributed_won_revenue_usd", "country_attributed_customers"):
        entry = next(m for m in out["metrics"] if m["metric"] == key)
        assert entry["status"] == "unproven", key
    spend = next(m for m in out["metrics"]
                 if m["metric"] == "country_attributed_spend_usd")
    assert spend["status"] == "identical"

    detail = " ".join(v.get("detail", "") for v in out["violations"])
    assert "deal proof" in detail


def test_f2_4b_country_revenue_needs_the_reconciliation_too(monkeypatch):
    """Both halves are required, not either one."""
    out = _audit_with(monkeypatch, _payloads(
        revenue_decision_mart={"spend_truth.country_spend_status": "mismatch"}))
    assert out["ok"] is False
    entry = next(m for m in out["metrics"]
                 if m["metric"] == "country_attributed_won_revenue_usd")
    assert entry["status"] == "unproven"


def test_f2_4c_every_identity_names_its_own_evidence():
    """An identity with no recognised coverage proof is NOT proven.

    A permissive default would certify any future metric that forgot to say what
    evidence backs it — the exact shape of the defect F1 fixed for the universal
    campaign-spend proof.
    """
    from services.cross_page_parity_service import METRIC_IDENTITIES

    known = {parity.PROOF_CAMPAIGN_SPEND, parity.PROOF_GEO_SPEND,
             parity.PROOF_COUNTRY_REVENUE, parity.PROOF_DEAL_LEDGER,
             parity.PROOF_MART_LEAD_FUNNEL, parity.PROOF_LIFECYCLE_FUNNEL}
    for key, spec in METRIC_IDENTITIES.items():
        assert spec.get("coverage_proof") in known, key

    proven, detail = parity._coverage_proven(
        {}, {"canonical_source": "x", "scope": "x", "coverage_proof": "invented"})
    assert proven is False
    assert "no recognised coverage proof" in detail


def test_f2_5_pages_pending_redesign_are_reported_not_omitted(monkeypatch):
    """A page absent from a parity report reads as a page with nothing to answer
    for, which is how an uncertified total keeps being read as a certified one."""
    from services.cross_page_parity_service import PENDING_REDESIGN_CONSUMERS

    assert set(PENDING_REDESIGN_CONSUMERS) == {"platform_evidence", "lead_intelligence"}
    out = _audit_with(monkeypatch, _payloads())
    named = {u["consumer"] for u in out["uncertified_consumers"]}
    assert named == {"platform_evidence", "lead_intelligence"}
    for u in out["uncertified_consumers"]:
        assert u["classification"] == "pending_redesign_non_authoritative"
        assert u["overlapping_metrics"] and u["services"] and u["note"]
    # They are declared uncertified, so they are never registered as consumers.
    assert not (named & parity.CERTIFIED_CONSUMERS)


def test_f2_5b_the_renderer_prints_the_uncertified_pages(capsys, monkeypatch):
    import scripts.audit_cross_page_canonical_parity as cli

    out = _audit_with(monkeypatch, _payloads())
    cli._render({"ok": out["ok"], "results": [out],
                 "violations": out["violations"],
                 "violation_codes": out["violation_codes"],
                 "uncertified_consumers": [
                     {"consumer": name, **detail} for name, detail
                     in sorted(parity.PENDING_REDESIGN_CONSUMERS.items())]})
    printed = capsys.readouterr().out
    assert "EXPLICITLY UNCERTIFIED" in printed
    assert "platform_evidence" in printed and "lead_intelligence" in printed
    assert "certified" in printed


def test_f2_6_the_audit_passes_only_when_every_identity_was_checked(monkeypatch):
    """`ok=true` requires every certified consumer AND every metric identity to
    have been checked — not merely that every page built."""
    out = _audit_with(monkeypatch, _payloads())
    assert out["ok"] is True
    assert out["violations"] == []
    assert len(out["metrics"]) == len(parity.METRIC_IDENTITIES)
    assert all(m["status"] == "identical" for m in out["metrics"]), \
        [(m["metric"], m["status"]) for m in out["metrics"] if m["status"] != "identical"]
    assert all(c["certified"] for c in out["consumer_certification"])


def test_f2_4d_an_empty_contacts_table_is_not_a_measured_zero(monkeypatch):
    """Found by running the audit against a real EMPTY PostgreSQL schema.

    Every page renders 0 leads and 0 SQLs, unanimously, from a table nobody
    synced. "It published a number" is not proof — `db_empty` means the query
    returned nothing at all, which is indistinguishable from never having run.
    """
    for override in ({"readiness.lead_metrics_status": "db_empty"},
                     {"readiness.lead_metrics_status": "withheld",
                      "readiness.lead_metrics_ready": False}):
        out = _audit_with(monkeypatch, _payloads(revenue_decision_mart=override))
        assert out["ok"] is False
        for key in ("campaign_attributable_sqls", "campaign_attributable_leads",
                    "source_group_sqls"):
            entry = next(m for m in out["metrics"] if m["metric"] == key)
            assert entry["status"] == "unproven", (key, override)
        assert parity.V_AGREEMENT_ON_UNPROVEN_COVERAGE in out["violation_codes"]


def test_f2_4e_an_unsynced_lifecycle_funnel_is_not_a_measured_zero(monkeypatch):
    """The funnel service reports `available: true` against an empty schema —
    five stages at 0, reconciled against nothing — while its own sync block says
    the bootstrap never ran. The sync block is the one that knows."""
    out = _audit_with(monkeypatch, _payloads(dashboard__overview={
        "lifecycle_funnel.sync.available": False,
        "lifecycle_funnel.sync.bootstrap_status": "unavailable"}))
    assert out["ok"] is False
    for stage in parity.LIFECYCLE_STAGES:
        entry = next(m for m in out["metrics"] if m["metric"] == f"lifecycle_{stage}")
        assert entry["status"] == "unproven", stage
    detail = " ".join(v.get("detail", "") for v in out["violations"])
    assert "sync" in detail


# ── The command itself, end to end, through the real builders ────────────────


def _cli_argv(monkeypatch, argv):
    """Run `main()` with a patched argv, returning its exit code."""
    import scripts.audit_cross_page_canonical_parity as cli
    monkeypatch.setattr(sys, "argv", argv)
    return cli.main()





def test_f2_7_the_cli_reports_full_parity_against_a_complete_fixture(monkeypatch, capsys):
    """Both output formats against a COMPLETE fixture — must exit 0."""
    import tests.test_pr_ads_138_dashboard_countries as t138
    from services import cross_page_parity_service as parity
    import scripts.audit_cross_page_canonical_parity as cli

    t138._patch_durable(monkeypatch)
    monkeypatch.setattr("db.connection.init_pool", lambda: None)
    # The 138 fixture leaves the contact funnel unwired; supply the one signal
    # that makes the lifecycle strip a measurement rather than an empty schema.
    real = parity._build_consumers

    def patched(window, now):
        built = real(window, now)
        ov = built["dashboard/overview"]["payload"]
        ov["kpis"].update({f"lifecycle_{s}": n for s, n in
                           (("leads", 105), ("mqls", 60), ("sqls", 30),
                            ("opportunities", 8), ("customers", 4))})
        ov["kpis"]["lifecycle_available"] = True
        ov["lifecycle_funnel"] = {"available": True, "status": "reconciled",
                                  "sync": {"available": True,
                                           "bootstrap_status": "complete"}}
        for stage in ("leads", "mqls", "sqls", "opportunities", "customers"):
            ov["metric_truth"][f"lifecycle_{stage}"]["truth_status"] = "ready"
        return built
    monkeypatch.setattr(parity, "_build_consumers", patched)

    rc = _cli_argv(monkeypatch, ["prog", "--window", "current_quarter"])
    text = capsys.readouterr().out
    assert "EXPLICITLY UNCERTIFIED" in text
    assert "certified" in text
    assert rc == 0, text[-4000:]

    rc = _cli_argv(monkeypatch, ["prog", "--window", "current_quarter", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert len(payload["uncertified_consumers"]) == 2
    assert len(payload["results"][0]["metrics"]) == 16
    assert all(c["certified"] for c in payload["results"][0]["consumer_certification"])


def test_f2_7b_the_cli_exits_nonzero_against_an_inconsistent_fixture(monkeypatch, capsys):
    """A deliberately inconsistent fixture — must exit 1 and name the violation."""
    import tests.test_pr_ads_138_dashboard_countries as t138
    from services import cross_page_parity_service as parity

    t138._patch_durable(monkeypatch)
    monkeypatch.setattr("db.connection.init_pool", lambda: None)
    real = parity._build_consumers

    def broken(window, now):
        built = real(window, now)
        built["dashboard/deals"]["payload"]["kpis"]["closed_won_revenue_usd"] = 42.0
        return built
    monkeypatch.setattr(parity, "_build_consumers", broken)

    rc = _cli_argv(monkeypatch, ["prog", "--window", "current_quarter"])
    text = capsys.readouterr().out
    assert rc == 1
    assert "consumer_values_differ" in text

    rc = _cli_argv(monkeypatch, ["prog", "--window", "current_quarter", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "consumer_values_differ" in payload["violation_codes"]
