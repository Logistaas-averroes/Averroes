#!/usr/bin/env python3
"""
scripts/import_windsor_mcp_search_terms.py

Import a previously exported Windsor MCP get_data payload into search_terms.

Use case: Windsor REST endpoint returns 0 rows for search terms, but MCP
(Model Context Protocol) previously returned ~45,292 rows. This script
provides a safe operator import path for MCP payloads.

Usage:
    # Dry-run (default, does NOT write):
    python scripts/import_windsor_mcp_search_terms.py \\
      --input data/windsor_mcp_search_terms_raw.json \\
      --dry-run

    # Apply (writes to DB):
    python scripts/import_windsor_mcp_search_terms.py \\
      --input data/windsor_mcp_search_terms_raw.json \\
      --apply

Rules:
  - Dry-run by default.
  - Validate exact field `search_term`.
  - Reject rows missing search_term.
  - Normalize ad_group_id from resource path if present.
  - Use existing write_search_terms().
  - Create sync_batch with source="windsor_mcp" dataset="search_terms".
  - Never call Google Ads.
  - Never call HubSpot.
  - Never push negatives.

PR-ADS-066 — Search Terms Production Verdict Panel & Windsor Source-Parity Resolution
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)


def _extract_ad_group_id(resource_path: str | None) -> str | None:
    """Extract ad_group_id from a Google Ads resource path string.

    Example: 'customers/1234567890/adGroups/111222333' -> '111222333'
    """
    if not resource_path:
        return None
    match = re.search(r"adGroups/(\d+)", resource_path)
    return match.group(1) if match else resource_path


def _parse_mcp_payload(raw_data: Any) -> list[dict[str, Any]]:
    """Parse MCP payload which may be double-encoded JSON.

    MCP get_data sometimes returns data as a JSON string inside a JSON string,
    or as a list of text blocks containing JSON.
    """
    # If it's a string, try to parse it as JSON
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            return []

    # If it's a list of dicts, return directly
    if isinstance(raw_data, list):
        # Check if items are dicts (normal case)
        if raw_data and isinstance(raw_data[0], dict):
            return raw_data
        # Check if items are strings (text blocks from MCP)
        if raw_data and isinstance(raw_data[0], str):
            combined = []
            for item in raw_data:
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, list):
                        combined.extend(parsed)
                    elif isinstance(parsed, dict):
                        combined.append(parsed)
                except (json.JSONDecodeError, TypeError):
                    continue
            return combined

    # If it's a dict with a 'data' or 'rows' key
    if isinstance(raw_data, dict):
        for key in ("data", "rows", "results", "content"):
            if key in raw_data:
                return _parse_mcp_payload(raw_data[key])

    return []


def validate_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate MCP rows. Returns (valid_rows, errors)."""
    valid = []
    errors = []

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"Row {i}: not a dict")
            continue

        search_term = row.get("search_term") or row.get("searchTerm") or row.get("search term")
        if not search_term or not str(search_term).strip():
            errors.append(f"Row {i}: missing or blank search_term")
            continue

        # Normalize
        normalized: dict[str, Any] = {
            "search_term": str(search_term).strip(),
            "campaign_name": row.get("campaign_name") or row.get("campaign") or row.get("campaignName") or "",
            "ad_group": row.get("ad_group") or row.get("adGroup") or row.get("ad_group_name") or "",
            "keyword": row.get("keyword") or row.get("keyword_text") or "",
            "match_type": row.get("match_type") or row.get("matchType") or "",
            "source_date": row.get("source_date") or row.get("date") or str(date.today()),
            "impressions": _safe_int(row.get("impressions", 0)),
            "clicks": _safe_int(row.get("clicks", 0)),
            "spend_usd": _safe_float(row.get("spend_usd") or row.get("spend") or row.get("cost", 0)),
            "conversions": _safe_float(row.get("conversions", 0)),
        }

        # Normalize ad_group_id from resource path
        ad_group_id = row.get("ad_group_id") or row.get("adGroupId") or row.get("ad_group_resource_name")
        if ad_group_id:
            normalized["ad_group_id"] = _extract_ad_group_id(str(ad_group_id))

        valid.append(normalized)

    return valid, errors


def _safe_int(val: Any) -> int:
    try:
        return int(val) if val is not None else 0
    except (ValueError, TypeError):
        return 0


def _safe_float(val: Any) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


def run_import(
    input_path: str,
    apply: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run the MCP import. Returns summary dict."""
    path = Path(input_path)
    if not path.exists():
        return {"error": f"Input file not found: {input_path}", "applied": False}

    with open(path) as f:
        raw_data = json.load(f)

    rows = _parse_mcp_payload(raw_data)
    if not rows:
        return {
            "error": "No parseable rows found in input file",
            "applied": False,
            "raw_type": type(raw_data).__name__,
        }

    valid_rows, validation_errors = validate_rows(rows)

    summary = {
        "input_file": str(path),
        "total_raw_rows": len(rows),
        "valid_rows": len(valid_rows),
        "rejected_rows": len(validation_errors),
        "validation_errors_sample": validation_errors[:10],
        "dry_run": not apply,
        "applied": False,
        "db_written": 0,
    }

    if verbose:
        summary["sample_valid_row"] = valid_rows[0] if valid_rows else None

    if not apply:
        summary["message"] = "Dry-run complete. Use --apply to write to database."
        return summary

    # Apply — write to DB
    try:
        from db.connection import get_conn
        from db.writers import start_sync_batch, finish_sync_batch, write_search_terms

        with get_conn() as conn:
            if conn is None:
                summary["error"] = "Database unavailable"
                return summary

            batch_id = start_sync_batch(
                conn=conn,
                source="windsor_mcp",
                dataset="search_terms",
                row_count=len(valid_rows),
            )

            written = write_search_terms(conn, valid_rows)
            summary["db_written"] = written
            summary["applied"] = True

            if batch_id and batch_id > 0:
                status = "success" if written > 0 else "failed"
                finish_sync_batch(conn, batch_id, status=status, row_count=written)

    except Exception as exc:
        summary["error"] = str(exc)
        summary["applied"] = False

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Import Windsor MCP search terms payload into DB"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to MCP JSON payload file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Validate only, do not write (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write to database",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show sample rows",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    apply = args.apply  # --apply overrides --dry-run
    result = run_import(
        input_path=args.input,
        apply=apply,
        verbose=args.verbose,
    )

    print(json.dumps(result, indent=2, default=str))

    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
