"""
analysis/legacy_source_guard.py

PR-ADS-156-F1 §3 — the ONE static scan for production code that still reads a
retired evidence source, and the ONE allowlist of the places permitted to.

Why it lives here rather than in a test
---------------------------------------
It began as a test-local helper. That made it a rule the test suite enforced and
the operational audit could not see: `audit_keyword_search_term_freshness`
declared a `legacy_source_active` violation code it had no way to emit, because
the only implementation of "is a legacy source active?" lived in a test module
the command cannot import as a contract.

A declared violation nothing can raise is worse than no violation at all — it
reads, in JSON, as a check that ran and passed. So the scan lives here, in a
module with no dependency on either caller, and BOTH execute it: the audit
command emits its findings as violations, and the regression suite asserts over
the same function. One definition of "legacy read", one allowlist, and no way
for the two to disagree.

What counts as a legacy read
----------------------------
Three concrete shapes, all in CODE (comments and docstrings are stripped first —
a guard that greps raw text fails on the paragraph explaining what was removed,
which is both wrong and a strong incentive to delete the explanation):

  * importing the retired Windsor connector;
  * reading from or writing into the legacy ``keywords`` snapshot table;
  * calling ``write_keywords(`` outside the places that still legitimately do.

Pure and read-only: it parses source files and returns findings. It opens no
database, calls no external service, and writes nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

#: Directories whose contents are PRODUCTION paths — the code that runs to serve
#: a page or complete a scheduled run. `scripts/` is deliberately excluded: a
#: one-off migration or an audit command reading legacy rows is the point of it.
#:
#: PR-ADS-156-F2 §4 widened this from ("scheduler", "services"). `analysis/` and
#: `api/` shape what a user sees just as directly — a legacy read there reaches
#: the page by a different route, not a less real one. `db/` is included because
#: that is where a legacy REPOSITORY HELPER would be introduced, and a helper is
#: how an indirect read enters a service that never mentions the legacy table.
PRODUCTION_DIRS = ("scheduler", "services", "analysis", "api", "db")

#: Retired providers and legacy evidence helpers, by the names a caller would
#: use. Detected as imports OR as calls, because a service that imports a
#: repository module and calls `repo.fetch_legacy_keywords(...)` never contains
#: the string "FROM keywords" — the literal-SQL scan alone would clear it.
RETIRED_PROVIDER_NAMES: frozenset[str] = frozenset({
    "windsor_pull", "windsor_mcp", "backfill_windsor",
    "import_windsor_mcp_search_terms",
})

#: Legacy keyword/search-term repository helpers. A production evidence path
#: calling one of these is reading the legacy snapshot at one remove.
LEGACY_REPOSITORY_HELPERS: frozenset[str] = frozenset({
    "fetch_keyword_theme_snapshot",
    "fetch_legacy_keyword_snapshot",
    "write_keywords",
})

#: The retired local snapshots. Consuming one as CURRENT evidence is the defect
#: PR-ADS-156-F2 §1 closed inside waste detection; this stops it reappearing
#: anywhere else.
LEGACY_SNAPSHOT_FILES: frozenset[str] = frozenset({
    "ads_search_terms.json", "ads_keywords.json", "windsor_search_terms.json",
})

#: Keyword-population fallbacks inside search-term evidence. Substituting one
#: population for another turns "no wasteful searches" into a keyword report
#: wearing a search-term label.
KEYWORD_FALLBACK_MARKERS: frozenset[str] = frozenset({
    "keywords_fallback", "keyword_fallback",
})

#: Modules allowed to touch retired providers or the legacy snapshot, and why.
#: Deliberately narrow: audit, reconciliation, migration and historical
#: diagnostics are the four reasons legacy access is legitimate, and every entry
#: names which one it is. An allowlist nobody maintains is a hole with
#: documentation attached, so the suite asserts each entry exists and is
#: justified.
LEGACY_ACCESS_ALLOWLIST: dict[str, str] = {
    "scripts/backfill_windsor.py": "migration — historical Windsor import",
    "scripts/import_windsor_mcp_search_terms.py": "migration — one-off MCP import",
    "connectors/windsor_pull.py": "retired connector, retained for history",
    "connectors/gclid_match.py": "historical diagnostic over the legacy JSON",
    "db/keyword_repository.py": "audit — counts legacy snapshot rows",
    "db/revenue_repository.py": "legacy keyword-theme snapshot for the "
                                "campaign page (non-evidence)",
    "db/writers.py": "writer for the legacy snapshot, still consumed",
    "api/server.py": "legacy snapshot consumers + legacy state reported as legacy",
    "scripts/audit_production_reality.py": "audit — production reality report",
    # PR-ADS-156 §5 required an INSPECTION before stopping the scheduled legacy
    # writes, and the inspection found four live consumers: the aggregated
    # keyword endpoint, the campaign drill-down preview, the keyword-review
    # action queue, and the keyword-theme snapshot behind the Campaigns page.
    # None of them is Keyword Evidence. Stopping the writes would have starved
    # all four, which is the "silently remove it" the section forbids — so the
    # writes stay, documented and non-authoritative, and this guard's job is to
    # stop NEW ones appearing rather than to pretend these do not exist.
    "scheduler/weekly.py": "legacy snapshot writer — four live non-evidence "
                           "consumers, documented in docs/38",
    "scheduler/monthly.py": "legacy snapshot writer — four live non-evidence "
                            "consumers, documented in docs/38",
    "scripts/verify_search_terms_pipeline.py": "diagnostic — reports legacy state",
    # PR-ADS-156-F2 §4 widened the scan to analysis/, api/ and db/, and to
    # indirect reads by NAME. These two are the consequences, and both are the
    # documented non-evidence consumers rather than new holes.
    "analysis/legacy_source_guard.py": "the guard itself — it must name the "
                                       "retired providers, legacy helpers and "
                                       "snapshots it detects",
    "services/dashboard_campaigns_service.py":
        "legacy keyword-theme snapshot behind the Campaigns page — one of the "
        "four inspected non-evidence consumers (PR-ADS-156 §5), historical and "
        "never a Keyword Evidence input",
}

#: Machine-readable reasons, so a caller can group findings without parsing prose.
REASON_RETIRED_PROVIDER_IMPORT = "retired_provider_import"
REASON_LEGACY_SNAPSHOT_ACCESS = "legacy_snapshot_access"
REASON_LEGACY_SNAPSHOT_WRITE = "legacy_snapshot_write"
#: PR-ADS-156-F2 §4 — the indirect routes.
REASON_LEGACY_REPOSITORY_CALL = "legacy_repository_call"
REASON_LEGACY_SNAPSHOT_FILE = "legacy_snapshot_file_consumed"
REASON_KEYWORD_FALLBACK = "keyword_fallback_in_search_term_evidence"


def code_only(path: Path | str) -> str:
    """Source with comments AND docstrings removed.

    The guard is about what the code DOES. Stripping prose first means a module
    can explain at length why it no longer reads Windsor without that
    explanation tripping the check — otherwise the cheapest way to pass would be
    to delete the explanation.
    """
    src = Path(path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # An unparseable file cannot be cleared by this guard, so return it
        # verbatim and let the substring checks judge it.
        return src
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    for doc in docstrings:
        code = code.replace(doc, "")
    return code


def _imported_and_called(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Every name this module imports, and every name it calls.

    PR-ADS-156-F2 §4 — the literal-SQL scan alone clears a service that imports
    a repository module and calls ``repo.fetch_legacy_keywords(...)``: the
    string "FROM keywords" is in the repository, not in the service. Names are
    what actually cross a module boundary, so names are what this looks at.
    """
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.update(alias.name.split("."))
                if alias.asname:
                    imported.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.update(node.module.split("."))
            for alias in node.names:
                imported.add(alias.name)
                if alias.asname:
                    imported.add(alias.asname)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return imported, called


