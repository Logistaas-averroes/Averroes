"""
scripts/audit_customer_revenue_reader_cutover.py

PR-ADS-161E — read-only proof that every production reader of closed-won
deals, customers, closed-won revenue, spend ratios and SQL-to-customer
conversion reads the canonical owner, and says what that owner decided.

Attribution ROAS and CAC are UNAVAILABLE everywhere (PR-ADS-161E decision 1):
closed-won outcomes are dated by close and spend by the day it was spent, so in
a long B2B sales cycle they are not one acquisition cohort. The close-date
ratios that are published carry explicit names —
``period_revenue_per_ad_spend`` and ``period_spend_per_customer_won`` — and
check 18 fails when one is presented as ROAS or CAC.

    python -m scripts.audit_customer_revenue_reader_cutover
    python -m scripts.audit_customer_revenue_reader_cutover --json

The canonical owner is ``services.canonical_customer_revenue_service`` over
``analysis.closed_won_truth``, read through ONE snapshot owned by
``services.canonical_revenue_service`` (the reader contract:
``load_won_deals`` / ``subset_metrics`` / ``reader_row``). This audit does not
decide any verdict. It proves:

 1  inventory_complete                    every production reader is inventoried
 2  customer_readers_canonical            customer readers use the contract
 3  revenue_readers_canonical             revenue readers use the contract
 4  no_won_deal_as_customer_label         no won-deal count is called customers
 5  no_legacy_table_gate                  revenue_integration_connected is gone
 6  windows_reconcile                     reader == canonical == independent
 7  status_and_reason_serialized          every metric carries status + reason
 8  withheld_values_absent                a withheld metric has no value
 9  withheld_not_reconstructable          no subset re-exposes a withheld total
10  zero_distinguishable                  published 0 renders "0", not a gap
11  period_ratios_withheld_on_withheld_input  derived ratios inherit the block
12  sql_to_customer_not_mixed             no rate across two cohorts
13  all_time_undated_and_no_future        All Time holds undated, no future
14  admin_links_resolve                   View blockers → workbench filters
15  non_admin_has_no_link                 viewers get no workbench link
16  no_external_calls_no_writes           no HubSpot / Google Ads / write path
17  independent_derivation_used           the audit restates, never reuses
18  no_period_ratio_named_roas_or_cac     no API field named roas/cac carries a
                                          figure; no UI calls a period ratio
                                          ROAS or CAC; certified ROAS/CAC are
                                          unavailable
19  meta_spend_disclosure_carried         PR-ADS-171B: every reader of the Meta
                                          spend truth carries the owner's
                                          retirement disclosure; pages read it
                                          only through the Meta line; the UI
                                          shows no figure without it
20  no_rate_over_meta_spend               the Meta line carries no HubSpot count
                                          and no ratio; withheld means absent
21  retired_readers_absent                PR-ADS-178C: /api/revenue-deals and
                                          /api/crm-funnel/customers-won are not
                                          registered or requested, and no module
                                          defines or calls build_customers_won

PR-ADS-168A: the same reader checks also run over the Channels page's
EVIDENCE windows (30d / 60d / 180d / all_time, ``report["evidence_windows"]``):
``load_won_deals(k, window_type="evidence")`` against the truth service and an
independent restatement of the evidence window, and ``channel_outcomes``
through checks 8, 9 and 18 against the publication over its settled days.

A metric WITHHELD for a CRM reason (a won deal without a company, without a
proven USD amount, without a close date) is a source finding, not a failure:
the audit exits 0 when every reader says so truthfully.

No HubSpot call. No Google Ads call. No write: the ledger is read in a
PostgreSQL REPEATABLE READ, READ ONLY snapshot; the page check reads the same
snapshot.

Exit codes
    0  every reader contract holds (metrics may be withheld — see the table)
    1  a reader contract is broken
    2  unavailable — the database or the canonical ledger could not be read
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

AUDIT_FILE = "scripts/audit_customer_revenue_reader_cutover.py"
WORKBENCH_FILE = "services/crm_truth_exceptions_service.py"
APP_JS = "static/app.js"
SERVER = "api/server.py"
PRODUCTION_PACKAGES = ("api", "services", "scheduler", "db", "analysis",
                       "connectors", "scripts")

#: PR-ADS-161E reader inventory. One row per consumer; the PR description and
#: docs/48 render this table. ``canonical_via`` is the symbol whose presence in
#: ``consumer`` proves the replacement is wired; ``status`` is ``migrated``,
#: ``withheld`` (a legacy reader that now publishes no figure), ``removed`` or
#: ``deferred`` (a reader that publishes no closed-won outcome, named with why).
INVENTORY = (
    {"consumer": "services/revenue_decision_mart.py",
     "surface": "/api/revenue-performance*; Overview, Revenue, Channels, Campaigns KPIs",
     "existing_source": "scope-ladder won_deals",
     "existing_meaning": "won-deal count published as `customers`",
     "canonical_replacement": "closed_won_deals + canonical customers metric "
                              "(closed_won_truth.canonical_metrics); attributed "
                              "subset via reader_row",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Customers = distinct companies or Withheld + reason; deals shown separately",
     "families": ("customers", "revenue", "period_ratios"),
     "canonical_via": "canonical_metrics", "status": "migrated"},
    {"consumer": "services/revenue_attribution_service.py",
     "surface": "/api/revenue-attribution/audit, the mart it feeds (PR-ADS-177C retired /api/revenue-attribution; PR-ADS-178C retired /api/revenue-deals — build_revenue_deals feeds the mart deal view and Overview)",
     "existing_source": "len(campaign deals), proven-amount sums",
     "existing_meaning": "per-campaign won deals as customers; partial sums",
     "canonical_replacement": "reader_row per campaign / country / summary; "
                              "campaign_placement_gate",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Won deals + customers columns; withheld rows say why",
     "families": ("customers", "revenue", "period_ratios"),
     "canonical_via": "reader_row", "status": "migrated"},
    {"consumer": "services/source_attribution_service.py",
     "surface": "build_revenue_by_source: the Dashboard Overview / Revenue / "
                "Deals / Channels source breakdowns (PR-ADS-168B deleted the "
                "Revenue by Source page, /api/revenue-by-source and the mart "
                "`source` view). PR-ADS-168A: /api/channel-funnel `outcomes` "
                "(build_channel_outcomes); Channels",
     "existing_source": "bucket deal count, bucket proven sum",
     "existing_meaning": "won deals as customers; customer rate over leads",
     "canonical_replacement": "reader_row per group / channel / platform; "
                              "lead_to_customer_rate incompatible. Channels: "
                              "load_won_deals(evidence window, settled bounds) + "
                              "reader_row per original source; totals = the "
                              "window's canonical_metrics",
     "window_cohort": "business window · close-date cohort; Channels: evidence "
                      "window ended yesterday · close-date cohort",
     "ui_effect": "Customer Rate column retired; customers per bucket, not additive. "
                  "Channels: outcome columns per channel, the deals-with-no-single-source row",
     "families": ("customers", "revenue", "period_ratios"),
     "canonical_via": "reader_row", "status": "migrated"},
    {"consumer": "services/campaign_identity_service.py",
     "surface": "/api/campaign-mapping-review",
     "existing_source": "label deal count / proven sum",
     "existing_meaning": "won deals as customers",
     "canonical_replacement": "reader_row per HubSpot campaign label",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Mapping card shows won deals and customers separately",
     "families": ("customers", "revenue"),
     "canonical_via": "reader_row", "status": "migrated"},
    # PR-ADS-165A: the Unit Economics row stood here
    # (`services/canonical_unit_economics_service.py`,
    # `/api/reports/unit-economics`). The page, its route and its service are
    # deleted by owner decision; `build_unit_economics` stays in
    # READER_ENTRY_POINTS so a module that brings it back is a reader again.
    {"consumer": "services/dashboard_overview_service.py",
     "surface": "/api/dashboard/overview",
     "existing_source": "mart summary customers (won deals); legacy gate",
     "existing_meaning": "won deals as customers",
     "canonical_replacement": "mart canonical customers + closed_won_deals",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Customers KPI from canonical metric; deals sub-line",
     "families": ("customers", "revenue"),
     "canonical_via": "customers_status", "status": "migrated"},
    {"consumer": "services/dashboard_revenue_service.py",
     "surface": "/api/dashboard/revenue",
     "existing_source": "won deals ÷ SQLs; legacy gate",
     "existing_meaning": "sql_to_customer_rate across two populations",
     "canonical_replacement": "canonical customers; SQL-to-customer and "
                              "revenue-per-SQL unavailable (incompatible cohorts)",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Rate cards read Unavailable + reason",
     "families": ("customers", "revenue", "sql_to_customer"),
     "canonical_via": "R_SQL_TO_CUSTOMER_INCOMPATIBLE", "status": "migrated"},
    {"consumer": "services/dashboard_deals_service.py",
     "surface": "/api/dashboard/deals",
     "existing_source": "gclid_attribution.has_closed_won_deal; won deals ÷ SQLs. "
                        "PR-ADS-178B: the mart deal summary, load_won_deals, "
                        "summarize_deals and build_revenue_by_source beside each other",
     "existing_meaning": "legacy-table won flag; mixed-cohort rate; three "
                         "closed-won counts over one ledger",
     "canonical_replacement": "PR-ADS-178B: republishes build_pipeline (the "
                              "Pipeline builder, row below) — no read of its own",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Dashboard → Deals renders the Pipeline page's KPI cards, deal "
                  "list and customers won",
     "families": ("customers", "revenue"),
     "canonical_via": "build_pipeline", "status": "migrated"},
    # PR-ADS-178B: the one Pipeline page (leads and deals together) and the
    # builder Dashboard → Deals republishes.
    {"consumer": "services/pipeline_service.py",
     "surface": "/api/pipeline; Pipeline page; Dashboard → Deals",
     "existing_source": "the Leads page's /api/crm-funnel/customers-won and the "
                        "Closed-Won Deals page's /api/revenue-performance?view=deal",
     "existing_meaning": "two customer builders and three closed-won counts on "
                         "three surfaces",
     "canonical_replacement": "the mart deal view's reader-contract summary for "
                              "deals / customers / revenue / average; the deal "
                              "list only beside a published count it equals; "
                              "subset_metrics per deal source group; customers "
                              "won only when the list numbers the published count. "
                              "PR-ADS-178C: the Customers won block also carries "
                              "load_won_deals' own customers / closed-won-deals "
                              "verdicts (the retired /api/crm-funnel/customers-won "
                              "reading, moved here from the funnel service)",
     "window_cohort": "business window through today · close-date cohort",
     "ui_effect": "KPI cards = {value, status, reason}; Withheld / Unavailable "
                  "+ reason; customers never summed; SQL→customer unavailable",
     "families": ("customers", "revenue", "sql_to_customer"),
     "canonical_via": "subset_metrics", "status": "migrated"},
    # PR-ADS-181A: the row for services/dashboard_channels_service.py
    # (/api/dashboard/channels, Dashboard → Channels) stood here. The owner
    # retired the tab; its route answers 404 and its builder is deleted, and
    # RETIRED_READER_ROUTES / RETIRED_READER_SYMBOLS keep them that way. The
    # Channels page reads closed-won outcomes through source_attribution_service
    # (build_channel_outcomes, the evidence pass below).
    {"consumer": "services/dashboard_campaigns_service.py",
     "surface": "/api/dashboard/campaigns",
     "existing_source": "per-campaign won deals summed as customers",
     "existing_meaning": "won deals as customers, summed",
     "canonical_replacement": "per-campaign reader_row fields; KPI = mart "
                              "attributed_customers",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Won deals + customers columns; KPI canonical",
     "families": ("customers", "revenue", "period_ratios"),
     "canonical_via": "attributed_customers", "status": "migrated"},
    {"consumer": "services/dashboard_countries_service.py",
     "surface": "/api/dashboard/countries",
     "existing_source": "per-country won deals; residual from all-source total",
     "existing_meaning": "won deals as customers; non-Google deals in the residual",
     "canonical_replacement": "per-country reader_row fields; residual from the "
                              "google_ads_source canonical total",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Closed-won deals KPI; customers per country, not summed",
     "families": ("customers", "revenue", "period_ratios"),
     "canonical_via": "google_ads_source_closed_won_deals", "status": "migrated"},
    # PR-ADS-166A's row for services/canonical_crm_funnel_service.py
    # (`build_customers_won`, /api/crm-funnel/customers-won) stood here.
    # PR-ADS-178C retired the route and moved the reading into the Pipeline
    # builder (row above); the funnel service reads no closed-won deal now,
    # and RETIRED_READER_ROUTES / RETIRED_READER_SYMBOLS keep it that way.
    {"consumer": "services/cross_page_parity_service.py",
     "surface": "/api/audit/cross-page-parity",
     "existing_source": "page `customers` fields",
     "existing_meaning": "won-deal parity under a customer name",
     "canonical_replacement": "closed_won_deals and canonical customers as two identities",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "none (admin audit)",
     "families": ("customers",),
     "canonical_via": "closed_won_deals", "status": "migrated"},
    {"consumer": "db/revenue_repository.py",
     "surface": "revenue_integration_connected()",
     "existing_source": "EXISTS over gclid_attribution",
     "existing_meaning": "a legacy table's existence gated canonical revenue",
     "canonical_replacement": "removed; readiness reads the canonical verdict "
                              "(canonical_revenue_available)",
     "window_cohort": "n/a",
     "ui_effect": "Revenue Health reads canonical_revenue_status",
     "families": ("revenue", "customers"),
     "canonical_via": None, "status": "removed"},
    {"consumer": "api/server.py",
     "surface": "/api/attribution/gclid-readiness (won_deals)",
     "existing_source": "data/hubspot_won_deals.json",
     "existing_meaning": "a local-file won-deal count",
     "canonical_replacement": "won_deals withheld; GCLID coverage figures kept",
     "window_cohort": "n/a",
     "ui_effect": "Won Deals card reads Withheld + reason",
     "families": ("customers",),
     "canonical_via": "won_deals_status", "status": "withheld"},
    {"consumer": "api/server.py",
     "surface": "/api/mailchimp/audit (admin)",
     "existing_source": "deal_source_attribution via fetch_durable_outcome_populations",
     "existing_meaning": "contacts with won deals as `customers`; SUM(COALESCE(amount, 0))",
     "canonical_replacement": "withheld: every closed-won outcome figure removed "
                              "(legacy_deal_source_attribution_not_canonical); "
                              "coverage figures kept",
     "window_cohort": "n/a", "ui_effect": "none (no frontend consumer)",
     "families": ("customers", "revenue"),
     "canonical_via": "_withhold_legacy_closed_won_outcomes", "status": "withheld"},
    {"consumer": "services/mailchimp_audit_service.py",
     "surface": "Mailchimp attribution feasibility audit (served only by /api/mailchimp/audit)",
     "existing_source": "deal_source_attribution",
     "existing_meaning": "contact-level closed-won outcomes for Mailchimp recipients",
     "canonical_replacement": "withheld at its only route (row above)",
     "window_cohort": "n/a", "ui_effect": "none",
     "families": (), "canonical_via": None, "status": "withheld"},
    {"consumer": "services/revenue_recovery_service.py",
     "surface": "admin revenue-recovery job run report",
     "existing_source": "the recovery job's own processed rows",
     "existing_meaning": "counts of what one recovery run found and wrote — an "
                         "operational job report, not a business total",
     "canonical_replacement": "deferred: describes a job run, publishes no "
                              "window's deals, customers or revenue",
     "window_cohort": "n/a", "ui_effect": "unchanged",
     "families": (), "canonical_via": None, "status": "deferred"},
    {"consumer": "services/revenue_reconciliation_service.py",
     "surface": "/api/audit/revenue-truth (admin)",
     "existing_source": "canonical ledger vs legacy ledgers",
     "existing_meaning": "a shadow reconciliation diagnostic",
     "canonical_replacement": "deferred: an admin diagnostic comparing sources, "
                              "not a page publishing a total",
     "window_cohort": "business window", "ui_effect": "unchanged",
     "families": (), "canonical_via": None, "status": "deferred"},
    {"consumer": "static/app.js",
     "surface": "every dashboard, Campaign / Country Revenue & Spend, Revenue by "
                "Source, mapping page",
     "existing_source": "API `customers` / revenue fields",
     "existing_meaning": "a value or a dash",
     "canonical_replacement": "canonicalMetricHtml / rowMetricHtml: value, 0, "
                              "Withheld + reason, Unavailable + reason; View "
                              "blockers for admins",
     "window_cohort": "as served", "ui_effect": "the four states, never a dash",
     "families": ("customers", "revenue", "period_ratios", "sql_to_customer"),
     "canonical_via": "canonicalMetricHtml", "status": "migrated"},
)

#: The canonical owner and its certification tooling: not consumers.
OWNERS = (
    "services/canonical_revenue_service.py",
    "services/canonical_customer_revenue_service.py",
    "analysis/closed_won_truth.py",
    "db/deal_ledger_repository.py",
    "services/crm_truth_exceptions_service.py",   # PR-ADS-191 read model
    "scripts/audit_customer_closed_won_truth.py",
    "scripts/audit_deal_company_association_coverage.py",
    "scripts/audit_crm_truth_exceptions.py",
    # PR-ADS-161H: a read-only shell diagnostic beside the audits. It reads
    # the truth's attribution partition and the acquisition contacts to
    # measure attribution feasibility; it serves no page and publishes nothing.
    "scripts/report_attribution_feasibility.py",
    AUDIT_FILE,
)
#: Legacy modules no production route reads any more (the routes that did now
#: withhold). Named so a new consumer of them fails ``inventory_complete``.
#: PR-ADS-162E deleted the local-JSON ROAS chain outright
#: (``analysis/roas_calculator.py``, ``analysis/unit_economics.py``,
#: ``services/unit_economics_service.py``, ``services/roas_snapshot_service.py``
#: and its two smoke scripts), with the routes and the daily job that served
#: it; :data:`READER_ENTRY_POINTS` still names its functions, so re-adding one
#: makes its module a reader that must be inventoried.
#: PR-ADS-165A deleted the canonical Unit Economics reader the same way
#: (``services/canonical_unit_economics_service.py`` and its route): a deleted
#: file is not listed here, and its entry point is named below.
RETIRED_LEGACY = (
    "analysis/attribution_matcher.py", "analysis/gclid_readiness_audit.py",
    "scripts/report_missing_deal_amounts.py",
)
#: A reference to any of these makes a module a closed-won reader.
READER_ENTRY_POINTS = (
    "load_won_deals", "reader_row", "subset_metrics", "load_closed_won_universe",
    "canonical_customer_revenue_service", "fetch_won_deals",
    "compute_all_campaign_roas", "compute_all_country_roas",
    "load_latest_roas_snapshot", "load_roas_snapshots",
    "run_gclid_readiness_audit", "compute_cac", "build_unit_economics",
    "has_closed_won_deal",
    "get_revenue_snapshot", "get_scope_ladder", "canonical_deal_rows",
    # Direct SQL over the legacy closed-won tables (gclid_attribution,
    # deal_source_attribution): every repository function that reads them is
    # an entry point, so a page calling one is a reader too.
    "fetch_durable_outcome_populations", "fetch_won_revenue",
    "fetch_campaign_deal_details", "fetch_country_deal_details",
    "fetch_revenue_deals", "fetch_source_revenue", "fetch_source_revenue_daily",
    # PR-ADS-168B review: fetch_source_deal_details is deleted; the name stays
    # (as build_unit_economics does) so a module that brings it back is a reader.
    "fetch_source_deal_details", "fetch_sql_lead_details",
    "source_attribution_health_counts",
)
#: PR-ADS-178C — closed-won reader routes retired with the Leads / Deals pages
#: (each answers 404; the Pipeline page, /api/pipeline, publishes both), and
#: the builder only one of them served. A route registered under one of these
#: paths, a page requesting one, or a production module that defines or calls
#: the builder again is a violation (``retired_readers_absent``).
#: PR-ADS-181A — the Dashboard → Channels tab's route and builder joined them
#: (owner decision: the Channels page is the one channel surface).
RETIRED_READER_ROUTES = ("/api/revenue-deals", "/api/crm-funnel/customers-won",
                         "/api/dashboard/channels")
RETIRED_READER_SYMBOLS = ("build_customers_won", "build_dashboard_channels")
#: The legacy gate this PR removes, by name.
LEGACY_GATE = "revenue_integration_connected"
FORBIDDEN_IMPORTS = ("db.writers", "writers", "connectors.hubspot_pull",
                     "hubspot_pull", "connectors.google_ads_direct",
                     "google_ads_direct", "connectors.google_ads_source",
                     "connectors.hubspot_deals",
                     # PR-ADS-171A: a reader-contract module never calls Meta.
                     "connectors.meta_ads", "meta_ads")
#: Workbench families a View blockers link may name (docs/47).
WORKBENCH_FAMILIES = ("customer_identity", "revenue_proof", "won_deal_membership")
_PUBLICATION_STATUSES = ("published", "withheld", "unavailable", "not_published")


class Audit:
    def __init__(self) -> None:
        self.violations: list = []
        self.checks: dict = {}

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = self.checks.get(name, True) and bool(ok)
        if not ok:
            self.violations.append(f"{name}: {detail}")


def _text(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _text_if_exists(rel: str) -> str:
    """``rel``'s text, or "" when the file does not exist — a missing reader
    carries no verdict (PR-ADS-178B: the counterfactual evaluates older trees
    that predate ``services/pipeline_service.py``)."""
    path = _ROOT / rel
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _imports_of(rel: str) -> set:
    tree = ast.parse(_text(rel))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            names.update(f"{mod}.{a.name}" if mod else a.name for a in node.names)
            names.update(a.name for a in node.names)
    return names


def _code_names(rel: str) -> set:
    """Every identifier ``rel``'s CODE uses — imports, names, attributes —
    so a path in a string or a docstring is not a read."""
    tree = ast.parse(_text(rel))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    for n in _imports_of(rel):
        names.update(n.split("."))
    return names


def _production_py() -> list:
    return sorted(str(p.relative_to(_ROOT)) for d in PRODUCTION_PACKAGES
                  for p in (_ROOT / d).rglob("*.py")
                  if "__pycache__" not in p.parts)


def _js_function(src: str, name: str) -> str:
    """The source of one top-level JS function (brace-matched)."""
    i = src.find(f"function {name}(")
    if i < 0:
        return ""
    j = src.find("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return src[i:]


# ── structural checks (1–5, 10, 12, 14–17) ────────────────────────────────────

def structural_checks(a: Audit) -> dict:
    inventoried = {row["consumer"] for row in INVENTORY}
    readers = []
    for rel in _production_py():
        if rel in OWNERS or rel in RETIRED_LEGACY:
            continue
        if _code_names(rel) & set(READER_ENTRY_POINTS):
            readers.append(rel)
    missing = sorted(set(readers) - inventoried)
    a.check("inventory_complete", not missing,
            f"production readers not in INVENTORY: {missing}")
    gone = sorted(r for r in inventoried if not (_ROOT / r).exists())
    a.check("inventory_complete", not gone, f"inventoried files missing: {gone}")
    # A retired legacy module may only be read by a withheld/deferred row.
    for rel in _production_py():
        if rel in OWNERS or rel in RETIRED_LEGACY:
            continue
        names = _imports_of(rel)
        for legacy in RETIRED_LEGACY:
            mod = legacy[:-3].replace("/", ".")
            if mod in names or any(n.startswith(mod + ".") for n in names):
                rows = [r for r in INVENTORY if r["consumer"] == rel]
                a.check("inventory_complete",
                        rows and all(r["status"] in ("withheld", "deferred",
                                                     "migrated") for r in rows),
                        f"{rel} reads retired {legacy} without an inventory row")

    for row in INVENTORY:
        via = row.get("canonical_via")
        if row["status"] in ("migrated", "withheld") and via:
            present = via in _text_if_exists(row["consumer"])
            for fam, check in (("customers", "customer_readers_canonical"),
                               ("revenue", "revenue_readers_canonical")):
                if fam in row["families"]:
                    a.check(check, present,
                            f"{row['consumer']} ({row['surface']}) does not "
                            f"use {via}")

    # 4 — no won-deal count labelled customers.
    bad = []
    py_patterns = (
        r"""["']customers["']\s*:\s*[^,\n]*\bwon_deals\b""",
        r"""\bcustomers\s*=\s*[^\n]*\b(won_deals|len\(\s*\w*deals?\w*\s*\))""",
        r"""["']customers["']\s*:\s*len\(""",
        r"""["']total_customers["']""",
        r"""["']deals_won["']""",
    )
    for rel in sorted({r["consumer"] for r in INVENTORY
                       if r["consumer"].endswith(".py")}):
        for i, line in enumerate(_text_if_exists(rel).splitlines(), 1):
            code = line.split("#", 1)[0]
            for pat in py_patterns:
                if re.search(pat, code):
                    bad.append(f"{rel}:{i}: {line.strip()[:90]}")
    js = _text(APP_JS)
    for pat in (r"Closed-Won Customers", r'_chanMomentumMetric = "customers"',
                r"fmtSourceRate\(\w+\.customers", r">Customer Rate<",
                r"k\.total_customers", r"countries_with_customers",
                r"residual_customers", r"r\.deals_won"):
        for m in re.finditer(pat, js):
            bad.append(f"{APP_JS}:{js.count(chr(10), 0, m.start()) + 1}: {pat}")
    a.check("no_won_deal_as_customer_label", not bad, "; ".join(bad[:8]))

    # 5 — the legacy existence gate is gone everywhere.
    users = []
    for rel in _production_py() + [APP_JS]:
        if rel == AUDIT_FILE:
            continue
        for i, line in enumerate(_text(rel).splitlines(), 1):
            code = line.split("#", 1)[0] if rel.endswith(".py") else line
            if LEGACY_GATE in code or "revenue_integration_status" in code:
                users.append(f"{rel}:{i}")
    a.check("no_legacy_table_gate", not users,
            f"{LEGACY_GATE} / revenue_integration_status still read at {users}")
    deals_src = _text("services/dashboard_deals_service.py")
    a.check("no_legacy_table_gate",
            "has_closed_won_deal" not in re.sub(r"#[^\n]*|\"\"\".*?\"\"\"", "",
                                                deals_src, flags=re.S),
            "dashboard_deals_service still reads gclid_attribution.has_closed_won_deal")

    # 10 — the renderer publishes 0 as "0" and a gap as a word with a reason.
    cm = _js_function(js, "canonicalMetricHtml")
    a.check("zero_distinguishable",
            'm.status === "published" && m.value !== null && m.value !== undefined'
            in cm and '"Withheld"' in cm and '"Unavailable"' in cm
            and "canonicalReasonLabel" in cm,
            "canonicalMetricHtml must render a published 0 and name withheld/"
            "unavailable with the reason")
    dv = _js_function(js, "dashValue")
    a.check("zero_distinguishable",
            "v === null || v === undefined" in dv and "|| 0" not in dv,
            "dashValue must treat only null/undefined as unavailable")

    # 12 — no SQL-to-customer / revenue-per-SQL ratio computed anywhere.
    mixed = []
    for rel in ("services/dashboard_revenue_service.py",
                "services/dashboard_deals_service.py",
                "services/pipeline_service.py",
                "services/source_attribution_service.py",
                "services/revenue_decision_mart.py"):
        for i, line in enumerate(_text_if_exists(rel).splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(customers|won_deals|closed_won_deals|won_revenue|revenue)"
                         r"[^\n]*/\s*[^\n]*\b(sqls|leads)\b", code):
                mixed.append(f"{rel}:{i}")
    # PR-ADS-178B: Dashboard → Deals republishes the Pipeline builder, which
    # carries the verdict for both surfaces.
    for rel in ("services/dashboard_revenue_service.py",
                "services/pipeline_service.py"):
        if "R_SQL_TO_CUSTOMER_INCOMPATIBLE" not in _text_if_exists(rel):
            mixed.append(f"{rel}: no incompatible-rate verdict")
    a.check("sql_to_customer_not_mixed", not mixed,
            f"cross-cohort rate computed at {mixed}")

    # 14 / 15 — View blockers: admin only, workbench families, window format.
    href = _js_function(js, "crmBlockersHref")
    a.check("non_admin_has_no_link",
            'if (!(_currentUser && _currentUser.role === "admin")) return null;'
            in href, "crmBlockersHref must return null for a non-admin")
    a.check("non_admin_has_no_link",
            "crmBlockersHref(" in cm and "href ?" in cm,
            "canonicalMetricHtml must render the link only when one is returned")
    fam_map = re.search(r"const CRM_BLOCKER_FAMILY = \{(.*?)\};", js, re.S)
    fams = set(re.findall(r':\s*"([a-z_]+)"', fam_map.group(1))) if fam_map else set()
    a.check("admin_links_resolve", fams and fams <= set(WORKBENCH_FAMILIES),
            f"blocker families {sorted(fams)} not all workbench families")
    # The workbench's own family constants, read as text: this audit is not one
    # of the workbench's named importers (audit_crm_truth_exceptions fences them).
    served = set(re.findall(r'^FAMILY_[A-Z]+ = "([a-z_]+)"',
                            _text(WORKBENCH_FILE), re.M))
    a.check("admin_links_resolve", fams and fams <= served,
            f"workbench serves {sorted(served)}, links name {sorted(fams)}")
    a.check("admin_links_resolve",
            'p.set("cte_window", `${o.windowType}/${o.windowKey}`)' in href
            and 'p.set("cte_family", family)' in href
            and "#/health?" in href,
            "crmBlockersHref must deep-link #/health with cte_window and cte_family")
    pend = _js_function(js, "_ctePendingFilters")
    a.check("admin_links_resolve",
            'q.get("cte_window")' in pend and 'q.get("cte_family")' in pend,
            "the workbench must read the deep-link filters")

    # 16 — no external call or write path in the reader contract or here.
    for rel in ("services/canonical_revenue_service.py",
                "analysis/closed_won_truth.py", AUDIT_FILE):
        bad_imp = sorted(n for n in _imports_of(rel) if n in FORBIDDEN_IMPORTS)
        a.check("no_external_calls_no_writes", not bad_imp,
                f"{rel} imports {bad_imp}")
    server = _text(SERVER)
    # PR-ADS-162E: the local-JSON ROAS report and snapshot routes are deleted
    # (they had been withholding every figure since PR-ADS-161E). Nothing in
    # the server may route to, or compute from, that chain again.
    for gone in ("compute_all_campaign_roas", "compute_all_country_roas",
                 "load_latest_roas_snapshot", "load_roas_snapshots",
                 "generate_roas_snapshot", '"/api/reports/roas/'):
        a.check("no_external_calls_no_writes", gone not in server,
                f"{SERVER} references the deleted legacy ROAS chain: {gone}")

    # 21 — PR-ADS-178C: the retired Leads / Deals readers stay retired
    # (PR-ADS-181A: and the retired Dashboard → Channels reader).
    retired_reader_checks(a, js)

    # 18 — no UI text presents a period ratio as ROAS or CAC.
    ui_bad = ui_ratio_label_violations(js, _text("static/index.html"))
    a.check("no_period_ratio_named_roas_or_cac", not ui_bad, "; ".join(ui_bad[:8]))

    # 19 / 20 — PR-ADS-171B: Meta spend.
    meta_structural_checks(a, js)

    # 17 — the audit's derivation does not call the module it audits.
    own = _text(AUDIT_FILE)
    body = own[own.find("# ── independent derivation"):
               own.find("# ── end independent derivation")]
    a.check("independent_derivation_used",
            body and "cwt." not in body and "closed_won_truth" not in body
            and "canonical_customer_revenue_service" not in body,
            "the independent derivation must not call the audited module")
    return {"readers": readers}


