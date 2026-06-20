"""
Revenue Attribution Service (PR-ADS-107A)

Shared, read-only data contract that powers both the ROAS by Campaign and the
ROAS by Country pages from a single truth source. It resolves a business window
(see analysis/business_windows.py) and aggregates revenue-attribution metrics
per campaign and per country.

Data doctrine:
  - HubSpot closed-won deals are revenue truth.
  - Google Ads API spend is platform/spend evidence.
  - Google Ads conversion value is NOT used as revenue truth.
  - Attribution uncertainty is surfaced, never hidden (High / Medium / Low).

Metrics per row:
  - spend         Google Ads API spend in the window
  - leads         attributed HubSpot paid-search contacts in the window
  - sqls          contacts with an existing SQL-qualified status (analysis.core)
  - customers     distinct closed-won deals (company association not modelled)
  - won_revenue   HubSpot closed-won amount
  - roas          won_revenue / spend   (None if spend == 0)
  - cac           spend / customers     (None if customers == 0)
  - confidence    high | medium | low   (from attribution tier)
  - verdict       winner | watch | waste | learning
  - attribution_notes

What is NOT in this file:
  - No external API calls (no HubSpot, Google Ads, or network requests).
  - No Google Ads writes. No HubSpot writes.
  - No bid/budget/campaign/keyword changes. No OCT.
  - No frontend formatting.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from analysis.attribution_confidence import get_confidence_severity
from analysis.attribution_matcher import attribute_deals
from analysis.business_windows import get_window_bounds, resolve_window
from analysis.core import QUALIFIED
from services.country_codes import get_country_code

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
SPEND_FILE = DATA_DIR / "campaign_performance.json"
CONTACTS_FILE = DATA_DIR / "crm_contacts.json"

# Verdict thresholds. Kept deliberately simple — see classify_verdict.
HEALTHY_ROAS = 1.0            # won_revenue >= spend is considered healthy
MIN_SPEND_FOR_VERDICT = 50.0  # below this (and no revenue) we are still learning

# Map an attribution tier severity to the public confidence label.
# get_confidence_severity already returns "high" | "medium" | "low".


# ── Data loaders (module-level so tests can patch them) ─────────────────────


def _load_json(path: Path) -> list:
    """Load a JSON list from ``path``; return [] if missing or unreadable."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return []


def _load_attributed_deals() -> list:
    """Closed-won deals with attribution tier/campaign/country attached."""
    return attribute_deals() or []


def _load_spend_rows() -> list:
    """Google Ads API spend/performance rows (campaign + country + date)."""
    return _load_json(SPEND_FILE)


def _load_contacts() -> list:
    """HubSpot paid-search contacts used for lead / SQL counts."""
    return _load_json(CONTACTS_FILE)


# ── Parsing / safety helpers ────────────────────────────────────────────────


def _parse_dt(value) -> datetime | None:
    """Parse an ISO date/datetime (or epoch-ms string) to aware UTC, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        # HubSpot timestamps are sometimes epoch milliseconds.
        if text.isdigit() and len(text) >= 12:
            try:
                return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(dt: datetime | None, start_dt: datetime | None, end_dt: datetime) -> bool:
    """Return True if ``dt`` falls inside [start_dt, end_dt).

    Undated rows are included only for unbounded (all_time) windows.
    """
    if dt is None:
        return start_dt is None
    if start_dt is not None and dt < start_dt:
        return False
    if dt >= end_dt:
        return False
    return True


def _norm(value) -> str:
    """Normalised grouping key: lowercased, trimmed."""
    return (value or "").strip().lower()


def _safe_float(value) -> float:
    """Coerce ``value`` to float, defaulting to 0.0 on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _deal_revenue(deal: dict) -> float:
    """Closed-won revenue for a deal — prefer amount, fall back to ACV/ARR."""
    for key in ("amount", "hs_acv", "hs_arr"):
        amount = _safe_float(deal.get(key))
        if amount:
            return amount
    return 0.0


def _spend_value(row: dict) -> float:
    """Spend for a Google Ads performance row (spend or cost)."""
    value = row.get("spend")
    if value is None:
        value = row.get("cost")
    return _safe_float(value)


def compute_roas(won_revenue: float, spend: float):
    """ROAS = won_revenue / spend. None when spend is zero (never Infinity)."""
    if not spend or spend <= 0:
        return None
    return round(won_revenue / spend, 2)


def compute_cac(spend: float, customers: int):
    """CAC = spend / customers. None when customers is zero (never Infinity)."""
    if not customers or customers <= 0:
        return None
    return round(spend / customers, 2)


def confidence_from_tiers(tiers: list) -> str:
    """Reduce a list of attribution tiers to a confidence label.

    Returns high | medium | low. Rows with no attributed deals are treated as
    low-confidence (inferred match).
    """
    if not tiers:
        return "low"
    counts: dict[str, int] = {}
    for tier in tiers:
        counts[tier] = counts.get(tier, 0) + 1
    dominant = max(counts, key=counts.get)
    return get_confidence_severity(dominant)


