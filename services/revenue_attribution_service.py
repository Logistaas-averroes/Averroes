"""
Revenue Attribution Service (PR-ADS-107A)

Shared, read-only data contract that powers both the ROAS by Campaign and the
ROAS by Country pages from a single truth source. It resolves a business window
(see analysis/business_windows.py) and aggregates revenue-attribution metrics
per campaign and per country.

Data doctrine:
  - HubSpot closed-won deals are revenue truth.
  - Google Ads API spend is platform/spend evidence.
  - Durable sources first (PR-ADS-108): the endpoint reads persisted PostgreSQL
    tables populated by the scheduler, NOT ephemeral local JSON files that only
    exist as a side effect of the weekly run on the box that ran it:
        geo                -> campaign + country spend (per-day, real country)
        leads              -> leads + SQLs (status_category)
        gclid_attribution  -> closed-won revenue/customers (by deal_close_date)
    Local JSON (data/ads_campaigns.json, etc.) is a DIAGNOSTIC FALLBACK only,
    used when the database is unavailable, and is clearly labeled as such.
    When neither a durable DB source nor a local file exists, the response says
    "source_unavailable" with the exact missing dependency — it never shows a
    silently-empty dashboard.
  - Google Ads conversion value is NOT used as revenue truth.
  - Attribution uncertainty is surfaced, never hidden (High / Medium / Low).
    DB GCLID attribution (gclid_attribution.match_status) is durable and is not
    downgraded. GCLID attribution derived from the legacy local
    campaign_performance.json index (fallback path only) is downgraded and
    labeled.

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
  - No legacy/pre-cutover spend source used as primary.
  - No frontend formatting.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from analysis.attribution_confidence import get_confidence_severity
from analysis.attribution_matcher import attribute_deals
from analysis.business_windows import get_window_bounds, resolve_window
from analysis.core import QUALIFIED
from db import revenue_repository as repo
from services.country_codes import get_country_code

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"

# Active Google Ads API scheduler-cutover output files (PR-ADS-104).
ADS_CAMPAIGNS_FILE = DATA_DIR / "ads_campaigns.json"
ADS_GEOS_FILE = DATA_DIR / "ads_geos.json"

# Legacy pre-cutover spend file — explicit fallback only, never primary.
LEGACY_SPEND_FILE = DATA_DIR / "campaign_performance.json"

# HubSpot-side inputs.
CONTACTS_FILE = DATA_DIR / "crm_contacts.json"
WON_DEALS_FILE = DATA_DIR / "hubspot_won_deals.json"

# Verdict thresholds. Kept deliberately simple — see classify_verdict.
HEALTHY_ROAS = 1.0            # won_revenue >= spend is considered healthy
MIN_SPEND_FOR_VERDICT = 50.0  # below this (and no revenue) we are still learning


def _rel(path: Path) -> str:
    """Render a data path as a stable 'data/<name>' string for diagnostics."""
    return f"data/{path.name}"


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


def _load_campaign_spend() -> dict:
    """Campaign spend from the active Google Ads API file, with legacy fallback.

    Returns a dict: {"rows": [...], "status": "...", "file": "data/..." | None}

    status:
        google_ads_api  — data/ads_campaigns.json (active scheduler output)
        legacy_fallback — data/campaign_performance.json (pre-cutover; flagged)
        no_spend_data   — neither file present / non-empty
    """
    rows = _load_json(ADS_CAMPAIGNS_FILE)
    if rows:
        return {"rows": rows, "status": "google_ads_api", "file": _rel(ADS_CAMPAIGNS_FILE)}
    legacy = _load_json(LEGACY_SPEND_FILE)
    if legacy:
        return {"rows": legacy, "status": "legacy_fallback", "file": _rel(LEGACY_SPEND_FILE)}
    return {"rows": [], "status": "no_spend_data", "file": None}


def _load_geo_spend() -> dict:
    """Geo spend from the active Google Ads API file (data/ads_geos.json).

    No legacy fallback: the pre-cutover country attribution is exactly what the
    cutover replaces. Geo rows currently carry country=None (criterion-ID -> name
    mapping is not implemented), so geo spend is generally NOT country-resolved.

    Returns: {"rows": [...], "file": "data/ads_geos.json" | None}
    """
    rows = _load_json(ADS_GEOS_FILE)
    return {"rows": rows, "file": _rel(ADS_GEOS_FILE) if rows else None}


def _load_contacts() -> list:
    """HubSpot paid-search contacts used for lead / SQL counts."""
    return _load_json(CONTACTS_FILE)


def _attribution_source_status() -> str:
    """Describe where the deal attribution / GCLID index comes from.

    attribute_deals() builds its GCLID->campaign index from the legacy
    data/campaign_performance.json. When that file is present, GCLID-derived
    confidence is legacy and must be downgraded (never claimed as high).
    """
    if LEGACY_SPEND_FILE.exists():
        return "legacy_gclid_index"
    return "hubspot_source_tags_only"


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


def _row_date(row: dict):
    """Date value for a spend row (active uses 'date'; legacy may use source_date)."""
    return row.get("date") or row.get("source_date")


def compute_roas(won_revenue: float, spend: float):
    """ROAS = won_revenue / spend. None when spend is zero (never Infinity)."""
    if not spend or spend <= 0:
        return None
    return round(won_revenue / spend, 2)


def compute_cac(spend: float, customers: int):
    """CAC = spend / customers.

    None when customers is zero OR spend is zero/unavailable — a $0 CAC would
    falsely imply free acquisition (e.g. countries where Google Ads spend is not
    country-resolved). Never returns Infinity.
    """
    if not spend or spend <= 0:
        return None
    if not customers or customers <= 0:
        return None
    return round(spend / customers, 2)


def confidence_from_tiers(tiers: list) -> str:
    """Reduce a list of attribution tiers to a confidence label.

    Returns high | medium | low. Rows with no attributed deals are treated as
    low-confidence (inferred match). Callers apply the legacy-GCLID downgrade.
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
    """Build base attribution notes for a finished row."""
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