def scan_legacy_sources(root: Path | str | None = None,
                        directories: tuple[str, ...] = PRODUCTION_DIRS) -> list[dict]:
    """Every unauthorised legacy evidence read in the production directories.

    Returns ``[{"path", "reason", "detail"}, …]``, sorted by path, and an empty
    list when nothing is found — which is a real all-clear here, because unlike
    a database read this scan cannot be refused: the files are either on disk
    and parsed, or the repository is not there at all.

    Six shapes, all in CODE (comments and docstrings are stripped first):

      * a retired provider imported OR called — by name, so an indirect call
        through an imported module is caught as well as a literal SQL string;
      * the legacy ``keywords`` snapshot read or written in SQL;
      * ``write_keywords(`` outside the documented allowlist;
      * a legacy keyword/search-term REPOSITORY HELPER imported or called —
        the indirect route that a literal-SQL scan cannot see;
      * a retired local JSON snapshot consumed as current evidence;
      * a keyword-population fallback inside search-term evidence.
    """
    base = Path(root) if root else _ROOT
    findings: list[dict] = []
    for directory in directories:
        target = base / directory
        if not target.exists():
            continue
        for path in sorted(target.rglob("*.py")):
            rel = str(path.relative_to(base))
            if rel in LEGACY_ACCESS_ALLOWLIST:
                continue
            code = code_only(path)
            lowered = code.lower()
            try:
                imported, called = _imported_and_called(ast.parse(code))
            except SyntaxError:
                imported, called = set(), set()
            names = imported | called

            def add(reason, detail):
                findings.append({"path": rel, "reason": reason, "detail": detail})

            hit = sorted(names & RETIRED_PROVIDER_NAMES)
            if hit or "import windsor" in lowered:
                add(REASON_RETIRED_PROVIDER_IMPORT,
                    f"{rel} imports or calls the retired Windsor provider"
                    + (f" ({', '.join(hit)})" if hit else ""))
            if "from keywords" in lowered or "into keywords" in lowered:
                add(REASON_LEGACY_SNAPSHOT_ACCESS,
                    f"{rel} reads or writes the legacy `keywords` snapshot")
            if "write_keywords(" in code:
                add(REASON_LEGACY_SNAPSHOT_WRITE,
                    f"{rel} writes the legacy `keywords` snapshot")

            helpers = sorted(names & LEGACY_REPOSITORY_HELPERS)
            if helpers:
                add(REASON_LEGACY_REPOSITORY_CALL,
                    f"{rel} imports or calls a legacy evidence repository helper "
                    f"({', '.join(helpers)}) — an indirect read of the legacy "
                    "snapshot")

            snapshots = sorted(f for f in LEGACY_SNAPSHOT_FILES if f in code)
            if snapshots:
                add(REASON_LEGACY_SNAPSHOT_FILE,
                    f"{rel} consumes a retired local snapshot "
                    f"({', '.join(snapshots)}) — a file on disk is not current "
                    "canonical evidence")

            fallbacks = sorted(m for m in KEYWORD_FALLBACK_MARKERS if m in lowered)
            if fallbacks:
                add(REASON_KEYWORD_FALLBACK,
                    f"{rel} substitutes a keyword population for search terms "
                    f"({', '.join(fallbacks)}) — a verified-empty search-term "
                    "interval is a measurement, not a gap to fill")
    return sorted(findings, key=lambda f: (f["path"], f["reason"]))


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-156-F3 review §2 — unscoped `search_terms` readers
# ═════════════════════════════════════════════════════════════════════════════
#
# The F3 review found the operational scripts still reading `search_terms` by
# date alone, months after the production readers had been scoped. That is not a
# gap someone was careless about; it is what happens when a rule lives in the
# reviewers' heads. The rule is now executable.
#
# It scans `scripts/` as well as the production directories, which the legacy
# scan above deliberately does not: reading LEGACY ROWS from a script is the
# whole point of a migration or a historical diagnostic, but reading the CURRENT
# population unscoped is the same defect wherever it happens. An operator's
# verification command and the dashboard must answer with the same rows.

