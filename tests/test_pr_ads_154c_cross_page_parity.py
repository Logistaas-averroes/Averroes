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
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis.business_windows import WINDOW_KEYS, resolve_window, resolve_window_in_zone  # noqa: E402
from services import canonical_contract as contract  # noqa: E402
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

def _payloads(**overrides):
    """A consistent set of consumer payloads, plus deliberate overrides."""
    win = {"key": "current_quarter", "start_date": "2026-04-01",
           "end_date": "2026-06-22", "timezone": "Europe/London"}
    base = {
        "dashboard/overview": {"window": dict(win), "kpis": {
            "google_ads_spend_usd": 13000.0, "closed_won_revenue_usd": 33000.0}},
        "dashboard/revenue": {"window": dict(win), "kpis": {
            "closed_won_revenue_usd": 33000.0, "customers": 3}},
        "dashboard/channels": {"window": dict(win), "kpis": {}},
        "dashboard/campaigns": {"window": dict(win), "kpis": {}},
        "dashboard/countries": {"window": dict(win), "kpis": {
            "won_revenue_usd": 29000.0}},
        "dashboard/deals": {"window": dict(win), "kpis": {}},
        "revenue_decision_mart": {
            "window": dict(win),
            # Agreement counts as parity only over PROVEN coverage: an unproven
            # window makes every consumer render the same zero, which is
            # unanimity about a number nobody measured.
            "spend_truth": {"campaign_spend_status": "verified"},
            "summary": {"spend_usd": 13000.0, "won_revenue_usd": 33000.0,
                        "customers": 3}},
    }
    for name, patch in overrides.items():
        target = base[name.replace("__", "/")]
        for path, value in patch.items():
            node = target
            *parents, leaf = path.split(".")
            for p in parents:
                node = node.setdefault(p, {})
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
        dashboard__overview={"kpis.google_ads_spend_usd": 0.0}))
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