def classify_verdict(
    spend: float,
    sqls: int,
    customers: int,
    won_revenue: float,
    roas,
    *,
    min_spend: float = MIN_SPEND_FOR_VERDICT,
    healthy_roas: float = HEALTHY_ROAS,
) -> str:
    """Classify a row into a simple business verdict.

    winner   = revenue/customers exist and ROAS is healthy
    watch    = SQLs (or some revenue) exist but customers/revenue/ROAS are weak
    waste    = spend exists but no SQLs/customers/revenue
    learning = low spend or insufficient data
    """
    spend = spend or 0
    sqls = sqls or 0
    customers = customers or 0
    won_revenue = won_revenue or 0
    has_revenue = customers > 0 or won_revenue > 0

    if spend < min_spend and not has_revenue:
        return "learning"
    if has_revenue and roas is not None and roas >= healthy_roas:
        return "winner"
    if sqls > 0 or has_revenue:
        return "watch"
    if spend > 0:
        return "waste"
    return "learning"


# ── Aggregation ─────────────────────────────────────────────────────────────


def _new_bucket(display_name: str) -> dict:
    return {
        "display": display_name,
        "spend": 0.0,
        "leads": 0,
        "sqls": 0,
        "customers": 0,
        "won_revenue": 0.0,
        "tiers": [],
        "campaign_id": None,
    }


def _row_notes(bucket: dict, confidence: str) -> list:
    """Build attribution notes for a finished row."""
    notes = []
    if confidence == "low":
        notes.append("Low-confidence attribution — campaign/country match inferred.")
    if bucket["customers"] > 0:
        notes.append(
            "Customers = distinct closed-won deal count "
            "(company association not modelled in current data)."
        )
    if bucket["spend"] > 0 and bucket["won_revenue"] == 0:
        notes.append(
            "Spend present but no HubSpot closed-won revenue attributed in this window."
        )
    if bucket["sqls"] > 0 and bucket["customers"] == 0:
        notes.append("SQLs present but not yet converted to closed-won customers.")
    return notes


def _finalize_row(bucket: dict) -> dict:
    spend = round(bucket["spend"], 2)
    won_revenue = round(bucket["won_revenue"], 2)
    customers = bucket["customers"]
    roas = compute_roas(won_revenue, spend)
    cac = compute_cac(spend, customers)
    confidence = confidence_from_tiers(bucket["tiers"])
    verdict = classify_verdict(
        spend, bucket["sqls"], customers, won_revenue, roas
    )
    return {
        "spend": spend,
        "leads": bucket["leads"],
        "sqls": bucket["sqls"],
        "customers": customers,
        "won_revenue": won_revenue,
        "roas": roas,
        "cac": cac,
        "confidence": confidence,
        "verdict": verdict,
        "attribution_notes": _row_notes(bucket, confidence),
    }


def _build_campaign_rows(deals: list, spend_rows: list, contacts: list) -> list:
    buckets: dict[str, dict] = {}

    def bucket_for(name, display=None):
        key = _norm(name)
        if key not in buckets:
            buckets[key] = _new_bucket(display or name or "unknown")
        elif display and buckets[key]["display"] in (None, "", "unknown"):
            buckets[key]["display"] = display
        return buckets[key]

    for row in spend_rows:
        b = bucket_for(row.get("campaign"), row.get("campaign"))
        b["spend"] += _spend_value(row)
        if not b["campaign_id"] and row.get("campaign_id"):
            b["campaign_id"] = str(row.get("campaign_id"))

    for contact in contacts:
        props = contact.get("properties", {}) if isinstance(contact, dict) else {}
        b = bucket_for(props.get("hs_analytics_source_data_1"))
        b["leads"] += 1
        if props.get("mql_status") in QUALIFIED:
            b["sqls"] += 1

    for deal in deals:
        b = bucket_for(deal.get("campaign"))
        b["customers"] += 1
        b["won_revenue"] += _deal_revenue(deal)
        tier = deal.get("attribution_confidence")
        if tier:
            b["tiers"].append(tier)

    rows = []
    for key, bucket in buckets.items():
        if not key:
            bucket["display"] = bucket["display"] or "unknown"
        finalized = _finalize_row(bucket)
        finalized["campaign_id"] = bucket["campaign_id"] or (key or "unknown")
        finalized["campaign_name"] = bucket["display"] or "unknown"
        rows.append(finalized)

    rows.sort(key=lambda r: (r["spend"], r["won_revenue"]), reverse=True)
    return rows


