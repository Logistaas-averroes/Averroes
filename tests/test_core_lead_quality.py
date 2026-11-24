"""
tests/test_core_lead_quality.py

PR-ADS-067 — Regression tests for the HubSpot MQL classification constants
`analysis/core.py` used to export.

PR-ADS-162H deleted four of the five lists (IN_PROGRESS, UNKNOWN, JUNK,
WRONG_FIT) with the weekly / monthly report that used them. `QUALIFIED`
survived because `services/revenue_attribution_service.py` imported it for its
local-JSON fallback's SQL count.

PR-ADS-177A. That fallback no longer counts SQLs: the one SQL count on every
page is the acquisition cohort, and with the database down it is unavailable,
never replaced by a JSON contact pull. `QUALIFIED` had no other importer, so it
is deleted with its last reader, and the three cases that pinned its contents
(`test_closed_sales_qualified_is_qualified`,
`test_closed_deal_created_is_qualified`,
`test_qualified_holds_exactly_the_two_sales_qualified_statuses`) go with it —
they described a constant no production code reads. Its removal, and that no
module imports it, are asserted rather than assumed.

The live status taxonomy — which statuses are qualified, junk, wrong-fit, in
progress or unknown, including the one-R `DICARDED` spelling — is
`analysis/mql_status_taxonomy.py` and `db.writers._map_status_category`, with
their own suites.

Run with:
  python -m pytest tests/test_core_lead_quality.py -v
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import analysis.core as core


def test_the_report_only_classification_lists_are_gone() -> None:
    """They had one user, `run_lead_quality`, which is deleted. Left behind they
    would be a second, unread copy of the status taxonomy. PR-ADS-177A adds
    `QUALIFIED`, whose last reader (the JSON fallback's SQL count) is deleted."""
    for name in ("IN_PROGRESS", "UNKNOWN", "JUNK", "WRONG_FIT", "QUALIFIED",
                 "run_lead_quality", "run_campaign_truth", "determine_verdict"):
        assert not hasattr(core, name), name


def test_no_production_module_imports_the_retired_qualified_list() -> None:
    """PR-ADS-177A: replaces `test_the_live_importer_of_qualified_still_resolves`
    — the importer it protected is gone, and a new one would be a fourth SQL
    definition."""
    offenders = []
    for folder in ("analysis", "api", "connectors", "db", "scheduler", "scripts", "services"):
        for path in (ROOT / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.ImportFrom) and node.module == "analysis.core"
                        and any(a.name == "QUALIFIED" for a in node.names)):
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
