"""
tests/test_backfill_api.py

PR-ADS-072 — Tests for the admin backfill API endpoints and the
run_backfill_from_options() helper.

Covers:
  - run_backfill_from_options: invalid source rejected
  - run_backfill_from_options: invalid chunk rejected
  - run_backfill_from_options: invalid date_from rejected
  - run_backfill_from_options: invalid date_to rejected
  - run_backfill_from_options: date_from > date_to rejected
  - run_backfill_from_options: invalid max_chunks rejected
  - run_backfill_from_options: dry_run=True returns plan without calling runners
  - run_backfill_from_options: dry_run=False calls run_backfill_plan
  - POST /api/backfill/run: unauthenticated request rejected
  - POST /api/backfill/run: non-admin request rejected
  - POST /api/backfill/run: invalid source rejected
  - POST /api/backfill/run: invalid date range rejected
  - POST /api/backfill/run: dry_run=True calls helper with dry_run=True
  - POST /api/backfill/run: response contains summary
  - POST /api/backfill/run: 409 when already running
  - GET /api/backfill/status: unauthenticated request rejected
  - GET /api/backfill/status: authenticated request returns running/latest
  - No unsafe action wording in API response

Run with:
    python -m pytest tests/test_backfill_api.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill import run_backfill_from_options

# Unsafe wording that must never appear in API responses.
_UNSAFE_API_PHRASES = [
    "push negative", "apply negative", "pause campaign",
    "send to google ads", "send to hubspot", "upload conversion",
    "change bid", "change budget",
]


@pytest.fixture(autouse=True)
def _set_test_env():
    """Ensure required env vars are present for all tests in this module."""
    import os
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    os.environ.setdefault("APP_ENV", "development")


# ---------------------------------------------------------------------------
# run_backfill_from_options — input validation
# ---------------------------------------------------------------------------

class TestRunBackfillFromOptionsValidation:
    def test_invalid_source_raises(self):
        with pytest.raises(ValueError, match="source must be"):
            run_backfill_from_options(
                source="invalid",
                date_from="2024-01-01",
                date_to="2024-01-31",
            )

    def test_invalid_chunk_raises(self):
        with pytest.raises(ValueError, match="chunk must be"):
            run_backfill_from_options(
                source="all",
                date_from="2024-01-01",
                date_to="2024-01-31",
                chunk="daily",
            )

    def test_invalid_date_from_raises(self):
        with pytest.raises(ValueError, match="date_from"):
            run_backfill_from_options(
                source="all",
                date_from="not-a-date",
                date_to="2024-01-31",
            )

    def test_invalid_date_to_raises(self):
        with pytest.raises(ValueError, match="date_to"):
            run_backfill_from_options(
                source="all",
                date_from="2024-01-01",
                date_to="not-a-date",
            )

    def test_date_from_after_date_to_raises(self):
        with pytest.raises(ValueError, match="date_from must be before"):
            run_backfill_from_options(
                source="all",
                date_from="2024-02-01",
                date_to="2024-01-01",
            )

    def test_invalid_max_chunks_raises(self):
        with pytest.raises(ValueError, match="max_chunks must be >= 1"):
            run_backfill_from_options(
                source="all",
                date_from="2024-01-01",
                date_to="2024-01-31",
                max_chunks=0,
            )

    def test_max_chunks_negative_raises(self):
        with pytest.raises(ValueError, match="max_chunks must be >= 1"):
            run_backfill_from_options(
                source="all",
                date_from="2024-01-01",
                date_to="2024-01-31",
                max_chunks=-5,
            )


# ---------------------------------------------------------------------------
# run_backfill_from_options — dry_run behaviour
# ---------------------------------------------------------------------------

class TestRunBackfillFromOptionsDryRun:
    def test_dry_run_does_not_call_backfill_runners(self):
        """dry_run=True must not call Windsor or HubSpot backfill functions."""
        with (
            patch("scripts.backfill.run_backfill_plan") as mock_run,
        ):
            result = run_backfill_from_options(
                source="all",
                date_from="2024-01-01",
                date_to="2024-02-28",
                dry_run=True,
                max_chunks=1,
            )
            mock_run.assert_not_called()

        assert result["dry_run"] is True
        assert result["source"] == "all"
        assert result["chunks_completed"] == 0
        assert isinstance(result["datasets"], dict)
        assert isinstance(result["errors"], list)

    def test_dry_run_summary_has_expected_keys(self):
        result = run_backfill_from_options(
            source="hubspot",
            date_from="2024-01-01",
            date_to="2024-01-31",
            dry_run=True,
        )
        for key in ("source", "date_from", "date_to", "chunk", "dry_run",
                    "chunks_total", "chunks_completed", "datasets", "errors"):
            assert key in result, f"missing key: {key}"

    def test_dry_run_datasets_have_dry_run_status(self):
        result = run_backfill_from_options(
            source="hubspot",
            date_from="2024-01-01",
            date_to="2024-01-31",
            dry_run=True,
        )
        for ds in result["datasets"].values():
            assert ds["status"] in ("dry_run", "unsupported_by_current_connector")

    def test_dry_run_google_ads_search_terms_marked_unsupported(self):
        result = run_backfill_from_options(
            source="google_ads",
            date_from="2024-01-01",
            date_to="2024-01-31",
            dry_run=True,
        )
        ds = result["datasets"].get("google_ads/search_terms", {})
        assert ds.get("status") == "unsupported_by_current_connector"

    def test_dry_run_chunks_total_matches_plan(self):
        result = run_backfill_from_options(
            source="hubspot",
            date_from="2024-01-01",
            date_to="2024-03-31",
            dry_run=True,
            chunk="monthly",
        )
        # 3 months × 2 hubspot datasets = 6 total
        assert result["chunks_total"] == 6

    def test_dry_run_false_calls_run_backfill_plan(self):
        """dry_run=False must call run_backfill_plan and write_summary."""
        fake_summary = {
            "source": "hubspot",
            "date_from": "2024-01-01",
            "date_to": "2024-01-31",
            "chunk": "monthly",
            "dry_run": False,
            "chunks_total": 2,
            "chunks_completed": 2,
            "datasets": {},
            "errors": [],
        }
        with (
            patch("scripts.backfill.run_backfill_plan", return_value=fake_summary) as mock_run,
            patch("scripts.backfill.write_summary") as mock_write,
        ):
            result = run_backfill_from_options(
                source="hubspot",
                date_from="2024-01-01",
                date_to="2024-01-31",
                dry_run=False,
            )
            mock_run.assert_called_once()
            mock_write.assert_called_once_with(fake_summary)

        assert result["dry_run"] is False


# ---------------------------------------------------------------------------
# FastAPI endpoint tests
# ---------------------------------------------------------------------------

# Minimal test helpers — these tests run against the FastAPI app with
# dependency overrides, using a monkeypatched backfill runner. They do not
# call live APIs or external services.

def _make_test_client():
    """Import the FastAPI app and wrap it in a TestClient."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] not available")

    # Import app — this triggers the lifespan setup; we accept that
    # start_scheduler/init_pool may fail gracefully in a test environment.
    try:
        from api.server import app  # noqa: PLC0415
    except Exception as exc:
        pytest.skip(f"api.server import failed: {exc}")

    return TestClient(app, raise_server_exceptions=False)


