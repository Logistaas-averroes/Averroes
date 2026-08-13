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
    python -m pytest tests/test_pr_ads_153c_pg_integration.py -v
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
    google = funnel.resolve_source_pair_allowlist("google_ads", facets["source_pairs"])
    page = repo.fetch_funnel_contact_page("sql", *_Q3, source_pairs_in=google)
    assert [r["contact_id"] for r in page["rows"]] == ["g"]


def test_empty_allowlist_yields_an_empty_page_not_an_ignored_filter(pg):
    """A classifier that matched nothing must filter everything out."""
    from db import crm_funnel_repository as repo

    _seed([_contact("1", entered_sql="2026-07-10T00:00:00Z")])
    page = repo.fetch_funnel_contact_page("sql", *_Q3, source_pairs_in=[])
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


# ─────────────────────────────────────────────────────────────────────────────
# PR-ADS-153C follow-up — the FULL source contract survives the trip into SQL
# ─────────────────────────────────────────────────────────────────────────────
# "Offline Sources" is the case that proves it: the primary alone is ambiguous,
# and only the drill-down separates SalesNash / Events (Other Paid) from
# reseller / referral / direct email (Organic) from a plain CRM migration
# (Offline). If the allow-list collapsed to the primary, all three would filter
# together.

_OFFLINE_FIXTURE = [
    ("events",    "Offline Sources", "Events",        "other_paid"),
    ("salesnash", "Offline Sources", "SalesNash",     "other_paid"),
    ("reseller",  "Offline Sources", "reseller",      "organic"),
    ("referral",  "Offline Sources", "referral",      "organic"),
    ("email",     "Offline Sources", "direct email",  "organic"),
    ("migration", "Offline Sources", "CRM migration", "offline"),
    ("social",    "PAID_SOCIAL",     "linkedin",      "other_paid"),
    ("organic",   "ORGANIC_SEARCH",  "google",        "organic"),
    ("ads",       "PAID_SEARCH",     "Brand - US",    "google_ads"),
]


def _seed_source_fixture(day_start=5):
    return _seed([
        _contact(cid, source=primary, campaign=detail,
                 entered_sql=f"2026-07-{day_start + i:02d}T00:00:00Z")
        for i, (cid, primary, detail, _group) in enumerate(_OFFLINE_FIXTURE)
    ])


@pytest.mark.parametrize("group,expected", [
    ("other_paid", {"events", "salesnash", "social"}),
    ("organic", {"reseller", "referral", "email", "organic"}),
    ("offline", {"migration"}),
    ("google_ads", {"ads"}),
])
def test_pair_allowlist_filters_offline_sources_by_its_drilldown(pg, group, expected):
    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as funnel

    _seed_source_fixture()
    facets = repo.fetch_distinct_facets()
    allowed = funnel.resolve_source_pair_allowlist(group, facets["source_pairs"])
    page = repo.fetch_funnel_contact_page("sql", *_Q3, source_pairs_in=allowed)
    assert {r["contact_id"] for r in page["rows"]} == expected
    assert page["total"] == len(expected)


def test_same_primary_source_splits_across_groups_in_sql(pg):
    """All six rows share 'Offline Sources'; SQL still separates them."""
    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as funnel

    _seed_source_fixture()
    facets = repo.fetch_distinct_facets()
    seen = set()
    for group in ("other_paid", "organic", "offline"):
        allowed = funnel.resolve_source_pair_allowlist(group, facets["source_pairs"])
        page = repo.fetch_funnel_contact_page("sql", *_Q3, source_pairs_in=allowed)
        ids = {r["contact_id"] for r in page["rows"]}
        assert not (ids & seen), "a contact was counted in two groups"
        seen |= ids
    assert {"events", "salesnash", "reseller", "referral", "email",
            "migration"} <= seen


def test_null_source_evidence_still_matches_its_allowlist_entry(pg):
    """NULL = NULL is unknown in SQL; the predicate must use IS NOT DISTINCT FROM."""
    from db import crm_funnel_repository as repo
    from services import canonical_crm_funnel_service as funnel

    _seed([
        _contact("none", source=None, campaign=None,
                 entered_sql="2026-07-05T00:00:00Z"),
        _contact("ads", source="PAID_SEARCH", campaign="Brand - US",
                 entered_sql="2026-07-06T00:00:00Z"),
    ])
    facets = repo.fetch_distinct_facets()
    assert (None, None) in facets["source_pairs"]
    allowed = funnel.resolve_source_pair_allowlist(
        "unclassified", facets["source_pairs"])
    page = repo.fetch_funnel_contact_page("sql", *_Q3, source_pairs_in=allowed)
    assert [r["contact_id"] for r in page["rows"]] == ["none"]


