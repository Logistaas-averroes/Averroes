"""
services/sql_truth_audit_service.py

PR-ADS-152 §1 — admin-only, read-only contact-level SQL-truth audit powering
``GET /api/audit/sql-truth``.

It proves, contact by contact, why the Dashboard / Revenue Decision Mart, Revenue
by Source and Keyword Evidence pages report DIFFERENT SQL counts for the same
account — the production "7 vs 1" — and confirms each difference is explained by
scope, attribution coverage, exclusions, or window dates, never by a fabricated
zero or a silent mismatch.

Everything is derived from the ONE canonical contact-outcome population
(services/canonical_contact_outcome_service) so the audit and the pages can never
disagree. The Dashboard / Source scopes are resolved on the business window; the
Keyword scope on the evidence window; the contact-set DIFFERENCES are computed on
a single shared date range (the business window) so they isolate SCOPE from WINDOW
— the window-only differences are proven separately (§5).

Privacy: no email addresses are ever returned. Contact id and company are the only
per-contact identifiers, for the authenticated admin drawer only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from services import canonical_contact_outcome_service as canon

logger = logging.getLogger(__name__)

_DEFAULT_BUSINESS_WINDOW = "current_quarter"
_DEFAULT_EVIDENCE_WINDOW = "30d"


# ── Pure audit assembly ──────────────────────────────────────────────────────
def build_audit(inputs: dict, business_win: dict, evidence_win: dict,
                *, keyword_attributable_keys: set | None = None,
                campaign_resolver=None,
                now: datetime | None = None) -> dict:
    """Assemble the full SQL-truth audit from raw canonical inputs. Pure.

    ``inputs`` is the repository payload ({available, lead_rows, exclusions,
    classification}). ``business_win`` / ``evidence_win`` are window blocks from
    ``canon.resolve_window_contract``. ``keyword_attributable_keys`` (optional) is
    the durable-key set of uniquely-keyword-attributed SQLs from the real keyword
    attribution path; when absent the keyword scope falls back to
    campaign-attributable and the audit says so.
    """
    now = now or datetime.now(timezone.utc)
    available = inputs.get("available", False)
    rows = inputs.get("lead_rows") or []
    excl = inputs.get("exclusions") or set()
    cls = inputs.get("classification") or []

    pops_b = canon.build_populations(rows, excl, cls, business_win["start"],
                                     business_win["end"], campaign_resolver=campaign_resolver)
    pops_e = canon.build_populations(rows, excl, cls, evidence_win["start"],
                                     evidence_win["end"], campaign_resolver=campaign_resolver)

    contract = {
        "contract_id": "logistaas",
        "dashboard": _dashboard_section(pops_b, business_win),
        "revenue_by_source": _source_section(pops_b, business_win),
        "keyword_evidence": _keyword_section(
            pops_e, evidence_win, keyword_attributable_keys),
        "differences": _differences(pops_b, keyword_attributable_keys, business_win),
        "reconciliation": {
            "dashboard": canon.reconciliation_metadata(
                pops_b, canon.SCOPE_CAMPAIGN_ATTRIBUTABLE, available=available),
            "revenue_by_source": canon.reconciliation_metadata(
                pops_b, canon.SCOPE_GOOGLE_ADS_SOURCE, available=available),
            "keyword_evidence": canon.reconciliation_metadata(
                pops_e, canon.SCOPE_KEYWORD_ATTRIBUTABLE, available=available,
                keyword_attributable=_kw_count(pops_e, keyword_attributable_keys)),
        },
    }

    return {
        "available": available,
        "business_window": _public_window(business_win),
        "evidence_window": _public_window(evidence_win),
        "contracts": [contract],
        "window_reconciliation": _window_reconciliation(pops_b, pops_e, business_win, evidence_win),
        "governance": {
            "read_only": True,
            "google_ads_writes": False,
            "hubspot_writes": False,
            "offline_conversion_uploads": False,
            "source_data_deletion": False,
            "emails_exposed": False,
        },
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _public_window(win: dict) -> dict:
    return {k: v for k, v in win.items() if k not in ("start", "end")}


def _kw_count(pops: dict, keyword_keys: set | None) -> int | None:
    if keyword_keys is None:
        return None
    camp = canon.campaign_attributable_sqls(pops["contacts"])
    return len(camp & keyword_keys)


def _dashboard_section(pops: dict, win: dict) -> dict:
    """Dashboard / Revenue Decision Mart contract (campaign-attributable scope)."""
    contacts = pops["contacts"]
    counts = pops["counts"]
    total_leads = sum(1 for c in contacts)
    not_google_ads = (counts["total_all_source_sqls"] - counts["google_ads_source_sqls"])
    pseudo_email = sum(
        1 for c in contacts
        if c["is_google_ads_source_sql"] and not c["is_campaign_attributable_sql"]
        and canon.campaign_disqualifier(c["campaign_name"]) in (
            canon.REASON_PSEUDO_CAMPAIGN, canon.REASON_EMAIL_CAMPAIGN)
    )
    return {
        "resolved_start_date": win["start_date"],
        "resolved_end_date": win["end_date"],
        "total_lead_contacts": total_leads,
        "total_qualified_contacts": counts["total_all_source_sqls"],
        "underlying_table": "leads",
        "date_field": canon.SQL_DATE_FIELD,
        "contact_key_definition": canon.SQL_DEDUP_KEY,
        "exclusion_count": len(pops["excluded"]),
        "pseudo_or_email_campaign_exclusion_count": pseudo_email,
        "not_google_ads_exclusion_count": not_google_ads,
        "campaign_attributable_sqls": counts["campaign_attributable_sqls"],
        "google_ads_source_sqls": counts["google_ads_source_sqls"],
    }


def _source_section(pops: dict, win: dict) -> dict:
    """Revenue by Source contract (google-ads-source scope)."""
    contacts = pops["contacts"]
    counts = pops["counts"]
    classified = sum(1 for c in contacts if c["classification_state"] != "missing")
    ga_classified = sum(
        1 for c in contacts
        if c["acquisition_group"] == canon.GROUP_GOOGLE_ADS
        and c["classification_state"] != "missing")
    other_qualified = counts["total_all_source_sqls"] - counts["google_ads_source_sqls"]
    return {
        "total_classified_contacts": classified,
        "google_ads_classified_contacts": ga_classified,
        "google_ads_qualified_contacts": counts["google_ads_source_sqls"],
        "other_source_qualified_contacts": other_qualified,
        "classification_freshness": {
            "stale_classification_contacts": counts["stale_classification_contacts"],
            "missing_classification_contacts": counts["missing_classification_contacts"],
        },
        "underlying_table": "contact_source_classification",
        "canonical_derived_from": "leads",
        "date_field": canon.SQL_DATE_FIELD,
        "window_start": win["start_date"],
        "window_end": win["end_date"],
    }


def _keyword_section(pops: dict, win: dict, keyword_keys: set | None) -> dict:
    """Keyword Evidence contract (keyword-attributable scope)."""
    contacts = pops["contacts"]
    counts = pops["counts"]
    camp = canon.campaign_attributable_sqls(contacts)
    ga = canon.google_ads_source_sqls(contacts)
    campaign_unmatched = len(ga - camp)
    missing_keyword = sum(
        1 for c in contacts
        if c["is_campaign_attributable_sql"] and not (c.get("keyword") or "").strip())
    if keyword_keys is None:
        uniquely = None
        ambiguous = None
    else:
        uniquely = len(camp & keyword_keys)
        ambiguous = len(camp) - uniquely - (missing_keyword)
        if ambiguous < 0:
            ambiguous = None
    return {
        "total_sql_contacts": counts["campaign_attributable_sqls"],
        "google_ads_source_sqls": counts["google_ads_source_sqls"],
        "campaign_attributable_sqls": counts["campaign_attributable_sqls"],
        "uniquely_keyword_attributed": uniquely,
        "ambiguous_or_unattributed": ambiguous,
        "campaign_unmatched": campaign_unmatched,
        "missing_keyword": missing_keyword,
        "window_start": win["start_date"],
        "window_end": win["end_date"],
    }


# ── Contact-set differences (durable keys, admin-safe reasons) ───────────────
def _differences(pops: dict, keyword_keys: set | None, win: dict) -> dict:
    """Set differences between the three page populations on ONE shared date range,
    so they isolate scope/attribution/exclusion from window dates."""
    contacts = pops["contacts"]
    by_key = {c["contact_key"]: c for c in contacts}

    dashboard = canon.campaign_attributable_sqls(contacts)
    source = canon.google_ads_source_sqls(contacts)
    if keyword_keys is None:
        keyword = set(dashboard)  # fallback: keyword page starts from campaign-attributable
        keyword_basis = "campaign_attributable_fallback"
    else:
        keyword = dashboard & keyword_keys
        keyword_basis = "uniquely_keyword_attributed"

    def _explain(keys):
        out = []
        for k in sorted(keys):
            c = by_key.get(k)
            if not c:
                continue
            out.append({
                "contact_key": k,
                "contact_id": c.get("contact_id"),
                "company": c.get("company"),
                "reasons": c.get("scope_block_reasons") or [canon.REASON_UNKNOWN],
            })
        return out

    return {
        "keyword_basis": keyword_basis,
        "dashboard_and_keyword": {"count": len(dashboard & keyword),
                                  "contact_keys": sorted(dashboard & keyword)},
        "source_and_dashboard": {"count": len(source & dashboard),
                                 "contact_keys": sorted(source & dashboard)},
        "source_only": {"count": len(source - dashboard - keyword),
                        "contacts": _explain(source - dashboard - keyword)},
        "dashboard_only": {"count": len(dashboard - source - keyword),
                           "contacts": _explain(dashboard - source - keyword)},
        "keyword_only": {"count": len(keyword - source - dashboard),
                         "contacts": _explain(keyword - source - dashboard)},
    }


# ── Window reconciliation (§5) ───────────────────────────────────────────────
def _window_reconciliation(pops_b: dict, pops_e: dict,
                           business_win: dict, evidence_win: dict) -> dict:
    """How many SQLs exist only because one window includes dates the other does
    not. Business and evidence vocabularies are compared, never merged."""
    b_keys = canon.google_ads_source_sqls(pops_b["contacts"])
    e_keys = canon.google_ads_source_sqls(pops_e["contacts"])
    b_only = b_keys - e_keys
    e_only = e_keys - b_keys

    b_by = {c["contact_key"]: c for c in pops_b["contacts"]}
    e_by = {c["contact_key"]: c for c in pops_e["contacts"]}

    def _dated(keys, by_key, reason):
        out = []
        for k in sorted(keys):
            c = by_key.get(k) or {}
            out.append({
                "contact_key": k,
                "contact_id": c.get("contact_id"),
                "company": c.get("company"),
                "contact_created_at": c.get("contact_created_at"),
                "reason": reason,
            })
        return out

    return {
        "business_population": {
            "window_key": business_win["window_key"],
            "start_date": business_win["start_date"],
            "end_date": business_win["end_date"],
            "google_ads_source_sqls": len(b_keys),
        },
        "evidence_population": {
            "window_key": evidence_win["window_key"],
            "start_date": evidence_win["start_date"],
            "end_date": evidence_win["end_date"],
            "google_ads_source_sqls": len(e_keys),
        },
        "business_only_count": len(b_only),
        "evidence_only_count": len(e_only),
        "business_only_contacts": _dated(b_only, b_by, canon.REASON_OUTSIDE_EVIDENCE_WINDOW),
        "evidence_only_contacts": _dated(e_only, e_by, canon.REASON_OUTSIDE_BUSINESS_WINDOW),
    }


# ── DB-backed runner ─────────────────────────────────────────────────────────
def run(business_window: str = _DEFAULT_BUSINESS_WINDOW,
        evidence_window: str = _DEFAULT_EVIDENCE_WINDOW,
        now: datetime | None = None) -> dict:
    """Resolve both windows, fetch read-only inputs once over their union, and
    build the audit. Enriches the keyword scope with the real keyword-attribution
    path when the database is reachable (best-effort; never fabricates)."""
    from db import canonical_contact_outcome_repository as repo  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    business_win = canon.resolve_window_contract(canon.WINDOW_BUSINESS, business_window, now=now)
    evidence_win = canon.resolve_window_contract(canon.WINDOW_EVIDENCE, evidence_window, now=now)

    # Fetch once over the union of both ranges; build_populations re-windows in
    # Python per scope.
    starts = [w["start"] for w in (business_win, evidence_win)]
    union_start = None if any(s is None for s in starts) else min(starts)
    union_end = max(business_win["end"], evidence_win["end"])
    inputs = repo.fetch_canonical_inputs(union_start, union_end)

    keyword_keys = _keyword_attributable_keys(evidence_win)
    # Use the real Google Ads campaign-identity resolver (business-window range) so
    # the audit's campaign-attributable scope matches the pages contact-for-contact.
    resolver = canon._build_identity_resolver(business_win["start"], business_win["end"])

    return build_audit(inputs, business_win, evidence_win,
                       keyword_attributable_keys=keyword_keys,
                       campaign_resolver=resolver, now=now)


def _keyword_attributable_keys(evidence_win: dict) -> set | None:
    """Best-effort durable-key set of uniquely-keyword-attributed SQLs from the
    real keyword-attribution path (the same one the Keyword Evidence page renders).
    Returns None (honest unavailable) on any failure — never a fabricated set."""
    try:
        from services import keyword_evidence_service as kw  # noqa: PLC0415

        start, end = evidence_win["start"], evidence_win["end"]
        pop = kw._build_population(start, end)
        attr = kw._keyword_sql_attribution(pop, start, end)
        if not attr.get("available"):
            return None
        keys: set = set()
        for row in (attr.get("by_criterion") or {}).values():
            keys.update(row.get("sql_contact_keys") or [])
        return keys
    except Exception as exc:  # noqa: BLE001
        logger.info("keyword attribution enrichment unavailable: %s", exc)
        return None
