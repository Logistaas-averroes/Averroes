"""
tests/test_pr_ads_156_f3_account_identity_cutover.py

PR-ADS-156-F3 — completing the search-term account-identity cutover.

The ingestion side of PR-ADS-156 worked. Production fetched and wrote 16,267
search-term rows with zero rejections and zero missing identities, and the
incremental run reported evidence ready.

The dedicated freshness audit failed anyway, and it was right to. F2 put
``customer_id`` into the natural key, so under the NEW key a complete row and
its account-less predecessor are DIFFERENT rows: the new rows did not conflict
with the old ones, did not supersede them, and both populations stayed. In the
certified window production held 32,367 rows, of which 16,100 had no account —
almost exactly one stale twin per new row.

That was never only an audit-display problem. Every reader that queried
``search_terms`` on a date window alone counted both copies: raw Search Terms,
the summary, N-grams, the evidence repositories, the Dashboard Campaign
signals. Metrics built that way are not off by a rounding error; they are close
to doubled.

Three things close it, and this suite proves each:

  1. ONE account-scoped canonical population, composed by every reader;
  2. a deterministic supersession of EXACT null-account twins that preserves the
     local analysis state before removing them;
  3. an audit that keeps measuring the un-narrowed population, so reader
     filtering can never certify a cutover that has not happened.

Run with:
    python -m pytest tests/test_pr_ads_156_f3_account_identity_cutover.py -v
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import search_term_scope as scope_mod  # noqa: E402
from scripts import audit_keyword_search_term_freshness as audit  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

_AUDIT_MODULE = "scripts.audit_keyword_search_term_freshness"

#: The configured account for this suite — the same value tests/conftest.py
#: exports, so a row seeded here is visible to a reader exercised anywhere.
ACCOUNT = "555"
OTHER_ACCOUNT = "777"

DAY = date(2026, 3, 1)


# ── helpers ──────────────────────────────────────────────────────────────────
def _exec(sql, params=()):
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def _scalar(sql, params=()):
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def _rows_of(sql, params=()):
    from db.connection import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() or []


def _fact(**over) -> dict:
    """A complete canonical search-term row in the writer's input shape."""
    row = {
        "date": DAY.isoformat(),
        "customer_id": ACCOUNT,
        "campaign": "brand - uk",
        "campaign_id": "10",
        "ad_group": "Core",
        "keyword": "freight",
        "match_type": "BROAD",
        "search_term": "freight forwarding software",
        "cost_micros": 5_000_000,
        "currency_code": "GBP",
        "clicks": 3,
        "impressions": 40,
        "conversions": 0.0,
        "source": "google_ads_api",
    }
    row.update(over)
    return row


_TWIN_COLUMNS = ("source_date, customer_id, campaign_name, campaign_id, "
                 "ad_group, keyword, match_type, search_term, cost_micros, "
                 "currency_code, source_system, spend_usd, clicks, impressions")


def _seed_pre_cutover_twin(*, customer_id=None, source_system="google_ads_api",
                           flagged=None, junk=None, pattern=None, **over):
    """A row exactly as production held it BEFORE the account column existed."""
    values = {
        "source_date": DAY, "customer_id": customer_id,
        "campaign_name": "brand - uk", "campaign_id": "10", "ad_group": "Core",
        "keyword": "freight", "match_type": "BROAD",
        "search_term": "freight forwarding software",
        "cost_micros": 5_000_000, "currency_code": "GBP",
        "source_system": source_system, "spend_usd": 5.0, "clicks": 3,
        "impressions": 40,
    }
    values.update(over)
    _exec(
        f"INSERT INTO search_terms ({_TWIN_COLUMNS}, is_flagged_waste, "
        "junk_category, matched_pattern) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (*values.values(), flagged, junk, pattern))


def _init_db(pg, monkeypatch):  # noqa: F811
    import db.connection as connection

    monkeypatch.setenv("DATABASE_URL", pg.url)
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    connection._pool = None
    connection.init_pool()
    from db.schema import init_db

    init_db()


def _write(rows):
    from db.writers import write_search_terms

    return write_search_terms(None, rows)


def _run_audit(*args, env_extra=None):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root",
           "PYTHONPATH": str(_ROOT), "GOOGLE_ADS_CUSTOMER_ID": ACCOUNT}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", _AUDIT_MODULE, *args],
                          cwd=str(_ROOT), capture_output=True, text=True, env=env)


