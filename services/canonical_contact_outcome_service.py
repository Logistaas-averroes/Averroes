"""
services/canonical_contact_outcome_service.py

PR-ADS-152 — ONE canonical, read-only contact-outcome contract that reconciles the
previously contradictory SQL counts across Dashboard, Revenue by Source, Revenue
Decision Mart, Campaign Evidence, Keyword Evidence and Search Terms.

The contradiction (production example: 7 vs 1) came from every page inventing its
own "SQL" population:

  - Revenue by Source counted qualified rows straight out of
    ``contact_source_classification`` — no snapshot dedup, no
    ``lead_truth_exclusions``, no pseudo/email-campaign filter, and it trusted a
    possibly-stale classification cache. That is the broad *google_ads_source*
    number (7).
  - Keyword / Campaign / Decision-Mart pages counted deduplicated qualified
    paid-search contacts that ALSO carry a safe Google Ads campaign identity —
    the *campaign_attributable* subset (1).

Neither number is "wrong"; they are DIFFERENT scopes. This service makes the
scopes explicit and provably nested:

    keyword_attributable_sqls
      ≤ campaign_attributable_sqls
      ≤ google_ads_source_sqls
      ≤ all_source_sqls

Doctrine (never relaxed):
  - Canonical identity: ``COALESCE(NULLIF(contact_id, ''), 'id:' || leads.id)``.
  - Deduplicate to the LATEST snapshot per identity FIRST (run_date DESC, id
    DESC), THEN read its outcome — a stale qualified snapshot can never resurrect.
  - SQL = latest ``status_category = 'qualified'``.
  - Business-event date = ``contact_created_at`` (never run_date). A contact with
    no business date is not counted inside any bounded window.
  - ``lead_truth_exclusions`` removes audited non-identity/no-createdate contacts
    from every scope.
  - Acquisition group is derived from the contact's own durable HubSpot source
    (``hs_analytics_source`` + campaign detail) so the scope never depends on a
    stale classification cache; the stored classification is then RECONCILED
    against it (stale / missing detected, never silently trusted).
  - No emails are ever returned. Contact id + company only (admin drawer).

This module is import-safe and its core is pure: ``build_populations`` and every
scope/reconciliation helper operate on plain dict rows, so the whole reconciliation
is unit-testable without a database. Only ``build`` touches the read-only
repository.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from analysis.business_windows import is_valid_window, resolve_window
from analysis.evidence_windows import is_evidence_window, resolve_evidence_window
from analysis.source_classification import GROUP_GOOGLE_ADS, classify_source
from db.revenue_repository import _PSEUDO_CAMPAIGNS

logger = logging.getLogger(__name__)

# ── Reconciliation-metadata constants (PR-ADS-152 §7) ────────────────────────
SQL_DEFINITION = "latest status_category = qualified"
SQL_DATE_FIELD = "contact_created_at"
SQL_DEDUP_KEY = "COALESCE(NULLIF(contact_id, ''), 'id:' || leads.id)"

# Named scopes (PR-ADS-152 §3). Never label a subset simply "sqls" without scope.
SCOPE_ALL_SOURCE = "all_source_sqls"
SCOPE_GOOGLE_ADS_SOURCE = "google_ads_source_sqls"
SCOPE_CAMPAIGN_ATTRIBUTABLE = "campaign_attributable_sqls"
SCOPE_KEYWORD_ATTRIBUTABLE = "keyword_attributable_sqls"

# Reconciliation status vocabulary (PR-ADS-152 §7). A mismatch must never render
# as a normal count.
STATUS_RECONCILED = "reconciled"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
STATUS_MISMATCH = "mismatch"

# Window vocabularies stay distinct and are never merged (PR-ADS-141).
WINDOW_BUSINESS = "business"
WINDOW_EVIDENCE = "evidence"

_QUALIFIED = "qualified"
_EMAIL_CAMPAIGN_RE = re.compile("email_campaign", re.IGNORECASE)

# Admin-safe, per-contact explanation codes for a set difference (PR-ADS-152 §1).
REASON_EXCLUDED_BY_LEAD_TRUTH = "excluded_by_lead_truth"
REASON_LATEST_STATUS_NOT_QUALIFIED = "latest_status_not_qualified"
REASON_MISSING_CAMPAIGN = "missing_campaign"
REASON_PSEUDO_CAMPAIGN = "pseudo_campaign"
REASON_EMAIL_CAMPAIGN = "email_campaign"
REASON_NOT_GOOGLE_ADS = "not_google_ads"
REASON_SOURCE_TYPE_NOT_PAID_SEARCH = "source_type_not_paid_search"
REASON_SOURCE_CLASSIFICATION_STALE = "source_classification_stale"
REASON_MISSING_SOURCE_CLASSIFICATION = "missing_source_classification"
REASON_OUTSIDE_BUSINESS_WINDOW = "outside_business_window"
REASON_OUTSIDE_EVIDENCE_WINDOW = "outside_evidence_window"
REASON_CONTACT_KEY_MISMATCH = "contact_key_mismatch"
REASON_UNKNOWN = "unknown_reason"


# ── Small pure helpers ───────────────────────────────────────────────────────
def _norm(value) -> str:
    return (value or "").strip().lower()


def is_safe_campaign(campaign_name) -> bool:
    """A safe Google Ads campaign identity: a real campaign label — present, not a
    HubSpot traffic-source pseudo-name, and not an email-send label."""
    name = (campaign_name or "").strip()
    if not name:
        return False
    if name.lower() in _PSEUDO_CAMPAIGNS:
        return False
    if _EMAIL_CAMPAIGN_RE.search(name):
        return False
    return True


def campaign_disqualifier(campaign_name) -> str | None:
    """Which safe-campaign rule a label fails (admin-safe reason), or None."""
    name = (campaign_name or "").strip()
    if not name:
        return REASON_MISSING_CAMPAIGN
    if name.lower() in _PSEUDO_CAMPAIGNS:
        return REASON_PSEUDO_CAMPAIGN
    if _EMAIL_CAMPAIGN_RE.search(name):
        return REASON_EMAIL_CAMPAIGN
    return None


def derive_acquisition_group(row: dict) -> str:
    """Durable acquisition group derived from the contact's OWN HubSpot source
    (Original Source + campaign detail) — independent of the classification cache
    so a stale cache can never inflate a scope."""
    return classify_source(row.get("hs_analytics_source"), row.get("campaign_name"))


def deduplicate_latest(raw_rows: list[dict]) -> list[dict]:
    """One row per durable contact identity, keeping the LATEST snapshot.

    Ordering matches the rest of the system: run_date DESC, id DESC. ``run_date``
    is an ISO ``YYYY-MM-DD`` string (lexicographic == chronological); ``row_id`` is
    the surrogate leads.id. A missing run_date sorts oldest. Pure — no I/O."""
    latest: dict[str, dict] = {}
    for r in raw_rows:
        key = r.get("contact_key")
        if not key:
            continue
        cur = latest.get(key)
        if cur is None or _snapshot_sort_key(r) > _snapshot_sort_key(cur):
            latest[key] = r
    return list(latest.values())


def _snapshot_sort_key(row: dict) -> tuple:
    run_date = row.get("run_date") or ""
    try:
        row_id = int(row.get("row_id") or 0)
    except (TypeError, ValueError):
        row_id = 0
    return (run_date, row_id)


def _in_window(created_at, start: date | None, end: date | None) -> bool:
    """True when a contact's business-event date falls inside ``[start, end]``.

    A contact with no business date is NEVER inside a bounded window. ``start`` /
    ``end`` may be None (unbounded, all-time)."""
    if not created_at:
        return False
    try:
        d = date.fromisoformat(str(created_at)[:10])
    except (TypeError, ValueError):
        return False
    if start is not None and d < start:
        return False
    if end is not None and d > end:
        return False
    return True


# ── Canonical population (pure) ──────────────────────────────────────────────
def build_populations(
    lead_rows: list[dict],
    exclusions: set | None,
    classification: list[dict] | None,
    start: date | None,
    end: date | None,
) -> dict:
    """Build the canonical contact-outcome population from raw ingredients. Pure.

    Steps (in this order, always):
      1. Deduplicate raw snapshots to the latest per durable identity.
      2. Split off ``lead_truth_exclusions`` (kept for the audit, never scored).
      3. Keep contacts whose LATEST business-event date is inside ``[start, end]``.
      4. Derive the acquisition group from the contact's own source, and reconcile
         the stored classification against the canonical latest row.
      5. Assign explicit scopes.

    Returns ``{contacts, excluded, counts, ...}`` where ``contacts`` is the list of
    in-window, non-excluded canonical contact dicts (each carrying its scope flags
    and admin-safe reason codes), and ``excluded`` is the list of in-window
    excluded contacts (for the audit).
    """
    exclusions = exclusions or set()
    class_by_key = {c.get("contact_key"): c for c in (classification or [])
                    if c.get("contact_key")}

    deduped = deduplicate_latest(lead_rows)

    contacts: list[dict] = []
    excluded: list[dict] = []
    excluded_sql_count = 0
    missing_business_date_sql = 0
    stale_classification = 0
    missing_classification = 0

    for row in deduped:
        key = row.get("contact_key")
        created = row.get("contact_created_at")
        is_sql = _norm(row.get("status_category")) == _QUALIFIED
        in_win = _in_window(created, start, end)

        # Excluded contacts: recorded for the audit, removed from every scope.
        if key in exclusions:
            if in_win:
                if is_sql:
                    excluded_sql_count += 1
                excluded.append(_excluded_contact(row, is_sql))
            continue

        # Missing business date: an SQL that cannot land in a bounded window.
        if is_sql and not created and (start is not None or end is not None):
            missing_business_date_sql += 1

        if not in_win:
            continue

        group = derive_acquisition_group(row)
        stored = class_by_key.get(key)
        class_state = _classification_state(row, stored)
        if class_state == "stale":
            stale_classification += 1
        elif class_state == "missing":
            missing_classification += 1

        contact = _canonical_contact(row, group, is_sql, stored, class_state)
        contacts.append(contact)

    counts = _scope_counts(contacts)
    counts.update({
        "excluded_sql_contacts": excluded_sql_count,
        "missing_business_date_sql_contacts": missing_business_date_sql,
        "stale_classification_contacts": stale_classification,
        "missing_classification_contacts": missing_classification,
    })
    return {
        "contacts": contacts,
        "excluded": excluded,
        "counts": counts,
    }


def _classification_state(row: dict, stored: dict | None) -> str:
    """Reconcile the stored classification against the canonical latest row.

    Returns 'ok' | 'stale' | 'missing'. Stale = present but disagreeing on the
    latest outcome, the business date, or the current acquisition group — i.e. the
    cache no longer reflects canonical truth (PR-ADS-152 §6)."""
    if stored is None:
        return "missing"
    if _norm(stored.get("status_category")) != _norm(row.get("status_category")):
        return "stale"
    if _as_day(stored.get("contact_created_at")) != _as_day(row.get("contact_created_at")):
        return "stale"
    if _norm(stored.get("acquisition_group")) != _norm(derive_acquisition_group(row)):
        return "stale"
    return "ok"


def _as_day(value) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def _canonical_contact(row, group, is_sql, stored, class_state) -> dict:
    is_google_ads = group == GROUP_GOOGLE_ADS
    campaign_bad = campaign_disqualifier(row.get("campaign_name"))
    has_safe_campaign = campaign_bad is None
    is_all_source_sql = is_sql
    is_google_ads_source_sql = is_sql and is_google_ads
    is_campaign_attributable = is_google_ads_source_sql and has_safe_campaign
    return {
        "contact_key": row.get("contact_key"),
        "contact_id": row.get("contact_id"),
        "company": row.get("company"),
        "contact_created_at": row.get("contact_created_at"),
        "status_category": row.get("status_category"),
        "source_type": row.get("source_type"),
        "acquisition_group": group,
        "campaign_name": row.get("campaign_name"),
        "keyword": row.get("keyword"),
        "country": row.get("country"),
        "has_gclid": bool(row.get("has_gclid")),
        "classification_state": class_state,
        "stored_acquisition_group": (stored or {}).get("acquisition_group"),
        "is_sql": is_sql,
        "is_all_source_sql": is_all_source_sql,
        "is_google_ads_source_sql": is_google_ads_source_sql,
        "is_campaign_attributable_sql": is_campaign_attributable,
        "scope_block_reasons": _scope_block_reasons(
            row, group, is_sql, campaign_bad, class_state),
    }


def _scope_block_reasons(row, group, is_sql, campaign_bad, class_state) -> list[str]:
    """Admin-safe reasons this contact drops out of the narrower scopes. Ordered
    from broadest scope inward so the audit can explain any set difference."""
    reasons: list[str] = []
    if not is_sql:
        reasons.append(REASON_LATEST_STATUS_NOT_QUALIFIED)
        return reasons
    if group != GROUP_GOOGLE_ADS:
        reasons.append(REASON_NOT_GOOGLE_ADS)
        if _norm(row.get("source_type")) != "paid_search":
            reasons.append(REASON_SOURCE_TYPE_NOT_PAID_SEARCH)
    if campaign_bad:
        reasons.append(campaign_bad)
    if class_state == "stale":
        reasons.append(REASON_SOURCE_CLASSIFICATION_STALE)
    elif class_state == "missing":
        reasons.append(REASON_MISSING_SOURCE_CLASSIFICATION)
    return reasons


def _excluded_contact(row, is_sql) -> dict:
    return {
        "contact_key": row.get("contact_key"),
        "contact_id": row.get("contact_id"),
        "company": row.get("company"),
        "contact_created_at": row.get("contact_created_at"),
        "status_category": row.get("status_category"),
        "is_sql": is_sql,
        "scope_block_reasons": [REASON_EXCLUDED_BY_LEAD_TRUTH],
    }


def _scope_counts(contacts: list[dict]) -> dict:
    return {
        "total_all_source_sqls": sum(1 for c in contacts if c["is_all_source_sql"]),
        "google_ads_source_sqls": sum(1 for c in contacts if c["is_google_ads_source_sql"]),
        "campaign_attributable_sqls": sum(1 for c in contacts if c["is_campaign_attributable_sql"]),
    }


# ── Named scope contact-key sets (PR-ADS-152 §3) ─────────────────────────────
def scope_keys(contacts: list[dict], scope: str) -> set:
    """Contact-key set for a named scope. Never returns a subset without the
    caller naming the scope explicitly."""
    if scope == SCOPE_ALL_SOURCE:
        return {c["contact_key"] for c in contacts if c["is_all_source_sql"]}
    if scope == SCOPE_GOOGLE_ADS_SOURCE:
        return {c["contact_key"] for c in contacts if c["is_google_ads_source_sql"]}
    if scope == SCOPE_CAMPAIGN_ATTRIBUTABLE:
        return {c["contact_key"] for c in contacts if c["is_campaign_attributable_sql"]}
    raise ValueError(f"Unknown scope '{scope}'")


def all_source_sqls(contacts):
    return scope_keys(contacts, SCOPE_ALL_SOURCE)


def google_ads_source_sqls(contacts):
    return scope_keys(contacts, SCOPE_GOOGLE_ADS_SOURCE)


def campaign_attributable_sqls(contacts):
    return scope_keys(contacts, SCOPE_CAMPAIGN_ATTRIBUTABLE)


# ── Reconciliation metadata (PR-ADS-152 §7) ──────────────────────────────────
def reconciliation_metadata(
    populations: dict,
    scope: str,
    *,
    available: bool = True,
    keyword_attributable: int | None = None,
    consumer_count: int | None = None,
) -> dict:
    """The reconciliation-metadata block every consuming API must expose.

    ``scope`` is the scope THIS consumer's headline count represents.
    ``consumer_count`` (optional) is the page's own rendered count for that scope;
    when it disagrees with the canonical count the status is ``mismatch`` — which
    must never render as a normal number (PR-ADS-152 §7).
    """
    counts = populations.get("counts", {}) if populations else {}
    status = _reconciliation_status(
        available=available, counts=counts,
        keyword_attributable=keyword_attributable,
        scope=scope, consumer_count=consumer_count)

    # Unavailable → every count is None, never a fabricated 0 (PR-ADS doctrine).
    if status == STATUS_UNAVAILABLE:
        total = ga = camp = excluded = unmatched = keyword_attributable = None
    else:
        total = counts.get("total_all_source_sqls")
        ga = counts.get("google_ads_source_sqls")
        camp = counts.get("campaign_attributable_sqls")
        excluded = counts.get("excluded_sql_contacts")
        unmatched = None
        if ga is not None and camp is not None:
            unmatched = ga - camp

    return {
        "sql_definition": SQL_DEFINITION,
        "sql_date_field": SQL_DATE_FIELD,
        "sql_dedup_key": SQL_DEDUP_KEY,
        "sql_scope": scope,
        "total_all_source_sqls": total,
        "google_ads_source_sqls": ga,
        "campaign_attributable_sqls": camp,
        "keyword_attributable_sqls": keyword_attributable,
        "excluded_sql_contacts": excluded,
        "unmatched_sql_contacts": unmatched,
        "reconciliation_status": status,
    }


def _reconciliation_status(*, available, counts, keyword_attributable,
                           scope, consumer_count) -> str:
    if not available or not counts:
        return STATUS_UNAVAILABLE
    total = counts.get("total_all_source_sqls")
    ga = counts.get("google_ads_source_sqls")
    camp = counts.get("campaign_attributable_sqls")

    # Subset invariant: keyword ≤ campaign ≤ google_ads ≤ all. A violation is a
    # real defect and must surface as a mismatch, never a normal count.
    ordered = [keyword_attributable, camp, ga, total]
    present = [x for x in ordered if x is not None]
    for smaller, larger in zip(present, present[1:]):
        if smaller > larger:
            return STATUS_MISMATCH

    # The page rendered a different count than canonical for its own scope.
    if consumer_count is not None:
        canonical = {
            SCOPE_ALL_SOURCE: total,
            SCOPE_GOOGLE_ADS_SOURCE: ga,
            SCOPE_CAMPAIGN_ATTRIBUTABLE: camp,
            SCOPE_KEYWORD_ATTRIBUTABLE: keyword_attributable,
        }.get(scope)
        if canonical is not None and consumer_count != canonical:
            return STATUS_MISMATCH

    # Coverage gap: qualified contacts excluded, or Google-Ads-source SQLs that are
    # not campaign-attributable, or a stale/missing classification cache — all
    # honest "partial" states (the difference is explained, not hidden).
    if counts.get("excluded_sql_contacts"):
        return STATUS_PARTIAL
    if counts.get("stale_classification_contacts") or counts.get("missing_classification_contacts"):
        return STATUS_PARTIAL
    if ga is not None and camp is not None and ga != camp:
        return STATUS_PARTIAL
    if keyword_attributable is not None and camp is not None and keyword_attributable != camp:
        return STATUS_PARTIAL
    return STATUS_RECONCILED


# ── Window resolution (business | evidence — never merged) ───────────────────
def resolve_window_contract(window_type: str, window_key: str,
                            now: datetime | None = None) -> dict:
    """Resolve a window in its OWN vocabulary and expose the reconciliation window
    block (PR-ADS-152 §5). ``window_type`` is 'business' or 'evidence'; the two
    vocabularies are never mixed.

    Returns ``{window_type, window_key, start_date, end_date, date_field, start,
    end}`` where ``start`` / ``end`` are ``date`` objects (start may be None for
    all-time) and the ``*_date`` fields are ISO strings.
    """
    now = now or datetime.now(timezone.utc)
    if window_type == WINDOW_BUSINESS:
        if not is_valid_window(window_key):
            raise ValueError(f"Invalid business window '{window_key}'")
        resolved = resolve_window(window_key, now=now)
        start = date.fromisoformat(resolved["start_date"]) if resolved["start_date"] else None
        end = date.fromisoformat(resolved["end_date"])
        return _window_block(WINDOW_BUSINESS, window_key, start, end)
    if window_type == WINDOW_EVIDENCE:
        if not is_evidence_window(window_key):
            raise ValueError(f"Invalid evidence window '{window_key}'")
        resolved = resolve_evidence_window(window_key)
        end = now.date()
        days = resolved["days"]
        start = None if days is None else (end - _timedelta_days(days - 1))
        return _window_block(WINDOW_EVIDENCE, window_key, start, end)
    raise ValueError(f"Unknown window_type '{window_type}' (business|evidence)")


def _timedelta_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _window_block(window_type, window_key, start: date | None, end: date) -> dict:
    return {
        "window_type": window_type,
        "window_key": window_key,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat(),
        "date_field": SQL_DATE_FIELD,
        "start": start,
        "end": end,
    }


# ── DB-backed entry point ────────────────────────────────────────────────────
def build(window_type: str, window_key: str, now: datetime | None = None) -> dict:
    """Resolve the window, fetch the read-only inputs, and build the canonical
    populations + reconciliation metadata. The ONE place this module touches the
    database.

    Returns ``{available, window, counts, contacts, excluded, reconciliation}``.
    """
    from db import canonical_contact_outcome_repository as repo  # noqa: PLC0415

    win = resolve_window_contract(window_type, window_key, now=now)
    inputs = repo.fetch_canonical_inputs(win["start"], win["end"])
    available = inputs.get("available", False)
    populations = build_populations(
        inputs.get("lead_rows") or [],
        inputs.get("exclusions") or set(),
        inputs.get("classification") or [],
        win["start"], win["end"],
    )
    window_out = {k: v for k, v in win.items() if k not in ("start", "end")}
    return {
        "available": available,
        "window": window_out,
        "counts": populations["counts"],
        "contacts": populations["contacts"],
        "excluded": populations["excluded"],
        "reconciliation": reconciliation_metadata(
            populations, SCOPE_GOOGLE_ADS_SOURCE, available=available),
        "generated_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def page_reconciliation(window_type: str, window_key: str, scope: str,
                        now: datetime | None = None,
                        consumer_count: int | None = None,
                        keyword_attributable: int | None = None) -> dict:
    """Additive reconciliation-metadata block for a consuming page (PR-ADS-152 §7).

    Defensive: any failure (bad window, DB down) yields an ``unavailable`` block
    rather than breaking the page — a page must never crash for lack of the
    reconciliation layer, and an unavailable reconciliation is never a fabricated
    zero. ``scope`` names what THIS page's headline count represents so the subset
    is never rendered under a bare "sqls" label. ``keyword_attributable`` is the
    page's uniquely-keyword-attributed count when relevant (Keyword Evidence).
    """
    try:
        result = build(window_type, window_key, now=now)
        block = reconciliation_metadata(
            {"counts": result["counts"]}, scope,
            available=result["available"], consumer_count=consumer_count,
            keyword_attributable=keyword_attributable)
        block["window"] = result["window"]
        return block
    except Exception as exc:  # noqa: BLE001
        logger.info("page_reconciliation unavailable (%s/%s): %s",
                    window_type, window_key, exc)
        return {
            "sql_definition": SQL_DEFINITION,
            "sql_date_field": SQL_DATE_FIELD,
            "sql_dedup_key": SQL_DEDUP_KEY,
            "sql_scope": scope,
            "total_all_source_sqls": None,
            "google_ads_source_sqls": None,
            "campaign_attributable_sqls": None,
            "keyword_attributable_sqls": None,
            "excluded_sql_contacts": None,
            "unmatched_sql_contacts": None,
            "reconciliation_status": STATUS_UNAVAILABLE,
        }


__all__ = [
    "SCOPE_ALL_SOURCE", "SCOPE_GOOGLE_ADS_SOURCE", "SCOPE_CAMPAIGN_ATTRIBUTABLE",
    "SCOPE_KEYWORD_ATTRIBUTABLE", "STATUS_RECONCILED", "STATUS_PARTIAL",
    "STATUS_UNAVAILABLE", "STATUS_MISMATCH", "WINDOW_BUSINESS", "WINDOW_EVIDENCE",
    "build", "build_populations", "deduplicate_latest", "derive_acquisition_group",
    "is_safe_campaign", "scope_keys", "all_source_sqls", "google_ads_source_sqls",
    "campaign_attributable_sqls", "reconciliation_metadata",
    "resolve_window_contract", "page_reconciliation",
]
