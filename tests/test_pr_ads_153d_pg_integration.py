"""
tests/test_pr_ads_153d_pg_integration.py

PR-ADS-153D — PostgreSQL-backed integration tests for the consolidated
search-term waste experience.

These prove behaviour that source-string assertions cannot:

  §10/§41  the SAME source-date fact ingested twice does not double spend,
           clicks, impressions or the unique term count — proven against the
           real writer and the real unique fact index, not a mock;
  §41      the same term text in two campaigns stays two distinct facts;
  §23      waste_terms annotations never contribute a metric;
  §15/§43  local review decisions are durable, shared, and survive a repeated
           flag observation without reopening;
  §25      flag history is monotonic and outlives a resolution;
  §44      one durable term yields one Action Queue item across repeated sync.

The tests spin up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user. If the server binaries or that user are unavailable the
whole module is skipped rather than failing.

Read-only against every external platform. The only writes are to the local
database.

Run with:
    python -m pytest tests/test_pr_ads_153d_pg_integration.py -v
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# ── Locate the PostgreSQL server binaries ────────────────────────────────────
_PG_BIN = None
for _cand in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
    if os.path.exists(os.path.join(_cand, "initdb")):
        _PG_BIN = _cand
        break


def _have_postgres() -> bool:
    if not _PG_BIN:
        return False
    try:
        import pwd
        pwd.getpwnam("postgres")
    except (KeyError, ImportError):
        return False
    if shutil.which("sudo") is None:
        return False
    # `sudo` existing is not the same as `sudo` being usable non-interactively.
    # Probe it (-n = never prompt) so the module SKIPS on a password-protected
    # sudo instead of running and then failing inside the cluster fixture.
    probe = subprocess.run(["sudo", "-n", "-u", "postgres", "true"],
                           capture_output=True, text=True)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


class _PgCluster:
    """A throwaway PostgreSQL cluster (initdb + start), owned by ``postgres``."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pgst153d_")
        # `postgres` must be able to create data/ and write the socket + log
        # inside this directory. mkdtemp gives us 0700, so widen it — chmod
        # only needs ownership, which we have, and works without root.
        # Handing ownership over is nicer where it is permitted, so try it
        # too, but never depend on it: as an unprivileged user it will fail.
        os.chmod(self.tmp, 0o777)
        _run(["sudo", "-n", "chown", "-R", "postgres:postgres", self.tmp])
        self.data = os.path.join(self.tmp, "data")
        self.port = _free_port()
        self.url = None

    def start(self):
        r = _run(["sudo", "-u", "postgres", os.path.join(_PG_BIN, "initdb"),
                  "-D", self.data, "-A", "trust", "-E", "UTF8"])
        if r.returncode != 0:
            raise RuntimeError(f"initdb failed: {r.stderr}")
        r = _run(["sudo", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
                  "-D", self.data, "-l", os.path.join(self.tmp, "log"), "-w",
                  "-o", f"-p {self.port} -k {self.tmp} -h 127.0.0.1", "start"])
        if r.returncode != 0:
            raise RuntimeError(f"pg_ctl start failed: {r.stderr}")
        import psycopg2
        for _ in range(20):
            try:
                c = psycopg2.connect(host="127.0.0.1", port=self.port,
                                     user="postgres", dbname="postgres")
                break
            except psycopg2.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError("could not connect to freshly started postgres")
        c.autocommit = True
        c.cursor().execute("CREATE DATABASE app")
        c.close()
        self.url = f"postgresql://postgres@127.0.0.1:{self.port}/app"
        return self

    def connect(self):
        import psycopg2
        return psycopg2.connect(self.url)

    def stop(self):
        _run(["sudo", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
              "-D", self.data, "-w", "stop"])
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture()
def pg(monkeypatch):
    """A live cluster with the real schema applied and db.connection pointed at it."""
    cluster = _PgCluster().start()
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)

        import db.connection as connection
        # Reset any pool created by an earlier test module.
        if hasattr(connection, "_pool"):
            monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()

        from db.schema import init_db
        init_db()

        yield cluster
    finally:
        try:
            import db.connection as connection
            if getattr(connection, "_pool", None) is not None:
                connection._pool.closeall()
                connection._pool = None
        except Exception:  # noqa: BLE001
            pass
        cluster.stop()




# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
_Q3_START, _Q3_END = date(2026, 7, 1), date(2026, 9, 30)


def _fact(term="freight forwarder jobs", *, source_date="2026-07-10",
          campaign="Brand - UK", campaign_id="1", ad_group="AG1",
          keyword="freight", match_type="BROAD", spend=40.0, clicks=10,
          impressions=100, conversions=0.0):
    """One canonical search-term fact row, in the connector's input shape."""
    return {
        "date": source_date,
        "campaign": campaign,
        "campaign_id": campaign_id,
        "ad_group": ad_group,
        "keyword": keyword,
        "match_type": match_type,
        "search_term": term,
        # PR-ADS-156-F3 §2: canonical rows carry the ACCOUNT. Without it these
        # rows fall outside the account-scoped canonical population, which is
        # the correct new behaviour — a fixture that omits it is describing
        # pre-cutover history, not the facts these tests are about.
        "customer_id": "555",
        "spend_usd": spend,
        "clicks": clicks,
        "impressions": impressions,
        "conversions": conversions,
        "cost_micros": int(spend * 1_000_000),
        "currency_code": "GBP",
        "source": "google_ads_api",
    }


def _write_facts(rows):
    from db.writers import write_search_terms

    return write_search_terms(None, rows)


def _flag_all_waste(junk_category="job_seeker", pattern="jobs"):
    """Mark every stored fact as durably flagged, as the weekly run would."""
    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE search_terms SET is_flagged_waste = TRUE, "
                "junk_category = %s, matched_pattern = %s",
                (junk_category, pattern))
        conn.commit()


def _write_waste_snapshots(term, campaign, runs):
    """Write N run-grained waste_terms snapshots — the double-count hazard."""
    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_type, started_at, status) "
                "VALUES ('weekly', NOW(), 'success') RETURNING id")
            run_id = cur.fetchone()[0]
            for run_date in runs:
                cur.execute(
                    "INSERT INTO waste_terms (run_id, run_date, search_term, "
                    "campaign_name, spend_usd, junk_category, matched_pattern, "
                    "crm_junk_confirmed) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (run_id, run_date, term, campaign, 40.0, "job_seeker",
                     "jobs", 2))
        conn.commit()


def _totals():
    """Raw canonical totals straight from the fact table."""
    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), SUM(spend_usd), SUM(clicks), SUM(impressions), "
                "COUNT(DISTINCT search_term) FROM search_terms")
            rows, spend, clicks, impressions, terms = cur.fetchone()
    return {"rows": int(rows), "spend": float(spend or 0),
            "clicks": int(clicks or 0), "impressions": int(impressions or 0),
            "unique_terms": int(terms)}


# ═════════════════════════════════════════════════════════════════════════════
# §10 / §41 — Repeated ingestion cannot double-count
# ═════════════════════════════════════════════════════════════════════════════
def test_same_fact_ingested_twice_does_not_double_spend(pg):
    """THE required regression test. Same source-date fact, ingested twice."""
    _write_facts([_fact()])
    once = _totals()
    _write_facts([_fact()])          # identical re-ingest
    twice = _totals()

    assert once["spend"] == 40.0
    assert twice["spend"] == 40.0, "re-ingesting the same fact doubled spend"
    assert twice["clicks"] == once["clicks"] == 10
    assert twice["impressions"] == once["impressions"] == 100
    assert twice["rows"] == once["rows"] == 1
    assert twice["unique_terms"] == once["unique_terms"] == 1