def test_funnel_aggregate_and_contact_page_agree_on_the_selected_source(pg):
    """§1: one population — the headline count equals the rows the table pages."""
    from services import canonical_crm_funnel_service as funnel

    _seed_source_fixture()
    for group in ("other_paid", "organic", "offline", "google_ads"):
        aggregate = funnel.build("business", "current_quarter",
                                 acquisition_group=group,
                                 now=datetime(2026, 8, 15, tzinfo=timezone.utc))
        page = funnel.contacts("business", "current_quarter", event="sql",
                               acquisition_group=group, page_size=100,
                               now=datetime(2026, 8, 15, tzinfo=timezone.utc))
        assert aggregate["events"]["sql"]["count"] == page["total"], group
        assert {r["contact_id"] for r in page["rows"]} == {
            c[0] for c in _OFFLINE_FIXTURE if c[3] == group}


def test_operational_status_counts_honour_the_source_filter(pg):
    from services import canonical_crm_funnel_service as funnel

    _seed([
        _contact("a", source="PAID_SEARCH", mql_status="MQL",
                 entered_lead="2026-07-05T00:00:00Z"),
        _contact("b", source="ORGANIC_SEARCH", campaign="google",
                 mql_status="MQL", entered_lead="2026-07-06T00:00:00Z"),
        _contact("c", source="ORGANIC_SEARCH", campaign="google",
                 mql_status="CLOSED - Bad Fit",
                 entered_lead="2026-07-07T00:00:00Z"),
    ])
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    everything = funnel.operational_status_breakdown(
        "business", "current_quarter", now=now)
    assert sum(everything["counts"].values()) == 3

    organic = funnel.operational_status_breakdown(
        "business", "current_quarter", acquisition_group="organic", now=now)
    assert sum(organic["counts"].values()) == 2
    assert organic["acquisition_group"] == "organic"

    google = funnel.operational_status_breakdown(
        "business", "current_quarter", acquisition_group="google_ads", now=now)
    assert sum(google["counts"].values()) == 1


def test_non_google_rows_from_sql_carry_no_campaign_or_keyword(pg):
    from services import canonical_crm_funnel_service as funnel

    _seed_source_fixture()
    page = funnel.contacts("business", "current_quarter", event="sql",
                           page_size=100,
                           now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    by_id = {r["contact_id"]: r for r in page["rows"]}
    for cid, _primary, detail, group in _OFFLINE_FIXTURE:
        row = by_id[cid]
        if group == "google_ads":
            continue
        assert row["campaign"] is None, cid
        assert row["keyword"] is None, cid
        assert row["campaign_semantics"] == "not_google_ads_source", cid
        # The drill-down evidence is still returned — just not as a campaign.
        assert row["source_detail_raw"] == detail
        assert row["source_channel_label"]


# ─────────────────────────────────────────────────────────────────────────────
# PR-ADS-153C follow-up — Disqualified / Other honours the Scope selector
# ─────────────────────────────────────────────────────────────────────────────
# The operational view keeps Scope visible, so its counts must describe the
# same population the funnel strip above it describes — proven here against
# real SQL, for every named scope.

_SCOPE_FIXTURE = [
    # (id, source, drill-down/campaign, keyword, mql_status)
    ("kw",       "PAID_SEARCH",    "Brand - US", "tms",  "MQL"),
    ("campaign", "PAID_SEARCH",    "Brand - US", None,   "CLOSED - Bad Fit"),
    ("unsafe",   "PAID_SEARCH",    "(not set)",  "tms",  "MQL"),
    ("organic",  "ORGANIC_SEARCH", "google",     "tms",  "MQL"),
    ("offline",  "Offline Sources", "Events",    "tms",  "MQL"),
]

_SAFE_CAMPAIGN = "Brand - US"


def _seed_scope_fixture():
    return _seed([
        _contact(cid, source=source, campaign=campaign,
                 mql_status=status,
                 entered_lead=f"2026-07-{5 + i:02d}T00:00:00Z",
                 entered_sql=f"2026-07-{5 + i:02d}T00:00:00Z")
        for i, (cid, source, campaign, _kw, status) in enumerate(_SCOPE_FIXTURE)
    ])


def _clear_keywords(*contact_ids):
    """`_contact` always sets a keyword; blank it where the fixture says None."""
    from db.connection import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hubspot_contact_funnel SET hs_analytics_source_data_2 = NULL "
                "WHERE contact_id = ANY(%s)", (list(contact_ids),))
        conn.commit()


def _identity(monkeypatch, *, available=True):
    """Pin the Google Ads campaign-identity contract for a deterministic test."""
    from services import canonical_crm_funnel_service as funnel

    def _resolver(label):
        return (label == _SAFE_CAMPAIGN,
                None if label == _SAFE_CAMPAIGN else "unsafe_campaign")

    monkeypatch.setattr(funnel, "_build_campaign_resolver",
                        lambda start, end: (_resolver, available))


_ALL_SCOPES = ("all_source", "google_ads_source",
               "campaign_attributable", "keyword_attributable")

_EXPECTED_BY_SCOPE = {
    "all_source": {"kw", "campaign", "unsafe", "organic", "offline"},
    "google_ads_source": {"kw", "campaign", "unsafe"},
    "campaign_attributable": {"kw", "campaign"},
    "keyword_attributable": {"kw"},
}