#: Directories scanned for unscoped reads.
SEARCH_TERM_READER_DIRS = (*PRODUCTION_DIRS, "scripts")

#: A scope FACTORY, by the shape of its name. Every helper that produces a
#: `SearchTermScope` ends in `_scope` — `canonical_scope`, `claimed_scope`,
#: `unscoped_history_scope`, and the endpoint-local `_canonical_search_term_scope`
#: — so the guard recognises the pattern rather than a fixed list it would fall
#: behind the first time someone adds a fourth.
SCOPE_FACTORY_SUFFIX = "_scope"

REASON_UNSCOPED_SEARCH_TERM_READ = "unscoped_search_term_read"

#: Reads of `search_terms` that are deliberately NOT account-scoped, each with
#: the reason it is exempt. An entry here is a claim that the code asks a
#: question about HISTORY — what the table holds — rather than about the
#: canonical population a person makes decisions from.
SEARCH_TERM_SCOPE_ALLOWLIST: dict[str, str] = {
    "db/search_term_repository.py:fetch_legacy_currency_audit":
        "historical diagnostic — its entire purpose is to count the rows the "
        "canonical scope excludes, so scoping it would make it report zero",
    "analysis/legacy_source_guard.py":
        "the guard itself — it must contain the markers it searches for",
}


def _innermost_statements(tree: ast.AST):
    """Every non-compound statement, which is the granularity a single query is
    written at. Using the enclosing function instead would let one scoped query
    vouch for an unscoped one three lines below it."""
    compound = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If,
                ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
                ast.Try, ast.Module)
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and not isinstance(node, compound):
            yield node


def _enclosing_function(tree: ast.AST, target: ast.stmt):
    """The innermost function containing ``target``, as (name, node)."""
    best = (None, None)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(child is target for child in ast.walk(node)):
                # Innermost wins: a nested helper is the real owner of the
                # query, and the module-level function around it may well
                # resolve a scope it never passes down.
                if best[1] is None or any(
                        child is best[1] for child in ast.walk(node)) is False:
                    best = (node.name, node)
    return best


def _sql_expression(stmt: ast.stmt):
    """The expression that builds the SQL string, if this statement builds one.

    Either the first positional argument of a ``.execute(...)`` call, or the
    right-hand side of an assignment. Returning the expression rather than the
    statement is what makes the literal/dynamic distinction below possible.
    """
    for node in ast.walk(stmt):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and node.args):
            return node.args[0]
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
        return stmt.value
    return None


