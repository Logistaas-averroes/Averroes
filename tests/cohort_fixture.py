"""PR-ADS-177A — feed the acquisition cohort in mock-based tests.

The Revenue Decision Mart no longer reads ``db.revenue_repository.
fetch_lead_quality`` (the legacy ``leads.status_category`` population). Its
leads and SQLs come from the acquisition cohort: the canonical contact funnel
read by ``db.crm_funnel_repository.fetch_acquisition_cohort_contacts`` and its
freshness verdict from ``analysis.sql_coverage_freshness.assess`` over
``fetch_contact_funnel_sync_state``.

Tests that drove the mart's leads / SQLs through a ``fetch_lead_quality`` mock
now drive the same CONTACTS through these two reads instead. Nothing here
decides a verdict: the freshness verdict is the REAL ``assess`` over a sync-state
row (healthy unless the caller passes another), and cohort membership is the
real window rule — the mock returns only contacts created inside the
``[start_at, end_before)`` instants the service asks for, exactly as the SQL does.

``contacts_from_lead_rows`` maps a legacy fixture row onto the contact it
described: a Google Ads (Paid Search) contact, its campaign label, its country,
and — for ``status_category == 'qualified'`` — a lifecycle stage that proves
SQL. No identifier is relabelled: each row becomes one contact with its own id.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

_ids = itertools.count(1)


def contacts_from_lead_rows(lead_rows, *, created: datetime) -> list[dict]:
    """One canonical-funnel contact per legacy lead fixture row."""
    out = []
    for r in lead_rows or []:
        n = next(_ids)
        qualified = r.get("status_category") == "qualified"
        out.append({
            "funnel_row_id": n,
            "contact_id": str(r.get("contact_id") or f"fixture-contact-{n}"),
            "created_at": r.get("created_at") or created,
            "lifecycle_stage": "salesqualifiedlead" if qualified else "lead",
            "sql_entered_direct": None, "sql_entered_recovered": None,
            "hs_analytics_source": "PAID_SEARCH",
            "hs_analytics_source_data_1": r.get("campaign_name"),
            "gclid": None,
            "ip_country": r.get("country"), "country": None,
        })
    return out


def healthy_sync_row(as_of: datetime) -> dict:
    """A contact-funnel sync-state row the real ``assess`` calls fresh."""
    return {"bootstrap_status": "complete", "last_sync_mode": "incremental",
            "last_incremental_status": "success",
            "last_successful_incremental_at": as_of - timedelta(hours=2),
            "last_incremental_at": as_of - timedelta(hours=2)}


def patch_cohort(monkeypatch, contacts, *, as_of: datetime | None = None,
                 sync_row: dict | None = None, available: bool = True) -> None:
    """Serve ``contacts`` as the canonical contact funnel and ``sync_row`` (a
    healthy one by default, proven ``as_of``) as its sync state."""
    import db.crm_funnel_repository as funnel_repo

    as_of = as_of or datetime.now(timezone.utc)
    rows = list(contacts or [])

    def _contacts(start_at, end_before):
        if not available:
            return {"available": False, "rows": [], "missing_created_at": None}
        inside = [c for c in rows if c.get("created_at") is not None
                  and (start_at is None or c["created_at"] >= start_at)
                  and c["created_at"] < end_before]
        return {"available": True, "rows": inside, "missing_created_at": 0}

    monkeypatch.setattr(funnel_repo, "fetch_acquisition_cohort_contacts", _contacts)
    monkeypatch.setattr(funnel_repo, "fetch_contact_funnel_sync_state",
                        lambda: {"available": True,
                                 "row": sync_row if sync_row is not None
                                 else healthy_sync_row(as_of)})


def withheld_sync_row(as_of: datetime) -> dict:
    """A sync-state row the real ``assess`` withholds: the bootstrap is still
    running (``source_bootstrap_incomplete``)."""
    return {**healthy_sync_row(as_of), "bootstrap_status": "running"}


def cohort_patch(contacts, *, as_of: datetime | None = None, sync_row: dict | None = None,
                 available: bool = True):
    """``patch_cohort`` as a context manager, for tests built on ``unittest.mock.patch``."""
    import contextlib

    import pytest

    @contextlib.contextmanager
    def _cm():
        mp = pytest.MonkeyPatch()
        try:
            patch_cohort(mp, contacts, as_of=as_of, sync_row=sync_row, available=available)
            yield
        finally:
            mp.undo()
    return _cm()


def seed_cohort_contacts(rows, *, created: datetime, label: str = "brand - uk") -> None:
    """PostgreSQL: write the contacts a legacy lead seed described into the
    canonical contact funnel through the PRODUCTION writer, and record a
    healthy (fresh) contact-funnel sync state.

    ``rows``: ``[(contact_id, country, status_category)]`` — the legacy seed
    helpers' shape. Each becomes one Paid Search contact with that campaign
    label, ``ip_country = country``, created at ``created``; ``qualified``
    becomes a lifecycle stage that proves SQL, anything else ``lead``."""
    from db import writers
    now = datetime.now(timezone.utc)
    writers.upsert_hubspot_contact_funnel([
        {"contact_id": str(cid), "created_at": created, "last_modified_at": created,
         "lifecycle_stage": "salesqualifiedlead" if category == "qualified" else "lead",
         "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": label,
         "ip_country": country}
        for cid, country, category in rows])
    writers.update_contact_funnel_sync_state(
        "contacts", bootstrap_status="complete", last_incremental_at=now,
        last_modified_watermark=now, last_status="success",
        last_sync_mode="incremental", last_incremental_status="success",
        last_successful_incremental_at=now, last_error=None)