_ROUTE_METHODS = ("get", "post", "put", "patch", "delete", "head", "options",
                  "api_route", "add_api_route", "add_route", "websocket")


def _registered_routes(src: str) -> set:
    """Every path a FastAPI route in ``src`` registers, by AST (a docstring or
    a comment that names a path is not a route): a decorator OR a direct call
    (``app.add_api_route(...)``), the path given positionally or as
    ``path=``, as a literal or as a module-level string constant. Paths are
    normalised without a trailing slash (``/x/`` answers ``/x`` by redirect).
    PR-ADS-178C-F1: the decorator-only, positional-literal scan let
    ``add_api_route``, ``path=`` and a named constant through."""
    tree = ast.parse(src)
    consts = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = node.value.value

    def _value(arg):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name):
            return consts.get(arg.id)
        if isinstance(arg, ast.JoinedStr) or isinstance(arg, ast.BinOp):
            # An f-string or a concatenation: keep its literal parts joined,
            # so a split path still reads as the path it builds.
            return "".join(n.value for n in ast.walk(arg)
                           if isinstance(n, ast.Constant) and isinstance(n.value, str))
        return None

    routes = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _ROUTE_METHODS):
            continue
        cands = list(node.args[:1]) + [k.value for k in node.keywords if k.arg == "path"]
        for arg in cands:
            v = _value(arg)
            if v and v.startswith("/"):
                routes.add(v.rstrip("/") or "/")
    return routes


