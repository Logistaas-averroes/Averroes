"""
tests/test_pr_ads_153c_pg_integration.py

PR-ADS-153C — PostgreSQL-backed integration tests for the canonical Leads page.

These prove behaviour that source-string assertions cannot:

  §29  the contact table is genuinely server-side paginated and bounded — the
       browser never receives more than one page;
  §30  default ordering is newest-relevant-event-first and deterministic, so
       paging can neither repeat nor skip a contact;
  §7   stage views are historical cohorts: a current Customer is still returned
       by the SQL view for the window it entered Sales Qualified Lead;
  §11  scope filtering happens in SQL via pre-resolved allow-lists, and a scope
       whose identity contract is unavailable is withheld rather than emptied;
  §12  acquisition-group filtering reuses the established source taxonomy;
  §8   operational status counts are a working dimension, not a funnel stage.

The tests spin up a throwaway PostgreSQL 16 cluster owned by the unprivileged
``postgres`` OS user. If the server binaries or that user are unavailable the
whole module is skipped rather than failing.

Run with:
    python -m pytest tests/test_pr_ads_153b_pg_integration.py -v
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
        self.tmp = tempfile.mkdtemp(prefix="pgst153c_")
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
def _contact(contact_id="1", *, lifecyclestage=None, mql_status=None,
             created="2026-01-05T00:00:00Z", modified="2026-07-01T00:00:00Z",
             entered_lead=None, entered_mql=None, entered_sql=None,
             entered_opportunity=None, entered_customer=None,
             source="PAID_SEARCH", campaign="Brand - US", company=None):
    return {"id": contact_id, "properties": {
        "lifecyclestage": lifecyclestage,
        "mql_status": mql_status,
        "createdate": created,
        "lastmodifieddate": modified,
        "hs_v2_date_entered_lead": entered_lead,
        "hs_v2_date_entered_marketingqualifiedlead": entered_mql,
        "hs_v2_date_entered_salesqualifiedlead": entered_sql,
        "hs_v2_date_entered_opportunity": entered_opportunity,
        "hs_v2_date_entered_customer": entered_customer,
        "hs_analytics_source": source,
        "hs_analytics_source_data_1": campaign,
        "hs_analytics_source_data_2": "tms",
        "company": company or f"Co {contact_id}",
    }}


def _write(contacts):
    from connectors.hubspot_pull import normalize_contact_funnel_row
    from db import writers as db_writers

    rows = [normalize_contact_funnel_row(c) for c in contacts]
    result = db_writers.upsert_hubspot_contact_funnel([r for r in rows if r])
    assert result["ok"] is True, result
    return result


def _pages(*contact_pages, complete=True):
    """Fake iterator that mirrors the real connector's completion sentinel."""
    def iterator(since, max_pages=None):
        for index, page in enumerate(contact_pages):
            yield page, {"watermark_ms": 0, "page_index": index, "complete": False}
        if complete:
            yield [], {"watermark_ms": 0, "complete": True}
    return iterator




def _scalar(pg, sql, params=None):
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None


def _seed(contacts):
    """Persist canonical contacts through the real normaliser + writer."""
    from connectors.hubspot_pull import normalize_contact_funnel_row
    from db import writers as db_writers

    rows = [normalize_contact_funnel_row(c) for c in contacts]
    result = db_writers.upsert_hubspot_contact_funnel([r for r in rows if r])
    assert result["ok"] is True, result
    return result


_Q3 = (date(2026, 7, 1), date(2026, 9, 30))


# =============================================================================
# §29 — Server-side pagination
# =============================================================================
def test_contact_page_is_bounded_and_paginated(pg):
    from db import crm_funnel_repository as repo

    _seed([_contact(str(i), entered_sql=f"2026-07-{(i % 28) + 1:02d}T00:00:00Z")
           for i in range(1, 26)])

    first = repo.fetch_funnel_contact_page("sql", *_Q3, page=1, page_size=10)
    assert first["available"] is True
    assert len(first["rows"]) == 10
    assert first["total"] == 25
    assert first["has_more"] is True

    last = repo.fetch_funnel_contact_page("sql", *_Q3, page=3, page_size=10)
    assert len(last["rows"]) == 5
    assert last["has_more"] is False


def test_page_size_is_capped_server_side(pg):
    from db import crm_funnel_repository as repo

    _seed([_contact(str(i), entered_sql="2026-07-10T00:00:00Z") for i in range(1, 6)])
    page = repo.fetch_funnel_contact_page("sql", *_Q3, page=1, page_size=10_000)
    assert page["page_size"] == repo.MAX_CONTACT_PAGE_SIZE


