"""
services/cross_page_parity_service.py

PR-ADS-154C — prove that every production page computes the same metric the same
way, and name the metrics that are *supposed* to differ.

The question this answers
------------------------
Not "do the pages roughly agree", which tolerance-based checks answer and which
is how disagreements survive. It answers: for a given metric identity, window,
customer, currency and attribution scope, does every consumer publish the
IDENTICAL value — and does each of them say, in its own payload, which canonical
source produced it?

Two pages disagreeing is only one of the failure modes. The others are worse
because they look like agreement:

  * two pages asking about different date ranges under the same window name
    (PR-ADS-154C's window-anchor defect — see ``canonical_contract``);
  * a page quietly falling back to a legacy provider and publishing the result
    under a canonical label;
  * two genuinely different metrics — total business revenue and Google
    Ads-attributed revenue — compared as though they should match, so the real
    difference is filed as a bug and the real bug is hidden inside it.

So this module compares only within a METRIC IDENTITY, and carries an explicit
register of the pairs that must never be compared at all.

Distinct by design
------------------
These are different questions, and a difference between them is information, not
a defect:

  * total business revenue           — every closed-won deal, any source
  * Google Ads-attributed revenue    — the subset attributable to Google Ads
  * country-attributed revenue       — the subset assigned to a real country;
                                       the residual is published separately
  * campaign spend                   — the canonical ROAS denominator
  * country-attributed spend         — the part geographic_view assigns to a
                                       country; the residual is the rest

Read-only throughout. No external platform is contacted; no table is written.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from analysis.business_windows import WINDOW_KEYS
from services.canonical_contract import (
    METRIC_TRUTH_KEY,
    SOURCE_CANONICAL_DEAL_LEDGER,
    SOURCE_CANONICAL_FUNNEL,
    SOURCE_CANONICAL_GEO,
    SOURCE_CANONICAL_SPEND,
    SOURCE_REVENUE_DECISION_MART,
    TRUTH_READY,
    resolve_canonical_window,
)

log = logging.getLogger(__name__)

# ── Violation codes ──────────────────────────────────────────────────────────
V_VALUE_MISMATCH = "consumer_values_differ"
V_WINDOW_MISMATCH = "consumer_windows_differ"
V_FALLBACK_USED = "legacy_fallback_used"
V_LEGACY_READ = "legacy_source_supplied_production_total"
V_SOURCE_UNAVAILABLE = "canonical_source_unavailable"
V_CONSUMER_FAILED = "consumer_raised"
V_UNCLASSIFIED_DIFFERENCE = "difference_not_classified"
#: Every consumer published the same figure, but the coverage behind it was never
#: proven — so the agreement is not evidence. See `_coverage_proven`.
V_AGREEMENT_ON_UNPROVEN_COVERAGE = "agreement_on_unproven_coverage"
#: PR-ADS-154C-F1.
V_CONSUMER_METRIC_MISSING = "consumer_metric_missing"
V_WINDOW_MISSING = "consumer_window_missing"
V_CONTRACT_INVALID = "metric_contract_invalid"
V_CONTRACT_INCONSISTENT = "metric_contract_inconsistent"

#: Country reconciliation states that count as governed geo readiness. Both are
#: accepted: `reconciled_with_residual` is the PR-ADS-131 case where the
#: shortfall is explicitly calculated and published rather than hidden.
ACCEPTED_COUNTRY_STATES = frozenset({"verified", "reconciled_with_residual"})

#: Metric identities. Each entry is ONE question, and every consumer listed must
#: answer it identically. Consumers are (label, dotted path into the payload).
#:
#: Paths are asserted against real payloads in the test suite, so a renamed key
#: fails a test rather than silently reporting the metric "unavailable" here —
#: an audit that goes quiet when it loses its grip is worse than no audit.
METRIC_IDENTITIES = {
    "google_ads_spend_usd": {
        "label": "Google Ads spend (USD)",
        "canonical_source": SOURCE_CANONICAL_SPEND,
        "scope": "google_ads_campaign_spend",
        "consumers": [
            ("dashboard/overview", "kpis.google_ads_spend_usd"),
            ("revenue_decision_mart", "summary.spend_usd"),
        ],
    },
    "closed_won_revenue_usd": {
        "label": "Closed-won revenue, ALL sources (USD)",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "all_source_business_revenue",
        "consumers": [
            ("dashboard/overview", "kpis.closed_won_revenue_usd"),
            ("dashboard/revenue", "kpis.closed_won_revenue_usd"),
            ("revenue_decision_mart", "summary.won_revenue_usd"),
        ],
    },
    "customers": {
        "label": "Closed-won customers, ALL sources",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "all_source_business_revenue",
        "consumers": [
            ("dashboard/revenue", "kpis.customers"),
            ("revenue_decision_mart", "summary.customers"),
        ],
    },
    "country_attributed_won_revenue_usd": {
        "label": "Closed-won revenue attributed to a country (USD)",
        "canonical_source": SOURCE_CANONICAL_GEO,
        "scope": "country_attributed_revenue",
        "consumers": [
            ("dashboard/countries", "kpis.won_revenue_usd"),
        ],
    },
}

#: Pairs that must NEVER be compared, with the reason. Registering them is what
#: stops a future reader from "fixing" a difference that is the answer.
DISTINCT_BY_DESIGN = [
    {
        "left": "closed_won_revenue_usd",
        "right": "country_attributed_won_revenue_usd",
        "reason": (
            "Total business revenue counts every closed-won deal; country-attributed "
            "revenue counts only the part assigned to a real country. The remainder "
            "is published as an explicit residual, never spread across countries."),
    },
    {
        "left": "google_ads_spend_usd",
        "right": "country_attributed_spend_usd",
        "reason": (
            "Google Ads geographic_view does not assign location-less spend to any "
            "country. The shortfall is the governed residual (PR-ADS-131), which is "
            "why an accepted residual is a truth-ready state rather than a mismatch."),
    },
]


def _dig(payload: dict, path: str):
    """Follow a dotted path; ``None`` when any step is absent."""
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _contract_problem(contract, spec: dict, metric_key: str,
                      consumer_window: tuple | None) -> str | None:
    """Check a reading's declared provenance against the registry. None = fine.

    PR-ADS-154C-F1 §2. Before this, the audit printed the ``canonical_source``
    the REGISTRY expected and called that provenance — a claim about itself, not
    about the number. A page could read anything at all and the audit would echo
    the source it wished for.

    Now each response declares, per metric, where its figure came from, and this
    checks the declaration. A missing contract is a failure: silence is not proof
    that the right source was used.
    """
    if not isinstance(contract, dict):
        return f"no {METRIC_TRUTH_KEY}.{metric_key} contract published"
    if contract.get("data_source") != spec["canonical_source"]:
        return (f"data_source is {contract.get('data_source')!r}, "
                f"expected {spec['canonical_source']!r}")
    if contract.get("scope") != spec["scope"]:
        return f"scope is {contract.get('scope')!r}, expected {spec['scope']!r}"
    if contract.get("truth_status") != TRUTH_READY:
        return f"truth_status is {contract.get('truth_status')!r}, expected 'ready'"
    if contract.get("fallback_used") is not False:
        return f"fallback_used is {contract.get('fallback_used')!r}, expected False"
    if consumer_window is not None:
        declared = (contract.get("window_start"), contract.get("window_end"),
                    contract.get("timezone"))
        if declared != consumer_window:
            return (f"contract window {declared} does not match the window the "
                    f"consumer published {consumer_window}")
    return None


def _consistent(readings: list, field: str) -> bool:
    """Do all readings' contracts agree on ``field``? Absent values are ignored."""
    seen = {(r.get("contract") or {}).get(field) for r in readings
            if (r.get("contract") or {}).get(field) is not None}
    return len(seen) <= 1


