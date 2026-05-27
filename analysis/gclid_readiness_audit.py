"""
GCLID Bridge Readiness Audit (PR-ADS-081)

Responsibility:
  - Audit whether the system is ready for click-level GCLID attribution.
  - Inspect current available data only.
  - NO writes to Google Ads, HubSpot, or any external system.
  - NO ROAS math.
  - NO API code.

Readiness statuses:
  READY       — Tier 1 possible matches exist and both HubSpot + Windsor expose GCLID.
  PARTIAL     — HubSpot has some GCLID coverage but Windsor/click matching is incomplete.
  NOT_READY   — No reliable Tier 1 GCLID match path exists.
  UNKNOWN     — Required source files are missing or empty.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"


def _load_won_deals() -> list:
    """Load won deals from data/hubspot_won_deals.json."""
    deals_file = DATA_DIR / "hubspot_won_deals.json"
    if not deals_file.exists():
        return []
    try:
        with open(deals_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _load_windsor_data() -> list:
    """Load Windsor campaign data from data/campaign_performance.json."""
    windsor_file = DATA_DIR / "campaign_performance.json"
    if not windsor_file.exists():
        return []
    try:
        with open(windsor_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def audit_hubspot_deal_gclid_coverage(deals: list) -> dict:
    """Audit GCLID coverage on HubSpot won deals.

    Returns counts of deals with/without direct GCLID.
    """
    total = len(deals)
    with_direct_gclid = sum(1 for d in deals if d.get("gclid"))
    without_gclid = total - with_direct_gclid

    return {
        "won_deals": total,
        "deals_with_direct_gclid": with_direct_gclid,
        "deals_without_gclid": without_gclid,
    }


def audit_hubspot_contact_gclid_coverage(deals: list) -> dict:
    """Audit GCLID coverage via associated contacts.

    Returns count of deals that have an associated contact GCLID.
    """
    with_contact_gclid = sum(
        1 for d in deals if d.get("associated_contact_gclid")
    )
    return {
        "deals_with_contact_gclid": with_contact_gclid,
    }


def audit_windsor_gclid_coverage(rows: list) -> dict:
    """Audit GCLID presence in Windsor campaign data.

    Returns count of Windsor rows that contain a GCLID field.
    """
    with_gclid = sum(1 for r in rows if r.get("gclid"))
    return {
        "windsor_rows_total": len(rows),
        "windsor_rows_with_gclid": with_gclid,
    }


def audit_joinability(deals: list, windsor_rows: list) -> dict:
    """Audit whether exact GCLID matches are possible between HubSpot and Windsor.

    Returns counts of potential tier matches.
    """
    # Build Windsor GCLID index
    windsor_gclids = {r.get("gclid") for r in windsor_rows if r.get("gclid")}

    tier_1_possible = 0
    tier_2_source_tag = 0
    tier_3_estimated = 0

    for deal in deals:
        deal_gclid = deal.get("gclid") or deal.get("associated_contact_gclid")
        if deal_gclid and deal_gclid in windsor_gclids:
            tier_1_possible += 1
            continue

        source = deal.get("hs_analytics_source", "")
        source_data_1 = deal.get("hs_analytics_source_data_1")
        if source and source.upper() == "PAID_SEARCH" and source_data_1:
            tier_2_source_tag += 1
            continue

        tier_3_estimated += 1

    return {
        "tier_1_possible_matches": tier_1_possible,
        "tier_2_source_tag_matches": tier_2_source_tag,
        "tier_3_estimated_required": tier_3_estimated,
    }


def _compute_readiness_score(
    deal_coverage: dict,
    contact_coverage: dict,
    windsor_coverage: dict,
    joinability: dict,
) -> int:
    """Compute a transparent 0–100 readiness score.

    Scoring:
      +30 HubSpot deals/contact GCLID coverage exists
      +30 Windsor/click GCLID coverage exists
      +25 Exact matches found
      +10 Source-tag fallback coverage exists
      +5  Country-level estimate warning is active (always true until GCLID wired)
    """
    score = 0

    # +30 HubSpot GCLID coverage
    has_deal_gclid = deal_coverage.get("deals_with_direct_gclid", 0) > 0
    has_contact_gclid = contact_coverage.get("deals_with_contact_gclid", 0) > 0
    if has_deal_gclid or has_contact_gclid:
        score += 30

    # +30 Windsor GCLID coverage
    if windsor_coverage.get("windsor_rows_with_gclid", 0) > 0:
        score += 30

    # +25 Exact matches found
    if joinability.get("tier_1_possible_matches", 0) > 0:
        score += 25

    # +10 Source-tag fallback
    if joinability.get("tier_2_source_tag_matches", 0) > 0:
        score += 10

    # +5 Country-level estimate warning (always active until GCLID is wired)
    score += 5

    return min(score, 100)


def _determine_readiness_status(
    deal_coverage: dict,
    contact_coverage: dict,
    windsor_coverage: dict,
    joinability: dict,
) -> str:
    """Determine readiness status using explicit rules.

    READY     — Tier 1 possible matches exist and both HubSpot + Windsor expose GCLID.
    PARTIAL   — HubSpot has some GCLID coverage but Windsor/click matching is incomplete.
    NOT_READY — No reliable Tier 1 GCLID match path exists.
    UNKNOWN   — Required source files are missing or empty.
    """
    has_deal_gclid = deal_coverage.get("deals_with_direct_gclid", 0) > 0
    has_contact_gclid = contact_coverage.get("deals_with_contact_gclid", 0) > 0
    has_windsor_gclid = windsor_coverage.get("windsor_rows_with_gclid", 0) > 0
    has_tier_1 = joinability.get("tier_1_possible_matches", 0) > 0

    # UNKNOWN — no data available
    won_deals = deal_coverage.get("won_deals", 0)
    windsor_total = windsor_coverage.get("windsor_rows_total", 0)
    if won_deals == 0 and windsor_total == 0:
        return "UNKNOWN"

    # READY — tier 1 matches exist with both sides having GCLID
    if has_tier_1 and (has_deal_gclid or has_contact_gclid) and has_windsor_gclid:
        return "READY"

    # PARTIAL — HubSpot has some GCLID but Windsor incomplete
    if (has_deal_gclid or has_contact_gclid) and not has_windsor_gclid:
        return "PARTIAL"

    # NOT_READY — no reliable Tier 1 path
    return "NOT_READY"


def _identify_blockers(
    deal_coverage: dict,
    contact_coverage: dict,
    windsor_coverage: dict,
    joinability: dict,
) -> list:
    """Identify blockers preventing GCLID readiness."""
    blockers = []

    if windsor_coverage.get("windsor_rows_with_gclid", 0) == 0:
        blockers.append({
            "severity": "high",
            "code": "WINDSOR_GCLID_MISSING",
            "message": "Windsor campaign data does not currently expose click-level GCLID rows.",
        })

    if deal_coverage.get("deals_with_direct_gclid", 0) == 0:
        blockers.append({
            "severity": "high",
            "code": "HUBSPOT_DEAL_GCLID_MISSING",
            "message": "No HubSpot won deals have a direct GCLID field populated.",
        })

    if contact_coverage.get("deals_with_contact_gclid", 0) == 0:
        blockers.append({
            "severity": "medium",
            "code": "HUBSPOT_CONTACT_GCLID_MISSING",
            "message": "No associated contacts have hs_google_click_id populated.",
        })

    if joinability.get("tier_1_possible_matches", 0) == 0:
        blockers.append({
            "severity": "high",
            "code": "NO_TIER_1_MATCHES",
            "message": "No exact GCLID match path exists between HubSpot deals and Windsor rows.",
        })

    return blockers


def _generate_next_checks(
    deal_coverage: dict,
    contact_coverage: dict,
    windsor_coverage: dict,
) -> list:
    """Generate actionable next checks based on current state."""
    checks = []

    if windsor_coverage.get("windsor_rows_with_gclid", 0) == 0:
        checks.append(
            "Confirm whether Windsor connector can pull click-level GCLID data."
        )

    if contact_coverage.get("deals_with_contact_gclid", 0) == 0:
        checks.append(
            "Confirm HubSpot forms preserve hs_google_click_id on contact creation."
        )

    if deal_coverage.get("deals_with_direct_gclid", 0) == 0:
        checks.append(
            "Confirm deal-contact association coverage for paid-search won deals."
        )

    if not checks:
        checks.append(
            "All GCLID prerequisites appear met. Verify match quality in production."
        )

    return checks


def run_gclid_readiness_audit(window_days: int = 60) -> dict:
    """Run the full GCLID bridge readiness audit.

    This function inspects current available data only. It does not write to
    Google Ads, HubSpot, or any external system.

    Args:
        window_days: Number of days to consider for the audit window.

    Returns:
        Complete audit result dict with readiness status, score, blockers, etc.
    """
    deals = _load_won_deals()
    windsor_rows = _load_windsor_data()

    deal_coverage = audit_hubspot_deal_gclid_coverage(deals)
    contact_coverage = audit_hubspot_contact_gclid_coverage(deals)
    windsor_coverage = audit_windsor_gclid_coverage(windsor_rows)
    joinability = audit_joinability(deals, windsor_rows)

    readiness_status = _determine_readiness_status(
        deal_coverage, contact_coverage, windsor_coverage, joinability
    )
    readiness_score = _compute_readiness_score(
        deal_coverage, contact_coverage, windsor_coverage, joinability
    )
    blockers = _identify_blockers(
        deal_coverage, contact_coverage, windsor_coverage, joinability
    )
    next_checks = _generate_next_checks(
        deal_coverage, contact_coverage, windsor_coverage
    )

    now = datetime.now(timezone.utc)

    return {
        "window": f"{window_days}d",
        "generated_at": now.isoformat(),
        "readiness_status": readiness_status,
        "readiness_score": readiness_score,
        "summary": {
            "won_deals": deal_coverage.get("won_deals", 0),
            "deals_with_direct_gclid": deal_coverage.get("deals_with_direct_gclid", 0),
            "deals_with_contact_gclid": contact_coverage.get("deals_with_contact_gclid", 0),
            "deals_without_gclid": deal_coverage.get("deals_without_gclid", 0),
            "windsor_rows_with_gclid": windsor_coverage.get("windsor_rows_with_gclid", 0),
            "tier_1_possible_matches": joinability.get("tier_1_possible_matches", 0),
            "tier_2_source_tag_matches": joinability.get("tier_2_source_tag_matches", 0),
            "tier_3_estimated_required": joinability.get("tier_3_estimated_required", 0),
        },
        "blockers": blockers,
        "next_checks": next_checks,
        "notes": [
            "This audit is read-only. It does not implement the GCLID bridge.",
        ],
    }
