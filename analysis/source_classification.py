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


# ── Channel / Platform taxonomy (PR-ADS-133) ─────────────────────────────────
# A richer, still-pure classification that resolves a HubSpot source into a
# three-level hierarchy: group → channel → platform. The GROUP always agrees with
# ``classify_source`` (so existing pages/aggregation stay consistent); the channel
# and platform add business-useful structure for the Revenue by Source page.
#
# Doctrine preserved verbatim: only Google Ads / Paid Search is spend-connected
# and ROAS-eligible. Everything else is revenue-only. Unknown / missing sources
# are Unclassified — never defaulted to Organic. No source detail is invented.

# Channel identifiers (stable).
CH_PAID_SEARCH = "paid_search"
CH_PAID_SOCIAL = "paid_social"
CH_EMAIL_MARKETING = "email_marketing"
CH_OTHER_CAMPAIGNS = "other_campaigns"
CH_EVENTS = "events"
CH_SALESNASH = "salesnash"
CH_ORGANIC_SEARCH_DIRECT = "organic_search_direct"
CH_ORGANIC_SOCIAL = "organic_social"
CH_REFERRALS_PARTNERS = "referrals_partners"
CH_DIRECT_EMAIL = "direct_email"
CH_IMPORTED_CRM = "imported_crm"
CH_NEEDS_REVIEW = "needs_review"
CH_UNSPECIFIED = "unspecified"

CHANNEL_LABELS = {
    CH_PAID_SEARCH: "Paid Search",
    CH_PAID_SOCIAL: "Paid Social",
    CH_EMAIL_MARKETING: "Email Marketing",
    CH_OTHER_CAMPAIGNS: "Other Campaigns",
    CH_EVENTS: "Events",
    CH_SALESNASH: "SalesNash",
    CH_ORGANIC_SEARCH_DIRECT: "Organic Search + Direct",
    CH_ORGANIC_SOCIAL: "Organic Social",
    CH_REFERRALS_PARTNERS: "Referrals / Partners",
    CH_DIRECT_EMAIL: "Direct Email",
    CH_IMPORTED_CRM: "Imported CRM Records",
    CH_NEEDS_REVIEW: "Needs Review",
    CH_UNSPECIFIED: "Unspecified",
}

# Platform identifiers (stable).
PF_GOOGLE_ADS = "google_ads"
PF_LINKEDIN = "linkedin"
PF_META = "meta"
PF_UNKNOWN_PAID_SOCIAL = "unknown_paid_social"
PF_UNKNOWN_ORGANIC_SOCIAL = "unknown_organic_social"
PF_HUBSPOT_EMAIL = "hubspot_email"
PF_CAMPAIGN_EMAIL = "campaign_email"
PF_UNKNOWN_EMAIL = "unknown_email"
PF_CAMPAIGN = "campaign"
PF_UTM = "utm"
PF_UNKNOWN_CAMPAIGN = "unknown_campaign"
PF_EVENTS = "events"
PF_SALESNASH = "salesnash"
PF_ORGANIC_SEARCH = "organic_search"
PF_DIRECT_TRAFFIC = "direct_traffic"
PF_REFERRALS = "referrals"
PF_RESELLERS = "resellers"
PF_DIRECT_EMAIL = "direct_email"
PF_OFFLINE_MIGRATION = "offline_migration"
PF_NEEDS_REVIEW = "needs_review"
PF_UNSPECIFIED = "unspecified"

PLATFORM_LABELS = {
    PF_GOOGLE_ADS: "Google Ads",
    PF_LINKEDIN: "LinkedIn",
    PF_META: "Meta",
    PF_UNKNOWN_PAID_SOCIAL: "Unknown Paid Social",
    PF_UNKNOWN_ORGANIC_SOCIAL: "Unknown Organic Social",
    PF_HUBSPOT_EMAIL: "HubSpot Email",
    PF_CAMPAIGN_EMAIL: "Campaign Email",
    PF_UNKNOWN_EMAIL: "Unknown Email",
    PF_CAMPAIGN: "Campaign",
    PF_UTM: "UTM",
    PF_UNKNOWN_CAMPAIGN: "Unknown Campaign",
    PF_EVENTS: "Events",
    PF_SALESNASH: "SalesNash",
    PF_ORGANIC_SEARCH: "Organic Search",
    PF_DIRECT_TRAFFIC: "Direct Traffic",
    PF_REFERRALS: "Referrals",
    PF_RESELLERS: "Resellers",
    PF_DIRECT_EMAIL: "Direct Email",
    PF_OFFLINE_MIGRATION: "Offline / Migration",
    PF_NEEDS_REVIEW: "Missing / Unknown / Ambiguous",
    PF_UNSPECIFIED: "Unspecified",
}

# Attribution-quality buckets (how confident the platform detection is).
QUALITY_KNOWN = "known"
QUALITY_UNKNOWN = "unknown"
QUALITY_IMPORTED = "imported"
QUALITY_NEEDS_REVIEW = "needs_review"


def _social_platform(detail_n: str, *, paid: bool) -> str:
    """Resolve a social platform from the drill-down detail. Unknown stays unknown
    (contextual to paid vs organic) — never guessed."""
    if "linkedin" in detail_n:
        return PF_LINKEDIN
    if "facebook" in detail_n or "instagram" in detail_n or "meta" in detail_n:
        return PF_META
    return PF_UNKNOWN_PAID_SOCIAL if paid else PF_UNKNOWN_ORGANIC_SOCIAL


def _email_platform(detail_n: str) -> str:
    if "hubspot" in detail_n:
        return PF_HUBSPOT_EMAIL
    if detail_n:
        return PF_CAMPAIGN_EMAIL
    return PF_UNKNOWN_EMAIL