@pytest.mark.parametrize("scope", _ALL_SCOPES)
def test_operational_counts_match_the_canonical_population_for_each_scope(
        pg, monkeypatch, scope):
    from services import canonical_crm_funnel_service as funnel

    _seed_scope_fixture()
    _clear_keywords("campaign")
    _identity(monkeypatch)

    breakdown = funnel.operational_status_breakdown(
        "business", "current_quarter", scope=scope,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert breakdown["available"] is True
    assert breakdown["scope"] == scope
    assert sum(breakdown["counts"].values()) == len(_EXPECTED_BY_SCOPE[scope])


def test_operational_scopes_are_progressively_narrower_subsets(pg, monkeypatch):
    """all_source ⊇ google_ads_source ⊇ campaign ⊇ keyword, on real counts."""
    from services import canonical_crm_funnel_service as funnel

    _seed_scope_fixture()
    _clear_keywords("campaign")
    _identity(monkeypatch)

    totals = []
    for scope in _ALL_SCOPES:
        breakdown = funnel.operational_status_breakdown(
            "business", "current_quarter", scope=scope,
            now=datetime(2026, 8, 15, tzinfo=timezone.utc))
        totals.append(sum(breakdown["counts"].values()))
    assert totals == sorted(totals, reverse=True)
    assert totals == [5, 3, 2, 1]


@pytest.mark.parametrize("scope", _ALL_SCOPES)
def test_operational_and_funnel_populations_use_identical_scope_semantics(
        pg, monkeypatch, scope):
    """The visible funnel strip and the working-status breakdown below it must
    count exactly the same contacts under the same scope."""
    from services import canonical_crm_funnel_service as funnel

    _seed_scope_fixture()
    _clear_keywords("campaign")
    _identity(monkeypatch)
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    # The contact page is the row-level expression of the same population.
    page = funnel.contacts("business", "current_quarter", event="lead",
                           scope=scope, page_size=100, now=now)
    breakdown = funnel.operational_status_breakdown(
        "business", "current_quarter", scope=scope, now=now)

    assert {r["contact_id"] for r in page["rows"]} == _EXPECTED_BY_SCOPE[scope]
    assert sum(breakdown["counts"].values()) == page["total"]

    # And the funnel aggregate's own scoped count agrees with both.
    aggregate = funnel.build("business", "current_quarter", scope=scope, now=now)
    assert aggregate["events"]["lead"]["count"] == page["total"]


def test_operational_view_applies_scope_and_source_together(pg, monkeypatch):
    from services import canonical_crm_funnel_service as funnel

    _seed_scope_fixture()
    _clear_keywords("campaign")
    _identity(monkeypatch)
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    def _total(**kw):
        return sum(funnel.operational_status_breakdown(
            "business", "current_quarter", now=now, **kw)["counts"].values())

    # Source alone.
    assert _total(acquisition_group="organic") == 1
    assert _total(acquisition_group="google_ads") == 3
    # Scope alone.
    assert _total(scope="google_ads_source") == 3
    # Both — an intersection, not a replacement.
    assert _total(scope="google_ads_source", acquisition_group="google_ads") == 3
    assert _total(scope="campaign_attributable",
                  acquisition_group="google_ads") == 2
    # A contradictory pair is a proven zero, never an ignored filter.
    assert _total(scope="google_ads_source", acquisition_group="organic") == 0


def test_operational_scope_matches_the_contact_page_under_source_too(pg, monkeypatch):
    from services import canonical_crm_funnel_service as funnel

    _seed_scope_fixture()
    _clear_keywords("campaign")
    _identity(monkeypatch)
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    for scope in _ALL_SCOPES:
        for group in (None, "google_ads", "organic", "other_paid"):
            page = funnel.contacts("business", "current_quarter", event="lead",
                                   scope=scope, acquisition_group=group,
                                   page_size=100, now=now)
            breakdown = funnel.operational_status_breakdown(
                "business", "current_quarter", scope=scope,
                acquisition_group=group, now=now)
            assert sum(breakdown["counts"].values()) == page["total"], (scope, group)


@pytest.mark.parametrize("scope", ["campaign_attributable", "keyword_attributable"])
def test_identity_unavailable_makes_operational_scope_unavailable_not_zero(
        pg, monkeypatch, scope):
    from services import canonical_crm_funnel_service as funnel

    _seed_scope_fixture()
    _identity(monkeypatch, available=False)

    breakdown = funnel.operational_status_breakdown(
        "business", "current_quarter", scope=scope,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert breakdown["available"] is False
    assert breakdown["counts"] is None          # NOT {} and NOT zeros
    assert breakdown["reason"] == "campaign_identity_unavailable"
    assert breakdown["campaign_identity_available"] is False


@pytest.mark.parametrize("scope", ["all_source", "google_ads_source"])
def test_identity_outage_does_not_suppress_the_broad_operational_scopes(
        pg, monkeypatch, scope):
    """An attribution outage must not blank CRM truth that never needed it."""
    from services import canonical_crm_funnel_service as funnel

    _seed_scope_fixture()
    _identity(monkeypatch, available=False)

    breakdown = funnel.operational_status_breakdown(
        "business", "current_quarter", scope=scope,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert breakdown["available"] is True
    assert sum(breakdown["counts"].values()) == len(_EXPECTED_BY_SCOPE[scope])