#: A retired path anywhere in app.js code (comments stripped), however the
#: request is built: quoted, after a ``${API_BASE}`` template part, or split.
def _js_requests(js: str, path: str) -> bool:
    code = _strip_comments(js)
    joined = re.sub(r"""["'`]\s*\+\s*["'`]""", "", code)
    return re.search(re.escape(path) + r"(?![\w-])", joined) is not None


def retired_reader_checks(a: Audit, js: str) -> None:
    """21 — PR-ADS-178C: no retired Leads / Deals reader is back
    (PR-ADS-181A: nor the retired Dashboard → Channels reader).

    * no route is registered under a retired path (it would answer again);
    * no page requests one (a fetch to a 404 is a page that never loads);
    * no production module defines or calls a retired builder (code names,
      not comments: a docstring that says it was retired is not a reader).
    """
    problems = [f"{SERVER} registers {r}" for r in RETIRED_READER_ROUTES
                if r in _registered_routes(_text(SERVER))]
    for r in RETIRED_READER_ROUTES:
        if _js_requests(js, r):
            problems.append(f"{APP_JS} requests {r}")
    for rel in _production_py():
        if rel == AUDIT_FILE:
            continue
        names = _code_names(rel)
        tree = ast.parse(_text(rel))
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for sym in RETIRED_READER_SYMBOLS:
            if sym in names or sym in defined:
                problems.append(f"{rel} defines or calls {sym}")
    a.check("retired_readers_absent", not problems, "; ".join(problems[:8]))


