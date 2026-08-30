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
from services.canonical_contract import (
    METRIC_TRUTH_KEY,
    SOURCE_CANONICAL_FUNNEL,
    SOURCE_CANONICAL_GEO,
    SOURCE_CANONICAL_SPEND,
    SOURCE_REVENUE_BY_SOURCE,
    SOURCE_REVENUE_DECISION_MART,
    TRUTH_READY,
    resolve_canonical_window,
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

#: Identities that partition a whole. Registering the split is not enough — if
#: the parts do not add back to the total, one of the three is wrong and the
#: audit cannot tell which, so it says so rather than certifying all three.
#:
#: PR-ADS-154C-F3: Countries assigns SQLs to real countries and holds the rest in
#: an explicit residual. 14 + 2 = 16 in the production current quarter.
CONSERVATION_RULES = [
    {
        "total": "campaign_attributable_sqls",
        "parts": ["country_attributed_sqls", "country_unattributed_residual_sqls"],
        "reason": (
            "Every campaign-attributable SQL is either assigned to a real "
            "canonical country or held in the governed residual. The residual is "
            "never spread across countries to make the parts match, and a real "
            "country total is never inflated to swallow it."),
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
    "customers": {
        "label": "Closed-won customers, ALL sources",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "all_source_business_revenue",
        "coverage_proof": PROOF_DEAL_LEDGER,
        "consumers": [
            ("dashboard/overview", "kpis.customers"),
            ("dashboard/revenue", "kpis.customers"),
            ("dashboard/channels", "kpis.total_customers"),
            ("dashboard/deals", "kpis.closed_won_customers"),
            ("revenue_decision_mart", "summary.customers"),
        ],
        "consumer_sources": {"dashboard/channels": SOURCE_REVENUE_BY_SOURCE},
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
    "campaign_attributed_customers": {
        "label": "Closed-won customers attributable to a campaign",
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
    "country_attributed_customers": {
        "label": "Closed-won customers attributed to a country",
        "canonical_source": SOURCE_CANONICAL_GEO,
        "scope": "country_attributed_revenue",
        "coverage_proof": PROOF_COUNTRY_REVENUE,
        "consumers": [
            ("dashboard/countries", "kpis.customers"),
        ],
    },

    # ── SQL identities. Deliberately separate populations ────────────────────
    # The mart's `summary.sqls` is the CAMPAIGN-ATTRIBUTABLE subset — it says so
    # in its own `sql_reconciliation` block — and every page that renders "SQLs"
    # beside campaign or country spend is rendering that same subset.
    "campaign_attributable_sqls": {
        "label": "SQLs attributable to a campaign",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "campaign_attributable_sqls",
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
    "campaign_attributable_leads": {
        "label": "Leads attributable to a campaign",
        "canonical_source": SOURCE_REVENUE_DECISION_MART,
        "scope": "campaign_attributable_leads",
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
for _stage in LIFECYCLE_STAGES:
    METRIC_IDENTITIES[f"lifecycle_{_stage}"] = {
        "label": f"Lifecycle {_stage}, ALL sources (canonical funnel)",
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
        "services": ["/api/leads", "/api/leads/country-summary"],
        "note": ("Publishes lead and SQL counts from the pre-canonical lead "
                 "snapshot. Not a canonical executive total. Consumer migration "
                 "belongs to the Lead Intelligence redesign."),
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
        "left": "campaign_attributable_sqls",
        "right": "source_group_sqls",
        "reason": (
            "Campaign-attributable SQLs count qualified leads reaching a canonical "
            "Google Ads campaign; source-group SQLs count them by CRM acquisition "
            "source across every channel. Different populations under one word."),
    },
    {
        "left": "campaign_attributable_sqls",
        "right": "country_attributed_sqls",
        "reason": (
            "Countries sums the SQLs it could assign to a REAL canonical country; "
            "the mart counts every campaign-attributable SQL. The difference is "
            "the governed residual, published separately — 14 + 2 = 16 in the "
            "production current quarter — not a disagreement to reconcile away."),
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
            "timestamp. Country-attributed SQLs are the mart's campaign "
            "population narrowed to deals with a canonical country."),
    },
    {
        "left": "campaign_attributable_sqls",
        "right": "lifecycle_sqls",
        "reason": (
            "Lifecycle SQLs are stage-ENTRY events dated by the contact's own "
            "hs_v2_date_entered_salesqualifiedlead timestamp, all sources. The "
            "campaign-attributable count is a mart lead population filtered to "
            "campaign identity. Same word, different event and different date."),
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
        "left": "campaign_attributable_leads",
        "right": "lifecycle_leads",
        "reason": (
            "The mart's lead population is dated by contact_created_at and filtered "
            "by campaign exclusions; the lifecycle lead count is a canonical funnel "
            "stage-entry event. Two lead definitions, both legitimate."),
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
    mart = (consumers.get("revenue_decision_mart") or {}).get("payload") or {}
    spend_truth = mart.get("spend_truth") or {}
    summary = mart.get("summary") or {}
    proof = spec.get("coverage_proof")

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

    builders = {
        "dashboard/overview": lambda: build_dashboard_overview(window=window, now=now),
        "dashboard/revenue": lambda: build_dashboard_revenue(window=window, now=now),
        "dashboard/channels": lambda: build_dashboard_channels(window=window, now=now),
        "dashboard/campaigns": lambda: build_dashboard_campaigns(window=window, now=now),
        "dashboard/countries": lambda: build_dashboard_countries(window=window, now=now),
        "dashboard/deals": lambda: build_dashboard_deals(window=window, now=now),
        "revenue_decision_mart": lambda: build_revenue_decision_mart(
            window=window, view="campaign", now=now),
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
    window_rows = []
    signatures: dict = {}
    for name, entry in consumers.items():
        payload = entry["payload"]
        if payload is None:
            continue                       # already reported as V_CONSUMER_FAILED
        sig, problem = _window_signature(payload, window)
        window_rows.append({
            "consumer": name,
            "window_start": sig[0] if sig else None,
            "window_end": sig[1] if sig else None,
            "timezone": sig[2] if sig else None,
            "problem": problem,
        })
        if sig:
            signatures.setdefault(sig, []).append(name)
        else:
            violations.append({"code": V_WINDOW_MISSING, "consumer": name,
                               "detail": problem})
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
            status = "unavailable"
            violations.append({
                "code": V_SOURCE_UNAVAILABLE, "metric": metric_key,
                "detail": "no consumer published this metric"})
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
    # PR-ADS-154C-F3. Splitting `campaign_attributable_sqls` into a real-country
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
        row = {"total": rule["total"], "parts": rule["parts"],
               "total_value": values[0], "part_values": values[1:],
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

    ok = not violations
    return {
        "window": window,
        "window_label": resolved.get("label"),
        "window_start": resolved.get("start_date"),
        "window_end": resolved.get("end_date"),
        "timezone": resolved.get("timezone"),
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
        "distinct_by_design": DISTINCT_BY_DESIGN,
        "violations": violations,
        "violation_codes": sorted({v["code"] for v in violations}),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


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
        "results": results,
        "violations": all_violations,
        "violation_codes": sorted({v["code"] for v in all_violations}),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
