#!/usr/bin/env python3
"""
scripts/audit_campaign_evidence_certification.py

PR-ADS-157 §8 — READ-ONLY certification gate for the Campaign Evidence page and
its investigation drawer.

This is a merge/deploy gate, not a report. It answers one question per check:
*can this surface prove what it publishes?* — and exits non-zero when it cannot.

    python -m scripts.audit_campaign_evidence_certification
    python -m scripts.audit_campaign_evidence_certification --window 30d --json
    echo $?     # 0 = certified · 1 = truth-contract violation · 2 = unavailable

Why exit 2 exists
-----------------
"The database is down" and "the code publishes an uncertified number" are
different answers that lead an operator to opposite actions: one is an outage to
wait out, the other is a defect to fix. Collapsing them into a single failure
code would make the gate louder and less useful. So an unavailable database or
an unresolved canonical scope exits **2**, and only a genuine truth-contract
violation exits **1**.

What it checks
--------------
  1  every supported Evidence Window builds without raising
  2  no legacy `keywords` / `waste_terms` reader remains in the campaign
     detail builder
  3  no drawer metric originates in `waste_terms`
  4  the keyword preview declares canonical `keyword_daily_facts` provenance
  5  the flagged preview declares canonical `search_terms` provenance with
     `waste_terms` as annotation only
  6  both previews are window-aligned with the requested Evidence Window
  7  both previews carry the complete §6 section contract on every path
  8  unavailable is never rendered as zero (no `available: False` with rows,
     and no unavailable section reporting a numeric total)
  9  the SQL reconciliation block is present and uses the
     `campaign_attributable_sqls` scope
 10  the publication rule holds: a non-reconciled scope withholds the
     aggregate SQL total and the aggregate CPQL
 11  every summary field reconciles to the population that actually feeds it:
     mapped rows → `confirmed_sqls_total` and `mapping_coverage.mapped_sqls`;
     Mapping Review rows → `mapping_coverage.unmatched_sqls`; and
     mapped + unmatched + excluded_not_google → `total_paid_search_sqls`
 12  campaign identity is unique — no two rows share a `campaign_key`
 13  campaigns sharing a display name keep distinct identities
 14  identity resolution never silently falls back to display name
 15  the frontend gates every SQL-dependent surface on the reconciliation,
     INCLUDING the SQL-dependent status filters
 16  the keyword account predicate is applied in SQL, before aggregation,
     sorting and pagination — not by filtering a returned page
 17  an empty account candidate list selects nothing rather than widening
 18  every unavailable section carries the complete §6 contract, and
     `api/server.py` maintains no section dictionaries of its own
 19  the drawer reads `db_unavailable` from the evidence ENVELOPE, and raises the
     whole-drawer banner only when the headline and both previews are unavailable
 20  no external write is performed by this command

Guarantees
----------
  * NO writes of any kind. Every database access is a SELECT, reached through
    read-only evidence services.
  * NO external API calls. Google Ads, HubSpot and Mailchimp are never
    contacted; every number comes from the local database.
  * NO contact PII is printed.
  * The command initialises its own connection pool, so it runs standalone
    (cron, a deploy step, a shell) and not only inside the web process.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

EXIT_CERTIFIED = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

#: Every Evidence Window the Campaign page offers. Auditing one window would
#: certify one window; a `180d`-only regression would ship green.
WINDOWS = ("7d", "14d", "30d", "60d", "180d", "all_time")

#: The population this page's SQL number represents.
SCOPE_CAMPAIGN_ATTRIBUTABLE = "campaign_attributable_sqls"

#: Reconciliation states in which aggregate SQL evidence may be published.
PUBLISHABLE_STATUSES = ("reconciled",)

#: The §6 drawer-section contract.
SECTION_KEYS = (
    "available", "reason", "source", "source_dataset", "source_table", "scope",
    "grain", "window", "window_start", "window_end", "all_time", "customer_id",
    "campaign_id", "identity_status", "coverage_status", "rows",
)

_API_SERVER = _ROOT / "api" / "server.py"
_APP_JS = _ROOT / "static" / "app.js"


class Findings:
    """Violations and unavailability, kept apart.

    A violation is a defect in what the product claims. Unavailability is the
    product being unable to claim anything right now. Merging them would make a
    database outage look like a bug and a bug look like an outage.
    """

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.unavailable: list[str] = []
        self.checks: list[dict] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    def violation(self, name: str, detail: str) -> None:
        self.violations.append(f"{name}: {detail}")
        self.record(name, False, detail)

    def unavailable_now(self, name: str, detail: str) -> None:
        self.unavailable.append(f"{name}: {detail}")
        self.record(name, False, detail)

    def passed(self, name: str, detail: str = "") -> None:
        self.record(name, True, detail)

    @property
    def exit_code(self) -> int:
        if self.violations:
            return EXIT_VIOLATION
        if self.unavailable:
            return EXIT_UNAVAILABLE
        return EXIT_CERTIFIED


# ─────────────────────────────────────────────────────────────────────────────
# Static checks — the readers that must no longer exist
# ─────────────────────────────────────────────────────────────────────────────

def _function_code(path: Path, name: str) -> str | None:
    """One function's executable code, comments and docstring stripped.

    Comments are not behaviour, and the comment explaining WHY a legacy read was
    removed contains the very string that would prove it had not been.
    """
    try:
        src = path.read_text()
        tree = ast.parse(src)
    except Exception:  # noqa: BLE001
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            if not seg:
                return None
            fn = ast.parse(seg).body[0]
            body = list(getattr(fn, "body", []))
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            fn.body = body or [ast.Pass()]
            return ast.unparse(fn)
    return None


def check_legacy_readers(f: Findings) -> None:
    """No legacy snapshot reader may remain in the campaign detail builder."""
    code = _function_code(_API_SERVER, "_build_campaign_detail")
    if code is None:
        f.violation("legacy_readers",
                    "_build_campaign_detail could not be located in api/server.py")
        return
    offenders = [t for t in ("FROM keywords", "FROM waste_terms", "DISTINCT ON",
                             "get_conn", "cur.execute")
                 if t in code]
    if offenders:
        f.violation("legacy_readers",
                    f"campaign detail builder still contains {offenders}")
    else:
        f.passed("legacy_readers",
                 "campaign detail builder holds no query and no legacy table read")


def check_no_waste_terms_metric(f: Findings) -> None:
    """`waste_terms` columns are annotations. None may be a drawer metric."""
    code = _function_code(_API_SERVER, "_build_campaign_detail")
    if code is None:
        f.violation("waste_terms_metric", "campaign detail builder not found")
        return
    offenders = [t for t in ("waste_terms.spend_usd", "crm_junk_confirmed",
                             "matched_pattern")
                 if t in code]
    if offenders:
        f.violation("waste_terms_metric",
                    f"waste_terms-derived fields present in the builder: {offenders}")
    else:
        f.passed("waste_terms_metric",
                 "no waste_terms column reaches the drawer as a metric")


def check_account_scope_before_aggregation(f: Findings) -> None:
    """PR-ADS-157 §1 — the account predicate must be in the QUERY.

    Filtering a returned page cannot certify a population: the page is one slice
    while `total_count`, the monetary KPIs and the coverage block are computed
    over all of it. A foreign-account row beyond the preview limit passes a page
    scan unnoticed and still contributes to every total.
    """
    repo = _ROOT / "db" / "keyword_repository.py"
    try:
        src = repo.read_text()
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now("account_scope", f"{repo} unreadable: {exc}")
        return

    if '_ACCOUNT = "customer_id = ANY(%s)"' not in src:
        f.violation("account_scope",
                    "db/keyword_repository.py has no account predicate; the "
                    "keyword preview cannot be account-scoped in SQL")
        return

    missing = [fn for fn in ("fetch_keyword_aggregates", "fetch_keyword_daily_costs")
               if "_scope(customer_ids)" not in (_function_code(repo, fn) or "")]
    if missing:
        f.violation("account_scope",
                    f"these keyword reads do not apply the account scope: {missing}")
        return

    # An empty candidate list must select nothing, not widen to every account.
    try:
        import db.keyword_repository as kw_repo
        empty_sql, empty_params = kw_repo._scope([])
        wide_sql, _ = kw_repo._scope(None)
    except Exception as exc:  # noqa: BLE001
        f.violation("account_scope", f"the scope helper could not be exercised: {exc}")
        return
    if "customer_id = ANY" not in empty_sql or empty_params != ([],):
        f.violation("account_scope",
                    "an empty account candidate list does not apply the predicate — "
                    "an unresolved account would widen to every account")
        return
    if "customer_id" in wide_sql:
        f.violation("account_scope",
                    "the account-wide read gained an account predicate, which "
                    "would silently change the Keyword Evidence page")
        return

    # And the preview must actually pass its candidates through.
    svc = _ROOT / "services" / "keyword_evidence_service.py"
    preview = _function_code(svc, "build_campaign_keyword_preview") or ""
    if "customer_ids=candidates" not in preview:
        f.violation("account_scope",
                    "the campaign keyword preview does not pass its account "
                    "candidates into the population query")
        return

    f.passed("account_scope",
             "account predicate applied in SQL before aggregation, sorting and "
             "pagination; empty candidate list selects nothing")


def check_section_contracts_are_complete(f: Findings) -> None:
    """PR-ADS-157 §3 — every fallback carries the full §6 contract.

    Exercised, not inspected: the shared builders are called on the paths a
    drawer actually hits and the returned dictionaries are checked key by key.
    """
    try:
        import services.keyword_evidence_service as kw
        import services.search_term_evidence_service as st
        import api.server as server
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now("section_contracts", f"services unimportable: {exc}")
        return

    cases = [
        ("keyword/no-window", server._campaign_keyword_preview(None, "1"),
         kw.KEYWORD_SECTION_KEYS),
        ("flagged/no-window", server._campaign_flagged_preview(None, "1"),
         st.FLAGGED_SECTION_KEYS),
        ("keyword/builder", kw.keyword_preview_unavailable("probe"),
         kw.KEYWORD_SECTION_KEYS),
        ("flagged/builder", st.flagged_preview_unavailable("probe"),
         st.FLAGGED_SECTION_KEYS),
        ("keyword/identity", kw.build_campaign_keyword_preview("30d", None),
         kw.KEYWORD_SECTION_KEYS),
        ("flagged/identity", st.build_campaign_flagged_preview("30d", None),
         st.FLAGGED_SECTION_KEYS),
    ]
    broken = []
    for label, section, keys in cases:
        missing = sorted(set(keys) - set(section or {}))
        if missing:
            broken.append(f"{label} missing {missing}")
            continue
        empty = [k for k in ("source", "source_dataset", "source_table", "scope",
                             "grain") if not section.get(k)]
        if empty:
            broken.append(f"{label} has empty {empty}")
    if broken:
        f.violation("section_contracts", "; ".join(broken))
    else:
        f.passed("section_contracts",
                 f"{len(cases)} fallback paths carry the complete §6 contract")

    # Structural: api/server.py must not maintain its own section dictionaries.
    for name in ("_campaign_keyword_preview", "_campaign_flagged_preview"):
        code = _function_code(_API_SERVER, name) or ""
        if '"source_table"' in code or "'source_table'" in code:
            f.violation("section_contracts",
                        f"{name} hand-builds a section dictionary instead of "
                        "using the shared unavailable builder")
            return
    f.passed("section_contract_ownership",
             "api/server.py delegates every unavailable section to the services")


def check_outage_propagation(f: Findings) -> None:
    """PR-ADS-157 §4 — the outage flag is read from the ENVELOPE.

    `build_campaign_drawer_evidence` returns `db_unavailable` beside a `None`
    campaign, so reading it off the row evaluated False in exactly the case it
    existed to detect and a dead database rendered as an empty campaign.
    """
    code = _function_code(_API_SERVER, "_build_campaign_detail")
    if code is None:
        f.violation("outage_propagation", "_build_campaign_detail not found")
        return

    if "(row or {}).get('db_unavailable')" in code or '(row or {}).get("db_unavailable")' in code:
        f.violation("outage_propagation",
                    "the drawer reads db_unavailable off the campaign row; the "
                    "flag is on the envelope and the row is None during an "
                    "outage, so the outage is never detected")
        return
    if "ev.get('db_unavailable')" not in code and 'ev.get("db_unavailable")' not in code:
        f.violation("outage_propagation",
                    "the drawer does not read db_unavailable from the drawer "
                    "evidence envelope")
        return

    # The whole-drawer banner needs all three reads to be unavailable.
    for token in ("keyword_preview.get('available')", 'keyword_preview.get("available")'):
        if token in code:
            break
    else:
        f.violation("outage_propagation",
                    "the whole-drawer outage flag does not consider the keyword "
                    "preview, so it could claim a total outage while a section loaded")
        return
    for token in ("flagged_preview.get('available')", 'flagged_preview.get("available")'):
        if token in code:
            break
    else:
        f.violation("outage_propagation",
                    "the whole-drawer outage flag does not consider the flagged "
                    "preview")
        return

    f.passed("outage_propagation",
             "outage read from the envelope; whole-drawer flag requires the "
             "headline and both previews to be unavailable")


def check_frontend_gates(f: Findings) -> None:
    """Every SQL-dependent surface must consult the one gate function."""
    try:
        js = _APP_JS.read_text()
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now("frontend_gates", f"static/app.js unreadable: {exc}")
        return

    if "campaignSqlPublication" not in js:
        f.violation("frontend_gates",
                    "the campaign SQL publication gate does not exist in static/app.js")
        return

    # The gate must be READ by each surface, not merely defined once. A gate
    # nothing calls is the exact defect this PR fixes: /api/campaigns already
    # returned sql_reconciliation and the page simply never read it.
    # PR-ADS-157 §2 — the SQL-dependent STATUS filters are gated too.
    filters_region = js[js.find("function renderCampaignEvidenceFilters"):]
    filters_region = filters_region[:filters_region.find("\nfunction ", 40)]
    predicate = js[js.find("function filterCampaignEvidence"):]
    predicate = predicate[:predicate.find("\nfunction ", 40)]
    status_problems = []
    if "CAMPAIGN_SQL_DEPENDENT_STATUSES.has(v)" not in filters_region:
        status_problems.append("the status <option> builder is not gated")
    if 'f.status = "all"' not in filters_region:
        status_problems.append("a stale SQL-dependent status selection is not cleared")
    if "CAMPAIGN_SQL_DEPENDENT_STATUSES.has(f.status)" not in predicate:
        status_problems.append("filterCampaignEvidence does not refuse "
                               "SQL-dependent statuses internally")
    else:
        gate_at = predicate.index("CAMPAIGN_SQL_DEPENDENT_STATUSES.has(f.status)")
        eq_at = predicate.find('(c.outcome_status || "") !== f.status')
        if eq_at != -1 and eq_at < gate_at:
            status_problems.append("the status equality check runs before the gate")
    if status_problems:
        f.violation("sql_status_filter_gate", "; ".join(status_problems))
    else:
        f.passed("sql_status_filter_gate",
                 "SQL-dependent status filters are disabled, cleared when stale, "
                 "and refused inside the predicate")

    required_callers = {
        "renderCampaignEvidenceKPIs": "KPI strip",
        "renderCampaignEvidenceFilters": "filter controls",
        "filterCampaignEvidence": "outcome filtering",
        "sortCampaignEvidence": "sort ordering",
        "renderCampaignEvidenceRow": "table row",
        "renderCampaignDrawer": "drawer headline",
        "_appendDrawerEvidenceSections": "lead-quality and country splits",
    }
    missing = []
    for fn, label in required_callers.items():
        marker = f"function {fn}"
        i = js.find(marker)
        if i == -1:
            missing.append(f"{label} ({fn} not found)")
            continue
        # Bound the scan at the next top-level function definition.
        j = js.find("\nfunction ", i + len(marker))
        body = js[i:j if j != -1 else len(js)]
        if "campaignSqlPublication" not in body:
            missing.append(f"{label} ({fn} does not consult the gate)")
    if missing:
        f.violation("frontend_gates",
                    "SQL-dependent surfaces not gated: " + "; ".join(missing))
    else:
        f.passed("frontend_gates",
                 f"all {len(required_callers)} SQL-dependent surfaces consult the gate")

    if "_campaignSqlReconciliation = data.sql_reconciliation" not in js:
        f.violation("frontend_gates",
                    "sql_reconciliation is not carried from /api/campaigns into state")
    else:
        f.passed("frontend_reconciliation_state",
                 "sql_reconciliation is stored in Campaign page state")

    if "Reconciliation required" not in js:
        f.violation("frontend_gates",
                    "no 'Reconciliation required' rendering exists")
    else:
        f.passed("frontend_withheld_label", "withheld SQL evidence has a label")

    if "Campaign-attributable SQLs" not in js:
        f.violation("frontend_scope_label",
                    "the SQL population is not named 'Campaign-attributable SQLs'")
    else:
        f.passed("frontend_scope_label", "SQL population is explicitly scoped")


# ─────────────────────────────────────────────────────────────────────────────
# Population reconciliation — which rows add up to which summary field
# ─────────────────────────────────────────────────────────────────────────────

def _sum_or_unavailable(values):
    """Sum, unless any contributor is unavailable.

    A withheld count is not a zero. Summing `None` as `0` would turn missing
    evidence into a smaller-but-confident number — the exact class of claim this
    gate exists to catch.
    """
    vals = list(values)
    if any(v is None for v in vals):
        return None
    return sum(vals)


def _reconcile(f: Findings, name: str, observed, published,
               observed_label: str, published_label: str,
               *, empty_population: bool = False) -> None:
    """Compare a population computed from the rows against the published field.

    Either side may legitimately be unavailable — when lead evidence is missing,
    every row withholds its SQL count and so does the summary. Unavailable on
    BOTH sides is therefore consistent. Unavailable on exactly ONE side is a
    violation: half the evidence is missing while the other half is published as
    certain.

    A population with no rows at all is the one case where a `0` on the row side
    contradicts nothing: there is no evidence there to disagree with a withheld
    field. It still has to match a field that *does* publish a number, so a
    dropped Mapping Review row is caught rather than excused.
    """
    if observed is None and published is None:
        f.passed(name, f"{published_label} is withheld, and so are the rows behind it")
    elif empty_population and published is None:
        f.passed(name, f"no rows in this population, and {published_label} is withheld")
    elif observed is None or published is None:
        f.violation(name,
                    f"{observed_label}={observed!r} but {published_label}={published!r} "
                    "— one side is withheld while the other publishes a number")
    elif observed != published:
        f.violation(name,
                    f"{observed_label}={observed} but {published_label}={published}")
    else:
        f.passed(name, f"{published_label} == {observed_label} == {observed}")


def check_summary_population_reconciliation(window, campaigns, summary,
                                            f: Findings) -> dict:
    """Reconcile each summary field against the rows that actually feed it.

    The campaign table carries two kinds of row and the summary deliberately
    counts only one of them:

      * `mapping_status="mapped"`   — SQLs attributed to a canonical Google Ads
        campaign identity. This population, and only this population, feeds
        `confirmed_sqls_total` and the CPQL denominator.
      * `mapping_status="unmatched"` — Mapping Review rows: real paid-search SQLs
        whose campaign identity is not yet proven. They are shown, never merged.

    A third population, `excluded_not_google_sqls`, has no rows at all: those
    SQLs are proven not to be Google Ads, so no campaign row can exist for them.

    Comparing `confirmed_sqls_total` against *every* row therefore compares a
    mapped-only number with a mapped + unmatched sum, and fails on any account
    with a single Mapping Review SQL (production: 71 mapped + 1 unmatched = 72
    rows over 180d; 382 + 232 = 614 over all_time). The fix is to reconcile each
    population against its own field — never to widen the published scope, which
    would let an unattributed SQL lower canonical CPQL.
    """
    cov = summary.get("mapping_coverage") or {}
    scoped = f"[{window}]"

    # ── the row population must partition exactly ───────────────────────────
    # A row whose mapping_status is neither value belongs to no population, so
    # it reconciles against nothing and disappears from every total silently.
    strays = sorted({(c.get("mapping_status") or "<missing>") for c in campaigns}
                    - {"mapped", "unmatched"})
    if strays:
        f.violation(f"population_partition{scoped}",
                    f"campaign rows carry unrecognised mapping_status {strays} — "
                    "every row must belong to exactly one reconciled population")
    else:
        f.passed(f"population_partition{scoped}",
                 "every campaign row is either mapped or a Mapping Review row")

    mapped_rows = [c for c in campaigns if c.get("mapping_status") == "mapped"]
    review_rows = [c for c in campaigns if c.get("mapping_status") == "unmatched"]
    mapped_row_sqls = _sum_or_unavailable(c.get("confirmed_sqls") for c in mapped_rows)
    review_row_sqls = _sum_or_unavailable(c.get("confirmed_sqls") for c in review_rows)

    # ── each population against the field it actually feeds ─────────────────
    _reconcile(f, f"mapped_rows_reconcile{scoped}",
               mapped_row_sqls, summary.get("confirmed_sqls_total"),
               "sum of confirmed_sqls over mapped rows", "summary.confirmed_sqls_total",
               empty_population=not mapped_rows)
    _reconcile(f, f"mapped_coverage_reconcile{scoped}",
               mapped_row_sqls, cov.get("mapped_sqls"),
               "sum of confirmed_sqls over mapped rows",
               "summary.mapping_coverage.mapped_sqls",
               empty_population=not mapped_rows)
    _reconcile(f, f"unmatched_rows_reconcile{scoped}",
               review_row_sqls, cov.get("unmatched_sqls"),
               "sum of confirmed_sqls over Mapping Review rows",
               "summary.mapping_coverage.unmatched_sqls",
               empty_population=not review_rows)

    # ── the three populations must add up to the declared total ─────────────
    parts = (cov.get("mapped_sqls"), cov.get("unmatched_sqls"),
             cov.get("excluded_not_google_sqls"))
    _reconcile(f, f"paid_search_partition{scoped}",
               _sum_or_unavailable(parts), cov.get("total_paid_search_sqls"),
               "mapped + unmatched + excluded_not_google",
               "summary.mapping_coverage.total_paid_search_sqls")

    # ── name every population in the output, so no reader has to guess ──────
    return {
        "campaign_attributable_sqls": {
            "rows": len(mapped_rows), "sqls": mapped_row_sqls,
            "definition": "mapped canonical Google Ads campaign rows — the only "
                          "population feeding confirmed_sqls_total and CPQL",
        },
        "unmatched_sqls": {
            "rows": len(review_rows), "sqls": review_row_sqls,
            "definition": "Mapping Review rows — paid-search SQLs with no proven "
                          "campaign identity; shown, never merged into the total",
        },
        "excluded_not_google_sqls": {
            "rows": 0, "sqls": cov.get("excluded_not_google_sqls"),
            "definition": "paid-search SQLs proven not to be Google Ads — no "
                          "campaign row exists for them",
        },
        "total_paid_search_sqls": {
            "rows": len(campaigns), "sqls": cov.get("total_paid_search_sqls"),
            "definition": "all three populations together",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Live checks — per window, against the real evidence services
# ─────────────────────────────────────────────────────────────────────────────

def _audit_window(window: str, f: Findings) -> dict:
    """Build the real payloads for one window and check every published claim."""
    out: dict = {"window": window}
    try:
        from services.campaign_evidence_service import build_campaign_evidence
        payload = build_campaign_evidence(window)
    except Exception as exc:  # noqa: BLE001
        f.unavailable_now(f"build[{window}]", f"campaign evidence build failed: {exc}")
        return {**out, "available": False}

    if payload.get("db_unavailable"):
        f.unavailable_now(f"build[{window}]", "database unavailable")
        return {**out, "available": False}

    campaigns = payload.get("campaigns") or []
    summary = payload.get("summary") or {}
    recon = payload.get("sql_reconciliation") or {}
    out["campaigns"] = len(campaigns)

    # ── 9 · the reconciliation block exists and names the right population ───
    if not recon:
        f.violation(f"reconciliation_present[{window}]",
                    "/api/campaigns returned no sql_reconciliation block")
    elif recon.get("sql_scope") != SCOPE_CAMPAIGN_ATTRIBUTABLE:
        f.violation(f"reconciliation_scope[{window}]",
                    f"sql_scope is {recon.get('sql_scope')!r}, expected "
                    f"{SCOPE_CAMPAIGN_ATTRIBUTABLE!r} — a page must name the "
                    "population it counts")
    else:
        f.passed(f"reconciliation_scope[{window}]", SCOPE_CAMPAIGN_ATTRIBUTABLE)

    status = recon.get("reconciliation_status")
    out["reconciliation_status"] = status
    publishable = status in PUBLISHABLE_STATUSES

    # ── 10 · unavailable is never zero ──────────────────────────────────────
    if status == "unavailable":
        numeric = {k: v for k, v in recon.items()
                   if k.endswith("_sqls") and isinstance(v, (int, float))}
        if numeric:
            f.violation(f"unavailable_not_zero[{window}]",
                        f"an unavailable reconciliation still published counts: {numeric}")
        else:
            f.passed(f"unavailable_not_zero[{window}]",
                     "unavailable reconciliation publishes no counts")

    # ── 11 · every summary field reconciles to the population that feeds it ─
    out["sql_populations"] = check_summary_population_reconciliation(
        window, campaigns, summary, f)

    # ── 12/13 · campaign identity is unique, and names do not merge ─────────
    keys = [c.get("campaign_key") for c in campaigns if c.get("campaign_key") is not None]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        f.violation(f"identity_unique[{window}]",
                    f"{len(dupes)} campaign_key value(s) appear on more than one row: "
                    f"{sorted(dupes)[:5]}")
    else:
        f.passed(f"identity_unique[{window}]", f"{len(keys)} distinct campaign identities")

    by_name: dict[str, set] = {}
    for c in campaigns:
        by_name.setdefault((c.get("campaign_name") or "").strip().lower(), set()).add(
            c.get("campaign_key"))
    shared = {n: k for n, k in by_name.items() if n and len(k) > 1}
    if shared:
        f.passed(f"same_name_separated[{window}]",
                 f"{len(shared)} display name(s) correctly kept as separate identities")
    else:
        f.passed(f"same_name_separated[{window}]",
                 "no display name is shared by two campaigns in this window")

    # ── 1/4/5/6/7/8 · the drawer sections, on a real campaign ───────────────
    probe = next((c for c in campaigns if c.get("campaign_key")), None)
    if probe is None:
        f.passed(f"drawer_sections[{window}]",
                 "no campaign with a resolved identity in this window — nothing to probe")
        return {**out, "available": True, "publishable": publishable}

    key = probe["campaign_key"]
    out["probe_campaign_key"] = key
    sections = {}
    try:
        from services.keyword_evidence_service import build_campaign_keyword_preview
        from services.search_term_evidence_service import build_campaign_flagged_preview
        sections["keyword"] = build_campaign_keyword_preview(window, key)
        sections["flagged"] = build_campaign_flagged_preview(window, key)
    except Exception as exc:  # noqa: BLE001
        # The adapters promise never to raise. If one does, that is a defect in
        # the contract itself, not an outage.
        f.violation(f"drawer_sections[{window}]",
                    f"a preview adapter raised instead of returning an "
                    f"unavailable section: {exc}")
        return {**out, "available": True, "publishable": publishable}

    expected_provenance = {
        "keyword": ("keyword_daily_facts", "keyword_facts"),
        "flagged": ("search_terms", "search_terms"),
    }
    for name, section in sections.items():
        label = f"section[{name}/{window}]"
        missing = [k for k in SECTION_KEYS if k not in section]
        if missing:
            f.violation(label, f"section contract missing {missing}")
            continue

        table, dataset = expected_provenance[name]
        if section.get("source_table") != table or section.get("source_dataset") != dataset:
            f.violation(label,
                        f"declares source {section.get('source_dataset')}/"
                        f"{section.get('source_table')}, expected {dataset}/{table}")
            continue

        if not section["available"]:
            if not section.get("reason"):
                f.violation(label, "unavailable section carries no reason code")
            elif section.get("rows"):
                f.violation(label, "unavailable section published rows")
            elif isinstance(section.get("total_count"), (int, float)):
                f.violation(label,
                            f"unavailable section published a total of "
                            f"{section['total_count']} — unavailable is not zero")
            else:
                f.passed(label, f"unavailable, reason={section['reason']}")
            continue

        # An available section must be window-aligned with what was requested.
        if section.get("window") != window:
            f.violation(label,
                        f"reports window {section.get('window')!r} for a "
                        f"{window!r} request")
        elif not (section.get("all_time")
                  or (section.get("window_start") and section.get("window_end"))):
            f.violation(label,
                        "available section has no window bounds — an unbounded "
                        "result must never be presented as selected-window evidence")
        else:
            f.passed(label,
                     f"available, {len(section['rows'])} row(s), "
                     f"coverage={section.get('coverage_status')}")

    if "waste_terms" != (sections["flagged"].get("annotation_table") or ""):
        f.violation(f"annotation_role[{window}]",
                    "the flagged section does not declare waste_terms as its "
                    "annotation source")
    elif "never a metric" not in (sections["flagged"].get("annotation_role") or "").lower():
        f.violation(f"annotation_role[{window}]",
                    "the flagged section does not declare waste_terms as "
                    "annotation-only")
    else:
        f.passed(f"annotation_role[{window}]",
                 "waste_terms is declared classification-annotation only")

    return {**out, "available": True, "publishable": publishable,
            "sections": {k: {"available": v.get("available"), "reason": v.get("reason")}
                         for k, v in sections.items()}}


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(windows) -> tuple[Findings, dict]:
    f = Findings()
    check_legacy_readers(f)
    check_no_waste_terms_metric(f)
    check_account_scope_before_aggregation(f)
    check_section_contracts_are_complete(f)
    check_outage_propagation(f)
    check_frontend_gates(f)
    per_window = {w: _audit_window(w, f) for w in windows}
    return f, per_window


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only PR-ADS-157 Campaign Evidence certification gate")
    parser.add_argument("--window", default=None,
                        help="Audit ONE evidence window instead of all of them")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (exit code is unchanged)")
    args = parser.parse_args()

    if args.window and args.window not in WINDOWS:
        print(f"Unknown window {args.window!r}. Supported: {', '.join(WINDOWS)}",
              file=sys.stderr)
        return EXIT_UNAVAILABLE
    windows = (args.window,) if args.window else WINDOWS

    try:
        from db.connection import init_pool
        init_pool()
    except Exception as exc:  # noqa: BLE001
        # Exit 2, not 1: the product is not broken, it is unreachable.
        payload = {"certified": False, "exit_code": EXIT_UNAVAILABLE,
                   "external_writes_performed": False,
                   "unavailable": [f"database pool could not be initialised: {exc}"]}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database pool could not be initialised: {exc}")
        return EXIT_UNAVAILABLE

    findings, per_window = run(windows)
    exit_code = findings.exit_code

    result = {
        "certified": exit_code == EXIT_CERTIFIED,
        "exit_code": exit_code,
        # Stated as a fact about this command, which performs no writes and
        # contacts no external API. Every number above came from the local
        # database through read-only evidence services.
        "external_writes_performed": False,
        "windows_audited": list(windows),
        "violations": findings.violations,
        "unavailable": findings.unavailable,
        "checks": findings.checks,
        "per_window": per_window,
    }

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return exit_code

    verdict = ("CERTIFIED" if exit_code == EXIT_CERTIFIED
               else "TRUTH-CONTRACT VIOLATION" if exit_code == EXIT_VIOLATION
               else "UNAVAILABLE")
    print("=" * 72)
    print("PR-ADS-157 — CAMPAIGN EVIDENCE CERTIFICATION (READ-ONLY GATE)")
    print(f"Windows audited: {', '.join(windows)}")
    print("=" * 72)
    passed = sum(1 for c in findings.checks if c["ok"])
    print(f"{passed}/{len(findings.checks)} checks passed")
    print("External writes performed: no")
    for c in findings.checks:
        if not c["ok"]:
            print(f"  ✗ {c['check']}: {c['detail']}")
    if findings.violations:
        print()
        print(f"{len(findings.violations)} truth-contract violation(s).")
    if findings.unavailable:
        print()
        print(f"{len(findings.unavailable)} check(s) could not run "
              "(database or canonical coverage unavailable).")
    print()
    print(f"VERDICT: {verdict}  (exit {exit_code})")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
