"""
Dashboard Campaigns & Keywords (PR-ADS-137)

The fourth Dashboard tab (Dashboard → Campaigns). It answers, in one screen:
which Google Ads campaigns and keyword/search-intent themes are producing real
business outcomes (SQLs / customers / closed-won revenue), and which are
spending without proof?

Composition doctrine — this service computes NO new business math and adds NO
new taxonomy. It composes canonical read-only contracts:

  - build_revenue_decision_mart(view="campaign")  -> the canonical per-campaign
      rows (spend / SQLs / customers / won revenue / period ratio / mapping state) plus
      the shared spend-truth (native GBP + FX-gated USD) and readiness. This is
      the campaign revenue & spend truth, re-presented — never re-computed.
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
  - The spend ratio is ``period_revenue_per_ad_spend`` (PR-ADS-161E): revenue
    CLOSED in the window ÷ the same window's spend. A non-cohort operational
    ratio, never attribution ROAS; only when spend + FX + revenue are safe, and
    never derived from the Google Ads conversion value.
  - No fake $0 / 0.00x / 0%. A missing amount stays null; a total that needs
    completeness becomes Unavailable when any part is unknown; a missing prior
    period yields "No comparison".
  - Revenue that maps to no campaign is preserved under "Unattributed / Needs
    Review", never dropped, never forced onto a campaign.
  - Keywords / search terms are READ-ONLY evidence: no negative-keyword writes,
    no bid/budget actions. Keyword themes carry NO outcome attribution and NO
    spend ratio (the durable data has none). Search-term panels present the existing
    waste-analysis classification only.
  - Read-only. NO writes to Google Ads, HubSpot, budgets, bids, campaigns,
    keywords, negatives, or offline conversions.
"""

from __future__ import annotations

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
# PR-ADS-161E: the campaign's closed-won deal count is withheld (a Google Ads
# won deal on no campaign, an undated or conflicting won deal) or unreadable —
# it is UNKNOWN, so the campaign is not judged on it.
STATUS_OUTCOME_WITHHELD = "Closed-won outcome withheld"

UNATTRIBUTED_LABEL = "Unattributed / Needs Review"


def _norm(label) -> str:
    """Normalise a campaign label for matching (lowercase, collapsed spaces)."""
    if not label:
        return ""
    return " ".join(str(label).replace("_", " ").strip().lower().split())


