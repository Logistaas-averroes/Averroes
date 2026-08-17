"""
Revenue by Acquisition Source service (PR-ADS-117)

Explains pipeline and closed-won revenue across Google Ads, Other Paid, Organic,
Offline, and Unclassified / Needs Review — from one durable, auditable source of
truth. Google Ads is the ONLY group with connected spend, so the ONLY group that
may show ROAS; every other group is revenue-only (ROAS unavailable, never $0 /
0.00x). Deal revenue is never split across sources, and each deal is counted once.

Window doctrine (PR-ADS-116): Leads / SQLs use contact_created_at; Won Revenue
uses deal_close_date; both use the selected business window.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from analysis.business_windows import resolve_window
from analysis.source_classification import (
    GROUP_GOOGLE_ADS, GROUP_OTHER_PAID, GROUP_ORGANIC, GROUP_OFFLINE,
    GROUP_UNCLASSIFIED, GROUP_LABELS, GROUPS_WITH_SPEND, RULE_VERSION,
    CHANNEL_LABELS, PLATFORM_LABELS, CH_UNSPECIFIED, PF_UNSPECIFIED,
    CH_PAID_SEARCH, PF_GOOGLE_ADS,
    classify_source, classify_source_taxonomy, attribute_deal,
)
from analysis import revenue_scope
from db import revenue_repository as repo
from services import canonical_revenue_service as canonical_revenue
from services import revenue_spend_truth_service

log = logging.getLogger(__name__)

# Display order of the page sections.
SECTION_ORDER = [GROUP_GOOGLE_ADS, GROUP_OTHER_PAID, GROUP_ORGANIC,
                 GROUP_OFFLINE, GROUP_UNCLASSIFIED]

# Per-platform status copy (PR-ADS-133). Only Google Ads is spend-connected /
# ROAS-eligible; every other source is revenue-only. Offline and Unclassified get
# their own honest labels — never a fabricated $0 / 0.00x ROAS.
STATUS_ROAS_AVAILABLE = "ROAS available"
STATUS_REVENUE_ONLY = "Revenue-only — no connected spend source"
STATUS_OFFLINE = "Imported CRM records — no reliable source attribution"
STATUS_NEEDS_REVIEW = "Needs review — source missing or unsafe"

# PR-ADS-140: honest Google Ads spend-truth states. Revenue by Source now reads
# the CANONICAL campaign-daily spend (same truth as the mart) instead of geo
# spend, so the Google Ads row can be in one of four states — each with its own
# label, never a fabricated $0 / 0.00x.
STATUS_FX_WITHHELD = "FX unavailable — USD ROAS withheld"
STATUS_COVERAGE_INCOMPLETE = "Canonical spend coverage incomplete"
STATUS_GOOGLE_SPEND_UNAVAILABLE = "Google Ads spend source unavailable"
STATUS_NO_SPEND_IN_WINDOW = "No Google Ads spend in this window"

# state -> (roas_status code, human status label) for the Google Ads group.
_GOOGLE_STATE_STATUS = {
    revenue_spend_truth_service.STATE_VERIFIED:
        ("available", STATUS_ROAS_AVAILABLE),
    # Verified $0 spend: ROAS has no denominator, so it is honestly unavailable —
    # never a "ROAS available" label next to an unavailable ROAS.
    revenue_spend_truth_service.STATE_VERIFIED_ZERO:
        ("unavailable_zero_spend", STATUS_NO_SPEND_IN_WINDOW),
    revenue_spend_truth_service.STATE_FX_INCOMPLETE:
        ("unavailable_fx", STATUS_FX_WITHHELD),
    revenue_spend_truth_service.STATE_COVERAGE_INCOMPLETE:
        ("unavailable_coverage", STATUS_COVERAGE_INCOMPLETE),
    revenue_spend_truth_service.STATE_SOURCE_UNAVAILABLE:
        ("unavailable_no_spend_source", STATUS_GOOGLE_SPEND_UNAVAILABLE),
}

# Google Ads states whose canonical USD spend is a real, verified figure (shown as
# the group spend, matching the mart top-line — including a verified $0).
_GOOGLE_SPEND_SHOWN_STATES = (
    revenue_spend_truth_service.STATE_VERIFIED,
    revenue_spend_truth_service.STATE_VERIFIED_ZERO,
)


def _platform_status(group: str, is_canonical: bool = False) -> str:
    """Status label for a channel/platform row.

    Only the CANONICAL Google Ads bucket (Paid Search → Google Ads) is
    spend-connected / ROAS-eligible. A non-canonical bucket inside the Google
    group (e.g. rows that fell into ``unspecified`` because their raw source was
    missing) is NOT the spend-connected platform, so it must never claim ROAS —
    it gets a needs-review status instead.
    """
    if group in GROUPS_WITH_SPEND:
        return STATUS_ROAS_AVAILABLE if is_canonical else STATUS_NEEDS_REVIEW
    if group == GROUP_OFFLINE:
        return STATUS_OFFLINE
    if group == GROUP_UNCLASSIFIED:
        return STATUS_NEEDS_REVIEW
    return STATUS_REVENUE_ONLY


def _taxonomy_for_section(section: str, primary_raw, detail_raw) -> tuple[str, str, str, str]:
    """Resolve (channel, channel_label, platform, platform_label) for a row whose
    authoritative GROUP is ``section`` (from the durable acquisition_group).

    The channel/platform are derived from the raw HubSpot source fields, but only
    when the derived group agrees with the row's stored section. When the raw
    fields are missing or the deal's stored group is ambiguous/unclassified (so it
    folds into a different section than its primary contact's raw source), the row
    lands in the section's explicit "Unspecified" channel — never mis-attributed
    to a real platform.
    """
    tax = classify_source_taxonomy(primary_raw, detail_raw)
    if tax["source_group"] == section:
        return (tax["source_channel"], tax["source_channel_label"],
                tax["source_platform"], tax["source_platform_label"])
    return (CH_UNSPECIFIED, CHANNEL_LABELS[CH_UNSPECIFIED],
            PF_UNSPECIFIED, PLATFORM_LABELS[PF_UNSPECIFIED])


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _nullable_float(v):
    """Coerce to float, or None when missing/invalid — never fabricates a 0.0."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _roas(won, spend):
    """ROAS = won revenue / spend, or None when spend is missing/zero.

    Never returns a fabricated 0.00x — a null spend (no connected source) yields a
    null ROAS so the UI renders "Unavailable".
    """
    if spend is None or spend <= 0:
        return None
    # A WITHHELD revenue numerator (every deal in the bucket has an unproven
    # currency) makes ROAS unknown, not 0.00x — `_safe_float(None)` used to turn
    # it into a confident zero return.
    if won is None:
        return None
    return round(_safe_float(won) / spend, 2)


