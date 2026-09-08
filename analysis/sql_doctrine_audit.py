"""
analysis/sql_doctrine_audit.py

PR-ADS-158 — the PURE core of the system-wide SQL doctrine audit.

Everything in this module operates on plain Python values: source files read
from disk, registry entries, and the population dictionaries the two existing
SQL services already produce. It performs no database access and contacts no
external system. The database-backed entry point lives in
``scripts/audit_sql_doctrine_inventory.py``; the reviewed registry lives in
``analysis/sql_doctrine_registry.py``.

What this module does
---------------------
1. **Static discovery.** Scans the production tree for every textual
   occurrence of an SQL-doctrine marker (legacy ``status_category =
   'qualified'`` expressions, lifecycle ``date_entered_sql`` reads, service
   imports, ``confirmed_sqls`` fields, bare "SQLs" labels, CPQL fields,
   SQL-dependent verdicts …). Tests, fixtures and documentation are scanned
   too, but are classified by LOCATION and never counted as production
   consumers.
2. **Reviewed classification.** Every production occurrence must match a
   registry rule. An occurrence with no rule is ``unknown_requires_review`` and
   makes the audit INCOMPLETE. Finding legacy code is not a failure; failing to
   classify it is.
3. **Population comparison.** Compares the legacy population
   (``services.canonical_contact_outcome_service`` — ``status_category =
   qualified`` on ``contact_created_at``) against the canonical lifecycle
   population (``services.canonical_crm_funnel_service`` — entered
   ``salesqualifiedlead`` on ``date_entered_sql``) on DURABLE CONTACT KEYS,
   never only on totals. Two equal totals over different contacts are a
   population difference.
4. **Hidden-cause exposure.** Re-runs the production reconciliation function
   with and without the non-SQL classification gaps so the audit can prove —
   rather than assert — whether an irrelevant non-SQL gap changes the SQL
   reconciliation status.

What it never does
------------------
No writes. No repairs. No inferred timestamps. No third SQL population: the
legacy and lifecycle populations are built by the two production services and
only COMPARED here.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

# ── Classification vocabulary ────────────────────────────────────────────────
CLS_CANONICAL = "canonical_lifecycle_active"
CLS_LEGACY = "legacy_status_active"
CLS_MIXED = "mixed_or_adapter_active"
CLS_GOOGLE_ADS_CONVERSION = "google_ads_conversion_not_sql"
CLS_DIAGNOSTIC = "diagnostic_comparison_only"
CLS_INACTIVE = "inactive_legacy"
CLS_TEST_FIXTURE = "test_fixture"
CLS_DOCUMENTATION = "documentation"
CLS_UNKNOWN = "unknown_requires_review"

CLASSIFICATIONS = (
    CLS_CANONICAL, CLS_LEGACY, CLS_MIXED, CLS_GOOGLE_ADS_CONVERSION,
    CLS_DIAGNOSTIC, CLS_INACTIVE, CLS_TEST_FIXTURE, CLS_DOCUMENTATION,
    CLS_UNKNOWN,
)

#: Classifications that describe a consumer still feeding a live surface.
ACTIVE_CLASSIFICATIONS = (CLS_CANONICAL, CLS_LEGACY, CLS_MIXED)

# ── Verdict vocabulary ───────────────────────────────────────────────────────
VERDICT_READY = "READY_FOR_ROADMAP"
VERDICT_INCOMPLETE = "AUDIT_INCOMPLETE"
VERDICT_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"

EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 1
EXIT_SOURCE_UNAVAILABLE = 2

# ── Windows (both vocabularies, never merged) ────────────────────────────────
EVIDENCE_WINDOWS = ("7d", "14d", "30d", "60d", "180d", "all_time")
BUSINESS_WINDOWS = ("current_quarter", "last_quarter", "last_6_months", "ytd",
                    "all_time")

# ── The canonical reference standard (PR-ADS-158 §3) ─────────────────────────
CANONICAL_STANDARD = {
    "sql_event": "contact entered HubSpot lifecycle stage 'salesqualifiedlead'",
    "source_property": "hs_v2_date_entered_salesqualifiedlead",
    "durable_column": "hubspot_contact_funnel.date_entered_sql",
    "source_table": "hubspot_contact_funnel",
    "dedup_key": "contact_id",
    "window_date": "SQL stage-entry date (date_entered_sql)",
    "source_of_truth": (
        "HubSpot lifecycle history / current stage-entry properties "
        "(hubspot_lifecycle_stage_history recovery COALESCEd where the property "
        "is absent)"),
    "missing_timestamp_rule": (
        "explicit coverage gap (missing_stage_entry_date); never replaced with "
        "contact creation date"),
    "cohort_rule": (
        "a contact that later becomes Opportunity or Customer remains in its "
        "historical SQL cohort"),
    "scope_lattice": "keyword_attributable <= campaign_attributable <= "
                     "google_ads_source <= all_source",
    "scope_rule": (
        "every named scope must be derived from the same lifecycle SQL event "
        "population; a page may display a narrower scope, never a different "
        "definition"),
    "google_ads_conversions": (
        "a separate advertising metric; never classified as a HubSpot SQL"),
}

LEGACY_STANDARD = {
    "sql_definition": "latest status_category = qualified",
    "source_table": "leads (scheduler snapshots) / contact_source_classification",
    "date_field": "contact_created_at",
    "dedup_key": "COALESCE(NULLIF(contact_id, ''), 'id:' || leads.id)",
    "derivation": "mql_status in {CLOSED - Sales Qualified, CLOSED - Deal Created}",
}

# ── Discovery patterns ───────────────────────────────────────────────────────
#: pattern id → (compiled regex, applies-to-suffixes or None for all)
PATTERNS: dict[str, tuple[re.Pattern, tuple[str, ...] | None]] = {
    "legacy_sql_literal": (
        re.compile(r"status_category\s*=\s*['\"]qualified['\"]"), None),
    "legacy_python_comparison": (
        re.compile(r"status_category[^\n=]{0,40}==\s*['\"]qualified['\"]"), None),
    "legacy_qualified_symbol": (
        re.compile(r"\b(_QUALIFIED|LEGACY_QUALIFIED)\b"), (".py",)),
    "legacy_outcome_service_ref": (
        re.compile(r"canonical_contact_outcome_(service|repository)"), None),
    "platform_sql_attribution_ref": (
        re.compile(r"platform_sql_attribution_(service|repository)"), None),
    "confirmed_sqls_ref": (re.compile(r"\bconfirmed_sqls\b"), None),
    "sqls_field_ref": (re.compile(r"\bsqls\b"), None),
    "sql_count_ref": (re.compile(r"sql_count\b"), None),
    "lifecycle_funnel_service_ref": (
        re.compile(r"canonical_crm_funnel_service|crm_funnel_repository"), None),
    "doctrine_comparison_service_ref": (
        re.compile(r"crm_funnel_reconciliation_service|sql_truth_audit_service"), None),
    "sql_reconciliation_ref": (re.compile(r"\bsql_reconciliation\b"), None),
    "lifecycle_sql_column_ref": (re.compile(r"\bdate_entered_sql\b"), None),
    "lifecycle_sql_property_ref": (
        re.compile(r"hs_v2_date_entered_salesqualifiedlead"), None),
    "lifecycle_sql_stage_ref": (re.compile(r"\bsalesqualifiedlead\b"), None),
    "contact_created_at_ref": (re.compile(r"\bcontact_created_at\b"), None),
    "sql_case_expression": (
        re.compile(r"CASE\s+WHEN[^\n]*['\"]qualified['\"]", re.IGNORECASE), None),
    "frontend_sqls_label": (re.compile(r"\bSQLs\b"), (".js", ".html")),
    "cpql_ref": (re.compile(r"\bcpql\b", re.IGNORECASE), None),
    "sql_verdict_ref": (
        re.compile(r"SQL producer|Spend without SQL proof|\bhas_sql\b|\bno_sql\b"),
        None),
}

#: Every pattern the brief requires the discovery to find, by name.
REQUIRED_PATTERN_IDS = tuple(PATTERNS.keys())

PRODUCTION_ROOTS = ("api", "services", "analysis", "db", "scripts",
                    "scheduler", "static", "connectors")
NON_PRODUCTION_ROOTS = ("tests", "docs")
SCAN_SUFFIXES = (".py", ".js", ".html", ".sql", ".md", ".json", ".yaml", ".yml")
_SKIP_DIRS = {".git", "__pycache__", "node_modules", "screenshots", ".pytest_cache"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")


def scrub(text: str) -> str:
    """Remove anything that looks like an email address or a phone number from
    a snippet. Source code carries none, but the rule is enforced rather than
    assumed (PR-ADS-158 §6)."""
    text = _EMAIL_RE.sub("[redacted-email]", text)
    return _PHONE_RE.sub("[redacted-number]", text)


@dataclass
class Occurrence:
    path: str                       # repo-relative, POSIX
    line: int
    pattern_id: str
    symbol: str                     # enclosing function/class, or "<module>"
    snippet: str
    location_kind: str              # production | test | documentation | fixture
    classification: str = CLS_UNKNOWN
    consumer: str | None = None
    rule_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "line": self.line,
            "pattern_id": self.pattern_id,
            "symbol": self.symbol,
            "snippet": self.snippet,
            "location_kind": self.location_kind,
            "classification": self.classification,
            "consumer": self.consumer,
            "rule_id": self.rule_id,
            "code_location": f"{self.path}:{self.symbol}",
        }


# ── Location classification ──────────────────────────────────────────────────
def location_kind(rel_path: str) -> str:
    """production | test | documentation | fixture, by path alone."""
    parts = rel_path.split("/")
    name = parts[-1]
    if parts[0] == "tests" or name.startswith("test_") or name == "conftest.py":
        return "test"
    if parts[0] == "docs" or name.lower().endswith(".md"):
        return "documentation"
    if "fixture" in name.lower() or name.endswith(".json"):
        return "fixture"
    if parts[0] in PRODUCTION_ROOTS:
        return "production"
    return "other"


# ── Enclosing-symbol resolution ──────────────────────────────────────────────
def python_symbol_index(source: str) -> list[tuple[int, int, str]]:
    """``[(start_line, end_line, qualified_name), …]`` for every def/class."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[int, int, str]] = []

    def _walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                end = getattr(child, "end_lineno", child.lineno)
                spans.append((child.lineno, end, name))
                _walk(child, name)
            else:
                _walk(child, prefix)

    _walk(tree, "")
    return spans