def _coverage_proven(consumers: dict, spec: dict) -> tuple[bool, str]:
    """Was the coverage behind THIS metric actually proven?

    Parity is only evidence when the thing consumers agree about was
    established. On an unproven window the canonical query returns zero rows and
    every consumer renders the same ``0.0`` — perfect agreement about a number
    nobody measured, which is the distinction PR-ADS-153F drew for geo.

    PR-ADS-154C-F1 §5: the proof is chosen per metric. Campaign-spend coverage
    was previously used as the universal answer, which meant a country metric
    could be certified by evidence about campaign spend, and revenue by evidence
    about neither. Each authority is now asked about itself:

      * campaign spend  -> campaign coverage AND FX coverage
      * country metrics -> geo coverage AND an accepted country reconciliation
                           (`verified` or `reconciled_with_residual`)
      * revenue / customers -> the deal ledger is available
      * funnel metrics  -> the contact funnel is available
    """
    mart = (consumers.get("revenue_decision_mart") or {}).get("payload") or {}
    spend_truth = mart.get("spend_truth") or {}
    summary = mart.get("summary") or {}
    source = spec["canonical_source"]

    if source == SOURCE_CANONICAL_SPEND:
        campaign = spend_truth.get("campaign_spend_status")
        fx = spend_truth.get("fx_status")
        if campaign == "verified" and fx == "verified":
            return True, ""
        return False, (f"campaign spend coverage is {campaign!r} and FX coverage "
                       f"is {fx!r}")

    if source == SOURCE_CANONICAL_GEO:
        country = spend_truth.get("country_spend_status")
        if country in ACCEPTED_COUNTRY_STATES:
            return True, ""
        return False, (f"country reconciliation is {country!r}, which is not one "
                       f"of {sorted(ACCEPTED_COUNTRY_STATES)}")

    if source in (SOURCE_REVENUE_DECISION_MART, SOURCE_CANONICAL_DEAL_LEDGER):
        if summary.get("revenue_available") is True:
            return True, ""
        return False, (f"canonical revenue availability is "
                       f"{summary.get('revenue_available')!r}")

    if source == SOURCE_CANONICAL_FUNNEL:
        if summary.get("leads_available", summary.get("sqls") is not None):
            return True, ""
        return False, "canonical contact funnel is unavailable"

    return True, ""


