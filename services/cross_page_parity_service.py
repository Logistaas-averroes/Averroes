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
    SOURCE_CANONICAL_GEO,
    SOURCE_CANONICAL_SPEND,
    SOURCE_REVENUE_DECISION_MART,
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


def _coverage_proven(consumers: dict) -> tuple[bool, str | None]:
    """Was the canonical spend coverage behind these figures actually proven?

    Parity between consumers is only evidence when the thing they agree about was
    established. On a window with no verified coverage, the canonical campaign
    spend query returns zero rows and every consumer renders the same ``0.0`` —
    perfect agreement about a number nobody measured. Zero rows over an unproven
    range is not a measured zero; it is the absence of a measurement, which is
    the distinction PR-ADS-153F drew for geo and which applies identically here.

    So the audit asks the mart what its own spend truth is, and refuses to count
    agreement as parity while that truth is anything but verified. The figures
    are still reported; only the verdict changes.
    """
    mart = (consumers.get("revenue_decision_mart") or {}).get("payload") or {}
    spend_truth = mart.get("spend_truth") or {}
    status = spend_truth.get("campaign_spend_status")
    if status == "verified":
        return True, None
    return False, status or "unknown"


def _window_signature(payload: dict) -> tuple | None:
    """The (start, end, timezone) a consumer actually used, or None."""
    w = payload.get("window") or {}
    if not isinstance(w, dict) or not w.get("end_date"):
        return None
    return (w.get("start_date"), w.get("end_date"), w.get("timezone"))


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

    # ── Window parity: every consumer must have used the SAME range ──────────
    window_rows = []
    signatures: dict = {}
    for name, entry in consumers.items():
        payload = entry["payload"]
        sig = _window_signature(payload) if payload else None
        window_rows.append({
            "consumer": name,
            "window_start": sig[0] if sig else None,
            "window_end": sig[1] if sig else None,
            "timezone": sig[2] if sig else None,
        })
        if sig:
            signatures.setdefault(sig, []).append(name)
    if len(signatures) > 1:
        violations.append({
            "code": V_WINDOW_MISMATCH, "metric": None,
            "detail": "consumers resolved the same window key to different ranges",
            "ranges": [{"range": list(sig), "consumers": names}
                       for sig, names in signatures.items()],
        })

    # ── Fallback usage: a canonical metric may never come from a fallback ────
    for name, entry in consumers.items():
        payload = entry["payload"] or {}
        for block_name in ("truth_contract", "disclosure", "source_truth"):
            block = payload.get(block_name)
            if isinstance(block, dict) and block.get("fallback_used") is True:
                violations.append({
                    "code": V_FALLBACK_USED, "consumer": name,
                    "detail": f"{block_name}.fallback_used is true"})
            if isinstance(block, dict) and block.get("legacy_fallback_used") is True:
                violations.append({
                    "code": V_LEGACY_READ, "consumer": name,
                    "detail": f"{block_name}.legacy_fallback_used is true"})

    # ── Value parity, within each metric identity ────────────────────────────
    # Agreement is only evidence when the coverage behind it was proven. See
    # `_coverage_proven`: an unproven window makes every consumer render the same
    # zero, which is unanimity about a number nobody measured.
    coverage_proven, coverage_status = _coverage_proven(consumers)
    metrics = []
    for metric_key, spec in METRIC_IDENTITIES.items():
        readings = []
        for consumer_name, path in spec["consumers"]:
            entry = consumers.get(consumer_name) or {}
            payload = entry.get("payload")
            value = _dig(payload, path) if payload else None
            readings.append({"consumer": consumer_name, "path": path, "value": value})

        present = [r for r in readings if r["value"] is not None]
        distinct = {_norm(r["value"]) for r in present}
        baseline = present[0]["value"] if present else None

        for r in readings:
            r["difference"], r["difference_pct"] = _diff(r["value"], baseline)

        if not present:
            status = "unavailable"
            violations.append({
                "code": V_SOURCE_UNAVAILABLE, "metric": metric_key,
                "detail": "no consumer published this metric"})
        elif len(distinct) > 1:
            status = "mismatch"
            violations.append({
                "code": V_VALUE_MISMATCH, "metric": metric_key,
                "detail": f"{len(distinct)} distinct values across consumers",
                "readings": [{"consumer": r["consumer"], "value": r["value"]}
                             for r in present]})
        elif (not coverage_proven
                and spec["canonical_source"] in (SOURCE_CANONICAL_SPEND,
                                                 SOURCE_CANONICAL_GEO)):
            status = "unproven"
            violations.append({
                "code": V_AGREEMENT_ON_UNPROVEN_COVERAGE, "metric": metric_key,
                "detail": (f"every consumer published {baseline!r}, but canonical "
                           f"spend coverage is {coverage_status!r} — zero rows over "
                           "an unproven range is not a measured zero")})
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
    """Compare numerics by value, everything else by identity of content."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
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
