"""PR-ADS-170A — two Google Ads geo criteria the owner labelled with a country.

Production (read-only, 2026-11-19): ``google_ads_geo_daily_spend`` holds two
criteria with no country code on any row — ``2192`` (17 rows, about £7.64) and
``2364`` (8 rows, about £3.34). Google's own lookup returns no country for
them. PR-ADS-165A / 167A showed them as "(unresolved location)" and withheld a
never-seen lead country's ``$0`` while such spend was in the window.

Owner decision, 2026-11-22: ``2192`` is Cuba (CU), ``2364`` is Iran (IR).

It is an explicit table (``config/geo_owner_labels.yaml``), not an inference:
only a listed id is labelled; a stored Google code always wins; a stored code
that differs from the label is surfaced, never merged; and the label is applied
when the data is READ — the stored rows stay what Google sent.

Real PostgreSQL, the real writers and sync, the real ``api_geo`` handler and
the real mart / Dashboard Countries builders — the harness of
``test_pr_ads_165a_countries_pg_integration``.

PR-ADS-176B deleted the ``#/geo`` page these cases also rendered through
``loadGeo``. Each case keeps its payload assertions; what the page printed is
asserted on the row it printed. The one Countries page's owner note (market
rows, the drawer, the residual's "In residual" items) is checked in case 18
and in ``tests/test_pr_ads_176b_*``.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, RATE, UK, _GoogleAds, _api, _seed_leads, _spend_rows,
    _today, _verify, _window, _write_fx, _write_spend, live_db,
)

APP_JS = ROOT / "static" / "app.js"
WORKFLOW = ROOT / ".github" / "workflows" / "pr-ads-153d-checks.yml"

#: Production's two criteria: Google reports them with no country.
C2192 = ("2192", None, None)
C2364 = ("2364", None, None)
#: A code-less criterion the owner has NOT labelled. By Google's id convention
#: it would be Iraq (2000 + 368) — and nothing may infer that.
UNLISTED = ("2368", None, None)
NOTE = "Country set by owner — Google Ads reports this location without a country"
UNAVAILABLE_NO_COUNTRY = "Unavailable — some spend in this window has no country"


def _labels_at(monkeypatch, tmp_path, text: str | None):
    """Point the loader at a config file with ``text`` (None: no file)."""
    from analysis import geo_owner_labels
    path = tmp_path / "geo_owner_labels.yaml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(geo_owner_labels, "CONFIG_PATH", path)
    return geo_owner_labels


# ── the table and the one rule, pure ────────────────────────────────────────
def test_01_the_owner_table_lists_exactly_the_two_approved_criteria():
    from analysis import geo_owner_labels
    from analysis.iso_countries import ISO_COUNTRY_NAMES
    labels = geo_owner_labels.owner_labels()
    assert labels == {
        "2192": {"code": "CU", "name": "Cuba", "approved": "2026-11-22", "by": "owner"},
        "2364": {"code": "IR", "name": "Iran", "approved": "2026-11-22", "by": "owner"},
    }
    assert all(v["code"] in ISO_COUNTRY_NAMES for v in labels.values())
    assert geo_owner_labels.OWNER_LABEL_NOTE == NOTE


def test_02_only_a_listed_criterion_is_labelled_and_nothing_is_inferred_from_the_id():
    """Google's convention is 2000 + ISO numeric: 2368 would be Iraq, 2004
    Afghanistan. They are not listed, so they resolve to nothing."""
    from db.geo_identity_sql import criterion_identity
    for cid in ("2368", "2004", "2999", "192", "02192", ""):
        ident = criterion_identity(cid, [], None)
        assert (ident["code"], ident["provenance"], ident["owner_label"]) == (
            None, None, None), cid
    cuba = criterion_identity("2192", [], None)
    assert (cuba["code"], cuba["name"], cuba["provenance"]) == (
        "CU", "Cuba", "owner_labelled")
    assert cuba["owner_label"] == {"code": "CU", "name": "Cuba",
                                   "approved": "2026-11-22", "by": "owner"}
    assert cuba["owner_label_mismatch"] is None
    iran = criterion_identity(2364, None, None)          # an int id is the same id
    assert (iran["code"], iran["name"], iran["provenance"]) == (
        "IR", "Iran", "owner_labelled")


def test_03_a_stored_google_code_always_wins_and_a_different_one_is_a_mismatch(caplog):
    from db import geo_identity_sql
    from db.geo_identity_sql import criterion_identity
    geo_identity_sql._MISMATCH_LOGGED.clear()
    # Google agrees with the owner: Google's word, no mismatch.
    same = criterion_identity("2192", ["cu "], "Cuba (Google)")
    assert (same["code"], same["name"], same["provenance"],
            same["owner_label_mismatch"]) == ("CU", "Cuba (Google)", "google_ads", None)
    # Google says something else: Google's code, and the mismatch is named.
    with caplog.at_level(logging.WARNING, logger="db.geo_identity_sql"):
        other = criterion_identity("2192", ["MX"], "Mexico")
    assert (other["code"], other["name"], other["provenance"]) == (
        "MX", "Mexico", "google_ads")
    assert other["owner_label_mismatch"] == {
        "criterion_id": "2192", "owner_code": "CU", "stored_codes": ["MX"]}
    assert any("2192" in r.getMessage() and "MX" in r.getMessage()
               and "NOT applied" in r.getMessage() for r in caplog.records)
    # Two stored codes that disagree name nothing — the label does not settle it.
    both = criterion_identity("2192", ["CU", "MX"], "Cuba")
    assert (both["code"], both["provenance"]) == (None, None)
    assert both["owner_label_mismatch"]["stored_codes"] == ["CU", "MX"]
    # The writer's and the sync's rule reads stored codes only.
    assert geo_identity_sql.resolved_code([]) is None


def test_04_a_malformed_entry_is_ignored_with_a_warning_and_never_raises(
        monkeypatch, tmp_path, caplog):
    text = """