def python_symbol_at(spans: list[tuple[int, int, str]], line: int) -> str:
    best = None
    for start, end, name in spans:
        if start <= line <= end:
            if best is None or (end - start) < (best[1] - best[0]):
                best = (start, end, name)
    return best[2] if best else "<module>"


_JS_FUNC_RE = re.compile(
    r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
    r"|^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\()"
)


def js_symbol_at(lines: list[str], line: int) -> str:
    """Enclosing TOP-LEVEL JavaScript function: the nearest preceding column-0
    ``function name(`` or ``const name = (…) =>`` declaration. Inner arrow
    functions are deliberately ignored so the registry keys on stable names."""
    for idx in range(line - 1, -1, -1):
        m = _JS_FUNC_RE.match(lines[idx])
        if m:
            return m.group(1) or m.group(2) or "<module>"
    return "<module>"


# ── Discovery ────────────────────────────────────────────────────────────────
def iter_scan_files(root: Path):
    for top in PRODUCTION_ROOTS + NON_PRODUCTION_ROOTS:
        base = root / top
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SCAN_SUFFIXES:
                continue
            if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            yield p


def discover_occurrences(root: Path, *, files=None) -> list[Occurrence]:
    """Scan the tree (or an explicit iterable of files) for every marker."""
    root = Path(root)
    found: list[Occurrence] = []
    for path in (files if files is not None else iter_scan_files(root)):
        path = Path(path)
        rel = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        kind = location_kind(rel)
        lines = source.splitlines()
        spans = python_symbol_index(source) if path.suffix == ".py" else None
        for lineno, text in enumerate(lines, start=1):
            for pattern_id, (regex, suffixes) in PATTERNS.items():
                if suffixes and path.suffix.lower() not in suffixes:
                    continue
                if not regex.search(text):
                    continue
                if spans is not None:
                    symbol = python_symbol_at(spans, lineno)
                elif path.suffix.lower() == ".js":
                    symbol = js_symbol_at(lines, lineno)
                else:
                    symbol = "<module>"
                found.append(Occurrence(
                    path=rel, line=lineno, pattern_id=pattern_id, symbol=symbol,
                    snippet=scrub(text.strip())[:160], location_kind=kind))
    return found


# ── Rule-based classification ────────────────────────────────────────────────
def _path_matches(rule_path: str, path: str) -> bool:
    """Exact file match only. Folder- and glob-wide bindings were removed in
    PR-ADS-158 review: a new SQL consumer inside a known directory must surface
    as ``unknown_requires_review``, never inherit a neighbour's classification."""
    return path == rule_path


def _symbol_matches(rule_symbol, symbol: str) -> bool:
    if rule_symbol is None:
        return True
    wanted = rule_symbol if isinstance(rule_symbol, (list, tuple, set)) else (rule_symbol,)
    parts = symbol.split(".")
    return any(w == symbol or w in parts for w in wanted)


def _pattern_matches(rule_pattern, pattern_id: str) -> bool:
    if rule_pattern is None:
        return True
    wanted = rule_pattern if isinstance(rule_pattern, (list, tuple, set)) else (rule_pattern,)
    return pattern_id in wanted


def rule_specificity(rule: dict) -> tuple:
    path = rule.get("path", "")
    exact = 0 if (path.endswith("/") or path.endswith("*")) else 1
    return (exact, 1 if rule.get("symbol") else 0, 1 if rule.get("pattern") else 0,
            len(path))


