"""
tests/test_dataset_freshness_endpoint.py

PR-ADS-067 — Tests for canonical enrichment behavior used by /api/datasets/freshness.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from unittest.mock import MagicMock, patch

from services.freshness_service import CanonicalFreshnessStatus


def _make_mock_conn(
    *,
    sync_rows: list[tuple] | None = None,
    batch_rows: list[tuple] | None = None,
    count_by_table: dict[str, int] | None = None,
    failing_tables: set[str] | None = None,
):
    sync_rows = sync_rows or []
    batch_rows = batch_rows or []
    count_by_table = count_by_table or {}
    failing_tables = failing_tables or set()
    state: dict[str, object] = {"mode": None, "fetchone": None}
    executed: list[dict[str, object]] = []

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    def execute(sql, params=None):
        sql_text = " ".join(str(sql).split())
        sql_lc = sql_text.lower()
        executed.append({"sql": sql_text, "params": params, "kind": "other"})
        if "from sync_state" in sql_lc:
            executed[-1]["kind"] = "sync_state"
            cur.description = [
                ("source",), ("dataset",), ("status",), ("last_successful_sync_at",),
                ("last_source_date",), ("last_batch_id",), ("error_message",), ("updated_at",),
            ]
            state["mode"] = "sync_state"
            state["fetchone"] = None
            return
        if "from sync_batches" in sql_lc:
            executed[-1]["kind"] = "sync_batches"
            cur.description = [("source",), ("dataset",), ("status",), ("row_count",)]
            state["mode"] = "sync_batches"
            state["fetchone"] = None
            return
        table = None
        if sql_lc.startswith("select count(*) from "):
            table = sql_lc.split("from ", 1)[1].split(" ", 1)[0]
        elif "sql('select count(*) from ')" in sql_lc and "identifier('" in sql_lc:
            match = re.search(r"Identifier\('([^']+)'\)", sql_text)
            table = match.group(1).lower() if match else None
        if table:
            executed[-1]["kind"] = "count"
            executed[-1]["table"] = table
            if table in failing_tables:
                raise RuntimeError(f"simulated count failure: {table}")
            cur.description = [("count",)]
            state["mode"] = "count"
            state["fetchone"] = (count_by_table.get(table, 0),)
            return
        raise AssertionError(f"Unexpected SQL: {sql_text}")

    def fetchall():
        if state["mode"] == "sync_state":
            return sync_rows
        if state["mode"] == "sync_batches":
            return batch_rows
        return []

    def fetchone():
        return state["fetchone"]

    cur.execute.side_effect = execute
    cur.fetchall.side_effect = fetchall
    cur.fetchone.side_effect = fetchone

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    return conn, executed


def _call_endpoint(*, conn, days: int):
    mock_db_conn = MagicMock()
    mock_db_conn.get_conn = MagicMock(return_value=conn)
    with patch.dict("sys.modules", {"db.connection": mock_db_conn}):
        from api.server import api_datasets_freshness

        return api_datasets_freshness(user={"username": "test"}, days=days)


def test_placeholder_unknown_sync_maps_to_not_run():
    conn, _ = _make_mock_conn()
    payload = _call_endpoint(conn=conn, days=60)
    campaigns = next(d for d in payload["datasets"] if d["dataset_key"] == "google_ads_api/campaigns")
    assert campaigns["status"] == "unknown"
    assert campaigns["canonical_status"] == CanonicalFreshnessStatus.NOT_RUN


def test_days_one_uses_exact_one_day_window_for_counts():
    now = datetime.now(timezone.utc)
    conn, executed = _make_mock_conn(
        sync_rows=[("google_ads_api", "campaigns", "success", now, date.today(), 11, None, now)],
        count_by_table={"campaigns": 7},
    )
    payload = _call_endpoint(conn=conn, days=1)
    campaigns = next(d for d in payload["datasets"] if d["dataset_key"] == "google_ads_api/campaigns")
    assert campaigns["rows_in_window"] == 7
    campaign_count_calls = [c for c in executed if c.get("kind") == "count" and c.get("table") == "campaigns"]
    assert campaign_count_calls, "Expected count query for campaigns table"
    assert campaign_count_calls[0]["params"] == (date.today() - timedelta(days=1),)


def test_row_count_query_failure_is_unknown_not_zero():
    now = datetime.now(timezone.utc)
    conn, _ = _make_mock_conn(
        sync_rows=[("gclid", "matches", "success", now, date.today(), 9, None, now)],
        count_by_table={"campaigns": 10, "search_terms": 10, "keywords": 10, "geo": 10, "leads": 10, "deals": 10},
        failing_tables={"gclid_attribution"},
    )
    payload = _call_endpoint(conn=conn, days=30)
    matches = next(d for d in payload["datasets"] if d["dataset_key"] == "gclid/matches")
    assert matches["rows_in_window"] is None
    # PR-ADS-095: refined from UNKNOWN to UNKNOWN_ROW_COUNT for clarity
    assert matches["canonical_status"] == CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT


def test_configured_datasets_get_count_coverage_or_unknown():
    now = datetime.now(timezone.utc)
    counts = {
        "campaigns": 5,
        "search_terms": 6,
        "waste_terms": 7,
        "keywords": 8,
        "geo": 9,
        "leads": 10,
        "deals": 11,
        "gclid_attribution": 12,
        "gclid_coverage_snapshots": 13,
        "historical_intelligence": 14,
    }
    conn, _ = _make_mock_conn(
        sync_rows=[
            ("analysis", "waste_terms", "success", now, date.today(), 1, None, now),
            ("analysis", "historical_intelligence", "success", now, date.today(), 2, None, now),
            ("gclid", "matches", "success", now, date.today(), 3, None, now),
            ("gclid", "coverage_snapshots", "success", now, date.today(), 4, None, now),
            ("google_ads_api", "search_terms", "success", now, date.today(), 5, None, now),
        ],
        count_by_table=counts,
    )
    payload = _call_endpoint(conn=conn, days=60)
    data_by_key = {d["dataset_key"]: d for d in payload["datasets"]}
    assert data_by_key["analysis/waste_terms"]["rows_in_window"] == 7
    assert data_by_key["analysis/historical_intelligence"]["rows_in_window"] == 14
    assert data_by_key["gclid/matches"]["rows_in_window"] == 12
    assert data_by_key["gclid/coverage_snapshots"]["rows_in_window"] == 13
    for row in payload["datasets"]:
        assert row["rows_in_window"] is not None or row["canonical_status"] == CanonicalFreshnessStatus.UNKNOWN


def test_dependency_blocked_still_applies_to_waste_terms_and_ngrams():
    """PR-ADS-095: emits BLOCKED_BY_DEPENDENCY (refined) instead of legacy
    DEPENDENCY_BLOCKED when upstream is actively empty/broken."""
    now = datetime.now(timezone.utc)
    conn, _ = _make_mock_conn(
        sync_rows=[
            ("google_ads_api", "search_terms", "success", now, date.today(), 1, None, now),
            ("analysis", "waste_terms", "success", now, date.today(), 2, None, now),
            ("computed", "ngrams", "success", now, date.today(), 3, None, now),
        ],
        count_by_table={"search_terms": 0, "waste_terms": 3},
    )
    payload = _call_endpoint(conn=conn, days=60)
    waste = next(d for d in payload["datasets"] if d["dataset_key"] == "analysis/waste_terms")
    ngrams = next(d for d in payload["datasets"] if d["dataset_key"] == "computed/ngrams")
    assert waste["canonical_status"] == CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY
    assert ngrams["canonical_status"] == CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY


def test_freshness_endpoint_aligns_with_war_room_derivable_state():
    """PR-ADS-095: /api/datasets/freshness uses the same HAS_DATA_STATES
    upstream handling as the War Room — derived datasets without sync state
    but with fresh upstream classify as NOT_RUN_BUT_DERIVABLE.
    """
    now = datetime.now(timezone.utc)
    conn, _ = _make_mock_conn(
        # Search Terms is fresh with data, but waste_terms/ngrams have no
        # sync_state rows at all (placeholder rows). Counts default to 0 for
        # tables we don't override.
        sync_rows=[
            ("google_ads_api", "search_terms", "success", now, date.today(), 1, None, now),
        ],
        count_by_table={
            "campaigns": 0,
            "search_terms": 1200,
            "keywords": 0,
            "geo": 0,
            "leads": 0,
            "deals": 0,
            "gclid_attribution": 0,
            "gclid_coverage_snapshots": 0,
            "waste_terms": 0,
            "historical_intelligence": 0,
        },
    )
    payload = _call_endpoint(conn=conn, days=60)
    waste = next(d for d in payload["datasets"] if d["dataset_key"] == "analysis/waste_terms")
    ngrams = next(d for d in payload["datasets"] if d["dataset_key"] == "computed/ngrams")
    assert waste["canonical_status"] == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE
    assert ngrams["canonical_status"] == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE


def test_system_health_summary_renderer_uses_canonical_summary():
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
    assert "data.canonical_summary" in app_js
    assert "getCanonicalCount(\"fresh_with_data\")" in app_js
    assert "getCanonicalCount(\"dependency_blocked\")" in app_js