def _campaign_platform(detail_n: str) -> str:
    if "utm" in detail_n:
        return PF_UTM
    if detail_n:
        return PF_CAMPAIGN
    return PF_UNKNOWN_CAMPAIGN


def _channel_platform(group: str, primary_n: str, detail_n: str) -> tuple[str, str]:
    """Resolve (channel, platform) for a group + normalised primary/detail.

    Deterministic and exhaustive. Only ever returns known ids; unknown source
    detail resolves to an explicit ``unknown_*`` platform, never a guessed one.
    """
    if group == GROUP_GOOGLE_ADS:
        return CH_PAID_SEARCH, PF_GOOGLE_ADS

    if group == GROUP_OTHER_PAID:
        if primary_n == "paid social":
            return CH_PAID_SOCIAL, _social_platform(detail_n, paid=True)
        if primary_n == "email marketing":
            return CH_EMAIL_MARKETING, _email_platform(detail_n)
        if primary_n == "other campaigns":
            return CH_OTHER_CAMPAIGNS, _campaign_platform(detail_n)
        # Offline Sources routed to Other Paid by their detail keyword.
        if "salesnash" in detail_n:
            return CH_SALESNASH, PF_SALESNASH
        if "events" in detail_n:
            return CH_EVENTS, PF_EVENTS
        return CH_OTHER_CAMPAIGNS, PF_UNKNOWN_CAMPAIGN

    if group == GROUP_ORGANIC:
        if primary_n == "organic search":
            return CH_ORGANIC_SEARCH_DIRECT, PF_ORGANIC_SEARCH
        if primary_n == "direct traffic":
            return CH_ORGANIC_SEARCH_DIRECT, PF_DIRECT_TRAFFIC
        if primary_n == "organic social":
            return CH_ORGANIC_SOCIAL, _social_platform(detail_n, paid=False)
        if primary_n == "referrals":
            return CH_REFERRALS_PARTNERS, PF_REFERRALS
        if primary_n == "direct email":
            return CH_DIRECT_EMAIL, PF_DIRECT_EMAIL
        # Offline Sources routed to Organic by their detail keyword.
        if "reseller" in detail_n:
            return CH_REFERRALS_PARTNERS, PF_RESELLERS
        if "direct email" in detail_n:
            return CH_DIRECT_EMAIL, PF_DIRECT_EMAIL
        if "referral" in detail_n:
            return CH_REFERRALS_PARTNERS, PF_REFERRALS
        return CH_REFERRALS_PARTNERS, PF_REFERRALS

    if group == GROUP_OFFLINE:
        return CH_IMPORTED_CRM, PF_OFFLINE_MIGRATION

    return CH_NEEDS_REVIEW, PF_NEEDS_REVIEW


def _attribution_quality(group: str, platform: str) -> str:
    if group == GROUP_UNCLASSIFIED:
        return QUALITY_NEEDS_REVIEW
    if group == GROUP_OFFLINE:
        return QUALITY_IMPORTED
    if platform in (PF_UNKNOWN_PAID_SOCIAL, PF_UNKNOWN_ORGANIC_SOCIAL,
                    PF_UNKNOWN_EMAIL, PF_UNKNOWN_CAMPAIGN):
        return QUALITY_UNKNOWN
    return QUALITY_KNOWN


def classify_source_taxonomy(primary, detail_1=None, detail_2=None,
                             utm_source=None, utm_medium=None, utm_campaign=None) -> dict:
    """Classify a HubSpot source into a group → channel → platform taxonomy.

    Pure and deterministic. The GROUP is delegated to ``classify_source`` so this
    never diverges from the rest of the system. Routing uses HubSpot Original
    Source + Original Source Drill-Down (``hs_analytics_source`` +
    ``hs_analytics_source_data_1``), which are the only source fields persisted
    locally today.

    ``detail_2`` (``hs_analytics_source_data_2``) and utm_* are accepted for
    forward-compatibility and richer platform labelling but are NOT yet used for
    routing: ``hs_analytics_source_data_2`` is fetched by the HubSpot connector but
    is NOT persisted in the durable reporting tables, so it is unavailable at read
    time. Using it for platform detection is deferred to PR-134, after the durable
    field is inspected/persisted — doing so here would require a schema + writer +
    backfill change outside this PR's scope. Until then the drawer surfaces
    ``source_drilldown_2`` as "Unavailable" rather than fabricating a value.

    Only Google Ads / Paid Search returns spend_connected=True, roas_available=True.
    Every other source is revenue-only. Missing / unknown primary → Unclassified,
    never Organic.
    """
    primary_n = normalize_source(primary)
    detail_n = normalize_source(detail_1)

    group = classify_source(primary, detail_1)
    channel, platform = _channel_platform(group, primary_n, detail_n)
    spend_connected = group in GROUPS_WITH_SPEND
    if not primary_n:
        reason = "hubspot_original_source_missing"
    elif group == GROUP_UNCLASSIFIED:
        reason = "hubspot_original_source_unknown_primary"
    else:
        reason = f"hubspot_original_source_{channel}_{platform}"
    return {
        "source_group": group,
        "source_group_label": GROUP_LABELS[group],
        "source_channel": channel,
        "source_channel_label": CHANNEL_LABELS[channel],
        "source_platform": platform,
        "source_platform_label": PLATFORM_LABELS[platform],
        "spend_connected": spend_connected,
        "roas_available": spend_connected,
        "attribution_quality": _attribution_quality(group, platform),
        "classification_reason": reason,
    }


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
