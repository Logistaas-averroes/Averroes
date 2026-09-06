"""PR-ADS-156-F4 — residual exact twins across the certified interval.

The production evidence
-----------------------
F3 reduced the duplicate population from 16,100 exact account-less twins to
exactly **one**, and the audit kept blocking with
`pre_cutover_null_customer_twin`. The remaining pair:

    source_date   2026-09-04
    campaign      global - competitors  (id 23094767513)
    ad_group      Competitors List
    keyword       (empty)      match_type  (empty)
    search_term   winfleet
    legacy twin   id 330284, run/batch 164 / 1355
    canonical row id 331967, run/batch 166 / 1378
    latest batch  1405

The canonical row still carrying batch **1378** is the whole story: batch 1405
covered that date and did not return this identity. Google Ads search-term
reporting is mutable — an identity present in one pull can be absent from the
next — so an older canonical observation stays stored while disappearing from
later pulls.

F3 supersedes twins only for identities present in the CURRENT INPUT ROWS. The
key that would have matched this twin was never in a later pull, so per-row
supersession could not reach it, and it sat inside a newly certified interval
untouched. Not a flaw in F3's rule: a gap in its reach.

What F4 changes
---------------
The reconciliation is now by INTERVAL as well as by input row. It asks a
different question — not "did this pull mention that identity" but "does this
interval still hold an exact twin of a canonical row". Every safety condition is
unchanged: exact match on all seven remaining key components, account-less twin,
canonical provenance on both sides, replacement belonging to the configured
account with complete identity, and an EXISTS so an unmatched historical row has
nothing to satisfy it.

Nothing here is special-cased. There is no `330284`, no `winfleet`, and no date
literal in the implementation — the production row disappears because it
satisfies the general rule, and the tests below are written over synthetic
fixtures that exercise that rule rather than that row.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts import audit_keyword_search_term_freshness as audit  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)
from tests.test_pr_ads_156_f3_account_identity_cutover import (  # noqa: E402
    ACCOUNT, OTHER_ACCOUNT, _TWIN_COLUMNS, _exec, _init_db, _rows_of,
    _run_audit, _scalar, _seed_batch,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

#: The certified interval under test. `DAY` is the date whose identity goes
#: missing from the current pull — the production shape, in miniature.
DAY = date(2026, 3, 4)
INTERVAL_START = DAY - timedelta(days=13)
INTERVAL_END = DAY + timedelta(days=1)
#: Deliberately outside the requested interval, to prove the sweep is bounded.
OUTSIDE = INTERVAL_START - timedelta(days=5)

#: The identity that exists in the table but is absent from the current pull.
OMITTED = {
    "campaign_name": "global - competitors",
    "campaign_id": "23094767513",
    "ad_group": "Competitors List",
    "keyword": None,
    "match_type": None,
    "search_term": "winfleet",
}


def _insert(**over):
    """One stored `search_terms` row. Defaults describe the omitted identity, so
    a test states only the column it is varying."""
    values = {
        "source_date": DAY, "customer_id": None,
        "campaign_name": OMITTED["campaign_name"],
        "campaign_id": OMITTED["campaign_id"], "ad_group": OMITTED["ad_group"],
        "keyword": OMITTED["keyword"], "match_type": OMITTED["match_type"],
        "search_term": OMITTED["search_term"],
        "cost_micros": 5_000_000, "currency_code": "GBP",
        "source_system": "google_ads_api", "spend_usd": 5.0, "clicks": 3,
        "impressions": 40,
    }
    flags = (over.pop("is_flagged_waste", None), over.pop("junk_category", None),
             over.pop("matched_pattern", None))
    values.update(over)
    _exec(
        f"INSERT INTO search_terms ({_TWIN_COLUMNS}, is_flagged_waste, "
        "junk_category, matched_pattern) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (*values.values(), *flags))


def _pull_row(**over):
    """One row in the CURRENT PULL's input shape — what Google Ads returned
    this time. Deliberately a different identity from `OMITTED`."""
    row = {
        "date": DAY.isoformat(), "customer_id": ACCOUNT,
        "campaign": "brand - uk", "campaign_id": "10", "ad_group": "Core",
        "keyword": "freight", "match_type": "BROAD",
        "search_term": "freight forwarding software",
        "cost_micros": 4_000_000, "currency_code": "GBP",
        "clicks": 2, "impressions": 30, "conversions": 0.0,
        "source": "google_ads_api",
    }
    row.update(over)
    return row


def _write(rows, **kwargs):
    from db.writers import write_search_terms

    return write_search_terms(None, rows, **kwargs)


def _sync_write(rows):
    """The write exactly as the sync service performs it: the REQUESTED
    interval passed explicitly, not inferred from the returned rows."""
    return _write(rows, interval_start=INTERVAL_START, interval_end=INTERVAL_END)


def _terms():
    return sorted(r[0] for r in _rows_of("SELECT search_term FROM search_terms"))


def _seed_the_production_shape():
    """The exact pair production was left with, plus the canonical replacement.

    The twin and its replacement BOTH already exist before this sync runs —
    written by earlier batches — and the identity is about to be absent from
    the current pull.
    """
    _insert(customer_id=None, is_flagged_waste=True,
            junk_category="competitor", matched_pattern="winfleet")   # the twin
    _insert(customer_id=ACCOUNT)                                      # replacement


# ═════════════════════════════════════════════════════════════════════════════
# 1–5 — the omitted identity is reconciled, annotations survive, canonical stays
# ═════════════════════════════════════════════════════════════════════════════
@_needs_pg
def test_1_to_5_an_omitted_identity_is_still_reconciled(pg, monkeypatch):  # noqa: F811
    """The production case end to end.

    Twin and replacement both pre-exist; the current pull contains a DIFFERENT
    identity entirely, which is what establishes the account and the interval.
    Per-row supersession cannot reach the twin — its key is not in the input —
    and the interval sweep does.
    """
    _init_db(pg, monkeypatch)
    _seed_the_production_shape()
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 2

    written = _sync_write([_pull_row()])

    # (2) the current input deliberately omits the identity…
    assert "winfleet" not in {r["search_term"] for r in [_pull_row()]}
    # (3) …and still establishes the account and the interval.
    assert written == 1

    # (4) the residual twin is gone, and (5) its replacement remains.
    remaining = _rows_of(
        "SELECT customer_id, is_flagged_waste, junk_category, matched_pattern "
        "FROM search_terms WHERE search_term = %s", (OMITTED["search_term"],))
    assert len(remaining) == 1
    customer_id, flagged, junk, pattern = remaining[0]
    assert customer_id == ACCOUNT

    # (4) the annotations were carried across before the twin went. They exist
    # nowhere upstream, so losing them would silently un-review a human
    # decision — and nothing downstream would ever show that it happened.
    assert flagged is True
    assert junk == "competitor"
    assert pattern == "winfleet"


@_needs_pg
def test_4b_an_existing_canonical_annotation_is_never_overwritten(pg, monkeypatch):  # noqa: F811
    """Precedence: the canonical row's own judgement wins. The twin's is older
    by construction, and reversing a newer decision with an older one is worse
    than losing it — it looks deliberate."""
    _init_db(pg, monkeypatch)
    _insert(customer_id=None, is_flagged_waste=True, junk_category="stale-old",
            matched_pattern="old")
    _insert(customer_id=ACCOUNT, is_flagged_waste=False,
            junk_category="reviewed-clean", matched_pattern="new")

    _sync_write([_pull_row()])

    row = _rows_of("SELECT is_flagged_waste, junk_category, matched_pattern "
                   "FROM search_terms WHERE search_term = %s",
                   (OMITTED["search_term"],))
    assert len(row) == 1
    assert row[0] == (False, "reviewed-clean", "new")


# ═════════════════════════════════════════════════════════════════════════════
# 6–10 — everything the sweep must never touch
# ═════════════════════════════════════════════════════════════════════════════
@_needs_pg
def test_6_an_unmatched_null_account_row_is_untouched(pg, monkeypatch):  # noqa: F811
    """No replacement exists, so the EXISTS is unsatisfied and the row stays.

    This is the population production discloses as 24 unmatched historical
    rows. They describe observations nobody can attribute to an account;
    deleting them would destroy evidence, and stamping an account on them would
    fabricate provenance.
    """
    _init_db(pg, monkeypatch)
    _insert(customer_id=None, search_term="orphaned historical query")

    _sync_write([_pull_row()])

    assert "orphaned historical query" in _terms()
    assert _scalar("SELECT customer_id FROM search_terms "
                   "WHERE search_term = 'orphaned historical query'") is None


@_needs_pg
def test_6b_a_whole_disclosed_history_population_survives(pg, monkeypatch):  # noqa: F811
    """The disclosed set at production scale, in miniature: 24 unmatched
    account-less rows, none of which has a replacement. All 24 remain."""
    _init_db(pg, monkeypatch)
    for i in range(24):
        _insert(customer_id=None, search_term=f"unmatched history {i}")

    _sync_write([_pull_row()])

    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 24


@_needs_pg
def test_7_a_windsor_or_unknown_provenance_row_is_untouched(pg, monkeypatch):  # noqa: F811
    """Provenance is required on the TWIN side too. A Windsor row is not a
    pre-cutover Google Ads twin, and an unlabelled row proves nothing about
    where it came from — neither may be deleted on a name match."""
    _init_db(pg, monkeypatch)
    _insert(customer_id=None, source_system="windsor",
            search_term="windsor twin shape")
    _insert(customer_id=ACCOUNT, search_term="windsor twin shape")
    _insert(customer_id=None, source_system=None,
            search_term="unlabelled twin shape")
    _insert(customer_id=ACCOUNT, search_term="unlabelled twin shape")

    _sync_write([_pull_row()])

    assert _scalar("SELECT COUNT(*) FROM search_terms WHERE customer_id IS NULL "
                   "AND COALESCE(source_system, '') <> 'google_ads_api'") == 2


@_needs_pg
def test_8_another_customers_row_is_untouched(pg, monkeypatch):  # noqa: F811
    """A complete row belonging to a DIFFERENT account is not a replacement for
    anything of ours — it is somebody else's observation of the same term, and
    it must neither be deleted nor used to justify deleting a twin."""
    _init_db(pg, monkeypatch)
    _insert(customer_id=None)                     # account-less twin
    _insert(customer_id=OTHER_ACCOUNT)            # NOT a replacement

    _sync_write([_pull_row()])

    rows = _rows_of("SELECT customer_id FROM search_terms WHERE search_term = %s "
                    "ORDER BY customer_id NULLS FIRST", (OMITTED["search_term"],))
    # Both survive: no replacement for this account exists.
    assert [r[0] for r in rows] == [None, OTHER_ACCOUNT]


@_needs_pg
@pytest.mark.parametrize("column,value", [
    ("campaign_name", "a different campaign"),
    ("campaign_id", "99999"),
    ("ad_group", "A Different Ad Group"),
    ("keyword", "some keyword"),
    ("match_type", "EXACT"),
    ("search_term", "a different term"),
])
def test_9_a_row_differing_in_any_key_component_is_untouched(
        pg, monkeypatch, column, value):  # noqa: F811
    """One parametrised case per natural-key component.

    A row differing by so much as a match type is a DIFFERENT observation. The
    match has to be exact on all seven, and this proves each one is load-bearing
    rather than assumed.
    """
    _init_db(pg, monkeypatch)
    _insert(customer_id=None, **{column: value})   # twin-shaped, but different
    _insert(customer_id=ACCOUNT)                   # the canonical row

    _sync_write([_pull_row()])

    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 1, column


@_needs_pg
def test_10_a_twin_outside_the_requested_interval_is_untouched(pg, monkeypatch):  # noqa: F811
    """The sweep is bounded by the interval this run CERTIFIES.

    An exact twin one day before the requested window is left alone: this run
    did not query that date, so it is in no position to reconcile it. An
    unbounded sweep would be a table-wide deletion wearing a date column.
    """
    _init_db(pg, monkeypatch)
    _insert(customer_id=None, source_date=OUTSIDE)
    _insert(customer_id=ACCOUNT, source_date=OUTSIDE)
    _insert(customer_id=None)                      # inside: will be reconciled
    _insert(customer_id=ACCOUNT)

    _sync_write([_pull_row()])

    assert _scalar("SELECT COUNT(*) FROM search_terms WHERE customer_id IS NULL "
                   "AND source_date = %s", (OUTSIDE,)) == 1
    assert _scalar("SELECT COUNT(*) FROM search_terms WHERE customer_id IS NULL "
                   "AND source_date = %s", (DAY,)) == 0


@_needs_pg
def test_10b_an_unresolved_account_sweeps_nothing(pg, monkeypatch):  # noqa: F811
    """Fail closed. Without a configured account there is no way to say which
    rows are this deployment's, so the sweep does not run at all — it does not
    fall back to matching every account."""
    _init_db(pg, monkeypatch)
    _seed_the_production_shape()
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)

    _sync_write([_pull_row(customer_id=None)])

    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") >= 1


# ═════════════════════════════════════════════════════════════════════════════
# 11–13 — idempotency, failure semantics, and honest counts
# ═════════════════════════════════════════════════════════════════════════════
@_needs_pg
def test_11_repeating_the_reconciliation_is_idempotent(pg, monkeypatch):  # noqa: F811
    """A scheduler runs this every day over an overlapping window. The second
    pass must find nothing left to do and change nothing."""
    _init_db(pg, monkeypatch)
    _seed_the_production_shape()

    _sync_write([_pull_row()])
    after_first = _rows_of("SELECT id, customer_id, is_flagged_waste, "
                           "junk_category FROM search_terms ORDER BY id")

    _sync_write([_pull_row()])
    _sync_write([_pull_row()])
    after_third = _rows_of("SELECT id, customer_id, is_flagged_waste, "
                           "junk_category FROM search_terms ORDER BY id")

    assert after_first == after_third
    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 0


@_needs_pg
def test_12_a_reconciliation_failure_is_not_a_successful_certified_write(
        pg, monkeypatch):  # noqa: F811
    """The sweep runs in the SAME transaction as the upsert.

    If it fails, the upsert rolls back with it and the writer reports 0 —
    which the sync service reads as failed persistence and records a `failed`
    batch. A reconciliation that did not complete must never leave an interval
    certified, and the rows it would have written must not be half-there.
    """
    _init_db(pg, monkeypatch)
    _seed_the_production_shape()
    before = _scalar("SELECT COUNT(*) FROM search_terms")

    import db.writers as writers

    original = writers.get_conn

    class _FailingCursor:
        """Executes normally until the residual DELETE, then raises."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            if "DELETE FROM search_terms AS twin" in sql:
                raise RuntimeError("simulated reconciliation failure")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    class _Conn:
        def __init__(self, inner):
            self._inner = inner

        def cursor(self):
            return _FailingCursor(self._inner.cursor().__enter__())

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _Ctx:
        def __enter__(self):
            self._cm = original()
            return _Conn(self._cm.__enter__())

        def __exit__(self, *exc):
            # Propagate the failure so the transaction rolls back.
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(writers, "get_conn", lambda: _Ctx())

    written = writers.write_search_terms(
        None, [_pull_row()], interval_start=INTERVAL_START,
        interval_end=INTERVAL_END)

    # The writer reports nothing written…
    assert written == 0
    # Restore only the connection factory — `monkeypatch.undo()` would also
    # revert the DATABASE_URL this fixture set, leaving the assertions below
    # with no database to read.
    monkeypatch.setattr(writers, "get_conn", original)
    # …and nothing was committed: no new row, and the twin is still there.
    assert _scalar("SELECT COUNT(*) FROM search_terms") == before
    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 1

    # And that is what the sync service turns into a FAILED batch, because a
    # prepared-but-unwritten pull is exactly its persistence-failure condition.
    from services.search_term_sync_service import sync_search_terms  # noqa: F401