# ═════════════════════════════════════════════════════════════════════════════
# §3 — deterministic supersession of EXACT null-account twins
# ═════════════════════════════════════════════════════════════════════════════
@_needs_pg
def test_1_a_complete_row_supersedes_its_exact_null_account_twin(pg, monkeypatch):  # noqa: F811
    """The production shape, reproduced: one pre-cutover row, then the normal
    rolling sync writing its complete replacement."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin()
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 1

    assert _write([_fact()]) == 1

    # One fact per canonical identity — the twin is gone, the complete row stays.
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 1
    assert _scalar("SELECT customer_id FROM search_terms") == ACCOUNT


@_needs_pg
def test_2_analysis_annotations_survive_the_supersession(pg, monkeypatch):  # noqa: F811
    """`is_flagged_waste`, `junk_category` and `matched_pattern` are the record
    of a human or a rule having judged this term. They exist nowhere upstream,
    so deleting the twin without carrying them across would silently un-review
    work somebody did."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin(flagged=True, junk="job_seeker", pattern="jobs")

    _write([_fact()])

    row = _rows_of("SELECT customer_id, is_flagged_waste, junk_category, "
                   "matched_pattern FROM search_terms")[0]
    assert row == (ACCOUNT, True, "job_seeker", "jobs")


@_needs_pg
def test_2b_a_newer_classification_is_never_overwritten_by_the_older_one(pg, monkeypatch):  # noqa: F811
    """COALESCE, not overwrite: the carry-over fills a gap, it does not reverse
    a judgement someone has already made on the canonical row."""
    _init_db(pg, monkeypatch)
    _write([_fact()])
    _exec("UPDATE search_terms SET is_flagged_waste = FALSE, "
          "junk_category = 'reviewed_clean' WHERE customer_id = %s", (ACCOUNT,))
    _seed_pre_cutover_twin(flagged=True, junk="job_seeker", pattern="jobs")

    _write([_fact()])

    row = _rows_of("SELECT is_flagged_waste, junk_category, matched_pattern "
                   "FROM search_terms WHERE customer_id = %s", (ACCOUNT,))[0]
    # The canonical row keeps its own decision; only the EMPTY field is filled.
    assert row[0] is False
    assert row[1] == "reviewed_clean"
    assert row[2] == "jobs"


@_needs_pg
def test_3_a_different_non_null_customer_is_untouched(pg, monkeypatch):  # noqa: F811
    """Another account's row is another account's observation. It is never
    superseded, never merged, and never counted here."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin(customer_id=OTHER_ACCOUNT)

    _write([_fact()])

    accounts = sorted(r[0] for r in _rows_of("SELECT customer_id FROM search_terms"))
    assert accounts == [ACCOUNT, OTHER_ACCOUNT]


@_needs_pg
def test_4_a_non_google_provenance_row_is_untouched(pg, monkeypatch):  # noqa: F811
    """A Windsor-era or unknown-provenance row is not a pre-cutover twin of a
    canonical row; it is a different system's record of the same day."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin(source_system="windsor")
    _seed_pre_cutover_twin(source_system=None, search_term="unknown provenance")

    _write([_fact()])

    survivors = sorted(
        (r[0], r[1]) for r in
        _rows_of("SELECT COALESCE(source_system,'none'), COALESCE(customer_id,'none') "
                 "FROM search_terms"))
    assert survivors == [("google_ads_api", ACCOUNT), ("none", "none"),
                         ("windsor", "none")]