def _finalize_row(bucket: dict, *, legacy_gclid: bool = False, extra_notes: list | None = None) -> dict:
    spend = round(bucket["spend"], 2)
    won_revenue = round(bucket["won_revenue"], 2)
    customers = bucket["customers"]
    roas = compute_roas(won_revenue, spend)
    cac = compute_cac(spend, customers)

    raw_confidence = confidence_from_tiers(bucket["tiers"])
    downgraded = legacy_gclid and raw_confidence == "high"
    confidence = "medium" if downgraded else raw_confidence

    verdict = classify_verdict(spend, bucket["sqls"], customers, won_revenue, roas)

    notes = _row_notes(bucket, confidence)
    if downgraded:
        notes.append(
            "GCLID attribution index is legacy (data/campaign_performance.json); "
            "confidence downgraded from high to medium."
        )
    if extra_notes:
        notes.extend(extra_notes)

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
        "attribution_notes": notes,
    }


def _build_campaign_rows(deals: list, spend_rows: list, contacts: list, *, legacy_gclid: bool) -> list:
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
        finalized = _finalize_row(bucket, legacy_gclid=legacy_gclid)
        finalized["campaign_id"] = bucket["campaign_id"] or (key or "unknown")
        finalized["campaign_name"] = bucket["display"] or "unknown"
        rows.append(finalized)

    rows.sort(key=lambda r: (r["spend"], r["won_revenue"]), reverse=True)
    return rows


def _top_campaign_for_country(country_key: str, deals: list) -> str | None:
    """Best campaign in a country by won revenue.

    Derived from HubSpot deals only: Google Ads geo rows are not country-resolved
    (country=None), so geo spend cannot be linked to a named country/campaign.
    """
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
    return None


