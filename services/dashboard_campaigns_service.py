"""
Dashboard Campaigns & Keywords (PR-ADS-137)

The fourth Dashboard tab (Dashboard → Campaigns). It answers, in one screen:
which Google Ads campaigns and keyword/search-intent themes are producing real
business outcomes (SQLs / customers / closed-won revenue), and which are
spending without proof?

Composition doctrine — this service computes NO new business math and adds NO
new taxonomy. It composes canonical read-only contracts:

  - build_revenue_decision_mart(view="campaign", settled=True) -> PR-ADS-177B:
      the SETTLED campaign view the Campaigns page reads (an open window ends
      yesterday). Rows (spend / leads / SQLs / deals / customers / won revenue
      / period ratio / mapping state / deal_ids), ``campaign_totals`` (the
      KPIs, the mart's own verdicts — never summed here), the cohort's
      ``campaign_residual_row``, spend truth and readiness.
  - fetch_canonical_campaign_spend                -> per-campaign native (GBP)
      AND USD spend over the same settled days (the only per-campaign
      native/USD split), shown only when the settle rule holds the spend.
  - load_won_deals(bounds=settled_revenue_bounds) -> drawer PROOF: each row's
      own deals (its ``deal_ids``), campaign-attributable scope.
  - fetch_search_term_signals                     -> window-scoped search-term
      waste-analysis signals (read-only evidence).

PR-ADS-177B deleted the page-side period ratio (`_period_ratio`), the
label-grouped all-source drawer (`_group_deals_by_campaign`), the
"Unattributed / Needs Review" all-source row and the keyword-theme panel (it
read the legacy, non-windowed `keywords` snapshot).

Truth rules (unchanged, never loosened):
  - Google Ads API = spend truth (native GBP). HubSpot closed-won = revenue truth
    (USD). Native GBP is never labelled USD. Reporting revenue is USD.
  - The spend ratio is ``period_revenue_per_ad_spend`` (PR-ADS-161E): revenue
    CLOSED in the window ÷ the same window's spend. A non-cohort operational
    ratio, never attribution ROAS; only when spend + FX + revenue are safe, and
    never derived from the Google Ads conversion value.
  - No fake $0 / 0.00x / 0%. A missing amount stays null; a total that needs
    completeness becomes Unavailable when any part is unknown; a missing prior
    period yields "No comparison". PR-ADS-177C: a PUBLISHED zero revenue over
    verified positive spend is a measured 0.00x (the mart row's reader-contract
    ratio, with its disclosure); unknown or withheld revenue, and zero or
    unknown spend, are never 0.
  - The cohort's contacts on no campaign row are the residual row (leads and
    SQLs only); campaign deals are campaign-attributable, so there is no deal
    residual on this page.
  - Search terms are READ-ONLY evidence: no negative-keyword writes, no
    bid/budget actions. Search-term panels present the existing waste-analysis
    classification only.
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
from services.google_ads_spend_service import configured_customer_id

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
# PR-ADS-177A: the campaign's SQL count is withheld (the acquisition-cohort
# verdict is not published) and nothing else decides its status — no won deal,
# no revenue. "SQL producer" or "Spend without SQL proof" would each be a claim
# about a count nobody published.
STATUS_SQLS_WITHHELD = "SQLs withheld"

# PR-ADS-177B: the window's spend is not held completely yet (the settle rule:
# yesterday is published after the morning re-read), so a campaign with no
# won deal is not judged "spend without proof" on spend that is still moving.
STATUS_SPEND_UNSETTLED = "Spend not settled"


def _norm(label) -> str:
    """Normalise a campaign label for matching (lowercase, collapsed spaces)."""
    if not label:
        return ""
    return " ".join(str(label).replace("_", " ").strip().lower().split())


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
            # PR-ADS-177C: two campaign ids that share a name are SUMMED under
            # it, as the mart's name-keyed row sums them — the last one read
            # used to overwrite the other, so the row printed one campaign's
            # spend beside the name's revenue. A missing USD figure on either
            # makes the name's USD unknown, never the other one's alone.
            k = _norm(r["campaign_name"])
            prev = by_name.get(k)
            if prev is None:
                by_name[k] = dict(entry)
            else:
                prev["native"] = (None if prev["native"] is None or entry["native"] is None
                                  else prev["native"] + entry["native"])
                prev["usd"] = (None if prev["usd"] is None or entry["usd"] is None
                               else prev["usd"] + entry["usd"])
                prev["fx_complete"] = prev["fx_complete"] and entry["fx_complete"]
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
    if (deals or 0) > 0:
        # Won deals, but no proven closed-won revenue yet.
        return STATUS_SQL_PRODUCER
    if sqls is None:
        # PR-ADS-177A: unknown is not zero — never "spend without SQL proof"
        # (or "SQL producer") over a withheld SQL count.
        return STATUS_SQLS_WITHHELD
    if sqls > 0:
        # Demand (SQLs), but no proven closed-won revenue yet.
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


def _build_campaign_rows(mart_rows: list, spend_maps: dict, deals_by_id: dict,
                         native_currency: str = "GBP", *,
                         spend_ready: bool = True, spend_reason: str | None = None,
                         deal_proof_available: bool = True) -> list:
    """The mart's settled campaign rows, re-presented (PR-ADS-177B).

    Every figure is the mart row's: leads / SQLs (the cohort), closed-won
    deals, customers, revenue and the period ratio the reader contract derived
    (``derive_period_revenue_per_ad_spend``) — none is recomputed here. The
    per-campaign native / USD split is the canonical spend table's over the
    SAME settled days, shown only when the settle rule holds the window's
    spend (``spend_ready``); otherwise it is None with the gap's reason.

    The drawer's proof deals are the row's own population: the deals whose
    ids the mart row carries (``deal_ids``), read from the canonical ledger
    over the same settled days. A withheld deal count has no proof list."""
    rows = []
    for m in mart_rows or []:
        cid = str(m.get("campaign_id")) if m.get("campaign_id") is not None else None
        spend = (spend_maps["by_name"].get(_norm(m.get("campaign_name")))
                 or spend_maps["by_id"].get(cid) or {}) if spend_ready else {}
        native_spend = spend.get("native") if spend_ready else None
        usd_spend = spend.get("usd") if spend_ready else None
        fx_complete = bool(spend.get("fx_complete"))
        deals = m.get("closed_won_deals")
        won = _round2(m.get("won_revenue"))
        amount_unknown = (deals or 0) > 0 and won is None
        ratio = m.get("period_revenue_per_ad_spend")
        status = _campaign_status(
            native_spend=native_spend, usd_spend=usd_spend, fx_complete=fx_complete,
            sqls=m.get("sqls"), deals=deals, won_revenue=won,
            spend_state=m.get("spend_state"), amount_unknown=amount_unknown)
        if not spend_ready and deals is not None and not (deals or 0):
            # Spend that is still moving (or unread) supports no "spend without
            # proof" claim either way.
            status = STATUS_SPEND_UNSETTLED if status in (
                STATUS_SPEND_NO_PROOF, STATUS_FX_UNAVAILABLE) else status
        if m.get("identity_status") == "campaign_name_shared_by_google_ads_campaigns":
            # PR-ADS-177C: several Google Ads campaigns under one name are not
            # judged as one campaign (the mart's mapping_required decision).
            status = STATUS_MAPPING_REVIEW
        ids = m.get("deal_ids")
        proof = None
        if deals is not None and ids is not None:
            proof = ([deals_by_id[i] for i in ids if i in deals_by_id]
                     if deal_proof_available else [])
        rows.append({
            "campaign_id": m.get("campaign_id"),
            "campaign_name": m.get("campaign_name"),
            "spend_native": native_spend,
            "native_currency": native_currency,
            "spend_usd": usd_spend,
            "spend_reason": None if spend_ready else spend_reason,
            "fx_complete": fx_complete,
            # PR-ADS-177A: the acquisition cohort — Google Ads contacts created
            # in the window placed on this campaign (leads), and those proven
            # to have reached SQL. None with the cohort's reason when withheld.
            "leads": m.get("leads"),
            "sqls": m.get("sqls"),
            "sqls_status": m.get("sqls_status"),
            "sqls_reason": m.get("sqls_reason"),
            # PR-ADS-161E: closed-won DEALS, and this campaign's customers —
            # distinct proven HubSpot companies (not additive across rows).
            "closed_won_deals": deals,
            "customers": m.get("customers"),
            "metrics": m.get("metrics"),
            "won_revenue_usd": won,
            "period_revenue_per_ad_spend": ratio,
            "period_ratio_available": ratio is not None,
            "status": status,
            "spend_state": m.get("spend_state"),
            # PR-ADS-177C: the Google Ads campaign ids behind the name.
            "google_ads_campaign_ids": m.get("google_ads_campaign_ids"),
            "identity_status": m.get("identity_status"),
            "identity_reason": m.get("identity_reason"),
            "attribution_status": _MAPPING_STATUS.get(m.get("spend_mapping"), "mapping_unavailable"),
            # PR-ADS-177B: the row's own deal population (the mart's ids).
            "deal_ids": ids,
            "deals": proof,
            "target_page": "campaigns",
        })
    # Revenue, then won deals, then spend — the mart's rows only. The cohort's
    # contacts on no campaign row are the separate residual (never a row here).
    rows.sort(key=lambda r: (
        r.get("won_revenue_usd") or 0.0,
        r.get("closed_won_deals") or 0,
        r.get("spend_native") or 0.0,
    ), reverse=True)
    return rows


def _build_kpis(campaign_rows: list, spend_truth: dict, totals: dict | None,
                cohort: dict | None, *, spend_ready: bool = True) -> dict:
    """Hero KPIs — the mart's ``campaign_totals`` (PR-ADS-177B), never a sum
    taken here. The period ratio is the mart's campaign-attributable revenue
    over campaign-attributable spend (``derive_period_revenue_per_ad_spend``),
    not campaign revenue over a different spend."""
    t = totals or {}
    tm = t.get("metrics") or {}
    cohort = cohort or {}
    residual = cohort.get("campaign_residual") or {}
    campaigns_with_spend = sum(1 for r in campaign_rows if (r.get("spend_native") or 0) > 0)
    native = spend_truth.get("native_spend") if spend_ready else None
    return {
        "campaigns_with_spend": campaigns_with_spend,
        "verified_spend_native": {"amount": native,
                                  "currency": spend_truth.get("native_currency") or "GBP"},
        "verified_spend_usd": t.get("spend"),
        "spend_metric": tm.get("spend"),
        "leads": t.get("leads"),
        "sqls": t.get("sqls"),
        "sqls_scope": cohort.get("sqls_scope"),
        "sqls_status": cohort.get("sql_status"),
        "sqls_reason": cohort.get("sql_reason"),
        # The part of `sqls` no campaign row holds (missing / pseudo / email /
        # conflicting campaign label): Σ row sqls + sqls_residual = sqls.
        "sqls_residual": residual.get("sqls"),
        "leads_residual": residual.get("leads"),
        "closed_won_deals": t.get("closed_won_deals"),
        "customers": t.get("customers"),
        "customers_metric": tm.get("customers"),
        "revenue_metric": tm.get("revenue_usd"),
        "deals_metric": tm.get("closed_won_deals"),
        "won_revenue_usd": t.get("won_revenue"),
        "period_revenue_per_ad_spend": t.get("period_revenue_per_ad_spend"),
        # The mart's reader-contract ratio, filed under its own name (audit
        # check 18: a published period ratio lives only under a period key).
        "metrics": {"period_revenue_per_ad_spend": tm.get("period_revenue_per_ad_spend")},
    }


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


#: PR-ADS-177C (M1): why the window's campaign spend is not shown, in words.
#: Keyed by the reason the settled view gives (``spend_reason``:
#: ``campaign_spend_unsettled_reason`` / the coverage reason / the
#: no-settled-day reason). A code not listed here is quoted, never guessed.
_SPEND_REASON_TEXT = {
    "spend_not_reread_since_day_ended":
        "yesterday's spend has not been re-read since the day ended — the "
        "morning Google Ads sync settles it",
    canonical_contract.NO_SETTLED_DAY_IN_WINDOW:
        "no day of this window has ended yet",
    "google_ads_spend_coverage_incomplete":
        "Google Ads spend coverage for this window is incomplete",
    "fx_coverage_incomplete": "FX coverage for this window is incomplete",
}


def _spend_reason_text(reason) -> str:
    if not reason:
        return "campaign spend for this window is not verified"
    return _SPEND_REASON_TEXT.get(
        reason, f"campaign spend for this window is not verified ({reason})")


def _build_decision_cards(campaign_rows: list, kpis: dict,
                          search_signals: dict, spend_truth: dict, *,
                          spend_ready: bool = True,
                          spend_reason: str | None = None) -> list:
    cards = []
    google_rows = list(campaign_rows)
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
            "target_page": "campaigns", "target_label": "Open Campaigns",
        })
    elif proven:
        top = max(proven, key=lambda r: (r.get("won_revenue_usd") or 0.0,
                                         r.get("closed_won_deals") or 0))
        won = (f"This campaign won {top['closed_won_deals']} deal"
               f"{'s' if top['closed_won_deals'] != 1 else ''} and "
               f"{_fmt_usd_short(top['won_revenue_usd'])} closed-won revenue")
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": f"{top['campaign_name']} has revenue proof",
            # PR-ADS-177C (M1): "with verified Google Ads spend" only when the
            # window's spend IS verified; before the morning sync it is not.
            "body": (won + " with verified Google Ads spend." if spend_ready
                     else won + f". Its Google Ads spend is not shown yet: "
                     f"{_spend_reason_text(spend_reason)}."),
            "reason": None if spend_ready else spend_reason,
            "target_page": "campaigns", "target_label": "Open Campaigns",
        })
    else:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "No campaign has revenue proof yet",
            "body": "No Google Ads campaign has both verified spend and closed-won "
                    "deals in this window.",
            "target_page": "campaigns", "target_label": "Open Campaigns",
        })

    # Watch — spend without customer proof.
    # PR-ADS-177A: a row whose SQLs are withheld still spent and still won no
    # deal — the claim here is about deals, which are known.
    no_proof = [r for r in google_rows if r["status"] in
                (STATUS_SPEND_NO_PROOF, STATUS_SQL_PRODUCER, STATUS_SQLS_WITHHELD)
                and (r.get("spend_native") or 0) > 0 and r.get("closed_won_deals") == 0]
    if no_proof:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "Campaign spend without won-deal proof",
            "body": (f"{len(no_proof)} campaign{'s' if len(no_proof) != 1 else ''} spent "
                     "verified Google Ads budget but won no deals in this "
                     "window. Review SQL quality before scaling."),
            "target_page": "campaigns", "target_label": "Open Campaigns",
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
            "target_page": "campaigns", "target_label": "Open Campaigns",
        })
    else:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "No unproven spend",
            "body": "Every spending campaign won at least one deal, or has "
                    "no verified spend to judge.",
            "target_page": "campaigns", "target_label": "Open Campaigns",
        })

    # Investigate — waste search terms or unattributed revenue.
    waste_count = (search_signals.get("counts") or {}).get("waste", 0)
    if waste_count > 0:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Search terms flagged as possible waste",
            "body": (f"{waste_count} search term{'s' if waste_count != 1 else ''} spent "
                     "with a waste flag this window. Review in Keywords & Search Terms — "
                     "this is evidence only, no platform write."),
            "target_page": "search-terms", "target_label": "Review in Keywords & Search Terms",
        })
    elif withheld:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Campaign attribution is withheld",
            "body": ("Per-campaign closed-won outcomes are withheld this window"
                     + (f" ({withheld_reason})" if withheld_reason else "")
                     + ", so whether all revenue maps to a campaign is not claimed."),
            "reason": withheld_reason,
            "target_page": "campaigns", "target_label": "Open Campaigns",
        })
    else:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "No waste or attribution flags",
            "body": "No flagged waste terms, and no campaign's closed-won "
                    "outcome is withheld this window.",
            "target_page": "search-terms", "target_label": "Open Keywords & Search Terms",
        })

    # Unavailable — explain WHY the ratio / USD spend is withheld, ACCURATELY:
    # spend not settled, FX/USD incomplete, revenue incomplete, or the ratio's own
    # refusal (zero spend …). PR-ADS-177C (M5): zero revenue over verified
    # positive spend is NOT among them — that ratio is a measured 0.00x.
    fx_ok = spend_truth.get("fx_status") == "verified"
    usd_present = kpis.get("verified_spend_usd") is not None
    revenue_known = kpis.get("won_revenue_usd") is not None
    native = (kpis.get("verified_spend_native") or {})
    native_cur = native.get("currency") or "GBP"
    if not spend_ready:
        # PR-ADS-177C (M1): before the morning sync (or on any settle / coverage
        # gap) NO spend is shown — native or USD — so this card must not say
        # the gap is FX, nor that native spend is still shown.
        headline = "Spend not settled"
        body = (f"Spend, USD spend and period revenue / ad spend are withheld: "
                f"{_spend_reason_text(spend_reason)}. No campaign spend figure is "
                "shown until it is — never a partial spend with a warning.")
    elif not fx_ok or not usd_present:
        headline = "USD spend & period revenue / ad spend unavailable"
        body = (f"USD spend and period revenue / ad spend are withheld because FX "
                f"coverage is incomplete — native {native_cur} spend is still shown, "
                "never a fabricated USD figure or ratio."
                if native.get("amount") is not None else
                "USD spend and period revenue / ad spend are withheld because FX "
                "coverage is incomplete, and no native spend figure is published "
                "for this window.")
    elif not revenue_known:
        headline = "Period revenue / ad spend unavailable"
        body = (f"Period revenue / ad spend is withheld because {_revenue_unknown_text(kpis, campaign_rows)}"
                " — never a lowered $0.")
    elif kpis.get("period_revenue_per_ad_spend") is None:
        # PR-ADS-177C (M5): a PUBLISHED zero revenue over verified positive
        # spend is a measured 0.00x (the reader-contract ratio), so a None here
        # is the ratio's own refusal, quoted.
        why = ((((kpis.get("metrics") or {}).get("period_revenue_per_ad_spend")
                 or {}).get("reason")) or "the ratio is not published")
        headline = "Period revenue / ad spend unavailable"
        body = (f"Period revenue / ad spend is not published for this window ({why}) — "
                "never a fabricated 0.00x.")
    else:
        headline = "Search-term outcome attribution unavailable"
        body = ("Spend, revenue and period revenue / ad spend are verified for this window. "
                "Search terms have no durable SQL / customer / revenue attribution — the "
                "search-term panels are read-only evidence.")
    cards.append({
        "type": "unavailable", "title": "Unavailable",
        "headline": headline, "body": body,
        "target_page": "revenue-health", "target_label": "Open Revenue Health",
    })
    return cards


def _build_truth_status(spend_truth: dict, readiness: dict, campaign_rows: list,
                        search_signals: dict,
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
        r.get("attribution_status") != "mapped"
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
        "search_terms": search_signals.get("status", "unavailable"),
    }


def _build_unavailable(kpis: dict, spend_truth: dict,
                       search_signals: dict, period_change: dict,
                       deal_proof_available: bool = True, *,
                       spend_ready: bool = True,
                       spend_reason: str | None = None) -> list:
    out = []
    native_shown = ((kpis.get("verified_spend_native") or {}).get("amount")) is not None
    native_cur = (kpis.get("verified_spend_native") or {}).get("currency") or "GBP"
    if kpis.get("verified_spend_usd") is None:
        # PR-ADS-177C (M1): the reason is the settled view's, not always FX.
        out.append({"metric": "verified_spend_usd",
                    "reason": (f"Campaign spend is not settled: {_spend_reason_text(spend_reason)}; "
                               "no spend figure is shown." if not spend_ready else
                               f"USD spend requires verified FX; native {native_cur} spend "
                               "is shown instead." if native_shown else
                               "USD spend requires verified FX.")})
    if kpis.get("period_revenue_per_ad_spend") is None:
        # State the ACTUAL reason the period ratio is None (aligned with the
        # decision card): spend not settled, FX/USD incomplete, revenue
        # incomplete, or the ratio's own refusal.
        fx_ok = spend_truth.get("fx_status") == "verified"
        usd_present = kpis.get("verified_spend_usd") is not None
        if not spend_ready:
            ratio_reason = ("Period revenue / ad spend requires settled spend — "
                            f"{_spend_reason_text(spend_reason)}.")
        elif not fx_ok or not usd_present:
            ratio_reason = ("Period revenue / ad spend requires verified FX / USD spend"
                            + (f"; native {native_cur} spend is shown instead."
                               if native_shown else "."))
        elif kpis.get("won_revenue_usd") is None:
            ratio_reason = ("Period revenue / ad spend requires published revenue — "
                            f"{_revenue_unknown_text(kpis)}.")
        else:
            why = ((((kpis.get("metrics") or {}).get("period_revenue_per_ad_spend")
                     or {}).get("reason")) or "the ratio is not published")
            ratio_reason = f"Period revenue / ad spend is not published ({why})."
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
                    "reason": (f"Leads and SQLs withheld ({kpis.get('sqls_reason')}) — the "
                               "acquisition cohort (contacts created in this window) is "
                               "not proven complete as of the contact-funnel watermark.")})
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
        # PR-ADS-177B: the same settled campaign view as the current period.
        prev_mart = build_revenue_decision_mart(view="campaign", window=prev_key,
                                                 now=prev_now, settled=True)
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
    resolve_canonical_window(window, now=now)
    ref_now = _coerce_utc(now)

    # PR-ADS-177B: the SETTLED campaign view — the one the Campaigns page
    # reads (an open window ends yesterday; spend under the settle rule; deals
    # over settled_revenue_bounds; the cohort over the same days). Overview,
    # Revenue, Deals and Channels keep the campaign view through today.
    mart = build_revenue_decision_mart(view="campaign", window=window, now=now,
                                       settled=True)
    window_block = mart.get("window") or {}
    mart_rows = mart.get("rows") or []
    spend_truth = mart.get("spend_truth") or {}
    readiness = mart.get("readiness") or {}
    revenue_connected = bool(readiness.get("canonical_revenue_available"))
    native_currency = spend_truth.get("native_currency") or "GBP"
    empty_window = bool((window_block.get("window_range") or {}).get("empty"))

    start = _parse_iso_date(window_block.get("start_date"))
    end = _parse_iso_date(window_block.get("end_date"))

    # The per-campaign native / USD split over the SAME settled days, shown
    # only when the settle rule holds the window's spend completely.
    spend_ready = (spend_truth.get("spend_coverage_status") == "complete"
                   and not empty_window)
    spend_reason = (None if spend_ready else (
        canonical_contract.NO_SETTLED_DAY_IN_WINDOW if empty_window
        else spend_truth.get("campaign_spend_unsettled_reason")
        or spend_truth.get("campaign_spend_coverage_reason")
        or "google_ads_spend_coverage_incomplete"))
    # PR-ADS-177C (M3): the configured account's spend only — the mart's
    # campaign rows are that account's (an unscoped read summed every account
    # present into a row's native / USD split).
    spend_maps = (_canonical_spend_by_campaign(repo.fetch_canonical_campaign_spend(
        start, end, customer_id=configured_customer_id()))
                  if spend_ready else {"by_id": {}, "by_name": {}})

    # Drawer PROOF: the canonical ledger over the same settled days (the row's
    # instants, ``settled_revenue_bounds``), campaign-attributable scope. Each
    # row's proof is exactly its ``deal_ids`` — the deals the row counts.
    # PR-ADS-177B: the all-source "Unattributed / Needs Review" row is gone;
    # the cohort's contacts on no campaign row are the residual below.
    canonical = (canonical_revenue.load_won_deals(
        window, now=now, bounds=canonical_contract.settled_revenue_bounds(window_block))
        if not empty_window else {"available": False,
                                  "reason": canonical_contract.NO_SETTLED_DAY_IN_WINDOW})
    deal_proof_available = bool(canonical.get("available"))
    deals_by_id = {}
    for deal in (canonical_revenue.canonical_deal_rows(
            canonical, revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE)
            if deal_proof_available else []):
        if deal.get("deal_id"):
            deals_by_id[str(deal["deal_id"])] = _deal_proof_row(
                deal, _deal_attribution_status(deal, True))

    campaign_rows = _build_campaign_rows(
        mart_rows, spend_maps, deals_by_id, native_currency,
        spend_ready=spend_ready, spend_reason=spend_reason,
        deal_proof_available=deal_proof_available)

    kpis = _build_kpis(campaign_rows, spend_truth, mart.get("campaign_totals"),
                       mart.get("cohort"), spend_ready=spend_ready)
    period_change = _build_period_change(window, ref_now, kpis)
    search_signals = (_build_search_term_signals(repo.fetch_search_term_signals(start, end))
                      if not empty_window else
                      {"status": "unavailable", "value_terms": [], "waste_terms": [],
                       "needs_review": [],
                       "reason": "No completed day in this window yet."})
    decision_cards = _build_decision_cards(campaign_rows, kpis, search_signals, spend_truth,
                                           spend_ready=spend_ready,
                                           spend_reason=spend_reason)
    truth_status = _build_truth_status(spend_truth, readiness, campaign_rows,
                                       search_signals, deal_proof_available)
    unavailable = _build_unavailable(kpis, spend_truth, search_signals, period_change,
                                     deal_proof_available, spend_ready=spend_ready,
                                     spend_reason=spend_reason)

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
         "customer_id": spend_truth.get("customer_id"),
         # PR-ADS-177B: the settle rule's reason, as the settled mart states it.
         **({"unavailable_reason": spend_truth["campaign_spend_unsettled_reason"]}
            if spend_truth.get("campaign_spend_status") != "verified"
            and spend_truth.get("campaign_spend_unsettled_reason") else {})},
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
        # PR-ADS-177A: the acquisition cohort's Google Ads SQLs.
        {"metric": "google_ads_cohort_sqls",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "google_ads_cohort_sqls",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("sqls") is not None
                          else canonical_contract.TRUTH_NOT_READY),
         "unavailable_reason": (None if kpis.get("sqls") is not None
                                else kpis.get("sqls_reason"))},
    ])

    if empty_window:
        # PR-ADS-177B: nothing was measured over no settled day.
        _mt = canonical_contract.name_no_settled_day(_mt)
    return {
        "window": window_block,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        canonical_contract.METRIC_TRUTH_KEY: _mt,
        "source_truth": "revenue_decision_mart_campaign_view",
        # PR-ADS-153E-B: spend/campaign identity remain Google Ads; the closed-won
        # deals behind every campaign row are the canonical ledger.
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        # PR-ADS-177A (docs/58 §7.5 precedent): the campaign rows and the KPIs
        # count CAMPAIGN-ATTRIBUTABLE deals (the mart's campaign view). Only
        # the Unattributed / Needs Review row holds wider, all-source deals —
        # it is declared separately. This said `all_source`.
        "revenue_scope": revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE,
        # PR-ADS-177B: the settled campaign view (owner decision 2026-11-24, 2).
        "settled_days": True,
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
        # PR-ADS-177A: the acquisition cohort behind every leads / SQLs figure
        # here (the mart's block). The page gates its SQL cells on THIS.
        "cohort": mart.get("cohort"),
        "metric_family": (mart.get("cohort") or {}).get("metric_family"),
        "window_basis": (mart.get("cohort") or {}).get("window_basis"),
        "period_change": period_change,
        "campaigns": campaign_rows,
        # PR-ADS-177B: the cohort's contacts on no campaign row (leads and
        # SQLs only), pinned last; never summed into a total.
        "campaign_residual_row": mart.get("campaign_residual_row"),
        # PR-ADS-177B: the keyword-theme panel read the legacy, non-windowed
        # `keywords` snapshot; it is dropped (docs/60 §3).
        "search_term_signals": search_signals,
        "decision_cards": decision_cards,
        "truth_status": truth_status,
        "readiness": readiness,
        "unavailable": unavailable,
    }
