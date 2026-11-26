#!/usr/bin/env python3
"""
scripts/audit_marketing_outcome_cohorts.py

PR-ADS-161B — read-only audit of the acquisition-cohort outcomes the product
publishes. PR-ADS-177C: the per-window certification moved from the retired
Campaign Evidence page onto the live Campaigns surface (the Revenue Decision
Mart's SETTLED campaign view, which the Campaigns page and Dashboard →
Campaigns read).

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
The Campaigns surface is built by ``services.revenue_decision_mart``. This
audit does not certify that payload with the code that produced it. For every
business window it re-derives, in SQL, over the surface's OWN settled days, how
many canonical contacts were created, how many canonical lifecycle evidence
proves reached SQL, how many of those are distinct contacts, how many have no
exact SQL-entry date, and how many have a Paid Search original source, and
compares the payload's published cohort breakdown against those numbers
(``audit_campaign_cohort``).

PR-ADS-177A — one SQL count
---------------------------
The Revenue Decision Mart (campaign and country views) and every Dashboard tab
that republishes it (Overview, Campaigns, Countries, Revenue, Deals) now carry
the SAME acquisition cohort. For every business window this audit also builds
those payloads and requires: when the cohort verdict is not ``published``, no
lead or SQL count is present anywhere they publish one
(``mart_withheld_exposures``); when it is published, the rows conserve the
total (campaign rows + campaign residual, country rows + residual row, and
Dashboard Countries' markets + residual all equal ``summary.sqls``).

PR-ADS-180B — the cohort's other funnel stages
-----------------------------------------------
The cohort also publishes how many of its contacts have reached MQL,
Opportunity and the Customer stage (``totals.reached_*``, and the Overview's
all-source ``lifecycle_acquisition_funnel``). Withheld, none is present
(``mart_withheld_exposures``); published, each obeys the cohort's identities
(``cohort_stage_problems``), the Overview block equals the cohort's
all-sources slot (``acquisition_funnel_problems``), and this audit's own SQL
re-derives every stage (``independent_counts`` → ``stage_reach``).

Nothing here writes. Not to HubSpot, not to Google Ads, not to the local
database.
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

from scripts.audit_sql_coverage_gate import BOUND_COLUMN  # noqa: E402

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

_COHORT_SERVICE = _ROOT / "services" / "marketing_outcome_cohort_service.py"

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
_META_SQL_FIELDS = ("mapped_count", "unattributed_count", "excluded_non_google_count")
#: The lifecycle-event disclosure's reached-SQL population counts. Read over
#: the same contact funnel, so withheld with the cohort (round 3: the first
#: version of this scan missed them, and the page showed 1,531 beside "no SQL
#: count is published").
_LIFECYCLE_SQL_FIELDS = ("reached_sql_by_current_stage", "exact_direct_timestamp",
                         "recovered_timestamp", "missing_exact_timestamp")


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-177A — the mart and the Dashboard payloads that republish the count
# ═════════════════════════════════════════════════════════════════════════════
#: Every lead / SQL count a mart- or Dashboard-shaped payload publishes from the
#: acquisition cohort, by location. Named, not discovered by key: the same
#: payloads ALSO carry other families on purpose (the Overview's lifecycle
#: stage-entry counts and the Pipeline page's funnel (PR-ADS-178B: lifecycle
#: stage entries, never the cohort), the source mix's legacy groups), each
#: declared as such, and a key-name walk would call
#: those exposures or — worse — be "fixed" by renaming them.
_MART_SUMMARY_FIELDS = ("leads", "sqls")
_MART_ROW_FIELDS = ("leads", "sqls")
_COHORT_TOTAL_FIELDS = ("leads", "sqls", "sqls_missing_event_timestamp",
                        # PR-ADS-180B: the cohort's other funnel stages.
                        "reached_mql", "reached_opportunity", "reached_customer_stage")
#: PR-ADS-180B — the cohort's funnel stages beside SQL: the breakdown slot
#: field each ``cohort.totals`` field equals (the Google Ads slot).
_STAGE_FIELDS = ("reached_mql", "reached_opportunity", "reached_customer_stage")
#: PR-ADS-180B — the Overview funnel (``lifecycle_acquisition_funnel``): its
#: ``totals`` key → the all-sources breakdown slot field it republishes.
_ACQ_FUNNEL_FIELDS = {"contacts": "contacts_acquired", "reached_mql": "reached_mql",
                      "reached_sql": "sqls", "reached_opportunity": "reached_opportunity",
                      "reached_customer_stage": "reached_customer_stage"}
#: PR-ADS-182A — every field of the cohort's source-group partition that must
#: add back to the all-sources slot.
_SOURCE_GROUP_FIELDS = ("contacts_acquired", "sqls", "sqls_missing_event_timestamp",
                        "reached_mql", "reached_opportunity", "reached_customer_stage")
_DASH_KPI_FIELDS = ("leads", "sqls", "sqls_residual", "leads_residual",
                    "google_ads_cohort_sqls", "campaign_row_cohort_sqls")
#: The marker an itemised outside-market sub-row carries instead of a count.
_IN_RESIDUAL = "in_residual"


def mart_withheld_exposures(payload: dict) -> list[str]:
    """Every place a withheld acquisition-cohort count is still present in a
    Revenue Decision Mart payload (any view) or a Dashboard payload built on it.

    Empty when the payload's own ``cohort`` verdict is ``published`` (nothing
    to withhold) — and when it is not, empty only if no lead / SQL count, no
    cohort total, no campaign residual, no bucket count and no row claiming a
    ``published`` SQL status survives anywhere the payload publishes one.
    Applies to: the mart's ``summary`` (and its metrics), ``rows``,
    ``country_totals``, ``regional_mix``, ``sql_reconciliation`` and
    ``cohort``; a Dashboard's ``kpis``, ``campaigns`` / ``countries`` rows,
    ``residual``, ``regional_mix`` and ``campaign_breakdown``.
    """
    cohort = payload.get("cohort") or {}
    if cohort.get("sql_status") == "published":
        return []
    out: list[str] = []

    def _num(v):
        return v is not None and v != _IN_RESIDUAL

    summary = payload.get("summary") or {}
    out += [f"summary.{k}" for k in _MART_SUMMARY_FIELDS if _num(summary.get(k))]
    for k in _MART_SUMMARY_FIELDS:
        m = ((summary.get("metrics") or {}).get(k) or {})
        if m.get("value") is not None or m.get("status") == "published":
            out.append(f"summary.metrics.{k}")
    out += [f"cohort.totals.{k}" for k in _COHORT_TOTAL_FIELDS
            if (cohort.get("totals") or {}).get(k) is not None]
    out += [f"cohort.campaign_residual.{k}" for k in ("leads", "sqls")
            if (cohort.get("campaign_residual") or {}).get(k) is not None]
    # PR-ADS-180B-F1 (m4): `cohort.contacts_without_created_at` is NOT scanned
    # here: it is a table-wide data-quality disclosure (every canonical
    # contact, no window), published whatever the verdict, exactly as
    # `readiness.missing_contact_created_at_count` and the coverage notes
    # publish it. Its copies are held equal by
    # :func:`created_at_disclosure_problems` instead.
    # PR-ADS-180B: the Overview's acquisition-cohort funnel (all sources);
    # PR-ADS-182A: the Pipeline page's (and Dashboard → Deals', which
    # republishes it) — the same block from the same producer.
    for key in ACQUISITION_FUNNEL_BLOCK_KEYS:
        out += acquisition_funnel_exposures(payload.get(key), published=False, label=key)
    meta = cohort.get("metadata") or {}
    out += [f"cohort.metadata.{k}" for k in _META_SQL_FIELDS if meta.get(k) is not None]
    # PR-ADS-180C (carried minor a): the coverage notes are a route too. A
    # withheld block must not say "4 cohort SQL contact(s) are proven by
    # lifecycle stage…" — a floor on the SQL count it withholds — nor lead
    # with any other cohort count. Only the table-wide no-created-date
    # disclosure may state a number (held by created_at_disclosure_problems).
    out += [f"cohort.metadata.coverage_notes[{i}]"
            for i, note in enumerate(meta.get("coverage_notes") or [])
            if withheld_note_states_a_count(note)]
    # PR-ADS-177C: the published breakdown, and the lifecycle-event counts.
    if cohort.get("breakdown") is not None:
        out.append("cohort.breakdown")
    lc = cohort.get("lifecycle_event_coverage") or {}
    out += [f"cohort.lifecycle_event_coverage.{k}" for k in _LIFECYCLE_SQL_FIELDS
            if lc.get(k) is not None]
    recon = payload.get("sql_reconciliation") or {}
    out += [f"sql_reconciliation.{k}" for k in ("consumer_count", "campaign_residual_sqls")
            if recon.get(k) is not None]

    def _rows(label, rows):
        found = []
        for i, r in enumerate(rows or []):
            name = r.get("campaign_name") or r.get("country") or r.get("country_name") or i
            found += [f"{label}[{name}].{k}" for k in _MART_ROW_FIELDS if _num(r.get(k))]
            if r.get("sqls_status") == "published":
                found.append(f"{label}[{name}].sqls_status=published")
            for k in _MART_ROW_FIELDS:
                m = ((r.get("metrics") or {}).get(k) or {})
                if m.get("value") is not None or m.get("status") == "published":
                    found.append(f"{label}[{name}].metrics.{k}")
            for item in r.get("outside_listed_market_items") or []:
                found += [f"{label}[{name}].items[{item.get('country_code')}].{k}"
                          for k in _MART_ROW_FIELDS if _num(item.get(k))]
        return found

    out += _rows("rows", payload.get("rows"))
    out += _rows("campaigns", payload.get("campaigns"))
    out += _rows("countries", payload.get("countries"))
    out += _rows("campaign_breakdown", payload.get("campaign_breakdown"))
    out += _rows("regional_mix", payload.get("regional_mix"))
    totals = payload.get("country_totals") or {}
    out += [f"country_totals.{k}" for k in _MART_ROW_FIELDS if _num(totals.get(k))]
    for k in _MART_ROW_FIELDS:
        m = ((totals.get("metrics") or {}).get(k) or {})
        if m.get("value") is not None or m.get("status") == "published":
            out.append(f"country_totals.metrics.{k}")
    kpis = payload.get("kpis") or {}
    for k in _DASH_KPI_FIELDS:
        v = kpis.get(k)
        if isinstance(v, dict):
            # PR-ADS-178B: the Pipeline builder publishes each KPI as
            # {value, status, reason}; a withheld one carries no value.
            if v.get("value") is not None or v.get("status") == "published":
                out.append(f"kpis.{k}.value")
        elif _num(v):
            out.append(f"kpis.{k}")
    if kpis.get("sqls_status") == "published":
        out.append("kpis.sqls_status=published")
    residual = payload.get("residual") or {}
    out += [f"residual.{k}" for k in _MART_ROW_FIELDS if _num(residual.get(k))]
    # PR-ADS-177B: the campaign view's page totals and pinned residual row
    # (the mart's and Dashboard → Campaigns').
    ctot = payload.get("campaign_totals") or {}
    out += [f"campaign_totals.{k}" for k in _MART_ROW_FIELDS if _num(ctot.get(k))]
    for k in _MART_ROW_FIELDS:
        m = ((ctot.get("metrics") or {}).get(k) or {})
        if m.get("value") is not None or m.get("status") == "published":
            out.append(f"campaign_totals.metrics.{k}")
    out += _rows("campaign_residual_row", [payload["campaign_residual_row"]]
                 if payload.get("campaign_residual_row") else [])
    return out


#: PR-ADS-182A — the payload keys an acquisition-cohort funnel block lives
#: under: the Overview's and the Pipeline page's (one producer,
#: ``dashboard_overview_service.acquisition_funnel_block``).
ACQUISITION_FUNNEL_BLOCK_KEYS = ("lifecycle_acquisition_funnel", "funnel")


def _is_acquisition_block(block) -> bool:
    return isinstance(block, dict) and "publication" in block and "totals" in block


def acquisition_funnel_exposures(block, *, published: bool,
                                 label: str = "lifecycle_acquisition_funnel") -> list[str]:
    """PR-ADS-180B — every count still present in an acquisition funnel
    block (Overview ``lifecycle_acquisition_funnel``; PR-ADS-182A also
    Pipeline ``funnel``) that must carry none: under a cohort that is not
    published (``published=False``), or whenever the block's OWN publication
    is not ``published``. Empty when ``block`` is None."""
    if not _is_acquisition_block(block):
        return []
    own = (block.get("publication") or {}).get("status")
    if published and own == "published":
        return []
    out = []
    totals = block.get("totals")
    if isinstance(totals, dict):
        out += [f"{label}.totals.{k}" for k, v in totals.items()
                if v is not None]
    elif totals is not None:
        out.append(f"{label}.totals")
    # PR-ADS-180B-F1 (m4): `contacts_without_created_at` is the cohort's
    # table-wide disclosure, not a count of this block (see
    # :func:`created_at_disclosure_problems`).
    if own == "published":
        out.append(f"{label}.publication=published")
    return out


def cohort_stage_problems(cohort: dict) -> list[str]:
    """PR-ADS-180B — under a PUBLISHED cohort, its other funnel stages obey the
    cohort's own identities: each ``cohort.totals`` stage count is the
    breakdown's Google Ads slot; Google Ads + excluded = all sources for each
    stage; no stage exceeds the contacts acquired; every stage is published
    beside the SQL count (a published SQL count with a missing stage is a
    partial funnel). Empty when the cohort is not published."""
    if (cohort or {}).get("sql_status") != "published":
        return []
    problems = []
    totals = cohort.get("totals") or {}
    bd = cohort.get("breakdown") or {}
    ga, ex, al = (bd.get(k) or {} for k in ("google_ads", "excluded_non_google",
                                             "all_sources"))
    for f in _STAGE_FIELDS:
        t = totals.get(f)
        if t is None:
            problems.append(f"cohort.totals.{f} is not published beside cohort.totals.sqls")
            continue
        if totals.get("leads") is not None and t > totals["leads"]:
            problems.append(f"cohort.totals.{f} {t} exceeds the leads {totals['leads']}")
        if not bd:
            continue
        if ga.get(f) != t:
            problems.append(f"cohort.totals.{f} {t} != breakdown Google Ads {ga.get(f)}")
        if None in (ga.get(f), ex.get(f), al.get(f)) or ga[f] + ex[f] != al[f]:
            problems.append(f"breakdown {f}: all {al.get(f)} != Google Ads {ga.get(f)} "
                            f"+ excluded {ex.get(f)}")
        if al.get(f) is not None and al.get("contacts_acquired") is not None \
                and al[f] > al["contacts_acquired"]:
            problems.append(f"breakdown all_sources.{f} exceeds the contacts acquired")
    # PR-ADS-182A: the all-source partition by source group (the Pipeline
    # Source filter reads one part) conserves every all-sources field.
    # (A breakdown without the partition publishes no source-group funnel:
    # the block producer reads that as counts missing — fail closed.)
    groups = bd.get("by_source_group") if bd else None
    if isinstance(groups, dict):
        for f in _SOURCE_GROUP_FIELDS:
            parts = [g.get(f) for g in groups.values()]
            if al.get(f) is None or None in parts or sum(parts) != al[f]:
                problems.append(f"breakdown by_source_group {f}: groups {parts} do not "
                                f"sum to all_sources {al.get(f)}")
    return problems


def acquisition_funnel_problems(payload: dict,
                                key: str = "lifecycle_acquisition_funnel") -> list[str]:
    """PR-ADS-180B — an acquisition funnel block against the cohort it
    republishes: the Overview's (``lifecycle_acquisition_funnel``) and,
    PR-ADS-182A, the Pipeline page's (``funnel``).

    The block must exist, state the cohort's family and window basis, and
    carry the cohort's verdict (its own ``unavailable`` for a published slot
    that lacks a count, or — Pipeline — for a lead filter the cohort cannot
    honour, is the one narrowing). Unpublished: no count anywhere
    (:func:`acquisition_funnel_exposures`). Published: every column EQUALS
    the cohort breakdown's slot it names — ``all_sources``, or
    ``by_source_group[<group>]`` for a Pipeline Source filter — the one
    cohort build both are read from, and no stage exceeds the contacts
    acquired. A block that does not honour its filter publishes nothing."""
    block = payload.get(key)
    cohort = payload.get("cohort") or {}
    if not _is_acquisition_block(block):
        return [f"the {'Overview' if key == 'lifecycle_acquisition_funnel' else 'Pipeline'} "
                f"carries no {key} block"]
    problems = []
    for meta in ("metric_family", "window_basis"):
        if block.get(meta) != cohort.get(meta):
            problems.append(f"{key}.{meta} {block.get(meta)!r} != "
                            f"cohort {cohort.get(meta)!r}")
    own = (block.get("publication") or {}).get("status")
    status = cohort.get("sql_status")
    filters = block.get("lead_filters") if isinstance(block.get("lead_filters"), dict) else {}
    unhonoured = filters.get("honoured") is False
    if unhonoured:
        if own != "unavailable" or (block.get("publication") or {}).get("reason") \
                != filters.get("unavailable_reason"):
            problems.append(f"{key} does not honour its lead filter but is {own!r}, "
                            "not unavailable with the filter's reason")
    elif own != status and not (own == "unavailable" and status == "published"):
        problems.append(f"{key} is {own!r} under a {status!r} cohort")
    problems += acquisition_funnel_exposures(
        block, published=status == "published" and not unhonoured, label=key)
    # PR-ADS-180B-F1 (m4): published whatever the verdict, so equal whatever it is.
    if block.get("contacts_without_created_at") != cohort.get("contacts_without_created_at"):
        problems.append(f"{key}.contacts_without_created_at differs from the cohort's")
    if own != "published" or status != "published":
        return problems
    totals = block.get("totals") or {}
    group = block.get("source_group")
    bd = cohort.get("breakdown") or {}
    slot = (bd.get("all_sources") if group is None
            else (bd.get("by_source_group") or {}).get(group)) or {}
    where = "all_sources" if group is None else f"by_source_group[{group}]"
    for col, field in _ACQ_FUNNEL_FIELDS.items():
        if totals.get(col) is None or totals.get(col) != slot.get(field):
            problems.append(f"{key}.totals.{col} {totals.get(col)} "
                            f"!= cohort breakdown {where}.{field} {slot.get(field)}")
        elif col != "contacts" and totals[col] > totals.get("contacts", 0):
            problems.append(f"{key}.totals.{col} exceeds the contacts")
    return problems


#: The coverage note that states the no-created-date count (the cohort
#: service's ``coverage_notes``), as a pattern: its number is read back.
_NO_CREATED_DATE_NOTE = re.compile(r"^(\d+) canonical contact\(s\) have no created date")


#: PR-ADS-180C: a coverage note stating a cohort SQL figure (the published
#: cohort's event-timestamp gap note), and any note that leads with a count.
_COHORT_SQL_NOTE = re.compile(r"^\d+ cohort SQL contact")
_COUNT_LED_NOTE = re.compile(r"^\s*\d")


def withheld_note_states_a_count(note) -> bool:
    """True when a coverage note of an UNPUBLISHED cohort states a figure it
    may not: the cohort-SQL gap note, or any note that leads with a count
    other than the table-wide no-created-date disclosure."""
    text = str(note)
    if _COHORT_SQL_NOTE.match(text):
        return True
    return bool(_COUNT_LED_NOTE.match(text)) and not _NO_CREATED_DATE_NOTE.match(text.strip())


def created_at_disclosure_problems(payload: dict) -> list[str]:
    """PR-ADS-180B-F1 (m4) — the count of canonical contacts with no created
    date is ONE table-wide figure (every contact, all sources, no window). It
    is a data-quality disclosure, not a cohort figure, so it is published
    whatever the cohort verdict — and every copy of it in one payload must be
    the same value:

    * ``cohort.contacts_without_created_at`` (None only when unread);
    * ``readiness.missing_contact_created_at_count`` (mart views);
    * ``lifecycle_acquisition_funnel.contacts_without_created_at`` (Overview);
    * ``cohort.metadata.coverage_notes``: exactly one note naming it when it
      is above 0, none when it is 0 or unknown, never another number.

    Empty for a payload that carries no cohort block."""
    cohort = payload.get("cohort")
    if not isinstance(cohort, dict) or "contacts_without_created_at" not in cohort:
        return []
    value = cohort.get("contacts_without_created_at")
    problems = []
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                              or value < 0):
        problems.append(f"cohort.contacts_without_created_at {value!r} is not a count")
    readiness = payload.get("readiness")
    if isinstance(readiness, dict) and "missing_contact_created_at_count" in readiness \
            and readiness["missing_contact_created_at_count"] != value:
        problems.append(f"readiness.missing_contact_created_at_count "
                        f"{readiness['missing_contact_created_at_count']!r} != "
                        f"cohort.contacts_without_created_at {value!r}")
    for key in ACQUISITION_FUNNEL_BLOCK_KEYS:
        block = payload.get(key)
        if _is_acquisition_block(block) and block.get("contacts_without_created_at") != value:
            problems.append(f"{key}.contacts_without_created_at "
                            f"{block.get('contacts_without_created_at')!r} != "
                            f"cohort.contacts_without_created_at {value!r}")
    notes = (cohort.get("metadata") or {}).get("coverage_notes") or []
    stated = [int(m.group(1)) for m in (_NO_CREATED_DATE_NOTE.match(str(n)) for n in notes)
              if m]
    expected = [value] if isinstance(value, int) and value > 0 else []
    if stated != expected:
        problems.append(f"cohort.metadata.coverage_notes state {stated} contact(s) with no "
                        f"created date; the cohort states {value!r}")
    return problems


def mart_conservation_problems(payload: dict, *, view: str) -> list[str]:
    """Under a PUBLISHED cohort, the rows must add back to ``summary.sqls``.

    * campaign view: Σ campaign rows + ``cohort.campaign_residual``;
    * country view: Σ country rows, the residual row included;
    * Dashboard Countries (``view="dashboard/countries"``): ``kpis.sqls`` +
      ``residual.sqls`` against the cohort's own total.

    A row the country gate withholds makes the sum not evaluable (returns
    nothing): a withheld part cannot disprove conservation.
    """
    cohort = payload.get("cohort") or {}
    if cohort.get("sql_status") != "published":
        return []
    total = (cohort.get("totals") or {}).get("sqls")
    problems = []
    for field in ("sqls", "leads"):
        want = (cohort.get("totals") or {}).get(field)
        if view == "campaign":
            parts = [r.get(field) for r in payload.get("rows") or []]
            parts.append((cohort.get("campaign_residual") or {}).get(field))
            # PR-ADS-177B: the pinned residual row is the same count.
            rrow = payload.get("campaign_residual_row")
            if rrow is not None and rrow.get(field) != parts[-1]:
                problems.append(f"{view}: campaign_residual_row.{field} "
                                f"{rrow.get(field)} != cohort.campaign_residual "
                                f"{parts[-1]}")
            ct = (payload.get("campaign_totals") or {}).get(field)
            if ct is not None and want is not None and ct != want:
                problems.append(f"{view}: campaign_totals.{field} {ct} != cohort "
                                f"total {want}")
        elif view == "country":
            parts = [r.get(field) for r in payload.get("rows") or []]
        else:
            k = payload.get("kpis") or {}
            parts = [k.get(field), (payload.get("residual") or {}).get(field)]
        if any(p is None for p in parts) or want is None:
            continue
        if sum(parts) != want:
            problems.append(f"{view}: rows + residual {field} = {sum(parts)}, "
                            f"but the cohort total is {want}")
    if view in ("campaign", "country") and (payload.get("summary") or {}).get("sqls") != total:
        problems.append(f"{view}: summary.sqls {(payload.get('summary') or {}).get('sqls')} "
                        f"!= cohort total {total}")
    # PR-ADS-180B: the cohort's other funnel stages (Campaigns and Countries
    # draw them in one funnel block from these totals).
    problems += [f"{view}: {p}" for p in cohort_stage_problems(cohort)]
    return problems


def mart_stale_disclosure_problems(payload: dict) -> list[str]:
    """PR-ADS-177A-F1 (truth audit MAJOR 1) — a cohort published over a STALE
    contact funnel is a count *as of* its watermark (`docs/44` §7). A payload
    that publishes it must say so in machine-readable form: ``stale`` true and
    an ``as_of`` on the block and in its metadata, from which every surface
    prints "measured as of …". A stale published count without them reads as
    current, which it is not.

    Also (MINOR 1): a published cohort declares the last day it counts
    (``window.end_date``) — the campaign and country views end on different
    days and each count says which."""
    c = payload.get("cohort") or {}
    if c.get("sql_status") != "published":
        return []
    meta = c.get("metadata") or {}
    reason = (meta.get("source_freshness") or {}).get("reason")
    problems = []
    if reason == "source_stale":
        if c.get("stale") is not True:
            problems.append("cohort.stale is not true over a source_stale funnel")
        if not c.get("as_of"):
            problems.append("cohort.as_of is missing over a source_stale funnel")
        if not meta.get("as_of"):
            problems.append("cohort.metadata.as_of is missing over a source_stale funnel")
    elif c.get("stale"):
        problems.append(f"cohort.stale is true but the funnel is {reason!r}")
    if not ((c.get("window") or {}).get("end_date")):
        problems.append("cohort.window.end_date is not declared")
    return problems


def audit_mart_window(a: Audit, *, window: str, payloads: dict) -> dict:
    """PR-ADS-177A — every mart / Dashboard payload of one business window.

    ``payloads``: name → payload (``mart/campaign``, ``mart/country``,
    ``dashboard/overview``, ``dashboard/campaigns``, ``dashboard/countries``,
    ``dashboard/revenue``, ``dashboard/deals``, PR-ADS-178B ``pipeline``)."""
    w = f"[business:{window}]"
    out = {"window": window, "surfaces": {}}
    for name, payload in payloads.items():
        if payload is None:
            a.cannot_check(f"{w} {name}", "the payload could not be built")
            continue
        status = (payload.get("cohort") or {}).get("sql_status")
        out["surfaces"][name] = {"sql_status": status,
                                 "sql_reason": (payload.get("cohort") or {}).get("sql_reason")}
        if status is None:
            a.broken(f"{w} {name} cohort_declared",
                     "the payload carries no acquisition-cohort verdict")
            continue
        exposed = mart_withheld_exposures(payload)
        if exposed:
            a.broken(f"{w} {name} withheld_not_exposed",
                     f"the cohort is {status!r} but a count is present at: {exposed[:6]}")
        elif status != "published":
            a.holds(f"{w} {name} withheld_not_exposed",
                    f"cohort {status} ({(payload.get('cohort') or {}).get('sql_reason')}); "
                    "no lead or SQL count is present")
        stale_problems = mart_stale_disclosure_problems(payload)
        if stale_problems:
            a.broken(f"{w} {name} stale_disclosed", "; ".join(stale_problems))
        elif status == "published":
            a.holds(f"{w} {name} stale_disclosed",
                    "the published cohort declares its window end and, when "
                    "the funnel is stale, its as-of watermark")
        # PR-ADS-180B-F1 (m4): the no-created-date disclosure, one value in
        # every copy, published whatever the verdict.
        disclosure = created_at_disclosure_problems(payload)
        if disclosure:
            a.broken(f"{w} {name} created_at_disclosure", "; ".join(disclosure))
        elif "contacts_without_created_at" in (payload.get("cohort") or {}):
            a.holds(f"{w} {name} created_at_disclosure",
                    "the no-created-date count is one value in every copy "
                    f"({(payload.get('cohort') or {}).get('contacts_without_created_at')!r})")
        # PR-ADS-180B: the Overview's acquisition-cohort funnel; PR-ADS-182A:
        # the Pipeline page's (and Dashboard → Deals', which republishes it).
        funnel_key = {"dashboard/overview": "lifecycle_acquisition_funnel",
                      "pipeline": "funnel", "dashboard/deals": "funnel"}.get(name)
        if funnel_key:
            acq = acquisition_funnel_problems(payload, funnel_key)
            if acq:
                a.broken(f"{w} {name} acquisition_funnel", "; ".join(acq[:6]))
            else:
                a.holds(f"{w} {name} acquisition_funnel",
                        "the acquisition funnel carries the cohort's "
                        "verdict and family; " + ("its columns equal the cohort's "
                                                  "slot" if status == "published"
                                                  else "no count is present"))
        view = ("campaign" if name in ("mart/campaign", "mart/campaign_settled")
                else "country" if name == "mart/country" else name)
        if view in ("campaign", "country", "dashboard/countries"):
            problems = mart_conservation_problems(payload, view=view)
            if problems:
                a.broken(f"{w} {name} conservation", "; ".join(problems))
            elif status == "published":
                a.holds(f"{w} {name} conservation",
                        "rows plus residual equal the cohort total")
    return out


#: Any payload key that would carry a cost-per-SQL. No live campaign surface
#: publishes one (PR-ADS-177B dropped CPQL with the lead-quality columns;
#: PR-ADS-177C retired the only page that did).
_CPQL_MARKER = "cpql"


def _cpql_keys(node, path: str = "") -> list[str]:
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else str(k)
            if _CPQL_MARKER in str(k).lower():
                found.append(here)
            found += _cpql_keys(v, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _cpql_keys(v, f"{path}[{i}]")
    return found


def audit_campaign_cohort(a: Audit, *, window: str, payload: dict,
                          independent: dict | None) -> dict:
    """Every per-window cohort guarantee, against the live Campaigns surface
    (the mart's SETTLED campaign view, ``mart/campaign_settled``).

    PR-ADS-177C — ported from the retired Campaign Evidence ``audit_window``.
    ``independent``: ``{"contacts_acquired", "sqls", "stage_only_sqls",
    "distinct_sql_contact_ids", "paid_search_sourced_sqls"}`` computed in SQL
    by this audit over the payload's own settled days. A window whose count
    is not published must expose nothing (``mart_withheld_exposures``); a
    funnel the surface could not read is ``cannot_check``.
    """
    w = f"[campaigns:{window}]"
    cohort = payload.get("cohort") or {}
    meta = cohort.get("metadata") or {}
    rows = payload.get("rows") or []
    status = cohort.get("sql_status")

    cpql = _cpql_keys(payload)
    if cpql:
        a.broken(f"{w} no_cpql_published", f"a cost-per-SQL key is present: {cpql[:5]}")
    else:
        a.holds(f"{w} no_cpql_published", "no CPQL key anywhere in the payload")

    lc = cohort.get("lifecycle_event_coverage") or {}
    if lc.get("published_on_this_page") is not False:
        a.broken(f"{w} lifecycle_events_not_relabelled",
                 "the surface does not declare lifecycle-event SQLs unpublished")
    else:
        a.holds(f"{w} lifecycle_events_not_relabelled",
                "lifecycle-event SQLs are declared not published here")

    if status != "published":
        exposed = mart_withheld_exposures(payload)
        if exposed:
            a.broken(f"{w} withheld_not_exposed",
                     f"SQL count is {status!r} ({cohort.get('sql_reason')}) but is still "
                     f"present at: {exposed[:6]}")
        else:
            a.holds(f"{w} withheld_not_exposed",
                    f"SQL count {status} ({cohort.get('sql_reason')}); no lead or SQL "
                    f"count, breakdown or CPQL is present anywhere in the payload")
        if status == "unavailable" or status is None:
            a.cannot_check(f"{w} cohort", f"the cohort is unavailable "
                           f"({cohort.get('sql_reason')}); nothing to reconcile")
        return {"window": window, "available": status == "withheld",
                "published": False, "sql_status": status,
                "sql_reason": cohort.get("sql_reason"), "as_of": meta.get("as_of"),
                "source_fresh": (meta.get("source_freshness") or {}).get("fresh")}

    breakdown = cohort.get("breakdown") or {}
    if not breakdown:
        a.broken(f"{w} cohort", "the SQL count is published but its breakdown is "
                 "missing, so it cannot be reconciled")
        return {"window": window, "available": False, "published": True}
    if independent is None:
        a.cannot_check(f"{w} independent", "the database is unavailable")
        return {"window": window, "available": False, "published": True}

    alls = breakdown["all_sources"]
    # 1. window membership is contact_created_at
    if meta.get("window_basis") != "contact_created_at":
        a.broken(f"{w} window_basis", f"declared {meta.get('window_basis')!r}")
    elif alls["contacts_acquired"] != independent["contacts_acquired"]:
        a.broken(f"{w} membership", f"payload acquired {alls['contacts_acquired']} "
                 f"contacts; created_at alone selects {independent['contacts_acquired']}")
    elif breakdown.get("rows_outside_window"):
        a.broken(f"{w} membership", f"{breakdown['rows_outside_window']} rows "
                 f"outside the window reached the cohort read")
    else:
        a.holds(f"{w} membership", f"{independent['contacts_acquired']} contacts, "
                f"selected by contact_created_at")

    # 2. SQL proof is canonical lifecycle evidence
    if meta.get("outcome_basis") != "latest_canonical_lifecycle_evidence":
        a.broken(f"{w} outcome_basis", f"declared {meta.get('outcome_basis')!r}")
    elif alls["sqls"] != independent["sqls"]:
        a.broken(f"{w} sql_proof", f"payload has {alls['sqls']} cohort SQLs; "
                 f"canonical evidence proves {independent['sqls']}")
    else:
        a.holds(f"{w} sql_proof", f"{independent['sqls']} SQLs, each proven by a "
                f"direct or recovered SQL-entry date or a stage implying SQL")

    # 2b. PR-ADS-180B: the cohort's other funnel stages, each against this
    # audit's own SQL over the same days and the same contacts.
    stage_gaps = [f"{f}: payload {alls.get(f)}, canonical evidence {independent.get(f)}"
                  for f in _STAGE_FIELDS
                  if independent.get(f) is None or alls.get(f) != independent.get(f)]
    if stage_gaps:
        a.broken(f"{w} stage_reach", "; ".join(stage_gaps))
    else:
        a.holds(f"{w} stage_reach", ", ".join(f"{f} {independent[f]}" for f in _STAGE_FIELDS)
                + " — each proven by a direct or recovered stage-entry date or a "
                  "stage at or beyond it")
    stage_problems = cohort_stage_problems(cohort)
    if stage_problems:
        a.broken(f"{w} stage_conservation", "; ".join(stage_problems[:6]))
    else:
        a.holds(f"{w} stage_conservation", "each stage: totals = Google Ads slot; "
                "Google Ads + excluded = all sources; never above the contacts acquired")

    # 3. contacts deduplicated
    if meta.get("dedup_key") != "contact_id":
        a.broken(f"{w} dedup", f"declared dedup key {meta.get('dedup_key')!r}")
    elif independent["distinct_sql_contact_ids"] != alls["sqls"]:
        a.broken(f"{w} dedup", f"{alls['sqls']} SQLs vs "
                 f"{independent['distinct_sql_contact_ids']} distinct contact ids")
    else:
        a.holds(f"{w} dedup", "one SQL per distinct contact_id "
                f"(fallback identities: {breakdown['dedup']['fallback_identities']}, "
                f"duplicate rows collapsed: {breakdown['dedup']['duplicate_rows']})")

    # 4. the published totals ARE the breakdown's Google Ads bucket; all
    #    sources = Google Ads + excluded; rows + residual = Google Ads.
    ga = breakdown["google_ads"]["sqls"]
    ex = breakdown["excluded_non_google"]["sqls"]
    al = alls["sqls"]
    totals = cohort.get("totals") or {}
    row_sqls = [r.get("sqls") for r in rows]
    residual = (cohort.get("campaign_residual") or {}).get("sqls")
    problems = []
    if totals.get("sqls") != ga:
        problems.append(f"cohort.totals.sqls {totals.get('sqls')} != breakdown "
                        f"Google Ads {ga}")
    if ga + ex != al:
        problems.append(f"all {al} != Google Ads {ga} + excluded {ex}")
    if any(v is None for v in row_sqls) or residual is None:
        problems.append("a campaign row or the residual carries no SQL count under "
                        "a published verdict")
    elif sum(row_sqls) + residual != ga:
        problems.append(f"campaign rows {sum(row_sqls)} + residual {residual} != "
                        f"Google Ads {ga}")
    if problems:
        a.broken(f"{w} bucket_reconciliation", "; ".join(problems))
    else:
        a.holds(f"{w} bucket_reconciliation",
                f"Google Ads {ga} = campaign rows {sum(row_sqls)} + residual "
                f"{residual}; all {al} = Google Ads {ga} + excluded {ex}")

    # 4b. the Google Ads / excluded SPLIT, against an independent source rule.
    # Every Paid Search SQL is either a Google Ads SQL or excluded ONLY because
    # an approved mapping says its label is not Google Ads.
    by_mapping = ((breakdown.get("excluded_non_google") or {}).get("by_reason")
                  or {}).get("label_mapped_not_google_ads", 0)
    paid = independent.get("paid_search_sourced_sqls")
    if paid is None:
        a.cannot_check(f"{w} google_ads_split", "the independent Paid Search count "
                       "is unavailable")
    elif ga + by_mapping != paid:
        a.broken(f"{w} google_ads_split",
                 f"{paid} SQLs have a Paid Search original source, but the surface "
                 f"has {ga} Google Ads SQLs + {by_mapping} excluded by an approved "
                 f"not-Google-Ads mapping")
    else:
        a.holds(f"{w} google_ads_split",
                f"{paid} Paid Search-sourced SQLs = {ga} Google Ads + {by_mapping} "
                f"excluded by approved mapping")

    # 5. lifecycle-event gaps disclosed and counted, not dated
    gap = alls["sqls_missing_event_timestamp"]
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

    return {
        "window": window, "available": True, "published": True,
        "sql_status": status, "sql_reason": None, "as_of": meta.get("as_of"),
        "source_fresh": (meta.get("source_freshness") or {}).get("fresh"),
        "settled_days": (cohort.get("window") or {}),
        "cohort": {"contacts_acquired_all_sources": alls["contacts_acquired"],
                   "google_ads_sqls": ga, "campaign_row_sqls": sum(row_sqls),
                   "residual_sqls": residual, "excluded_non_google_sqls": ex,
                   "all_source_sqls": al, "sqls_missing_event_timestamp": gap},
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

    from analysis.crm_lifecycle import EVENT_CUSTOMER, EVENT_MQL, EVENT_OPPORTUNITY

    stages = list(stages_implying_event(EVENT_SQL))
    direct = repo.direct_date_sql(EVENT_SQL)
    recovered = repo.recovered_date_sql(EVENT_SQL)
    proven = (f"({direct} IS NOT NULL OR {recovered} IS NOT NULL "
              f"OR lower(btrim(f.lifecycle_stage)) = ANY(%s))")
    # PR-ADS-180B: the cohort's other funnel stages, decided in SQL by the same
    # three kinds of evidence (direct date, recovered date, a stage at or
    # beyond it) — not by the service's Python.
    reach = {field: (f"({repo.direct_date_sql(e)} IS NOT NULL OR "
                     f"{repo.recovered_date_sql(e)} IS NOT NULL OR "
                     f"lower(btrim(f.lifecycle_stage)) = ANY(%s))",
                     list(stages_implying_event(e)))
             for field, e in (("reached_mql", EVENT_MQL),
                              ("reached_opportunity", EVENT_OPPORTUNITY),
                              ("reached_customer_stage", EVENT_CUSTOMER))}
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
                           COUNT(*) FILTER (WHERE {proven} AND {paid}),
                           {", ".join(f"COUNT(*) FILTER (WHERE {pred})"
                                      for pred, _st in reach.values())}
                    FROM {repo.FUNNEL_TABLE} f
                    {repo._recovery_join()}
                    WHERE f.created_at IS NOT NULL
                      AND (%s::timestamptz IS NULL OR f.created_at >= %s)
                      AND f.created_at < %s
                    """,
                    (stages, stages, stages, stages, PY_WHITESPACE_RUN,
                     *(st for _pred, st in reach.values()),
                     start_at, start_at, end_before),
                )
                row = cur.fetchone()
                conn.rollback()
    except Exception:  # noqa: BLE001
        return None
    return {"contacts_acquired": row[0], "sqls": row[1],
            "stage_only_sqls": row[2], "distinct_sql_contact_ids": row[3],
            "paid_search_sourced_sqls": row[4],
            **{field: row[5 + i] for i, field in enumerate(reach)}}


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
def _settled_instants(payload: dict):
    """The UTC instants of the surface's OWN settled days (``cohort.window``),
    through the cohort service's one ``window_instants`` — or None."""
    from datetime import date as _date  # noqa: PLC0415
    from services import marketing_outcome_cohort_service as cohort_svc  # noqa: PLC0415
    w = ((payload or {}).get("cohort") or {}).get("window") or {}
    if not w.get("end_date"):
        return None
    start = _date.fromisoformat(w["start_date"]) if w.get("start_date") else None
    return cohort_svc.window_instants(start, _date.fromisoformat(w["end_date"]))


