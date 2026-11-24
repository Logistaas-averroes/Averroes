"""
services/cross_page_parity_service.py

PR-ADS-154C — prove that every production page computes the same metric the same
way, and name the metrics that are *supposed* to differ.

The question this answers
------------------------
Not "do the pages roughly agree", which tolerance-based checks answer and which
is how disagreements survive. It answers: for a given metric identity, window,
customer, currency and attribution scope, does every consumer publish the
IDENTICAL value — and does each of them say, in its own payload, which canonical
source produced it?

Two pages disagreeing is only one of the failure modes. The others are worse
because they look like agreement:

  * two pages asking about different date ranges under the same window name
    (PR-ADS-154C's window-anchor defect — see ``canonical_contract``);
  * a page quietly falling back to a legacy provider and publishing the result
    under a canonical label;
  * two genuinely different metrics — total business revenue and Google
    Ads-attributed revenue — compared as though they should match, so the real
    difference is filed as a bug and the real bug is hidden inside it.

So this module compares only within a METRIC IDENTITY, and carries an explicit
register of the pairs that must never be compared at all.

Distinct by design
------------------
These are different questions, and a difference between them is information, not
a defect:

  * total business revenue           — every closed-won deal, any source
  * Google Ads-attributed revenue    — the subset attributable to Google Ads
  * country-attributed revenue       — the subset assigned to a real country;
                                       the residual is published separately
  * campaign spend                   — the canonical ROAS denominator
  * country-attributed spend         — the part geographic_view assigns to a
                                       country; the residual is the rest

Read-only throughout. No external platform is contacted; no table is written.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from analysis.business_windows import WINDOW_KEYS
from analysis import closed_won_truth as cwt  # PR-ADS-161E
from services import canonical_revenue_service as canonical_revenue
from services.canonical_contract import (
    METRIC_TRUTH_KEY,
    SOURCE_CANONICAL_DEAL_LEDGER,
    SOURCE_CANONICAL_FUNNEL,
    SOURCE_CANONICAL_GEO,
    SOURCE_CANONICAL_SPEND,
    SOURCE_REVENUE_BY_SOURCE,
    SOURCE_REVENUE_DECISION_MART,
    THROUGH_TODAY_NOT_SETTLED,
    TRUTH_READY,
    resolve_canonical_window,
    resolve_settled_window,
    settled_revenue_bounds,
)

log = logging.getLogger(__name__)

# ── Violation codes ──────────────────────────────────────────────────────────
V_VALUE_MISMATCH = "consumer_values_differ"
V_WINDOW_MISMATCH = "consumer_windows_differ"
V_FALLBACK_USED = "legacy_fallback_used"
V_LEGACY_READ = "legacy_source_supplied_production_total"
V_SOURCE_UNAVAILABLE = "canonical_source_unavailable"
V_CONSUMER_FAILED = "consumer_raised"
V_UNCLASSIFIED_DIFFERENCE = "difference_not_classified"
#: Every consumer published the same figure, but the coverage behind it was never
#: proven — so the agreement is not evidence. See `_coverage_proven`.
V_AGREEMENT_ON_UNPROVEN_COVERAGE = "agreement_on_unproven_coverage"
#: PR-ADS-154C-F1.
V_CONSUMER_METRIC_MISSING = "consumer_metric_missing"
V_WINDOW_MISSING = "consumer_window_missing"
V_CONTRACT_INVALID = "metric_contract_invalid"
V_CONTRACT_INCONSISTENT = "metric_contract_inconsistent"
#: PR-ADS-154C-F2. A registered consumer that was built, published a window, and
#: certified no metric identity. Building is not certification.
V_CONSUMER_UNCERTIFIED = "consumer_certified_nothing"
#: A consumer named in the registry that the audit never built at all.
V_CONSUMER_NOT_BUILT = "registered_consumer_not_built"
#: PR-ADS-154C-F3. A consumer published a NUMBER while its own contract says the
#: canonical source behind it is unavailable or not ready. Deliberately not a
#: value mismatch: those two consumers may agree perfectly and still both be
#: publishing a figure neither is entitled to.
V_VALUE_OVER_UNAVAILABLE_SOURCE = "value_published_while_source_unavailable"
#: Split identities that must add back up did not.
V_CONSERVATION_BROKEN = "metric_conservation_broken"
#: PR-ADS-177A: one SQL count. The Countries page, Dashboard Countries and
#: Campaign Evidence publish the window's Google Ads acquisition-cohort SQLs
#: over the same days; any two published values that differ is this.
V_COHORT_SQL_PARITY_BROKEN = "cohort_sql_parity_broken"

# ── PR-ADS-155 §7 — four reasons a metric is absent, told apart ──────────────
# `canonical_source_unavailable` used to cover all of them. It is kept as the
# code for "we could not tell", so an unclassifiable absence is still reported
# and never silently downgraded; the three specific codes below replace it
# wherever the consumers' own contracts say which situation it actually is.
#
# The distinction is operational, not cosmetic. A database outage is fixed by an
# engineer; an unproven coverage range is fixed by a backfill; an unpriced deal
# is fixed by whoever owns the deal, in HubSpot, in about a minute. Reporting all
# three as one code sends every one of them to the wrong person.
#: The canonical store could not be read at all.
V_DB_UNREADABLE = "canonical_database_unreadable"
#: The store is readable, but the population for this window is not releasable.
V_POPULATION_UNAVAILABLE = "revenue_population_unavailable"
#: The population IS available and complete; its TOTAL is unknown because deals
#: inside it carry no amount. The most actionable of the four.
V_TOTAL_UNPUBLISHABLE = "revenue_total_unpublishable_missing_amount"
#: The lifecycle funnel is readable but stage-entry evidence is incomplete.
V_LIFECYCLE_PARTIAL = "lifecycle_coverage_partial"
#: PR-ADS-161E. The deal population is available; the CUSTOMER total is unknown
#: because a won deal in it has no company, or several — a CRM record to fix,
#: named by the CRM Truth Exceptions workbench.
V_CUSTOMER_IDENTITY_UNRESOLVED = "customer_total_unpublishable_identity_unresolved"
#: PR-ADS-161E. The company-association evidence is missing, stale, or its full
#: scan is not current — the association sync must run, no CRM record is wrong.
V_CUSTOMER_ASSOCIATION_COVERAGE = "customer_association_coverage_unproven"

#: Canonical unavailable-reasons → the class of problem they represent. The keys
#: are `canonical_revenue_service`'s own constants, so a reason renamed there
#: cannot silently stop matching here.
_UNAVAILABLE_CLASSES = {
    canonical_revenue.REASON_LEDGER_UNREADABLE:
        ("database_unreadable", V_DB_UNREADABLE,
         "the canonical deal ledger could not be read"),
    canonical_revenue.REASON_SYNC_STATE_UNREADABLE:
        ("database_unreadable", V_DB_UNREADABLE,
         "the canonical ledger's sync state could not be read"),
    canonical_revenue.REASON_COVERAGE_NOT_PROVEN:
        ("population_unavailable", V_POPULATION_UNAVAILABLE,
         "the canonical ledger's coverage for this window is not proven, so its "
         "population is not releasable"),
    canonical_revenue.REASON_REVENUE_INCOMPLETE:
        ("total_unpublishable", V_TOTAL_UNPUBLISHABLE,
         "the population IS available and complete, but closed-won deals inside "
         "it have no amount in HubSpot, so the total is unknown. This is not an "
         "outage: it is resolved by pricing those deals at source"),
    # PR-ADS-155-F1: the revenue integration is not wired at all, so no scoped
    # revenue population exists to read. Classifiable, and emphatically not the
    # same as "we could not tell" — an operator connects the integration.
    "canonical_closed_won_population_unavailable":
        ("population_unavailable", V_POPULATION_UNAVAILABLE,
         "the revenue integration is not connected, so no closed-won population "
         "is readable for this scope"),
    # PR-ADS-161E: readers now republish the CANONICAL reason codes
    # (analysis.closed_won_truth). Each is the same class of problem as its
    # legacy twin above — one refusal, whichever vocabulary a page carries.
    cwt.R_REVENUE_UNPROVEN:
        ("total_unpublishable", V_TOTAL_UNPUBLISHABLE,
         "the population IS available, but closed-won deals inside it have no "
         "proven USD amount, so the total is unknown — resolved by pricing "
         "those deals at source"),
    cwt.R_UNDATED_WON_DEALS:
        ("population_unavailable", V_POPULATION_UNAVAILABLE,
         "a won deal has no close date, so this window's population is withheld"),
    cwt.R_WON_DEFINITION_CONFLICT:
        ("population_unavailable", V_POPULATION_UNAVAILABLE,
         "HubSpot's won flag and won stage disagree for a deal in this window, "
         "so its population is withheld"),
    cwt.R_LEDGER_STALE:
        ("population_unavailable", V_POPULATION_UNAVAILABLE,
         "the deal sync has not succeeded within the staleness limit, so this "
         "window's population is not current — restore the incremental sync"),
    cwt.R_DEALS_WITHHELD:
        ("population_unavailable", V_POPULATION_UNAVAILABLE,
         "the closed-won deal count is withheld for this window"),
    # PR-ADS-161E: the canonical customer-identity refusals. Each names who
    # fixes it — a CRM record, the association sync, or an engineer.
    cwt.R_CUSTOMER_IDENTITY:
        ("customer_identity_unresolved", V_CUSTOMER_IDENTITY_UNRESOLVED,
         "a won deal in this window has no HubSpot company, or several, so the "
         "distinct-company total is unknown — resolved in HubSpot; the CRM Truth "
         "Exceptions workbench names the deal"),
    cwt.R_CUSTOMER_IDENTITY_STALE:
        ("customer_association_coverage", V_CUSTOMER_ASSOCIATION_COVERAGE,
         "a won deal's company evidence is stale — the association sync must "
         "re-observe it"),
    cwt.R_CUSTOMER_IDENTITY_UNAVAILABLE:
        ("customer_association_coverage", V_CUSTOMER_ASSOCIATION_COVERAGE,
         "a won deal's company was never successfully observed — the "
         "association sync must observe it"),
    cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT:
        ("customer_association_coverage", V_CUSTOMER_ASSOCIATION_COVERAGE,
         "the latest full company-association scan is not current for the won "
         "set — the association scan must run"),
    cwt.R_CUSTOMER_IDENTITY_NOT_INGESTED:
        ("customer_association_coverage", V_CUSTOMER_ASSOCIATION_COVERAGE,
         "company associations have not been ingested — the association sync "
         "must run"),
    cwt.R_COMPANY_ASSOCIATIONS_UNREADABLE:
        ("database_unreadable", V_DB_UNREADABLE,
         "the company-association tables could not be read"),
}


def _classify_unavailable(readings: list, metric_key: str):
    """Which of the four situations made every consumer withhold this metric.

    Returns ``(status, violation_code, detail)``. Falls back to the original
    catch-all when the consumers' contracts do not say — an absence we cannot
    explain is still a violation, and pretending to know which kind it is would
    be its own fabrication.
    """
    declared = [r for r in readings if r.get("unavailable_reason")]
    classes = {_UNAVAILABLE_CLASSES[r["unavailable_reason"]]
               for r in declared if r["unavailable_reason"] in _UNAVAILABLE_CLASSES}
    reasons = sorted({r["unavailable_reason"] for r in declared})
    # A specific owner is named only when EVERY consumer explains its absence
    # with a registered reason. One silent consumer, or one reason the audit
    # does not know, means nobody can say the others' reason is the whole
    # story — so the absence stays the catch-all, with who said what.
    silent = sorted(str(r.get("consumer")) for r in readings
                    if not r.get("unavailable_reason"))
    unregistered = sorted({r["unavailable_reason"] for r in declared
                           if r["unavailable_reason"] not in _UNAVAILABLE_CLASSES})

    if len(classes) == 1 and not silent and not unregistered:
        status, code, why = classes.pop()
        return status, code, (
            f"no consumer published {metric_key}: {why} "
            f"(declared reason: {', '.join(reasons)})")
    if len(classes) == 1:
        gaps = []
        if silent:
            gaps.append(f"consumer(s) {', '.join(silent)} declared no reason")
        if unregistered:
            gaps.append(f"reason(s) {', '.join(unregistered)} are not ones the "
                        "audit can classify")
        return ("unavailable", V_SOURCE_UNAVAILABLE,
                f"no consumer published {metric_key}; some consumers declare "
                f"{', '.join(sorted(set(reasons) - set(unregistered)))}, but "
                f"{' and '.join(gaps)}, so no single owner can be named")
    if classes:
        # Consumers disagree about WHY. That is itself worth naming: they are
        # reading the same canonical contract and reached different refusals.
        return ("unavailable", V_SOURCE_UNAVAILABLE,
                f"no consumer published {metric_key}, and the consumers declare "
                f"differing reasons: {', '.join(reasons)}")
    if reasons:
        return ("unavailable", V_SOURCE_UNAVAILABLE,
                f"no consumer published {metric_key} (declared reason(s): "
                f"{', '.join(reasons)}, none of which the audit can classify)")
    return ("unavailable", V_SOURCE_UNAVAILABLE,
            f"no consumer published {metric_key}, and none declared a reason")

#: PR-ADS-167A — the settled-day REFERENCE: the decision mart's country view.
#: Owner decision 2026-11-20: the country consumers (Country Revenue & Spend,
#: Dashboard Countries) end a window that includes today at YESTERDAY. They
#: cannot be compared with consumers whose window ends today, and must not be
#: silently. This reference is the same canonical contract over the settled
#: window, so the country consumers' coverage proof and the SQL conservation
#: rule are evaluated over the SAME days they measured. It certifies no
#: identity of its own (it is not in METRIC_IDENTITIES).
CONSUMER_SETTLED_REFERENCE = "revenue_decision_mart/country"

#: Identities that partition a whole. Registering the split is not enough — if
#: the parts do not add back to the total, one of the three is wrong and the
#: audit cannot tell which, so it says so rather than certifying all three.
#:
#: PR-ADS-154C-F3: Countries assigns SQLs to real countries and holds the rest in
#: an explicit residual. 14 + 2 = 16 in the production current quarter.
CONSERVATION_RULES = [
    {
        # PR-ADS-177A: the total is the Google Ads acquisition cohort.
        "total": "google_ads_cohort_sqls",
        "parts": ["country_attributed_sqls", "country_unattributed_residual_sqls"],
        # PR-ADS-167A: the parts come from Dashboard Countries. When it declares
        # a settled-day window (it ends at yesterday), the total is read from
        # the settled reference over the SAME days — never from consumers whose
        # window includes today, which would break conservation by a day of
        # SQLs, or hide a real break behind one.
        "parts_consumer": "dashboard/countries",
        "settled_total": (CONSUMER_SETTLED_REFERENCE, "summary.sqls"),
        "reason": (
            "Every Google Ads acquisition-cohort SQL is either placed on a real "
            "canonical country (by the contact's own HubSpot country) or held in "
            "the governed residual. The residual is never spread across countries "
            "to make the parts match, and a real country total is never inflated "
            "to swallow it."),
    },
]

#: Country reconciliation states that count as governed geo readiness. Both are
#: accepted: `reconciled_with_residual` is the PR-ADS-131 case where the
#: shortfall is explicitly calculated and published rather than hidden.
ACCEPTED_COUNTRY_STATES = frozenset({"verified", "reconciled_with_residual"})

#: Coverage-proof kinds. Which authority has to have proven itself before a
#: figure from it counts as measured rather than merely rendered. Named
#: explicitly per identity: PR-ADS-154C-F1 chose the proof from the metric's
#: canonical source, which cannot separate country SPEND from country REVENUE
#: even though they depend on different things being true (F2 §4).
PROOF_CAMPAIGN_SPEND = "campaign_spend_and_fx_coverage"
PROOF_GEO_SPEND = "geo_coverage_and_country_reconciliation"
PROOF_COUNTRY_REVENUE = "country_reconciliation_and_deal_ledger"
PROOF_DEAL_LEDGER = "canonical_deal_ledger"
PROOF_MART_LEAD_FUNNEL = "mart_lead_population"
PROOF_LIFECYCLE_FUNNEL = "canonical_contact_funnel"
#: PR-ADS-154C-F3-F1 §3. Country SQLs need the mart's lead population proven AND
#: the country assignment that partitions it — the figure has two dependencies
#: and naming only one of them would certify it on half its evidence.
PROOF_COUNTRY_SQLS = "mart_lead_population_and_country_assignment"

#: Metric identities. Each entry is ONE question, and every consumer listed must
#: answer it identically. Consumers are (label, dotted path into the payload).
#:
#: ``canonical_source`` is the authority the identity is defined against.
#: ``consumer_sources`` overrides it for a consumer that legitimately reads a
#: DIFFERENT canonical authority for the same question — Channels renders the
#: all-source totals from the source-group taxonomy, which is the canonical deal
#: ledger grouped by acquisition source. The declaration is still checked exactly;
#: the registry simply knows which authority each page is expected to name, so a
#: page cannot satisfy the audit by claiming an authority it never read.
#:
#: Paths are asserted against real payloads in the test suite, so a renamed key
#: fails a test rather than silently reporting the metric "unavailable" here —
#: an audit that goes quiet when it loses its grip is worse than no audit.
#: PR-ADS-166A — `GET /api/crm-funnel/customers-won`, the Leads page's
#: "Customers won" block.
CONSUMER_LEADS_CUSTOMERS_WON = "leads/customers_won"

METRIC_IDENTITIES = {
    # ── Google Ads spend, FULL account denominator ───────────────────────────
    # Countries' `kpis.verified_spend_usd` is deliberately NOT here. It sums the
    # per-country rows, so it is the country-ATTRIBUTED denominator; it equals the
    # full-account figure only when geographic_view happens to place every penny.
    # Same label, different question — see `country_attributed_spend_usd`.
    "google_ads_spend_usd": {
        "label": "Google Ads spend, full account (USD)",
        "canonical_source": SOURCE_CANONICAL_SPEND,
        "scope": "google_ads_campaign_spend",
        "coverage_proof": PROOF_CAMPAIGN_SPEND,
        "consumers": [
            ("dashboard/overview", "kpis.google_ads_spend_usd"),
            ("dashboard/campaigns", "kpis.verified_spend_usd"),
            ("revenue_decision_mart", "summary.spend_usd"),
        ],
    },
    "country_attributed_spend_usd": {
        "label": "Google Ads spend attributed to a country (USD)",
        "canonical_source": SOURCE_CANONICAL_GEO,
        "scope": "country_attributed_spend",
        "coverage_proof": PROOF_GEO_SPEND,
        "consumers": [
            ("dashboard/countries", "kpis.verified_spend_usd"),
        ],
    },

    # ── All-source commercial outcomes ───────────────────────────────────────
    # The company-wide totals. Every closed-won deal, whatever brought it in.
    "closed_won_revenue_usd": {
        "label": "Closed-won revenue, ALL sources (USD)",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "all_source_business_revenue",
        "coverage_proof": PROOF_DEAL_LEDGER,
        "consumers": [
            ("dashboard/overview", "kpis.closed_won_revenue_usd"),
            ("dashboard/revenue", "kpis.closed_won_revenue_usd"),
            ("dashboard/channels", "kpis.closed_won_revenue_usd"),
            ("dashboard/deals", "kpis.closed_won_revenue_usd"),
            ("revenue_decision_mart", "summary.won_revenue_usd"),
        ],
        "consumer_sources": {"dashboard/channels": SOURCE_REVENUE_BY_SOURCE},
    },
    # PR-ADS-161E: deals and customers are two identities. A closed-won DEAL is a
    # HubSpot deal; a CUSTOMER is a distinct proven HubSpot company behind them.
    "closed_won_deals": {
        "label": "Closed-won deals, ALL sources",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "all_source_business_revenue",
        "coverage_proof": PROOF_DEAL_LEDGER,
        "consumers": [
            ("dashboard/overview", "kpis.closed_won_deals"),
            ("dashboard/revenue", "kpis.closed_won_deals"),
            ("dashboard/channels", "kpis.total_closed_won_deals"),
            ("dashboard/deals", "kpis.closed_won_deals"),
            ("revenue_decision_mart", "summary.closed_won_deals"),
            # PR-ADS-166A: the Leads page's "Customers won" block reads the
            # reader contract directly; it must show the Deals page's figures.
            (CONSUMER_LEADS_CUSTOMERS_WON, "closed_won_deals.value"),
        ],
        "consumer_sources": {
            "dashboard/channels": SOURCE_REVENUE_BY_SOURCE,
            CONSUMER_LEADS_CUSTOMERS_WON: SOURCE_CANONICAL_DEAL_LEDGER},
    },
    "customers": {
        "label": "Customers (distinct HubSpot companies), ALL sources",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "all_source_business_revenue",
        "coverage_proof": PROOF_DEAL_LEDGER,
        "consumers": [
            ("dashboard/overview", "kpis.customers"),
            ("dashboard/revenue", "kpis.customers"),
            ("dashboard/channels", "kpis.customers"),
            ("dashboard/deals", "kpis.closed_won_customers"),
            ("revenue_decision_mart", "summary.customers"),
            (CONSUMER_LEADS_CUSTOMERS_WON, "customers.value"),
        ],
        "consumer_sources": {
            CONSUMER_LEADS_CUSTOMERS_WON: SOURCE_CANONICAL_DEAL_LEDGER},
    },

    # ── Campaign-ATTRIBUTABLE outcomes — a SUBSET, not the business total ────
    "campaign_attributed_won_revenue_usd": {
        "label": "Closed-won revenue attributable to a campaign (USD)",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "campaign_attributable_revenue",
        "coverage_proof": PROOF_DEAL_LEDGER,
        "consumers": [
            ("dashboard/campaigns", "kpis.won_revenue_usd"),
            ("revenue_decision_mart", "summary.attributed_won_revenue_usd"),
        ],
    },
    "campaign_attributed_closed_won_deals": {
        "label": "Closed-won deals attributable to a campaign",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "campaign_attributable_revenue",
        "coverage_proof": PROOF_DEAL_LEDGER,
        "consumers": [
            ("dashboard/campaigns", "kpis.closed_won_deals"),
            ("revenue_decision_mart", "summary.attributed_closed_won_deals"),
        ],
    },
    "campaign_attributed_customers": {
        "label": "Customers (distinct HubSpot companies) attributable to a campaign",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "campaign_attributable_revenue",
        "coverage_proof": PROOF_DEAL_LEDGER,
        "consumers": [
            ("dashboard/campaigns", "kpis.customers"),
            ("revenue_decision_mart", "summary.attributed_customers"),
        ],
    },

    # ── Country-ATTRIBUTED outcomes — a different subset again ───────────────
    # These need BOTH proofs. Geo readiness says the spend side is placed; it says
    # nothing about whether the closed-won deals behind the revenue were readable.
    "country_attributed_won_revenue_usd": {
        "label": "Closed-won revenue attributed to a country (USD)",
        "canonical_source": SOURCE_CANONICAL_GEO,
        "scope": "country_attributed_revenue",
        "coverage_proof": PROOF_COUNTRY_REVENUE,
        "consumers": [
            ("dashboard/countries", "kpis.won_revenue_usd"),
        ],
    },
    "country_attributed_closed_won_deals": {
        "label": "Closed-won deals attributed to a country",
        "canonical_source": SOURCE_CANONICAL_GEO,
        "scope": "country_attributed_revenue",
        "coverage_proof": PROOF_COUNTRY_REVENUE,
        "consumers": [
            ("dashboard/countries", "kpis.closed_won_deals"),
        ],
    },

    # ── SQL identities. Deliberately separate populations ────────────────────
    # The mart's `summary.sqls` is the CAMPAIGN-ATTRIBUTABLE subset — it says so
    # in its own `sql_reconciliation` block — and every page that renders "SQLs"
    # beside campaign or country spend is rendering that same subset.
    # PR-ADS-177A: ONE SQL count — the window's Google Ads acquisition cohort
    # (contacts created in the window, SQL proven as of the contact-funnel
    # watermark). Every page that renders "SQLs" renders this.
    "google_ads_cohort_sqls": {
        "label": "SQLs · Google Ads contacts acquired in the window (acquisition cohort)",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "google_ads_cohort_sqls",
        "coverage_proof": PROOF_MART_LEAD_FUNNEL,
        # PR-ADS-154C-F3: Dashboard Countries is NOT here. Its `kpis.sqls` sums
        # the REAL-COUNTRY rows only — 14 against the mart's 16 in the production
        # quarter — because the SQLs it could not assign to a country are held in
        # its explicit residual bucket. Registering it as this identity declared a
        # subset to be the whole population and reported the difference as a
        # parity mismatch, which is the "labels look similar" trap.
        "consumers": [
            ("dashboard/overview", "kpis.sqls"),
            ("dashboard/revenue", "kpis.sqls"),
            ("dashboard/campaigns", "kpis.sqls"),
            ("dashboard/deals", "kpis.sqls"),
            ("revenue_decision_mart", "summary.sqls"),
        ],
    },
    # The two halves of the country split. They are separate identities, and
    # together they must account for the whole — see SQL_CONSERVATION below.
    # PR-ADS-154C-F3-F1 §3: these are DECISION-MART figures. The SQLs originate
    # in the canonical HubSpot lead population the mart aggregates, and are then
    # PARTITIONED by country assignment. Declaring canonical geo as their source
    # said they were Google Ads geo-spend facts, which they are not — geo decides
    # which side of the partition an SQL falls on, it does not produce the SQL.
    # Geo coverage remains a prerequisite through `coverage_proof`, where a
    # prerequisite belongs; the data ORIGIN is stated truthfully.
    "country_attributed_sqls": {
        "label": "SQLs attributed to a real country",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "country_attributed_sqls",
        "coverage_proof": PROOF_COUNTRY_SQLS,
        "consumers": [
            ("dashboard/countries", "kpis.sqls"),
        ],
    },
    "country_unattributed_residual_sqls": {
        "label": "SQLs that could not be assigned to a country (governed residual)",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "country_unattributed_residual_sqls",
        "coverage_proof": PROOF_COUNTRY_SQLS,
        "consumers": [
            ("dashboard/countries", "residual.sqls"),
        ],
    },
    # Channels counts SQLs by SOURCE GROUP, which is a different population and
    # genuinely differs from the campaign-attributable subset — 0 against 25 in
    # the reference fixture, and that difference is the answer, not a defect.
    # Registered as its own identity so it is checked, and never compared with
    # the one above.
    "source_group_sqls": {
        "label": "SQLs by source group (channel taxonomy)",
        "canonical_source": SOURCE_REVENUE_BY_SOURCE,
        "scope": "source_group_sqls",
        "coverage_proof": PROOF_MART_LEAD_FUNNEL,
        "consumers": [
            ("dashboard/channels", "kpis.total_sqls"),
        ],
    },
    "google_ads_cohort_leads": {
        "label": "Leads · Google Ads contacts acquired in the window (acquisition cohort)",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "google_ads_cohort_leads",
        "coverage_proof": PROOF_MART_LEAD_FUNNEL,
        "consumers": [
            ("dashboard/overview", "kpis.leads"),
            ("revenue_decision_mart", "summary.leads"),
        ],
    },
}

#: Lifecycle stages from the canonical HubSpot contact funnel — a DIFFERENT
#: authority from the mart's lead population above, reached through each stage's
#: own ``hs_v2_date_entered_*`` timestamp. A contact becomes an MQL on one date
#: and an SQL on another, so these are five questions with five event-date
#: semantics, not one metric sliced five ways, and none of them is the
#: campaign-attributable SQL count or the channel source-group count. The
#: registry keeps all three apart on purpose.
LIFECYCLE_STAGES = ("leads", "mqls", "sqls", "opportunities", "customers")
#: PR-ADS-166A: the fifth stage is a lifecycle stage MOVE, never "customers"
#: (the identity key stays `lifecycle_customers`; only the wording changes).
_LIFECYCLE_STAGE_LABELS = {"customers": "Moved to Customer stage"}
for _stage in LIFECYCLE_STAGES:
    METRIC_IDENTITIES[f"lifecycle_{_stage}"] = {
        "label": (f"{_LIFECYCLE_STAGE_LABELS.get(_stage, 'Lifecycle ' + _stage)}"
                  ", ALL sources (canonical funnel)"),
        "canonical_source": SOURCE_CANONICAL_FUNNEL,
        "scope": f"lifecycle_{_stage}",
        "coverage_proof": PROOF_LIFECYCLE_FUNNEL,
        "consumers": [("dashboard/overview", f"kpis.lifecycle_{_stage}")],
    }


def expected_source(spec: dict, consumer: str) -> str:
    """The canonical authority THIS consumer is expected to name for THIS metric."""
    return (spec.get("consumer_sources") or {}).get(consumer, spec["canonical_source"])


#: Consumers the audit CERTIFIES. Each must contribute at least one substantive
#: metric identity — building successfully and having its window checked is not
#: certification, and counting it as inspected on that basis was the gap
#: PR-ADS-154C-F2 closes: seven consumers were built and four identities were
#: certified, so three pages passed by producing nothing to check.
CERTIFIED_CONSUMERS = frozenset(
    consumer
    for spec in METRIC_IDENTITIES.values()
    for consumer, _path in spec["consumers"]
)

#: Pages that publish overlapping executive-looking totals but are NOT certified
#: canonical sources, and are scheduled for redesign. Reported explicitly by the
#: audit rather than omitted: a page missing from a parity report reads as a page
#: with nothing to answer for.
PENDING_REDESIGN_CONSUMERS = {
    "platform_evidence": {
        "classification": "pending_redesign_non_authoritative",
        "overlapping_metrics": ["spend_usd", "sqls"],
        "services": ["campaign_evidence_service", "keyword_evidence_service"],
        "note": ("Publishes spend and SQL figures at an evidence grain. Not a "
                 "canonical executive total and must not be read as one. Consumer "
                 "migration belongs to the Platform Evidence redesign."),
    },
    "lead_intelligence": {
        "classification": "pending_redesign_non_authoritative",
        "overlapping_metrics": ["leads", "sqls"],
        # PR-ADS-176C: "/api/leads/country-summary" retired with /api/geo.
        "services": ["/api/leads"],
        "note": ("Publishes lead and SQL counts from the pre-canonical lead "
                 "snapshot. Not a canonical executive total. Consumer migration "
                 "belongs to the Lead Intelligence redesign."),
    },
}

#: PR-ADS-168A — pages that publish closed-won figures over EVIDENCE windows
#: (N account days ending today), not business windows. This audit compares
#: consumers over one business window, so it cannot certify them, and it does
#: not pretend to: they are declared here, outside ``METRIC_IDENTITIES``, and
#: reported. Their reader contract is certified per evidence window by the
#: reader cutover audit (``audit_customer_revenue_reader_cutover``: membership
#: against the truth service and an independent restatement, checks 8/9/18 on
#: the page payload).
EVIDENCE_WINDOW_CONSUMERS = {
    "channel_outcomes": {
        "window_type": "evidence",
        "windows": ["30d", "60d", "180d", "all_time"],
        "metrics": ["closed_won_deals", "customers", "revenue_usd",
                    "google_ads_spend_usd"],
        "services": ["services.source_attribution_service.build_channel_outcomes",
                     "/api/channel-funnel (outcomes)"],
        "certified_by": "scripts/audit_customer_revenue_reader_cutover.py "
                        "(evidence-window pass)",
        "note": ("Channels page outcomes: deals closed in an evidence window's "
                 "days ended at yesterday, per HubSpot original source. Not "
                 "comparable with a business-window total; the period's own "
                 "canonical metrics are its totals."),
    },
}

#: Pairs that must NEVER be compared, with the reason. Registering them is what
#: stops a future reader from "fixing" a difference that is the answer.
DISTINCT_BY_DESIGN = [
    {
        "left": "closed_won_revenue_usd",
        "right": "country_attributed_won_revenue_usd",
        "reason": (
            "Total business revenue counts every closed-won deal; country-attributed "
            "revenue counts only the part assigned to a real country. The remainder "
            "is published as an explicit residual, never spread across countries."),
    },
    {
        "left": "google_ads_spend_usd",
        "right": "country_attributed_spend_usd",
        "reason": (
            "Google Ads geographic_view does not assign location-less spend to any "
            "country. The shortfall is the governed residual (PR-ADS-131), which is "
            "why an accepted residual is a truth-ready state rather than a mismatch."),
    },
    {
        "left": "google_ads_cohort_sqls",
        "right": "source_group_sqls",
        "reason": (
            "Cohort SQLs count Google Ads-sourced contacts created in the window "
            "that reached SQL; source-group SQLs count legacy qualified leads by "
            "CRM acquisition source across every channel. Different populations "
            "under one word."),
    },
    {
        "left": "google_ads_cohort_sqls",
        "right": "country_attributed_sqls",
        "reason": (
            "Countries sums the cohort SQLs whose contact's own country is a REAL "
            "listed market; the mart counts every Google Ads cohort SQL. The "
            "difference is the governed residual, published separately — not a "
            "disagreement to reconcile away (PR-ADS-177A conservation)."),
    },
    {
        "left": "country_attributed_sqls",
        "right": "country_unattributed_residual_sqls",
        "reason": (
            "The two halves of the country split. An SQL is in exactly one of "
            "them, and a residual SQL is never assigned to a guessed country to "
            "make the real-country total look complete."),
    },
    {
        "left": "country_attributed_sqls",
        "right": "source_group_sqls",
        "reason": (
            "Country assignment and CRM acquisition source are different axes "
            "over different populations; neither is a rollup of the other."),
    },
    {
        "left": "country_attributed_sqls",
        "right": "lifecycle_sqls",
        "reason": (
            "Lifecycle SQLs are all-source stage-entry events on their own "
            "timestamp. Country-attributed SQLs are the acquisition cohort's "
            "Google Ads contacts whose own country is a listed market."),
    },
    {
        "left": "google_ads_cohort_sqls",
        "right": "lifecycle_sqls",
        "reason": (
            "Lifecycle SQLs are stage-ENTRY events dated by the contact's own "
            "hs_v2_date_entered_salesqualifiedlead timestamp, all sources. The "
            "cohort count is Google Ads contacts CREATED in the window that have "
            "reached SQL by any proof. Same word, different event and date."),
    },
    {
        "left": "lifecycle_customers",
        "right": "customers",
        "reason": (
            "A lifecycle customer is a contact that entered the customer stage; a "
            "revenue customer is a closed-won deal in the canonical ledger. "
            "PR-ADS-153C kept these apart deliberately and neither substitutes for "
            "the other."),
    },
    {
        "left": "google_ads_cohort_leads",
        "right": "lifecycle_leads",
        "reason": (
            "The cohort's leads are Google Ads contacts created in the window; the "
            "lifecycle lead count is a canonical funnel stage-entry event, all "
            "sources. Two lead definitions, both legitimate."),
    },
]


def _dig(payload: dict, path: str):
    """Follow a dotted path; ``None`` when any step is absent."""
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


#: Fields every metric contract must actually CARRY. PR-ADS-154C-F2 §3: F1
#: checked that the declared window matched the published one, which two absent
#: values satisfy just as well as two present ones. Presence is checked first, so
#: a contract cannot pass by omitting the thing it is supposed to state.
#:
#: ``customer_id`` is required as a KEY but may hold ``None`` — some canonical
#: authorities are not account-scoped, and inventing an identity for them would be
#: the opposite of provenance. ``window_start`` is absent only for ``all_time``.
REQUIRED_CONTRACT_FIELDS = (
    "metric", "data_source", "scope", "truth_status", "window",
    "window_end", "timezone", "currency", "fallback_used", "customer_id",
)


def _contract_problem(contract, spec: dict, metric_key: str,
                      consumer_window: tuple | None, consumer: str = "",
                      window: str = "") -> str | None:
    """Check a reading's declared provenance against the registry. None = fine.

    PR-ADS-154C-F1 §2. Before this, the audit printed the ``canonical_source``
    the REGISTRY expected and called that provenance — a claim about itself, not
    about the number. A page could read anything at all and the audit would echo
    the source it wished for.

    Now each response declares, per metric, where its figure came from, and this
    checks the declaration. A missing contract is a failure: silence is not proof
    that the right source was used.
    """
    if not isinstance(contract, dict):
        return f"no {METRIC_TRUTH_KEY}.{metric_key} contract published"

    # Presence FIRST. Every check below compares two values, and two missing
    # values compare equal — so a contract omitting its window would have passed
    # the consistency check by saying nothing at all.
    absent = [f for f in REQUIRED_CONTRACT_FIELDS if f not in contract]
    if window != "all_time" and "window_start" not in contract:
        absent.append("window_start")
    if absent:
        return f"contract omits required field(s): {', '.join(sorted(absent))}"

    # The contract must be about the metric it is filed under. A block keyed
    # `customers` that names itself `attributed_customers` describes a different
    # question, and reading it as provenance for this one is the mistake the
    # per-metric registry exists to prevent.
    if contract.get("metric") != metric_key:
        return (f"contract.metric is {contract.get('metric')!r}, filed under "
                f"{metric_key!r}")

    wanted_source = expected_source(spec, consumer)
    if contract.get("data_source") != wanted_source:
        return (f"data_source is {contract.get('data_source')!r}, "
                f"expected {wanted_source!r}")
    if contract.get("scope") != spec["scope"]:
        return f"scope is {contract.get('scope')!r}, expected {spec['scope']!r}"
    if contract.get("truth_status") != TRUTH_READY:
        return f"truth_status is {contract.get('truth_status')!r}, expected 'ready'"
    if contract.get("fallback_used") is not False:
        return f"fallback_used is {contract.get('fallback_used')!r}, expected False"

    # The window the contract names must be the window that was ASKED for, not
    # merely one the page and its own contract agree on. A page that resolved
    # `ytd` while the audit asked for `current_quarter` is internally consistent
    # and answering a different question.
    if window and contract.get("window") != window:
        return (f"contract.window is {contract.get('window')!r}, expected the "
                f"requested window {window!r}")

    if consumer_window is not None:
        declared = (contract.get("window_start"), contract.get("window_end"),
                    contract.get("timezone"))
        if declared != consumer_window:
            return (f"contract window {declared} does not match the window the "
                    f"consumer published {consumer_window}")
    return None


def _consistent(readings: list, field: str) -> bool:
    """Do all readings' contracts agree on ``field``? Absent values are ignored."""
    seen = {(r.get("contract") or {}).get(field) for r in readings
            if (r.get("contract") or {}).get(field) is not None}
    return len(seen) <= 1


