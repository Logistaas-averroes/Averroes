"""PR-ADS-161H — attribution feasibility report (read-only).

The report measures whether cohort attribution could work; it publishes
nothing. Campaign placement is read from the REAL canonical closed-won truth
(driven here through ``get_closed_won_truth`` with a stubbed ledger), never
recomputed. Unknown stays None, and an unproven population is not measured.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from scripts import report_attribution_feasibility as raf
from services import canonical_customer_revenue_service as ccr
from tests.canonical_ledger_fixtures import (
    READY_SYNC_STATE, fake_closed_won_universe, label_resolver, ledger_row,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _gads(deal_id, **kw):
    return ledger_row(deal_id, acquisition_group="google_ads",
                      attribution_status="attributed",
                      attribution_reason="contacts_agree",
                      deal_close_date=_iso(CLOSE), **kw)


def _universe(rows, contacts, *, findings=()):
    sync = dict(READY_SYNC_STATE,
                last_incremental_at=_iso(NOW - timedelta(hours=3)))
    u = fake_closed_won_universe(rows, sync_state=sync)()
    u["acquisition_contacts"] = contacts
    # What load_closed_won_universe adds: the coverage gate's verdict.
    u["coverage_findings"] = list(findings) if findings is not None else None
    return u


def _contact(deal_id, contact_id, created):
    return {"deal_id": str(deal_id), "contact_id": str(contact_id),
            "contact_created_at": None if created is None else _iso(created),
            "funnel_row_present": created is not None}


def _report(rows, contacts, **kw):
    u = _universe(rows, contacts, **kw)
    truth = ccr.get_closed_won_truth(list(raf.WINDOWS), now=NOW, universe=u,
                                     resolver=label_resolver(),
                                     resolver_detail="test label resolver")
    return raf.measure(u, truth, NOW)


ROWS = [
    _gads("d1", campaign_name_raw="Brand", primary_contact_id="c1"),
    _gads("d2", primary_contact_id="c2"),                 # no campaign label
    ledger_row("d3", deal_close_date=_iso(CLOSE), association_status="none",
               association_count=0),
    ledger_row("d4", deal_close_date=_iso(CLOSE),
               association_status="lookup_failed",
               attribution_status="unavailable"),
    ledger_row("d5", deal_close_date=_iso(CLOSE), primary_contact_id="c5"),
    ledger_row("d6", deal_close_date=_iso(CLOSE), primary_contact_id="c6"),
]
CONTACTS = [
    _contact("d1", "c1", CLOSE - timedelta(days=40)),
    _contact("d2", "c2", CLOSE - timedelta(days=200)),
    _contact("d5", "c5", None),                           # creation not held
    _contact("d6", "c6", CLOSE + timedelta(days=5)),      # added after close
]


def test_01_campaign_placement_is_the_canonical_partition_not_a_recount():
    r = _report(ROWS, CONTACTS)
    assert r["exit_code"] == raf.EXIT_MEASURED
    all_time = next(w for w in r["windows"] if w["window"] == "all_time")
    assert all_time["closed_won_deals"] == 6
    assert all_time["partition"]["campaign_attributable"]["deals"] == 1
    assert all_time["partition"]["google_ads_unplaced"]["deals"] == 1
    assert all_time["partition"]["google_ads_unplaced"]["reasons"] == {
        "no_campaign_label": 1}
    assert all_time["google_ads_source_deals"] == 2


def test_02_contact_evidence_separates_no_contact_from_unresolved():
    ev = _report(ROWS, CONTACTS)["contact_evidence"]
    assert ev == {raf.C_DATED: 3, raf.C_UNDATED: 1, raf.C_NO_CONTACT: 1,
                  raf.C_UNRESOLVED: 1}


def test_03_cycle_length_counts_contacts_added_after_close_separately():
    c = _report(ROWS, CONTACTS)["cycle_length"]
    assert c["all_won"]["measured"] == 2            # d1 (40d) and d2 (200d)
    assert c["all_won"]["first_contact_created_after_close"] == 1   # d6
    assert c["all_won"]["not_measurable"] == 3      # d3, d4, d5
    assert c["all_won"]["percentile_days"]["p50"] == 40.0
    assert c["all_won"]["percentile_days"]["p90"] == 200.0
    assert c["all_won"]["closed_within_days"]["90"] == {"deals": 1,
                                                        "share": 0.5}
    assert c["google_ads"]["deals"] == 2 and c["google_ads"]["measured"] == 2


def test_04_nothing_measurable_is_none_never_zero():
    rows = [ledger_row("d5", deal_close_date=_iso(CLOSE),
                       primary_contact_id="c5")]
    c = _report(rows, [_contact("d5", "c5", None)])["cycle_length"]["all_won"]
    assert c["measured"] == 0
    assert set(c["percentile_days"].values()) == {None}
    assert set(c["closed_within_days"].values()) == {None}


def test_05_an_unproven_ledger_is_not_measured():
    finding = {"code": "bootstrap_incomplete", "message": "running"}
    r = _report(ROWS, CONTACTS, findings=[finding])
    assert r["exit_code"] == raf.EXIT_UNAVAILABLE and "windows" not in r
    # Absent is not "no findings".
    assert _report(ROWS, CONTACTS, findings=None)["exit_code"] == \
        raf.EXIT_UNAVAILABLE


def test_06_an_unreadable_ledger_is_unavailable():
    u = fake_closed_won_universe([], available=False)()
    assert raf.measure(u, {"available": False}, NOW)["exit_code"] == \
        raf.EXIT_UNAVAILABLE


def test_07_a_truth_that_withholds_the_population_stops_the_report():
    """The truth can apply a gate the raw universe does not carry (e.g. a
    stale ledger). A window it reports unavailable stops the measurement."""
    u = _universe(ROWS, CONTACTS)
    truth = ccr.get_closed_won_truth(list(raf.WINDOWS), now=NOW, universe=u,
                                     resolver=label_resolver(),
                                     resolver_detail="t")
    truth["windows"][0]["publication"]["closed_won_deals"] = {
        "status": "unavailable", "reason": "canonical_ledger_stale"}
    r = raf.measure(u, truth, NOW)
    assert r["exit_code"] == raf.EXIT_UNAVAILABLE
    assert "canonical_ledger_stale" in r["detail"]


def test_08_percentile_is_nearest_rank():
    assert raf.percentile([], 50) is None
    assert raf.percentile([10.0], 90) == 10.0
    assert raf.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert raf.percentile([1.0, 2.0, 3.0, 4.0], 75) == 3.0
    assert raf.percentile([1.0, 2.0, 3.0, 4.0], 90) == 4.0


def test_09_text_render_shows_the_three_measurements(capsys):
    raf._render(_report(ROWS, CONTACTS))   # noqa: SLF001
    out = capsys.readouterr().out
    for heading in ("1. CAMPAIGN PLACEMENT", "2. ACQUISITION DATE",
                    "3. CYCLE LENGTH", "HOW TO READ THIS"):
        assert heading in out
    assert "google_ads_unplaced" in out


def test_10_main_without_a_database_exits_2_with_a_json_report(
        monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert raf.main(["--json"]) == raf.EXIT_UNAVAILABLE
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):])
    assert report["available"] is False


def test_11_the_report_writes_nothing():
    src = open(raf.__file__).read().lower()
    for verb in ("insert ", "update ", "delete ", "upsert", "commit("):
        assert verb not in src
