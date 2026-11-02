"""
services/canonical_unit_economics_service.py

PR-ADS-153E-B — Unit Economics on canonical sources only.

What this replaces
------------------
Unit Economics was the last page still served by the third revenue lineage:

    analysis/roas_calculator.compute_all_campaign_roas()
      ├─ attribute_deals()        → data/attributed_deals.json  (local JSON)
      └─ _load_windsor_spend()    → data/campaign_performance.json (Windsor)

That chain had no deduplication by ``deal_id``, no currency doctrine, no
business windows (it used rolling ad-style day windows), and it summed
``hs_acv or amount`` — an unverified number in an unknown currency — as USD. It
also treated a missing amount as ``0``, so a deal with no recorded value silently
lowered CAC and inflated LTV/CAC. Two pages could therefore disagree about how
many customers the same period produced, and Unit Economics was usually the
optimistic one.

What it uses now
----------------
* **Advertising cost** — the canonical Google Ads spend truth
  (``services.revenue_spend_truth_service``), the same USD denominator the mart
  and Revenue by Source show. Never Windsor, never a local file.
* **Customers and revenue** — the canonical deal ledger through
  ``services.canonical_revenue_service``, at an EXPLICIT attribution scope.
* **Business totals** — ``all_source`` scope, so "the business closed N
  customers" counts every won deal, attributed or not.
* **Business windows** — ``analysis.business_windows``. Rolling day windows are
  gone: a 60-day lookback cuts a B2B sales cycle in half and gave this page a
  different period from every other revenue page.

What it withholds, and why
--------------------------
LTV/CAC and payback need recurring revenue per deal. ``hs_mrr`` / ``hs_arr`` are
not part of the canonical ledger, and the local JSON that carried them is not an
admissible source. So those metrics are returned as ``None`` with a stated
reason rather than computed from a source this PR just removed. Average deal
value is canonical and is returned as a real number.

PR-ADS-161E: CAC and attribution ROAS are UNAVAILABLE here. Closed-won outcomes
are dated by close, spend by the day it was spent; in a long B2B sales cycle a
deal closing this window was usually acquired by spend from an earlier one, so
the two do not share an acquisition cohort. What is published instead is two
explicitly named non-cohort operational ratios over the same window:
``period_revenue_per_ad_spend`` and ``period_spend_per_customer_won``.

Withholding is the honest outcome: an LTV/CAC of 4.2x derived from a file nobody
maintains is worse than an explicit "not available — recurring revenue is not
canonical yet", because only one of them can be acted on safely.

Read-only. No external API calls, no writes.
"""

from __future__ import annotations

from services import canonical_revenue_service as _canonical_snapshot  # PR-ADS-161E: one snapshot per page
import logging
from datetime import datetime, timezone

from analysis import revenue_scope
from analysis.business_windows import resolve_window
from services import canonical_revenue_service as canonical_revenue

log = logging.getLogger(__name__)

# The scope an ADVERTISING efficiency metric is computed at. The period ratios
# divide by (or into) Google Ads spend, so their numerator must be the population that spend could
# plausibly have produced — never the whole business.
DEFAULT_ADVERTISING_SCOPE = revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE

# Metrics that require per-deal recurring revenue, which the canonical ledger
# does not carry. Reported as unavailable with this reason, never estimated.
RECURRING_REVENUE_REASON = (
    "Recurring revenue (MRR/ARR) is not part of the canonical deal ledger. "
    "LTV, LTV/CAC and payback are withheld rather than computed from the "
    "retired local JSON chain."
)

WITHHELD_METRICS = ("ltv_to_cac", "payback_months", "avg_deal_mrr",
                    "ltv_revenue_usd")


def _unavailable_metrics() -> list:
    return [{"metric": m, "reason": RECURRING_REVENUE_REASON}
            for m in WITHHELD_METRICS]


def _avg(total, count):
    """Average, or None. Never a fabricated 0 when the divisor is 0/unknown."""
    if total is None or not count:
        return None
    return round(float(total) / count, 2)


