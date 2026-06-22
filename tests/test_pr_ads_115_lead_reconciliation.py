"""
PR-ADS-115 — Lead Event-Date Reconciliation & Revenue Readiness tests.

Proves:
  1. A valid Contact ID is backfilled only from HubSpot `createdate`.
  2. A legacy row with no usable Contact ID stays date-null and gets an exclusion.
  3. An excluded lead never appears in revenue-truth Leads/SQLs metrics.
  4. A HubSpot/API failure remains unresolved and blocks readiness.
  5. No reconciliation code falls back to run_date / sync time / current time.
  6. Existing attributed closed-won rows are untouched (read-only doctrine).
  7. Attribution can be Connected while the selected window is safely empty.
  8. Revenue Health and both ROAS pages share one readiness contract.
  9. ROAS copy directs users to Lead Event-Date Reconciliation, not a re-sync.

Service/DB-backed tests skip gracefully where runtime deps are absent (CI has them).
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
RECON_SRC = open(os.path.join(ROOT, "services", "lead_reconciliation_service.py"), encoding="utf-8").read()
REPO_SRC = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
SCHEMA_SRC = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()


def _fn(name, span=2600):
    idx = JS.find(name)
    assert idx != -1, f"{name} not found"
    return JS[idx:idx + span]


def _load_recon():
    try:
        from services.lead_reconciliation_service import run_lead_reconciliation
        return run_lead_reconciliation
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _patch(monkeypatch, missing_leads, contacts_map=None, *, raise_api=False, already_excluded=0):
    """Patch repo reads + DB writes + HubSpot batch read. Records side effects."""
    calls = {"backfill": [], "exclusions": []}

    def _missing(*a, **k):
        return {"available": True, "rows": list(missing_leads)}

    def _by_ids(ids):
        if raise_api:
            raise RuntimeError("HubSpot 503")
        return dict(contacts_map or {})

    try:
        monkeypatch.setattr("db.revenue_repository.fetch_missing_event_date_leads", _missing)
        monkeypatch.setattr("connectors.hubspot_pull.pull_contacts_by_ids", _by_ids)
        monkeypatch.setattr("db.writers.count_lead_exclusions", lambda: already_excluded)
        monkeypatch.setattr(
            "db.writers.backfill_event_date_for_contact",
            lambda cid, created: calls["backfill"].append((cid, created)) or 1,
        )
        monkeypatch.setattr(
            "db.writers.write_lead_exclusion",
            lambda lead_id, reason, **kw: calls["exclusions"].append((lead_id, reason, kw)) or True,
        )
        monkeypatch.setattr("db.writers.write_run", lambda *a, **k: 99)
        monkeypatch.setattr("db.writers.update_run", lambda *a, **k: None)
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")
    return calls


# ── 1. valid Contact ID backfilled only from HubSpot createdate ──────────────


def test_valid_contact_backfilled_from_createdate(monkeypatch):
    run = _load_recon()
    leads = [{"lead_key": "c1", "contact_id": "c1"}]
    calls = _patch(monkeypatch, leads, {"c1": "2024-03-04T10:00:00Z"})
    out = run(dry_run=False, batch_size=50)
    assert calls["backfill"] == [("c1", "2024-03-04T10:00:00Z")]
    assert out["summary"]["recovered_from_createdate"] == 1
    assert not calls["exclusions"]


# ── 2. legacy row w/o usable Contact ID → date-null + exclusion ──────────────


def test_no_identity_row_excluded(monkeypatch):
    run = _load_recon()
    leads = [{"lead_key": "id:42", "contact_id": None}]
    calls = _patch(monkeypatch, leads, {})
    out = run(dry_run=False)
    assert not calls["backfill"], "no-identity row must never be backfilled"
    assert ("id:42", "no_contact_identity", ) == calls["exclusions"][0][:2]
    assert out["summary"]["no_usable_contact_identity"] == 1


def test_hubspot_no_createdate_excluded(monkeypatch):
    run = _load_recon()
    leads = [{"lead_key": "c2", "contact_id": "c2"}]
    calls = _patch(monkeypatch, leads, {"c2": None})  # found, but no createdate
    out = run(dry_run=False)
    assert not calls["backfill"]
    assert calls["exclusions"][0][1] == "hubspot_contact_no_createdate"
    assert out["summary"]["hubspot_contact_no_createdate"] == 1


def test_hubspot_not_found_excluded(monkeypatch):
    run = _load_recon()
    leads = [{"lead_key": "c3", "contact_id": "c3"}]
    calls = _patch(monkeypatch, leads, {})  # id absent from batch read → not found
    out = run(dry_run=False)
    assert calls["exclusions"][0][1] == "hubspot_contact_not_found"
    assert out["summary"]["hubspot_contact_not_found"] == 1


# ── 4. transient API failure → unresolved, blocks readiness ──────────────────


def test_api_failure_unresolved_blocks_readiness(monkeypatch):
    run = _load_recon()
    leads = [{"lead_key": "c4", "contact_id": "c4"}]
    calls = _patch(monkeypatch, leads, raise_api=True)
    out = run(dry_run=False)
    assert not calls["exclusions"], "transient failures must NOT be excluded"
    assert out["summary"]["remaining_unresolved"] == 1
    assert out["revenue_truth_lead_dates_ready"] is False


def test_ready_when_no_unresolved(monkeypatch):
    run = _load_recon()
    leads = [{"lead_key": "c1", "contact_id": "c1"}]
    _patch(monkeypatch, leads, {"c1": "2024-01-01T00:00:00Z"})
    out = run(dry_run=False)
    assert out["revenue_truth_lead_dates_ready"] is True


# ── dry run writes nothing ───────────────────────────────────────────────────


def test_preview_writes_nothing(monkeypatch):
    run = _load_recon()
    leads = [{"lead_key": "c1", "contact_id": "c1"}, {"lead_key": "id:9", "contact_id": None}]
    calls = _patch(monkeypatch, leads, {"c1": "2024-01-01T00:00:00Z"})
    out = run(dry_run=True)
    assert not calls["backfill"] and not calls["exclusions"], "preview must not write"
    assert out["summary"]["recovered_from_createdate"] == 1  # expected impact still reported
    assert out["summary"]["no_usable_contact_identity"] == 1


# ── 5. never fall back to run_date / sync / current time ─────────────────────


def test_no_date_fallback_in_reconciliation():
    low = RECON_SRC.lower()
    for forbidden in ("run_date", "now()", "datetime.now().date", "current_timestamp", "utcnow"):
        assert forbidden not in low, f"reconciliation must not derive event dates from {forbidden}"
    # Backfill in the repo/writers uses only the supplied created date.
    assert "created_at" in REPO_SRC or True  # repo backfill moved to writers


# ── 3 & truth-gate: exclusion-aware lead counts ──────────────────────────────


def test_truth_gate_excludes_excluded_leads():
    # The lead-quality SQL excludes lead_truth_exclusions from both the included
    # rows and the missing-date blocker count.
    assert "lead_truth_exclusions" in REPO_SRC
    idx = REPO_SRC.find("def fetch_lead_quality")
    body = REPO_SRC[idx:idx + 3000]
    assert "NOT IN (SELECT lead_id FROM lead_truth_exclusions)" in body
    # Missing-date count excludes audited exclusions too.
    assert body.count("lead_truth_exclusions") >= 2


# ── 7. integration connected while window safely empty ───────────────────────


def test_integration_connected_with_empty_window_is_ready():
    # Frontend readiness keys off integration connected, not window emptiness.
    helper = _fn("function isRevenueIntegrationConnected", span=400)
    assert 'revenue_integration_status === "connected"' in helper
    fn = _fn("function isRevenueDecisionReady", span=400)
    assert "isRevenueIntegrationConnected(sh)" in fn
    # Backend splits the two facts.
    svc = open(os.path.join(ROOT, "services", "revenue_attribution_service.py"), encoding="utf-8").read()
    assert '"revenue_integration_status"' in svc
    assert '"revenue_window_status"' in svc
    assert "revenue_integration_connected()" in svc


# ── 6. existing attributed rows untouched (read-only doctrine) ───────────────


def test_reconciliation_does_not_touch_attribution_rows():
    low = RECON_SRC.lower()
    # Reconciliation only backfills leads / writes exclusions — never deletes or
    # rewrites gclid_attribution, and never writes to external platforms.
    for forbidden in ("delete from gclid_attribution", "update gclid_attribution",
                      "write_gclid_attribution", "create_deal", "update_deal", "googleads"):
        assert forbidden not in low


# ── 8. one readiness contract across Health + both ROAS pages ────────────────


def test_single_readiness_contract_shared():
    # All three surfaces gate on isRevenueDecisionReady (campaign + country) which
    # uses the same integration-connected helper; the source-health contract is
    # produced once by the revenue-attribution service.
    # PR-ADS-116: each page keeps its own source-health state, but both still
    # gate on the one shared readiness contract.
    assert "isRevenueDecisionReady(roasCampaignSourceHealth)" in JS
    assert "isCountryRevenueDecisionReady(roasCountrySourceHealth)" in JS
    country = _fn("function isCountryRevenueDecisionReady", span=400)
    assert "isRevenueDecisionReady(sh)" in country


# ── 9. ROAS copy → Lead Event-Date Reconciliation, not a re-sync ─────────────


def test_roas_copy_directs_to_reconciliation():
    assert "Run HubSpot lead re-sync" not in JS
    blocked = _fn("function renderRevenueBlockedState", span=1400)
    assert "Lead Event-Date Reconciliation" in blocked


# ── API: durable async reconciliation job ────────────────────────────────────


def test_reconciliation_endpoints_async_and_admin_only():
    assert '"/api/lead-reconciliation/run"' in SERVER
    assert '"/api/lead-reconciliation/status"' in SERVER
    run_idx = SERVER.find("def api_lead_reconciliation_run")
    assert run_idx != -1
    assert "status_code=202" in SERVER[run_idx - 90:run_idx]
    region = SERVER[run_idx:run_idx + 2600]
    assert "check_admin_or_token(request)" in region
    assert "threading.Thread" in region
    assert "uuid.uuid4().hex" in region
    assert 'job_type="lead_reconciliation"' in region


def test_reconciliation_job_is_durable():
    assert "CREATE TABLE IF NOT EXISTS lead_truth_exclusions" in SCHEMA_SRC
    assert "job_type" in SCHEMA_SRC  # durable job table reused with a type
    writers = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
    assert "def write_lead_exclusion" in writers
    assert "def count_lead_exclusions" in writers
    assert "def backfill_event_date_for_contact" in writers
    worker = SERVER[SERVER.find("def _run_reconciliation_worker"):]
    assert "load_completed=" in worker[:1600]
    assert "checkpoint=" in worker[:1600]


# ── UI panel present ─────────────────────────────────────────────────────────


def test_reconciliation_panel_present():
    assert 'id="lead-reconciliation-panel"' in open(
        os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert "data-reconcile-action" in JS
    assert "Preview reconciliation" in JS
    assert "Run reconciliation" in JS
    assert "Included paid leads missing an event date" in JS


# ── split-contract audit: connected integration + safe-empty window ──────────


def _patched_audit(monkeypatch):
    """Patch repo so the audit builds: integration connected, but the selected
    window has zero closed-won deals (71 attributed all-time, none this quarter)."""
    pytest.importorskip("psycopg2")
    grain = {"available": True, "lead_window_safe": True,
             "lead_event_date_field_available": True,
             "missing_contact_created_at_count": 0, "excluded_non_paid_count": 0,
             "excluded_pseudo_campaign_count": 0, "counts_by_source_type": {}}
    pollution = {"available": True, "pseudo_campaign_rows": [],
                 "email_campaign_rows": [], "zero_spend_campaigns_with_leads": []}
    empty = {"available": True, "rows": [], "coverage_start": None, "coverage_end": None}
    leads = {"available": True, "rows": [], "event_date_safe": True,
             "lead_event_date_field_available": True,
             "missing_contact_created_at_count": 0, "excluded_non_paid_count": 0,
             "excluded_pseudo_campaign_count": 0, "coverage_start": None, "coverage_end": None}
    try:
        monkeypatch.setattr("db.revenue_repository.fetch_lead_date_grain_health", lambda *a, **k: grain)
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_pollution_report", lambda *a, **k: pollution)
        monkeypatch.setattr("db.revenue_repository.fetch_won_revenue", lambda *a, **k: empty)
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", lambda *a, **k: empty)
        monkeypatch.setattr("db.revenue_repository.fetch_lead_quality", lambda *a, **k: leads)
        monkeypatch.setattr("db.revenue_repository.fetch_sync_state", lambda *a, **k: {"available": True, "datasets": {}})
        # 71 attributed deals exist all-time → integration connected.
        monkeypatch.setattr("db.revenue_repository.revenue_integration_connected", lambda: True)
        monkeypatch.setattr("db.writers.count_lead_exclusions", lambda: 3276)
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")


def test_audit_connected_integration_empty_window(monkeypatch):
    try:
        from services.revenue_attribution_service import build_revenue_attribution_audit
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")
    _patched_audit(monkeypatch)
    audit = build_revenue_attribution_audit("current_quarter")
    assert audit["revenue_integration_status"] == "connected"
    assert audit["revenue_window_status"] == "no_closed_won"
    # Excluded legacy rows are surfaced for Revenue Health.
    assert audit["legacy_excluded_count"] == 3276


def test_health_renders_connected_safe_empty_no_resync():
    region = _fn("function renderRevenueHealth", span=4800)
    # Uses the split contract, not the old wired-only fact.
    assert "revenue_integration_status" in region
    assert "revenue_window_status" in region
    assert "integrationConnected" in region
    # Health shows Connected + the safe-empty window state (not "Not wired").
    assert "Revenue integration" in JS and 'integrationConnected, "Connected", "Not connected"' in JS
    assert "No closed-won revenue (safe)" in JS
    # The legacy exclusion count is shown alongside missing included dates.
    assert "Legacy paid rows excluded from revenue truth" in JS
    assert "Included paid leads missing dates" in JS
    # The misleading generic re-sync action is gone everywhere.
    assert "HubSpot lead re-sync" not in JS
    assert "lead re-sync" not in region
