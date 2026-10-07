"""
analysis/closed_won_truth.py

PR-ADS-161D — what a closed-won deal, a customer and closed-won revenue ARE,
decided once. Pure: no database, no HubSpot, no Google Ads, no clock.

Three concepts that the product has repeatedly let stand in for one another:

* a **closed-won deal** is one distinct HubSpot deal (``deal_id``);
* a **customer** is one distinct, PROVEN customer identity — a HubSpot company.
  One customer may own several won deals; a won deal is never a customer;
* **closed-won revenue** is the sum, over distinct won deals, of amounts whose
  USD value is proven (``analysis.deal_currency``). Never a contact's value,
  never pipeline, never an OCT value.

The won predicate is NOT restated here. It is ``hs_is_closed_won IS TRUE``
(``analysis.deal_truth.is_won``, docs/35 §3), applied by the one ledger read.
The confirmed won STAGE is held here only to CROSS-CHECK that predicate: a deal
HubSpot flags won in a different stage, or a deal in the won stage HubSpot does
not flag won, is a disagreement between two definitions, and a count over a
window containing one is withheld rather than resolved in either direction.

Two questions, never blended
----------------------------
**Closed-won event cohort** — which deals CLOSED in the window? Membership is
the canonical deal close date, half-open ``[start, end)``.

**Marketing acquisition cohort** — which contacts were ACQUIRED in the window
and produced a won deal? Membership is the associated contacts' creation time.
Spend from an acquisition window divided by deals selected by close date is
neither CAC nor ROAS, and nothing here computes either.
"""

from __future__ import annotations

from datetime import datetime, timezone

from analysis.deal_currency import (
    CURRENCY_CONVERTED,
    CURRENCY_UNAVAILABLE,
    CURRENCY_VERIFIED_USD,
    REASON_HOME_CURRENCY_UNVERIFIED,
    REASON_NO_AMOUNT,
    REASON_NO_CLOSE_DATE,
    REASON_NO_FX_RATE,
    REASON_UNKNOWN_CURRENCY,
    is_summable,
)
from analysis.revenue_scope import has_campaign, is_google_ads_attributed

CLOSED_WON_TRUTH_RULE_VERSION = "v1"

#: The confirmed Logistaas won stage (HubSpot deal stage id). A CROSS-CHECK on
#: ``hs_is_closed_won``, never the predicate — docs/35 §3 forbids a hardcoded
#: stage id as the population filter, because it ties revenue truth to one
#: portal's configuration.
CONFIRMED_WON_STAGE_ID = "326093516"
CONFIRMED_WON_STAGE_LABEL = "Deal Won / Payment Received"

# ── Won-definition agreement · denominator: one deal ────────────────────────
WON_DEFINITIONS_AGREE = "won_flag_and_stage_agree"
WON_FLAG_OTHER_STAGE = "won_flag_true_in_another_stage"
WON_STAGE_FLAG_NOT_TRUE = "won_stage_but_won_flag_not_true"
WON_DEFINITION_STATES = (WON_DEFINITIONS_AGREE, WON_FLAG_OTHER_STAGE,
                         WON_STAGE_FLAG_NOT_TRUE)

# ── Close date · denominator: one won deal ──────────────────────────────────
CLOSE_EXACT = "exact_close_date"
CLOSE_MISSING = "missing_close_date"
#: A won deal whose close date is after the reference instant: exact, but not
#: an event that has happened. It belongs to no window ending today.
CLOSE_INVALID = "invalid_close_date"
CLOSE_DATE_STATES = (CLOSE_EXACT, CLOSE_MISSING, CLOSE_INVALID)

# ── Amount · denominator: one won deal ──────────────────────────────────────
AMOUNT_POSITIVE = "positive_amount"
AMOUNT_ZERO = "zero_amount"          # a real claim the deal was worth nothing
AMOUNT_MISSING = "missing_amount"    # we do not know — never zero
AMOUNT_INVALID = "invalid_amount"
AMOUNT_NEGATIVE = "negative_amount"
AMOUNT_STATES = (AMOUNT_POSITIVE, AMOUNT_ZERO, AMOUNT_MISSING, AMOUNT_INVALID,
                 AMOUNT_NEGATIVE)