def classify_occurrences(occurrences: list[Occurrence], rules: list[dict]) -> list[Occurrence]:
    """Assign a classification to every occurrence.

    Non-production locations are classified by location. Production
    occurrences take the most specific matching rule; with none they stay
    ``unknown_requires_review``.
    """
    ordered = sorted(rules, key=rule_specificity, reverse=True)
    for occ in occurrences:
        if occ.location_kind == "test":
            occ.classification = CLS_TEST_FIXTURE
            occ.rule_id = "location:test"
            continue
        if occ.location_kind == "fixture":
            occ.classification = CLS_TEST_FIXTURE
            occ.rule_id = "location:fixture"
            continue
        if occ.location_kind == "documentation":
            occ.classification = CLS_DOCUMENTATION
            occ.rule_id = "location:documentation"
            continue
        for rule in ordered:
            if (_path_matches(rule["path"], occ.path)
                    and _symbol_matches(rule.get("symbol"), occ.symbol)
                    and _pattern_matches(rule.get("pattern"), occ.pattern_id)):
                occ.classification = rule["classification"]
                occ.consumer = rule.get("consumer")
                occ.rule_id = rule.get("id")
                break
        else:
            occ.classification = CLS_UNKNOWN
    return occurrences


#: Patterns specific enough to bind an occurrence on their own (path + pattern)
#: without naming the enclosing symbol. Everything else (``sqls``,
#: ``contact_created_at``, ``cpql``, bare "SQLs" labels, ``confirmed_sqls`` …)
#: is too broad to classify an unreviewed function and requires a symbol.
SPECIFIC_PATTERNS = frozenset({
    "legacy_sql_literal", "legacy_python_comparison", "sql_case_expression",
    "legacy_qualified_symbol", "legacy_outcome_service_ref",
    "platform_sql_attribution_ref", "lifecycle_sql_column_ref",
    "lifecycle_sql_property_ref", "lifecycle_sql_stage_ref",
    "lifecycle_funnel_service_ref", "doctrine_comparison_service_ref",
    "sql_reconciliation_ref",
})

MODULE_SYMBOL = "<module>"


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def rule_binding_problems(rule: dict) -> list[str]:
    """Why a rule is not an explicit reviewed binding (PR-ADS-158 review).

    A rule binds an occurrence only through:
      * ``path`` + ``symbol`` (an enclosing function/class; ``<module>`` must
        additionally name the ``pattern``s it covers so a new module-level
        statement of another kind stays unreviewed); or
      * ``path`` + a pattern in ``SPECIFIC_PATTERNS``.
    A folder, glob or whole-file rule is rejected: it would let a new consumer
    inside a known file inherit its neighbour's classification unreviewed.
    """
    rid = rule.get("id", "?")
    problems: list[str] = []
    path = rule.get("path", "")
    if not path or path.endswith("/") or "*" in path:
        problems.append(f"{rid}: folder/glob path binding is not allowed: {path!r}")
    symbols = _as_list(rule.get("symbol"))
    patterns = _as_list(rule.get("pattern"))
    if not symbols and not patterns:
        problems.append(f"{rid}: whole-file binding is not allowed (needs symbol or specific pattern)")
    if not symbols and patterns and not set(patterns) <= SPECIFIC_PATTERNS:
        broad = sorted(set(patterns) - SPECIFIC_PATTERNS)
        problems.append(f"{rid}: pattern-only binding uses broad pattern(s) {broad}")
    if MODULE_SYMBOL in symbols and not patterns:
        problems.append(f"{rid}: '<module>' binding must name its patterns")
    if MODULE_SYMBOL in symbols and len(symbols) > 1:
        problems.append(f"{rid}: '<module>' must be bound on its own, not with functions")
    return problems


def validate_rules(rules: list[dict], root: Path | None = None) -> list[str]:
    """Registry self-consistency: every rule is an explicit binding, names a
    known classification and (when a root is given) an existing path. Returns
    problems, never raises."""
    problems: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        rid = rule.get("id")
        if not rid:
            problems.append("rule without id")
            continue
        if rid in seen:
            problems.append(f"duplicate rule id {rid}")
        seen.add(rid)
        if rule.get("classification") not in CLASSIFICATIONS:
            problems.append(f"{rid}: unknown classification {rule.get('classification')!r}")
        problems.extend(rule_binding_problems(rule))
        for pat in _as_list(rule.get("pattern")):
            if pat not in PATTERNS:
                problems.append(f"{rid}: unknown pattern {pat!r}")
        if root is not None:
            path = rule.get("path", "")
            if not (Path(root) / path).exists():
                problems.append(f"{rid}: path does not exist: {path}")
    return problems


def validate_consumers(consumers: list[dict], root: Path | None = None) -> list[str]:
    """Every registry consumer must carry the full PR-ADS-158 §4 record and point
    at code that exists at the audited commit."""
    problems: list[str] = []
    seen: set[str] = set()
    for c in consumers:
        name = c.get("consumer")
        if not name:
            problems.append("consumer without a name")
            continue
        if name in seen:
            problems.append(f"duplicate consumer {name}")
        seen.add(name)
        for key in REQUIRED_CONSUMER_FIELDS:
            if key not in c:
                problems.append(f"{name}: missing field {key}")
        if c.get("classification") not in CLASSIFICATIONS:
            problems.append(f"{name}: unknown classification {c.get('classification')!r}")
        if c.get("confidence") not in ("confirmed", "inferred", "unknown"):
            problems.append(f"{name}: confidence must be confirmed|inferred|unknown")
        if root is not None:
            loc = (c.get("code_location") or "").split(":", 1)[0]
            if loc and not (Path(root) / loc).exists():
                problems.append(f"{name}: code_location path missing: {loc}")
    return problems


REQUIRED_CONSUMER_FIELDS = (
    "consumer", "purpose", "api_endpoint", "service_function",
    "repository_source", "source_table", "sql_definition", "date_field",
    "dedup_key", "windows", "scope", "affects_headline", "affects_row",
    "affects_cpql", "affects_filters", "affects_sorting", "affects_drawer",
    "affects_export", "affects_decision", "truth_status_behaviour",
    "code_location", "migration_notes", "confidence", "classification",
    "affects_executive_totals", "affects_operational_decisions",
)


# ── Static inventory summary ─────────────────────────────────────────────────
def build_static_inventory(occurrences: list[Occurrence], consumers: list[dict],
                           rule_problems: list[str], consumer_problems: list[str]) -> dict:
    production = [o for o in occurrences if o.location_kind == "production"]
    unclassified = [o.as_dict() for o in production if o.classification == CLS_UNKNOWN]
    by_class: dict[str, int] = {c: 0 for c in CLASSIFICATIONS}
    for o in occurrences:
        by_class[o.classification] = by_class.get(o.classification, 0) + 1
    patterns_hit = {pid: sum(1 for o in production if o.pattern_id == pid)
                    for pid in PATTERNS}
    consumers_by_class: dict[str, list[str]] = {c: [] for c in CLASSIFICATIONS}
    for c in consumers:
        consumers_by_class.setdefault(c["classification"], []).append(c["consumer"])
    return {
        "occurrences_total": len(occurrences),
        "production_occurrences": len(production),
        "non_production_occurrences": len(occurrences) - len(production),
        "occurrences_by_classification": by_class,
        "production_patterns_hit": patterns_hit,
        "unclassified_occurrences": unclassified,
        "registry_problems": rule_problems + consumer_problems,
        "consumers_by_classification": consumers_by_class,
        "occurrences": [o.as_dict() for o in occurrences],
    }


# ── Population comparison (pure) ─────────────────────────────────────────────
def _key(value) -> str:
    return str(value or "").strip()