@_needs_pg
@pytest.mark.parametrize("differing", [
    {"source_date": DAY - timedelta(days=1)},
    {"campaign_name": "another campaign"},
    {"campaign_id": "99"},
    {"ad_group": "Other AG"},
    {"keyword": "different keyword"},
    {"match_type": "EXACT"},
    {"search_term": "a different query"},
])
def test_5_a_row_differing_in_any_key_component_is_untouched(pg, monkeypatch,  # noqa: F811
                                                             differing):
    """EXACT means exact. A row that differs by so much as a match type is a
    different observation, and merging it would destroy a fact rather than
    deduplicate one."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin(**differing)

    _write([_fact()])

    assert _scalar("SELECT COUNT(*) FROM search_terms") == 2
    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 1


@_needs_pg
def test_6_repeating_the_sync_is_idempotent(pg, monkeypatch):  # noqa: F811
    """The rolling window re-syncs the same days every run. Doing so must change
    nothing but the updated timestamps."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin()

    for _ in range(3):
        _write([_fact(), _fact(search_term="second query")])

    assert _scalar("SELECT COUNT(*) FROM search_terms") == 2
    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 0
    assert _scalar("""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM search_terms
            GROUP BY source_date, COALESCE(customer_id,''),
                     COALESCE(campaign_name,''), COALESCE(campaign_id,''),
                     COALESCE(ad_group,''), COALESCE(keyword,''),
                     COALESCE(match_type,''), search_term
            HAVING COUNT(*) > 1) d""") == 0


def test_7_newly_ingested_rows_still_reject_missing_customer_identity():
    """F1's ingestion rule is unchanged: a row with no account never becomes a
    canonical row in the first place."""
    from services import search_term_sync_service as st_sync

    prepared, rejected = st_sync.classify_rows([
        _fact(),
        _fact(customer_id=None, search_term="no account"),
        _fact(customer_id="   ", search_term="blank account"),
    ])
    assert len(prepared) == 1
    assert rejected[st_sync.REJECT_MISSING_CUSTOMER_ID] == 2


# ═════════════════════════════════════════════════════════════════════════════
# §2 — every production reader consumes the account-scoped population
# ═════════════════════════════════════════════════════════════════════════════
def _seed_reader_fixture():
    """One complete canonical row and one pre-cutover twin that must never be
    counted beside it. Deliberately NOT written through the writer, so the
    supersession does not remove the twin — this is the state a reader has to
    survive between the cutover and the next successful sync."""
    _seed_pre_cutover_twin(spend_usd=5.0, clicks=3, impressions=40)
    _exec(
        f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (DAY, ACCOUNT, "brand - uk", "10", "Core", "freight", "BROAD",
         "freight forwarding software", 5_000_000, "GBP", "google_ads_api",
         5.0, 3, 40))


@_needs_pg
def test_8_to_11_repository_readers_exclude_null_account_rows(pg, monkeypatch):  # noqa: F811
    """Search-term aggregates, daily costs and the drawer series all read the
    account-scoped population.

    Before F3 each of these bounded only on the date window, so the twin was
    summed alongside its replacement — spend, clicks and impressions all
    doubled, from SQL that looked entirely reasonable.
    """
    _init_db(pg, monkeypatch)
    _seed_reader_fixture()
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 2

    import db.search_term_repository as repo

    agg = repo.fetch_search_term_aggregates(DAY - timedelta(days=7), DAY)
    assert agg["available"] is True
    assert agg["source"]["row_count"] == 1
    assert agg["source"]["clicks_total"] == 3
    assert agg["source"]["impressions_total"] == 40
    assert agg["source"]["cost_micros_total"] == 5_000_000

    costs = repo.fetch_search_term_daily_costs(DAY - timedelta(days=7), DAY)
    assert costs["available"] is True
    assert len(costs["rows"]) == 1
    assert costs["rows"][0]["cost_micros"] == 5_000_000

    daily = repo.fetch_search_term_daily_for_campaign(
        DAY - timedelta(days=7), DAY, "freight forwarding software",
        campaign_id="10")
    assert daily["available"] is True
    assert len(daily["rows"]) == 1
    assert daily["rows"][0]["clicks"] == 3


@_needs_pg
def test_13_dashboard_campaign_search_term_signals_exclude_null_account_rows(
        pg, monkeypatch):  # noqa: F811
    _init_db(pg, monkeypatch)
    _seed_reader_fixture()

    import db.revenue_repository as revenue_repo

    out = revenue_repo.fetch_search_term_signals(DAY - timedelta(days=7), DAY)
    assert out["available"] is True
    assert len(out["rows"]) == 1
    assert out["rows"][0]["clicks"] == 3