@_needs_pg
def test_13_deleted_twins_do_not_inflate_the_batch_row_count(pg, monkeypatch):  # noqa: F811
    """`row_count` means UPSTREAM rows written, and stays comparable with
    `fetched` and `prepared`. A superseded twin is a reconciliation action, not
    a newly written source row — counting it would make a healthy sync look
    like it wrote more than Google Ads returned."""
    _init_db(pg, monkeypatch)
    # Five residual twins waiting, and one row in the current pull.
    for i in range(5):
        _insert(customer_id=None, search_term=f"residual {i}")
        _insert(customer_id=ACCOUNT, search_term=f"residual {i}")

    written = _sync_write([_pull_row()])

    assert written == 1, "one upstream row was written, not six"
    assert _scalar("SELECT COUNT(*) FROM search_terms "
                   "WHERE customer_id IS NULL") == 0


@_needs_pg
def test_13b_the_superseded_count_is_reported_separately(pg, monkeypatch, caplog):  # noqa: F811
    """A deterministic count, logged under its own name, so an operator can see
    what reconciliation did without inferring it from a row total."""
    import logging

    _init_db(pg, monkeypatch)
    for i in range(3):
        _insert(customer_id=None, search_term=f"residual {i}")
        _insert(customer_id=ACCOUNT, search_term=f"residual {i}")

    with caplog.at_level(logging.INFO, logger="db.writers"):
        _sync_write([_pull_row()])

    assert "residual_twins_superseded=3" in caplog.text