def _build_country_rows(
    deals: list,
    geo_rows: list,
    contacts: list,
    *,
    legacy_gclid: bool,
    country_spend_available: bool,
) -> list:
    buckets: dict[str, dict] = {}

    def bucket_for(name, display=None):
        key = _norm(name)
        if key not in buckets:
            buckets[key] = _new_bucket(display or name or "unknown")
        elif display and buckets[key]["display"] in (None, "", "unknown"):
            buckets[key]["display"] = display
        return buckets[key]

    # Geo spend is only attributed to a country when the geo row is actually
    # country-resolved (row["country"] is not None). Unmapped geo spend is NEVER
    # merged into a named country bucket.
    for row in geo_rows:
        country = row.get("country")
        if not country:
            continue
        b = bucket_for(country, country)
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
        extra_notes = []
        if not country_spend_available:
            extra_notes.append(
                "Country-level Google Ads spend is not available: geo rows are not "
                "country-resolved (geo country mapping not implemented). Spend shown "
                "is 0; leads/SQLs/customers/revenue are HubSpot contact/deal-side."
            )
        finalized = _finalize_row(bucket, legacy_gclid=legacy_gclid, extra_notes=extra_notes)
        display = bucket["display"] or "unknown"
        finalized["country"] = display
        finalized["country_code"] = get_country_code(display)
        finalized["top_campaign"] = _top_campaign_for_country(key, deals)
        rows.append(finalized)

    rows.sort(key=lambda r: (r["spend"], r["won_revenue"]), reverse=True)
    return rows


def _build_summary(deals: list, spend_rows: list, contacts: list, *, legacy_gclid: bool) -> dict:
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
    raw_confidence = confidence_from_tiers(tiers)
    confidence = "medium" if (legacy_gclid and raw_confidence == "high") else raw_confidence

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


# ── Durable DB path (PR-ADS-108) ─────────────────────────────────────────────


def _match_status_to_tier(match_status) -> str:
    """Map a gclid_attribution.match_status to an attribution tier."""
    ms = (match_status or "").strip().lower()
    if ms == "matched":
        return "tier_1_gclid"
    if ms == "url_fallback":
        return "tier_2_source_tag"
    return "tier_3_spend_weighted"


def _build_db_rows(spend_rows: list, lead_rows: list, revenue_rows: list, *, group_field: str):
    """Aggregate durable DB rows into finished campaign or country rows.

    group_field is "campaign_name" or "country". Returns (rows, country_spend_available).
    """
    buckets: dict[str, dict] = {}

    def bucket_for(name):
        key = _norm(name)
        if key not in buckets:
            buckets[key] = _new_bucket(name or "unknown")
        return key, buckets[key]

    # spend rows: {campaign_name, country, spend}
    for row in spend_rows:
        _, b = bucket_for(row.get(group_field))
        b["spend"] += _safe_float(row.get("spend"))

    # lead rows: {campaign_name, country, status_category, has_gclid}
    for row in lead_rows:
        _, b = bucket_for(row.get(group_field))
        b["leads"] += 1
        if row.get("status_category") == "qualified":
            b["sqls"] += 1

    # revenue rows: {campaign_name, country, deal_id, deal_amount_usd, match_status}
    for row in revenue_rows:
        _, b = bucket_for(row.get(group_field))
        b["customers"] += 1
        b["won_revenue"] += _safe_float(row.get("deal_amount_usd"))
        b["tiers"].append(_match_status_to_tier(row.get("match_status")))

    rows = []
    for key, bucket in buckets.items():
        finalized = _finalize_row(bucket, legacy_gclid=False)
        if group_field == "campaign_name":
            finalized["campaign_id"] = key or "unknown"
            finalized["campaign_name"] = bucket["display"] or "unknown"
        else:
            display = bucket["display"] or "unknown"
            finalized["country"] = display
            finalized["country_code"] = get_country_code(display)
            finalized["top_campaign"] = _db_top_campaign(key, revenue_rows, spend_rows)
        rows.append(finalized)

    rows.sort(key=lambda r: (r["spend"], r["won_revenue"]), reverse=True)
    return rows


