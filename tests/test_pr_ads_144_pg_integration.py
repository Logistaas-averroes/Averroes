"""
PR-ADS-144 — PostgreSQL-backed integration tests (schema migration + writer + FX).

These prove behaviour that static source-string assertions cannot:

  §1  the guarded migration upgrades a PRE-PR-144 legacy schema — adds
      cost_micros / currency_code / source_system, replaces the unique index
      with the campaign_id-aware definition, is idempotent, and the writer's
      ON CONFLICT target executes against the live index;
  §2  two campaign IDs (10 and 20) sharing a display name for the same
      date/name/ad_group/keyword/term both persist;
  §3  a null-campaign_id legacy row followed by an id-bearing reingestion of
      the same fact does NOT double-count (the id-bearing identity supersedes
      the ambiguous null-id twin);
  §4  connector → adapter → writer → repository → service proves £100 native
      (GBP) is NOT rendered as $100 USD;
  §5  two source dates with DIFFERENT FX rates yield the EXACT per-date
      converted total, never a window-average approximation.

The tests spin up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user (postgres refuses to run as root). If the server binaries
or that user are unavailable the whole module is skipped rather than failing.
"""

import glob
import os
import shutil
import socket
import subprocess
import tempfile
import time

import pytest

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
    return shutil.which("sudo") is not None


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
        self.tmp = tempfile.mkdtemp(prefix="pgst144_")
        # PR-ADS-153D: `postgres` must be able to create data/ and write the
        # socket + log inside this directory. mkdtemp gives us 0700, so a
        # different user cannot even traverse it — which is why this harness
        # errored with "initdb: could not access directory ... Permission
        # denied" on a CI runner while passing locally as root. chmod only
        # needs ownership, which we have, and works without root; handing
        # ownership over is nicer where permitted, so try it too but never
        # depend on it.
        os.chmod(self.tmp, 0o777)
        _run(["sudo", "-n", "chown", "-R", "postgres:postgres", self.tmp])
        # postgres must own the data dir it initialises.
        _run(["chown", "-R", "postgres:postgres", self.tmp])
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
        # Create the application database.
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
def pg():
    cluster = _PgCluster().start()
    try:
        yield cluster
    finally:
        cluster.stop()


# ── Legacy (pre-PR-144) search_terms schema — NO lineage columns, OLD index ──
_LEGACY_SCHEMA = """
CREATE TABLE migrations (migration_id VARCHAR(50) PRIMARY KEY);
CREATE TABLE search_terms (
  id               SERIAL PRIMARY KEY,
  run_id           INTEGER,
  source_date      DATE          NOT NULL,
  campaign_name    TEXT,
  campaign_id      TEXT,
  ad_group         TEXT,
  keyword          TEXT,
  match_type       TEXT,
  search_term      TEXT          NOT NULL,
  spend_usd        NUMERIC(10,2) DEFAULT 0,
  clicks           INTEGER       DEFAULT 0,
  impressions      INTEGER       DEFAULT 0,
  conversions      NUMERIC(8,2)  DEFAULT 0,
  is_flagged_waste BOOLEAN,
  junk_category    TEXT,
  matched_pattern  TEXT,
  sync_batch_id    INTEGER,
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);
-- OLD unique index: NO campaign_id.
CREATE UNIQUE INDEX idx_search_terms_unique_fact ON search_terms (
    source_date,
    COALESCE(campaign_name, ''),
    COALESCE(ad_group,      ''),
    COALESCE(keyword,       ''),
    COALESCE(match_type,    ''),
    search_term
);
"""