def test_repeated_run_snapshots_do_not_duplicate_the_term_count(pg):
    """Five weekly runs observing the same term is still ONE durable term."""
    _write_facts([_fact()])
    _flag_all_waste()
    _write_waste_snapshots("freight forwarder jobs", "Brand - UK",
                           ["2026-07-0%d" % d for d in range(1, 6)])

    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM waste_terms")
            snapshots = cur.fetchone()[0]
    assert snapshots == 5, "fixture should create the double-count hazard"

    # The canonical fact table is unaffected by how many runs observed it.
    assert _totals()["rows"] == 1
    assert _totals()["spend"] == 40.0


def test_canonical_spend_equals_the_search_terms_source_of_truth(pg):
    _write_facts([
        _fact(term="a", spend=10.0),
        _fact(term="b", spend=25.5),
    ])
    assert _totals()["spend"] == 35.5

    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(spend_usd) FROM search_terms")
            canonical = float(cur.fetchone()[0])
    assert canonical == 35.5


def test_annotations_never_become_a_second_spend_ledger(pg):
    """waste_terms spend must not reach any canonical total."""
    _write_facts([_fact(spend=40.0)])
    _flag_all_waste()
    _write_waste_snapshots("freight forwarder jobs", "Brand - UK",
                           ["2026-07-01", "2026-07-08", "2026-07-15"])

    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(spend_usd) FROM waste_terms")
            annotation_spend = float(cur.fetchone()[0])
    # The annotation table holds 3 x 40 = 120 of snapshot spend...
    assert annotation_spend == 120.0
    # ...and the canonical truth is still 40.
    assert _totals()["spend"] == 40.0


def test_same_term_in_two_campaigns_does_not_collide(pg):
    _write_facts([
        _fact(term="tms", campaign="Brand - UK", campaign_id="1", spend=10.0),
        _fact(term="tms", campaign="Gulf", campaign_id="2", spend=20.0),
    ])
    totals = _totals()
    assert totals["rows"] == 2, "two campaigns must stay two facts"
    assert totals["unique_terms"] == 1        # one query text...
    assert totals["spend"] == 30.0            # ...but both spends counted once

    from analysis.search_term_identity import term_identity_key
    assert term_identity_key("1", "tms") != term_identity_key("2", "tms")


def test_campaign_identity_is_preserved_through_the_writer(pg):
    _write_facts([_fact(campaign="Brand - UK", campaign_id="1")])
    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT campaign_id, campaign_name FROM search_terms")
            cid, name = cur.fetchone()
    # The writer normalises campaign display names to lowercase (pre-existing
    # behaviour). What matters here is that the campaign_id — the strong
    # identifier the durable identity is built from — survives intact.
    assert cid == "1"
    assert name == "brand - uk"


def test_a_changed_metric_updates_rather_than_appends(pg):
    """Google Ads restating a day must correct the fact, not add to it."""
    _write_facts([_fact(spend=40.0, clicks=10)])
    _write_facts([_fact(spend=55.0, clicks=14)])
    totals = _totals()
    assert totals["rows"] == 1
    assert totals["spend"] == 55.0
    assert totals["clicks"] == 14


# ═════════════════════════════════════════════════════════════════════════════
# §15 / §25 / §43 — Durable local review state
# ═════════════════════════════════════════════════════════════════════════════
def test_review_decision_is_durable_and_shared(pg):
    from db import search_term_review_repository as repo
    from analysis.search_term_identity import term_identity_key

    result = repo.upsert_review_decision(
        campaign_key="1", search_term="Freight Jobs",
        review_state="exclude_candidate", reviewed_by="tester")
    assert result["available"] is True

    ident = term_identity_key("1", "Freight Jobs")
    fetched = repo.fetch_review(ident)
    assert fetched["available"] is True
    assert fetched["row"]["review_state"] == "exclude_candidate"
    # Normalised components are persisted alongside the digest, so the row
    # stays auditable without recomputing a hash.
    assert fetched["row"]["search_term_normalized"] == "freight jobs"
    assert fetched["row"]["campaign_key"] == "1"


