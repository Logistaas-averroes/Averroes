"""
Acquisition-source classification (PR-ADS-117)

Maps HubSpot Original Source (primary) + Original Source Drill-Down (detail) to a
business acquisition group, deterministically and auditably. Pure functions — no
DB, no network — so the rules are exhaustively testable.

Doctrine:
  - Google Ads is the ONLY group with Google Ads spend, so the ONLY group that
    may ever show ROAS. Every other group is revenue-only.
  - Unknown / missing source values are Unclassified — NEVER defaulted to Organic.
  - Deal revenue is never split or allocated across sources.
"""

from __future__ import annotations

# Bump when the mapping rules change so historical classifications stay auditable.
RULE_VERSION = "v1"

# Acquisition groups (stable identifiers persisted with every classification).
GROUP_GOOGLE_ADS = "google_ads"
GROUP_OTHER_PAID = "other_paid"
GROUP_ORGANIC = "organic"
GROUP_OFFLINE = "offline"
GROUP_UNCLASSIFIED = "unclassified"

GROUP_LABELS = {
    GROUP_GOOGLE_ADS: "Google Ads",
    GROUP_OTHER_PAID: "Other Paid",
    GROUP_ORGANIC: "Organic",
    GROUP_OFFLINE: "Offline",
    GROUP_UNCLASSIFIED: "Unclassified / Needs Review",
}

# Only Google Ads has a connected spend source → only Google Ads may show ROAS.
GROUPS_WITH_SPEND = {GROUP_GOOGLE_ADS}

# Primary (Original Source) → group. Keys are normalised (lowercase, underscores
# treated as spaces).
_PRIMARY_OTHER_PAID = {"paid social", "other campaigns", "email marketing"}
_PRIMARY_ORGANIC = {
    "direct traffic", "organic search", "organic social", "direct email", "referrals",
}
_PRIMARY_OFFLINE = {"offline sources", "offline source"}

# Within "Offline Sources", the drill-down detail decides the group.
_OFFLINE_DETAIL_OTHER_PAID = ("salesnash", "events")
_OFFLINE_DETAIL_ORGANIC = ("reseller", "referral", "direct email")


def normalize_source(value) -> str:
    """Case/format-insensitive normalisation: lowercase, underscores == spaces.

    Slashes are preserved (e.g. "SalesNash / Events") so detail keywords still
    match. Returns "" for None/blank.
    """
    if value is None:
        return ""
    text = str(value).replace("_", " ").strip().lower()
    return " ".join(text.split())


def classify_source(source_primary_raw, source_detail_raw) -> str:
    """Classify a contact's HubSpot source into an acquisition group.

    Unknown or missing values → Unclassified (never Organic).
    """
    primary = normalize_source(source_primary_raw)
    detail = normalize_source(source_detail_raw)

    if not primary:
        return GROUP_UNCLASSIFIED

    if primary == "paid search":
        return GROUP_GOOGLE_ADS
    if primary in _PRIMARY_OTHER_PAID:
        return GROUP_OTHER_PAID
    if primary in _PRIMARY_ORGANIC:
        return GROUP_ORGANIC
    if primary in _PRIMARY_OFFLINE:
        if any(k in detail for k in _OFFLINE_DETAIL_OTHER_PAID):
            return GROUP_OTHER_PAID
        if any(k in detail for k in _OFFLINE_DETAIL_ORGANIC):
            return GROUP_ORGANIC
        return GROUP_OFFLINE
    if primary == "other offline sources":
        return GROUP_OFFLINE

    return GROUP_UNCLASSIFIED


# ── Deal-level source attribution safety ─────────────────────────────────────

ATTR_ATTRIBUTED = "attributed"
ATTR_AMBIGUOUS = "ambiguous"
ATTR_UNCLASSIFIED = "unclassified"


def attribute_deal(contact_groups) -> dict:
    """Attribute a closed-won deal to a source group from its associated contacts.

    Rules (revenue is NEVER split or allocated across sources):
      - one classified contact, or multiple in the SAME group → attribute to it
      - multiple contacts with CONFLICTING groups → ambiguous
      - no associated classified contact → unclassified

    Returns {acquisition_group, attribution_status, attribution_reason}, where
    acquisition_group is the real group when attributed, else the status bucket
    ("ambiguous" / "unclassified") so aggregation counts each deal exactly once.
    """
    classified = [g for g in (contact_groups or []) if g and g != GROUP_UNCLASSIFIED]
    if not classified:
        return {
            "acquisition_group": ATTR_UNCLASSIFIED,
            "attribution_status": ATTR_UNCLASSIFIED,
            "attribution_reason": "no_classified_contact",
        }
    distinct = set(classified)
    if len(distinct) == 1:
        group = next(iter(distinct))
        return {
            "acquisition_group": group,
            "attribution_status": ATTR_ATTRIBUTED,
            "attribution_reason": "single_contact" if len(classified) == 1 else "multi_same_group",
        }
    return {
        "acquisition_group": ATTR_AMBIGUOUS,
        "attribution_status": ATTR_AMBIGUOUS,
        "attribution_reason": "multi_conflicting_groups",
    }
