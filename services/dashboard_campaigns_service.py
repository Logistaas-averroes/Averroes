"""
Dashboard Campaigns & Keywords (PR-ADS-137)

The fourth Dashboard tab (Dashboard → Campaigns). It answers, in one screen:
which Google Ads campaigns and keyword/search-intent themes are producing real
business outcomes (SQLs / customers / closed-won revenue), and which are
spending without proof?

Composition doctrine — this service computes NO new business math and adds NO
new taxonomy. It composes canonical read-only contracts:

  - build_revenue_decision_mart(view="campaign")  -> the canonical per-campaign
      rows (spend / SQLs / customers / won revenue / ROAS / mapping state) plus
      the shared spend-truth (native GBP + FX-gated USD) and readiness. This is
      the ROAS-by-Campaign truth, re-presented — never re-computed.
  - fetch_canonical_campaign_spend                -> per-campaign native (GBP)
      AND USD spend + FX completeness (the only place with a per-campaign
      native/USD split).
  - fetch_revenue_deals                           -> the closed-won deal ledger
      (per-deal null-safe amounts) for drawer proof and to preserve revenue that
      maps to no Google Ads campaign under "Unattributed / Needs Review".
  - fetch_search_term_signals                     -> window-scoped search-term
      waste-analysis signals (read-only evidence).
  - fetch_keyword_theme_snapshot                  -> latest keyword snapshot
      (recent spend/clicks by theme; NOT window-scoped, NO outcome attribution).

Truth rules (unchanged, never loosened):
  - Google Ads API = spend truth (native GBP). HubSpot closed-won = revenue truth
    (USD). Native GBP is never labelled USD. Reporting revenue is USD.
  - ROAS is campaign-level only, only when spend + FX + revenue are safe, and is
    never derived from the Google Ads conversion value.
  - No fake $0 / 0.00x / 0%. A missing amount stays null; a total that needs
    completeness becomes Unavailable when any part is unknown; a missing prior
    period yields "No comparison".
  - Revenue that maps to no campaign is preserved under "Unattributed / Needs
    Review", never dropped, never forced onto a campaign.
  - Keywords / search terms are READ-ONLY evidence: no negative-keyword writes,
    no bid/budget actions. Keyword themes carry NO outcome attribution and NO
    ROAS (the durable data has none). Search-term panels present the existing
    waste-analysis classification only.
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
from services import canonical_contract
from analysis import revenue_scope
from db import revenue_repository as repo
from services import canonical_revenue_service as canonical_revenue
from services.revenue_decision_mart import build_revenue_decision_mart

# Reuse the Overview tab's pure window/delta helpers (PR-ADS-134/135/136).
from services.dashboard_overview_service import (
    _coerce_utc,
    _delta_metric,
    _parse_iso_date,
    _previous_window_reference,
    _round2,
)

logger = logging.getLogger(__name__)


# ── Campaign status copy (business-facing) ───────────────────────────────────
STATUS_REVENUE_PROVEN = "Revenue proven"
STATUS_SQL_PRODUCER = "SQL producer"
STATUS_SPEND_NO_PROOF = "Spend without SQL / customer proof"
STATUS_ATTRIBUTION_MISSING = "Revenue attribution missing"
STATUS_FX_UNAVAILABLE = "FX unavailable"
STATUS_MAPPING_REVIEW = "Mapping needs review"

UNATTRIBUTED_LABEL = "Unattributed / Needs Review"


def _norm(label) -> str:
    """Normalise a campaign label for matching (lowercase, collapsed spaces)."""
    if not label:
        return ""
    return " ".join(str(label).replace("_", " ").strip().lower().split())


def _roas(won, usd_spend):
    """Campaign ROAS = won revenue / USD spend, only when both are safe.

    None when USD spend is unavailable/zero, revenue is unknown, OR revenue is a
    non-positive (zero) return — a spend-with-no-revenue campaign is conveyed by
    its status, never a fabricated / misleading 0.00x, and never from native GBP
    or the Google Ads conversion value.
    """
    if won is None or usd_spend is None or usd_spend <= 0 or won <= 0:
        return None
    return round(float(won) / float(usd_spend), 2)


def _canonical_spend_by_campaign(spend_result: dict) -> dict:
    """Map canonical Google Ads spend rows by campaign_id AND normalised name.

    Each entry carries native (GBP) spend, USD spend (None on FX gaps), and FX
    completeness — the only per-campaign native/USD split available.
    """
    by_id: dict = {}
    by_name: dict = {}
    for r in (spend_result.get("rows") or []):
        entry = {
            "native": r.get("spend"),
            "usd": r.get("spend_usd"),
            "fx_complete": bool(r.get("fx_complete")),
        }
        if r.get("campaign_id") is not None:
            by_id[str(r["campaign_id"])] = entry
        if r.get("campaign_name"):
            by_name[_norm(r["campaign_name"])] = entry
    return {"by_id": by_id, "by_name": by_name}


def _deal_proof_row(deal: dict, attribution_status: str) -> dict:
    """One closed-won deal proof row for a campaign drawer.

    Only durably-stored fields are populated. Company id, main contact, contact
    id and the deal NAME are not persisted locally, so they are explicit None →
    the UI renders "Unavailable", never a fabricated value. A missing amount
    stays None (never a fake $0).
    """
    amount = deal.get("deal_amount_usd")
    return {
        "deal_id": deal.get("deal_id"),
        # PR-ADS-153E-B: the canonical ledger stores the deal's own name and no
        # company field; `company` is honestly Unavailable rather than filled
        # with a deal name under a Company heading.
        "deal_name": deal.get("deal_name"),
        "company": None,
        "company_id": None,
        "main_contact": None,
        "contact_id": deal.get("primary_contact_id"),
        "deal": deal.get("deal_name"),
        "amount_usd": _round2(amount) if amount is not None else None,
        "currency_status": deal.get("currency_status"),
        "close_date": deal.get("deal_close_date"),
        "country": deal.get("country"),
        "attribution_status": attribution_status,
        "attribution_scope": deal.get("attribution_scope"),
    }


def _deal_attribution_status(deal: dict, mapped: bool) -> str:
    """Attribution state for a canonical deal on a campaign row.

    PR-ADS-153E-B: uses the ledger's own ``attribution_status`` — decided once by
    the shared association resolver — instead of inferring it from a GCLID
    match_status. A deal whose contacts disagree stays ``ambiguous`` and is never
    silently promoted to attributed.
    """
    if not mapped:
        return "unattributed"
    status = (deal.get("attribution_status") or "").strip().lower()
    if status == "ambiguous":
        return "ambiguous"
    return "attributed" if status == "attributed" else "ambiguous"


def _group_deals_by_campaign(deals_rows: list, label_to_canonical: dict) -> tuple[dict, set]:
    """Group closed-won deals onto canonical campaigns, else Unattributed.

    Returns (deals_by_key, unknown_amount_keys) where deals_by_key maps a canonical
    campaign_id (or the sentinel UNATTRIBUTED_LABEL) to its proof rows, and
    unknown_amount_keys is the set of keys holding a deal with an unknown amount
    (so that key's revenue total renders Unavailable, never a lowered $0).
    """
    deals_by_key: dict = {}
    unknown_amount_keys: set = set()
    for deal in deals_rows or []:
        canonical = label_to_canonical.get(_norm(deal.get("campaign_name")))
        mapped = canonical is not None
        key = canonical if mapped else UNATTRIBUTED_LABEL
        status = _deal_attribution_status(deal, mapped)
        deals_by_key.setdefault(key, []).append(_deal_proof_row(deal, status))
        if deal.get("deal_amount_usd") is None:
            unknown_amount_keys.add(key)
    return deals_by_key, unknown_amount_keys


def _campaign_status(*, native_spend, usd_spend, fx_complete, sqls, customers,
                     won_revenue, spend_state, amount_unknown) -> str:
    """Business status for a campaign row (never an engineering term)."""
    has_spend = native_spend is not None and native_spend > 0
    unmapped = spend_state in (None, "unmapped") and not has_spend
    if (customers or 0) > 0 and (won_revenue or 0) > 0:
        return STATUS_REVENUE_PROVEN
    if amount_unknown and (customers or 0) > 0:
        # Customers exist but their revenue can't be totalled → attribution review.
        return STATUS_ATTRIBUTION_MISSING
    if (sqls or 0) > 0 or (customers or 0) > 0:
        # Demand (SQLs) and/or customers, but no proven closed-won revenue yet.
        return STATUS_SQL_PRODUCER
    if has_spend:
        # Spend exists but no SQL/customer proof. If the USD spend is missing only
        # because FX is incomplete, say so; otherwise it's genuinely unproven.
        if usd_spend is None and not fx_complete:
            return STATUS_FX_UNAVAILABLE
        return STATUS_SPEND_NO_PROOF
    if unmapped:
        return STATUS_MAPPING_REVIEW
    return STATUS_SPEND_NO_PROOF


# Google Ads spend mapping state (from the mart's spend_mapping) → UI label. The
# mart distinguishes "matched" from "unavailable" — never collapse them, so the
# UI can tell "mapping doesn't exist" from "mapping data unavailable".
_MAPPING_STATUS = {"matched": "mapped", "unavailable": "mapping_unavailable"}


def _build_campaign_rows(mart_rows: list, spend_maps: dict, deals_by_key: dict,
                         unknown_amount_keys: set, native_currency: str = "GBP") -> list:
    """Merge mart campaign rows (outcomes + mapping) with the per-campaign native/
    USD spend split and the closed-won proof deals. Preserves revenue that maps to
    no campaign under an Unattributed / Needs Review row."""
    rows = []
    for m in mart_rows or []:
        cid = str(m.get("campaign_id")) if m.get("campaign_id") is not None else None
        spend = (spend_maps["by_id"].get(cid)
                 or spend_maps["by_name"].get(_norm(m.get("campaign_name")))
                 or {})
        native_spend = spend.get("native")
        usd_spend = spend.get("usd")
        fx_complete = bool(spend.get("fx_complete"))

        key = cid if cid is not None else _norm(m.get("campaign_name"))
        amount_unknown = key in unknown_amount_keys
        # Won revenue: the mart's canonical per-campaign total, blanked to None if
        # any of this campaign's deals has an unknown amount (never a lowered $0).
        won = None if amount_unknown else _round2(m.get("won_revenue"))
        row_roas = _roas(won, usd_spend)

        status = _campaign_status(
            native_spend=native_spend, usd_spend=usd_spend, fx_complete=fx_complete,
            sqls=m.get("sqls"), customers=m.get("customers"), won_revenue=won,
            spend_state=m.get("spend_state"), amount_unknown=amount_unknown)

        rows.append({
            "campaign_id": m.get("campaign_id"),
            "campaign_name": m.get("campaign_name"),
            "spend_native": native_spend,
            "native_currency": native_currency,
            "spend_usd": usd_spend,
            "fx_complete": fx_complete,
            "sqls": m.get("sqls"),
            "customers": m.get("customers"),
            "won_revenue_usd": won,
            "roas": row_roas,
            "roas_available": row_roas is not None,
            "status": status,
            "spend_state": m.get("spend_state"),
            "attribution_status": _MAPPING_STATUS.get(m.get("spend_mapping"), "mapping_unavailable"),
            "deals": deals_by_key.get(cid) or deals_by_key.get(key) or [],
            "target_page": "roas-campaigns",
        })

    # Preserve closed-won revenue that maps to no Google Ads campaign.
    unattributed_deals = deals_by_key.get(UNATTRIBUTED_LABEL) or []
    if unattributed_deals:
        amount_unknown = UNATTRIBUTED_LABEL in unknown_amount_keys
        customers = len(unattributed_deals)
        won = None if amount_unknown else _round2(
            sum((d["amount_usd"] or 0.0) for d in unattributed_deals))
        rows.append({
            "campaign_id": None,
            "campaign_name": UNATTRIBUTED_LABEL,
            "spend_native": None,
            "native_currency": native_currency,
            "spend_usd": None,
            "fx_complete": False,
            "sqls": None,
            "customers": customers,
            "won_revenue_usd": won,
            "roas": None,
            "roas_available": False,
            "status": STATUS_ATTRIBUTION_MISSING,
            "spend_state": None,
            "attribution_status": "unattributed",
            "deals": unattributed_deals,
            "target_page": "revenue-health",
        })

    # Google Ads campaigns first (by revenue, then customers, then spend); the
    # unattributed bucket sinks to the bottom.
    rows.sort(key=lambda r: (
        r["campaign_name"] != UNATTRIBUTED_LABEL,
        r.get("won_revenue_usd") or 0.0,
        r.get("customers") or 0,
        r.get("spend_native") or 0.0,
    ), reverse=True)
    return rows


def _build_kpis(campaign_rows: list, spend_truth: dict) -> dict:
    """Hero KPIs derived from the campaign rows (so they reconcile with the
    leaderboard / bubble map) plus the canonical spend truth."""
    # KPIs are Google-Ads-scoped (exclude the Unattributed / Needs Review callout),
    # so "Won Revenue" and ROAS reconcile with each other and with the canonical
    # mart — the unattributed row is surfaced separately, never in the ROAS numerator.
    google_rows = [r for r in campaign_rows if r["campaign_name"] != UNATTRIBUTED_LABEL]
    campaigns_with_spend = sum(1 for r in google_rows
                               if (r.get("spend_native") or 0) > 0)

    # SQLs: from the rows; Unavailable if any row withholds them (unsafe date grain).
    sqls_incomplete = any(r.get("sqls") is None for r in google_rows)
    total_sqls = None if sqls_incomplete else sum((r.get("sqls") or 0) for r in google_rows)

    # Customers: Unavailable if any row withholds them (a disconnected revenue
    # integration blanks them) — never a fabricated 0.
    customers_incomplete = any(r.get("customers") is None for r in google_rows)
    total_customers = (None if customers_incomplete
                       else sum((r.get("customers") or 0) for r in google_rows))

    # Revenue: Unavailable if ANY Google campaign's revenue is unknown (never a
    # partial sum implying completeness).
    revenue_incomplete = any(r.get("won_revenue_usd") is None for r in google_rows)
    total_won = (None if revenue_incomplete
                 else _round2(sum((r.get("won_revenue_usd") or 0.0) for r in google_rows)))

    native = spend_truth.get("native_spend")
    usd = spend_truth.get("usd_spend")
    return {
        "campaigns_with_spend": campaigns_with_spend,
        "verified_spend_native": {"amount": native, "currency": spend_truth.get("native_currency") or "GBP"},
        "verified_spend_usd": usd,
        "sqls": total_sqls,
        "customers": total_customers,
        "won_revenue_usd": total_won,
        "roas": _roas(total_won, usd),
    }


# ── Keyword themes (presentation grouping; recent snapshot, no attribution) ───

KEYWORD_THEME_ORDER = [
    "brand", "competitor", "logistics_software", "tms_transport",
    "compliance_customs", "country_market", "other_terms", "needs_review",
]
KEYWORD_THEME_LABELS = {
    "brand": "Brand",
    "competitor": "Competitor",
    "logistics_software": "Freight / Logistics Software",
    "tms_transport": "TMS / Transport Management",
    "compliance_customs": "Compliance / Customs",
    "country_market": "Country / Market",
    "other_terms": "Other terms",
    "needs_review": "Unknown / Needs Review",
}
_THEME_RULES = [
    ("brand", ("logistaas",)),
    ("competitor", ("competitor", "vs ", " alternative", "compare")),
    ("compliance_customs", ("customs", "compliance", "clearance", "duty", "tariff")),
    ("tms_transport", ("tms", "transport management", "fleet", "dispatch")),
    ("logistics_software", ("freight", "forwarding", "logistics software", "logistics",
                            "shipping software", "supply chain", "warehouse")),
    ("country_market", ("uae", "saudi", "qatar", "kuwait", "egypt", "africa",
                        "middle east", "gcc")),
]


def _classify_keyword_theme(keyword) -> str:
    """Presentation-only theme for a keyword's text. Best-effort grouping — this
    is NOT outcome attribution and never implies a keyword caused revenue."""
    kw = _norm(keyword)
    if not kw:
        return "needs_review"
    for theme, needles in _THEME_RULES:
        if any(n in kw for n in needles):
            return theme
    return "other_terms"


def _build_keyword_themes(snapshot: dict) -> dict:
    """Recent keyword spend/clicks grouped by theme. Explicitly NOT window-scoped
    and carrying NO outcome attribution — SQLs / customers / revenue / ROAS are
    never fabricated for a keyword theme."""
    if not snapshot.get("available") or not (snapshot.get("rows") or []):
        return {"status": "unavailable", "run_date": snapshot.get("run_date"),
                "window_scoped": False, "themes": [],
                "reason": "Keyword-level spend is a recent scheduler snapshot without "
                          "business-window or outcome attribution."}
    buckets: dict = {}
    for r in snapshot["rows"]:
        theme = _classify_keyword_theme(r.get("keyword"))
        b = buckets.setdefault(theme, {"spend_usd": 0.0, "clicks": 0, "keywords": 0})
        b["spend_usd"] += float(r.get("spend_usd") or 0.0)
        b["clicks"] += int(r.get("clicks") or 0)
        b["keywords"] += 1
    themes = []
    for theme in KEYWORD_THEME_ORDER:
        b = buckets.get(theme)
        if not b:
            continue
        themes.append({
            "theme": theme,
            "theme_label": KEYWORD_THEME_LABELS[theme],
            "spend_usd": _round2(b["spend_usd"]),
            "clicks": b["clicks"],
            "keywords": b["keywords"],
            # No outcome attribution exists for keywords → these stay Unavailable.
            "sqls": None,
            "customers": None,
            "won_revenue_usd": None,
            "roas": None,
            "status": "Recent spend / clicks — no outcome attribution",
        })
    themes.sort(key=lambda t: t.get("spend_usd") or 0.0, reverse=True)
    return {"status": "ready", "run_date": snapshot.get("run_date"),
            "window_scoped": False, "themes": themes, "reason": None}


# ── Search-term signals (window-scoped waste-analysis evidence, read-only) ────

_SIGNAL_LIMIT = 8


def _build_search_term_signals(result: dict) -> dict:
    """Bucket window-scoped search terms by the durable waste-analysis tri-state:
    value (reviewed clean), waste (flagged), needs-review (unanalyzed). READ-ONLY
    evidence — there is NO outcome attribution, so no term claims revenue."""
    if not result.get("available"):
        return {"status": "unavailable", "value_terms": [], "waste_terms": [],
                "needs_review": [],
                "reason": "Search-term signals are unavailable for this window."}
    value: list = []
    waste: list = []
    review: list = []
    for r in (result.get("rows") or []):
        term = {
            "search_term": r.get("search_term"),
            "campaign_name": r.get("campaign_name"),
            "spend_usd": _round2(r.get("spend_usd")),
            "clicks": int(r.get("clicks") or 0),
            "junk_category": r.get("junk_category"),
        }
        flag = r.get("is_flagged_waste")
        if flag is True:
            waste.append(term)
        elif flag is False:
            value.append(term)
        else:
            review.append(term)

    def _top(items):
        return sorted(items, key=lambda t: t.get("spend_usd") or 0.0, reverse=True)[:_SIGNAL_LIMIT]

    return {
        "status": "ready",
        # "value" = reviewed clean (not flagged as waste); NOT a revenue claim.
        "value_terms": _top(value),
        "waste_terms": _top(waste),
        "needs_review": _top(review),
        "counts": {"value": len(value), "waste": len(waste), "needs_review": len(review)},
        "reason": None,
    }


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


def _build_decision_cards(campaign_rows: list, kpis: dict, keyword_themes: dict,
                          search_signals: dict, spend_truth: dict) -> list:
    cards = []
    google_rows = [r for r in campaign_rows if r["campaign_name"] != UNATTRIBUTED_LABEL]

    # Scale — the top revenue-proven campaign.
    proven = [r for r in google_rows if r["status"] == STATUS_REVENUE_PROVEN]
    if proven:
        top = max(proven, key=lambda r: (r.get("won_revenue_usd") or 0.0,
                                         r.get("customers") or 0))
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": f"{top['campaign_name']} has revenue proof",
            "body": (f"This campaign produced {top['customers']} customer"
                     f"{'s' if top['customers'] != 1 else ''} and "
                     f"{_fmt_usd_short(top['won_revenue_usd'])} closed-won revenue with "
                     "verified Google Ads spend."),
            "target_page": "roas-campaigns", "target_label": "Open ROAS by Campaign",
        })
    else:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "No campaign has revenue proof yet",
            "body": "No Google Ads campaign has both verified spend and closed-won "
                    "customers in this window.",
            "target_page": "roas-campaigns", "target_label": "Open ROAS by Campaign",
        })

    # Watch — spend without customer proof.
    no_proof = [r for r in google_rows if r["status"] in
                (STATUS_SPEND_NO_PROOF, STATUS_SQL_PRODUCER)
                and (r.get("spend_native") or 0) > 0 and (r.get("customers") or 0) == 0]
    if no_proof:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "Campaign spend without customer proof",
            "body": (f"{len(no_proof)} campaign{'s' if len(no_proof) != 1 else ''} spent "
                     "verified Google Ads budget but produced no customers in this "
                     "window. Review SQL quality before scaling."),
            "target_page": "roas-campaigns", "target_label": "Open ROAS by Campaign",
        })
    else:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "No unproven spend",
            "body": "Every spending campaign produced at least one customer, or has "
                    "no verified spend to judge.",
            "target_page": "roas-campaigns", "target_label": "Open ROAS by Campaign",
        })

    # Investigate — waste search terms or unattributed revenue.
    waste_count = (search_signals.get("counts") or {}).get("waste", 0)
    unattributed = next((r for r in campaign_rows
                         if r["campaign_name"] == UNATTRIBUTED_LABEL), None)
    if waste_count > 0:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Search terms flagged as possible waste",
            "body": (f"{waste_count} search term{'s' if waste_count != 1 else ''} spent "
                     "with a waste flag this window. Review in Search Terms — this is "
                     "evidence only, no platform write."),
            "target_page": "search-terms", "target_label": "Review in Search Terms",
        })
    elif unattributed:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Closed-won revenue not mapped to a campaign",
            "body": (f"{_fmt_usd_short(unattributed.get('won_revenue_usd'))} closed-won "
                     "revenue could not be attributed to a Google Ads campaign. Review "
                     "campaign attribution."),
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })
    else:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "No waste or attribution flags",
            "body": "No flagged waste terms and all closed-won revenue maps to a "
                    "campaign this window.",
            "target_page": "search-terms", "target_label": "Open Search Terms",
        })

    # Unavailable — explain WHY ROAS / USD spend is withheld, ACCURATELY (ROAS is
    # None either because FX/USD is incomplete, revenue is incomplete, or there is
    # simply no closed-won revenue yet — these read very differently).
    fx_ok = spend_truth.get("fx_status") == "verified"
    usd_present = kpis.get("verified_spend_usd") is not None
    revenue_known = kpis.get("won_revenue_usd") is not None
    if not fx_ok or not usd_present:
        headline = "USD spend & ROAS unavailable"
        body = ("USD spend and ROAS are withheld because FX coverage is incomplete — "
                "native GBP spend is still shown, never a fabricated USD or ROAS.")
    elif not revenue_known:
        headline = "ROAS unavailable"
        body = ("ROAS is withheld because some closed-won revenue is incomplete (an "
                "unknown deal amount) — never a lowered $0.")
    elif kpis.get("roas") is None:
        headline = "ROAS not yet meaningful"
        body = ("There is no closed-won revenue yet to measure against this window's "
                "verified spend, so ROAS is not shown — never a fabricated 0.00x.")
    else:
        headline = "Keyword outcome attribution unavailable"
        body = ("Spend, revenue and ROAS are verified for this window. Keyword-level "
                "SQLs / customers / revenue have no durable attribution — keyword and "
                "search-term panels are read-only evidence.")
    cards.append({
        "type": "unavailable", "title": "Unavailable",
        "headline": headline, "body": body,
        "target_page": "revenue-health", "target_label": "Open Revenue Health",
    })
    return cards


def _build_truth_status(spend_truth: dict, readiness: dict, campaign_rows: list,
                        keyword_themes: dict, search_signals: dict,
                        deal_proof_available: bool = True) -> dict:
    fx = spend_truth.get("fx_status")
    cov = spend_truth.get("campaign_spend_status")
    if fx == "verified" and cov == "verified":
        google_ads_spend = "ready"
    elif cov == "incomplete" or fx == "incomplete":
        google_ads_spend = "partial"
    else:
        google_ads_spend = "unavailable"

    # Campaign attribution is "ready" ONLY when every campaign row is cleanly
    # mapped: any unattributed bucket, any mapping-unavailable row, or any
    # "Mapping needs review" status makes it partial.
    attribution_partial = any(
        r["campaign_name"] == UNATTRIBUTED_LABEL
        or r.get("attribution_status") != "mapped"
        or r.get("status") == STATUS_MAPPING_REVIEW
        for r in campaign_rows
    )
    return {
        "spend": google_ads_spend,
        "fx": "ready" if fx == "verified" else ("partial" if fx == "incomplete" else "unavailable"),
        "revenue": "ready" if readiness.get("revenue_integration_connected") else "blocked",
        "campaign_attribution": "partial" if attribution_partial else "ready",
        # Client/deal proof comes from the closed-won deal ledger; when that is
        # down we cannot prove campaigns even if the mart revenue is canonical.
        "deal_proof": "ready" if deal_proof_available else "unavailable",
        "keyword_attribution": "unavailable",  # no keyword→outcome attribution exists
        "search_terms": search_signals.get("status", "unavailable"),
    }


def _build_unavailable(kpis: dict, spend_truth: dict, keyword_themes: dict,
                       search_signals: dict, period_change: dict,
                       deal_proof_available: bool = True) -> list:
    out = []
    if kpis.get("verified_spend_usd") is None:
        out.append({"metric": "verified_spend_usd",
                    "reason": "USD spend requires verified FX; native GBP spend is shown instead."})
    if kpis.get("roas") is None:
        # State the ACTUAL reason ROAS is None (aligned with the decision card):
        # FX/USD incomplete, revenue incomplete, or simply no closed-won revenue.
        fx_ok = spend_truth.get("fx_status") == "verified"
        usd_present = kpis.get("verified_spend_usd") is not None
        if not fx_ok or not usd_present:
            roas_reason = ("Campaign ROAS requires verified FX / USD spend; native GBP "
                           "spend is shown instead.")
        elif kpis.get("won_revenue_usd") is None:
            roas_reason = ("Campaign ROAS requires complete revenue — an unknown deal "
                           "amount makes it incomplete.")
        else:
            roas_reason = "ROAS not yet meaningful; no closed-won revenue in this window."
        out.append({"metric": "roas", "reason": roas_reason})
    if not deal_proof_available:
        out.append({"metric": "deal_proof",
                    "reason": "Closed-won deal proof is unavailable — the deal ledger could "
                              "not be read. Campaign revenue may still be canonical from the mart."})
    if kpis.get("sqls") is None:
        out.append({"metric": "sqls",
                    "reason": "SQLs withheld — lead business event dates are unavailable."})
    # Keyword themes carry no outcome attribution — structurally unavailable.
    out.append({"metric": "keyword_outcome_attribution",
                "reason": "Keyword-level SQLs / customers / revenue / ROAS have no durable attribution."})
    if keyword_themes.get("status") != "ready":
        out.append({"metric": "keyword_themes",
                    "reason": keyword_themes.get("reason") or "Keyword themes unavailable."})
    if search_signals.get("status") != "ready":
        out.append({"metric": "search_term_signals",
                    "reason": search_signals.get("reason") or "Search-term signals unavailable."})
    if not period_change.get("available"):
        out.append({"metric": "period_change",
                    "reason": period_change.get("reason") or "No comparison available."})
    return out


def _previous_window_deals(prev_key, prev_now) -> dict:
    """Canonical won deals for the PREVIOUS window, for the comparison baseline.

    Same contract, same scope, same window resolver as the current period — a
    delta between two differently-defined populations is not a delta.
    """
    base = canonical_revenue.load_won_deals(prev_key, now=prev_now)
    if not base.get("available"):
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": canonical_revenue.canonical_deal_rows(
            base, revenue_scope.SCOPE_ALL_SOURCE),
    }


def _build_period_change(window: str, now: datetime, kpis: dict) -> dict:
    """Previous-period deltas via a shifted canonical mart build. Never a fake 0%."""
    ref = _previous_window_reference(window, now)
    if ref is None:
        return {"available": False, "label": None, "note": None, "metrics": {},
                "reason": "All Time has no previous period to compare against."}
    prev_key, prev_now, label, note = ref
    try:
        prev_mart = build_revenue_decision_mart(view="campaign", window=prev_key, now=prev_now)
        pw = prev_mart.get("window") or {}
        prev_deals = _previous_window_deals(prev_key, prev_now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dashboard campaigns previous-period build failed: %s", exc)
        return {"available": False, "label": label, "note": note, "metrics": {},
                "reason": "Previous period unavailable."}
    ps = prev_mart.get("summary") or {}
    pst = prev_mart.get("spend_truth") or {}
    connected = bool((prev_mart.get("readiness") or {}).get("revenue_integration_connected"))
    # The mart summary counts a null-amount deal as $0; if the previous window had
    # ANY unknown deal amount, its revenue total is not a trustworthy baseline —
    # withhold it (No comparison) rather than measuring against a lowered $0.
    prev_amount_unknown = (not prev_deals.get("available")) or any(
        d.get("deal_amount_usd") is None for d in (prev_deals.get("rows") or []))
    prev_rev = ps.get("won_revenue_usd") if (connected and not prev_amount_unknown) else None
    prev_cust = ps.get("customers") if connected else None
    metrics = {
        "won_revenue_usd": _delta_metric(kpis.get("won_revenue_usd"), prev_rev),
        "customers": _delta_metric(kpis.get("customers"), prev_cust),
        "sqls": _delta_metric(kpis.get("sqls"), ps.get("sqls")),
        "verified_spend_usd": _delta_metric(kpis.get("verified_spend_usd"), pst.get("usd_spend")),
        "roas": _delta_metric(kpis.get("roas"), ps.get("roas")),
    }
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label, "note": note, "metrics": metrics, "reason": None,
    }


def build_dashboard_campaigns(window: str = "current_quarter",
                              now: datetime | None = None) -> dict:
    """Build the Dashboard Campaigns & Keywords contract for one business window.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolved = resolve_canonical_window(window, now=now)
    ref_now = _coerce_utc(now)

    mart = build_revenue_decision_mart(view="campaign", window=window, now=now)
    window_block = mart.get("window") or {}
    mart_rows = mart.get("rows") or []
    spend_truth = mart.get("spend_truth") or {}
    readiness = mart.get("readiness") or {}
    revenue_connected = bool(readiness.get("revenue_integration_connected"))
    native_currency = spend_truth.get("native_currency") or "GBP"

    start = _parse_iso_date(window_block.get("start_date") or resolved.get("start_date"))
    end = _parse_iso_date(window_block.get("end_date") or resolved.get("end_date"))

    spend_result = repo.fetch_canonical_campaign_spend(start, end)
    spend_maps = _canonical_spend_by_campaign(spend_result)

    # Closed-won deal ledger: drawer proof + null-amount detection + preserving
    # revenue that maps to no campaign. Build the raw-label → canonical map first.
    label_to_canonical: dict = {}
    for m in mart_rows:
        cid = str(m.get("campaign_id")) if m.get("campaign_id") is not None else _norm(m.get("campaign_name"))
        for lbl in [m.get("campaign_name"), *(m.get("aliases") or [])]:
            if lbl:
                label_to_canonical[_norm(lbl)] = cid
    # The closed-won deal ledger backs drawer PROOF, null-amount detection, and the
    # Unattributed bucket. If it is unavailable we must NOT imply a campaign has no
    # deals — the mart revenue can stay canonical, but client/deal proof is withheld.
    # PR-ADS-153E-B: proof deals come from the canonical ledger at `all_source`
    # scope. Deliberately the WIDEST scope on a Google Ads page: a won deal that
    # maps to no canonical campaign is not dropped to make the campaign rows look
    # complete — it lands in the explicit Unattributed bucket, which is both the
    # coverage gap and the operator's mapping worklist. The campaign ROWS remain
    # campaign-scoped; only the residual is wider.
    canonical = canonical_revenue.load_won_deals(window, now=now)
    deal_proof_available = bool(canonical.get("available"))
    deal_rows = (canonical_revenue.canonical_deal_rows(
        canonical, revenue_scope.SCOPE_ALL_SOURCE)
        if deal_proof_available else [])
    deals_by_key, unknown_amount_keys = _group_deals_by_campaign(
        deal_rows, label_to_canonical)

    campaign_rows = _build_campaign_rows(mart_rows, spend_maps, deals_by_key,
                                         unknown_amount_keys, native_currency)

    if not revenue_connected:
        # Closed-won customers AND revenue come from the revenue integration, so
        # both are unknown when it is disconnected — blank them (spend / SQLs
        # survive). The mart returns a mart-level $0/0 here, which would otherwise
        # render as a VERIFIED zero. Never fabricate a $0 or a 0-customer proof.
        for c in campaign_rows:
            c["customers"] = None
            c["won_revenue_usd"] = None
            c["roas"] = None
            c["roas_available"] = False

    kpis = _build_kpis(campaign_rows, spend_truth)
    period_change = _build_period_change(window, ref_now, kpis)
    keyword_themes = _build_keyword_themes(repo.fetch_keyword_theme_snapshot())
    search_signals = _build_search_term_signals(repo.fetch_search_term_signals(start, end))
    decision_cards = _build_decision_cards(campaign_rows, kpis, keyword_themes,
                                           search_signals, spend_truth)
    truth_status = _build_truth_status(spend_truth, readiness, campaign_rows,
                                       keyword_themes, search_signals, deal_proof_available)
    unavailable = _build_unavailable(kpis, spend_truth, keyword_themes,
                                     search_signals, period_change, deal_proof_available)

    # PR-ADS-154C-F2 §3: per-metric provenance. Spend here is the FULL-account
    # canonical denominator — the same figure the Overview and the mart publish,
    # which is why it is registered as one identity with them — while revenue and
    # customers are the campaign-ATTRIBUTABLE subset summed from the Google Ads
    # rows, deliberately excluding the Unattributed bucket. Two different scopes
    # on one page; one response-level source name could describe neither.
    _resolved_window = {
        "key": window_block.get("key"), "start_date": window_block.get("start_date"),
        "end_date": window_block.get("end_date"), "timezone": window_block.get("timezone"),
    }
    _mt = canonical_contract.metric_truth_block(_resolved_window, [
        {"metric": "google_ads_spend_usd",
         "data_source": canonical_contract.SOURCE_CANONICAL_SPEND,
         "scope": "google_ads_campaign_spend",
         "truth_status": (canonical_contract.TRUTH_READY
                          if spend_truth.get("campaign_spend_status") == "verified"
                          else canonical_contract.TRUTH_NOT_READY),
         "customer_id": spend_truth.get("customer_id")},
        *[{"metric": m,
           "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
           "scope": "campaign_attributable_revenue",
           "truth_status": (canonical_contract.TRUTH_READY
                            if kpis.get(field) is not None
                            else canonical_contract.TRUTH_NOT_READY)}
          for m, field in (("campaign_attributed_won_revenue_usd", "won_revenue_usd"),
                           ("campaign_attributed_customers", "customers"))],
        {"metric": "campaign_attributable_sqls",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "campaign_attributable_sqls",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("sqls") is not None
                          else canonical_contract.TRUTH_NOT_READY)},
    ])

    return {
        "window": window_block,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        canonical_contract.METRIC_TRUTH_KEY: _mt,
        "source_truth": "revenue_decision_mart_campaign_view",
        # PR-ADS-153E-B: spend/campaign identity remain Google Ads; the closed-won
        # deals behind every campaign row are the canonical ledger.
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "revenue_available": deal_proof_available,
        "revenue_unavailable_reason": (None if deal_proof_available
                                       else canonical.get("reason")),
        "revenue_violation_codes": canonical.get("violation_codes") or [],
        "as_of": canonical.get("as_of"),
        "attribution_coverage": canonical_revenue.get_scope_ladder(base=canonical),
        "legacy_fallback_used": False,
        "google_ads_conversion_value_used": False,
        # Closed-won deal PROOF availability (separate from mart revenue truth):
        # false when the deal ledger could not be read, so drawers must not claim
        # a campaign has no deals.
        "deal_proof_available": deal_proof_available,
        "kpis": kpis,
        "period_change": period_change,
        "campaigns": campaign_rows,
        "keyword_themes": keyword_themes,
        "search_term_signals": search_signals,
        "decision_cards": decision_cards,
        "truth_status": truth_status,
        "readiness": readiness,
        "unavailable": unavailable,
    }