def _index_def(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'idx_search_terms_unique_fact'")
        row = cur.fetchone()
    return row[0] if row else ""


def _columns(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'search_terms'")
        return {r[0] for r in cur.fetchall()}


def _apply_current_schema(conn):
    """Apply the current db/schema.py DDL (the real production migration path)."""
    from db.schema import _DDL
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


# ════════════════ §1 Migration on a legacy schema ════════════════


def test_migration_upgrades_legacy_schema(pg):
    conn = pg.connect()
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(_LEGACY_SCHEMA)
    conn.commit()

    # Legacy starting point: no lineage columns; index has NO campaign_id.
    assert "cost_micros" not in _columns(conn)
    assert "currency_code" not in _columns(conn)
    assert "source_system" not in _columns(conn)
    assert "campaign_id" not in _index_def(conn)

    _apply_current_schema(conn)

    # Columns added.
    cols = _columns(conn)
    assert {"cost_micros", "currency_code", "source_system"} <= cols
    # Index replaced with the campaign_id-aware definition.
    idx = _index_def(conn)
    assert "campaign_id" in idx
    # Migration recorded.
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM migrations WHERE migration_id = "
                    "'PR-ADS-144-currency-and-id-key'")
        assert cur.fetchone() is not None
    conn.close()


def test_migration_is_idempotent(pg):
    conn = pg.connect()
    with conn.cursor() as cur:
        cur.execute(_LEGACY_SCHEMA)
    conn.commit()
    _apply_current_schema(conn)
    idx_first = _index_def(conn)
    # Re-applying the whole schema must not error and must not change the index.
    _apply_current_schema(conn)
    _apply_current_schema(conn)
    assert _index_def(conn) == idx_first
    assert "campaign_id" in _index_def(conn)
    assert {"cost_micros", "currency_code", "source_system"} <= _columns(conn)
    conn.close()


def test_migration_deletes_null_id_twin_of_id_bearing_fact(pg):
    """A pre-existing null-id row that collides (under the new key) with an
    id-bearing row for the same fact is deterministically removed."""
    conn = pg.connect()
    with conn.cursor() as cur:
        cur.execute(_LEGACY_SCHEMA)
        # Drop the OLD unique index so we can seed BOTH a null-id and an
        # id-bearing row for the same fact (they would collide under it) — this
        # reproduces the exact double-count hazard the migration must resolve.
        cur.execute("DROP INDEX idx_search_terms_unique_fact")
        cur.execute(
            "INSERT INTO search_terms(source_date, campaign_name, campaign_id, "
            "search_term) VALUES "
            "('2026-07-01','brand - uk', NULL, 'freight software'),"
            "('2026-07-01','brand - uk', '10', 'freight software'),"
            "('2026-07-01','brand - uk', '20', 'other term')")
    conn.commit()
    _apply_current_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT campaign_id, search_term FROM search_terms "
                    "ORDER BY search_term, campaign_id NULLS FIRST")
        rows = cur.fetchall()
    # The null-id twin of 'freight software' is gone; the id-10 and the
    # distinct id-20 fact both survive.
    assert ("10", "freight software") in rows
    assert ("20", "other term") in rows
    assert (None, "freight software") not in rows
    conn.close()


# ════════════════ §2/§3 Writer against the migrated index ════════════════


def _init_writer_db(pg, monkeypatch):
    """Point db.connection at the cluster, migrate from legacy, return nothing."""
    import db.connection as connection
    monkeypatch.setenv("DATABASE_URL", pg.url)
    connection._pool = None
    connection.init_pool()
    # Start from the legacy schema, then migrate — exercises the real path.
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_LEGACY_SCHEMA)
    from db.schema import _DDL
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)