# ── 19 / 20: Meta spend (PR-ADS-171B) ─────────────────────────────────────────

#: The ONLY reader of Meta spend totals, and the readers allowed to call it.
#: Each must carry ``owner_declared_retired_disclosure`` beside the figure.
META_SPEND_TRUTH = "services/meta_ads_spend_truth_service.py"
META_TRUTH_ENTRY = "build_meta_ads_spend_truth_for_days"
META_SPEND_READERS = (
    {"consumer": "services/meta_ads_spend_line_service.py",
     "surface": "the Meta Ads (spend) line (Channels)"},
    {"consumer": "services/meta_ads_adset_truth_service.py",
     "surface": "ad-set breakdown (PR-ADS-171C); read by the Campaigns page's "
                "Meta Ads — ad sets table (PR-ADS-177B)"},
)
#: PR-ADS-177B: the ad-set breakdown's ONLY page reader, and the JS gate every
#: figure of its table goes through. A module that calls the breakdown and is
#: not registered here is a violation; the reader must carry the owner's
#: retirement disclosure beside the figures.
META_ADSET_ENTRY = "build_meta_adset_breakdown_for_days"
META_ADSET_PAGES = (
    {"consumer": "services/meta_ads_adset_table_service.py",
     "surface": "/api/meta-ads/adsets; Campaigns → Meta Ads — ad sets"},
)
META_ADSET_JS_GATE = "metaAdsetTableGate"
META_ADSET_JS_RENDERERS = ("renderMetaAdsetTable",)
#: Pages show Meta spend only through the line, which gates on the disclosure.
META_LINE_ENTRY = "build_meta_ads_spend_line"
META_LINE_PAGES = (
    {"consumer": "services/source_attribution_service.py",
     "surface": "/api/channel-funnel `outcomes.meta_ads_line`; Channels"},
    # PR-ADS-181A: services/dashboard_channels_service.py (Dashboard → Channels)
    # is retired with its tab; the Channels page is the line's one page.
)
#: The JS that renders the line, and the one gate every renderer goes through.
META_JS_GATE = "metaLineSpendMetric"
#: PR-ADS-181A: the strip cell (channelOutcomeStripHtml) and the retired
#: Dashboard tab's panel (renderChanMetaPanel) are gone; the Meta Ads card of
#: the Channels page's paid block is the one place the figure is drawn.
META_JS_RENDERERS = ("channelMetaSpendCellHtml", "channelMetaCardHtml")
#: Keys the line must never carry: a HubSpot count, or a figure over one.
#: (``[l]`` keeps the SQL doctrine scanner from reading a field name here.)
META_LINE_FORBIDDEN_KEYS = re.compile(
    r"(?:^|_)(?:contacts?|sq[l]s?|mq[l]s?|leads?|deals?|customers?|revenue|won|"
    r"cpl|cpa|ratio|per)(?:$|_)")
#: Keys the line carries that name a forbidden word but hold no count: the
#: comparison verdict and the per-account metadata.
_META_LINE_ALLOWED_KEYS = ("compared_with_outcomes", "meta_reported_leads_not_used")


#: A Meta spend rate over a HubSpot count, in any wording: plural, singular or
#: abbreviated (``per lead``, ``/ contact``, ``/ SQL``, ``CPL``, ``cost per``),
#: any case, within 80 characters after a mention of Meta on the same line.
#: (``[l]`` keeps the SQL doctrine scanner from reading a field name here.)
META_RATE_LABEL = re.compile(
    r"(?i)meta[^\n]{0,80}?(?:\bper\s+(?:leads?|contacts?|sq[l]s?|mq[l]s?|deals?|customers?)\b"
    r"|/\s*(?:leads?|contacts?|sq[l]s?|mq[l]s?|deals?|customers?)\b"
    r"|\bcpl\b|\bcpa\b|\bcost[\s_-]*per\b)")


def _string_constants(rel: str) -> set:
    return {n.value for n in ast.walk(ast.parse(_text(rel)))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def meta_structural_checks(a: Audit, js: str) -> None:
    readers = {r["consumer"] for r in META_SPEND_READERS}
    pages = {r["consumer"] for r in META_LINE_PAGES}
    for rel in _production_py():
        if rel in (META_SPEND_TRUTH, AUDIT_FILE):
            continue
        names = _code_names(rel)
        if META_TRUTH_ENTRY in names:
            a.check("meta_spend_disclosure_carried", rel in readers,
                    f"{rel} reads the Meta spend truth and is not in META_SPEND_READERS")
        if META_LINE_ENTRY in names and rel != "services/meta_ads_spend_line_service.py":
            a.check("meta_spend_disclosure_carried", rel in pages,
                    f"{rel} builds the Meta line and is not in META_LINE_PAGES")
    adset_pages = {r["consumer"] for r in META_ADSET_PAGES}
    for rel in _production_py():
        if rel in (AUDIT_FILE, "services/meta_ads_adset_truth_service.py"):
            continue
        if META_ADSET_ENTRY in _code_names(rel):
            a.check("meta_spend_disclosure_carried", rel in adset_pages,
                    f"{rel} reads the Meta ad-set breakdown and is not in META_ADSET_PAGES")
    for rel in sorted(adset_pages):
        a.check("meta_spend_disclosure_carried", (_ROOT / rel).exists()
                and "owner_declared_retired_disclosure" in _string_constants(rel),
                f"registered Meta ad-set reader {rel} is missing or does not "
                "carry owner_declared_retired_disclosure")
    adset_gate = _js_function(js, META_ADSET_JS_GATE)
    a.check("meta_spend_disclosure_carried",
            "owner_declared_retired_disclosure" in adset_gate
            and "meta_retired_disclosure_missing" in adset_gate,
            f"{META_ADSET_JS_GATE} must withhold a table whose declaration has no disclosure")
    for fn in META_ADSET_JS_RENDERERS:
        body = _js_function(js, fn)
        a.check("meta_spend_disclosure_carried", bool(body) and META_ADSET_JS_GATE in body,
                f"{fn} must read the Meta ad-set table through {META_ADSET_JS_GATE}")
    for rel in sorted(readers | pages):
        # A registered file that is gone is a broken register, not a crash.
        a.check("meta_spend_disclosure_carried", (_ROOT / rel).exists(),
                f"registered Meta spend reader {rel} does not exist")
    for rel in sorted(r for r in readers if (_ROOT / r).exists()):
        a.check("meta_spend_disclosure_carried",
                "owner_declared_retired_disclosure" in _string_constants(rel),
                f"{rel} reads the Meta spend truth without carrying "
                "owner_declared_retired_disclosure")
    gate = _js_function(js, META_JS_GATE)
    a.check("meta_spend_disclosure_carried",
            "owner_declared_retired_disclosure" in gate
            and "meta_retired_disclosure_missing" in gate,
            f"{META_JS_GATE} must withhold a figure whose declaration has no disclosure")
    for fn in META_JS_RENDERERS:
        body = _js_function(js, fn)
        a.check("meta_spend_disclosure_carried", bool(body) and META_JS_GATE in body,
                f"{fn} must read the Meta figure through {META_JS_GATE}")
    # No renderer reads the line's spend around the gate: outside it, the
    # Meta line's variables never have ``.spend`` read.
    outside = js.replace(gate, "") if gate else js
    bypass = [m.group(0) for m in re.finditer(
        r"\b(?:metaLine|meta_ads_line|meta)\.spend\b", outside)]
    a.check("meta_spend_disclosure_carried", not bypass,
            f"app.js reads the Meta line's spend outside {META_JS_GATE}: {bypass[:4]}")
    # 20 — no column or cell divides Meta spend by a HubSpot count.
    rate = META_RATE_LABEL.search(js)
    a.check("no_rate_over_meta_spend", rate is None,
            "app.js labels a Meta spend rate over a HubSpot count: "
            + (repr(rate.group(0)[-120:]) if rate else ""))


def meta_line_violations(line) -> list:
    """Pure. What is wrong with one payload's Meta line."""
    if line is None:
        return []
    out = []
    spend = (line or {}).get("spend") or {}
    published = spend.get("status") == "published"
    if published and line.get("owner_declared_retired") \
            and not line.get("owner_declared_retired_disclosure"):
        out.append("a published Meta figure rests on the owner's declaration "
                   "without its disclosure")
    if not published:
        figures = [f"spend={spend.get('value')}"] if spend.get("value") is not None else []
        native = line.get("native_spend") or {}
        if native.get("value") is not None and native.get("status") != "published":
            figures.append(f"native_spend={native.get('value')}")
        for acct in line.get("accounts") or []:
            for k in ("native_spend", "usd_spend"):
                if (acct.get(k) or {}).get("value") is not None:
                    figures.append(f"{acct.get('account_id')}.{k}")
        if figures:
            out.append(f"withheld Meta spend carries figures: {figures[:4]}")
    for path, d in _walk(line):
        for k in d:
            if k in _META_LINE_ALLOWED_KEYS:
                continue
            if META_LINE_FORBIDDEN_KEYS.search(str(k)):
                out.append(f"the Meta line carries {path}.{k}")
    comparison = line.get("comparison") or {}
    if comparison.get("status") == "published" or comparison.get("value") is not None:
        out.append(f"the Meta line publishes a comparison: {comparison}")
    return out


def meta_line_checks(a: Audit, where: str, line) -> None:
    bad = meta_line_violations(line)
    disclosure = [b for b in bad if "declaration" in b]
    a.check("meta_spend_disclosure_carried", not disclosure, f"{where}: {disclosure}")
    rest = [b for b in bad if b not in disclosure]
    a.check("no_rate_over_meta_spend", not rest, f"{where}: {rest[:4]}")


# ── independent derivation ────────────────────────────────────────────────────
# Restated from the raw snapshot rows; nothing here calls the truth module.

_ACCOUNT_TZ = "Europe/London"
_WON_STAGE_ID = "326093516"


def _dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        p = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return p if p.tzinfo else p.replace(tzinfo=timezone.utc)


_EVIDENCE_DAYS = {"7d": 7, "14d": 14, "30d": 30, "60d": 60, "180d": 180,
                  "all_time": None}


def independent_evidence_bounds(window_key: str, now) -> tuple:
    """An evidence window restated (PR-ADS-168A): N calendar days in the
    account zone ending on today's account date, half-open on account-local
    midnights; All Time has no start."""
    from datetime import time, timedelta  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    tz = ZoneInfo(_ACCOUNT_TZ)
    today = now.astimezone(tz).date()
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz)
    days = _EVIDENCE_DAYS[window_key]
    start = (None if days is None else
             datetime.combine(today - timedelta(days=days - 1), time.min, tzinfo=tz))
    return (start.astimezone(timezone.utc) if start else None,
            end.astimezone(timezone.utc))