def _window_bounds(window: str, now):
    resolved = resolve_window(window, now=now)
    start = date.fromisoformat(resolved["start_date"]) if resolved["start_date"] else None
    end = date.fromisoformat(resolved["end_date"])
    return resolved, start, end


def _bucket_revenue(bucket: dict):
    """A bucket's USD revenue, or ``None`` when NOTHING in it was proven.

    A bucket holding three won deals whose currency could never be resolved has
    an unknown total, not a $0 one. Returning 0.00 there would put a confident
    zero next to a customer count of three — and, in the Google Ads group, feed a
    0.00x ROAS built on a numerator we never had.
    """
    if not bucket.get("revenue_known_deals") and bucket.get("customers"):
        return None
    return round(bucket.get("won_revenue") or 0.0, 2)


def _unavailable_revenue_by_source(resolved, canonical, spend_truth, now) -> dict:
    """The explicit quarantined page (PR-ADS-153E-B).

    Revenue by Source has no second revenue lineage to fall back to, by design:
    `deal_source_attribution` holds a different population and would silently
    change what the page means mid-incident. Leads/SQL structure is still shown
    where it is readable, but every revenue figure is null with a stated reason.
    """
    return {
        "window": resolved,
        "groups": [],
        "summary": source_attribution_health_counts(),
        "source_spend_truth": spend_truth,
        "source_truth": canonical_revenue.CANONICAL_SOURCE,
        "revenue_available": False,
        "revenue_unavailable_reason": canonical.get("reason"),
        "revenue_unavailable_detail": canonical.get("detail"),
        "revenue_violation_codes": canonical.get("violation_codes") or [],
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "canonical_reconciliation": None,
        "as_of": canonical.get("as_of"),
        "google_ads_conversion_value_used": False,
        "legacy_fallback_used": False,
        "sql_reconciliation": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _section_bucket(acquisition_group: str) -> str:
    """Map a stored group to its display section. Ambiguous and unclassified deal
    revenue fold into the Unclassified / Needs Review section (counted once)."""
    if acquisition_group in (GROUP_GOOGLE_ADS, GROUP_OTHER_PAID, GROUP_ORGANIC, GROUP_OFFLINE):
        return acquisition_group
    return GROUP_UNCLASSIFIED


def _finalize_channels(group: str, channels: dict, group_spend,
                       google_status_label: str | None = None) -> list:
    """Shape the nested channel/platform dicts into sorted list rows (PR-ADS-133).

    Only the Google Ads group carries spend/ROAS, and only on its canonical
    Paid Search → Google Ads channel/platform. That bucket's ROAS is computed
    from its OWN won revenue / spend (not the group ROAS), so a non-canonical
    Google bucket never distorts it. Every other bucket — and any non-canonical
    Google bucket — keeps spend/ROAS None (never a fabricated $0 / 0.00x) and
    gets an honest status label. When the Google spend source is unavailable
    (group_spend is None), even the canonical bucket shows spend/ROAS None.

    PR-ADS-140: ``google_status_label`` is the canonical Google Ads spend-truth
    status (ROAS available / FX withheld / coverage incomplete / source
    unavailable). It labels the canonical bucket so a withheld-USD state reads
    honestly instead of always claiming "ROAS available".
    """
    has_spend = group in GROUPS_WITH_SPEND
    out = []
    for ch_key, ch in channels.items():
        # Spend/ROAS attach ONLY to the canonical Paid Search channel — never to a
        # non-canonical bucket (e.g. an ``unspecified`` bucket from a raw-source
        # mismatch) that happens to sit inside the Google Ads group.
        canonical_channel = has_spend and ch_key == CH_PAID_SEARCH
        ch_won = _bucket_revenue(ch)
        platforms = []
        for pf_key, pf in ch["platforms"].items():
            canonical_platform = canonical_channel and pf_key == PF_GOOGLE_ADS
            pf_won = _bucket_revenue(pf)
            platforms.append({
                "platform": pf_key,
                "label": pf["label"],
                "leads": pf["leads"],
                "sqls": pf["sqls"],
                "customers": pf["customers"],
                "won_revenue": pf_won,
                "revenue_unavailable_deals": pf.get("revenue_unavailable_deals", 0),
                "revenue_complete": not pf.get("revenue_unavailable_deals"),
                "spend": group_spend if canonical_platform else None,
                # Bucket-level ROAS = this bucket's won revenue / spend.
                "roas": _roas(pf_won, group_spend) if canonical_platform else None,
                "status": ((google_status_label or STATUS_ROAS_AVAILABLE)
                           if canonical_platform and google_status_label is not None
                           else _platform_status(group, is_canonical=canonical_platform)),
            })
        platforms.sort(key=lambda p: (p["won_revenue"] or 0.0, p["leads"]), reverse=True)
        out.append({
            "channel": ch_key,
            "label": ch["label"],
            "leads": ch["leads"],
            "sqls": ch["sqls"],
            "customers": ch["customers"],
            "won_revenue": ch_won,
            "revenue_unavailable_deals": ch.get("revenue_unavailable_deals", 0),
            "revenue_complete": not ch.get("revenue_unavailable_deals"),
            "spend": group_spend if canonical_channel else None,
            "roas": _roas(ch_won, group_spend) if canonical_channel else None,
            "platforms": platforms,
        })
    out.sort(key=lambda c: (c["won_revenue"] or 0.0, c["leads"]), reverse=True)
    return out


def build_revenue_by_source(window: str, now: datetime | None = None) -> dict:
    """Build the Revenue by Source contract for a business window.

    Each group additionally carries a ``channels`` breakdown (PR-ADS-133), and
    each channel a ``platforms`` breakdown, derived at read time from the durable
    raw HubSpot source fields. Group totals stay authoritative (from
    acquisition_group) and every nested level sums back to them. Only Google Ads
    is spend-connected / ROAS-eligible.

    Raises ValueError for an unsupported window.
    """
    resolved, start, end = _window_bounds(window, now)

    leads = repo.fetch_source_leads(start, end)
    # PR-ADS-153E-B: closed-won revenue now comes from the canonical deal ledger
    # through the shared read contract, at `all_source` scope. `deal_source_attribution`
    # held all closed-won deals but no currency contract, so an unverified amount
    # entered a USD total as if it had been proven. Fail-closed: an unreadable or
    # unproven ledger returns an explicit unavailable page rather than a silent
    # fallback to the legacy table — a fallback would change the population under
    # the same headline.
    canonical = canonical_revenue.load_won_deals(window, now=now)
    # PR-ADS-140: Google Ads source spend is the CANONICAL campaign-daily spend
    # truth — the SAME number the Revenue Decision Mart shows at the top — NOT the
    # geo/country table (repo.fetch_campaign_country_spend). Geo spend stays
    # diagnostic only and is never the source-level Google Ads denominator, so the
    # page can no longer tell two spend truths (canonical top vs geo Google Ads row).
    spend_truth = revenue_spend_truth_service.build_google_ads_spend_truth(window, now=now)

    if not canonical.get("available"):
        return _unavailable_revenue_by_source(resolved, canonical, spend_truth, now)

    revenue_rows = canonical_revenue.canonical_deal_rows(
        canonical, revenue_scope.SCOPE_ALL_SOURCE)
    canonical_totals = canonical_revenue.summarize_deals(
        canonical.get("deals"), revenue_scope.SCOPE_ALL_SOURCE)

    buckets = {g: {"leads": 0, "sqls": 0, "customers": 0, "won_revenue": 0.0,
                   "revenue_known_deals": 0, "revenue_unavailable_deals": 0}
               for g in SECTION_ORDER}
    # Nested channel → platform sub-buckets per group (PR-ADS-133). Group totals
    # remain authoritative from acquisition_group; these only ADD a breakdown and
    # always sum back to the group total by construction.
    nested = {g: {} for g in SECTION_ORDER}

    def _platform_bucket(section, primary_raw, detail_raw):
        ch, ch_label, pf, pf_label = _taxonomy_for_section(section, primary_raw, detail_raw)
        channel = nested[section].setdefault(
            ch, {"label": ch_label, "leads": 0, "sqls": 0, "customers": 0,
                 "won_revenue": 0.0, "revenue_known_deals": 0,
                 "revenue_unavailable_deals": 0, "platforms": {}})
        platform = channel["platforms"].setdefault(
            pf, {"label": pf_label, "leads": 0, "sqls": 0, "customers": 0,
                 "won_revenue": 0.0, "revenue_known_deals": 0,
                 "revenue_unavailable_deals": 0})
        return channel, platform

    for row in (leads.get("rows") or []):
        g = _section_bucket(row.get("acquisition_group") or GROUP_UNCLASSIFIED)
        qualified = row.get("status_category") == "qualified"
        buckets[g]["leads"] += 1
        if qualified:
            buckets[g]["sqls"] += 1
        channel, platform = _platform_bucket(
            g, row.get("source_primary_raw"), row.get("source_detail_raw"))
        channel["leads"] += 1
        platform["leads"] += 1
        if qualified:
            channel["sqls"] += 1
            platform["sqls"] += 1

    for row in revenue_rows:
        g = _section_bucket(row.get("acquisition_group") or GROUP_UNCLASSIFIED)
        # A deal whose currency could not be proven has an UNKNOWN value, not a
        # zero one. `_safe_float(None)` used to add 0.00 here, which silently
        # asserted the deal was worth nothing and made every bucket total —
        # and the ROAS built on it — quietly wrong.
        amount = row.get("deal_amount_usd")
        channel, platform = _platform_bucket(
            g, row.get("source_primary_raw"), row.get("source_detail_raw"))
        for bucket in (buckets[g], channel, platform):
            bucket["customers"] += 1
            if amount is None:
                bucket["revenue_unavailable_deals"] += 1
            else:
                bucket["won_revenue"] += float(amount)
                bucket["revenue_known_deals"] += 1

    # Only Google Ads has a connected spend source, and it is the CANONICAL
    # campaign-daily spend (PR-ADS-140). Its spend is the reporting USD spend, shown
    # ONLY when the denominator is FX-safe (verified). Every other state — FX
    # incomplete, coverage incomplete, no source — yields spend None (NOT a
    # fabricated $0) so ROAS stays null and the UI renders an honest status.
    google_state = spend_truth.get("state")
    google_roas_status, google_status_label = _GOOGLE_STATE_STATUS.get(
        google_state,
        ("unavailable_no_spend_source", STATUS_GOOGLE_SPEND_UNAVAILABLE))
    google_spend = (spend_truth.get("usd_spend")
                    if google_state in _GOOGLE_SPEND_SHOWN_STATES
                    else None)

    groups = []
    for g in SECTION_ORDER:
        b = buckets[g]
        has_spend = g in GROUPS_WITH_SPEND
        won = _bucket_revenue(b)
        if has_spend:
            group_spend = google_spend
            # ROAS = Google Ads won revenue / canonical USD spend, only with real
            # (non-null) spend — never a $0 denominator, never a fabricated 0.00x.
            roas = _roas(won, group_spend)
            roas_status = google_roas_status
            status_label = google_status_label
            channels = _finalize_channels(g, nested[g], group_spend,
                                          google_status_label=google_status_label)
        else:
            # Non-Google sources are revenue-only: no connected spend source, so
            # spend/ROAS stay None (never $0 / 0.00x).
            group_spend = None
            roas = None
            roas_status = "unavailable_no_spend_source"
            status_label = None
            channels = _finalize_channels(g, nested[g], None)
        groups.append({
            "group": g,
            "label": GROUP_LABELS[g],
            "has_spend": has_spend,
            "spend": group_spend,
            "leads": b["leads"],
            "sqls": b["sqls"],
            "customers": b["customers"],
            "won_revenue": won,
            # Deals in this group whose USD value could not be proven. They are
            # counted as customers (the deal IS won) and excluded from revenue.
            "revenue_unavailable_deals": b["revenue_unavailable_deals"],
            "revenue_complete": b["revenue_unavailable_deals"] == 0,
            "roas": roas,
            "roas_status": roas_status,
            # PR-ADS-140: honest Google Ads spend-truth label (ROAS available / FX
            # withheld / coverage incomplete / source unavailable). None for
            # non-Google groups, which keep their revenue-only status.
            "spend_status_label": status_label,
            "channels": channels,
        })

    summary = source_attribution_health_counts()

    # PR-ADS-152 §1: the Google Ads group's DISPLAYED SQL count is the canonical
    # google_ads_source_sqls — deduplicated, exclusion-filtered, staleness-aware,
    # real campaign identity — NOT the raw classification count. The old
    # classification-derived number moves to an audit-only ``legacy_classification_sqls``
    # field. The group, its canonical Paid Search channel and its Google Ads
    # platform all display the SAME reconciled total. A mismatch withholds the count
    # (rendered "Reconciliation required"), never a contradictory normal number.
    from services import canonical_contact_outcome_service as _canon  # noqa: PLC0415
    sql_reconciliation = _canon.page_reconciliation(
        _canon.WINDOW_BUSINESS, window, _canon.SCOPE_GOOGLE_ADS_SOURCE, now=now)
    _recon_status = sql_reconciliation.get("reconciliation_status")
    _available = _recon_status != _canon.STATUS_UNAVAILABLE
    _mismatch = _recon_status == _canon.STATUS_MISMATCH
    canonical_ga_sqls = sql_reconciliation.get("google_ads_source_sqls")
    _display_sqls = None if _mismatch else canonical_ga_sqls

    for g in groups:
        if g["group"] != GROUP_GOOGLE_ADS:
            continue
        g["google_ads_source_sqls"] = canonical_ga_sqls
        g["campaign_attributable_sqls"] = sql_reconciliation.get("campaign_attributable_sqls")
        g["sqls_scope"] = _canon.SCOPE_GOOGLE_ADS_SOURCE
        g["sql_reconciliation_status"] = _recon_status
        # Only override the displayed count when the canonical reconciliation is
        # actually available; otherwise keep the classification-derived number so a
        # DB-less context still renders an honest figure.
        if _available:
            g["legacy_classification_sqls"] = g.get("sqls")
            g["sqls"] = _display_sqls
            for ch in g.get("channels") or []:
                if ch.get("channel") == CH_PAID_SEARCH:
                    ch["legacy_classification_sqls"] = ch.get("sqls")
                    ch["sqls"] = _display_sqls
                    ch["sqls_scope"] = _canon.SCOPE_GOOGLE_ADS_SOURCE
                    for pf in ch.get("platforms") or []:
                        if pf.get("platform") == PF_GOOGLE_ADS:
                            pf["legacy_classification_sqls"] = pf.get("sqls")
                            pf["sqls"] = _display_sqls
                            pf["sqls_scope"] = _canon.SCOPE_GOOGLE_ADS_SOURCE

    # ── Reconciliation to canonical all-source truth (PR-ADS-153E-B) ─────────
    # The page's own rows must add back up to the canonical business total, or
    # say exactly how much they do not cover. Nothing is dropped to make the
    # classified rows look clean: an unclassified or ambiguous deal keeps its
    # bucket, and a deal whose currency was never proven is reported as an
    # uncovered AMOUNT rather than quietly counted as $0.
    displayed_customers = sum(g["customers"] for g in groups)
    displayed_revenue = round(
        sum(g["won_revenue"] for g in groups if g["won_revenue"] is not None), 2)
    canonical_revenue_usd = canonical_totals["revenue_usd"]
    reconciliation = {
        "scope": revenue_scope.SCOPE_ALL_SOURCE,
        "source": canonical_revenue.CANONICAL_SOURCE,
        "canonical_won_deals": canonical_totals["won_deals"],
        "canonical_revenue_usd": canonical_revenue_usd,
        "displayed_customers": displayed_customers,
        "displayed_revenue_usd": displayed_revenue,
        "uncovered_deals": canonical_totals["won_deals"] - displayed_customers,
        "uncovered_revenue_usd": (
            round(canonical_revenue_usd - displayed_revenue, 2)
            if canonical_revenue_usd is not None else None),
        # Deals that ARE in a bucket but contribute no money, because their
        # currency could not be proven. They are the honest reason a bucket's
        # customer count can exceed the deals behind its revenue.
        "revenue_unavailable_deals": canonical_totals["currency_unavailable_deals"],
        "ambiguous_associations": canonical_totals["ambiguous_associations"],
        "failed_associations": canonical_totals["failed_associations"],
        "reconciles": (
            canonical_totals["won_deals"] == displayed_customers
            and canonical_revenue_usd is not None
            and abs(canonical_revenue_usd - displayed_revenue) < 0.01),
    }

    return {
        "window": resolved,
        "groups": groups,
        "summary": summary,
        # PR-ADS-140: proof for the Google Ads spend number — the canonical
        # campaign-daily spend truth (native GBP + USD reporting + FX/coverage
        # status). Geo spend is diagnostic and explicitly NOT used here.
        "source_spend_truth": spend_truth,
        # PR-ADS-153E-B: won revenue is the canonical deal ledger. The HubSpot
        # source classification still decides which BUCKET a deal lands in; it no
        # longer decides which deals exist or what they are worth.
        "source_truth": canonical_revenue.CANONICAL_SOURCE,
        "classification_truth": "hubspot_original_source_classification",
        "revenue_available": True,
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "canonical_reconciliation": reconciliation,
        "as_of": canonical.get("as_of"),
        "legacy_fallback_used": False,
        "google_ads_conversion_value_used": False,
        # PR-ADS-152: canonical SQL-scope reconciliation metadata (§7).
        "sql_reconciliation": sql_reconciliation,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _source_detail_row(r: dict, group: str, channel_label: str, platform_label: str) -> dict:
    """One 'client / deal behind this source platform' drawer row (PR-ADS-133).

    Only durably-stored fields are populated. Contact name, company id, deal name
    and HubSpot lifecycle are NOT persisted locally, so they are explicit None →
    the UI renders "Unavailable", never a fabricated value. A missing amount stays
    None (never a fake $0).
    """
    amount = _nullable_float(r.get("deal_amount_usd"))
    return {
        # PR-ADS-153E-B: the canonical ledger stores the DEAL's own name. The
        # legacy `company` column was the associated contact's employer, joined
        # in from `gclid_attribution` — a different record about a different
        # object. It is not renamed onto `company` here, because a deal name
        # under a "Company" heading is a fabricated label.
        "deal_name": r.get("deal_name") or None,
        "company": None,                    # contact employer is not on the ledger
        "company_id": None,                 # company record id is not stored
        "main_contact": None,               # contact name is not stored
        "contact_id": r.get("primary_contact_id") or None,
        "lifecycle_stage": None,            # HubSpot lifecyclestage is not persisted
        "status_category": r.get("status_category") or None,
        "deal": r.get("deal_name") or None,
        "deal_id": r.get("deal_id") or None,
        "amount": round(amount, 2) if amount is not None else None,
        # Why an amount is missing, so the drawer can say "currency unavailable"
        # instead of leaving a bare dash next to a real won deal.
        "currency_status": r.get("currency_status"),
        "currency_reason": r.get("currency_reason"),
        "close_date": r.get("deal_close_date"),
        "source": channel_label,
        "source_drilldown_1": platform_label,
        "source_drilldown_2": None,         # hs_analytics_source_data_2 not persisted
        "campaign_source_label": r.get("campaign_name") or r.get("source_detail_raw") or None,
        "attribution_status": r.get("attribution_status") or None,
        "attribution_scope": r.get("attribution_scope"),
    }


def _source_contact_row(r: dict, channel_label: str, platform_label: str) -> dict:
    """One 'lead / SQL behind this source platform' drawer row (PR-ADS-133).

    Proves a classified contact. Only durably-stored fields are populated; contact
    name, company id and HubSpot lifecycle are NOT persisted, so they are explicit
    None → the UI renders "Unavailable". ``is_sql`` marks a qualified contact.
    """
    status = r.get("status_category") or None
    return {
        "company": r.get("company") or None,
        "company_id": None,                 # company record id is not stored
        "main_contact": None,               # contact name is not stored
        "contact_id": r.get("contact_id") or None,
        "lifecycle_stage": None,            # HubSpot lifecyclestage is not persisted
        "status_category": status,
        "is_sql": status == "qualified",
        "created_date": r.get("contact_created_at"),
        "source": channel_label,
        "source_drilldown_1": platform_label,
        "source_drilldown_2": None,         # hs_analytics_source_data_2 not persisted
        "campaign_source_label": r.get("source_detail_raw") or None,
    }


def build_source_platform_detail(window: str, source_group: str, source_channel: str,
                                 source_platform: str, now: datetime | None = None) -> dict:
    """Clients/deals + leads/SQLs behind ONE source platform (PR-ADS-133).

    Read-only lazy drilldown for the Revenue by Source drawer, in two sections:

      - ``contacts``: classified contacts proving the Leads / SQLs, windowed by
        contact_created_at (the lead business-event date).
      - ``deals``: closed-won deals proving the Won Revenue, windowed by
        deal_close_date.

    Both derive their group/channel/platform with the SAME taxonomy the page rows
    use and keep only rows matching the requested (group, channel, platform).
    Never writes anything; never fabricates ids, names or amounts. ``rows`` is kept
    as an alias of ``deals`` for backward compatibility.

    Raises ValueError for an unsupported window.
    """
    resolved, start, end = _window_bounds(window, now)
    generated_at = datetime.now(timezone.utc).isoformat()

    def _matches(r):
        section = _section_bucket(r.get("acquisition_group") or GROUP_UNCLASSIFIED)
        ch, ch_label, pf, pf_label = _taxonomy_for_section(
            section, r.get("source_primary_raw"), r.get("source_detail_raw"))
        hit = section == source_group and ch == source_channel and pf == source_platform
        return hit, ch_label, pf_label

    # PR-ADS-153E-B: the deal side of the drawer is the canonical ledger, at
    # `all_source` scope, so the deals proving a bucket ARE the deals counted in
    # it. The legacy read joined `deal_source_attribution` to `gclid_attribution`
    # for a company name, which quietly restricted the drawer to deals the GCLID
    # ledger also happened to hold.
    canonical = canonical_revenue.load_won_deals(window, now=now)
    deals_fetch = {
        "available": bool(canonical.get("available")),
        "rows": (canonical_revenue.canonical_deal_rows(
            canonical, revenue_scope.SCOPE_ALL_SOURCE)
            if canonical.get("available") else []),
    }
    contacts_fetch = repo.fetch_source_contact_details(start, end)
    # Fail closed on the DEAL side specifically. Rendering an empty deals
    # section because the ledger was unreadable — while the contacts section
    # loads and the drawer otherwise looks healthy — reads as "this bucket has
    # no deals", which is a far stronger claim than "we could not read them".
    # An unreadable canonical ledger makes the whole drilldown unavailable, with
    # the reason named, and no legacy ledger is consulted in its place.
    if not deals_fetch.get("available") or not contacts_fetch.get("available"):
        unreadable_ledger = not deals_fetch.get("available")
        return {
            "window": resolved, "source_group": source_group,
            "source_channel": source_channel, "source_platform": source_platform,
            "contacts": [], "deals": [], "rows": [],
            # Counts are NULL, not 0 — see above.
            "summary": {"contacts": None, "sqls": None, "deals": None},
            # Contact evidence is a DIFFERENT source (the classification
            # tables) and may still be readable. It is reported as its own
            # availability flag rather than merged into the drawer's status, so
            # a reader can see that one half is readable without the response
            # ever implying the revenue half is healthy.
            "contact_evidence_available": bool(contacts_fetch.get("available")),
            "revenue_source": canonical_revenue.CANONICAL_SOURCE,
            "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
            "revenue_available": not unreadable_ledger,
            "revenue_unavailable_reason": (canonical.get("reason")
                                           if unreadable_ledger else None),
            "revenue_violation_codes": (canonical.get("violation_codes") or []
                                        if unreadable_ledger else []),
            "as_of": canonical.get("as_of"),
            "legacy_fallback_used": False,
            "source_health": {
                "status": (canonical.get("reason") or "canonical_ledger_unreadable")
                if unreadable_ledger else "database_unavailable"},
            "generated_at": generated_at,
        }

    deals = []
    for r in (deals_fetch.get("rows") or []):
        hit, ch_label, pf_label = _matches(r)
        if hit:
            deals.append(_source_detail_row(r, source_group, ch_label, pf_label))

    contacts = []
    for r in (contacts_fetch.get("rows") or []):
        hit, ch_label, pf_label = _matches(r)
        if hit:
            contacts.append(_source_contact_row(r, ch_label, pf_label))

    return {
        "window": resolved,
        "source_group": source_group,
        "source_channel": source_channel,
        "source_platform": source_platform,
        "contacts": contacts,
        "deals": deals,
        "rows": deals,   # backward-compatible alias
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "revenue_available": True,
        "as_of": canonical.get("as_of"),
        "legacy_fallback_used": False,
        "summary": {
            "contacts": len(contacts),
            "sqls": sum(1 for c in contacts if c.get("is_sql")),
            "deals": len(deals),
        },
        "source_health": {"status": "ready"},
        "generated_at": generated_at,
    }


def source_attribution_health_counts() -> dict:
    """Durable classification/attribution counts for the source-health summary."""
    import db.writers as db_writers  # noqa: PLC0415
    return db_writers.source_attribution_health_counts()


def build_source_attribution_health() -> dict:
    return {
        "summary": source_attribution_health_counts(),
        "classification_rule_version": RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Classification helpers (pure, shared with the backfill) ──────────────────


def classify_contact_row(contact: dict) -> dict:
    """Classify one HubSpot contact dict into a durable classification row.

    primary = Original Source (hs_analytics_source); detail = Original Source
    Drill-Down (hs_analytics_source_data_1). Raw values are preserved.
    """
    props = contact.get("properties") or contact
    primary = props.get("hs_analytics_source")
    detail = props.get("hs_analytics_source_data_1")
    contact_id = contact.get("id") or contact.get("contact_id")
    key = (str(contact_id).strip() if contact_id else "") or f"id:{contact.get('row_id', '')}"
    from db.writers import _map_status_category  # noqa: PLC0415
    return {
        "contact_key": key,
        "contact_id": str(contact_id) if contact_id else None,
        "source_primary_raw": primary,
        "source_detail_raw": detail,
        "acquisition_group": classify_source(primary, detail),
        "classification_rule_version": RULE_VERSION,
        "contact_created_at": props.get("createdate"),
        "status_category": _map_status_category(props.get("mql_status")),
    }


def attribute_deal_row(deal: dict) -> dict:
    """Build a durable deal-source-attribution row from a deal with its contacts.

    deal: {deal_id, deal_close_date, deal_amount_usd,
           contacts:[{contact_id, source_primary, source_detail}]}.
    Revenue is never split; the deal maps to one group / ambiguous / unclassified.
    """
    contacts = deal.get("contacts") or []
    groups = [classify_source(c.get("source_primary"), c.get("source_detail")) for c in contacts]
    decision = attribute_deal(groups)
    primary_contact = contacts[0] if contacts else {}
    return {
        "deal_id": deal.get("deal_id"),
        "associated_contact_id": primary_contact.get("contact_id"),
        "acquisition_group": decision["acquisition_group"],
        "source_primary_raw": primary_contact.get("source_primary"),
        "source_detail_raw": primary_contact.get("source_detail"),
        "attribution_status": decision["attribution_status"],
        "attribution_reason": decision["attribution_reason"],
        "deal_close_date": deal.get("deal_close_date"),
        "deal_amount_usd": deal.get("deal_amount_usd"),
        "classification_rule_version": RULE_VERSION,
    }


# ── Durable, resumable historical backfill (reuses PR-ADS-114 job pattern) ────


def run_source_attribution_backfill(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    dry_run: bool = True,
    chunk_months: int = 1,
    resume: bool = False,
    job_id: str | None = None,
    progress: dict | None = None,
    checkpoint=None,
    load_completed=None,
) -> dict:
    """Classify all historical contacts and attribute all closed-won deals.

    Reads HubSpot read-only; writes ONLY the local classification tables (when
    not dry_run). Resumable via durable checkpoints. NEVER writes to HubSpot or
    Google Ads, and NEVER overwrites raw HubSpot data.
    """
    from connectors.hubspot_pull import (  # noqa: PLC0415
        pull_all_contacts_in_range,
        pull_closed_won_deals_with_sources_in_range,
    )
    import db.writers as db_writers  # noqa: PLC0415
    from dateutil.relativedelta import relativedelta  # noqa: PLC0415

    # Default range: All Time floor through today.
    end = date.fromisoformat(date_to) if date_to else datetime.now(tz=timezone.utc).date()
    start = date.fromisoformat(date_from) if date_from else date(2018, 1, 1)
    if start > end:
        raise ValueError("date_from must be before or equal to date_to")

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = progress if progress is not None else {}
    state.update({"running": True, "dry_run": dry_run, "phase": "starting",
                  "job_id": job_id, "started_at": started_at})

    if resume and load_completed is not None:
        try:
            completed = set(load_completed() or [])
        except Exception:  # noqa: BLE001
            completed = set()
    else:
        completed = set()

    summary = {"contacts_classified": 0, "deals_attributed": 0,
               "ambiguous_deals": 0, "unclassified_deals": 0, "failed": 0}
    chunks_detail: list = []
    errors: list = []

    def _emit(status):
        if checkpoint is None:
            return
        try:
            checkpoint(job_id, {
                "status": status, "phase": state.get("phase"),
                "current_chunk": state.get("current_chunk"),
                "completed_chunks": list(completed),
                "summary": dict(summary), "chunks": list(chunks_detail), "errors": list(errors),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("[source_backfill] checkpoint failed: %s", exc)

    cursor = start
    step = relativedelta(months=max(1, chunk_months))
    while cursor <= end:
        chunk_to = min(cursor + step - relativedelta(days=1), end)
        chunk_key = f"{cursor.isoformat()}:{chunk_to.isoformat()}"
        if resume and chunk_key in completed:
            cursor = chunk_to + relativedelta(days=1)
            continue
        state["current_chunk"] = chunk_key
        chunk = {"chunk": chunk_key, "contacts": 0, "deals": 0, "deal_lookups_failed": 0}

        # Contacts → classify. (Re-upserting on a retried chunk is idempotent.)
        state["phase"] = "contacts"
        contacts_ok = True
        try:
            contacts = pull_all_contacts_in_range(
                date_from=cursor.isoformat(), date_to=chunk_to.isoformat())
            rows = [classify_contact_row(c) for c in contacts]
            chunk["contacts"] = len(rows)
            summary["contacts_classified"] += len(rows)
            if not dry_run and rows:
                db_writers.upsert_contact_source_classification(rows)
        except Exception as exc:  # noqa: BLE001
            contacts_ok = False
            errors.append(f"contacts {chunk_key}: {exc}")
            summary["failed"] += 1

        # Closed-won deals → attribute. A deal whose HubSpot association/source
        # lookup failed is NOT turned into an Unclassified row — it is skipped
        # (existing durable attribution untouched), counted failed, and keeps the
        # chunk incomplete so resume retries it. Only a SUCCESSFUL zero-contact
        # lookup becomes Unclassified.
        state["phase"] = "deals"
        deals_ok = True
        try:
            deals = pull_closed_won_deals_with_sources_in_range(
                date_from=cursor.isoformat(), date_to=chunk_to.isoformat())
            drows = []
            failed_lookups = 0
            for d in deals:
                if d.get("lookup_failed"):
                    failed_lookups += 1
                    continue
                drows.append(attribute_deal_row(d))
            chunk["deals"] = len(drows)
            chunk["deal_lookups_failed"] = failed_lookups
            for dr in drows:
                st = dr["attribution_status"]
                if st == "attributed":
                    summary["deals_attributed"] += 1
                elif st == "ambiguous":
                    summary["ambiguous_deals"] += 1
                else:
                    summary["unclassified_deals"] += 1
            if not dry_run and drows:
                db_writers.upsert_deal_source_attribution(drows)
            if failed_lookups:
                deals_ok = False
                summary["failed"] += failed_lookups
                errors.append(
                    f"deals {chunk_key}: {failed_lookups} deal source lookup(s) failed (retryable)")
        except Exception as exc:  # noqa: BLE001
            deals_ok = False
            errors.append(f"deals {chunk_key}: {exc}")
            summary["failed"] += 1

        chunks_detail.append(chunk)
        # Only a fully-successful chunk is recorded complete; an incomplete chunk
        # is retried on resume.
        if contacts_ok and deals_ok:
            completed.add(chunk_key)
        state["completed_chunks"] = list(completed)
        _emit("running")
        cursor = chunk_to + relativedelta(days=1)

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = "failed" if (errors and not chunks_detail) else ("partial" if errors else "success")
    result = {
        "status": status, "dry_run": dry_run,
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "chunk_months": chunk_months, "started_at": started_at, "finished_at": finished_at,
        "summary": summary, "chunks": chunks_detail, "errors": errors,
    }
    state.update({"running": False, "phase": "done", "finished_at": finished_at, "latest": result})
    if checkpoint is not None:
        state["phase"] = "done"
        checkpoint(job_id, {
            "status": status, "phase": "done", "current_chunk": None,
            "completed_chunks": list(completed), "summary": dict(summary),
            "chunks": list(chunks_detail), "errors": list(errors), "finished_at": finished_at,
        })
    return result
