#!/usr/bin/env python3
"""
scripts/audit_sql_coverage_gate.py

PR-ADS-160 §7 — the read-only gate that fails when the boundary's guarantees
stop holding.

    python -m scripts.audit_sql_coverage_gate
    python -m scripts.audit_sql_coverage_gate --json

Exit codes
----------
    0  every guarantee holds
    1  a guarantee is BROKEN — the code or the data contradicts the doctrine
    2  a required check could not run; the gate proves nothing and says so

Why a separate gate
-------------------
``audit_lifecycle_sql_coverage`` answers "what is the coverage?". This answers a
narrower and harsher question: "has anything happened that would make the
previous answer a lie?". They are different jobs. A coverage audit can honestly
report an incomplete window forever; this gate must go red the first time a
bound is mistaken for a date, or a proven timestamp disappears.

The six conditions, each a real failure mode rather than a category
------------------------------------------------------------------
1. a post-boundary contact reached SQL with no exact timestamp;
2. an exact SQL timestamp can be overwritten with NULL by a sparse payload —
   checked STRUCTURALLY against the writer, because by the time it shows up in
   data the evidence that it happened is precisely what was destroyed;
3. a boundary observation appears in an event-date field;
4. a consumer reads ``known_reached_sql_by`` as if it were ``date_entered_sql``;
5. a window reported as certified carries unresolved membership;
6. the canonical readers disagree.

Nothing here writes. Not to HubSpot, not to the local database.
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

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNAVAILABLE = 2

_WRITERS_FILE = _ROOT / "db" / "writers.py"

#: The column carrying the boundary's UPPER bound. It may be read by the
#: coverage analysis and by this gate. It may never be assigned into a
#: stage-entry date, and never coalesced with one.
BOUND_COLUMN = "known_reached_sql_by"
#: The stage-entry date columns a bound must never reach.
EVENT_DATE_COLUMNS = (
    "date_entered_lead", "date_entered_mql", "date_entered_sql",
    "date_entered_opportunity", "date_entered_customer",
)

#: Modules allowed to mention the bound at all. Everything else naming it is a
#: consumer that has to be looked at, which is the point of an allow-list: the
#: check fails on a NEW reader, not only on a provably wrong one.
_BOUND_READERS = {
    "analysis/lifecycle_sql_coverage.py",      # rules windows out; never dates
    "db/crm_funnel_repository.py",             # reads the bound as a bound
    "db/writers.py",                           # writes it to its own table
    "db/schema.py",                            # defines it
    "services/sql_coverage_boundary_service.py",
    "scripts/establish_sql_coverage_boundary.py",
    "scripts/audit_lifecycle_sql_coverage.py",
    "scripts/audit_sql_coverage_gate.py",
}


class Gate:
    """Broken guarantees and unavailable checks, kept apart.

    A broken guarantee means the system contradicts its own doctrine. An
    unavailable check means the gate could not look. Merging them would let a
    real breach hide behind an outage, and an outage look like a breach.
    """

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


def check_timestamps_cannot_be_erased(g: Gate) -> dict:
    """§7.2 — a sparse payload must not blank a proven stage-entry date.

    Checked by parsing the writer rather than by looking for erased data,
    because an erasure destroys its own evidence: once `date_entered_sql` is
    NULL there is nothing left to distinguish "we lost it" from "there never was
    one". The structural check is the only one that can fire BEFORE the damage.

    This defect was real and was proven against a live PostgreSQL instance:
    a later payload omitting the property produced ``{'ok': True,
    'persisted': 1}`` and a stored value of ``None``.
    """
    try:
        source = _WRITERS_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        g.cannot_check("stage_dates_not_erasable",
                       f"the writer source could not be read: {exc}")
        return {"available": False, "protected": None}

    tree = ast.parse(source)
    guard = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_contact_funnel_set":
            guard = ast.get_source_segment(source, node) or ""
            break
    if guard is None:
        g.broken("stage_dates_not_erasable",
                 "the contact-funnel upsert has no per-column guard, so a "
                 "sparse HubSpot payload can overwrite a proven stage-entry "
                 "timestamp with NULL and report success")
        return {"available": True, "protected": False}

    unprotected = [c for c in EVENT_DATE_COLUMNS
                   if "COALESCE" not in guard.upper()]
    if unprotected:
        g.broken("stage_dates_not_erasable",
                 "the upsert guard does not COALESCE stage-entry columns, so "
                 "absence in a payload is treated as a correction")
        return {"available": True, "protected": False}

    # The guard exists and coalesces; confirm every event-date column is in the
    # protected set rather than trusting that the list was kept up to date.
    from db import writers  # noqa: PLC0415

    protected = set(getattr(writers, "_CONTACT_FUNNEL_EVIDENCE_COLUMNS", ()))
    missing = [c for c in EVENT_DATE_COLUMNS if c not in protected]
    if missing:
        g.broken("stage_dates_not_erasable",
                 f"stage-entry column(s) {missing} are not in the protected "
                 f"set, so a sparse payload can still blank them")
        return {"available": True, "protected": False, "missing": missing}

    g.holds("stage_dates_not_erasable",
            f"all {len(EVENT_DATE_COLUMNS)} stage-entry columns are refreshed "
            "only from a present value; absence cannot erase evidence")
    return {"available": True, "protected": True}


def _code_lines(text: str) -> list[str]:
    """The module's CODE, with comments and docstrings removed.

    The blending check has to read what the module DOES, not what it says about
    itself. Prose that names the bound and a date column in one sentence — this
    very file's docstring does exactly that — is documentation, and flagging it
    would make the gate fire on its own explanation of why it fires.

    SQL string literals are deliberately KEPT: a bound coalesced into a date
    inside a query is precisely the defect being hunted, and it lives in a
    string.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text.splitlines()

    blanked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            end = getattr(first, "end_lineno", first.lineno)
            blanked.update(range(first.lineno, end + 1))

    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        if number in blanked:
            out.append("")
            continue
        # Drop trailing/whole-line comments. A `#` inside a string literal is
        # rare here and erring toward dropping is safe: it can only cause the
        # gate to miss prose, never to miss code.
        stripped = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
        out.append(stripped)
    return out