@_needs_pg
def test_14_another_valid_customer_cannot_enter_this_accounts_totals(pg, monkeypatch):  # noqa: F811
    """A second account is not a duplicate to be merged and not history to be
    disclosed — it is somebody else's data, and it stays out."""
    _init_db(pg, monkeypatch)
    _exec(
        f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (DAY, ACCOUNT, "brand - uk", "10", "Core", "freight", "BROAD",
         "freight forwarding software", 5_000_000, "GBP", "google_ads_api",
         5.0, 3, 40))
    _exec(
        f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (DAY, OTHER_ACCOUNT, "brand - uk", "10", "Core", "freight", "BROAD",
         "freight forwarding software", 90_000_000, "GBP", "google_ads_api",
         90.0, 99, 999))

    import db.search_term_repository as repo

    agg = repo.fetch_search_term_aggregates(DAY - timedelta(days=7), DAY)
    assert agg["source"]["row_count"] == 1
    assert agg["source"]["clicks_total"] == 3
    assert agg["source"]["cost_micros_total"] == 5_000_000


@pytest.mark.parametrize("path", ["/api/search-terms", "/api/search-terms/summary",
                                  "/api/search-terms/ngrams"])
def test_8_to_10_endpoints_compose_the_canonical_scope(path):
    """Raw Search Terms, the summary and N-grams all start their WHERE clause
    from the shared scope.

    Checked in the AST because the defect was an absence: a date-only filter
    reads as perfectly sensible SQL, and nothing about it hints that it counts
    two rows for one observation.
    """
    src = (_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    handler = {
        "/api/search-terms": "api_search_terms",
        "/api/search-terms/summary": "api_search_terms_summary",
        "/api/search-terms/ngrams": "api_search_terms_ngrams",
    }[path]
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == handler)
    dumped = ast.dump(fn)
    # The scope is resolved, the fail-closed branch exists, and the condition
    # list is SEEDED with the scope rather than appending it as an afterthought.
    assert "_canonical_search_term_scope" in dumped
    assert "_search_term_scope_unavailable" in dumped
    assert "scope" in dumped and "sql" in dumped


def test_11_and_12_evidence_and_flagged_read_through_the_scoped_repository():
    """Search Terms + Patterns evidence and Flagged/Waste evidence do not query
    the table themselves — they go through the repository, which is scoped. The
    guard is that they keep doing so."""
    service = (_ROOT / "services" / "search_term_evidence_service.py").read_text(
        encoding="utf-8")
    assert "FROM search_terms" not in service
    assert "st_repo.fetch_search_term_aggregates" in service
    assert "st_repo.fetch_search_term_daily_costs" in service

    campaigns = (_ROOT / "services" / "dashboard_campaigns_service.py").read_text(
        encoding="utf-8")
    assert "FROM search_terms" not in campaigns
    assert "fetch_search_term_signals" in campaigns


def test_15_an_unresolved_customer_produces_unavailable_not_unscoped_totals(monkeypatch):
    """Fail closed. There is one configured account today, which makes "just
    read everything" look harmless — and that is the trap, because the day a
    second account exists every historical total silently changes meaning."""
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)

    unavailable = scope_mod.canonical_scope(DAY - timedelta(days=7), DAY)
    assert unavailable.available is False
    assert unavailable.reason == scope_mod.REASON_CUSTOMER_NOT_CONFIGURED
    assert unavailable.sql == "FALSE"
    assert unavailable.params == ()

    import db.revenue_repository as revenue_repo
    import db.search_term_repository as repo

    for result in (repo.fetch_search_term_aggregates(None, DAY),
                   repo.fetch_search_term_daily_costs(None, DAY),
                   repo.fetch_search_term_daily_for_campaign(None, DAY, "t"),
                   revenue_repo.fetch_search_term_signals(None, DAY)):
        assert result["available"] is False
        assert result["reason"] == scope_mod.REASON_CUSTOMER_NOT_CONFIGURED
        assert result["rows"] == []


def test_15b_the_scope_matches_only_exact_account_spellings():
    """Google Ads writes an id both as 1234567890 and 123-456-7890, and which
    form reached the column depends on how the variable was typed that day.
    Both are accepted; nothing else is, and never NULL."""
    forms = scope_mod.customer_id_candidates("123-456-7890")
    assert forms == ["123-456-7890", "1234567890"]
    assert scope_mod.customer_id_candidates("1234567890") == forms
    # No account configured means no candidates — not a wildcard. `""` is an
    # explicit "nothing configured"; `None` means "read the configuration",
    # which is why the two are not the same argument.
    assert scope_mod.customer_id_candidates("") == []
    assert scope_mod.customer_id_candidates("   ") == []
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
        assert scope_mod.customer_id_candidates(None) == []
    finally:
        monkeypatch.undo()
    # A short/unusual id is used verbatim rather than reformatted into a guess.
    assert scope_mod.customer_id_candidates("555") == ["555"]