def _period_ratio(won, usd_spend):
    """Campaign period revenue / ad spend = revenue closed in the window / the
    same window's USD spend, only when both are safe. Not attribution ROAS.

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


def _campaign_status(*, native_spend, usd_spend, fx_complete, sqls, deals,
                     won_revenue, spend_state, amount_unknown) -> str:
    """Business status for a campaign row (never an engineering term)."""
    if deals is None:
        # Unknown is not zero: never "spend without proof" over a withheld count.
        return STATUS_OUTCOME_WITHHELD
    has_spend = native_spend is not None and native_spend > 0
    unmapped = spend_state in (None, "unmapped") and not has_spend
    if (deals or 0) > 0 and (won_revenue or 0) > 0:
        return STATUS_REVENUE_PROVEN
    if amount_unknown and (deals or 0) > 0:
        # Customers exist but their revenue can't be totalled → attribution review.
        return STATUS_ATTRIBUTION_MISSING
    if (sqls or 0) > 0 or (deals or 0) > 0:
        # Demand (SQLs) and/or won deals, but no proven closed-won revenue yet.
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
                         unknown_amount_keys: set, native_currency: str = "GBP",
                         revenue_total_published: bool = True) -> list:
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
        row_ratio = _period_ratio(won, usd_spend)

        status = _campaign_status(
            native_spend=native_spend, usd_spend=usd_spend, fx_complete=fx_complete,
            sqls=m.get("sqls"), deals=m.get("closed_won_deals"), won_revenue=won,
            spend_state=m.get("spend_state"), amount_unknown=amount_unknown)

        rows.append({
            "campaign_id": m.get("campaign_id"),
            "campaign_name": m.get("campaign_name"),
            "spend_native": native_spend,
            "native_currency": native_currency,
            "spend_usd": usd_spend,
            "fx_complete": fx_complete,
            "sqls": m.get("sqls"),
            # PR-ADS-161E: closed-won DEALS, and this campaign's customers —
            # distinct proven HubSpot companies (not additive across rows).
            "closed_won_deals": m.get("closed_won_deals"),
            "customers": m.get("customers"),
            "metrics": m.get("metrics"),
            "won_revenue_usd": won,
            "period_revenue_per_ad_spend": row_ratio,
            "period_ratio_available": row_ratio is not None,
            "status": status,
            "spend_state": m.get("spend_state"),
            "attribution_status": _MAPPING_STATUS.get(m.get("spend_mapping"), "mapping_unavailable"),
            # Withheld means absent: no proof list beside a withheld count.
            "deals": (None if m.get("closed_won_deals") is None
                      else deals_by_key.get(cid) or deals_by_key.get(key) or []),
            "target_page": "roas-campaigns",
        })

    # Preserve closed-won revenue that maps to no Google Ads campaign.
    unattributed_deals = deals_by_key.get(UNATTRIBUTED_LABEL) or []
    if unattributed_deals:
        # PR-ADS-161E: a subset of the window — its total is published only
        # when the canonical verdict publishes the window's revenue.
        amount_unknown = (UNATTRIBUTED_LABEL in unknown_amount_keys
                          or not revenue_total_published)
        deals = len(unattributed_deals)
        won = None if amount_unknown else _round2(
            sum(d["amount_usd"] for d in unattributed_deals))
        rows.append({
            "campaign_id": None,
            "campaign_name": UNATTRIBUTED_LABEL,
            "spend_native": None,
            "native_currency": native_currency,
            "spend_usd": None,
            "fx_complete": False,
            "sqls": None,
            "closed_won_deals": deals,
            "customers": None,
            "won_revenue_usd": won,
            "period_revenue_per_ad_spend": None,
            "period_ratio_available": False,
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
        r.get("closed_won_deals") or 0,
        r.get("spend_native") or 0.0,
    ), reverse=True)
    return rows


def _build_kpis(campaign_rows: list, spend_truth: dict,
                summary: dict | None = None) -> dict:
    """Hero KPIs derived from the campaign rows (so they reconcile with the
    leaderboard / bubble map) plus the canonical spend truth."""
    # KPIs are Google-Ads-scoped (exclude the Unattributed / Needs Review callout),
    # so "Won Revenue" and the period ratio reconcile with each other and with the
    # canonical mart — the unattributed row is surfaced separately, never in the
    # ratio's numerator.
    google_rows = [r for r in campaign_rows if r["campaign_name"] != UNATTRIBUTED_LABEL]
    campaigns_with_spend = sum(1 for r in google_rows
                               if (r.get("spend_native") or 0) > 0)

    # SQLs: from the rows; Unavailable if any row withholds them (unsafe date grain).
    sqls_incomplete = any(r.get("sqls") is None for r in google_rows)
    total_sqls = None if sqls_incomplete else sum((r.get("sqls") or 0) for r in google_rows)

    # Won deals: Unavailable if any row withholds them — never a fabricated 0.
    # Deals add up across campaigns; customers (companies) do not, so the
    # page's customer figure is the canonical campaign-attributable metric.
    deals_incomplete = any(r.get("closed_won_deals") is None for r in google_rows)
    total_deals = (None if deals_incomplete
                   else sum((r.get("closed_won_deals") or 0) for r in google_rows))

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
        "closed_won_deals": total_deals,
        "customers": (summary or {}).get("attributed_customers"),
        "customers_metric": (((summary or {}).get("metrics") or {})
                             .get("attributed_customers")),
        # PR-ADS-161E: the canonical verdicts the KPI cards render when a
        # campaign-attributable total is not published.
        "revenue_metric": (((summary or {}).get("metrics") or {})
                           .get("attributed_revenue_usd")),
        "deals_metric": (((summary or {}).get("metrics") or {})
                         .get("attributed_closed_won_deals")),
        "won_revenue_usd": total_won,
        "period_revenue_per_ad_spend": _period_ratio(total_won, usd),
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
    and carrying NO outcome attribution — SQLs / customers / revenue / spend ratios are
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
            "period_revenue_per_ad_spend": None,
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


def _build_decision_cards(campaign_rows: list, kpis: dict, keyword_themes: dict,
                          search_signals: dict, spend_truth: dict) -> list:
    cards = []
    google_rows = [r for r in campaign_rows if r["campaign_name"] != UNATTRIBUTED_LABEL]
    # PR-ADS-161E: rows whose deal count is withheld support no claim either way.
    withheld = [r for r in google_rows if r.get("closed_won_deals") is None]
    withheld_reason = next((((r.get("metrics") or {}).get("closed_won_deals") or {})
                            .get("reason") for r in withheld
                            if ((r.get("metrics") or {}).get("closed_won_deals") or {})
                            .get("reason")), None)

    # Scale — the top revenue-proven campaign.
    proven = [r for r in google_rows if r["status"] == STATUS_REVENUE_PROVEN]
    if withheld and not proven:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "Campaign closed-won outcomes are withheld",
            "body": (f"{len(withheld)} campaign{'s' if len(withheld) != 1 else ''}' "
                     "closed-won deals are withheld in this window"
                     + (f" ({withheld_reason})" if withheld_reason else "")
                     + ", so no campaign is judged on revenue here."),
            "reason": withheld_reason,
            "target_page": "roas-campaigns", "target_label": "Open Campaign Revenue & Spend",
        })
    elif proven:
        top = max(proven, key=lambda r: (r.get("won_revenue_usd") or 0.0,
                                         r.get("closed_won_deals") or 0))
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": f"{top['campaign_name']} has revenue proof",
            "body": (f"This campaign won {top['closed_won_deals']} deal"
                     f"{'s' if top['closed_won_deals'] != 1 else ''} and "
                     f"{_fmt_usd_short(top['won_revenue_usd'])} closed-won revenue with "
                     "verified Google Ads spend."),
            "target_page": "roas-campaigns", "target_label": "Open Campaign Revenue & Spend",
        })
    else:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "No campaign has revenue proof yet",
            "body": "No Google Ads campaign has both verified spend and closed-won "
                    "deals in this window.",
            "target_page": "roas-campaigns", "target_label": "Open Campaign Revenue & Spend",
        })

    # Watch — spend without customer proof.
    no_proof = [r for r in google_rows if r["status"] in
                (STATUS_SPEND_NO_PROOF, STATUS_SQL_PRODUCER)
                and (r.get("spend_native") or 0) > 0 and r.get("closed_won_deals") == 0]
    if no_proof:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "Campaign spend without won-deal proof",
            "body": (f"{len(no_proof)} campaign{'s' if len(no_proof) != 1 else ''} spent "
                     "verified Google Ads budget but won no deals in this "
                     "window. Review SQL quality before scaling."),
            "target_page": "roas-campaigns", "target_label": "Open Campaign Revenue & Spend",
        })
    elif withheld:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "Unproven spend cannot be judged",
            "body": ("Closed-won deals are withheld for "
                     f"{len(withheld)} campaign{'s' if len(withheld) != 1 else ''}"
                     + (f" ({withheld_reason})" if withheld_reason else "")
                     + ", so spend without proof is not claimed."),
            "reason": withheld_reason,
            "target_page": "roas-campaigns", "target_label": "Open Campaign Revenue & Spend",
        })
    else:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "No unproven spend",
            "body": "Every spending campaign won at least one deal, or has "
                    "no verified spend to judge.",
            "target_page": "roas-campaigns", "target_label": "Open Campaign Revenue & Spend",
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
    elif withheld:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Campaign attribution is withheld",
            "body": ("Per-campaign closed-won outcomes are withheld this window"
                     + (f" ({withheld_reason})" if withheld_reason else "")
                     + ", so whether all revenue maps to a campaign is not claimed."),
            "reason": withheld_reason,
            "target_page": "roas-campaigns", "target_label": "Open Campaign Revenue & Spend",
        })
    else:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "No waste or attribution flags",
            "body": "No flagged waste terms and all closed-won revenue maps to a "
                    "campaign this window.",
            "target_page": "search-terms", "target_label": "Open Search Terms",
        })

    # Unavailable — explain WHY the ratio / USD spend is withheld, ACCURATELY (it is
    # None either because FX/USD is incomplete, revenue is incomplete, or there is
    # simply no closed-won revenue yet — these read very differently).
    fx_ok = spend_truth.get("fx_status") == "verified"
    usd_present = kpis.get("verified_spend_usd") is not None
    revenue_known = kpis.get("won_revenue_usd") is not None
    if not fx_ok or not usd_present:
        headline = "USD spend & period revenue / ad spend unavailable"
        body = ("USD spend and period revenue / ad spend are withheld because FX "
                "coverage is incomplete — native GBP spend is still shown, never a "
                "fabricated USD figure or ratio.")
    elif not revenue_known:
        headline = "Period revenue / ad spend unavailable"
        body = (f"Period revenue / ad spend is withheld because {_revenue_unknown_text(kpis, campaign_rows)}"
                " — never a lowered $0.")
    elif kpis.get("period_revenue_per_ad_spend") is None:
        headline = "Period revenue / ad spend not yet meaningful"
        body = ("There is no closed-won revenue yet to measure against this window's "
                "verified spend, so no ratio is shown — never a fabricated 0.00x.")
    else:
        headline = "Keyword outcome attribution unavailable"
        body = ("Spend, revenue and period revenue / ad spend are verified for this window. Keyword-level "
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
        "revenue": "ready" if readiness.get("canonical_revenue_available") else "blocked",
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
    if kpis.get("period_revenue_per_ad_spend") is None:
        # State the ACTUAL reason the period ratio is None (aligned with the decision card):
        # FX/USD incomplete, revenue incomplete, or simply no closed-won revenue.
        fx_ok = spend_truth.get("fx_status") == "verified"
        usd_present = kpis.get("verified_spend_usd") is not None
        if not fx_ok or not usd_present:
            ratio_reason = ("Period revenue / ad spend requires verified FX / USD "
                            "spend; native GBP spend is shown instead.")
        elif kpis.get("won_revenue_usd") is None:
            ratio_reason = ("Period revenue / ad spend requires published revenue — "
                            f"{_revenue_unknown_text(kpis)}.")
        else:
            ratio_reason = ("Period revenue / ad spend not yet meaningful; no "
                            "closed-won revenue in this window.")
        out.append({"metric": "period_revenue_per_ad_spend", "reason": ratio_reason})
    # Attribution ROAS is never published: not one acquisition cohort.
    out.append({"metric": "roas",
                "reason": ("Unavailable: attribution ROAS requires revenue and "
                           "spend over one acquisition cohort.")})
    if not deal_proof_available:
        out.append({"metric": "deal_proof",
                    "reason": "Closed-won deal proof is unavailable — the deal ledger could "
                              "not be read. Campaign revenue may still be canonical from the mart."})
    if kpis.get("sqls") is None:
        out.append({"metric": "sqls",
                    "reason": "SQLs withheld — lead business event dates are unavailable."})
    # Keyword themes carry no outcome attribution — structurally unavailable.
    out.append({"metric": "keyword_outcome_attribution",
                "reason": "Keyword-level SQLs / customers / revenue / spend ratios have no durable attribution."})
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
    connected = bool((prev_mart.get("readiness") or {}).get("canonical_revenue_available"))
    # The mart summary counts a null-amount deal as $0; if the previous window had
    # ANY unknown deal amount, its revenue total is not a trustworthy baseline —
    # withhold it (No comparison) rather than measuring against a lowered $0.
    prev_amount_unknown = (not prev_deals.get("available")) or any(
        d.get("deal_amount_usd") is None for d in (prev_deals.get("rows") or []))
    prev_rev = ps.get("won_revenue_usd") if (connected and not prev_amount_unknown) else None
    # PR-ADS-161E: like for like. This page's KPIs are the campaign-attributable
    # subset, so the previous period is that subset too — it used to compare
    # against the previous ALL-SOURCE totals.
    prev_rev = ps.get("attributed_won_revenue_usd") if prev_rev is not None else None
    prev_cust = ps.get("attributed_customers") if connected else None
    prev_deals = ps.get("attributed_closed_won_deals") if connected else None
    metrics = {
        "won_revenue_usd": _delta_metric(kpis.get("won_revenue_usd"), prev_rev),
        "closed_won_deals": _delta_metric(kpis.get("closed_won_deals"), prev_deals),
        "customers": _delta_metric(kpis.get("customers"), prev_cust),
        "sqls": _delta_metric(kpis.get("sqls"), ps.get("sqls")),
        "verified_spend_usd": _delta_metric(kpis.get("verified_spend_usd"), pst.get("usd_spend")),
        "period_revenue_per_ad_spend": _delta_metric(kpis.get("period_revenue_per_ad_spend"), ps.get("period_revenue_per_ad_spend")),
    }
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label, "note": note, "metrics": metrics, "reason": None,
    }


@_canonical_snapshot.single_snapshot
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
    revenue_connected = bool(readiness.get("canonical_revenue_available"))
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

    campaign_rows = _build_campaign_rows(
        mart_rows, spend_maps, deals_by_key, unknown_amount_keys, native_currency,
        revenue_total_published=(
            ((canonical.get("publication") or {}).get("revenue_usd") or {})
            .get("status") == "published"))

    if not revenue_connected:
        # Closed-won customers AND revenue come from the revenue integration, so
        # both are unknown when it is disconnected — blank them (spend / SQLs
        # survive). The mart returns a mart-level $0/0 here, which would otherwise
        # render as a VERIFIED zero. Never fabricate a $0 or a 0-customer proof.
        for c in campaign_rows:
            c["closed_won_deals"] = None
            c["customers"] = None
            c["won_revenue_usd"] = None
            c["period_revenue_per_ad_spend"] = None
            c["period_ratio_available"] = False

    kpis = _build_kpis(campaign_rows, spend_truth,
                       summary=(mart.get("summary") or {}) if revenue_connected else {})
    # PR-ADS-161E: a page total is a subset of the window — when the window
    # withholds a family, the sum of whatever rows exist (often none) is not a
    # total. Unknown, never 0.
    _pub = canonical.get("publication") or {}
    if (_pub.get("closed_won_deals") or {}).get("status") != "published":
        kpis["closed_won_deals"] = None
    if (_pub.get("revenue_usd") or {}).get("status") != "published":
        kpis["won_revenue_usd"] = None
        kpis["period_revenue_per_ad_spend"] = None
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
    # PR-ADS-155-F1: the campaign-attributable scope's canonical §5 disclosure,
    # carried through the mart from the scope ladder.
    _attributed_disclosure = ((mart.get("summary") or {})
                              .get("attributed_revenue_disclosure") or {})
    _mt = canonical_contract.metric_truth_block(_resolved_window, [
        {"metric": "google_ads_spend_usd",
         "data_source": canonical_contract.SOURCE_CANONICAL_SPEND,
         "scope": "google_ads_campaign_spend",
         "truth_status": (canonical_contract.TRUTH_READY
                          if spend_truth.get("campaign_spend_status") == "verified"
                          else canonical_contract.TRUTH_NOT_READY),
         "customer_id": spend_truth.get("customer_id")},
        # PR-ADS-155-F1. The revenue metric was `not_ready` with no reason and no
        # violation codes, so the parity audit could see it withheld but not why,
        # and fell back to the generic `canonical_source_unavailable` for a
        # blocker the system already knew exactly. The reason comes from the
        # mart's campaign-attributable disclosure — the scope ladder's own
        # verdict — so this page republishes ONE refusal rather than inventing a
        # second vocabulary for it.
        {"metric": "campaign_attributed_won_revenue_usd",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "campaign_attributable_revenue",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("won_revenue_usd") is not None
                          else canonical_contract.TRUTH_NOT_READY),
         "unavailable_reason": (None if kpis.get("won_revenue_usd") is not None
                                else _attributed_disclosure.get("unavailable_reason")),
         "violation_codes": ([] if kpis.get("won_revenue_usd") is not None
                             else _attributed_disclosure.get("violation_codes") or [])},
        # The COUNT is complete whatever the amounts are, so it keeps its own status.
        {"metric": "campaign_attributed_closed_won_deals",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "campaign_attributable_revenue",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("closed_won_deals") is not None
                          else canonical_contract.TRUTH_NOT_READY)},
        {"metric": "campaign_attributed_customers",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "campaign_attributable_revenue",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("customers") is not None
                          else canonical_contract.TRUTH_NOT_READY)},
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
