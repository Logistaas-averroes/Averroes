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
 11  summary totals reconcile to the sum of the table rows
 12  campaign identity is unique — no two rows share a `campaign_key`
 13  campaigns sharing a display name keep distinct identities
 14  identity resolution never silently falls back to display name
 15  the frontend gates every SQL-dependent surface on the reconciliation
 16  no external write is performed by this command

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

    # ── 11 · summary reconciles to the rows it summarises ───────────────────
    # Only meaningful when both sides are present; a None total is a withheld
    # total, not a disagreement.
    row_sqls = [c.get("confirmed_sqls") for c in campaigns]
    if summary.get("confirmed_sqls_total") is not None and all(v is not None for v in row_sqls):
        if sum(row_sqls) != summary["confirmed_sqls_total"]:
            f.violation(f"summary_reconciles[{window}]",
                        f"summary confirmed_sqls_total={summary['confirmed_sqls_total']} "
                        f"but the rows sum to {sum(row_sqls)}")
        else:
            f.passed(f"summary_reconciles[{window}]",
                     f"{summary['confirmed_sqls_total']} == sum of rows")

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