def run(now: datetime | None = None) -> tuple[Audit, dict]:
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

    # PR-ADS-177A: the mart and the Dashboard payloads, every business window.
    # PR-ADS-177C: the Campaigns surface (``mart/campaign_settled``) is ALSO
    # certified against this audit's independent SQL over its own days.
    #
    # PR-ADS-179A: each window is read inside ONE snapshot
    # (``services.snapshot_memo.certification_window``): every payload below,
    # and this audit's own independent SQL, read the database as of one
    # instant, and the inputs the payloads share (the mart core, the won-deal
    # population) are built once per window instead of once per payload.
    # Nothing is carried from one window to the next.
    #
    # PR-ADS-179A-F1: what this certifies is a SNAPSHOT-CONSISTENT build of
    # each page. A web request reads statement by statement under READ
    # COMMITTED, so a read torn by a sync committing mid-request is invisible
    # here by construction (before, it was visible only by chance). A read the
    # snapshot refused — even one a reader swallowed — makes the window
    # cannot_check (``refused_reads``).
    #
    # PR-ADS-179B: a window that could not pin its snapshot (the schema lock
    # held past its bound) runs unpinned, as before 179A — and the report
    # says so per window (``snapshot``) and in ``snapshot_pinning``. The exit
    # code does not change.
    from analysis.business_windows import WINDOW_KEYS
    from services.snapshot_memo import (
        certification_window, pinning_summary, refused_reads, window_snapshot,
    )
    report["business_windows"] = []
    pinning = []
    for bw in WINDOW_KEYS:
        disclosure: dict = {}
        with certification_window(disclosure) as scope:
            payloads = _build_mart_payloads(bw, now)
            settled = payloads.get("mart/campaign_settled")
            instants = _settled_instants(settled) if settled is not None else None
            independent = independent_counts(*instants) if instants else None
        for refusal in refused_reads(scope):
            a.cannot_check(f"[business:{bw}] pinned snapshot",
                           f"a read in this window was refused by its pinned "
                           f"snapshot: {refusal}")
        snapshot = window_snapshot(scope, disclosure)
        pinning.append((bw, snapshot))
        report["business_windows"].append({**audit_mart_window(
            a, window=bw, payloads=payloads), "snapshot": snapshot})
        if settled is None:
            a.cannot_check(f"[campaigns:{bw}] build", "the settled campaign view "
                           "could not be built")
            report["windows"].append({"window": bw, "available": False})
            continue
        report["windows"].append(audit_campaign_cohort(
            a, window=bw, payload=settled, independent=independent))

    report["snapshot_pinning"] = pinning_summary(pinning)
    report["population_split"] = global_population_split()
    return a, report


