#!/usr/bin/env python3
"""
scripts/audit_marketing_outcome_cohorts.py

PR-ADS-161B — read-only audit of the acquisition-cohort outcomes Campaign
Evidence publishes.

    python -m scripts.audit_marketing_outcome_cohorts
    python -m scripts.audit_marketing_outcome_cohorts --json

Exit codes
----------
    0  every supported window reconciles and every guarantee holds
    1  an implementation or reconciliation VIOLATION
    2  a required check could not run; the audit proves nothing about it

Incomplete lifecycle-event timestamps are NOT a violation here. They are
disclosed, and the audit proves they are counted in the cohort and kept out of
its dates — which is the whole contract. Event-time coverage is the SQL
coverage gate's job (``scripts/audit_sql_coverage_gate.py``), and this audit
neither repeats nor weakens it.

Independence
------------
The page payload is built by ``services.campaign_evidence_service``. This audit
does not certify that payload with the code that produced it. For every
window it re-derives, in SQL, how many canonical contacts were created in the
window and how many of them canonical lifecycle evidence proves reached SQL,
and compares the payload against those numbers. The campaign-identity
resolver is shared on purpose: it is the contract being applied, not the
code under test.

Nothing here writes. Not to HubSpot, not to Google Ads, not to the local
database.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.audit_sql_coverage_gate import BOUND_COLUMN  # noqa: E402

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

_COHORT_SERVICE = _ROOT / "services" / "marketing_outcome_cohort_service.py"
_CAMPAIGN_SERVICE = _ROOT / "services" / "campaign_evidence_service.py"

#: Timestamps the cohort must never use as a date. A boundary is an upper bound;
#: sync and ingestion stamps describe our pipeline, not the contact.
#:
#: The boundary column's name is taken from ``audit_sql_coverage_gate``, not
#: spelled here. That gate fails on any module outside its allow-list that names
#: the bound at all — it cannot tell a reader from a module that forbids it, and
#: should not have to. This audit never reads the bound; it only forbids it, so
#: it is not added to that allow-list. Importing the gate's own definition also
#: means a rename there renames what this check forbids.
FORBIDDEN_DATE_SOURCES = (
    BOUND_COLUMN, "boundary_observed_at", "observed_at",
    "last_ingested_at", "first_ingested_at", "last_modified_at",
    "updated_at", "recorded_at", "last_incremental_at",
)

#: A run of Python whitespace (``str.isspace``) as a PostgreSQL ARE, so the
#: audit's SQL normalises a source exactly as ``normalize_source`` does.
PY_WHITESPACE_RUN = "[" + "".join(
    f"\\u{cp:04x}" for cp in range(0x3001) if chr(cp).isspace()) + "]+"

#: Anything that could reach an external system or write to our own database.
FORBIDDEN_WRITE_MARKERS = (
    "connectors.", "hubspot", "googleads", "google_ads_api", "requests.post",
    "INSERT ", "UPDATE ", "DELETE ", "db.writers", "from db import writers",
)


class Audit:
    """Violations and unavailable checks, kept apart (as in every gate here)."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.unavailable: list[str] = []
        self.checks: list[dict] = []

    def broken(self, name: str, detail: str) -> None:
        self.violations.append(f"{name}: {detail}")
        self.checks.append({"check": name, "ok": False, "detail": detail})

    def cannot_check(self, name: str, detail: str) -> None:
        self.unavailable.append(f"{name}: {detail}")
        self.checks.append({"check": name, "ok": False, "detail": detail})

    def holds(self, name: str, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": True, "detail": detail})

    @property
    def exit_code(self) -> int:
        if self.violations:
            return EXIT_VIOLATION
        if self.unavailable:
            return EXIT_UNAVAILABLE
        return EXIT_OK


# ═════════════════════════════════════════════════════════════════════════════
# Structural checks (no database)
# ═════════════════════════════════════════════════════════════════════════════
def _code_without_docstrings(path: Path) -> str:
    """Source text with docstrings and comments removed, so a sentence that
    NAMES a forbidden source to forbid it is not mistaken for a use of it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant) and isinstance(
                    body[0].value.value, str):
                body[0].value.value = ""
    return ast.unparse(tree)


#: The functions that decide membership, SQL proof and buckets. The guarantee
#: "no other date reaches the cohort" is about THESE. The disclosure function
#: legitimately REPORTS the boundary instant as metadata, so a whole-module scan
#: would be wrong in the other direction — and a check that false-positives is a
#: check someone eventually weakens.
CLASSIFICATION_FUNCTIONS = (
    "window_instants", "_as_instant", "in_window", "sql_proof", "contact_identity",
    "contact_bucket", "_merge_duplicates", "build_cohort", "build_window_outcomes",
)


def _function_sources(path: Path, names) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant):
                node.body = body[1:] or [ast.Pass()]
            out[node.name] = ast.unparse(node)
    return out


def check_no_date_contamination(a: Audit, *, path: Path = _COHORT_SERVICE,
                                functions=CLASSIFICATION_FUNCTIONS) -> dict:
    """Check 8 (structural half): no classification path reads a pipeline or
    boundary timestamp. Missing functions are reported, not skipped — a rename
    must not quietly empty the check."""
    sources = _function_sources(path, functions)
    missing = sorted(set(functions) - set(sources))
    hits = {fn: sorted(m for m in FORBIDDEN_DATE_SOURCES if m in src)
            for fn, src in sources.items()}
    hits = {fn: h for fn, h in hits.items() if h}
    name = "lifecycle_gaps_do_not_contaminate_cohort_dates"
    if missing:
        a.cannot_check(name, f"classification function(s) not found: {missing}")
    elif hits:
        a.broken(name, f"{hits}: cohort membership and proof must use "
                       f"contact_created_at and stage-entry evidence alone")
    else:
        a.holds(name, f"{len(sources)} classification functions reference no "
                      f"boundary, sync or ingestion timestamp")
    return {"forbidden_references": hits, "missing_functions": missing}


def check_no_write_paths(a: Audit, *, paths=(_COHORT_SERVICE,)) -> dict:
    """Checks 9-10: no HubSpot write, no external write, no database write."""
    found: dict[str, list[str]] = {}
    for path in paths:
        code = _code_without_docstrings(Path(path))
        hits = [m for m in FORBIDDEN_WRITE_MARKERS if m.lower() in code.lower()]
        if hits:
            found[str(Path(path).relative_to(_ROOT))] = hits
    name = "no_external_or_database_write_path"
    if found:
        a.broken(name, f"write-capable references found: {found}")
    else:
        a.holds(name, "the cohort service imports no connector and issues no "
                      "write; this command performs none")
    return {"write_markers": found}


# ═════════════════════════════════════════════════════════════════════════════
# Per-window checks (pure — every input is passed in)
# ═════════════════════════════════════════════════════════════════════════════
#: Every payload location a cohort SQL count (or a CPQL derived from one) can
#: occupy. When the SQL count is not published, ALL of them must be null.
_SUMMARY_SQL_FIELDS = (
    "cohort_sqls_google_ads", "cohort_sqls_mapped", "cohort_sqls_unattributed",
    "cohort_sqls_excluded_non_google", "cohort_sqls_all_sources",
    "cohort_sqls_missing_event_timestamp", "cohort_cpql_usd",
)
_ROW_SQL_FIELDS = ("cohort_sqls", "cohort_sqls_missing_event_timestamp", "cohort_cpql_usd")
_META_SQL_FIELDS = ("mapped_count", "unattributed_count", "excluded_non_google_count")
_RECON_SQL_FIELDS = ("google_ads_sqls", "sum_campaign_sqls", "unattributed_google_ads_sqls",
                     "excluded_non_google_sqls", "all_source_sqls")
#: The lifecycle-event disclosure's reached-SQL population counts. Read over
#: the same contact funnel, so withheld with the cohort (round 3: the first
#: version of this scan missed them, and the page showed 1,531 beside "no SQL
#: count is published").
_LIFECYCLE_SQL_FIELDS = ("reached_sql_by_current_stage", "exact_direct_timestamp",
                         "recovered_timestamp", "missing_exact_timestamp")


def withheld_exposures(payload: dict) -> list[str]:
    """Every place a withheld cohort SQL count (or its CPQL) is still present.

    The page publishes nothing SQL-derived from the canonical contact funnel
    unless the verdict is ``published``: not the total, not its parts, not a
    row, not a CPQL, not the funnel-wide reached-SQL counts in the
    lifecycle-event disclosure. An empty list means the payload honours that.

    Scope, stated rather than implied: the LEGACY lead-status fields declared in
    ``legacy_sql`` (``confirmed_sqls``, ``cpql_usd``, ``confirmed_sqls_total``,
    ``overall_cpql_usd``, ``mapping_coverage``, ``sql_reconciliation``) are a
    different metric family kept for other readers. They are not checked here;
    that no Campaign Evidence surface consumes them is enforced by
    ``audit_campaign_evidence_certification.check_frontend_gates``.
    """
    cohort = payload.get("cohort") or {}
    summary = payload.get("summary") or {}
    out = [f"summary.{k}" for k in _SUMMARY_SQL_FIELDS if summary.get(k) is not None]
    for r in payload.get("campaigns") or []:
        out += [f"campaigns[{r.get('campaign_key')}].{k}"
                for k in _ROW_SQL_FIELDS if r.get(k) is not None]
        if r.get("cohort_cpql_status") == "published":
            out.append(f"campaigns[{r.get('campaign_key')}].cohort_cpql_status=published")
        if r.get("cohort_sql_status") == "published":
            out.append(f"campaigns[{r.get('campaign_key')}].cohort_sql_status=published")
    meta = cohort.get("metadata") or {}
    out += [f"cohort.metadata.{k}" for k in _META_SQL_FIELDS if meta.get(k) is not None]
    recon = cohort.get("reconciliation") or {}
    out += [f"cohort.reconciliation.{k}" for k in _RECON_SQL_FIELDS
            if recon.get(k) is not None]
    if cohort.get("breakdown") is not None:
        out.append("cohort.breakdown")
    lc = cohort.get("lifecycle_event_coverage") or {}
    out += [f"cohort.lifecycle_event_coverage.{k}" for k in _LIFECYCLE_SQL_FIELDS
            if lc.get(k) is not None]
    if summary.get("cohort_cpql_status") == "published" or cohort.get("cpql_status") == "published":
        out.append("cpql_status=published")
    return out


def check_closed_won_not_published(a: Audit, *, window: str, payload: dict) -> None:
    """Closed-won deals are not published by this page (PR-ADS-161B round 3):
    declared as such, and absent from every row and the summary."""
    w = f"[{window}]"
    decl = ((payload.get("cohort") or {}).get("closed_won_deals") or {})
    leaked = [k for k in (payload.get("summary") or {}) if "closed_won" in k]
    for r in payload.get("campaigns") or []:
        leaked += [f"campaigns[{r.get('campaign_key')}].{k}" for k in r if "closed_won" in k]
    if decl.get("published_on_this_page") is not False:
        a.broken(f"{w} closed_won_not_published",
                 "the payload does not declare closed-won deals unpublished")
    elif leaked:
        a.broken(f"{w} closed_won_not_published", f"closed-won fields present: {leaked[:5]}")
    else:
        a.holds(f"{w} closed_won_not_published",
                "declared unpublished; no closed-won field on any row or the summary")


def audit_window(a: Audit, *, window: str, payload: dict, independent: dict) -> dict:
    """Every per-window guarantee, against one page payload.

    ``independent``: ``{"contacts_acquired", "sqls", "stage_only_sqls",
    "distinct_sql_contact_ids", "paid_search_sourced_sqls"}`` computed in SQL
    by this audit.

    A window whose SQL count is NOT published has nothing to reconcile. What it
    must prove instead is that nothing SQL-derived leaked into the payload; a
    leak is a violation. A canonical funnel the page could not read is
    ``cannot_check`` — the audit then proves nothing about the window.
    """
    w = f"[{window}]"
    cohort = payload.get("cohort") or {}
    meta = cohort.get("metadata") or {}
    summary = payload.get("summary") or {}
    rows = payload.get("campaigns") or []

    check_closed_won_not_published(a, window=window, payload=payload)

    status = cohort.get("sql_status")
    if status != "published":
        exposed = withheld_exposures(payload)
        if exposed:
            a.broken(f"{w} withheld_not_exposed",
                     f"SQL count is {status!r} ({cohort.get('sql_reason')}) but is still "
                     f"present at: {exposed[:6]}")
        else:
            a.holds(f"{w} withheld_not_exposed",
                    f"SQL count {status} ({cohort.get('sql_reason')}); no SQL count, "
                    f"breakdown or CPQL is present anywhere in the payload")
        if status == "unavailable" or status is None:
            a.cannot_check(f"{w} cohort", f"the cohort is unavailable "
                           f"({cohort.get('sql_reason')}); nothing to reconcile")
        return {"window": window, "available": status == "withheld",
                "published": False, "sql_status": status,
                "sql_reason": cohort.get("sql_reason"), "as_of": meta.get("as_of"),
                "source_fresh": (meta.get("source_freshness") or {}).get("fresh"),
                "legacy": {
                    "confirmed_sqls_mapped": summary.get("confirmed_sqls_total"),
                    "overall_cpql_usd": summary.get("overall_cpql_usd"),
                    "overall_cpql_scope": summary.get("overall_cpql_scope"),
                }}

    breakdown = cohort.get("breakdown") or {}
    if not breakdown:
        a.broken(f"{w} cohort", "the SQL count is published but its breakdown is "
                 "missing, so it cannot be reconciled")
        return {"window": window, "available": False, "published": True}

    # 1. window membership is contact_created_at
    if meta.get("window_basis") != "contact_created_at":
        a.broken(f"{w} window_basis", f"declared {meta.get('window_basis')!r}")
    elif breakdown["all_sources"]["contacts_acquired"] != independent["contacts_acquired"]:
        a.broken(f"{w} membership", f"payload acquired "
                 f"{breakdown['all_sources']['contacts_acquired']} contacts; "
                 f"created_at alone selects {independent['contacts_acquired']}")
    elif breakdown.get("rows_outside_window"):
        a.broken(f"{w} membership", f"{breakdown['rows_outside_window']} rows "
                 f"outside the window reached the cohort read")
    else:
        a.holds(f"{w} membership", f"{independent['contacts_acquired']} contacts, "
                f"selected by contact_created_at")

    # 2. SQL proof is canonical lifecycle evidence
    if meta.get("outcome_basis") != "latest_canonical_lifecycle_evidence":
        a.broken(f"{w} outcome_basis", f"declared {meta.get('outcome_basis')!r}")
    elif breakdown["all_sources"]["sqls"] != independent["sqls"]:
        a.broken(f"{w} sql_proof", f"payload has {breakdown['all_sources']['sqls']} "
                 f"cohort SQLs; canonical evidence proves {independent['sqls']}")
    else:
        a.holds(f"{w} sql_proof", f"{independent['sqls']} SQLs, each proven by a "
                f"direct or recovered SQL-entry date or a stage implying SQL")

    # 3. contacts deduplicated
    if meta.get("dedup_key") != "contact_id":
        a.broken(f"{w} dedup", f"declared dedup key {meta.get('dedup_key')!r}")
    elif independent["distinct_sql_contact_ids"] != breakdown["all_sources"]["sqls"]:
        a.broken(f"{w} dedup", f"{breakdown['all_sources']['sqls']} SQLs vs "
                 f"{independent['distinct_sql_contact_ids']} distinct contact ids")
    else:
        a.holds(f"{w} dedup", "one SQL per distinct contact_id "
                f"(fallback identities: {breakdown['dedup']['fallback_identities']}, "
                f"duplicate rows collapsed: {breakdown['dedup']['duplicate_rows']})")

    # 4. Google Ads total = campaigns + unattributed; all = google + excluded
    ga = summary.get("cohort_sqls_google_ads")
    mapped = summary.get("cohort_sqls_mapped")
    un = summary.get("cohort_sqls_unattributed")
    ex = summary.get("cohort_sqls_excluded_non_google")
    al = summary.get("cohort_sqls_all_sources")
    if None in (ga, mapped, un, ex, al):
        a.cannot_check(f"{w} bucket_reconciliation", "a summary total is null")
    elif mapped + un != ga or ga + ex != al:
        a.broken(f"{w} bucket_reconciliation",
                 f"google {ga} vs campaigns {mapped} + unattributed {un}; "
                 f"all {al} vs google {ga} + excluded {ex}")
    else:
        a.holds(f"{w} bucket_reconciliation",
                f"google {ga} = campaigns {mapped} + unattributed {un}; "
                f"all {al} = google {ga} + excluded {ex}")

    # 4b. the Google Ads / excluded SPLIT, against an independent source rule.
    # Check 4 only proves the buckets add up to each other; an SQL moved from
    # Google Ads to excluded would still add up. This re-derives "original
    # source is Paid Search" in this audit's own SQL. Every Paid Search SQL is
    # either a Google Ads SQL or excluded ONLY because an approved mapping says
    # its label is not Google Ads — nothing else may move it.
    ex_reasons = (breakdown.get("excluded_non_google") or {}).get("by_reason") or {}
    by_mapping = ex_reasons.get("label_mapped_not_google_ads", 0)
    paid = independent.get("paid_search_sourced_sqls")
    if paid is None or ga is None:
        a.cannot_check(f"{w} google_ads_split", "the independent Paid Search count "
                       "or the Google Ads total is unavailable")
    elif ga + by_mapping != paid:
        a.broken(f"{w} google_ads_split",
                 f"{paid} SQLs have a Paid Search original source, but the page "
                 f"has {ga} Google Ads SQLs + {by_mapping} excluded by an approved "
                 f"not-Google-Ads mapping")
    else:
        a.holds(f"{w} google_ads_split",
                f"{paid} Paid Search-sourced SQLs = {ga} Google Ads + {by_mapping} "
                f"excluded by approved mapping")

    # 5. campaign rows reconcile to the summary
    mapped_rows = [r for r in rows if r.get("mapping_status") == "mapped"]
    review_rows = [r for r in rows if r.get("mapping_status") == "unmatched"]
    row_mapped = sum(r.get("cohort_sqls") or 0 for r in mapped_rows)
    row_review = sum(r.get("cohort_sqls") or 0 for r in review_rows)
    without_row = (breakdown["unattributed"].get("without_label_row") or {}).get("sqls")
    if mapped is not None and row_mapped != mapped:
        a.broken(f"{w} rows_reconcile", f"mapped rows sum to {row_mapped}, summary "
                 f"says {mapped}")
    elif un is not None and without_row is not None and row_review + without_row != un:
        a.broken(f"{w} rows_reconcile", f"mapping-review rows {row_review} + "
                 f"unlabelled {without_row} != unattributed {un}")
    else:
        a.holds(f"{w} rows_reconcile", f"mapped rows {row_mapped}; review rows "
                f"{row_review} + unlabelled {without_row} = unattributed {un}")

    # 6. CPQL uses cohort SQLs (summary and every published row)
    cpql_problems = []
    if summary.get("cohort_cpql_status") == "published":
        expect = round(float(summary["spend_usd"]) / ga, 2) if ga else None
        if summary.get("cohort_cpql_usd") != expect:
            cpql_problems.append(f"summary CPQL {summary.get('cohort_cpql_usd')} != "
                                 f"spend {summary['spend_usd']} / cohort SQLs {ga}")
    elif summary.get("cohort_cpql_usd") is not None:
        cpql_problems.append("a CPQL value is present while its status is "
                             f"{summary.get('cohort_cpql_status')!r}")
    if (summary.get("cohort_cpql_status") == "not_applicable" and ga
            and summary.get("cohort_cpql_reason") != "zero_window_spend"):
        cpql_problems.append("CPQL marked not_applicable over a non-zero SQL count")
    # A CPQL can never be published over an SQL count that is not, nor over
    # zero spend (a $0 CPQL reads as free SQLs).
    if cohort.get("sql_status") != "published":
        if summary.get("cohort_cpql_status") == "published" or any(
                r.get("cohort_cpql_status") == "published" for r in rows):
            cpql_problems.append(f"a CPQL is published while the SQL count is "
                                 f"{cohort.get('sql_status')!r}")
    if summary.get("cohort_cpql_status") == "published" and not (
            summary.get("spend_usd") or 0) > 0:
        cpql_problems.append("summary CPQL published over zero spend")
    for r in rows:
        if r.get("cohort_cpql_status") == "published":
            if not (r.get("spend_usd") or 0) > 0:
                cpql_problems.append(f"row {r.get('campaign_key')}: CPQL published "
                                     f"over zero spend")
                continue
            expect = round(float(r["spend_usd"]) / r["cohort_sqls"], 2)
            if r.get("cohort_cpql_usd") != expect:
                cpql_problems.append(f"row {r.get('campaign_key')}: CPQL "
                                     f"{r.get('cohort_cpql_usd')} != {expect}")
        elif r.get("cohort_cpql_usd") is not None:
            cpql_problems.append(f"row {r.get('campaign_key')}: CPQL value with "
                                 f"status {r.get('cohort_cpql_status')!r}")
    if cpql_problems:
        a.broken(f"{w} cpql_uses_cohort_sqls", "; ".join(cpql_problems[:5]))
    else:
        a.holds(f"{w} cpql_uses_cohort_sqls",
                f"status {summary.get('cohort_cpql_status')} "
                f"({summary.get('cohort_cpql_reason') or 'published'})")

    # 8. lifecycle-event gaps disclosed and counted, not dated
    gap = breakdown["all_sources"]["sqls_missing_event_timestamp"]
    stage_only = breakdown["proof_counts"].get("lifecycle_stage_implies_sql")
    if gap != independent["stage_only_sqls"] or stage_only != gap:
        a.broken(f"{w} lifecycle_gaps_disclosed",
                 f"payload discloses {gap} undated cohort SQLs (stage-only proofs "
                 f"{stage_only}); canonical evidence has {independent['stage_only_sqls']}")
    elif gap and not meta.get("coverage_notes"):
        a.broken(f"{w} lifecycle_gaps_disclosed", "undated SQLs with no coverage note")
    elif meta.get("coverage_status") == "cohort_complete" and gap:
        a.broken(f"{w} lifecycle_gaps_disclosed", "coverage claims complete with gaps")
    else:
        a.holds(f"{w} lifecycle_gaps_disclosed",
                f"{gap} cohort SQL(s) with no exact SQL-entry date: counted, "
                f"disclosed, no date attached")

    lc = cohort.get("lifecycle_event_coverage") or {}
    if lc.get("published_on_this_page") is not False:
        a.broken(f"{w} lifecycle_events_not_relabelled",
                 "the page does not declare lifecycle-event SQLs unpublished")

    return {
        "window": window,
        "available": True,
        "published": True,
        "sql_status": status,
        "sql_reason": None,
        "as_of": meta.get("as_of"),
        "source_fresh": (meta.get("source_freshness") or {}).get("fresh"),
        "cohort": {
            "contacts_acquired_all_sources": breakdown["all_sources"]["contacts_acquired"],
            "google_ads_sqls": ga, "campaign_sqls": mapped, "unattributed_sqls": un,
            "excluded_non_google_sqls": ex, "all_source_sqls": al,
            "sqls_missing_event_timestamp": gap,
            "cpql_usd": summary.get("cohort_cpql_usd"),
            "cpql_status": summary.get("cohort_cpql_status"),
        },
        "legacy": {
            "confirmed_sqls_mapped": summary.get("confirmed_sqls_total"),
            "overall_cpql_usd": summary.get("overall_cpql_usd"),
            "overall_cpql_scope": summary.get("overall_cpql_scope"),
        },
        "spend_usd": summary.get("spend_usd"),
        "unattributed_by_reason": breakdown["unattributed"].get("by_reason"),
        "excluded_by_reason": breakdown["excluded_non_google"].get("by_reason"),
        "excluded_sqls_with_gclid": breakdown["excluded_non_google"].get("sqls_with_gclid"),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Independent reads (this audit's own SQL)
# ═════════════════════════════════════════════════════════════════════════════
def independent_counts(start_at, end_before) -> dict | None:
    """Membership and SQL proof, decided in SQL — not by the service's Python.

    Uses the repository's precedence DEFINITIONS (they are the contract) but
    none of the service's code. ``None`` when the database is unavailable.
    """
    from analysis.crm_lifecycle import EVENT_SQL, stages_implying_event
    from db import crm_funnel_repository as repo
    from db.connection import get_conn

    stages = list(stages_implying_event(EVENT_SQL))
    direct = repo.direct_date_sql(EVENT_SQL)
    recovered = repo.recovered_date_sql(EVENT_SQL)
    proven = (f"({direct} IS NOT NULL OR {recovered} IS NOT NULL "
              f"OR lower(btrim(f.lifecycle_stage)) = ANY(%s))")
    stage_only = (f"({direct} IS NULL AND {recovered} IS NULL "
                  f"AND lower(btrim(f.lifecycle_stage)) = ANY(%s))")
    # analysis.source_classification's Paid Search rule, re-stated in SQL rather
    # than imported: underscores as spaces, every whitespace run collapsed to
    # one space THEN trimmed, lowercased. "Whitespace" is Python's own
    # str.isspace() set — what normalize_source's strip()/split() use — since
    # PostgreSQL's \s misses NBSP and btrim() strips only spaces (PR-ADS-161B
    # re-review: tab / newline / NBSP edges disagreed with classify_source).
    paid = ("btrim(regexp_replace(lower(replace(coalesce(f.hs_analytics_source, ''), "
            "'_', ' ')), %s, ' ', 'g'), ' ') = 'paid search'")
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    f"""
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE {proven}),
                           COUNT(*) FILTER (WHERE {stage_only}),
                           COUNT(DISTINCT f.contact_id) FILTER (WHERE {proven}),
                           COUNT(*) FILTER (WHERE {proven} AND {paid})
                    FROM {repo.FUNNEL_TABLE} f
                    {repo._recovery_join()}
                    WHERE f.created_at IS NOT NULL
                      AND (%s::timestamptz IS NULL OR f.created_at >= %s)
                      AND f.created_at < %s
                    """,
                    (stages, stages, stages, stages, PY_WHITESPACE_RUN,
                     start_at, start_at, end_before),
                )
                row = cur.fetchone()
                conn.rollback()
    except Exception:  # noqa: BLE001
        return None
    return {"contacts_acquired": row[0], "sqls": row[1],
            "stage_only_sqls": row[2], "distinct_sql_contact_ids": row[3],
            "paid_search_sourced_sqls": row[4]}


def global_population_split() -> dict | None:
    """Why the all-time cohort is not the 1,531 coverage population.

    The coverage population counts contacts whose CURRENT stage implies SQL.
    The cohort also counts contacts whose SQL-entry date is recorded but whose
    stage later moved below SQL — the event is proven by its timestamp — and
    excludes contacts with no created date, which belong to no window.
    """
    from analysis.crm_lifecycle import EVENT_SQL, stages_implying_event
    from db import crm_funnel_repository as repo
    from db.connection import get_conn

    stages = list(stages_implying_event(EVENT_SQL))
    direct = repo.direct_date_sql(EVENT_SQL)
    recovered = repo.recovered_date_sql(EVENT_SQL)
    stage_ok = "lower(btrim(f.lifecycle_stage)) = ANY(%s)"
    dated = f"({direct} IS NOT NULL OR {recovered} IS NOT NULL)"
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    f"""
                    SELECT COUNT(*) FILTER (WHERE {stage_ok}),
                           COUNT(*) FILTER (WHERE {stage_ok} AND NOT {dated}),
                           COUNT(*) FILTER (WHERE {dated} AND NOT {stage_ok}),
                           COUNT(*) FILTER (WHERE ({stage_ok} OR {dated})
                                              AND f.created_at IS NULL)
                    FROM {repo.FUNNEL_TABLE} f
                    {repo._recovery_join()}
                    """,
                    (stages, stages, stages, stages),
                )
                row = cur.fetchone()
                conn.rollback()
    except Exception:  # noqa: BLE001
        return None
    return {"reached_sql_by_current_stage": row[0],
            "of_which_missing_exact_timestamp": row[1],
            "sql_dated_but_stage_now_below_sql": row[2],
            "sql_proven_but_no_created_at": row[3]}


# ═════════════════════════════════════════════════════════════════════════════
# Run
# ═════════════════════════════════════════════════════════════════════════════
def run(now: datetime | None = None) -> tuple[Audit, dict]:
    from analysis.evidence_windows import EVIDENCE_WINDOWS
    from services import marketing_outcome_cohort_service as cohort_svc
    from services.campaign_evidence_service import _window_bounds, build_campaign_evidence

    now = now or datetime.now(tz=timezone.utc)
    a = Audit()
    report: dict = {
        "generated_at": now.isoformat(),
        "external_writes_performed": False,
        "database_writes_performed": False,
        "hubspot_calls_performed": False,
        "google_ads_calls_performed": False,
        "windows": [],
    }
    report["date_contamination"] = check_no_date_contamination(a)
    report["write_paths"] = check_no_write_paths(a)

    for window in EVIDENCE_WINDOWS:
        start, end, _ = _window_bounds(window, now)
        start_at, end_before = cohort_svc.window_instants(start, end)
        independent = independent_counts(start_at, end_before)
        if independent is None:
            a.cannot_check(f"[{window}] independent", "the database is unavailable")
            report["windows"].append({"window": window, "available": False})
            continue
        payload = build_campaign_evidence(window, now=now)
        report["windows"].append(audit_window(
            a, window=window, payload=payload, independent=independent))

    report["population_split"] = global_population_split()
    return a, report


def _render(report: dict, a: Audit, exit_code: int) -> None:
    print("=" * 78)
    print("  PR-ADS-161B — MARKETING OUTCOME COHORTS AUDIT (READ-ONLY)")
    print("=" * 78)
    print(f"  audit complete:            {not a.unavailable}")
    print(f"  external writes performed: {report['external_writes_performed']}")
    print(f"  database writes performed: {report['database_writes_performed']}")
    print(f"  HubSpot calls performed:   {report['hubspot_calls_performed']}")
    print()
    for check in a.checks:
        mark = "✓" if check["ok"] else "✗"
        print(f"  {mark} {check['check']}")
        if check["detail"]:
            print(f"      {check['detail']}")

    rows = [w for w in report["windows"] if w.get("legacy")]
    if rows:
        print()
        print("  Before (legacy lead status) → after (acquisition cohort), Google Ads:")
        print(f"  {'window':<9}{'legacy SQLs':>12}{'cohort SQLs':>13}"
              f"{'campaign':>10}{'unattr.':>9}{'undated':>9}"
              f"{'legacy CPQL':>13}{'cohort CPQL':>13}")
        fmt_cpql = (lambda v: "—" if v is None else f"${v:,.2f}")
        for w in rows:
            lg = w["legacy"]
            if not w.get("published"):
                print(f"  {w['window']:<9}{str(lg['confirmed_sqls_mapped']):>12}"
                      f"   cohort {w.get('sql_status')} ({w.get('sql_reason')}) — "
                      f"nothing published{fmt_cpql(lg['overall_cpql_usd']):>13}")
                continue
            ch = w["cohort"]
            print(f"  {w['window']:<9}{str(lg['confirmed_sqls_mapped']):>12}"
                  f"{str(ch['google_ads_sqls']):>13}{str(ch['campaign_sqls']):>10}"
                  f"{str(ch['unattributed_sqls']):>9}"
                  f"{str(ch['sqls_missing_event_timestamp']):>9}"
                  f"{fmt_cpql(lg['overall_cpql_usd']):>13}{fmt_cpql(ch['cpql_usd']):>13}")
        print("  legacy SQLs = campaign-mapped leads.status_category = qualified;"
              " cohort SQLs = all Google Ads (campaign + unattributed).")
        print(f"  as of: {rows[0].get('as_of')}  (source fresh: {rows[0].get('source_fresh')})")

    split = report.get("population_split")
    if split:
        print()
        print("  Reached-SQL population vs the coverage population:")
        for k, v in split.items():
            print(f"    {k:<42} {v}")

    print()
    if a.violations:
        print(f"  {len(a.violations)} VIOLATION(S).")
    if a.unavailable:
        print(f"  {len(a.unavailable)} check(s) could not run. The audit proves "
              "nothing about those.")
    withheld = [w["window"] for w in report["windows"]
                if w.get("sql_status") == "withheld"]
    if not a.violations and not a.unavailable:
        if withheld:
            print(f"  Every published window reconciles. Withheld (nothing published, "
                  f"nothing exposed): {', '.join(withheld)}.")
        else:
            print("  Every supported window reconciles. Lifecycle-event timestamp gaps "
                  "are disclosed above and do not affect the cohort.")
    print(f"\n  exit {exit_code}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of acquisition-cohort Campaign Evidence")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    args = parser.parse_args()

    try:
        from db.connection import init_pool

        init_pool()
    except Exception as exc:  # noqa: BLE001
        payload = {"audit_complete": False, "external_writes_performed": False,
                   "unavailable": [f"database pool unavailable: {exc}"]}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database pool unavailable: {exc}")
        return EXIT_UNAVAILABLE

    a, report = run()
    exit_code = a.exit_code
    report["audit_complete"] = not a.unavailable
    report["violations"] = a.violations
    report["unavailable"] = a.unavailable
    report["checks"] = a.checks
    report["exit_code"] = exit_code

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report, a, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
