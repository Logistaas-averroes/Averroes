"""
tests/test_pr_ads_153e_a_pg_integration.py

PR-ADS-153E-A — PostgreSQL-backed integration tests for the canonical deal
ledger.

These prove durable behaviour that source assertions cannot:

  §11.3-4   reprocessing a deal, and relabelling its campaign/source, updates
            ONE row rather than minting a duplicate;
  §11.15    an older replay cannot overwrite newer HubSpot state;
  §11.14    a failed association lookup preserves earlier successful evidence;
  §11.16    every pipeline stage is stored, and only hs_is_closed_won counts;
  §11.17    the ledger summary equals an aggregation of the ledger rows;
  §11.8-11  currency provenance survives a round trip and never becomes 0;
  §11.18    the reconciliation audit exits non-zero on each invariant violation.

The suite spins up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user. If the binaries or that user are unavailable the module is
skipped rather than failing — and CI fails loudly on a skip, because a skipped
database suite is not merge evidence.

Read-only against every external platform; the only writes are local.

Run with:
    python -m pytest tests/test_pr_ads_153e_a_pg_integration.py -v
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
        self.tmp = tempfile.mkdtemp(prefix="pg153ea_")
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
        # Every sudo here passes -n for the same reason the skip probe does: a
        # password-protected sudo must fail immediately, not block the suite on
        # an invisible prompt with no tty to answer it.
        r = _run(["sudo", "-n", "-u", "postgres", os.path.join(_PG_BIN, "initdb"),
                  "-D", self.data, "-A", "trust", "-E", "UTF8"])
        if r.returncode != 0:
            raise RuntimeError(f"initdb failed: {r.stderr}")
        r = _run(["sudo", "-n", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
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
        _run(["sudo", "-n", "-u", "postgres", os.path.join(_PG_BIN, "pg_ctl"),
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
_T0 = "2026-06-01T00:00:00+00:00"     # oldest
_T1 = "2026-07-01T00:00:00+00:00"
_T2 = "2026-08-01T00:00:00+00:00"     # newest


def _ledger_row(deal_id="D1", *, won=True, closed=True,
                stage_id="326093516", stage_label="Deal Won / Payment Received",
                close_date="2026-07-10T00:00:00+00:00",
                modified=_T1, amount=1000.0, currency_code="USD",
                revenue_usd=1000.0, currency_status="verified_usd",
                currency_reason="deal_currency_is_usd",
                gclid=None, campaign="Brand - UK", country="AE",
                acquisition_group="google_ads",
                primary_contact_id="C1", association_count=1,
                association_status="resolved",
                attribution_status="attributed"):
    """One normalized ledger row, in the shape the sync service produces."""
    return {
        "deal_id": deal_id, "deal_name": f"Deal {deal_id}",
        "pipeline_id": "default", "deal_stage_id": stage_id,
        "deal_stage_label": stage_label,
        "hs_is_closed": closed, "hs_is_closed_won": won,
        "deal_created_at": "2026-05-01T00:00:00+00:00",
        "deal_close_date": close_date,
        "hubspot_lastmodified_at": modified,
        "amount_raw": amount, "deal_currency_code": currency_code,
        "amount_in_home_currency": amount, "home_currency_code": "USD",
        "revenue_usd": revenue_usd, "currency_status": currency_status,
        "currency_reason": currency_reason,
        "primary_contact_id": primary_contact_id,
        "association_count": association_count,
        "association_status": association_status,
        "association_reason": "single_associated_contact",
        "gclid": gclid, "campaign_name_raw": campaign, "keyword_raw": None,
        "country_raw": country, "source_primary_raw": "PAID_SEARCH",
        "source_detail_raw": campaign, "acquisition_group": acquisition_group,
        "attribution_status": attribution_status,
        "attribution_reason": "single_contact",
        "sync_batch_id": None,
        "source_fetched_at": "2026-08-16T00:00:00+00:00",
    }


def _assoc(contact_id, **kw):
    base = {"contact_id": str(contact_id), "association_type_id": "4",
            "association_label": "Primary", "is_primary": True,
            "primary_selection_reason": "single_associated_contact",
            "gclid": None, "campaign_name_raw": "Brand - UK",
            "keyword_raw": None, "country_raw": "AE",
            "source_primary_raw": "PAID_SEARCH",
            "source_detail_raw": "Brand - UK",
            "acquisition_group": "google_ads"}
    base.update(kw)
    return base


def _count(table) -> int:
    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return int(cur.fetchone()[0])


# ═════════════════════════════════════════════════════════════════════════════
# Schema
# ═════════════════════════════════════════════════════════════════════════════
def test_schema_creates_the_ledger_and_is_idempotent(pg):
    from db.connection import get_conn
    from db.schema import init_db

    init_db()   # second application must be a no-op
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table in ("hubspot_deal_ledger",
                          "hubspot_deal_contact_association",
                          "hubspot_deal_sync_state"):
                cur.execute("SELECT to_regclass(%s)", (table,))
                assert cur.fetchone()[0] is not None, table
            # deal_id is the PRIMARY KEY — the durable identity contract.
            cur.execute("""
                SELECT a.attname FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid
                                   AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'hubspot_deal_ledger'::regclass
                  AND i.indisprimary""")
            assert [r[0] for r in cur.fetchall()] == ["deal_id"]


def test_legacy_revenue_tables_are_untouched(pg):
    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            for table in ("gclid_attribution", "deal_source_attribution",
                          "deals"):
                cur.execute("SELECT to_regclass(%s)", (table,))
                assert cur.fetchone()[0] is not None, table


# ═════════════════════════════════════════════════════════════════════════════
# §11.3-4 — Idempotency by deal_id
# ═════════════════════════════════════════════════════════════════════════════
def test_3_reprocessing_a_deal_never_creates_a_duplicate(pg):
    from db import deal_ledger_repository as repo

    for _ in range(5):
        repo.upsert_deal(_ledger_row("D1"), associations=[_assoc("C1")])
    assert _count("hubspot_deal_ledger") == 1
    assert _count("hubspot_deal_contact_association") == 1


def test_4_campaign_and_source_relabelling_updates_the_same_deal(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D1", campaign="Brand - UK",
                                 acquisition_group="google_ads"),
                     associations=[_assoc("C1")])
    repo.upsert_deal(_ledger_row("D1", campaign="Gulf",
                                 acquisition_group="other_paid",
                                 modified=_T2),
                     associations=[_assoc("C1", campaign_name_raw="Gulf")])

    assert _count("hubspot_deal_ledger") == 1
    row = repo.fetch_deal("D1")["row"]
    assert row["campaign_name_raw"] == "Gulf"
    assert row["acquisition_group"] == "other_paid"


def test_4b_gclid_arriving_later_updates_the_same_deal(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D1", gclid=None), associations=[_assoc("C1")])
    repo.upsert_deal(_ledger_row("D1", gclid="Cj0abc", modified=_T2),
                     associations=[_assoc("C1", gclid="Cj0abc")])
    assert _count("hubspot_deal_ledger") == 1
    assert repo.fetch_deal("D1")["row"]["gclid"] == "Cj0abc"


# ═════════════════════════════════════════════════════════════════════════════
# §11.15 — Monotonic replay
# ═════════════════════════════════════════════════════════════════════════════
def test_15_older_replay_cannot_overwrite_newer_state(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D1", stage_label="Deal Won / Payment Received",
                                 won=True, modified=_T2),
                     associations=[_assoc("C1")])
    stale = repo.upsert_deal(
        _ledger_row("D1", stage_label="In Trials", stage_id="334269159",
                    won=False, modified=_T0),
        associations=[_assoc("C1")])

    assert stale["skipped_stale"] == 1
    assert stale["written"] == 0
    row = repo.fetch_deal("D1")["row"]
    assert row["hs_is_closed_won"] is True
    assert row["deal_stage_label"] == "Deal Won / Payment Received"


def test_15c_a_stale_replay_leaves_the_ASSOCIATION_BRIDGE_untouched(pg):
    """The bridge follows the ledger row. Replacing associations from an
    observation the ledger just rejected as old reintroduces exactly the
    out-of-order corruption the monotonic guard exists to prevent."""
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D1", modified=_T2),
                     associations=[_assoc("C1", campaign_name_raw="Brand - UK")])

    stale = repo.upsert_deal(
        _ledger_row("D1", modified=_T0, primary_contact_id="C2"),
        associations=[_assoc("C2", campaign_name_raw="Old Campaign")])

    assert stale["skipped_stale"] == 1
    rows = repo.fetch_associations("D1")["rows"]
    assert [r["contact_id"] for r in rows] == ["C1"], (
        "a stale replay replaced the association bridge")
    assert rows[0]["campaign_name_raw"] == "Brand - UK"


def test_15d_an_unknown_timestamp_cannot_overwrite_a_known_one(pg):
    """"We don't know when this was modified" cannot outrank "we know it was
    modified on Tuesday"."""
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D1", modified=_T2, won=True),
                     associations=[_assoc("C1")])

    nulled = repo.upsert_deal(
        _ledger_row("D1", modified=None, won=False, stage_id="379124201",
                    stage_label="Lost Deal"),
        associations=[_assoc("C1")])

    assert nulled["skipped_stale"] == 1
    assert nulled["written"] == 0
    row = repo.fetch_deal("D1")["row"]
    assert row["hs_is_closed_won"] is True
    assert row["deal_stage_label"] == "Deal Won / Payment Received"


