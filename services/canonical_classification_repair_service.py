"""
services/canonical_classification_repair_service.py

PR-ADS-152 §6 — audit and repair ``contact_source_classification`` against the
canonical latest ``leads`` rows.

The Revenue by Source page reads the durable classification cache. When that cache
drifts from the canonical latest contact snapshot (a contact re-qualified, its
business date changed, or its source group was re-derived) the page silently
counts stale qualified contacts. This service reconciles the cache and repairs it
with an IDEMPOTENT LOCAL UPSERT only.

Required checks (all derived from the SAME canonical rows the pages score):
  - one row per canonical contact_key;
  - same latest status_category;
  - same contact_created_at;
  - current acquisition classification (re-derived from the contact's own source);
  - excluded contacts are flagged (they are not scored by the canonical scopes);
  - stale records identified and repaired locally;
  - missing classifications backfilled.

Governance (never violated):
  - Read-only with respect to HubSpot and Google Ads — no external reads/writes,
    no offline-conversion uploads.
  - The original HubSpot / leads evidence is NEVER deleted or overwritten; the
    only write is an idempotent upsert into the local
    ``contact_source_classification`` cache (keyed by contact_key).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from analysis.account_time import account_today
from analysis.source_classification import RULE_VERSION
from services.canonical_contact_outcome_service import (
    deduplicate_latest, derive_acquisition_group, group_safely_rederivable,
    _classification_state,
)

logger = logging.getLogger(__name__)


def _canonical_classification_row(row: dict, stored: dict | None = None) -> dict:
    """The classification row the canonical latest lead SHOULD have. Pure.

    PR-ADS-152 §4 — preserves the HubSpot source taxonomy truth:
      - ``source_detail_raw`` is NEVER the campaign_name. The durable ``leads``
        table persists no genuine Original-Source drill-down, so a backfill stores
        it as NULL and a repair PRESERVES the existing drill-down.
      - ``source_primary_raw`` is preserved on an existing row (only re-derived on
        a genuine backfill from the durable HubSpot source).
      - the acquisition group is only re-derived when it is safe to do so from the
        primary source alone; otherwise the stored group is kept so Offline
        sub-classifications (Events, referral, SalesNash, …) are never damaged.
    """
    primary = row.get("hs_analytics_source")
    if stored is not None:
        # Existing (stale) row: keep the raw taxonomy, update only outcome/date and
        # a SAFELY re-derivable group.
        source_primary_raw = stored.get("source_primary_raw")
        source_detail_raw = stored.get("source_detail_raw")
        if group_safely_rederivable(primary):
            group = derive_acquisition_group(row)
        else:
            group = stored.get("acquisition_group")
    else:
        # Missing row → backfill from genuine durable HubSpot fields; drill-down
        # detail is unavailable on leads, so store NULL (never invented).
        source_primary_raw = primary
        source_detail_raw = None
        group = derive_acquisition_group(row)
    return {
        "contact_key": row.get("contact_key"),
        "contact_id": row.get("contact_id"),
        "source_primary_raw": source_primary_raw,
        "source_detail_raw": source_detail_raw,
        "acquisition_group": group,
        "classification_rule_version": RULE_VERSION,
        "contact_created_at": row.get("contact_created_at"),
        "status_category": row.get("status_category"),
    }


def plan_repairs(lead_rows: list[dict], classification: list[dict],
                 exclusions: set | None = None) -> dict:
    """Compare the canonical latest leads against the stored classification and
    plan idempotent upserts. Pure — no I/O.

    Returns ``{report, upserts}`` where ``upserts`` is the list of classification
    rows to write (only for stale/missing keys) and ``report`` summarises the
    reconciliation.
    """
    exclusions = exclusions or set()
    stored_by_key = {c.get("contact_key"): c for c in (classification or [])
                     if c.get("contact_key")}

    deduped = deduplicate_latest(lead_rows)

    report = {
        "canonical_contacts": len(deduped),
        "classification_rows": len(stored_by_key),
        "checked": 0,
        "ok": 0,
        "stale_repaired": 0,
        "missing_backfilled": 0,
        "excluded_flagged": 0,
        "orphan_classifications": 0,
    }
    upserts: list[dict] = []
    seen_keys: set = set()

    for row in deduped:
        key = row.get("contact_key")
        if not key:
            continue
        seen_keys.add(key)
        report["checked"] += 1
        if key in exclusions:
            report["excluded_flagged"] += 1
        stored = stored_by_key.get(key)
        state = _classification_state(row, stored)
        if state == "ok":
            report["ok"] += 1
            continue
        if state == "missing":
            report["missing_backfilled"] += 1
        else:  # stale
            report["stale_repaired"] += 1
        upserts.append(_canonical_classification_row(row, stored))

    # Classifications that no longer have any canonical latest lead in scope are
    # reported (never deleted — original evidence is preserved).
    report["orphan_classifications"] = sum(
        1 for k in stored_by_key if k not in seen_keys)

    return {"report": report, "upserts": upserts}


def run_repair(start=None, end=None, *, dry_run: bool = True,
               now: datetime | None = None) -> dict:
    """Fetch the canonical inputs and repair the classification cache.

    ``dry_run`` (default) plans the repair without writing. Read-only with respect
    to external systems in every mode; the only write (when ``dry_run`` is False)
    is the local idempotent upsert.

    PR-ADS-152 §3 — the default range is an EXPLICIT all-time window: ``start=None``
    (no lower bound) and ``end=today`` in the Google Ads account reporting timezone,
    so the dry run reads every dated contact rather than relying on an implicit /
    open-ended bound.
    """
    from db import canonical_contact_outcome_repository as repo  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    if end is None:
        end = account_today(now)
    inputs = repo.fetch_canonical_inputs(start, end)
    if not inputs.get("available"):
        return {
            "available": False, "dry_run": dry_run,
            "report": {}, "written": 0,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    plan = plan_repairs(
        inputs.get("lead_rows") or [],
        inputs.get("classification") or [],
        inputs.get("exclusions") or set(),
    )
    written = 0
    if not dry_run and plan["upserts"]:
        import db.writers as db_writers  # noqa: PLC0415
        written = db_writers.upsert_contact_source_classification(plan["upserts"])

    return {
        "available": True,
        "dry_run": dry_run,
        "report": plan["report"],
        "planned_upserts": len(plan["upserts"]),
        "written": written,
        "classification_rule_version": RULE_VERSION,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