def test_writer_on_conflict_and_two_ids_same_name_both_persist(pg, monkeypatch):
    _init_writer_db(pg, monkeypatch)
    from db.writers import write_search_terms

    # Same source_date / campaign name / ad group / keyword / term, campaign
    # IDs 10 and 20 — both are distinct facts and must both persist.
    base = {"date": "2026-07-01", "customer_id": "555", "campaign": "brand - uk",
            "ad_group": "Core", "keyword": "freight forwarding software",
            "match_type": "BROAD", "search_term": "freight forwarding software",
            "cost_micros": 5_000_000, "currency_code": "GBP",
            "source": "google_ads_api", "clicks": 3, "impressions": 40}
    n = write_search_terms(None, [
        {**base, "campaign_id": "10"},
        {**base, "campaign_id": "20"},
    ])
    assert n == 2

    # Re-running the SAME write (idempotent upsert on the campaign_id-aware key)
    # must not create new rows and must not raise.
    write_search_terms(None, [
        {**base, "campaign_id": "10"},
        {**base, "campaign_id": "20"},
    ])

    import db.connection as connection
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT campaign_id, COUNT(*) FROM search_terms "
                        "WHERE search_term='freight forwarding software' "
                        "GROUP BY campaign_id ORDER BY campaign_id")
            rows = cur.fetchall()
    assert rows == [("10", 1), ("20", 1)]   # both ids, one fact each


def test_writer_null_id_then_id_bearing_no_double_count(pg, monkeypatch):
    _init_writer_db(pg, monkeypatch)
    from db.writers import write_search_terms

    fact = {"date": "2026-07-01", "customer_id": "555",
            "campaign": "brand - uk", "ad_group": "Core",
            "keyword": "kw", "match_type": "BROAD",
            "search_term": "freight software", "cost_micros": 5_000_000,
            "currency_code": "GBP", "source": "google_ads_api"}
    # 1) Legacy ingestion with NO campaign_id.
    write_search_terms(None, [{**fact}])
    # 2) Later reingestion of the SAME fact WITH a campaign_id.
    write_search_terms(None, [{**fact, "campaign_id": "10"}])

    import db.connection as connection
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT campaign_id FROM search_terms "
                        "WHERE search_term='freight software'")
            rows = [r[0] for r in cur.fetchall()]
    # Exactly ONE fact remains — the id-bearing one; no null-id double count.
    assert rows == ["10"]


# ════════════════ §4/§5 connector→writer→repo→service currency truth ════════════════


def test_end_to_end_gbp_not_usd_per_date_fx(pg, monkeypatch):
    """Full pipeline: connector-shaped Google Ads rows → adapter → writer →
    repository → evidence service. £ native converts per-source-date, and two
    dates with DIFFERENT rates yield the EXACT per-date total (not an average)."""
    _init_writer_db(pg, monkeypatch)
    import db.connection as connection
    from connectors.google_ads_source import normalize_search_term_row
    from db.writers import write_search_terms

    # Seed FX rates: GBP→USD differs across the two source dates.
    #   2026-07-01: 1.20   2026-07-02: 1.40
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, "
                "rate, provider) VALUES "
                "('2026-07-01','GBP','USD',1.20,'test'),"
                "('2026-07-02','GBP','USD',1.40,'test')")

    # Two Google Ads search-term rows for the SAME term/campaign on the two
    # dates: £100 on 07-01 and £100 on 07-02 (cost_micros = 100_000_000 each).
    raw_rows = [
        {"date": "2026-07-01", "customer_id": "555", "campaign_id": "10", "campaign_name": "brand - uk",
         "ad_group_id": "1", "ad_group_name": "Core",
         "search_term": "freight forwarding software", "impressions": 50,
         "clicks": 5, "cost_micros": 100_000_000, "spend": 100.0,
         "currency_code": "GBP", "conversions": 1.0},
        {"date": "2026-07-02", "customer_id": "555", "campaign_id": "10", "campaign_name": "brand - uk",
         "ad_group_id": "1", "ad_group_name": "Core",
         "search_term": "freight forwarding software", "impressions": 60,
         "clicks": 6, "cost_micros": 100_000_000, "spend": 100.0,
         "currency_code": "GBP", "conversions": 0.0},
    ]
    normalized = [normalize_search_term_row(r) for r in raw_rows]
    assert all(n is not None for n in normalized)
    assert write_search_terms(None, normalized) == 2

    # Confirm the writer stored raw lineage (not a mislabelled USD).
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT source_date, cost_micros, currency_code, "
                        "source_system FROM search_terms ORDER BY source_date")
            stored = cur.fetchall()
    assert stored[0][1] == 100_000_000 and stored[0][2] == "GBP"
    assert stored[0][3] == "google_ads_api"

    # Drive the evidence service over a window covering both dates. Canonical
    # spend / identity aren't needed for the currency proof — stub them out so
    # the population build focuses on the durable search_terms + FX path.
    import db.revenue_repository as revenue_repo
    from datetime import date
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": True, "customer_id": "c1",
                                      "rows": [], "total_spend_usd": None,
                                      "fx_complete": False})
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda customer_id=None: {"available": True, "mappings": []})

    from services.search_term_evidence_service import build_search_term_evidence
    # A fixed 'now' so the 30d window includes 2026-07-01/02.
    from datetime import datetime, timezone
    now = datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)
    out = build_search_term_evidence("30d", now=now)

    row = [r for r in out["rows"]
           if r["search_term"] == "freight forwarding software"][0]
    # Per-date truth: £100×1.20 + £100×1.40 = $120 + $140 = $260.00.
    # A window-average (1.30) would give $260 too for equal amounts, so use
    # UNEQUAL amounts below to make the average WRONG — see assertion note.
    assert row["spend_native"] == 200.0
    assert row["native_currency"] == "GBP"
    assert row["spend_usd"] == 260.0          # exact per-date sum
    assert row["fx_complete"] is True
    # £200 native is NOT $200.
    assert row["spend_usd"] != 200.0
    assert out["kpis"]["reported_spend_usd"] == 260.0