# ── Currency · denominator: one won deal ────────────────────────────────────
CURRENCY_CANONICAL_USD = "canonical_usd"
CURRENCY_PROVEN_FX = "converted_to_usd_with_proven_fx"
CURRENCY_MISSING = "missing_currency"
CURRENCY_MISSING_FX = "missing_fx_rate"
CURRENCY_UNSUPPORTED = "unsupported_currency"
#: No amount, so there is no currency question to answer.
CURRENCY_NOT_APPLICABLE = "not_applicable_no_amount"
CURRENCY_STATES = (CURRENCY_CANONICAL_USD, CURRENCY_PROVEN_FX, CURRENCY_MISSING,
                   CURRENCY_MISSING_FX, CURRENCY_UNSUPPORTED,
                   CURRENCY_NOT_APPLICABLE)

# ── Customer identity · denominator: one won deal ───────────────────────────
CUSTOMER_RESOLVED_SINGLE = "resolved_single_customer"
CUSTOMER_RESOLVED_MULTI_PATH = "resolved_same_customer_multiple_paths"
CUSTOMER_NO_COMPANY = "unresolved_no_company"
CUSTOMER_AMBIGUOUS = "ambiguous_multiple_companies"
#: We could not look: no company-association evidence exists for the deal.
CUSTOMER_ASSOCIATION_UNAVAILABLE = "association_unavailable"
CUSTOMER_IDENTITY_STATES = (CUSTOMER_RESOLVED_SINGLE,
                            CUSTOMER_RESOLVED_MULTI_PATH, CUSTOMER_NO_COMPANY,
                            CUSTOMER_AMBIGUOUS,
                            CUSTOMER_ASSOCIATION_UNAVAILABLE)
CUSTOMER_RESOLVED_STATES = (CUSTOMER_RESOLVED_SINGLE,
                            CUSTOMER_RESOLVED_MULTI_PATH)

# ── Marketing attribution partition · denominator: one won deal ────────────
# Exhaustive and mutually exclusive, so every won deal lands in exactly one and
# the buckets always sum to the all-source total. Nothing is dropped.
BUCKET_CAMPAIGN = "campaign_attributable"
BUCKET_GOOGLE_ADS_UNPLACED = "google_ads_unplaced"
BUCKET_EXCLUDED = "excluded_by_approved_mapping"
BUCKET_OTHER_SOURCE = "attributed_other_source"
BUCKET_UNATTRIBUTED = "unattributed"
BUCKET_AMBIGUOUS = "ambiguous"
ATTRIBUTION_BUCKETS = (BUCKET_CAMPAIGN, BUCKET_GOOGLE_ADS_UNPLACED,
                       BUCKET_EXCLUDED, BUCKET_OTHER_SOURCE,
                       BUCKET_UNATTRIBUTED, BUCKET_AMBIGUOUS)

# ── Acquisition-cohort membership · denominator: (won deal, window) ─────────
ACQ_MEMBER = "member"
ACQ_NOT_MEMBER = "not_member"
#: Its contacts were created partly inside and partly outside the window.
ACQ_AMBIGUOUS = "ambiguous_contacts_span_window"
#: A contact's creation time is not held — it could be in any window.
ACQ_UNRESOLVED = "unresolved_contact_creation_unknown"
#: No associated contact: no contact was acquired, so it is in no acquisition
#: cohort. Disclosed, never placed.
ACQ_NO_CONTACT = "no_associated_contact"
ACQUISITION_STATES = (ACQ_MEMBER, ACQ_NOT_MEMBER, ACQ_AMBIGUOUS,
                      ACQ_UNRESOLVED, ACQ_NO_CONTACT)

# ── Publication statuses ────────────────────────────────────────────────────
PUBLISHED = "published"
WITHHELD = "withheld"
UNAVAILABLE = "unavailable"
NOT_PUBLISHED = "not_published"
PUBLICATION_STATUSES = (PUBLISHED, WITHHELD, UNAVAILABLE, NOT_PUBLISHED)

