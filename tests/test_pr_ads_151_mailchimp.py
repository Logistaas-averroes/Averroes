"""
tests/test_pr_ads_151_mailchimp.py

PR-ADS-151 — Mailchimp Read-Only Connection & Data Audit.

Covers:
  - connector governance (GET-only; credentials never exposed), server-prefix
    derivation, retry / 429 / 401 / 5xx handling, normalisation;
  - durable repository upsert idempotency + fail-closed identity;
  - sync-service preflight + rolling-refresh window logic;
  - status-service connection/dataset state mapping;
  - attribution-audit reconciliation math (privacy-preserving, safe 1:1 only);
  - read-only API endpoints + auth + no-mutation-route governance.

No live Mailchimp/HubSpot calls: the network is always mocked.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
os.environ.setdefault("APP_ENV", "development")


# ── Fake HTTP plumbing ────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json


@contextmanager
def _mc_env(monkeypatch, *, key="deadbeef-us21", prefix=""):
    # MAILCHIMP_API_KEY is the only required variable (PR-ADS-151 completion);
    # MAILCHIMP_ENABLED is no longer used.
    monkeypatch.setenv("MAILCHIMP_API_KEY", key)
    monkeypatch.setenv("MAILCHIMP_SERVER_PREFIX", prefix)
    monkeypatch.delenv("MAILCHIMP_ENABLED", raising=False)
    yield


# ── Connector: configuration & prefix derivation ─────────────────────────────

class TestConnectorConfig:
    def test_prefix_from_key_suffix(self):
        from connectors import mailchimp_pull as mc
        assert mc.derive_server_prefix(api_key="abc123-us21") == "us21"

    def test_explicit_prefix_wins(self):
        from connectors import mailchimp_pull as mc
        assert mc.derive_server_prefix(api_key="abc-us21", explicit_prefix="us9") == "us9"

    def test_invalid_key_yields_none(self):
        from connectors import mailchimp_pull as mc
        assert mc.derive_server_prefix(api_key="nodashhere") is None
        assert mc.derive_server_prefix(api_key="abc-BADPREFIX!") is None

    def test_config_status_never_exposes_key(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        with _mc_env(monkeypatch, key="supersecret-us5"):
            cfg = mc.config_status()
        assert "supersecret" not in str(cfg)
        assert cfg["server_prefix"] == "us5"
        assert cfg["has_api_key"] is True
        assert cfg["configured"] is True
        # No field named api_key / key leaks the secret.
        assert "api_key" not in cfg and "key" not in cfg

    def test_key_alone_enables_and_configures(self, monkeypatch):
        """A valid API key alone (no MAILCHIMP_ENABLED) → configured=true."""
        from connectors import mailchimp_pull as mc
        monkeypatch.delenv("MAILCHIMP_ENABLED", raising=False)
        monkeypatch.setenv("MAILCHIMP_SERVER_PREFIX", "")
        monkeypatch.setenv("MAILCHIMP_API_KEY", "abcd1234-us3")
        assert mc.is_enabled() is True
        cfg = mc.config_status()
        assert cfg["configured"] is True
        assert cfg["enabled"] is True
        assert cfg["server_prefix"] == "us3"

    def test_key_absent_not_configured(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        monkeypatch.delenv("MAILCHIMP_ENABLED", raising=False)
        monkeypatch.setenv("MAILCHIMP_API_KEY", "")
        assert mc.is_enabled() is False
        assert mc.config_status()["configured"] is False

    def test_key_without_resolvable_prefix_not_configured(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        monkeypatch.setenv("MAILCHIMP_SERVER_PREFIX", "")
        monkeypatch.setenv("MAILCHIMP_API_KEY", "nodashkey")
        assert mc.config_status()["configured"] is False

    def test_enabled_true_env_is_ignored_without_key(self, monkeypatch):
        """MAILCHIMP_ENABLED is no longer honoured: it can't configure without a key."""
        from connectors import mailchimp_pull as mc
        monkeypatch.setenv("MAILCHIMP_ENABLED", "true")
        monkeypatch.setenv("MAILCHIMP_API_KEY", "")
        assert mc.config_status()["configured"] is False

    def test_not_configured_raises(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        monkeypatch.setenv("MAILCHIMP_API_KEY", "")
        with pytest.raises(mc.MailchimpNotConfigured):
            mc.ping()


# ── Connector: governance (GET-only) ──────────────────────────────────────────

class TestConnectorGovernance:
    def test_request_refuses_non_get(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        with _mc_env(monkeypatch):
            for verb in ("POST", "PUT", "PATCH", "DELETE"):
                with pytest.raises(mc.MailchimpError):
                    mc._request("campaigns", method=verb)

    def test_connector_source_has_no_write_verbs(self):
        """Static governance guard: the connector never calls a mutating verb."""
        import inspect
        from connectors import mailchimp_pull as mc
        src = inspect.getsource(mc)
        for forbidden in ("requests.post", "requests.put", "requests.patch",
                          "requests.delete", ".post(", ".put(", ".patch(", ".delete("):
            assert forbidden not in src, f"forbidden mutating call found: {forbidden}"

    def test_ping_success(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        with _mc_env(monkeypatch):
            monkeypatch.setattr(mc.requests, "get",
                                lambda *a, **k: _FakeResp(200, {"health_status": "Everything's Chimpy!"}))
            result = mc.ping()
        assert result["ok"] is True
        assert "Chimpy" in result["health_status"]


# ── Connector: error handling ─────────────────────────────────────────────────

class TestConnectorErrors:
    def test_401_maps_to_auth_error(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        with _mc_env(monkeypatch):
            monkeypatch.setattr(mc.requests, "get", lambda *a, **k: _FakeResp(401))
            with pytest.raises(mc.MailchimpAuthError):
                mc.ping()

    def test_403_maps_to_auth_error(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        with _mc_env(monkeypatch):
            monkeypatch.setattr(mc.requests, "get", lambda *a, **k: _FakeResp(403))
            with pytest.raises(mc.MailchimpAuthError):
                mc.ping()

    def test_429_exhausts_to_rate_limited(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        with _mc_env(monkeypatch):
            monkeypatch.setattr(mc.time, "sleep", lambda *_a, **_k: None)
            monkeypatch.setattr(mc.requests, "get",
                                lambda *a, **k: _FakeResp(429, headers={"Retry-After": "0"}))
            with pytest.raises(mc.MailchimpRateLimited):
                mc.ping()

    def test_429_then_success_retries(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        calls = {"n": 0}

        def _get(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResp(429, headers={"Retry-After": "0"})
            return _FakeResp(200, {"health_status": "ok"})

        with _mc_env(monkeypatch):
            monkeypatch.setattr(mc.time, "sleep", lambda *_a, **_k: None)
            monkeypatch.setattr(mc.requests, "get", _get)
            assert mc.ping()["ok"] is True
        assert calls["n"] == 2

    def test_500_exhausts_to_retryable(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        with _mc_env(monkeypatch):
            monkeypatch.setattr(mc.time, "sleep", lambda *_a, **_k: None)
            monkeypatch.setattr(mc.requests, "get", lambda *a, **k: _FakeResp(500))
            with pytest.raises(mc.MailchimpRetryableError):
                mc.ping()


# ── Connector: normalisation ──────────────────────────────────────────────────

class TestConnectorNormalisation:
    def test_normalize_campaign(self):
        from connectors import mailchimp_pull as mc
        raw = {
            "id": "abc", "web_id": 42, "type": "regular", "status": "sent",
            "create_time": "2026-01-01T00:00:00+00:00",
            "send_time": "2026-01-02T00:00:00+00:00", "emails_sent": 1000,
            "settings": {"subject_line": "Hi", "title": "Jan Blast"},
            "recipients": {"list_id": "list1", "recipient_count": 1000},
            "report_summary": {"opens": 100},
        }
        n = mc.normalize_campaign(raw)
        assert n["campaign_id"] == "abc"
        assert n["list_id"] == "list1"
        assert n["subject_line"] == "Hi"
        assert n["title"] == "Jan Blast"
        assert n["emails_sent"] == 1000
        assert n["source_system"] == "mailchimp"

    def test_normalize_report_delivered_estimate(self):
        from connectors import mailchimp_pull as mc
        raw = {
            "id": "abc", "list_id": "l1", "emails_sent": 1000,
            "opens": {"opens_total": 300, "unique_opens": 250, "open_rate": 0.25},
            "clicks": {"clicks_total": 80, "unique_clicks": 60,
                       "unique_subscriber_clicks": 55, "click_rate": 0.06},
            "bounces": {"hard_bounces": 5, "soft_bounces": 3, "syntax_errors": 2},
            "unsubscribed": 4, "abuse_reports": 1,
        }
        n = mc.normalize_report(raw)
        assert n["delivered_estimate"] == 1000 - 5 - 3 - 2
        assert n["unique_opens"] == 250
        assert n["unique_clicks"] == 60
        assert n["hard_bounces"] == 5
        assert n["unsubscribes"] == 4
        assert n["abuse_reports"] == 1

    def test_sent_to_drops_email_address(self, monkeypatch):
        """Governance: recipient identities carry only the MD5 email_id, never the
        raw address."""
        from connectors import mailchimp_pull as mc
        page = {"sent_to": [
            {"email_id": "md5hash1", "email_address": "leak@example.com",
             "status": "sent", "list_id": "l1", "open_count": 2},
        ], "total_items": 1}
        with _mc_env(monkeypatch):
            monkeypatch.setattr(mc.requests, "get", lambda *a, **k: _FakeResp(200, page))
            members = mc.get_campaign_sent_to("abc")
        assert members[0]["member_id"] == "md5hash1"
        assert "leak@example.com" not in str(members)
        assert "email_address" not in members[0]

    def test_pagination_collects_all(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        pages = [
            {"campaigns": [{"id": f"c{i}", "settings": {}, "recipients": {}} for i in range(200)],
             "total_items": 250},
            {"campaigns": [{"id": f"c{i}", "settings": {}, "recipients": {}} for i in range(50)],
             "total_items": 250},
        ]
        state = {"i": 0}

        def _get(*a, **k):
            resp = _FakeResp(200, pages[state["i"]])
            state["i"] += 1
            return resp

        with _mc_env(monkeypatch):
            monkeypatch.setattr(mc.requests, "get", _get)
            camps = mc.list_campaigns(page_size=200)
        assert len(camps) == 250


# ── Repository: upsert idempotency + fail-closed identity ─────────────────────

class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.setdefault("execute", []).append((sql, params))

    def executemany(self, sql, rows):
        self.store.setdefault("executemany", []).append((sql, list(rows)))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)


class TestRepositoryUpserts:
    def _patch_conn(self, monkeypatch, store):
        from db import mailchimp_repository as repo

        @contextmanager
        def _fake_get_conn():
            yield _FakeConn(store)

        monkeypatch.setattr(repo, "get_conn", _fake_get_conn)

    def test_upsert_campaigns_skips_missing_id(self, monkeypatch):
        from db import mailchimp_repository as repo
        store: dict = {}
        self._patch_conn(monkeypatch, store)
        rows = [{"campaign_id": "c1", "emails_sent": 10},
                {"emails_sent": 5}]  # missing campaign_id → rejected
        stats = repo.upsert_campaigns(rows, sync_batch_id=7)
        assert stats["prepared"] == 1
        assert stats["skipped_no_id"] == 1
        assert stats["written"] == 1

    def test_upsert_uses_on_conflict(self, monkeypatch):
        from db import mailchimp_repository as repo
        store: dict = {}
        self._patch_conn(monkeypatch, store)
        repo.upsert_campaigns([{"campaign_id": "c1"}])
        sql = store["executemany"][0][0]
        assert "ON CONFLICT (campaign_id) DO UPDATE" in sql

    def test_reports_upsert_is_single_row_per_campaign(self, monkeypatch):
        from db import mailchimp_repository as repo
        store: dict = {}
        self._patch_conn(monkeypatch, store)
        repo.upsert_campaign_reports([{"campaign_id": "c1", "emails_sent": 10}])
        sql = store["executemany"][0][0]
        assert "ON CONFLICT (campaign_id) DO UPDATE" in sql

    def test_links_upsert_keyed_on_campaign_and_link(self, monkeypatch):
        from db import mailchimp_repository as repo
        store: dict = {}
        self._patch_conn(monkeypatch, store)
        stats = repo.upsert_campaign_links([
            {"campaign_id": "c1", "link_id": "l1", "total_clicks": 3},
            {"campaign_id": "c1"},  # missing link_id → rejected
        ])
        assert stats["skipped_no_id"] == 1
        sql = store["executemany"][0][0]
        assert "ON CONFLICT (campaign_id, link_id) DO UPDATE" in sql

    def test_audience_snapshot_keyed_on_list_and_date(self, monkeypatch):
        from db import mailchimp_repository as repo
        store: dict = {}
        self._patch_conn(monkeypatch, store)
        repo.upsert_audience_snapshots([{"list_id": "l1", "member_count": 100}])
        sql = store["executemany"][0][0]
        assert "ON CONFLICT (list_id, snapshot_date) DO UPDATE" in sql

    def test_db_unavailable_when_no_pool(self, monkeypatch):
        from db import mailchimp_repository as repo

        @contextmanager
        def _no_conn():
            yield None

        monkeypatch.setattr(repo, "get_conn", _no_conn)
        stats = repo.upsert_campaigns([{"campaign_id": "c1"}])
        assert stats["db_unavailable"] is True
        assert stats["written"] == 0


# ── Sync service ──────────────────────────────────────────────────────────────

class TestSyncService:
    def test_preflight_skips_when_no_key(self, monkeypatch):
        import services.mailchimp_sync_service as sync
        monkeypatch.setenv("MAILCHIMP_API_KEY", "")
        result = sync.run_incremental()
        assert result["status"] == "skipped"
        assert result["reason"] == "not_configured"

    def test_backfill_skips_when_no_key(self, monkeypatch):
        import services.mailchimp_sync_service as sync
        monkeypatch.setenv("MAILCHIMP_API_KEY", "")
        assert sync.maybe_start_backfill_on_deploy() == "not_configured"

    def _patch_sync_deps(self, monkeypatch, *, discovery, refresh_ids, reports_called):
        """Wire _sync_campaigns' lazily-imported deps for a network-free unit test."""
        from connectors import mailchimp_pull as mc
        from db import mailchimp_repository as repo
        import db.writers as w
        monkeypatch.setattr(mc, "list_campaigns", lambda **k: list(discovery))
        monkeypatch.setattr(mc, "get_campaign_report",
                            lambda cid: (reports_called.append(cid) or {"campaign_id": cid}))
        monkeypatch.setattr(mc, "get_campaign_link_details", lambda cid: [])
        monkeypatch.setattr(w, "start_sync_batch", lambda **k: 0)
        monkeypatch.setattr(w, "finish_sync_batch", lambda **k: True)
        monkeypatch.setattr(repo, "upsert_campaigns",
                            lambda rows, sync_batch_id=None: {"prepared": len(rows), "written": len(rows), "db_unavailable": False})
        monkeypatch.setattr(repo, "upsert_campaign_reports",
                            lambda rows, sync_batch_id=None: {"prepared": len(rows), "written": len(rows), "db_unavailable": False})
        monkeypatch.setattr(repo, "upsert_campaign_links",
                            lambda rows, sync_batch_id=None: {"prepared": len(rows), "written": len(rows), "db_unavailable": False})
        monkeypatch.setattr(repo, "get_sync_state", lambda: {})
        monkeypatch.setattr(repo, "update_sync_state", lambda **k: True)
        captured = {}
        monkeypatch.setattr(repo, "sent_campaign_ids_for_refresh",
                            lambda window_days=None: (captured.__setitem__("window_days", window_days) or list(refresh_ids)))
        return captured

    def test_report_refresh_selected_from_durable_table_not_discovery(self, monkeypatch):
        """§2: a campaign sent 15 days ago is refreshed from the DURABLE table even
        when discovery (watermark API) returns no new campaigns."""
        import services.mailchimp_sync_service as sync
        reports_called: list = []
        cap = self._patch_sync_deps(monkeypatch, discovery=[], refresh_ids=["c15"],
                                    reports_called=reports_called)
        out = sync._sync_campaigns(sync_type="daily", since_send_time=None,
                                   refresh_recent_days=30, full=False, run_id=None)
        assert reports_called == ["c15"]          # refreshed despite empty discovery
        assert cap["window_days"] == 30            # rolling window applied
        assert out["datasets"]["reports"]["refreshed"] == 1

    def test_backfill_refreshes_all_sent(self, monkeypatch):
        """§3: a full backfill refreshes ALL sent campaigns (window_days=None)."""
        import services.mailchimp_sync_service as sync
        reports_called: list = []
        cap = self._patch_sync_deps(monkeypatch, discovery=[], refresh_ids=["a", "b"],
                                    reports_called=reports_called)
        sync._sync_campaigns(sync_type="backfill", since_send_time=None,
                             refresh_recent_days=None, full=True, run_id=None)
        assert cap["window_days"] is None
        assert sorted(reports_called) == ["a", "b"]

    def test_unsent_campaigns_do_not_cause_report_errors(self, monkeypatch):
        """§3: unsent campaigns are never in the durable refresh set, so discovering
        drafts never produces report errors or a partial result."""
        import services.mailchimp_sync_service as sync
        reports_called: list = []
        # Discovery returns a draft + a sent campaign; durable refresh set is sent-only.
        discovery = [{"campaign_id": "draft1", "status": "save", "send_time": None},
                     {"campaign_id": "sent1", "status": "sent", "send_time": "2026-06-01T00:00:00+00:00"}]
        self._patch_sync_deps(monkeypatch, discovery=discovery, refresh_ids=["sent1"],
                              reports_called=reports_called)
        out = sync._sync_campaigns(sync_type="daily", since_send_time=None,
                                   refresh_recent_days=30, full=False, run_id=None)
        assert reports_called == ["sent1"]           # draft never requested
        assert out["datasets"]["reports"]["errors"] == 0
        assert out["datasets"]["reports"]["ok"] is True


# ── Status service ────────────────────────────────────────────────────────────

class TestStatusService:
    def test_connection_not_configured(self):
        from services.mailchimp_status_service import derive_connection_state, CONN_NOT_CONFIGURED
        state = derive_connection_state(
            {"enabled": False, "has_api_key": False, "server_prefix": None},
            ping=lambda: {"health_status": "ok"})
        assert state["state"] == CONN_NOT_CONFIGURED

    def test_connection_connected(self):
        from services.mailchimp_status_service import derive_connection_state, CONN_CONNECTED
        state = derive_connection_state(
            {"enabled": True, "has_api_key": True, "server_prefix": "us1"},
            ping=lambda: {"health_status": "Chimpy"})
        assert state["state"] == CONN_CONNECTED

    def test_connection_permission_denied(self):
        from connectors import mailchimp_pull as mc
        from services.mailchimp_status_service import derive_connection_state, CONN_PERMISSION_DENIED

        def _ping():
            raise mc.MailchimpAuthError("nope")

        state = derive_connection_state(
            {"enabled": True, "has_api_key": True, "server_prefix": "us1"}, ping=_ping)
        assert state["state"] == CONN_PERMISSION_DENIED

    def test_connection_rate_limited(self):
        from connectors import mailchimp_pull as mc
        from services.mailchimp_status_service import derive_connection_state, CONN_RATE_LIMITED

        def _ping():
            raise mc.MailchimpRateLimited("429")

        state = derive_connection_state(
            {"enabled": True, "has_api_key": True, "server_prefix": "us1"}, ping=_ping)
        assert state["state"] == CONN_RATE_LIMITED

    def test_connection_failed(self):
        from services.mailchimp_status_service import derive_connection_state, CONN_FAILED

        def _ping():
            raise RuntimeError("boom")

        state = derive_connection_state(
            {"enabled": True, "has_api_key": True, "server_prefix": "us1"}, ping=_ping)
        assert state["state"] == CONN_FAILED

    def test_skipped_ping_returns_defined_state_not_none(self, monkeypatch):
        """live_ping=False on a configured connector must still return a defined
        string state (never None), preserving the connection-state contract."""
        from services import mailchimp_status_service as st
        with _mc_env(monkeypatch, key="deadbeef-us7"):
            status = st.get_status(live_ping=False)
        assert status["connection"]["state"] == st.CONN_NOT_CHECKED
        assert status["connection"]["state"] is not None

    def test_map_dataset_state(self):
        from services.mailchimp_status_service import (
            map_dataset_state, DS_FRESH, DS_STALE, DS_PARTIAL_BACKFILL, DS_FAILED, DS_NOT_RUN)
        from services.freshness_service import CanonicalFreshnessStatus as C
        assert map_dataset_state(C.FRESH_WITH_DATA, backfill_status="complete", has_rows=True) == DS_FRESH
        assert map_dataset_state(C.STALE_WITH_DATA, backfill_status="complete", has_rows=True) == DS_STALE
        assert map_dataset_state(C.FRESH_WITH_DATA, backfill_status="running", has_rows=True) == DS_PARTIAL_BACKFILL
        assert map_dataset_state(C.FAILED_NO_DATA, backfill_status="complete", has_rows=False) == DS_FAILED
        assert map_dataset_state(C.NOT_RUN, backfill_status=None, has_rows=False) == DS_NOT_RUN


# ── Attribution audit ─────────────────────────────────────────────────────────

def _durable(sql=None, cust=None, won=None, db_unavailable=False):
    """Build a durable-population dict like fetch_durable_outcome_populations()."""
    sql = set(sql or [])
    cust = set(cust or [])
    won = dict(won or {})
    return {
        "sql_contacts": sql, "customer_contacts": cust, "closed_won_by_contact": won,
        "durable_sql_population": len(sql), "durable_customer_population": len(cust),
        "durable_closed_won_population": len(won),
        "db_unavailable": db_unavailable, "window_start": None, "window_end": None,
    }


class TestAttributionAudit:
    def test_md5_matches_mailchimp_convention(self):
        from services.mailchimp_audit_service import md5_email
        import hashlib
        assert md5_email("Foo@Bar.COM") == hashlib.md5(b"foo@bar.com").hexdigest()
        assert md5_email("") is None
        assert md5_email(None) is None

    def test_outcomes_come_from_durable_truth(self):
        """Outcomes are counted from durable SQL/customer/revenue sets, not lifecycle."""
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"camp1": [
            {"member_id": "h1"}, {"member_id": "h2"}, {"member_id": "h3"}, {"member_id": ""},
        ]}
        identity = {"h1": ["1"], "h2": ["2"]}  # h3 → unmatched; "" → no identity
        durable = _durable(sql={"1", "2"}, cust={"2"}, won={"2": 5000.0})
        audit = build_attribution_audit(recipients, identity, durable, identity_available=True)
        assert audit["recipients_inspected"] == 4
        assert audit["recipients_with_member_identity"] == 3
        assert audit["recipients_safely_matchable"] == 2
        assert audit["unmatched_recipients"] == 1
        assert audit["sql_contacts"] == 2
        assert audit["customer_contacts"] == 1
        assert audit["contacts_with_closed_won_revenue"] == 1
        assert audit["overall_attribution_coverage"] == round(2 / 3, 4)

    def test_attributed_outcomes_are_subset_of_durable_populations(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"c": [{"member_id": "h1"}, {"member_id": "h2"}]}
        identity = {"h1": ["1"], "h2": ["2"]}
        # Durable SQL population is larger than what Mailchimp attributes.
        durable = _durable(sql={"1", "2", "9", "10"}, cust={"2"}, won={"2": 100.0})
        audit = build_attribution_audit(recipients, identity, durable, identity_available=True)
        rec = audit["reconciliation"]
        assert rec["outcomes_available"] is True
        assert rec["durable_sql_population"] == 4
        assert rec["attributed_sql"] == 2
        assert rec["sql_is_subset_of_durable"] is True
        assert rec["customers_is_subset_of_durable"] is True
        assert rec["closed_won_is_subset_of_durable"] is True
        assert rec["attributed_closed_won_revenue_usd"] == 100.0

    def test_ambiguous_matches_get_no_outcomes(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"camp1": [{"member_id": "amb"}]}
        identity = {"amb": ["1", "2"]}  # one member → two contacts = ambiguous
        durable = _durable(sql={"1", "2"}, cust={"1"}, won={"1": 9.0})
        audit = build_attribution_audit(recipients, identity, durable, identity_available=True)
        assert audit["ambiguous_matches"] == 1
        assert audit["recipients_safely_matchable"] == 0
        assert audit["sql_contacts"] == 0
        assert audit["customer_contacts"] == 0
        assert audit["contacts_with_closed_won_revenue"] == 0

    def test_identity_unavailable_withholds_outcomes(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"camp1": [{"member_id": "h1"}, {"member_id": ""}]}
        audit = build_attribution_audit(recipients, {}, _durable(sql={"1"}),
                                        identity_available=False)
        assert audit["recipients_with_member_identity"] == 1
        assert audit["recipients_safely_matchable"] is None
        assert audit["sql_contacts"] is None
        assert audit["overall_attribution_coverage"] is None

    def test_durable_unavailable_withholds_outcomes_but_keeps_coverage(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"c": [{"member_id": "h1"}]}
        identity = {"h1": ["1"]}
        audit = build_attribution_audit(recipients, identity, _durable(db_unavailable=True),
                                        identity_available=True)
        # Identity coverage still measured; outcomes withheld.
        assert audit["recipients_safely_matchable"] == 1
        assert audit["outcomes_available"] is False
        assert audit["sql_contacts"] is None

    def test_build_identity_index_hashes_and_carries_no_outcomes(self):
        from services.mailchimp_audit_service import build_identity_index, md5_email
        contacts = [
            {"id": "1", "properties": {"email": "a@x.com", "lifecyclestage": "salesqualifiedlead"}},
            {"id": "2", "properties": {"email": "b@x.com"}},
            {"id": "3", "properties": {"email": ""}},  # no email → skipped
        ]
        index = build_identity_index(contacts)
        assert index[md5_email("a@x.com")] == ["1"]
        assert md5_email("b@x.com") in index
        # Identity bridge only — no outcome flags leak in, and no raw emails.
        assert "a@x.com" not in str(index)
        assert "is_sql" not in str(index)

    def test_audit_output_contains_no_emails(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"c": [{"member_id": "h1"}]}
        identity = {"h1": ["1"]}
        audit = build_attribution_audit(recipients, identity, _durable(sql={"1"}),
                                        identity_available=True)
        assert "@" not in str(audit)


# ── API endpoints + governance ────────────────────────────────────────────────

def _make_client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] not available")
    try:
        from api.server import app
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    return TestClient(app, raise_server_exceptions=False)


def _admin_cookie():
    from api.auth import set_session
    from starlette.responses import Response as StarletteResponse
    r = StarletteResponse()
    set_session(r, "testadmin", "admin")
    for part in r.headers.get("set-cookie", "").split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return part.split("=", 1)[1]
    return None


class TestMailchimpEndpoints:
    def test_status_requires_auth(self):
        client = _make_client()
        assert client.get("/api/mailchimp/status").status_code == 401

    def test_status_returns_config_without_key(self, monkeypatch):
        monkeypatch.delenv("MAILCHIMP_ENABLED", raising=False)
        monkeypatch.setenv("MAILCHIMP_API_KEY", "supersecret-us9")
        client = _make_client()
        cookie = _admin_cookie()
        if not cookie:
            pytest.skip("no session cookie")
        res = client.get("/api/mailchimp/status?live=0", cookies={"ads_session": cookie})
        assert res.status_code == 200
        body = res.json()
        assert "supersecret" not in res.text
        assert body["config"]["server_prefix"] == "us9"
        assert "api_key" not in body["config"]

    def test_campaigns_requires_auth(self):
        client = _make_client()
        assert client.get("/api/mailchimp/campaigns").status_code == 401

    def test_campaign_detail_requires_campaign_id(self):
        client = _make_client()
        cookie = _admin_cookie()
        if not cookie:
            pytest.skip("no session cookie")
        res = client.get("/api/mailchimp/campaign-detail", cookies={"ads_session": cookie})
        # Missing required query param → 422 (validation) is acceptable too.
        assert res.status_code in (400, 422)

    def test_audit_requires_admin(self):
        client = _make_client()
        assert client.get("/api/mailchimp/audit").status_code in (401, 403)

    def test_sync_requires_admin(self):
        client = _make_client()
        assert client.post("/api/mailchimp/sync").status_code in (401, 403)

    def test_no_mailchimp_route_mutates_mailchimp(self):
        """Governance: the only non-GET Mailchimp route is the local sync trigger,
        and it never issues a Mailchimp write (connector is GET-only)."""
        from api.server import app
        for route in app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/mailchimp") and hasattr(route, "methods"):
                non_get = set(route.methods) - {"GET", "HEAD", "OPTIONS"}
                if non_get:
                    # Only the operator sync trigger may be POST — and it drives a
                    # GET-only connector + local-DB writes, never a Mailchimp write.
                    assert path == "/api/mailchimp/sync"
                    assert non_get == {"POST"}