def _coverage_proven(consumers: dict, spec: dict) -> tuple[bool, str]:
    """Was the coverage behind THIS metric actually proven?

    Parity is only evidence when the thing consumers agree about was
    established. On an unproven window the canonical query returns zero rows and
    every consumer renders the same ``0.0`` — perfect agreement about a number
    nobody measured, which is the distinction PR-ADS-153F drew for geo.

    PR-ADS-154C-F1 §5: the proof is chosen per metric. Campaign-spend coverage
    was previously used as the universal answer, which meant a country metric
    could be certified by evidence about campaign spend, and revenue by evidence
    about neither.

    PR-ADS-154C-F2 §4: the proof is now named EXPLICITLY on each identity rather
    than inferred from its canonical source, because one source can back two
    metrics that depend on different things being true. Country spend and country
    revenue both come from the geo authority, but geo coverage says only that the
    spend side is placed — it is silent on whether the closed-won deals behind
    the revenue were readable at all. Marking country revenue ready because geo
    spend is ready is the specific false positive this closes.

      * campaign spend   -> campaign coverage AND FX coverage
      * country spend    -> an accepted country reconciliation (`verified` or
                            `reconciled_with_residual`)
      * country revenue  -> that reconciliation AND canonical deal-ledger proof
      * revenue / customers -> the deal ledger is available
      * mart lead/SQL population -> the mart published a lead count
      * lifecycle stages -> the canonical contact funnel is available

    An unrecognised proof kind is NOT proven. A permissive default here would
    certify any future identity that forgot to name its evidence.
    """
    proof = spec.get("coverage_proof")
    mart = (consumers.get("revenue_decision_mart") or {}).get("payload") or {}
    # PR-ADS-167A: a COUNTRY identity's coverage is proven over the days its
    # consumer measured. When that consumer declares a settled-day window, the
    # proof is the settled reference's (same canonical contract, same days),
    # not the campaign mart's window that includes today.
    if proof in (PROOF_GEO_SPEND, PROOF_COUNTRY_REVENUE, PROOF_COUNTRY_SQLS) \
            and any(_declares_settled_window(
                (consumers.get(c) or {}).get("payload"))
                for c, _p in spec.get("consumers") or []):
        mart = (consumers.get(CONSUMER_SETTLED_REFERENCE) or {}).get("payload") or {}
    spend_truth = mart.get("spend_truth") or {}
    summary = mart.get("summary") or {}

    def _country_ok() -> tuple[bool, str]:
        country = spend_truth.get("country_spend_status")
        if country in ACCEPTED_COUNTRY_STATES:
            return True, ""
        return False, (f"country reconciliation is {country!r}, which is not one "
                       f"of {sorted(ACCEPTED_COUNTRY_STATES)}")

    def _ledger_ok() -> tuple[bool, str]:
        if summary.get("revenue_available") is True:
            return True, ""
        return False, (f"canonical revenue availability is "
                       f"{summary.get('revenue_available')!r}")

    if proof == PROOF_CAMPAIGN_SPEND:
        campaign = spend_truth.get("campaign_spend_status")
        fx = spend_truth.get("fx_status")
        if campaign == "verified" and fx == "verified":
            return True, ""
        return False, (f"campaign spend coverage is {campaign!r} and FX coverage "
                       f"is {fx!r}")

    if proof == PROOF_GEO_SPEND:
        return _country_ok()

    if proof == PROOF_COUNTRY_REVENUE:
        ok, detail = _country_ok()
        if not ok:
            return False, detail
        ok, detail = _ledger_ok()
        if not ok:
            return False, (f"geo coverage is ready but the deal proof is not: "
                           f"{detail}")
        return True, ""

    if proof == PROOF_DEAL_LEDGER:
        return _ledger_ok()

    def _mart_lead_population_proven() -> tuple[bool, str]:
        # "It published a number" is not proof — an empty contacts table publishes
        # 0 on every page, which is unanimity about a population nobody measured.
        # `lead_metrics_status` distinguishes the three cases the mart already
        # knows apart: `db` (rows were read), `db_empty` (the query returned
        # nothing at all) and `withheld` (the business event date was unsafe).
        #
        # There is no contacts coverage ledger, so `db_empty` cannot be told from
        # "HubSpot was never synced" — and a quarter that genuinely closed no
        # leads is therefore reported as unproven rather than certified. Saying so
        # is the honest answer; certifying it would be the PR-ADS-153F fabricated
        # zero under a different table.
        readiness = mart.get("readiness") or {}
        status = readiness.get("lead_metrics_status")
        if readiness.get("lead_metrics_ready") is True and status == "db":
            return True, ""
        return False, (f"the mart's lead population is {status!r} "
                       f"(lead_metrics_ready={readiness.get('lead_metrics_ready')!r}), "
                       "so no lead or SQL count over this window was measured")

    if proof == PROOF_MART_LEAD_FUNNEL:
        return _mart_lead_population_proven()

    if proof == PROOF_COUNTRY_SQLS:
        ok, detail = _mart_lead_population_proven()
        if not ok:
            return False, detail
        return _country_ok()

    if proof == PROOF_LIFECYCLE_FUNNEL:
        # Same distinction one authority over. The funnel service reports
        # `available: true` against an empty schema — every stage 0, reconciled
        # against nothing — while its own `sync` block says the bootstrap never
        # ran. The sync block is the one that knows.
        overview = (consumers.get("dashboard/overview") or {}).get("payload") or {}
        funnel = overview.get("lifecycle_funnel") or {}
        sync = funnel.get("sync") or {}
        if (overview.get("kpis") or {}).get("lifecycle_available") is not True:
            return False, "the canonical HubSpot contact funnel is unavailable"
        if sync.get("available") is not True:
            return False, (f"the contact funnel reports available, but its sync is "
                           f"{sync.get('bootstrap_status')!r} and has never run — "
                           "stage counts over an unsynced range are not measurements")
        return True, ""

    return False, f"no recognised coverage proof for this identity ({proof!r})"