R_SOURCE_UNREADABLE = "canonical_ledger_unreadable"
R_COVERAGE_NOT_PROVEN = "canonical_coverage_not_proven"
R_WON_DEFINITION_CONFLICT = "won_flag_and_stage_disagree"
R_UNDATED_WON_DEALS = "won_deals_without_close_date"
R_REVENUE_UNPROVEN = "closed_won_deals_missing_proven_usd_amount"
R_CUSTOMER_IDENTITY = "customer_identity_unresolved"
R_CUSTOMER_IDENTITY_NOT_INGESTED = "company_associations_not_ingested"
R_CAMPAIGN_IDENTITY_UNAVAILABLE = "campaign_identity_unavailable"
R_DEALS_WITHHELD = "closed_won_deal_count_withheld"
R_REVENUE_WITHHELD = "closed_won_revenue_withheld"
R_ACQ_UNRESOLVED = "acquisition_membership_unresolved"
R_CAMPAIGN_UNPLACED = "google_ads_deals_not_placed_on_a_campaign"
R_ROAS_NOT_CERTIFIED = ("roas_requires_compatible_numerator_and_denominator_"
                        "cohorts_pr_ads_161e")
R_CAC_NOT_CERTIFIED = "cac_requires_acquisition_cohort_spend_pr_ads_161e"


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# Per-deal states
# ═════════════════════════════════════════════════════════════════════════════

def won_definition_state(row: dict) -> str:
    """Do HubSpot's won flag and the confirmed won stage agree for this deal?"""
    stage_won = str(row.get("deal_stage_id") or "").strip() == CONFIRMED_WON_STAGE_ID
    flag_won = row.get("hs_is_closed_won") is True
    if flag_won and stage_won:
        return WON_DEFINITIONS_AGREE
    if flag_won:
        return WON_FLAG_OTHER_STAGE
    return WON_STAGE_FLAG_NOT_TRUE


def close_date_state(row: dict, now: datetime) -> str:
    close = _as_utc(row.get("deal_close_date"))
    if close is None:
        return CLOSE_MISSING
    return CLOSE_INVALID if close > now else CLOSE_EXACT


def amount_state(row: dict) -> str:
    """The state of the amount that would be SUMMED.

    A deal whose currency the ledger proved carries that proof in
    ``revenue_usd`` (a home-currency deal may have no raw ``amount`` at all), so
    that value is classified; otherwise the raw HubSpot ``amount`` is.
    """
    proven = is_summable(row.get("currency_status")) and \
        row.get("revenue_usd") is not None
    raw = row.get("revenue_usd") if proven else row.get("amount_raw")
    if raw is None:
        return AMOUNT_MISSING
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return AMOUNT_INVALID
    if value != value:  # NaN
        return AMOUNT_INVALID
    if value < 0:
        return AMOUNT_NEGATIVE
    return AMOUNT_ZERO if value == 0 else AMOUNT_POSITIVE


def currency_state(row: dict) -> str:
    """The deal's USD lineage, from the ledger's own currency resolution.

    Fails closed: a status or reason this module does not recognise is
    ``unsupported_currency`` — never assumed to be USD.
    """
    status = row.get("currency_status")
    reason = row.get("currency_reason")
    if status == CURRENCY_VERIFIED_USD:
        return CURRENCY_CANONICAL_USD
    if status == CURRENCY_CONVERTED:
        return CURRENCY_PROVEN_FX
    if status == CURRENCY_UNAVAILABLE:
        if reason == REASON_NO_AMOUNT:
            return CURRENCY_NOT_APPLICABLE
        if reason in (REASON_UNKNOWN_CURRENCY, REASON_HOME_CURRENCY_UNVERIFIED):
            return CURRENCY_MISSING
        if reason in (REASON_NO_FX_RATE, REASON_NO_CLOSE_DATE):
            return CURRENCY_MISSING_FX
    return CURRENCY_UNSUPPORTED


def revenue_is_proven(row: dict) -> bool:
    """May this deal's ``revenue_usd`` be summed? The ledger's own currency
    rule, plus: a won deal's value is never negative — a negative amount is
    invalid evidence, not a refund to net off the total. Fails closed."""
    return is_summable(row.get("currency_status")) and \
        row.get("revenue_usd") is not None and \
        amount_state(row) in (AMOUNT_POSITIVE, AMOUNT_ZERO)