# ═════════════════════════════════════════════════════════════════════════════
# 14–16 — the audit clears, F3 behaviour holds, and nothing reaches outward
# ═════════════════════════════════════════════════════════════════════════════
@_needs_pg
def test_14_the_audit_goes_from_blocking_to_clean(pg, monkeypatch):  # noqa: F811
    """Before and after, on a real database.

    The audit is not weakened anywhere in this change — it is the same strict
    check, and it stops blocking because the condition it reports genuinely
    stops being true.
    """
    _init_db(pg, monkeypatch)
    _seed_the_production_shape()
    _seed_batch(DAY)

    before = json.loads(_run_audit("--json",
                                   env_extra={"DATABASE_URL": pg.url}).stdout)
    before_search = next(d for d in before["datasets"]
                         if d["dataset"] == "search_terms")
    assert audit.V_NULL_CUSTOMER_TWIN in before["violation_codes"]
    assert before_search["null_customer_twins"] == 1

    _sync_write([_pull_row()])

    after = json.loads(_run_audit("--json",
                                  env_extra={"DATABASE_URL": pg.url}).stdout)
    after_search = next(d for d in after["datasets"]
                        if d["dataset"] == "search_terms")
    assert audit.V_NULL_CUSTOMER_TWIN not in after["violation_codes"]
    assert after_search["null_customer_twins"] == 0
    assert after_search["current"]["duplicate_natural_key_groups"] == 0
    assert after_search["interval_canonical_provenance"]["rows_missing_identity"] == 0


