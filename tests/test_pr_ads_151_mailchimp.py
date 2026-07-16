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
def _mc_env(monkeypatch, *, enabled="true", key="deadbeef-us21", prefix=""):
    monkeypatch.setenv("MAILCHIMP_ENABLED", enabled)
    monkeypatch.setenv("MAILCHIMP_API_KEY", key)
    monkeypatch.setenv("MAILCHIMP_SERVER_PREFIX", prefix)
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

    def test_is_enabled_variants(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        for val, expected in [("true", True), ("1", True), ("yes", True),
                              ("false", False), ("", False), ("no", False)]:
            monkeypatch.setenv("MAILCHIMP_ENABLED", val)
            assert mc.is_enabled() is expected

    def test_not_configured_raises(self, monkeypatch):
        from connectors import mailchimp_pull as mc
        monkeypatch.setenv("MAILCHIMP_ENABLED", "false")
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
    def test_preflight_skips_when_disabled(self, monkeypatch):
        import services.mailchimp_sync_service as sync
        monkeypatch.setenv("MAILCHIMP_ENABLED", "false")
        result = sync.run_incremental()
        assert result["status"] == "skipped"
        assert result["reason"] == "not_configured"

    def test_backfill_skips_when_disabled(self, monkeypatch):
        import services.mailchimp_sync_service as sync
        monkeypatch.setenv("MAILCHIMP_ENABLED", "false")
        assert sync.maybe_start_backfill_on_deploy() == "not_configured"

    def test_full_backfill_refreshes_all_campaigns(self):
        import services.mailchimp_sync_service as sync
        campaigns = [
            {"campaign_id": "c1", "send_time": "2020-01-01T00:00:00+00:00"},
            {"campaign_id": "c2", "send_time": None},
            {"campaign_id": "c3", "send_time": "2026-06-01T00:00:00+00:00"},
        ]
        from datetime import date
        ids = sync._campaigns_needing_report(campaigns, date(2026, 7, 1),
                                             refresh_recent_days=30, full=True)
        assert ids == ["c1", "c2", "c3"]

    def test_incremental_refreshes_only_recent(self):
        import services.mailchimp_sync_service as sync
        from datetime import date
        campaigns = [
            {"campaign_id": "old", "send_time": "2020-01-01T00:00:00+00:00"},
            {"campaign_id": "draft", "send_time": None},
            {"campaign_id": "recent", "send_time": "2026-06-20T00:00:00+00:00"},
        ]
        ids = sync._campaigns_needing_report(campaigns, date(2026, 7, 1),
                                             refresh_recent_days=30, full=False)
        assert ids == ["recent"]


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

class TestAttributionAudit:
    def test_md5_matches_mailchimp_convention(self):
        from services.mailchimp_audit_service import md5_email
        # Mailchimp email_id is MD5 of the lowercased address.
        import hashlib
        assert md5_email("Foo@Bar.COM") == hashlib.md5(b"foo@bar.com").hexdigest()
        assert md5_email("") is None
        assert md5_email(None) is None

    def test_safe_one_to_one_attaches_outcomes(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"camp1": [
            {"member_id": "h1"}, {"member_id": "h2"}, {"member_id": "h3"}, {"member_id": ""},
        ]}
        index = {
            "h1": [{"contact_id": "1", "is_sql": True, "is_customer": False, "closed_won_amount": 0}],
            "h2": [{"contact_id": "2", "is_sql": True, "is_customer": True, "closed_won_amount": 5000}],
            # h3 not present → unmatched
        }
        audit = build_attribution_audit(recipients, index, hubspot_available=True)
        assert audit["recipients_inspected"] == 4
        assert audit["recipients_with_member_identity"] == 3
        assert audit["recipients_safely_matchable"] == 2
        assert audit["unmatched_recipients"] == 1
        assert audit["sql_contacts"] == 2
        assert audit["customer_contacts"] == 1
        assert audit["contacts_with_closed_won_revenue"] == 1
        assert audit["overall_attribution_coverage"] == round(2 / 3, 4)

    def test_ambiguous_matches_get_no_outcomes(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"camp1": [{"member_id": "amb"}]}
        index = {"amb": [
            {"contact_id": "1", "is_sql": True, "is_customer": True, "closed_won_amount": 9},
            {"contact_id": "2", "is_sql": True, "is_customer": True, "closed_won_amount": 9},
        ]}
        audit = build_attribution_audit(recipients, index, hubspot_available=True)
        assert audit["ambiguous_matches"] == 1
        assert audit["recipients_safely_matchable"] == 0
        # No outcomes attached from an ambiguous match.
        assert audit["sql_contacts"] == 0
        assert audit["customer_contacts"] == 0
        assert audit["contacts_with_closed_won_revenue"] == 0

    def test_hubspot_unavailable_withholds_outcomes(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"camp1": [{"member_id": "h1"}, {"member_id": ""}]}
        audit = build_attribution_audit(recipients, {}, hubspot_available=False)
        # Identity coverage still measured...
        assert audit["recipients_with_member_identity"] == 1
        # ...but matched/outcomes are withheld (None), never fabricated.
        assert audit["recipients_safely_matchable"] is None
        assert audit["sql_contacts"] is None
        assert audit["overall_attribution_coverage"] is None

    def test_build_hubspot_index_flags_and_hashes(self):
        from services.mailchimp_audit_service import build_hubspot_index, md5_email
        contacts = [
            {"id": "1", "properties": {"email": "a@x.com", "lifecyclestage": "salesqualifiedlead"}},
            {"id": "2", "properties": {"email": "b@x.com", "lifecyclestage": "customer"}},
            {"id": "3", "properties": {"email": ""}},  # no email → skipped
        ]
        index = build_hubspot_index(contacts, closed_won_contact_ids={"2"})
        assert md5_email("a@x.com") in index
        assert index[md5_email("a@x.com")][0]["is_sql"] is True
        b = index[md5_email("b@x.com")][0]
        assert b["is_customer"] is True
        assert b["closed_won_amount"] > 0
        # No raw emails retained anywhere in the index.
        assert "a@x.com" not in str(index)

    def test_audit_output_contains_no_emails(self):
        from services.mailchimp_audit_service import build_attribution_audit
        recipients = {"c": [{"member_id": "h1"}]}
        index = {"h1": [{"contact_id": "1", "is_sql": False, "is_customer": False, "closed_won_amount": 0}]}
        audit = build_attribution_audit(recipients, index, hubspot_available=True)
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
        monkeypatch.setenv("MAILCHIMP_ENABLED", "false")
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