owner_labelled_geo_criteria:
  "2192": {code: CU, approved: "2026-11-22", by: owner}
  "2364": {code: ZZ, approved: "2026-11-22", by: owner}     # not an ISO code
  "2400": {code: Jordan, approved: "2026-11-22", by: owner} # a name, not a code
  "2414": {code: NO, approved: "2026-11-22", by: owner}     # YAML: NO is False
  "2422": {code: LB, by: owner}                             # nobody dated it
  "2434": {code: LY, approved: "soon", by: owner}           # not a date
  "2504": {code: MA, approved: "2026-11-22"}                # nobody approved it
  "2512": CU                                                # not a mapping
  abc: {code: CU, approved: "2026-11-22", by: owner}        # not a criterion id
  "2792": {code: tr, approved: 2026-11-22, by: " ops "}     # valid: date, lower-case
"""
    from analysis.iso_countries import ISO_COUNTRY_NAMES
    labels_mod = _labels_at(monkeypatch, tmp_path, text)
    with caplog.at_level(logging.WARNING, logger="analysis.geo_owner_labels"):
        labels = labels_mod.owner_labels()
    assert labels == {
        "2192": {"code": "CU", "name": "Cuba", "approved": "2026-11-22", "by": "owner"},
        "2792": {"code": "TR", "name": ISO_COUNTRY_NAMES["TR"],
                 "approved": "2026-11-22", "by": "ops"},
    }
    warned = " | ".join(r.getMessage() for r in caplog.records)
    for ignored in ("2364", "2400", "2414", "2422", "2434", "2504", "2512", "abc"):
        assert f"entry {ignored}" in warned or f"entry '{ignored}'" in warned, ignored
    # The rule then treats an ignored id as any unlisted one.
    from db.geo_identity_sql import criterion_identity
    assert criterion_identity("2364", [], None)["code"] is None

    # One criterion listed twice ("2192" and 2192) is a contradiction: neither.
    (tmp_path / "twice.yaml").write_text(
        'owner_labelled_geo_criteria:\n'
        '  "2192": {code: CU, approved: "2026-11-22", by: owner}\n'
        '  2192: {code: MX, approved: "2026-11-22", by: owner}\n', encoding="utf-8")
    monkeypatch.setattr(labels_mod, "CONFIG_PATH", tmp_path / "twice.yaml")
    assert labels_mod.owner_labels() == {}


@pytest.mark.parametrize("text", [
    None,                                        # no file
    "owner_labelled_geo_criteria: [2192, 2364]", # a list, not a mapping
    "owner_labelled_geo_criteria: {",            # not YAML
    "- just\n- a list\n",                        # the document is not a mapping
    "",                                          # empty
    "something_else: 1\n",                       # the key is absent
])
def test_05_an_unreadable_or_empty_config_means_no_labels(monkeypatch, tmp_path, text):
    labels_mod = _labels_at(monkeypatch, tmp_path, text)
    assert labels_mod.owner_labels() == {}
    from db.geo_identity_sql import criterion_identity
    assert criterion_identity("2192", [], None)["code"] is None


# ── the Countries page ──────────────────────────────────────────────────────


def _production_shape(cluster, *, extra_rows=(), leads=None):
    """UK spend every day of a 30-day window; 2192 on 17 days and 2364 on 8,
    all inside it, none with a code — the production state. Leads: Cuba (two
    spellings), Iran, the UK, and Pakistan (no Google spend ever)."""
    start, end = _window(30)
    _write_fx(start - timedelta(days=30), end)
    rows = [_spend_rows(UK, start + timedelta(days=i), 20.0) for i in range(30)]
    rows += [_spend_rows(C2192, start + timedelta(days=i), 0.45) for i in range(17)]
    rows += [_spend_rows(C2364, start + timedelta(days=i), 0.42) for i in range(8)]
    rows += list(extra_rows)
    _write_spend(rows)
    _verify(start - timedelta(days=30), end)
    _seed_leads(cluster, leads if leads is not None else [
        ("u1", "United Kingdom", "qualified"),
        ("c1", "cuba", "junk"), ("c2", "Cuba", "qualified"),
        ("i1", "Iran, Islamic Republic of", "junk"),
        ("p1", "Pakistan", "junk")])
    return start, end


def _stored_codes(cluster, criterion: str) -> set:
    with cluster.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT country_code, country_name FROM google_ads_geo_daily_spend "
                    "WHERE country_criterion_id = %s", (criterion,))
        return set(cur.fetchall())


def test_10_production_shape_reads_cuba_and_iran_with_provenance_and_nothing_unresolved(
        live_db, tmp_path):
    _production_shape(live_db)
    api = _api("30d")

    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    cu, ir, gb, pk = by["code:CU"], by["code:IR"], by["code:GB"], by["code:PK"]
    assert (cu["country"], cu["country_code"], cu["country_resolved"]) == ("Cuba", "CU", True)
    assert (cu["spend_usd"], cu["spend_status"], cu["spend_reason"]) == (
        round(17 * 0.45 * RATE, 2), "published", None)
    assert (cu["country_provenance"], cu["owner_labelled_criteria"]) == (
        "owner_labelled", ["2192"])
    assert (cu["total_leads"], cu["lead_country_spellings"]) == (2, ["Cuba", "cuba"])
    assert (ir["country"], ir["spend_usd"], ir["country_provenance"],
            ir["owner_labelled_criteria"]) == (
        "Iran", round(8 * 0.42 * RATE, 2), "owner_labelled", ["2364"])
    assert ir["lead_country_spellings"] == ["Iran, Islamic Republic of"]
    # A Google-named country and a lead-only one carry no owner provenance.
    assert (gb["country_provenance"], gb["owner_labelled_criteria"]) == (None, [])
    assert (pk["country_provenance"], pk["owner_labelled_criteria"]) == (None, [])
    assert (pk["spend_usd"], pk["spend_status"], pk["spend_reason"]) == (
        0.0, "published", None)
    # The zero's dependency on the labels is a field; a row with spend has none.
    assert pk["zero_rests_on_owner_labels"] == ["2192", "2364"]
    assert [r["zero_rests_on_owner_labels"] for r in (cu, ir, gb)] == [[], [], []]
    assert "spend:unresolved" not in by
    assert geo["unresolved_spend"] == {
        "criteria": 0, "conflicting_criteria": 0, "spend_usd": None,
        "blocks_zero": False, "zero_withheld_countries": []}
    assert geo["owner_labelled_geo"] == {
        "criteria": [
            {"criterion_id": "2192", "country_code": "CU", "country": "Cuba",
             "approved": "2026-11-22", "by": "owner"},
            {"criterion_id": "2364", "country_code": "IR", "country": "Iran",
             "approved": "2026-11-22", "by": "owner"}],
        "mismatches": [], "note": NOTE}

    # Totals conserved: the label moved spend between rows, and added none.
    expected = round((30 * 20.0 + 17 * 0.45 + 8 * 0.42) * RATE, 2)
    assert geo["total_spend_usd"] == expected
    assert round(sum(r["spend_usd"] or 0 for r in geo["rows"]), 2) == expected

    # The stored rows are what Google sent: no code, no name. Nothing was written.
    assert _stored_codes(live_db, "2192") == {(None, None)}
    assert _stored_codes(live_db, "2364") == {(None, None)}


def test_11_an_unlisted_code_less_criterion_stays_unresolved_and_still_withholds(
        live_db, tmp_path):
    """2368 is code-less and NOT listed. It stays "(unresolved location)" and
    Pakistan stays unavailable — the labels did not loosen the 167A rule."""
    start, end = _window(30)
    _production_shape(live_db, extra_rows=[_spend_rows(UNLISTED, end, 3.0)])
    api = _api("30d")
    geo = api["geo"]
    # PR-ADS-176B: what the deleted #/geo table printed, as the rows say it.
    by = {r["country"]: r for r in geo["rows"]}
    assert by["Pakistan"]["spend_usd"] is None
    assert by["Pakistan"]["spend_reason"] == "spend_location_unresolved"
    assert (by["Cuba"]["spend_usd"], by["Cuba"]["country_provenance"]) == (
        round(17 * 0.45 * RATE, 2), "owner_labelled")          # printed "$10"
    assert "Iraq" not in by
    assert geo["unresolved_spend"] == {
        "criteria": 1, "conflicting_criteria": 0, "spend_usd": 3.0 * RATE,
        "blocks_zero": True, "zero_withheld_countries": ["Pakistan"]}
    assert geo["total_spend_usd"] == round(
        (30 * 20.0 + 17 * 0.45 + 8 * 0.42 + 3.0) * RATE, 2)


def test_12_an_owner_labelled_criterion_is_the_carrier_of_its_code(live_db, tmp_path):
    """167A's zero rule: a spend-less country is a measured ``$0`` beside loose
    spend only when another criterion carries its code. Cuba's carrier is
    2192 — on the owner's word — so with 2192's rows OUTSIDE the window and an
    unlisted code-less criterion inside it, Cuba is ``$0`` and says why;
    Pakistan, carried by nothing, is still unavailable."""
    from db import revenue_repository as repo
    start, end = _window(7)
    old = start - timedelta(days=20)
    _write_fx(old, end)
    _write_spend([_spend_rows(UK, start + timedelta(days=i), 20.0) for i in range(7)]
                 + [_spend_rows(C2192, old, 0.45), _spend_rows(UNLISTED, end, 3.0)])
    _verify(old, end)
    _seed_leads(live_db, [("c1", "Cuba", "junk"), ("p1", "Pakistan", "junk"),
                          ("i1", "Iran", "junk")])

    found = repo.fetch_geo_code_carriers(["CU", "IR", "PK", "GB"])
    assert found == {"available": True, "carriers": {
        "CU": [{"criterion_id": "2192", "codes": ["CU"], "provenance": "owner_labelled"}],
        "GB": [{"criterion_id": UK[0], "codes": ["GB"], "provenance": "google_ads"}]}}

    api = _api("7d")
    by = {r["country_key"]: r for r in api["geo"]["rows"]}
    assert (by["code:CU"]["spend_usd"], by["code:CU"]["spend_status"],
            by["code:CU"]["country_provenance"],
            by["code:CU"]["owner_labelled_criteria"]) == (
        0.0, "published", "owner_labelled", ["2192"])
    assert by["code:CU"]["zero_rests_on_owner_labels"] == ["2192"]
    assert api["geo"]["unresolved_spend"]["zero_withheld_countries"] == [
        "Iran", "Pakistan"]


def test_13_a_stored_google_code_beats_the_label_and_the_mismatch_is_surfaced(
        live_db, tmp_path, caplog):
    """Google one day reports 2192 as Mexico. Every row of 2192 is then
    Mexico's — the stored code — the owner's "Cuba" is not applied, the two are
    never merged, and the payload and the log both say so."""
    from db import geo_identity_sql
    geo_identity_sql._MISMATCH_LOGGED.clear()
    start, end = _window(30)
    _production_shape(live_db, extra_rows=[
        _spend_rows(("2192", "MX", "Mexico"), start - timedelta(days=5), 1.0)])
    with caplog.at_level(logging.WARNING, logger="db.geo_identity_sql"):
        api = _api("30d")
    geo = api["geo"]
    by = {r["country_key"]: r for r in geo["rows"]}
    assert (by["code:MX"]["spend_usd"], by["code:MX"]["country_provenance"]) == (
        round(17 * 0.45 * RATE, 2), None)
    # Cuba has leads and no spend row; nothing unresolved is in the window and
    # no criterion carries CU: a measured zero, and not an owner-set COUNTRY.
    assert (by["code:CU"]["spend_usd"], by["code:CU"]["country_provenance"]) == (0.0, None)
    assert by["code:CU"]["zero_rests_on_owner_labels"] == ["2364"]
    assert geo["owner_labelled_geo"]["mismatches"] == [
        {"criterion_id": "2192", "owner_code": "CU", "stored_codes": ["MX"]}]
    assert [c["criterion_id"] for c in geo["owner_labelled_geo"]["criteria"]] == ["2364"]
    assert any("2192" in r.getMessage() and "MX" in r.getMessage()
               for r in caplog.records)
    assert geo["total_spend_usd"] == round(
        (30 * 20.0 + 17 * 0.45 + 8 * 0.42) * RATE, 2)

    # Google agreeing with the owner is Google's word, and no mismatch.
    from db import revenue_repository as repo
    _write_spend([_spend_rows(("2364", "IR", "Iran"), start - timedelta(days=5), 1.0)],
                 run="sync-2")
    out = repo.fetch_geo_daily_spend_by_country(start, end)
    row = {r["country_criterion_id"]: r for r in out["rows"]}["2364"]
    assert (row["country_code"], row["identity_provenance"],
            row["owner_label_mismatch"]) == ("IR", "google_ads", None)


def test_14_a_malformed_config_does_not_crash_the_page_and_labels_nothing(
        live_db, tmp_path, monkeypatch):
    _production_shape(live_db)
    _labels_at(monkeypatch, tmp_path,
               'owner_labelled_geo_criteria:\n'
               '  "2192": {code: Cuba, approved: "2026-11-22", by: owner}\n'
               '  "2364": {code: IR}\n')
    api = _api("30d")
    # Exactly the pre-170A payload: no row rests on an owner label.
    by = {r["country"]: r for r in api["geo"]["rows"]}
    assert by["Pakistan"]["spend_reason"] == "spend_location_unresolved"
    assert not any(r.get("country_provenance") == "owner_labelled"
                   for r in api["geo"]["rows"])
    assert api["geo"]["unresolved_spend"]["criteria"] == 2
    assert api["geo"]["owner_labelled_geo"] == {"criteria": [], "mismatches": [],
                                                "note": NOTE}
    assert api["geo"]["total_spend_usd"] == round(
        (30 * 20.0 + 17 * 0.45 + 8 * 0.42) * RATE, 2)


# ── the sync: the label is never written ────────────────────────────────────
class _Google(_GoogleAds):
    """Google answers for 2192 with NO country (production), until told to."""

    def __init__(self):
        super().__init__()
        self.known = {UK[0]: {"country_code": "GB", "name": "United Kingdom"}}
        self.asked: list = []

    def country_codes(self, criterion_ids) -> dict:   # noqa: D102 — override
        self.asked.append(sorted(str(i) for i in criterion_ids))
        return {str(i): self.known[str(i)] for i in criterion_ids
                if str(i) in self.known}


def _sync(monkeypatch, google, date_from: date, date_to: date, job="sync") -> dict:
    from services import google_ads_geo_sync_service as geo
    monkeypatch.setattr(geo, "fetch_geo_daily", google.fetch)
    monkeypatch.setattr(geo, "fetch_geo_country_codes", google.country_codes)
    return geo.run_google_ads_geo_sync(
        date_from=date_from.isoformat(), date_to=date_to.isoformat(),
        dry_run=False, job_id=job, resume=False)


def test_15_the_sync_stores_what_google_sent_and_a_later_google_code_wins(
        live_db, monkeypatch):
    from db import revenue_repository as repo
    today = _today()
    day = today - timedelta(days=1)
    google = _Google()
    google.gbp_by_day[day] = {UK: 10.0, C2192: 0.45, UNLISTED: 1.0}
    out = _sync(monkeypatch, google, today - timedelta(days=6), today)
    assert out["status"] == "success", out
    ident = out["chunks"][-1]["country_identity"]
    # Stored with no code — both of them. The run names the one the readers
    # will label; it does not label it.
    assert ident["unresolved"] == ["2192", "2368"]
    assert ident["owner_labelled"] == ["2192"]
    assert _stored_codes(live_db, "2192") == {(None, None)}
    # Google WAS asked about 2192: the label does not stop the question.
    assert google.asked == [["2192", "2368", UK[0]]]
    # A reader labels it; the table does not carry the label.
    row = {r["country_criterion_id"]: r for r in
           repo.fetch_geo_daily_spend_by_country(day, day)["rows"]}
    assert (row["2192"]["country_code"], row["2192"]["identity_provenance"],
            row["2192"]["identity_codes"]) == ("CU", "owner_labelled", [])
    assert row["2368"]["country_code"] is None

    # The next morning Google names it — differently. It is asked again
    # (nothing stored names it), its answer is stored, and it wins.
    google.known["2192"] = {"country_code": "MX", "name": "Mexico"}
    google.asked.clear()
    again = _sync(monkeypatch, google, today - timedelta(days=6), today, job="next")
    assert again["status"] == "success", again
    assert ["2192", "2368"] in google.asked
    assert again["chunks"][-1]["country_identity"]["owner_labelled"] == []
    assert _stored_codes(live_db, "2192") == {("MX", "Mexico")}
    row = {r["country_criterion_id"]: r for r in
           repo.fetch_geo_daily_spend_by_country(day, day)["rows"]}["2192"]
    assert (row["country_code"], row["country_name"], row["identity_provenance"]) == (
        "MX", "Mexico", "google_ads")
    assert row["owner_label_mismatch"] == {
        "criterion_id": "2192", "owner_code": "CU", "stored_codes": ["MX"]}


def test_16_neither_writer_reads_the_owner_table():
    """The range writer and the sync fill codes from STORED evidence only
    (``resolved_code``). If either called the reader's rule the label would be
    written into the table and Google's later answer could never win."""
    writers = (ROOT / "db" / "writers.py").read_text(encoding="utf-8")
    assert "geo_owner_labels" not in writers
    assert "criterion_identity(" not in writers
    sync = (ROOT / "services" / "google_ads_geo_sync_service.py").read_text(encoding="utf-8")
    body = sync[sync.index("def _resolve_country_metadata"):]
    body = body[:body.index("\ndef ")]
    assert "criterion_identity(" not in body
    # The one use of the owner table there comes after the rows are filled.
    assert body.count("geo_owner_labels.owner_labels()") == 1
    assert body.index("geo_owner_labels.owner_labels()") > body.index('r["country_code"] = meta.get')
    # Every READER goes through the one rule.
    repo = (ROOT / "db" / "revenue_repository.py").read_text(encoding="utf-8")
    for fn in ("def fetch_geo_daily_spend_by_country",
               "def fetch_geo_criterion_identities", "def fetch_geo_code_carriers"):
        part = repo[repo.index(fn):]
        part = part[:part.index("\ndef ")]
        assert "criterion_identity(" in part, fn
        assert "resolved_code(" not in part, fn
    service = (ROOT / "services" / "country_evidence_service.py").read_text(encoding="utf-8")
    part = service[service.index("def identify_spend_rows"):]
    assert "criterion_identity(" in part[:part.index("\ndef ")]


