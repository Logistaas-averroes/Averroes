"""
tests/canonical_ledger_fixtures.py

Shared fixtures for the PR-ADS-153E-B consumer cutover.

Every migrated production consumer now reads closed-won revenue through
``services.canonical_revenue_service``, which reads
``db.deal_ledger_repository``. Test modules that used to stub
``db.revenue_repository.fetch_revenue_deals`` / ``fetch_won_revenue`` /
``fetch_source_revenue`` stub the canonical ledger here instead — through ONE
helper, so no test can accidentally invent its own idea of what a ready ledger
looks like.

Nothing here touches a database or a network.
"""

from __future__ import annotations

# A sync state that satisfies the 153E-A2 coverage gate in full: the historical
# bootstrap completed, its timestamps are present and ordered, and a successful
# INCREMENTAL ran after it. Deliberately spelled out rather than generated —
# a test that needs an UNREADY ledger should copy this and break one field, so
# the diff shows exactly which condition is under test.
READY_SYNC_STATE = {
    "scope": "deals",
    "bootstrap_status": "complete",
    "bootstrap_started_at": "2026-01-02T00:00:00+00:00",
    "bootstrap_completed_at": "2026-01-02T06:00:00+00:00",
    "last_modified_watermark": "2026-06-22T00:00:00+00:00",
    "last_incremental_at": "2026-06-22T03:00:00+00:00",
    "last_status": "success",
    "last_error": None,
    "last_sync_mode": "incremental",
    "deals_seen": 4,
    "pages_fetched": 1,
    "association_failures": 0,
    "updated_at": "2026-06-22T03:00:00+00:00",
}


def ledger_row(deal_id, **overrides) -> dict:
    """One canonical ledger row with every column a consumer may read.

    Defaults describe a fully-resolved, unambiguous, Google-Ads-attributed won
    deal in USD; each test overrides only the field it is exercising.
    """
    row = {
        "deal_id": str(deal_id),
        "deal_name": f"Deal {deal_id}",
        "deal_stage_id": "326093516",
        "deal_stage_label": "Deal Won / Payment Received",
        "hs_is_closed": True,
        "hs_is_closed_won": True,
        "deal_created_at": None,
        "deal_close_date": "2026-06-14T00:00:00+00:00",
        "amount_raw": 1000.0,
        "deal_currency_code": "USD",
        "revenue_usd": 1000.0,
        "currency_status": "verified_usd",
        "currency_reason": "deal_currency_is_usd",
        "gclid": None,
        "campaign_name_raw": None,
        "keyword_raw": None,
        "country_raw": None,
        "source_primary_raw": None,
        "source_detail_raw": None,
        "acquisition_group": "unclassified",
        "attribution_status": "unclassified",
        "attribution_reason": "no_classified_contact",
        "primary_contact_id": None,
        "association_count": 1,
        "association_status": "resolved",
        "association_reason": "single_contact",
    }
    row.update(overrides)
    return row


DEFAULT_CLOSE_DATE = "2026-06-14T00:00:00+00:00"


def from_legacy_deal_rows(rows, *, default_close_date=DEFAULT_CLOSE_DATE) -> list:
    """Translate a legacy ``gclid_attribution``-shaped fixture to ledger rows.

    Keeps existing test datasets meaningful across the cutover: the same deals,
    the same amounts, the same campaigns — expressed in canonical columns. A
    legacy row with a null ``deal_amount_usd`` becomes a deal whose currency
    could not be proven, which is the canonical way to say "value unknown".
    """
    out = []
    for r in rows or []:
        amount = r.get("deal_amount_usd")
        proven = amount is not None
        group = r.get("acquisition_group")
        if group is None:
            group = "google_ads" if r.get("campaign_name") else "unclassified"
        status = r.get("attribution_status")
        if status is None:
            status = "attributed" if r.get("campaign_name") else "unclassified"
        out.append(ledger_row(
            r.get("deal_id"),
            deal_name=r.get("company") or f"Deal {r.get('deal_id')}",
            deal_close_date=(r.get("deal_close_date") or default_close_date),
            deal_stage_label=r.get("deal_stage_label") or "Deal Won / Payment Received",
            amount_raw=amount,
            revenue_usd=amount if proven else None,
            currency_status="verified_usd" if proven else "unavailable",
            currency_reason=("deal_currency_is_usd" if proven else "no_amount"),
            campaign_name_raw=r.get("campaign_name"),
            country_raw=r.get("country"),
            gclid=r.get("gclid") or ("gclid-" + str(r.get("deal_id"))
                                     if r.get("match_source") == "gclid" else None),
            source_primary_raw=r.get("source_primary_raw"),
            source_detail_raw=r.get("source_detail_raw"),
            acquisition_group=group,
            attribution_status=status,
            primary_contact_id=r.get("contact_id"),
        ))
    return out


