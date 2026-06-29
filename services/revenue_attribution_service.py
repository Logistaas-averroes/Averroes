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


def _finalize_row(
    bucket: dict,
    *,
    legacy_gclid: bool = False,
    extra_notes: list | None = None,
    revenue_available: bool = True,
    lead_metrics_withheld: bool = False,
    spend_trusted: bool = True,
    spend_mapping_unavailable: bool = False,
    spend_state: str | None = None,
) -> dict:
    # PR-ADS-118: spend is a usable ROAS denominator only when it comes from a
    # complete canonical source AND this row maps to a verified spend campaign.
    #   - spend_trusted=False  → canonical coverage incomplete / geo-fallback:
    #     spend is diagnostic only, ROAS is unavailable (never from a partial
    #     or geo denominator).
    #   - spend_mapping_unavailable=True → a revenue/lead campaign with no
    #     canonical spend match: spend itself is Unavailable (never a fake $0).
    won_revenue = round(bucket["won_revenue"], 2)
    customers = bucket["customers"]
    if spend_mapping_unavailable:
        spend = None
        roas = None
    else:
        spend = round(bucket["spend"], 2)
        # ROAS is null (not 0) when the revenue source is not wired — a 0 would
        # falsely imply "spent and earned nothing" when we simply have no truth.
        roas = compute_roas(won_revenue, spend) if (revenue_available and spend_trusted) else None
    cac = compute_cac(spend or 0, customers)

    raw_confidence = confidence_from_tiers(bucket["tiers"])
    downgraded = legacy_gclid and raw_confidence == "high"
    confidence = "medium" if downgraded else raw_confidence

    leads_val = None if lead_metrics_withheld else bucket["leads"]
    sqls_val = None if lead_metrics_withheld else bucket["sqls"]
    sqls_for_verdict = 0 if lead_metrics_withheld else bucket["sqls"]

    has_revenue = customers > 0 or won_revenue > 0
    verdict = classify_verdict(spend, sqls_for_verdict, customers, won_revenue, roas)
    if lead_metrics_withheld and not has_revenue:
        # Without trusted lead/SQL signal we cannot call a spend-only row "waste".
        verdict = "learning"
    # PR-ADS-120: an unmapped campaign has incomplete spend truth — it is never
    # classed winner/waste/watch; the decision is "mapping_required".
    if spend_mapping_unavailable or spend_state == "unmapped":
        verdict = "mapping_required"

    notes = _row_notes(bucket, confidence)
    if lead_metrics_withheld:
        notes.append(
            "Lead/SQL metrics withheld — HubSpot contact_created_at (business event "
            "date) is not available, so this window is not lead-safe."
        )
    if not revenue_available:
        notes.append(
            "Revenue attribution not wired for this window — ROAS is unavailable, not zero."
        )
    if spend_mapping_unavailable:
        notes.append(
            "Spend mapping unavailable — this campaign has no matching canonical "
            "Google Ads spend campaign, so spend and ROAS are unavailable (never $0)."
        )
    elif not spend_trusted:
        notes.append(
            "Spend coverage incomplete — canonical Google Ads spend is not fully "
            "verified for this window, so ROAS is unavailable (not from a partial "
            "or geo denominator)."
        )
    if spend_state == "verified_zero_spend":
        notes.append(
            "Verified zero spend — this campaign is matched to canonical Google Ads "
            "spend and genuinely spent $0 in this window (a verified zero, not a "
            "missing mapping)."
        )
    if downgraded:
        notes.append(
            "GCLID attribution index is legacy (data/campaign_performance.json); "
            "confidence downgraded from high to medium."
        )
    if extra_notes:
        notes.extend(extra_notes)

    return {
        "spend": spend,
        "leads": leads_val,
        "sqls": sqls_val,
        "customers": customers,
        "won_revenue": won_revenue,
        "roas": roas,
        "cac": cac,
        "confidence": confidence,
        "verdict": verdict,
        "spend_mapping": "unavailable" if spend_mapping_unavailable else "matched",
        # PR-ADS-120: mapped_exact | mapped_manual | unmapped | verified_zero_spend.
        "spend_state": spend_state,
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


def _build_db_rows(spend_rows: list, lead_rows: list, revenue_rows: list, *, group_field: str,
                   revenue_available: bool = True, lead_metrics_withheld: bool = False,
                   spend_trusted: bool = True, spend_mapping_keys: set | None = None,
                   manual_target_keys: set | None = None, compute_spend_state: bool = False):
    """Aggregate durable DB rows into finished campaign or country rows.

    group_field is "campaign_name" or "country". Buckets with an empty/unknown
    key are dropped — the ROAS truth table never shows an "unknown" row.

    PR-ADS-118 spend-truth gating:
      - spend_trusted=False → spend is diagnostic only, ROAS unavailable.
      - spend_mapping_keys (campaign grouping only): the set of normalized
        campaign keys that have a canonical spend match. A revenue/lead campaign
        whose key is NOT in this set has its spend + ROAS marked unavailable
        (never a fake $0). Passed only for a resolved canonical window with spend
        rows; None disables per-row mapping checks.

    PR-ADS-120 spend semantics (campaign grouping, compute_spend_state=True):
      each row gets a ``spend_state`` of mapped_exact | mapped_manual | unmapped |
      verified_zero_spend. An unmapped row shows Unavailable (never $0) and is
      never classed winner/waste/watch (verdict = mapping_required).
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
    # (empty when lead metrics are withheld for an unsafe date grain)
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
        if not key:
            continue  # drop unknown / blank campaign or country
        mapping_unavailable = bool(spend_mapping_keys is not None and key not in spend_mapping_keys)
        # PR-ADS-120 spend state (campaign grouping only).
        spend_state = None
        if compute_spend_state and group_field == "campaign_name" and spend_mapping_keys is not None:
            if key not in spend_mapping_keys:
                spend_state = "unmapped"
            elif _safe_float(bucket["spend"]) > 0:
                spend_state = ("mapped_manual"
                               if (manual_target_keys and key in manual_target_keys)
                               else "mapped_exact")
            else:
                spend_state = "verified_zero_spend"
        finalized = _finalize_row(
            bucket, legacy_gclid=False,
            revenue_available=revenue_available,
            lead_metrics_withheld=lead_metrics_withheld,
            spend_trusted=spend_trusted,
            spend_mapping_unavailable=mapping_unavailable,
            spend_state=spend_state,
        )
        if group_field == "campaign_name":
            finalized["campaign_id"] = key
            finalized["campaign_name"] = bucket["display"] or "unknown"
        else:
            display = bucket["display"] or "unknown"
            finalized["country"] = display
            finalized["country_code"] = get_country_code(display)
            finalized["top_campaign"] = _db_top_campaign(key, revenue_rows, spend_rows)
        rows.append(finalized)

    # Spend may be None (unavailable mapping); sort those as 0 so ordering is
    # stable and revenue still breaks ties.
    rows.sort(key=lambda r: (r["spend"] or 0.0, r["won_revenue"]), reverse=True)
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


def _build_db_summary(spend_rows: list, lead_rows: list, revenue_rows: list, *,
                      revenue_available: bool = True, lead_metrics_withheld: bool = False,
                      spend_trusted: bool = True) -> dict:
    spend = round(sum(_safe_float(r.get("spend")) for r in spend_rows), 2)
    leads = None if lead_metrics_withheld else len(lead_rows)
    sqls = None if lead_metrics_withheld else sum(
        1 for r in lead_rows if r.get("status_category") == "qualified"
    )
    customers = len(revenue_rows)
    won_revenue = round(sum(_safe_float(r.get("deal_amount_usd")) for r in revenue_rows), 2)
    tiers = [_match_status_to_tier(r.get("match_status")) for r in revenue_rows]
    # PR-ADS-118: the summary ROAS is only trustworthy from a complete canonical
    # denominator; otherwise it is unavailable (never from a partial/geo total).
    return {
        "spend": spend,
        "leads": leads,
        "sqls": sqls,
        "customers": customers,
        "won_revenue": won_revenue,
        "roas": compute_roas(won_revenue, spend) if (revenue_available and spend_trusted) else None,
        "cac": compute_cac(spend, customers),
        "confidence": confidence_from_tiers(tiers),
    }


def _window_date_bounds(window: str, now: datetime | None):
    """Return (start_date, end_date) as date objects (start may be None)."""
    resolved = resolve_window(window, now=now)
    start_date = date.fromisoformat(resolved["start_date"]) if resolved["start_date"] else None
    end_date = date.fromisoformat(resolved["end_date"])
    return resolved, start_date, end_date


def _ident_norm(name) -> str:
    """Identity normalization (case / spaces / commas / hyphens) for matching."""
    from services.campaign_identity_service import normalize_campaign_name  # noqa: PLC0415
    return normalize_campaign_name(name)


def _build_resolution_map(canonical_rows: list, approved_mappings: list) -> dict:
    """Identity-normalized external label -> canonical Google Ads campaign name.

    PR-ADS-120: combines two sources, both applied to the ACTUAL ROAS aggregation:
      - exact-normalized auto-links from the canonical campaign set (case /
        spaces / commas / hyphens), and
      - APPROVED manual mappings (admin), which override exact auto-links.
    Only approved manual mappings (approved_at IS NOT NULL) are honoured; an
    unapproved label is never resolved (it stays "Spend mapping unavailable").
    """
    res: dict[str, str] = {}
    for c in canonical_rows or []:
        name = c.get("campaign_name")
        nz = _ident_norm(name)
        if nz and name:
            res[nz] = name  # exact-normalized auto-link target
    for m in approved_mappings or []:
        if not m.get("approved_at"):
            continue
        nz = _ident_norm(m.get("external_campaign_label"))
        canon = m.get("canonical_campaign_name")
        if nz and canon:
            res[nz] = canon  # explicit approved manual mapping overrides
    return res


def _dedupe_mappings(applied_log: list) -> list:
    """Unique applied-mapping records (external label -> canonical) for audit."""
    seen = set()
    out = []
    for m in applied_log:
        key = (m.get("external_campaign_label"), m.get("canonical_campaign_name"))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def _apply_identity_map(rows: list, mapping: dict, applied_log: list) -> list:
    """Rewrite external HubSpot/UTM campaign labels to their canonical Google Ads
    campaign name so they aggregate onto canonical spend. Preserves the original
    label in ``external_campaign_label`` and records each applied mapping for audit.
    """
    if not mapping:
        return rows
    out = []
    for r in rows:
        label = r.get("campaign_name")
        canon = mapping.get(_ident_norm(label))
        if canon and _norm(canon) != _norm(label):
            nr = dict(r)
            nr["campaign_name"] = canon
            nr["external_campaign_label"] = label  # preserve original truth
            out.append(nr)
            applied_log.append({"external_campaign_label": label, "canonical_campaign_name": canon})
        else:
            out.append(r)
    return out


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
    revenue_rows = revenue["rows"]

    # PR-ADS-118: Campaign ROAS spend truth comes ONLY from the canonical Google
    # Ads campaign-daily table, NEVER the geo table. When the canonical table is
    # reachable we always evaluate its coverage — even with zero rows, because a
    # COMPLETE coverage window with zero rows is verified zero spend, whereas a
    # window with missing/failed chunks is "incomplete" (never read as $0).
    #
    # Spend is a usable ROAS denominator only when canonical coverage is COMPLETE.
    # When the canonical table is unreachable we fall to geo, but geo is
    # DIAGNOSTIC ONLY and must never back a Campaign or Country ROAS.
    canonical = repo.fetch_canonical_campaign_spend(start_date, end_date)
    canonical_access = bool(canonical.get("available"))
    if canonical_access:
        from services.google_ads_spend_service import (  # noqa: PLC0415
            analyze_coverage as _analyze_cov, SPEND_VARIANCE_TOLERANCE,
        )
        coverage = repo.fetch_spend_coverage(start_date, end_date)
        _cov = _analyze_cov(start_date, end_date, coverage.get("chunks", []))
        coverage_complete = bool(_cov.get("complete"))
        spend_coverage_status = "complete" if coverage_complete else "incomplete"
        spend_source = "canonical_google_ads_api"
        canonical_rows = canonical.get("rows") or []
        canonical_total = round(float(canonical.get("total_spend") or 0), 2)
        canonical_customer_id = canonical.get("customer_id")
        canonical_currency = canonical.get("currency_code") or "GBP"
        # PR-ADS-119: HubSpot revenue is USD, so the ROAS denominator must be USD.
        # Convert via daily FX (already done in the repo). fx_complete is None when
        # the FX layer was not evaluated (legacy/patched canonical) — treat as
        # not-blocking and fall back to native spend for backward compatibility.
        fx_complete_raw = canonical.get("fx_complete")
        fx_evaluated = fx_complete_raw is not None
        fx_complete = bool(fx_complete_raw) if fx_evaluated else True
        fx_coverage_status = (
            ("complete" if fx_complete else "incomplete") if fx_evaluated else "not_evaluated"
        )
        usd_total = canonical.get("total_spend_usd")
        spend_native_total = canonical_total
        spend_usd_total = round(float(usd_total), 2) if usd_total is not None else None
        use_usd = fx_evaluated and fx_complete
        reporting_currency = canonical.get("reporting_currency") or "USD"
        # Effective realized rate (native→USD) from the per-day conversions; used
        # only to express the reconciled country (geo) split in USD.
        fx_effective_rate = (
            (spend_usd_total / spend_native_total)
            if (use_usd and spend_usd_total is not None and spend_native_total) else None
        )
        # Campaign spend rows in the ROAS currency (USD when FX complete; native
        # diagnostic otherwise).
        campaign_spend_rows = [
            {"campaign_name": r.get("campaign_name"), "country": None,
             "spend": (r.get("spend_usd") if use_usd else r.get("spend"))}
            for r in canonical_rows
        ]
        # Geo reconciliation for Country ROAS: geo total must match canonical for
        # the same window within tolerance (compared in native currency).
        geo_total_for_recon = round(sum(_safe_float(r.get("spend")) for r in spend_rows), 2)
        if canonical_total > 0:
            country_spend_reconciled = (
                abs(geo_total_for_recon - canonical_total) / canonical_total
                <= SPEND_VARIANCE_TOLERANCE
            )
        else:
            country_spend_reconciled = (geo_total_for_recon == 0)
    else:
        # Canonical table unreachable → geo is diagnostic only, NEVER a ROAS
        # denominator. ROAS is unavailable for both Campaign and Country.
        spend_source = "geo_fallback"
        spend_coverage_status = "geo_fallback"
        coverage_complete = False
        fx_complete = False
        fx_coverage_status = "unavailable"
        use_usd = False
        reporting_currency = "USD"
        fx_effective_rate = None
        spend_native_total = None
        spend_usd_total = None
        campaign_spend_rows = spend_rows  # diagnostic display only
        canonical_total = None
        canonical_customer_id = None
        canonical_currency = None
        country_spend_reconciled = None

    # PR-ADS-119: apply APPROVED campaign-identity mappings so external
    # HubSpot/UTM labels aggregate onto their canonical Google Ads campaign BEFORE
    # campaign rows and ROAS are computed. Only approved mappings are applied;
    # unapproved labels stay unmapped ("Spend mapping unavailable"). The original
    # external label is preserved in audit metadata.
    applied_identity_mappings: list = []
    approved_mappings = (repo.fetch_campaign_identity(canonical_customer_id).get("mappings", [])
                         if canonical_access else [])
    # PR-ADS-120b: "Not Google Ads" exclusions — labels an admin marked as not a
    # real Google Ads campaign (offline / import / bad UTM / sales / old CRM
    # label). Drop them from the Google Ads ROAS aggregation entirely so they
    # never surface as an unmapped row or a fabricated $0; they are reported
    # separately (excluded_campaign_labels) for audit.
    excluded_label_keys = {
        _ident_norm(m.get("external_campaign_label"))
        for m in approved_mappings
        if m.get("match_method") == "not_google_ads" and m.get("approved_at")
        and m.get("external_campaign_label")
    }
    excluded_campaign_labels = sorted({
        m.get("external_campaign_label")
        for m in approved_mappings
        if m.get("match_method") == "not_google_ads" and m.get("approved_at")
        and m.get("external_campaign_label")
    })
    if excluded_label_keys:
        revenue_rows = [r for r in revenue_rows
                        if _ident_norm(r.get("campaign_name")) not in excluded_label_keys]
    resolution_map = (_build_resolution_map(canonical.get("rows") or [], approved_mappings)
                      if canonical_access else {})
    if resolution_map:
        revenue_rows = _apply_identity_map(revenue_rows, resolution_map, applied_identity_mappings)

    # Campaign spend is a trusted ROAS denominator only when canonical coverage is
    # complete AND FX coverage is complete (USD reporting). Country spend
    # additionally requires geo↔canonical reconciliation.
    campaign_spend_trusted = canonical_access and coverage_complete and fx_complete
    country_spend_trusted = campaign_spend_trusted and (country_spend_reconciled is True)
    # Country spend split (geo, native) expressed in USD via the realized rate so
    # Country ROAS is also USD — never native-vs-USD mixed.
    if country_spend_trusted and fx_effective_rate is not None:
        country_spend_rows = [
            {**r, "spend": _safe_float(r.get("spend")) * fx_effective_rate} for r in spend_rows
        ]
    else:
        country_spend_rows = spend_rows
    # PR-ADS-120: campaign-identity resolution is determined by canonical COVERAGE
    # completeness (we know the full campaign set), independent of FX. A revenue
    # campaign absent from the canonical spend set is "unmapped" → spend + ROAS
    # Unavailable (never a fake $0). FX only gates whether USD spend / ROAS show.
    spend_identity_resolved = canonical_access and coverage_complete
    spend_mapping_keys = None
    manual_target_keys: set = set()
    if spend_identity_resolved and campaign_spend_rows:
        spend_mapping_keys = {_norm(r.get("campaign_name")) for r in campaign_spend_rows}
        # Canonical campaigns reachable via an approved MANUAL mapping → labelled
        # mapped_manual; exact-normalized matches are mapped_exact.
        manual_target_keys = {
            _norm(m.get("canonical_campaign_name"))
            for m in approved_mappings
            if m.get("approved_at") and m.get("canonical_campaign_name")
        }

    # PR-ADS-109: lead metrics are trusted only when the business event date
    # (HubSpot contact_created_at) is available. Otherwise withhold them — never
    # compute SQLs from sync-date-contaminated rows.
    lead_event_date_safe = bool(leads.get("event_date_safe"))
    lead_metrics_withheld = not lead_event_date_safe
    lead_rows = [] if lead_metrics_withheld else leads["rows"]
    if excluded_label_keys and lead_rows:
        lead_rows = [r for r in lead_rows
                     if _ident_norm(r.get("campaign_name")) not in excluded_label_keys]
    if resolution_map and lead_rows:
        lead_rows = _apply_identity_map(lead_rows, resolution_map, applied_identity_mappings)

    # Revenue is "wired" only when there are closed-won attributed rows; without
    # them ROAS is null (not zero) and the status says so.
    revenue_available = bool(revenue_rows)

    named_spend = [r for r in spend_rows if r.get("country")]
    country_spend_available = bool(named_spend)

    campaigns = _build_db_rows(
        campaign_spend_rows, lead_rows, revenue_rows, group_field="campaign_name",
        revenue_available=revenue_available, lead_metrics_withheld=lead_metrics_withheld,
        spend_trusted=campaign_spend_trusted, spend_mapping_keys=spend_mapping_keys,
        manual_target_keys=manual_target_keys, compute_spend_state=spend_identity_resolved,
    )
    # PR-ADS-120b: multiple external labels can resolve to ONE canonical campaign.
    # Spend is aggregated once (never duplicated); the other external labels are
    # surfaced as aliases under the canonical campaign so the table shows the
    # canonical row a single time. A label identical to the canonical name was
    # never rewritten and is therefore not an alias.
    alias_by_canon: dict[str, list] = {}
    for m in _dedupe_mappings(applied_identity_mappings):
        canon_key = _norm(m.get("canonical_campaign_name"))
        bucket = alias_by_canon.setdefault(canon_key, [])
        lbl = m.get("external_campaign_label")
        if lbl and lbl not in bucket:
            bucket.append(lbl)
    for c in campaigns:
        c["aliases"] = alias_by_canon.get(_norm(c.get("campaign_name")), [])
    countries = _build_db_rows(
        country_spend_rows, lead_rows, revenue_rows, group_field="country",
        revenue_available=revenue_available, lead_metrics_withheld=lead_metrics_withheld,
        spend_trusted=country_spend_trusted,
    )
    summary = _build_db_summary(
        campaign_spend_rows, lead_rows, revenue_rows,
        revenue_available=revenue_available, lead_metrics_withheld=lead_metrics_withheld,
        spend_trusted=campaign_spend_trusted,
    )

    campaign_spend_status = "db" if spend_rows else "db_empty"
    contacts_status = "db" if leads.get("rows") else "db_empty"
    deals_status = "db" if revenue_rows else "db_empty"

    lead_date_grain_status = "event_date" if lead_event_date_safe else "unsafe_sync_date"
    lead_metrics_status = "withheld" if lead_metrics_withheld else (
        "db" if leads.get("rows") else "db_empty"
    )
    revenue_attribution_status = (
        "gclid_attribution_db" if revenue_available else "not_wired_or_no_closed_won"
    )
    # PR-ADS-115: split revenue truth into two independent facts. A connected
    # integration whose selected window simply has no closed-won deals is a SAFE
    # EMPTY state — not "not wired". Readiness keys off the integration fact;
    # window emptiness is handled downstream as a safe empty table.
    revenue_integration_connected = repo.revenue_integration_connected()
    revenue_integration_status = "connected" if revenue_integration_connected else "not_connected"
    revenue_window_status = "has_revenue" if revenue_available else "no_closed_won"
    # Top-level attribution_source_status retained for backward compatibility.
    if revenue_available:
        attribution_status = "gclid_attribution_db"
    elif not lead_metrics_withheld and leads.get("rows"):
        attribution_status = "hubspot_source_tags_only"
    else:
        attribution_status = "none"

    # Partial when spend coverage does not reach back to the window start, or
    # when lead metrics are withheld, or revenue is not wired.
    data_is_partial = lead_metrics_withheld or not revenue_available
    cov_start = spend.get("coverage_start")
    if start_date is not None and (cov_start is None or cov_start > resolved["start_date"]):
        data_is_partial = True

    warnings: list[str] = []
    if lead_metrics_withheld:
        warnings.append(
            "Lead/SQL metrics withheld: HubSpot contact_created_at (business event "
            "date) is missing for historical rows, so this window is not lead-safe. "
            "Treat SQL counts as withheld until the audit passes."
        )
    if not revenue_available:
        warnings.append(
            "Revenue attribution is not connected for this window — ROAS is "
            "unavailable (null), not zero."
        )
    if campaign_spend_status == "db_empty":
        warnings.append("No Google Ads spend rows in durable table 'geo' for this window.")
    if data_is_partial and cov_start and start_date is not None and cov_start > resolved["start_date"]:
        warnings.append(
            f"Partial data: durable spend coverage starts {cov_start}, after the "
            f"requested window start {resolved['start_date']}."
        )

    db_tables_used = [t for t, present in (
        ("geo", bool(spend_rows)),
        ("leads", bool(leads.get("rows"))),
        ("gclid_attribution", bool(revenue_rows)),
    ) if present]

    source_health = {
        "mode": "database",
        "campaign_spend_status": campaign_spend_status,
        "hubspot_contacts_status": contacts_status,
        "hubspot_deals_status": deals_status,
        "attribution_status": attribution_status,
        "lead_date_grain_status": lead_date_grain_status,
        "lead_metrics_status": lead_metrics_status,
        "revenue_attribution_status": revenue_attribution_status,
        "revenue_integration_status": revenue_integration_status,
        "revenue_window_status": revenue_window_status,
        # PR-ADS-118 spend-truth contract for Campaign / Country ROAS gating.
        # ROAS is produced ONLY from a complete canonical denominator; geo is
        # never a Campaign/Country ROAS denominator.
        "spend_source": spend_source,
        "spend_coverage_status": spend_coverage_status,
        "campaign_roas_available": campaign_spend_trusted,
        "country_roas_available": country_spend_trusted,
        "campaign_spend_total": canonical_total,
        "country_spend_reconciled": country_spend_reconciled,
        "canonical_customer_id": canonical_customer_id,
        "canonical_currency": canonical_currency,
        # PR-ADS-119 currency contract: native GBP truth + USD reporting via daily
        # FX. ROAS renders only when canonical coverage complete AND FX complete.
        "spend_native_total": spend_native_total,
        "spend_native_currency": canonical_currency,
        "spend_usd_total": spend_usd_total,
        "reporting_currency": reporting_currency,
        "fx_coverage_status": fx_coverage_status,
        "revenue_currency": "USD",
        # Approved campaign-identity mappings applied to the ROAS pipeline; the
        # original external labels are preserved here for audit.
        "applied_campaign_mappings": _dedupe_mappings(applied_identity_mappings),
        # PR-ADS-120b: labels marked "Not Google Ads" and excluded from Google Ads
        # ROAS (offline / import / bad UTM / CRM). Never shown as $0 or unmapped.
        "excluded_campaign_labels": excluded_campaign_labels,
        "data_is_partial": data_is_partial,
        "coverage_start": spend.get("coverage_start") or revenue.get("coverage_start"),
        "coverage_end": spend.get("coverage_end") or revenue.get("coverage_end"),
        "missing_contact_created_at_count": leads.get("missing_contact_created_at_count", 0),
        "excluded_non_paid_count": leads.get("excluded_non_paid_count", 0),
        "excluded_pseudo_campaign_count": leads.get("excluded_pseudo_campaign_count", 0),
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
        "lead_date_grain_status": lead_date_grain_status,
        "lead_metrics_status": lead_metrics_status,
        "revenue_attribution_status": revenue_attribution_status,
        "revenue_integration_status": revenue_integration_status,
        "revenue_window_status": revenue_window_status,
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

    # The JSON fallback filters contacts by HubSpot createdate, so leads are
    # event-date based; revenue is wired only when attributed deals exist.
    revenue_attribution_status = "local_json_fallback" if deals_w else "not_wired_or_no_closed_won"
    # PR-ADS-115 split contract: integration connected if ANY won deal exists.
    revenue_integration_status = "connected" if deals else "not_connected"
    revenue_window_status = "has_revenue" if deals_w else "no_closed_won"

    source_health = {
        "mode": "local_json_fallback" if has_any_local else "source_unavailable",
        "campaign_spend_status": campaign_spend_status,
        "hubspot_contacts_status": "local_json_fallback" if contacts else "source_unavailable",
        "hubspot_deals_status": "local_json_fallback" if deals else "source_unavailable",
        "attribution_status": attribution_status_json,
        "lead_date_grain_status": "event_date",
        "lead_metrics_status": "local_json_fallback" if contacts else "source_unavailable",
        "revenue_attribution_status": revenue_attribution_status,
        "revenue_integration_status": revenue_integration_status,
        "revenue_window_status": revenue_window_status,
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
        "lead_date_grain_status": "event_date",
        "lead_metrics_status": "local_json_fallback" if contacts else "source_unavailable",
        "revenue_attribution_status": revenue_attribution_status,
        "revenue_integration_status": revenue_integration_status,
        "revenue_window_status": revenue_window_status,
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


def build_revenue_deals(window: str, now: datetime | None = None) -> dict:
    """Build the Closed-Won Revenue Ledger contract for a business window.

    PR-ADS-113. Read-only deal-level revenue truth from the durable
    gclid_attribution table, windowed by deal_close_date (NEVER the scheduler
    run_date). Only closed-won deals count as revenue. One latest row per
    deal_id (deduped in the repository).

    Distinct states:
      - ledger_status="database_unavailable" when the durable ledger cannot be
        read. This is NOT the same as a safe-empty window.
      - safe-empty: ledger available but no closed-won deals in the window
        (deals == [], summary totals zeroed).

    Returns a contract with window, summary, deals, source_health, generated_at.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolved, start_date, end_date = _window_date_bounds(window, now)

    ledger = repo.fetch_revenue_deals(start_date, end_date)
    generated_at = datetime.now(timezone.utc).isoformat()

    if not ledger.get("available"):
        # The durable ledger/database cannot be read — distinct from safe-empty.
        return {
            "window": resolved,
            "summary": {
                "deal_count": 0,
                "won_revenue": None,
                "average_deal_value": None,
                "exact_gclid_count": 0,
            },
            "deals": [],
            "source_health": {
                "ledger_status": "database_unavailable",
                "revenue_attribution_status": "database_unavailable",
            },
            "generated_at": generated_at,
        }

    rows = ledger.get("rows", [])

    deals = []
    won_revenue = 0.0
    exact_gclid_count = 0
    for row in rows:
        amount = _safe_float(row.get("deal_amount_usd"))
        won_revenue += amount
        match_source = (row.get("match_source") or "").strip().lower()
        if match_source == "gclid":
            exact_gclid_count += 1
        deals.append({
            "deal_id": row.get("deal_id"),
            "company": row.get("company"),
            "country": row.get("country"),
            "campaign_name": row.get("campaign_name"),
            "deal_close_date": row.get("deal_close_date"),
            "deal_amount_usd": round(amount, 2),
            "deal_stage_label": row.get("deal_stage_label"),
            "match_status": row.get("match_status"),
            "match_source": row.get("match_source"),
        })

    # Sort: most recent close first, then largest revenue.
    deals.sort(
        key=lambda d: (d.get("deal_close_date") or "", d.get("deal_amount_usd") or 0.0),
        reverse=True,
    )

    deal_count = len(deals)
    revenue_wired = deal_count > 0
    summary = {
        "deal_count": deal_count,
        # No closed-won deals -> revenue is unavailable (None), never a fake $0.
        "won_revenue": round(won_revenue, 2) if revenue_wired else None,
        "average_deal_value": (
            round(won_revenue / deal_count, 2) if revenue_wired else None
        ),
        "exact_gclid_count": exact_gclid_count,
    }

    return {
        "window": resolved,
        "summary": summary,
        "deals": deals,
        "source_health": {
            "ledger_status": "available",
            "revenue_attribution_status": (
                "gclid_attribution_db" if revenue_wired else "not_wired_or_no_closed_won"
            ),
        },
        "generated_at": generated_at,
    }


def build_revenue_attribution_audit(window: str, now: datetime | None = None) -> dict:
    """Truth audit for /api/revenue-attribution (PR-ADS-109).

    Read-only. Proves *why* the windows are safe or unsafe: whether each metric
    filters by a business event date, whether non-paid / pseudo rows contaminate
    the campaign universe, whether revenue attribution is wired, and whether the
    business windows actually differ.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolved, start_date, end_date = _window_date_bounds(window, now)

    grain = repo.fetch_lead_date_grain_health(start_date, end_date)
    pollution = repo.fetch_campaign_pollution_report(start_date, end_date)
    revenue = repo.fetch_won_revenue(start_date, end_date)

    lead_window_safe = bool(grain.get("lead_window_safe"))
    spend_window_safe = True   # geo.run_date is the per-day source date
    revenue_window_safe = True  # gclid_attribution.deal_close_date is the close date

    date_grain_health = {
        "spend_date_field": "geo.run_date",
        "lead_date_field_current": "leads.contact_created_at",
        "lead_event_date_field_available": bool(grain.get("lead_event_date_field_available")),
        "deal_date_field": "gclid_attribution.deal_close_date",
        "lead_window_safe": lead_window_safe,
        "spend_window_safe": spend_window_safe,
        "revenue_window_safe": revenue_window_safe,
    }

    # Window comparison — Current Quarter / YTD / All Time totals via the same
    # safe build logic. Identical totals are NOT a problem: they are correct when
    # all available data falls inside the current quarter. Window integrity is a
    # property of the resolved DATE BOUNDARIES (window_ranges), not the aggregates.
    window_comparison = {}
    window_ranges = {}
    for wk in ("current_quarter", "ytd", "all_time"):
        try:
            window_comparison[wk] = build_revenue_attribution(wk, now=now)["summary"]
        except Exception:  # noqa: BLE001
            window_comparison[wk] = {}
        try:
            wr = resolve_window(wk, now=now)
            window_ranges[wk] = {
                "key": wr["key"],
                "start_date": wr["start_date"],
                "end_date": wr["end_date"],
            }
        except Exception:  # noqa: BLE001
            window_ranges[wk] = {}

    blockers: list[str] = []
    if not grain.get("available"):
        blockers.append("Database unavailable — cannot audit durable lead date grain.")
    if not lead_window_safe:
        blockers.append("leads.contact_created_at missing for historical rows")
    if pollution.get("pseudo_campaign_rows"):
        blockers.append("Pseudo-traffic campaign rows present in the leads table.")
    if pollution.get("email_campaign_rows"):
        blockers.append("Email-campaign rows present in the leads table.")

    verdict = "SAFE" if not blockers else "UNSAFE"

    # PR-ADS-115 split contract: integration connected = ANY durable attributed
    # closed-won deal exists (regardless of window); window status reflects only
    # the selected business window. A connected integration with no deals in the
    # selected window is a SAFE EMPTY state, not "not wired".
    revenue_integration_connected = repo.revenue_integration_connected()
    window_has_revenue = bool(revenue.get("rows"))
    try:
        import db.writers as _db_writers  # noqa: PLC0415
        legacy_excluded_count = _db_writers.count_lead_exclusions()
    except Exception:  # noqa: BLE001
        legacy_excluded_count = 0

    return {
        "window": {
            "key": resolved["key"],
            "label": resolved.get("label"),
            "start_date": resolved["start_date"],
            "end_date": resolved["end_date"],
            "is_closed_window": resolved.get("is_closed_window", False),
        },
        "date_grain_health": date_grain_health,
        "counts_by_source_type": grain.get("counts_by_source_type", {}),
        "excluded_non_paid_count": grain.get("excluded_non_paid_count", 0),
        "excluded_pseudo_campaign_count": grain.get("excluded_pseudo_campaign_count", 0),
        "missing_contact_created_at_count": grain.get("missing_contact_created_at_count", 0),
        "legacy_excluded_count": legacy_excluded_count,
        "pseudo_campaign_rows": pollution.get("pseudo_campaign_rows", []),
        "email_campaign_rows": pollution.get("email_campaign_rows", []),
        "zero_spend_campaigns_with_leads": pollution.get("zero_spend_campaigns_with_leads", []),
        # Retained for backward compatibility (window-only fact).
        "revenue_attribution_wired": window_has_revenue,
        "revenue_integration_status": "connected" if revenue_integration_connected else "not_connected",
        "revenue_window_status": "has_revenue" if window_has_revenue else "no_closed_won",
        "window_comparison": window_comparison,
        "window_ranges": window_ranges,
        "verdict": verdict,
        "blockers": blockers,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
