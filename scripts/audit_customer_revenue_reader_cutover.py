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
     "surface": "/api/revenue-attribution*, /api/revenue-deals; Campaign Revenue & Spend / Country",
     "existing_source": "len(campaign deals), proven-amount sums",
     "existing_meaning": "per-campaign won deals as customers; partial sums",
     "canonical_replacement": "reader_row per campaign / country / summary; "
                              "campaign_placement_gate",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Won deals + customers columns; withheld rows say why",
     "families": ("customers", "revenue", "period_ratios"),
     "canonical_via": "reader_row", "status": "migrated"},
    {"consumer": "services/source_attribution_service.py",
     "surface": "/api/revenue-by-source; Revenue by Source",
     "existing_source": "bucket deal count, bucket proven sum",
     "existing_meaning": "won deals as customers; customer rate over leads",
     "canonical_replacement": "reader_row per group / channel / platform; "
                              "lead_to_customer_rate incompatible",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Customer Rate column retired; customers per bucket, not additive",
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
    {"consumer": "services/canonical_unit_economics_service.py",
     "surface": "/api/reports/unit-economics; Unit Economics",
     "existing_source": "compute_cac(spend, won_deals)",
     "existing_meaning": "CAC over won deals",
     "canonical_replacement": "reader_row: CAC and ROAS unavailable (not one "
                              "acquisition cohort); period_spend_per_customer_won "
                              "over canonical customers, withheld when customers are",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "CAC Unavailable + reason; Spend per Customer Won This Period "
                  "with the non-cohort disclosure",
     "families": ("customers", "revenue", "period_ratios"),
     "canonical_via": "reader_row", "status": "migrated"},
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
     "existing_source": "gclid_attribution.has_closed_won_deal; won deals ÷ SQLs",
     "existing_meaning": "legacy-table won flag; mixed-cohort rate",
     "canonical_replacement": "canonical won-deal contacts from the snapshot; "
                              "canonical customers; rate unavailable",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Deals and customers separate; SQL→customer Unavailable + reason",
     "families": ("customers", "revenue", "sql_to_customer"),
     "canonical_via": "load_closed_won_universe", "status": "migrated"},
    {"consumer": "services/dashboard_channels_service.py",
     "surface": "/api/dashboard/channels",
     "existing_source": "per-channel won deals summed as customers",
     "existing_meaning": "won deals as customers, summed",
     "canonical_replacement": "per-channel closed_won_deals; window customers "
                              "from the mart's canonical metric",
     "window_cohort": "business window · close-date cohort",
     "ui_effect": "Momentum / mix / matrix count won deals; Customers KPI canonical",
     "families": ("customers", "revenue", "period_ratios"),
     "canonical_via": "customers_status", "status": "migrated"},
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
     "surface": "/api/reports/roas/campaigns, /api/reports/roas/countries",
     "existing_source": "analysis.roas_calculator over local JSON",
     "existing_meaning": "won deals as customers; local-JSON revenue and ROAS",
     "canonical_replacement": "withheld (legacy_local_revenue_not_canonical); "
                              "superseded_by /api/revenue-attribution",
     "window_cohort": "n/a",
     "ui_effect": "none (no frontend consumer)",
     "families": ("customers", "revenue", "legacy_roas"),
     "canonical_via": "_LEGACY_REVENUE_WITHHELD", "status": "withheld"},
    {"consumer": "api/server.py",
     "surface": "/api/reports/roas/snapshots, /api/reports/roas/snapshots/latest",
     "existing_source": "persisted local-JSON ROAS snapshots",
     "existing_meaning": "revenue, ROAS, LTV/CAC and verdict counts",
     "canonical_replacement": "withheld: metadata only, every outcome removed",
     "window_cohort": "n/a",
     "ui_effect": "Snapshot history says Withheld + reason; links Campaign Revenue & Spend",
     "families": ("revenue", "legacy_roas", "legacy_cac"),
     "canonical_via": "_withhold_legacy_roas_snapshot", "status": "withheld"},
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
     "surface": "/api/attribution/confidence-summary",
     "existing_source": "local ROAS snapshot rows",
     "existing_meaning": "attribution tier counts only — no outcome figure",
     "canonical_replacement": "deferred: publishes no deal, customer, revenue "
                              "or ratio value",
     "window_cohort": "n/a", "ui_effect": "unchanged",
     "families": (), "canonical_via": None, "status": "deferred"},
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
    {"consumer": "scheduler/daily.py",
     "surface": "daily ROAS snapshot job",
     "existing_source": "services.roas_snapshot_service (local JSON)",
     "existing_meaning": "persists a local snapshot file",
     "canonical_replacement": "deferred: still persisted locally; every route "
                              "that serves it withholds the figures",
     "window_cohort": "n/a", "ui_effect": "none",
     "families": (), "canonical_via": None, "status": "deferred"},
    {"consumer": "static/app.js",
     "surface": "every dashboard, Campaign / Country Revenue & Spend, Revenue by "
                "Source, Unit Economics, mapping page",
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
    AUDIT_FILE,
)
#: Legacy modules no production route reads any more (the routes that did now
#: withhold). Named so a new consumer of them fails ``inventory_complete``.
RETIRED_LEGACY = (
    "analysis/roas_calculator.py", "analysis/unit_economics.py",
    "analysis/attribution_matcher.py", "analysis/gclid_readiness_audit.py",
    "services/unit_economics_service.py", "services/roas_snapshot_service.py",
    "scripts/smoke_test_roas.py", "scripts/smoke_test_roas_snapshot.py",
    "scripts/report_missing_deal_amounts.py",
)
#: A reference to any of these makes a module a closed-won reader.
READER_ENTRY_POINTS = (
    "load_won_deals", "reader_row", "subset_metrics", "load_closed_won_universe",
    "canonical_customer_revenue_service", "fetch_won_deals",
    "compute_all_campaign_roas", "compute_all_country_roas",
    "load_latest_roas_snapshot", "load_roas_snapshots",
    "run_gclid_readiness_audit", "compute_cac", "has_closed_won_deal",
    "get_revenue_snapshot", "get_scope_ladder", "canonical_deal_rows",
    # Direct SQL over the legacy closed-won tables (gclid_attribution,
    # deal_source_attribution): every repository function that reads them is
    # an entry point, so a page calling one is a reader too.
    "fetch_durable_outcome_populations", "fetch_won_revenue",
    "fetch_campaign_deal_details", "fetch_country_deal_details",
    "fetch_revenue_deals", "fetch_source_revenue", "fetch_source_revenue_daily",
    "fetch_source_deal_details", "fetch_sql_lead_details",
    "source_attribution_health_counts",
)
#: The legacy gate this PR removes, by name.
LEGACY_GATE = "revenue_integration_connected"
FORBIDDEN_IMPORTS = ("db.writers", "writers", "connectors.hubspot_pull",
                     "hubspot_pull", "connectors.google_ads_direct",
                     "google_ads_direct", "connectors.google_ads_source",
                     "connectors.hubspot_deals")
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
            present = via in _text(row["consumer"])
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
        for i, line in enumerate(_text(rel).splitlines(), 1):
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
                "services/source_attribution_service.py",
                "services/revenue_decision_mart.py"):
        for i, line in enumerate(_text(rel).splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(customers|won_deals|closed_won_deals|won_revenue|revenue)"
                         r"[^\n]*/\s*[^\n]*\b(sqls|leads)\b", code):
                mixed.append(f"{rel}:{i}")
    for rel in ("services/dashboard_revenue_service.py",
                "services/dashboard_deals_service.py"):
        if "R_SQL_TO_CUSTOMER_INCOMPATIBLE" not in _text(rel):
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
    for fn in ("get_roas_campaigns", "get_roas_countries"):
        i = server.find(f"async def {fn}(")
        region = server[i:server.find("@app.", i + 1)]
        a.check("no_external_calls_no_writes",
                "compute_all_" not in region,
                f"{fn} still computes from the legacy chain")

    # 18 — no UI text presents a period ratio as ROAS or CAC.
    ui_bad = ui_ratio_label_violations(js, _text("static/index.html"))
    a.check("no_period_ratio_named_roas_or_cac", not ui_bad, "; ".join(ui_bad[:8]))

    # 17 — the audit's derivation does not call the module it audits.
    own = _text(AUDIT_FILE)
    body = own[own.find("# ── independent derivation"):
               own.find("# ── end independent derivation")]
    a.check("independent_derivation_used",
            body and "cwt." not in body and "closed_won_truth" not in body
            and "canonical_customer_revenue_service" not in body,
            "the independent derivation must not call the audited module")
    return {"readers": readers}


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