# ── Country Revenue & Spend and Dashboard Countries ─────────────────────────
def _seed_revenue_window(cluster, monkeypatch, *, campaign_gbp=107.0, extra=()):
    """Campaign spend and geo spend on one day of an open window: the UK £100,
    2364 £4 and 2192 £3, the two with no code (plus ``extra`` geo countries).
    They reconcile exactly unless ``campaign_gbp`` says the campaign total is
    larger — a shortfall Google's geographic view assigns to no location."""
    import db.writers as writers
    from services.canonical_contract import resolve_canonical_window
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    w = resolve_canonical_window("last_6_months")
    start, today = date.fromisoformat(w["start_date"]), date.fromisoformat(w["end_date"])
    day = today - timedelta(days=1)
    _write_fx(start, today)
    assert writers.upsert_campaign_daily_spend([{
        "customer_id": CUSTOMER, "currency_code": "GBP", "campaign_id": "111",
        "campaign_name": "brand - uk", "spend_date": day,
        "cost_micros": int(round(campaign_gbp * 1_000_000)),
        "source_query_version": "t"}]) == 1
    _write_spend([_spend_rows(UK, day, 100.0, campaign="111"),
                  _spend_rows(C2364, day, 4.0, campaign="111"),
                  _spend_rows(C2192, day, 3.0, campaign="111")]
                 + [_spend_rows(c, day, gbp, campaign="111") for c, gbp in extra])
    assert writers.upsert_spend_coverage(
        CUSTOMER, start, today, "verified", rows_written=1,
        cost_micros_total=int(round(campaign_gbp * 1_000_000)))
    _verify(start, today)
    return start, day