def test_paging_never_repeats_or_skips_a_contact(pg):
    """Deterministic ordering: the union of pages is exactly the population."""
    from db import crm_funnel_repository as repo

    # Several contacts deliberately SHARE an event date, so the tiebreak matters.
    _seed([_contact(str(i), entered_sql="2026-07-10T00:00:00Z") for i in range(1, 13)])

    seen = []
    for page_number in (1, 2, 3):
        page = repo.fetch_funnel_contact_page(
            "sql", *_Q3, page=page_number, page_size=5)
        seen.extend(r["contact_id"] for r in page["rows"])

    assert len(seen) == 12
    assert len(set(seen)) == 12


# =============================================================================
# §30 — Ordering
# =============================================================================
def test_default_ordering_is_newest_event_first(pg):
    from db import crm_funnel_repository as repo

    _seed([
        _contact("old", entered_sql="2026-07-02T00:00:00Z"),
        _contact("mid", entered_sql="2026-08-02T00:00:00Z"),
        _contact("new", entered_sql="2026-09-02T00:00:00Z"),
    ])
    page = repo.fetch_funnel_contact_page("sql", *_Q3)
    assert [r["contact_id"] for r in page["rows"]] == ["new", "mid", "old"]


def test_each_view_orders_by_its_own_event_date(pg):
    """The MQL tab orders by MQL date, not by SQL date or createdate."""
    from db import crm_funnel_repository as repo

    _seed([
        _contact("a", entered_mql="2026-09-01T00:00:00Z",
                 entered_sql="2026-07-01T00:00:00Z"),
        _contact("b", entered_mql="2026-07-01T00:00:00Z",
                 entered_sql="2026-09-01T00:00:00Z"),
    ])
    assert [r["contact_id"] for r in
            repo.fetch_funnel_contact_page("mql", *_Q3)["rows"]] == ["a", "b"]
    assert [r["contact_id"] for r in
            repo.fetch_funnel_contact_page("sql", *_Q3)["rows"]] == ["b", "a"]


def test_ordering_ignores_contact_creation_date(pg):
    from db import crm_funnel_repository as repo

    _seed([
        _contact("newest_created", created="2026-06-30T00:00:00Z",
                 entered_sql="2026-07-02T00:00:00Z"),
        _contact("oldest_created", created="2024-01-01T00:00:00Z",
                 entered_sql="2026-09-02T00:00:00Z"),
    ])
    rows = repo.fetch_funnel_contact_page("sql", *_Q3)["rows"]
    assert rows[0]["contact_id"] == "oldest_created"


# =============================================================================
# §7 — Historical cohorts
# =============================================================================
def test_current_customer_is_still_returned_by_its_sql_view(pg):
    from db import crm_funnel_repository as repo

    _seed([_contact("1", lifecyclestage="customer",
                    entered_sql="2026-07-10T00:00:00Z",
                    entered_customer="2026-11-02T00:00:00Z")])
    rows = repo.fetch_funnel_contact_page("sql", *_Q3)["rows"]
    assert [r["contact_id"] for r in rows] == ["1"]
    assert rows[0]["lifecycle_stage"] == "customer"
    # ...and it is NOT in the Q3 customer view, because that happened in Q4.
    assert repo.fetch_funnel_contact_page("customer", *_Q3)["total"] == 0


def test_a_contact_appears_in_every_stage_cohort_it_entered(pg):
    from db import crm_funnel_repository as repo

    _seed([_contact("1", lifecyclestage="customer",
                    entered_lead="2026-07-01T00:00:00Z",
                    entered_mql="2026-07-05T00:00:00Z",
                    entered_sql="2026-07-10T00:00:00Z",
                    entered_opportunity="2026-07-20T00:00:00Z",
                    entered_customer="2026-08-01T00:00:00Z")])
    for event in ("lead", "mql", "sql", "opportunity", "customer"):
        assert repo.fetch_funnel_contact_page(event, *_Q3)["total"] == 1, event


def test_missing_stage_date_excludes_the_contact_from_that_view(pg):
    from db import crm_funnel_repository as repo

    _seed([_contact("1", created="2026-07-05T00:00:00Z")])  # no stage evidence
    for event in ("lead", "mql", "sql", "opportunity", "customer"):
        assert repo.fetch_funnel_contact_page(event, *_Q3)["total"] == 0, event


