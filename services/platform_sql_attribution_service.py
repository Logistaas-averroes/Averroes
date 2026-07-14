"""
services/platform_sql_attribution_service.py

PR-ADS-146C — ONE shared, read-only SQL-attribution path used by BOTH the Keyword
Evidence and Search Terms pages. Adds HubSpot-confirmed **Attributed SQLs** to
each keyword criterion / term × campaign unit without either page rebuilding the
SQL logic itself.

Doctrine (never relaxed):
  - SQL = deduplicated qualified paid-search contact (see
    db.platform_sql_attribution_repository) on its ``contact_created_at`` event
    date; Google Ads platform conversions, MQLs, meetings, in-progress leads and
    deals are NOT SQLs, and a contact counts once regardless of scheduler
    snapshots or associated deals.
  - Campaign identity reuses the Campaign Evidence doctrine
    (``_resolve_campaign_identity``): durable id → approved mapping →
    exact-normalized unique fallback; never fuzzy, never merges duplicate display
    names, ``not_google_ads`` excluded, unresolved labels stay unattributed.
  - Keyword attribution assigns a contact to a criterion ONLY when exactly one
    criterion matches (canonical campaign id + exact normalized keyword). The
    same keyword text across ad groups / criterion ids / duplicate campaign ids is
    AMBIGUOUS and attaches to none — the count is never copied onto every row.
  - Search-term attribution requires a directly persisted, non-empty user query.
    The durable ``leads`` table stores only a HubSpot keyword, never the query, so
    exact search-term coverage is currently zero and every unit is reported
    UNAVAILABLE (—), never a fabricated 0. A search term is NEVER inferred from a
    keyword / match type / campaign / similarity.

Strictly read-only: no Google Ads / HubSpot writes, no mutations, no deletions.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

logger = logging.getLogger(__name__)

# ── API-audit constants (PR-ADS-146C §13) ────────────────────────────────────
SQL_SOURCE = "HubSpot"
SQL_DEFINITION = "status_category qualified"
SQL_DATE_FIELD = "contact_created_at"
SQL_DEDUP_KEY = "COALESCE(NULLIF(contact_id, ''), 'id:' || leads.id)"
SQL_ATTRIBUTION_METHOD_KEYWORD = "canonical_campaign_id + exact_normalized_keyword (unique criterion only)"
SQL_ATTRIBUTION_METHOD_SEARCH_TERM = "canonical_campaign_id + exact_persisted_search_term (unique unit only)"
KEYWORD_ATTR_SOURCE = "hubspot_keyword_exact"
SEARCH_TERM_ATTR_SOURCE = "hubspot_query_exact"

# Per-row attribution states.
ST_ATTRIBUTED = "attributed"
ST_KNOWN_ZERO = "known_zero"
ST_AMBIGUOUS = "ambiguous"
ST_UNAVAILABLE = "unavailable"
ST_MAPPING_REVIEW = "mapping_review"
# PR-ADS-146C §2 — a row that WOULD be zero but cannot be proven zero because the
# window's SQL population has unresolved-campaign / missing-text contacts that
# could plausibly belong to it. Displays "—", never a fabricated 0.
ST_PARTIAL_ATTRIBUTION = "partial_attribution"

_PARTIAL_ZERO_REASON = (
    "Attribution coverage is incomplete for this window — some qualified contacts "
    "have an unresolved campaign or no keyword evidence and could plausibly belong "
    "here, so a zero cannot be proven for this keyword.")


# ── Normalization ────────────────────────────────────────────────────────────
def normalize_term(value) -> str:
    """Exact-equality normalization for keyword / search-term text: lowercase,
    strip surrounding match-type wrappers ([exact], "phrase"), drop the broad
    modifier '+', and collapse internal whitespace. Deterministic — never fuzzy."""
    if value is None:
        return ""
    t = str(value).strip().lower()
    if len(t) >= 2 and ((t[0] == "[" and t[-1] == "]") or (t[0] == '"' and t[-1] == '"')):
        t = t[1:-1].strip()
    t = t.replace("+", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ── SQL population + campaign identity resolution ────────────────────────────
def fetch_and_resolve_contacts(start: date | None, end: date) -> dict:
    """Deduplicated qualified SQL contacts for ``[start, end]`` with each
    contact's canonical campaign identity resolved by the Campaign Evidence
    doctrine. Read-only. Returns ``{available, contacts, leads_have_search_term}``.
    """
    import db.platform_sql_attribution_repository as sql_repo  # noqa: PLC0415
    import db.revenue_repository as revenue_repo  # noqa: PLC0415
    from services.search_term_evidence_service import (  # noqa: PLC0415
        _identity_label_index, _resolve_campaign_identity, _spend_index,
    )

    repo = sql_repo.fetch_sql_contacts(start, end)
    if not repo.get("available"):
        return {"available": False, "contacts": [],
                "leads_have_search_term": sql_repo.LEADS_HAVE_SEARCH_TERM}

    canonical = revenue_repo.fetch_canonical_campaign_spend(start, end)
    identity = revenue_repo.fetch_campaign_identity(canonical.get("customer_id"))
    spend_by_id, norm_to_ids = _spend_index(canonical)
    identity_by_label, _aliases = _identity_label_index(identity)

    contacts: list = []
    for r in repo.get("rows") or []:
        status, campaign_key, _display = _resolve_campaign_identity(
            None, r.get("campaign_name"), spend_by_id, norm_to_ids, identity_by_label)
        # A canonical Google Ads campaign id exists only for a 'mapped' label.
        cid = campaign_key if status == "mapped" else None
        sql_date = r.get("contact_created_at")
        contacts.append({
            "contact_key": r.get("contact_key"),
            "contact_id": r.get("contact_id"),
            "company": r.get("company"),
            "sql_date": sql_date.isoformat() if isinstance(sql_date, date) else sql_date,
            "campaign_label": r.get("campaign_name"),
            "campaign_mapping_status": status,
            "campaign_key": campaign_key,
            "canonical_campaign_id": cid,
            "keyword_raw": r.get("keyword"),
            "keyword_norm": normalize_term(r.get("keyword")),
            "search_term_raw": r.get("search_term"),
            "search_term_norm": normalize_term(r.get("search_term")),
            "has_gclid": bool(r.get("has_gclid")),
        })
    return {"available": True, "contacts": contacts,
            "leads_have_search_term": repo.get("leads_have_search_term", False)}


# ── Generic exact-unique attribution ─────────────────────────────────────────
def _attribute(contacts: list, units: list, *, text_field: str, available: bool,
               population_has_text: bool, zero_proof: bool) -> dict:
    """Assign each SQL contact to at most ONE unit by (canonical campaign id +
    exact normalized ``text_field``), only when exactly one unit matches. Shared
    by keyword (text_field='keyword_norm') and search-term
    (text_field='search_term_norm') attribution.

    ``units`` items: {key, campaign_id, text_norm, mapping_status}.
    ``zero_proof``: whether the window's SQL population is complete enough to prove
    a zero (no unresolved-campaign / missing-text contacts). When False a
    would-be-zero row is reported ``partial_attribution`` (—), never a fake 0.
    Returns {by_unit, reconciliation, coverage, contacts_by_unit}.
    """
    # Group attributable units by (campaign_id, text_norm) — only mapped units
    # with a non-empty text participate in an attribution key.
    by_ck: dict = {}
    for u in units:
        if u.get("mapping_status") != "mapped":
            continue
        tnorm = u.get("text_norm") or ""
        cid = u.get("campaign_id")
        if not tnorm or not cid:
            continue
        by_ck.setdefault((str(cid), tnorm), []).append(u["key"])

    assigned_to: dict = {}          # unit key -> [contact_key, …]
    uniquely_attributed = 0
    ambiguous_contacts = 0
    contact_matched_key: dict = {}  # contact_key -> unit key (for reconciliation)
    for c in contacts:
        cid = c.get("canonical_campaign_id")
        tnorm = c.get(text_field) or ""
        if not cid or not tnorm:
            continue                # campaign-unmatched or missing text → unattributed
        group = by_ck.get((str(cid), tnorm))
        if not group:
            continue                # campaign-matched but text not in population
        if len(group) == 1:
            key = group[0]
            assigned_to.setdefault(key, []).append(c["contact_key"])
            contact_matched_key[c["contact_key"]] = key
            uniquely_attributed += 1
        else:
            ambiguous_contacts += 1  # attaches to NONE — never copied onto rows

    by_unit: dict = {}
    row_sql_sum = 0
    for u in units:
        key = u["key"]
        if not available:
            by_unit[key] = _row_state(ST_UNAVAILABLE, None, source=None)
            continue
        if u.get("mapping_status") != "mapped":
            by_unit[key] = _row_state(ST_MAPPING_REVIEW, None,
                                      reason="Campaign identity unresolved (mapping review)")
            continue
        tnorm = u.get("text_norm") or ""
        cid = u.get("campaign_id")
        # No text evidence exists in the CRM population at all → unavailable (—),
        # never a fabricated 0 (search-term case, PR-ADS-146C §5/§6).
        if not population_has_text:
            by_unit[key] = _row_state(ST_UNAVAILABLE, None,
                                      reason="No exact query evidence in the CRM population")
            continue
        group = by_ck.get((str(cid), tnorm), [])
        if len(group) > 1:
            by_unit[key] = _row_state(ST_AMBIGUOUS, None, candidate_count=len(group),
                                      reason=(f"'{u.get('text_raw') or tnorm}' matches "
                                              f"{len(group)} criteria under this campaign"))
            continue
        keys = assigned_to.get(key, [])
        row_sql_sum += len(keys)
        src = (KEYWORD_ATTR_SOURCE if text_field == "keyword_norm"
               else SEARCH_TERM_ATTR_SOURCE)
        if keys:
            # A confirmed positive attribution — always visible, even under
            # partial coverage (it is a confirmed attributed count, not a claim
            # of the complete SQL total).
            by_unit[key] = _row_state(ST_ATTRIBUTED, len(keys), source=src, contact_keys=keys)
        elif zero_proof:
            by_unit[key] = _row_state(ST_KNOWN_ZERO, 0, source=src, contact_keys=[])
        else:
            by_unit[key] = _row_state(ST_PARTIAL_ATTRIBUTION, None, reason=_PARTIAL_ZERO_REASON)

    total = len(contacts)
    unattributed = total - uniquely_attributed - ambiguous_contacts
    recon_status = "ok" if row_sql_sum == uniquely_attributed else "mismatch"
    return {
        "by_unit": by_unit,
        "reconciliation": {
            "total_sql_contacts": total,
            "uniquely_attributed_sql_contacts": uniquely_attributed,
            "ambiguous_sql_contacts": ambiguous_contacts,
            "unattributed_sql_contacts": unattributed,
            "row_sql_sum": row_sql_sum,
            "reconciliation_status": recon_status,
        },
        "uniquely_attributed": uniquely_attributed,
        "ambiguous_contacts": ambiguous_contacts,
    }


def _row_state(status, count, *, source=None, contact_keys=None,
               candidate_count=None, reason=None) -> dict:
    return {
        "attributed_sqls": count,
        "sql_attribution_status": status,
        "sql_attribution_source": source if status in (ST_ATTRIBUTED, ST_KNOWN_ZERO) else None,
        "sql_contact_keys": list(contact_keys or []),
        "sql_candidate_count": candidate_count,
        "sql_ambiguity_reason": reason,
    }


# ── Attribution completeness / zero-proof (§2/§3) ────────────────────────────
def _completeness(contacts: list, *, text_field: str, available: bool) -> dict:
    """Whether the window's SQL population is complete enough to PROVE a zero.
    A single unresolved-campaign or missing-text contact could plausibly belong
    to any criterion, so it blocks every known-zero for the window."""
    total = len(contacts)
    with_identity = sum(1 for c in contacts if c.get("canonical_campaign_id"))
    with_text = sum(1 for c in contacts if c.get(text_field))
    missing_identity = total - with_identity
    missing_text = sum(1 for c in contacts if not c.get(text_field))
    if not available:
        status = "unavailable"
    elif missing_identity == 0 and missing_text == 0:
        status = "complete"
    else:
        status = "partial"
    # A genuinely empty (but available) population proves zero for all criteria.
    zero_proof = available and missing_identity == 0 and missing_text == 0
    return {
        "sql_contacts_with_campaign_identity": with_identity,
        "sql_contacts_with_keyword" if text_field == "keyword_norm"
        else "sql_contacts_with_exact_search_term": with_text,
        "sql_contacts_missing_campaign_identity": missing_identity,
        "sql_contacts_missing_keyword" if text_field == "keyword_norm"
        else "sql_contacts_missing_search_term": missing_text,
        "sql_attribution_completeness_status": status,
        "zero_proof_available": zero_proof,
    }


# ── Keyword attribution (§4) ─────────────────────────────────────────────────
def attribute_keywords(contacts: list, criteria: list, *, available: bool) -> dict:
    """``criteria``: [{criterion_key, campaign_id, keyword_text, mapping_status}].
    Returns per-criterion attribution + reconciliation + coverage + audit."""
    units = [{
        "key": c["criterion_key"],
        "campaign_id": c.get("campaign_id"),
        "text_norm": normalize_term(c.get("keyword_text")),
        "text_raw": c.get("keyword_text"),
        "mapping_status": c.get("mapping_status"),
    } for c in criteria]

    completeness = _completeness(contacts, text_field="keyword_norm", available=available)
    res = _attribute(contacts, units, text_field="keyword_norm",
                     available=available, population_has_text=available,
                     zero_proof=completeness["zero_proof_available"])
    audit = _keyword_audit(contacts, res, available=available, completeness=completeness)
    return {
        "by_criterion": res["by_unit"],
        "reconciliation": res["reconciliation"],
        "coverage": audit["coverage"],
        "completeness": completeness,
        "audit": audit,
        "available": available,
    }


def _keyword_audit(contacts: list, res: dict, *, available: bool, completeness: dict) -> dict:
    total = len(contacts)
    with_gclid = sum(1 for c in contacts if c.get("has_gclid"))
    with_identity = sum(1 for c in contacts if c.get("canonical_campaign_id"))
    with_keyword = sum(1 for c in contacts if c.get("keyword_norm"))
    with_search_term = sum(1 for c in contacts if c.get("search_term_norm"))
    campaign_unmatched = sum(1 for c in contacts if not c.get("canonical_campaign_id"))
    missing_keyword = sum(1 for c in contacts if not c.get("keyword_norm"))
    uniq = res["uniquely_attributed"]
    cov_pct = round(uniq / total * 100.0, 1) if total else None
    return {
        "population": {
            "total_sql_contacts": total,
            "sql_contacts_with_gclid": with_gclid,
            "sql_contacts_with_campaign_identity": with_identity,
            "sql_contacts_with_keyword": with_keyword,
            "sql_contacts_with_exact_search_term": with_search_term,
        },
        "keyword_attribution": {
            "uniquely_attributed_sql_contacts": uniq,
            "ambiguous_sql_contacts": res["ambiguous_contacts"],
            "campaign_unmatched_sql_contacts": campaign_unmatched,
            "missing_keyword_sql_contacts": missing_keyword,
            "coverage_pct": cov_pct,
        },
        "completeness": completeness,
        "coverage": {
            "available": available,
            "total_sql_contacts": total,
            "uniquely_attributed_sql_contacts": uniq,
            "coverage_pct": cov_pct,
            "grain": "keyword",
            **completeness,
        },
    }


# ── Search-term attribution (§5) ─────────────────────────────────────────────
def attribute_search_terms(contacts: list, units_in: list, *, available: bool,
                           leads_have_search_term: bool) -> dict:
    """``units_in``: [{unit_key, campaign_id, search_term, mapping_status}].
    A unit is attributed only from a directly persisted, exact user query; with no
    persisted query in the CRM every unit is UNAVAILABLE (—), never zero."""
    population_has_text = bool(leads_have_search_term
                              and any(c.get("search_term_norm") for c in contacts))
    units = [{
        "key": u["unit_key"],
        "campaign_id": u.get("campaign_id"),
        "text_norm": normalize_term(u.get("search_term")),
        "text_raw": u.get("search_term"),
        "mapping_status": u.get("mapping_status"),
    } for u in units_in]

    completeness = _completeness(contacts, text_field="search_term_norm", available=available)
    res = _attribute(contacts, units, text_field="search_term_norm",
                     available=available, population_has_text=population_has_text,
                     zero_proof=completeness["zero_proof_available"] and population_has_text)
    audit = _search_term_audit(contacts, res, available=available,
                               population_has_text=population_has_text,
                               completeness=completeness)
    return {
        "by_unit": res["by_unit"],
        "reconciliation": res["reconciliation"],
        "coverage": audit["coverage"],
        "completeness": completeness,
        "audit": audit,
        "available": available,
        "population_has_text": population_has_text,
    }


def _search_term_audit(contacts: list, res: dict, *, available: bool,
                       population_has_text: bool, completeness: dict) -> dict:
    total = len(contacts)
    with_search_term = sum(1 for c in contacts if c.get("search_term_norm"))
    campaign_unmatched = sum(1 for c in contacts if not c.get("canonical_campaign_id"))
    missing_search_term = sum(1 for c in contacts if not c.get("search_term_norm"))
    uniq = res["uniquely_attributed"]
    cov_pct = round(uniq / total * 100.0, 1) if total else None
    return {
        "search_term_attribution": {
            # §4 — these are DISTINCT: contacts that carried an exact user query
            # vs. contacts uniquely attributed to a term. Never conflate them.
            "sql_contacts_with_exact_search_term": with_search_term,
            "uniquely_attributed_search_term_sql_contacts": uniq,
            "ambiguous_sql_contacts": res["ambiguous_contacts"],
            "missing_search_term_sql_contacts": missing_search_term,
            "campaign_unmatched_sql_contacts": campaign_unmatched,
            "coverage_pct": cov_pct,
            "exact_query_evidence_available": population_has_text,
        },
        "coverage": {
            "available": available,
            "total_sql_contacts": total,
            "sql_contacts_with_exact_search_term": with_search_term,
            "uniquely_attributed_search_term_sql_contacts": uniq,
            "uniquely_attributed_sql_contacts": uniq,
            "coverage_pct": cov_pct,
            "exact_query_evidence_available": population_has_text,
            "grain": "search_term",
            **completeness,
        },
    }


# ── Drawer contact detail (§7/§8 — no email addresses) ───────────────────────
def contact_details_for_keys(contacts: list, contact_keys: list) -> list:
    """Compact deduplicated SQL contact rows for a drawer — company, contact id,
    SQL date, campaign label, keyword evidence, GCLID presence. Never emails."""
    wanted = set(contact_keys or [])
    out = []
    for c in contacts:
        if c.get("contact_key") not in wanted:
            continue
        out.append({
            "contact_key": c.get("contact_key"),
            "contact_id": c.get("contact_id"),
            "company": c.get("company"),
            "sql_date": c.get("sql_date"),
            "campaign_label": c.get("campaign_label"),
            "campaign_mapping_status": c.get("campaign_mapping_status"),
            "keyword": c.get("keyword_raw"),
            "search_term": c.get("search_term_raw"),
            "has_gclid": bool(c.get("has_gclid")),
        })
    out.sort(key=lambda r: (r.get("sql_date") or ""), reverse=True)
    return out
