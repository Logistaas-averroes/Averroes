"""PR-ADS-171B — the Meta Ads (spend) line and the Paid Social split on a real
PostgreSQL schema.

The real Meta sync (against the fake Graph API of the two production
accounts), writers, repository and truth function feed the line; the real
funnel read feeds the Paid Social split from the stored drill-down column.
Every case needs the database; a skipped module is not merge evidence.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from tests import meta_ads_fakes as fakes
from tests.test_pr_ads_171a_meta_ads_pg_integration import (  # noqa: F401 — fixture
    _fx, _pair, _run, live_db, q,
)

#: An hour after the 06:00 UTC sync of 18 Nov: Amman is read through the 17th,
#: Los Angeles through the 16th.
LATER = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)


def _line(start, end):
    from services.meta_ads_spend_line_service import build_meta_ads_spend_line
    return json.loads(json.dumps(build_meta_ads_spend_line(
        start, end, now=LATER, page_zone="Europe/London")))


def test_01_the_line_publishes_what_the_stored_accounts_prove(live_db, monkeypatch):
    fakes.configure(monkeypatch, _pair())
    _fx(monkeypatch)
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    line = _line(date(2026, 11, 10), date(2026, 11, 16))
    assert line["state"] == "verified"
    # 70.00 + 60.34 in Amman; Los Angeles verified, no delivery.
    assert line["spend"]["value"] == 130.34 and line["spend"]["status"] == "published"
    accounts = {a["account_id"]: a for a in line["accounts"]}
    assert accounts[fakes.OLD_ACCOUNT_ID]["verified_through"] == "2026-11-17"
    assert accounts[fakes.ACCOUNT_ID]["verified_through"] == "2026-11-16"
    assert accounts[fakes.OLD_ACCOUNT_ID]["native_spend"]["value"] == 130.34
    assert line["owner_declared_retired"] == []


def test_02_a_day_one_account_has_not_read_withholds_everything(live_db, monkeypatch):
    fakes.configure(monkeypatch, _pair())
    _fx(monkeypatch)
    _run()
    # The 17th: read in Amman, not yet in Los Angeles.
    line = _line(date(2026, 11, 10), date(2026, 11, 17))
    assert line["spend"]["status"] == "withheld"
    assert line["spend"]["reason"] == "meta_ads_spend_coverage_incomplete"
    text = json.dumps(line)
    for figure in ("130.34", "142.34", "70.0", "60.34", "12.0"):
        assert figure not in text, figure
    assert f"account {fakes.ACCOUNT_ID} is not verified" in line["withheld_detail"]


def test_03_not_set_up_and_nothing_stored_is_not_connected(live_db, monkeypatch):
    fakes.unconfigure(monkeypatch)
    line = _line(date(2026, 11, 10), date(2026, 11, 16))
    assert line["connected"] is False
    assert line["spend"]["reason"] == "meta_ads_not_connected"


def test_04_paid_social_split_reads_the_stored_drilldown(live_db):
    from db import crm_funnel_repository as funnel_repo
    from services.channel_funnel_service import build_funnel, reconcile
    rows = [("1", "facebook"), ("2", "instagram"), ("3", "linkedin"),
            ("4", None), ("5", ""), ("6", "facebook ads")]
    with live_db.connect() as conn, conn.cursor() as cur:
        for cid, drill in rows:
            cur.execute(
                "INSERT INTO hubspot_contact_funnel (contact_id, created_at, "
                "lifecycle_stage, hs_analytics_source, hs_analytics_source_data_1) "
                "VALUES (%s, %s, 'lead', 'PAID_SOCIAL', %s)",
                (cid, datetime(2026, 11, 1, 10, tzinfo=timezone.utc), drill))
        conn.commit()
    read = funnel_repo.fetch_channel_funnel_contacts(
        datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert read["available"] is True
    funnel = build_funnel(read["rows"])
    assert reconcile(funnel) == []
    (ch,) = funnel["channels"]
    split = {p["platform"]: p["contacts"] for p in ch["platform_split"]}
    assert split == {"meta": 2, "linkedin": 1, "other": 1, "unknown": 2}
