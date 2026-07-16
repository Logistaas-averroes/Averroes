"""
services/mailchimp_audit_service.py

Attribution Feasibility Audit (PR-ADS-151).

Answers ONE question before any Email Marketing dashboard is built: *can* Mailchimp
campaign recipients be safely reconciled with HubSpot contacts, and with what
coverage?

Privacy + governance:
  - Recipient email addresses are NEVER exposed, logged, or persisted. Matching
    uses Mailchimp's ``email_id`` — the MD5 of the lowercased email — as a durable,
    one-way join key against MD5(lower(HubSpot email)). Only aggregate counts leave
    this module.
  - HubSpot outcomes (SQL / customer / closed-won revenue) are attached ONLY when
    exactly one Mailchimp recipient maps to exactly one HubSpot contact. Ambiguous
    (one member → many contacts) and unmatched recipients contribute NO outcomes.
  - The audit reports coverage FIRST; it never presents outcomes as if attribution
    were complete.

The reconciliation math is a pure function (``build_attribution_audit``) so it is
fully testable with synthetic data — no live calls, no real emails.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# HubSpot lifecycle stages we treat as Sales-Qualified (or beyond).
_SQL_LIFECYCLE = {"salesqualifiedlead", "opportunity", "customer"}
_CUSTOMER_LIFECYCLE = {"customer"}


def md5_email(email: Optional[str]) -> Optional[str]:
    """MD5 of the lowercased, trimmed email — the same key Mailchimp uses for
    ``email_id``. Returns None for blank input. Never logs the address."""
    if not email:
        return None
    e = str(email).strip().lower()
    if not e:
        return None
    return hashlib.md5(e.encode("utf-8")).hexdigest()  # noqa: S324 — join key, not security


# ── Pure reconciliation ───────────────────────────────────────────────────────

def build_attribution_audit(
    recipients_by_campaign: dict[str, list],
    hubspot_index: dict[str, list],
    *,
    hubspot_available: bool,
) -> dict[str, Any]:
    """Compute attribution-feasibility coverage. Pure — no I/O.

    ``recipients_by_campaign``: {campaign_id: [{member_id, status}, ...]} where
        member_id is Mailchimp's MD5 email_id (durable member identity).
    ``hubspot_index``: {md5_email: [{contact_id, is_sql, is_customer,
        closed_won_amount}, ...]}. Multiple contacts under one key = ambiguous.
    ``hubspot_available``: when False, identity coverage is still reported but
        matched/ambiguous/unmatched and all outcomes are withheld (None).

    Never exposes emails; outcomes come only from safe 1:1 matches.
    """
    campaigns_inspected = 0
    recipients_total = 0
    recipients_with_identity = 0

    matchable = 0            # member identity maps to exactly one HubSpot contact
    ambiguous = 0            # member identity maps to >1 HubSpot contact
    unmatched = 0            # member identity present, no HubSpot contact

    safe_contact_ids: set = set()
    sql_contact_ids: set = set()
    customer_contact_ids: set = set()
    closed_won_contact_ids: set = set()

    per_campaign: list[dict[str, Any]] = []

    for campaign_id, members in (recipients_by_campaign or {}).items():
        campaigns_inspected += 1
        c_recipients = 0
        c_with_identity = 0
        c_matchable = 0
        c_ambiguous = 0
        c_unmatched = 0

        for m in members or []:
            c_recipients += 1
            recipients_total += 1
            member_id = (m.get("member_id") or "").strip() if isinstance(m, dict) else ""
            if not member_id:
                continue
            c_with_identity += 1
            recipients_with_identity += 1

            if not hubspot_available:
                continue

            contacts = hubspot_index.get(member_id) or []
            if len(contacts) == 0:
                c_unmatched += 1
                unmatched += 1
            elif len(contacts) == 1:
                c_matchable += 1
                matchable += 1
                # Safe 1:1 — outcomes may be attached.
                ct = contacts[0]
                cid = ct.get("contact_id")
                if cid is not None:
                    safe_contact_ids.add(cid)
                    if ct.get("is_sql"):
                        sql_contact_ids.add(cid)
                    if ct.get("is_customer"):
                        customer_contact_ids.add(cid)
                    if float(ct.get("closed_won_amount") or 0) > 0:
                        closed_won_contact_ids.add(cid)
            else:
                c_ambiguous += 1
                ambiguous += 1

        coverage = round(c_matchable / c_with_identity, 4) if c_with_identity else None
        per_campaign.append({
            "campaign_id": campaign_id,
            "recipients": c_recipients,
            "recipients_with_member_identity": c_with_identity,
            "safely_matchable": c_matchable if hubspot_available else None,
            "ambiguous": c_ambiguous if hubspot_available else None,
            "unmatched": c_unmatched if hubspot_available else None,
            "attribution_coverage": coverage if hubspot_available else None,
        })

    overall_coverage = (
        round(matchable / recipients_with_identity, 4)
        if hubspot_available and recipients_with_identity else None
    )

    return {
        "hubspot_available": hubspot_available,
        "mailchimp_campaigns_inspected": campaigns_inspected,
        "recipients_inspected": recipients_total,
        "recipients_with_member_identity": recipients_with_identity,
        "recipients_safely_matchable": matchable if hubspot_available else None,
        "ambiguous_matches": ambiguous if hubspot_available else None,
        "unmatched_recipients": unmatched if hubspot_available else None,
        "sql_contacts": len(sql_contact_ids) if hubspot_available else None,
        "customer_contacts": len(customer_contact_ids) if hubspot_available else None,
        "contacts_with_closed_won_revenue": len(closed_won_contact_ids) if hubspot_available else None,
        "safely_matched_contacts": len(safe_contact_ids) if hubspot_available else None,
        "overall_attribution_coverage": overall_coverage,
        "campaign_level_coverage": per_campaign,
        "governance_note": (
            "Recipient email addresses are never exposed. HubSpot outcomes are "
            "attached only where one Mailchimp recipient maps to exactly one HubSpot "
            "contact; ambiguous and unmatched recipients contribute no outcomes."
        ),
    }


def build_hubspot_index(contacts: list, closed_won_contact_ids: Optional[set] = None) -> dict:
    """Build the MD5-email → contacts index from HubSpot contact records. Pure.

    Each contact dict may carry ``email`` (or ``properties.email``),
    ``lifecyclestage``, ``mql_status``. Emails are hashed immediately and never
    retained. ``closed_won_contact_ids`` marks contacts with closed-won revenue.
    """
    from db.writers import _map_status_category  # noqa: PLC0415
    closed = closed_won_contact_ids or set()
    index: dict[str, list] = {}
    for c in contacts or []:
        props = c.get("properties") if isinstance(c.get("properties"), dict) else c
        email = (props or {}).get("email") or c.get("email")
        key = md5_email(email)
        if not key:
            continue
        contact_id = c.get("id") or (props or {}).get("contact_id") or c.get("contact_id")
        lifecycle = ((props or {}).get("lifecyclestage") or "").strip().lower()
        mql = (props or {}).get("mql_status")
        status_category = _map_status_category(mql)
        is_sql = lifecycle in _SQL_LIFECYCLE or status_category == "qualified"
        is_customer = lifecycle in _CUSTOMER_LIFECYCLE
        index.setdefault(key, []).append({
            "contact_id": str(contact_id) if contact_id is not None else None,
            "is_sql": bool(is_sql),
            "is_customer": bool(is_customer),
            "closed_won_amount": 1.0 if (contact_id is not None and str(contact_id) in closed) else 0.0,
        })
    return index


# ── Orchestration (best-effort live) ──────────────────────────────────────────

def run_audit(*, max_campaigns: int = 10, hubspot_window_days: int = 365) -> dict:
    """Run the feasibility audit end-to-end (read-only). Never raises.

    Bounds live work to the most-recent ``max_campaigns`` sent campaigns to avoid
    downloading unnecessary personal data. Builds the HubSpot index best-effort;
    if HubSpot is unavailable, identity coverage is still reported and outcomes are
    withheld with an explicit flag.
    """
    from connectors import mailchimp_pull as mc  # noqa: PLC0415

    config = mc.config_status()
    if not config["configured"]:
        return {"status": "not_configured",
                "detail": "Mailchimp is not configured (enabled + key + prefix required)",
                "connection": {"server_prefix": config["server_prefix"]}}

    # 1. Choose campaigns to inspect (durable first, then live fallback).
    campaign_ids = _select_audit_campaigns(max_campaigns)
    if not campaign_ids:
        return {"status": "no_campaigns",
                "detail": "No sent Mailchimp campaigns available to audit yet.",
                "audit": build_attribution_audit({}, {}, hubspot_available=False)}

    # 2. Pull sent-to member identities per campaign (MD5 identities only).
    recipients_by_campaign: dict[str, list] = {}
    member_pull_errors = 0
    error_detail = None
    for cid in campaign_ids:
        try:
            recipients_by_campaign[cid] = mc.get_campaign_sent_to(cid)
        except mc.MailchimpError as exc:
            member_pull_errors += 1
            error_detail = error_detail or str(exc)
            logger.warning("Mailchimp sent-to pull failed for %s: %s", cid, exc)
            recipients_by_campaign[cid] = []

    # 3. Build HubSpot index (best-effort, privacy-preserving).
    hubspot_index, hubspot_available, hubspot_note = _build_hubspot_index_live(hubspot_window_days)

    audit = build_attribution_audit(
        recipients_by_campaign, hubspot_index, hubspot_available=hubspot_available)

    return {
        "status": "ok",
        "campaigns_audited": len(campaign_ids),
        "member_pull_errors": member_pull_errors,
        "member_pull_error_detail": error_detail,
        "hubspot_note": hubspot_note,
        "audit": audit,
    }


def _select_audit_campaigns(max_campaigns: int) -> list:
    from db import mailchimp_repository as repo  # noqa: PLC0415
    ids = repo.recent_sent_campaign_ids(limit=max_campaigns)
    if ids:
        return ids
    # Durable table empty — fall back to a bounded live campaign list.
    try:
        from connectors import mailchimp_pull as mc  # noqa: PLC0415
        campaigns = mc.list_campaigns(page_size=max_campaigns)
        sent = [c["campaign_id"] for c in campaigns
                if c.get("status") == "sent" or c.get("send_time")]
        return sent[:max_campaigns]
    except Exception as exc:  # noqa: BLE001
        logger.warning("live campaign fallback for audit failed: %s", exc)
        return []


def _build_hubspot_index_live(window_days: int) -> tuple[dict, bool, Optional[str]]:
    """Assemble the MD5-email → contacts index from HubSpot (read-only).

    Returns (index, available, note). On any failure the audit still reports
    Mailchimp-side identity coverage with outcomes withheld.
    """
    import os  # noqa: PLC0415
    if not (os.getenv("HUBSPOT_API_KEY") or "").strip():
        return {}, False, "HUBSPOT_API_KEY not set — outcomes withheld; identity coverage only."
    try:
        from datetime import date, timedelta  # noqa: PLC0415
        from connectors import hubspot_pull as hs  # noqa: PLC0415
        date_to = date.today()
        date_from = date_to - timedelta(days=max(1, int(window_days)))
        contacts = hs.pull_all_contacts_in_range(date_from.isoformat(), date_to.isoformat())
        # Closed-won contact ids within the window (for revenue-linked contacts).
        closed_won_ids: set = set()
        try:
            deals = hs.pull_closed_won_deals_in_range(date_from.isoformat(), date_to.isoformat())
            for d in deals:
                cid = d.get("contact_id")
                if cid is not None:
                    closed_won_ids.add(str(cid))
        except Exception as exc:  # noqa: BLE001
            logger.warning("closed-won pull for audit failed: %s", exc)
        index = build_hubspot_index(contacts, closed_won_contact_ids=closed_won_ids)
        note = (f"HubSpot index built from {len(contacts)} contacts over last "
                f"{window_days} days.")
        return index, True, note
    except Exception as exc:  # noqa: BLE001
        logger.warning("HubSpot index build failed: %s", exc)
        return {}, False, f"HubSpot lookup failed ({exc}); outcomes withheld."
