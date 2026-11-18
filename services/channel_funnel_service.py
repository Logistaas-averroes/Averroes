"""PR-ADS-162A — the channel funnel: one acquisition cohort, every channel.

Answers one question for every marketing channel at once: *of the contacts
acquired in this period, how many have reached each funnel stage so far?*

* **Cohort.** Contacts whose HubSpot ``created_at`` falls in the window —
  the same membership rule, window days and freshness verdict as Campaign
  Evidence's acquisition cohort (``marketing_outcome_cohort_service``), over
  ALL sources instead of Google Ads only.
* **Channel.** HubSpot's own *original source* (``hs_analytics_source``),
  unmodified — Paid Search, Paid Social, Organic Search, Email Marketing, AI
  Referrals, Offline … — with the original-source drill-down
  (``hs_analytics_source_data_1``: linkedin / facebook / chatgpt.com / a
  campaign label) underneath. Nothing is re-interpreted, so a source HubSpot
  adds tomorrow appears here under its own name.
* **Stages.** MQL, SQL, Opportunity, Customer stage. A contact counts as
  having reached a stage on the strongest canonical evidence: the direct
  stage-entry timestamp, the recovered one, or a current lifecycle stage that
  ranks at or above it. The SQL rule is the cohort service's own
  ``sql_proof``. Evidence says a stage WAS reached; it never supplies a date.

This module publishes NO deals, revenue, customers (companies) or spend: those
have their own owners and gates. "Customer stage" is a HubSpot contact
lifecycle stage, counted in contacts.

One publication verdict (``sql_publication``) covers the whole table. When it
is not ``published`` the payload carries no counts at all — withheld means
absent.

Read-only. No HubSpot or Google Ads call.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from analysis.crm_lifecycle import (
    EVENT_CUSTOMER, EVENT_MQL, EVENT_OPPORTUNITY, EVENT_SQL,
    normalize_lifecycle_stage, stages_implying_event,
)
from services import marketing_outcome_cohort_service as cohort_service

log = logging.getLogger(__name__)

SOURCE = "canonical_contact_funnel_all_sources"
RULE_VERSION = "channel_funnel_v1"
DEFAULT_WINDOW = "180d"

#: The stages reported, in funnel order. ``key`` is the API field.
STAGES = (
    {"event": EVENT_MQL, "key": "reached_mql", "label": "MQL"},
    {"event": EVENT_SQL, "key": "reached_sql", "label": "SQL"},
    {"event": EVENT_OPPORTUNITY, "key": "reached_opportunity",
     "label": "Opportunity"},
    {"event": EVENT_CUSTOMER, "key": "reached_customer_stage",
     "label": "Customer stage"},
)
STAGE_KEYS = tuple(s["key"] for s in STAGES)

_IMPLYING = {s["event"]: frozenset(stages_implying_event(s["event"]))
             for s in STAGES}

NO_SOURCE = "(no source recorded)"
NO_DRILLDOWN = "(none)"
MAX_DRILLDOWNS = 8

#: Display names for HubSpot's original-source values. A value not listed is
#: shown exactly as HubSpot sent it.
SOURCE_LABELS = {
    "PAID_SEARCH": "Paid Search",
    "PAID_SOCIAL": "Paid Social",
    "ORGANIC_SEARCH": "Organic Search (SEO)",
    "DIRECT_TRAFFIC": "Direct Traffic",
    "EMAIL_MARKETING": "Email Marketing",
    "SOCIAL_MEDIA": "Organic Social",
    "REFERRALS": "Referrals",
    "OTHER_CAMPAIGNS": "Other Campaigns",
    "AI_REFERRALS": "AI Referrals",
    "OFFLINE": "Offline Sources",
    NO_SOURCE: "No source recorded",
}

STAGE_NOTE = (
    "Each stage counts contacts with canonical evidence of reaching it: a "
    "stage-entry timestamp, or a current lifecycle stage at or beyond it. "
    "'Customer stage' is the HubSpot contact lifecycle stage, counted in "
    "contacts; it is not the company-level customer count on revenue pages.")
CHANNEL_NOTE = (
    "Channel is HubSpot's original source for the contact, unmodified. The "
    "drill-down is HubSpot's original-source detail (for example linkedin, "
    "facebook, chatgpt.com or a campaign name).")


def source_key(row: dict) -> str:
    value = str(row.get("hs_analytics_source") or "").strip().upper()
    return value or NO_SOURCE


def drilldown_key(row: dict) -> str:
    value = str(row.get("hs_analytics_source_data_1") or "").strip().lower()
    return value or NO_DRILLDOWN


def reached(row: dict, event: str) -> bool:
    """Has this contact canonical evidence of reaching ``event``?"""
    if event == EVENT_SQL:
        # One SQL rule in the product: the acquisition cohort's own.
        return cohort_service.sql_proof(row) is not None
    if row.get(f"{event}_entered_direct") is not None:
        return True
    if row.get(f"{event}_entered_recovered") is not None:
        return True
    return normalize_lifecycle_stage(row.get("lifecycle_stage")) in _IMPLYING[event]


def _slot() -> dict:
    return {"contacts": 0, **{k: 0 for k in STAGE_KEYS}}


def _bump(slot: dict, flags: dict) -> None:
    slot["contacts"] += 1
    for key, hit in flags.items():
        if hit:
            slot[key] += 1


def build_funnel(rows: list) -> dict:
    """Pure: classify one cohort's contacts into channels and stages."""
    totals = _slot()
    channels: dict = {}
    seen: set = set()
    duplicates = 0
    for row in rows or []:
        cid, _fallback = cohort_service.contact_identity(row)
        if cid in seen:
            duplicates += 1
            continue
        seen.add(cid)
        flags = {s["key"]: reached(row, s["event"]) for s in STAGES}
        _bump(totals, flags)
        ch = channels.setdefault(source_key(row), {**_slot(), "drill": {}})
        _bump(ch, flags)
        _bump(ch["drill"].setdefault(drilldown_key(row), _slot()), flags)

    out = []
    for key, ch in channels.items():
        drill = sorted(ch.pop("drill").items(),
                       key=lambda kv: (-kv[1]["contacts"], kv[0]))
        top = [{"value": v, **slot} for v, slot in drill[:MAX_DRILLDOWNS]]
        rest = drill[MAX_DRILLDOWNS:]
        if rest:
            other = _slot()
            for _v, slot in rest:
                for k in other:
                    other[k] += slot[k]
            top.append({"value": f"(other: {len(rest)} values)", **other})
        out.append({"source": key, "label": SOURCE_LABELS.get(key, key),
                    **ch, "drilldowns": top})
    out.sort(key=lambda c: (-c["contacts"], c["source"]))
    return {"totals": totals, "channels": out, "duplicate_rows": duplicates}