def test_per_date_fx_differs_from_window_average(pg, monkeypatch):
    """With UNEQUAL daily spend, the per-date total must differ from the
    window-average-rate approximation — proving genuine per-date conversion."""
    _init_writer_db(pg, monkeypatch)
    import db.connection as connection
    from connectors.google_ads_source import normalize_search_term_row
    from db.writers import write_search_terms

    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, "
                "rate, provider) VALUES "
                "('2026-07-01','GBP','USD',1.00,'test'),"
                "('2026-07-02','GBP','USD',2.00,'test')")

    # Day 1: £300 at 1.00 = $300.  Day 2: £100 at 2.00 = $200.  Exact = $500.
    # Window-average rate = (1.00+2.00)/2 = 1.50 → £400 × 1.50 = $600 (WRONG).
    raw = [
        {"date": "2026-07-01", "customer_id": "555", "campaign_id": "10", "campaign_name": "brand",
         "ad_group_id": "1", "ad_group_name": "Core", "search_term": "x",
         "impressions": 1, "clicks": 1, "cost_micros": 300_000_000,
         "spend": 300.0, "currency_code": "GBP", "conversions": 0.0},
        {"date": "2026-07-02", "customer_id": "555", "campaign_id": "10", "campaign_name": "brand",
         "ad_group_id": "1", "ad_group_name": "Core", "search_term": "x",
         "impressions": 1, "clicks": 1, "cost_micros": 100_000_000,
         "spend": 100.0, "currency_code": "GBP", "conversions": 0.0},
    ]
    write_search_terms(None, [normalize_search_term_row(r) for r in raw])

    import db.revenue_repository as revenue_repo
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": True, "customer_id": "c1",
                                      "rows": [], "total_spend_usd": None,
                                      "fx_complete": False})
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda customer_id=None: {"available": True, "mappings": []})

    from datetime import datetime, timezone
    from services.search_term_evidence_service import build_search_term_evidence
    out = build_search_term_evidence(
        "30d", now=datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc))
    row = [r for r in out["rows"] if r["search_term"] == "x"][0]
    assert row["spend_native"] == 400.0
    assert row["spend_usd"] == 500.0          # exact per-date
    assert row["spend_usd"] != 600.0          # NOT the window-average result


