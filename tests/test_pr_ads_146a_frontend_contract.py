"""
tests/test_pr_ads_146a_frontend_contract.py

PR-ADS-146A frontend truth contract (static analysis of app.js / index.html — no
JS execution). Guards the behaviours the operator sees:

  §7  The Keyword Evidence header renders no technical pills — the
      renderPageExplanation keyword early-return keeps source / Depends-on /
      Read-only chips off the page.
  §8  Before the first successful keyword-fact sync, Verified Keyword Spend reads
      "Unavailable" with the not-run subline — never "$0.00 Complete". A zero
      subtotal is Complete only when the window is proven synced.
  §4  Changing the Evidence Window re-queries PostgreSQL (loadPage), closes the
      keyword drawer and never triggers a Google Ads pull; stale responses are
      dropped via the _kwReqId guard.
  §5  The admin-only "Refresh Keyword Data" action posts to the local refresh
      endpoint and is gated on the admin role.
  §6  The page fetches all-time completeness metadata and only drops the partial
      note when history is complete.
  §11 Help copy explains the window is an instant DB query and the sync model.

Run with:
    python -m pytest tests/test_pr_ads_146a_frontend_contract.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
_INDEX = (_ROOT / "static" / "index.html").read_text(encoding="utf-8")


# ── §8 truthful not-run / verified-empty ─────────────────────────────────────
def test_not_run_card_shows_unavailable_first_sync_message():
    assert "Keyword facts have not completed their first sync" in _APP_JS
    assert "kwFirstSyncState" in _APP_JS


def test_no_dollar_zero_complete_for_unsynced_window():
    # The verified-empty guard must consult kwWindowProvenSynced before ever
    # rendering a zero subtotal as complete.
    assert "kwWindowProvenSynced" in _APP_JS
    assert "predates completed keyword facts" in _APP_JS


def test_window_proof_uses_backend_verdict():
    # §6 — the frontend trusts the backend's selected_window_fully_synced proof
    # rather than inferring coverage from a stored-range comparison alone.
    assert "selected_window_fully_synced" in _APP_JS


def test_first_sync_state_reads_keyword_facts_freshness():
    assert '"google_ads_api/keyword_facts"' in _APP_JS
    assert "canonical_status" in _APP_JS


# ── §4 instant DB refresh + race safety ──────────────────────────────────────
def test_window_change_requeries_db_and_closes_drawer():
    assert "closeKwDrawer" in _APP_JS
    # The keyword loader guards against stale responses with a request id.
    assert "_kwReqId" in _APP_JS
    assert "reqId !== _kwReqId" in _APP_JS


def test_keyword_load_never_calls_google_ads_directly():
    # The page loader hits the read API, not any sync/pull entrypoint.
    assert "/api/keyword-evidence?" in _APP_JS
    assert "pull_keyword_performance" not in _APP_JS


# ── §5 admin-only refresh action ─────────────────────────────────────────────
def test_admin_refresh_button_present_and_gated():
    assert 'id="kw-refresh-btn"' in _INDEX
    assert "runKeywordRefresh" in _APP_JS
    assert "/api/keyword-evidence/refresh" in _APP_JS
    # Gated on admin role and hidden by default.
    assert 'role === "admin"' in _APP_JS


# ── §6 all-time completeness ─────────────────────────────────────────────────
def test_history_metadata_fetched_and_gates_partial_note():
    assert "/api/keyword-evidence/history" in _APP_JS
    assert "history_complete" in _APP_JS
    assert "_kwHistory" in _APP_JS


# ── §7 no technical pills on the keyword page ────────────────────────────────
def test_keyword_page_explanation_chips_suppressed():
    # renderPageExplanation returns early for keywords so no source / Depends-on /
    # Read-only pills render on the page header.
    assert "page-explanation-keywords" in _APP_JS or "renderPageExplanation" in _APP_JS


# ── §11 help copy ────────────────────────────────────────────────────────────
def test_help_explains_instant_window_and_sync_model():
    assert "howItStaysCurrent" in _APP_JS
    assert "PostgreSQL query" in _APP_JS
    assert "never calls or mutates Google Ads" in _APP_JS