def independent_members(universe: dict, window_key: str, now) -> dict:
    """Members of a business window, re-derived: the won flag only, an exact
    close date in ``[start, end)`` and not after ``now``; All Time also holds
    the undated won deals."""
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


def window_checks(a: Audit, key: str, universe: dict, truth_w: dict, base: dict,
                  mart: dict | None, *, now, pages: dict | None = None) -> dict:
    from services import canonical_revenue_service as crs  # noqa: PLC0415

    pub = truth_w.get("publication") or {}
    exp = independent_members(universe, key, now)
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
        page_leak_checks(a, key, name, pub, payload, placement)
        _ratio_name_check(a, f"{key} {name}", payload)
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
                if m == "closed_won_deals" and d.get("deals"):
                    leaks.append(f"{path}.deals (proof list) beside a withheld "
                                 "deal count")
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
    return (
        ("revenue_decision_mart", mart),
        ("revenue_decision_mart_source", mart_view("source")),
        ("revenue_decision_mart_country", mart_view("country")),
        ("revenue_attribution", mod("services.revenue_attribution_service",
                                    "build_revenue_attribution")),
        ("revenue_by_source", mod("services.source_attribution_service",
                                  "build_revenue_by_source")),
        ("unit_economics", mod("services.canonical_unit_economics_service",
                               "build_unit_economics")),
        ("dashboard_overview", mod("services.dashboard_overview_service",
                                   "build_dashboard_overview")),
        ("dashboard_revenue", mod("services.dashboard_revenue_service",
                                  "build_dashboard_revenue")),
        ("dashboard_channels", mod("services.dashboard_channels_service",
                                   "build_dashboard_channels")),
        ("dashboard_campaigns", mod("services.dashboard_campaigns_service",
                                    "build_dashboard_campaigns")),
        ("dashboard_countries", mod("services.dashboard_countries_service",
                                    "build_dashboard_countries")),
        ("dashboard_deals", mod("services.dashboard_deals_service",
                                "build_dashboard_deals")),
    )


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

    now = now or datetime.now(tz=timezone.utc)
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
        for key in WINDOW_KEYS:
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
            report["windows"].append(window_checks(
                a, key, universe, by_key[key], base, mart, now=now, pages=built))
        after = _write_fingerprint()
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
    for w in report.get("windows") or []:
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