def test_15e_an_unknown_timestamp_still_writes_a_new_row(pg):
    """A deal HubSpot never gave a modification date for must still be
    ingestible — the guard withholds overwrites, not the deal itself."""
    from db import deal_ledger_repository as repo

    first = repo.upsert_deal(_ledger_row("D_NEW", modified=None),
                             associations=[_assoc("C1")])
    assert first["written"] == 1
    assert repo.fetch_deal("D_NEW")["row"]["deal_id"] == "D_NEW"

    # Both sides unknown: still writable, since neither is evidence of recency.
    again = repo.upsert_deal(
        _ledger_row("D_NEW", modified=None, stage_label="Churn Deal",
                    stage_id="379124203", won=False),
        associations=[_assoc("C1")])
    assert again["written"] == 1
    assert repo.fetch_deal("D_NEW")["row"]["deal_stage_label"] == "Churn Deal"


def test_15b_newer_state_is_applied(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D1", won=True, modified=_T0),
                     associations=[_assoc("C1")])
    repo.upsert_deal(_ledger_row("D1", won=False, stage_id="379124203",
                                 stage_label="Churn Deal", modified=_T2),
                     associations=[_assoc("C1")])
    row = repo.fetch_deal("D1")["row"]
    assert row["hs_is_closed_won"] is False
    assert row["deal_stage_label"] == "Churn Deal"


