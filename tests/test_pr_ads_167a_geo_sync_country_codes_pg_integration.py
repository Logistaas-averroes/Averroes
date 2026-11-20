"""PR-ADS-167A — the geo sync no longer blanks known country codes, and no
longer calls a failed country lookup a clean run.

PR-ADS-165A, known gap 2. ``_resolve_country_metadata`` swallowed a failed
``geo_target_constant`` lookup and wrote every row of the re-read range with
``country_code`` NULL — and ``replace_geo_daily_spend_chunk`` deletes the range
first, so the codes already stored for those days were gone. The chunk was
``verified`` and the run ``success``. A criterion whose only rows were inside
the range lost its country for good; the Countries page then read its spend as
"(unresolved location)".

Now:

* the sync names each criterion from the codes already stored for it (the
  one-code rule every reader applies), and asks Google only about the rest;
* the writer never writes NULL over a stored code (the same rule, read inside
  its own transaction before the range is deleted; the upsert COALESCEs);
* a failed lookup still writes the rows — the spend is real and the chunk is
  ``verified`` — but the run is ``partial`` with reason
  ``country_lookup_failed``, the ledger chunk carries the error, and the daily
  incremental step records a ``partial`` sync batch. Not ``success``, not
  ``failed``.

Real PostgreSQL; the real sync, writers and incremental step. Only the two
Google calls are fakes, and the fake's lookup can raise.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, UAE, UK, _GoogleAds, _today, live_db,
)

KZ = ("2398", "KZ", "Kazakhstan")       # first seen on the day the lookup fails
CUBA = ("2192", None, None)             # Google answers, with no country


class _Google(_GoogleAds):
    """Google Ads as the sync sees it. ``lookup_fails`` makes the
    geo_target_constant call raise, as a DEADLINE_EXCEEDED would; ``asked``
    records which criteria the sync asked about."""

    KNOWN = {c[0]: {"country_code": c[1], "name": c[2]} for c in (UK, UAE, KZ)}

    def __init__(self):
        super().__init__()
        self.lookup_fails = False
        self.asked: list = []

    def country_codes(self, criterion_ids) -> dict:   # noqa: D102 — override
        self.asked.append(sorted(str(i) for i in criterion_ids))
        if self.lookup_fails:
            raise RuntimeError("geo_target_constant: DEADLINE_EXCEEDED")
        return {str(i): self.KNOWN[str(i)] for i in criterion_ids
                if str(i) in self.KNOWN}


def _sync(monkeypatch, google, date_from: date, date_to: date, *, resume=True,
          job="sync") -> dict:
    from services import google_ads_geo_sync_service as geo
    monkeypatch.setattr(geo, "fetch_geo_daily", google.fetch)
    monkeypatch.setattr(geo, "fetch_geo_country_codes", google.country_codes)
    return geo.run_google_ads_geo_sync(
        date_from=date_from.isoformat(), date_to=date_to.isoformat(),
        dry_run=False, job_id=job, resume=resume)


def _codes(cluster, start: date, end: date) -> dict:
    """{criterion: sorted set of stored codes (None for a blank)} in range."""
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT country_criterion_id, country_code, country_name "
                    "FROM google_ads_geo_daily_spend WHERE spend_date BETWEEN %s AND %s",
                    (start, end))
        out: dict = {}
        for criterion, code, name in cur.fetchall():
            out.setdefault(criterion, set()).add((code, name))
        return {k: sorted(v, key=str) for k, v in out.items()}


def _history(google, today):
    """UK every day of the last fortnight; the UAE only inside the last 7
    days (so ONLY rows the morning re-read replaces name it)."""
    for i in range(14):
        google.gbp_by_day[today - timedelta(days=i)] = {UK: 10.0}
    google.gbp_by_day[today - timedelta(days=3)][UAE] = 50.0


def test_01_a_failed_lookup_keeps_every_stored_code_and_the_run_is_partial(
        live_db, monkeypatch):
    today = _today()
    google = _Google()
    _history(google, today)
    first = _sync(monkeypatch, google, today - timedelta(days=13), today,
                  job="day-one")
    assert first["status"] == "success", first

    # This morning: Kazakhstan spends for the first time, and the lookup fails.
    google.gbp_by_day[today - timedelta(days=1)][KZ] = 7.0
    google.lookup_fails = True
    google.asked.clear()
    out = _sync(monkeypatch, google, today - timedelta(days=6), today, job="today")

    # Not success, and not failed: the spend landed, the names did not.
    assert out["status"] == "partial", out
    assert out["reason"] == "country_lookup_failed"
    assert out["summary"]["chunks_failed"] == 0
    assert out["summary"]["country_lookups_failed"] == 1
    assert any("country lookup failed" in e for e in out["errors"]), out["errors"]
    chunk = out["chunks"][-1]
    assert chunk["status"] == "verified" and chunk["country_lookup"] == "failed"
    assert chunk["country_identity"]["unresolved"] == ["2398"]
    # Storage named the UK and the UAE; Google was asked about Kazakhstan only.
    assert google.asked == [["2398"]]

    stored = _codes(live_db, today - timedelta(days=6), today)
    assert stored[UK[0]] == [("GB", "United Kingdom")]
    assert stored[UAE[0]] == [("AE", "United Arab Emirates")]   # its ONLY rows
    assert stored[KZ[0]] == [(None, None)]                      # unknown: NULL
    # The spend itself is complete: every row was written.
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, error_message FROM google_ads_geo_coverage "
                    "WHERE chunk_start = %s AND chunk_end = %s",
                    (today - timedelta(days=6), today))
        status, error = cur.fetchone()
        assert status == "verified"
        assert "country lookup failed" in error
        cur.execute("SELECT last_status, checkpoint_date FROM google_ads_geo_sync_state")
        last_status, checkpoint = cur.fetchone()
        assert last_status == "partial"
        assert checkpoint == today          # day one's, not advanced by a partial

    # The next run with the lookup working names Kazakhstan, and is clean.
    google.lookup_fails = False
    google.asked.clear()
    again = _sync(monkeypatch, google, today - timedelta(days=6), today,
                  resume=False, job="today-retry")
    assert again["status"] == "success", again
    assert google.asked == [["2398"]]
    assert _codes(live_db, today - timedelta(days=6), today)[KZ[0]] == [
        ("KZ", "Kazakhstan")]


def test_02_a_location_google_answers_for_without_a_country_is_not_a_failure(
        live_db, monkeypatch):
    """Production's `2192`: the lookup succeeds and names no country. That is
    Google's answer about the location, not a failed lookup."""
    today = _today()
    google = _Google()
    google.gbp_by_day[today - timedelta(days=1)] = {UK: 10.0, CUBA: 0.45}
    out = _sync(monkeypatch, google, today - timedelta(days=6), today)
    assert out["status"] == "success", out
    assert out["chunks"][-1]["country_identity"]["unresolved"] == ["2192"]
    assert "country_lookup" not in out["chunks"][-1]
    assert _codes(live_db, today - timedelta(days=6), today)["2192"] == [(None, None)]