def test_17_the_revenue_country_pages_carry_the_label_and_conserve_the_total(
        live_db, monkeypatch):
    from analysis import country_identity
    from db import revenue_repository as repo
    from services.dashboard_countries_service import build_dashboard_countries
    from services.revenue_decision_mart import build_revenue_decision_mart
    start, day = _seed_revenue_window(live_db, monkeypatch)
    expected_block = {
        "criteria": [
            {"criterion_id": "2192", "country_code": "CU", "country": "Cuba",
             "approved": "2026-11-22", "by": "owner"},
            {"criterion_id": "2364", "country_code": "IR", "country": "Iran",
             "approved": "2026-11-22", "by": "owner"}],
        "mismatches": []}

    read = repo.fetch_geo_daily_spend_by_country(start, day)
    assert read["owner_labelled_geo"] == expected_block
    assert read["total_spend"] == pytest.approx(107.0)

    # Country Revenue & Spend (the mart's country view).
    mart = build_revenue_decision_mart(view="country", window="last_6_months")
    assert mart["spend_truth"]["country_roas_unblockable"] is True
    assert mart["spend_truth"]["owner_labelled_geo"] == expected_block
    rows = {r.get("country_code"): r for r in mart["rows"] if not r.get("is_residual")}
    iran = rows["IR"]
    assert iran["country"] == "Iran"
    assert iran["spend"] == pytest.approx(4.0 * RATE)
    assert (iran["spend_country_provenance"], iran["owner_labelled_criteria"]) == (
        "owner_labelled", ["2364"])
    assert rows["GB"]["spend"] == pytest.approx(100.0 * RATE)
    assert "spend_country_provenance" not in rows["GB"]
    # Cuba is not in the 96-market registry these two pages group on
    # (`country_identity.SUPPORTED_COUNTRIES`, unchanged): like every other
    # Google country outside it, its spend is in the residual — never a market
    # row, never dropped.
    assert not country_identity.resolve(name="Cuba", code="CU").is_country
    assert "CU" not in rows
    residual = next(r for r in mart["rows"] if r.get("is_residual"))
    assert residual["spend"] == pytest.approx(3.0 * RATE)
    assert residual["spend_components"] == {
        "outside_listed_markets": pytest.approx(3.0 * RATE),
        "country_unidentified": 0.0, "geo_unassigned": 0.0}
    assert residual["outside_listed_market_countries"] == ["Cuba"]
    assert "spend_country_provenance" not in residual
    # Conserved: every pound of geo spend is on exactly one row.
    assert sum(r["spend"] for r in mart["rows"]) == pytest.approx(107.0 * RATE)

    # Dashboard Countries.
    page = build_dashboard_countries("last_6_months")
    assert page["owner_labelled_geo"] == expected_block
    by = {c.get("country_code"): c for c in page["countries"]}
    assert (by["IR"]["country_name"], by["IR"]["spend_native"]) == ("Iran", pytest.approx(4.0))
    assert by["IR"]["spend_usd"] == pytest.approx(4.0 * RATE)
    assert (by["IR"]["spend_country_provenance"], by["IR"]["owner_labelled_criteria"]) == (
        "owner_labelled", ["2364"])
    assert "spend_country_provenance" not in by["GB"]
    assert "CU" not in by
    # This page's spend card sums its market rows (unchanged): the UK and Iran.
    # Cuba's £3 is on no market row here, exactly as before the label and as
    # for every Google country outside the registry — and Iran's £4, which the
    # card left out while 2364 named nothing, is now in it.
    assert page["kpis"]["verified_spend_native"]["amount"] == pytest.approx(104.0)
    assert sum(c["spend_native"] for c in page["countries"]) == pytest.approx(104.0)
    assert page["kpis"]["countries_with_spend"] == 2
    # … and the payload says so: the card's scope, and what lies outside it.
    assert page["kpis"]["verified_spend_scope"] == "listed_markets"
    assert page["kpis"]["spend_outside_listed_markets"] == {
        "native": 3.0, "usd": pytest.approx(3.0 * RATE), "currency": "GBP",
        "countries": ["Cuba"], "no_country_native": 0.0}


