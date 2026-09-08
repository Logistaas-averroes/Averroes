#!/usr/bin/env python3
"""
scripts/audit_sql_doctrine_inventory.py

PR-ADS-158 — READ-ONLY system-wide SQL doctrine audit and legacy consumer
inventory.

    python -m scripts.audit_sql_doctrine_inventory
    python -m scripts.audit_sql_doctrine_inventory --json
    python -m scripts.audit_sql_doctrine_inventory --static-only
    echo $?   # 0 = audit complete (legacy may remain)
              # 1 = audit incomplete / internal contradiction / unclassified occurrence
              # 2 = required database or canonical source unavailable

What it is
----------
An investigation, not a gate and not a migration. It answers, with evidence:

  * how many SQL definitions remain in production code and who consumes each;
  * which consumers use the canonical HubSpot lifecycle SQL event and which
    still use ``status_category = qualified`` on ``contact_created_at``;
  * for every supported Evidence and Business window, how the legacy and
    lifecycle SQL populations differ — on durable contact keys, not totals;
  * which data-coverage gaps prevent a complete lifecycle SQL total;
  * whether a non-SQL classification gap is what downgrades the Campaign
    Evidence SQL reconciliation (PR-ADS-158 §7);
  * every CPQL field and every SQL-dependent decision surface.

Finding legacy code is the expected result. Failing to classify it is the
failure: an unclassified production occurrence makes ``audit_complete`` false
and exits 1. ``migration_complete`` is reported separately and is expected to
be false while active legacy consumers remain.

Guarantees
----------
  * NO writes. Every database access is a SELECT through the existing
    read-only repositories, reached via the two production SQL services
    (``canonical_contact_outcome_service`` for the legacy population,
    ``canonical_crm_funnel_service`` for the lifecycle population). No third
    SQL population implementation exists here.
  * A runtime guard sets every pooled connection this process obtains to
    ``SESSION CHARACTERISTICS AS TRANSACTION READ ONLY``, so any write path
    reached by accident fails loudly at the database instead of succeeding.
  * NO external API calls: HubSpot, Google Ads and Mailchimp are never contacted.
  * NO contact PII: output is counts and reason codes. Snippets are source
    code, scrubbed of anything email- or phone-shaped.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import sql_doctrine_audit as audit  # noqa: E402
from analysis import sql_doctrine_registry as registry  # noqa: E402

logger = logging.getLogger("sql_doctrine_audit")

EXIT_COMPLETE = audit.EXIT_COMPLETE
EXIT_INCOMPLETE = audit.EXIT_INCOMPLETE
EXIT_SOURCE_UNAVAILABLE = audit.EXIT_SOURCE_UNAVAILABLE

#: The audit's own modules, proven write-free by static inspection on every run.
_SELF_MODULES = {
    "analysis/sql_doctrine_audit.py": _ROOT / "analysis" / "sql_doctrine_audit.py",
    "analysis/sql_doctrine_registry.py": _ROOT / "analysis" / "sql_doctrine_registry.py",
    "scripts/audit_sql_doctrine_inventory.py": Path(__file__).resolve(),
}


# ── Runtime read-only guard ──────────────────────────────────────────────────
class ReadOnlyPool:
    """Wrap the psycopg2 pool so every connection handed out in THIS process is
    session-read-only. ``db.connection.get_conn`` reads the module-level pool
    at call time, so the guard covers every repository — including modules
    that bound ``get_conn`` by value at import."""

    def __init__(self, inner):
        self._inner = inner
        self.guarded_connections = 0

    def getconn(self, *args, **kwargs):
        conn = self._inner.getconn(*args, **kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            conn.commit()
            self.guarded_connections += 1
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
        return conn

    def putconn(self, *args, **kwargs):
        return self._inner.putconn(*args, **kwargs)

    def closeall(self):
        return self._inner.closeall()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def install_read_only_guard() -> dict:
    """Initialise the pool (standalone execution) and wrap it read-only."""
    from db import connection

    connection.init_pool()
    if connection._pool is None:  # noqa: SLF001
        return {"installed": False, "reason": "database pool could not be initialised"}
    if not isinstance(connection._pool, ReadOnlyPool):  # noqa: SLF001
        connection._pool = ReadOnlyPool(connection._pool)  # noqa: SLF001
    return {"installed": True, "reason": None,
            "mechanism": "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY on every pooled connection"}


# ── Runtime sources ──────────────────────────────────────────────────────────
def fetch_runtime_sources(now: datetime) -> dict:
    """Read every input ONCE through the existing repositories. Read-only."""
    from db import canonical_contact_outcome_repository as legacy_repo
    from db import crm_funnel_repository as funnel_repo
    from services import canonical_contact_outcome_service as canon

    windows = resolve_all_windows(canon, now)
    max_end = max(w["end"] for w in windows)
    legacy_inputs = legacy_repo.fetch_canonical_inputs(None, max_end)
    funnel_fetch = funnel_repo.fetch_all_funnel_contacts()
    return {
        "legacy_inputs": legacy_inputs,
        "funnel_fetch": funnel_fetch,
        "windows": windows,
    }


def resolve_all_windows(canon, now: datetime) -> list[dict]:
    out = []
    for key in audit.EVIDENCE_WINDOWS:
        out.append(canon.resolve_window_contract(canon.WINDOW_EVIDENCE, key, now=now))
    for key in audit.BUSINESS_WINDOWS:
        out.append(canon.resolve_window_contract(canon.WINDOW_BUSINESS, key, now=now))
    return out


def production_resolver_factory(start, end):
    """The production Google Ads campaign-identity resolver (read-only)."""
    from services import canonical_contact_outcome_service as canon
    return canon._build_identity_resolver(start, end)  # noqa: SLF001


def production_keyword_keys(window: dict):
    """Uniquely-keyword-attributed legacy SQL keys via the real keyword path."""
    from services import sql_truth_audit_service
    return sql_truth_audit_service._keyword_attributable_keys(window)  # noqa: SLF001


# ── Runtime comparison (no I/O: everything is injected) ──────────────────────
def build_runtime_comparison(*, legacy_inputs: dict, funnel_fetch: dict, windows: list[dict],
                             resolver_factory, keyword_keys_fn=None) -> dict:
    """Compare the legacy and lifecycle SQL populations for every window.

    Both populations are built by the PRODUCTION services' pure builders on
    the rows fetched once. ``resolver_factory(start, end)`` returns
    ``(resolver, identity_available)``; ``keyword_keys_fn(window)`` returns the
    legacy keyword-attributable key set or ``None`` (evidence windows only).
    """
    from services import canonical_contact_outcome_service as canon
    from services import canonical_crm_funnel_service as funnel

    legacy_available = bool(legacy_inputs.get("available"))
    lifecycle_available = bool(funnel_fetch.get("available"))
    if not legacy_available or not lifecycle_available:
        reasons = []
        if not legacy_available:
            reasons.append("legacy_leads_source_unavailable")
        if not lifecycle_available:
            reasons.append("canonical_contact_store_unavailable")
        return {"available": False, "reason": ",".join(reasons), "windows": [],
                "sources": _sources_block(legacy_inputs, funnel_fetch, legacy_available,
                                          lifecycle_available)}

    lead_rows = legacy_inputs.get("lead_rows") or []
    exclusions = legacy_inputs.get("exclusions") or set()
    classification = legacy_inputs.get("classification") or []
    funnel_rows = funnel_fetch.get("rows") or []

    # All-time key sets (SQL under each doctrine at ANY time) — needed to tell a
    # date shift from a population difference.
    max_end = max(w["end"] for w in windows)
    all_resolver, all_identity = resolver_factory(None, max_end)
    legacy_all = canon.build_populations(lead_rows, exclusions, classification, None, max_end,
                                         campaign_resolver=all_resolver)
    legacy_all_keys, _ = audit.split_legacy_keys(
        audit.legacy_scope_keys(legacy_all["contacts"])["all_source"])
    lifecycle_all = funnel.build_populations(funnel_rows, None, max_end,
                                             campaign_resolver=all_resolver,
                                             identity_available=all_identity)
    lifecycle_all_keys = audit.lifecycle_scope_keys(lifecycle_all["events"]["sql"])["all_source"]

    comparisons = []
    for win in windows:
        start, end = win["start"], win["end"]
        resolver, identity_available = resolver_factory(start, end)
        legacy_pops = canon.build_populations(lead_rows, exclusions, classification, start, end,
                                              campaign_resolver=resolver)
        if not identity_available:
            legacy_pops["counts"]["campaign_attributable_sqls"] = None
        lifecycle_pops = funnel.build_populations(funnel_rows, start, end,
                                                  campaign_resolver=resolver,
                                                  identity_available=identity_available)
        keyword_keys, keyword_note = None, None
        if win["window_type"] == canon.WINDOW_EVIDENCE and keyword_keys_fn is not None:
            keyword_keys = keyword_keys_fn(win)
            keyword_note = (None if keyword_keys is not None
                            else "keyword attribution path unavailable")
        elif win["window_type"] == canon.WINDOW_BUSINESS:
            keyword_note = "keyword attribution is evidence-window only (Keyword Evidence page)"

        comparisons.append(audit.compare_window(
            {k: v for k, v in win.items() if k not in ("start", "end")},
            legacy={"available": True, "counts": legacy_pops["counts"],
                    "contacts": legacy_pops["contacts"],
                    "identity_available": identity_available},
            lifecycle={"available": True, "populations": lifecycle_pops},
            legacy_all_time_keys=legacy_all_keys,
            lifecycle_all_time_keys=lifecycle_all_keys,
            legacy_keyword_keys=keyword_keys, legacy_keyword_note=keyword_note,
            reconcile_fn=canon.reconciliation_metadata,
            lifecycle_status_fn=funnel.reconciliation_status,
        ))

    return {
        "available": True,
        "reason": None,
        "windows": comparisons,
        "sources": _sources_block(legacy_inputs, funnel_fetch, True, True),
    }


def _sources_block(legacy_inputs, funnel_fetch, legacy_available, lifecycle_available) -> dict:
    return {
        "legacy": {
            "available": legacy_available,
            "table": legacy_inputs.get("table", "leads"),
            "date_field": legacy_inputs.get("date_field", "contact_created_at"),
            "latest_snapshot_rows": len(legacy_inputs.get("lead_rows") or []) if legacy_available else None,
            "exclusion_keys": len(legacy_inputs.get("exclusions") or ()) if legacy_available else None,
            "classification_rows": len(legacy_inputs.get("classification") or []) if legacy_available else None,
            "service": "services.canonical_contact_outcome_service.build_populations",
        },
        "lifecycle": {
            "available": lifecycle_available,
            "table": funnel_fetch.get("table", "hubspot_contact_funnel"),
            "recovery_table": funnel_fetch.get("recovery_table"),
            "date_field": "date_entered_sql",
            "contact_rows": len(funnel_fetch.get("rows") or []) if lifecycle_available else None,
            "service": "services.canonical_crm_funnel_service.build_populations",
        },
    }


# ── Static inventory ─────────────────────────────────────────────────────────
def build_static(root: Path) -> dict:
    occurrences = audit.discover_occurrences(root)
    audit.classify_occurrences(occurrences, registry.RULES)
    rule_problems = audit.validate_rules(registry.RULES, root)
    consumer_problems = audit.validate_consumers(registry.CONSUMERS, root)
    return audit.build_static_inventory(occurrences, registry.CONSUMERS,
                                        rule_problems, consumer_problems)


def write_safety() -> dict:
    sources = {}
    for label, path in _SELF_MODULES.items():
        try:
            sources[label] = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "problems": [f"{label}: unreadable ({exc})"],
                    "checked_modules": sorted(_SELF_MODULES)}
    return audit.write_safety_proof(sources)


def audited_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


# ── Orchestration ────────────────────────────────────────────────────────────
def run_audit(*, root: Path = _ROOT, now: datetime | None = None,
              static_only: bool = False, runtime: dict | None = None) -> dict:
    """Assemble the full report.

    ``runtime`` may be injected (tests) as the output of
    ``build_runtime_comparison``; otherwise the database is read.
    """
    now = now or datetime.now(timezone.utc)
    static = build_static(root)
    safety = write_safety()

    if runtime is None and not static_only:
        guard = install_read_only_guard()
        safety = dict(safety, runtime_guard=guard)
        if not guard["installed"]:
            runtime = {"available": False, "reason": guard["reason"], "windows": [],
                       "sources": None}
        else:
            try:
                sources = fetch_runtime_sources(now)
                runtime = build_runtime_comparison(
                    legacy_inputs=sources["legacy_inputs"],
                    funnel_fetch=sources["funnel_fetch"],
                    windows=sources["windows"],
                    resolver_factory=production_resolver_factory,
                    keyword_keys_fn=production_keyword_keys)
            except Exception as exc:  # noqa: BLE001
                logger.warning("runtime comparison failed: %s", exc)
                runtime = {"available": False, "reason": f"runtime_comparison_failed: {exc}",
                           "windows": [], "sources": None}
    elif runtime is None:
        runtime = {"available": False, "reason": "static_only", "windows": [], "sources": None}

    report = audit.assemble_report(
        static=static,
        consumers=registry.CONSUMERS,
        cpql_consumers=registry.CPQL_CONSUMERS,
        decision_surfaces=registry.DECISION_SURFACES,
        known_conflicts=registry.KNOWN_CONTRACT_CONFLICTS,
        window_comparisons=runtime.get("windows") or [],
        runtime_available=bool(runtime.get("available")),
        runtime_reason=runtime.get("reason"),
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        audited_commit=audited_commit(root),
        write_safety=safety,
    )
    report["runtime_sources"] = runtime.get("sources")
    if static_only:
        # A deliberate static run is complete without the database; only the
        # runtime comparison is absent, and the verdict must say so rather than
        # claim a source outage.
        report["runtime_unavailable_reason"] = "static_only"
        if report["audit_complete"]:
            report["verdict"] = audit.VERDICT_READY
            report["exit_code"] = audit.EXIT_COMPLETE
            report["summary"]["verdict"] = audit.VERDICT_READY
        else:
            report["verdict"] = audit.VERDICT_INCOMPLETE
            report["exit_code"] = audit.EXIT_INCOMPLETE
            report["summary"]["verdict"] = audit.VERDICT_INCOMPLETE
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only PR-ADS-158 system-wide SQL doctrine audit")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (exit code is unchanged)")
    parser.add_argument("--static-only", action="store_true",
                        help="Skip the database-backed window comparison")
    parser.add_argument("--occurrences", action="store_true",
                        help="Include every discovered occurrence in the JSON (large)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    report = run_audit(static_only=args.static_only)

    if args.json:
        payload = dict(report)
        if not args.occurrences:
            payload["occurrences"] = (
                f"omitted ({len(report['occurrences'])} occurrences; pass --occurrences)")
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(audit.render_human(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