def reconcile(funnel: dict) -> list:
    """Problems that make the table untrustworthy. Empty when it adds up."""
    problems = []
    if funnel.get("duplicate_rows"):
        problems.append(f"{funnel['duplicate_rows']} duplicate contact rows")
    totals = funnel["totals"]
    for key in ("contacts",) + STAGE_KEYS:
        if sum(c[key] for c in funnel["channels"]) != totals[key]:
            problems.append(f"channels do not sum to the total for {key}")
    for c in funnel["channels"]:
        for key in ("contacts",) + STAGE_KEYS:
            if sum(d[key] for d in c["drilldowns"]) != c[key]:
                problems.append(
                    f"drill-downs do not sum to {c['source']} for {key}")
        for key in STAGE_KEYS:
            if c[key] > c["contacts"]:
                problems.append(f"{c['source']}: {key} exceeds contacts")
    return problems


def _window(window_key: str, now: datetime | None) -> dict:
    from analysis.account_time import account_today  # noqa: PLC0415
    from analysis.evidence_windows import resolve_evidence_window  # noqa: PLC0415

    resolved = resolve_evidence_window(window_key)
    today = account_today(now)
    days = resolved["days"]
    start_date = None if days is None else today - timedelta(days=days - 1)
    start_at, end_before = cohort_service.window_instants(start_date, today)
    return {"key": resolved["key"], "is_all_time": days is None,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": today.isoformat(),
            "start_at": start_at, "end_before": end_before}


def build_channel_funnel(window: str = DEFAULT_WINDOW, *,
                         now: datetime | None = None) -> dict:
    """The ``/api/channel-funnel`` payload. Raises ``EvidenceWindowError`` for
    an unknown window; never raises for unavailable data."""
    from db import crm_funnel_repository as funnel_repo  # noqa: PLC0415

    now = now or datetime.now(tz=timezone.utc)
    w = _window(window, now)
    freshness = cohort_service.read_freshness(now)
    contacts = funnel_repo.fetch_channel_funnel_contacts(
        w["start_at"], w["end_before"])

    funnel, problems = None, []
    if contacts.get("available"):
        funnel = build_funnel(contacts.get("rows") or [])
        problems = reconcile(funnel)
    status, reason = cohort_service.sql_publication(
        cohort_available=funnel is not None,
        reconciliation_problems=problems, freshness=freshness)
    published = status == cohort_service.STATUS_PUBLISHED

    return {
        "source": SOURCE,
        "rule_version": RULE_VERSION,
        "generated_at": now.isoformat(),
        "metric_family": cohort_service.METRIC_FAMILY_COHORT,
        "window_basis": cohort_service.WINDOW_BASIS_COHORT,
        "window": {"key": w["key"], "start_date": w["start_date"],
                   "end_date": w["end_date"], "is_all_time": w["is_all_time"]},
        "publication": {"status": status, "reason": reason},
        "freshness": {
            "fresh": freshness.get("fresh"),
            "reason": freshness.get("reason"),
            "as_of": freshness.get("last_successful_incremental_at"),
            "age_hours": freshness.get("age_hours"),
        },
        "stages": [{"key": s["key"], "label": s["label"]} for s in STAGES],
        # Withheld means absent: no count of any kind unless published.
        "totals": funnel["totals"] if published else None,
        "channels": funnel["channels"] if published else None,
        "contacts_without_created_at": (
            contacts.get("missing_created_at") if published else None),
        "reconciliation_problems": problems,
        "notes": {"stages": STAGE_NOTE, "channels": CHANNEL_NOTE,
                  "maturity": cohort_service.COHORT_MATURITY_NOTE},
        "hubspot_calls_performed": False,
        "external_writes_performed": False,
    }
