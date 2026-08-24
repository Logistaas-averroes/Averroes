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

from analysis import revenue_scope
from analysis.attribution_confidence import get_confidence_severity
from analysis.attribution_matcher import attribute_deals
from analysis.business_windows import get_window_bounds, resolve_window
from analysis.core import QUALIFIED
from db import revenue_repository as repo
from services import canonical_revenue_service as canonical_revenue
from analysis import country_identity
from analysis.country_identity import country_name_for_code, get_country_code

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


def _read_geo_coverage_ledger(customer_id, start_date, end_date) -> dict:
    """The durable geo coverage ledger for one account and range, or an honest gap.

    PR-ADS-153F. Never raises: a coverage read that fails must degrade to "we do
    not know", which the gate treats as blocking, rather than crashing the whole
    revenue contract. An unreadable ledger and an empty one are different facts
    and this preserves the difference — ``available: False`` means unreadable.
    """
    try:
        from services.google_ads_geo_sync_service import (  # noqa: PLC0415
            analyze_geo_coverage, configured_customer_id,
        )
        return analyze_geo_coverage(
            customer_id or configured_customer_id(), start_date, end_date)
    except Exception:  # noqa: BLE001 — coverage must never crash the contract
        return {"available": False, "complete": False,
                "missing_ranges": [], "failed_chunks": []}