def _build_mart_payloads(window: str, now) -> dict:
    """The surfaces that republish the one SQL count, built by production code.
    A builder that raises yields None (``cannot_check``), never a crash."""
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    from services.dashboard_countries_service import build_dashboard_countries
    from services.dashboard_deals_service import build_dashboard_deals
    from services.dashboard_overview_service import build_dashboard_overview
    from services.dashboard_revenue_service import build_dashboard_revenue
    from services.pipeline_service import build_pipeline
    from services.revenue_decision_mart import build_revenue_decision_mart
    builders = {
        "mart/campaign": lambda: build_revenue_decision_mart(view="campaign", window=window, now=now),
        "mart/country": lambda: build_revenue_decision_mart(view="country", window=window, now=now),
        # PR-ADS-177B: the Campaigns page's settled campaign view.
        "mart/campaign_settled": lambda: build_revenue_decision_mart(
            view="campaign", window=window, now=now, settled=True),
        "dashboard/overview": lambda: build_dashboard_overview(window=window, now=now),
        "dashboard/campaigns": lambda: build_dashboard_campaigns(window=window, now=now),
        "dashboard/countries": lambda: build_dashboard_countries(window=window, now=now),
        "dashboard/revenue": lambda: build_dashboard_revenue(window=window, now=now),
        "dashboard/deals": lambda: build_dashboard_deals(window=window, now=now),
        # PR-ADS-178B: the Pipeline page (Dashboard → Deals republishes it).
        "pipeline": lambda: build_pipeline(window, now=now),
    }
    out = {}
    for name, fn in builders.items():
        try:
            out[name] = json.loads(json.dumps(fn(), default=str))
        except Exception:  # noqa: BLE001
            out[name] = None
    return out


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

    rows = [w for w in report["windows"] if w.get("published")]
    if rows:
        print()
        print("  The Campaigns surface (settled days), Google Ads acquisition cohort:")
        print(f"  {'window':<17}{'cohort SQLs':>12}{'rows':>7}{'residual':>10}"
              f"{'excluded':>10}{'undated':>9}")
        for w in rows:
            ch = w["cohort"]
            print(f"  {w['window']:<17}{str(ch['google_ads_sqls']):>12}"
                  f"{str(ch['campaign_row_sqls']):>7}{str(ch['residual_sqls']):>10}"
                  f"{str(ch['excluded_non_google_sqls']):>10}"
                  f"{str(ch['sqls_missing_event_timestamp']):>9}")
        print(f"  as of: {rows[0].get('as_of')}  (source fresh: {rows[0].get('source_fresh')})")

    pinning = report.get("snapshot_pinning") or {}
    if pinning:
        print()
        print(f"  Snapshot: {pinning.get('note')}")

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
        description="Read-only audit of the published acquisition cohort")
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
