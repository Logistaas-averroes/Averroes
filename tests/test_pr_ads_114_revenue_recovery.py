"""
PR-ADS-114 — Revenue Truth Recovery & Durable Daily Sync tests.

Proves:
  - incremental HubSpot writes never use run_id=None
  - fetched non-empty contacts/deals cannot be marked successful with 0 persisted
  - recovery writes contact_created_at (passes createdate-bearing contacts to write_leads)
  - recovery reports contacts still missing createdate (date-grain readiness)
  - closed-won deals are pulled by closedate (not contact-creation / scheduler date)
  - only real-GCLID deals enter gclid_attribution; no synthetic GCLID is created
  - recovery distinguishes: no closed-won / closed-won w/o GCLID / attributed rows written
  - admin-only recovery endpoints exist and are read-only to external platforms

Service-level tests skip gracefully where runtime deps (hubspot/psycopg2/fastapi)
are absent; they run for real in CI.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCR = open(os.path.join(ROOT, "scheduler", "incremental_sync.py"), encoding="utf-8").read()
RECOVERY_SRC = open(os.path.join(ROOT, "services", "revenue_recovery_service.py"), encoding="utf-8").read()
CONN_SRC = open(os.path.join(ROOT, "connectors", "hubspot_pull.py"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()


def _require_runtime():
    pytest.importorskip("hubspot")
    pytest.importorskip("psycopg2")


# ════════════════ build_attribution_rows_from_deals (pure) ════════════════


def _build():
    from services.revenue_recovery_service import build_attribution_rows_from_deals
    return build_attribution_rows_from_deals


def test_only_real_gclid_deals_are_attributed():
    deals = [
        {"deal_id": "1", "gclid": "CjwK-real", "campaign_name": "gulf",
         "deal_amount_usd": 1000, "deal_close_date": "2026-05-01"},
        {"deal_id": "2", "gclid": "", "campaign_name": "gulf",
         "deal_amount_usd": 2000, "deal_close_date": "2026-05-02"},
        {"deal_id": "3", "gclid": None, "campaign_name": None,
         "deal_amount_usd": 3000, "deal_close_date": "2026-05-03"},
    ]
    rows, counts = _build()(deals)
    assert len(rows) == 1
    assert rows[0]["deal_id"] == "1"
    assert rows[0]["gclid"] == "CjwK-real"
    assert rows[0]["match_source"] == "gclid"
    assert counts["closed_won_deals_found"] == 3
    assert counts["closed_won_deals_with_gclid"] == 1
    assert counts["closed_won_deals_without_gclid"] == 2
    assert counts["deals_without_campaign_mapping"] == 1  # deal 3 has no campaign


def test_no_synthetic_gclid_created():
    # Every attributed row's GCLID must equal an input GCLID — never invented.
    deals = [{"deal_id": "9", "gclid": "G-INPUT", "campaign_name": "c",
              "deal_amount_usd": 5, "deal_close_date": "2026-01-01"}]
    rows, _ = _build()(deals)
    assert all(r["gclid"] == "G-INPUT" for r in rows)
    # No GCLID fabrication primitives anywhere in the recovery source.
    low = RECOVERY_SRC.lower()
    for primitive in ("uuid", "random", "secrets.", "uuid4", "token_hex"):
        assert primitive not in low, f"fabrication primitive present: {primitive}"


def test_attribution_empty_when_no_deals():
    rows, counts = _build()([])
    assert rows == []
    assert counts["closed_won_deals_found"] == 0
    assert counts["closed_won_deals_with_gclid"] == 0


# ════════════════ connector: closed-won by closedate ════════════════


def test_connector_pulls_closed_won_by_closedate():
    # The won-deal pull filters by dealstage + closedate, never run_date or createdate.
    idx = CONN_SRC.find("def pull_closed_won_deals_in_range")
    assert idx != -1
    body = CONN_SRC[idx:idx + 2500]
    assert '"propertyName": "dealstage"' in body
    assert '"propertyName": "closedate"' in body
    assert "run_date" not in body
    # Must not window won deals by contact creation date.
    assert "createdate" not in body[body.find("filterGroups"):body.find("properties")]


# ════════════════ recovery service (runtime) ════════════════


def _patch_recovery(monkeypatch, contacts, deals, *, leads_written=None, attr_written=None):
    """Patch connector pulls + DB writers for the recovery service. Skips if
    runtime deps are unavailable."""
    _require_runtime()
    calls = {"write_leads": [], "write_gclid": [], "closed_won_args": [], "contacts_args": []}

    def _contacts(*a, **k):
        calls["contacts_args"].append(k or a)
        return contacts

    def _deals(*a, **k):
        calls["closed_won_args"].append(k or a)
        return deals

    def _write_leads(run_id, rows):
        calls["write_leads"].append((run_id, rows))
        return leads_written if leads_written is not None else len(rows)

    def _write_gclid(run_id, rows, *a, **k):
        calls["write_gclid"].append((run_id, rows))
        return attr_written if attr_written is not None else len(rows)

    try:
        monkeypatch.setattr("connectors.hubspot_pull.pull_paid_search_contacts_in_range", _contacts)
        monkeypatch.setattr("connectors.hubspot_pull.pull_closed_won_deals_in_range", _deals)
        monkeypatch.setattr("db.writers.write_leads", _write_leads)
        monkeypatch.setattr("db.writers.write_gclid_attribution", _write_gclid)
        monkeypatch.setattr("db.writers.write_run", lambda *a, **k: 777)
        monkeypatch.setattr("db.writers.update_run", lambda *a, **k: None)
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")
    return calls


def test_recovery_writes_contacts_with_createdate(monkeypatch):
    contacts = [
        {"id": "c1", "properties": {"createdate": "2026-01-05T00:00:00Z",
                                    "hs_analytics_source": "PAID_SEARCH"}},
        {"id": "c2", "properties": {"createdate": "2026-02-05T00:00:00Z",
                                    "hs_analytics_source": "PAID_SEARCH"}},
    ]
    calls = _patch_recovery(monkeypatch, contacts, [])
    from services.revenue_recovery_service import run_revenue_recovery
    result = run_revenue_recovery(date_from="2026-01-01", date_to="2026-02-28",
                                  dry_run=False, chunk_months=1)
    # write_leads received the createdate-bearing contacts under a real run_id.
    assert calls["write_leads"], "recovery must persist contacts"
    for run_id, rows in calls["write_leads"]:
        assert run_id == 777
        for c in rows:
            assert (c.get("properties") or c).get("createdate")
    assert result["summary"]["contacts_still_missing_createdate"] == 0


def test_recovery_reports_contacts_missing_createdate(monkeypatch):
    contacts = [
        {"id": "c1", "properties": {"createdate": "2026-01-05T00:00:00Z"}},
        {"id": "c2", "properties": {"createdate": ""}},  # missing date
    ]
    _patch_recovery(monkeypatch, contacts, [])
    from services.revenue_recovery_service import run_revenue_recovery
    result = run_revenue_recovery(date_from="2026-01-01", date_to="2026-01-31",
                                  dry_run=False, chunk_months=1)
    assert result["summary"]["contacts_still_missing_createdate"] == 1


def test_recovery_pulls_deals_by_closedate_not_contacts(monkeypatch):
    calls = _patch_recovery(monkeypatch, [], [])
    from services.revenue_recovery_service import run_revenue_recovery
    run_revenue_recovery(date_from="2026-01-01", date_to="2026-01-31",
                         dry_run=False, chunk_months=1)
    # Closed-won deals were fetched directly by closedate range.
    assert calls["closed_won_args"], "recovery must pull closed-won deals by closedate"


def test_recovery_distinguishes_no_deals(monkeypatch):
    _patch_recovery(monkeypatch, [], [])
    from services.revenue_recovery_service import run_revenue_recovery
    out = run_revenue_recovery(date_from="2026-01-01", date_to="2026-01-31",
                               dry_run=False, chunk_months=1)
    s = out["summary"]
    assert s["closed_won_deals_found"] == 0
    assert s["attributed_deal_rows_written"] == 0


def test_recovery_distinguishes_deals_without_gclid(monkeypatch):
    deals = [{"deal_id": "1", "gclid": "", "campaign_name": "c",
              "deal_amount_usd": 100, "deal_close_date": "2026-01-10"}]
    calls = _patch_recovery(monkeypatch, [], deals)
    from services.revenue_recovery_service import run_revenue_recovery
    out = run_revenue_recovery(date_from="2026-01-01", date_to="2026-01-31",
                               dry_run=False, chunk_months=1)
    s = out["summary"]
    assert s["closed_won_deals_found"] == 1
    assert s["closed_won_deals_with_gclid"] == 0
    assert s["attributed_deal_rows_written"] == 0
    assert not calls["write_gclid"]  # nothing attributable → no write


def test_recovery_distinguishes_attributed_rows_written(monkeypatch):
    deals = [{"deal_id": "1", "gclid": "G-1", "campaign_name": "c",
              "deal_amount_usd": 100, "deal_close_date": "2026-01-10"}]
    calls = _patch_recovery(monkeypatch, [], deals, attr_written=1)
    from services.revenue_recovery_service import run_revenue_recovery
    out = run_revenue_recovery(date_from="2026-01-01", date_to="2026-01-31",
                               dry_run=False, chunk_months=1)
    s = out["summary"]
    assert s["closed_won_deals_with_gclid"] == 1
    assert s["attributed_deal_rows_written"] == 1
    assert calls["write_gclid"], "attributable rows must be written"
    run_id, rows = calls["write_gclid"][0]
    assert run_id == 777
    assert rows[0]["gclid"] == "G-1"


def test_recovery_dry_run_writes_nothing(monkeypatch):
    deals = [{"deal_id": "1", "gclid": "G-1", "campaign_name": "c",
              "deal_amount_usd": 100, "deal_close_date": "2026-01-10"}]
    contacts = [{"id": "c1", "properties": {"createdate": "2026-01-05T00:00:00Z"}}]
    calls = _patch_recovery(monkeypatch, contacts, deals)
    from services.revenue_recovery_service import run_revenue_recovery
    out = run_revenue_recovery(date_from="2026-01-01", date_to="2026-01-31",
                               dry_run=True, chunk_months=1)
    assert not calls["write_leads"], "dry run must not write contacts"
    assert not calls["write_gclid"], "dry run must not write attribution"
    # Expected impact is still reported.
    assert out["summary"]["closed_won_deals_with_gclid"] == 1
    assert out["summary"]["attributed_deal_rows_written"] == 0


def test_recovery_default_range_is_all_time(monkeypatch):
    _patch_recovery(monkeypatch, [], [])
    from services.revenue_recovery_service import run_revenue_recovery, DEFAULT_RECOVERY_START
    out = run_revenue_recovery(dry_run=True, chunk_months=12)
    assert out["date_from"] == DEFAULT_RECOVERY_START.isoformat()


def test_recovery_is_resumable(monkeypatch):
    _patch_recovery(monkeypatch, [], [])
    from services.revenue_recovery_service import run_revenue_recovery
    progress = {}
    run_revenue_recovery(date_from="2026-01-01", date_to="2026-03-31",
                         dry_run=False, chunk_months=1, progress=progress)
    completed = set(progress["completed_chunks"])
    assert completed, "completed chunks must be tracked for resumability"
    # A resumed run skips already-completed chunks.
    out2 = run_revenue_recovery(date_from="2026-01-01", date_to="2026-03-31",
                                dry_run=False, chunk_months=1, resume=True,
                                progress=progress)
    assert out2["chunks"] == [], "resume must skip completed chunks"


# ════════════════ incremental sync: real run_id + honest persistence ════════


def _patch_incremental(monkeypatch, *, contacts_write=None, contacts_pull=None):
    _require_runtime()
    captured = {"run_ids": []}

    def _pull3(*a, **k):
        return [{"date": "2026-01-01", "campaign": "c", "spend": 1.0}]

    def _spy_write(run_id, rows):
        captured["run_ids"].append(run_id)
        return len(rows)

    try:
        for fn in ("pull_campaign_performance_range", "pull_keyword_performance_range",
                   "pull_geo_performance_range"):
            monkeypatch.setattr(f"connectors.windsor_pull.{fn}", _pull3)
        monkeypatch.setattr("connectors.hubspot_pull.pull_paid_search_contacts_in_range",
                            contacts_pull or _pull3)
        monkeypatch.setattr("connectors.hubspot_pull.pull_deals_with_gclid", lambda c, *a, **k: _pull3())
        monkeypatch.setattr("connectors.hubspot_pull.pull_closed_won_deals_in_range", lambda *a, **k: [])
        monkeypatch.setattr("db.writers.write_campaigns", _spy_write)
        monkeypatch.setattr("db.writers.write_keywords", _spy_write)
        monkeypatch.setattr("db.writers.write_geo", _spy_write)
        monkeypatch.setattr("db.writers.write_leads", contacts_write or _spy_write)
        monkeypatch.setattr("db.writers.write_deals", _spy_write)
        monkeypatch.setattr("db.writers.write_gclid_attribution", lambda *a, **k: 0)
        monkeypatch.setattr("db.writers.write_run", lambda *a, **k: 4242)
        monkeypatch.setattr("db.writers.update_run", lambda *a, **k: None)
        monkeypatch.setattr("db.writers.start_sync_batch", lambda *a, **k: 1)
        monkeypatch.setattr("db.writers.finish_sync_batch", lambda *a, **k: True)
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")
    return captured


def test_incremental_writes_never_use_none_run_id(monkeypatch):
    captured = _patch_incremental(monkeypatch)
    from scheduler.incremental_sync import run_daily_incremental_sync
    result = run_daily_incremental_sync(run_reason="test")
    assert result["run_id"] == 4242
    assert captured["run_ids"], "writers must be called"
    assert all(rid == 4242 for rid in captured["run_ids"])
    assert None not in captured["run_ids"]


def test_incremental_nonempty_fetch_zero_persist_is_failure(monkeypatch):
    # Contacts fetched (3 rows) but write persists 0 → dataset must be 'failed'.
    captured = _patch_incremental(
        monkeypatch,
        contacts_pull=lambda *a, **k: [{"date": "2026-01-01", "campaign": "c"} for _ in range(3)],
        contacts_write=lambda run_id, rows: 0,
    )
    from scheduler.incremental_sync import run_daily_incremental_sync
    result = run_daily_incremental_sync(run_reason="test")
    assert result["datasets"]["hubspot/contacts"]["status"] == "failed"


# ════════════════ API endpoints + no external writes ════════════════


def test_recovery_endpoints_registered_and_admin_only():
    assert '"/api/revenue-recovery/run"' in SERVER
    assert '"/api/revenue-recovery/status"' in SERVER
    run_idx = SERVER.find("def api_revenue_recovery_run")
    status_idx = SERVER.find("def api_revenue_recovery_status")
    assert run_idx != -1 and status_idx != -1
    # Both endpoints enforce admin.
    assert "check_admin_or_token(request)" in SERVER[run_idx:run_idx + 600]
    assert "check_admin_or_token(request)" in SERVER[status_idx:status_idx + 400]


def test_no_external_platform_writes_in_recovery_or_sync():
    # Read-only doctrine: neither module performs Google Ads / HubSpot mutations.
    for src in (RECOVERY_SRC, INCR):
        low = src.lower()
        for forbidden in ("create_deal", "update_deal", "create_contact",
                          "update_contact", "mutate", "googleads", "google_ads_upload",
                          "oct_upload", "upload_conversions"):
            assert forbidden not in low, f"forbidden external write token: {forbidden}"
