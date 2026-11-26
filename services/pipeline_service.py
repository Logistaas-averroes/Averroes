"""
services/pipeline_service.py

PR-ADS-178B — the one Pipeline page: leads and deals together (``docs/61``).

The Leads page and the Closed-Won Deals page were two pages under the
Pipeline menu item, and Dashboard → Deals was a third surface with its own
composition. The same metric came from different builders: three SQL counts,
three closed-won counts over one ledger, two customer builders, a page-side
revenue check beside the reader contract. ``build_pipeline`` is now the ONE
builder for all three surfaces (the Pipeline page and Dashboard → Deals read
it; ``/api/pipeline`` serves it). It composes canonical contracts inside ONE
closed-won snapshot and computes no business figure of its own:

  * SQLs — the window's Google Ads ACQUISITION COHORT (the mart's ``cohort``
    block and ``summary.metrics.sqls``; the same count as Overview, Campaigns
    and Dashboard → Deals). Withheld means absent, with the cohort's reason.
  * Closed-won deals, customers, won revenue — the mart deal view's summary
    (``summary.metrics``: the window's canonical reader-contract verdicts, the
    identity Overview, Revenue and Channels publish), republished as
    ``{value, status, reason}``; average deal value — the deal view's own
    ``ledger_summary`` (the reader contract over the very rows the list shows),
    only when it counted the same deals and revenue. Customers are distinct
    HubSpot companies and are never summed.
  * The lifecycle funnel — PR-ADS-182A (owner decision): the ACQUISITION
    COHORT, by contact create date (``docs/67``). Contacts created in the
    window and how many have reached each stage so far: the mart deal view's
    ``cohort`` block (the build the SQL card reads), republished by the ONE
    block producer Overview uses (``dashboard_overview_service.
    acquisition_funnel_block``) under the cohort's one verdict. No
    stage-entry count is published on this page any more.
  * The deal list — the mart deal view's rows (``build_revenue_deals``), only
    when the closed-won population is published AND the list has exactly that
    many deals; otherwise the list is absent (None) with the reason.
  * Customers won — one entry per distinct company, only when the customer
    count is published AND the list numbers exactly that count. The lead
    filters do not touch it (PR-ADS-178B-F1 m1). The block also carries the
    closed-won reader contract's OWN verdicts for customers and closed-won
    deals, read straight from ``load_won_deals`` in this snapshot — the
    reading the retired ``/api/crm-funnel/customers-won`` published
    (PR-ADS-166A), which the cross-page parity audit compares with the mart
    deal view's figures beside it (consumer ``pipeline/customers_won``,
    PR-ADS-178C).
  * The source breakdown — the deal list partitioned by the deal's OWN source
    group, each part through ``subset_metrics`` (the reader contract).

The Scope and Source filters are LEAD filters: they narrow the funnel (and the
paged contact list served by ``/api/crm-funnel/contacts``), never a deal
figure. On the cohort funnel a Source filter (and Scope Google Ads-source) reads
the cohort's own partition by source group; Campaign- / Keyword-attributable
cannot be honoured there and the funnel says so (unavailable, with the reason).
Read-only: no write to HubSpot, Google Ads or the database.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from analysis import closed_won_truth as cwt
from analysis import revenue_scope
from analysis.source_classification import GROUP_GOOGLE_ADS
from services import canonical_contract
from services import canonical_crm_funnel_service as crm_funnel
from services import canonical_revenue_service as canonical_revenue
from services.canonical_contract import resolve_canonical_window
from services.revenue_decision_mart import build_revenue_decision_mart

logger = logging.getLogger(__name__)

PAGE = "pipeline"

#: The funnel's label (PR-ADS-182A): the acquisition cohort, by create date.
FUNNEL_LABEL = "Acquisition cohort"
#: What the funnel counts, in the page's words.
FUNNEL_DESCRIPTION = ("Contacts created in these days, and how far they have got "
                      "so far.")
#: PR-ADS-182A — a lead Scope the acquisition cohort is not partitioned by
#: (campaign- / keyword-attributable need the campaign resolver per contact):
#: the funnel is unavailable under it, with this reason — never the
#: unfiltered cohort under the filter's name.
REASON_SCOPE_NOT_ON_COHORT_BASIS = "lead_scope_not_on_cohort_basis"
#: Scope Google Ads-source with a Source other than Google Ads: a contact has
#: one source group, so the two filters select no contact together.
REASON_FILTERS_SELECT_NO_CONTACT = "lead_filters_select_no_contact"
#: The metric-truth scope of an unfiltered Pipeline funnel — the Overview
#: funnel's (the parity audit compares the two).
FUNNEL_TRUTH_SCOPE_ALL_SOURCE = "all_source_acquisition_cohort"
#: The lead filters (Scope, Source) apply to these sections only.
LEAD_FILTER_APPLIES_TO = ("funnel", "leads")

#: The deal list does not number the published count: it is not shown (the
#: PR-ADS-164A reason the UI already names, "the deal rows received do not
#: match the published count").
REASON_DEAL_LIST_MISMATCH = "closed_won_rows_do_not_match_count"
#: A published deal count of 0 has no average.
REASON_NO_DEALS_TO_AVERAGE = "no_closed_won_deals_in_window"
#: The mart deal view answered no verdict for a metric: fail closed.
REASON_VERDICT_MISSING = "closed_won_verdict_missing"
#: The average deal value's own population (the deal count it divides by,
#: the deal count and revenue of the summary it comes from) is not the
#: published deals and revenue beside it: it describes other deals, so it is
#: not shown (PR-ADS-178B-F1 m3).
REASON_AVERAGE_POPULATION_MISMATCH = "average_deal_value_population_mismatch"
#: A deal whose source group the ledger does not carry. Shown as itself,
#: never folded into another group.
SOURCE_NOT_RECORDED = "not_recorded"

TRUTH_FOOTER = ("Open and lost deals are not in the deal ledger: only closed-won "
                "deals are synced, so open pipeline, lost deals and opportunity "
                "aging are not shown here.")

# ── Customers won (PR-ADS-166A; moved here from the funnel service by
#    PR-ADS-178C, which retired ``/api/crm-funnel/customers-won``) ──────────
#: What the Customers won reading is, said on the payload itself: distinct
#: HubSpot companies on closed-won deals by close date — never the lifecycle
#: stage-move count ("Moved to Customer stage in HubSpot"), and never derived
#: from it. Nothing in this reading touches the funnel.
CUSTOMERS_WON_METRIC_FAMILY = "closed_won_customers"
CUSTOMERS_WON_WINDOW_BASIS = "deal_close_date"
CUSTOMERS_WON_DEDUP_KEY = "hubspot_company_id"
CUSTOMERS_WON_LABEL = "Customers won"
CUSTOMERS_WON_DEFINITION = (
    "Distinct HubSpot companies with a closed-won deal whose close date is "
    "in this period.")
CUSTOMERS_WON_NOT_THE_SAME_AS = (
    "Not the same as contacts moved to the Customer lifecycle stage: that "
    "counts CRM stage changes, including old contacts updated by the team.")
#: The list does not number the published customer count: not shown.
REASON_ROWS_DO_NOT_MATCH = "closed_won_rows_do_not_match_count"
#: What a won customer's name field supports.
COMPANY_NAMED = "named"
COMPANY_NO_NAME = "no_name_in_hubspot"      # read, and HubSpot holds none
COMPANY_NAME_NOT_LOADED = "not_loaded"      # never asked
COMPANY_NOT_RETURNED = "not_returned"       # asked; HubSpot did not return it
COMPANY_NAME_UNAVAILABLE = "unavailable"    # the name store could not be read

#: Deal fields the list and its drawer show. ``company`` is not one: the
#: ledger never carries the contact's employer (it was always null).
_DEAL_FIELDS = ("deal_id", "deal_name", "deal_close_date", "deal_amount_usd",
                "currency_status", "deal_stage_label", "acquisition_group",
                "campaign_name", "country", "attribution_status",
                "attribution_scope")


def _verdict(m) -> dict:
    """``{value, status, reason}`` exactly as the contract carries it, or
    unavailable when it carries none. A value never survives a non-published
    status; published with no number is not a publishable zero."""
    if not isinstance(m, dict) or m.get("status") not in cwt.PUBLICATION_STATUSES:
        return cwt.metric(None, cwt.UNAVAILABLE, REASON_VERDICT_MISSING)
    if m["status"] == cwt.PUBLISHED and m.get("value") is None:
        return cwt.metric(None, cwt.UNAVAILABLE, REASON_VERDICT_MISSING)
    extra = {k: v for k, v in m.items() if k not in ("value", "status", "reason")}
    return cwt.metric(m.get("value"), m["status"], m.get("reason"), **extra)


def _average_deal_value(ledger: dict, deals: dict, revenue: dict) -> dict:
    """The deal view's own average (``ledger_summary.average_deal_value``:
    the published total ÷ every won deal), with a status. It inherits a
    withheld total or count; a published 0 deals has no average (never $0);
    and it is shown only when the ledger summary it comes from counted the
    SAME deals and revenue as the cards beside it.

    ``deal_count`` is the divisor (``won_revenue / deal_count``), so it is
    compared with the published deals itself — not only the summary's
    ``closed_won_deals`` (PR-ADS-178B-F1 m3). A summary with no average is a
    missing verdict; an average over other deals or other revenue is a
    population mismatch, said in its own words."""
    for m in (revenue, deals):
        if m["status"] != cwt.PUBLISHED:
            return cwt.metric(None, m["status"], m["reason"])
    if not deals["value"]:
        return cwt.metric(None, cwt.UNAVAILABLE, REASON_NO_DEALS_TO_AVERAGE)
    led = ledger or {}
    value = led.get("average_deal_value")
    if value is None:
        return cwt.metric(None, cwt.UNAVAILABLE, REASON_VERDICT_MISSING)
    if (led.get("deal_count") != deals["value"]
            or led.get("closed_won_deals") != deals["value"]
            or led.get("won_revenue") is None
            or round(float(led["won_revenue"]), 2) != round(float(revenue["value"]), 2)):
        return cwt.metric(None, cwt.UNAVAILABLE, REASON_AVERAGE_POPULATION_MISMATCH)
    return cwt.metric(value, cwt.PUBLISHED)


def _rates() -> dict:
    """SQL → customer joins two cohorts (SQLs by contact creation, won deals
    by close date); the opportunity rates need a stage the ledger does not
    hold. None is computed: the one rate the page names is unavailable with
    the reason."""
    return {"sql_to_customer_rate": cwt.incompatible_rate(cwt.R_SQL_TO_CUSTOMER_INCOMPATIBLE)}


def _deal_row(r: dict) -> dict:
    return {k: r.get(k) for k in _DEAL_FIELDS}


def _deal_list(rows: list, deals: dict, population: dict) -> tuple:
    """``(rows | None, status, reason)``. The list is shown only beside a
    published closed-won count (``deals``: the KPI card's verdict, with its
    status and reason when it is not published), only when the deal view's
    own population verdict says the same count, and only when the list
    numbers exactly that count."""
    if deals["status"] != cwt.PUBLISHED:
        return None, deals["status"], deals["reason"]
    if (population["status"] != cwt.PUBLISHED or population["value"] != deals["value"]
            or len(rows) != deals["value"]):
        return None, cwt.UNAVAILABLE, REASON_DEAL_LIST_MISMATCH
    out = [_deal_row(r) for r in rows]
    # Close date newest first, then the larger amount (owner decision 7).
    out.sort(key=lambda d: (str(d.get("deal_close_date") or ""),
                            d.get("deal_amount_usd") or 0.0), reverse=True)
    return out, cwt.PUBLISHED, None


def _source_breakdown(base: dict, deal_rows: list | None) -> list | None:
    """The deal list partitioned by each deal's OWN source group, each part
    through the reader contract (``subset_metrics``). None when the list is
    withheld. Customers per group are distinct companies and do not add up."""
    if deal_rows is None:
        return None
    groups: dict = {}
    for r in deal_rows:
        groups.setdefault(r.get("acquisition_group") or SOURCE_NOT_RECORDED, []).append(r)
    out = []
    for group, members in groups.items():
        m = canonical_revenue.subset_metrics(base, members)
        out.append({
            "source_group": group,
            "closed_won_deals": m["closed_won_deals"]["value"],
            "customers": m["customers"]["value"],
            "won_revenue": m["revenue_usd"]["value"],
            "metrics": {k: m[k] for k in ("closed_won_deals", "customers", "revenue_usd")},
            "customers_additive": False,
        })
    out.sort(key=lambda g: (g["won_revenue"] or 0.0, g["closed_won_deals"] or 0),
             reverse=True)
    return out


def _iso(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _reader_verdict(base: dict, name: str) -> dict:
    """The closed-won reader contract's own verdict for ``name``
    (``load_won_deals``' ``canonical_metrics``), exactly as it carries it.

    A missing or malformed verdict is ``unavailable`` — never a count, and
    never the other metric's value. A value never survives a non-published
    status; published with no number is not a publishable zero."""
    m = (base.get("canonical_metrics") or {}).get(name)
    if not isinstance(m, dict) or m.get("status") not in cwt.PUBLICATION_STATUSES:
        return cwt.metric(None, cwt.UNAVAILABLE, base.get("reason") or REASON_VERDICT_MISSING)
    if m["status"] == cwt.PUBLISHED:
        if m.get("value") is None:
            return cwt.metric(None, cwt.UNAVAILABLE, REASON_VERDICT_MISSING)
        return cwt.metric(m["value"], cwt.PUBLISHED)
    return cwt.metric(None, m["status"], m.get("reason"))


def _won_customers_list(base: dict, customers: dict) -> tuple:
    """``(rows | None, omitted_reason | None)`` for a PUBLISHED customer count.

    One entry per distinct company on the window's won deals, from the
    reader's own population and identity map (``deals`` and
    ``company_ids_by_deal``) — the same members the published count was made
    from. Nothing here decides who is a customer; it only lists them and adds
    the name the platform holds. If the entries do not number exactly the
    published count, there is no list (``closed_won_rows_do_not_match_count``):
    a list that disagrees with its own headline is not shown beside it."""
    if customers.get("status") != cwt.PUBLISHED or not base.get("available"):
        return None, None
    by_deal = base.get("company_ids_by_deal")
    if not isinstance(by_deal, dict):
        return None, REASON_ROWS_DO_NOT_MATCH
    grouped: dict = {}
    for deal in base.get("deals") or []:
        ids = {str(c) for c in by_deal.get(str(deal.get("deal_id"))) or []}
        if len(ids) != 1:
            # A published count means every member resolves to one company.
            return None, REASON_ROWS_DO_NOT_MATCH
        entry = grouped.setdefault(ids.pop(), {"deals": 0, "latest": None})
        entry["deals"] += 1
        closed = _iso(deal.get("deal_close_date"))
        if closed and (entry["latest"] is None or closed > entry["latest"]):
            entry["latest"] = closed
    if len(grouped) != customers.get("value"):
        return None, REASON_ROWS_DO_NOT_MATCH

    from db import hubspot_name_repository as names_repo  # noqa: PLC0415
    stored = names_repo.fetch_company_names(list(grouped))
    names = stored.get("names") or {}
    rows = []
    for company_id, entry in grouped.items():
        if not stored.get("available"):
            name, status = None, COMPANY_NAME_UNAVAILABLE
        elif company_id not in names:
            name, status = None, COMPANY_NAME_NOT_LOADED
        elif names[company_id].get("name"):
            name, status = names[company_id]["name"], COMPANY_NAMED
        elif not names[company_id].get("read"):
            name, status = None, COMPANY_NOT_RETURNED
        else:
            name, status = None, COMPANY_NO_NAME
        rows.append({"company_id": company_id, "company_name": name,
                     "company_name_status": status,
                     "closed_won_deals": entry["deals"],
                     "latest_close_date": entry["latest"]})
    rows.sort(key=lambda r: (r["latest_close_date"] or "", r["company_id"]),
              reverse=True)
    return rows, None


def _customers_won_reading(base: dict, resolved: dict) -> dict:
    """The Customers won block's own reading of the closed-won reader
    contract (PR-ADS-166A's ``/api/crm-funnel/customers-won`` payload, moved
    into this builder by PR-ADS-178C): the window, the metric family, and the
    reader's verdicts for customers and closed-won deals — two fields, never
    summed, never derived from each other or from the funnel — with the
    provenance the cross-page parity audit checks (consumer
    ``pipeline/customers_won``, source: the canonical deal ledger)."""
    customers = {**_reader_verdict(base, "customers"), "dedup_key": CUSTOMERS_WON_DEDUP_KEY}
    deals = _reader_verdict(base, "closed_won_deals")
    window = {k: resolved.get(k) for k in ("key", "start_date", "end_date", "timezone")}

    def entry(metric, verdict):
        return {"metric": metric,
                "data_source": canonical_contract.SOURCE_CANONICAL_DEAL_LEDGER,
                "scope": "all_source_business_revenue",
                "truth_status": (canonical_contract.TRUTH_READY
                                 if verdict["status"] == cwt.PUBLISHED
                                 else canonical_contract.TRUTH_NOT_READY),
                "unavailable_reason": verdict.get("reason")}
    return {
        "metric_family": CUSTOMERS_WON_METRIC_FAMILY,
        "window_basis": CUSTOMERS_WON_WINDOW_BASIS,
        "dedup_key": CUSTOMERS_WON_DEDUP_KEY,
        "label": CUSTOMERS_WON_LABEL,
        "definition": CUSTOMERS_WON_DEFINITION,
        "not_the_same_as": CUSTOMERS_WON_NOT_THE_SAME_AS,
        "source": "hubspot_deal_ledger",
        "available": bool(base.get("available")),
        "window": {**window, "window_type": "business",
                   "start": base.get("window_start"),
                   "end_exclusive": base.get("window_end"),
                   "label": base.get("window_label")},
        "as_of": base.get("as_of"),
        "cohort": base.get("cohort"),
        "customers": customers,
        "closed_won_deals": deals,
        "legacy_fallback_used": False,
        canonical_contract.METRIC_TRUTH_KEY: canonical_contract.metric_truth_block(
            window, [entry("customers", customers),
                     entry("closed_won_deals", deals)]),
    }


def _customers_won(base: dict, customers: dict, resolved: dict) -> dict:
    """Section 6. A list only beside a PUBLISHED customer count (``customers``:
    the Customers card's verdict) and only when it numbers exactly that count;
    absent otherwise, with the reason. Beside it, always, the block's own
    reading of the reader contract (``_customers_won_reading``).

    The lead filters do not touch it: it is the window's all-source won-deal
    population, the same one the Customers card counts, whatever the funnel
    is narrowed to (PR-ADS-178B-F1 m1)."""
    reading = _customers_won_reading(base, resolved)
    if customers["status"] != cwt.PUBLISHED:
        return {"status": customers["status"], "reason": customers["reason"], **reading}
    rows, omitted = _won_customers_list(base, customers)
    if rows is None or len(rows) != customers["value"]:
        return {"status": cwt.WITHHELD, "reason": omitted or REASON_ROWS_DO_NOT_MATCH,
                **reading}
    return {"status": cwt.PUBLISHED, "reason": None, "rows": rows, **reading}


def _cohort_selection(scope: str, acquisition_group) -> tuple:
    """``(source_group | None, unavailable_reason | None)``: which part of the
    acquisition cohort the lead filters select (PR-ADS-182A).

    The cohort is partitioned by the contact's source group with the Source
    filter's own rule (``by_source_group``), and Scope Google Ads-source IS
    that group (``_contact_scopes``: the acquisition group is Google Ads). The
    campaign- and keyword-attributable scopes are not on the cohort basis:
    unavailable with the reason, never the wider cohort under their name."""
    if scope in (crm_funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE, crm_funnel.SCOPE_KEYWORD_ATTRIBUTABLE):
        return None, REASON_SCOPE_NOT_ON_COHORT_BASIS
    if scope == crm_funnel.SCOPE_GOOGLE_ADS_SOURCE:
        if acquisition_group not in (None, GROUP_GOOGLE_ADS):
            return None, REASON_FILTERS_SELECT_NO_CONTACT
        return GROUP_GOOGLE_ADS, None
    return acquisition_group, None


def _funnel(cohort: dict, scope: str, acquisition_group) -> dict:
    """The lifecycle funnel: the acquisition cohort (PR-ADS-182A, docs/67).

    The mart deal view's ``cohort`` block — the SAME build the SQL card reads,
    in the same request — through the one block producer the Overview uses.
    No database read here and no verdict of its own: unpublished means no
    count, a published slot that is missing is unavailable, never 0."""
    from services.dashboard_overview_service import (  # noqa: PLC0415
        acquisition_funnel_block,
    )
    source_group, reason = _cohort_selection(scope, acquisition_group)
    block = acquisition_funnel_block(cohort, source_group=source_group,
                                     unavailable_reason=reason)
    unfiltered = scope == crm_funnel.SCOPE_ALL_SOURCE and acquisition_group is None
    return {
        **block,
        "label": FUNNEL_LABEL,
        "description": FUNNEL_DESCRIPTION,
        "lead_filters": {
            "scope": scope,
            "scope_label": crm_funnel.SCOPE_LABELS[scope],
            "acquisition_group": acquisition_group,
            "acquisition_group_label": (crm_funnel.GROUP_LABELS[acquisition_group]
                                        if acquisition_group else "All sources"),
            "source_group": source_group,
            "honoured": reason is None,
            "unavailable_reason": reason,
        },
        # The metric-truth scope: only the unfiltered funnel is the Overview's.
        "truth_scope": (FUNNEL_TRUTH_SCOPE_ALL_SOURCE if unfiltered
                        else f"lead_filtered_acquisition_cohort:{scope}:{acquisition_group or 'all'}"),
    }


def _metric_truth(window_block: dict, kpis: dict, summary: dict, cohort: dict,
                  funnel: dict) -> dict:
    """Per-metric provenance in the shape the cross-page parity audit checks.
    Every figure is the mart deal view's, so the all-source closed-won triple
    is the SAME identity the Overview, Revenue and Channels pages publish."""
    resolved = {k: window_block.get(k) for k in ("key", "start_date", "end_date", "timezone")}

    def ready(m):
        return (canonical_contract.TRUTH_READY if m["status"] == cwt.PUBLISHED
                else canonical_contract.TRUTH_NOT_READY)
    return canonical_contract.metric_truth_block(resolved, [
        # The mart's own revenue contract: its reason and violation codes
        # (revenue_total_*), so this consumer names the refusal the mart does.
        {"metric": "closed_won_revenue_usd",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "all_source_business_revenue",
         "truth_status": ready(kpis["won_revenue_usd"]),
         "unavailable_reason": (None if kpis["won_revenue_usd"]["status"] == cwt.PUBLISHED
                                else summary.get("revenue_total_unavailable_reason")
                                or kpis["won_revenue_usd"]["reason"]),
         "violation_codes": summary.get("revenue_total_violation_codes")},
        {"metric": "closed_won_deals",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "all_source_business_revenue",
         "truth_status": ready(kpis["closed_won_deals"]),
         "unavailable_reason": kpis["closed_won_deals"]["reason"]},
        {"metric": "customers",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "all_source_business_revenue",
         "truth_status": ready(kpis["customers"]),
         "unavailable_reason": (None if kpis["customers"]["status"] == cwt.PUBLISHED
                                else summary.get("customers_reason")
                                or kpis["customers"]["reason"])},
        {"metric": "google_ads_cohort_sqls",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "google_ads_cohort_sqls",
         "truth_status": ready(kpis["sqls"]),
         "unavailable_reason": (None if kpis["sqls"]["status"] == cwt.PUBLISHED
                                else cohort.get("sql_reason"))},
        # PR-ADS-182A: the funnel — the acquisition cohort under its one
        # verdict; unfiltered, the Overview funnel's scope (parity-audited).
        {"metric": "lifecycle_acquisition_funnel",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": funnel["truth_scope"],
         "truth_status": (canonical_contract.TRUTH_READY
                          if funnel["publication"]["status"] == cwt.PUBLISHED
                          else canonical_contract.TRUTH_NOT_READY),
         "unavailable_reason": (None if funnel["publication"]["status"] == cwt.PUBLISHED
                                else funnel["publication"]["reason"])},
    ])


@canonical_revenue.single_snapshot
def build_pipeline(window: str = "current_quarter", now: datetime | None = None, *,
                   scope: str = crm_funnel.SCOPE_ALL_SOURCE,
                   acquisition_group: str | None = None) -> dict:
    """The Pipeline contract for one business window (``docs/61`` §4).

    ``scope`` / ``acquisition_group`` are lead filters: they narrow the funnel
    only. Every deal figure is the window's, whatever they are.

    Raises:
        ValueError: an unsupported window, scope or acquisition group.
    """
    resolved = resolve_canonical_window(window, now=now)
    if scope not in crm_funnel.ORDERED_SCOPES:
        raise ValueError(f"Unknown scope '{scope}'")
    acquisition_group = acquisition_group or None
    if acquisition_group is not None and acquisition_group not in crm_funnel.ACQUISITION_GROUPS:
        raise ValueError(f"Unknown acquisition group '{acquisition_group}'")
    lead_filtered = scope != crm_funnel.SCOPE_ALL_SOURCE or acquisition_group is not None

    mart = build_revenue_decision_mart(view="deal", window=window, now=now)
    window_block = mart.get("window") or {}
    summary = mart.get("summary") or {}
    cohort = mart.get("cohort") or {}
    ledger = mart.get("ledger_summary") or {}
    summary_metrics = summary.get("metrics") or {}

    # The mart deal view's summary: the window's canonical verdicts (the
    # reader contract's canonical_metrics) — the same identity Overview,
    # Revenue and Channels publish, checked by the parity audit.
    deals_m = _verdict(summary_metrics.get("closed_won_deals"))
    customers_m = _verdict(summary_metrics.get("customers"))
    revenue_m = _verdict(summary_metrics.get("revenue_usd"))
    kpis = {
        # The acquisition cohort, with its one verdict (docs/59).
        "sqls": _verdict(summary_metrics.get("sqls")),
        "closed_won_deals": deals_m,
        "customers": {**customers_m, "dedup_key": "hubspot_company_id",
                      "additive": False},
        "won_revenue_usd": revenue_m,
        "average_deal_value_usd": _average_deal_value(ledger, deals_m, revenue_m),
    }

    population = _verdict(mart.get("closed_won_population"))
    deal_rows, deals_status, deals_reason = _deal_list(mart.get("rows") or [], deals_m,
                                                       population)

    # The same snapshot the mart's deal view read (one_snapshot is shared).
    base = canonical_revenue.load_won_deals(window, now=now)
    # PR-ADS-182A: the funnel is the cohort block the SQL card reads.
    funnel = _funnel(cohort, scope, acquisition_group)

    return {
        "page": PAGE,
        "window": window_block,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        "source_truth": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "google_ads_conversion_value_used": False,
        # No legacy closed-won table is read, ever (PR-ADS-153E-B).
        "legacy_fallback_used": False,
        "as_of": ledger.get("as_of") or (base or {}).get("as_of"),
        "filters": {
            "scope": scope,
            "scope_label": crm_funnel.SCOPE_LABELS[scope],
            "acquisition_group": acquisition_group,
            "acquisition_group_label": (crm_funnel.GROUP_LABELS[acquisition_group]
                                        if acquisition_group else "All sources"),
            "lead_filtered": lead_filtered,
            "applies_to": list(LEAD_FILTER_APPLIES_TO),
        },
        # The acquisition cohort behind kpis.sqls — a page gates its SQL
        # figure on THIS block of THIS response (cohortCountHtml).
        "cohort": cohort,
        "metric_family": cohort.get("metric_family"),
        "window_basis": cohort.get("window_basis"),
        "kpis": kpis,
        # The deal list's own verdicts, beside it: a list is shown under a
        # published count only (the reader-cutover audit's sibling rule).
        "metrics": {"closed_won_deals": deals_m, "customers": kpis["customers"],
                    "revenue_usd": revenue_m},
        "closed_won_population": population,
        "funnel": funnel,
        "deals": deal_rows,
        "deals_status": deals_status,
        "deals_reason": deals_reason,
        "source_breakdown": _source_breakdown(base, deal_rows),
        "customers_won": _customers_won(base or {}, kpis["customers"], resolved),
        "truth_footer": TRUTH_FOOTER,
        **_rates(),
        canonical_contract.METRIC_TRUTH_KEY: _metric_truth(window_block, kpis, summary,
                                                           cohort, funnel),
    }