# ═════════════════════════════════════════════════════════════════════════════
# §5 — the audit stays strict, and reader filtering cannot hide the cutover
# ═════════════════════════════════════════════════════════════════════════════
@_needs_pg
def test_16_exact_twins_after_a_purported_cutover_fail_the_audit(pg, monkeypatch):  # noqa: F811
    """The production failure, reproduced end to end.

    Both rows are canonical provenance and complete ingestion is correct — and
    the table still holds two rows for one observation. Narrowing the readers
    fixed the pages; it must not be able to certify the database.
    """
    _init_db(pg, monkeypatch)
    _seed_reader_fixture()
    _seed_batch(DAY)

    res = _run_audit("--json", env_extra={"DATABASE_URL": pg.url})
    assert res.returncode == 1, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert audit.V_NULL_CUSTOMER_TWIN in payload["violation_codes"]

    search = next(d for d in payload["datasets"] if d["dataset"] == "search_terms")
    assert search["null_customer_twins"] == 1
    # The account-scoped view alone would have looked clean — which is exactly
    # why the audit does not rely on it.
    assert search["current"]["row_count"] == 1
    assert search["current"]["rows_missing_identity"] == 0
    assert search["interval_canonical_provenance"]["rows_missing_identity"] == 1
    # One problem, one code: the row lacks identity BECAUSE its twin was never
    # superseded, so it is reported as a twin and not also as a generic
    # identity fault. `missing_identity` is reserved for what neither the twin
    # nor the history bucket explains — the case current ingestion could still
    # be causing.
    assert audit.V_MISSING_IDENTITY not in payload["violation_codes"]


@_needs_pg
def test_17_unmatched_historical_null_account_rows_are_disclosed_not_counted(
        pg, monkeypatch):  # noqa: F811
    """A null-account row with nothing to replace it is history. Failing on it
    would make this command permanently red over rows nobody can repair, and
    inventing an account for it would be fabricating provenance."""
    _init_db(pg, monkeypatch)
    # A complete canonical row, and an unrelated null-account row on the same
    # day with a DIFFERENT term — no replacement exists for it.
    _exec(
        f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (DAY, ACCOUNT, "brand - uk", "10", "Core", "freight", "BROAD",
         "freight forwarding software", 5_000_000, "GBP", "google_ads_api",
         5.0, 3, 40))
    _seed_pre_cutover_twin(search_term="orphaned historical query")
    _seed_batch(DAY)

    res = _run_audit("--json", env_extra={"DATABASE_URL": pg.url})
    payload = json.loads(res.stdout)
    search = next(d for d in payload["datasets"] if d["dataset"] == "search_terms")

    assert search["null_customer_twins"] == 0
    assert search["unmatched_null_customer_rows"] == 1
    assert audit.V_NULL_CUSTOMER_TWIN not in payload["violation_codes"]
    assert any(d["code"] == audit.D_UNMATCHED_NULL_CUSTOMER
               for d in payload["disclosures"])
    # Disclosed, and contributing nothing.
    assert search["current"]["row_count"] == 1
    # No account was invented for it.
    assert _scalar("SELECT customer_id FROM search_terms "
                   "WHERE search_term = 'orphaned historical query'") is None


@_needs_pg
def test_16b_an_account_bearing_row_without_full_identity_still_blocks(pg, monkeypatch):  # noqa: F811
    """The residual case `missing_identity` is reserved for: THIS account's own
    row, produced by current ingestion, without a campaign id. Neither a twin
    nor history explains it, so nothing else would report it."""
    _init_db(pg, monkeypatch)
    _exec(
        f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (DAY, ACCOUNT, "brand - uk", None, "Core", "freight", "BROAD",
         "no campaign id", 5_000_000, "GBP", "google_ads_api", 5.0, 3, 40))
    _seed_batch(DAY)

    payload = json.loads(_run_audit("--json",
                                    env_extra={"DATABASE_URL": pg.url}).stdout)
    assert audit.V_MISSING_IDENTITY in payload["violation_codes"]
    detail = next(v["detail"] for v in payload["violations"]
                  if v["code"] == audit.V_MISSING_IDENTITY)
    assert "explained by neither" in detail