def _nullable_float(value) -> float | None:
    """Coerce ``value`` to float, or None when it is missing/invalid.

    Unlike ``_safe_float`` this never fabricates a 0.0 — used where a missing
    value must render as "unavailable" rather than a real-looking $0.00.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        # Won deals in this bucket whose USD value could not be proven. Kept
        # separate so the bucket can say "3 customers, revenue incomplete"
        # instead of implying the missing deals were worth nothing.
        "revenue_unavailable_deals": 0,
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
    revenue_readable: bool = True,
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
    # PR-ADS-153E-B: an UNREADABLE canonical ledger leaves every bucket empty,
    # and `round(0.0, 2)` turned that outage into a confident $0 with 0 customers
    # on the row. Both are withheld instead, so an outage reads as an outage.
    #
    # `revenue_readable` is deliberately distinct from `revenue_available`: a
    # window that genuinely closed no deals IS zero customers and is reported as
    # such. Only an unreadable source withholds.
    won_revenue = round(bucket["won_revenue"], 2) if revenue_readable else None
    customers = bucket["customers"] if revenue_readable else None
    if spend_mapping_unavailable:
        spend = None
        roas = None
    else:
        spend = round(bucket["spend"], 2)
        # ROAS is null (not 0) when the revenue source is not wired — a 0 would
        # falsely imply "spent and earned nothing" when we simply have no truth.
        roas = (compute_roas(won_revenue, spend)
                if (revenue_available and spend_trusted and won_revenue is not None)
                else None)
    cac = compute_cac(spend or 0, customers) if customers is not None else None

    raw_confidence = confidence_from_tiers(bucket["tiers"])
    downgraded = legacy_gclid and raw_confidence == "high"
    confidence = "medium" if downgraded else raw_confidence

    leads_val = None if lead_metrics_withheld else bucket["leads"]
    sqls_val = None if lead_metrics_withheld else bucket["sqls"]
    sqls_for_verdict = 0 if lead_metrics_withheld else bucket["sqls"]

    has_revenue = (customers or 0) > 0 or (won_revenue or 0) > 0
    verdict = classify_verdict(spend, sqls_for_verdict, customers or 0,
                               won_revenue or 0.0, roas)
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


def _apply_geo_spend_residual(countries: list, residual_native, residual_usd,
                              use_usd) -> list:
    """Attach the Google Ads geo SPEND residual to the one residual country row.

    PR-ADS-153F. There is exactly ONE residual bucket per country view, and it
    carries two independent facts that must not be confused with each other:

      * **spend side** — the campaign↔geo shortfall Google Ads could not assign
        to a country (``geographic_view`` omits location-less spend by design).
        Governed by the unchanged PR-ADS-131 eligibility rules.
      * **revenue side** — closed-won deals whose CRM country could not be
        identified, produced by the country bucketing itself.

    Before 153F the spend residual was appended as its own extra row with
    ``customers: 0, won_revenue: 0.0`` while unidentifiable revenue was silently
    dropped, so the view could show a residual that asserted "no revenue here"
    over revenue it had discarded. Merging them means
    ``Sum(real country rows) + residual`` reconciles on BOTH sides.

    USD is shown only when FX is complete; otherwise USD stays None (never
    native GBP relabelled as USD).
    """
    spend = residual_usd if use_usd else residual_native
    existing = next((r for r in countries if r.get("is_residual")), None)
    if existing is not None:
        existing["spend"] = spend
        existing["spend_native"] = residual_native
        existing["spend_usd"] = residual_usd
        existing["spend_mapping"] = "matched"
        existing.setdefault("attribution_notes", []).append(
            "Includes Google Ads spend the geographic view does not assign to any "
            "country. Shown separately; never spread across real countries.")
        return countries
    countries.append(_geo_spend_only_residual_row(residual_native, residual_usd, use_usd))
    return countries


def _geo_spend_only_residual_row(residual_native, residual_usd, use_usd) -> dict:
    """The residual row when there is unattributed SPEND but no unattributed revenue.

    Zero customers / zero revenue is a MEASURED fact here — the country bucketing
    ran and produced no unidentifiable-geography deals — not an assumption.
    """
    spend = residual_usd if use_usd else residual_native
    return {
        "country": country_identity.RESIDUAL_LABEL,
        "country_key": country_identity.RESIDUAL_KEY,
        "country_code": None,
        "country_status": country_identity.STATUS_RESIDUAL,
        "is_residual": True,
        "spend": spend,
        "spend_native": residual_native,
        "spend_usd": residual_usd,
        "leads": 0,
        "sqls": 0,
        "customers": 0,
        "won_revenue": 0.0,
        # No revenue basis for unattributed spend — ROAS is null (never a fake 0.0 an
        # API consumer could read as a real computed ROAS), matching the mart's
        # "no revenue → ROAS unavailable" doctrine. The UI renders it as an em-dash.
        "roas": None,
        "cac": None,
        "confidence": "n/a",
        "verdict": "unattributed",
        "decision": "unattributed",
        "spend_mapping": "matched",
        "spend_state": None,
        "top_campaign": None,
        "attribution_notes": [
            "Unattributed residual — Google Ads geographic view does not assign this "
            "spend to a country. Shown separately; never spread across real countries."
        ],
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
        # PR-ADS-153F: canonical key, matching how the bucket was built.
        if country_identity.country_key(deal.get("country")) != country_key:
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

    # PR-ADS-153F: the legacy JSON country path groups on the SAME canonical key
    # as the durable path. It is diagnostic-only since 153E-B (revenue is
    # withheld here), but two key spaces for "country" is exactly the defect this
    # PR removes — leaving one behind would let it grow back.
    def bucket_for(name, display=None):
        identity = country_identity.resolve(name=name)
        key = identity.key
        if key not in buckets:
            buckets[key] = _new_bucket(identity.label)
            buckets[key]["country_identity"] = identity
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
        identity = bucket.get("country_identity")
        finalized["country_key"] = key
        finalized["country"] = identity.label if identity else bucket["display"]
        finalized["country_code"] = identity.code if identity else None
        finalized["country_status"] = identity.status if identity else None
        finalized["is_residual"] = bool(identity.is_residual) if identity else False
        finalized["top_campaign"] = (
            None if (identity is not None and identity.is_residual)
            else _top_campaign_for_country(key, deals))
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


def _scope_to_tier(attribution_scope) -> str:
    """Map a canonical attribution SCOPE to an attribution-confidence tier.

    PR-ADS-153E-B: replaces ``_match_status_to_tier``, which read
    ``gclid_attribution.match_status`` — a record of how a click id was matched
    to a campaign. The canonical equivalent is the narrowest scope the deal
    qualifies for (``analysis.revenue_scope``), which is an ordered statement
    about how strong the evidence is, not about how a join was performed.
    """
    scope = (attribution_scope or "").strip().lower()
    if scope == revenue_scope.SCOPE_GCLID_ATTRIBUTABLE:
        return "tier_1_gclid"
    if scope == revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE:
        return "tier_2_source_tag"
    return "tier_3_spend_weighted"


def _build_db_rows(spend_rows: list, lead_rows: list, revenue_rows: list, *, group_field: str,
                   revenue_available: bool = True, revenue_readable: bool = True,
                   lead_metrics_withheld: bool = False,
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
    by_country = group_field == "country"

    def bucket_for(name, code=None):
        # PR-ADS-153F: country rows group on the CANONICAL country key, never on
        # a raw lowercased label. Dashboard Countries already keyed ISO-code-first
        # while this path keyed on the string, so the same window produced two
        # different key sets — and therefore two different sets of country rows —
        # on two pages claiming to describe the same thing.
        if by_country:
            identity = country_identity.resolve(name=name, code=code)
            key = identity.key
            if key not in buckets:
                buckets[key] = _new_bucket(identity.label)
                buckets[key]["country_identity"] = identity
            return key, buckets[key]
        key = _norm(name)
        if key not in buckets:
            buckets[key] = _new_bucket(name or "unknown")
        return key, buckets[key]

    # spend rows: {campaign_name, country, spend}
    for row in spend_rows:
        _, b = bucket_for(row.get(group_field), row.get("country_code"))
        b["spend"] += _safe_float(row.get("spend"))

    # lead rows: {campaign_name, country, status_category, has_gclid}
    # (empty when lead metrics are withheld for an unsafe date grain)
    for row in lead_rows:
        _, b = bucket_for(row.get(group_field), row.get("country_code"))
        b["leads"] += 1
        if row.get("status_category") == "qualified":
            b["sqls"] += 1

    # revenue rows: canonical scoped deals (PR-ADS-153E-B). A deal whose currency
    # could not be proven is still a CUSTOMER — it is genuinely won — but it
    # contributes no revenue, and the bucket records that its money is incomplete
    # so a ROAS is not built on a silently understated numerator.
    for row in revenue_rows:
        _, b = bucket_for(row.get(group_field), row.get("country_code"))
        b["customers"] += 1
        amount = row.get("deal_amount_usd")
        if amount is None:
            b["revenue_unavailable_deals"] += 1
        else:
            b["won_revenue"] += float(amount)
        b["tiers"].append(_scope_to_tier(row.get("attribution_scope")))

    rows = []
    for key, bucket in buckets.items():
        # PR-ADS-153F: a COUNTRY bucket is never dropped. Blank, invalid and
        # unresolved geography used to be discarded here, so revenue that exists
        # simply vanished from ROAS by Country while Dashboard Countries kept it
        # as a residual — the two pages then reported different totals for the
        # same window. Country keys are now always non-empty (a real `code:XX` or
        # the explicit residual), so the guard below only ever drops a blank
        # CAMPAIGN key, which keeps that page's existing contract untouched.
        if not key:
            continue
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
            revenue_readable=revenue_readable,
            lead_metrics_withheld=lead_metrics_withheld,
            spend_trusted=spend_trusted,
            spend_mapping_unavailable=mapping_unavailable,
            spend_state=spend_state,
        )
        if group_field == "campaign_name":
            finalized["campaign_id"] = key
            finalized["campaign_name"] = bucket["display"] or "unknown"
        else:
            # PR-ADS-153F: identity comes from the shared contract, so the code
            # is the one that produced the key rather than a second, independent
            # resolution of the display label (which is how a row could carry a
            # code that disagreed with the bucket it was in).
            identity = bucket.get("country_identity")
            finalized["country_key"] = key
            finalized["country"] = identity.label if identity else bucket["display"]
            finalized["country_code"] = identity.code if identity else None
            finalized["country_status"] = identity.status if identity else None
            finalized["is_residual"] = bool(identity.is_residual) if identity else False
            if identity is not None and identity.is_residual:
                # The residual carries real revenue, so it must not be scored as
                # a market: no ROAS verdict, no "top campaign", and an explicit
                # note saying what it is. Its numbers stay visible and countable.
                finalized["verdict"] = "unattributed"
                finalized["decision"] = "unattributed"
                finalized["top_campaign"] = None
                finalized.setdefault("attribution_notes", []).append(
                    "Revenue whose country could not be identified "
                    f"({identity.reason}). Shown separately so totals reconcile; "
                    "never spread across real countries.")
            else:
                finalized["top_campaign"] = _db_top_campaign(key, revenue_rows, spend_rows)
        rows.append(finalized)

    # Spend may be None (unavailable mapping); sort those as 0 so ordering is
    # stable and revenue still breaks ties.
    rows.sort(key=lambda r: (r["spend"] or 0.0, r["won_revenue"]), reverse=True)
    return rows


def _db_top_campaign(country_key: str, revenue_rows: list, spend_rows: list) -> str | None:
    """Top campaign in a country by won revenue, falling back to spend (DB rows).

    PR-ADS-153F: matches on the CANONICAL country key, the same one the row was
    bucketed under. Matching on a normalized name here while bucketing on the
    canonical key would silently return "no top campaign" for every row whose
    label differed from its code.
    """
    by_rev: dict[str, float] = {}
    for r in revenue_rows:
        if country_identity.country_key(r.get("country"), r.get("country_code")) == country_key:
            name = r.get("campaign_name") or "unknown"
            by_rev[name] = by_rev.get(name, 0.0) + _safe_float(r.get("deal_amount_usd"))
    if by_rev:
        best = max(by_rev, key=by_rev.get)
        if by_rev[best] > 0:
            return best
    by_spend: dict[str, float] = {}
    for r in spend_rows:
        if country_identity.country_key(r.get("country"), r.get("country_code")) == country_key:
            name = r.get("campaign_name") or "unknown"
            by_spend[name] = by_spend.get(name, 0.0) + _safe_float(r.get("spend"))
    if by_spend:
        best = max(by_spend, key=by_spend.get)
        if by_spend[best] > 0:
            return best
    return None


def _build_db_summary(spend_rows: list, lead_rows: list, revenue_rows: list, *,
                      revenue_available: bool = True, revenue_readable: bool = True,
                      lead_metrics_withheld: bool = False,
                      spend_trusted: bool = True) -> dict:
    spend = round(sum(_safe_float(r.get("spend")) for r in spend_rows), 2)
    leads = None if lead_metrics_withheld else len(lead_rows)
    sqls = None if lead_metrics_withheld else sum(
        1 for r in lead_rows if r.get("status_category") == "qualified"
    )
    customers = len(revenue_rows) if revenue_readable else None
    known = [r for r in revenue_rows if r.get("deal_amount_usd") is not None]
    # Withheld, not zero, when the window holds won deals whose value was never
    # proven at all. A confident $0 beside a non-zero customer count is exactly
    # the fabrication this contract exists to prevent.
    won_revenue = (round(sum(float(r["deal_amount_usd"]) for r in known), 2)
                   if (revenue_readable and (known or not revenue_rows)) else None)
    tiers = [_scope_to_tier(r.get("attribution_scope")) for r in revenue_rows]
    # PR-ADS-118: the summary ROAS is only trustworthy from a complete canonical
    # denominator; otherwise it is unavailable (never from a partial/geo total).
    return {
        "spend": spend,
        "leads": leads,
        "sqls": sqls,
        "customers": customers,
        "won_revenue": won_revenue,
        "revenue_unavailable_deals": (len(revenue_rows) - len(known)
                                      if revenue_readable else None),
        "roas": (compute_roas(won_revenue, spend)
                 if (revenue_available and spend_trusted and won_revenue is not None)
                 else None),
        "cac": compute_cac(spend, customers) if customers else None,
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


def _build_from_db(resolved, start_date, end_date, window=None, now=None) -> dict | None:
    """Build the contract from durable DB sources.

    Returns the full contract dict, or None when the database is unavailable
    (so the caller can fall back to the local-JSON diagnostic path).
    """
    spend = repo.fetch_campaign_country_spend(start_date, end_date)
    if not spend["available"]:
        return None  # DB unreachable — signal caller to fall back

    leads = repo.fetch_lead_quality(start_date, end_date)
    sync = repo.fetch_sync_state()

    # ── Revenue: canonical ledger, joined only through attribution evidence ──
    # PR-ADS-153E-B. Google Ads still owns spend, clicks and campaign identity;
    # it no longer defines which deals exist. The ROAS tables therefore read the
    # canonical won population and narrow it with the explicit scope lattice:
    #
    #   * campaign rows  → `campaign_attributable` (a usable campaign identifier)
    #   * country rows   → `google_ads_source`     (Google Ads evidence, any campaign)
    #
    # The all-source ladder is carried alongside so these views can state that
    # attributed revenue is a SUBSET, and how large a subset — they must never
    # read as total business revenue.
    canonical_base = canonical_revenue.load_won_deals(
        window, start=None if window else start_date,
        end=None if window else end_date, now=now)
    revenue_available_canonical = bool(canonical_base.get("available"))
    campaign_revenue_rows = (canonical_revenue.canonical_deal_rows(
        canonical_base, revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE)
        if revenue_available_canonical else [])
    country_revenue_rows = (canonical_revenue.canonical_deal_rows(
        canonical_base, revenue_scope.SCOPE_GOOGLE_ADS_SOURCE)
        if revenue_available_canonical else [])
    scope_ladder = canonical_revenue.get_scope_ladder(base=canonical_base)

    spend_rows = spend["rows"]
    # The campaign-scoped set drives identity mapping, the summary and the
    # campaign table; the country table uses its own wider scope below.
    revenue_rows = campaign_revenue_rows

    # PR-ADS-118: Campaign ROAS spend truth comes ONLY from the canonical Google
    # Ads campaign-daily table, NEVER the geo table. When the canonical table is
    # reachable we always evaluate its coverage — even with zero rows, because a
    # COMPLETE coverage window with zero rows is verified zero spend, whereas a
    # window with missing/failed chunks is "incomplete" (never read as $0).
    #
    # Spend is a usable ROAS denominator only when canonical coverage is COMPLETE.
    # When the canonical table is unreachable we fall to geo, but geo is
    # DIAGNOSTIC ONLY and must never back a Campaign or Country ROAS.
    # PR-ADS-154B §2: one account scope, resolved before any read, applied to
    # every side of the comparison — campaign total, geo total, spend ledger and
    # geo ledger. The geo ledger has been customer-scoped since PR-ADS-153F; the
    # other three were not, so a multi-account database compared totals spanning
    # every account against coverage proven for one. When no account is
    # configured this is None and the reads stay account-wide, unchanged.
    from services.google_ads_spend_service import (  # noqa: PLC0415
        configured_customer_id as _configured_customer_id,
    )
    _scope_customer_id = _configured_customer_id()

    canonical = repo.fetch_canonical_campaign_spend(start_date, end_date, _scope_customer_id)
    canonical_access = bool(canonical.get("available"))
    if canonical_access:
        from services.google_ads_spend_service import (  # noqa: PLC0415
            analyze_coverage as _analyze_cov, SPEND_VARIANCE_TOLERANCE,
        )
        coverage = repo.fetch_spend_coverage(start_date, end_date, _scope_customer_id)
        _cov = _analyze_cov(start_date, end_date, coverage.get("chunks", []))
        coverage_complete = bool(_cov.get("complete"))
        spend_coverage_status = "complete" if coverage_complete else "incomplete"
        # PR-ADS-129: classify the coverage REASON from the durable ledger so ROAS
        # availability is auditable — a window whose first spend row starts after
        # the window start is coverage-COMPLETE when the earlier period was fetched
        # and verified as zero spend (never inferred zero).
        from services.google_ads_spend_service import classify_coverage as _classify_cov  # noqa: PLC0415
        _cs = canonical.get("coverage_start")
        _first_spend = _cs if (isinstance(_cs, str) or _cs is None) else _cs.isoformat()
        _ce = canonical.get("coverage_end")
        _last_spend = _ce if (isinstance(_ce, str) or _ce is None) else _ce.isoformat()
        coverage_detail = _classify_cov(
            start_date, end_date, coverage.get("chunks", []), _first_spend)
        campaign_spend_coverage_status = coverage_detail["status"]
        campaign_spend_coverage_reason = coverage_detail["reason"]
        spend_source = "canonical_google_ads_api"
        canonical_rows = canonical.get("rows") or []
        canonical_total = round(float(canonical.get("total_spend") or 0), 2)
        canonical_customer_id = _scope_customer_id or canonical.get("customer_id")
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
        # PR-ADS-124: prefer the canonical Google Ads API geo total (the durable
        # google_ads_geo_daily_spend table, populated by the geo sync) when it has
        # rows; otherwise fall back to the legacy geo-table sum. This moves country
        # ROAS reconciliation onto canonical Google Ads API geo rows without ever
        # loosening the rule (no geo rows → fall back, never a fabricated match).
        geo_canonical = repo.fetch_geo_daily_spend_total(
            start_date, end_date, _scope_customer_id)
        # PR-ADS-153F: the durable coverage ledger is read HERE, before the geo
        # total is chosen, because a legacy fallback is only defensible while we
        # genuinely do not know what canonical geo holds for this range. Scoped
        # to THIS window's canonical spend customer — coverage is an
        # account-scoped fact, and reading it unscoped would let one account's
        # verified history declare another account's window covered.
        _geo_ledger = _read_geo_coverage_ledger(
            canonical_customer_id, start_date, end_date)
        geo_coverage_ok = bool(_geo_ledger.get("available") and _geo_ledger.get("complete"))
        _geo_failed_chunks = _geo_ledger.get("failed_chunks") or []
        # PR-ADS-154B §2: the two totals below are only comparable if they were
        # measured over the same accounts and the same currency. Both tables key
        # on customer_id and store currency_code per row, and neither query
        # converts, so a mixed set is summed as though it were one thing.
        _scope_notes = []
        if int(canonical.get("customer_count") or 0) > 1:
            _scope_notes.append("campaign spend spans multiple Google Ads accounts")
        if int(geo_canonical.get("customer_count") or 0) > 1:
            _scope_notes.append("geo spend spans multiple Google Ads accounts")
        if (int(canonical.get("currency_count") or 0) > 1
                or int(geo_canonical.get("currency_count") or 0) > 1):
            _scope_notes.append("spend rows span multiple currencies and are summed unconverted")
        _geo_currency = geo_canonical.get("currency_code")
        if (geo_canonical.get("has_rows") and _geo_currency and canonical_currency
                and _geo_currency != canonical_currency):
            _scope_notes.append(
                f"campaign total is {canonical_currency} but geo total is {_geo_currency}")
        comparison_like_for_like = not _scope_notes
        if geo_canonical.get("available") and geo_canonical.get("has_rows"):
            geo_total_for_recon = round(float(geo_canonical.get("total_spend") or 0.0), 2)
        elif (geo_canonical.get("available") and geo_coverage_ok
              and not _geo_failed_chunks):
            # A successful query over a range the ledger certifies as fully
            # fetched, returning nothing, is a MEASURED zero — not an absence.
            # Falling back to the legacy table here would answer a question
            # canonical geo had already answered, with a different source.
            geo_total_for_recon = 0.0
        else:
            geo_total_for_recon = round(sum(_safe_float(r.get("spend")) for r in spend_rows), 2)
        if canonical_total > 0:
            country_spend_reconciled = (
                abs(geo_total_for_recon - canonical_total) / canonical_total
                <= SPEND_VARIANCE_TOLERANCE
            )
        else:
            country_spend_reconciled = (geo_total_for_recon == 0)
        # PR-ADS-129: surface the exact geo↔canonical variance so the ROAS by
        # Country blocked card can show real totals (never fabricated).
        country_geo_total_native = geo_total_for_recon
        country_spend_variance_native = round(geo_total_for_recon - canonical_total, 2)
        country_spend_variance_pct = (
            round(abs(country_spend_variance_native) / canonical_total * 100, 4)
            if canonical_total > 0 else None
        )
        country_spend_tolerance = SPEND_VARIANCE_TOLERANCE
    else:
        # Canonical table unreachable → geo is diagnostic only, NEVER a ROAS
        # denominator. ROAS is unavailable for both Campaign and Country.
        spend_source = "geo_fallback"
        spend_coverage_status = "geo_fallback"
        coverage_complete = False
        campaign_spend_coverage_status = "unavailable"
        campaign_spend_coverage_reason = "canonical_unavailable"
        coverage_detail = {"missing_chunks": [], "failed_chunks": [], "verified_zero_chunks": []}
        _first_spend = None
        _last_spend = None
        country_geo_total_native = None
        country_spend_variance_native = None
        country_spend_variance_pct = None
        # Reuse the shared constant so the tolerance can never silently drift.
        from services.google_ads_spend_service import (  # noqa: PLC0415
            SPEND_VARIANCE_TOLERANCE as _SVT,
        )
        country_spend_tolerance = _SVT
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
        # With no canonical baseline there is nothing to certify geo against, so
        # the ledger is not consulted and coverage is reported as unproven rather
        # than as absent-and-therefore-fine.
        _geo_ledger = {"available": False, "complete": False,
                       "missing_ranges": [], "failed_chunks": []}
        geo_coverage_ok = False
        _geo_failed_chunks = []
        # No canonical baseline means no comparison was made at all, so there is
        # no scope at which one could have been like-for-like. False, not True:
        # this branch already reports every country metric unavailable, and an
        # optimistic default here would be the only thing arguing otherwise.
        _scope_notes = ["canonical campaign spend is unavailable"]
        comparison_like_for_like = False

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
        # The same exclusions apply to the country table: a label an admin marked
        # "not a real Google Ads campaign" is not Google Ads revenue in ANY
        # advertising view. The deal keeps its place in all-source truth.
        country_revenue_rows = [r for r in country_revenue_rows
                                if _ident_norm(r.get("campaign_name"))
                                not in excluded_label_keys]
    resolution_map = (_build_resolution_map(canonical.get("rows") or [], approved_mappings)
                      if canonical_access else {})
    if resolution_map:
        revenue_rows = _apply_identity_map(revenue_rows, resolution_map, applied_identity_mappings)

    # PR-ADS-124: the COUNTRY spend SOURCE. When the canonical Google Ads geo
    # table has rows, the ROAS by Country table is built from the SAME canonical
    # source as the reconciliation total — never the legacy `geo` table. Each row
    # carries the country resolved from its criterion id (country_name / code) and
    # per-day-FX USD. A criterion that did not resolve to a country is dropped from
    # named rows (honest partial coverage), never bucketed as unknown spend.
    geo_by_country = repo.fetch_geo_daily_spend_by_country(start_date, end_date)
    # PR-ADS-153F: readability is a statement about the QUERY, rows are a
    # statement about the data, and a coverage-verified empty result is a third
    # thing again — a proven zero. The gate needs all three named separately.
    canonical_geo_readable = bool(canonical_access and geo_by_country.get("available"))
    canonical_geo_country = bool(canonical_geo_readable and geo_by_country.get("has_rows"))
    canonical_geo_verified_zero = bool(
        canonical_geo_readable and not geo_by_country.get("has_rows")
        and geo_coverage_ok and not _geo_failed_chunks)
    if canonical_geo_verified_zero:
        # Canonical geo answered, and the answer was "no country spend in this
        # range". Reaching for the legacy table here would replace a proven
        # answer with a different source's guess at the same question.
        geo_country_source = "canonical_google_ads_api"
        country_source_rows = []
    elif canonical_geo_country:
        geo_country_source = "canonical_google_ads_api"
        country_source_rows = []
        for r in geo_by_country.get("rows", []):
            name = (r.get("country_name")
                    or country_name_for_code(r.get("country_code"))
                    or r.get("country_code"))
            if not name:
                continue  # unresolved criterion id → not country-named
            country_source_rows.append({
                "campaign_name": None, "country": name,
                "spend": r.get("spend"), "spend_usd": r.get("spend_usd"),
                "fx_complete": r.get("fx_complete"),
            })
    else:
        geo_country_source = "legacy_geo_table"
        country_source_rows = spend_rows

    # Campaign spend is a trusted ROAS denominator only when canonical coverage is
    # complete AND FX coverage is complete (USD reporting). Country spend
    # additionally requires geo↔canonical reconciliation AND that the visible
    # country rows are sourced from the SAME canonical geo table (PR-ADS-124) —
    # never unblock from the legacy table or a bare reconciled total.
    campaign_spend_trusted = canonical_access and coverage_complete and fx_complete
    # PR-ADS-131: a by-design unattributed residual (geo assigns most spend to
    # countries; the shortfall is location-less spend the geographic_view omits) is
    # a SAFE unblock. Real country rows keep their geo-attributed spend and an
    # explicit "Unknown / Unattributed country" residual bucket is added — never spread
    # across countries, never a real country, never loosening tolerance. Missing geo
    # dates / campaigns missing geo / incomplete FX/coverage stay BLOCKED.
    country_residual = {"eligible": False, "residual_native": None,
                        "residual_pct": None, "reason": None}
    _country_missing_geo_dates: list = []
    _country_campaigns_missing_geo: list = []
    if canonical_geo_country and country_spend_reconciled is False:
        from services.google_ads_geo_sync_service import (  # noqa: PLC0415
            _geo_reconciliation_detail, evaluate_country_residual,
        )
        _geo_detail = _geo_reconciliation_detail(
            start_date, end_date, canonical_total, geo_total_for_recon, "mismatch")
        _country_missing_geo_dates = _geo_detail.get("missing_geo_dates") or []
        _country_campaigns_missing_geo = _geo_detail.get("campaigns_missing_geo") or []
        country_residual = evaluate_country_residual(
            canonical_total, geo_total_for_recon, _geo_detail,
            coverage_complete=coverage_complete, fx_complete=fx_complete,
            geo_has_rows=canonical_geo_country, reconciled=False)
    country_residual_eligible = bool(country_residual["eligible"])
    # PR-ADS-153F: carry the gap reason and the durable geo-coverage evidence into
    # source_health so the shared country-truth disclosure can state WHY a window
    # is blocked and WHICH ranges are missing or failed — evidence the spend
    # comparison alone cannot produce (it cannot tell "never fetched" from
    # "fetched, genuinely zero").
    country_gap_reason = country_residual.get("reason")
    # PR-ADS-153F blocker 1: durable geo coverage is a MANDATORY input, not side
    # evidence. Its whole purpose is to separate "fetched and genuinely zero"
    # from "never fetched", and a gate that reads it but does not require it
    # discards exactly that distinction — a window whose geo was never synced
    # would reconcile against a total it should not have trusted. `_geo_ledger`
    # and `geo_coverage_ok` are read once, above, before the geo total is chosen.
    #
    # A coverage-verified empty geo range is trustworthy for the same reason a
    # populated one is: the ledger proves it was fetched. Requiring ROWS here
    # would have made this surface disagree with `build_geo_reconciliation`
    # about the same window, which is the contradiction blocker 1 removed.
    country_spend_trusted = (
        campaign_spend_trusted
        and (canonical_geo_country or canonical_geo_verified_zero) and geo_coverage_ok
        and (country_spend_reconciled is True or country_residual_eligible))
    # Country spend expressed in the ROAS currency: when trusted + FX complete, use
    # the per-day-FX USD carried on the canonical geo rows; otherwise native
    # diagnostic. Never native-vs-USD mixed.
    if country_spend_trusted and canonical_geo_country and use_usd:
        country_spend_rows = [
            {"campaign_name": r.get("campaign_name"), "country": r.get("country"),
             "spend": r.get("spend_usd")}
            for r in country_source_rows
        ]
    elif country_spend_trusted and fx_effective_rate is not None:
        country_spend_rows = [
            {**r, "spend": _safe_float(r.get("spend")) * fx_effective_rate}
            for r in country_source_rows
        ]
    else:
        country_spend_rows = country_source_rows
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

    # Revenue is "wired" only when the canonical ledger is READABLE and the
    # scope actually contains attributed deals. An unreadable ledger is not an
    # empty one: ROAS stays null (never zero) and the status names the reason.
    revenue_available = bool(revenue_available_canonical and revenue_rows)

    # PR-ADS-124: country availability keys off the COUNTRY SOURCE rows (canonical
    # geo when present, else legacy) so it reflects the same source that feeds the
    # table and the reconciliation total.
    named_spend = [r for r in country_source_rows if r.get("country")]
    country_spend_available = bool(named_spend)

    campaigns = _build_db_rows(
        campaign_spend_rows, lead_rows, revenue_rows, group_field="campaign_name",
        revenue_available=revenue_available,
        revenue_readable=revenue_available_canonical,
        lead_metrics_withheld=lead_metrics_withheld,
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
        country_spend_rows, lead_rows, country_revenue_rows, group_field="country",
        revenue_available=revenue_available,
        revenue_readable=revenue_available_canonical,
        lead_metrics_withheld=lead_metrics_withheld,
        spend_trusted=country_spend_trusted,
    )
    # PR-ADS-131: append the explicit residual bucket AFTER the real country rows so
    # Sum(country spend) + residual == canonical campaign spend. USD is converted
    # with the mart's effective FX rate only when FX is complete (else USD is None,
    # never native GBP relabelled as USD).
    country_residual_usd = None
    if country_residual_eligible:
        _res_native = country_residual["residual_native"]
        country_residual_usd = (round(_res_native * fx_effective_rate, 2)
                                if (use_usd and fx_effective_rate is not None) else None)
        countries = _apply_geo_spend_residual(
            countries, _res_native, country_residual_usd, use_usd)
    summary = _build_db_summary(
        campaign_spend_rows, lead_rows, revenue_rows,
        revenue_available=revenue_available,
        revenue_readable=revenue_available_canonical,
        lead_metrics_withheld=lead_metrics_withheld,
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
        canonical_revenue.CANONICAL_SOURCE if revenue_available
        else ("canonical_ledger_unavailable" if not revenue_available_canonical
              else "no_campaign_attributable_deals_in_window")
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
        attribution_status = canonical_revenue.CANONICAL_SOURCE
    elif not lead_metrics_withheld and leads.get("rows"):
        attribution_status = "hubspot_source_tags_only"
    else:
        attribution_status = "none"

    # Partial when campaign spend coverage is genuinely incomplete (per the durable
    # coverage LEDGER — PR-ADS-129), or when lead metrics are withheld, or revenue
    # is not wired. NOTE: we no longer treat "first geo row starts after the window
    # start" as partial — that geo-first-row heuristic falsely flagged windows whose
    # earlier period was fetched and verified as zero spend.
    data_is_partial = lead_metrics_withheld or not revenue_available
    if canonical_access and not coverage_complete:
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
    # PR-ADS-129: the spend-coverage warning is driven by the CANONICAL coverage
    # ledger classification (missing / failed chunks), never the geo table's first
    # row. Coverage that is complete — including verified-zero before the first
    # spend row — is NOT flagged as partial.
    if canonical_access and campaign_spend_coverage_status in ("incomplete", "unknown"):
        if campaign_spend_coverage_reason == "failed_chunks":
            warnings.append(
                "Campaign spend coverage incomplete: one or more Google Ads spend "
                "backfill chunks FAILED for this window — ROAS is unavailable until "
                "they are re-run."
            )
        elif campaign_spend_coverage_status == "unknown":
            # e.g. all_time with no durable coverage ledger — completeness cannot be
            # established, so ROAS stays unavailable (never assumed complete).
            warnings.append(
                "Campaign spend coverage could not be verified for this window: "
                "there is no durable Google Ads spend coverage ledger — ROAS is "
                "unavailable until the Google Ads spend backfill runs."
            )
        else:
            warnings.append(
                "Campaign spend coverage incomplete: durable Google Ads spend for "
                f"{resolved['start_date']} → {resolved['end_date']} has "
                f"unverified dates ({campaign_spend_coverage_reason}) — ROAS is "
                "unavailable until the missing period is backfilled or verified as "
                "zero spend."
            )

    db_tables_used = [t for t, present in (
        ("geo", bool(spend_rows)),
        ("leads", bool(leads.get("rows"))),
        # PR-ADS-153E-B: revenue is the canonical ledger, not gclid_attribution.
        ("hubspot_deal_ledger", bool(revenue_rows)),
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
        # PR-ADS-129: auditable coverage classification. campaign_spend_coverage_status
        # is "complete" even when the first spend row starts after the window start,
        # provided the earlier period was fetched and VERIFIED as zero spend.
        "campaign_spend_coverage_status": campaign_spend_coverage_status,
        "campaign_spend_coverage_reason": campaign_spend_coverage_reason,
        "spend_coverage_detail": {
            "requested_start": resolved["start_date"],
            "requested_end": resolved["end_date"],
            "first_spend_date": _first_spend,
            "last_spend_date": _last_spend,
            "missing_chunks": coverage_detail.get("missing_chunks", []),
            "failed_chunks": coverage_detail.get("failed_chunks", []),
            "verified_zero_chunks": coverage_detail.get("verified_zero_chunks", []),
        },
        "campaign_roas_available": campaign_spend_trusted,
        "country_roas_available": country_spend_trusted,
        # PR-ADS-124: which spend source backed the country rows (canonical geo
        # table vs legacy geo table) — country ROAS is trusted only from canonical.
        "geo_country_source": geo_country_source,
        "campaign_spend_total": canonical_total,
        "country_spend_reconciled": country_spend_reconciled,
        # PR-ADS-153F: durable geo coverage is a MANDATORY gate input. Every
        # field the shared gate needs is published here, so the mart derives the
        # same verdict from the same facts rather than from a subset.
        "country_gap_reason": country_gap_reason,
        "geo_coverage_status": ("complete" if _geo_ledger.get("complete")
                                else "incomplete" if _geo_ledger.get("available")
                                else "unavailable"),
        "geo_coverage_missing_ranges": _geo_ledger.get("missing_ranges") or [],
        "failed_geo_dates": _geo_ledger.get("failed_chunks") or [],
        "country_geo_rows_available": bool(canonical_geo_country),
        # The gate's `geo_readable` input: did the canonical geo QUERY succeed?
        # Distinct from `country_geo_rows_available` (did it return rows) and
        # from `country_geo_verified_zero` (it returned nothing, and the ledger
        # proves the range was fetched, so nothing is the real answer).
        "country_geo_query_readable": bool(canonical_geo_readable),
        "country_geo_verified_zero": bool(canonical_geo_verified_zero),
        "missing_geo_dates": list(_country_missing_geo_dates or []),
        "campaigns_missing_geo": list(_country_campaigns_missing_geo or []),
        # PR-ADS-154B §2: the gate's `comparison_like_for_like` input, plus the
        # reasons behind a False. Without this the mart reads the field absent
        # and — reading fail-closed, correctly — blocks; publishing it here is
        # what lets a genuinely single-account, single-currency comparison pass.
        "comparison_like_for_like": comparison_like_for_like,
        "comparison_scope_notes": list(_scope_notes),
        "scope_customer_id": _scope_customer_id,
        # PR-ADS-129: exact geo↔canonical variance for the Country blocked card.
        "country_geo_total": country_geo_total_native,
        "country_spend_variance": country_spend_variance_native,
        "country_spend_variance_pct": country_spend_variance_pct,
        "country_spend_tolerance": country_spend_tolerance,
        # PR-ADS-131: safe unblock via an explicit unattributed residual bucket.
        "country_residual_eligible": country_residual_eligible,
        "country_residual_native": country_residual["residual_native"],
        "country_residual_usd": country_residual_usd,
        "country_residual_pct": country_residual["residual_pct"],
        "country_residual_reason_code": country_residual["reason"],
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
        # Revenue coverage now comes from the canonical ledger's own window, not
        # from a legacy table's min/max close date.
        "coverage_start": spend.get("coverage_start") or canonical_base.get("window_start"),
        "coverage_end": spend.get("coverage_end") or canonical_base.get("window_end"),
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
        # PR-ADS-124: "available" only when canonical geo named rows feed the table;
        # legacy-only or unresolved criteria are "partial"; no geo at all is honest.
        "geo_country_mapping_status": (
            "available" if (canonical_geo_country and country_spend_available)
            else ("no_geo_data" if not country_source_rows else "partial")
        ),
        "geo_country_source": geo_country_source,
        "data_is_partial": data_is_partial,
        # ── PR-ADS-153E-B revenue provenance and scope ───────────────────────
        # These views are ADVERTISING views. They declare the attribution scope
        # their revenue was narrowed to and carry the all-source ladder, so a
        # reader can see that the attributed figure is a subset of the business
        # — and exactly how much of the business it covers. Non-attributed deals
        # are never removed from the company-wide population to make these rows
        # add up; they simply are not in this scope.
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE,
        "country_revenue_scope": revenue_scope.SCOPE_GOOGLE_ADS_SOURCE,
        "revenue_available": revenue_available_canonical,
        "revenue_unavailable_reason": (None if revenue_available_canonical
                                       else canonical_base.get("reason")),
        "revenue_violation_codes": canonical_base.get("violation_codes") or [],
        "as_of": canonical_base.get("as_of"),
        "attribution_coverage": scope_ladder,
        "legacy_fallback_used": False,
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
      1. Read spend and leads from persisted PostgreSQL tables (geo / leads) and
         closed-won revenue from the canonical deal ledger through
         ``services.canonical_revenue_service`` (PR-ADS-153E-B).
      2. If the database is unavailable, fall back to local JSON files for the
         SPEND/lead shape only, clearly labeled as a diagnostic fallback
         (source_health.mode). Revenue is never served from local JSON.
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

    db_result = _build_from_db(resolved, start_date, end_date, window=window, now=now)
    if db_result is not None:
        return db_result

    # Database unavailable — diagnostic fallback to local JSON for SPEND SHAPE
    # only. PR-ADS-153E-B: local JSON is a prohibited revenue source. It holds a
    # different deal population with no deduplication and no currency doctrine,
    # so serving revenue from it during a database outage would silently swap
    # what "closed-won revenue" means at the worst possible moment.
    start_dt, end_dt = get_window_bounds(window, now=now)
    return _withhold_json_revenue(_build_from_json(resolved, start_dt, end_dt))


# Metrics that depend on the closed-won deal population, and are therefore
# unavailable whenever revenue does not come from the canonical ledger.
_REVENUE_DERIVED_KEYS = ("customers", "won_revenue", "roas", "cac", "confidence")


def _withhold_json_revenue(contract: dict) -> dict:
    """Blank every revenue-derived metric on the local-JSON diagnostic fallback.

    Spend, leads and SQLs survive — they come from their own sources and the
    fallback is honest about being diagnostic. Revenue does not: it would come
    from `data/attributed_deals.json`, which is one of the three conflicting
    lineages PR-ADS-153E replaced. Withholding it leaves the page saying "revenue
    unavailable" instead of quietly showing a second, smaller set of customers.
    """
    if not isinstance(contract, dict):
        return contract
    for key in _REVENUE_DERIVED_KEYS:
        if key in (contract.get("summary") or {}):
            contract["summary"][key] = None
    for bucket in ("campaigns", "countries"):
        for row in contract.get(bucket) or []:
            for key in _REVENUE_DERIVED_KEYS:
                if key in row:
                    row[key] = None
    health = contract.setdefault("source_health", {})
    health["revenue_attribution_status"] = "canonical_ledger_unavailable"
    health["revenue_source"] = canonical_revenue.CANONICAL_SOURCE
    health["legacy_fallback_used"] = False
    contract["revenue_available"] = False
    contract["revenue_unavailable_reason"] = (
        canonical_revenue.REASON_LEDGER_UNREADABLE)
    contract.setdefault("warnings", []).append(
        "Closed-won revenue is unavailable: the canonical deal ledger could not "
        "be read and local JSON is not a permitted revenue source.")
    return contract


def _mask_gclid(gclid) -> str | None:
    """Show a masked GCLID prefix (privacy) or None — never a fake value."""
    if not gclid:
        return None
    g = str(gclid)
    return (g[:6] + "…") if len(g) > 6 else g


def _deal_detail_row(r: dict) -> dict:
    """One 'client / deal behind this campaign' detail row (PR-ADS-130).

    Only fields the durable tables actually store are populated; unavailable
    identity fields (company record id, contact name, deal name are not stored)
    are explicit None so the UI shows "unavailable", never a fabricated id.
    """
    return {
        # PR-ADS-153E-B: the canonical ledger stores the DEAL's name, not the
        # associated contact's employer, so the company columns are honestly
        # unavailable rather than filled with a deal name.
        "company_name": None,
        "company_record_id": None,
        "main_contact_name": None,          # contact name is not stored
        "main_contact_record_id": r.get("primary_contact_id") or None,
        "deal_name": r.get("deal_name") or None,
        "deal_record_id": r.get("deal_id") or None,
        # Preserve None for a missing amount — never fabricate a $0.00 the UI
        # would show as real revenue; the drawer renders it as "unavailable".
        "deal_amount_usd": (lambda a: round(a, 2) if a is not None else None)(
            _nullable_float(r.get("deal_amount_usd"))),
        "currency_status": r.get("currency_status"),
        "deal_close_date": r.get("deal_close_date"),
        "attributed_campaign_label": r.get("external_campaign_label") or r.get("campaign_name"),
        "canonical_campaign_name": r.get("campaign_name"),
        # The attribution SCOPE this deal qualifies for, replacing the GCLID
        # match_status/match_source pair that described a join, not evidence.
        "match_method": r.get("attribution_scope") or None,
        "attribution_status": r.get("attribution_status") or None,
        "gclid": _mask_gclid(r.get("gclid")),
        "country": r.get("country") or None,
        "deal_stage_label": r.get("deal_stage_label") or None,
    }


def build_campaign_deal_details(window: str, campaign: str,
                                now: datetime | None = None) -> dict:
    """Closed-won client/deal detail rows behind ONE canonical campaign (PR-ADS-130).

    Read-only drilldown for the ROAS by Campaign drawer. Fetches closed-won deals
    from the durable gclid_attribution table (windowed by deal_close_date), applies
    the SAME canonical identity map the ROAS rows use so a deal's raw campaign label
    groups onto its canonical campaign, then returns the deals for the requested
    canonical campaign. Never writes anything; never fabricates company/contact ids.

    Raises ValueError for an unsupported window.
    """
    resolved, start_date, end_date = _window_date_bounds(window, now)
    generated_at = datetime.now(timezone.utc).isoformat()

    # PR-ADS-153E-B: the deals proving a campaign row come from the canonical
    # ledger at `campaign_attributable` scope — the SAME scope the ROAS row was
    # aggregated at, so the drawer can never list a different set of deals from
    # the number it is opened from.
    base = canonical_revenue.load_won_deals(window, now=now)
    if not base.get("available"):
        return {
            "window": resolved, "campaign": campaign, "details": [],
            "revenue_source": canonical_revenue.CANONICAL_SOURCE,
            "revenue_scope": revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE,
            "revenue_available": False,
            "revenue_unavailable_reason": base.get("reason"),
            "legacy_fallback_used": False,
            "source_health": {"status": base.get("reason") or "database_unavailable"},
            "generated_at": generated_at,
        }

    rows = canonical_revenue.canonical_deal_rows(
        base, revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE)
    # Apply the canonical identity map (exact-normalized auto-links + approved
    # manual mappings) so raw deal labels resolve to the canonical campaign name.
    canonical = repo.fetch_canonical_campaign_spend(start_date, end_date)
    canonical_access = bool(canonical.get("available"))
    approved = (repo.fetch_campaign_identity(canonical.get("customer_id")).get("mappings", [])
                if canonical_access else [])
    resolution_map = (_build_resolution_map(canonical.get("rows") or [], approved)
                      if canonical_access else {})
    mapped = _apply_identity_map(rows, resolution_map, []) if resolution_map else rows

    target = _norm(campaign)
    details = [_deal_detail_row(r) for r in mapped if _norm(r.get("campaign_name")) == target]
    details.sort(
        key=lambda d: (d.get("deal_close_date") or "", d.get("deal_amount_usd") or 0.0),
        reverse=True,
    )
    return {
        "window": resolved,
        "campaign": campaign,
        "details": details,
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE,
        "revenue_available": True,
        "as_of": base.get("as_of"),
        "legacy_fallback_used": False,
        "source_health": {"status": "available"},
        "generated_at": generated_at,
    }


def _country_deal_detail_row(r: dict) -> dict:
    """One 'client / deal behind this country' detail row (PR-ADS-132).

    Only fields the durable tables actually store are populated; identity fields
    that are NOT stored (company record id, contact name, deal name) are explicit
    None so the UI renders "unavailable", never a fabricated id. A missing amount
    stays None (the drawer shows "unavailable"), never a fake $0.00.
    """
    return {
        # See `_deal_detail_row`: the ledger has the deal's name, not a company.
        "company_name": None,
        "company_record_id": None,
        "main_contact_name": None,          # contact name is not stored
        "main_contact_record_id": r.get("primary_contact_id") or None,
        "deal_name": r.get("deal_name") or None,
        "deal_record_id": r.get("deal_id") or None,
        # Preserve None for a missing amount — never fabricate a $0.00 the UI
        # would read as real revenue; the drawer renders it as "unavailable".
        "deal_amount_usd": (lambda a: round(a, 2) if a is not None else None)(
            _nullable_float(r.get("deal_amount_usd"))),
        "currency_status": r.get("currency_status"),
        "deal_close_date": r.get("deal_close_date"),
        # campaign_name is the CANONICAL display label (post identity map);
        # raw_campaign_name preserves the original external/UTM label.
        "campaign_name": r.get("campaign_name") or None,
        "raw_campaign_name": (r.get("raw_campaign_name")
                              or r.get("external_campaign_label")
                              or r.get("campaign_name")),
        "attribution_status": r.get("attribution_status") or None,
        "attribution_scope": r.get("attribution_scope") or None,
        "gclid_masked": _mask_gclid(r.get("gclid")),
        "country": r.get("country") or None,
        "deal_stage_label": r.get("deal_stage_label") or None,
    }


def build_country_deal_details(window: str, country: str,
                               country_code: str | None = None,
                               now: datetime | None = None) -> dict:
    """Closed-won client/deal detail rows behind ONE country (PR-ADS-132).

    Read-only validation drilldown for the ROAS by Country drawer. Fetches
    closed-won deals from the durable gclid_attribution table (windowed by
    deal_close_date) for EXACTLY the requested country — matched by ISO code when
    available, else by exact normalized name (never fuzzy / `contains`, so
    "United Arab Emirates" never leaks into "United Kingdom"). Applies the SAME
    canonical identity map the ROAS rows use so a deal's raw campaign label groups
    onto its canonical campaign name for display. Never writes anything; never
    fabricates company / contact / deal ids or amounts.

    Raises ValueError for an unsupported window.
    """
    resolved, start_date, end_date = _window_date_bounds(window, now)
    generated_at = datetime.now(timezone.utc).isoformat()
    code = (country_code or "").strip().upper() or None

    # PR-ADS-153E-B: canonical ledger at `google_ads_source` scope — the same
    # scope the ROAS by Country row aggregates. Country matching stays EXACT
    # (ISO code when available, else exact normalized name); no fuzzy matching
    # is introduced, so "United Arab Emirates" still cannot leak into
    # "United Kingdom".
    base = canonical_revenue.load_won_deals(window, now=now)
    if not base.get("available"):
        return {
            "window": resolved, "country": country, "country_code": code,
            "details": [],
            "summary": {"companies": None, "deals": None,
                        "won_revenue_usd": None, "customers": None},
            "revenue_source": canonical_revenue.CANONICAL_SOURCE,
            "revenue_scope": revenue_scope.SCOPE_GOOGLE_ADS_SOURCE,
            "revenue_available": False,
            "revenue_unavailable_reason": base.get("reason"),
            "legacy_fallback_used": False,
            "source_health": {"status": base.get("reason") or "database_unavailable"},
            "generated_at": generated_at,
        }

    # PR-ADS-153F: the drilldown resolves the requested country through the SAME
    # contract the ROAS row was built with and compares canonical keys. It used
    # to run its own third rule — normalized-name equality OR a separate code
    # resolution — so a drilldown could contain deals the row it opened from did
    # not count, and vice versa.
    #
    # Requesting the residual bucket EXPLICITLY (by its key or its label) returns
    # exactly the deals that bucket is built from, which is what makes the
    # residual row's numbers auditable rather than merely disclosed.
    #
    # A request for a label that is neither a supported country nor the residual
    # is refused rather than answered with the residual's contents. Every such
    # label resolves to the same residual key, so answering would return one
    # unidentifiable country's deals under another's name — a `contains`-style
    # leak by a different route, and the drilldown has never been allowed to do
    # that ("United Arab Emirates" must never leak into "United Kingdom").
    target = country_identity.resolve(name=country, code=code)
    asked_for_residual = (
        str(country or "").strip().lower() in (
            country_identity.RESIDUAL_KEY, country_identity.RESIDUAL_LABEL.lower())
        or str(code or "").strip().lower() == country_identity.RESIDUAL_KEY)
    if not target.is_country and not asked_for_residual:
        return {
            "window": resolved, "country": country, "country_code": code,
            "country_key": None, "details": [],
            "summary": {"companies": None, "deals": 0, "won_revenue_usd": None,
                        "customers": 0, "revenue_unavailable_deals": 0},
            "revenue_source": canonical_revenue.CANONICAL_SOURCE,
            "revenue_scope": revenue_scope.SCOPE_GOOGLE_ADS_SOURCE,
            "revenue_available": True,
            "country_status": target.status,
            "country_unresolved_reason": target.reason,
            "as_of": base.get("as_of"),
            "legacy_fallback_used": False,
            "source_health": {"status": "country_not_canonical"},
            "generated_at": generated_at,
        }

    target_key = target.key
    rows = [r for r in canonical_revenue.canonical_deal_rows(
        base, revenue_scope.SCOPE_GOOGLE_ADS_SOURCE)
        if country_identity.country_key(r.get("country"), r.get("country_code")) == target_key]
    # Preserve the RAW label before the canonical identity map overwrites it, so
    # the drawer can show both "Campaign / Source" (canonical) and the raw label.
    for r in rows:
        r.setdefault("raw_campaign_name", r.get("campaign_name"))
    # Apply the canonical identity map (exact-normalized auto-links + approved
    # manual mappings) so raw deal labels resolve to the canonical campaign name.
    canonical = repo.fetch_canonical_campaign_spend(start_date, end_date)
    canonical_access = bool(canonical.get("available"))
    approved = (repo.fetch_campaign_identity(canonical.get("customer_id")).get("mappings", [])
                if canonical_access else [])
    resolution_map = (_build_resolution_map(canonical.get("rows") or [], approved)
                      if canonical_access else {})
    mapped = _apply_identity_map(rows, resolution_map, []) if resolution_map else rows

    details = [_country_deal_detail_row(r) for r in mapped]
    # Sorted by amount (largest first, missing amounts last), then most recent close.
    details.sort(
        key=lambda d: (
            d.get("deal_amount_usd") is not None,
            d.get("deal_amount_usd") or 0.0,
            d.get("deal_close_date") or "",
        ),
        reverse=True,
    )

    deal_ids = {d["deal_record_id"] for d in details if d.get("deal_record_id")}
    amounts = [d["deal_amount_usd"] for d in details if d.get("deal_amount_usd") is not None]
    won_revenue = round(sum(amounts), 2) if amounts else None
    return {
        "window": resolved,
        "country": country,
        "country_code": code,
        "details": details,
        "summary": {
            # The ledger carries no company identity, so a distinct-company count
            # cannot be produced from it. Withheld rather than approximated with
            # a deal count that would silently mean something else.
            "companies": None,
            "deals": len(deal_ids),
            "won_revenue_usd": won_revenue,
            "customers": len(deal_ids),
            "revenue_unavailable_deals": sum(
                1 for d in details if d.get("deal_amount_usd") is None),
        },
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_GOOGLE_ADS_SOURCE,
        "revenue_available": True,
        "as_of": base.get("as_of"),
        "legacy_fallback_used": False,
        "source_health": {"status": "ready"},
        "generated_at": generated_at,
    }


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
    resolved, _start_date, _end_date = _window_date_bounds(window, now)

    canonical = canonical_revenue.load_won_deals(window, now=now)
    generated_at = datetime.now(timezone.utc).isoformat()

    if not canonical.get("available"):
        # The canonical ledger cannot be read, or its coverage is not proven —
        # distinct from safe-empty, and NOT a cue to fall back to
        # `gclid_attribution`: that table holds a narrower population, so a
        # fallback would quietly redefine "closed-won revenue" mid-incident.
        return {
            "window": resolved,
            "summary": {
                "deal_count": None,
                "won_revenue": None,
                "average_deal_value": None,
                "exact_gclid_count": None,
            },
            "deals": [],
            "source_health": {
                "ledger_status": canonical.get("reason") or "database_unavailable",
                "revenue_attribution_status": "canonical_ledger_unavailable",
                "violation_codes": canonical.get("violation_codes") or [],
            },
            "revenue_source": canonical_revenue.CANONICAL_SOURCE,
            "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
            "revenue_available": False,
            "as_of": canonical.get("as_of"),
            "legacy_fallback_used": False,
            "generated_at": generated_at,
        }

    rows = canonical_revenue.canonical_deal_rows(
        canonical, revenue_scope.SCOPE_ALL_SOURCE)
    totals = canonical_revenue.summarize_deals(
        canonical.get("deals"), revenue_scope.SCOPE_ALL_SOURCE)

    deals = []
    gclid_attributable = 0
    for row in rows:
        if row.get("attribution_scope") == revenue_scope.SCOPE_GCLID_ATTRIBUTABLE:
            gclid_attributable += 1
        deals.append({
            "deal_id": row.get("deal_id"),
            # The ledger stores the deal's own name; the contact employer the
            # legacy `company` column carried is not a deal field at all.
            "deal_name": row.get("deal_name"),
            "company": None,
            "country": row.get("country"),
            "campaign_name": row.get("campaign_name"),
            "deal_close_date": row.get("deal_close_date"),
            # Null when the currency was never proven — never summed as $0.
            "deal_amount_usd": row.get("deal_amount_usd"),
            "currency_status": row.get("currency_status"),
            "deal_stage_label": row.get("deal_stage_label"),
            "attribution_status": row.get("attribution_status"),
            "attribution_scope": row.get("attribution_scope"),
            "acquisition_group": row.get("acquisition_group"),
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
        # Withheld (None) when no deal in the window had a provable amount —
        # never a fake $0 standing in for "we could not resolve the currency".
        # An EMPTY window also reports None rather than $0: this contract has
        # always distinguished "no closed-won deals here" from "these deals were
        # worth nothing", and the cutover does not change that.
        "won_revenue": totals["revenue_usd"] if revenue_wired else None,
        "average_deal_value": (
            round(totals["revenue_usd"] / totals["revenue_deals"], 2)
            if totals["revenue_deals"] else None
        ),
        # Renamed from `exact_gclid_count`: this is the GCLID-attributable SUBSET
        # of an all-source population, and the lattice guarantees it is a subset.
        "exact_gclid_count": gclid_attributable,
        "gclid_attributable_deals": gclid_attributable,
        "currency_unavailable_deals": totals["currency_unavailable_deals"],
        "ambiguous_associations": totals["ambiguous_associations"],
        "failed_associations": totals["failed_associations"],
    }

    return {
        "window": resolved,
        "summary": summary,
        "deals": deals,
        "source_health": {
            "ledger_status": "available",
            "revenue_attribution_status": (
                canonical_revenue.CANONICAL_SOURCE if revenue_wired
                else "no_closed_won_deals_in_window"
            ),
        },
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "revenue_available": True,
        "as_of": canonical.get("as_of"),
        "attribution_coverage": canonical_revenue.get_scope_ladder(base=canonical),
        "legacy_fallback_used": False,
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
    # PR-ADS-153E-B: the audit asks the SAME contract the pages read, so it can
    # never report a window as revenue-safe using a lineage no page consumes.
    revenue_base = canonical_revenue.load_won_deals(window, now=now)

    lead_window_safe = bool(grain.get("lead_window_safe"))
    spend_window_safe = True   # geo.run_date is the per-day source date
    # The canonical ledger is windowed by the canonical deal close date, never a
    # scheduler run date.
    revenue_window_safe = bool(revenue_base.get("available"))

    date_grain_health = {
        "spend_date_field": "geo.run_date",
        "lead_date_field_current": "leads.contact_created_at",
        "lead_event_date_field_available": bool(grain.get("lead_event_date_field_available")),
        "deal_date_field": "hubspot_deal_ledger.deal_close_date",
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
    window_has_revenue = bool(revenue_base.get("deals"))
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