def test_review_decision_is_keyed_by_identity_not_raw_text(pg):
    from db import search_term_review_repository as repo
    from analysis.search_term_identity import term_identity_key

    repo.upsert_review_decision(campaign_key="1", search_term="Freight  JOBS",
                                review_state="keep")
    repo.upsert_review_decision(campaign_key="1", search_term="freight jobs",
                                review_state="monitor")
    # Same durable identity → one row, latest decision wins.
    fetched = repo.fetch_review(term_identity_key("1", "freight jobs"))
    assert fetched["row"]["review_state"] == "monitor"

    from db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM search_term_review")
            assert cur.fetchone()[0] == 1


def test_flag_observation_never_reopens_a_human_decision(pg):
    from db import search_term_review_repository as repo
    from analysis.search_term_identity import term_identity_key

    repo.upsert_review_decision(campaign_key="1", search_term="freight jobs",
                                review_state="resolved")
    # A later sync observes the flag again.
    repo.record_flag_observations([{
        "campaign_key": "1", "search_term": "freight jobs",
        "flagged_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "reason": "job_seeker", "raw_reason": "job_seeker"}])

    row = repo.fetch_review(term_identity_key("1", "freight jobs"))["row"]
    assert row["review_state"] == "resolved", "a repeat observation reopened it"
    assert row["latest_flagged_at"] is not None


def test_flag_history_is_monotonic_and_survives_resolution(pg):
    from db import search_term_review_repository as repo
    from analysis.search_term_identity import term_identity_key

    early = datetime(2026, 1, 5, tzinfo=timezone.utc)
    late = datetime(2026, 8, 1, tzinfo=timezone.utc)
    repo.record_flag_observations([{
        "campaign_key": "1", "search_term": "freight jobs",
        "flagged_at": late, "reason": "job_seeker", "raw_reason": "job_seeker"}])
    repo.record_flag_observations([{
        "campaign_key": "1", "search_term": "freight jobs",
        "flagged_at": early, "reason": "job_seeker", "raw_reason": "job_seeker"}])
    repo.upsert_review_decision(campaign_key="1", search_term="freight jobs",
                                review_state="resolved")

    row = repo.fetch_review(term_identity_key("1", "freight jobs"))["row"]
    assert row["first_flagged_at"].startswith("2026-01-05")
    assert row["latest_flagged_at"].startswith("2026-08-01")
    assert row["review_state"] == "resolved"

    # The history is still discoverable after the decision.
    historical = repo.fetch_historically_flagged()
    assert any(r["term_identity"] == term_identity_key("1", "freight jobs")
               for r in historical["rows"])


def test_repeated_flag_observation_is_idempotent(pg):
    from db import search_term_review_repository as repo
    from db.connection import get_conn

    obs = [{"campaign_key": "1", "search_term": "freight jobs",
            "flagged_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
            "reason": "job_seeker", "raw_reason": "job_seeker"}]
    for _ in range(5):
        repo.record_flag_observations(obs)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM search_term_review")
            assert cur.fetchone()[0] == 1


def test_review_state_is_never_invented_when_absent(pg):
    from db import search_term_review_repository as repo

    fetched = repo.fetch_review("nonexistent-identity")
    assert fetched["available"] is True
    assert fetched["row"] is None      # absent, not "keep"


def test_review_store_unavailable_is_not_reported_as_no_reviews(pg, monkeypatch):
    from db import search_term_review_repository as repo

    class _NullConn:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(repo, "get_conn", lambda: _NullConn())
    fetched = repo.fetch_reviews_for_identities(["x"])
    assert fetched["available"] is False
    assert fetched["reason"] == "database_unavailable"


# ═════════════════════════════════════════════════════════════════════════════
# §41 / §44 — End-to-end: flagged view and queue over real facts
# ═════════════════════════════════════════════════════════════════════════════
def _flagged(window="all_time"):
    from services.search_term_evidence_service import build_flagged_search_terms

    return build_flagged_search_terms(window, page=1, page_size=100)


def test_flagged_view_reads_deduplicated_canonical_facts(pg):
    _write_facts([_fact(spend=40.0, clicks=10)])
    _write_facts([_fact(spend=40.0, clicks=10)])   # re-ingest
    _flag_all_waste()

    payload = _flagged()
    assert payload.get("db_unavailable") is not True
    assert payload["kpis"]["flagged_terms"] == 1
    assert payload["kpis"]["clicks"] == 10
    assert payload["canonical_fact_source"]["table"] == "search_terms"


def test_flagged_view_and_queue_agree_after_repeated_sync(pg):
    from api import server

    for _ in range(3):
        _write_facts([_fact(spend=200.0, clicks=50)])
    _flag_all_waste()
    _write_waste_snapshots("freight forwarder jobs", "Brand - UK",
                           ["2026-07-01", "2026-07-08", "2026-07-15"])

    payload = _flagged()
    rows = payload["rows"]
    assert len(rows) == 1

    items = server._build_waste_queue_items(None, 365, 100.0)  # noqa: SLF001
    assert len(items) == 1
    assert items[0]["evidence"]["term_identity"] == rows[0]["term_identity"]
    assert items[0]["evidence"]["spend_usd"] == rows[0]["spend_usd"]


def test_resolved_term_leaves_the_queue_and_stays_on_the_page(pg):
    from api import server
    from db import search_term_review_repository as repo

    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()

    before = server._build_waste_queue_items(None, 365, 100.0)  # noqa: SLF001
    assert len(before) == 1

    repo.upsert_review_decision(
        campaign_key=(_flagged()["rows"][0]["campaign_key"]),
        search_term="freight forwarder jobs", review_state="resolved")

    after = server._build_waste_queue_items(None, 365, 100.0)  # noqa: SLF001
    assert after == [], "a resolved term must not remain actionable"
    # But it is still visible and auditable on the page.
    rows = _flagged()["rows"]
    assert len(rows) == 1
    assert rows[0]["review_state"] == "resolved"
    assert rows[0]["action_needed"] is False


def test_legacy_waste_route_no_longer_double_counts(pg):
    """The compatibility adapter reads canonical facts, so repeated snapshots
    cannot inflate what it reports."""
    from api import server

    _write_facts([_fact(spend=40.0)])
    _flag_all_waste()
    _write_waste_snapshots("freight forwarder jobs", "Brand - UK",
                           ["2026-07-01", "2026-07-08", "2026-07-15",
                            "2026-07-22", "2026-07-29"])

    out = server.api_waste(user={"e": "x"}, days=30, window="all_time")
    assert out["canonical_source"] == "search_terms"
    assert len(out["waste"]) == 1, "five run snapshots must not be five rows"
    assert out["total_count"] == 1

    # The annotation table holds 5 x 40 = 200 of snapshot spend. Whatever the
    # adapter reports, it is never that: either the canonical 40.00, or
    # Unavailable when FX lineage is incomplete — never a fabricated multiple.
    reported = out["waste"][0]["spend_usd"]
    assert reported in (None, 40.0), reported
    assert reported != 200.0

    from db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(spend_usd) FROM waste_terms")
            assert float(cur.fetchone()[0]) == 200.0   # the old page's claim
    assert _totals()["spend"] == 40.0                  # the canonical truth


# ═════════════════════════════════════════════════════════════════════════════
# Merge-blocker 4 (PR #156) — flag history has a PRODUCTION write path, and
# out-of-order observations cannot rewrite newer evidence
# ═════════════════════════════════════════════════════════════════════════════
# Before: record_flag_observations was only ever called by tests, so in a real
# deployment first_flagged_at / latest_flagged_at / latest_flag_reason /
# latest_raw_reason stayed NULL forever. And an older replay overwrote the
# latest-* fields, because they used a plain COALESCE(EXCLUDED, existing).
# After: the weekly waste-analysis run records history through the canonical
# flagged population, and latest-* only moves forward in time.

_T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)      # oldest
_T1 = datetime(2026, 7, 1, tzinfo=timezone.utc)
_T2 = datetime(2026, 8, 1, tzinfo=timezone.utc)      # newest


def _observe(when, reason, raw, *, term="freight jobs", campaign_key="1",
             display=None):
    from db import search_term_review_repository as repo

    return repo.record_flag_observations([{
        "campaign_key": campaign_key, "search_term": term,
        "search_term_display": display or term,
        "campaign_name_display": "Brand - UK",
        "flagged_at": when, "reason": reason, "raw_reason": raw}])


def _history(term="freight jobs", campaign_key="1"):
    from analysis.search_term_identity import term_identity_key
    from db import search_term_review_repository as repo

    return repo.fetch_review(term_identity_key(campaign_key, term))["row"]


# ── 1. Newer observation followed by an older replay ────────────────────────
def test_older_replay_widens_history_but_keeps_the_newer_reason(pg):
    _observe(_T2, "job_seeker", "job_seeker")
    _observe(_T0, "low_commercial_intent", "informational")

    row = _history()
    assert row["first_flagged_at"].startswith("2026-06-01")   # window widened
    assert row["latest_flagged_at"].startswith("2026-08-01")  # not moved back
    # The reason still belongs to the NEWEST observation.
    assert row["latest_flag_reason"] == "job_seeker"
    assert row["latest_raw_reason"] == "job_seeker"


def test_older_replay_does_not_overwrite_newer_display_evidence(pg):
    _observe(_T2, "job_seeker", "job_seeker", display="Freight JOBS (new)")
    _observe(_T0, "informational", "informational", display="freight jobs (old)")
    assert _history()["search_term_display"] == "Freight JOBS (new)"


# ── 2. Older observation followed by a newer one ────────────────────────────
def test_newer_observation_advances_the_latest_fields(pg):
    _observe(_T0, "informational", "informational")
    _observe(_T2, "job_seeker", "job_seeker")

    row = _history()
    assert row["first_flagged_at"].startswith("2026-06-01")
    assert row["latest_flagged_at"].startswith("2026-08-01")
    assert row["latest_flag_reason"] == "job_seeker"


def test_equal_timestamp_observation_updates_the_reason(pg):
    """Same instant is treated as at-least-as-new, so a corrected reason for the
    same observation still lands."""
    _observe(_T1, "informational", "informational")
    _observe(_T1, "job_seeker", "job_seeker")
    assert _history()["latest_flag_reason"] == "job_seeker"


# ── 3. The same observation repeated ────────────────────────────────────────
def test_repeated_identical_observation_is_idempotent(pg):
    from db.connection import get_conn

    for _ in range(5):
        _observe(_T1, "job_seeker", "job_seeker")

    row = _history()
    assert row["first_flagged_at"].startswith("2026-07-01")
    assert row["latest_flagged_at"].startswith("2026-07-01")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM search_term_review")
            assert cur.fetchone()[0] == 1


# ── 4. Resolved before another observation ──────────────────────────────────
def test_observation_after_resolution_never_reopens_the_review(pg):
    from db import search_term_review_repository as repo

    _observe(_T0, "job_seeker", "job_seeker")
    repo.upsert_review_decision(campaign_key="1", search_term="freight jobs",
                                review_state="resolved", reviewed_by="tester")
    _observe(_T2, "job_seeker", "job_seeker")

    row = _history()
    assert row["review_state"] == "resolved"
    assert row["reviewed_by"] == "tester"
    # History still advanced — the audit trail is live, the decision is not.
    assert row["latest_flagged_at"].startswith("2026-08-01")


def test_observation_after_keep_never_reopens_the_review(pg):
    from db import search_term_review_repository as repo

    repo.upsert_review_decision(campaign_key="1", search_term="freight jobs",
                                review_state="keep")
    _observe(_T2, "job_seeker", "job_seeker")
    assert _history()["review_state"] == "keep"


# ── 5. Historical backfill without reopening the review ─────────────────────
def test_backfill_widens_history_without_reopening_a_decision(pg):
    from db import search_term_review_repository as repo

    _observe(_T2, "job_seeker", "job_seeker")
    repo.upsert_review_decision(campaign_key="1", search_term="freight jobs",
                                review_state="resolved")
    # A backfill replays much older evidence.
    _observe(_T0, "job_seeker", "job_seeker")

    row = _history()
    assert row["review_state"] == "resolved"
    assert row["first_flagged_at"].startswith("2026-06-01")
    assert row["latest_flagged_at"].startswith("2026-08-01")


# ── The production write path itself ────────────────────────────────────────
def test_weekly_run_populates_flag_history_from_canonical_facts(pg):
    """The gap this blocker names: history must be written by production code,
    not only by tests."""
    from services.search_term_flag_history_service import record_flag_history

    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()

    result = record_flag_history("all_time")
    assert result["available"] is True
    assert result["observed"] == 1
    assert result["written"] == 1

    row = _history(term="freight forwarder jobs")
    assert row is not None
    assert row["first_flagged_at"] is not None
    assert row["latest_flagged_at"] is not None
    assert row["latest_flag_reason"] == "job_seeker"
    assert row["latest_raw_reason"] == "job_seeker"
    # Untouched by an observation.
    assert row["review_state"] == "unreviewed"


def test_history_identity_matches_the_page_and_the_queue(pg):
    """History keyed off a raw campaign name would never join. It must use the
    same canonical identity the flagged view computes."""
    from services.search_term_flag_history_service import record_flag_history

    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()
    record_flag_history("all_time")

    row = _flagged()["rows"][0]
    stored = _history(term="freight forwarder jobs",
                      campaign_key=row["campaign_key"])
    assert stored is not None
    assert stored["term_identity"] == row["term_identity"]
    # And the page now surfaces the history it wrote.
    assert _flagged()["rows"][0]["first_flagged_at"] is not None


def test_recording_history_twice_changes_nothing(pg):
    from services.search_term_flag_history_service import record_flag_history

    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()
    record_flag_history("all_time")
    before = _history(term="freight forwarder jobs")
    record_flag_history("all_time")
    after = _history(term="freight forwarder jobs")

    assert before["first_flagged_at"] == after["first_flagged_at"]
    assert before["latest_flagged_at"] == after["latest_flagged_at"]
    assert before["latest_flag_reason"] == after["latest_flag_reason"]


def test_backfill_is_safe_after_a_decision_exists(pg):
    from db import search_term_review_repository as repo
    from services.search_term_flag_history_service import backfill_flag_history

    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()
    row = _flagged()["rows"][0]
    repo.upsert_review_decision(campaign_key=row["campaign_key"],
                                search_term="freight forwarder jobs",
                                review_state="keep")

    assert backfill_flag_history()["available"] is True
    stored = _history(term="freight forwarder jobs",
                      campaign_key=row["campaign_key"])
    assert stored["review_state"] == "keep"
    assert stored["first_flagged_at"] is not None


def test_history_is_not_recorded_from_a_quarantined_population(pg):
    """A population whose own contract cannot be explained must not write
    unexplainable evidence into a durable audit table."""
    from db.connection import get_conn
    from services.search_term_flag_history_service import record_flag_history

    _write_facts([_fact(spend=200.0)])
    # Flagged with NO reason evidence → truth_state mismatch.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE search_terms SET is_flagged_waste = TRUE, "
                        "junk_category = NULL, matched_pattern = NULL")
        conn.commit()

    result = record_flag_history("all_time")
    assert result["available"] is False
    assert result["reason"] == "population_quarantined"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM search_term_review")
            assert cur.fetchone()[0] == 0


# ═════════════════════════════════════════════════════════════════════════════
# Merge-blocker 5 (PR #156) — the production audit is a real merge gate
# ═════════════════════════════════════════════════════════════════════════════
# Before: the script always exited 0, and it inspected the queue only AFTER the
# queue had already silently collapsed duplicate identities.
# After: it audits the COMPLETE flagged population pre-dedup and exits non-zero
# on every reconciliation failure — including with --json.

def _run_audit(window="all_time", *, json_mode=False):
    """Invoke the audit exactly as a deploy check would, capturing its exit."""
    import importlib

    audit = importlib.import_module("scripts.audit_search_term_waste_truth")
    importlib.reload(audit)
    argv = ["audit", "--window", window] + (["--json"] if json_mode else [])
    old = sys.argv
    sys.argv = argv
    try:
        return audit.main()
    finally:
        sys.argv = old


def test_audit_passes_on_reconciled_data(pg):
    from services.search_term_flag_history_service import record_flag_history

    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()
    record_flag_history("all_time")

    assert _run_audit() == 0
    assert _run_audit(json_mode=True) == 0


def test_audit_fails_when_flag_history_was_never_persisted(pg):
    """Flagged terms with no durable history means the production writer is not
    running — exactly the gap merge-blocker 4 named."""
    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()
    # Deliberately do NOT record history.
    assert _run_audit() == 1


def test_audit_fails_on_a_mismatch_population(pg):
    from db.connection import get_conn

    _write_facts([_fact(spend=200.0)])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE search_terms SET is_flagged_waste = TRUE, "
                        "junk_category = NULL, matched_pattern = NULL")
        conn.commit()
    assert _run_audit() == 1


