"""
Campaign identity reconciliation (PR-ADS-119 / PR-ADS-120b).

The raw Google Ads campaign identity (campaign_id + historical campaign name) is
immutable truth. External HubSpot/UTM campaign labels are mapped to a canonical
Google Ads campaign through a durable, auditable mapping table.

Rules:
  - Exact normalized name matches may be auto-linked (punctuation/spacing/case
    normalization only).
  - Fuzzy matches (e.g. "mexico,chile" -> "Mexico, Chile, Colombia") are NEVER
    auto-applied — they require an explicit, auditable manual mapping. The fuzzy
    scorer only RANKS candidates for the admin workbench; it never links.
  - An unmatched external label shows "Spend mapping unavailable / ROAS
    unavailable" — never a fabricated $0 spend.
  - A label that is not a real Google Ads campaign (offline/import/bad-UTM/CRM)
    can be marked "Not Google Ads" so it is excluded from Google Ads ROAS — it
    shows "Excluded from Google Ads ROAS", never a fake $0.

Read-only with respect to Google Ads/HubSpot; the only writes are to the local
google_ads_campaign_identity table (manual mappings / exclusions), which never
overwrite the raw spend-table identity.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from difflib import SequenceMatcher

from analysis.business_windows import resolve_window
from db import revenue_repository as repo

log = logging.getLogger(__name__)

_NORM_RE = re.compile(r"[^a-z0-9]+")

# A candidate at/above this blended score is surfaced as a "suggested" fuzzy
# match. Below it, the campaign is still a selectable dropdown option (all
# active/spending and known-identity campaigns are always selectable), it is
# simply not flagged as a suggestion.
FUZZY_SUGGEST_THRESHOLD = 0.5


def normalize_campaign_name(name) -> str:
    """Normalize for exact-match comparison: lowercase, punctuation/spacing folded.

    This is deliberately NOT fuzzy — it only folds case, punctuation, and runs of
    whitespace. "Gulf - Region!" and "gulf region" normalize equal; "mexico,chile"
    and "emerging markets" do NOT.
    """
    if not name:
        return ""
    return _NORM_RE.sub(" ", str(name).strip().lower()).strip()


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fuzzy_match_score(label, campaign_name) -> float:
    """Blended fuzzy similarity in [0, 1] for RANKING candidates only.

    Combines a character-sequence ratio with a token-set Jaccard overlap and
    takes the stronger signal, so both "mexico,chile" -> "Mexico, Chile,
    Colombia" (sequence-heavy) and word-reordered labels score well. Operates on
    identity-normalized strings; never used to auto-link (admin selects).
    """
    a = normalize_campaign_name(label)
    b = normalize_campaign_name(campaign_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jaccard = (len(ta & tb) / len(ta | tb)) if (ta or tb) else 0.0
    return round(max(seq, jaccard), 4)


def auto_link_exact_matches(ga_campaigns: list, external_labels: list) -> list:
    """Propose auto-links where an external label normalizes exactly to a GA name.

    Returns proposals [{external_campaign_label, campaign_id,
    canonical_campaign_name, historical_campaign_name, match_method}]. Only exact
    normalized equality qualifies — never a fuzzy/substring match.
    """
    by_norm = {}
    for c in ga_campaigns:
        by_norm.setdefault(normalize_campaign_name(c.get("campaign_name")), c)

    proposals = []
    for label in external_labels:
        norm = normalize_campaign_name(label)
        if not norm:
            continue
        cand = by_norm.get(norm)
        if cand is not None:
            proposals.append({
                "external_campaign_label": label,
                "campaign_id": cand.get("campaign_id"),
                "canonical_campaign_name": cand.get("campaign_name"),
                "historical_campaign_name": cand.get("campaign_name"),
                "match_method": "exact_normalized",
            })
    return proposals


def record_manual_mapping(customer_id: str, external_campaign_label: str,
                          campaign_id: str, canonical_campaign_name: str, *,
                          historical_campaign_name: str | None = None,
                          approved_by: str | None = None) -> bool:
    """Persist an explicit, auditable manual mapping (approved_at set by writer).

    Never overwrites the raw spend-table identity. The historical campaign name
    is stored as a copy for audit.
    """
    import db.writers as db_writers  # noqa: PLC0415
    return db_writers.upsert_campaign_identity(
        customer_id, external_campaign_label,
        campaign_id=campaign_id,
        canonical_campaign_name=canonical_campaign_name,
        historical_campaign_name=historical_campaign_name or canonical_campaign_name,
        match_method="manual",
        approved_by=approved_by,
    )


def record_exclusion(customer_id: str, external_campaign_label: str, *,
                     approved_by: str | None = None, reason: str | None = None) -> bool:
    """Mark an external label as "Not Google Ads" (PR-ADS-120b).

    Used for labels that came from HubSpot/source attribution but are not real
    Google Ads campaigns (offline campaign, imported historical campaign, bad
    UTM, sales/import source, old manual CRM label). The label is then EXCLUDED
    from Google Ads ROAS instead of showing as an unmapped row / fake $0.

    Stored in the same auditable identity table with match_method="not_google_ads"
    and NO canonical campaign (campaign_id / canonical_campaign_name are NULL), so
    the raw Google Ads campaign identity is never touched.
    """
    if reason:
        log.info("campaign label excluded as Not-Google-Ads: %s (reason: %s)",
                 external_campaign_label, reason)
    import db.writers as db_writers  # noqa: PLC0415
    return db_writers.upsert_campaign_identity(
        customer_id, external_campaign_label,
        campaign_id=None,
        canonical_campaign_name=None,
        historical_campaign_name=external_campaign_label,
        match_method="not_google_ads",
        approved_by=approved_by,
    )


def _build_candidate_pool(ga_window_rows: list, known_campaigns: list,
                          native_currency: str, reporting_currency: str) -> list:
    """The controlled candidate universe an admin may map a label onto.

    Merges (by campaign_id) the all-time known campaign identities with the
    SELECTED-WINDOW native/USD spend, so the pool always includes:
      - active/spending campaigns in the window (with their window spend),
      - campaigns with zero spend but a known identity, and
      - archived/removed campaigns that had historical spend.
    Window spend defaults to 0 (native) / None (USD until FX complete) for a
    campaign with no spend in the selected window — never fabricated.
    """
    pool: dict[str, dict] = {}

    def _key(cid, name):
        return f"id:{cid}" if cid is not None else f"name:{normalize_campaign_name(name)}"

    for c in known_campaigns or []:
        cid = c.get("campaign_id")
        name = c.get("campaign_name")
        if cid is None and not name:
            continue
        pool[_key(cid, name)] = {
            "campaign_id": str(cid) if cid is not None else None,
            "canonical_campaign_name": name,
            "native_spend": 0.0,
            "native_currency": native_currency,
            "usd_spend": None,
            "reporting_currency": reporting_currency,
            "had_historical_spend": bool(c.get("had_historical_spend")),
            "last_spend_date": c.get("last_spend_date"),
            "has_window_spend": False,
        }

    for r in ga_window_rows or []:
        cid = r.get("campaign_id")
        name = r.get("campaign_name")
        if cid is None and not name:
            continue
        entry = pool.get(_key(cid, name))
        native = _safe_float(r.get("spend"))
        usd = _safe_float(r.get("spend_usd"))
        if entry is None:
            entry = {
                "campaign_id": str(cid) if cid is not None else None,
                "canonical_campaign_name": name,
                "native_spend": 0.0,
                "native_currency": native_currency,
                "usd_spend": None,
                "reporting_currency": reporting_currency,
                "had_historical_spend": bool(native and native > 0),
                "last_spend_date": None,
                "has_window_spend": False,
            }
            pool[_key(cid, name)] = entry
        if not entry.get("canonical_campaign_name"):
            entry["canonical_campaign_name"] = name
        if native is not None:
            entry["native_spend"] = native
        if usd is not None:
            entry["usd_spend"] = usd
        if native and native > 0:
            entry["has_window_spend"] = True
            entry["had_historical_spend"] = True

    return list(pool.values())


def build_candidates(label: str, pool: list) -> list:
    """Score + rank the candidate pool for one external label.

    Every pool campaign is returned as a selectable candidate (the searchable
    dropdown is the full controlled list). Each carries a match_score and a
    match_reason so the workbench can surface suggestions first:
      exact_normalized > fuzzy_name > active_spend > historical_spend >
      zero_spend_known_identity.
    """
    out = []
    for c in pool:
        name = c.get("canonical_campaign_name")
        score = fuzzy_match_score(label, name)
        if score >= 0.999:
            reason = "exact_normalized"
        elif score >= FUZZY_SUGGEST_THRESHOLD:
            reason = "fuzzy_name"
        elif c.get("has_window_spend"):
            reason = "active_spend"
        elif c.get("had_historical_spend"):
            reason = "historical_spend"
        else:
            reason = "zero_spend_known_identity"
        out.append({
            "campaign_id": c.get("campaign_id"),
            "canonical_campaign_name": name,
            "native_spend": c.get("native_spend"),
            "native_currency": c.get("native_currency"),
            "usd_spend": c.get("usd_spend"),
            "reporting_currency": c.get("reporting_currency"),
            "match_score": score,
            "match_reason": reason,
        })
    out.sort(key=lambda x: (x["match_score"],
                            x["usd_spend"] if x["usd_spend"] is not None else (x["native_spend"] or 0.0),
                            (x["canonical_campaign_name"] or "")),
             reverse=True)
    return out


def _confidence_for(status: str, top_score: float | None) -> str:
    if status in ("matched", "manual"):
        return "high"
    if status == "not_google_ads":
        return "n/a"
    if top_score is None:
        return "low"
    if top_score >= 0.8:
        return "high"
    if top_score >= FUZZY_SUGGEST_THRESHOLD:
        return "medium"
    return "low"


def _safe_lead_quality(start, end) -> dict:
    try:
        out = repo.fetch_lead_quality(start, end)
        return out if isinstance(out, dict) else {"rows": []}
    except Exception as exc:  # noqa: BLE001
        log.warning("mapping review lead-quality fetch failed: %s", exc)
        return {"rows": []}


def _safe_known_campaigns(customer_id) -> dict:
    try:
        out = repo.fetch_all_known_campaigns(customer_id)
        return out if isinstance(out, dict) else {"campaigns": []}
    except Exception as exc:  # noqa: BLE001
        log.warning("mapping review known-campaign fetch failed: %s", exc)
        return {"campaigns": []}


def _canonical_label_revenue(window, now) -> dict:
    """Canonical Google-Ads-scoped won deals, in the row shape this module reads.

    Returns ``{available, rows}``. Unavailable is propagated rather than replaced
    with an empty row set: an empty result would tell an admin that every label
    earned nothing, which is a much stronger claim than "we could not read the
    ledger".
    """
    from analysis import revenue_scope  # noqa: PLC0415
    from services import canonical_revenue_service as canonical_revenue  # noqa: PLC0415

    base = canonical_revenue.load_won_deals(window, now=now)
    if not base.get("available"):
        return {"available": False, "rows": [], "reason": base.get("reason")}
    return {
        "available": True,
        "rows": canonical_revenue.canonical_deal_rows(
            base, revenue_scope.SCOPE_GOOGLE_ADS_SOURCE),
    }


def build_mapping_review(window: str, now: datetime | None = None) -> dict:
    """Admin mapping-workbench payload (PR-ADS-119 / PR-ADS-120b).

    For each external HubSpot campaign label in the window, expose everything an
    admin needs to resolve it WITHOUT code/DB access: revenue, leads/SQLs/
    customers, native + USD spend, the current match status, and a fully scored
    candidate list (suggested Google Ads campaigns + the full searchable list).
    Read-only; never fabricates $0 spend for an unmatched label.
    """
    resolved = resolve_window(window, now=now)
    start = _date(resolved["start_date"])
    end = _date(resolved["end_date"])

    canonical = repo.fetch_canonical_campaign_spend(start, end)
    # PR-ADS-153E-B: the revenue attached to each external campaign label comes
    # from the canonical deal ledger at `google_ads_source` scope — wide enough to
    # include a Google Ads deal whose label has not been mapped yet (mapping it is
    # the whole point of this workbench), and narrow enough that non-advertising
    # revenue never appears beside a Google Ads campaign candidate.
    revenue = _canonical_label_revenue(window, now)
    customer_id = canonical.get("customer_id")
    identity = repo.fetch_campaign_identity(customer_id)
    leads = _safe_lead_quality(start, end)
    # Scope the candidate pool to THIS Google Ads account — never expose another
    # customer's campaigns in the workbench.
    known = _safe_known_campaigns(customer_id)

    ga_campaigns = canonical.get("rows") or []
    ga_by_norm = {normalize_campaign_name(c.get("campaign_name")): c for c in ga_campaigns}
    native_currency = canonical.get("currency_code") or "GBP"
    reporting_currency = canonical.get("reporting_currency") or "USD"

    approved_by_label = {
        normalize_campaign_name(m.get("external_campaign_label")): m
        for m in (identity.get("mappings") or [])
    }

    pool = _build_candidate_pool(ga_campaigns, known.get("campaigns"),
                                 native_currency, reporting_currency)

    # Revenue + customers per external campaign label.
    rev_by_label: dict[str, dict] = {}
    for r in (revenue.get("rows") or []):
        label = r.get("campaign_name")
        if not label:
            continue
        entry = rev_by_label.setdefault(label, {"revenue": 0.0, "deals": set()})
        # A deal whose currency was never proven contributes no money but is
        # still a customer; `or 0.0` used to bill it as $0 against the label.
        amount = r.get("deal_amount_usd")
        if amount is None:
            entry["revenue_unavailable_deals"] = entry.get(
                "revenue_unavailable_deals", 0) + 1
        else:
            entry["revenue"] += float(amount)
        deal_id = r.get("deal_id")
        if deal_id is not None:
            entry["deals"].add(deal_id)

    # Leads + SQLs per external campaign label (when lead truth is available).
    lead_by_label: dict[str, dict] = {}
    for r in (leads.get("rows") or []):
        label = r.get("campaign_name")
        if not label:
            continue
        e = lead_by_label.setdefault(label, {"leads": 0, "sqls": 0})
        e["leads"] += 1
        if r.get("status_category") == "qualified":
            e["sqls"] += 1
    leads_available = bool(leads.get("rows"))

    # Every external label that needs review: revenue labels, lead labels, and any
    # label already carrying an admin decision (manual mapping / exclusion).
    labels = set(rev_by_label) | set(lead_by_label)
    for m in (identity.get("mappings") or []):
        if m.get("external_campaign_label"):
            labels.add(m.get("external_campaign_label"))

    rows = []
    unmatched_count = 0
    for label in sorted(labels):
        norm = normalize_campaign_name(label)
        approved = approved_by_label.get(norm)
        candidate = ga_by_norm.get(norm)
        rev = rev_by_label.get(label) or {}
        ld = lead_by_label.get(label) or {}
        revenue_usd = round(float(rev.get("revenue", 0.0)), 2)
        customers = len(rev.get("deals", set()))
        # When lead truth is available, a label with no lead rows genuinely has 0
        # leads/SQLs (not "unknown"); only withhold (None) when no lead data exists.
        leads_n = ld.get("leads", 0) if leads_available else None
        sqls_n = ld.get("sqls", 0) if leads_available else None

        candidates: list = []
        if approved and approved.get("match_method") == "not_google_ads":
            status, match_status = "not_google_ads", "not_google_ads"
            cand_name, cid, ga_row = None, None, None
        elif approved:
            status, match_status = "manual", "manual"
            cand_name = approved.get("canonical_campaign_name")
            cid = approved.get("campaign_id")
            # Spend must come ONLY from the manually-mapped campaign (cid); never
            # fall back to the label's coincidental exact match (a different
            # campaign). A mapped campaign with no window spend shows no spend.
            ga_row = next((c for c in ga_campaigns if str(c.get("campaign_id")) == str(cid)), None)
            candidates = build_candidates(label, pool)
        elif candidate is not None:
            status, match_status = "matched", "exact_normalized"
            cand_name = candidate.get("campaign_name")
            cid = candidate.get("campaign_id")
            ga_row = candidate
            candidates = build_candidates(label, pool)
        else:
            status, match_status = "unmatched", "unmatched"
            cand_name, cid, ga_row = None, None, None
            candidates = build_candidates(label, pool)
            unmatched_count += 1

        native_spend = ga_row.get("spend") if ga_row else None
        usd_spend = ga_row.get("spend_usd") if ga_row else None
        top = candidates[0] if candidates else None
        top_score = top.get("match_score") if top else None
        if status == "matched":
            match_reason, match_score = "exact_normalized", 1.0
        elif status == "manual":
            match_reason, match_score = "manual", 1.0
        elif status == "not_google_ads":
            match_reason, match_score = "not_google_ads", None
        else:
            match_reason = top.get("match_reason") if top else None
            match_score = top_score

        rows.append({
            # Legacy keys (PR-ADS-119 contract — preserved for backward compat).
            "hubspot_campaign_label": label,
            "google_ads_candidate": cand_name,
            "native_spend": native_spend,
            "native_currency": native_currency,
            "usd_spend": usd_spend,
            "reporting_currency": reporting_currency,
            "revenue": revenue_usd,
            "match_status": match_status,
            # PR-ADS-120b workbench keys.
            "external_campaign_label": label,
            "revenue_usd": revenue_usd,
            "status": status,
            "campaign_id": cid,
            "canonical_campaign_name": cand_name,
            "leads": leads_n,
            "sqls": sqls_n,
            "customers": customers,
            "match_reason": match_reason,
            "match_score": match_score,
            "confidence": _confidence_for(status, top_score),
            "candidates": candidates,
        })

    return {
        "window": resolved,
        "customer_id": canonical.get("customer_id"),
        "native_currency": native_currency,
        "reporting_currency": reporting_currency,
        "rows": rows,
        "unmatched_count": unmatched_count,
        "leads_available": leads_available,
        # Full controlled campaign list (alphabetical) for the searchable dropdown.
        "campaign_options": _campaign_options(pool),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def _campaign_options(pool: list) -> list:
    """Stable, alphabetical view of the candidate pool for a dropdown."""
    opts = [{
        "campaign_id": c.get("campaign_id"),
        "canonical_campaign_name": c.get("canonical_campaign_name"),
        "native_spend": c.get("native_spend"),
        "native_currency": c.get("native_currency"),
        "usd_spend": c.get("usd_spend"),
        "reporting_currency": c.get("reporting_currency"),
        "has_window_spend": c.get("has_window_spend"),
        "had_historical_spend": c.get("had_historical_spend"),
    } for c in pool]
    opts.sort(key=lambda x: (x["canonical_campaign_name"] or "").lower())
    return opts


def _date(value):
    from datetime import date as _d  # noqa: PLC0415
    return _d.fromisoformat(value) if value else None
