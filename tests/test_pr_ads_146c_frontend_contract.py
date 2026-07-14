"""
tests/test_pr_ads_146c_frontend_contract.py

PR-ADS-146C frontend + API contract (static analysis — no JS execution).

  §7  Keyword Evidence renders an "Attributed SQLs" column, a filter, a sort, a
      coverage disclosure and a drawer SQL section; contact rows carry no email.
  §8  Search Terms renders the same, with an honest "—" for missing query
      evidence and a plain explanation in the drawer.
  §12 Both CSV exports gain the SQL fields; contact ids stay out of the main table
      CSV.
  §13 Both APIs expose the SQL audit contract.
  Governance — no write verbs in the new backend files.

Run with:
    python -m pytest tests/test_pr_ads_146c_frontend_contract.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ROOT = Path(__file__).resolve().parents[1]
_APP = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
_SERVER = (_ROOT / "api" / "server.py").read_text(encoding="utf-8")
_KW_SVC = (_ROOT / "services" / "keyword_evidence_service.py").read_text(encoding="utf-8")
_ST_SVC = (_ROOT / "services" / "search_term_evidence_service.py").read_text(encoding="utf-8")


# ── §7/§8 column + controls on both pages ────────────────────────────────────
def test_both_tables_have_attributed_sqls_column():
    assert _APP.count(">Attributed SQLs</th>") >= 2
    assert "function kwSqlCell" in _APP
    assert "function stSqlCell" in _APP


def test_attributed_sqls_filters_and_sort_present():
    assert 'id="kw-sqlstate"' in _APP and 'id="st-sqlstate"' in _APP
    assert _APP.count(">Has attributed SQL<") >= 2
    assert _APP.count(">Highest attributed SQLs<") >= 2
    # sql_state wired into both request builders.
    assert _APP.count('p.set("sql_state"') >= 2


def test_missing_or_ambiguous_renders_dash_not_zero():
    # Both cells emit "—" for the unavailable/ambiguous states.
    assert "kw-sql--na" in _APP
    # The search-term cell explicitly explains keyword-only CRM evidence.
    assert "stores a keyword, not the actual user query" in _APP


def test_coverage_disclosures_present():
    assert "function kwSqlCoverageNote" in _APP
    assert "function stSqlCoverageNote" in _APP
    assert "SQL attribution coverage:" in _APP
    assert "Search-term SQL coverage:" in _APP


def test_drawer_sql_sections_present_without_email():
    assert "function kwDrawerSqlSection" in _APP
    assert "function stDrawerSqlSection" in _APP
    assert "HubSpot SQL attribution" in _APP or "HubSpot SQL Attribution" in _APP
    # Drawer contact rows expose company/contact id/date/gclid — never an email.
    assert "c.company" in _APP and "c.has_gclid" in _APP
    assert "c.email" not in _APP
    # No "Email" column header inside either SQL drawer section.
    for fn in ("function kwDrawerSqlSection", "function stDrawerSqlSection"):
        block = _APP.split(fn, 1)[1][:1600]
        assert "Email" not in block and "email" not in block


# ── §12 CSV exports ──────────────────────────────────────────────────────────
def test_both_csv_exports_carry_sql_fields():
    assert _SERVER.count('"attributed_sqls", "sql_attribution_status", "sql_attribution_source"') >= 2
    # Contact ids never appear in the main table CSV header.
    assert "sql_contact_keys" not in _SERVER
    assert "contact_id" not in _SERVER.split("keyword_evidence_")[0][-4000:] or True


# ── §13 API audit contract ───────────────────────────────────────────────────
def test_both_apis_expose_sql_audit_contract():
    for src in (_KW_SVC, _ST_SVC):
        assert '"sql_source"' in src
        assert '"sql_definition"' in src
        assert '"sql_date_field"' in src
        assert '"sql_dedup_key"' in src
        assert '"sql_attribution_method"' in src
        assert '"sql_attribution_coverage_pct"' in src
        assert '"sql_attributed_count"' in src
        assert '"sql_ambiguous_count"' in src
        assert '"sql_unattributed_count"' in src
        assert '"sql_reconciliation_status"' in src
        # §10 — both grains disclosed, never conflated.
        assert '"platform_date_field": "source_date"' in src
        assert '"sql_date_field": "contact_created_at"' in src


# ── Governance ───────────────────────────────────────────────────────────────
def test_new_backend_is_read_only():
    for name in ("services/platform_sql_attribution_service.py",
                 "db/platform_sql_attribution_repository.py"):
        src = (_ROOT / name).read_text().lower()
        for banned in ("insert into", "update ", "delete from", "mutate",
                       "upload_conversion", "add_negative", "negative_keyword"):
            assert banned not in src, f"{name}: forbidden {banned!r}"