@_needs_pg
def test_15_the_current_input_supersession_still_works(pg, monkeypatch):  # noqa: F811
    """F3's behaviour, unchanged: when the identity IS in the current pull, its
    account-less twin is superseded by the per-row path exactly as before. F4
    adds reach; it does not replace what already worked over 16,100 rows."""
    _init_db(pg, monkeypatch)
    _insert(customer_id=None, campaign_name="brand - uk", campaign_id="10",
            ad_group="Core", keyword="freight", match_type="BROAD",
            search_term="freight forwarding software",
            is_flagged_waste=True, junk_category="job_seeker")

    written = _sync_write([_pull_row()])

    assert written == 1
    rows = _rows_of("SELECT customer_id, is_flagged_waste, junk_category "
                    "FROM search_terms WHERE search_term = %s",
                    ("freight forwarding software",))
    assert len(rows) == 1
    assert rows[0] == (ACCOUNT, True, "job_seeker")


def test_16_no_external_write_path_is_introduced():
    """The reconciliation is local SQL over one table.

    Read from source: the writer must not have acquired a Google Ads or HubSpot
    client, an HTTP call, or any mutating verb aimed outward. A cleanup routine
    is exactly the kind of code where an "while we're here" external call would
    look reasonable and be catastrophic.
    """
    import ast

    from analysis.legacy_source_guard import code_only

    # Scoped to `write_search_terms` itself. `db/writers.py` is the module for
    # every durable writer in the product, HubSpot ledger rows included, so a
    # whole-file scan would fail on code that has nothing to do with this
    # change — and a test that fails for the wrong reason gets its assertion
    # loosened rather than read.
    src = code_only(_ROOT / "db" / "writers.py")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "write_search_terms")
    body = ast.unparse(fn).lower()

    for forbidden in ("requests.", "httpx", "urllib", "hubspot", "googleads",
                      "google_ads_client", "api_client", "mutate", "post(",
                      "put(", "patch("):
        assert forbidden not in body, forbidden

    # And the sweep statements touch exactly one table.
    start = src.index("_residual_twin_match")
    sweep = src[start:src.index("_null_twin_delete", start)].lower()
    assert "search_terms" in sweep
    for other in ("keyword_daily_facts", "hubspot", "deals", "leads", "runs"):
        assert other not in sweep, other