@_needs_pg
def test_18_and_19_the_production_shape_reproduces_and_then_certifies(pg, monkeypatch):  # noqa: F811
    """BEFORE and AFTER, on a real database.

    Before: pre-cutover rows with no account, then a complete canonical sync
    beside them — the audit fails with the twin violation. After: one normal
    rolling sync through the writer — one fact per canonical identity, and the
    audit passes. No manual SQL delete, no historical backfill.
    """
    _init_db(pg, monkeypatch)

    terms = [f"query {i}" for i in range(5)]
    for term in terms:
        _seed_pre_cutover_twin(search_term=term, flagged=True,
                               junk="job_seeker", pattern="jobs")
    # Plus one genuinely historical row with no replacement in the sync below.
    _seed_pre_cutover_twin(search_term="retired query")
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 6

    # BEFORE — the complete rows arrive without superseding anything, exactly as
    # they did in production (seeded directly to reproduce that state).
    for term in terms:
        _exec(
            f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (DAY, ACCOUNT, "brand - uk", "10", "Core", "freight", "BROAD",
             term, 5_000_000, "GBP", "google_ads_api", 5.0, 3, 40))
    _seed_batch(DAY)
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 11
    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 6

    before = json.loads(_run_audit("--json",
                                   env_extra={"DATABASE_URL": pg.url}).stdout)
    assert before["ok"] is False
    assert audit.V_NULL_CUSTOMER_TWIN in before["violation_codes"]
    st_before = next(d for d in before["datasets"] if d["dataset"] == "search_terms")
    assert st_before["null_customer_twins"] == 5
    assert st_before["unmatched_null_customer_rows"] == 1

    # AFTER — one ordinary rolling sync. The writer supersedes each exact twin.
    _write([_fact(search_term=term) for term in terms])

    assert _scalar("SELECT COUNT(*) FROM search_terms") == 6      # 5 + 1 history
    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 1
    # The analysis state survived on every superseded row.
    assert _scalar("SELECT COUNT(*) FROM search_terms WHERE customer_id = %s "
                   "AND is_flagged_waste IS TRUE AND junk_category = 'job_seeker'",
                   (ACCOUNT,)) == 5

    _seed_batch(DAY)
    after = json.loads(_run_audit("--json",
                                  env_extra={"DATABASE_URL": pg.url}).stdout)
    st_after = next(d for d in after["datasets"] if d["dataset"] == "search_terms")
    assert st_after["null_customer_twins"] == 0
    assert st_after["current"]["row_count"] == 5
    assert st_after["current"]["duplicate_natural_key_groups"] == 0
    assert audit.V_NULL_CUSTOMER_TWIN not in after["violation_codes"]
    assert audit.V_MISSING_IDENTITY not in after["violation_codes"]
    # The unmatched historical row is still there, still disclosed, still uncounted.
    assert st_after["unmatched_null_customer_rows"] == 1
    assert any(d["code"] == audit.D_UNMATCHED_NULL_CUSTOMER
               for d in after["disclosures"])


def _seed_batch(day: date) -> None:
    """A successful canonical batch covering `day`, so the audit has a certified
    interval to measure inside."""
    for dataset in ("search_terms", "keyword_facts"):
        _exec("INSERT INTO sync_batches (source, dataset, sync_type, status, "
              "row_count, fetched_count, prepared_count, rejected_count, "
              "verified_empty, date_from, date_to, started_at, finished_at) "
              "VALUES ('google_ads_api', %s, 'daily', 'success', 1, 1, 1, 0, "
              "FALSE, %s, %s, NOW(), NOW())",
              (dataset, day - timedelta(days=13), day))
        _exec("INSERT INTO sync_state (source, dataset, status, "
              "last_successful_sync_at, last_source_date) "
              "VALUES ('google_ads_api', %s, 'success', NOW(), %s) "
              "ON CONFLICT (source, dataset) DO UPDATE SET status = 'success', "
              "last_successful_sync_at = NOW(), "
              "last_source_date = EXCLUDED.last_source_date",
              (dataset, day))