def independent_members(universe: dict, window_key: str, now,
                        window_type: str = "business") -> dict:
    """Members of a business (or, PR-ADS-168A, an evidence) window,
    re-derived: the won flag only, an exact close date in ``[start, end)`` and
    not after ``now``; All Time also holds the undated won deals."""
    if window_type == "evidence":
        start, end = independent_evidence_bounds(window_key, now)
    else:
        from analysis.business_windows import get_window_bounds  # noqa: PLC0415 - the definition
        start, end = get_window_bounds(window_key, now=now)
    start, end = _dt(start), _dt(end)
    all_time = start is None
    members, undated, future = [], 0, 0
    for r in universe.get("won_rows") or []:
        if r.get("hs_is_closed_won") is not True and "hs_is_closed_won" in r:
            continue
        close = _dt(r.get("deal_close_date"))
        if close is None:
            undated += 1
            if all_time:
                members.append(str(r["deal_id"]))
            continue
        if close > now:
            future += 1
            continue
        if (start is None or close >= start) and close < end:
            members.append(str(r["deal_id"]))
    conflicts = 0
    for d in universe.get("won_definition_rows") or []:
        if (d.get("hs_is_closed_won") is True) != (
                str(d.get("deal_stage_id") or "") == _WON_STAGE_ID):
            conflicts += 1
    return {"members": sorted(members), "undated": undated, "future": future,
            "conflicts": conflicts, "all_time": all_time}


def _walk(value, path=""):
    if isinstance(value, dict):
        yield path, value
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")

# ── end independent derivation ────────────────────────────────────────────────


def _metric_dicts(payload):
    """Every ``{value, status, ...}`` metric anywhere in ``payload``."""
    for path, d in _walk(payload):
        if "status" in d and "value" in d and d.get("status") in _PUBLICATION_STATUSES:
            yield path, d


#: PR-ADS-167A — pages whose window ends at yesterday when it includes today
#: (owner decision 2026-11-20). Their figures are judged against the canonical
#: verdict for THOSE days (``load_won_deals(..., through=...)``), not the
#: window that includes today.
SETTLED_DAY_PAGES = frozenset({"revenue_decision_mart_country",
                               "dashboard_countries",
                               # PR-ADS-177C (M2): the Campaigns page's settled
                               # campaign view, Dashboard → Campaigns (which
                               # reads it since PR-ADS-177B) and the Meta ad-set
                               # table end at yesterday too.
                               "revenue_decision_mart_campaign_settled",
                               "dashboard_campaigns", "meta_ads_adsets"})


def settled_publication(key: str, now) -> dict | None:
    """The canonical publication over the settled-day window of ``key``, or
    None when that window is the full one (closed)."""
    from services import canonical_revenue_service as crs  # noqa: PLC0415
    from services.canonical_contract import (  # noqa: PLC0415
        resolve_settled_window, settled_revenue_bounds,
    )
    settled = resolve_settled_window(key, now=now)
    if not settled.get("through_reason"):
        return None
    base = crs.load_won_deals(key, now=now, bounds=settled_revenue_bounds(settled))
    return base.get("publication") or {}


def settled_mart_checks(a: Audit, key: str, now, mart: dict) -> dict:
    """PR-ADS-177C (M2) — ``windows_reconcile`` for the Campaigns page's
    SETTLED campaign view: its summary equals the canonical reader over the
    SAME settled days (``load_won_deals(bounds=settled_revenue_bounds)``),
    figure for figure, and a figure the canonical reader withholds is absent.
    A window with no settled day publishes nothing."""
    from services import canonical_revenue_service as crs  # noqa: PLC0415
    from services.canonical_contract import (  # noqa: PLC0415
        resolve_settled_window, settled_revenue_bounds,
    )
    s = (mart or {}).get("summary") or {}
    settled = resolve_settled_window(key, now=now)
    if (settled.get("window_range") or {}).get("empty"):
        for field in ("closed_won_deals", "customers", "won_revenue_usd"):
            a.check("windows_reconcile", s.get(field) is None,
                    f"{key} campaign_settled: {field}={s.get(field)} over no settled day")
        return {"settled": "no_settled_day"}
    base = crs.load_won_deals(key, now=now, bounds=settled_revenue_bounds(settled))
    row = crs.reader_row(base, base.get("deals") or [])
    out = {}
    for field, m in (("closed_won_deals", "closed_won_deals"),
                     ("customers", "customers"), ("won_revenue_usd", "revenue_usd")):
        canon = (row.get("metrics") or {}).get(m) or {}
        out[field] = canon.get("value")
        if canon.get("status") == "published":
            mine, theirs = s.get(field), canon.get("value")
            same = (mine == theirs if not isinstance(theirs, float)
                    else mine is not None and abs(float(mine) - theirs) < 0.005)
            a.check("windows_reconcile", same,
                    f"{key} campaign_settled: mart {field}={mine} != canonical over "
                    f"the settled days {theirs}")
        else:
            a.check("withheld_values_absent", s.get(field) is None,
                    f"{key} campaign_settled: {field}={s.get(field)} while the "
                    f"settled days' {m} is {canon.get('status')}")
    return out


def window_checks(a: Audit, key: str, universe: dict, truth_w: dict, base: dict,
                  mart: dict | None, *, now, pages: dict | None = None,
                  page_pubs: dict | None = None,
                  window_type: str = "business") -> dict:
    from services import canonical_revenue_service as crs  # noqa: PLC0415

    pub = truth_w.get("publication") or {}
    exp = independent_members(universe, key, now, window_type)
    # 6 — the reader's verdicts are the canonical ones.
    for m in ("closed_won_deals", "revenue_usd", "customers"):
        mine = (base.get("publication") or {}).get(m) or {}
        a.check("windows_reconcile",
                mine.get("status") == (pub.get(m) or {}).get("status")
                and mine.get("reason") == (pub.get(m) or {}).get("reason"),
                f"{key}.{m}: reader {mine} != canonical {pub.get(m)}")
    truth_ids = (truth_w.get("membership") or {}).get("deal_ids")
    if base.get("available"):
        reader_ids = sorted(str(d["deal_id"]) for d in base.get("deals") or [])
        a.check("windows_reconcile",
                truth_ids is not None and reader_ids == sorted(map(str, truth_ids)),
                f"{key}: reader members {len(reader_ids)} != canonical "
                f"{None if truth_ids is None else len(truth_ids)}")
        a.check("windows_reconcile", reader_ids == exp["members"],
                f"{key}: reader members {len(reader_ids)} != independent "
                f"{len(exp['members'])}")
    else:
        a.check("windows_reconcile",
                (pub.get("closed_won_deals") or {}).get("status") != "published",
                f"{key}: reader released no population but the canonical "
                "deal count is published")
    # 13 — All Time holds the undated deals and no future-dated one.
    if exp["all_time"]:
        mem = truth_w.get("membership") or {}
        a.check("all_time_undated_and_no_future",
                mem.get("undated_included") is True
                and mem.get("undated_won_deals") == exp["undated"],
                f"all_time undated: canonical {mem.get('undated_won_deals')} "
                f"(included={mem.get('undated_included')}) != independent "
                f"{exp['undated']}")
        if base.get("available"):
            a.check("all_time_undated_and_no_future",
                    base.get("undated_won_deals_included") == exp["undated"],
                    f"all_time reader holds {base.get('undated_won_deals_included')}"
                    f" undated deals, independent {exp['undated']}")
            late = [str(d["deal_id"]) for d in base.get("deals") or []
                    if _dt(d.get("deal_close_date")) is not None
                    and _dt(d.get("deal_close_date")) > now]
            a.check("all_time_undated_and_no_future", not late,
                    f"all_time admits future-dated deals {late[:5]}")

    # 7 / 8 — a reader row over the whole window carries status + reason, and
    # a withheld value is None.
    row = crs.reader_row(base, base.get("deals") or [], spend_usd=1.0)
    for m, d in row["metrics"].items():
        a.check("status_and_reason_serialized",
                d.get("status") in _PUBLICATION_STATUSES
                and (d.get("status") == "published" or bool(d.get("reason"))),
                f"{key}.{m}: {d}")
        if d.get("status") != "published":
            a.check("withheld_values_absent", d.get("value") is None,
                    f"{key}.{m} is {d.get('status')} with value {d.get('value')}")
    try:
        json.dumps(row["metrics"])
    except (TypeError, ValueError) as exc:
        a.check("status_and_reason_serialized", False, f"{key}: {exc}")
    # 11 — derived ratios inherit a withheld input.
    rev, cust = row["metrics"]["revenue_usd"], row["metrics"]["customers"]
    per_spend = row["metrics"].get(PERIOD_REVENUE_PER_AD_SPEND) or {}
    per_customer = row["metrics"].get(PERIOD_SPEND_PER_CUSTOMER_WON) or {}
    a.check("period_ratios_withheld_on_withheld_input",
            rev["status"] == "published" or per_spend.get("status") != "published",
            f"{key}: period revenue / ad spend published over {rev['status']} revenue")
    a.check("period_ratios_withheld_on_withheld_input",
            cust["status"] == "published" or per_customer.get("status") != "published",
            f"{key}: spend per customer won published over {cust['status']} customers")
    # 18 — the reader contract itself names no period ratio ROAS or CAC.
    _ratio_name_check(a, f"{key} reader_row", row)
    # Customers are companies, never the deal count.
    if cust["status"] == "published" and base.get("available"):
        ids = set()
        for d in base.get("deals") or []:
            ids.update(str(c) for c in (base.get("company_ids_by_deal") or {})
                       .get(str(d["deal_id"])) or [])
        a.check("customer_readers_canonical", cust["value"] == len(ids),
                f"{key}: customers {cust['value']} != distinct companies {len(ids)}")

    page = {}
    placement = base.get("campaign_placement")
    if mart is not None:
        page = mart_checks(a, key, pub, truth_w, mart)
        page_leak_checks(a, key, "revenue_decision_mart", pub, mart, placement)
        _ratio_name_check(a, f"{key} revenue_decision_mart", mart)
    for name, payload in (pages or {}).items():
        if name in PIPELINE_PAGES and isinstance(payload, dict):
            pipeline_checks(a, key, name, pub, truth_w, payload)
        page_leak_checks(a, key, name, (page_pubs or {}).get(name) or pub,
                         payload, placement)
        _ratio_name_check(a, f"{key} {name}", payload)
        if isinstance(payload, dict) and "meta_ads_line" in payload:
            meta_line_checks(a, f"{key} {name}", payload["meta_ads_line"])
    return {"window": key, "publication": {m: pub.get(m) for m in
                                           ("closed_won_deals", "revenue_usd",
                                            "customers")},
            "outcomes": {m: (truth_w.get("outcomes") or {}).get(m) for m in
                         ("closed_won_deals", "revenue_usd", "customers")},
            "independent_members": len(exp["members"]),
            "undated_won_deals": exp["undated"],
            "future_dated_won_deals": exp["future"], "page": page}