# =============================================================================
# §11/§12 — Scope + source filtering, server-side
# =============================================================================
def test_acquisition_group_filter_uses_the_canonical_taxonomy(pg):
    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as funnel

    _seed([
        _contact("g", source="PAID_SEARCH", entered_sql="2026-07-10T00:00:00Z"),
        _contact("o", source="ORGANIC_SEARCH", entered_sql="2026-07-11T00:00:00Z"),
        _contact("s", source="PAID_SOCIAL", entered_sql="2026-07-12T00:00:00Z"),
    ])
    facets = repo.fetch_distinct_facets()
    google = funnel.resolve_source_allowlist("google_ads", facets["sources"])
    page = repo.fetch_funnel_contact_page("sql", *_Q3, sources_in=google)
    assert [r["contact_id"] for r in page["rows"]] == ["g"]


def test_empty_allowlist_yields_an_empty_page_not_an_ignored_filter(pg):
    """A classifier that matched nothing must filter everything out."""
    from db import crm_funnel_repository as repo

    _seed([_contact("1", entered_sql="2026-07-10T00:00:00Z")])
    page = repo.fetch_funnel_contact_page("sql", *_Q3, sources_in=[])
    assert page["total"] == 0


def test_keyword_scope_requires_a_keyword_label(pg):
    from db import crm_funnel_repository as repo

    _seed([_contact("1", entered_sql="2026-07-10T00:00:00Z")])
    with_kw = repo.fetch_funnel_contact_page("sql", *_Q3, require_keyword=True)
    assert with_kw["total"] == 1

    # Blank the keyword and the contact leaves the keyword scope.
    _seed([{**_contact("2", entered_sql="2026-07-11T00:00:00Z"),
            "properties": {**_contact("2", entered_sql="2026-07-11T00:00:00Z")["properties"],
                           "hs_analytics_source_data_2": ""}}])
    page = repo.fetch_funnel_contact_page("sql", *_Q3, require_keyword=True)
    assert "2" not in [r["contact_id"] for r in page["rows"]]


def test_company_search_and_status_filter_are_applied_in_sql(pg):
    from db import crm_funnel_repository as repo

    _seed([
        _contact("1", company="Acme Freight", mql_status="OPEN - Connecting",
                 entered_sql="2026-07-10T00:00:00Z"),
        _contact("2", company="Globex", mql_status="CLOSED - Job Seeker",
                 entered_sql="2026-07-11T00:00:00Z"),
    ])
    by_company = repo.fetch_funnel_contact_page("sql", *_Q3, company_query="acme")
    assert [r["contact_id"] for r in by_company["rows"]] == ["1"]

    by_status = repo.fetch_funnel_contact_page(
        "sql", *_Q3, operational_status="open_working")
    assert [r["contact_id"] for r in by_status["rows"]] == ["1"]


def test_service_contacts_end_to_end_returns_admin_safe_rows(pg):
    from services import canonical_crm_funnel_service as funnel

    _seed([_contact("1", lifecyclestage="customer", company="Acme Freight",
                    entered_sql="2026-07-10T00:00:00Z",
                    entered_customer="2026-08-01T00:00:00Z")])
    payload = funnel.contacts(
        "business", "current_quarter", event="sql",
        now=datetime(2026, 8, 15, tzinfo=timezone.utc))

    assert payload["available"] is True
    assert payload["total"] == 1
    row = payload["rows"][0]
    assert row["company"] == "Acme Freight"
    assert row["event_date"] == "2026-07-10"
    assert row["lifecycle_stage"] == "customer"       # current state, separate
    assert row["stage_dates"]["customer"] == "2026-08-01"
    assert "email" not in row


# =============================================================================
# §8 — Operational status counts
# =============================================================================
def test_operational_status_counts_are_grouped_server_side(pg):
    from services import canonical_crm_funnel_service as funnel

    _seed([
        _contact("1", mql_status="OPEN - Connecting",
                 entered_lead="2026-07-01T00:00:00Z"),
        _contact("2", mql_status="OPEN - Meeting Booked",
                 entered_lead="2026-07-02T00:00:00Z"),
        _contact("3", mql_status="CLOSED - Bad Product Fit",
                 entered_lead="2026-07-03T00:00:00Z"),
        _contact("4", mql_status=None, entered_lead="2026-07-04T00:00:00Z"),
    ])
    payload = funnel.operational_status_breakdown(
        "business", "current_quarter", event="lead",
        now=datetime(2026, 8, 15, tzinfo=timezone.utc))

    assert payload["available"] is True
    counts = payload["counts"]
    assert counts["open_working"] == 2
    assert counts["bad_fit"] == 1
    assert counts["no_verdict"] == 1


def test_unavailable_database_is_not_reported_as_an_empty_page(pg, monkeypatch):
    from db import crm_funnel_repository as repo

    class _NullConn:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(repo, "get_conn", lambda: _NullConn())
    page = repo.fetch_funnel_contact_page("sql", *_Q3)
    assert page["available"] is False
    assert page["total"] == 0
    assert page["rows"] == []