def _top_campaign_for_country(country_key: str, deals: list, spend_rows: list) -> str | None:
    """Best campaign in a country by won revenue, falling back to spend."""
    by_revenue: dict[str, dict] = {}
    for deal in deals:
        if _norm(deal.get("country")) != country_key:
            continue
        name = deal.get("campaign") or "unknown"
        entry = by_revenue.setdefault(name, {"name": name, "revenue": 0.0})
        entry["revenue"] += _deal_revenue(deal)
    if by_revenue:
        best = max(by_revenue.values(), key=lambda e: e["revenue"])
        if best["revenue"] > 0:
            return best["name"]

    by_spend: dict[str, dict] = {}
    for row in spend_rows:
        if _norm(row.get("country")) != country_key:
            continue
        name = row.get("campaign") or "unknown"
        entry = by_spend.setdefault(name, {"name": name, "spend": 0.0})
        entry["spend"] += _spend_value(row)
    if by_spend:
        best = max(by_spend.values(), key=lambda e: e["spend"])
        if best["spend"] > 0:
            return best["name"]

    return None


def _build_country_rows(deals: list, spend_rows: list, contacts: list) -> list:
    buckets: dict[str, dict] = {}

    def bucket_for(name, display=None):
        key = _norm(name)
        if key not in buckets:
            buckets[key] = _new_bucket(display or name or "unknown")
        elif display and buckets[key]["display"] in (None, "", "unknown"):
            buckets[key]["display"] = display
        return buckets[key]

    for row in spend_rows:
        b = bucket_for(row.get("country"), row.get("country"))
        b["spend"] += _spend_value(row)

    for contact in contacts:
        props = contact.get("properties", {}) if isinstance(contact, dict) else {}
        b = bucket_for(props.get("ip_country"))
        b["leads"] += 1
        if props.get("mql_status") in QUALIFIED:
            b["sqls"] += 1

    for deal in deals:
        b = bucket_for(deal.get("country"))
        b["customers"] += 1
        b["won_revenue"] += _deal_revenue(deal)
        tier = deal.get("attribution_confidence")
        if tier:
            b["tiers"].append(tier)

    rows = []
    for key, bucket in buckets.items():
        finalized = _finalize_row(bucket)
        display = bucket["display"] or "unknown"
        finalized["country"] = display
        finalized["country_code"] = get_country_code(display)
        finalized["top_campaign"] = _top_campaign_for_country(key, deals, spend_rows)
        rows.append(finalized)

    rows.sort(key=lambda r: (r["spend"], r["won_revenue"]), reverse=True)
    return rows


def _build_summary(deals: list, spend_rows: list, contacts: list) -> dict:
    spend = round(sum(_spend_value(r) for r in spend_rows), 2)
    leads = len(contacts)
    sqls = sum(
        1
        for c in contacts
        if (c.get("properties", {}) if isinstance(c, dict) else {}).get("mql_status")
        in QUALIFIED
    )
    customers = len(deals)
    won_revenue = round(sum(_deal_revenue(d) for d in deals), 2)
    roas = compute_roas(won_revenue, spend)
    cac = compute_cac(spend, customers)
    tiers = [d.get("attribution_confidence") for d in deals if d.get("attribution_confidence")]
    confidence = confidence_from_tiers(tiers)

    return {
        "spend": spend,
        "leads": leads,
        "sqls": sqls,
        "customers": customers,
        "won_revenue": won_revenue,
        "roas": roas,
        "cac": cac,
        "confidence": confidence,
    }


def build_revenue_attribution(window: str, now: datetime | None = None) -> dict:
    """Build the shared revenue-attribution contract for a business window.

    Args:
        window: A business-window key (see analysis.business_windows.WINDOW_KEYS).
        now: Optional reference time for deterministic resolution/testing.

    Returns:
        Dict with window, summary, campaigns, countries, and source metadata.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolved = resolve_window(window, now=now)
    start_dt, end_dt = get_window_bounds(window, now=now)

    deals = _load_attributed_deals()
    spend_rows = _load_spend_rows()
    contacts = _load_contacts()

    deals_w = [
        d for d in deals if _in_window(_parse_dt(d.get("closedate")), start_dt, end_dt)
    ]
    spend_w = [
        r
        for r in spend_rows
        if _in_window(_parse_dt(r.get("date") or r.get("source_date")), start_dt, end_dt)
    ]
    contacts_w = [
        c
        for c in contacts
        if _in_window(
            _parse_dt((c.get("properties", {}) if isinstance(c, dict) else {}).get("createdate")),
            start_dt,
            end_dt,
        )
    ]

    summary = _build_summary(deals_w, spend_w, contacts_w)
    campaigns = _build_campaign_rows(deals_w, spend_w, contacts_w)
    countries = _build_country_rows(deals_w, spend_w, contacts_w)

    return {
        "window": resolved,
        "summary": summary,
        "campaigns": campaigns,
        "countries": countries,
        "source_truth": "hubspot_closed_won_revenue",
        "spend_source": "google_ads_api",
        "google_ads_conversion_value_used": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