def test_audit_fails_when_the_review_store_is_unavailable(pg, monkeypatch):
    import db.search_term_review_repository as repo
    from services.search_term_flag_history_service import record_flag_history

    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()
    record_flag_history("all_time")
    assert _run_audit() == 0            # healthy first

    monkeypatch.setattr(repo, "fetch_reviews_for_identities",
                        lambda ids: {"available": False, "rows": {}})
    assert _run_audit() == 1


def test_audit_fails_on_duplicate_identities_before_queue_dedup(pg, monkeypatch):
    """The queue collapses duplicates, so auditing it alone would miss this.
    The gate must inspect the complete flagged population first."""
    import services.search_term_evidence_service as svc
    from services.search_term_flag_history_service import record_flag_history

    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()
    record_flag_history("all_time")

    real = svc.build_flagged_search_terms

    def _duplicated(window, **kw):
        payload = real(window, **kw)
        payload["rows"] = payload["rows"] + payload["rows"]
        return payload

    monkeypatch.setattr(svc, "build_flagged_search_terms", _duplicated)
    assert _run_audit() == 1


def test_audit_json_mode_also_exits_non_zero(pg):
    _write_facts([_fact(spend=200.0)])
    _flag_all_waste()          # flagged, but no history recorded
    assert _run_audit(json_mode=True) == 1


def test_audit_computes_identities_with_the_shared_contract(pg):
    """Not by concatenating SQL strings — a delimiter-joined key is not the
    identity the product uses, so it could agree here and disagree everywhere
    else."""
    source = (Path(__file__).resolve().parents[1]
              / "scripts" / "audit_search_term_waste_truth.py").read_text()
    assert "from analysis.search_term_identity import term_identity_key" in source
    canonical = source.split("def _canonical_facts(")[1].split("\ndef ")[0]
    assert "term_identity_key(" in canonical
    # No delimiter-concatenated identity in the identity query.
    assert "|| '|' ||" not in canonical