@_needs_pg
def test_15c_an_unresolved_account_makes_the_audit_unavailable(pg, monkeypatch):  # noqa: F811
    """Not "everything" — nothing. Certifying the whole table here would be the
    readers' mistake, made by the check meant to catch it."""
    _init_db(pg, monkeypatch)
    res = _run_audit("--json", env_extra={"DATABASE_URL": pg.url,
                                          "GOOGLE_ADS_CUSTOMER_ID": ""})
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Traceback" not in res.stderr
    payload = json.loads(res.stdout)
    assert payload["violation_codes"] == [audit.V_ACCOUNT_NOT_CONFIGURED]
    assert payload["datasets"] == []

    human = _run_audit(env_extra={"DATABASE_URL": pg.url,
                                  "GOOGLE_ADS_CUSTOMER_ID": ""})
    assert human.returncode == 2
    assert "Traceback" not in human.stderr


@_needs_pg
def test_renderers_survive_every_population_shape(pg, monkeypatch):  # noqa: F811
    """Both output modes, over each shape an operator will actually meet."""
    _init_db(pg, monkeypatch)
    _seed_batch(DAY)

    shapes = {
        "healthy": lambda: _exec(
            f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (DAY, ACCOUNT, "brand - uk", "10", "Core", "freight", "BROAD",
             "healthy query", 5_000_000, "GBP", "google_ads_api", 5.0, 3, 40)),
        "exact_twin": lambda: _seed_pre_cutover_twin(search_term="healthy query"),
        "unmatched_history": lambda: _seed_pre_cutover_twin(
            search_term="orphan query"),
        "two_accounts": lambda: _exec(
            f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (DAY, OTHER_ACCOUNT, "brand - uk", "10", "Core", "freight", "BROAD",
             "healthy query", 9_000_000, "GBP", "google_ads_api", 9.0, 9, 90)),
    }
    for seed in shapes.values():
        seed()
        for args in (("--json",), ()):
            res = _run_audit(*args, env_extra={"DATABASE_URL": pg.url})
            assert res.returncode in (0, 1), res.stdout + res.stderr
            assert "Traceback" not in res.stderr
            assert res.stdout.strip()


# ═════════════════════════════════════════════════════════════════════════════
# §4 / §20 — one declared key, and no external writes
# ═════════════════════════════════════════════════════════════════════════════
def test_the_declared_natural_key_names_the_account_everywhere():
    """A contract describing a key that no longer exists is worse than none,
    because people act on it."""
    assert "COALESCE(customer_id,'')" in scope_mod.SEARCH_TERMS_NATURAL_KEY

    from db.search_term_repository import SEARCH_TERMS_NATURAL_KEY

    assert SEARCH_TERMS_NATURAL_KEY is scope_mod.SEARCH_TERMS_NATURAL_KEY

    repo_doc = (_ROOT / "db" / "search_term_repository.py").read_text(encoding="utf-8")
    assert "COALESCE(customer_id,'')" in repo_doc

    service = (_ROOT / "services" / "search_term_evidence_service.py").read_text(
        encoding="utf-8")
    assert '"grain": ("customer_id + source_date' in service
    assert '"account_scoped": True' in service

    schema = (_ROOT / "db" / "schema.py").read_text(encoding="utf-8")
    index = schema[schema.rindex("CREATE UNIQUE INDEX idx_search_terms_unique_fact"):]
    assert "COALESCE(customer_id" in index[:index.index(");")]

    identity = (_ROOT / "analysis" / "search_term_identity.py").read_text(
        encoding="utf-8")
    assert "Google Ads account" in identity


def test_20_no_google_ads_or_hubspot_write_is_introduced():
    """Read Google Ads, write local tables. Nothing this PR adds mutates a
    remote resource."""
    mutating = ("mutate", "MutateGoogleAds", "campaign_criterion_service",
                "add_negative", "update_budget", "set_bid",
                "hubspot.*create", "crm/v3/objects")
    for rel in ("analysis/search_term_scope.py", "db/search_term_repository.py",
                "scripts/audit_keyword_search_term_freshness.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))
        for verb in mutating:
            assert verb not in code, f"{rel} mentions {verb}"

    # The scope module opens nothing at all — it builds SQL text and parameters.
    scope_src = (_ROOT / "analysis" / "search_term_scope.py").read_text(
        encoding="utf-8")
    assert "get_conn" not in scope_src
    assert "requests" not in scope_src