def _db_top_campaign(country_key: str, revenue_rows: list, spend_rows: list) -> str | None:
    """Top campaign in a country by won revenue, falling back to spend (DB rows)."""
    by_rev: dict[str, float] = {}
    for r in revenue_rows:
        if _norm(r.get("country")) == country_key:
            name = r.get("campaign_name") or "unknown"
            by_rev[name] = by_rev.get(name, 0.0) + _safe_float(r.get("deal_amount_usd"))
    if by_rev:
        best = max(by_rev, key=by_rev.get)
        if by_rev[best] > 0:
            return best
    by_spend: dict[str, float] = {}
    for r in spend_rows:
        if _norm(r.get("country")) == country_key:
            name = r.get("campaign_name") or "unknown"
            by_spend[name] = by_spend.get(name, 0.0) + _safe_float(r.get("spend"))
    if by_spend:
        best = max(by_spend, key=by_spend.get)
        if by_spend[best] > 0:
            return best
    return None


def _build_db_summary(spend_rows: list, lead_rows: list, revenue_rows: list) -> dict:
    spend = round(sum(_safe_float(r.get("spend")) for r in spend_rows), 2)
    leads = len(lead_rows)
    sqls = sum(1 for r in lead_rows if r.get("status_category") == "qualified")
    customers = len(revenue_rows)
    won_revenue = round(sum(_safe_float(r.get("deal_amount_usd")) for r in revenue_rows), 2)
    tiers = [_match_status_to_tier(r.get("match_status")) for r in revenue_rows]
    return {
        "spend": spend,
        "leads": leads,
        "sqls": sqls,
        "customers": customers,
        "won_revenue": won_revenue,
        "roas": compute_roas(won_revenue, spend),
        "cac": compute_cac(spend, customers),
        "confidence": confidence_from_tiers(tiers),
    }


def _window_date_bounds(window: str, now: datetime | None):
    """Return (start_date, end_date) as date objects (start may be None)."""
    resolved = resolve_window(window, now=now)
    start_date = date.fromisoformat(resolved["start_date"]) if resolved["start_date"] else None
    end_date = date.fromisoformat(resolved["end_date"])
    return resolved, start_date, end_date


