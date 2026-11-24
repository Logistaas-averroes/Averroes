"""
Dashboard Countries & Geo Intelligence (PR-ADS-138)

The fifth Dashboard tab (Dashboard → Countries). It answers, in one screen:
which countries / markets are producing SQLs, customers and closed-won revenue,
and where is Google Ads spend present without business proof?

Composition doctrine — this service computes NO new business math, adds NO new
attribution model, and NEVER redistributes the unattributed residual into real
countries. It composes canonical read-only contracts:

  - build_revenue_decision_mart(view="country")   -> the canonical per-country
      rows (spend / SQLs / customers / won revenue / period ratio / mapping state) plus
      the shared spend-truth (native GBP + FX-gated USD, geo reconciliation
      status, and the explicit residual bucket) and readiness. This is the
      country revenue & spend truth, re-presented — never re-computed.
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
  - Geo period revenue / ad spend is shown ONLY when a country has real country-attributed Google Ads
    spend, FX is complete, revenue is safe, and geo reconciliation is unblockable
    (verified or reconciled-with-residual) — never from the Google Ads conversion
    value, never for the residual.
  - No fake $0 / 0.00x / 0%. A missing amount stays null; a total that needs
    completeness becomes Unavailable when any part is unknown; a missing prior
    period yields "No comparison".
  - The unattributed geo residual (spend Google Ads' geographic view does not
    assign to a country) and closed-won revenue that maps to NO country are
    PRESERVED in an explicit residual bucket — never distributed across real
    countries, never mapped, never given a spend ratio, never a drawer.
  - Read-only. NO writes to Google Ads, HubSpot, budgets, bids, campaigns,
    keywords, negatives, or offline conversions.
"""

from __future__ import annotations

from analysis import closed_won_truth as cwt  # PR-ADS-161E
from services import canonical_revenue_service as _canonical_snapshot  # PR-ADS-161E: one snapshot per page
import logging
from datetime import datetime, timezone

# PR-ADS-154C: THE canonical window anchor — the Google Ads account
# calendar day, not UTC. Sharing `resolve_window` while disagreeing about
# which day "today" is meant two pages could resolve `current_quarter` to
# different quarters across the account's midnight and both call it "this
# quarter". Spend is denominated in the account day, so the account day wins.
from services.canonical_contract import resolve_canonical_window
from services import canonical_contract
from analysis import revenue_scope
from db import revenue_repository as repo
from services import canonical_revenue_service as canonical_revenue
from analysis import country_identity
from analysis.country_identity import get_country_code
from services import google_ads_geo_sync_service as geo_gate
from services import country_spend_zero_rule as zero_rule  # PR-ADS-176A
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
# PR-ADS-176A: these two are computed from the market's closed-won DEAL count,
# so neither says "customer". A won deal is not a customer (PR-ADS-161E): a
# customer is a distinct HubSpot company from the reader contract, and no
# status here is computed from companies.
STATUS_WON_DEAL_REVENUE_UNPROVEN = "Won deal, revenue not proven"
STATUS_SQL_PRODUCER = "SQL producer"
STATUS_SPEND_NO_WON_DEAL = "Spend without a won deal"
STATUS_FX_UNAVAILABLE = "FX unavailable"
STATUS_GEO_REVIEW = "Geo attribution needs review"
STATUS_NO_SPEND = "No spend in window"
# PR-ADS-161E: the market's closed-won deal count is withheld or unreadable —
# unknown, so the market is not judged on it.
STATUS_OUTCOME_WITHHELD = "Closed-won outcome withheld"


def _deal_proof_reason_text(reason) -> str:
    """Why closed-won deal proof is absent: an outage, or a canonical
    withholding (a CRM finding) — never the one dressed as the other."""
    if canonical_revenue.is_ledger_outage(reason):
        return "the closed-won deal ledger could not be read"
    return f"closed-won deals are withheld for this window ({reason})"

# The residual is NOT a country. PR-ADS-153F: the label now comes from the ONE
# country identity contract, so the dashboard, the mart, Country Revenue & Spend and the
# drilldown cannot disagree about what the residual is called — the previous
# copy of this string here was a second place the name could drift.
RESIDUAL_LABEL = country_identity.RESIDUAL_LABEL


# ── Region mapping (presentation grouping only) ──────────────────────────────
# PR-ADS-176B: the table moved, unchanged, to `analysis/country_regions.py` so
# the Countries page (the mart country view) groups by the same regions. These
# names stay as aliases for this module's callers.
from analysis.country_regions import (  # noqa: E402
    COUNTRY_REGION as _COUNTRY_REGION,
    REGION_AFRICA, REGION_APAC, REGION_EUROPE, REGION_LATAM, REGION_MIDDLE_EAST,
    REGION_NORTH_AMERICA, REGION_ORDER, REGION_UNKNOWN,
    region_for_code as _region_for_code,
)


def _norm(label) -> str:
    """Normalise a country label for matching (lowercase, collapsed spaces)."""
    if not label:
        return ""
    return " ".join(str(label).replace("_", " ").strip().lower().split())


def _country_key(country_name, country_code) -> str | None:
    """Canonical join key for a country, via the ONE shared identity contract.

    PR-ADS-153F: this used to be ISO-code-first with a ``name:<normalized>``
    fallback, while Country Revenue & Spend keyed on a bare lowercased string and the
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


def _period_ratio(won, usd_spend):
    """Geo period revenue / ad spend = won revenue / USD spend, only when both are safe.

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
    # PR-ADS-170A-F1: the part of ``residual`` whose country IS known (Google
    # named it, or the owner labelled it) but is not a listed market.
    outside: dict | None = None
    outside_names: set = set()
    # PR-ADS-176A: the same outside-market spend, itemised per country.
    outside_items: dict = {}
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
            code = r.get("country_code")
            label = country_identity.iso_country_label(code) if isinstance(code, str) else None
            if label:
                outside = _merge_spend(outside, dict(entry))
                outside_names.add(label)
                _code = code.strip().upper()
                prior = outside_items.get(_code)
                merged = _merge_spend(
                    None if prior is None else prior["spend"], dict(entry))
                provenance = set(prior["provenance"]) if prior else set()
                if r.get("identity_provenance"):
                    provenance.add(r.get("identity_provenance"))
                outside_items[_code] = {"country": label, "spend": merged,
                                        "provenance": provenance}
    return {"by_key": by_key, "residual": residual, "outside_markets": outside,
            "outside_market_countries": sorted(outside_names),
            "outside_market_items": outside_items}


#: PR-ADS-176A: an outside-market sub-item's outcomes are not per country here.
IN_RESIDUAL = "in_residual"


def _outside_market_items(geo_maps: dict | None) -> list | None:
    """The residual's outside-listed-markets spend, one sub-item per country.

    PR-ADS-176A. A BREAKDOWN of ``kpis.spend_outside_listed_markets`` (the items
    sum to it), never additional to the residual. Their leads, SQLs and deals
    are not split per country: ``"in_residual"``, never 0. None when the geo
    read failed.
    """
    if geo_maps is None:
        return None
    out = []
    items = geo_maps.get("outside_market_items") or {}
    for code in sorted(items):
        it = items[code]
        prov = sorted(it.get("provenance") or ())
        spend = it.get("spend") or {}
        out.append({
            "country": it.get("country"), "country_code": code,
            "spend_native": _round2(spend.get("native")),
            "spend_usd": (_round2(spend.get("usd"))
                          if spend.get("usd") is not None else None),
            "provenance": prov[0] if len(prov) == 1 else (prov or None),
            "sqls": IN_RESIDUAL, "closed_won_deals": IN_RESIDUAL,
            "won_revenue_usd": IN_RESIDUAL,
            "breakdown_of": "kpis.spend_outside_listed_markets",
        })
    return out


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