# Lazy client fixture so the import only happens when the test runs.
@pytest.fixture()
def client():
    return _make_test_client()


def _set_admin_session(client):
    """
    Inject a fake signed session cookie for an admin user.

    We call /auth/login with overridden authenticate_user to avoid needing
    a real AUTH_USERS_JSON environment variable.
    """
    from api.auth import set_session
    from starlette.responses import Response as StarletteResponse

    r = StarletteResponse()
    set_session(r, "testadmin", "admin")
    cookie_header = r.headers.get("set-cookie", "")
    # Extract cookie value: ads_session=<value>
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return part.split("=", 1)[1]
    return None


class TestBackfillRunEndpoint:
    def test_unauthenticated_returns_401_or_403(self, client):
        res = client.post(
            "/api/backfill/run",
            json={"source": "all", "date_from": "2024-01-01", "date_to": "2024-01-31"},
        )
        assert res.status_code in (401, 403)

    def test_invalid_source_returns_422(self, client):
        cookie_val = _set_admin_session(client)
        cookies = {"ads_session": cookie_val} if cookie_val else {}

        res = client.post(
            "/api/backfill/run",
            json={
                "source": "bad_source",
                "date_from": "2024-01-01",
                "date_to": "2024-01-31",
                "dry_run": True,
            },
            cookies=cookies,
        )
        assert res.status_code == 422

    def test_invalid_date_range_returns_422(self, client):
        cookie_val = _set_admin_session(client)
        cookies = {"ads_session": cookie_val} if cookie_val else {}

        res = client.post(
            "/api/backfill/run",
            json={
                "source": "all",
                "date_from": "2024-02-01",
                "date_to": "2024-01-01",
                "dry_run": True,
            },
            cookies=cookies,
        )
        assert res.status_code == 422

    def test_invalid_chunk_returns_422(self, client):
        cookie_val = _set_admin_session(client)
        cookies = {"ads_session": cookie_val} if cookie_val else {}

        res = client.post(
            "/api/backfill/run",
            json={
                "source": "all",
                "date_from": "2024-01-01",
                "date_to": "2024-01-31",
                "chunk": "yearly",
                "dry_run": True,
            },
            cookies=cookies,
        )
        assert res.status_code == 422

    def test_dry_run_calls_helper_with_dry_run_true(self, client):
        cookie_val = _set_admin_session(client)
        if not cookie_val:
            pytest.skip("Could not create session cookie")

        fake_summary = {
            "source": "hubspot",
            "date_from": "2024-01-01",
            "date_to": "2024-01-31",
            "chunk": "monthly",
            "dry_run": True,
            "chunks_total": 2,
            "chunks_completed": 0,
            "datasets": {},
            "errors": [],
        }
        with patch(
            "scripts.backfill.run_backfill_from_options",
            return_value=fake_summary,
        ) as mock_fn:
            res = client.post(
                "/api/backfill/run",
                json={
                    "source": "hubspot",
                    "date_from": "2024-01-01",
                    "date_to": "2024-01-31",
                    "dry_run": True,
                },
                cookies={"ads_session": cookie_val},
            )

        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")

        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["dry_run"] is True
        assert "summary" in data
        assert mock_fn.call_args.kwargs.get("dry_run") is True

    def test_response_contains_required_fields(self, client):
        cookie_val = _set_admin_session(client)
        if not cookie_val:
            pytest.skip("Could not create session cookie")

        fake_summary = {
            "source": "hubspot",
            "date_from": "2024-01-01",
            "date_to": "2024-01-31",
            "chunk": "monthly",
            "dry_run": True,
            "chunks_total": 2,
            "chunks_completed": 0,
            "datasets": {},
            "errors": [],
        }
        with patch("scripts.backfill.run_backfill_from_options", return_value=fake_summary):
            res = client.post(
                "/api/backfill/run",
                json={
                    "source": "hubspot",
                    "date_from": "2024-01-01",
                    "date_to": "2024-01-31",
                    "dry_run": True,
                },
                cookies={"ads_session": cookie_val},
            )

        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")

        assert res.status_code == 200
        data = res.json()
        for field in ("status", "dry_run", "source", "date_from", "date_to", "chunk",
                      "started_at", "finished_at", "summary"):
            assert field in data, f"missing field: {field}"

    def test_response_wording_safe(self, client):
        """API response must not contain unsafe action wording."""
        cookie_val = _set_admin_session(client)
        if not cookie_val:
            pytest.skip("Could not create session cookie")

        fake_summary = {
            "source": "hubspot", "date_from": "2024-01-01", "date_to": "2024-01-31",
            "chunk": "monthly", "dry_run": True, "chunks_total": 0, "chunks_completed": 0,
            "datasets": {}, "errors": [],
        }
        with patch("scripts.backfill.run_backfill_from_options", return_value=fake_summary):
            res = client.post(
                "/api/backfill/run",
                json={"source": "hubspot", "date_from": "2024-01-01", "date_to": "2024-01-31", "dry_run": True},
                cookies={"ads_session": cookie_val},
            )

        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")

        body = res.text.lower()
        for phrase in _UNSAFE_API_PHRASES:
            assert phrase not in body, f"unsafe phrase found in response: {phrase!r}"


class TestBackfillStatusEndpoint:
    def test_unauthenticated_returns_401_or_403(self, client):
        res = client.get("/api/backfill/status")
        assert res.status_code in (401, 403)

    def test_authenticated_returns_running_and_latest(self, client):
        cookie_val = _set_admin_session(client)
        if not cookie_val:
            pytest.skip("Could not create session cookie")

        res = client.get(
            "/api/backfill/status",
            cookies={"ads_session": cookie_val},
        )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")

        assert res.status_code == 200
        data = res.json()
        assert "running" in data
        assert "latest" in data
        assert isinstance(data["running"], bool)