def _node_eval(tmp_path, script: str) -> str:
    node = shutil.which("node")
    assert node, "node is required: CI runs `node --check static/app.js`"
    path = tmp_path / "eval.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout


def test_18_all_three_country_pages_render_the_same_owner_note(tmp_path):
    """The real ``ownerLabelledCountryNote`` from app.js, evaluated in node,
    and each page's row template calling it with the row's own field."""
    src = APP_JS.read_text(encoding="utf-8")
    const = re.search(r"^const OWNER_LABELLED_COUNTRY_NOTE = .*?;$", src, re.M).group(0)
    def source_of(name):
        at = src.index(f"function {name}(")
        return src[at:src.index("\n}\n", at) + 3]
    fn = source_of("ownerLabelledCountryNote") + source_of("spendOwnerNote")
    script = (
        "const escapeHtml = (s) => String(s).replace(/&/g, '&amp;')"
        ".replace(/</g, '&lt;').replace(/\"/g, '&quot;');\n"
        f"{const}\n{fn}\n"
        "console.log(JSON.stringify([OWNER_LABELLED_COUNTRY_NOTE,"
        " ownerLabelledCountryNote('owner_labelled'), ownerLabelledCountryNote('google_ads'),"
        " ownerLabelledCountryNote(null), ownerLabelledCountryNote(undefined),"
        " spendOwnerNote({spend_country_provenance: 'owner_labelled'}),"
        " spendOwnerNote({country_provenance: 'owner_labelled'}), spendOwnerNote(null)]));\n")
    note, owner, google, null, undef, spend, wrong_field, no_row = json.loads(
        _node_eval(tmp_path, script))
    assert note == NOTE
    assert owner == f' <span class="owner-label-note" title="{NOTE}">set by owner</span>'
    assert (google, null, undef) == ("", "", "")
    assert (spend, wrong_field, no_row) == (owner, "", "")

    def body(name):
        a = src.index(f"function {name}(")
        return src[a:src.index("\nfunction ", a + 10)]
    # PR-ADS-176B: the #/geo page (renderGeoTable) is deleted. The one
    # Countries page's market row and drawer, and its "In residual" items —
    # countries outside the listed markets, e.g. owner-labelled Cuba — carry
    # the same note.
    table = body("renderRoasCountryTable")
    assert "const name = countryDisplayName(r.country);" in table
    assert "${escapeHtml(name)}${spendOwnerNote(r)}</td>" in table
    assert "${ownerLabelledCountryNote(item.provenance)}" in body(
        "renderCountryResidualItemRow")
    assert "<strong>${escapeHtml(name)}</strong>${spendOwnerNote(r)}" in body(
        "renderCountryDrawer")
    dash = src[src.index('<tr class="ctry-row" data-ctry-row='):]
    assert ('${escapeHtml(r.country_name || "—")}${spendOwnerNote(r)}'
            in dash[:dash.index("</tr>")])
    # The residual row itself is not a country and carries no note: the
    # helpers are called from those row templates and nowhere else.
    # definition, spendOwnerNote, the Countries page's "In residual" items
    assert src.count("ownerLabelledCountryNote(") == 3
    # definition + the Countries page's row and drawer + the Dashboard
    # Countries row and drawer
    assert src.count("spendOwnerNote(") == 5
    drawer = body("wireCtryDrawers")
    assert ('<strong>${escapeHtml(r.country_name || "Country")}</strong>${spendOwnerNote(r)}'
            in drawer)