def from_source_rows(rows, *, default_close_date="2026-05-10T00:00:00+00:00") -> list:
    """Translate a legacy ``deal_source_attribution``-shaped fixture to ledger rows.

    Those fixtures describe the SOURCE side of a deal (acquisition group + raw
    HubSpot source fields + amount) and, for the daily variant, a ``close_date``.
    After the cutover the page reads one canonical row set for both the mix and
    the trend, so both fixture shapes translate here.
    """
    out = []
    for i, r in enumerate(rows or [], start=1):
        amount = r.get("deal_amount_usd")
        proven = amount is not None
        close = r.get("close_date") or r.get("deal_close_date") or default_close_date
        if len(str(close)) == 10:
            close = f"{close}T00:00:00+00:00"
        group = r.get("acquisition_group") or "unclassified"
        out.append(ledger_row(
            r.get("deal_id") or f"src{i}",
            deal_close_date=close,
            amount_raw=amount,
            revenue_usd=amount if proven else None,
            currency_status="verified_usd" if proven else "unavailable",
            currency_reason=("deal_currency_is_usd" if proven else "no_amount"),
            campaign_name_raw=r.get("campaign_name"),
            country_raw=r.get("country"),
            source_primary_raw=r.get("source_primary_raw"),
            source_detail_raw=r.get("source_detail_raw"),
            acquisition_group=group,
            attribution_status=(r.get("attribution_status")
                                if r.get("attribution_status") in
                                ("attributed", "ambiguous", "unclassified")
                                else ("attributed" if group != "unclassified"
                                      else "unclassified")),
        ))
    return out


def patch_canonical_ledger(monkeypatch, rows, *, available=True, reason=None,
                           sync_state=None, sync_available=True):
    """Stub the canonical deal ledger for a test.

    ``rows`` are canonical ledger rows (see :func:`ledger_row`). ``available``
    False simulates an unreadable ledger; ``sync_state`` overrides the coverage
    state so a test can prove the fail-closed path.
    """
    import db.deal_ledger_repository as ledger_repo

    state = READY_SYNC_STATE if sync_state is None else sync_state
    rows = list(rows or [])

    monkeypatch.setattr(
        ledger_repo, "fetch_sync_state",
        lambda: ({"available": True, "row": dict(state) if state else None}
                 if sync_available
                 else {"available": False, "row": None, "reason": "db_unavailable"}))
    monkeypatch.setattr(
        ledger_repo, "fetch_won_deals",
        lambda start=None, end=None: (
            {"available": True, "rows": [dict(r) for r in rows]} if available
            else {"available": False, "rows": [],
                  "reason": reason or "db_unavailable"}))
    monkeypatch.setattr(
        ledger_repo, "fetch_won_state_counts",
        lambda start=None, end=None: (
            {"available": True,
             "counts": {"unknown_won": 0, "not_won": 0, "deals_in_window": len(rows)}}
            if available else
            {"available": False, "counts": {}, "reason": "db_unavailable"}))
    return rows


def canonical_ledger_patch(rows=(), *, available=True, reason=None,
                           sync_state=None, sync_available=True):
    """The same stub as :func:`patch_canonical_ledger`, as ONE context manager.

    For suites built on ``with patch(...), patch(...):`` chains rather than
    monkeypatch. Returns a single ``patch.multiple`` so a caller adds one item
    to the chain instead of three.
    """
    from unittest.mock import patch  # noqa: PLC0415

    state = READY_SYNC_STATE if sync_state is None else sync_state
    rows = [dict(r) for r in (rows or [])]

    def _sync():
        if not sync_available:
            return {"available": False, "row": None, "reason": "db_unavailable"}
        return {"available": True, "row": dict(state) if state else None}

    def _deals(start=None, end=None):
        if not available:
            return {"available": False, "rows": [],
                    "reason": reason or "db_unavailable"}
        return {"available": True, "rows": [dict(r) for r in rows]}

    def _counts(start=None, end=None):
        if not available:
            return {"available": False, "counts": {}, "reason": "db_unavailable"}
        return {"available": True,
                "counts": {"unknown_won": 0, "not_won": 0,
                           "deals_in_window": len(rows)}}

    return patch.multiple("db.deal_ledger_repository",
                          fetch_sync_state=_sync,
                          fetch_won_deals=_deals,
                          fetch_won_state_counts=_counts)


__all__ = ["READY_SYNC_STATE", "DEFAULT_CLOSE_DATE", "ledger_row", "from_legacy_deal_rows",
           "from_source_rows", "patch_canonical_ledger", "canonical_ledger_patch"]
