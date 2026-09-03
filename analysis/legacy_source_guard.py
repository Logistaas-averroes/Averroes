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
PRODUCTION_DIRS = ("scheduler", "services")

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
}

#: Machine-readable reasons, so a caller can group findings without parsing prose.
REASON_RETIRED_PROVIDER_IMPORT = "retired_provider_import"
REASON_LEGACY_SNAPSHOT_ACCESS = "legacy_snapshot_access"
REASON_LEGACY_SNAPSHOT_WRITE = "legacy_snapshot_write"


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


def scan_legacy_sources(root: Path | str | None = None,
                        directories: tuple[str, ...] = PRODUCTION_DIRS) -> list[dict]:
    """Every unauthorised legacy read in the production directories.

    Returns ``[{"path", "reason", "detail"}, …]``, sorted by path, and an empty
    list when nothing is found — which is a real all-clear here, because unlike
    a database read this scan cannot be refused: the files are either on disk
    and parsed, or the repository is not there at all.
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
            if "windsor_pull" in lowered or "import windsor" in lowered:
                findings.append({
                    "path": rel, "reason": REASON_RETIRED_PROVIDER_IMPORT,
                    "detail": f"{rel} imports the retired Windsor provider"})
            if "from keywords" in lowered or "into keywords" in lowered:
                findings.append({
                    "path": rel, "reason": REASON_LEGACY_SNAPSHOT_ACCESS,
                    "detail": f"{rel} reads or writes the legacy `keywords` snapshot"})
            if "write_keywords(" in code:
                findings.append({
                    "path": rel, "reason": REASON_LEGACY_SNAPSHOT_WRITE,
                    "detail": f"{rel} writes the legacy `keywords` snapshot"})
    return sorted(findings, key=lambda f: (f["path"], f["reason"]))


__all__ = [
    "PRODUCTION_DIRS", "LEGACY_ACCESS_ALLOWLIST",
    "REASON_RETIRED_PROVIDER_IMPORT", "REASON_LEGACY_SNAPSHOT_ACCESS",
    "REASON_LEGACY_SNAPSHOT_WRITE",
    "code_only", "scan_legacy_sources",
]