def test_missing_fx_date_withholds_usd(pg, monkeypatch):
    """When one source date lacks an FX rate, USD is withheld and native is
    preserved (fx_complete = false)."""
    _init_writer_db(pg, monkeypatch)
    import db.connection as connection
    from connectors.google_ads_source import normalize_search_term_row
    from db.writers import write_search_terms

    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            # Only 07-01 has a rate; 07-02 is missing.
            cur.execute(
                "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, "
                "rate, provider) VALUES ('2026-07-01','GBP','USD',1.20,'test')")

    raw = [
        {"date": "2026-07-01", "customer_id": "555", "campaign_id": "10", "campaign_name": "brand",
         "ad_group_id": "1", "ad_group_name": "Core", "search_term": "x",
         "impressions": 1, "clicks": 1, "cost_micros": 100_000_000,
         "spend": 100.0, "currency_code": "GBP", "conversions": 0.0},
        {"date": "2026-07-02", "customer_id": "555", "campaign_id": "10", "campaign_name": "brand",
         "ad_group_id": "1", "ad_group_name": "Core", "search_term": "x",
         "impressions": 1, "clicks": 1, "cost_micros": 100_000_000,
         "spend": 100.0, "currency_code": "GBP", "conversions": 0.0},
    ]
    write_search_terms(None, [normalize_search_term_row(r) for r in raw])

    import db.revenue_repository as revenue_repo
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": True, "customer_id": "c1",
                                      "rows": [], "total_spend_usd": None,
                                      "fx_complete": False})
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda customer_id=None: {"available": True, "mappings": []})

    from datetime import datetime, timezone
    from services.search_term_evidence_service import build_search_term_evidence
    out = build_search_term_evidence(
        "30d", now=datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc))
    row = [r for r in out["rows"] if r["search_term"] == "x"][0]
    assert row["spend_native"] == 200.0       # native preserved
    assert row["spend_usd"] is None           # USD withheld (a date lacks FX)
    assert row["cpc_usd"] is None             # CPC-USD withheld too
    assert row["fx_complete"] is False
    # Coverage must be Unavailable when the numerator is not FX-safe.
    assert out["kpis"]["coverage"]["status"] == "unavailable"


# ════════════════ §6 Drawer identity safety — same name, two ids ════════════════


def _stub_identity_and_spend(monkeypatch):
    """No approved mapping (each stored id keeps its own key); canonical spend
    present so ids resolve to display names. FX rate seeded by the caller."""
    import db.revenue_repository as revenue_repo
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda customer_id=None: {"available": True, "mappings": []})
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": True, "customer_id": "c1",
                                      "rows": [
                                          {"campaign_id": "10", "campaign_name": "brand - uk",
                                           "spend": 0.0, "spend_usd": None, "fx_complete": True},
                                          {"campaign_id": "20", "campaign_name": "brand - uk",
                                           "spend": 0.0, "spend_usd": None, "fx_complete": True}],
                                      "total_spend_usd": None, "fx_complete": False})