# ═════════════════════════════════════════════════════════════════════════════
# PR-ADS-170A-F1 — review corrections
# ═════════════════════════════════════════════════════════════════════════════
#: A country Google Ads names itself that is not one of the 96 listed markets.
FIJI = ("2242", "FJ", "Fiji")


def test_20_the_residual_row_adds_the_geo_shortfall_to_non_market_spend(
        live_db, monkeypatch):
    """Campaign £113; geo: UK £100, 2364 £4 (Iran, a listed market), 2192 £3
    (Cuba) and Fiji £2 — the last two are known countries outside the listed
    markets — so £4 is assigned to no location. The residual row used to be
    OVERWRITTEN with that £4, dropping Cuba's and Fiji's £5: the rows summed to
    £108 beside a campaign total of £113."""
    from analysis import country_identity
    from services.dashboard_countries_service import build_dashboard_countries
    from services.revenue_decision_mart import build_revenue_decision_mart
    assert not country_identity.resolve(name="Fiji", code="FJ").is_country
    _seed_revenue_window(live_db, monkeypatch, campaign_gbp=113.0, extra=[(FIJI, 2.0)])

    mart = build_revenue_decision_mart(view="country", window="last_6_months")
    st = mart["spend_truth"]
    assert st["country_spend_status"] == "reconciled_with_residual"
    assert st["country_residual_native"] == pytest.approx(4.0)     # the shortfall alone
    # Every pound of campaign spend is on exactly one row.
    assert sum(r["spend"] for r in mart["rows"]) == pytest.approx(113.0 * RATE)
    residual = [r for r in mart["rows"] if r.get("is_residual")]
    assert len(residual) == 1
    residual = residual[0]
    assert residual["spend"] == pytest.approx((5.0 + 4.0) * RATE)
    # … and the row says what its spend is made of.
    assert residual["spend_components"] == {
        "outside_listed_markets": pytest.approx(5.0 * RATE),
        "country_unidentified": 0.0,
        "geo_unassigned": pytest.approx(4.0 * RATE)}
    assert residual["outside_listed_market_countries"] == ["Cuba", "Fiji"]
    assert (residual["spend_native"], residual["spend_usd"]) == (
        pytest.approx(9.0), pytest.approx(9.0 * RATE))
    assert (residual["spend_geo_unassigned_native"],
            residual["spend_geo_unassigned_usd"]) == (
        pytest.approx(4.0), pytest.approx(4.0 * RATE))
    notes = " | ".join(residual["attribution_notes"])
    assert ("Includes Google Ads spend whose country is known but is not one of "
            "the listed markets (Cuba, Fiji). It is not spend without a country.") in notes
    # No deal or lead is in this residual: nothing here claims an unidentified one.
    assert "could not be identified" not in notes
    rows = {r.get("country_code"): r for r in mart["rows"] if not r.get("is_residual")}
    assert (rows["GB"]["spend"], rows["IR"]["spend"]) == (
        pytest.approx(100.0 * RATE), pytest.approx(4.0 * RATE))

    # Dashboard Countries is not restructured: its residual tile stays the
    # shortfall, its card stays the listed markets — and now says so, with the
    # amount outside them.
    page = build_dashboard_countries("last_6_months")
    assert page["residual"]["spend_native"] == pytest.approx(4.0)
    k = page["kpis"]
    assert k["verified_spend_native"]["amount"] == pytest.approx(104.0)
    assert k["verified_spend_scope"] == "listed_markets"
    assert k["spend_outside_listed_markets"] == {
        "native": 5.0, "usd": pytest.approx(5.0 * RATE), "currency": "GBP",
        "countries": ["Cuba", "Fiji"], "no_country_native": 0.0}
    # Listed markets + outside them + the shortfall = the campaign total.
    assert (k["verified_spend_native"]["amount"]
            + k["spend_outside_listed_markets"]["native"]
            + page["residual"]["spend_native"]) == pytest.approx(113.0)


