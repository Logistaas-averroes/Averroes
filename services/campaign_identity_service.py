"""
Campaign identity reconciliation (PR-ADS-119).

The raw Google Ads campaign identity (campaign_id + historical campaign name) is
immutable truth. External HubSpot/UTM campaign labels are mapped to a canonical
Google Ads campaign through a durable, auditable mapping table.

Rules:
  - Exact normalized name matches may be auto-linked (punctuation/spacing/case
    normalization only).
  - Fuzzy matches (e.g. "mexico,chile" -> "Emerging Markets") are NEVER
    auto-applied — they require an explicit, auditable manual mapping.
  - An unmatched external label shows "Spend mapping unavailable / ROAS
    unavailable" — never a fabricated $0 spend.

Read-only with respect to Google Ads/HubSpot; the only writes are to the local
google_ads_campaign_identity table (manual/auto mappings), which never overwrite
the raw spend-table identity.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from analysis.business_windows import resolve_window
from db import revenue_repository as repo

log = logging.getLogger(__name__)

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_campaign_name(name) -> str:
    """Normalize for exact-match comparison: lowercase, punctuation/spacing folded.

    This is deliberately NOT fuzzy — it only folds case, punctuation, and runs of
    whitespace. "Gulf - Region!" and "gulf region" normalize equal; "mexico,chile"
    and "emerging markets" do NOT.
    """
    if not name:
        return ""
    return _NORM_RE.sub(" ", str(name).strip().lower()).strip()


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


def build_mapping_review(window: str, now: datetime | None = None) -> dict:
    """Admin mapping-review payload (PR-ADS-119).

    For each external HubSpot campaign label in the window, show the Google Ads
    candidate, native spend, USD spend, revenue, and match status. Read-only.
    """
    resolved = resolve_window(window, now=now)
    start = _date(resolved["start_date"])
    end = _date(resolved["end_date"])

    canonical = repo.fetch_canonical_campaign_spend(start, end)
    revenue = repo.fetch_won_revenue(start, end)
    identity = repo.fetch_campaign_identity(canonical.get("customer_id"))

    ga_campaigns = canonical.get("rows") or []
    ga_by_norm = {normalize_campaign_name(c.get("campaign_name")): c for c in ga_campaigns}

    approved_by_label = {
        normalize_campaign_name(m.get("external_campaign_label")): m
        for m in (identity.get("mappings") or [])
    }

    # Revenue per external campaign label.
    rev_by_label: dict[str, dict] = {}
    for r in (revenue.get("rows") or []):
        label = r.get("campaign_name")
        if not label:
            continue
        entry = rev_by_label.setdefault(label, {"label": label, "revenue": 0.0})
        entry["revenue"] += float(r.get("deal_amount_usd") or 0.0)

    reporting_currency = canonical.get("reporting_currency") or "USD"
    native_currency = canonical.get("currency_code") or "GBP"
    rows = []
    for label, info in sorted(rev_by_label.items()):
        norm = normalize_campaign_name(label)
        approved = approved_by_label.get(norm)
        candidate = ga_by_norm.get(norm)
        if approved:
            match_status = "manual" if approved.get("match_method") == "manual" else "matched"
            cand = approved.get("canonical_campaign_name")
            cid = approved.get("campaign_id")
            ga_row = next((c for c in ga_campaigns if str(c.get("campaign_id")) == str(cid)), candidate)
        elif candidate is not None:
            match_status = "exact_normalized"
            cand = candidate.get("campaign_name")
            ga_row = candidate
        else:
            match_status = "unmatched"
            cand = None
            ga_row = None

        native_spend = ga_row.get("spend") if ga_row else None
        usd_spend = ga_row.get("spend_usd") if ga_row else None
        rows.append({
            "hubspot_campaign_label": label,
            "google_ads_candidate": cand,
            "native_spend": native_spend,
            "native_currency": native_currency,
            "usd_spend": usd_spend,
            "reporting_currency": reporting_currency,
            "revenue": round(info["revenue"], 2),
            "match_status": match_status,
        })

    unmatched = [r for r in rows if r["match_status"] == "unmatched"]
    return {
        "window": resolved,
        "customer_id": canonical.get("customer_id"),
        "native_currency": native_currency,
        "reporting_currency": reporting_currency,
        "rows": rows,
        "unmatched_count": len(unmatched),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def _date(value):
    from datetime import date as _d  # noqa: PLC0415
    return _d.fromisoformat(value) if value else None