def _derives_a_scope(func: ast.AST | None) -> bool:
    """Whether the function obtains a scope from the shared module.

    Deliberately function-level, and only reached for queries whose predicate is
    assembled from a variable. Statement-level matching cannot follow
    ``where_sql = " AND ".join(conditions)`` three lines up without implementing
    dataflow, and a guard that reported those as violations would be a guard
    everybody learns to ignore. The literal case below — which is the defect
    this section actually found — is checked exactly.
    """
    if func is None:
        return False
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name and name.endswith(SCOPE_FACTORY_SUFFIX):
                return True
    return False


def scan_unscoped_search_term_readers(
        root: Path | str | None = None,
        directories: tuple[str, ...] = SEARCH_TERM_READER_DIRS) -> list[dict]:
    """Every statement that SELECTs from ``search_terms`` without scoping it.

    Returns ``[{"path", "function", "reason", "detail"}, …]``. Writers,
    migrations and schema operations are excluded: this is about what a consumer
    READS, and a DELETE inside a supersession or a migration is neither a
    consumer nor a read.
    """
    base = Path(root) if root else _ROOT
    findings: list[dict] = []
    for directory in directories:
        target = base / directory
        if not target.exists():
            continue
        for path in sorted(target.rglob("*.py")):
            rel = str(path.relative_to(base))
            if rel in SEARCH_TERM_SCOPE_ALLOWLIST:
                continue
            try:
                tree = ast.parse(code_only(path))
            except SyntaxError:
                continue
            for stmt in _innermost_statements(tree):
                try:
                    text = ast.unparse(stmt)
                except Exception:  # noqa: BLE001 — unparse is best-effort
                    continue
                lowered = text.lower()
                if "from search_terms" not in lowered:
                    continue
                # Not a consumer read. A writer's supersession DELETE and a
                # migration's cleanup both name the table and neither answers a
                # question anyone reads a number from.
                if any(w in lowered for w in ("delete from search_terms",
                                              "insert into search_terms",
                                              "update search_terms")):
                    continue

                func_name, func_node = _enclosing_function(tree, stmt)
                if f"{rel}:{func_name}" in SEARCH_TERM_SCOPE_ALLOWLIST:
                    continue

                sql_expr = _sql_expression(stmt)
                dynamic = sql_expr is not None and any(
                    isinstance(n, (ast.Name, ast.Attribute, ast.Call,
                                   ast.FormattedValue))
                    for n in ast.walk(sql_expr))

                if dynamic:
                    # The predicate comes from somewhere; require the function
                    # to have got it from the shared scope module.
                    if _derives_a_scope(func_node):
                        continue
                    why = ("builds its predicate from a variable, and its "
                           "enclosing function never obtains a scope from "
                           "analysis.search_term_scope")
                else:
                    # A fully literal query: the whole predicate is visible
                    # right here, so it can be judged exactly. This is the shape
                    # the F3 review found in the operational scripts — bounded
                    # on `source_date` and nothing else.
                    if "customer_id" in lowered or "{scope}" in lowered:
                        continue
                    why = ("is a fully literal query whose WHERE clause never "
                           "mentions `customer_id`")

                findings.append({
                    "path": rel,
                    "function": func_name,
                    "reason": REASON_UNSCOPED_SEARCH_TERM_READ,
                    "detail": (
                        f"{rel}"
                        + (f" ({func_name})" if func_name else "")
                        + f" reads `search_terms` unscoped: it {why}. During the "
                          "cutover a query like this counted every observation "
                          "twice. Compose the predicate from "
                          "analysis.search_term_scope, or allowlist it in "
                          "SEARCH_TERM_SCOPE_ALLOWLIST with the reason it is a "
                          "historical diagnostic."),
                })
    return sorted(findings, key=lambda f: (f["path"], f["function"] or ""))


__all__ = [
    "PRODUCTION_DIRS", "LEGACY_ACCESS_ALLOWLIST",
    "SEARCH_TERM_READER_DIRS", "SEARCH_TERM_SCOPE_ALLOWLIST", "SCOPE_FACTORY_SUFFIX",
    "REASON_UNSCOPED_SEARCH_TERM_READ", "scan_unscoped_search_term_readers",
    "RETIRED_PROVIDER_NAMES", "LEGACY_REPOSITORY_HELPERS",
    "LEGACY_SNAPSHOT_FILES", "KEYWORD_FALLBACK_MARKERS",
    "REASON_RETIRED_PROVIDER_IMPORT", "REASON_LEGACY_SNAPSHOT_ACCESS",
    "REASON_LEGACY_SNAPSHOT_WRITE", "REASON_LEGACY_REPOSITORY_CALL",
    "REASON_LEGACY_SNAPSHOT_FILE", "REASON_KEYWORD_FALLBACK",
    "code_only", "scan_legacy_sources",
]