def test_21_the_shortfall_is_added_never_written_over_and_unknown_stays_unknown():
    from services.revenue_attribution_service import (
        _apply_geo_spend_residual, _residual_spend_components,
    )

    def row(spend):
        return {"is_residual": True, "spend": spend, "attribution_notes": [],
                "spend_components": _residual_spend_components(
                    outside_listed_markets=spend, country_unidentified=0.0,
                    geo_unassigned=0.0)}
    out = _apply_geo_spend_residual([row(6.25)], 4.0, 5.0, True,
                                    bucketed_native=5.0, bucketed_usd=6.25)[0]
    assert out["spend"] == 11.25
    assert out["spend_components"] == {"outside_listed_markets": 6.25,
                                       "country_unidentified": 0.0,
                                       "geo_unassigned": 5.0}
    assert (out["spend_native"], out["spend_usd"]) == (9.0, 11.25)
    # An unknown USD shortfall makes the row's USD unknown — not the part known.
    unknown = _apply_geo_spend_residual([row(6.25)], 4.0, None, True,
                                        bucketed_native=5.0, bucketed_usd=6.25)[0]
    assert (unknown["spend"], unknown["spend_usd"], unknown["spend_native"]) == (
        None, None, 9.0)
    assert unknown["spend_components"]["geo_unassigned"] is None
    # A residual holding nothing yet: the shortfall is the whole row.
    alone = _apply_geo_spend_residual(
        [{"is_residual": True, "attribution_notes": []}], 250.0, 325.0, True)[0]
    assert (alone["spend"], alone["spend_native"], alone["spend_usd"]) == (
        325.0, 250.0, 325.0)


@pytest.mark.parametrize("text, why", [
    ('owner_labelled_geo_criteria:\n'
     '  "2192": {code: CU, approved: "2026-11-22", by: owner}\n'
     '  "2192": {code: MX, approved: "2026-11-22", by: owner}\n'
     '  "2364": {code: IR, approved: "2026-11-22", by: owner}\n', "same string key twice"),
    ('owner_labelled_geo_criteria:\n'
     '  2192: {code: CU, approved: "2026-11-22", by: owner}\n'
     '  2192: {code: MX, approved: "2026-11-22", by: owner}\n'
     '  "2364": {code: IR, approved: "2026-11-22", by: owner}\n', "same int key twice"),
    ('owner_labelled_geo_criteria:\n'
     '  "2192": {code: CU, approved: "2026-11-22", by: owner}\n'
     '  2192: {code: MX, approved: "2026-11-22", by: owner}\n'
     '  "2364": {code: IR, approved: "2026-11-22", by: owner}\n', "string and int key"),
    ('owner_labelled_geo_criteria:\n'
     '  "2192": {code: CU, approved: "2026-11-22", by: owner}\n'
     'owner_labelled_geo_criteria:\n'
     '  "2192": {code: MX, approved: "2026-11-22", by: owner}\n', "top-level key twice"),
    ('owner_labelled_geo_criteria:\n'
     '  "2192": {code: CU, code: MX, approved: "2026-11-22", by: owner}\n'
     '  "2364": {code: IR, approved: "2026-11-22", by: owner}\n', "a field twice"),
])
def test_22_a_duplicated_key_anywhere_labels_nothing(monkeypatch, tmp_path, caplog,
                                                     text, why):
    """``yaml.safe_load`` keeps the LAST of two equal keys, silently: 2192
    would read Mexico with no trace of the contradiction. Fail closed — the
    whole file labels nothing, and says why."""
    labels_mod = _labels_at(monkeypatch, tmp_path, text)
    with caplog.at_level(logging.WARNING, logger="analysis.geo_owner_labels"):
        assert labels_mod.owner_labels() == {}, why
    warned = " | ".join(r.getMessage() for r in caplog.records)
    assert "duplicate key" in warned and "no criterion is owner-labelled" in warned
    from db.geo_identity_sql import criterion_identity
    assert criterion_identity("2192", [], None)["code"] is None
    assert criterion_identity("2364", [], None)["code"] is None


def test_23_a_future_approval_and_a_non_ascii_id_are_not_labels(
        monkeypatch, tmp_path, caplog):
    from analysis import geo_owner_labels
    # raising=False: against the first commit (no `_today`) this case then fails
    # on what it is about — the future date is accepted — not on the patch.
    monkeypatch.setattr(geo_owner_labels, "_today", lambda: date(2026, 11, 22),
                        raising=False)
    text = (
        'owner_labelled_geo_criteria:\n'
        '  "2192": {code: CU, approved: "2026-11-22", by: owner}\n'      # today: valid
        '  "2364": {code: IR, approved: "2026-11-23", by: owner}\n'      # tomorrow
        '  "2400": {code: JO, approved: 2031-01-01, by: owner}\n'        # a YAML date
        '  "٢١٩٢": {code: CU, approved: "2026-11-22", by: owner}\n'
        '  "２１９３": {code: CU, approved: "2026-11-22", by: owner}\n')
    labels_mod = _labels_at(monkeypatch, tmp_path, text)
    with caplog.at_level(logging.WARNING, logger="analysis.geo_owner_labels"):
        labels = labels_mod.owner_labels()
    assert sorted(labels) == ["2192"]
    warned = " | ".join(r.getMessage() for r in caplog.records)
    assert "entry 2364 ignored: approved 2026-11-23 is in the future" in warned
    assert "entry 2400 ignored: approved 2031-01-01 is in the future" in warned
    assert warned.count("criterion id is not a number") == 2