def _window_signature(payload: dict, window: str) -> tuple:
    """The (start, end, timezone) a consumer used, plus a problem description.

    Returns ``(signature, None)`` when the window is complete, or
    ``(None, reason)`` when it is not. A consumer that publishes no window has
    not agreed with anyone — it has declined to say what it measured — so the
    caller records a violation rather than dropping it from the comparison.

    ``start_date`` may be ``None`` only for ``all_time``, whose lower bound is
    genuinely open.
    """
    w = payload.get("window")
    if not isinstance(w, dict):
        return None, "no window block"
    if not w.get("key"):
        return None, "window block has no key"
    if not w.get("end_date"):
        return None, "window block has no end_date"
    if w.get("start_date") is None and window != "all_time":
        return None, f"window block has no start_date (window={window})"
    if not w.get("timezone"):
        return None, "window block has no effective timezone"
    return (w.get("start_date"), w.get("end_date"), w.get("timezone")), None


def _build_consumers(window: str, now: datetime | None) -> dict:
    """Build every production consumer once, capturing failures rather than raising.

    A consumer that raises is a finding, not a reason to abandon the audit: the
    other consumers still have something to say, and a silent abort would report
    fewer violations than exist.
    """
    from services.dashboard_overview_service import build_dashboard_overview  # noqa: PLC0415
    from services.dashboard_revenue_service import build_dashboard_revenue  # noqa: PLC0415
    from services.dashboard_countries_service import build_dashboard_countries  # noqa: PLC0415
    from services.dashboard_campaigns_service import build_dashboard_campaigns  # noqa: PLC0415
    from services.dashboard_channels_service import build_dashboard_channels  # noqa: PLC0415
    from services.dashboard_deals_service import build_dashboard_deals  # noqa: PLC0415
    from services.revenue_decision_mart import build_revenue_decision_mart  # noqa: PLC0415

    builders = {
        "dashboard/overview": lambda: build_dashboard_overview(window=window, now=now),
        "dashboard/revenue": lambda: build_dashboard_revenue(window=window, now=now),
        "dashboard/channels": lambda: build_dashboard_channels(window=window, now=now),
        "dashboard/campaigns": lambda: build_dashboard_campaigns(window=window, now=now),
        "dashboard/countries": lambda: build_dashboard_countries(window=window, now=now),
        "dashboard/deals": lambda: build_dashboard_deals(window=window, now=now),
        "revenue_decision_mart": lambda: build_revenue_decision_mart(
            window=window, view="campaign", now=now),
    }

    built: dict = {}
    for name, fn in builders.items():
        try:
            built[name] = {"payload": fn(), "error": None}
        except Exception as exc:  # noqa: BLE001
            built[name] = {"payload": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return built


def audit_window(window: str, now: datetime | None = None) -> dict:
    """Audit cross-page canonical parity for ONE business window."""
    resolved = resolve_canonical_window(window, now=now)
    consumers = _build_consumers(window, now)
    violations: list = []

    # ── Consumers that could not be built at all ─────────────────────────────
    for name, entry in consumers.items():
        if entry["error"]:
            violations.append({"code": V_CONSUMER_FAILED, "consumer": name,
                               "detail": entry["error"]})

    # ── Window parity: every built consumer must publish a COMPLETE window ───
    # PR-ADS-154C-F1: a consumer that omits its window used to contribute no
    # signature at all, so a single remaining signature read as unanimity. One
    # page silently dropping its window looked exactly like every page agreeing.
    window_rows = []
    signatures: dict = {}
    for name, entry in consumers.items():
        payload = entry["payload"]
        if payload is None:
            continue                       # already reported as V_CONSUMER_FAILED
        sig, problem = _window_signature(payload, window)
        window_rows.append({
            "consumer": name,
            "window_start": sig[0] if sig else None,
            "window_end": sig[1] if sig else None,
            "timezone": sig[2] if sig else None,
            "problem": problem,
        })
        if sig:
            signatures.setdefault(sig, []).append(name)
        else:
            violations.append({"code": V_WINDOW_MISSING, "consumer": name,
                               "detail": problem})
    if len(signatures) > 1:
        violations.append({
            "code": V_WINDOW_MISMATCH, "metric": None,
            "detail": "consumers resolved the same window key to different ranges",
            "ranges": [{"range": list(sig), "consumers": names}
                       for sig, names in signatures.items()],
        })

    # ── Fallback usage ───────────────────────────────────────────────────────
    # PR-ADS-154C-F1: the real dashboards publish `legacy_fallback_used` as a
    # TOP-LEVEL boolean and `source_truth` as a STRING. The previous check looked
    # only inside nested dicts of those names, so `isinstance(block, dict)` was
    # False for every production payload and the detection was completely inert —
    # a guard that could not fire on the shape it was written for.
    for name, entry in consumers.items():
        payload = entry["payload"] or {}
        for flag, code in (("fallback_used", V_FALLBACK_USED),
                           ("legacy_fallback_used", V_LEGACY_READ)):
            if payload.get(flag) is True:
                violations.append({"code": code, "consumer": name,
                                   "detail": f"top-level {flag} is true"})
            for block_name in ("truth_contract", "disclosure", "source_truth",
                               "country_truth", "spend_truth"):
                block = payload.get(block_name)
                if isinstance(block, dict) and block.get(flag) is True:
                    violations.append({
                        "code": code, "consumer": name,
                        "detail": f"{block_name}.{flag} is true"})

    # ── Value parity, within each metric identity ────────────────────────────
    # Agreement is only evidence when the coverage behind it was proven. See
    # `_coverage_proven`: an unproven window makes every consumer render the same
    # zero, which is unanimity about a number nobody measured.
    metrics = []
    for metric_key, spec in METRIC_IDENTITIES.items():
        readings = []
        for consumer_name, path in spec["consumers"]:
            entry = consumers.get(consumer_name) or {}
            payload = entry.get("payload")
            value = _dig(payload, path) if payload else None
            contract = ((payload or {}).get(METRIC_TRUTH_KEY) or {}).get(metric_key)
            problem = _contract_problem(contract, spec, metric_key,
                                        _window_signature(payload or {}, window)[0])
            readings.append({"consumer": consumer_name, "path": path, "value": value,
                             "contract": contract, "contract_problem": problem})

        present = [r for r in readings if r["value"] is not None]
        missing = [r for r in readings if r["value"] is None]
        bad_contract = [r for r in readings if r["contract_problem"]]
        distinct = {_norm(r["value"]) for r in present}
        baseline = present[0]["value"] if present else None

        for r in readings:
            r["difference"], r["difference_pct"] = _diff(r["value"], baseline)

        # PR-ADS-154C-F1 §5: coverage proof is chosen per METRIC, not one
        # campaign-spend answer applied to everything. Geo metrics need geo
        # coverage and an accepted country reconciliation; revenue needs the deal
        # ledger; funnel metrics need the contact funnel.
        coverage_proven, coverage_detail = _coverage_proven(consumers, spec)

        if not present:
            status = "unavailable"
            violations.append({
                "code": V_SOURCE_UNAVAILABLE, "metric": metric_key,
                "detail": "no consumer published this metric"})
        elif missing:
            # PR-ADS-154C-F1 §3: comparing only the readings that happen to be
            # present let a page that dropped a metric look like agreement. Every
            # REGISTERED consumer must answer, or the others are agreeing among
            # themselves about a question one of them declined.
            status = "consumer_missing"
            violations.append({
                "code": V_CONSUMER_METRIC_MISSING, "metric": metric_key,
                "detail": (f"{len(missing)} registered consumer(s) published no "
                           f"value while others did"),
                "missing": [{"consumer": r["consumer"], "path": r["path"]}
                            for r in missing]})
        elif bad_contract:
            # Provenance is CHECKED, not echoed. Printing the registry's expected
            # `canonical_source` proves nothing about where the number came from.
            status = "unverified_provenance"
            violations.append({
                "code": V_CONTRACT_INVALID, "metric": metric_key,
                "detail": "; ".join(f"{r['consumer']}: {r['contract_problem']}"
                                    for r in bad_contract)})
        elif len(distinct) > 1:
            status = "mismatch"
            violations.append({
                "code": V_VALUE_MISMATCH, "metric": metric_key,
                "detail": f"{len(distinct)} distinct values across consumers",
                "readings": [{"consumer": r["consumer"], "value": r["value"]}
                             for r in present]})
        elif not _consistent(present, "currency") or not _consistent(present, "customer_id"):
            status = "unverified_provenance"
            violations.append({
                "code": V_CONTRACT_INCONSISTENT, "metric": metric_key,
                "detail": "consumers disagree on currency or customer identity",
                "readings": [{"consumer": r["consumer"],
                              "currency": (r["contract"] or {}).get("currency"),
                              "customer_id": (r["contract"] or {}).get("customer_id")}
                             for r in present]})
        elif not coverage_proven:
            status = "unproven"
            violations.append({
                "code": V_AGREEMENT_ON_UNPROVEN_COVERAGE, "metric": metric_key,
                "detail": (f"every consumer published {baseline!r}, but {coverage_detail}"
                           " — a figure over an unproven range is not a measurement")})
        else:
            status = "identical"

        metrics.append({
            "metric": metric_key,
            "label": spec["label"],
            "scope": spec["scope"],
            "canonical_source": spec["canonical_source"],
            "status": status,
            "value": baseline,
            "readings": readings,
        })

    ok = not violations
    return {
        "window": window,
        "window_label": resolved.get("label"),
        "window_start": resolved.get("start_date"),
        "window_end": resolved.get("end_date"),
        "timezone": resolved.get("timezone"),
        "ok": ok,
        "consumers_inspected": sorted(consumers),
        "consumer_windows": window_rows,
        "metrics": metrics,
        "distinct_by_design": DISTINCT_BY_DESIGN,
        "violations": violations,
        "violation_codes": sorted({v["code"] for v in violations}),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _norm(value):
    """Normalise a reading for EXACT comparison.

    PR-ADS-154C-F1: this rounded to six decimals while the command claimed
    parity was exact. Rounding is a tolerance wearing different clothes — a
    narrow one, but the audit's whole argument is that two renderings of the same
    canonical figure must not need one. Two values that differ at the seventh
    decimal are two answers to one question, and if that ever happens it is worth
    knowing rather than smoothing away.

    ``Decimal(str(...))`` is used rather than raw floats so ``2.0`` and ``2``
    compare equal and the textual form does not reintroduce binary
    representation noise of its own.
    """
    from decimal import Decimal, InvalidOperation  # noqa: PLC0415

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:      # nan / inf
            return repr(value)
    return value


def _diff(value, baseline):
    """Absolute and percentage difference from the baseline reading."""
    if value is None or baseline is None:
        return None, None
    try:
        d = round(float(value) - float(baseline), 6)
    except (TypeError, ValueError):
        return None, None
    if not baseline:
        return d, None
    return d, round(abs(d) / abs(float(baseline)) * 100, 4)


def audit_all_windows(windows=None, now: datetime | None = None) -> dict:
    """Audit every required business window and roll the verdict up.

    ``ok`` is the conjunction: one window failing parity fails the audit, because
    a page that agrees this quarter and disagrees year-to-date is not a page that
    agrees.
    """
    keys = list(windows or WINDOW_KEYS)
    results = [audit_window(w, now=now) for w in keys]
    all_violations = [{**v, "window": r["window"]}
                      for r in results for v in r["violations"]]
    return {
        "ok": all(r["ok"] for r in results),
        "windows_audited": keys,
        "results": results,
        "violations": all_violations,
        "violation_codes": sorted({v["code"] for v in all_violations}),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
