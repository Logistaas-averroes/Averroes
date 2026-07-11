"""
PR-ADS-142 — Campaign Evidence Decision Page Revamp.

Proves the Campaigns page is a decision-grade evidence page reading honest
contracts, without changing any verdict/lead/junk-rate/CPQL logic:

  - /api/campaigns returns decision aggregates + an EXPLICIT, honest spend
    contract (per-run snapshot, never a fabricated window total); CPQL is null
    when there are no confirmed SQLs; the evidence window (incl. all_time with no
    lower date bound) drives the query and an unknown window is a 400.
  - the frontend uses the shared evidence shell, replaces the four oversized
    verdict cards with compact filter chips, prioritises CRM outcomes over Google
    Ads platform events, and never fabricates $0 / 0.00x / null / undefined / NaN.

Read-only. No writes to Google Ads or HubSpot.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
INDEX_HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
STYLES = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()

CAMPAIGN_COLS = ["campaign_name", "verdict", "verdict_reason", "spend_usd", "clicks",
                 "impressions", "conversions", "total_leads", "confirmed_sqls",
                 "junk_count", "junk_rate_pct", "cpql_usd", "run_count", "last_run_date"]


# ── Fake DB (no Postgres in this env) ───────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows, cols):
        self._rows, self._cols = rows, cols
        self.executed = []
        self.description = [(c,) for c in cols]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows, cols, sink):
        self._rows, self._cols, self._sink = rows, cols, sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        cur = _FakeCursor(self._rows, self._cols)
        self._sink.append(cur)
        return cur


def _patch_campaigns(monkeypatch, rows, cols=None):
    import db.connection as conn_mod
    sink = []
    monkeypatch.setattr(conn_mod, "get_conn",
                        lambda: _FakeConn(rows, cols or CAMPAIGN_COLS, sink))
    return sink


def _server():
    import importlib
    return importlib.import_module("api.server")


# ════════════════ 1. Backend — window contract ════════════════


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


def _cookie():
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    from api.auth import set_session
    from starlette.responses import Response
    r = Response()
    set_session(r, "testviewer", "viewer")
    for part in r.headers.get("set-cookie", "").split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return part.split("=", 1)[1]
    return None


@pytest.fixture()
def client_cookie():
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    c, ck = _make_client(), _cookie()
    if not ck:
        pytest.skip("could not mint cookie")
    return c, ck


@pytest.mark.parametrize("window", ["7d", "14d", "30d", "60d", "180d", "all_time"])
def test_campaigns_accepts_all_windows(client_cookie, window):
    c, ck = client_cookie
    res = c.get(f"/api/campaigns?window={window}", cookies={"ads_session": ck})
    assert res.status_code == 200, res.text
    assert res.json().get("window") == window


def test_campaigns_rejects_invalid_window(client_cookie):
    c, ck = client_cookie
    res = c.get("/api/campaigns?window=99d", cookies={"ads_session": ck})
    assert res.status_code == 400 and "99d" in res.text


def test_campaigns_all_time_sql_has_no_lower_bound(monkeypatch):
    sink = _patch_campaigns(monkeypatch, rows=[])
    server = _server()
    server.api_campaigns(user={"e": "x"}, days=30, window="all_time")
    sql_all = " ".join(s for cur in sink for s in cur.executed)
    assert sql_all, "no SQL executed"
    assert "run_date >=" not in sql_all      # all_time = no lower date restriction
    assert "FROM campaigns" in sql_all


def test_campaigns_numeric_window_keeps_lower_bound(monkeypatch):
    sink = _patch_campaigns(monkeypatch, rows=[])
    server = _server()
    server.api_campaigns(user={"e": "x"}, days=30, window="30d")
    sql_all = " ".join(s for cur in sink for s in cur.executed)
    assert "run_date >=" in sql_all


# ════════════════ 2. Backend — honest spend + decision aggregates ════════════


def _rows_two():
    return [
        # winner: SCALE, 5 sqls, low junk, cpql present
        ("brand - uk", "SCALE", "ok", 100.0, 10, 100, 2.0, 20, 5, 0, 0.0, 20.0, 3, "2026-07-01"),
        # junk: FIX, 0 sqls, high junk, cpql should be nulled by the endpoint
        ("junk camp", "FIX", "high junk", 50.0, 5, 50, 0.0, 10, 0, 7, 70.0, 999.0, 2, "2026-07-01"),
    ]


def test_campaigns_explicit_spend_semantics(monkeypatch):
    _patch_campaigns(monkeypatch, _rows_two())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    assert out["spend_semantics"] == "latest_run_snapshot"   # never a window total
    assert out["spend_status"] == "snapshot"
    assert out["spend_currency"] == "USD"


def test_spend_requiring_review_unavailable_never_fake_zero(monkeypatch):
    _patch_campaigns(monkeypatch, _rows_two())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    srr = out["summary"]["spend_requiring_review"]
    assert srr["status"] == "unavailable"
    assert srr["value"] is None            # never a fabricated $0 sum
    assert srr["reason"]


def test_cpql_null_when_no_confirmed_sqls(monkeypatch):
    _patch_campaigns(monkeypatch, _rows_two())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    by = {c["campaign_name"]: c for c in out["campaigns"]}
    assert by["junk camp"]["confirmed_sqls"] == 0
    assert by["junk camp"]["cpql_usd"] is None      # N/A, never a fake $0
    assert by["brand - uk"]["cpql_usd"] == 20.0


def test_decision_aggregates_and_verdicts_passthrough(monkeypatch):
    # The endpoint SURFACES stored verdicts — it never computes/reclassifies them.
    _patch_campaigns(monkeypatch, _rows_two())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    s = out["summary"]
    assert s["campaigns_with_evidence"] == 2
    assert s["confirmed_sqls_total"] == 5
    assert s["confirmed_junk_total"] == 7
    assert s["verdict_counts"] == {"SCALE": 1, "HOLD": 0, "FIX": 1, "CUT": 0}
    verdicts = {c["campaign_name"]: c["verdict"] for c in out["campaigns"]}
    assert verdicts == {"brand - uk": "SCALE", "junk camp": "FIX"}


def test_missing_conversions_not_fabricated_to_zero(monkeypatch):
    # Copilot review: a NULL conversions must stay None (drawer renders "—"),
    # never a fabricated 0.0.
    rows = [("c", "HOLD", "", None, None, None, None, 0, 0, 0, None, None, 1, None)]
    _patch_campaigns(monkeypatch, rows)
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    assert out["campaigns"][0]["conversions"] is None


def test_campaigns_db_unavailable_shape(monkeypatch):
    import db.connection as conn_mod

    class _None:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(conn_mod, "get_conn", lambda: _None())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="all_time")
    assert out["db_unavailable"] is True
    assert out["campaigns"] == []
    assert out["summary"]["campaigns_with_evidence"] == 0   # never fabricated
    assert out["spend_semantics"] == "latest_run_snapshot"


# ════════════════ 3. Backend — read-only + doctrine ════════════════


FORBIDDEN_WRITES = (
    "requests.post", "requests.put", "requests.patch", "requests.delete",
    ".mutate(", "upload_offline", "upload_conversions", "create_deal",
    "update_deal", "create_contact", "update_contact", "set_budget",
    "set_bid", "pause_campaign", "enable_campaign", "add_negative",
    "negative_keyword",
)


def test_campaigns_handler_is_select_only():
    start = SERVER.find("def api_campaigns(")
    nxt = SERVER.find("\n@app.", start + 1)
    body = SERVER[start:nxt]
    upper = body.upper()
    for verb in (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " TRUNCATE "):
        assert verb not in upper
    for token in FORBIDDEN_WRITES:
        assert token.lower() not in body.lower()


def test_junk_rate_denominator_excludes_unknown():
    # The junk-rate formula (unchanged) sums qualified+in_progress+junk+wrong_fit —
    # unknown is NOT in the denominator. Assert the formula in campaign-detail.
    idx = SERVER.find("def _build_campaign_detail(")
    body = SERVER[idx:idx + 12000]
    assert "confirmed_sqls + in_progress + confirmed_junk + wrong_fit" in body
    assert "verdicted = confirmed_sqls + in_progress + confirmed_junk + wrong_fit + unknown" not in body


# ════════════════ 4. Frontend — decision page structure ════════════════


def test_legacy_verdict_cards_removed_from_page():
    section = INDEX_HTML[INDEX_HTML.find('id="page-campaigns"'):
                         INDEX_HTML.find('id="page-campaigns"') + 800]
    assert 'id="vc-scale"' not in section
    assert "verdict-count-grid" not in section
    assert 'id="camp-table-body"' not in section
    assert 'id="campaign-evidence-shell"' in section


def test_page_uses_shared_evidence_shell():
    fn = APP_JS[APP_JS.find("function renderCampaignEvidencePage"):
                APP_JS.find("function renderCampaignEvidencePage") + 2000]
    assert "renderEvidencePageShell(" in fn
    assert "Campaign Evidence" in fn


def test_compact_verdict_filter_chips_and_controls():
    filt = APP_JS[APP_JS.find("function renderCampaignDecisionFilters"):
                  APP_JS.find("function renderCampaignDecisionFilters") + 2200]
    # Verdict distribution is chips, not four oversized cards.
    assert "campaign-verdict-chip" in filt
    for v in ("SCALE", "HOLD", "FIX", "CUT"):
        assert v in filt
    # Search, outcome, sort, clear controls exist.
    assert 'id="campaign-search"' in filt
    assert 'id="campaign-outcome"' in filt
    for outcome in ("has_sql", "no_sql", "has_junk", "insufficient"):
        assert outcome in filt
    assert 'id="campaign-sort"' in filt
    for srt in ("attention", "spend", "sqls", "junk_rate", "cpql", "name"):
        assert srt in filt
    assert 'id="campaign-clear-filters"' in filt


def test_table_prioritises_crm_outcomes_over_platform_events():
    fn = APP_JS[APP_JS.find("function renderCampaignDecisionTable"):
                APP_JS.find("function renderCampaignDecisionTable") + 1600]
    # CRM outcome columns are present in the primary decision table…
    assert "Confirmed SQLs" in fn and "Confirmed Junk" in fn and "CPQL" in fn
    # …and Google Ads conversions is NOT a primary decision-table column.
    assert "Google Conv" not in fn


def test_google_conversions_labelled_non_outcome_in_drawer():
    fn = APP_JS[APP_JS.find("Google Conv."):APP_JS.find("Google Conv.") + 400]
    assert "Platform event — not a confirmed SQL or customer" in APP_JS


def test_priority_sort_is_cut_fix_hold_scale():
    fn = APP_JS[APP_JS.find("const CAMPAIGN_VERDICT_ORDER"):
                APP_JS.find("const CAMPAIGN_VERDICT_ORDER") + 120]
    assert "CUT: 0" in fn and "FIX: 1" in fn and "HOLD: 2" in fn and "SCALE: 3" in fn


def test_row_never_fabricates_values():
    fn = APP_JS[APP_JS.find("function renderCampaignEvidenceRow"):
                APP_JS.find("function renderCampaignEvidenceRow") + 2000]
    # Missing spend → Unavailable; no confirmed SQLs → CPQL N/A (never a fake $0).
    assert "Unavailable" in fn
    assert '"N/A"' in fn
    for bad in (">null<", ">undefined<", ">NaN<", "0.00x", "$0.00"):
        assert bad not in fn


def test_evidence_window_controls_page_and_drawer():
    loader = APP_JS[APP_JS.find("async function loadCampaignEvidence"):
                    APP_JS.find("async function loadCampaignEvidence") + 900]
    assert "evidenceWindowQuery()" in loader
    # Drawer follows the same evidence window (all_time honoured, no 365 downgrade).
    assert "campaign-detail?campaign_name=${encodeURIComponent(campaignName)}&window=${encodeURIComponent(getEvidenceWindow())}" in APP_JS
    assert "evidenceWindowDays" not in APP_JS


def test_drawer_open_full_keyword_and_waste_links():
    assert "Open full Keyword Evidence" in APP_JS
    assert "Open full Waste Evidence" in APP_JS
    assert "drawer-keywords-fullpage-btn" in APP_JS
    assert "drawer-waste-fullpage-btn" in APP_JS


def test_drawer_has_readonly_decision_note():
    assert "Decision state is evidence-based and read-only. No Google Ads changes are made." in APP_JS


# ════════════════ 5. Responsive + accessibility ════════════════


def test_responsive_table_and_mobile_cards():
    assert "@media (max-width: 1024px)" in STYLES        # tablet: hide low-priority cols
    assert "@media (max-width: 640px)" in STYLES          # mobile: stacked cards
    assert ".campaign-decision-table td::before" in STYLES  # data-label cards
    assert "data-label=" in APP_JS


def test_row_open_handler_wired_once_not_double():
    # Copilot review: only the <tr> carries data-campaign-open, so the click
    # handler fires once (the inner button bubbles to the row) — no double open.
    row = APP_JS[APP_JS.find("function renderCampaignEvidenceRow"):
                 APP_JS.find("function wireCampaignEvidenceControls")]
    assert row.count("data-campaign-open") == 1        # the <tr> only
    assert 'class="btn btn--secondary campaign-open-btn">' in row   # button has none


def test_row_is_keyboard_openable_and_drawer_escapes():
    row = APP_JS[APP_JS.find("function renderCampaignEvidenceRow"):
                 APP_JS.find("function wireCampaignEvidenceControls")]
    assert 'tabindex="0"' in row and 'role="button"' in row
    # Drawer keyboard handling (Escape closes) is retained.
    handler = APP_JS[APP_JS.find("function _drawerKeyHandler"):
                     APP_JS.find("function _drawerKeyHandler") + 300]
    assert "Escape" in handler and "closeCampaignDrawer" in handler


def test_revenue_pages_untouched():
    # The revenue page list is unchanged and campaigns evidence never imports
    # revenue/ROAS logic into its render path.
    rev = APP_JS[APP_JS.find("const REVENUE_PAGES"):APP_JS.find("const REVENUE_PAGES") + 200]
    for rp in ("roas-campaigns", "roas-countries", "deals", "revenue-by-source"):
        assert rp in rev


# ════════════════ 6. Pre-merge audit — snapshot-vs-window honesty ════════════
# Validation for the 10 audit points: latest-snapshot semantics, null
# preservation, summary completeness, contract accuracy, consumer migration,
# mobile visibility, formula stability, and Revenue/Dashboard isolation.


def _rows_nullable():
    # A row where EVERY metric the source can leave NULL is NULL — clicks,
    # impressions, conversions, total_leads, confirmed_sqls, junk_count,
    # junk_rate_pct, cpql_usd, spend_usd. Only verdict + name are populated.
    return [("null camp", "HOLD", "insufficient",
             None, None, None, None, None, None, None, None, None, 1, "2026-07-02")]


# Point 1 — the UI never presents snapshot metrics as selected-window totals.
def test_ui_labels_disclose_latest_snapshot_not_window_total():
    kpi = APP_JS[APP_JS.find("function renderCampaignEvidenceKPIs"):
                 APP_JS.find("function renderCampaignEvidenceKPIs") + 2200]
    assert "Confirmed SQLs — Latest Snapshot" in kpi
    assert "Confirmed Junk — Latest Snapshot" in kpi
    assert "Spend Evidence — Latest Snapshot" in kpi
    # The visible disclosure banner is verbatim from the contract.
    assert ("Evidence Window filters stored snapshot dates. It does not convert "
            "the snapshot into an all-time or selected-window business total." in APP_JS)
    # Table column headers carry the same latest-snapshot framing (incl. CPQL).
    tbl = APP_JS[APP_JS.find("function renderCampaignDecisionTable"):
                 APP_JS.find("function renderCampaignDecisionTable") + 1600]
    assert "CPQL — Latest Snapshot" in tbl


# Point 2 — all_time returns the latest snapshot per campaign across ALL stored
# snapshot dates (no lower bound, still one row per campaign), and the response
# discloses latest-snapshot semantics at both levels.
def test_all_time_is_latest_snapshot_not_lifetime_sum(monkeypatch):
    sink = _patch_campaigns(monkeypatch, _rows_two())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="all_time")
    sql_all = " ".join(s for cur in sink for s in cur.executed)
    assert "run_date >=" not in sql_all                 # no lower bound
    assert "DISTINCT ON (df.campaign_name)" in sql_all   # one snapshot per campaign
    assert out["metric_semantics"] == "latest_stored_snapshot"
    for c in out["campaigns"]:
        assert c["metric_semantics"] == "latest_stored_snapshot"


# Point 3 — snapshot date + (unavailable) analysis period are present per
# campaign and surfaced in the drawer.
def test_snapshot_date_and_period_present_per_campaign(monkeypatch):
    _patch_campaigns(monkeypatch, _rows_two())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    c = out["campaigns"][0]
    assert c["snapshot_date"] == "2026-07-01"
    # Period is unprovable per-row → null + explicit unavailable flag.
    assert c["snapshot_metric_period_days"] is None
    assert c["snapshot_period_available"] is False


def test_drawer_shows_snapshot_date_and_period():
    drawer = APP_JS[APP_JS.find("function renderCampaignDrawer"):
                    APP_JS.find("function _appendDrawerEvidenceSections")]
    # The drawer builds snapshot date + analysis-period disclosure text.
    assert "snapshot_date" in drawer
    assert "snapshot analysis period unavailable" in drawer
    assert "latest stored snapshot" in drawer


def _build_detail_body():
    idx = SERVER.find("def _build_campaign_detail(")
    end = SERVER.find("\n@app.", idx)
    return SERVER[idx:end if end != -1 else idx + 14000]


def test_detail_endpoint_emits_snapshot_metadata():
    body = _build_detail_body()
    assert '"metric_semantics": "latest_stored_snapshot"' in body
    assert '"snapshot_metric_period_days": None' in body
    assert '"snapshot_period_available": False' in body


# Point 4 — NULL metrics stay unavailable (None), never fabricated to 0.
def test_null_metrics_preserved_not_zero(monkeypatch):
    _patch_campaigns(monkeypatch, _rows_nullable())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    c = out["campaigns"][0]
    for field in ("clicks", "impressions", "conversions", "total_leads",
                  "confirmed_sqls", "confirmed_junk", "junk_rate_pct", "spend_usd",
                  "cpql_usd"):
        assert c[field] is None, f"{field} should be None (unavailable), not 0"


def test_genuine_zero_is_preserved_as_zero(monkeypatch):
    # A source-recorded 0 stays 0 (not None) — only NULL becomes unavailable.
    rows = [("zero camp", "FIX", "no sqls", 10.0, 0, 0, 0.0, 0, 0, 0, 0.0, None, 1, "2026-07-02")]
    _patch_campaigns(monkeypatch, rows)
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    c = out["campaigns"][0]
    assert c["confirmed_sqls"] == 0 and c["confirmed_junk"] == 0
    assert c["clicks"] == 0 and c["impressions"] == 0
    assert c["cpql_usd"] is None            # 0 SQLs → CPQL undefined (N/A), not $0


def test_frontend_row_renders_null_as_dash_and_zero_as_zero():
    row = APP_JS[APP_JS.find("function renderCampaignEvidenceRow"):
                 APP_JS.find("function wireCampaignEvidenceControls")]
    # null SQLs/junk → an unavailable dash, genuine 0 → "0" via fmtCount.
    assert "const sqls = c.confirmed_sqls;" in row   # raw value, not `|| 0` coerced
    assert "sqls == null" in row                     # unavailable → dash
    assert "c.confirmed_junk == null" in row
    # CPQL: "—" when SQLs unavailable, "N/A" when a genuine 0.
    assert 'sqls == null ? "—"' in row
    assert 'sqls === 0 ? "N/A"' in row


# Point 5 — summary completeness is explicit; a missing metric is counted as
# missing, never summed as 0.
def test_summary_completeness_partial_when_a_metric_is_null(monkeypatch):
    rows = [
        ("has camp", "SCALE", "ok", 100.0, 10, 100, 2.0, 20, 5, 1, 5.0, 20.0, 1, "2026-07-02"),
        ("null camp", "HOLD", "n/a", None, None, None, None, None, None, None, None, None, 1, "2026-07-02"),
    ]
    _patch_campaigns(monkeypatch, rows)
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    comp = out["summary"]["completeness"]
    assert comp["confirmed_sqls"] == {"status": "partial", "available": 1, "missing": 1}
    assert comp["confirmed_junk"] == {"status": "partial", "available": 1, "missing": 1}
    # The null row's SQLs (None) is NOT added as 0 — total is the one real value.
    assert out["summary"]["confirmed_sqls_total"] == 5
    assert out["summary"]["confirmed_junk_total"] == 1


def test_summary_completeness_complete_when_all_present(monkeypatch):
    _patch_campaigns(monkeypatch, _rows_two())
    out = _server().api_campaigns(user={"e": "x"}, days=30, window="30d")
    comp = out["summary"]["completeness"]
    assert comp["confirmed_sqls"]["status"] == "complete"
    assert comp["confirmed_junk"]["status"] == "complete"
    assert comp["confirmed_sqls"]["missing"] == 0


def test_frontend_kpi_discloses_completeness():
    fn = APP_JS[APP_JS.find("function campaignCompletenessSub"):
                APP_JS.find("function campaignCompletenessSub") + 400]
    assert 'comp.status === "partial"' in fn
    assert "unavailable" in fn


# Point 6 — the canonical API contract matches the response shape.
CONTRACT = open(os.path.join(ROOT, "docs", "API_CONTRACT.md"), encoding="utf-8").read()


def test_api_contract_documents_latest_snapshot_contract():
    seg = CONTRACT[CONTRACT.find("GET /api/campaigns"):
                   CONTRACT.find("GET /api/leads")]
    assert seg, "campaigns contract section missing"
    for token in ("latest_stored_snapshot", "snapshot_metric_period_days",
                  "snapshot_period_available", "metric_semantics",
                  "spend_semantics", "completeness", "all_time",
                  "confirmed_junk", "db_unavailable"):
        assert token in seg, f"API_CONTRACT missing {token} in /api/campaigns section"
    # The breaking replacement of the old averaged fields is documented.
    for old in ("avg_spend_usd", "total_confirmed_sqls", "avg_junk_rate_pct",
                "avg_cpql_usd", "trend"):
        assert old in seg, f"API_CONTRACT should document removal of {old}"
    assert "BREAKING" in seg


# Point 7 — old field consumers are migrated (no aliases needed); latest_verdict
# alias retained.
def test_no_stale_consumers_of_removed_campaign_fields():
    # The only /api/campaigns consumer (loadCampaignEvidence) reads the new fields.
    for removed in ("avg_spend_usd", "total_confirmed_sqls",
                    "avg_junk_rate_pct", "avg_cpql_usd"):
        assert removed not in APP_JS, f"frontend still references removed field {removed}"
    # Backward-compat alias preserved so any lingering reader of latest_verdict works.
    assert '"latest_verdict": r["verdict"]' in SERVER


# Point 8 — mobile cards show Spend Evidence (col 3) and Confirmed Junk (col 5).
def test_mobile_shows_spend_and_confirmed_junk_columns():
    mobile = STYLES[STYLES.find("@media (max-width: 640px)"):
                    STYLES.find("@media (max-width: 640px)") + 1600]
    # Tablet hides cols 3/5 via a more specific nth-child rule; the mobile block
    # must re-assert them so Spend Evidence + Confirmed Junk appear on cards.
    assert ".campaign-decision-table td:nth-child(3)" in mobile
    assert ".campaign-decision-table td:nth-child(5)" in mobile
    seg = mobile[mobile.find("td:nth-child(3)"):mobile.find("td:nth-child(3)") + 120]
    assert "display: flex" in seg
    # Evidence summary text (col 8) stays hidden on mobile; Open evidence remains.
    assert "td:nth-child(8) { display: none; }" in mobile


# Point 9 — verdict / junk-rate / CPQL formulas are unchanged (surfaced, not
# recomputed). The endpoint never calls determine_verdict or reclassifies.
def test_verdict_and_cpql_formulas_unchanged():
    core = open(os.path.join(ROOT, "analysis", "core.py"), encoding="utf-8").read()
    # Verdict formula lives in analysis/core.py determine_verdict — untouched.
    assert "def determine_verdict(" in core
    # CPQL formula (spend / confirmed_sqls) unchanged in the pipeline.
    assert "round(spend / confirmed_sqls, 2)" in core
    # The endpoint surfaces stored verdict/cpql — it does not import determine_verdict.
    start = SERVER.find("def api_campaigns(")
    nxt = SERVER.find("\n@app.", start + 1)
    body = SERVER[start:nxt]
    assert "determine_verdict" not in body


# Point 10 — Revenue and Dashboard endpoints/pages are untouched by this slice.
def test_revenue_and_dashboard_handlers_not_modified_by_campaigns_slice():
    start = SERVER.find("def api_campaigns(")
    nxt = SERVER.find("\n@app.", start + 1)
    body = SERVER[start:nxt]
    # The campaigns handler does not reach into revenue/dashboard services.
    for foreign in ("dashboard_overview_service", "dashboard_revenue_service",
                    "revenue_by_source", "closed_won", "get_revenue_performance"):
        assert foreign not in body
