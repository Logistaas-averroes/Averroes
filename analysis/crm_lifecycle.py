"""
analysis/crm_lifecycle.py

PR-ADS-153B — HubSpot Lifecycle Stage is the CANONICAL Averroes funnel spine.

Doctrine
--------
HubSpot owns CRM lifecycle truth. Averroes ingests, persists, deduplicates,
attributes, windows, reconciles and reports it. Averroes never invents a parallel
lifecycle and never derives a funnel stage from ``mql_status``.

The canonical funnel is:

    Lead → Marketing Qualified Lead → Sales Qualified Lead → Opportunity → Customer

A funnel EVENT is proven by the HubSpot stage-entry timestamp
(``hs_v2_date_entered_*``), not by the contact's *current* lifecycle stage. This
matters: a contact currently at ``customer`` still entered ``salesqualifiedlead``
on some date and must remain countable in that historical SQL cohort. Funnel
counts are therefore NOT mutually exclusive by current stage.

Additional live lifecycle values (subscriber, evangelist, other, the custom
"Discarded Contact" and "Reseller" stages) are preserved verbatim and reported
honestly — never silently folded into one of the five primary stages.

This module is pure: no I/O, no database, no HubSpot calls.
"""

from __future__ import annotations

# Rule version for the lifecycle interpretation. Bump when the mapping changes
# so persisted rows remain auditable against the rule that produced them.
LIFECYCLE_RULE_VERSION = "v1"

# ── Primary funnel stages (HubSpot internal values) ──────────────────────────
STAGE_LEAD = "lead"
STAGE_MQL = "marketingqualifiedlead"
STAGE_SQL = "salesqualifiedlead"
STAGE_OPPORTUNITY = "opportunity"
STAGE_CUSTOMER = "customer"

# ── Additional live lifecycle values (persisted, never remapped) ─────────────
STAGE_SUBSCRIBER = "subscriber"
STAGE_EVANGELIST = "evangelist"
STAGE_OTHER = "other"
# Custom portal stages, exposed by HubSpot as numeric internal values.
STAGE_DISCARDED_CONTACT = "370543605"
STAGE_RESELLER = "377714653"

PRIMARY_FUNNEL_STAGES = (
    STAGE_LEAD,
    STAGE_MQL,
    STAGE_SQL,
    STAGE_OPPORTUNITY,
    STAGE_CUSTOMER,
)

ADDITIONAL_STAGES = (
    STAGE_SUBSCRIBER,
    STAGE_EVANGELIST,
    STAGE_OTHER,
    STAGE_DISCARDED_CONTACT,
    STAGE_RESELLER,
)

KNOWN_STAGES = PRIMARY_FUNNEL_STAGES + ADDITIONAL_STAGES

STAGE_LABELS = {
    STAGE_LEAD: "Lead",
    STAGE_MQL: "Marketing Qualified Lead",
    STAGE_SQL: "Sales Qualified Lead",
    STAGE_OPPORTUNITY: "Opportunity",
    STAGE_CUSTOMER: "Customer",
    STAGE_SUBSCRIBER: "Subscriber",
    STAGE_EVANGELIST: "Evangelist",
    STAGE_OTHER: "Other",
    STAGE_DISCARDED_CONTACT: "Discarded Contact",
    STAGE_RESELLER: "Reseller",
}

# ── Canonical funnel EVENTS ──────────────────────────────────────────────────
# An event key is the short Averroes name; each maps to the HubSpot stage-entry
# property that is its ONLY canonical event date, and to the durable column that
# persists it.
EVENT_LEAD = "lead"
EVENT_MQL = "mql"
EVENT_SQL = "sql"
EVENT_OPPORTUNITY = "opportunity"
EVENT_CUSTOMER = "customer"

FUNNEL_EVENTS = (EVENT_LEAD, EVENT_MQL, EVENT_SQL, EVENT_OPPORTUNITY, EVENT_CUSTOMER)

