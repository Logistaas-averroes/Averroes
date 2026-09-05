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


__all__ = [
    "PRODUCTION_DIRS", "LEGACY_ACCESS_ALLOWLIST",
    "RETIRED_PROVIDER_NAMES", "LEGACY_REPOSITORY_HELPERS",
    "LEGACY_SNAPSHOT_FILES", "KEYWORD_FALLBACK_MARKERS",
    "REASON_RETIRED_PROVIDER_IMPORT", "REASON_LEGACY_SNAPSHOT_ACCESS",
    "REASON_LEGACY_SNAPSHOT_WRITE", "REASON_LEGACY_REPOSITORY_CALL",
    "REASON_LEGACY_SNAPSHOT_FILE", "REASON_KEYWORD_FALLBACK",
    "code_only", "scan_legacy_sources",
]