def _declares_settled_window(payload) -> bool:
    """Does this consumer say its window ends at yesterday (PR-ADS-167A)?"""
    w = (payload or {}).get("window") if isinstance(payload, dict) else None
    return isinstance(w, dict) and w.get("through_reason") == THROUGH_TODAY_NOT_SETTLED


def _window_signature(payload: dict, window: str) -> tuple:
    """The (start, end, timezone) a consumer used, plus a problem description.

    Returns ``(signature, None)`` when the window is complete, or
    ``(None, reason)`` when it is not. A consumer that publishes no window has
    not agreed with anyone — it has declined to say what it measured — so the
    caller records a violation rather than dropping it from the comparison.

    ``start_date`` may be ``None`` only for ``all_time``, whose lower bound is
    genuinely open.
    """
    w = payload.get("window")
    if not isinstance(w, dict):
        return None, "no window block"
    if not w.get("key"):
        return None, "window block has no key"
    if not w.get("end_date"):
        return None, "window block has no end_date"
    if w.get("start_date") is None and window != "all_time":
        return None, f"window block has no start_date (window={window})"
    if not w.get("timezone"):
        return None, "window block has no effective timezone"
    return (w.get("start_date"), w.get("end_date"), w.get("timezone")), None


def _build_consumers(window: str, now: datetime | None) -> dict:
    """Build every production consumer once, capturing failures rather than raising.

    A consumer that raises is a finding, not a reason to abandon the audit: the
    other consumers still have something to say, and a silent abort would report
    fewer violations than exist.
    """
    from services.dashboard_overview_service import build_dashboard_overview  # noqa: PLC0415
    from services.dashboard_revenue_service import build_dashboard_revenue  # noqa: PLC0415
    from services.dashboard_countries_service import build_dashboard_countries  # noqa: PLC0415
    from services.dashboard_campaigns_service import build_dashboard_campaigns  # noqa: PLC0415
    from services.dashboard_channels_service import build_dashboard_channels  # noqa: PLC0415
    from services.dashboard_deals_service import build_dashboard_deals  # noqa: PLC0415
    from services.revenue_decision_mart import build_revenue_decision_mart  # noqa: PLC0415

    from services.canonical_crm_funnel_service import (  # noqa: PLC0415
        build_customers_won,
    )

    builders = {
        CONSUMER_LEADS_CUSTOMERS_WON: lambda: build_customers_won(
            "business", window, now=now),
        "dashboard/overview": lambda: build_dashboard_overview(window=window, now=now),
        "dashboard/revenue": lambda: build_dashboard_revenue(window=window, now=now),
        "dashboard/channels": lambda: build_dashboard_channels(window=window, now=now),
        "dashboard/campaigns": lambda: build_dashboard_campaigns(window=window, now=now),
        "dashboard/countries": lambda: build_dashboard_countries(window=window, now=now),
        "dashboard/deals": lambda: build_dashboard_deals(window=window, now=now),
        "revenue_decision_mart": lambda: build_revenue_decision_mart(
            window=window, view="campaign", now=now),
        CONSUMER_SETTLED_REFERENCE: lambda: build_revenue_decision_mart(
            window=window, view="country", now=now),
    }

    built: dict = {}
    for name, fn in builders.items():
        try:
            built[name] = {"payload": fn(), "error": None}
        except Exception as exc:  # noqa: BLE001
            built[name] = {"payload": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return built


def audit_window(window: str, now: datetime | None = None) -> dict:
    """Audit cross-page canonical parity for ONE business window."""
    resolved = resolve_canonical_window(window, now=now)
    consumers = _build_consumers(window, now)
    violations: list = []

    # ── Consumers that could not be built at all ─────────────────────────────
    for name, entry in consumers.items():
        if entry["error"]:
            violations.append({"code": V_CONSUMER_FAILED, "consumer": name,
                               "detail": entry["error"]})

    # ── Window parity: every built consumer must publish a COMPLETE window ───
    # PR-ADS-154C-F1: a consumer that omits its window used to contribute no
    # signature at all, so a single remaining signature read as unanimity. One
    # page silently dropping its window looked exactly like every page agreeing.
    # PR-ADS-167A: a consumer that DECLARES a settled-day window (it ends at
    # yesterday because today is not settled) is not compared with the others —
    # that one-day difference is the owner's decision, and comparing it would
    # report a deliberate difference as a defect. It is compared, instead, with
    # the settled window the audit resolves itself from the same key and
    # instant: a declaration the audit cannot reproduce (wrong days, or a
    # closed window claiming to be cut) is a window mismatch.
    settled = resolve_settled_window(window, now=now)
    window_rows = []
    signatures: dict = {}
    for name, entry in consumers.items():
        payload = entry["payload"]
        if payload is None:
            continue                       # already reported as V_CONSUMER_FAILED
        sig, problem = _window_signature(payload, window)
        is_settled = _declares_settled_window(payload)
        window_rows.append({
            "consumer": name,
            "window_start": sig[0] if sig else None,
            "window_end": sig[1] if sig else None,
            "timezone": sig[2] if sig else None,
            "through_reason": (THROUGH_TODAY_NOT_SETTLED if is_settled else None),
            "problem": problem,
        })
        if not sig:
            violations.append({"code": V_WINDOW_MISSING, "consumer": name,
                               "detail": problem})
        elif is_settled:
            expected = (settled.get("start_date"), settled.get("end_date"))
            # Dates are not enough: a consumer declaring the right days but
            # reading deals on the UTC calendar would pass (review round 1's
            # MAJOR stayed hidden that way). Its published instants must be
            # THE settled revenue bounds too.
            want = settled_revenue_bounds(settled) if settled.get("through_reason") else None
            want_utc = (None if want is None else
                        (want[0].isoformat() if want[0] else None, want[1].isoformat()))
            w = payload.get("window") or {}
            got_utc = (w.get("start_utc"), w.get("end_utc_exclusive"))
            if want_utc is not None and got_utc != want_utc:
                violations.append({
                    "code": V_WINDOW_MISMATCH, "metric": None, "consumer": name,
                    "detail": (f"{name} declares a settled-day window whose "
                               f"instants {list(got_utc)} are not the settled "
                               f"revenue bounds {list(want_utc)} for {window!r}")})
            if not settled.get("through_reason") or sig[:2] != expected:
                violations.append({
                    "code": V_WINDOW_MISMATCH, "metric": None, "consumer": name,
                    "detail": (f"{name} declares a settled-day window "
                               f"{list(sig[:2])}, but the settled window for "
                               f"{window!r} is {list(expected)} "
                               f"(through_reason="
                               f"{settled.get('through_reason')!r})")})
        else:
            signatures.setdefault(sig, []).append(name)
    if len(signatures) > 1:
        violations.append({
            "code": V_WINDOW_MISMATCH, "metric": None,
            "detail": "consumers resolved the same window key to different ranges",
            "ranges": [{"range": list(sig), "consumers": names}
                       for sig, names in signatures.items()],
        })

    # ── Fallback usage ───────────────────────────────────────────────────────
    # PR-ADS-154C-F1: the real dashboards publish `legacy_fallback_used` as a
    # TOP-LEVEL boolean and `source_truth` as a STRING. The previous check looked
    # only inside nested dicts of those names, so `isinstance(block, dict)` was
    # False for every production payload and the detection was completely inert —
    # a guard that could not fire on the shape it was written for.
    for name, entry in consumers.items():
        payload = entry["payload"] or {}
        for flag, code in (("fallback_used", V_FALLBACK_USED),
                           ("legacy_fallback_used", V_LEGACY_READ)):
            if payload.get(flag) is True:
                violations.append({"code": code, "consumer": name,
                                   "detail": f"top-level {flag} is true"})
            for block_name in ("truth_contract", "disclosure", "source_truth",
                               "country_truth", "spend_truth"):
                block = payload.get(block_name)
                if isinstance(block, dict) and block.get(flag) is True:
                    violations.append({
                        "code": code, "consumer": name,
                        "detail": f"{block_name}.{flag} is true"})

    # ── Value parity, within each metric identity ────────────────────────────
    # Agreement is only evidence when the coverage behind it was proven. See
    # `_coverage_proven`: an unproven window makes every consumer render the same
    # zero, which is unanimity about a number nobody measured.
    metrics = []
    for metric_key, spec in METRIC_IDENTITIES.items():
        readings = []
        for consumer_name, path in spec["consumers"]:
            entry = consumers.get(consumer_name) or {}
            payload = entry.get("payload")
            value = _dig(payload, path) if payload else None
            contract = ((payload or {}).get(METRIC_TRUTH_KEY) or {}).get(metric_key)
            problem = _contract_problem(contract, spec, metric_key,
                                        _window_signature(payload or {}, window)[0],
                                        consumer=consumer_name, window=window)
            # PR-ADS-154C-F3 §5: an "unavailable" reading must say WHY, from the
            # consumer's own declaration — source, status, reason, violation
            # codes — and whether it reached for a fallback. A reading that only
            # says `None` sends the reader back to the database to find out what
            # the page already knew.
            c = contract if isinstance(contract, dict) else {}
            readings.append({
                "consumer": consumer_name, "path": path, "value": value,
                "expected_source": expected_source(spec, consumer_name),
                "declared_source": c.get("data_source"),
                "truth_status": c.get("truth_status"),
                "unavailable_reason": c.get("unavailable_reason"),
                "violation_codes": c.get("violation_codes") or [],
                "fallback_used": c.get("fallback_used"),
                "legacy_fallback_used": (payload or {}).get("legacy_fallback_used"),
                "contract": contract, "contract_problem": problem})

        present = [r for r in readings if r["value"] is not None]
        missing = [r for r in readings if r["value"] is None]
        bad_contract = [r for r in readings if r["contract_problem"]]
        distinct = {_norm(r["value"]) for r in present}
        baseline = present[0]["value"] if present else None

        for r in readings:
            r["difference"], r["difference_pct"] = _diff(r["value"], baseline)

        # PR-ADS-154C-F1 §5: coverage proof is chosen per METRIC, not one
        # campaign-spend answer applied to everything. Geo metrics need geo
        # coverage and an accepted country reconciliation; revenue needs the deal
        # ledger; funnel metrics need the contact funnel.
        coverage_proven, coverage_detail = _coverage_proven(consumers, spec)

        # PR-ADS-154C-F3. A consumer that publishes a NUMBER while its own
        # contract says the canonical source behind it is not ready. This is not
        # a value mismatch — two such consumers can agree perfectly and both be
        # publishing a figure neither is entitled to, which is exactly how
        # $878,324.80 appeared on three pages, agreeing with itself, over a
        # population whose total was unknown. It gets its own code so it can
        # never be read as "the pages disagree".
        # An ABSENT `truth_status` is a different, more precise finding — the
        # contract omitted a required field — so it stays with the presence check
        # rather than being reported as a deliberate publish over a known-bad
        # source.
        published_over_unavailable = [
            r for r in present
            if isinstance(r.get("contract"), dict)
            and r["contract"].get("truth_status") is not None
            and r["contract"].get("truth_status") != TRUTH_READY
        ]

        if published_over_unavailable:
            status = "published_over_unavailable_source"
            violations.append({
                "code": V_VALUE_OVER_UNAVAILABLE_SOURCE, "metric": metric_key,
                "detail": "; ".join(
                    f"{r['consumer']} published {r['value']!r} while its contract "
                    f"declares truth_status="
                    f"{(r['contract'] or {}).get('truth_status')!r}"
                    + (f" ({(r['contract'] or {}).get('unavailable_reason')})"
                       if (r['contract'] or {}).get('unavailable_reason') else "")
                    for r in published_over_unavailable),
                "readings": [{
                    "consumer": r["consumer"], "path": r["path"], "value": r["value"],
                    "truth_status": (r["contract"] or {}).get("truth_status"),
                    "unavailable_reason": (r["contract"] or {}).get("unavailable_reason"),
                    "canonical_source": (r["contract"] or {}).get("data_source"),
                } for r in published_over_unavailable]})
        elif not present:
            # PR-ADS-155 §7. "No consumer published this metric" was one bucket
            # holding four situations an operator resolves in four different
            # ways. All-Time closed-won revenue landed in it, so a total that is
            # blocked by 14 unpriced HubSpot deals — a fixed, nameable, fixable
            # list — read exactly like a database outage. The readings already
            # carry each consumer's own declared reason; the audit now classifies
            # on it instead of reporting the absence.
            status, code, detail = _classify_unavailable(readings, metric_key)
            violations.append({
                "code": code, "metric": metric_key, "detail": detail,
                "readings": [{"consumer": r["consumer"],
                              "truth_status": r["truth_status"],
                              "unavailable_reason": r["unavailable_reason"],
                              "violation_codes": r["violation_codes"]}
                             for r in readings]})
        elif missing:
            # PR-ADS-154C-F1 §3: comparing only the readings that happen to be
            # present let a page that dropped a metric look like agreement. Every
            # REGISTERED consumer must answer, or the others are agreeing among
            # themselves about a question one of them declined.
            status = "consumer_missing"
            violations.append({
                "code": V_CONSUMER_METRIC_MISSING, "metric": metric_key,
                "detail": (f"{len(missing)} registered consumer(s) published no "
                           f"value while others did"),
                "missing": [{"consumer": r["consumer"], "path": r["path"]}
                            for r in missing]})
        elif bad_contract:
            # Provenance is CHECKED, not echoed. Printing the registry's expected
            # `canonical_source` proves nothing about where the number came from.
            status = "unverified_provenance"
            violations.append({
                "code": V_CONTRACT_INVALID, "metric": metric_key,
                "detail": "; ".join(f"{r['consumer']}: {r['contract_problem']}"
                                    for r in bad_contract)})
        elif len(distinct) > 1:
            status = "mismatch"
            violations.append({
                "code": V_VALUE_MISMATCH, "metric": metric_key,
                "detail": f"{len(distinct)} distinct values across consumers",
                "readings": [{"consumer": r["consumer"], "value": r["value"]}
                             for r in present]})
        elif not _consistent(present, "currency") or not _consistent(present, "customer_id"):
            status = "unverified_provenance"
            violations.append({
                "code": V_CONTRACT_INCONSISTENT, "metric": metric_key,
                "detail": "consumers disagree on currency or customer identity",
                "readings": [{"consumer": r["consumer"],
                              "currency": (r["contract"] or {}).get("currency"),
                              "customer_id": (r["contract"] or {}).get("customer_id")}
                             for r in present]})
        elif not coverage_proven:
            status = "unproven"
            violations.append({
                "code": V_AGREEMENT_ON_UNPROVEN_COVERAGE, "metric": metric_key,
                "detail": (f"every consumer published {baseline!r}, but {coverage_detail}"
                           " — a figure over an unproven range is not a measurement")})
        else:
            status = "identical"

        metrics.append({
            "metric": metric_key,
            "label": spec["label"],
            "scope": spec["scope"],
            "canonical_source": spec["canonical_source"],
            "coverage_proof": spec.get("coverage_proof"),
            "status": status,
            "value": baseline,
            "readings": readings,
        })

    # ── Conservation: do the parts of a split identity add back up? ─────────
    # PR-ADS-154C-F3. Splitting `google_ads_cohort_sqls` into a real-country
    # total and a governed residual is only honest if the two account for the
    # whole. If they do not, one of the three is wrong and the audit cannot tell
    # which — so it says so rather than certifying all three as identical.
    by_key = {m["metric"]: m for m in metrics}
    conservation = []
    for rule in CONSERVATION_RULES:
        total_entry = by_key.get(rule["total"])
        part_entries = [by_key.get(p) for p in rule["parts"]]
        values = [None if e is None else e.get("value")
                  for e in [total_entry, *part_entries]]
        total_source = "metric_identity"
        parts_payload = (consumers.get(rule.get("parts_consumer")) or {}).get("payload")
        if rule.get("settled_total") and _declares_settled_window(parts_payload):
            # PR-ADS-167A: the parts measured the settled-day window; the total
            # is the settled reference's figure over the same days, published
            # only while that reference's own window is the settled one.
            ref_name, ref_path = rule["settled_total"]
            ref = (consumers.get(ref_name) or {}).get("payload") or {}
            same_days = (_declares_settled_window(ref)
                         and _window_signature(ref, window)[0]
                         == _window_signature(parts_payload, window)[0])
            values[0] = _dig(ref, ref_path) if same_days else None
            total_source = f"{ref_name}:{ref_path}"
        row = {"total": rule["total"], "parts": rule["parts"],
               "total_value": values[0], "part_values": values[1:],
               "total_source": total_source,
               "reason": rule["reason"]}
        if any(v is None for v in values):
            # Not a violation: a withheld component cannot disprove conservation,
            # and calling that a failure would punish an honest outage.
            row["status"] = "not_evaluable"
            row["detail"] = "one or more components were not published"
        else:
            parts_sum = sum(values[1:])
            row["parts_sum"] = parts_sum
            if _norm(parts_sum) == _norm(values[0]):
                row["status"] = "conserved"
            else:
                row["status"] = "broken"
                row["detail"] = (
                    f"{' + '.join(f'{p}={v}' for p, v in zip(rule['parts'], values[1:]))} "
                    f"= {parts_sum}, but {rule['total']} = {values[0]}")
                violations.append({
                    "code": V_CONSERVATION_BROKEN, "metric": rule["total"],
                    "detail": row["detail"] + " — " + rule["reason"]})
        conservation.append(row)

    # ── PR-ADS-177A: one SQL count across the Countries page, Dashboard
    # Countries and Campaign Evidence, over the SAME days ─────────────────
    cohort_sql_parity = _cohort_sql_parity(consumers, window, now)
    if cohort_sql_parity["status"] == "broken":
        violations.append({"code": V_COHORT_SQL_PARITY_BROKEN,
                           "metric": "google_ads_cohort_sqls",
                           "detail": cohort_sql_parity["detail"]})

    # ── Certification: did every registered consumer actually answer? ────────
    # PR-ADS-154C-F2 §1. The audit built seven consumers and certified four
    # identities, then reported all seven as "inspected". Three pages passed by
    # having nothing checked — the strongest possible form of the agreement-shaped
    # failure this whole command exists to catch. A consumer is certified only
    # when at least one metric identity it is registered for came back with a
    # value AND a contract the registry accepts.
    certified: dict = {name: {"consumer": name, "identities_certified": 0,
                              "identities_registered": 0, "certified": False}
                       for name in sorted(CERTIFIED_CONSUMERS)}
    for entry in metrics:
        for r in entry["readings"]:
            row = certified.get(r["consumer"])
            if row is None:
                continue
            row["identities_registered"] += 1
            if r["value"] is not None and not r["contract_problem"]:
                row["identities_certified"] += 1
    for name, row in certified.items():
        row["certified"] = row["identities_certified"] > 0
        if name not in consumers:
            violations.append({
                "code": V_CONSUMER_NOT_BUILT, "consumer": name,
                "detail": ("registered in METRIC_IDENTITIES but not built by "
                           "_build_consumers, so nothing about it was checked")})
        elif not row["certified"]:
            violations.append({
                "code": V_CONSUMER_UNCERTIFIED, "consumer": name,
                "detail": (f"built and window-checked, but none of its "
                           f"{row['identities_registered']} registered metric "
                           f"identities produced a value with a valid contract")})

    # ── PR-ADS-155 §7 — coverage disclosures, distinct from violations ───────
    # The fourth class the audit must tell apart is lifecycle-partial: the
    # canonical funnel IS readable and its cohort IS published, but HubSpot holds
    # no stage-entry timestamp for some contacts that demonstrably reached a
    # stage. That is a disclosed, quantified gap in the source system, not a
    # product defect, and marking the audit red for it would make red the
    # permanent state of a condition we have decided to report rather than
    # invent our way out of. It is surfaced in its own section, with counts, so
    # it is impossible to miss and impossible to confuse with a broken metric.
    coverage_disclosures = _lifecycle_coverage_disclosures(consumers)

    ok = not violations
    return {
        "window": window,
        "window_label": resolved.get("label"),
        "window_start": resolved.get("start_date"),
        "window_end": resolved.get("end_date"),
        "timezone": resolved.get("timezone"),
        # PR-ADS-167A: the window the settled-day consumers must declare.
        "settled_window": {"start_date": settled.get("start_date"),
                           "end_date": settled.get("end_date"),
                           "through_reason": settled.get("through_reason")},
        "ok": ok,
        "consumers_inspected": sorted(consumers),
        # What "inspected" is worth, per consumer. `consumers_inspected` alone
        # said only that a page was built.
        "consumer_certification": [certified[k] for k in sorted(certified)],
        # PR-ADS-154C-F2 §5. Pages that publish overlapping executive-looking
        # figures and are NOT certified. Named here rather than omitted: a page
        # absent from a parity report reads as a page with nothing to answer for,
        # which is how an uncertified total keeps being read as a certified one.
        "uncertified_consumers": [
            {"consumer": name, **detail}
            for name, detail in sorted(PENDING_REDESIGN_CONSUMERS.items())],
        "consumer_windows": window_rows,
        "metrics": metrics,
        # PR-ADS-154C-F3: the split identities and whether they add back up.
        "conservation": conservation,
        # PR-ADS-177A: the one SQL count on three surfaces, the same days.
        "cohort_sql_parity": cohort_sql_parity,
        # PR-ADS-155 §7: quantified source-system gaps. Reported, not counted as
        # violations — see the comment above the assignment.
        "coverage_disclosures": coverage_disclosures,
        "distinct_by_design": DISTINCT_BY_DESIGN,
        "violations": violations,
        "violation_codes": sorted({v["code"] for v in violations}),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _cohort_sql_total(rows_total, residual):
    """``rows_total + residual`` when both are published integers, else None."""
    if rows_total is None or residual is None:
        return None
    if not isinstance(rows_total, int) or not isinstance(residual, int):
        return None
    return rows_total + residual


def _cohort_sql_parity(consumers: dict, window: str, now) -> dict:
    """PR-ADS-177A — the window's Google Ads acquisition-cohort SQLs, read from
    three surfaces over the SAME settled days, must be one number:

    * the Countries page (the mart's country view): listed markets'
      ``country_totals.sqls`` + the residual row's ``sqls``;
    * Dashboard Countries: ``kpis.sqls`` + ``residual.sqls``;
    * Campaign Evidence, built over exactly those days
      (``build_campaign_evidence(bounds=…)``): ``summary.cohort_sqls_google_ads``.

    A surface that withholds is ``not_evaluable`` (a withheld count cannot
    disprove parity), never a pass; any two published values that differ is
    ``broken``.
    """
    from datetime import date as _date  # noqa: PLC0415
    page = (consumers.get(CONSUMER_SETTLED_REFERENCE) or {}).get("payload") or {}
    dash = (consumers.get("dashboard/countries") or {}).get("payload") or {}
    residual_row = next((r for r in (page.get("rows") or []) if r.get("is_residual")), None)
    page_residual = ((residual_row or {}).get("sqls") if residual_row is not None
                     else (0 if (page.get("cohort") or {}).get("sql_status") == "published"
                           else None))
    readings = {
        "countries_page": _cohort_sql_total(
            (page.get("country_totals") or {}).get("sqls"), page_residual),
        "dashboard_countries": _cohort_sql_total(
            (dash.get("kpis") or {}).get("sqls"), (dash.get("residual") or {}).get("sqls")),
        "campaign_evidence": None,
    }
    w = page.get("window") or {}
    ce_error = None
    try:
        start = _date.fromisoformat(w["start_date"]) if w.get("start_date") else None
        end = _date.fromisoformat(w["end_date"]) if w.get("end_date") else None
        if end is not None and not (w.get("window_range") or {}).get("empty"):
            from services.campaign_evidence_service import build_campaign_evidence  # noqa: PLC0415
            ce = build_campaign_evidence(window, now=now, bounds=(start, end))
            readings["campaign_evidence"] = (ce.get("summary") or {}).get(
                "cohort_sqls_google_ads")
    except Exception as exc:  # noqa: BLE001 - a finding, never a crash
        ce_error = f"{type(exc).__name__}: {exc}"[:300]
    present = {k: v for k, v in readings.items() if v is not None}
    out = {"window": {"start_date": w.get("start_date"), "end_date": w.get("end_date")},
           "readings": readings, "campaign_evidence_error": ce_error}
    if len(set(present.values())) > 1:
        out["status"] = "broken"
        out["detail"] = ("the window's Google Ads acquisition-cohort SQLs differ across "
                         "surfaces over the same days: "
                         + ", ".join(f"{k}={v}" for k, v in sorted(present.items())))
    elif len(present) < len(readings):
        out["status"] = "not_evaluable"
        out["detail"] = ("withheld or unavailable on: "
                         + ", ".join(sorted(set(readings) - set(present))))
    else:
        out["status"] = "identical"
    return out


def _lifecycle_coverage_disclosures(consumers: dict) -> list[dict]:
    """Quantified lifecycle-coverage gaps, per consumer that publishes a cohort.

    PR-ADS-155 §7. Reads the consumer's OWN published ``lifecycle_cohort`` block
    rather than re-deriving coverage here, so the audit reports exactly what the
    page reported and cannot disagree with it.
    """
    rows = []
    for name in sorted(consumers):
        payload = (consumers.get(name) or {}).get("payload")
        cohort = (payload or {}).get("lifecycle_cohort")
        if not isinstance(cohort, dict):
            continue
        status = cohort.get("coverage_status")
        if status == "complete":
            continue
        # An UNAVAILABLE cohort is not a partial one, and must not be labelled as
        # though we had measured a gap in it. It is reported under the same code
        # family the absent-metric classifier uses, so one vocabulary describes
        # "could not read" wherever it appears.
        rows.append({
            "consumer": name,
            "code": (V_LIFECYCLE_PARTIAL if status == "partial"
                     else V_DB_UNREADABLE),
            "coverage_status": status,
            "truth_status": cohort.get("truth_status"),
            "cohort_size": cohort.get("cohort_size"),
            "excluded_contacts": cohort.get("excluded_contacts"),
            "exclusion_reasons": cohort.get("exclusion_reasons") or [],
            "detail": (
                "the lifecycle cohort is published but incomplete: HubSpot holds "
                "no stage-entry timestamp for some contacts that demonstrably "
                "reached a stage. No substitute date is used, and the missing "
                "transitions are excluded and counted rather than imputed."
                if status == "partial" else
                "the lifecycle cohort could not be built, so its coverage is "
                "UNKNOWN — not partial, and not complete."),
        })
    return rows


def _norm(value):
    """Normalise a reading for EXACT comparison.

    PR-ADS-154C-F1: this rounded to six decimals while the command claimed
    parity was exact. Rounding is a tolerance wearing different clothes — a
    narrow one, but the audit's whole argument is that two renderings of the same
    canonical figure must not need one. Two values that differ at the seventh
    decimal are two answers to one question, and if that ever happens it is worth
    knowing rather than smoothing away.

    ``Decimal(str(...))`` is used rather than raw floats so ``2.0`` and ``2``
    compare equal and the textual form does not reintroduce binary
    representation noise of its own.
    """
    from decimal import Decimal, InvalidOperation  # noqa: PLC0415

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:      # nan / inf
            return repr(value)
    return value


def _diff(value, baseline):
    """Absolute and percentage difference from the baseline reading."""
    if value is None or baseline is None:
        return None, None
    try:
        d = round(float(value) - float(baseline), 6)
    except (TypeError, ValueError):
        return None, None
    if not baseline:
        return d, None
    return d, round(abs(d) / abs(float(baseline)) * 100, 4)


def audit_all_windows(windows=None, now: datetime | None = None) -> dict:
    """Audit every required business window and roll the verdict up.

    ``ok`` is the conjunction: one window failing parity fails the audit, because
    a page that agrees this quarter and disagrees year-to-date is not a page that
    agrees.
    """
    keys = list(windows or WINDOW_KEYS)
    results = [audit_window(w, now=now) for w in keys]
    all_violations = [{**v, "window": r["window"]}
                      for r in results for v in r["violations"]]
    return {
        "ok": all(r["ok"] for r in results),
        "windows_audited": keys,
        "uncertified_consumers": [
            {"consumer": name, **detail}
            for name, detail in sorted(PENDING_REDESIGN_CONSUMERS.items())],
        "evidence_window_consumers": [
            {"consumer": name, **detail}
            for name, detail in sorted(EVIDENCE_WINDOW_CONSUMERS.items())],
        "results": results,
        "violations": all_violations,
        "violation_codes": sorted({v["code"] for v in all_violations}),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