# ═════════════════════════════════════════════════════════════════════════════
# §11.14 — A failed lookup never destroys prior evidence
# ═════════════════════════════════════════════════════════════════════════════
def test_14_failed_association_lookup_preserves_earlier_evidence(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(
        _ledger_row("D1", gclid="Cj0KEQ", campaign="Brand - UK", country="AE"),
        associations=[_assoc("C1", gclid="Cj0KEQ",
                             campaign_name_raw="Brand - UK")])
    assert _count("hubspot_deal_contact_association") == 1

    # A later sync whose association lookup FAILED. This is EXACTLY the row the
    # sync service builds in that case: `primary_contact_evidence` returns an
    # all-None evidence block, so every association-derived column arrives NULL.
    # The deal facts (stage, amount, currency) were read successfully and must
    # still update; the evidence must not.
    failed = _ledger_row("D1", modified=_T2, amount=2500.0, revenue_usd=2500.0,
                         primary_contact_id=None, association_count=None,
                         association_status="lookup_failed",
                         attribution_status="unavailable",
                         gclid=None, campaign=None, country=None,
                         acquisition_group=None)
    failed["source_primary_raw"] = None
    failed["source_detail_raw"] = None
    repo.upsert_deal(failed, associations=None, associations_observed=False)

    rows = repo.fetch_associations("D1")["rows"]
    assert len(rows) == 1, "prior association evidence was destroyed"
    assert rows[0]["contact_id"] == "C1"
    assert rows[0]["campaign_name_raw"] == "Brand - UK"

    row = repo.fetch_deal("D1")["row"]
    # The LEDGER ROW keeps its evidence too — not just the bridge. Zeroing these
    # would move the deal out of Google Ads on the next read purely because an
    # API call timed out.
    assert row["gclid"] == "Cj0KEQ", "a failed lookup erased the ledger GCLID"
    assert row["campaign_name_raw"] == "Brand - UK"
    assert row["country_raw"] == "AE"
    assert row["acquisition_group"] == "google_ads"
    assert row["source_primary_raw"] == "PAID_SEARCH"
    assert row["primary_contact_id"] == "C1"
    assert row["association_count"] == 1
    # Including the verdict itself: the row still describes the last conclusion
    # actually REACHED. The failed attempt is recorded in sync-state coverage,
    # not by demoting an established attribution to "unavailable".
    assert row["association_status"] == "resolved"
    assert row["attribution_status"] == "attributed"
    # ...while deal facts read successfully in the same run DID update.
    assert row["amount_raw"] == 2500.0
    assert row["revenue_usd"] == 2500.0


def test_14d_a_first_ever_failed_lookup_is_recorded_as_lookup_failed(pg):
    """There is nothing to preserve on a brand-new row, so the failure is
    stored — the audit must be able to see deals we have never resolved."""
    from db import deal_ledger_repository as repo

    repo.upsert_deal(
        _ledger_row("D_NEVER", primary_contact_id=None, association_count=None,
                    association_status="lookup_failed",
                    attribution_status="unavailable", gclid=None, campaign=None,
                    country=None, acquisition_group=None),
        associations=None, associations_observed=False)

    row = repo.fetch_deal("D_NEVER")["row"]
    assert row["association_status"] == "lookup_failed"
    assert row["attribution_status"] == "unavailable"
    assert row["gclid"] is None
    assert repo.fetch_associations("D_NEVER")["rows"] == []


def test_14e_failed_lookups_are_counted_in_sync_state_coverage(pg):
    """The preserved row must not make the failure invisible."""
    from db import deal_ledger_repository as repo

    repo.record_sync_state(status="partial", association_failures=3,
                           error="association_lookup_failed", deals_seen=10)
    state = repo.fetch_sync_state()["row"]
    assert state["association_failures"] == 3
    assert state["last_status"] == "partial"


def test_14b_successful_empty_observation_does_clear_associations(pg):
    """A SUCCESSFUL lookup that finds no contacts is a real fact and is applied
    — the distinction from a failure is the whole point."""
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D1"), associations=[_assoc("C1")])
    repo.upsert_deal(_ledger_row("D1", modified=_T2, association_status="none",
                                 association_count=0, primary_contact_id=None),
                     associations=[], associations_observed=True)
    assert repo.fetch_associations("D1")["rows"] == []


def test_13_ambiguous_deal_retains_every_association(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(
        _ledger_row("D1", primary_contact_id=None, association_count=2,
                    association_status="ambiguous",
                    attribution_status="ambiguous", campaign=None,
                    country=None, acquisition_group=None),
        associations=[_assoc("C1", is_primary=False, campaign_name_raw="Brand - UK"),
                      _assoc("C2", is_primary=False, campaign_name_raw="Gulf")])

    rows = repo.fetch_associations("D1")["rows"]
    assert len(rows) == 2
    assert {r["campaign_name_raw"] for r in rows} == {"Brand - UK", "Gulf"}
    # And the ledger row displays no single campaign as if it were the answer.
    assert repo.fetch_deal("D1")["row"]["campaign_name_raw"] is None


# ═════════════════════════════════════════════════════════════════════════════
# §11.1-2, §11.5-7, §11.16 — Population and the won predicate
# ═════════════════════════════════════════════════════════════════════════════
def test_1_non_gclid_won_deal_is_in_the_canonical_population(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D_GCLID", gclid="Cj0abc"),
                     associations=[_assoc("C1")])
    repo.upsert_deal(_ledger_row("D_NO_GCLID", gclid=None),
                     associations=[_assoc("C2")])

    summary = repo.fetch_ledger_summary()["summary"]
    assert summary["won_deals"] == 2
    assert summary["won_with_gclid"] == 1
    assert summary["won_without_gclid"] == 1
    # Both contribute revenue — the legacy GCLID ledger could hold only one.
    assert summary["revenue_usd"] == 2000.0


def test_5_stage_label_saying_won_does_not_make_a_deal_won(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(
        _ledger_row("D_FAKE", won=False,
                    stage_label="Deal Won / Payment Received",
                    revenue_usd=5000.0),
        associations=[_assoc("C1")])
    summary = repo.fetch_ledger_summary()["summary"]
    assert summary["won_deals"] == 0
    assert summary["revenue_usd"] is None


def test_7_unknown_won_state_is_stored_as_unknown_not_false(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D_UNKNOWN", won=None),
                     associations=[_assoc("C1")])
    summary = repo.fetch_ledger_summary()["summary"]
    assert summary["won_deals"] == 0            # fails closed
    assert summary["unknown_won_deals"] == 1    # and is counted, not hidden


def test_16_all_stages_are_stored_and_only_won_counts(pg):
    from db import deal_ledger_repository as repo

    stages = [
        ("D_OPEN", "334269159", "In Trials", False, False),
        ("D_LOST", "379124201", "Lost Deal", True, False),
        ("D_DOWN", "379124202", "Downgrade Deal", True, False),
        ("D_CHURN", "379124203", "Churn Deal", True, False),
        ("D_WON", "326093516", "Deal Won / Payment Received", True, True),
    ]
    for deal_id, stage_id, label, closed, won in stages:
        repo.upsert_deal(
            _ledger_row(deal_id, stage_id=stage_id, stage_label=label,
                        closed=closed, won=won),
            associations=[_assoc("C1")])

    breakdown = {r["deal_stage_label"]: r["deals"]
                 for r in repo.fetch_stage_breakdown()["rows"]}
    for _, _, label, _, _ in stages:
        assert label in breakdown, label

    summary = repo.fetch_ledger_summary()["summary"]
    assert summary["distinct_deals"] == 5
    assert summary["won_deals"] == 1
    assert summary["revenue_usd"] == 1000.0


# ═════════════════════════════════════════════════════════════════════════════
# §11.8-11 — Currency provenance round trip
# ═════════════════════════════════════════════════════════════════════════════
def test_8_unavailable_currency_is_withheld_not_zeroed(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(
        _ledger_row("D_NOCCY", amount=750.0, currency_code=None,
                    revenue_usd=None, currency_status="unavailable",
                    currency_reason="unknown_currency"),
        associations=[_assoc("C1")])

    row = repo.fetch_deal("D_NOCCY")["row"]
    assert row["revenue_usd"] is None       # NOT 0.0
    assert row["amount_raw"] == 750.0       # the raw amount is preserved
    summary = repo.fetch_ledger_summary()["summary"]
    assert summary["revenue_usd"] is None
    assert summary["won_currency_unavailable"] == 1
    assert summary["won_currency_proven"] == 0


def test_8b_unproven_currency_never_enters_the_usd_total(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D_OK", amount=100.0, revenue_usd=100.0),
                     associations=[_assoc("C1")])
    repo.upsert_deal(
        _ledger_row("D_BAD", amount=900.0, currency_code="XYZ",
                    revenue_usd=None, currency_status="unavailable",
                    currency_reason="no_fx_rate_for_close_date"),
        associations=[_assoc("C2")])

    summary = repo.fetch_ledger_summary()["summary"]
    assert summary["revenue_usd"] == 100.0          # only the proven deal
    assert summary["won_currency_unavailable"] == 1
    # The raw total is reported separately and is explicitly NOT a USD figure.
    assert summary["amount_raw_total"] == 1000.0


def test_10_converted_currency_round_trips(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(
        _ledger_row("D_GBP", amount=100.0, currency_code="GBP",
                    revenue_usd=126.0, currency_status="converted",
                    currency_reason="converted_from_gbp_at_close_date"),
        associations=[_assoc("C1")])
    row = repo.fetch_deal("D_GBP")["row"]
    assert row["revenue_usd"] == 126.0
    assert row["amount_raw"] == 100.0
    assert row["deal_currency_code"] == "GBP"
    assert repo.fetch_ledger_summary()["summary"]["revenue_usd"] == 126.0


# ═════════════════════════════════════════════════════════════════════════════
# §11.17 — Summary equals the aggregation of rows
# ═════════════════════════════════════════════════════════════════════════════
def test_17_summary_equals_aggregation_of_ledger_rows(pg):
    from db import deal_ledger_repository as repo

    for i in range(7):
        repo.upsert_deal(
            _ledger_row(f"D{i}", won=(i % 2 == 0), amount=100.0 * (i + 1),
                        revenue_usd=100.0 * (i + 1)),
            associations=[_assoc(f"C{i}")])

    summary = repo.fetch_ledger_summary()["summary"]
    rows = repo.fetch_ledger_rows(won_only=True)["rows"]
    assert summary["won_deals"] == len(rows)
    assert summary["revenue_usd"] == sum(r["revenue_usd"] for r in rows)


# ═════════════════════════════════════════════════════════════════════════════
# Sync state
# ═════════════════════════════════════════════════════════════════════════════
def test_watermark_advances_only_on_success(pg):
    from db import deal_ledger_repository as repo

    repo.record_sync_state(status="success", watermark=_T1, deals_seen=3)
    assert repo.fetch_sync_state()["row"]["last_modified_watermark"].startswith(
        "2026-07-01")

    # A partial run must not move the watermark past deals it never read.
    repo.record_sync_state(status="partial", watermark=_T2, deals_seen=1,
                           error="association_lookup_cap_reached")
    state = repo.fetch_sync_state()["row"]
    assert state["last_modified_watermark"].startswith("2026-07-01")
    assert state["last_status"] == "partial"
    assert state["last_error"] == "association_lookup_cap_reached"


def test_failed_sync_is_recorded_as_failed(pg):
    from db import deal_ledger_repository as repo

    repo.record_sync_state(status="failed", error="pull_failed: boom")
    state = repo.fetch_sync_state()["row"]
    assert state["last_status"] == "failed"
    assert state["deals_seen"] == 0     # zero rows AND an explicit failure


# ═════════════════════════════════════════════════════════════════════════════
# §11.18 — The reconciliation audit gate
# ═════════════════════════════════════════════════════════════════════════════
def _run_audit(window="all_time", *, json_mode=False):
    import importlib

    audit = importlib.import_module("scripts.audit_canonical_revenue_truth")
    importlib.reload(audit)
    argv = ["audit", "--window", window] + (["--json"] if json_mode else [])
    old = sys.argv
    sys.argv = argv
    try:
        return audit.main()
    finally:
        sys.argv = old


def _insert_legacy_source_row(deal_id, *, amount=1000.0,
                              close_date="2026-07-10T00:00:00+00:00"):
    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO deal_source_attribution "
                "(deal_id, acquisition_group, attribution_status, "
                " deal_close_date, deal_amount_usd) "
                "VALUES (%s, 'google_ads', 'attributed', %s, %s)",
                (deal_id, close_date, amount))
        conn.commit()


def _healthy_ledger():
    """A fully reconciled shadow state.

    The deal has no GCLID, so its absence from `gclid_attribution` is the
    EXPECTED structural difference. It IS present in `deal_source_attribution`,
    which is deal-keyed and has no such excuse — a canonical won deal missing
    from that ledger is unexplained and fails the gate.
    """
    from db import deal_ledger_repository as repo

    repo.upsert_deal(_ledger_row("D1", gclid=None), associations=[_assoc("C1")])
    _insert_legacy_source_row("D1")
    repo.record_sync_state(status="success", watermark=_T2, deals_seen=1)


def test_18_audit_passes_on_a_reconciled_ledger(pg):
    _healthy_ledger()
    assert _run_audit() == 0
    assert _run_audit(json_mode=True) == 0


def test_18b_audit_fails_when_a_legacy_deal_is_missing_from_canonical(pg):
    """The one difference direction that is never expected: it means the
    canonical sync missed a deal."""
    from db.connection import get_conn

    _healthy_ledger()
    assert _run_audit() == 0
    _insert_legacy_source_row("D_LEGACY_ONLY", amount=500)
    assert _run_audit() == 1

    # And it is reported as MISSING, not as a classification disagreement.
    from services.revenue_reconciliation_service import (
        REASON_MISSING_FROM_CANONICAL, build_revenue_reconciliation,
    )
    report = build_revenue_reconciliation("all_time")
    diff = next(d for d in report["legacy_diffs"]
                if d["ledger"] == "deal_source_attribution")
    assert [r["deal_id"] for r in diff["legacy_only"]] == ["D_LEGACY_ONLY"]
    assert diff["legacy_only"][0]["reason"] == REASON_MISSING_FROM_CANONICAL
    assert diff["won_disagreement"] == []
    assert get_conn is not None   # the fixture's connection factory is live


def test_18c_audit_fails_when_a_failed_sync_is_the_latest_state(pg):
    from db import deal_ledger_repository as repo

    _healthy_ledger()
    assert _run_audit() == 0
    repo.record_sync_state(status="failed", error="pull_failed")
    assert _run_audit() == 1


def test_18d_audit_fails_when_an_unproven_currency_carries_revenue(pg):
    """revenue_usd set while the currency was never proven — the exact
    unverified-USD assumption this PR removes."""
    from db import deal_ledger_repository as repo

    repo.upsert_deal(
        _ledger_row("D_LEAK", revenue_usd=999.0,
                    currency_status="unavailable",
                    currency_reason="unknown_currency"),
        associations=[_assoc("C1")])
    repo.record_sync_state(status="success", watermark=_T2, deals_seen=1)
    assert _run_audit() == 1


def test_18e_audit_fails_when_a_failed_lookup_is_reported_as_unclassified(pg):
    from db import deal_ledger_repository as repo

    repo.upsert_deal(
        _ledger_row("D_MISREPORTED", association_status="lookup_failed",
                    attribution_status="unclassified", primary_contact_id=None),
        associations=None, associations_observed=False)
    repo.record_sync_state(status="success", watermark=_T2, deals_seen=1)
    assert _run_audit() == 1


def test_audit_itemizes_non_gclid_deals_as_expected_differences(pg):
    """A non-GCLID canonical deal absent from the legacy GCLID ledger is an
    EXPECTED difference, and must be named as such rather than left unexplained."""
    from services.revenue_reconciliation_service import (
        build_revenue_reconciliation,
    )

    _healthy_ledger()
    report = build_revenue_reconciliation("all_time")
    gclid_diff = next(d for d in report["legacy_diffs"]
                      if d["ledger"] == "gclid_attribution")
    only = gclid_diff["canonical_only"]
    assert len(only) == 1
    assert only[0]["deal_id"] == "D1"
    assert only[0]["reason"] == "non_gclid_deal_excluded_by_legacy_ledger"
    assert only[0]["has_gclid"] is False
    # And it does not fail the gate — it is the defect this PR exists to fix.
    assert report["ok"] is True


def test_audit_reports_no_contact_pii(pg):
    import json as _json

    from services.revenue_reconciliation_service import (
        build_revenue_reconciliation,
    )

    _healthy_ledger()
    blob = _json.dumps(build_revenue_reconciliation("all_time"), default=str).lower()
    for banned in ("email", "@", "firstname", "lastname"):
        assert banned not in blob, banned


def test_reconciliation_is_shadow_mode(pg):
    from services.revenue_reconciliation_service import (
        build_revenue_reconciliation,
    )

    _healthy_ledger()
    gov = build_revenue_reconciliation("all_time")["governance"]
    assert gov["shadow_mode"] is True
    assert gov["read_only"] is True
    assert gov["external_writes"] is False


def test_won_disagreement_is_separated_from_a_missing_deal(pg):
    """A deal the canonical ledger HOLDS but classifies as lost is a
    classification disagreement, not a gap in the sync. Reporting it as
    "missing from canonical" sends an operator hunting a bug that is not there.
    """
    from db import deal_ledger_repository as repo
    from services.revenue_reconciliation_service import (
        REASON_LEGACY_PREDICATE_FALSE_POSITIVE, build_revenue_reconciliation,
    )

    _healthy_ledger()
    # Canonically NOT won, but held — and the legacy source ledger counts it.
    repo.upsert_deal(
        _ledger_row("D_LOST", won=False, stage_id="379124201",
                    stage_label="Lost Deal"),
        associations=[_assoc("C2")])
    _insert_legacy_source_row("D_LOST", amount=750)

    report = build_revenue_reconciliation("all_time")
    diff = next(d for d in report["legacy_diffs"]
                if d["ledger"] == "deal_source_attribution")

    assert diff["legacy_only"] == [], "a held deal was reported as missing"
    item, = diff["won_disagreement"]
    assert item["deal_id"] == "D_LOST"
    assert item["canonical_is_closed_won"] is False
    assert item["reason"] == REASON_LEGACY_PREDICATE_FALSE_POSITIVE
    # The legacy predicate's false positives are the known, expected defect.
    assert item["expected"] is True
    assert report["ok"] is True
    assert _run_audit() == 0


def test_a_gclid_won_deal_missing_from_the_legacy_gclid_ledger_fails(pg):
    """Absent WITHOUT the structural excuse: the GCLID ledger can hold this deal
    and does not."""
    from db import deal_ledger_repository as repo
    from services.revenue_reconciliation_service import (
        REASON_GCLID_DEAL_MISSING_FROM_LEGACY, build_revenue_reconciliation,
    )

    repo.upsert_deal(_ledger_row("D_G", gclid="Cj0KEQ"),
                     associations=[_assoc("C1", gclid="Cj0KEQ")])
    _insert_legacy_source_row("D_G")
    repo.record_sync_state(status="success", watermark=_T2, deals_seen=1)

    report = build_revenue_reconciliation("all_time")
    diff = next(d for d in report["legacy_diffs"]
                if d["ledger"] == "gclid_attribution")
    item, = diff["canonical_only"]
    assert item["reason"] == REASON_GCLID_DEAL_MISSING_FROM_LEGACY
    assert item["expected"] is False
    assert report["ok"] is False
    assert _run_audit() == 1


def test_duplicate_legacy_rows_are_counted_only_inside_the_window(pg):
    from datetime import datetime, timezone

    from db import deal_ledger_repository as repo
    from db.connection import get_conn
    from services.revenue_reconciliation_service import (
        build_revenue_reconciliation,
    )

    _healthy_ledger()
    with get_conn() as conn:
        with conn.cursor() as cur:
            for i, close in enumerate(("2026-07-10T00:00:00+00:00",
                                       "2026-07-10T00:00:00+00:00",
                                       "2019-01-01T00:00:00+00:00",
                                       "2019-01-01T00:00:00+00:00")):
                cur.execute(
                    "INSERT INTO gclid_attribution "
                    "(attribution_key, deal_id, deal_amount_usd, "
                    " deal_close_date, deal_stage, deal_stage_label, gclid, "
                    " created_at) "
                    "VALUES (%s, %s, 100, %s, '326093516', "
                    "        'Deal Won / Payment Received', 'g', NOW())",
                    (f"key{i}", "D_DUP" if i < 2 else "D_OLD_DUP", close))
        conn.commit()

    # A window that contains only the 2026 pair.
    q3 = build_revenue_reconciliation(
        "current_quarter", now=datetime(2026, 7, 15, tzinfo=timezone.utc))
    diff = next(d for d in q3["legacy_diffs"]
                if d["ledger"] == "gclid_attribution")
    dupes = {d["deal_id"] for d in diff["duplicate_legacy_rows"]}
    assert dupes == {"D_DUP"}, (
        "the duplicate scan is unwindowed and reported deals outside the "
        f"reconciled window: {dupes}")
    assert repo.fetch_sync_state()["available"] is True


def test_sync_state_checkpoint_advances_the_watermark_on_a_capped_run(pg):
    """A capped backfill must be able to resume. The watermark still refuses to
    move for a plain partial or a failure."""
    from db import deal_ledger_repository as repo

    repo.record_sync_state(status="success", watermark=_T0, deals_seen=1)
    assert repo.fetch_sync_state()["row"]["last_modified_watermark"].startswith(
        "2026-06-01")

    # A partial run with NO checkpoint leaves it alone.
    repo.record_sync_state(status="partial", watermark=_T2, deals_seen=1)
    assert repo.fetch_sync_state()["row"]["last_modified_watermark"].startswith(
        "2026-06-01")

    # A partial run that cleanly processed a prefix DOES advance to it.
    repo.record_sync_state(status="partial", watermark=_T1, deals_seen=1,
                           watermark_is_checkpoint=True)
    assert repo.fetch_sync_state()["row"]["last_modified_watermark"].startswith(
        "2026-07-01")

    # A failure never advances, checkpoint flag or not.
    repo.record_sync_state(status="failed", watermark=_T2,
                           watermark_is_checkpoint=True, error="boom")
    assert repo.fetch_sync_state()["row"]["last_modified_watermark"].startswith(
        "2026-07-01")