def legacy_scope_keys(legacy_contacts: list[dict]) -> dict:
    """Durable-key sets per named scope from the legacy population contacts
    (``canonical_contact_outcome_service.build_populations`` output)."""
    return {
        "all_source": {_key(c["contact_key"]) for c in legacy_contacts if c.get("is_all_source_sql")},
        "google_ads_source": {_key(c["contact_key"]) for c in legacy_contacts
                              if c.get("is_google_ads_source_sql")},
        "campaign_attributable": {_key(c["contact_key"]) for c in legacy_contacts
                                  if c.get("is_campaign_attributable_sql")},
    }


def lifecycle_scope_keys(sql_population: list[dict]) -> dict:
    """Contact-id sets per scope from the lifecycle SQL event population
    (``canonical_crm_funnel_service.build_populations()["events"]["sql"]``).
    An identity-dependent scope is ``None`` when unavailable, never an empty set."""
    out = {}
    for scope in ("all_source", "google_ads_source", "campaign_attributable",
                  "keyword_attributable"):
        if any(c["scopes"].get(scope) is None for c in sql_population):
            out[scope] = None
        else:
            out[scope] = {_key(c["contact_id"]) for c in sql_population if c["scopes"][scope]}
    return out


def split_legacy_keys(keys: set) -> tuple[set, set]:
    """(hubspot_identified, without_hubspot_identity) — a legacy row keyed
    ``id:<leads.id>`` carries no HubSpot contact id and can never overlap."""
    hubspot = {k for k in keys if k and not k.startswith("id:")}
    return hubspot, keys - hubspot


def classification_gap_breakdown(legacy_contacts: list[dict]) -> dict:
    """Stale / missing classification, split by whether the contact is an SQL.
    This is the split the production status function does NOT make."""
    out = {"sql_stale": 0, "sql_missing": 0, "non_sql_stale": 0, "non_sql_missing": 0}
    for c in legacy_contacts:
        state = c.get("classification_state")
        prefix = "sql" if c.get("is_sql") else "non_sql"
        if state == "stale":
            out[f"{prefix}_stale"] += 1
        elif state == "missing":
            out[f"{prefix}_missing"] += 1
    return out


def campaign_identity_breakdown(legacy_contacts: list[dict]) -> dict:
    """Why Google-Ads-source legacy SQLs are not campaign-attributable."""
    out = {"unmatched_campaign_identities": 0, "ambiguous_campaign_identities": 0,
           "not_google_ads_campaign": 0, "missing_or_pseudo_campaign": 0,
           "campaign_identity_unavailable": 0}
    for c in legacy_contacts:
        if not c.get("is_google_ads_source_sql") or c.get("is_campaign_attributable_sql"):
            continue
        reasons = set(c.get("scope_block_reasons") or [])
        if "unresolved_campaign_mapping" in reasons:
            out["unmatched_campaign_identities"] += 1
        if "ambiguous_campaign_identity" in reasons:
            out["ambiguous_campaign_identities"] += 1
        if "campaign_not_google_ads" in reasons:
            out["not_google_ads_campaign"] += 1
        if reasons & {"missing_campaign", "pseudo_campaign", "email_campaign"}:
            out["missing_or_pseudo_campaign"] += 1
        if "campaign_identity_unavailable" in reasons:
            out["campaign_identity_unavailable"] += 1
    return out


def lifecycle_campaign_breakdown(sql_population: list[dict]) -> dict:
    out = {"unmatched_campaign_identities": 0, "ambiguous_campaign_identities": 0,
           "not_google_ads_campaign": 0, "missing_or_pseudo_campaign": 0,
           "campaign_identity_unavailable": 0}
    for c in sql_population:
        if not c["scopes"].get("google_ads_source") or c["scopes"].get("campaign_attributable"):
            continue
        reason = c.get("campaign_block_reason")
        if reason == "unresolved_campaign_mapping":
            out["unmatched_campaign_identities"] += 1
        elif reason == "ambiguous_campaign_identity":
            out["ambiguous_campaign_identities"] += 1
        elif reason == "campaign_not_google_ads":
            out["not_google_ads_campaign"] += 1
        elif reason in ("missing_campaign", "pseudo_campaign", "email_campaign"):
            out["missing_or_pseudo_campaign"] += 1
        elif reason == "campaign_identity_unavailable":
            out["campaign_identity_unavailable"] += 1
    return out


def legacy_reconciliation_reasons(counts: dict, breakdown: dict,
                                  keyword_attributable=None) -> list[str]:
    """Machine-readable reasons behind the legacy reconciliation status — the
    causes ``canonical_contact_outcome_service._reconciliation_status`` acts on
    but does not name."""
    reasons: list[str] = []
    total = counts.get("total_all_source_sqls")
    ga = counts.get("google_ads_source_sqls")
    camp = counts.get("campaign_attributable_sqls")
    ordered = [keyword_attributable, camp, ga, total]
    present = [x for x in ordered if x is not None]
    if any(a > b for a, b in zip(present, present[1:])):
        reasons.append("scope_nesting_broken")
    if camp is None and ga is not None:
        reasons.append("campaign_identity_unavailable")
    if counts.get("excluded_sql_contacts"):
        reasons.append("excluded_sql_contacts")
    if breakdown.get("sql_stale"):
        reasons.append("stale_sql_classification")
    if breakdown.get("sql_missing"):
        reasons.append("missing_sql_classification")
    if breakdown.get("non_sql_stale"):
        reasons.append("stale_non_sql_classification")
    if breakdown.get("non_sql_missing"):
        reasons.append("missing_non_sql_classification")
    if ga is not None and camp is not None and ga != camp:
        reasons.append("google_ads_sqls_not_campaign_attributable")
    if keyword_attributable is not None and camp is not None and keyword_attributable != camp:
        reasons.append("campaign_sqls_not_keyword_attributable")
    return reasons


GAP_CATEGORIES = ("sql_stale", "sql_missing", "non_sql_stale", "non_sql_missing")


def _counterfactual_counts(counts: dict, breakdown: dict, removed: set) -> dict:
    """The production counts with the named gap categories removed. Production
    accumulates ``stale_classification_contacts`` /
    ``missing_classification_contacts`` over SQL and non-SQL contacts alike;
    removing a category subtracts exactly its contacts from that total."""
    out = dict(counts)
    stale = (0 if "sql_stale" in removed else breakdown["sql_stale"]) + \
            (0 if "non_sql_stale" in removed else breakdown["non_sql_stale"])
    missing = (0 if "sql_missing" in removed else breakdown["sql_missing"]) + \
              (0 if "non_sql_missing" in removed else breakdown["non_sql_missing"])
    out["stale_classification_contacts"] = stale
    out["missing_classification_contacts"] = missing
    return out


