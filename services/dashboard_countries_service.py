"""
Dashboard Countries & Geo Intelligence (PR-ADS-138)

The fifth Dashboard tab (Dashboard → Countries). It answers, in one screen:
which countries / markets are producing SQLs, customers and closed-won revenue,
and where is Google Ads spend present without business proof?

Composition doctrine — this service computes NO new business math, adds NO new
attribution model, and NEVER redistributes the unattributed residual into real
countries. It composes canonical read-only contracts:

  - build_revenue_decision_mart(view="country")   -> the canonical per-country
      rows (spend / SQLs / customers / won revenue / ROAS / mapping state) plus
      the shared spend-truth (native GBP + FX-gated USD, geo reconciliation
      status, and the explicit residual bucket) and readiness. This is the
      ROAS-by-Country truth, re-presented — never re-computed.
  - fetch_geo_daily_spend_by_country              -> per-country native (GBP)
      AND USD spend + per-country FX completeness, read directly from the
      canonical google_ads_geo_daily_spend table (the same source as the geo
      reconciliation). The only place with a per-country native/USD split.
  - fetch_revenue_deals                           -> the closed-won deal ledger
      (per-deal null-safe amounts) for drawer proof, null-amount detection, and
      to preserve closed-won revenue that maps to NO country under an explicit
      "Unknown / Unattributed country" residual — never forced onto a country.

Truth rules (unchanged, never loosened):
  - Google Ads API = spend truth (native GBP). HubSpot closed-won = revenue truth
    (USD). Native GBP is never labelled USD. Reporting revenue is USD.
  - Geo ROAS is shown ONLY when a country has real country-attributed Google Ads
    spend, FX is complete, revenue is safe, and geo reconciliation is unblockable
    (verified or reconciled-with-residual) — never from the Google Ads conversion
    value, never for the residual.
  - No fake $0 / 0.00x / 0%. A missing amount stays null; a total that needs
    completeness becomes Unavailable when any part is unknown; a missing prior
    period yields "No comparison".
  - The unattributed geo residual (spend Google Ads' geographic view does not
    assign to a country) and closed-won revenue that maps to NO country are
    PRESERVED in an explicit residual bucket — never distributed across real
    countries, never mapped, never given a ROAS, never a drawer.
  - Read-only. NO writes to Google Ads, HubSpot, budgets, bids, campaigns,
    keywords, negatives, or offline conversions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

# PR-ADS-154C: THE canonical window anchor — the Google Ads account
# calendar day, not UTC. Sharing `resolve_window` while disagreeing about
# which day "today" is meant two pages could resolve `current_quarter` to
# different quarters across the account's midnight and both call it "this
# quarter". Spend is denominated in the account day, so the account day wins.
from services.canonical_contract import resolve_canonical_window
from analysis import revenue_scope
from db import revenue_repository as repo
from services import canonical_revenue_service as canonical_revenue
from analysis import country_identity
from analysis.country_identity import get_country_code
from services import google_ads_geo_sync_service as geo_gate
from services.revenue_decision_mart import build_revenue_decision_mart

# Reuse the Overview tab's pure window/delta helpers (PR-ADS-134/135/136/137).
from services.dashboard_overview_service import (
    _coerce_utc,
    _delta_metric,
    _parse_iso_date,
    _previous_window_reference,
    _round2,
)

logger = logging.getLogger(__name__)


# ── Country status copy (business-facing) ────────────────────────────────────
STATUS_REVENUE_PROVEN = "Revenue proven"
STATUS_CUSTOMER_PROVEN = "Customer proven"
STATUS_SQL_PRODUCER = "SQL producer"
STATUS_SPEND_NO_PROOF = "Spend without customer proof"
STATUS_FX_UNAVAILABLE = "FX unavailable"
STATUS_GEO_REVIEW = "Geo attribution needs review"
STATUS_NO_SPEND = "No spend in window"

# The residual is NOT a country. PR-ADS-153F: the label now comes from the ONE
# country identity contract, so the dashboard, the mart, ROAS by Country and the
# drilldown cannot disagree about what the residual is called — the previous
# copy of this string here was a second place the name could drift.
RESIDUAL_LABEL = country_identity.RESIDUAL_LABEL


# ── Region mapping (presentation grouping only) ──────────────────────────────
# There is NO existing country_code -> region contract in the codebase, so this
# introduces one purely for presentation grouping — it adds no business math and
# nothing is mislabelled (an unresolved / unknown code lands in a clearly named
# "Unknown / Needs Review" lane, never guessed). Mexico is grouped with LATAM
# (commercial grouping); North-African markets are grouped with Africa.
REGION_NORTH_AMERICA = "North America"
REGION_EUROPE = "Europe"
REGION_MIDDLE_EAST = "Middle East"
REGION_LATAM = "LATAM"
REGION_AFRICA = "Africa"
REGION_APAC = "APAC"
REGION_UNKNOWN = "Unknown / Needs Review"

REGION_ORDER = [
    REGION_NORTH_AMERICA, REGION_EUROPE, REGION_MIDDLE_EAST,
    REGION_LATAM, REGION_AFRICA, REGION_APAC, REGION_UNKNOWN,
]

_COUNTRY_REGION = {
    # North America
    "US": REGION_NORTH_AMERICA, "CA": REGION_NORTH_AMERICA,
    # LATAM (Mexico grouped commercially with Latin America)
    "MX": REGION_LATAM, "BR": REGION_LATAM, "AR": REGION_LATAM, "CL": REGION_LATAM,
    "CO": REGION_LATAM, "PE": REGION_LATAM, "EC": REGION_LATAM, "VE": REGION_LATAM,
    "UY": REGION_LATAM, "PY": REGION_LATAM, "BO": REGION_LATAM, "PA": REGION_LATAM,
    "CR": REGION_LATAM, "GT": REGION_LATAM, "DO": REGION_LATAM,
    # Europe
    "GB": REGION_EUROPE, "IE": REGION_EUROPE, "FR": REGION_EUROPE, "DE": REGION_EUROPE,
    "ES": REGION_EUROPE, "PT": REGION_EUROPE, "IT": REGION_EUROPE, "NL": REGION_EUROPE,
    "BE": REGION_EUROPE, "LU": REGION_EUROPE, "CH": REGION_EUROPE, "AT": REGION_EUROPE,
    "PL": REGION_EUROPE, "CZ": REGION_EUROPE, "SK": REGION_EUROPE, "HU": REGION_EUROPE,
    "RO": REGION_EUROPE, "BG": REGION_EUROPE, "GR": REGION_EUROPE, "CY": REGION_EUROPE,
    "MT": REGION_EUROPE, "SE": REGION_EUROPE, "NO": REGION_EUROPE, "DK": REGION_EUROPE,
    "FI": REGION_EUROPE, "IS": REGION_EUROPE, "EE": REGION_EUROPE, "LV": REGION_EUROPE,
    "LT": REGION_EUROPE, "UA": REGION_EUROPE, "RU": REGION_EUROPE, "HR": REGION_EUROPE,
    "SI": REGION_EUROPE, "RS": REGION_EUROPE,
    # Middle East (Levant + Gulf + Turkey / Iran / Israel)
    "SA": REGION_MIDDLE_EAST, "AE": REGION_MIDDLE_EAST, "QA": REGION_MIDDLE_EAST,
    "KW": REGION_MIDDLE_EAST, "BH": REGION_MIDDLE_EAST, "OM": REGION_MIDDLE_EAST,
    "JO": REGION_MIDDLE_EAST, "LB": REGION_MIDDLE_EAST, "IQ": REGION_MIDDLE_EAST,
    "YE": REGION_MIDDLE_EAST, "SY": REGION_MIDDLE_EAST, "PS": REGION_MIDDLE_EAST,
    "IL": REGION_MIDDLE_EAST, "IR": REGION_MIDDLE_EAST, "TR": REGION_MIDDLE_EAST,
    # Africa (incl. North Africa)
    "EG": REGION_AFRICA, "MA": REGION_AFRICA, "DZ": REGION_AFRICA, "TN": REGION_AFRICA,
    "LY": REGION_AFRICA, "SD": REGION_AFRICA, "ZA": REGION_AFRICA, "NG": REGION_AFRICA,
    "KE": REGION_AFRICA, "GH": REGION_AFRICA, "ET": REGION_AFRICA, "TZ": REGION_AFRICA,
    "UG": REGION_AFRICA,
    # APAC
    "CN": REGION_APAC, "HK": REGION_APAC, "TW": REGION_APAC, "JP": REGION_APAC,
    "KR": REGION_APAC, "IN": REGION_APAC, "PK": REGION_APAC, "BD": REGION_APAC,
    "LK": REGION_APAC, "SG": REGION_APAC, "MY": REGION_APAC, "ID": REGION_APAC,
    "TH": REGION_APAC, "VN": REGION_APAC, "PH": REGION_APAC, "AU": REGION_APAC,
    "NZ": REGION_APAC,
}


def _region_for_code(code) -> str:
    if not code:
        return REGION_UNKNOWN
    return _COUNTRY_REGION.get(str(code).strip().upper(), REGION_UNKNOWN)


def _norm(label) -> str:
    """Normalise a country label for matching (lowercase, collapsed spaces)."""
    if not label:
        return ""
    return " ".join(str(label).replace("_", " ").strip().lower().split())


def _country_key(country_name, country_code) -> str | None:
    """Canonical join key for a country, via the ONE shared identity contract.

    PR-ADS-153F: this used to be ISO-code-first with a ``name:<normalized>``
    fallback, while ROAS by Country keyed on a bare lowercased string and the
    drilldown used a third rule. Three key spaces meant the same window could
    produce different country rows on pages that claim to describe the same
    thing, so the rule now lives in ``analysis.country_identity`` and every
    consumer calls it.

    Returns None for geography that is not an identified country, which this
    page's callers already treat as "goes to the residual bucket, never forced
    onto a real country". The residual key itself is
    ``analysis.country_identity.RESIDUAL_KEY``.
    """
    identity = country_identity.resolve(name=country_name, code=country_code)
    return identity.key if identity.is_country else None


def _roas(won, usd_spend):
    """Geo ROAS = won revenue / USD spend, only when both are safe.

    None when USD spend is unavailable/zero, revenue is unknown, OR revenue is a
    non-positive (zero) return — a spend-with-no-revenue country is conveyed by
    its status, never a fabricated / misleading 0.00x, and never from native GBP
    or the Google Ads conversion value.
    """
    if won is None or usd_spend is None or usd_spend <= 0 or won <= 0:
        return None
    return round(float(won) / float(usd_spend), 2)


# ── Per-country geo spend (native GBP + FX-gated USD) ─────────────────────────

def _merge_spend(existing: dict | None, entry: dict) -> dict:
    """Accumulate two geo-spend entries for the same country key.

    The geo source groups by criterion id; on the rare chance two criteria resolve
    to the same country we SUM (never last-wins, which would silently drop spend).
    USD is withheld (None) if EITHER side withheld it (FX gap); FX is complete only
    when both are.
    """
    if existing is None:
        return entry
    a_usd, b_usd = existing.get("usd"), entry.get("usd")
    return {
        "native": (existing.get("native") or 0.0) + (entry.get("native") or 0.0),
        "usd": None if (a_usd is None or b_usd is None) else a_usd + b_usd,
        "fx_complete": bool(existing.get("fx_complete")) and bool(entry.get("fx_complete")),
    }


def _geo_spend_by_country(geo_result: dict) -> dict:
    """Map canonical Google Ads geo spend rows by CANONICAL country key.

    Each entry carries native (GBP) spend, USD spend (None on any FX gap for that
    country), and FX completeness — the only per-country native/USD split.

    PR-ADS-153F: one map keyed on ``analysis.country_identity.country_key``,
    replacing the previous by_code AND by_name pair. Two maps meant a caller had
    to decide which to try first, and a row present in one but not the other
    produced a different answer depending on that order — a second country join
    rule hiding inside a lookup helper.

    Geo spend whose country cannot be identified is kept under
    ``residual``: it is real Google Ads spend and must stay in the account
    total, but it is not evidence about any particular market.
    """
    by_key: dict = {}
    residual: dict | None = None
    for r in (geo_result.get("rows") or []):
        entry = {
            "native": r.get("spend"),
            "usd": r.get("spend_usd"),
            "fx_complete": bool(r.get("fx_complete")),
        }
        identity = country_identity.resolve(name=r.get("country_name"),
                                            code=r.get("country_code"))
        if identity.is_country:
            by_key[identity.key] = _merge_spend(by_key.get(identity.key), entry)
        else:
            residual = _merge_spend(residual, entry)
    return {"by_key": by_key, "residual": residual}


# ── Closed-won deal proof (grouped by country; residual preserved) ────────────

def _deal_proof_row(deal: dict, attribution_status: str) -> dict:
    """One closed-won deal proof row for a country drawer.

    Only durably-stored fields are populated. Company id, main contact, contact
    id and the deal NAME are not persisted locally, so they are explicit None →
    the UI renders "Unavailable", never a fabricated value. A missing amount
    stays None (never a fake $0).
    """
    amount = deal.get("deal_amount_usd")
    return {
        "deal_id": deal.get("deal_id"),
        # PR-ADS-153E-B: the ledger stores the deal's own name and no company
        # field, so `company` is honestly Unavailable rather than mislabelled.
        "deal_name": deal.get("deal_name"),
        "company": None,
        "company_id": None,
        "main_contact": None,
        "contact_id": deal.get("primary_contact_id"),
        "deal": deal.get("deal_name"),
        "amount_usd": _round2(amount) if amount is not None else None,
        "currency_status": deal.get("currency_status"),
        "close_date": deal.get("deal_close_date"),
        "campaign_name": deal.get("campaign_name"),
        "country": deal.get("country"),
        "attribution_status": attribution_status,
        "attribution_scope": deal.get("attribution_scope"),
    }


def _deal_attribution(deal: dict, *, residual: bool) -> str:
    """PR-ADS-153E-B: canonical attribution state, not a GCLID match heuristic.

    ``ambiguous`` — contacts that disagree about the acquisition source — stays
    visible as ``needs_review`` rather than being resolved by picking one.
    """
    if residual:
        return "no_country"
    return ("attributed"
            if (deal.get("attribution_status") or "").strip().lower() == "attributed"
            else "needs_review")


def _group_deals_by_country(deals_rows: list) -> tuple[dict, set, list, bool]:
    """Group closed-won deals onto canonical country keys, else the residual.

    Returns (deals_by_key, unknown_amount_keys, residual_deals,
    residual_amount_unknown) where deals_by_key maps a country key to its proof
    rows, unknown_amount_keys is the set of keys holding a deal with an unknown
    amount (so that key's revenue total renders Unavailable, never a lowered $0),
    residual_deals are the closed-won deals that map to NO country, and
    residual_amount_unknown flags an unknown amount among the residual deals.
    """
    deals_by_key: dict = {}
    unknown_amount_keys: set = set()
    residual_deals: list = []
    residual_amount_unknown = False
    for deal in deals_rows or []:
        key = _country_key(deal.get("country"), get_country_code(deal.get("country")))
        if key is None:
            residual_deals.append(_deal_proof_row(deal, _deal_attribution(deal, residual=True)))
            if deal.get("deal_amount_usd") is None:
                residual_amount_unknown = True
            continue
        deals_by_key.setdefault(key, []).append(
            _deal_proof_row(deal, _deal_attribution(deal, residual=False)))
        if deal.get("deal_amount_usd") is None:
            unknown_amount_keys.add(key)
    return deals_by_key, unknown_amount_keys, residual_deals, residual_amount_unknown


# Google Ads geo spend mapping state (from the mart's spend_mapping) → UI label.
_MAPPING_STATUS = {"matched": "mapped", "unavailable": "mapping_unavailable"}


def _country_status(*, native_spend, usd_spend, fx_complete, sqls, customers,
                    won_revenue, attribution_status) -> str:
    """Business status for a country row (never an engineering term)."""
    has_spend = native_spend is not None and native_spend > 0
    spend_unknown = native_spend is None  # geo source unavailable (not a measured 0)
    if (customers or 0) > 0 and (won_revenue or 0) > 0:
        return STATUS_REVENUE_PROVEN
    if (customers or 0) > 0:
        # Customers exist but their revenue is not proven (unknown / zero amount).
        return STATUS_CUSTOMER_PROVEN
    if (sqls or 0) > 0:
        return STATUS_SQL_PRODUCER
    if has_spend:
        # Spend exists but no SQL / customer proof.
        if attribution_status == "mapping_unavailable":
            return STATUS_GEO_REVIEW
        if usd_spend is None and not fx_complete:
            return STATUS_FX_UNAVAILABLE
        return STATUS_SPEND_NO_PROOF
    if spend_unknown:
        # Spend could not be read (geo source down) — never asserted as a real "no
        # spend" measured zero.
        return STATUS_GEO_REVIEW
    return STATUS_NO_SPEND


def _build_country_rows(mart_real_rows: list, geo_maps: dict, geo_available: bool,
                        deals_by_key: dict, unknown_amount_keys: set,
                        country_roas_unblockable: bool, revenue_connected: bool,
                        native_currency: str = "GBP") -> list:
    """Merge mart country rows (outcomes + mapping) with the per-country native/
    USD geo spend split and the closed-won proof deals. The residual is handled
    separately — this builds ONLY real countries, never the residual.

    When the revenue integration is disconnected, closed-won customers AND revenue
    are unknown, so they (and ROAS) are blanked to None here — BEFORE the status is
    derived — so a row can never claim "Revenue proven" over Unavailable numbers.
    Spend / SQLs survive (independent of the revenue integration)."""
    rows = []
    for m in mart_real_rows or []:
        country_name = m.get("country") or "Unknown"
        country_code = m.get("country_code")
        # PR-ADS-153F: ONE lookup on the canonical key. The mart row and the geo
        # row now resolve through the same contract, so a country cannot be
        # matched by name on one page and by code on another.
        key = _country_key(country_name, country_code)
        spend = geo_maps["by_key"].get(key) if key else None
        if spend is not None:
            native_spend = spend.get("native")
            usd_spend = spend.get("usd")
            fx_complete = bool(spend.get("fx_complete"))
        elif geo_available:
            # The geo table was read successfully and this country is absent from
            # it → it genuinely has no Google Ads geo spend in this window. That is
            # a measured zero (revenue may still be proven from another channel),
            # not a withheld value.
            native_spend = 0.0
            usd_spend = 0.0
            fx_complete = True
        else:
            # The geo spend source could not be read → spend is Unavailable, never
            # a fabricated 0.
            native_spend = None
            usd_spend = None
            fx_complete = False

        amount_unknown = key is not None and key in unknown_amount_keys
        if not revenue_connected:
            # Customers AND revenue come from the revenue integration → unknown.
            customers = None
            won = None
        else:
            customers = m.get("customers")
            # Won revenue: the mart's canonical per-country total, blanked to None
            # if any of this country's deals has an unknown amount (never a lowered $0).
            won = None if amount_unknown else _round2(m.get("won_revenue"))

        attribution_status = _MAPPING_STATUS.get(m.get("spend_mapping"), "mapping_unavailable")
        # ROAS only when this country has real attributed spend, FX is complete,
        # revenue is safe, AND geo reconciliation is unblockable for the account.
        row_roas = None
        if country_roas_unblockable and (native_spend or 0) > 0:
            row_roas = _roas(won, usd_spend)

        status = _country_status(
            native_spend=native_spend, usd_spend=usd_spend, fx_complete=fx_complete,
            sqls=m.get("sqls"), customers=customers, won_revenue=won,
            attribution_status=attribution_status)

        # PR-ADS-153F: the row publishes the canonical key and the canonical code
        # resolved from it, so an API consumer joins on identity rather than on a
        # display label. `country_name` stays the presentation label.
        identity = country_identity.resolve(name=country_name, code=country_code)
        rows.append({
            "country_key": key,
            "country_code": identity.code if identity.is_country else country_code,
            "country_name": identity.label if identity.is_country else country_name,
            "region": _region_for_code(identity.code if identity.is_country else country_code),
            "spend_native": native_spend,
            "native_currency": native_currency,
            "spend_usd": usd_spend,
            "fx_complete": fx_complete,
            "sqls": m.get("sqls"),
            "customers": customers,
            "won_revenue_usd": won,
            "roas": row_roas,
            "roas_available": row_roas is not None,
            "status": status,
            "attribution_status": attribution_status,
            "top_campaign": m.get("top_campaign"),
            "deals": (deals_by_key.get(key) or []) if key is not None else [],
            "is_residual": False,
            "target_page": "roas-countries",
        })

    # Real countries first: revenue, then customers, then spend.
    rows.sort(key=lambda r: (
        r.get("won_revenue_usd") or 0.0,
        r.get("customers") or 0,
        r.get("spend_native") or 0.0,
    ), reverse=True)
    return rows


def _residual_gap(total, real_rows: list, key: str):
    """Canonical residual for one metric = window total − Σ(real country rows).

    None when the total is unknown or ANY real row withholds the metric (the
    residual cannot be derived by subtraction from an incomplete set). Never a
    negative (clamped to 0 to absorb rounding)."""
    if total is None:
        return None
    if any(r.get(key) is None for r in real_rows):
        return None
    gap = total - sum((r.get(key) or 0) for r in real_rows)
    return gap if gap > 0 else 0


def _build_residual(summary: dict, real_rows: list, spend_truth: dict,
                    mart_residual_row: dict | None, residual_amount_unknown: bool,
                    revenue_connected: bool) -> dict:
    """The explicit 'Unknown / Unattributed country' bucket — the CANONICAL residual.

    Residual customers / revenue / SQLs are derived from the canonical Revenue
    Decision Mart (its window TOTAL minus the sum of the real country rows), NOT
    from the drawer deal ledger — so they SURVIVE a deal-ledger outage instead of
    collapsing to a fake $0 / empty. The deal ledger is used ONLY to detect an
    unknown residual deal amount (which then withholds the residual revenue total,
    never a lowered $0). The geo-spend residual (spend the geographic view does not
    assign to a country) is carried from spend_truth (with the mart residual row as
    a fallback). It is never mapped, never given a ROAS, never a drawer.
    """
    if not revenue_connected:
        # Customers AND revenue come from the revenue integration → unknown.
        customers = None
        won = None
        sqls = None
    else:
        gap = _residual_gap(summary.get("customers"), real_rows, "customers")
        customers = None if gap is None else int(round(gap))
        gap_sqls = _residual_gap(summary.get("sqls"), real_rows, "sqls")
        sqls = None if gap_sqls is None else int(round(gap_sqls))
        # Revenue: canonical mart residual, withheld if a residual deal has an
        # unknown amount (the mart total counts it as $0, so the derived residual
        # would be lowered) — never a fabricated / lowered $0.
        if residual_amount_unknown:
            won = None
        else:
            won = _round2(_residual_gap(summary.get("won_revenue_usd"), real_rows, "won_revenue_usd"))

    native = spend_truth.get("country_residual_native")
    usd = spend_truth.get("country_residual_usd")
    if native is None and mart_residual_row is not None:
        native = mart_residual_row.get("spend_native")
        usd = mart_residual_row.get("spend_usd")

    reason_bits = []
    if spend_truth.get("country_residual_reason"):
        reason_bits.append(spend_truth["country_residual_reason"])
    reason_bits.append("Closed-won revenue or customers that could not be safely assigned "
                       "to a country — preserved separately, never spread across real countries.")
    return {
        "label": RESIDUAL_LABEL,
        "customers": customers,
        "won_revenue_usd": won,
        "sqls": sqls,
        "spend_native": native,
        "spend_usd": usd,
        "native_currency": spend_truth.get("native_currency") or "GBP",
        "reason": " ".join(reason_bits),
        "roas": None,
        "roas_available": False,
        "drawer_available": False,
    }


def _build_kpis(country_rows: list, residual: dict, spend_truth: dict,
                country_roas_unblockable: bool, geo_available: bool,
                revenue_connected: bool) -> dict:
    """Hero KPIs derived from the real country rows (so they reconcile with the
    leaderboard / map) plus the canonical spend truth. The residual is surfaced
    separately, never in the ROAS numerator.

    ``geo_available`` / ``revenue_connected`` gate the totals so an EMPTY leaderboard
    (a source outage, where ``any([])`` would otherwise be False) never fabricates a
    $0 / 0-customer total presented as measured truth."""
    countries_with_spend = sum(1 for r in country_rows if (r.get("spend_native") or 0) > 0)
    countries_with_customers = sum(1 for r in country_rows if (r.get("customers") or 0) > 0)

    # Verified geo spend: summed native across real countries (excludes residual).
    # Unavailable if the geo source is down, or ANY country's spend is unknown
    # (never a partial or empty-outage total presented as complete).
    spend_incomplete = (not geo_available) or any(r.get("spend_native") is None for r in country_rows)
    verified_native = (None if spend_incomplete
                       else _round2(sum((r.get("spend_native") or 0.0) for r in country_rows)))
    # USD: Unavailable if any country with spend has USD withheld (FX incomplete).
    usd_incomplete = any(r.get("spend_usd") is None for r in country_rows
                         if (r.get("spend_native") or 0) > 0)
    verified_usd = (None if (spend_incomplete or usd_incomplete)
                    else _round2(sum((r.get("spend_usd") or 0.0) for r in country_rows)))

    sqls_incomplete = any(r.get("sqls") is None for r in country_rows)
    total_sqls = None if sqls_incomplete else sum((r.get("sqls") or 0) for r in country_rows)

    # Customers / revenue are Unavailable when the revenue integration is
    # disconnected (covers the empty-rows case, where per-row blanking has nothing
    # to blank) or when any contributing row withholds them.
    customers_incomplete = (not revenue_connected) or any(r.get("customers") is None for r in country_rows)
    total_customers = (None if customers_incomplete
                       else sum((r.get("customers") or 0) for r in country_rows))

    revenue_incomplete = (not revenue_connected) or any(r.get("won_revenue_usd") is None for r in country_rows)
    total_won = (None if revenue_incomplete
                 else _round2(sum((r.get("won_revenue_usd") or 0.0) for r in country_rows)))

    geo_roas = _roas(total_won, verified_usd) if country_roas_unblockable else None
    return {
        "countries_with_spend": countries_with_spend,
        "countries_with_customers": countries_with_customers,
        "verified_spend_native": {"amount": verified_native,
                                  "currency": spend_truth.get("native_currency") or "GBP"},
        "verified_spend_usd": verified_usd,
        "sqls": total_sqls,
        "customers": total_customers,
        "won_revenue_usd": total_won,
        "geo_roas": geo_roas,
        "residual_revenue_usd": residual.get("won_revenue_usd"),
        "residual_customers": residual.get("customers"),
    }


def _build_regional_mix(country_rows: list, country_roas_unblockable: bool) -> list:
    """Aggregate real countries into regions (presentation grouping only)."""
    buckets: dict = {}
    for r in country_rows:
        region = r.get("region") or REGION_UNKNOWN
        b = buckets.setdefault(region, {
            "region": region, "countries": 0,
            "spend_native": 0.0, "spend_native_incomplete": False,
            "spend_usd": 0.0, "spend_usd_incomplete": False,
            "sqls": 0, "sqls_incomplete": False,
            "customers": 0, "customers_incomplete": False,
            "won_revenue_usd": 0.0, "revenue_incomplete": False,
        })
        b["countries"] += 1
        if r.get("spend_native") is None:
            b["spend_native_incomplete"] = True
        else:
            b["spend_native"] += r["spend_native"]
        if (r.get("spend_native") or 0) > 0 and r.get("spend_usd") is None:
            b["spend_usd_incomplete"] = True
        else:
            b["spend_usd"] += (r.get("spend_usd") or 0.0)
        if r.get("sqls") is None:
            b["sqls_incomplete"] = True
        else:
            b["sqls"] += r["sqls"]
        if r.get("customers") is None:
            b["customers_incomplete"] = True
        else:
            b["customers"] += r["customers"]
        if r.get("won_revenue_usd") is None:
            b["revenue_incomplete"] = True
        else:
            b["won_revenue_usd"] += r["won_revenue_usd"]

    out = []
    for region in REGION_ORDER:
        b = buckets.get(region)
        if not b:
            continue
        native = None if b["spend_native_incomplete"] else _round2(b["spend_native"])
        usd = None if (b["spend_native_incomplete"] or b["spend_usd_incomplete"]) else _round2(b["spend_usd"])
        sqls = None if b["sqls_incomplete"] else b["sqls"]
        customers = None if b["customers_incomplete"] else b["customers"]
        won = None if b["revenue_incomplete"] else _round2(b["won_revenue_usd"])
        roas = _roas(won, usd) if country_roas_unblockable else None
        status = (STATUS_REVENUE_PROVEN if (customers or 0) > 0 and (won or 0) > 0
                  else (STATUS_CUSTOMER_PROVEN if (customers or 0) > 0
                        else (STATUS_SQL_PRODUCER if (sqls or 0) > 0
                              else (STATUS_SPEND_NO_PROOF if (native or 0) > 0
                                    else STATUS_NO_SPEND))))
        out.append({
            "region": region,
            "countries": b["countries"],
            "spend_native": native,
            "native_currency": country_rows[0].get("native_currency") if country_rows else "GBP",
            "spend_usd": usd,
            "sqls": sqls,
            "customers": customers,
            "won_revenue_usd": won,
            "roas": roas,
            "roas_available": roas is not None,
            "status": status,
        })
    out.sort(key=lambda r: (
        r.get("won_revenue_usd") or 0.0,
        r.get("customers") or 0,
        r.get("spend_native") or 0.0,
    ), reverse=True)
    return out


def _fmt_usd_short(value) -> str:
    if value is None:
        return "Unavailable"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "Unavailable"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:.0f}"


def _build_decision_cards(country_rows: list, residual: dict, kpis: dict,
                          spend_truth: dict, deal_proof_available: bool) -> list:
    cards = []

    # Scale — the top revenue-proven country.
    proven = [r for r in country_rows if r["status"] == STATUS_REVENUE_PROVEN]
    if proven:
        top = max(proven, key=lambda r: (r.get("won_revenue_usd") or 0.0,
                                         r.get("customers") or 0))
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": f"{top['country_name']} has revenue proof",
            "body": (f"{top['country_name']} produced {top['customers']} customer"
                     f"{'s' if top['customers'] != 1 else ''} and "
                     f"{_fmt_usd_short(top['won_revenue_usd'])} closed-won revenue this "
                     "window."),
            "target_page": "roas-countries", "target_label": "Open ROAS by Country",
        })
    else:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "No country has revenue proof yet",
            "body": "No market has both closed-won customers and proven revenue in "
                    "this window.",
            "target_page": "roas-countries", "target_label": "Open ROAS by Country",
        })

    # Watch — spend without customer proof.
    no_proof = [r for r in country_rows if r["status"] in
                (STATUS_SPEND_NO_PROOF, STATUS_SQL_PRODUCER, STATUS_FX_UNAVAILABLE)
                and (r.get("spend_native") or 0) > 0 and (r.get("customers") or 0) == 0]
    if no_proof:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "Markets spending without customer proof",
            "body": (f"{len(no_proof)} countr{'ies' if len(no_proof) != 1 else 'y'} spent "
                     "Google Ads budget but produced no customers in this window. Review "
                     "before scaling."),
            "target_page": "roas-countries", "target_label": "Open ROAS by Country",
        })
    else:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "No unproven market spend",
            "body": "Every spending market produced at least one customer, or has no "
                    "spend to judge.",
            "target_page": "roas-countries", "target_label": "Open ROAS by Country",
        })

    # Investigate — geo attribution review, or revenue that maps to no country.
    geo_ok = geo_gate.country_geo_ready(spend_truth.get("country_spend_status"))
    residual_rev = residual.get("won_revenue_usd")
    residual_customers = residual.get("customers")
    if not geo_ok:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Geo attribution needs review",
            "body": ("Google Ads geographic spend does not reconcile with the canonical "
                     "campaign spend for this window, so country ROAS is withheld. Native "
                     "spend is still shown, never a fabricated USD or ROAS."),
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })
    elif (residual_customers or 0) > 0 or (residual_rev or 0) > 0:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Closed-won revenue with no country",
            "body": (f"{_fmt_usd_short(residual_rev)} closed-won revenue could not be "
                     "attributed to a country. It is preserved separately, never spread "
                     "across markets. Review attribution."),
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })
    else:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "No geo attribution flags",
            "body": "Geo spend reconciles and all closed-won revenue maps to a country "
                    "this window.",
            "target_page": "roas-countries", "target_label": "Open ROAS by Country",
        })

    # Unavailable — explain WHY geo ROAS / USD spend is withheld, ACCURATELY.
    fx_ok = spend_truth.get("fx_status") == "verified"
    usd_present = kpis.get("verified_spend_usd") is not None
    revenue_known = kpis.get("won_revenue_usd") is not None
    if not deal_proof_available:
        headline = "Deal proof unavailable"
        body = ("The closed-won deal ledger could not be read, so country deal/client "
                "proof is withheld. Country revenue may still be canonical from the mart.")
    elif not geo_ok:
        headline = "Geo ROAS unavailable"
        body = ("Country ROAS is withheld because Google Ads geographic spend does not "
                "reconcile with the canonical campaign spend — never a fabricated ROAS.")
    elif not fx_ok or not usd_present:
        headline = "USD spend & Geo ROAS unavailable"
        body = ("USD spend and Geo ROAS are withheld because FX coverage is incomplete — "
                "native GBP spend is still shown, never a fabricated USD or ROAS.")
    elif not revenue_known:
        headline = "Geo ROAS unavailable"
        body = ("Geo ROAS is withheld because some closed-won revenue is incomplete (an "
                "unknown deal amount) — never a lowered $0.")
    elif kpis.get("geo_roas") is None:
        headline = "Geo ROAS not yet meaningful"
        body = ("There is no country-attributed closed-won revenue yet to measure against "
                "this window's verified spend, so Geo ROAS is not shown — never a "
                "fabricated 0.00x.")
    else:
        headline = "Country truth verified"
        body = ("Spend, FX, revenue and geo reconciliation are verified for this window. "
                "The residual is isolated and never distributed across countries.")
    cards.append({
        "type": "unavailable", "title": "Unavailable",
        "headline": headline, "body": body,
        "target_page": "revenue-health", "target_label": "Open Revenue Health",
    })
    return cards


def _build_truth_status(spend_truth: dict, readiness: dict,
                        deal_proof_available: bool) -> dict:
    fx = spend_truth.get("fx_status")
    country_spend = spend_truth.get("country_spend_status")

    if country_spend == "verified":
        spend = "ready"
    elif country_spend in ("reconciled_with_residual",):
        spend = "ready"
    elif country_spend == "mismatch":
        spend = "partial"
    else:
        spend = "unavailable"

    if country_spend == "verified":
        geo_attribution = "ready"
    elif country_spend == "reconciled_with_residual":
        # Geo reconciles with an explicit, isolated residual — still ready.
        geo_attribution = "ready"
    elif country_spend == "mismatch":
        geo_attribution = "partial"
    else:
        geo_attribution = "unavailable"

    # Residual isolation is a first-class truth: it is "ready" whenever the geo
    # spend is unblockable (the residual is captured, never spread); otherwise the
    # residual cannot be cleanly isolated.
    residual = "ready" if geo_gate.country_geo_ready(country_spend) else "partial"

    return {
        "spend": spend,
        "fx": "ready" if fx == "verified" else ("partial" if fx == "incomplete" else "unavailable"),
        "revenue": "ready" if readiness.get("revenue_integration_connected") else "blocked",
        "geo_attribution": geo_attribution,
        "residual": residual,
        # Client/deal proof comes from the closed-won deal ledger; when that is
        # down we cannot prove countries even if the mart revenue is canonical.
        "deal_proof": "ready" if deal_proof_available else "unavailable",
    }


def _build_unavailable(kpis: dict, spend_truth: dict, period_change: dict,
                       deal_proof_available: bool) -> list:
    out = []
    if kpis.get("verified_spend_usd") is None:
        out.append({"metric": "verified_spend_usd",
                    "reason": "USD spend requires verified FX; native GBP spend is shown instead."})
    if kpis.get("geo_roas") is None:
        geo_ok = geo_gate.country_geo_ready(spend_truth.get("country_spend_status"))
        fx_ok = spend_truth.get("fx_status") == "verified"
        usd_present = kpis.get("verified_spend_usd") is not None
        # PR-ADS-153F §8: name the ACTUAL gap. The gate is holistic now, so
        # `unavailable` covers an unreadable baseline, incomplete FX and a geo
        # range nobody fetched alike — telling all three operators to "re-run
        # geo sync" would send two of them to fix the wrong thing.
        gap_reason = geo_gate.describe_geo_gap(spend_truth.get("country_gap_codes"))
        if not geo_ok:
            roas_reason = gap_reason or (
                "Geo ROAS requires Google Ads geographic spend that reconciles "
                "with the canonical campaign spend — it does not for this window.")
        elif not fx_ok or not usd_present:
            roas_reason = ("Geo ROAS requires verified FX / USD spend; native GBP spend is "
                           "shown instead.")
        elif kpis.get("won_revenue_usd") is None:
            roas_reason = ("Geo ROAS requires complete revenue — an unknown deal amount "
                           "makes it incomplete.")
        else:
            roas_reason = "Geo ROAS not yet meaningful; no country-attributed closed-won revenue."
        out.append({"metric": "geo_roas", "reason": roas_reason})
    if not deal_proof_available:
        out.append({"metric": "deal_proof",
                    "reason": "Closed-won deal proof is unavailable — the deal ledger could "
                              "not be read. Country revenue may still be canonical from the mart."})
    if kpis.get("sqls") is None:
        out.append({"metric": "sqls",
                    "reason": "SQLs withheld — lead business event dates are unavailable."})
    # Residual revenue: withheld either because the revenue integration is
    # disconnected (customers also unknown) or because a residual closed-won deal
    # has an unknown amount (customers still countable). Use the exact KPI field
    # name so a client can attach the reason to kpis.residual_revenue_usd.
    if kpis.get("residual_revenue_usd") is None:
        if kpis.get("residual_customers") is None:
            residual_reason = ("Revenue with no country is withheld — the revenue integration "
                               "is disconnected.")
        else:
            residual_reason = ("Residual revenue requires complete amounts — an unknown closed-won "
                               "deal amount with no country makes it incomplete (never a lowered $0).")
        out.append({"metric": "residual_revenue_usd", "reason": residual_reason})
    if not period_change.get("available"):
        out.append({"metric": "period_change",
                    "reason": period_change.get("reason") or "No comparison available."})
    return out


# ── Assembly (shared by the main build and the previous-period comparison) ────

def _assemble(window: str, now: datetime | None) -> dict:
    """Fetch + merge the country contract for one window (no period comparison).

    Returns the pieces both the main payload and the previous-period delta need,
    so the comparison measures like-for-like (country-attributed) figures.
    """
    mart = build_revenue_decision_mart(view="country", window=window, now=now)
    window_block = mart.get("window") or {}
    mart_rows = mart.get("rows") or []
    summary = mart.get("summary") or {}
    spend_truth = mart.get("spend_truth") or {}
    readiness = mart.get("readiness") or {}
    revenue_connected = bool(readiness.get("revenue_integration_connected"))
    native_currency = spend_truth.get("native_currency") or "GBP"
    country_roas_unblockable = bool(spend_truth.get("country_roas_unblockable"))

    # The residual is an appended mart row (is_residual). It is NOT a country —
    # extract it (its geo-spend residual) BEFORE filtering the real countries. The
    # canonical residual REVENUE / CUSTOMERS come from the mart window total minus
    # the real country rows (see _build_residual), never the drawer deal ledger.
    mart_residual_row = next((r for r in mart_rows if r.get("is_residual")), None)
    mart_real_rows = [r for r in mart_rows if not r.get("is_residual")]

    start = _parse_iso_date(window_block.get("start_date"))
    end = _parse_iso_date(window_block.get("end_date"))

    geo = repo.fetch_geo_daily_spend_by_country(start, end)
    geo_available = bool(geo.get("available"))
    geo_maps = _geo_spend_by_country(geo)

    # PR-ADS-153E-B: the deals proving each country row come from the canonical
    # ledger at `all_source` scope. Deals that map to NO country keep their place
    # in the explicit residual bucket — they are never dropped to make the named
    # country rows reconcile, and a won deal with no advertising attribution is
    # still part of the business.
    canonical = canonical_revenue.load_won_deals(window, now=now)
    deal_proof_available = bool(canonical.get("available"))
    deal_rows = (canonical_revenue.canonical_deal_rows(
        canonical, revenue_scope.SCOPE_ALL_SOURCE)
        if deal_proof_available else [])
    (deals_by_key, unknown_amount_keys,
     residual_deals, residual_amount_unknown) = _group_deals_by_country(deal_rows)

    country_rows = _build_country_rows(
        mart_real_rows, geo_maps, geo_available, deals_by_key, unknown_amount_keys,
        country_roas_unblockable, revenue_connected, native_currency)

    residual = _build_residual(summary, country_rows, spend_truth, mart_residual_row,
                               residual_amount_unknown, revenue_connected)
    kpis = _build_kpis(country_rows, residual, spend_truth, country_roas_unblockable,
                       geo_available, revenue_connected)

    return {
        "window_block": window_block,
        "spend_truth": spend_truth,
        "readiness": readiness,
        "country_rows": country_rows,
        "residual": residual,
        "kpis": kpis,
        "country_roas_unblockable": country_roas_unblockable,
        "deal_proof_available": deal_proof_available,
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "revenue_unavailable_reason": (None if deal_proof_available
                                       else canonical.get("reason")),
        "revenue_violation_codes": canonical.get("violation_codes") or [],
        "as_of": canonical.get("as_of"),
        "legacy_fallback_used": False,
        # PR-ADS-153F §8: the shared disclosure block — sources, scope, exact UTC
        # window bounds, freshness, coverage, reconciliation, residual and
        # whether the residual was accepted. Built by the geo gate so this page
        # and ROAS by Country disclose the SAME facts in the same shape.
        "country_truth": geo_gate.build_country_truth_disclosure(
            spend_truth, window_block,
            revenue_source=canonical_revenue.CANONICAL_SOURCE,
            revenue_scope=revenue_scope.SCOPE_ALL_SOURCE,
            revenue_available=deal_proof_available,
            revenue_reason=canonical.get("reason"),
            revenue_violation_codes=canonical.get("violation_codes"),
            as_of=canonical.get("as_of"),
        ),
    }


def _build_period_change(window: str, now: datetime, kpis: dict) -> dict:
    """Previous-period deltas via a shifted country assembly. Never a fake 0%."""
    ref = _previous_window_reference(window, now)
    if ref is None:
        return {"available": False, "label": None, "note": None, "metrics": {},
                "reason": "All Time has no previous period to compare against."}
    prev_key, prev_now, label, note = ref
    try:
        prev = _assemble(prev_key, prev_now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dashboard countries previous-period build failed: %s", exc)
        return {"available": False, "label": label, "note": note, "metrics": {},
                "reason": "Previous period unavailable."}
    pk = prev["kpis"]
    metrics = {
        "won_revenue_usd": _delta_metric(kpis.get("won_revenue_usd"), pk.get("won_revenue_usd")),
        "customers": _delta_metric(kpis.get("customers"), pk.get("customers")),
        "sqls": _delta_metric(kpis.get("sqls"), pk.get("sqls")),
        "verified_spend_usd": _delta_metric(kpis.get("verified_spend_usd"), pk.get("verified_spend_usd")),
        "geo_roas": _delta_metric(kpis.get("geo_roas"), pk.get("geo_roas")),
    }
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label, "note": note, "metrics": metrics, "reason": None,
    }


def build_dashboard_countries(window: str = "current_quarter",
                              now: datetime | None = None) -> dict:
    """Build the Dashboard Countries & Geo Intelligence contract for one window.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolve_canonical_window(window, now=now)
    ref_now = _coerce_utc(now)

    core = _assemble(window, now)
    spend_truth = core["spend_truth"]
    readiness = core["readiness"]
    country_rows = core["country_rows"]
    residual = core["residual"]
    kpis = core["kpis"]
    deal_proof_available = core["deal_proof_available"]

    period_change = _build_period_change(window, ref_now, kpis)
    regional_mix = _build_regional_mix(country_rows, core["country_roas_unblockable"])
    decision_cards = _build_decision_cards(country_rows, residual, kpis, spend_truth,
                                           deal_proof_available)
    truth_status = _build_truth_status(spend_truth, readiness, deal_proof_available)
    unavailable = _build_unavailable(kpis, spend_truth, period_change, deal_proof_available)

    return {
        "window": core["window_block"],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        "source_truth": "revenue_decision_mart_country_view",
        # PR-ADS-153E-B: geography and spend stay Google Ads; the closed-won
        # deals behind every country row are the canonical ledger.
        "revenue_source": core["revenue_source"],
        "revenue_scope": core["revenue_scope"],
        "revenue_available": deal_proof_available,
        "revenue_unavailable_reason": core["revenue_unavailable_reason"],
        "revenue_violation_codes": core["revenue_violation_codes"],
        "as_of": core["as_of"],
        "legacy_fallback_used": False,
        "google_ads_conversion_value_used": False,
        # Closed-won deal PROOF availability (separate from mart revenue truth):
        # false when the deal ledger could not be read, so drawers must not claim
        # a country has no deals.
        "deal_proof_available": deal_proof_available,
        "kpis": kpis,
        "period_change": period_change,
        "countries": country_rows,
        "residual": residual,
        "regional_mix": regional_mix,
        "decision_cards": decision_cards,
        "truth_status": truth_status,
        "readiness": readiness,
        "unavailable": unavailable,
    }