#: Keys that carry a figure of one canonical metric family, wherever they sit
#: in a page payload. ``known_revenue_usd`` — the named subset of priced deals,
#: labelled as such (PR-ADS-155) — is deliberately not a total key.
_FAMILY_KEYS = {
    "customers": ("customers", "attributed_customers", "google_ads_source_customers",
                  "closed_won_customers", "total_customers"),
    "closed_won_deals": ("closed_won_deals", "attributed_closed_won_deals",
                         "google_ads_source_closed_won_deals",
                         "total_closed_won_deals", "won_deals", "deals_won"),
    "revenue_usd": ("won_revenue_usd", "won_revenue", "closed_won_revenue_usd",
                    "total_revenue_usd", "attributed_won_revenue_usd",
                    "google_ads_source_won_revenue_usd"),
}
#: A row metric name → the sibling field that must be absent with it.
_SIBLING = {"closed_won_deals": ("closed_won_deals",),
            "customers": ("customers", "closed_won_customers"),
            "revenue_usd": ("won_revenue", "won_revenue_usd",
                            "closed_won_revenue_usd")}


def _is_figure(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


#: Row keys that name a campaign; rows that name the NOT-placed bucket are
#: not per-campaign figures.
_CAMPAIGN_KEYS = ("campaign_name", "campaign_id", "campaign")
_NOT_A_CAMPAIGN = ("unattributed", "unclassified", "needs review", "no campaign")


def _per_campaign_row(d: dict) -> bool:
    name = next((d.get(k) for k in _CAMPAIGN_KEYS if d.get(k) is not None), None)
    if name is None:
        return False
    return not any(p in str(name).lower() for p in _NOT_A_CAMPAIGN)


def page_leak_checks(a: Audit, key: str, page: str, pub: dict, payload,
                     placement: dict | None = None) -> None:
    """8 / 9 over ONE page payload.

    * every ``{value, status}`` metric that is not published has no value;
    * a row whose metric is not published carries no sibling figure for it,
      and no proof list beside a withheld deal count;
    * no identity map anywhere;
    * when the WINDOW withholds a family, no figure of that family appears
      anywhere on the page — no row, subset, bucket or total.
    """
    for path, d in _metric_dicts(payload):
        if d.get("status") != "published":
            a.check("withheld_values_absent", d.get("value") is None,
                    f"{key} {page}{path} is {d.get('status')} with value "
                    f"{d.get('value')}")
    leaks = []
    withheld = {fam for fam in _FAMILY_KEYS
                if (pub.get(fam) or {}).get("status") != "published"}
    for path, d in _walk(payload):
        if "company_ids_by_deal" in d:
            leaks.append(f"{path}.company_ids_by_deal")
        metrics = d.get("metrics") if isinstance(d.get("metrics"), dict) else {}
        for m, siblings in _SIBLING.items():
            mm = metrics.get(m)
            if isinstance(mm, dict) and mm.get("status") not in (None, "published"):
                for sib in siblings:
                    if _is_figure(d.get(sib)):
                        leaks.append(f"{path}.{sib}={d.get(sib)} beside a "
                                     f"{mm.get('status')} {m}")
                # PR-ADS-176A round 2: ANY list — [] included, which says "no
                # deals" — beside a withheld count is a leak.
                if m == "closed_won_deals" and d.get("deals") is not None:
                    leaks.append(f"{path}.deals={d.get('deals')!r} (proof list) "
                                 "beside a withheld deal count")
                # PR-ADS-176A truth audit MINOR 5: `deal_ids` is a proof list
                # too. Beside a withheld count it is None — an empty list says
                # "no deals", which nobody counted.
                if m == "closed_won_deals" and d.get("deal_ids") is not None:
                    leaks.append(f"{path}.deal_ids={d.get('deal_ids')!r} (proof "
                                 "list) beside a withheld deal count")
        for proof in ("deal_ids", "deals"):
            if (proof in d and d.get(proof) is not None
                    and "closed_won_deals" in d and d.get("closed_won_deals") is None
                    and not isinstance(metrics.get("closed_won_deals"), dict)):
                # A row withheld wholesale (no metrics block): same rule.
                leaks.append(f"{path}.{proof}={d.get(proof)!r} beside a "
                             "withheld deal count")
        for fam in withheld:
            for k in _FAMILY_KEYS[fam]:
                if _is_figure(d.get(k)):
                    leaks.append(f"{path}.{k}={d.get(k)} while the window's "
                                 f"{fam} is {(pub.get(fam) or {}).get('status')}")
        # The canonical placement verdict withholds every PER-CAMPAIGN figure
        # (a Google Ads won deal resolves to no campaign); scope totals and the
        # not-placed bucket are not per-campaign.
        if (placement or {}).get("status") not in (None, "published") \
                and _per_campaign_row(d):
            for k in (*_FAMILY_KEYS["closed_won_deals"], *_FAMILY_KEYS["revenue_usd"],
                      *_FAMILY_KEYS["customers"], "deals"):
                if _is_figure(d.get(k)):
                    leaks.append(f"{path}.{k}={d.get(k)} on a campaign row while "
                                 f"per-campaign placement is {placement.get('status')}")
    a.check("withheld_not_reconstructable", not leaks,
            f"{key} {page}: {leaks[:6]}")


def mart_checks(a: Audit, key: str, pub: dict, truth_w: dict, mart: dict) -> dict:
    """The page every KPI reads: the Revenue Decision Mart summary."""
    s = mart.get("summary") or {}
    out = truth_w.get("outcomes") or {}
    pairs = (("closed_won_deals", "closed_won_deals"),
             ("customers", "customers"), ("won_revenue_usd", "revenue_usd"))
    for field, m in pairs:
        p = (pub.get(m) or {}).get("status")
        if p == "published":
            a.check("windows_reconcile", s.get(field) == out.get(m),
                    f"{key}: mart {field}={s.get(field)} != canonical {out.get(m)}")
        else:
            a.check("withheld_values_absent", s.get(field) is None,
                    f"{key}: mart {field}={s.get(field)} while {m} is {p}")
    for m in ("closed_won_deals", "revenue_usd", "customers"):
        mine = (s.get("metrics") or {}).get(m) or {}
        canon = pub.get(m) or {}
        a.check("status_and_reason_serialized",
                mine.get("status") == canon.get("status")
                and (canon.get("status") == "published"
                     or mine.get("reason") == canon.get("reason")),
                f"{key}: mart metrics.{m} {mine.get('status')}/"
                f"{mine.get('reason')} != canonical {canon}")
    a.check("status_and_reason_serialized",
            s.get("customers_status") == (pub.get("customers") or {}).get("status")
            and s.get("customers_reason") == (pub.get("customers") or {}).get("reason"),
            f"{key}: mart customers verdict {s.get('customers_status')}/"
            f"{s.get('customers_reason')} != canonical {pub.get('customers')}")
    for path, d in _metric_dicts(mart):
        if d.get("status") != "published":
            a.check("withheld_values_absent", d.get("value") is None,
                    f"{key}{path} is {d.get('status')} with value {d.get('value')}")
    return {"closed_won_deals": s.get("closed_won_deals"),
            "customers": s.get("customers"),
            "customers_status": s.get("customers_status"),
            "won_revenue_usd": s.get("won_revenue_usd")}


#: PR-ADS-178B — pages built by ``build_pipeline`` (through today).
PIPELINE_PAGES = frozenset({"pipeline", "dashboard_deals"})


def pipeline_checks(a: Audit, key: str, page: str, pub: dict, truth_w: dict,
                    payload: dict) -> None:
    """6 / 7 / 8 over a Pipeline payload: every KPI is the canonical verdict
    (published → the canonical value; otherwise no value and the canonical
    status and reason), and the deal list is exactly the window's members —
    or absent beside a count that is not published."""
    out = truth_w.get("outcomes") or {}
    kpis = payload.get("kpis") or {}
    for field, m in (("closed_won_deals", "closed_won_deals"),
                     ("customers", "customers"), ("won_revenue_usd", "revenue_usd")):
        mine = kpis.get(field) or {}
        canon = pub.get(m) or {}
        if canon.get("status") == "published":
            a.check("windows_reconcile",
                    mine.get("status") == "published" and mine.get("value") == out.get(m),
                    f"{key} {page}: kpis.{field}={mine.get('value')} "
                    f"({mine.get('status')}) != canonical {out.get(m)}")
        else:
            a.check("withheld_values_absent", mine.get("value") is None,
                    f"{key} {page}: kpis.{field}={mine.get('value')} while {m} "
                    f"is {canon.get('status')}")
            a.check("status_and_reason_serialized",
                    mine.get("status") == canon.get("status")
                    and mine.get("reason") == canon.get("reason"),
                    f"{key} {page}: kpis.{field} {mine.get('status')}/"
                    f"{mine.get('reason')} != canonical {canon}")
    deals = payload.get("deals")
    canon_deals = pub.get("closed_won_deals") or {}
    if canon_deals.get("status") == "published":
        truth_ids = (truth_w.get("membership") or {}).get("deal_ids")
        got = None if deals is None else sorted(str(d.get("deal_id")) for d in deals)
        a.check("windows_reconcile",
                got is not None and truth_ids is not None
                and got == sorted(map(str, truth_ids)),
                f"{key} {page}: deal list {None if got is None else len(got)} != "
                f"canonical members {None if truth_ids is None else len(truth_ids)}")
    else:
        a.check("withheld_not_reconstructable", deals is None,
                f"{key} {page}: a deal list beside a {canon_deals.get('status')} "
                "deal count")


#: The tables a closed-won read could touch; fingerprinted before and after.
_FINGERPRINT_TABLES = ("hubspot_deal_ledger", "hubspot_deal_company_association",
                       "hubspot_deal_company_observation",
                       "hubspot_deal_company_sync_state", "sync_batches")


def _write_fingerprint():
    """``{table: (rows, md5)}`` — or None when it cannot be read."""
    try:
        from db.connection import get_conn  # noqa: PLC0415
        out = {}
        with get_conn() as conn:
            if conn is None:
                return None
            cur = conn.cursor()
            for t in _FINGERPRINT_TABLES:
                cur.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' "
                            f"ORDER BY x::text), '')) FROM {t} x")
                out[t] = tuple(cur.fetchone())
            conn.rollback()
        return out
    except Exception:  # noqa: BLE001
        return None