def hidden_reconciliation_causes(counts: dict, legacy_contacts: list[dict], *,
                                 scope: str, reconcile_fn,
                                 keyword_attributable=None,
                                 available: bool = True) -> dict:
    """Prove, per gap category, whether it changes the SQL reconciliation status.

    ``reconcile_fn`` is the production
    ``canonical_contact_outcome_service.reconciliation_metadata``. It is
    evaluated on the production counts and on one counterfactual per category
    (that category's contacts removed), plus "all SQL gaps removed" and "all
    non-SQL gaps removed". A category *changes* the status only when its own
    counterfactual yields a different status than production. When no single
    category changes the status but removing a group does, the group is
    reported as a JOINT dependency rather than attributed to any one member.
    Another independent cause (an unresolved campaign identity, an excluded
    SQL, a nesting violation) keeps every counterfactual ``partial`` and so
    correctly yields ``False`` everywhere.
    """
    breakdown = classification_gap_breakdown(legacy_contacts)

    def status_without(removed: set) -> str:
        cf = _counterfactual_counts(counts, breakdown, removed)
        return reconcile_fn({"counts": cf}, scope, available=available,
                            keyword_attributable=keyword_attributable)["reconciliation_status"]

    production = status_without(set())
    single = {cat: status_without({cat}) for cat in GAP_CATEGORIES}
    without_all_non_sql = status_without({"non_sql_stale", "non_sql_missing"})
    without_all_sql = status_without({"sql_stale", "sql_missing"})
    without_all = status_without(set(GAP_CATEGORIES))

    changes = {cat: (breakdown[cat] > 0 and single[cat] != production)
               for cat in GAP_CATEGORIES}
    non_sql_group_changes = without_all_non_sql != production
    sql_group_changes = without_all_sql != production
    joint = {
        "non_sql": non_sql_group_changes and not (changes["non_sql_stale"] or changes["non_sql_missing"]),
        "sql": sql_group_changes and not (changes["sql_stale"] or changes["sql_missing"]),
        "all_gaps": (without_all != production) and not non_sql_group_changes and not sql_group_changes,
    }
    non_sql_gap_present = bool(breakdown["non_sql_stale"] or breakdown["non_sql_missing"])
    # A single-category flip implies the group flip (removing more gaps can only
    # move the status the same way), so the per-category flags can never
    # contradict the group-level flag. Asserted, not assumed.
    consistent = (not (changes["non_sql_stale"] or changes["non_sql_missing"])
                  or non_sql_group_changes)
    return {
        "sql_contacts_stale_classification": breakdown["sql_stale"],
        "sql_contacts_missing_classification": breakdown["sql_missing"],
        "non_sql_contacts_stale_classification": breakdown["non_sql_stale"],
        "non_sql_contacts_missing_classification": breakdown["non_sql_missing"],
        "production_status": production,
        "status_without": {
            "sql_stale": single["sql_stale"],
            "sql_missing": single["sql_missing"],
            "non_sql_stale": single["non_sql_stale"],
            "non_sql_missing": single["non_sql_missing"],
            "all_sql_gaps": without_all_sql,
            "all_non_sql_gaps": without_all_non_sql,
            "all_classification_gaps": without_all,
        },
        "category_changes_sql_status": changes,
        "joint_dependency": joint,
        "status_if_only_sql_gaps_counted": without_all_non_sql,
        "non_sql_gap_present": non_sql_gap_present,
        "irrelevant_non_sql_gap_affects_sql_status": non_sql_group_changes,
        "flags_consistent": consistent,
        "status_function": "services.canonical_contact_outcome_service._reconciliation_status",
        "method": "counterfactual re-evaluation of the production status function per gap category",
    }


def lifecycle_reasons_not_about_sql(reasons: list[str], coverage: dict) -> list[str]:
    """Lifecycle ``partial`` reasons that do not concern the SQL event: a missing
    Lead/MQL/Opportunity/Customer stage date, or an unknown lifecycle stage,
    downgrade the whole funnel status even when SQL coverage is complete."""
    out: list[str] = []
    missing = (coverage or {}).get("stage_reached_without_entry_date") or {}
    if "missing_stage_entry_date" in reasons and not missing.get("sql"):
        out.append("missing_stage_entry_date:non_sql_events_only")
    if "unknown_lifecycle_stage" in reasons:
        out.append("unknown_lifecycle_stage")
    return out