def customer_identity_state(company_ids, *, source_available: bool) -> str:
    """One won deal's customer identity, from its company associations.

    ``company_ids`` is every company id reached for the deal, by every path,
    duplicates included (two paths to one company is still one company).
    ``source_available`` False means no company-association evidence exists
    for the deal at all — we could not look, which is not "no company".

    Never invents an identity from a contact, an email domain, a company name or
    a campaign: only a HubSpot company id counts.
    """
    if not source_available:
        return CUSTOMER_ASSOCIATION_UNAVAILABLE
    ids = [str(c).strip() for c in (company_ids or []) if str(c or "").strip()]
    distinct = set(ids)
    if not distinct:
        return CUSTOMER_NO_COMPANY
    if len(distinct) > 1:
        return CUSTOMER_AMBIGUOUS
    return (CUSTOMER_RESOLVED_MULTI_PATH if len(ids) > 1
            else CUSTOMER_RESOLVED_SINGLE)


def attribution_bucket(row: dict, resolve_label=None) -> tuple[str, str | None,
                                                               str | None]:
    """``(bucket, campaign_id, reason)`` for one won deal. Exactly one bucket.

    The deal's attribution evidence is the ledger's own deal-level resolution
    (``analysis.deal_truth``): unambiguous only when ONE contact is associated or
    every associated contact carries identical evidence. A conflict is
    ``ambiguous`` and is placed nowhere narrower.

    Campaign placement goes through ``resolve_label`` — production binds Campaign
    Evidence's ``_assign_lead`` (approved durable mapping, else an exact
    normalized match to exactly ONE canonical spend campaign; never fuzzy). With
    no resolver the campaign question is not answered, and the deal stays a
    Google Ads deal that is not placed on a campaign.
    """
    if row.get("attribution_status") == "ambiguous" \
            or row.get("association_status") == "ambiguous":
        return BUCKET_AMBIGUOUS, None, "associated_contacts_disagree"
    association = row.get("association_status")
    if association == "none":
        return BUCKET_UNATTRIBUTED, None, "no_associated_contact"
    if association == "lookup_failed" or \
            row.get("attribution_status") == "unavailable":
        return BUCKET_UNATTRIBUTED, None, "association_lookup_failed"
    if is_google_ads_attributed(row):
        if resolve_label is None:
            return (BUCKET_GOOGLE_ADS_UNPLACED, None,
                    R_CAMPAIGN_IDENTITY_UNAVAILABLE)
        if not has_campaign(row):
            return BUCKET_GOOGLE_ADS_UNPLACED, None, "no_campaign_label"
        kind, key = resolve_label(row.get("campaign_name_raw"))
        if kind == "google_ads":
            return BUCKET_CAMPAIGN, str(key), None
        if kind == "not_google_ads":
            return BUCKET_EXCLUDED, None, "label_approved_as_not_google_ads"
        return BUCKET_GOOGLE_ADS_UNPLACED, None, "campaign_label_unmapped"
    group = str(row.get("acquisition_group") or "").strip().lower()
    if row.get("attribution_status") == "attributed" and group \
            and group != "unclassified":
        return BUCKET_OTHER_SOURCE, None, f"acquisition_group:{group}"
    return BUCKET_UNATTRIBUTED, None, "source_unclassified"


def is_paid_search_source(row: dict) -> bool:
    """HubSpot original source PAID_SEARCH, on unambiguous deal evidence."""
    if row.get("attribution_status") != "attributed":
        return False
    return str(row.get("source_primary_raw") or "").strip().upper() \
        == "PAID_SEARCH"


def in_window(instant, start, end) -> bool:
    """Half-open ``[start, end)``. ``start`` None is no lower bound."""
    value = _as_utc(instant)
    if value is None:
        return False
    if start is not None and value < start:
        return False
    return value < end