#: The country-attributed revenue population is unreadable because the revenue
#: integration is not wired at all — distinct from "the ledger refused" and from
#: "a deal in it has no amount". Three causes, three reasons.
REASON_REVENUE_NOT_CONNECTED = "canonical_closed_won_population_unavailable"


def _country_revenue_total_verdict(country_rows: list, *, deal_proof_available: bool,
                                   revenue_connected: bool, canonical: dict) -> dict:
    """Is the COUNTRY-ATTRIBUTED revenue total publishable? (PR-ADS-155-F1)

    Decided over the country rows — the population the KPI actually sums — using
    the canonical rule, so the contract can never declare a readiness the number
    beside it contradicts.

    A row withholds its revenue for exactly two reasons (see
    ``_build_country_rows``): the revenue integration is not connected, or one of
    that country's deals has an unknown amount. They are separated here because
    they send an operator to two different places.

    PR-ADS-155-F1-F1 — the denominators are DEALS, not rows. The first cut passed
    ``won_deals=len(country_rows)`` and counted rows whose revenue was withheld,
    which produced the right blocking boolean and a false disclosure: "1 of 5
    closed-won deal(s) have no proven amount" over five COUNTRIES that between
    them might hold ninety deals. The verdict is a statement about deals, so it
    is computed from the deals attached to the published rows — the same
    population the KPI sums, at the grain the sentence claims.
    """
    if not deal_proof_available:
        return canonical_revenue.total_verdict_for_population(
            won_deals=None, unpriced_deals=None,
            scope="country_attributed_revenue",
            population_available=False,
            unavailable_reason=canonical.get("reason"),
            unavailable_detail=canonical.get("detail"),
            violation_codes=canonical.get("violation_codes"))
    if not revenue_connected:
        return canonical_revenue.total_verdict_for_population(
            won_deals=None, unpriced_deals=None,
            scope="country_attributed_revenue",
            population_available=False,
            unavailable_reason=REASON_REVENUE_NOT_CONNECTED,
            unavailable_detail=("the revenue integration is not connected, so no "
                                "country's closed-won revenue is readable"))

    counts = country_attributed_deal_counts(country_rows)
    # Belt and braces, decided by the canonical module. By construction a row is
    # blanked only because one of ITS deals has no proven amount, so the deal
    # count already catches every case — but a contract claiming readiness beside
    # a total the page could not sum is the exact defect this rule prevents, so
    # the row-level fact is passed in rather than assumed away.
    #
    # It is passed, not applied here: exactly one module decides revenue
    # completeness (PR-ADS-154C-F3-F1 §4). Assembling the verdict in this file
    # would make it a second decider, which `test_f3f1_4` correctly refuses.
    return canonical_revenue.total_verdict_for_population(
        won_deals=counts["closed_won_deals"],
        unpriced_deals=counts["revenue_unavailable_deals"],
        scope="country_attributed_revenue",
        unsummable_parts=sum(1 for r in (country_rows or [])
                             if r.get("won_revenue_usd") is None),
        parts_label="country row")


def country_attributed_deal_counts(country_rows: list) -> dict:
    """Real closed-won deal counts for the published country population.

    The deals attached to the country ROWS, which is exactly the population the
    country revenue KPI sums. Deals that mapped to no country live in the
    residual and are not part of this total; deals that mapped to a country with
    no mart row never reach a published row and are not part of it either.

    A deal is "priced" when its proof row carries an amount. ``_deal_proof_row``
    already applies the canonical currency rule — it stores ``amount_usd`` only
    where the ledger proved one — so this counts the same fact the row displays
    rather than re-deriving a second currency judgement here.
    """
    closed_won = 0
    unpriced = 0
    for row in country_rows or []:
        for deal in (row.get("deals") or []):
            closed_won += 1
            if deal.get("amount_usd") is None:
                unpriced += 1
    return {
        "closed_won_deals": closed_won,
        "revenue_proven_deals": closed_won - unpriced,
        "revenue_unavailable_deals": unpriced,
    }


def _group_deals_by_country(deals_rows: list, mart_real_rows: list,
                            mart_residual_row: dict | None) -> tuple[dict, set, list, bool]:
    """Each country row's drawer deals: EXACTLY the deals that row counts.

    PR-ADS-176A. The drawer listed the ALL-SOURCE won deals of the country
    while the row (the mart's country view) counts its ``google_ads_source``
    deals, so a won deal from any other channel sat in a Google Ads market's
    drawer without being in its count — and in the revenue verdict's deal
    denominators, which are computed from these drawers. Each mart row now
    carries ``deal_ids``, its own population (same scope, same settled days,
    same "not Google Ads" exclusions); the drawer is those deals and nothing
    else. A row with no ``deal_ids`` (withheld) has an empty drawer.

    ``deals_rows`` are the canonical deal rows of the page's load. Returns
    (deals_by_key, unknown_amount_keys, residual_deals,
    residual_amount_unknown) where deals_by_key maps a country key to its proof
    rows, unknown_amount_keys is the set of keys holding a deal with an unknown
    amount (so that key's revenue total renders Unavailable, never a lowered
    $0), residual_deals are the residual row's deals, and
    residual_amount_unknown flags an unknown amount among them.
    """
    by_id = {str(d.get("deal_id")): d for d in deals_rows or [] if d.get("deal_id")}
    deals_by_key: dict = {}
    unknown_amount_keys: set = set()
    for m in mart_real_rows or []:
        key = _country_key(m.get("country") or "Unknown", m.get("country_code"))
        if key is None:
            continue
        for deal_id in m.get("deal_ids") or []:
            deal = by_id.get(str(deal_id))
            if deal is None:
                continue
            deals_by_key.setdefault(key, []).append(
                _deal_proof_row(deal, _deal_attribution(deal, residual=False)))
            if deal.get("deal_amount_usd") is None:
                unknown_amount_keys.add(key)
    residual_deals: list = []
    residual_amount_unknown = False
    for deal_id in (mart_residual_row or {}).get("deal_ids") or []:
        deal = by_id.get(str(deal_id))
        if deal is None:
            continue
        residual_deals.append(_deal_proof_row(deal, _deal_attribution(deal, residual=True)))
        if deal.get("deal_amount_usd") is None:
            residual_amount_unknown = True
    return deals_by_key, unknown_amount_keys, residual_deals, residual_amount_unknown


# Google Ads geo spend mapping state (from the mart's spend_mapping) → UI label.
_MAPPING_STATUS = {"matched": "mapped", "unavailable": "mapping_unavailable"}


def _country_status(*, native_spend, usd_spend, fx_complete, sqls, customers,
                    won_revenue, attribution_status) -> str:
    """Business status for a country row (never an engineering term)."""
    if customers is None:
        # ``customers`` is the row's won-DEAL count here; unknown is not zero.
        return STATUS_OUTCOME_WITHHELD
    has_spend = native_spend is not None and native_spend > 0
    spend_unknown = native_spend is None  # geo source unavailable (not a measured 0)
    if (customers or 0) > 0 and (won_revenue or 0) > 0:
        return STATUS_REVENUE_PROVEN
    if (customers or 0) > 0:
        # Won deals exist but their revenue is not proven (unknown / zero
        # amount). Deals, not customers (PR-ADS-176A).
        return STATUS_WON_DEAL_REVENUE_UNPROVEN
    if (sqls or 0) > 0:
        return STATUS_SQL_PRODUCER
    if has_spend:
        # Spend exists but no SQL and no won deal.
        if attribution_status == "mapping_unavailable":
            return STATUS_GEO_REVIEW
        if usd_spend is None and not fx_complete:
            return STATUS_FX_UNAVAILABLE
        return STATUS_SPEND_NO_WON_DEAL
    if spend_unknown:
        # Spend could not be read (geo source down) — never asserted as a real "no
        # spend" measured zero.
        return STATUS_GEO_REVIEW
    return STATUS_NO_SPEND