def test_03_the_daily_step_records_a_partial_batch_and_a_run_error(
        live_db, monkeypatch):
    """Run status doctrine: `partial` reaches the sync batch — the durable
    record every freshness surface reads — as `partial`, and the run's error
    list names it. It is not collapsed into either neighbour."""
    today = _today()
    google = _Google()
    google.gbp_by_day[today - timedelta(days=1)] = {UK: 10.0, KZ: 7.0}
    google.lookup_fails = True
    from services import google_ads_geo_sync_service as geo
    monkeypatch.setattr(geo, "fetch_geo_daily", google.fetch)
    monkeypatch.setattr(geo, "fetch_geo_country_codes", google.country_codes)
    from scheduler import incremental_sync as inc
    errors: list = []
    out = inc._sync_canonical_geo(run_id=None, date_to=today, errors=errors)
    assert out["status"] == "partial", out
    assert errors and "partial" in errors[0], errors
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, error_message FROM sync_batches "
                    "WHERE dataset = 'canonical_geo' ORDER BY id DESC LIMIT 1")
        status, error = cur.fetchone()
        cur.execute("SELECT status, last_source_date FROM sync_state "
                    "WHERE dataset = 'canonical_geo'")
        state = cur.fetchone()
    assert status == "partial"
    assert "country lookup failed" in error
    # Freshness says partial, and a partial advances no watermark.
    assert state == ("partial", None)


def test_04_the_writer_never_writes_null_over_a_stored_code(live_db):
    """Whoever calls it. The range replacement reads the criterion's code on
    record before it deletes the range; the upsert keeps a stored code."""
    import db.writers as writers
    d = _today() - timedelta(days=2)

    def row(criterion, code, name, gbp=5.0, day=d):
        return {"customer_id": CUSTOMER, "currency_code": "GBP",
                "country_criterion_id": criterion, "country_code": code,
                "country_name": name, "campaign_id": "c1", "spend_date": day,
                "cost_micros": int(gbp * 1_000_000), "source_query_version": "t"}

    assert writers.upsert_geo_daily_spend(
        [row(*UAE), row(*UK), row("2999", "FR", "France"),
         row("2999", "DE", "Germany", day=d - timedelta(days=30))]) == 4
    out = writers.replace_geo_daily_spend_chunk(
        CUSTOMER, d.isoformat(), d.isoformat(),
        [row(UAE[0], None, None, 6.0), row(UK[0], "", None, 7.0),
         row("2999", None, None, 8.0), row("2192", None, None, 1.0)])
    stored = _codes(live_db, d, d)
    assert stored[UAE[0]] == [("AE", "United Arab Emirates")]
    assert stored[UK[0]] == [("GB", "United Kingdom")]
    # Two codes on record name nothing; nothing on record names nothing.
    assert stored["2999"] == [(None, None)]
    assert stored["2192"] == [(None, None)]
    assert (out["written"], out.get("codes_preserved")) == (4, 2)

    # The upsert: a later payload with no country keeps the stored one.
    assert writers.upsert_geo_daily_spend([row(*UK, gbp=9.0)]) == 1
    assert writers.upsert_geo_daily_spend([row(UK[0], None, None, gbp=11.0)]) == 1
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT country_code, country_name, cost_micros FROM "
                    "google_ads_geo_daily_spend WHERE country_criterion_id = %s "
                    "AND spend_date = %s", (UK[0], d))
        assert cur.fetchone() == ("GB", "United Kingdom", 11_000_000)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