def acquisition_state(contacts, start, end, *,
                      association_status: str | None = None) -> str:
    """Acquisition-cohort membership of ONE won deal for ONE window.

    ``contacts`` are the deal's associated contacts, each ``{contact_id,
    contact_created_at, funnel_row_present}``. A deal is a member only when
    EVERY associated contact was created inside the window — a deal whose
    contacts span the boundary has no single acquisition window, and a contact
    whose creation time is not held could belong to any window.

    ``association_status`` is the ledger's own: only ``none`` (the lookup
    SUCCEEDED and found nobody) proves the deal has no contact. A failed lookup,
    or a deal the ledger says has contacts while none are stored, is unresolved
    — we do not know who it was acquired through.
    """
    rows = [c for c in (contacts or []) if c.get("contact_id")]
    if association_status == "lookup_failed":
        return ACQ_UNRESOLVED
    if not rows:
        return ACQ_NO_CONTACT if association_status == "none" \
            else ACQ_UNRESOLVED
    if any(_as_utc(c.get("contact_created_at")) is None for c in rows):
        return ACQ_UNRESOLVED
    inside = [in_window(c.get("contact_created_at"), start, end) for c in rows]
    if all(inside):
        return ACQ_MEMBER
    if any(inside):
        return ACQ_AMBIGUOUS
    return ACQ_NOT_MEMBER


# ═════════════════════════════════════════════════════════════════════════════
# One window
# ═════════════════════════════════════════════════════════════════════════════

def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def _revenue(rows) -> float:
    return round(sum(float(r["revenue_usd"]) for r in rows
                     if revenue_is_proven(r)), 2)


def _bucket_totals(rows) -> dict:
    """Deals and proven revenue for one population. Revenue is withheld (None)
    unless every deal in it has proven USD — a partial sum is not a total."""
    priced = [r for r in rows if revenue_is_proven(r)]
    complete = len(priced) == len(rows)
    return {"deals": len(rows),
            "revenue_usd": _revenue(rows) if complete else None,
            "deals_without_proven_usd": len(rows) - len(priced)}