def _build_country_rows(mart_real_rows: list, geo_maps: dict, geo_available: bool,
                        deals_by_key: dict, unknown_amount_keys: set,
                        country_roas_unblockable: bool, revenue_connected: bool,
                        native_currency: str = "GBP",
                        zero_evidence: dict | None = None) -> list:
    """Merge mart country rows (outcomes + mapping) with the per-country native/
    USD geo spend split and the closed-won proof deals. The residual is handled
    separately — this builds ONLY real countries, never the residual.

    When the revenue integration is disconnected, closed-won customers AND revenue
    are unknown, so they (and the period ratio) are blanked to None here — BEFORE the status is
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
        spend_reason = None
        zero_rests_on: list = []
        if spend is not None:
            native_spend = spend.get("native")
            usd_spend = spend.get("usd")
            fx_complete = bool(spend.get("fx_complete"))
        elif geo_available and zero_rule.zero_spend_verdict(
                zero_evidence, country_identity.resolve(
                    name=country_name, code=country_code).code)[0]:
            # PR-ADS-176A: the geo table was read and this country has no row
            # in it, AND the shared zero rule proves no spend in the window can
            # be its own: a measured zero (revenue may still be proven from
            # another channel). It used to be a zero on "the geo read
            # succeeded" alone — while spend Google reported against a
            # location no stored row names could be this country's.
            native_spend = 0.0
            usd_spend = 0.0
            fx_complete = True
            # PR-ADS-176B-F2: a $0 that stands only on the owner's labels says
            # so, from the same zero-rule evidence (one rule: the shared one).
            zero_rests_on = zero_rule.zero_rests_on_owner_labels(
                zero_evidence, country_identity.resolve(
                    name=country_name, code=country_code).code)
        elif geo_available:
            # No geo row, and the zero is not proven: unknown, never $0.
            native_spend = None
            usd_spend = None
            fx_complete = False
            spend_reason = zero_rule.zero_spend_verdict(
                zero_evidence, country_identity.resolve(
                    name=country_name, code=country_code).code)[1]
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
            customers = m.get("closed_won_deals")
            # Won revenue: the mart's canonical per-country total, blanked to None
            # if any of this country's deals has an unknown amount (never a lowered $0).
            won = None if amount_unknown else _round2(m.get("won_revenue"))

        attribution_status = _MAPPING_STATUS.get(m.get("spend_mapping"), "mapping_unavailable")
        # The period ratio only when this country has real attributed spend, FX is complete,
        # revenue is safe, AND geo reconciliation is unblockable for the account.
        row_ratio = None
        if country_roas_unblockable and (native_spend or 0) > 0:
            row_ratio = _period_ratio(won, usd_spend)

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
            # PR-ADS-176A: why spend is None when the zero rule withheld it.
            "spend_reason": spend_reason,
            # PR-ADS-176B-F2: the owner-labelled criteria a published $0
            # rests on ([] for any other spend).
            "zero_rests_on_owner_labels": zero_rests_on,
            "sqls": m.get("sqls"),
            "closed_won_deals": customers,
            # PR-ADS-161E: this market's customers — distinct proven HubSpot
            # companies, not additive across markets.
            "customers": m.get("customers") if revenue_connected else None,
            "metrics": m.get("metrics") if revenue_connected else None,
            "won_revenue_usd": won,
            "period_revenue_per_ad_spend": row_ratio,
            "period_ratio_available": row_ratio is not None,
            "status": status,
            "attribution_status": attribution_status,
            "top_campaign": m.get("top_campaign"),
            # PR-ADS-176A round 2: no proof list beside a withheld deal count
            # or a withheld population — None, never [] (which reads "none").
            "deals": (None if (customers is None or m.get("deal_ids") is None)
                      else (deals_by_key.get(key) or []) if key is not None else []),
            "is_residual": False,
            "target_page": "roas-countries",
        })

    # Real countries first: revenue, then customers, then spend.
    rows.sort(key=lambda r: (
        r.get("won_revenue_usd") or 0.0,
        r.get("closed_won_deals") or 0,
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
                    revenue_connected: bool, geo_maps: dict | None = None) -> dict:
    """The explicit 'Unknown / Unattributed country' bucket — the CANONICAL residual.

    Residual customers / revenue / SQLs are derived from the canonical Revenue
    Decision Mart (its window TOTAL minus the sum of the real country rows), NOT
    from the drawer deal ledger — so they SURVIVE a deal-ledger outage instead of
    collapsing to a fake $0 / empty. The deal ledger is used ONLY to detect an
    unknown residual deal amount (which then withholds the residual revenue total,
    never a lowered $0). The geo-spend residual (spend the geographic view does not
    assign to a country) is carried from spend_truth (with the mart residual row as
    a fallback). It is never mapped, never given a spend ratio, never a drawer.
    """
    if not revenue_connected:
        # Customers AND revenue come from the revenue integration → unknown.
        customers = None
        won = None
        sqls = None
    else:
        # PR-ADS-161E: the country rows partition the GOOGLE ADS-SOURCE deals,
        # so the residual subtracts them from that scope's canonical total —
        # never from the all-source total, which put every non-Google deal in
        # a bucket labelled "could not be assigned to a country".
        gap = _residual_gap(summary.get("google_ads_source_closed_won_deals"),
                            real_rows, "closed_won_deals")
        customers = None if gap is None else int(round(gap))
        gap_sqls = _residual_gap(summary.get("sqls"), real_rows, "sqls")
        sqls = None if gap_sqls is None else int(round(gap_sqls))
        # Revenue: canonical mart residual, withheld if a residual deal has an
        # unknown amount (the mart total counts it as $0, so the derived residual
        # would be lowered) — never a fabricated / lowered $0.
        if residual_amount_unknown:
            won = None
        else:
            won = _round2(_residual_gap(summary.get("google_ads_source_won_revenue_usd"),
                                        real_rows, "won_revenue_usd"))

    native = spend_truth.get("country_residual_native")
    usd = spend_truth.get("country_residual_usd")
    if native is None and mart_residual_row is not None:
        # The shortfall alone (PR-ADS-170A-F1: the mart row's `spend_native` is
        # now the whole residual row, which also holds countries outside the
        # listed markets).
        native = mart_residual_row.get("spend_geo_unassigned_native",
                                       mart_residual_row.get("spend_native"))
        usd = mart_residual_row.get("spend_geo_unassigned_usd",
                                    mart_residual_row.get("spend_usd"))

    reason_bits = []
    if spend_truth.get("country_residual_reason"):
        reason_bits.append(spend_truth["country_residual_reason"])
    reason_bits.append("Google Ads closed-won deals and revenue that could not be safely assigned "
                       "to a country — preserved separately, never spread across real countries.")
    return {
        "label": RESIDUAL_LABEL,
        "closed_won_deals": customers,
        "won_revenue_usd": won,
        "sqls": sqls,
        "spend_native": native,
        "spend_usd": usd,
        "native_currency": spend_truth.get("native_currency") or "GBP",
        "reason": " ".join(reason_bits),
        # PR-ADS-176A: Google-named (or owner-labelled) countries outside the
        # listed markets, itemised — a breakdown, never additional.
        "outside_listed_market_items": _outside_market_items(geo_maps),
        "period_revenue_per_ad_spend": None,
        "period_ratio_available": False,
        "drawer_available": False,
    }


def _build_kpis(country_rows: list, residual: dict, spend_truth: dict,
                country_roas_unblockable: bool, geo_available: bool,
                revenue_connected: bool, geo_maps: dict | None = None) -> dict:
    """Hero KPIs derived from the real country rows (so they reconcile with the
    leaderboard / map) plus the canonical spend truth. The residual is surfaced
    separately, never in the ratio numerator.

    ``geo_available`` / ``revenue_connected`` gate the totals so an EMPTY leaderboard
    (a source outage, where ``any([])`` would otherwise be False) never fabricates a
    $0 / 0-customer total presented as measured truth."""
    # PR-ADS-176A truth audit M3: a count over rows whose spend is unknown is
    # unknown — the geo read failed, or the zero rule withheld a market's $0.
    countries_with_spend = (
        None if (not geo_available or any(r.get("spend_native") is None for r in country_rows))
        else sum(1 for r in country_rows if (r.get("spend_native") or 0) > 0))
    # Unknown when any market's deal count is withheld — never a lowered count.
    countries_with_closed_won_deals = (
        None if any(r.get("closed_won_deals") is None for r in country_rows)
        else sum(1 for r in country_rows if r["closed_won_deals"] > 0))

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
    deals_incomplete = (not revenue_connected) or any(r.get("closed_won_deals") is None for r in country_rows)
    total_closed_won_deals = (None if deals_incomplete
                       else sum((r.get("closed_won_deals") or 0) for r in country_rows))

    revenue_incomplete = (not revenue_connected) or any(r.get("won_revenue_usd") is None for r in country_rows)
    total_won = (None if revenue_incomplete
                 else _round2(sum((r.get("won_revenue_usd") or 0.0) for r in country_rows)))

    geo_ratio = _period_ratio(total_won, verified_usd) if country_roas_unblockable else None
    # PR-ADS-170A-F1: the spend card is the LISTED MARKETS' spend — the market
    # rows — not all Google Ads geo spend. Geo spend whose country is known but
    # is not a listed market (Cuba, by the owner's label; any Google country
    # outside the registry) is on no row of this page, so the payload states
    # the scope and how much lies outside it. None when the geo read failed.
    outside = (geo_maps or {}).get("outside_markets") if geo_available else None
    no_country = None
    if geo_available and geo_maps is not None:
        whole = geo_maps.get("residual") or {}
        no_country = _round2((whole.get("native") or 0.0)
                             - ((outside or {}).get("native") or 0.0))
    # PR-ADS-176A: a market whose $0 the zero rule cannot prove makes the
    # listed markets' spend total unknown — its spend may be in the residual's
    # no-country part. The reason travels with the withheld total.
    zero_withheld = sorted({r.get("country_name") for r in country_rows
                            if r.get("spend_reason") == zero_rule.R_SPEND_LOCATION_UNRESOLVED})
    return {
        "verified_spend_scope": "listed_markets",
        "verified_spend_reason": (zero_rule.R_SPEND_LOCATION_UNRESOLVED
                                  if zero_withheld else None),
        "zero_withheld_countries": zero_withheld,
        "spend_outside_listed_markets": (None if (not geo_available or geo_maps is None) else {
            "native": _round2((outside or {}).get("native") or 0.0),
            "usd": (_round2(outside.get("usd")) if (outside and outside.get("usd") is not None)
                    else (0.0 if not outside else None)),
            "currency": spend_truth.get("native_currency") or "GBP",
            "countries": list((geo_maps or {}).get("outside_market_countries") or []),
            # Geo spend Google reports with no resolvable country at all.
            "no_country_native": no_country,
        }),
        "countries_with_spend": countries_with_spend,
        "countries_with_closed_won_deals": countries_with_closed_won_deals,
        "verified_spend_native": {"amount": verified_native,
                                  "currency": spend_truth.get("native_currency") or "GBP"},
        "verified_spend_usd": verified_usd,
        "sqls": total_sqls,
        # PR-ADS-154C-F3: name the population. This is the sum over REAL country
        # rows; the SQLs that could not be assigned to a country are in
        # `residual.sqls`, and the two together are the campaign-attributable
        # total. Without this the KPI reads as "SQLs" and is quietly a subset —
        # the same trap the Overview closed with `sqls_scope` in PR-ADS-152.
        "sqls_scope": "country_attributed_sqls",
        "sqls_residual": residual.get("sqls"),
        "closed_won_deals": total_closed_won_deals,
        "won_revenue_usd": total_won,
        "geo_period_revenue_per_ad_spend": geo_ratio,
        "residual_revenue_usd": residual.get("won_revenue_usd"),
        "residual_closed_won_deals": residual.get("closed_won_deals"),
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
            "closed_won_deals": 0, "deals_incomplete": False,
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
        if r.get("closed_won_deals") is None:
            b["deals_incomplete"] = True
        else:
            b["closed_won_deals"] += r["closed_won_deals"]
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
        customers = None if b["deals_incomplete"] else b["closed_won_deals"]
        won = None if b["revenue_incomplete"] else _round2(b["won_revenue_usd"])
        ratio = _period_ratio(won, usd) if country_roas_unblockable else None
        # PR-ADS-176A truth audit M3: the region's status follows the country
        # status rule (`_country_status`) — a withheld deal count is not a
        # "spend without a won deal", and an unknown spend is not "no spend".
        if customers is None:
            status = STATUS_OUTCOME_WITHHELD
        elif customers > 0 and (won or 0) > 0:
            status = STATUS_REVENUE_PROVEN
        elif customers > 0:
            status = STATUS_WON_DEAL_REVENUE_UNPROVEN
        elif (sqls or 0) > 0:
            status = STATUS_SQL_PRODUCER
        elif (native or 0) > 0:
            status = STATUS_SPEND_NO_WON_DEAL
        elif native is None:
            status = STATUS_GEO_REVIEW
        else:
            status = STATUS_NO_SPEND
        out.append({
            "region": region,
            "countries": b["countries"],
            "spend_native": native,
            "native_currency": country_rows[0].get("native_currency") if country_rows else "GBP",
            "spend_usd": usd,
            "sqls": sqls,
            "closed_won_deals": customers,
            "won_revenue_usd": won,
            "period_revenue_per_ad_spend": ratio,
            "period_ratio_available": ratio is not None,
            "status": status,
        })
    out.sort(key=lambda r: (
        r.get("won_revenue_usd") or 0.0,
        r.get("closed_won_deals") or 0,
        r.get("spend_native") or 0.0,
    ), reverse=True)
    return out



# PR-ADS-161E: the reason a revenue total is unknown is the canonical one — an
# unpriced deal, a deal on no campaign, an undated deal — never assumed.
_REVENUE_REASON_TEXT = {
    "closed_won_deals_missing_proven_usd_amount": "a closed-won deal has no proven USD amount",
    "closed_won_deals_missing_amount": "a closed-won deal has no proven USD amount",
    "google_ads_deals_not_placed_on_a_campaign": "a Google Ads won deal is on no campaign",
    "campaign_identity_unavailable": "campaign identity is unavailable",
    "won_deals_without_close_date": "a won deal has no close date",
    "won_flag_and_stage_disagree": "HubSpot's won flag and won stage disagree",
    "closed_won_deal_count_withheld": "the closed-won deal count is withheld",
}


def _revenue_unknown_text(kpis: dict, rows=()) -> str:
    reasons = [((kpis.get("revenue_metric") or {}).get("reason"))]
    reasons += [(((r.get("metrics") or {}).get("revenue_usd") or {}).get("reason"))
                for r in rows or []]
    reason = next((r for r in reasons if r), None)
    if not reason:
        return "closed-won revenue is withheld"
    return _REVENUE_REASON_TEXT.get(reason, f"closed-won revenue is withheld ({reason})")

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


def _zero_withheld_text(kpis: dict) -> str | None:
    """PR-ADS-176A truth audit M1: why the listed markets' spend is withheld
    when the shared zero rule withheld a market's $0 — never FX: FX may be
    verified, and native spend is withheld too. None when that is not why."""
    if kpis.get("verified_spend_reason") != zero_rule.R_SPEND_LOCATION_UNRESOLVED:
        return None
    names = ", ".join(kpis.get("zero_withheld_countries") or []) or "A market"
    return (f"{names} had no Google Ads geo spend row in this window, and the window "
            "holds spend Google reported against a location no stored row names, "
            "which may be theirs — so their $0 is not proven, and the listed "
            "markets' spend (native and USD) is withheld with it")


def _build_decision_cards(country_rows: list, residual: dict, kpis: dict,
                          spend_truth: dict, deal_proof_available: bool,
                          deal_proof_reason=None) -> list:
    cards = []
    # PR-ADS-161E: markets whose deal count is withheld support no claim.
    withheld = [r for r in country_rows if r.get("closed_won_deals") is None]

    # Scale — the top revenue-proven country.
    proven = [r for r in country_rows if r["status"] == STATUS_REVENUE_PROVEN]
    if withheld and not proven:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "Market closed-won outcomes are withheld",
            "body": (f"Closed-won deals are withheld for {len(withheld)} "
                     f"market{'s' if len(withheld) != 1 else ''} in this window, so "
                     "no market is judged on revenue here."),
            "target_page": "roas-countries", "target_label": "Open Countries",
        })
    elif proven:
        top = max(proven, key=lambda r: (r.get("won_revenue_usd") or 0.0,
                                         r.get("closed_won_deals") or 0))
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": f"{top['country_name']} has revenue proof",
            "body": (f"{top['country_name']} won {top['closed_won_deals']} deal"
                     f"{'s' if top['closed_won_deals'] != 1 else ''} and "
                     f"{_fmt_usd_short(top['won_revenue_usd'])} closed-won revenue this "
                     "window."),
            "target_page": "roas-countries", "target_label": "Open Countries",
        })
    else:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "No country has revenue proof yet",
            "body": "No market has both closed-won deals and proven revenue in "
                    "this window.",
            "target_page": "roas-countries", "target_label": "Open Countries",
        })

    # Watch — spend without a won deal.
    no_proof = [r for r in country_rows if r["status"] in
                (STATUS_SPEND_NO_WON_DEAL, STATUS_SQL_PRODUCER, STATUS_FX_UNAVAILABLE)
                and (r.get("spend_native") or 0) > 0 and r.get("closed_won_deals") == 0]
    if no_proof:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "Markets spending without won-deal proof",
            "body": (f"{len(no_proof)} countr{'ies' if len(no_proof) != 1 else 'y'} spent "
                     "Google Ads budget but won no deals in this window. Review "
                     "before scaling."),
            "target_page": "roas-countries", "target_label": "Open Countries",
        })
    elif withheld:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "Unproven market spend cannot be judged",
            "body": (f"Closed-won deals are withheld for {len(withheld)} "
                     f"market{'s' if len(withheld) != 1 else ''}, so spend without "
                     "proof is not claimed."),
            "target_page": "roas-countries", "target_label": "Open Countries",
        })
    else:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "No unproven market spend",
            "body": "Every spending market won at least one deal, or has no "
                    "spend to judge.",
            "target_page": "roas-countries", "target_label": "Open Countries",
        })

    # Investigate — geo attribution review, or revenue that maps to no country.
    geo_ok = geo_gate.country_geo_ready(spend_truth.get("country_spend_status"))
    residual_rev = residual.get("won_revenue_usd")
    residual_customers = residual.get("closed_won_deals")
    if not geo_ok:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Geo attribution needs review",
            "body": ("Google Ads geographic spend does not reconcile with the canonical "
                     "campaign spend for this window, so country period revenue / ad spend is withheld. Native "
                     "spend is still shown, never a fabricated USD figure or ratio."),
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })
    elif residual_customers is None or (residual_rev is None
                                         and (residual_customers or 0) > 0):
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Unassigned closed-won outcome is withheld",
            "body": ("The closed-won deals or revenue that no country holds are "
                     "withheld this window, so whether all revenue maps to a "
                     "country is not claimed."),
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
            "target_page": "roas-countries", "target_label": "Open Countries",
        })

    # Unavailable — explain WHY geo period ratio / USD spend is withheld, ACCURATELY.
    fx_ok = spend_truth.get("fx_status") == "verified"
    usd_present = kpis.get("verified_spend_usd") is not None
    revenue_known = kpis.get("won_revenue_usd") is not None
    if not deal_proof_available:
        headline = "Deal proof unavailable"
        body = (f"Country deal/client proof is withheld: "
                f"{_deal_proof_reason_text(deal_proof_reason)}.")
    elif not geo_ok:
        headline = "Geo period revenue / ad spend unavailable"
        body = ("Country period revenue / ad spend is withheld because Google Ads geographic spend does not "
                "reconcile with the canonical campaign spend — never a fabricated ratio.")
    elif _zero_withheld_text(kpis):
        headline = "Listed-market spend & geo period revenue / ad spend unavailable"
        body = (f"{_zero_withheld_text(kpis)}. Geo period revenue / ad spend is "
                "withheld for the same reason — never a fabricated figure or ratio.")
    elif not fx_ok or not usd_present:
        headline = "USD spend & geo period revenue / ad spend unavailable"
        body = ("USD spend and geo period revenue / ad spend are withheld because FX coverage is incomplete — "
                "native GBP spend is still shown, never a fabricated USD figure or ratio.")
    elif not revenue_known:
        headline = "Geo period revenue / ad spend unavailable"
        body = (f"Geo period revenue / ad spend is withheld because {_revenue_unknown_text(kpis, country_rows)}"
                " — never a lowered $0.")
    elif kpis.get("geo_period_revenue_per_ad_spend") is None:
        headline = "Geo period revenue / ad spend not yet meaningful"
        body = ("There is no country-attributed closed-won revenue yet to measure against "
                "this window's verified spend, so Geo period revenue / ad spend is not shown — never a "
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
        "revenue": "ready" if readiness.get("canonical_revenue_available") else "blocked",
        "geo_attribution": geo_attribution,
        "residual": residual,
        # Client/deal proof comes from the closed-won deal ledger; when that is
        # down we cannot prove countries even if the mart revenue is canonical.
        "deal_proof": "ready" if deal_proof_available else "unavailable",
    }


def _build_unavailable(kpis: dict, spend_truth: dict, period_change: dict,
                       deal_proof_available: bool, deal_proof_reason=None) -> list:
    out = []
    if _zero_withheld_text(kpis):
        out.append({"metric": "verified_spend_usd",
                    "reason": f"Listed-market spend is withheld: {_zero_withheld_text(kpis)}."})
    elif kpis.get("verified_spend_usd") is None:
        out.append({"metric": "verified_spend_usd",
                    "reason": "USD spend requires verified FX; native GBP spend is shown instead."})
    if kpis.get("geo_period_revenue_per_ad_spend") is None:
        geo_ok = geo_gate.country_geo_ready(spend_truth.get("country_spend_status"))
        fx_ok = spend_truth.get("fx_status") == "verified"
        usd_present = kpis.get("verified_spend_usd") is not None
        # PR-ADS-153F §8: name the ACTUAL gap. The gate is holistic now, so
        # `unavailable` covers an unreadable baseline, incomplete FX and a geo
        # range nobody fetched alike — telling all three operators to "re-run
        # geo sync" would send two of them to fix the wrong thing.
        gap_reason = geo_gate.describe_geo_gap(spend_truth.get("country_gap_codes"))
        if not geo_ok:
            ratio_reason = gap_reason or (
                "Geo period revenue / ad spend requires Google Ads geographic spend that reconciles "
                "with the canonical campaign spend — it does not for this window.")
        elif _zero_withheld_text(kpis):
            ratio_reason = ("Geo period revenue / ad spend requires the listed markets' spend — "
                            f"{_zero_withheld_text(kpis)}.")
        elif not fx_ok or not usd_present:
            ratio_reason = ("Geo period revenue / ad spend requires verified FX / USD spend; native GBP spend is "
                           "shown instead.")
        elif kpis.get("won_revenue_usd") is None:
            ratio_reason = ("Geo period revenue / ad spend requires published revenue — "
                           f"{_revenue_unknown_text(kpis)}.")
        else:
            ratio_reason = "Geo period revenue / ad spend not yet meaningful; no country-attributed closed-won revenue."
        out.append({"metric": "geo_period_revenue_per_ad_spend", "reason": ratio_reason})
    if not deal_proof_available:
        out.append({"metric": "deal_proof",
                    "reason": ("Closed-won deal proof is absent — "
                               f"{_deal_proof_reason_text(deal_proof_reason)}.")})
    if kpis.get("sqls") is None:
        out.append({"metric": "sqls",
                    "reason": "SQLs withheld — lead business event dates are unavailable."})
    # Residual revenue: withheld either because the revenue integration is
    # disconnected (customers also unknown) or because a residual closed-won deal
    # has an unknown amount (customers still countable). Use the exact KPI field
    # name so a client can attach the reason to kpis.residual_revenue_usd.
    if kpis.get("residual_revenue_usd") is None:
        if kpis.get("residual_closed_won_deals") is None:
            residual_reason = ("Revenue with no country is withheld — "
                               f"{_revenue_unknown_text(kpis)}.")
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
    revenue_connected = bool(readiness.get("canonical_revenue_available"))
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

    # PR-ADS-176A: the configured account's geo spend only (PR-ADS-154B: the
    # mart's geo total and coverage are scoped to it; this read was not, so a
    # second account's spend landed on these market rows), and the shared zero
    # rule's evidence for the mart's countries, from ONE snapshot.
    from services.google_ads_spend_service import (  # noqa: PLC0415
        configured_customer_id,
    )
    _candidates = sorted({
        c for c in (country_identity.resolve(
            name=m.get("country"), code=m.get("country_code")).code
            for m in mart_real_rows) if c})
    _geo_read = zero_rule.read_country_spend(start, end, configured_customer_id(),
                                             _candidates)
    geo = _geo_read["spend"]
    zero_evidence = _geo_read["zero_rule"]
    geo_available = bool(geo.get("available"))
    geo_maps = _geo_spend_by_country(geo)

    # PR-ADS-153E-B: the deals proving each country row come from the canonical
    # ledger. PR-ADS-176A: at the ROW's scope, `google_ads_source` — not
    # `all_source` — and only the deals the row itself counts (its mart
    # `deal_ids`). Deals the residual row counts are its drawer-less proof.
    # PR-ADS-167A: the SAME days as the mart country view and the geo spend
    # above — an open window ends at yesterday (``through_reason``), for the
    # deals proving each country exactly as for its spend.
    canonical = canonical_revenue.load_won_deals(
        window, now=now,
        bounds=canonical_contract.settled_revenue_bounds(window_block))
    deal_proof_available = bool(canonical.get("available"))
    deal_rows = (canonical_revenue.canonical_deal_rows(
        canonical, revenue_scope.SCOPE_GOOGLE_ADS_SOURCE)
        if deal_proof_available else [])
    (deals_by_key, unknown_amount_keys,
     residual_deals, residual_amount_unknown) = _group_deals_by_country(
        deal_rows, mart_real_rows, mart_residual_row)

    country_rows = _build_country_rows(
        mart_real_rows, geo_maps, geo_available, deals_by_key, unknown_amount_keys,
        country_roas_unblockable, revenue_connected, native_currency,
        zero_evidence=zero_evidence)
    # PR-ADS-170A: a market whose geo spend is under it because the owner
    # labelled the Google criterion (Google reports it without a country) says
    # so — from the SAME geo read that supplied its spend.
    from services.revenue_attribution_service import (  # noqa: PLC0415
        stamp_owner_labelled_countries,
    )
    owner_labelled_geo = (geo.get("owner_labelled_geo") or {}) if geo_available else {}
    country_rows = stamp_owner_labelled_countries(country_rows, owner_labelled_geo)

    # PR-ADS-155-F1. The country-attributed revenue TOTAL gets its verdict from
    # its own population, via the canonical rule — not from geo readiness. Geo
    # readiness says the spend side is placed; it is silent on whether the deals
    # behind the revenue carry proven amounts, which is why this page published
    # `truth_status: ready` beside a null revenue value.
    #
    # The population is the COUNTRY ROWS, because that is exactly what the KPI
    # sums. Deriving it from the deal grouping instead looked equivalent and is
    # not: a deal can map to a country that has no mart row, so it would make the
    # verdict describe a population the published figure never included.
    _country_revenue_verdict = _country_revenue_total_verdict(
        country_rows,
        deal_proof_available=deal_proof_available,
        revenue_connected=revenue_connected,
        canonical=canonical)

    residual = _build_residual(summary, country_rows, spend_truth, mart_residual_row,
                               residual_amount_unknown, revenue_connected,
                               geo_maps=geo_maps if geo_available else None)
    kpis = _build_kpis(country_rows, residual, spend_truth, country_roas_unblockable,
                       geo_available, revenue_connected, geo_maps=geo_maps)
    # PR-ADS-161E: the window's canonical verdicts — a country total is a
    # subset of the window, so when the window withholds it the card says why.
    # Only a NON-published verdict travels: a published one has nothing to add.
    _pub = canonical.get("publication") or {}
    # A country total is a subset of the window: when the window withholds a
    # family, the sum of the rows that exist is not a total — unknown, never 0.
    if (_pub.get("closed_won_deals") or {}).get("status") != cwt.PUBLISHED:
        for _k in ("closed_won_deals", "countries_with_closed_won_deals",
                   "residual_closed_won_deals"):
            kpis[_k] = None
    if (_pub.get("revenue_usd") or {}).get("status") != cwt.PUBLISHED:
        for _k in ("won_revenue_usd", "geo_period_revenue_per_ad_spend", "residual_revenue_usd"):
            if _k in kpis:
                kpis[_k] = None
    for _key, _metric in (("revenue_metric", "revenue_usd"),
                          ("deals_metric", "closed_won_deals")):
        _st = (_pub.get(_metric) or {}).get("status") or cwt.UNAVAILABLE
        kpis[_key] = (None if _st == cwt.PUBLISHED else cwt.metric(
            None, _st, (_pub.get(_metric) or {}).get("reason")
            or canonical.get("reason")))

    return {
        "window_block": window_block,
        "spend_truth": spend_truth,
        "readiness": readiness,
        "country_rows": country_rows,
        "owner_labelled_geo": {
            "criteria": list(owner_labelled_geo.get("criteria") or []),
            "mismatches": list(owner_labelled_geo.get("mismatches") or []),
        },
        "residual": residual,
        "kpis": kpis,
        "country_roas_unblockable": country_roas_unblockable,
        "deal_proof_available": deal_proof_available,
        "deal_proof_reason": canonical.get("reason"),
        # PR-ADS-155-F1: the scoped verdict, so the contract builder states the
        # page's own truth rather than re-deriving it from a spend-side flag.
        "country_revenue_verdict": _country_revenue_verdict,
        # PR-ADS-155-F1-F1: the real deal denominators behind that verdict, so
        # the disclosure counts DEALS and a reader can check the arithmetic
        # rather than take the verdict's word for it.
        # PR-ADS-176A truth audit MINOR 2: the drawers are the mart rows' own
        # populations (`deal_ids`). A row whose population is withheld (None)
        # makes every count over the drawers unknown — never 0.
        "country_revenue_deal_counts": (
            country_attributed_deal_counts(country_rows)
            if (deal_proof_available and revenue_connected
                and not any(m.get("deal_ids") is None for m in mart_rows)) else {
                "closed_won_deals": None, "revenue_proven_deals": None,
                "revenue_unavailable_deals": None}),
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        # PR-ADS-176B: the scope the rows, the residual and the drawers count —
        # `google_ads_source` (the mart's country rows; `deal_rows` above).
        # It said `all_source`, a population no row here holds (docs/58 §5).
        "revenue_scope": revenue_scope.SCOPE_GOOGLE_ADS_SOURCE,
        "revenue_unavailable_reason": (None if deal_proof_available
                                       else canonical.get("reason")),
        "revenue_violation_codes": canonical.get("violation_codes") or [],
        "as_of": canonical.get("as_of"),
        "legacy_fallback_used": False,
        # PR-ADS-153F §8: the shared disclosure block — sources, scope, exact UTC
        # window bounds, freshness, coverage, reconciliation, residual and
        # whether the residual was accepted. Built by the geo gate so this page
        # and Country Revenue & Spend disclose the SAME facts in the same shape.
        "country_truth": geo_gate.build_country_truth_disclosure(
            spend_truth, window_block,
            revenue_source=canonical_revenue.CANONICAL_SOURCE,
            revenue_scope=revenue_scope.SCOPE_GOOGLE_ADS_SOURCE,
            revenue_available=deal_proof_available,
            revenue_reason=canonical.get("reason"),
            revenue_violation_codes=canonical.get("violation_codes"),
            as_of=canonical.get("as_of"),
        ),
    }


#: Row status when its figures are withheld for the window.
STATUS_COUNTRY_FIGURES_WITHHELD = "Country figures withheld"


def _withhold_when_geo_not_ready(kpis: dict, rows: list, residual: dict,
                                 spend_truth: dict) -> tuple:
    """PR-ADS-167A — withheld means absent, for the totals AND their parts.

    When geo is not ready (any gap the shared gate names: yesterday not yet
    re-read before the morning sync, no settled day in the window, an
    unreconciled or uncovered window …) the KPI totals of spend, revenue,
    closed-won deals and SQLs are withheld. Each country row and the residual
    then withhold the same figures with the same reason: rows that add back
    up to a withheld total would publish it. Native spend stays only where the
    KPI keeps it — when the geo COVERAGE is complete (an FX-only gap: "native
    spend is shown instead").
    """
    if spend_truth.get("country_roas_unblockable") or spend_truth.get("geo_ready"):
        return kpis, rows, residual
    # Row / residual figures that are statements about country-attributed
    # geography: spend, revenue, the deal count and its proof, customers, and
    # the SQL split whose partition geo decides.
    row_geo_figures = ("spend_usd", "won_revenue_usd", "closed_won_deals",
                       "customers", "sqls", "period_revenue_per_ad_spend")
    reason = next(iter(spend_truth.get("country_gap_codes") or []),
                  None) or "country_geo_not_ready"
    keep_native = spend_truth.get("geo_coverage_status") == "complete"
    kpis = {**kpis, "verified_spend_usd": None, "won_revenue_usd": None,
            "closed_won_deals": None, "sqls": None}
    if not keep_native and isinstance(kpis.get("verified_spend_native"), dict):
        kpis["verified_spend_native"] = {**kpis["verified_spend_native"],
                                         "amount": None}
    if isinstance(kpis.get("spend_outside_listed_markets"), dict):
        # Withheld with the card it qualifies (PR-ADS-170A-F1).
        _o = {**kpis["spend_outside_listed_markets"], "usd": None}
        if not keep_native:
            _o["native"] = None
            _o["no_country_native"] = None
        kpis["spend_outside_listed_markets"] = _o
    for k in ("countries_with_closed_won_deals", "residual_closed_won_deals",
              "residual_revenue_usd", "geo_period_revenue_per_ad_spend"):
        if k in kpis:
            kpis[k] = None
    if not keep_native:
        # PR-ADS-176A truth audit M3: a count of rows whose spend is withheld.
        kpis["countries_with_spend"] = None
    if reason == canonical_contract.NO_SETTLED_DAY_IN_WINDOW:
        # No completed day: the window's own deal / revenue verdicts are not
        # "published zeros" either.
        kpis["revenue_metric"] = cwt.metric(None, cwt.UNAVAILABLE, reason)
        kpis["deals_metric"] = cwt.metric(None, cwt.UNAVAILABLE, reason)
    out_rows = []
    for r in rows or []:
        row = {**r, **{k: None for k in row_geo_figures},
               "metrics": None, "deals": None, "period_ratio_available": False,
               "status": STATUS_COUNTRY_FIGURES_WITHHELD,
               "withheld_reason": reason}
        if not keep_native:
            row["spend_native"] = None
        out_rows.append(row)
    residual = {**(residual or {}), "closed_won_deals": None,
                "won_revenue_usd": None, "sqls": None, "spend_usd": None,
                # PR-ADS-176A: withheld with the spend it itemises.
                "outside_listed_market_items": None,
                "withheld_reason": reason}
    if not keep_native:
        residual["spend_native"] = None
    return kpis, out_rows, residual


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
    # PR-ADS-167A: the previous period withholds by the same rule — a delta
    # against a withheld figure is no comparison, not a comparison with it.
    pk = _withhold_when_geo_not_ready(prev["kpis"], prev["country_rows"],
                                      prev["residual"], prev["spend_truth"])[0]
    metrics = {
        "won_revenue_usd": _delta_metric(kpis.get("won_revenue_usd"), pk.get("won_revenue_usd")),
        "closed_won_deals": _delta_metric(kpis.get("closed_won_deals"), pk.get("closed_won_deals")),
        "sqls": _delta_metric(kpis.get("sqls"), pk.get("sqls")),
        "verified_spend_usd": _delta_metric(kpis.get("verified_spend_usd"), pk.get("verified_spend_usd")),
        "geo_period_revenue_per_ad_spend": _delta_metric(kpis.get("geo_period_revenue_per_ad_spend"), pk.get("geo_period_revenue_per_ad_spend")),
    }
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label, "note": note, "metrics": metrics, "reason": None,
    }


@_canonical_snapshot.single_snapshot
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
    _revenue_connected = bool(readiness.get("canonical_revenue_available"))
    # PR-ADS-167A: withheld means absent — before anything (leaderboard,
    # regional mix, decision cards, deltas) is derived from the figures.
    kpis, country_rows, residual = _withhold_when_geo_not_ready(
        kpis, country_rows, residual, spend_truth)

    period_change = _build_period_change(window, ref_now, kpis)
    regional_mix = _build_regional_mix(country_rows, core["country_roas_unblockable"])
    deal_proof_reason = core.get("deal_proof_reason")
    decision_cards = _build_decision_cards(country_rows, residual, kpis, spend_truth,
                                           deal_proof_available, deal_proof_reason)
    truth_status = _build_truth_status(spend_truth, readiness, deal_proof_available)
    unavailable = _build_unavailable(kpis, spend_truth, period_change, deal_proof_available,
                                     deal_proof_reason)

    # PR-ADS-154C-F1: per-metric provenance. Country-attributed revenue is a
    # DISTINCT identity from all-source business revenue, and the scope declared
    # here is what the audit checks that distinction against.
    _geo_ready = bool(spend_truth.get("country_roas_unblockable")
                      or spend_truth.get("geo_ready"))
    # PR-ADS-154C-F2 §4: country REVENUE needs the closed-won deal proof as well.
    # Geo readiness says the spend side is placed; it is silent on whether the
    # deals behind the revenue were readable, so a page with ready geo coverage
    # and an unreadable ledger must not declare its revenue ready.
    _country_revenue_ready = bool(_geo_ready and deal_proof_available)
    _country_revenue_status = (canonical_contract.TRUTH_READY
                               if _country_revenue_ready
                               else canonical_contract.TRUTH_NOT_READY)
    # PR-ADS-155-F1. The REVENUE metric and the CUSTOMER metric shared one status,
    # which is wrong in both directions. A count is complete whatever the amounts
    # are, so customers may stay ready while the total is withheld; and the total
    # is not ready merely because geo coverage is placed. Two facts, two statuses,
    # each proved from what it actually depends on.
    _country_revenue_verdict = core.get("country_revenue_verdict") or {}
    _country_total_ok = bool(_country_revenue_ready
                             and _country_revenue_verdict.get("publishable"))
    # PR-ADS-167A — withheld means absent. Every figure below is a statement
    # about country-attributed geography. When geo is not ready the page
    # declared them `not_ready` and published them anyway: spend, revenue, the
    # deal count, and the SQL split whose partition geo decides. Open windows
    # end yesterday on this page (owner decision 2026-11-20), so after the
    # morning sync they are ready; before it — yesterday not yet re-read — and
    # for any other geo gap, the cross-page parity audit and the contract test
    # (`test_f3_15`) would report it. They are absent instead.
    # Native spend is the one figure an FX-only gap leaves usable (the gate's
    # own wording: "native spend is shown instead"); it is withheld only when
    # the geo COVERAGE itself is not complete.
    _geo_gap_reason = next(iter(spend_truth.get("country_gap_codes") or []),
                           None) or "country_geo_not_ready"
    # PR-ADS-167A: over a window with no completed day EVERY metric names that.
    _no_day = (_geo_gap_reason if _geo_gap_reason
               == canonical_contract.NO_SETTLED_DAY_IN_WINDOW else None)
    if not _geo_ready:
        # The figures were withheld above (`_withhold_when_geo_not_ready`).
        # Say why, in the gate's own words — not "requires verified FX".
        _why = (geo_gate.describe_geo_gap(spend_truth.get("country_gap_codes"))
                or "Country-attributed spend is withheld: geo coverage is not ready.")
        unavailable = [u for u in unavailable
                       if u.get("metric") not in ("verified_spend_usd", "sqls")]
        unavailable += [{"metric": "verified_spend_usd", "reason": _why},
                        {"metric": "sqls", "reason": _why}]
    _mt = canonical_contract.metric_truth_block(core["window_block"], [
        # The total: ready only when geo coverage is placed AND every
        # country-attributed won deal has a proven amount. Withheld carries the
        # canonical reason and codes, so the audit can tell an unpriced-deal
        # blocker apart from a geo outage.
        {"metric": "country_attributed_won_revenue_usd",
         "data_source": canonical_contract.SOURCE_CANONICAL_GEO,
         "scope": "country_attributed_revenue",
         "truth_status": (canonical_contract.TRUTH_READY if _country_total_ok
                          else canonical_contract.TRUTH_NOT_READY),
         # When the scoped verdict names a blocker, that is the reason. When the
         # verdict is fine but geo coverage is not placed, the reason is the geo
         # gate's — never left blank, which is what forced the audit's generic
         # catch-all in production.
         "unavailable_reason": (None if _country_total_ok else _no_day or (
             _country_revenue_verdict.get("reason")
             or canonical_revenue.REASON_COVERAGE_NOT_PROVEN)),
         "violation_codes": ([] if _country_total_ok
                             else _country_revenue_verdict.get("violation_codes") or []),
         "customer_id": spend_truth.get("customer_id")},
        # The count, on its own readiness. Blanking a number we did measure would
        # be its own fabrication. It states a reason when withheld for the same
        # reason the total does: an absence nobody explains forces the audit back
        # to its generic catch-all for a blocker the page already knows.
        {"metric": "country_attributed_closed_won_deals",
         "data_source": canonical_contract.SOURCE_CANONICAL_GEO,
         "scope": "country_attributed_revenue",
         "truth_status": _country_revenue_status,
         "unavailable_reason": (None if _country_revenue_ready else _no_day or (
             core.get("revenue_unavailable_reason") if not deal_proof_available
             else REASON_REVENUE_NOT_CONNECTED if not _revenue_connected
             else canonical_revenue.REASON_COVERAGE_NOT_PROVEN)),
         "violation_codes": ([] if _country_revenue_ready
                             else (core.get("revenue_violation_codes") or []
                                   if not deal_proof_available else [])),
         "customer_id": spend_truth.get("customer_id")},
        # Country-attributed SPEND is a different question from the full-account
        # denominator the Overview and the mart publish: this figure is the sum of
        # the per-country rows, and geographic_view does not place location-less
        # spend in any of them. Registered under its own identity so the two are
        # never compared (PR-ADS-131's governed residual is the difference).
        {"metric": "country_attributed_spend_usd",
         "data_source": canonical_contract.SOURCE_CANONICAL_GEO,
         "scope": "country_attributed_spend",
         "truth_status": (canonical_contract.TRUTH_READY
                          if (_geo_ready and kpis.get("verified_spend_usd") is not None)
                          else canonical_contract.TRUTH_NOT_READY),
         # PR-ADS-167A: a withheld figure names why. PR-ADS-176A truth audit
         # MINOR 3: in a ready window the zero rule can withhold it too.
         "unavailable_reason": (_geo_gap_reason if not _geo_ready
                                else kpis.get("verified_spend_reason")
                                if kpis.get("verified_spend_usd") is None else None),
         "customer_id": spend_truth.get("customer_id")},
        # PR-ADS-154C-F3: this page's SQL headline is the REAL-COUNTRY total —
        # 14 against the mart's 16 in the production quarter — because the SQLs
        # it could not assign to a country are held in the residual below, never
        # spread across countries. Declaring it `campaign_attributable_sqls` said
        # a subset was the whole population, and the parity audit correctly
        # reported the difference as a mismatch between two numbers that were
        # both right about different questions.
        # PR-ADS-154C-F3-F1 §3: these SQLs come from the canonical lead
        # population the DECISION MART aggregates, partitioned by country
        # assignment. Geo decides which side of the partition an SQL falls on;
        # it does not produce the SQL, so naming it as the source misstated the
        # data's origin. Geo coverage stays a prerequisite in the audit's
        # `coverage_proof`, where a prerequisite belongs.
        {"metric": "country_attributed_sqls",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "country_attributed_sqls",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("sqls") is not None
                          else canonical_contract.TRUTH_NOT_READY),
         "unavailable_reason": (None if _geo_ready else _geo_gap_reason),
         "customer_id": spend_truth.get("customer_id")},
        {"metric": "country_unattributed_residual_sqls",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "country_unattributed_residual_sqls",
         "truth_status": (canonical_contract.TRUTH_READY
                          if residual.get("sqls") is not None
                          else canonical_contract.TRUTH_NOT_READY),
         "unavailable_reason": (None if _geo_ready else _geo_gap_reason),
         "customer_id": spend_truth.get("customer_id")},
    ])

    return {
        "window": core["window_block"],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        canonical_contract.METRIC_TRUTH_KEY: _mt,
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
        # PR-ADS-155-F1-F1: the DEAL denominators behind the country revenue
        # verdict — complete count, priced, unpriced. Published so the withheld
        # total's disclosure counts deals rather than countries, and so a reader
        # can check the arithmetic instead of trusting the verdict's word.
        "country_revenue_disclosure": {
            **(core.get("country_revenue_deal_counts") or {}),
            "scope": "country_attributed_revenue",
            "total_revenue_usd": (kpis.get("won_revenue_usd")
                                  if _country_total_ok else None),
            "total_revenue_publishable": _country_total_ok,
            "unavailable_reason": (None if _country_total_ok
                                   else _country_revenue_verdict.get("reason")),
            "violation_codes": ([] if _country_total_ok
                                else _country_revenue_verdict.get(
                                    "violation_codes") or []),
            "source": canonical_revenue.CANONICAL_SOURCE,
            "fallback_used": False,
        },
        "kpis": kpis,
        "period_change": period_change,
        "countries": country_rows,
        # PR-ADS-170A: the window's Google criteria whose country is the
        # owner's label, and any labelled criterion a stored Google code
        # overrode. A row it applies to carries `spend_country_provenance`.
        "owner_labelled_geo": core.get("owner_labelled_geo")
        or {"criteria": [], "mismatches": []},
        "residual": residual,
        "regional_mix": regional_mix,
        "decision_cards": decision_cards,
        "truth_status": truth_status,
        "readiness": readiness,
        "unavailable": unavailable,
    }