def _page_builders():
    """Every production page that publishes a closed-won figure, as
    ``(name, build(window, now))``. All read-only."""
    def mart(w, now):
        from services.revenue_decision_mart import build_revenue_decision_mart  # noqa: PLC0415
        return build_revenue_decision_mart(view="campaign", window=w, now=now)

    def mod(module, fn, **kw):
        def build(w, now):
            import importlib  # noqa: PLC0415
            return getattr(importlib.import_module(module), fn)(w, now=now, **kw)
        return build
    def mart_view(view):
        def build(w, now):
            from services.revenue_decision_mart import build_revenue_decision_mart  # noqa: PLC0415
            return build_revenue_decision_mart(view=view, window=w, now=now)
        return build

    def settled_campaign(w, now):
        from services.revenue_decision_mart import build_revenue_decision_mart  # noqa: PLC0415
        return build_revenue_decision_mart(view="campaign", window=w, now=now,
                                           settled=True)
    return (
        ("revenue_decision_mart", mart),
        ("revenue_decision_mart_country", mart_view("country")),
        ("revenue_attribution", mod("services.revenue_attribution_service",
                                    "build_revenue_attribution")),
        ("revenue_by_source", mod("services.source_attribution_service",
                                  "build_revenue_by_source")),
        ("dashboard_overview", mod("services.dashboard_overview_service",
                                   "build_dashboard_overview")),
        ("dashboard_revenue", mod("services.dashboard_revenue_service",
                                  "build_dashboard_revenue")),
        ("dashboard_campaigns", mod("services.dashboard_campaigns_service",
                                    "build_dashboard_campaigns")),
        ("dashboard_countries", mod("services.dashboard_countries_service",
                                    "build_dashboard_countries")),
        ("dashboard_deals", mod("services.dashboard_deals_service",
                                "build_dashboard_deals")),
        # PR-ADS-178B: the Pipeline page (through today, like Deals): checks 8,
        # 9 and 18 walk it, and pipeline_checks reconciles its KPIs and deal
        # list with the canonical truth (windows_reconcile).
        ("pipeline", mod("services.pipeline_service", "build_pipeline")),
        # PR-ADS-177C (M2): the Campaigns page reads the mart's SETTLED
        # campaign view, and shows the Meta ad-set table beside it. Both were
        # outside check 18, the leak checks and windows_reconcile.
        ("revenue_decision_mart_campaign_settled", settled_campaign),
        ("meta_ads_adsets", mod("services.meta_ads_adset_table_service",
                                "build_meta_adset_table")),
    )


#: PR-ADS-168A — the evidence windows the Channels page offers. The reader
#: contract is checked for each (``load_won_deals(k, window_type="evidence")``
#: against the truth service and the independent restatement), and the page
#: builders below run on it so checks 8, 9 and 18 cover them.
EVIDENCE_PASS_WINDOWS = ("30d", "60d", "180d", "all_time")


def _evidence_page_builders():
    """Production pages that publish closed-won figures over an EVIDENCE
    window, as ``(name, build(window, now))``. All read-only. Each is a
    settled-day page: its open window ends yesterday."""
    def channel_outcomes(w, now):
        from services.source_attribution_service import build_channel_outcomes  # noqa: PLC0415
        return build_channel_outcomes(w, now=now)
    return (("channel_outcomes", channel_outcomes),)


def settled_evidence_publication(key: str, now) -> dict:
    """The canonical publication over an evidence window ended at yesterday
    (the days the settled-day evidence pages publish)."""
    from services import canonical_revenue_service as crs  # noqa: PLC0415
    from services.canonical_contract import (  # noqa: PLC0415
        resolve_settled_evidence_window, settled_revenue_bounds,
    )
    settled = resolve_settled_evidence_window(key, now=now)
    base = crs.load_won_deals(key, window_type="evidence", now=now,
                              bounds=settled_revenue_bounds(settled))
    return base.get("publication") or {}


def pure_checks(a: Audit) -> None:
    """11 / 8 / 18 on the contract functions themselves, independent of data."""
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    withheld = cwt.metric(None, cwt.WITHHELD, cwt.R_CUSTOMER_IDENTITY)
    per_spend = cwt.derive_period_revenue_per_ad_spend(withheld, 100.0)
    per_customer = cwt.derive_period_spend_per_customer_won(withheld, 100.0)
    a.check("period_ratios_withheld_on_withheld_input",
            per_spend["status"] == cwt.WITHHELD and per_spend["value"] is None
            and per_customer["status"] == cwt.WITHHELD
            and per_customer["value"] is None,
            f"derived over a withheld input: {per_spend} {per_customer}")
    zero = cwt.derive_period_revenue_per_ad_spend(cwt.metric(0.0, cwt.PUBLISHED), 100.0)
    a.check("zero_distinguishable",
            zero["status"] == cwt.PUBLISHED and zero["value"] == 0.0,
            f"a published zero revenue must give a published 0 ratio: {zero}")
    # 18 — a published period ratio declares itself non-cohort, and certified
    # ROAS / CAC are unavailable whatever the inputs.
    for d in (zero, cwt.derive_period_spend_per_customer_won(
            cwt.metric(4, cwt.PUBLISHED), 100.0)):
        a.check("no_period_ratio_named_roas_or_cac",
                d.get("status") == cwt.PUBLISHED
                and d.get("ratio_kind") == RATIO_KIND_OPERATIONAL
                and d.get("cohort_basis") == PERIOD_COHORT_BASIS
                and all(p in (d.get("disclosure") or "") for p in DISCLOSURE_PHRASES),
                f"a published period ratio lacks its non-cohort disclosure: {d}")
    for d in (cwt.certified_roas(), cwt.certified_cac()):
        a.check("no_period_ratio_named_roas_or_cac",
                d.get("status") == cwt.UNAVAILABLE and d.get("value") is None
                and bool(d.get("reason")),
                f"certified ROAS / CAC must be unavailable with a reason: {d}")
    a.check("no_period_ratio_named_roas_or_cac",
            not any(hasattr(cwt, n) for n in ("derive_roas", "derive_cac")),
            "closed_won_truth still exports derive_roas / derive_cac")
    leaked = cwt.metric(5, cwt.WITHHELD, "x")
    a.check("withheld_values_absent", leaked["value"] is None,
            "metric() kept a value under a withheld status")
    rate = cwt.incompatible_rate(cwt.R_SQL_TO_CUSTOMER_INCOMPATIBLE)
    a.check("sql_to_customer_not_mixed",
            rate["value"] is None and rate["status"] == cwt.UNAVAILABLE,
            f"incompatible rate published: {rate}")


# ── 18: a close-date period ratio is never presented as ROAS or CAC ──────────

#: The two published close-date ratios, by their only permitted names.
PERIOD_REVENUE_PER_AD_SPEND = "period_revenue_per_ad_spend"
PERIOD_SPEND_PER_CUSTOMER_WON = "period_spend_per_customer_won"
PERIOD_RATIO_KEYS = (PERIOD_REVENUE_PER_AD_SPEND, PERIOD_SPEND_PER_CUSTOMER_WON)
PERIOD_COHORT_BASIS = "close_date_outcomes_over_same_window_spend"
RATIO_KIND_OPERATIONAL = "non_cohort_operational_ratio"
#: The disclosure every surface states beside a period ratio.
DISCLOSURE_PHRASES = ("Non-cohort operational ratio",
                      "Closed-won outcomes may originate from earlier advertising periods")
#: The visible labels of the two period ratios.
PERIOD_RATIO_LABELS = ("Period Revenue / Ad Spend", "Spend per Customer Won This Period")
#: A key (or a row's ``key``/``metric`` value) that names ROAS or CAC, after
#: camelCase is split: ``roas``, ``geo_roas``, ``geoRoas``, ``cac``,
#: ``ltv_to_cac``, ``return_on_ad_spend``, ``customer_acquisition_cost`` …
_RATIO_NAME = re.compile(
    r"(?:^|_)(?:roas|cac|return_on_ad_spend|customer_acquisition_cost)(?:$|_)")
#: ROAS / CAC named in prose — a payload string or UI text. Case variants are
#: explicit so a JS identifier (``campRoasCell``, ``r.roas``) is not prose.
_RATIO_WORD = re.compile(r"\b(?:ROAS|Roas|CAC|Cac)\b"
                         r"|(?i:return on ad spend|customer acquisition cost)")
#: The status words a certified ROAS / CAC may carry.
_CERTIFIED_STATUSES = ("unavailable", "not_published")
#: Exact texts that may name ROAS / CAC. Each SAYS the metric is unavailable
#: (or is LTV/CAC, a recurring-revenue metric that is always withheld). Only the
#: token is exempt — the rest of its line or string is still scanned.
_ALLOWED_RATIO_TEXT = (
    "Unavailable: attribution ROAS requires revenue and spend over one acquisition cohort.",
    "attribution ROAS needs revenue and spend over one acquisition cohort",
    "CAC needs spend and customers over one acquisition cohort",
    "LTV/CAC", "LTV / CAC",
)
#: The UI's certified labels: defined once, rendered only beside ``roas``/``cac``.
_CERTIFIED_LABELS_LINE = ('const CERTIFIED_RATIO_LABELS = { roas: "Attribution ROAS '
                          '(acquisition cohort)", cac: "CAC (acquisition cohort)" };')


def _ratio_named(key) -> bool:
    if not isinstance(key, str):
        return False
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    return bool(_RATIO_NAME.search(snake))


def _names_ratio_in_text(text: str) -> bool:
    for allowed in _ALLOWED_RATIO_TEXT:
        text = text.replace(allowed, "")
    return bool(_RATIO_WORD.search(text))