def check_bound_is_not_a_date(g: Gate) -> dict:
    """§7.3/§7.4 — the boundary observation may never become an event date.

    Two distinct hazards, checked separately:

    * a module outside the allow-list reads ``known_reached_sql_by`` at all —
      a new consumer that has not been reasoned about;
    * any module assigns or coalesces the bound into a stage-entry date column.
    """
    offenders, blenders = [], []
    for path in sorted(_ROOT.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if rel.startswith(("tests/", ".git/")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if BOUND_COLUMN not in text:
            continue
        if rel not in _BOUND_READERS:
            offenders.append(rel)
        # The blending hazard: the bound named on the same line as an event
        # date column, or inside a COALESCE with one. Scanned over CODE only —
        # documentation that explains the distinction must not trip the check
        # that enforces it.
        for line in _code_lines(text):
            if BOUND_COLUMN not in line:
                continue
            if any(col in line for col in EVENT_DATE_COLUMNS):
                blenders.append(f"{rel}: {line.strip()[:110]}")

    if offenders:
        g.broken("bound_is_not_a_date",
                 f"module(s) outside the allow-list read {BOUND_COLUMN}: "
                 f"{offenders}. An upper bound on an unknown event is not a "
                 f"date, and every new reader of it must be reasoned about")
    if blenders:
        g.broken("bound_is_not_a_date",
                 f"the boundary bound appears alongside a stage-entry date "
                 f"column: {blenders}")
    if not offenders and not blenders:
        g.holds("bound_is_not_a_date",
                f"{BOUND_COLUMN} is read only by the {len(_BOUND_READERS)} "
                "modules that treat it as a bound, and never blended into a "
                "stage-entry date")
    return {"available": True, "offenders": offenders, "blenders": blenders}


def check_no_boundary_timestamp_in_event_dates(g: Gate) -> dict:
    """§7.3 — no contact's SQL date may equal the boundary observation instant.

    A data check, not a source check. If a boundary timestamp ever leaks into
    ``date_entered_sql``, every contact bounded by that boundary acquires the
    same fabricated date — a signature this query recognises exactly.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415
    from db.connection import get_conn  # noqa: PLC0415

    state = repo.fetch_active_sql_coverage_boundary()
    if not state.get("available"):
        g.cannot_check("no_boundary_timestamp_in_event_dates",
                       "the boundary store could not be read")
        return {"available": False, "contaminated": None}
    boundary = state.get("boundary")
    if boundary is None:
        g.holds("no_boundary_timestamp_in_event_dates",
                "no boundary is established, so no boundary instant exists to "
                "leak into an event-date column")
        return {"available": True, "contaminated": 0}

    try:
        with get_conn() as conn:
            if conn is None:
                g.cannot_check("no_boundary_timestamp_in_event_dates",
                               "the contact store could not be read")
                return {"available": False, "contaminated": None}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM hubspot_contact_funnel
                     WHERE date_entered_sql = %s
                        OR date_entered_lead = %s
                        OR date_entered_mql = %s
                        OR date_entered_opportunity = %s
                        OR date_entered_customer = %s
                    """,
                    tuple([boundary.get("observed_at")] * 5),
                )
                contaminated = int(cur.fetchone()[0] or 0)
    except Exception as exc:  # noqa: BLE001
        g.cannot_check("no_boundary_timestamp_in_event_dates", str(exc)[:200])
        return {"available": False, "contaminated": None}

    if contaminated:
        g.broken("no_boundary_timestamp_in_event_dates",
                 f"{contaminated} contact(s) carry the boundary observation "
                 f"instant as a stage-entry date. A bound has been written as "
                 f"an event")
    else:
        g.holds("no_boundary_timestamp_in_event_dates",
                "no contact carries the boundary instant in any stage-entry "
                "date column")
    return {"available": True, "contaminated": contaminated}


def check_no_open_post_boundary_gaps(g: Gate) -> dict:
    """§7.1 — after the boundary, every SQL transition must carry an exact date."""
    from db import crm_funnel_repository as repo  # noqa: PLC0415

    incidents = repo.fetch_post_boundary_incidents(status="open")
    if not incidents.get("available"):
        g.cannot_check("no_open_post_boundary_gaps",
                       "the post-boundary incident store could not be read, so "
                       "it is unknown whether any prospective gap exists")
        return {"available": False, "open": None}

    rows = incidents.get("rows") or []
    if rows:
        by_reason: dict = {}
        for row in rows:
            key = row.get("reason")
            by_reason[key] = by_reason.get(key, 0) + 1
        g.broken("no_open_post_boundary_gaps",
                 f"{len(rows)} contact(s) reached SQL after the boundary with "
                 f"no exact entry timestamp: {dict(sorted(by_reason.items()))}. "
                 f"These are OUR gaps, not HubSpot's")
    else:
        g.holds("no_open_post_boundary_gaps",
                "no post-boundary contact is missing an exact SQL timestamp")
    return {"available": True, "open": len(rows)}


def check_certified_windows_are_resolved(g: Gate, now: datetime) -> dict:
    """§7.5/§7.6 — a certified window must have no unresolved membership.

    Reuses the coverage audit's own verdicts rather than recomputing them. A
    second implementation that agreed would prove nothing, and one that
    disagreed would report this gate's bug as the product's.
    """
    from scripts.audit_lifecycle_sql_coverage import run as coverage_run

    try:
        findings, report = coverage_run(now)
    except Exception as exc:  # noqa: BLE001
        g.cannot_check("certified_windows_resolved",
                       f"the coverage audit could not run: {exc}")
        return {"available": False, "certified": None}

    if findings.unavailable:
        g.cannot_check("certified_windows_resolved",
                       f"the coverage audit could not complete "
                       f"({len(findings.unavailable)} check(s) unavailable), so "
                       f"no certification claim can be verified")
        return {"available": False, "certified": None}

    bad = []
    for win in report.get("windows") or []:
        if not win.get("certified"):
            continue
        if win.get("window_membership_unresolved"):
            bad.append(f"{win.get('window_type')}/{win.get('window')}")
    if bad:
        g.broken("certified_windows_resolved",
                 f"window(s) {bad} are reported CERTIFIED while carrying "
                 f"unresolved membership")

    recon = report.get("read_reconciliation") or {}
    if findings.violations:
        g.broken("canonical_readers_agree",
                 f"the coverage audit found {len(findings.violations)} contract "
                 f"violation(s): {findings.violations[:3]}")
    elif not recon.get("reconciliation_complete"):
        g.broken("canonical_readers_agree",
                 f"the canonical readers did not reconcile "
                 f"({recon.get('combinations_compared')}/"
                 f"{recon.get('combinations_expected')} compared)")
    else:
        g.holds("canonical_readers_agree",
                f"all {recon.get('combinations_expected')} window/scope "
                "combinations reconciled")

    if not bad:
        certified = (report.get("certification") or {}).get("windows_certified")
        g.holds("certified_windows_resolved",
                f"{certified} certified window(s), none carrying unresolved "
                "membership")
    return {"available": True,
            "certified": (report.get("certification") or {}).get(
                "windows_certified"),
            "unresolved_certified": bad}


def run(now: datetime | None = None) -> tuple[Gate, dict]:
    now = now or datetime.now(tz=timezone.utc)
    g = Gate()
    report: dict = {
        "generated_at": now.isoformat(),
        # Stated as a fact about this command, on every path.
        "external_writes_performed": False,
        "database_writes_performed": False,
        "hubspot_calls_performed": False,
    }
    report["stage_dates_not_erasable"] = check_timestamps_cannot_be_erased(g)
    report["bound_is_not_a_date"] = check_bound_is_not_a_date(g)
    report["boundary_in_event_dates"] = \
        check_no_boundary_timestamp_in_event_dates(g)
    report["post_boundary_gaps"] = check_no_open_post_boundary_gaps(g)
    report["certified_windows"] = check_certified_windows_are_resolved(g, now)
    return g, report


def _render(report: dict, gate: Gate, exit_code: int) -> None:
    print("=" * 78)
    print("  PR-ADS-160 — SQL COVERAGE GATE (READ-ONLY)")
    print("=" * 78)
    print(f"  gate complete:             {not gate.unavailable}")
    print(f"  external writes performed: {report['external_writes_performed']}")
    print(f"  database writes performed: {report['database_writes_performed']}")

    print()
    for check in gate.checks:
        mark = "✓" if check["ok"] else "✗"
        print(f"  {mark} {check['check']}")
        if check["detail"]:
            print(f"      {check['detail']}")

    print()
    if gate.violations:
        print(f"  {len(gate.violations)} guarantee(s) BROKEN — the system "
              "contradicts its own doctrine.")
    if gate.unavailable:
        print(f"  {len(gate.unavailable)} check(s) could not run. The gate "
              "proves nothing about those.")
    if not gate.violations and not gate.unavailable:
        print("  Every guarantee holds.")
    print(f"\n  exit {exit_code}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only SQL coverage boundary gate")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (exit code unchanged)")
    args = parser.parse_args()

    try:
        from db.connection import init_pool

        init_pool()
    except Exception as exc:  # noqa: BLE001
        payload = {"gate_complete": False, "external_writes_performed": False,
                   "unavailable": [f"database pool unavailable: {exc}"]}
        print(json.dumps(payload, indent=2) if args.json
              else f"UNAVAILABLE — database pool unavailable: {exc}")
        return EXIT_UNAVAILABLE

    gate, report = run()
    exit_code = gate.exit_code
    report["gate_complete"] = not gate.unavailable
    report["violations"] = gate.violations
    report["unavailable"] = gate.unavailable
    report["checks"] = gate.checks
    report["exit_code"] = exit_code

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report, gate, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