def compare_window(window: dict, *, legacy: dict | None, lifecycle: dict | None,
                   legacy_all_time_keys: set | None, lifecycle_all_time_keys: set | None,
                   legacy_keyword_keys: set | None, legacy_keyword_note: str | None,
                   reconcile_fn, lifecycle_status_fn) -> dict:
    """Compare the two SQL populations for ONE window on durable keys.

    ``legacy`` is ``{available, counts, contacts, identity_available}`` from the
    legacy service; ``lifecycle`` is ``{available, populations}`` from the
    lifecycle service (``populations`` = ``build_populations`` output).
    Either may be ``None`` / unavailable — the comparison then reports
    ``unavailable`` for that side and never a fabricated zero.
    """
    out: dict = {
        "window_type": window["window_type"],
        "window": window["window_key"],
        "start_date": window["start_date"],
        "end_date": window["end_date"],
        "legacy_definition": LEGACY_STANDARD["sql_definition"],
        "legacy_date_field": LEGACY_STANDARD["date_field"],
        "legacy_dedup_key": LEGACY_STANDARD["dedup_key"],
        "lifecycle_definition": CANONICAL_STANDARD["sql_event"],
        "lifecycle_event_date_field": "date_entered_sql",
        "lifecycle_dedup_key": CANONICAL_STANDARD["dedup_key"],
        "legacy_available": bool(legacy and legacy.get("available")),
        "lifecycle_available": bool(lifecycle and lifecycle.get("available")),
    }

    # ── legacy side ──────────────────────────────────────────────────────────
    if out["legacy_available"]:
        contacts = legacy.get("contacts") or []
        counts = dict(legacy.get("counts") or {})
        keys = legacy_scope_keys(contacts)
        hubspot_keys, no_identity = split_legacy_keys(keys["all_source"])
        kw = legacy_keyword_keys
        kw_count = len(kw) if kw is not None else None
        camp_available = legacy.get("identity_available", True) and counts.get(
            "campaign_attributable_sqls") is not None
        breakdown = classification_gap_breakdown(contacts)
        hidden = hidden_reconciliation_causes(
            counts, contacts, scope="campaign_attributable_sqls",
            reconcile_fn=reconcile_fn, keyword_attributable=kw_count)
        legacy_status = hidden["production_status"]
        out["legacy_counts"] = {
            "all_source": len(keys["all_source"]),
            "google_ads_source": len(keys["google_ads_source"]),
            "campaign_attributable": len(keys["campaign_attributable"]) if camp_available else None,
            "keyword_attributable": kw_count,
            "keyword_attributable_note": legacy_keyword_note,
        }
        out["legacy_excluded_sql_contacts"] = counts.get("excluded_sql_contacts")
        out["legacy_missing_business_date_sql_contacts"] = counts.get(
            "missing_business_date_sql_contacts")
        out["legacy_sqls_without_hubspot_identity"] = len(no_identity)
        out["legacy_campaign_identity"] = campaign_identity_breakdown(contacts)
        out["classification_gaps"] = hidden
        out["legacy_reconciliation"] = {
            "status": legacy_status,
            "reasons": legacy_reconciliation_reasons(counts, breakdown, kw_count),
            "scope": "campaign_attributable_sqls",
        }
        out["legacy_complete_total_publishable"] = legacy_status == "reconciled"
        out["legacy_cpql_denominator_complete"] = (
            legacy_status == "reconciled" and camp_available)
    else:
        hubspot_keys = None
        keys = None
        out["legacy_counts"] = {"all_source": None, "google_ads_source": None,
                                "campaign_attributable": None, "keyword_attributable": None,
                                "keyword_attributable_note": legacy_keyword_note}
        out["legacy_reconciliation"] = {"status": "unavailable",
                                        "reasons": ["legacy_source_unavailable"],
                                        "scope": "campaign_attributable_sqls"}
        out["classification_gaps"] = None
        out["legacy_complete_total_publishable"] = False
        out["legacy_cpql_denominator_complete"] = False

    # ── lifecycle side ───────────────────────────────────────────────────────
    if out["lifecycle_available"]:
        pops = lifecycle["populations"]
        sql_pop = pops["events"]["sql"]
        lkeys = lifecycle_scope_keys(sql_pop)
        coverage = pops.get("coverage") or {}
        missing_sql_dates = ((coverage.get("stage_reached_without_entry_date") or {})
                             .get("sql") or 0)
        status = lifecycle_status_fn(pops, available=True)
        reasons = list(status.get("reasons") or [])
        out["lifecycle_counts"] = {
            scope: (len(v) if v is not None else None) for scope, v in lkeys.items()}
        out["lifecycle_campaign_identity"] = lifecycle_campaign_breakdown(sql_pop)
        out["missing_sql_entry_date_count"] = missing_sql_dates
        out["lifecycle_unknown_stage_contacts"] = coverage.get("unknown_lifecycle_stage_contacts")
        out["lifecycle_reconciliation"] = {
            "status": status.get("status"),
            "reasons": reasons,
            "reasons_not_about_sql": lifecycle_reasons_not_about_sql(reasons, coverage),
        }
        out["lifecycle_complete_total_publishable"] = (
            status.get("status") != "mismatch" and missing_sql_dates == 0)
        out["lifecycle_complete_total_reasons"] = (
            [] if out["lifecycle_complete_total_publishable"]
            else (["missing_stage_entry_date:sql"] if missing_sql_dates else [])
            + (["scope_nesting_broken"] if status.get("status") == "mismatch" else []))
        out["lifecycle_cpql_denominator_complete"] = (
            out["lifecycle_complete_total_publishable"]
            and lkeys.get("campaign_attributable") is not None)
    else:
        lkeys = None
        out["lifecycle_counts"] = {"all_source": None, "google_ads_source": None,
                                   "campaign_attributable": None, "keyword_attributable": None}
        out["missing_sql_entry_date_count"] = None
        out["lifecycle_reconciliation"] = {"status": "unavailable",
                                           "reasons": ["canonical_contact_store_unavailable"],
                                           "reasons_not_about_sql": []}
        out["lifecycle_complete_total_publishable"] = False
        out["lifecycle_complete_total_reasons"] = ["canonical_contact_store_unavailable"]
        out["lifecycle_cpql_denominator_complete"] = False

    # ── set comparison ───────────────────────────────────────────────────────
    if hubspot_keys is not None and lkeys is not None:
        lifecycle_all = lkeys["all_source"]
        overlap = hubspot_keys & lifecycle_all
        legacy_only = hubspot_keys - lifecycle_all
        lifecycle_only = lifecycle_all - hubspot_keys
        both_doctrines = (legacy_all_time_keys or set()) & (lifecycle_all_time_keys or set())
        date_shifted = (legacy_only | lifecycle_only) & both_doctrines
        legacy_only_never_lifecycle_sql = {
            k for k in legacy_only if k not in (lifecycle_all_time_keys or set())}
        lifecycle_only_never_legacy_sql = {
            k for k in lifecycle_only if k not in (legacy_all_time_keys or set())}
        out.update({
            "comparison_available": True,
            "overlap_count": len(overlap),
            "legacy_only_count": len(legacy_only),
            "lifecycle_only_count": len(lifecycle_only),
            "date_shifted_count": len(date_shifted),
            "legacy_only_never_lifecycle_sql_count": len(legacy_only_never_lifecycle_sql),
            "lifecycle_only_never_legacy_qualified_count": len(lifecycle_only_never_legacy_sql),
            "totals_equal": len(hubspot_keys) == len(lifecycle_all),
            "populations_equal": hubspot_keys == lifecycle_all,
            "population_difference": hubspot_keys != lifecycle_all,
            "scope_set_comparison": {
                scope: _scope_set_delta(keys.get(scope) if keys else None, lkeys.get(scope))
                for scope in ("google_ads_source", "campaign_attributable")
            },
        })
        out["difference_reason_codes"] = _difference_reason_codes(out)
    else:
        out.update({
            "comparison_available": False,
            "overlap_count": None, "legacy_only_count": None,
            "lifecycle_only_count": None, "date_shifted_count": None,
            "legacy_only_never_lifecycle_sql_count": None,
            "lifecycle_only_never_legacy_qualified_count": None,
            "totals_equal": None, "populations_equal": None,
            "population_difference": None, "scope_set_comparison": None,
            "difference_reason_codes": ["comparison_unavailable"],
        })
    return out


def _scope_set_delta(legacy_keys: set | None, lifecycle_keys: set | None) -> dict:
    if legacy_keys is None or lifecycle_keys is None:
        return {"available": False, "overlap": None, "legacy_only": None,
                "lifecycle_only": None}
    hub, _ = split_legacy_keys(legacy_keys)
    return {"available": True, "overlap": len(hub & lifecycle_keys),
            "legacy_only": len(hub - lifecycle_keys),
            "lifecycle_only": len(lifecycle_keys - hub)}


def _difference_reason_codes(cmp: dict) -> list[str]:
    codes: list[str] = []
    if cmp.get("date_shifted_count"):
        codes.append("event_date_moved_from_creation_to_stage_entry")
    if cmp.get("legacy_only_never_lifecycle_sql_count"):
        codes.append("legacy_qualified_without_lifecycle_sql_entry")
    if cmp.get("lifecycle_only_never_legacy_qualified_count"):
        codes.append("lifecycle_sql_entry_without_legacy_qualified_status")
    if cmp.get("legacy_sqls_without_hubspot_identity"):
        codes.append("legacy_rows_without_hubspot_identity")
    if cmp.get("missing_sql_entry_date_count"):
        codes.append("lifecycle_sql_reached_without_entry_timestamp")
    if cmp.get("legacy_excluded_sql_contacts"):
        codes.append("legacy_lead_truth_exclusions")
    if not codes and cmp.get("population_difference"):
        codes.append("unexplained_population_difference")
    if not cmp.get("population_difference"):
        codes.append("populations_identical")
    return codes


# ── Coverage gaps + conflicts ────────────────────────────────────────────────
def coverage_gaps(window_comparisons: list[dict]) -> dict:
    """The data-coverage gaps that prevent a complete lifecycle SQL total."""
    incomplete = [w for w in window_comparisons
                  if (w.get("missing_sql_entry_date_count") or 0) > 0]
    identity_unavailable = [w for w in window_comparisons
                            if w.get("lifecycle_available")
                            and w.get("lifecycle_counts", {}).get("campaign_attributable") is None]
    without_identity = [w for w in window_comparisons
                        if (w.get("legacy_sqls_without_hubspot_identity") or 0) > 0]
    max_missing = max((w.get("missing_sql_entry_date_count") or 0)
                      for w in window_comparisons) if window_comparisons else None
    return {
        "lifecycle_sql_reached_without_entry_timestamp": {
            "max_contacts": max_missing,
            "windows_affected": [f"{w['window_type']}:{w['window']}" for w in incomplete],
            "rule": CANONICAL_STANDARD["missing_timestamp_rule"],
            "blocks_complete_lifecycle_total": bool(incomplete),
        },
        "campaign_identity_unavailable_windows": [
            f"{w['window_type']}:{w['window']}" for w in identity_unavailable],
        "legacy_sqls_without_hubspot_identity_windows": [
            f"{w['window_type']}:{w['window']}" for w in without_identity],
        "windows_with_population_differences": [
            f"{w['window_type']}:{w['window']}" for w in window_comparisons
            if w.get("population_difference")],
        "windows_with_incomplete_lifecycle_timestamp_coverage": [
            f"{w['window_type']}:{w['window']}" for w in incomplete],
        "windows_where_non_sql_gap_changes_sql_status": [
            f"{w['window_type']}:{w['window']}" for w in window_comparisons
            if (w.get("classification_gaps") or {}).get("irrelevant_non_sql_gap_affects_sql_status")],
    }