def test_drawer_identity_safety_same_name_two_ids(pg, monkeypatch):
    """Adversarial: identical search term, identical campaign display name,
    campaign IDs 10 and 20, one null-ID historical daily row, and a waste_terms
    proof row keyed by the shared campaign name.

    Proves: drawer 10 sees only ID-10 daily facts; drawer 20 only ID-20;
    the ambiguous null-ID row is in NEITHER; CRM-junk confirmations/date/source
    are unavailable in both (never borrowed); and classification state still
    comes correctly from each selected search_terms unit."""
    _init_writer_db(pg, monkeypatch)
    import db.connection as connection

    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            # id 10 — flagged waste on 2026-07-01.
            cur.execute(
                "INSERT INTO search_terms(customer_id, source_date, campaign_name, campaign_id, "
                "ad_group, keyword, match_type, search_term, cost_micros, "
                "currency_code, source_system, is_flagged_waste, junk_category) "
                "VALUES ('555','2026-07-01','brand - uk','10','Core','kw','BROAD',"
                "'freight software',50000000,'GBP','google_ads_api',TRUE,'competitor')")
            # id 20 — needs review (is_flagged_waste NULL) on 2026-07-01.
            cur.execute(
                "INSERT INTO search_terms(customer_id, source_date, campaign_name, campaign_id, "
                "ad_group, keyword, match_type, search_term, cost_micros, "
                "currency_code, source_system, is_flagged_waste) "
                "VALUES ('555','2026-07-01','brand - uk','20','Core','kw','BROAD',"
                "'freight software',80000000,'GBP','google_ads_api',NULL)")
            # Ambiguous NULL-campaign_id historical daily row on a DIFFERENT date
            # (so the writer's null-id supersession does not remove it) — it must
            # surface in neither id-scoped drawer.
            cur.execute(
                "INSERT INTO search_terms(customer_id, source_date, campaign_name, campaign_id, "
                "ad_group, keyword, match_type, search_term, cost_micros, "
                "currency_code, source_system, is_flagged_waste) "
                "VALUES ('555','2026-06-20','brand - uk',NULL,'Core','kw','BROAD',"
                "'freight software',30000000,'GBP','google_ads_api',NULL)")
            # waste_terms proof keyed ONLY by the shared campaign name.
            cur.execute("INSERT INTO runs(run_type, started_at, status) "
                        "VALUES ('weekly', NOW(), 'success') RETURNING id")
            run_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO waste_terms(run_id, run_date, search_term, "
                "campaign_name, spend_usd, junk_category, matched_pattern, "
                "crm_junk_confirmed) VALUES (%s,'2026-07-02','freight software',"
                "'brand - uk',10.0,'competitor','brand',7)", (run_id,))
            # FX so USD is present (not the focus, but avoids withholding noise).
            cur.execute(
                "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, "
                "rate, provider) VALUES "
                "('2026-06-20','GBP','USD',1.20,'t'),"
                "('2026-07-01','GBP','USD',1.20,'t')")

    _stub_identity_and_spend(monkeypatch)

    from datetime import datetime, timezone
    from services.search_term_evidence_service import build_search_term_drawer
    now = datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)

    d10 = build_search_term_drawer("30d", "freight software", campaign_key="10", now=now)
    d20 = build_search_term_drawer("30d", "freight software", campaign_key="20", now=now)

    dates10 = {r["source_date"] for r in d10["daily"]["rows"]}
    dates20 = {r["source_date"] for r in d20["daily"]["rows"]}

    # Drawer 10 sees only ID-10 facts; drawer 20 only ID-20 — each on 07-01.
    assert dates10 == {"2026-07-01"}
    assert dates20 == {"2026-07-01"}
    # ID-10 daily native = £50, ID-20 = £80 — proving no cross-contamination.
    assert d10["daily"]["rows"][0]["spend_native"] == 50.0
    assert d20["daily"]["rows"][0]["spend_native"] == 80.0
    # The ambiguous null-ID historical row (2026-06-20) is in NEITHER drawer.
    assert "2026-06-20" not in dates10
    assert "2026-06-20" not in dates20

    # Classification STATE comes correctly from each selected unit.
    assert d10["term"]["state"] == "flagged"
    assert d10["classification"]["state"] == "flagged"
    assert d20["term"]["state"] == "needs_review"
    assert d20["classification"]["state"] == "needs_review"

    # CRM-junk confirmations / date / source are WITHHELD in BOTH — never
    # borrowed from the shared-name waste_terms row (crm_junk_confirmed=7).
    for d in (d10, d20):
        assert d["classification"]["crm_junk_confirmed"] is None
        assert d["classification"]["classification_date"] is None
        assert d["classification"]["classification_source"] is None
        assert d["classification"]["proof_status"] == "unavailable"