def _leaves(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, value


def ratio_name_violations(payload) -> list:
    """Every place in ``payload`` that presents a figure as ROAS or CAC.

    * a key naming ROAS / CAC (any case, camelCase, or a synonym) that carries
      a number, a string with a digit, or a container holding any number
      anywhere — or a metric under it whose status is not unavailable;
    * a row that names ROAS / CAC by value (``{"key": "roas", "value": 2.4}``)
      and carries a number;
    * any payload text that calls something ROAS or CAC, except the exact
      certified-unavailable phrasings and LTV/CAC;
    * a published close-date ratio (``cohort_basis``) under any other name, or
      without its ``ratio_kind`` and disclosure.
    """
    out = []
    for path, d in _walk(payload):
        for k, v in d.items():
            where = f"{path}.{k}"
            if _ratio_named(k):
                if _is_figure(v):
                    out.append(f"{where}={v}")
                elif isinstance(v, str) and re.search(r"\d", v):
                    out.append(f"{where}={v!r}")
                elif isinstance(v, (dict, list, tuple)):
                    if isinstance(v, dict) and "status" in v \
                            and v.get("status") not in _CERTIFIED_STATUSES:
                        out.append(f"{where} has status {v.get('status')!r}")
                    figures = [q for q, x in _leaves(v) if _is_figure(x)]
                    if figures:
                        out.append(f"{where} carries a figure at {figures[:3]}")
        for name_key in ("key", "metric", "name", "id"):
            if _ratio_named(d.get(name_key)):
                rest = {kk: vv for kk, vv in d.items() if kk != name_key}
                figures = [q for q, x in _leaves(rest) if _is_figure(x)]
                if figures:
                    out.append(f"{path} names {d.get(name_key)!r} and carries "
                               f"a figure at {figures[:3]}")
    # Every string, wherever it sits (dict values and list items alike).
    for where, v in _leaves(payload):
        if isinstance(v, str) and " " in v and _names_ratio_in_text(v):
            out.append(f"{where}: text names ROAS/CAC: {v[:90]!r}")
    for path, d in _metric_dicts(payload):
        if d.get("status") != "published" or d.get("cohort_basis") != PERIOD_COHORT_BASIS:
            continue
        name = path.rsplit(".", 1)[-1]
        if name not in PERIOD_RATIO_KEYS:
            out.append(f"{path}: a close-date period ratio named {name!r}")
        if d.get("ratio_kind") != RATIO_KIND_OPERATIONAL or not all(
                p in (d.get("disclosure") or "") for p in DISCLOSURE_PHRASES):
            out.append(f"{path}: a period ratio without its non-cohort disclosure")
    return out


def _ratio_name_check(a: Audit, where: str, payload) -> None:
    bad = ratio_name_violations(payload)
    a.check("no_period_ratio_named_roas_or_cac", not bad, f"{where}: {bad[:6]}")


def _strip_comments(src: str) -> str:
    """Block comments, HTML comments and WHOLE-LINE ``//`` comments only. A
    trailing ``//`` is left in place: it may sit inside a string, and a
    comment that names ROAS beside code is cheap to reword."""
    keep_lines = lambda m: "\n" * m.group(0).count("\n")  # noqa: E731
    src = re.sub(r"/\*.*?\*/", keep_lines, src, flags=re.S)
    src = re.sub(r"<!--.*?-->", keep_lines, src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def ui_ratio_label_violations(js: str, html: str = "") -> list:
    """Every visible ROAS / CAC word in the UI that is not an exact certified-
    unavailable text, a certified label rendered beside anything but its own
    metric, plus a missing period-ratio label or disclosure."""
    out = []
    for name, src in (("app.js", js), ("index.html", html)):
        for i, line in enumerate(_strip_comments(src).splitlines(), 1):
            text = line.replace(_CERTIFIED_LABELS_LINE, "")
            if _names_ratio_in_text(text):
                out.append(f"{name}:{i}: {line.strip()[:100]}")
    if js.count(_CERTIFIED_LABELS_LINE) != 1:
        out.append("app.js: CERTIFIED_RATIO_LABELS must be defined exactly once, "
                   "with only the two certified labels")
    for i, line in enumerate(js.splitlines(), 1):
        for metric in re.findall(r"CERTIFIED_RATIO_LABELS\.(\w+)", line):
            if metric not in ("roas", "cac") or f'"{metric}"' not in line \
                    or any(k in line for k in PERIOD_RATIO_KEYS):
                out.append(f"app.js:{i}: CERTIFIED_RATIO_LABELS.{metric} is not "
                           f"rendered beside its own certified metric")
    for text in (*PERIOD_RATIO_LABELS, *DISCLOSURE_PHRASES):
        if text not in js:
            out.append(f"app.js: missing {text!r}")
    return out


def run(now: datetime | None = None, *, pages: bool = True) -> tuple[Audit, dict]:
    from analysis import closed_won_truth as cwt  # noqa: PLC0415
    from analysis.business_windows import WINDOW_KEYS  # noqa: PLC0415
    from services import canonical_customer_revenue_service as svc  # noqa: PLC0415
    from services import canonical_revenue_service as crs  # noqa: PLC0415
    from services.snapshot_memo import (  # noqa: PLC0415
        certification_window, pinning_summary, refused_reads, window_snapshot,
    )

    now = now or datetime.now(tz=timezone.utc)
    # PR-ADS-179B: whether each window was read inside its pinned snapshot.
    pinning: list = []
    a = Audit()
    structural = structural_checks(a)
    pure_checks(a)
    report = {"audit": "customer_revenue_reader_cutover",
              "generated_at": now.isoformat(),
              "hubspot_calls_performed": False,
              "google_ads_calls_performed": False,
              "external_writes_performed": False,
              "database_writes_performed": None,      # measured below
              "inventory": [dict(r, families=list(r["families"])) for r in INVENTORY],
              "structural": structural, "windows": [],
              # PR-ADS-168A: evidence windows, beside (not inside) the
              # business windows — they have no mart summary.
              "evidence_windows": [],
              "pages_not_run": []}
    with crs.one_snapshot():
        universe = crs.load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID)
        report["available"] = bool(universe.get("available"))
        if not universe.get("available"):
            report.update(checks=a.checks, violations=a.violations,
                          exit_code=EXIT_VIOLATION if a.violations else EXIT_UNAVAILABLE,
                          verdict="contract_broken" if a.violations else "unavailable",
                          detail=universe.get("reason") or universe.get("error"))
            return a, report
        truth = svc.get_closed_won_truth(
            [("business", k) for k in WINDOW_KEYS], now=now, universe=universe)
        by_key = {w["window"]["window_key"]: w for w in truth["windows"]}
        before = _write_fingerprint()
        # PR-ADS-179A: each window is read inside ONE snapshot
        # (``services.snapshot_memo.certification_window``): its pages, its
        # reader-contract read and its settled publication read the database
        # as of one instant, and the inputs the pages share (the mart core, the
        # won-deal population) are built once per window. The closed-won
        # universe is NOT read in that snapshot: it is the one read above,
        # outside every window, and shared with the truth service (the outer
        # one_snapshot) — as before 179A.
        # PR-ADS-179A-F1: this certifies a snapshot-consistent build of each
        # page; a web request reads per statement (READ COMMITTED), so a torn
        # read inside one request is invisible here by construction. A read the
        # snapshot refused, even one a reader swallowed, is a page not run.
        # PR-ADS-179B: a window that could not pin its snapshot runs unpinned
        # (as before 179A); each window's ``snapshot`` and the report's
        # ``snapshot_pinning`` say so. The exit code does not change.
        for key in WINDOW_KEYS:
            disclosure: dict = {}
            with certification_window(disclosure) as scope:
                base = crs.load_won_deals(key, now=now)
                mart, built = None, {}
                if pages:
                    for name, build in _page_builders():
                        try:
                            payload = build(key, now)
                        except Exception as exc:  # noqa: BLE001
                            report["pages_not_run"].append(
                                {"window": key, "page": name,
                                 "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
                            continue
                        if name == "revenue_decision_mart":
                            mart = payload
                        else:
                            built[name] = payload
                settled_pub = (settled_publication(key, now)
                               if any(n in SETTLED_DAY_PAGES for n in built) else None)
                checked = window_checks(
                    a, key, universe, by_key[key], base, mart, now=now, pages=built,
                    page_pubs=({n: settled_pub for n in built if n in SETTLED_DAY_PAGES}
                               if settled_pub is not None else None))
                if "revenue_decision_mart_campaign_settled" in built:
                    checked["campaign_settled"] = settled_mart_checks(
                        a, key, now, built["revenue_decision_mart_campaign_settled"])
                report["windows"].append(checked)
            # PR-ADS-179B-F1: read once the window's reads are done.
            checked["snapshot"] = window_snapshot(scope, disclosure)
            pinning.append((key, checked["snapshot"]))
            report["pages_not_run"] += [
                {"window": key, "page": "pinned_snapshot",
                 "detail": f"a read in this window was refused by its pinned "
                           f"snapshot: {r}"[:300]} for r in refused_reads(scope)]
        # PR-ADS-168A — the evidence windows (Channels page outcomes).
        ev_truth = svc.get_closed_won_truth(
            [("evidence", k) for k in EVIDENCE_PASS_WINDOWS], now=now,
            universe=universe)
        ev_by_key = {w["window"]["window_key"]: w for w in ev_truth["windows"]}
        for key in EVIDENCE_PASS_WINDOWS:
            disclosure = {}
            with certification_window(disclosure) as scope:
                base = crs.load_won_deals(key, window_type="evidence", now=now)
                built = {}
                if pages:
                    for name, build in _evidence_page_builders():
                        try:
                            built[name] = build(key, now)
                        except Exception as exc:  # noqa: BLE001
                            report["pages_not_run"].append(
                                {"window": f"evidence/{key}", "page": name,
                                 "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
                settled_pub = (settled_evidence_publication(key, now)
                               if built else None)
                checked = window_checks(
                    a, key, universe, ev_by_key[key], base, None, now=now,
                    pages=built, window_type="evidence",
                    page_pubs={n: settled_pub for n in built} if built else None)
                checked["window"] = f"evidence/{key}"
                report["evidence_windows"].append(checked)
            checked["snapshot"] = window_snapshot(scope, disclosure)
            pinning.append((f"evidence/{key}", checked["snapshot"]))
            report["pages_not_run"] += [
                {"window": f"evidence/{key}", "page": "pinned_snapshot",
                 "detail": f"a read in this window was refused by its pinned "
                           f"snapshot: {r}"[:300]} for r in refused_reads(scope)]
        after = _write_fingerprint()
    report["snapshot_pinning"] = pinning_summary(pinning)
    # 16 — measured, not declared: the closed-won tables are byte-identical.
    report["database_writes_performed"] = (
        None if before is None or after is None else before != after)
    a.check("no_external_calls_no_writes",
            report["database_writes_performed"] is not True,
            "a closed-won table changed while the audit ran")
    if before is None or after is None:
        report["pages_not_run"].append(
            {"window": None, "page": "write_fingerprint",
             "detail": "the closed-won tables could not be fingerprinted"})
    report["checks"] = a.checks
    report["violations"] = a.violations
    if a.violations:
        report["exit_code"], report["verdict"] = EXIT_VIOLATION, "contract_broken"
    elif report["pages_not_run"]:
        report["exit_code"], report["verdict"] = EXIT_UNAVAILABLE, "unavailable"
    else:
        report["exit_code"], report["verdict"] = EXIT_OK, "contracts_hold"
    return a, report


def _fmt(v, money=False):
    if v is None:
        return "—"
    return f"{v:,.2f}" if money else str(v)


def _render(report: dict) -> None:
    print("=" * 96)
    print("  PR-ADS-161E — CUSTOMER / REVENUE READER CUTOVER AUDIT (read-only)")
    print(f"  verdict: {report.get('verdict')}    HubSpot calls: False    "
          f"external writes: False    database writes: "
          f"{report.get('database_writes_performed')} (measured)")
    print("=" * 96)
    print(f"\n  {'window':<18}{'won deals':>10}{'customers':>11}{'revenue USD':>16}"
          "  deals / revenue / customers")
    for w in (report.get("windows") or []) + (report.get("evidence_windows") or []):
        pub, out = w["publication"], w["outcomes"]
        st = " / ".join(f"{(pub.get(m) or {}).get('status')}"
                        f"{'' if (pub.get(m) or {}).get('status') == 'published' else ' (' + str((pub.get(m) or {}).get('reason')) + ')'}"
                        for m in ("closed_won_deals", "revenue_usd", "customers"))
        print(f"  {w['window']:<18}{_fmt(out.get('closed_won_deals')):>10}"
              f"{_fmt(out.get('customers')):>11}"
              f"{_fmt(out.get('revenue_usd'), True):>16}  {st}")
    print("\n  CHECKS")
    for name, ok in sorted((report.get("checks") or {}).items()):
        print(f"    {'PASS' if ok else 'FAIL'}  {name}")
    if report.get("snapshot_pinning"):
        print(f"\n  SNAPSHOT: {report['snapshot_pinning'].get('note')}")
    if report.get("pages_not_run"):
        print("\n  PAGE CHECKS NOT RUN (exit 2 — a check that did not run proves nothing)")
        for p in report["pages_not_run"]:
            print(f"    - {p['window']}: {p['detail']}")
    if report.get("violations"):
        print("\n  CONTRACT VIOLATIONS")
        for v in report["violations"]:
            print(f"    - {v}")
    else:
        print("\n  Every reader contract holds. Withheld metrics are withheld for "
              "the reason shown, not hidden.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only proof that production readers use the canonical "
                    "closed-won owner")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    args = parser.parse_args(argv)

    from db.connection import ensure_database_ready  # noqa: PLC0415

    ready, error = ensure_database_ready()
    if not ready:
        payload = {"available": False, "verdict": "unavailable",
                   "detail": f"database not ready: {error}",
                   "external_writes_performed": False,
                   "database_writes_performed": False,
                   "hubspot_calls_performed": False}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database not ready: {error}")
        return EXIT_UNAVAILABLE
    try:
        _, report = run()
    except Exception as exc:  # noqa: BLE001
        payload = {"available": False, "verdict": "unavailable",
                   "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — {payload['detail']}")
        return EXIT_UNAVAILABLE
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