# event key → HubSpot property name (the source of canonical event-date truth)
EVENT_HUBSPOT_PROPERTY = {
    EVENT_LEAD: "hs_v2_date_entered_lead",
    EVENT_MQL: "hs_v2_date_entered_marketingqualifiedlead",
    EVENT_SQL: "hs_v2_date_entered_salesqualifiedlead",
    EVENT_OPPORTUNITY: "hs_v2_date_entered_opportunity",
    EVENT_CUSTOMER: "hs_v2_date_entered_customer",
}

# event key → durable column on hubspot_contact_funnel
EVENT_DATE_COLUMN = {
    EVENT_LEAD: "date_entered_lead",
    EVENT_MQL: "date_entered_mql",
    EVENT_SQL: "date_entered_sql",
    EVENT_OPPORTUNITY: "date_entered_opportunity",
    EVENT_CUSTOMER: "date_entered_customer",
}

# event key → the lifecycle stage the event proves entry into
EVENT_STAGE = {
    EVENT_LEAD: STAGE_LEAD,
    EVENT_MQL: STAGE_MQL,
    EVENT_SQL: STAGE_SQL,
    EVENT_OPPORTUNITY: STAGE_OPPORTUNITY,
    EVENT_CUSTOMER: STAGE_CUSTOMER,
}

EVENT_LABELS = {
    EVENT_LEAD: "Leads",
    EVENT_MQL: "MQLs",
    EVENT_SQL: "SQLs",
    EVENT_OPPORTUNITY: "Opportunities",
    EVENT_CUSTOMER: "Lifecycle Customers",
}

# Ordered funnel progression used for cohort-safe conversion steps.
FUNNEL_PROGRESSION = (
    (EVENT_LEAD, EVENT_MQL),
    (EVENT_MQL, EVENT_SQL),
    (EVENT_SQL, EVENT_OPPORTUNITY),
    (EVENT_OPPORTUNITY, EVENT_CUSTOMER),
)

# ── Ordinal rank, used only for "has the contact reached at least X" reporting.
# It is NEVER used to infer a missing stage-entry date.
STAGE_RANK = {
    STAGE_SUBSCRIBER: 0,
    STAGE_LEAD: 1,
    STAGE_MQL: 2,
    STAGE_SQL: 3,
    STAGE_OPPORTUNITY: 4,
    STAGE_CUSTOMER: 5,
    STAGE_EVANGELIST: 6,
}


def normalize_lifecycle_stage(value) -> str | None:
    """Normalise a raw HubSpot ``lifecyclestage`` value.

    Lower-cases and trims. Unknown values are PRESERVED (returned normalised),
    never guessed into a known stage and never dropped — a new portal stage must
    surface as itself so ``is_known_stage`` can flag it for audit.

    Returns None for null/blank (absence of lifecycle evidence).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.lower()


def is_known_stage(stage) -> bool:
    """True when the normalised stage is one Averroes has explicitly documented."""
    return normalize_lifecycle_stage(stage) in KNOWN_STAGES


def stage_label(stage) -> str | None:
    """Human label for a lifecycle stage; unknown stages return their raw value."""
    normalised = normalize_lifecycle_stage(stage)
    if normalised is None:
        return None
    return STAGE_LABELS.get(normalised, str(stage))


def is_primary_funnel_stage(stage) -> bool:
    """True when the stage is one of the five canonical funnel stages."""
    return normalize_lifecycle_stage(stage) in PRIMARY_FUNNEL_STAGES


def stage_rank(stage) -> int | None:
    """Ordinal rank of a stage, or None when it has no funnel ordering.

    Never used to infer missing stage-entry evidence — reporting only.
    """
    return STAGE_RANK.get(normalize_lifecycle_stage(stage))


def event_date_column(event: str) -> str:
    """Durable column that stores the canonical event date for a funnel event."""
    try:
        return EVENT_DATE_COLUMN[event]
    except KeyError as exc:  # noqa: PERF203
        raise ValueError(f"Unknown funnel event '{event}'") from exc


def hubspot_property_for_event(event: str) -> str:
    """HubSpot stage-entry property that owns the event date for a funnel event."""
    try:
        return EVENT_HUBSPOT_PROPERTY[event]
    except KeyError as exc:  # noqa: PERF203
        raise ValueError(f"Unknown funnel event '{event}'") from exc


def is_valid_event(event) -> bool:
    return event in FUNNEL_EVENTS