def _redact(value):
    """Every number (and id list) in ``value`` replaced by None, recursively.

    Withheld means absent, not hidden: a payload whose headline is withheld
    must not let the number be re-assembled from its disclosures.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return None
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return None
    return value


def evaluate_window(*, won_rows, definition_rows, contacts_by_deal, start, end,
                    is_all_time: bool, now: datetime, coverage_findings,
                    resolve_label=None, company_ids_by_deal=None,
                    unknown_won_rows=None) -> dict:
    """The closed-won truth for one window. Pure.

    ``won_rows``           every ``hs_is_closed_won IS TRUE`` deal, all time.
    ``definition_rows``    won-flag/won-stage cross-check rows, all time.
    ``contacts_by_deal``   deal_id → associated contacts with creation times.
    ``coverage_findings``  the ledger coverage gate's findings (``[]`` proven).
    ``resolve_label``      the campaign resolver, or None when unavailable.
    ``company_ids_by_deal`` deal_id → company ids, or None when the repository
                           holds no company associations at all.
    ``unknown_won_rows``   deals whose ``hs_is_closed_won`` IS NULL — neither
                           won nor lost; disclosed beside the count (docs/35 §3).
    """
    findings = list(coverage_findings or [])
    company_source = company_ids_by_deal is not None

    # ── membership, by the canonical close date only ────────────────────────
    undated = [r for r in won_rows
               if close_date_state(r, now) == CLOSE_MISSING]
    future = [r for r in won_rows if close_date_state(r, now) == CLOSE_INVALID]
    # A close date after ``now`` has not happened, whatever the window's end:
    # a window ending at tomorrow's midnight, or a quarter's end, must not
    # admit a deal dated later today or next month. Membership therefore
    # requires an EXACT close date (dated, not after ``now``) inside the window.
    members = [r for r in won_rows
               if close_date_state(r, now) == CLOSE_EXACT
               and in_window(r.get("deal_close_date"), start, end)]
    if is_all_time:
        # All Time contains every won deal whose identity and won state are
        # proven — an undated one included. A future-dated one has not closed.
        members = members + undated
    ids = [str(r.get("deal_id")) for r in members]

    # Won-definition disagreements that could touch this window.
    conflicts = []
    for d in definition_rows or []:
        state = won_definition_state(d)
        if state == WON_DEFINITIONS_AGREE:
            continue
        close = _as_utc(d.get("deal_close_date"))
        # An undated conflict could belong to any window, so it blocks every
        # one. A dated conflict blocks only a window it could be a member of —
        # and a close after ``now`` is a member of none, All Time included.
        if close is None or (close <= now and (
                is_all_time or in_window(close, start, end))):
            conflicts.append({"deal_id": d.get("deal_id"), "state": state,
                              "deal_stage_id": d.get("deal_stage_id")})

    # ── attribution partition ───────────────────────────────────────────────
    placed = [(r,) + attribution_bucket(r, resolve_label) for r in members]
    partition = {b: [] for b in ATTRIBUTION_BUCKETS}
    reasons: dict = {b: {} for b in ATTRIBUTION_BUCKETS}
    by_campaign: dict = {}
    for row, bucket, campaign_id, reason in placed:
        partition[bucket].append(row)
        if reason:
            reasons[bucket][reason] = reasons[bucket].get(reason, 0) + 1
        if bucket == BUCKET_CAMPAIGN:
            by_campaign.setdefault(campaign_id, []).append(row)

    # ── customer identity ───────────────────────────────────────────────────
    identity = {}
    for r in members:
        cid = str(r.get("deal_id"))
        identity[cid] = customer_identity_state(
            (company_ids_by_deal or {}).get(cid), source_available=company_source)
    resolved_customers = set()
    for r in members:
        cid = str(r.get("deal_id"))
        if identity[cid] in CUSTOMER_RESOLVED_STATES:
            companies = {str(c).strip() for c in
                         (company_ids_by_deal or {}).get(cid) or []
                         if str(c or "").strip()}
            resolved_customers |= companies

    # ── acquisition cohort ──────────────────────────────────────────────────
    # The same won population as the event cohort, minus nothing but deals
    # whose close lies after ``now`` (they have not closed). An undated deal
    # stays: its acquisition membership is proven by contact creation, not by
    # a close date.
    acq_population = [r for r in won_rows
                      if close_date_state(r, now) != CLOSE_INVALID]
    acq = {}
    for r in acq_population:
        cid = str(r.get("deal_id"))
        acq[cid] = acquisition_state(
            contacts_by_deal.get(cid) or [], start, end,
            association_status=r.get("association_status"))
    acq_members = [r for r in acq_population
                   if acq[str(r.get("deal_id"))] == ACQ_MEMBER]
    acq_unresolved = [r for r in acq_population
                      if acq[str(r.get("deal_id"))] == ACQ_UNRESOLVED]
    acq_ambiguous = [r for r in acq_population
                     if acq[str(r.get("deal_id"))] == ACQ_AMBIGUOUS]

    # Acquisition membership does not depend on the close date, so every
    # conflict that has happened (dated up to now, or undated) can touch it.
    acq_conflicts = [d for d in definition_rows or []
                     if won_definition_state(d) != WON_DEFINITIONS_AGREE
                     and close_date_state(d, now) != CLOSE_INVALID]

    # Deals whose won state HubSpot has not told us: disclosed, never folded
    # into won or lost (docs/35 §3, as ``load_won_deals`` reports them).
    unknown_read = unknown_won_rows is not None   # None: not read — never 0
    unknown = list(unknown_won_rows or [])
    unknown_in_window = [d for d in unknown
                         if close_date_state(d, now) == CLOSE_EXACT
                         and in_window(d.get("deal_close_date"), start, end)]
    unknown_undated = [d for d in unknown
                       if close_date_state(d, now) == CLOSE_MISSING]
    if is_all_time:
        unknown_in_window = unknown_in_window + unknown_undated

    # ── publication ─────────────────────────────────────────────────────────
    if findings:
        deal_status, deal_reason = UNAVAILABLE, R_COVERAGE_NOT_PROVEN
    elif conflicts:
        deal_status, deal_reason = WITHHELD, R_WON_DEFINITION_CONFLICT
    elif undated and not is_all_time:
        deal_status, deal_reason = WITHHELD, R_UNDATED_WON_DEALS
    else:
        deal_status, deal_reason = PUBLISHED, None

    unpriced = [r for r in members if not revenue_is_proven(r)]
    if deal_status != PUBLISHED:
        rev_status = deal_status
        rev_reason = deal_reason if deal_status == UNAVAILABLE \
            else R_DEALS_WITHHELD
    elif unpriced:
        rev_status, rev_reason = WITHHELD, R_REVENUE_UNPROVEN
    else:
        rev_status, rev_reason = PUBLISHED, None

    unresolved_identity = [r for r in members
                           if identity[str(r.get("deal_id"))]
                           not in CUSTOMER_RESOLVED_STATES]
    if deal_status != PUBLISHED:
        cust_status = deal_status
        cust_reason = deal_reason if deal_status == UNAVAILABLE \
            else R_DEALS_WITHHELD
    elif not company_source:
        cust_status, cust_reason = WITHHELD, R_CUSTOMER_IDENTITY_NOT_INGESTED
    elif unresolved_identity:
        cust_status, cust_reason = WITHHELD, R_CUSTOMER_IDENTITY
    else:
        cust_status, cust_reason = PUBLISHED, None

    if rev_status != PUBLISHED:
        camp_status = rev_status
        camp_reason = rev_reason if rev_status == UNAVAILABLE \
            else R_REVENUE_WITHHELD
    elif resolve_label is None:
        camp_status, camp_reason = WITHHELD, R_CAMPAIGN_IDENTITY_UNAVAILABLE
    elif partition[BUCKET_GOOGLE_ADS_UNPLACED]:
        # A Google Ads deal on no campaign could belong to any of them: every
        # per-campaign figure would be a lower bound presented as a total.
        camp_status, camp_reason = WITHHELD, R_CAMPAIGN_UNPLACED
    else:
        camp_status, camp_reason = PUBLISHED, None

    if findings:
        acq_status, acq_reason = UNAVAILABLE, R_COVERAGE_NOT_PROVEN
    elif acq_conflicts:
        acq_status, acq_reason = WITHHELD, R_WON_DEFINITION_CONFLICT
    elif acq_unresolved or acq_ambiguous:
        acq_status, acq_reason = WITHHELD, R_ACQ_UNRESOLVED
    else:
        acq_status, acq_reason = PUBLISHED, None
    if acq_status != PUBLISHED:
        acq_rev_status = acq_status
        acq_rev_reason = acq_reason if acq_status == UNAVAILABLE \
            else R_DEALS_WITHHELD
    elif not all(revenue_is_proven(r) for r in acq_members):
        acq_rev_status, acq_rev_reason = WITHHELD, R_REVENUE_UNPROVEN
    else:
        acq_rev_status, acq_rev_reason = PUBLISHED, None

    publishes = deal_status == PUBLISHED
    revenue_published = rev_status == PUBLISHED
    customers_published = cust_status == PUBLISHED
    campaign_published = camp_status == PUBLISHED
    acq_published = acq_status == PUBLISHED

    def _totals(rows):
        totals = _bucket_totals(rows)
        if not revenue_published:
            totals["revenue_usd"] = None
        return totals

    def _bucket(name):
        totals = _totals(partition[name])
        totals["reasons"] = reasons[name]
        return totals

    google_ads_rows = [r for r in members if is_google_ads_attributed(r)
                       and r.get("attribution_status") != "ambiguous"]
    paid_rows = [r for r in members if is_paid_search_source(r)]

    # Everything below is derived from the window's MEMBERSHIP. When the count
    # is not published it is redacted as a whole, so the withheld number cannot
    # be re-assembled from ids, buckets or coverage distributions.
    membership_derived = {
        "membership_ids": sorted(ids),
        "outcomes": {
            "closed_won_deals": len(members),
            "closed_won_deals_confirmed_in_window": len(
                [r for r in members if close_date_state(r, now) == CLOSE_EXACT]),
            # A count of PROVEN identities. NULL when no identity source exists
            # at all — "we could not look" is not "zero customers".
            "confirmed_customer_lower_bound": (len(resolved_customers)
                                               if company_source else None),
            "unresolved_customer_identity": len(unresolved_identity),
            "deals_without_proven_usd": len(unpriced),
        },
        "attribution": {
            "all_source": _totals(members),
            "paid_search_source": _totals(paid_rows),
            "google_ads_source": _totals(google_ads_rows),
            "partition": {b: _bucket(b) for b in ATTRIBUTION_BUCKETS},
        },
        "coverage": {
            "deal_identity": {"deals": len(members),
                              "distinct_deal_ids": len(set(ids)),
                              "duplicate_deal_ids": len(ids) - len(set(ids))},
            "close_date_of_members": _count(close_date_state(r, now)
                                            for r in members),
            "customer_identity": _count(identity.values()),
            "amount": _count(amount_state(r) for r in members),
            "currency": _count(currency_state(r) for r in members),
            "contact_association": _count(
                (r.get("association_status") or "unknown") for r in members),
            "campaign_attribution": {b: len(partition[b])
                                     for b in ATTRIBUTION_BUCKETS},
        },
    }
    if not publishes:
        membership_derived = _redact(membership_derived)
    outcomes = membership_derived["outcomes"]
    outcomes["customers"] = (len(resolved_customers) if customers_published
                             else None)
    outcomes["revenue_usd"] = _revenue(members) if revenue_published else None
    attribution = membership_derived["attribution"]
    attribution["by_campaign"] = (
        {cid: _bucket_totals(rows) for cid, rows in sorted(by_campaign.items())}
        if campaign_published else None)

    acquisition = {
        "membership_date_field": "contact_created_at",
        "membership_rule": ("every associated contact created inside the "
                            "window"),
        "status": acq_status,
        "reason": acq_reason,
        "closed_won_deals": len(acq_members) if acq_published else None,
        "revenue_usd": (_revenue(acq_members)
                        if acq_rev_status == PUBLISHED else None),
        "revenue_status": acq_rev_status,
        "revenue_reason": acq_rev_reason,
        # Why it is withheld — counts of deals whose membership is unknown.
        # Never the confirmed members: those would be the withheld number's
        # lower bound.
        "unresolved_membership": len(acq_unresolved),
        "ambiguous_membership": len(acq_ambiguous),
        "won_definition_conflicts": len(acq_conflicts),
    }

    result = {
        "membership": {
            "deal_ids": membership_derived["membership_ids"],
            "undated_won_deals": len(undated),
            "undated_included": bool(is_all_time),
            "future_dated_won_deals": len(future),
            "won_definition_conflicts": conflicts,
            # docs/35 §3: neither won nor lost, so in no count — disclosed.
            "unknown_won_state_deals": (len(unknown_in_window)
                                        if unknown_read else None),
            "unknown_won_state_undated": (len(unknown_undated)
                                          if unknown_read else None),
        },
        "outcomes": outcomes,
        "attribution": attribution,
        "acquisition_cohort": acquisition,
        "coverage": {
            # Disclosure over the deals CONFIRMED in this window (plus, for All
            # Time, the undated ones) — redacted with the count when withheld.
            "basis": "confirmed_members_of_this_window",
            **membership_derived["coverage"],
            "close_date": {CLOSE_MISSING: len(undated),
                           CLOSE_INVALID: len(future),
                           "missing_included_in_this_window": bool(is_all_time)},
            "won_definition": {"conflicts_touching_window": len(conflicts)},
        },
        "publication": {
            "closed_won_deals": {"status": deal_status, "reason": deal_reason},
            "revenue_usd": {"status": rev_status, "reason": rev_reason},
            "customers": {"status": cust_status, "reason": cust_reason},
            "campaign_revenue": {"status": camp_status, "reason": camp_reason},
            "acquisition_cohort": {"status": acq_status, "reason": acq_reason},
            "acquisition_revenue": {"status": acq_rev_status,
                                    "reason": acq_rev_reason},
            "roas": {"status": NOT_PUBLISHED, "reason": R_ROAS_NOT_CERTIFIED},
            "cac": {"status": NOT_PUBLISHED, "reason": R_CAC_NOT_CERTIFIED},
        },
    }
    if findings:
        # Coverage unproven: the rows are an unknown fraction of history, so
        # no number they yield is evidence of anything — not even a disclosure.
        for key in ("membership", "outcomes", "attribution",
                    "acquisition_cohort"):
            result[key] = _redact(result[key])
        result["coverage"] = {"basis": result["coverage"]["basis"],
                              **_redact({k: v for k, v in
                                         result["coverage"].items()
                                         if k != "basis"})}
    return result
