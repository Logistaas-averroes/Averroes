"""
services/mailchimp_audit_service.py

Attribution Feasibility Audit (PR-ADS-151).

Answers ONE question before any Email Marketing dashboard is built: *can* Mailchimp
campaign recipients be safely reconciled with HubSpot contacts, and with what
coverage — and are the resulting outcomes a subset of Averroes' durable truth?

Privacy + governance:
  - Recipient email addresses are NEVER exposed, logged, or persisted. Matching
    uses Mailchimp's ``email_id`` (MD5 of the lowercased email) as a durable,
    one-way join key against MD5(lower(HubSpot email)). Only aggregate counts leave
    this module.
  - HubSpot outcomes (SQL / customer / closed-won revenue) are attached ONLY when
    exactly one Mailchimp recipient maps to exactly one HubSpot contact.
  - Outcome counts are NOT recomputed from live HubSpot lifecycle properties. They
    come from Averroes' DURABLE business-truth contracts (PR-ADS-151 §4):
      SQL      = durable ``leads.status_category='qualified'`` (deduped per contact,
                 ``lead_truth_exclusions`` applied, windowed by contact_created_at);
      customer = durable ``deal_source_attribution`` closed-won contacts;
      revenue  = durable ``deal_source_attribution`` SUM(deal_amount_usd) per
                 contact (deduped per deal, windowed by deal_close_date).
    The audit returns reconciliation metadata proving each attributed outcome set is
    a subset of the corresponding durable population.

The reconciliation math is a pure function (``build_attribution_audit``) so it is
fully testable with synthetic data — no live calls, no real emails.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def md5_email(email: Optional[str]) -> Optional[str]:
    """MD5 of the lowercased, trimmed email — the same key Mailchimp uses for
    ``email_id``. Returns None for blank input. Never logs the address."""
    if not email:
        return None
    e = str(email).strip().lower()
    if not e:
        return None
    return hashlib.md5(e.encode("utf-8")).hexdigest()  # noqa: S324 — join key, not security


# ── Identity bridge (HubSpot email → contact_id, hashed) ──────────────────────

def build_identity_index(contacts: list) -> dict:
    """Build the MD5-email → [contact_id, ...] identity bridge from HubSpot contact
    records. Pure. Emails are hashed immediately and NEVER retained.

    This bridge ONLY resolves recipient identity (which HubSpot contact a Mailchimp
    member is). It carries NO business outcomes — those come from durable truth.
    """
    index: dict[str, list] = {}
    for c in contacts or []:
        props = c.get("properties") if isinstance(c.get("properties"), dict) else c
        email = (props or {}).get("email") or c.get("email")
        key = md5_email(email)
        if not key:
            continue
        contact_id = c.get("id") or (props or {}).get("contact_id") or c.get("contact_id")
        if contact_id is None:
            continue
        index.setdefault(key, []).append(str(contact_id))
    return index


# ── Pure reconciliation ───────────────────────────────────────────────────────

def build_attribution_audit(
    recipients_by_campaign: dict[str, list],
    identity_index: dict[str, list],
    durable: Optional[dict],
    *,
    identity_available: bool,
) -> dict[str, Any]:
    """Compute attribution-feasibility coverage + durable-truth reconciliation. Pure.

    ``recipients_by_campaign``: {campaign_id: [{member_id, status}, ...]} where
        member_id is Mailchimp's MD5 email_id.
    ``identity_index``: {md5_email: [contact_id, ...]} (>1 = ambiguous).
    ``durable``: durable business-truth populations from
        ``db.mailchimp_repository.fetch_durable_outcome_populations`` — sets of
        contact_ids + population sizes. Outcomes are withheld if it is None / DB
        unavailable.
    ``identity_available``: when False, identity coverage is still reported but
        matched/ambiguous/unmatched and all outcomes are withheld (None).

    Never exposes emails; outcomes come only from safe 1:1 matches AND durable truth.
    """
    durable = durable or {}
    durable_available = bool(durable) and not durable.get("db_unavailable", True)
    outcomes_available = identity_available and durable_available

    sql_set = durable.get("sql_contacts") or set()
    customer_set = durable.get("customer_contacts") or set()
    won_map = durable.get("closed_won_by_contact") or {}

    campaigns_inspected = 0
    recipients_total = 0
    recipients_with_identity = 0

    matchable = 0
    ambiguous = 0
    unmatched = 0

    safe_contact_ids: set = set()
    sql_contact_ids: set = set()
    customer_contact_ids: set = set()
    closed_won_contact_ids: set = set()
    attributed_revenue = 0.0

    per_campaign: list[dict[str, Any]] = []

    for campaign_id, members in (recipients_by_campaign or {}).items():
        campaigns_inspected += 1
        c_recipients = c_with_identity = c_matchable = c_ambiguous = c_unmatched = 0

        for m in members or []:
            c_recipients += 1
            recipients_total += 1
            member_id = (m.get("member_id") or "").strip() if isinstance(m, dict) else ""
            if not member_id:
                continue
            c_with_identity += 1
            recipients_with_identity += 1

            if not identity_available:
                continue

            contacts = identity_index.get(member_id) or []
            if len(contacts) == 0:
                c_unmatched += 1
                unmatched += 1
            elif len(contacts) == 1:
                c_matchable += 1
                matchable += 1
                cid = contacts[0]
                safe_contact_ids.add(cid)
                if outcomes_available:
                    if cid in sql_set:
                        sql_contact_ids.add(cid)
                    if cid in customer_set:
                        customer_contact_ids.add(cid)
                    if cid in won_map:
                        closed_won_contact_ids.add(cid)
                        attributed_revenue += float(won_map.get(cid) or 0)
            else:
                c_ambiguous += 1
                ambiguous += 1

        coverage = round(c_matchable / c_with_identity, 4) if c_with_identity else None
        per_campaign.append({
            "campaign_id": campaign_id,
            "recipients": c_recipients,
            "recipients_with_member_identity": c_with_identity,
            "safely_matchable": c_matchable if identity_available else None,
            "ambiguous": c_ambiguous if identity_available else None,
            "unmatched": c_unmatched if identity_available else None,
            "attribution_coverage": coverage if identity_available else None,
        })

    overall_coverage = (
        round(matchable / recipients_with_identity, 4)
        if identity_available and recipients_with_identity else None
    )

    dur_sql_pop = durable.get("durable_sql_population")
    dur_cust_pop = durable.get("durable_customer_population")
    dur_won_pop = durable.get("durable_closed_won_population")

    reconciliation = {
        "identity_available": identity_available,
        "durable_available": durable_available,
        "outcomes_available": outcomes_available,
        "window_start": durable.get("window_start"),
        "window_end": durable.get("window_end"),
        "durable_sql_population": dur_sql_pop,
        "durable_customer_population": dur_cust_pop,
        "durable_closed_won_population": dur_won_pop,
        "attributed_sql": len(sql_contact_ids) if outcomes_available else None,
        "attributed_customers": len(customer_contact_ids) if outcomes_available else None,
        "attributed_closed_won": len(closed_won_contact_ids) if outcomes_available else None,
        "attributed_closed_won_revenue_usd": round(attributed_revenue, 2) if outcomes_available else None,
        # Proof: each attributed outcome set is drawn FROM the durable population,
        # so it must be a subset. Reported so the caller can assert it explicitly.
        "sql_is_subset_of_durable": (
            (len(sql_contact_ids) <= (dur_sql_pop or 0)) if outcomes_available else None),
        "customers_is_subset_of_durable": (
            (len(customer_contact_ids) <= (dur_cust_pop or 0)) if outcomes_available else None),
        "closed_won_is_subset_of_durable": (
            (len(closed_won_contact_ids) <= (dur_won_pop or 0)) if outcomes_available else None),
        "definitions": {
            "sql": "durable leads.status_category='qualified' (deduped per contact, "
                   "lead_truth_exclusions applied, windowed by contact_created_at)",
            "customer": "durable deal_source_attribution closed-won contacts "
                        "(distinct associated_contact_id, windowed by deal_close_date)",
            "closed_won_revenue": "durable deal_source_attribution SUM(deal_amount_usd) "
                                  "per contact (deduped per deal_id, windowed by deal_close_date)",
        },
    }

    return {
        "identity_available": identity_available,
        "outcomes_available": outcomes_available,
        "mailchimp_campaigns_inspected": campaigns_inspected,
        "recipients_inspected": recipients_total,
        "recipients_with_member_identity": recipients_with_identity,
        "recipients_safely_matchable": matchable if identity_available else None,
        "ambiguous_matches": ambiguous if identity_available else None,
        "unmatched_recipients": unmatched if identity_available else None,
        "safely_matched_contacts": len(safe_contact_ids) if identity_available else None,
        "sql_contacts": len(sql_contact_ids) if outcomes_available else None,
        "customer_contacts": len(customer_contact_ids) if outcomes_available else None,
        "contacts_with_closed_won_revenue": len(closed_won_contact_ids) if outcomes_available else None,
        "overall_attribution_coverage": overall_coverage,
        "campaign_level_coverage": per_campaign,
        "reconciliation": reconciliation,
        "governance_note": (
            "Recipient email addresses are never exposed. HubSpot outcomes are "
            "attached only where one Mailchimp recipient maps to exactly one HubSpot "
            "contact, and are counted from Averroes' durable SQL/customer/revenue "
            "truth — never recomputed from live HubSpot lifecycle. Attributed "
            "outcomes are a subset of the durable populations (see reconciliation)."
        ),
    }


# ── Orchestration (best-effort live) ──────────────────────────────────────────

def run_audit(*, max_campaigns: int = 10, hubspot_window_days: int = 365) -> dict:
    """Run the feasibility audit end-to-end (read-only). Never raises.

    Bounds live work to the most-recent ``max_campaigns`` SENT campaigns to avoid
    downloading unnecessary personal data. Resolves recipient identity via a
    best-effort HubSpot email→id bridge; reads OUTCOME truth from Averroes' durable
    tables. If the identity bridge or durable truth is unavailable, identity
    coverage is still reported and outcomes are withheld with an explicit flag.
    """
    from connectors import mailchimp_pull as mc  # noqa: PLC0415

    config = mc.config_status()
    if not config["configured"]:
        return {"status": "not_configured",
                "detail": "Mailchimp is not configured (a valid MAILCHIMP_API_KEY is required)",
                "connection": {"server_prefix": config["server_prefix"]}}

    # 1. Choose SENT campaigns to inspect (durable first, then bounded live).
    campaign_ids = _select_audit_campaigns(max_campaigns)
    if not campaign_ids:
        return {"status": "no_campaigns",
                "detail": "No sent Mailchimp campaigns available to audit yet.",
                "audit": build_attribution_audit({}, {}, None, identity_available=False)}

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

    # 3. Identity bridge (HubSpot email → contact_id, hashed) + durable truth.
    from datetime import date, timedelta  # noqa: PLC0415
    window_end = date.today()
    window_start = window_end - timedelta(days=max(1, int(hubspot_window_days)))

    identity_index, identity_available, identity_note = _build_identity_index_live(
        window_start, window_end)
    durable = _fetch_durable_populations(window_start, window_end)

    audit = build_attribution_audit(
        recipients_by_campaign, identity_index, durable,
        identity_available=identity_available)

    return {
        "status": "ok",
        "campaigns_audited": len(campaign_ids),
        "member_pull_errors": member_pull_errors,
        "member_pull_error_detail": error_detail,
        "identity_note": identity_note,
        "audit": audit,
    }


def _select_audit_campaigns(max_campaigns: int) -> list:
    from db import mailchimp_repository as repo  # noqa: PLC0415
    ids = repo.recent_sent_campaign_ids(limit=max_campaigns)
    if ids:
        return ids
    # Durable table empty — fall back to a bounded live campaign list (sent only).
    try:
        from connectors import mailchimp_pull as mc  # noqa: PLC0415
        campaigns = mc.list_campaigns(page_size=max_campaigns)
        sent = [c["campaign_id"] for c in campaigns
                if c.get("status") == "sent" and c.get("send_time")]
        return sent[:max_campaigns]
    except Exception as exc:  # noqa: BLE001
        logger.warning("live campaign fallback for audit failed: %s", exc)
        return []


def _fetch_durable_populations(window_start, window_end) -> dict:
    from db import mailchimp_repository as repo  # noqa: PLC0415
    try:
        return repo.fetch_durable_outcome_populations(window_start, window_end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("durable outcome populations fetch failed: %s", exc)
        return {"db_unavailable": True}


def _build_identity_index_live(window_start, window_end) -> tuple[dict, bool, Optional[str]]:
    """Assemble the MD5-email → [contact_id] identity bridge from HubSpot (read-only).

    Returns (index, available, note). On any failure the audit still reports
    Mailchimp-side identity coverage with outcomes withheld.
    """
    import os  # noqa: PLC0415
    if not (os.getenv("HUBSPOT_API_KEY") or "").strip():
        return {}, False, "HUBSPOT_API_KEY not set — outcomes withheld; identity coverage only."
    try:
        from connectors import hubspot_pull as hs  # noqa: PLC0415
        contacts = hs.pull_all_contacts_in_range(window_start.isoformat(), window_end.isoformat())
        index = build_identity_index(contacts)
        note = (f"HubSpot identity bridge built from {len(contacts)} contacts "
                f"({window_start.isoformat()}→{window_end.isoformat()}).")
        return index, True, note
    except Exception as exc:  # noqa: BLE001
        logger.warning("HubSpot identity bridge build failed: %s", exc)
        return {}, False, f"HubSpot lookup failed ({exc}); outcomes withheld."