@_canonical_snapshot.single_snapshot
def build_unit_economics(window: str = "current_quarter",
                         scope: str = DEFAULT_ADVERTISING_SCOPE,
                         now: datetime | None = None) -> dict:
    """Unit economics for one business window, at one declared attribution scope.

    Returns a contract carrying BOTH populations, each labelled:

    * ``business`` — ``all_source`` customers and revenue. This is the company.
    * ``advertising`` — the selected scope, plus canonical Google Ads spend and
      the two period ratios (``period_revenue_per_ad_spend``,
      ``period_spend_per_customer_won``). CAC and ROAS are unavailable: not one
      acquisition cohort.

    Fails closed: when the canonical ledger is unreadable or its coverage is not
    proven, every revenue-derived figure is ``None`` with a reason, and no legacy
    source is consulted.

    Raises ValueError for an unsupported business window.
    """
    from services.revenue_attribution_service import build_revenue_attribution
    from services.revenue_spend_truth_service import build_google_ads_spend_truth

    resolved = resolve_window(window, now=now)
    scope = revenue_scope.normalize_scope(scope)
    generated_at = datetime.now(timezone.utc).isoformat()

    base = canonical_revenue.load_won_deals(window, now=now)
    spend_truth = build_google_ads_spend_truth(window, now=now)
    # `usd_spend` is None whenever FX or coverage is unverified — the spend truth
    # already refuses to relabel native GBP as USD, so a null here means the
    # denominator is unknown and the period ratios must stay unknown with it.
    spend_usd = spend_truth.get("usd_spend")

    if not base.get("available"):
        # PR-ADS-161E: the population is not released (an undated or
        # conflicting won deal, unproven coverage, an outage), but the page
        # still republishes the canonical verdicts — "Withheld" with the reason
        # (and the admin View blockers link), never a bare "Unavailable".
        spend_reason = (None if spend_usd is not None else
                        f"google_ads_usd_spend_{spend_truth.get('state') or 'unavailable'}")
        business_canon = canonical_revenue.reader_row(base, [])
        advertising_canon = canonical_revenue.reader_row(
            base, [], spend_usd=spend_usd, spend_reason=spend_reason,
            period_ratio_scope_ok=scope in (revenue_scope.SCOPE_GOOGLE_ADS_SOURCE,
                                            revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE))
        withheld_block = lambda canon, label_scope: {  # noqa: E731
            "scope": label_scope,
            "scope_label": revenue_scope.SCOPE_LABELS[label_scope],
            "closed_won_deals": None, "customers": None, "revenue_usd": None,
            "avg_deal_value_usd": None, "metrics": canon["metrics"]}
        advertising_unavailable = {
            **withheld_block(advertising_canon, scope),
            "spend_usd": spend_usd,
            "spend_state": spend_truth.get("state"),
            "period_revenue_per_ad_spend": None,
            "period_spend_per_customer_won": None,
            "cac": None, "roas": None,
            "ltv_to_cac": None, "payback_months": None, "avg_deal_mrr": None,
        }
        return {
            "window": resolved,
            "generated_at": generated_at,
            "read_only": True,
            "source": canonical_revenue.CANONICAL_SOURCE,
            "spend_source": "canonical_google_ads_api",
            "revenue_available": False,
            "revenue_unavailable_reason": base.get("reason"),
            "revenue_violation_codes": base.get("violation_codes") or [],
            "as_of": base.get("as_of"),
            "business": withheld_block(business_canon,
                                       revenue_scope.SCOPE_ALL_SOURCE),
            "advertising": advertising_unavailable,
            "by_campaign": [],
            "spend_truth": spend_truth,
            "unavailable": _unavailable_metrics() + [{
                "metric": "unit_economics",
                "reason": ("Canonical revenue is unavailable: "
                           f"{base.get('reason')}. No legacy revenue source is "
                           "consulted as a fallback."),
            }],
            "legacy_fallback_used": False,
            "google_ads_conversion_value_used": False,
            "windsor_used": False,
        }

    ladder = canonical_revenue.get_scope_ladder(base=base)
    all_source = canonical_revenue.summarize_deals(
        base.get("deals"), revenue_scope.SCOPE_ALL_SOURCE)
    scoped = canonical_revenue.summarize_deals(base.get("deals"), scope)

    # PR-ADS-154C-F3-F1 §2. `revenue_usd` is now the TOTAL — None whenever any
    # deal in scope has no proven amount — and `known_revenue_usd` is the
    # diagnostic sum of the proven ones. This page publishes both, so a reader
    # can see the proven figure without it standing in for a total nobody knows.
    _SCOPE_FIELDS = (
        "scope", "scope_label", "won_deals", "revenue_usd", "known_revenue_usd",
        "revenue_deals", "currency_unavailable_deals", "currency_complete",
        "ambiguous_associations", "failed_associations")

    # PR-ADS-161E: the canonical reader contract. Customers are distinct
    # proven HubSpot companies (never the won-deal count), revenue is a total
    # or None, and the period ratios are derived only when every input is
    # published. CAC and ROAS are never derived here (not one cohort).
    business_canon = canonical_revenue.reader_row(
        base, canonical_revenue.filter_deals(base.get("deals"),
                                             revenue_scope.SCOPE_ALL_SOURCE))
    business = {
        **{k: all_source[k] for k in _SCOPE_FIELDS},
        "closed_won_deals": business_canon["closed_won_deals"],
        "customers": business_canon["customers"],
        "revenue_usd": business_canon["won_revenue"],
        # Average deal value = the published revenue TOTAL ÷ every won deal.
        # Never the proven part divided by the proven deals: that would describe
        # a different population than the deal count beside it.
        "avg_deal_value_usd": _avg(business_canon["won_revenue"],
                                   business_canon["closed_won_deals"]),
        "metrics": business_canon["metrics"],
    }

    spend_reason = (None if spend_usd is not None else
                    f"google_ads_usd_spend_{spend_truth.get('state') or 'unavailable'}")
    # PR-ADS-161E: Google Ads spend can only have produced Google Ads deals.
    # A ratio of all-source (or any non-Google) revenue or customers over Google
    # Ads spend credits advertising with outcomes it did not produce, so the
    # period ratios are unavailable for those scopes (``numerator_and_denominator_
    # scopes_differ``). The scope total itself is exact and is not gated.
    advertising_canon = canonical_revenue.reader_row(
        base, canonical_revenue.filter_deals(base.get("deals"), scope),
        spend_usd=spend_usd, spend_reason=spend_reason,
        period_ratio_scope_ok=scope in (revenue_scope.SCOPE_GOOGLE_ADS_SOURCE,
                                revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE))
    advertising = {
        **{k: scoped[k] for k in _SCOPE_FIELDS},
        "closed_won_deals": advertising_canon["closed_won_deals"],
        "customers": advertising_canon["customers"],
        "revenue_usd": advertising_canon["won_revenue"],
        "spend_usd": spend_usd,
        "spend_state": spend_truth.get("state"),
        # Non-cohort operational ratios over the same window (docs/48 §4).
        "period_revenue_per_ad_spend":
            advertising_canon["period_revenue_per_ad_spend"],
        "period_spend_per_customer_won":
            advertising_canon["period_spend_per_customer_won"],
        # Certified CAC / attribution ROAS: unavailable (one acquisition
        # cohort is required); the reason is in ``metrics``.
        "cac": None,
        "roas": None,
        "avg_deal_value_usd": _avg(advertising_canon["won_revenue"],
                                   advertising_canon["closed_won_deals"]),
        "metrics": advertising_canon["metrics"],
        # Every metric below needs per-deal recurring revenue.
        "ltv_to_cac": None,
        "payback_months": None,
        "avg_deal_mrr": None,
    }

    # Per-campaign rows reuse the campaign attribution build rather than assembling a
    # second campaign table. One table, one join, one set of numbers: a separate
    # aggregation here is exactly how two pages come to disagree about the same
    # campaign's spend.
    attribution = build_revenue_attribution(window, now=now)
    by_campaign = []
    for row in attribution.get("campaigns") or []:
        # The campaign attribution row already carries the canonical fields,
        # the period ratios included; nothing is re-derived here.
        by_campaign.append({
            "campaign": row.get("campaign_name"),
            "spend": row.get("spend"),
            "closed_won_deals": row.get("closed_won_deals"),
            "customers": row.get("customers"),
            "won_revenue_usd": row.get("won_revenue"),
            "revenue_unavailable_deals": row.get("revenue_unavailable_deals"),
            "period_revenue_per_ad_spend":
                row.get("period_revenue_per_ad_spend"),
            "period_spend_per_customer_won":
                row.get("period_spend_per_customer_won"),
            "cac": None,
            "roas": None,
            "metrics": row.get("metrics"),
            "avg_deal_value_usd": _avg(row.get("won_revenue"),
                                       row.get("closed_won_deals")),
            "ltv_to_cac": None,
            "payback_months": None,
            "verdict": None,
            "attribution_confidence": row.get("confidence"),
        })

    unavailable = _unavailable_metrics()
    for m in ("cac", "roas", "period_spend_per_customer_won",
              "period_revenue_per_ad_spend"):
        verdict = advertising["metrics"].get(m) or {}
        if verdict.get("status") != "published":
            unavailable.append({"metric": m, "status": verdict.get("status"),
                                "reason": verdict.get("reason")})
    if spend_usd is None:
        unavailable.append({
            "metric": "spend",
            "reason": ("Canonical Google Ads USD spend is unavailable "
                       f"({spend_truth.get('state')}) — the period ratios are "
                       "withheld rather than divided by an assumed spend."),
        })
    if not all_source["currency_complete"]:
        unavailable.append({
            "metric": "revenue_completeness",
            "reason": (f"{all_source['currency_unavailable_deals']} won deal(s) "
                       "have no provable USD value and are excluded from every "
                       "revenue total (counted, never valued at $0)."),
        })

    return {
        "window": resolved,
        "generated_at": generated_at,
        "read_only": True,
        "source": canonical_revenue.CANONICAL_SOURCE,
        "spend_source": "canonical_google_ads_api",
        "revenue_available": True,
        "revenue_unavailable_reason": None,
        "revenue_violation_codes": [],
        "as_of": base.get("as_of"),
        "scope": scope,
        "business_total_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "business": business,
        "advertising": advertising,
        "attribution_coverage": ladder,
        "by_campaign": by_campaign,
        "spend_truth": spend_truth,
        "unavailable": unavailable,
        "legacy_fallback_used": False,
        "google_ads_conversion_value_used": False,
        "windsor_used": False,
    }


__all__ = [
    "DEFAULT_ADVERTISING_SCOPE", "RECURRING_REVENUE_REASON",
    "WITHHELD_METRICS", "build_unit_economics",
]