def test_17_the_sync_service_passes_the_requested_interval_explicitly(monkeypatch):
    """The wiring, asserted at the boundary.

    Deriving the sweep bounds from the RETURNED rows would shrink the interval
    by exactly the dates whose identities went missing — which is the entire
    class of row F4 exists to reach. So the service passes what it REQUESTED
    and is about to certify, and this proves it rather than trusting it.
    """
    import connectors.google_ads_source as source
    import db.writers as writers
    import services.search_term_sync_service as svc

    captured = {}

    def _capture(run_id, rows, sync_batch_id=None, **kw):
        captured.update(kw)
        return len(rows)

    # Patched on the real module: the service does `import db.writers as w`,
    # which resolves through the package attribute rather than `sys.modules`,
    # so substituting the module object there would silently be ignored — and
    # the test would pass against the real writer having proved nothing.
    monkeypatch.setattr(writers, "start_sync_batch", lambda **kw: 4242)
    monkeypatch.setattr(writers, "write_search_terms", _capture)
    monkeypatch.setattr(writers, "finish_sync_batch", lambda **kw: True)
    monkeypatch.setattr(source, "pull_search_terms_range",
                        lambda a, b: [_pull_row()], raising=False)

    start, end = date(2026, 3, 1), date(2026, 3, 14)
    result = svc.sync_search_terms(start, end, "daily")

    assert result["batch_id"] == 4242, result
    assert captured.get("interval_start") == start
    assert captured.get("interval_end") == end


@_needs_pg
def test_18_without_explicit_bounds_the_sweep_stays_bounded_by_the_rows(
        pg, monkeypatch):  # noqa: F811
    """A caller that passes no interval still gets a BOUNDED sweep, derived
    from the prepared rows' own span — never a table-wide deletion.

    The twin outside that span survives, which is what makes this a fallback
    rather than a loophole.
    """
    _init_db(pg, monkeypatch)
    _insert(customer_id=None, source_date=OUTSIDE)
    _insert(customer_id=ACCOUNT, source_date=OUTSIDE)
    _seed_the_production_shape()

    written = _write([_pull_row()])          # no interval kwargs at all

    assert written == 1
    # In-span twin reconciled; the one outside the rows' span untouched.
    assert _scalar("SELECT COUNT(*) FROM search_terms WHERE customer_id IS NULL "
                   "AND source_date = %s", (DAY,)) == 0
    assert _scalar("SELECT COUNT(*) FROM search_terms WHERE customer_id IS NULL "
                   "AND source_date = %s", (OUTSIDE,)) == 1