def test_24_the_map_hover_and_the_revenue_pages_carry_the_marker_and_the_mismatch(
        live_db, tmp_path):
    """Provenance wherever a labelled country's spend is shown, not only in
    the table: the Countries map hover, the Dashboard Countries bubble tooltip
    and row drawer; and the mismatch notice on the two revenue pages.

    PR-ADS-176B: the Countries map (#/geo) is deleted with its page. What it
    drew from is still served: the payload marks both labelled countries, and
    in production's shape names no mismatch (so no notice)."""
    _production_shape(live_db)
    geo = _api("30d")["geo"]
    prov = {r["country"]: r.get("country_provenance") for r in geo["rows"]}
    assert (prov["Cuba"], prov["Iran"]) == ("owner_labelled", "owner_labelled")
    assert prov["United Kingdom"] != "owner_labelled"
    assert (geo.get("owner_labelled_geo") or {}).get("mismatches") == []

    src = APP_JS.read_text(encoding="utf-8")

    def source_of(name):
        at = src.index(f"function {name}(")
        return src[at:src.index("\n}\n", at) + 3]

    def body(name):
        at = src.index(f"function {name}(")
        return src[at:src.index("\nfunction ", at + 10)]
    script = (
        "const escapeHtml = (s) => String(s).replace(/&/g, '&amp;')"
        ".replace(/</g, '&lt;').replace(/\"/g, '&quot;');\n"
        "const fmtMoney = (v) => '$' + Number(v).toFixed(2);\n"
        "const fmtCompactCurrency = (v, c) => (c === 'GBP' ? '£' : c + ' ') + Number(v).toFixed(2);\n"
        + "".join(source_of(n) for n in (
            "ownerLabelledCountryText", "ownerLabelMismatchText",
            "ownerLabelMismatchNotice", "residualSpendCell", "ctryOutsideMarketsText"))
        + "console.log(JSON.stringify(["
        "ownerLabelledCountryText('owner_labelled'), ownerLabelledCountryText('google_ads'),"
        "ownerLabelMismatchNotice({mismatches: []}), ownerLabelMismatchNotice(undefined),"
        "ownerLabelMismatchNotice({mismatches: [{criterion_id: '2192', owner_code: 'CU', stored_codes: ['MX']},"
        " {criterion_id: '2364', owner_code: 'IR', stored_codes: ['IQ', 'IR']}]}),"
        "residualSpendCell({spend: 11.25, spend_components: {outside_listed_markets: 6.25,"
        " country_unidentified: 0, geo_unassigned: 5}, outside_listed_market_countries: ['Cuba', 'Fiji']}),"
        "residualSpendCell({spend: 5, spend_components: {outside_listed_markets: 0,"
        " country_unidentified: 0, geo_unassigned: 5}, outside_listed_market_countries: []}),"
        "residualSpendCell({spend: 5}),"
        "ctryOutsideMarketsText({spend_outside_listed_markets: {native: 5, currency: 'GBP', countries: ['Cuba', 'Fiji']}}),"
        "ctryOutsideMarketsText({spend_outside_listed_markets: {native: 0, currency: 'GBP', countries: []}}),"
        "ctryOutsideMarketsText({spend_outside_listed_markets: null}), ctryOutsideMarketsText({})"
        "]));\n")
    (text_owner, text_google, none_a, none_b, notice, cell, cell_plain, cell_old,
     outside, outside_zero, outside_null, outside_absent) = json.loads(
        _node_eval(tmp_path, script))
    assert (text_owner, text_google) == (" (country set by owner)", "")
    assert (none_a, none_b) == ("", "")
    assert notice == (
        '<p class="mapping-notice mapping-notice--warn owner-label-mismatch" role="status">'
        "Owner country label not applied — Google Ads now reports a country for "
        "locations the owner labelled differently (location 2192: the owner set CU, "
        "Google Ads reports MX — MX is used; location 2364: the owner set IR, Google "
        "Ads reports IQ, IR (two different countries, so its spend is under no "
        "country)). Google's code always wins. Review the entry in "
        "config/geo_owner_labels.yaml.</p>")
    assert cell == ('$11.25<br><small class="metric-reason">$6.25 is spend in Cuba, Fiji '
                    "— known countries, not among the listed markets; $5.00 has no "
                    "country</small>")
    assert (cell_plain, cell_old) == ("$5.00", "$5.00")
    assert outside == (" · not included: £5.00 in countries outside the listed "
                       "markets (Cuba, Fiji)")
    assert (outside_zero, outside_null, outside_absent) == ("", "", "")

    # Each helper is wired where the figure is shown.
    assert ('${r.country_name || "—"}${ownerLabelledCountryText(r.spend_country_provenance)}'
            in body("wireCtryBubbleHover"))
    assert "${ownerLabelMismatchNotice(d.owner_labelled_geo)}" in body("renderDashboardCountries")
    assert ("${ownerLabelMismatchNotice(st.owner_labelled_geo)}"
            in body("renderRoasCountriesPage"))
    # PR-ADS-176B: the residual's spend cell, unless the gate withheld it.
    assert ("? roasCountrySpendCell(r) : residualSpendCell(r)}</td>"
            in body("renderRoasCountryTable"))
    kpi = body("renderCtryKpiRow")
    assert "sub: usdSub + ctryOutsideMarketsText(k)," in kpi
    assert "country-attributed · listed markets only" in kpi
    # PR-ADS-176B: the #/geo page's own notice element went with it; the one
    # Countries page renders the notice in its body (asserted above).
    assert 'id="geo-owner-label-notice"' not in (ROOT / "static" / "index.html").read_text(
        encoding="utf-8")


def test_19_this_module_is_in_ci_s_postgresql_step_and_its_did_run_list():
    text = WORKFLOW.read_text(encoding="utf-8")
    name = Path(__file__).stem
    assert f"tests/{name}.py \\" in text
    assert f'"tests.{name}",' in text


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
