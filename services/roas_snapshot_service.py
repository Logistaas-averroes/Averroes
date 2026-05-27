"""
ROAS Snapshot Service (PR-ADS-080C)
Daily persistence layer for ROAS snapshots.

Responsibility:
  - Generate daily ROAS snapshots using existing 080A calculation functions.
  - Persist snapshots to runtime JSON files under data/roas_snapshots/.
  - Load latest or historical snapshots for API consumption.

What is NOT in this file:
  - No ROAS math — uses analysis/roas_calculator.py.
  - No external API calls (no HubSpot, no Windsor, no Google Ads).
  - No Google Ads writes.
  - No HubSpot writes.
  - No OCT.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from analysis.roas_calculator import (
    compute_all_campaign_roas,
    compute_all_country_roas,
)
from services.unit_economics_service import compute_unit_economics_summary

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
SNAPSHOT_DIR = DATA_DIR / "roas_snapshots"
VALID_WINDOWS = {"7d": 7, "14d": 14, "30d": 30, "60d": 60, "90d": 90, "365d": 365}


def _parse_snapshot_window(window: str) -> int:
    """Parse and validate ROAS window using strict allowlist."""
    if window in VALID_WINDOWS:
        return VALID_WINDOWS[window]
    raise ValueError("Invalid window. Valid values: 7d, 14d, 30d, 60d, 90d, 365d")


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically write JSON by replacing a temp file in the same directory."""
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(payload, tmp_file, indent=2, default=str)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def generate_roas_snapshot(window: str = "60d") -> dict:
    """Generate a full ROAS snapshot from existing 080A calculation functions.

    Args:
        window: Time window string (e.g., '60d'). Passed through to calculators.

    Returns:
        Complete snapshot dict ready for persistence.
    """
    window_days = _parse_snapshot_window(window)

    logger.info("Generating ROAS snapshot for window=%s", window)

    # Compute campaign and country ROAS using 080A functions
    campaigns = compute_all_campaign_roas(window_days=window_days)
    countries = compute_all_country_roas(window_days=window_days)

    # Compute overall unit economics using shared helper.
    unit_economics = compute_unit_economics_summary(campaigns)
    total_spend = unit_economics["total_spend"]
    total_deals = unit_economics["total_deals_won"]
    total_acv = unit_economics["total_acv_revenue"]
    total_ltv = unit_economics["total_ltv_revenue"]

    # Count verdicts
    verdict_counts = {"SCALE": 0, "HOLD": 0, "FIX": 0, "CUT": 0, "INSUFFICIENT_DATA": 0}
    for c in campaigns:
        v = c.get("verdict", "INSUFFICIENT_DATA")
        if v in verdict_counts:
            verdict_counts[v] += 1

    # Collect warnings
    warnings = []
    if total_spend == 0:
        warnings.append("No spend data available across all campaigns")
    if total_deals == 0:
        warnings.append("No won deals found in window")
    if not campaigns:
        warnings.append("No campaign data available")
    if not countries:
        warnings.append("No country data available")

    now = datetime.now(timezone.utc)
    snapshot = {
        "snapshot_date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "window": window,
        "source_truth": "hubspot_won_deals_plus_windsor_spend",
        "google_ads_conversion_value_used": False,
        "campaigns": campaigns,
        "countries": countries,
        "unit_economics": unit_economics,
        "summary": {
            "campaign_count": len(campaigns),
            "country_count": len(countries),
            "scale_count": verdict_counts["SCALE"],
            "hold_count": verdict_counts["HOLD"],
            "fix_count": verdict_counts["FIX"],
            "cut_count": verdict_counts["CUT"],
            "insufficient_data_count": verdict_counts["INSUFFICIENT_DATA"],
            "total_spend": round(total_spend, 2),
            "total_acv_revenue": round(total_acv, 2),
            "total_ltv_revenue": round(total_ltv, 2),
        },
        "warnings": warnings,
    }

    logger.info(
        "ROAS snapshot generated: %d campaigns, %d countries, verdict=%s",
        len(campaigns),
        len(countries),
        unit_economics.get("overall_verdict"),
    )
    return snapshot


def save_roas_snapshot(snapshot: dict) -> dict:
    """Persist a ROAS snapshot to the filesystem.

    Saves to:
      data/roas_snapshots/roas_snapshot_YYYY-MM-DD_<window>.json
      data/roas_snapshots/latest_<window>.json (pointer)

    Args:
        snapshot: Complete snapshot dict from generate_roas_snapshot.

    Returns:
        Dict with paths written and status.
    """
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    snapshot_date = snapshot.get("snapshot_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    window = snapshot.get("window", "60d")

    filename = f"roas_snapshot_{snapshot_date}_{window}.json"
    filepath = SNAPSHOT_DIR / filename
    latest_path = SNAPSHOT_DIR / f"latest_{window}.json"

    try:
        _atomic_write_json(filepath, snapshot)
        _atomic_write_json(latest_path, snapshot)

        logger.info("ROAS snapshot saved: %s", filepath)
        logger.info("Latest pointer updated: %s", latest_path)

        return {
            "status": "success",
            "snapshot_path": str(filepath),
            "latest_path": str(latest_path),
            "snapshot_date": snapshot_date,
            "window": window,
        }
    except Exception as exc:
        logger.error("Failed to save ROAS snapshot: %s", exc)
        return {
            "status": "failed",
            "error": str(exc),
            "snapshot_date": snapshot_date,
            "window": window,
        }


def load_latest_roas_snapshot(window: str = "60d") -> dict | None:
    """Load the latest persisted ROAS snapshot for a given window.

    Args:
        window: Time window (e.g., '60d').

    Returns:
        Snapshot dict if found, None otherwise.
    """
    latest_path = SNAPSHOT_DIR / f"latest_{window}.json"
    if not latest_path.exists():
        logger.info("No latest snapshot found for window=%s", window)
        return None

    try:
        with open(latest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load latest snapshot: %s", exc)
        return None


def load_roas_snapshots(limit: int = 30, window: str | None = None) -> list[dict]:
    """Load historical ROAS snapshots.

    Args:
        limit: Maximum number of snapshots to return.
        window: If provided, filter to snapshots matching this window.

    Returns:
        List of snapshot dicts, most recent first.
    """
    if not SNAPSHOT_DIR.exists():
        return []

    pattern = "roas_snapshot_*.json"
    files = sorted(SNAPSHOT_DIR.glob(pattern), reverse=True)

    # Exclude latest pointer files
    files = [f for f in files if not f.name.startswith("latest_")]

    # Filter by window if specified
    if window:
        files = [f for f in files if f.name.endswith(f"_{window}.json")]

    snapshots = []
    for filepath in files[:limit]:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                snapshots.append(json.load(f))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping corrupt snapshot %s: %s", filepath, exc)
            continue

    return snapshots