def test_drawer_unique_name_allows_classification_proof(pg, monkeypatch):
    """Control: when the campaign label uniquely identifies ONE campaign for the
    term, the scoped waste_terms proof IS surfaced (the safety gate is not
    over-broad)."""
    _init_writer_db(pg, monkeypatch)
    import db.connection as connection

    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO search_terms(customer_id, source_date, campaign_name, campaign_id, "
                "ad_group, keyword, match_type, search_term, cost_micros, "
                "currency_code, source_system, is_flagged_waste, junk_category) "
                "VALUES ('555','2026-07-01','gulf','2','Core','kw','BROAD',"
                "'freight jobs',40000000,'GBP','google_ads_api',TRUE,'job_seeker')")
            cur.execute("INSERT INTO runs(run_type, started_at, status) "
                        "VALUES ('weekly', NOW(), 'success') RETURNING id")
            run_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO waste_terms(run_id, run_date, search_term, "
                "campaign_name, spend_usd, junk_category, matched_pattern, "
                "crm_junk_confirmed) VALUES (%s,'2026-07-02','freight jobs',"
                "'gulf',5.0,'job_seeker','jobs',4)", (run_id,))
            cur.execute(
                "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, "
                "rate, provider) VALUES ('2026-07-01','GBP','USD',1.20,'t')")

    import db.revenue_repository as revenue_repo
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda customer_id=None: {"available": True, "mappings": []})
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": True, "customer_id": "c1",
                                      "rows": [{"campaign_id": "2", "campaign_name": "gulf",
                                                "spend": 0.0, "spend_usd": None,
                                                "fx_complete": True}],
                                      "total_spend_usd": None, "fx_complete": False})

    from datetime import datetime, timezone
    from services.search_term_evidence_service import build_search_term_drawer
    out = build_search_term_drawer("30d", "freight jobs", campaign_key="2",
                                   now=datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc))
    assert out["classification"]["proof_status"] == "available"
    assert out["classification"]["crm_junk_confirmed"] == 4
    assert out["classification"]["classification_date"] == "2026-07-02"
    assert out["classification"]["classification_source"] is not None


# ════════════════ PR-ADS-145 — waste bridge + legacy audit (real DB) ════════════════