# ── Report assembly ──────────────────────────────────────────────────────────
def assemble_report(*, static: dict, consumers: list[dict], cpql_consumers: list[dict],
                    decision_surfaces: list[dict], known_conflicts: list[dict],
                    window_comparisons: list[dict], runtime_available: bool,
                    runtime_reason: str | None, generated_at: str,
                    audited_commit: str | None, write_safety: dict) -> dict:
    unclassified = static["unclassified_occurrences"]
    registry_problems = static["registry_problems"]
    active_legacy = [c for c in consumers if c["classification"] == CLS_LEGACY]
    mixed = [c for c in consumers if c["classification"] == CLS_MIXED]
    canonical = [c for c in consumers if c["classification"] == CLS_CANONICAL]

    audit_complete = (not unclassified and not registry_problems
                      and write_safety.get("ok", False))
    migration_complete = audit_complete and not active_legacy and not mixed
    if not runtime_available:
        verdict = VERDICT_SOURCE_UNAVAILABLE
        exit_code = EXIT_SOURCE_UNAVAILABLE
    elif not audit_complete:
        verdict = VERDICT_INCOMPLETE
        exit_code = EXIT_INCOMPLETE
    else:
        verdict = VERDICT_READY
        exit_code = EXIT_COMPLETE

    gaps = coverage_gaps(window_comparisons)
    summary = {
        "audit_complete": audit_complete,
        "active_canonical_lifecycle_consumers": len(canonical),
        "active_legacy_consumers": len(active_legacy),
        "mixed_adapter_consumers": len(mixed),
        "unknown_unclassified_occurrences": len(unclassified),
        "consumers_affecting_executive_totals": sum(
            1 for c in consumers if c["classification"] in ACTIVE_CLASSIFICATIONS
            and c.get("affects_executive_totals")),
        "consumers_affecting_operational_decisions": sum(
            1 for c in consumers if c["classification"] in ACTIVE_CLASSIFICATIONS
            and c.get("affects_operational_decisions")),
        "windows_audited": len(window_comparisons),
        "windows_with_population_differences": len(gaps["windows_with_population_differences"]),
        "windows_with_incomplete_lifecycle_timestamp_coverage": len(
            gaps["windows_with_incomplete_lifecycle_timestamp_coverage"]),
        "windows_where_non_sql_gap_changes_sql_status": len(
            gaps["windows_where_non_sql_gap_changes_sql_status"]),
        "external_writes_performed": False,
        "database_writes_performed": False,
        "verdict": verdict,
    }
    return {
        "audit": "PR-ADS-158 SQL doctrine inventory",
        "generated_at": generated_at,
        "audited_commit": audited_commit,
        "audit_complete": audit_complete,
        "migration_complete": migration_complete,
        "verdict": verdict,
        "exit_code": exit_code,
        "runtime_comparison_available": runtime_available,
        "runtime_unavailable_reason": runtime_reason,
        "canonical_standard": CANONICAL_STANDARD,
        "legacy_standard": LEGACY_STANDARD,
        "inventory": consumers,
        "active_legacy_consumers": active_legacy,
        "mixed_consumers": mixed,
        "canonical_lifecycle_consumers": canonical,
        "unclassified_occurrences": unclassified,
        "registry_problems": registry_problems,
        "static_discovery": {
            k: v for k, v in static.items()
            if k not in ("unclassified_occurrences", "registry_problems", "occurrences")},
        "occurrences": static["occurrences"],
        "window_comparisons": window_comparisons,
        "coverage_gaps": gaps,
        "cpql_consumers": cpql_consumers,
        "decision_surfaces": decision_surfaces,
        "known_contract_conflicts": known_conflicts,
        "write_safety": write_safety,
        "external_writes_performed": False,
        "database_writes_performed": False,
        "summary": summary,
    }


def _fmt(v):
    return "—" if v is None else str(v)