def _build_from_db(resolved, start_date, end_date) -> dict | None:
    """Build the contract from durable DB sources.

    Returns the full contract dict, or None when the database is unavailable
    (so the caller can fall back to the local-JSON diagnostic path).
    """
    spend = repo.fetch_campaign_country_spend(start_date, end_date)
    if not spend["available"]:
        return None  # DB unreachable — signal caller to fall back

    leads = repo.fetch_lead_quality(start_date, end_date)
    revenue = repo.fetch_won_revenue(start_date, end_date)
    sync = repo.fetch_sync_state()

    spend_rows = spend["rows"]
    lead_rows = leads["rows"]
    revenue_rows = revenue["rows"]

    named_spend = [r for r in spend_rows if r.get("country")]
    country_spend_available = bool(named_spend)

    campaigns = _build_db_rows(spend_rows, lead_rows, revenue_rows, group_field="campaign_name")
    countries = _build_db_rows(spend_rows, lead_rows, revenue_rows, group_field="country")
    summary = _build_db_summary(spend_rows, lead_rows, revenue_rows)

    campaign_spend_status = "db" if spend_rows else "db_empty"
    contacts_status = "db" if lead_rows else "db_empty"
    deals_status = "db" if revenue_rows else "db_empty"
    if revenue_rows:
        attribution_status = "gclid_attribution_db"
    elif lead_rows:
        attribution_status = "hubspot_source_tags_only"
    else:
        attribution_status = "none"

    # Partial when spend coverage does not reach back to the window start.
    data_is_partial = False
    cov_start = spend.get("coverage_start")
    if start_date is not None:
        if cov_start is None or cov_start > resolved["start_date"]:
            data_is_partial = True

    warnings: list[str] = []
    if campaign_spend_status == "db_empty":
        warnings.append(
            "No Google Ads spend rows in durable table 'geo' for this window."
        )
    if deals_status == "db_empty":
        warnings.append(
            "No closed-won revenue in durable table 'gclid_attribution' for this window."
        )
    if data_is_partial:
        warnings.append(
            f"Partial data: durable spend coverage starts {cov_start or 'unknown'}, "
            f"after the requested window start {resolved['start_date']}."
        )
    if not country_spend_available and spend_rows:
        warnings.append(
            "Country-level spend rows are present but missing country names."
        )

    db_tables_used = [t for t, present in (
        ("geo", bool(spend_rows)),
        ("leads", bool(lead_rows)),
        ("gclid_attribution", bool(revenue_rows)),
    ) if present]

    source_health = {
        "mode": "database",
        "campaign_spend_status": campaign_spend_status,
        "hubspot_contacts_status": contacts_status,
        "hubspot_deals_status": deals_status,
        "attribution_status": attribution_status,
        "data_is_partial": data_is_partial,
        "coverage_start": spend.get("coverage_start") or revenue.get("coverage_start"),
        "coverage_end": spend.get("coverage_end") or revenue.get("coverage_end"),
        "files_used": {},
        "db_tables_used": db_tables_used,
        "sync_state": sync.get("datasets", {}),
        "warnings": warnings,
    }

    return {
        "window": resolved,
        "summary": summary,
        "campaigns": campaigns,
        "countries": countries,
        "spend_source": "google_ads_api",
        "revenue_source": "hubspot_closed_won",
        "source_truth": "hubspot_closed_won_revenue",
        "google_ads_conversion_value_used": False,
        "campaign_spend_source_status": campaign_spend_status,
        "attribution_source_status": attribution_status,
        "country_spend_available": country_spend_available,
        "geo_country_mapping_status": "available" if country_spend_available else (
            "no_geo_data" if not spend_rows else "partial"
        ),
        "data_is_partial": data_is_partial,
        "source_health": source_health,
        "files_used": {},
        "db_tables_used": db_tables_used,
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Local-JSON diagnostic fallback (used only when the DB is unavailable) ────


def _build_from_json(resolved, start_dt, end_dt) -> dict:
    """Diagnostic fallback: build the contract from local JSON files.

    Used only when the durable database is unavailable. Clearly labeled so the
    UI never implies a production/durable source it does not have.
    """
    deals = _load_attributed_deals()
    contacts = _load_contacts()
    campaign_spend = _load_campaign_spend()
    geo_spend = _load_geo_spend()
    attribution_status_json = _attribution_source_status()
    legacy_gclid = attribution_status_json == "legacy_gclid_index"

    deals_w = [d for d in deals if _in_window(_parse_dt(d.get("closedate")), start_dt, end_dt)]
    contacts_w = [
        c for c in contacts
        if _in_window(
            _parse_dt((c.get("properties", {}) if isinstance(c, dict) else {}).get("createdate")),
            start_dt, end_dt,
        )
    ]
    campaign_spend_w = [
        r for r in campaign_spend["rows"] if _in_window(_parse_dt(_row_date(r)), start_dt, end_dt)
    ]
    geo_spend_w = [
        r for r in geo_spend["rows"] if _in_window(_parse_dt(_row_date(r)), start_dt, end_dt)
    ]

    named_geo = [r for r in geo_spend_w if r.get("country")]
    country_spend_available = bool(named_geo)
    if not geo_spend_w:
        geo_country_mapping_status = "no_geo_data"
    elif len(named_geo) == len(geo_spend_w):
        geo_country_mapping_status = "available"
    elif named_geo:
        geo_country_mapping_status = "partial"
    else:
        geo_country_mapping_status = "not_implemented"

    summary = _build_summary(deals_w, campaign_spend_w, contacts_w, legacy_gclid=legacy_gclid)
    campaigns = _build_campaign_rows(deals_w, campaign_spend_w, contacts_w, legacy_gclid=legacy_gclid)
    countries = _build_country_rows(
        deals_w, geo_spend_w, contacts_w,
        legacy_gclid=legacy_gclid, country_spend_available=country_spend_available,
    )

    has_any_local = bool(campaign_spend["rows"] or geo_spend["rows"] or deals or contacts)

    if campaign_spend["rows"]:
        campaign_spend_status = "local_json_fallback"
    else:
        campaign_spend_status = "source_unavailable"

    warnings: list[str] = [
        "Database unavailable — served from local JSON diagnostic fallback, "
        "not a durable production source.",
    ]
    if campaign_spend_status == "source_unavailable":
        warnings.append(
            "No durable campaign spend source: DB table 'geo' unreachable and "
            "data/ads_campaigns.json missing or empty."
        )
    if legacy_gclid:
        warnings.append(
            "GCLID attribution index is sourced from legacy local "
            "data/campaign_performance.json; high-confidence claims downgraded to medium."
        )

    files_used = {
        "campaign_spend": campaign_spend["file"],
        "geo_spend": geo_spend["file"],
        "revenue": _rel(WON_DEALS_FILE) if deals else None,
        "contacts": _rel(CONTACTS_FILE) if contacts else None,
    }

    source_health = {
        "mode": "local_json_fallback" if has_any_local else "source_unavailable",
        "campaign_spend_status": campaign_spend_status,
        "hubspot_contacts_status": "local_json_fallback" if contacts else "source_unavailable",
        "hubspot_deals_status": "local_json_fallback" if deals else "source_unavailable",
        "attribution_status": attribution_status_json,
        "data_is_partial": True,
        "coverage_start": None,
        "coverage_end": None,
        "files_used": files_used,
        "db_tables_used": [],
        "sync_state": {},
        "warnings": warnings,
    }

    return {
        "window": resolved,
        "summary": summary,
        "campaigns": campaigns,
        "countries": countries,
        "spend_source": "google_ads_api",
        "revenue_source": "hubspot_closed_won",
        "source_truth": "hubspot_closed_won_revenue",
        "google_ads_conversion_value_used": False,
        "campaign_spend_source_status": campaign_spend_status,
        "attribution_source_status": attribution_status_json,
        "country_spend_available": country_spend_available,
        "geo_country_mapping_status": geo_country_mapping_status,
        "data_is_partial": True,
        "source_health": source_health,
        "files_used": files_used,
        "db_tables_used": [],
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def build_revenue_attribution(window: str, now: datetime | None = None) -> dict:
    """Build the shared revenue-attribution contract for a business window.

    Durable-source strategy (PR-ADS-108):
      1. Read from persisted PostgreSQL tables (geo / leads / gclid_attribution).
      2. If the database is unavailable, fall back to local JSON files, clearly
         labeled as a diagnostic fallback (source_health.mode).
      3. If neither a durable DB source nor a local file exists, the response
         reports "source_unavailable" with the exact missing dependency.

    The response always includes a source_health block so the UI can never imply
    a source/freshness it does not have.

    Args:
        window: A business-window key (see analysis.business_windows.WINDOW_KEYS).
        now: Optional reference time for deterministic resolution/testing.

    Returns:
        Dict with window, summary, campaigns, countries, and source_health.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolved, start_date, end_date = _window_date_bounds(window, now)

    db_result = _build_from_db(resolved, start_date, end_date)
    if db_result is not None:
        return db_result

    # Database unavailable — diagnostic fallback to local JSON.
    start_dt, end_dt = get_window_bounds(window, now=now)
    return _build_from_json(resolved, start_dt, end_dt)