def test_waste_bridge_and_legacy_audit_end_to_end(pg, monkeypatch):
    """On a real DB: a confirmed waste_terms row flips a NULL-flag search term to
    Flagged waste (safely scoped), a legacy no-currency row stays visible/
    unverified with a preserved subtotal, and the currency audit reports it."""
    _init_writer_db(pg, monkeypatch)
    import db.connection as connection

    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            # Verified GBP term, not yet classified (is_flagged_waste NULL).
            cur.execute(
                "INSERT INTO search_terms(customer_id, source_date, campaign_name, "
                "campaign_id, ad_group, keyword, match_type, search_term, "
                "cost_micros, currency_code, source_system, is_flagged_waste) VALUES "
                "('555','2026-07-01','gulf','2','Core','kw','BROAD','freight jobs',"
                "20000000,'GBP','google_ads_api',NULL)")
            # Legacy row: NO currency lineage.
            cur.execute(
                "INSERT INTO search_terms(source_date, campaign_name, campaign_id, "
                "ad_group, keyword, match_type, search_term, spend_usd) VALUES "
                "('2026-02-01','old camp','9','Core','kw','BROAD','legacy term',3.00)")
            # FX for the verified term's date.
            cur.execute(
                "INSERT INTO fx_rates(rate_date, base_currency, quote_currency, "
                "rate, provider) VALUES ('2026-07-01','GBP','USD',1.25,'t')")
            # Confirmed waste evidence from the weekly pipeline (name-scoped).
            cur.execute("INSERT INTO runs(run_type, started_at, status) "
                        "VALUES ('weekly', NOW(), 'success') RETURNING id")
            run_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO waste_terms(run_id, run_date, search_term, "
                "campaign_name, spend_usd, junk_category, matched_pattern, "
                "crm_junk_confirmed) VALUES (%s,'2026-07-02','freight jobs','gulf',"
                "5.0,'job_seeker','jobs',4)", (run_id,))

    import db.revenue_repository as revenue_repo
    monkeypatch.setattr(revenue_repo, "fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: {"available": True, "customer_id": "c1",
                                      "rows": [{"campaign_id": "2", "campaign_name": "gulf"}],
                                      "total_spend_usd": 100.0, "fx_complete": True})
    monkeypatch.setattr(revenue_repo, "fetch_campaign_identity",
                        lambda customer_id=None: {"available": True, "mappings": []})

    from datetime import datetime, timezone
    from services.search_term_evidence_service import build_search_term_evidence
    out = build_search_term_evidence(
        "all_time", now=datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc))

    fj = [r for r in out["rows"] if r["search_term"] == "freight jobs"][0]
    # Bridge: NULL-flag term becomes Flagged waste from safe waste_terms evidence.
    assert fj["state"] == "flagged"
    assert fj["classification_source"] == "waste_terms"
    assert fj["spend_usd"] == 25.0                 # £20 × 1.25 verified

    # PR-ADS-156-F3 §2: the legacy row is no longer part of the canonical
    # population at all. It has no account and no proven provenance, so it is
    # HISTORY — disclosed by the currency audit below, and contributing nothing
    # to the evidence rows or the KPIs.
    #
    # Before F3 it appeared here as a visible "unverified" row. That was the
    # right answer while the only question was currency lineage; it stopped
    # being the right answer once account identity became part of what makes a
    # row canonical, because a row nobody can attribute to an account is not a
    # quieter version of this account's evidence.
    assert [r for r in out["rows"] if r["search_term"] == "legacy term"] == []
    assert out["kpis"]["verified_spend_usd"] == 25.0

    # Read-only legacy audit reports the preserved row.
    from db.search_term_repository import fetch_legacy_currency_audit
    audit = fetch_legacy_currency_audit()
    assert audit["available"] is True
    assert audit["summary"]["legacy_unverified_row_count"] == 1
    assert "old camp" in audit["summary"]["campaigns"]
    assert audit["summary"]["rows_with_verified_replacement"] == 0
    # And nothing was mutated — both rows still present.
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM search_terms")
            assert cur.fetchone()[0] == 2


def test_legacy_audit_flags_exact_verified_replacement(pg, monkeypatch):
    """A legacy row that later gains an EXACT verified replacement (same natural
    key, proven lineage) is reported as replaced."""
    _init_writer_db(pg, monkeypatch)
    import db.connection as connection
    with connection.get_conn() as conn:
        with conn.cursor() as cur:
            # Legacy (no lineage) and a verified replacement for the SAME fact
            # (different campaign_id keeps both rows under the PR-144 key).
            cur.execute(
                "INSERT INTO search_terms(source_date, campaign_name, campaign_id, "
                "ad_group, keyword, match_type, search_term, spend_usd) VALUES "
                "('2026-02-01','gulf',NULL,'Core','kw','BROAD','freight jobs',3.00)")
            cur.execute(
                "INSERT INTO search_terms(source_date, campaign_name, campaign_id, "
                "ad_group, keyword, match_type, search_term, cost_micros, "
                "currency_code, source_system) VALUES "
                "('2026-02-01','gulf','2','Core','kw','BROAD','freight jobs',"
                "5000000,'GBP','google_ads_api')")
    from db.search_term_repository import fetch_legacy_currency_audit
    audit = fetch_legacy_currency_audit()
    legacy = [r for r in audit["rows"] if r["campaign_id"] is None][0]
    assert legacy["has_verified_replacement"] is True
    assert audit["summary"]["rows_with_verified_replacement"] == 1
