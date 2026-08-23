"""
PR-ADS-132 — ROAS by Country Client Drilldown Drawer.

A validation-only drawer: each REAL country row on ROAS by Country can expand to
show the closed-won clients / deals behind it (company / contact / deal record
ids, amount, close date, campaign attribution), served by a read-only endpoint.

Core guarantees exercised here:
  - endpoint + service + repository function exist and are wired;
  - the country detail query is READ-ONLY and windowed by deal_close_date;
  - country matching is EXACT (ISO code when known, else exact normalized name) —
    "United Arab Emirates" NEVER leaks into "United Kingdom", no `contains`;
  - a missing deal amount stays None (UI: "unavailable"), never a fabricated $0;
  - a contact record id is returned only when stored, never fabricated;
  - the residual ("Unknown / Unattributed country") row has NO expand drawer;
  - the drawer renders the validation columns and never shows undefined/null;
  - no Google Ads / HubSpot writes; no spend/ROAS/residual/FX/mart math changes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.canonical_ledger_fixtures import (  # noqa: E402
    from_legacy_deal_rows, patch_canonical_ledger,
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
REPO_SRC = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
SERVICE_SRC = open(os.path.join(ROOT, "services", "revenue_attribution_service.py"),
                   encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()


def _slice(hay, marker, span=2600):
    i = hay.find(marker)
    assert i != -1, f"{marker} not found"
    return hay[i:i + span]


def _repo_fn_body(name):
    i = REPO_SRC.find(f"def {name}")
    assert i != -1, f"{name} not found in revenue_repository.py"
    j = REPO_SRC.find("\ndef ", i + 1)
    return REPO_SRC[i:(j if j != -1 else len(REPO_SRC))]


def _service_fn_body(name):
    i = SERVICE_SRC.find(f"def {name}")
    assert i != -1, f"{name} not found in revenue_attribution_service.py"
    j = SERVICE_SRC.find("\ndef ", i + 1)
    return SERVICE_SRC[i:(j if j != -1 else len(SERVICE_SRC))]


# ── DB fake so the REAL repository matching logic can be exercised without a DB ──

class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return None


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCur()


def _patch_db(monkeypatch, rows, *, available=True):
    """PR-ADS-153E-B: the country drilldown reads the CANONICAL deal ledger.

    The legacy fake-cursor plumbing is kept for the repository SQL guards below
    (which still assert `fetch_country_deal_details` is read-only and matches
    countries exactly), but the service itself is stubbed at the canonical read.
    """
    import db.revenue_repository as repo
    monkeypatch.setattr(repo, "get_conn", lambda: _FakeConn())
    monkeypatch.setattr(repo, "_rows_as_dicts", lambda cur: [dict(r) for r in rows])
    patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(rows),
                           available=available)


# Deal rows as stored in gclid_attribution (country is a NAME; no code column).
DB_ROWS = [
    {"deal_id": "987654", "contact_id": "123456", "gclid": "Cj0KCQmexicogclid",
     "company": "Example Logistics", "country": "Mexico",
     "campaign_name": "mexico,chile", "deal_close_date": "2026-05-18",
     "deal_amount_usd": 51400.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
    {"deal_id": "111222", "contact_id": None, "gclid": None,
     "company": "UAE Freight Co", "country": "United Arab Emirates",
     "campaign_name": "Gulf", "deal_close_date": "2026-04-02",
     "deal_amount_usd": None, "deal_stage_label": "Closed Won",
     "match_status": "url_fallback", "match_source": "first_url"},
    {"deal_id": "333444", "contact_id": "999", "gclid": None,
     "company": "UK Shippers Ltd", "country": "United Kingdom",
     "campaign_name": "Europe", "deal_close_date": "2026-03-10",
     "deal_amount_usd": 8000.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
]


def _patch_identity(monkeypatch):
    import db.revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e, *_a, **_k: {
        "available": True, "customer_id": "111",
        "rows": [{"campaign_name": "Mexico, Chile, Colombia"},
                 {"campaign_name": "Gulf"}, {"campaign_name": "Europe"}]})
    monkeypatch.setattr(repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})


# ══════════════ Test 1 — backend endpoint / service / repo exist ══════════════

def test_backend_endpoint_service_repo_exist():
    assert '@app.get("/api/revenue-performance/country-detail")' in SERVER
    assert "build_country_deal_details" in SERVICE_SRC
    assert "def fetch_country_deal_details" in REPO_SRC
    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/revenue-performance/country-detail" in routes


# ══════════════ Test 2 — the country detail query is read-only ══════════════

def test_country_detail_query_is_read_only():
    body = _repo_fn_body("fetch_country_deal_details").lower()
    for forbidden in ("insert", "update ", "delete", "upsert", "drop", "alter",
                      "requests.post", "requests.patch", "requests.put"):
        assert forbidden not in body, f"country detail query must be read-only (found '{forbidden}')"


# ══════════════ Test 3 — windowed by closed-won deal_close_date ══════════════

def test_windowed_by_deal_close_date():
    body = _repo_fn_body("fetch_country_deal_details")
    # The date WINDOW predicate uses the real close date.
    assert "deal_close_date >=" in body
    assert "deal_close_date <" in body
    # Never windowed by the scheduler run date or a sync date.
    assert "run_date" not in body
    assert "sync_date" not in body


# ══════════════ Test 4 — exact country identity, no fuzzy / contains ══════════════

def test_exact_country_identity_no_fuzzy():
    # Backend supports both an ISO code and a country name.
    assert "country_code" in _repo_fn_body("fetch_country_deal_details")
    assert "country_code" in _service_fn_body("build_country_deal_details")
    assert "country_code" in SERVER  # endpoint exposes the safer param
    # Matching resolves an ISO code (safe) and never uses a fuzzy/contains match.
    body = _repo_fn_body("fetch_country_deal_details")
    assert "get_country_code" in body
    lower = body.lower()
    assert "country ilike" not in lower
    assert "country like" not in lower
    assert ".contains(" not in lower


def test_country_code_prevents_uae_leaking_into_uk(monkeypatch):
    _patch_db(monkeypatch, DB_ROWS)
    _patch_identity(monkeypatch)
    from services.revenue_attribution_service import build_country_deal_details
    # Requesting the UAE (AE) must return ONLY the UAE deal — never the UK one.
    out = build_country_deal_details("ytd", "United Arab Emirates", "AE")
    ids = {d["deal_record_id"] for d in out["details"]}
    assert ids == {"111222"}, "UAE must not match United Kingdom"
    assert out["country_code"] == "AE"

    uk = build_country_deal_details("ytd", "United Kingdom", "GB")
    assert {d["deal_record_id"] for d in uk["details"]} == {"333444"}


def test_country_name_only_match_is_exact(monkeypatch):
    _patch_db(monkeypatch, DB_ROWS)
    _patch_identity(monkeypatch)
    from services.revenue_attribution_service import build_country_deal_details
    # No code supplied → exact normalized-name match (Mexico resolves to just Mexico).
    out = build_country_deal_details("ytd", "Mexico")
    assert {d["deal_record_id"] for d in out["details"]} == {"987654"}


def test_name_only_path_is_exact_never_contains(monkeypatch):
    """A label that is not a canonical country drills into NOTHING.

    PR-ADS-153F changed the contract this test guards, and strengthened it.
    Before, a country with no ISO code was matched by exact normalized name, so
    "Narnia" returned its own deals and a near-miss ("Narnian Republic") was
    correctly excluded.

    Neither label is a supported country now, so both resolve to the ONE residual
    bucket — and answering a request for "Narnia" with the residual's contents
    would hand back "Narnian Republic"'s deals under Narnia's name, which is the
    very leak this test exists to prevent, just by a different route. The
    drilldown therefore refuses: no details, and an explicit reason saying the
    requested country is not canonical.

    The residual is still fully auditable — it is reachable by asking for it by
    name, which the test below proves.
    """
    rows = [
        {"deal_id": "n1", "contact_id": "c", "gclid": None, "company": "Co A",
         "country": "Narnia", "campaign_name": "x", "deal_close_date": "2026-05-01",
         "deal_amount_usd": 10.0, "deal_stage_label": "Closed Won",
         "match_status": "matched", "match_source": "gclid"},
        {"deal_id": "n2", "contact_id": "c", "gclid": None, "company": "Co B",
         "country": "Narnian Republic", "campaign_name": "x", "deal_close_date": "2026-05-01",
         "deal_amount_usd": 20.0, "deal_stage_label": "Closed Won",
         "match_status": "matched", "match_source": "gclid"},
    ]
    _patch_db(monkeypatch, rows)
    _patch_identity(monkeypatch)
    from services.revenue_attribution_service import build_country_deal_details
    out = build_country_deal_details("ytd", "Narnia")
    assert out["details"] == []
    assert out["source_health"]["status"] == "country_not_canonical"
    assert out["country_status"] == "invalid"
    # Above all: the near-miss did not leak in.
    assert "n2" not in {d.get("deal_record_id") for d in out["details"]}

    # The residual bucket itself IS reachable, and returns both unidentifiable
    # deals — that is what makes the residual row's totals auditable.
    from analysis.country_identity import RESIDUAL_LABEL
    residual = build_country_deal_details("ytd", RESIDUAL_LABEL)
    assert {d["deal_record_id"] for d in residual["details"]} == {"n1", "n2"}


def test_name_only_path_pushes_exact_filter_into_sql():
    # PR-ADS-132 follow-up (review): the name-only path filters in SQL by an EXACT
    # normalized name (btrim + lower + collapsed whitespace) — not a LIKE/contains —
    # so an on-demand drilldown never scans every closed-won row in Python.
    body = _repo_fn_body("fetch_country_deal_details")
    assert "regexp_replace(btrim(country)" in body
    assert "name_only" in body
    low = body.lower()
    assert "country like" not in low and "country ilike" not in low


# ══════════════ Test 5 — a missing amount stays None, never $0 ══════════════

def test_missing_amount_is_null_not_zero(monkeypatch):
    _patch_db(monkeypatch, DB_ROWS)
    _patch_identity(monkeypatch)
    from services.revenue_attribution_service import build_country_deal_details
    out = build_country_deal_details("ytd", "United Arab Emirates", "AE")
    d = next(x for x in out["details"] if x["deal_record_id"] == "111222")
    assert d["deal_amount_usd"] is None      # NOT 0.0 — the UI shows "unavailable"
    assert d["deal_amount_usd"] != 0.0
    # The frontend drawer renders a null amount as "unavailable", not $0.00.
    assert "d.deal_amount_usd == null" in JS


# ══════════════ Test 6 — contact id only when stored, never fabricated ══════════════

def test_contact_id_only_when_stored(monkeypatch):
    _patch_db(monkeypatch, DB_ROWS)
    _patch_identity(monkeypatch)
    from services.revenue_attribution_service import build_country_deal_details
    mexico = build_country_deal_details("ytd", "Mexico")["details"][0]
    assert mexico["main_contact_record_id"] == "123456"   # stored contact_id surfaced
    uae = build_country_deal_details("ytd", "United Arab Emirates", "AE")["details"][0]
    assert uae["main_contact_record_id"] is None          # not stored → None, not faked
    # A contact NAME is never stored, so it is always None (renders "unavailable").
    assert mexico["main_contact_name"] is None
    # Company record id is only a name in durable data → id is None, never fabricated.
    assert mexico["company_record_id"] is None
    # PR-ADS-153E-B: the canonical ledger names the DEAL, not a company, so the
    # company columns are Unavailable and the deal name is what is shown.
    assert mexico["company_name"] is None
    assert mexico["deal_name"] == "Example Logistics"


def test_gclid_is_masked_never_full(monkeypatch):
    _patch_db(monkeypatch, DB_ROWS)
    _patch_identity(monkeypatch)
    from services.revenue_attribution_service import build_country_deal_details
    mexico = build_country_deal_details("ytd", "Mexico")["details"][0]
    assert mexico["gclid_masked"]
    assert mexico["gclid_masked"] != "Cj0KCQmexicogclid"   # never the full gclid


def test_db_unavailable_is_reported(monkeypatch):
    _patch_db(monkeypatch, [], available=False)
    from services.revenue_attribution_service import build_country_deal_details
    out = build_country_deal_details("ytd", "Mexico", "MX")
    assert out["details"] == []
    # PR-ADS-153E-B: the reason names the canonical read, and nothing falls back.
    assert out["revenue_available"] is False
    assert out["legacy_fallback_used"] is False
    assert out["source_health"]["status"] == "canonical_ledger_unreadable"


# ══════════════ Test 7 — frontend has an expandable country row ══════════════

def test_frontend_has_expandable_country_row():
    assert "data-country-expand" in JS
    assert "country-drilldown-row" in JS
    assert "function loadCountryDrilldown" in JS
    assert "/api/revenue-performance/country-detail?window=" in JS
    # The country key + lazy-load helpers named in the spec exist.
    assert "function countryDrilldownKey" in JS
    assert "function renderCountryDrilldown" in JS


# ══════════════ Test 8 — the residual row has no drawer ══════════════

def test_residual_row_has_no_drawer():
    table = _slice(JS, "function renderRoasCountryTable", span=3200)
    # The render branches on is_residual; only the real-country branch invokes the
    # expand control + emits the drilldown row.
    assert "r.is_residual" in table
    resid = table.find("r.is_residual")
    ctrl = table.find("countryExpandControl(r, i)")
    assert ctrl > resid > -1, "expand control must be on the real-country branch"
    # The residual branch (is_residual → the real-row expand control) never emits
    # an expand button or a drilldown drawer row.
    residual_branch = table[resid:ctrl]
    assert "country-expand" not in residual_branch
    assert "country-drilldown-row" not in residual_branch


# ══════════════ Test 9 — the drawer renders the validation columns ══════════════

def test_drawer_renders_validation_columns():
    fn = _slice(JS, "function renderCountryDrilldown", span=2400)
    assert "Clients / Deals Behind This Country" in fn
    for col in (">Company<", ">Company ID<", ">Main Contact<", ">Contact ID<",
                ">Deal<", ">Deal ID<", ">Amount<", ">Close Date<",
                ">Campaign / Source<", ">Attribution<"):
        assert col in fn, f"drilldown table missing column {col}"
    # States required by the spec.
    assert "No attributed closed-won client detail found for this country in this window." in fn
    assert "Loading country client details" in JS
    assert "Country client details could not be loaded." in JS


# ══════════════ Test 10 — no undefined / null text in the drawer ══════════════

def test_no_undefined_or_null_text():
    fn = _slice(JS, "function renderCountryDrilldownRow", span=1400)
    helper = _slice(JS, "function drilldownValue", span=400)
    assert "unavailable" in helper
    # Every value routes through the safe formatter — no raw undefined/null.
    assert "drilldownValue(" in fn
    assert "undefined" not in fn
    assert " null<" not in fn and ">null<" not in fn


def test_css_classes_present():
    for cls in (".country-expand-button", ".country-drilldown-row",
                ".country-drilldown-panel", ".country-drilldown-table",
                ".country-drilldown-empty", ".country-drilldown-muted"):
        assert cls in CSS, f"missing style {cls}"


# ══════════════ Test 11 — scope protection (no math / write changes) ══════════════

def test_scope_protection_no_writes_or_math_changes():
    # The new country-detail code must not INVOKE the spend/ROAS/residual/FX/mart
    # truth engines (it only reads durable deal rows) or perform any platform write.
    # These are call-site tokens, so an explanatory comment mentioning a concept
    # (e.g. "residual is never matched") does not trip the guard.
    for body in (_repo_fn_body("fetch_country_deal_details"),
                 _service_fn_body("build_country_deal_details"),
                 _service_fn_body("_country_deal_detail_row")):
        low = body.lower()
        for forbidden in ("evaluate_country_residual", "country_spend_tolerance",
                          "country_residual_tolerance", "fx_effective_rate",
                          "build_revenue_decision_mart", "compute_roas(",
                          "set_budget", "set_bid", ".mutate(",
                          "requests.post", "requests.put", "requests.patch"):
            assert forbidden not in low, f"country detail code must not reference '{forbidden}'"


def test_no_unsafe_writes_in_touched_files():
    files = [
        "static/app.js",
        "services/revenue_attribution_service.py",
        "db/revenue_repository.py",
        "api/server.py",
    ]
    for rel in files:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
        for forbidden in (".mutate(", "upload_offline", "offline_conversion",
                          "requests.post", "requests.put", "requests.patch",
                          "requests.delete", "set_budget", "set_bid",
                          "pause_campaign", "enable_campaign"):
            assert forbidden not in src, f"{rel} must not reference '{forbidden}'"