def render_human(report: dict) -> str:
    """Human-readable report ending with the PR-ADS-158 §9 summary block."""
    s = report["summary"]
    lines: list[str] = []
    lines.append("=" * 76)
    lines.append("PR-ADS-158 — SYSTEM-WIDE SQL DOCTRINE AUDIT (READ-ONLY)")
    lines.append(f"Generated: {report['generated_at']}   Commit: {_fmt(report.get('audited_commit'))}")
    lines.append("=" * 76)
    cs = report["canonical_standard"]
    lines.append("Canonical reference standard:")
    lines.append(f"  SQL event : {cs['sql_event']}")
    lines.append(f"  Property  : {cs['source_property']}  ->  column {cs['durable_column']}")
    lines.append(f"  Dedup     : {cs['dedup_key']}   Window date: {cs['window_date']}")
    lines.append(f"  Scopes    : {cs['scope_lattice']}")
    ls = report["legacy_standard"]
    lines.append("Legacy doctrine still found in production:")
    lines.append(f"  {ls['sql_definition']} on {ls['date_field']} "
                 f"(dedup {ls['dedup_key']})")
    lines.append("")
    lines.append("Static inventory (registry-classified consumers):")
    for c in report["inventory"]:
        lines.append(f"  [{c['classification']}] {c['consumer']}")
        lines.append(f"      def={c['sql_definition']} | date={c['date_field']} | "
                     f"scope={c['scope']} | {c['code_location']}")
    sd = report["static_discovery"]
    lines.append("")
    lines.append(f"Discovered occurrences: {sd['occurrences_total']} "
                 f"(production {sd['production_occurrences']}, "
                 f"tests/docs/fixtures {sd['non_production_occurrences']})")
    for cls, n in sd["occurrences_by_classification"].items():
        lines.append(f"  {cls:34s} {n}")
    if report["unclassified_occurrences"]:
        lines.append("UNCLASSIFIED production occurrences (audit incomplete):")
        for o in report["unclassified_occurrences"]:
            lines.append(f"  {o['path']}:{o['line']} [{o['pattern_id']}] {o['snippet']}")
    if report["registry_problems"]:
        lines.append("Registry problems:")
        for p in report["registry_problems"]:
            lines.append(f"  - {p}")
    lines.append("")
    lines.append("Runtime window comparison (legacy vs lifecycle, durable contact keys):")
    if not report["runtime_comparison_available"]:
        lines.append(f"  UNAVAILABLE — {report.get('runtime_unavailable_reason')}")
    for w in report["window_comparisons"]:
        lc, fc = w["legacy_counts"], w["lifecycle_counts"]
        lines.append(f"  {w['window_type']}:{w['window']}  [{_fmt(w['start_date'])} .. {w['end_date']}]")
        lines.append(f"      legacy    all={_fmt(lc['all_source'])} ga={_fmt(lc['google_ads_source'])} "
                     f"camp={_fmt(lc['campaign_attributable'])} kw={_fmt(lc['keyword_attributable'])} "
                     f"status={w['legacy_reconciliation']['status']} "
                     f"reasons={','.join(w['legacy_reconciliation']['reasons']) or '-'}")
        lines.append(f"      lifecycle all={_fmt(fc['all_source'])} ga={_fmt(fc['google_ads_source'])} "
                     f"camp={_fmt(fc['campaign_attributable'])} kw={_fmt(fc['keyword_attributable'])} "
                     f"status={w['lifecycle_reconciliation']['status']} "
                     f"reasons={','.join(w['lifecycle_reconciliation']['reasons']) or '-'}")
        lines.append(f"      overlap={_fmt(w['overlap_count'])} legacy_only={_fmt(w['legacy_only_count'])} "
                     f"lifecycle_only={_fmt(w['lifecycle_only_count'])} "
                     f"date_shifted={_fmt(w['date_shifted_count'])} "
                     f"missing_sql_entry_date={_fmt(w['missing_sql_entry_date_count'])} "
                     f"diff={','.join(w['difference_reason_codes'])}")
        gaps = w.get("classification_gaps") or {}
        if gaps:
            lines.append(f"      classification gaps: sql stale={gaps['sql_contacts_stale_classification']} "
                         f"missing={gaps['sql_contacts_missing_classification']} | "
                         f"non-sql stale={gaps['non_sql_contacts_stale_classification']} "
                         f"missing={gaps['non_sql_contacts_missing_classification']} | "
                         f"irrelevant_non_sql_gap_affects_sql_status="
                         f"{gaps['irrelevant_non_sql_gap_affects_sql_status']}")
        lines.append(f"      publishable: legacy_total={w['legacy_complete_total_publishable']} "
                     f"lifecycle_total={w['lifecycle_complete_total_publishable']} | "
                     f"cpql denominator complete: legacy={w['legacy_cpql_denominator_complete']} "
                     f"lifecycle={w['lifecycle_cpql_denominator_complete']}")
    lines.append("")
    lines.append("CPQL consumers:")
    for c in report["cpql_consumers"]:
        lines.append(f"  {c['consumer']}: denominator={c['sql_denominator_definition']} "
                     f"scope={c['sql_denominator_scope']} date={c['sql_denominator_date_field']} "
                     f"publication={c['publication']}")
    lines.append("Decision surfaces depending on an SQL count:")
    for d in report["decision_surfaces"]:
        lines.append(f"  {d['surface']} ({d['sql_definition']}) — {d['code_location']}")
    lines.append("Known contract conflicts:")
    for k in report["known_contract_conflicts"]:
        lines.append(f"  - {k['id']}: {k['summary']}")
    lines.append("")
    lines.append("-" * 76)
    lines.append(f"Audit completed: {'yes' if s['audit_complete'] else 'no'}")
    lines.append(f"Active canonical-lifecycle consumers: {s['active_canonical_lifecycle_consumers']}")
    lines.append(f"Active legacy consumers: {s['active_legacy_consumers']}")
    lines.append(f"Mixed/adapter consumers: {s['mixed_adapter_consumers']}")
    lines.append(f"Unknown/unclassified occurrences: {s['unknown_unclassified_occurrences']}")
    lines.append(f"Consumers affecting executive totals: {s['consumers_affecting_executive_totals']}")
    lines.append(f"Consumers affecting operational decisions: {s['consumers_affecting_operational_decisions']}")
    lines.append(f"Windows with population differences: {s['windows_with_population_differences']}")
    lines.append("Windows with incomplete lifecycle timestamp coverage: "
                 f"{s['windows_with_incomplete_lifecycle_timestamp_coverage']}")
    lines.append(f"External writes performed: {'yes' if s['external_writes_performed'] else 'no'}")
    lines.append(f"Database writes performed: {'yes' if s['database_writes_performed'] else 'no'}")
    lines.append(f"Migration complete: {'yes' if report['migration_complete'] else 'no'}")
    lines.append(f"Verdict: {s['verdict']}")
    return "\n".join(lines)


# ── Write-safety proof (static) ──────────────────────────────────────────────
_WRITE_MODULE_MARKERS = (
    "db.writers", "connectors.oct_uploader", "connectors.negative_pusher",
    "hubspot_contact_funnel_sync_service", "canonical_classification_repair_service",
    "canonical_coverage_repair_service", "lifecycle_history_recovery_service",
    "google_ads_geo_sync_service", "keyword_sync_service", "search_term_sync_service",
    "mailchimp_sync_service", "hubspot_deal_sync_service", "scheduler.",
)
_WRITE_SQL_RE = re.compile(r"(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|COPY|GRANT)\b", re.IGNORECASE)
_HTTP_WRITE_RE = re.compile(r"requests\.(post|put|patch|delete)|httpx\.(post|put|patch|delete)")


def _statement_text(node) -> str | None:
    """The literal text of a statement argument, or None when not a literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def write_safety_proof(module_sources: dict[str, str]) -> dict:
    """Static proof that the audit's OWN modules import no writer and issue no
    write statement. ``module_sources`` maps a label to source text.

    Only statements handed to ``execute`` / ``executemany`` are inspected, so
    a registry description that names a table an existing writer inserts into
    is data, not a write."""
    problems: list[str] = []
    for label, src in module_sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            problems.append(f"{label}: unparsable ({exc})")
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = [mod] + [f"{mod}.{a.name}" for a in node.names]
            for n in names:
                if any(marker in n for marker in _WRITE_MODULE_MARKERS):
                    problems.append(f"{label}: imports write-capable module {n}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("execute", "executemany") and node.args:
                    text = _statement_text(node.args[0])
                    if text is None:
                        problems.append(f"{label}: non-literal statement passed to "
                                        f"{node.func.attr} (cannot prove read-only)")
                    elif _WRITE_SQL_RE.match(text.strip()):
                        problems.append(f"{label}: executes a write statement")
                if node.func.attr == "executemany":
                    problems.append(f"{label}: uses executemany")
        if _HTTP_WRITE_RE.search(src):
            problems.append(f"{label}: contains an HTTP write call")
    return {"ok": not problems, "problems": problems,
            "checked_modules": sorted(module_sources)}


__all__ = [
    "CLASSIFICATIONS", "ACTIVE_CLASSIFICATIONS", "CANONICAL_STANDARD",
    "LEGACY_STANDARD", "PATTERNS", "EVIDENCE_WINDOWS", "BUSINESS_WINDOWS",
    "Occurrence", "discover_occurrences", "classify_occurrences",
    "validate_rules", "validate_consumers", "build_static_inventory",
    "legacy_scope_keys", "lifecycle_scope_keys", "split_legacy_keys",
    "classification_gap_breakdown", "hidden_reconciliation_causes",
    "legacy_reconciliation_reasons", "compare_window", "coverage_gaps",
    "assemble_report", "render_human", "write_safety_proof",
]
