"""PR-ADS-171B — Meta spend on the Channels page (and, until PR-ADS-181A
retired it, the Dashboard's Channels tab).

* A separate "Meta Ads (spend)" line, fed ONLY by
  ``meta_ads_spend_truth_service.build_meta_ads_spend_truth_for_days`` over the
  page's settled dates (each ad account on its own calendar, the truth's
  rule). Published → the number; withheld / unavailable / coverage
  incomplete / not connected → no spend figure anywhere in the payload, and
  the word and the reason on the page.
* The owner's retirement declaration is shown beside the figure whenever its
  marker is on it; a marker without its disclosure withholds the figure.
* HubSpot's Paid Social contacts split by drill-down 1 exactly as stored:
  facebook + instagram = Meta, linkedin = LinkedIn, anything else = other, no
  value = unknown — never Meta.
* No ratio, key or column joins Meta spend to a HubSpot count.

The Meta store is faked at the repository boundary with the two production
accounts (2026-11-22): 944370805110952 USD Asia/Amman, 4730305973961806 GBP
America/Los_Angeles with no spend. The truth function itself is the real one.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from analysis import closed_won_truth as cwt
from tests import meta_ads_fakes as fakes

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

#: 12:00 UTC, 22 Nov 2026: London's today is the 22nd, the settled window
#: ends on the 21st.
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
YESTERDAY = date(2026, 11, 21)
AMMAN = fakes.OLD_ACCOUNT_ID
LA = fakes.ACCOUNT_ID
#: Production's Amman figure since 2026-04-20 — a sentinel: it must not appear
#: anywhere in a payload whose total is not published.
AMMAN_USD = 5927.77


def _chunk(start, end, fetched_at, status="verified"):
    return {"chunk_start": start, "chunk_end": end, "status": status,
            "rows_written": 3, "cost_micros_total": 0, "fetched_at": fetched_at}


def amman(**kw):
    a = {"account_id": AMMAN, "name": "Logistaas", "currency": "USD",
         "zone": "Asia/Amman", "native": AMMAN_USD, "usd": AMMAN_USD,
         "fx_complete": True, "last_spend": "2026-11-21",
         # The 06:00 UTC sync: Amman's 21st ended at 21:00 UTC on the 21st.
         "chunks": [_chunk("2026-04-20", "2026-11-21", "2026-11-22T06:02:00+00:00")]}
    a.update(kw)
    return a


def los_angeles(**kw):
    a = {"account_id": LA, "name": "Logistaas Limited", "currency": "GBP",
         "zone": "America/Los_Angeles", "native": 0.0, "usd": 0.0,
         "fx_complete": True, "last_spend": None,
         # Production today: at 06:00 UTC Los Angeles' 21st has not ended, so
         # the sync reads it through the 20th.
         "chunks": [_chunk("2026-04-20", "2026-11-20", "2026-11-22T06:02:00+00:00")]}
    a.update(kw)
    return a


def los_angeles_read_through_21st(**kw):
    """The same account once its 21st is read after it ended (08:00 UTC)."""
    return los_angeles(chunks=[_chunk("2026-04-20", "2026-11-21",
                                      "2026-11-22T09:00:00+00:00")], **kw)


def meta_store(monkeypatch, accounts, *, configured=None, retired=None,
               available=True, states=None):
    """Production's Meta store at the repository boundary, and the
    environment. ``configured``: the ids listed in credential set 1 (default:
    every account); ``retired``: META_AD_ACCOUNT_RETIRED."""
    from db import meta_ads_repository as repo
    fakes.unconfigure(monkeypatch)
    ids = [a["account_id"] for a in accounts] if configured is None else configured
    if ids:
        monkeypatch.setenv("META_ADS_ACCESS_TOKEN", fakes.SENTINEL_TOKEN)
        monkeypatch.setenv("META_AD_ACCOUNT_ID", ",".join(ids))
        monkeypatch.setenv("META_API_VERSION", fakes.VERSION)
    if retired is not None:
        monkeypatch.setenv("META_AD_ACCOUNT_RETIRED", retired)
    by_id = {a["account_id"]: a for a in accounts}

    def stored():
        if not available:
            return {"available": False, "accounts": []}
        rows = []
        for a in accounts:
            verified = [c for c in a["chunks"] if c["status"] == "verified"]
            rows.append({
                "account_id": a["account_id"], "account_name": a["name"],
                "currency_code": a["currency"], "time_zone_name": a["zone"],
                "covered_from": min((c["chunk_start"] for c in verified), default=None),
                "covered_through": max((c["chunk_end"] for c in verified), default=None),
                "has_spend_rows": bool(a["native"]),
                "last_spend_date": a.get("last_spend")})
        return {"available": True, "accounts": rows}

    calls = []

    def coverage(s, e, account_id):
        calls.append((account_id, s, e))
        return {"available": True, "chunks": by_id[account_id]["chunks"]}

    def spend(s, e, account_id):
        a = by_id[account_id]
        return {"available": True, "total_spend": a["native"],
                "total_spend_usd": a["usd"] if a["fx_complete"] else None,
                "fx_complete": a["fx_complete"], "currency_code": a["currency"]}

    monkeypatch.setattr(repo, "fetch_meta_accounts", stored)
    monkeypatch.setattr(repo, "fetch_meta_account_sync_states", lambda: {
        "available": available, "states": states or {}})
    monkeypatch.setattr(repo, "fetch_meta_spend_coverage", coverage)
    monkeypatch.setattr(repo, "fetch_meta_campaign_spend", spend)
    return calls


def published(monkeypatch):
    return meta_store(monkeypatch, [amman(), los_angeles_read_through_21st()])


# ── the Channels outcomes, with the 168A fixtures ───────────────────────────

def _outcomes(monkeypatch, window="180d", now=NOW):
    from tests.test_pr_ads_168a_channel_outcomes import _contact, _deal, _ledger, _spend
    import tests.test_pr_ads_168a_channel_outcomes as t168
    monkeypatch.setattr(t168, "NOW", now)
    _ledger(monkeypatch, [_deal("a", "2026-11-10"), _deal("b", "2026-11-11"),
                          _deal("s", "2026-11-12")],
            [_contact("a", 1, "PAID_SEARCH"), _contact("b", 2, "DIRECT_TRAFFIC"),
             _contact("s", 3, "PAID_SOCIAL")])
    _spend(monkeypatch, fetched_at=datetime(2026, 11, 22, 6, 2, tzinfo=timezone.utc))
    from services.source_attribution_service import build_channel_outcomes
    return json.loads(json.dumps(build_channel_outcomes(window, now=now), default=str))


def _row(out, source):
    return next(r for r in out["channels"] if r["source"] == source)


def _no_meta_figure(payload) -> list:
    """Every place the Amman sentinel appears in a payload."""
    text = json.dumps(payload)
    return [m.group(0) for m in re.finditer(r"5,?927(?:\.77)?", text)]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Published
# ═════════════════════════════════════════════════════════════════════════════
def test_01_a_published_truth_is_the_number_on_the_line(monkeypatch):
    published(monkeypatch)
    out = _outcomes(monkeypatch)
    line = out["meta_ads_line"]
    assert line["label"] == "Meta Ads (spend)"
    assert line["state"] == "verified" and line["connected"] is True
    assert line["spend"] == {"value": AMMAN_USD, "status": "published", "reason": None,
                             "currency": "USD", "source": "meta_ads"}
    # Two currencies, but only the USD account spent: the truth publishes a
    # USD native total (a zero in GBP does not mix currencies), and the line
    # republishes it as-is. Each account is also shown in its own currency.
    assert line["mixed_currencies"] is True
    assert line["native_spend"] == {"value": AMMAN_USD, "status": "published",
                                    "reason": None, "currency": "USD"}
    by_id = {a["account_id"]: a for a in line["accounts"]}
    assert by_id[AMMAN]["native_spend"] == {"value": AMMAN_USD, "status": "published",
                                            "reason": None, "currency": "USD"}
    assert by_id[LA]["native_spend"]["value"] == 0.0
    assert by_id[LA]["native_spend"]["currency"] == "GBP"
    # Each account's own verified-through day and zone; the zones disclosed.
    assert (by_id[AMMAN]["verified_through"], by_id[AMMAN]["time_zone"]) == (
        "2026-11-21", "Asia/Amman")
    assert (by_id[LA]["verified_through"], by_id[LA]["time_zone"]) == (
        "2026-11-21", "America/Los_Angeles")
    assert line["mixed_time_zones"] is True
    assert "Asia/Amman" in line["day_basis_disclosure"]
    assert "America/Los_Angeles" in line["day_basis_disclosure"]
    assert "not the Europe/London days" in line["day_basis_disclosure"]
    assert line["in_channel_partition"] is False and line["added_to_totals"] is False
    # The totals' spend is still "no spend covers every channel".
    assert out["totals"]["spend"]["reason"] == "all_channel_spend_not_connected"
    meta_source = next(s for s in out["spend_sources"] if s["platform"] == "meta")
    assert meta_source["connected"] is True
    assert meta_source["shown_on"] == "__meta_ads_spend__"


def test_02_the_page_window_maps_to_the_same_settled_dates_in_every_account(monkeypatch):
    """The truth is asked for the page's settled dates — the evidence window's
    start through London's yesterday — with ``settled=True``; nothing else."""
    import services.meta_ads_spend_truth_service as truth_service
    published(monkeypatch)
    asked = []
    real = truth_service.build_meta_ads_spend_truth_for_days

    def spy(start, end, **kw):
        asked.append((start, end, kw))
        return real(start, end, **kw)

    monkeypatch.setattr(truth_service, "build_meta_ads_spend_truth_for_days", spy)
    out = _outcomes(monkeypatch, window="30d")
    assert asked == [(date(2026, 10, 24), YESTERDAY, {"settled": True, "now": NOW})]
    assert out["meta_ads_line"]["start_date"] == out["window"]["start_date"] == "2026-10-24"
    assert out["meta_ads_line"]["end_date"] == out["window"]["end_date"] == "2026-11-21"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Not published: withheld means absent
# ═════════════════════════════════════════════════════════════════════════════
def _state_store(monkeypatch, case):
    if case == "coverage_incomplete":           # production today
        meta_store(monkeypatch, [amman(), los_angeles()])
    elif case == "not_connected":
        meta_store(monkeypatch, [])
    elif case == "source_unavailable":
        meta_store(monkeypatch, [amman(), los_angeles()], available=False)
    elif case == "fx_incomplete":
        meta_store(monkeypatch, [amman(), los_angeles_read_through_21st(
            native=123.45, usd=None, fx_complete=False, last_spend="2026-11-21")])
    elif case == "never_read":
        # Configured, never read: nothing is known about its spend.
        meta_store(monkeypatch, [amman()], configured=[AMMAN, LA])
    else:
        raise AssertionError(case)


@pytest.mark.parametrize("case,status,reason", [
    ("coverage_incomplete", "withheld", "meta_ads_spend_coverage_incomplete"),
    ("not_connected", "unavailable", "meta_ads_not_connected"),
    ("source_unavailable", "unavailable", "meta_ads_spend_source_unavailable"),
    ("fx_incomplete", "withheld", "fx_rates_missing"),
    ("never_read", "withheld", "meta_ads_spend_coverage_incomplete"),
])
def test_03_a_total_that_is_not_published_leaves_no_figure_in_the_payload(
        monkeypatch, case, status, reason):
    _state_store(monkeypatch, case)
    out = _outcomes(monkeypatch)
    line = out["meta_ads_line"]
    assert line["spend"]["status"] == status and line["spend"]["reason"] == reason
    assert line["spend"]["value"] is None
    # Not the total, not any account's — nowhere in the whole payload.
    assert _no_meta_figure(out) == []
    assert "123.45" not in json.dumps(out)
    for a in line["accounts"]:
        assert a["native_spend"]["value"] is None and a["usd_spend"]["value"] is None
    if case == "coverage_incomplete":
        assert line["withheld_detail"] == (
            f"account {LA} is not verified for every day through 2026-11-21 "
            "(verified through 2026-11-20)")
    if case == "not_connected":
        # Meta not set up: the Paid Social row is as it was before.
        assert line["connected"] is False
        assert _row(out, "PAID_SOCIAL")["spend"]["reason"] == "channel_spend_not_connected"
    if case == "source_unavailable":
        # Could not tell: neither "set up" nor "not set up".
        assert line["connected"] is None
        meta_source = next(s for s in out["spend_sources"] if s["platform"] == "meta")
        assert meta_source["connected"] is None


def test_03b_an_empty_settled_window_reads_no_truth_and_publishes_nothing(monkeypatch):
    from services.meta_ads_spend_line_service import build_meta_ads_spend_line
    import services.meta_ads_spend_truth_service as truth_service
    monkeypatch.setattr(truth_service, "build_meta_ads_spend_truth_for_days",
                        lambda *a, **k: pytest.fail("no truth for an empty window"))
    line = build_meta_ads_spend_line(date(2026, 10, 1), date(2026, 9, 30), now=NOW,
                                     empty_reason="no_settled_day_in_window")
    assert line["spend"] == {"value": None, "status": "unavailable",
                             "reason": "no_settled_day_in_window",
                             "currency": "USD", "source": "meta_ads"}
    assert line["accounts"] == [] and line["connected"] is None


def test_03c_a_truth_that_raises_fails_closed(monkeypatch):
    import services.meta_ads_spend_truth_service as truth_service

    def boom(*a, **k):
        raise RuntimeError("database gone")
    monkeypatch.setattr(truth_service, "build_meta_ads_spend_truth_for_days", boom)
    from services.meta_ads_spend_line_service import build_meta_ads_spend_line
    line = build_meta_ads_spend_line(None, YESTERDAY, now=NOW)
    assert line["spend"]["status"] == "unavailable"
    assert line["spend"]["reason"] == "meta_ads_spend_line_failed"
    assert line["connected"] is None


def test_03d_fx_incomplete_with_one_currency_publishes_the_native_total_only(monkeypatch):
    """The truth publishes a native total when USD cannot be had and every
    account shares a currency; the line republishes exactly that."""
    meta_store(monkeypatch, [los_angeles_read_through_21st(
        native=123.45, usd=None, fx_complete=False, last_spend="2026-11-21")])
    line = _outcomes(monkeypatch)["meta_ads_line"]
    assert line["state"] == "fx_incomplete"
    assert line["spend"]["value"] is None and line["spend"]["reason"] == "fx_rates_missing"
    assert line["native_spend"] == {"value": 123.45, "status": "published",
                                    "reason": None, "currency": "GBP"}
    # No account figure beside a total that is not published in USD.
    assert all(a["native_spend"]["value"] is None for a in line["accounts"])


# ═════════════════════════════════════════════════════════════════════════════
# 3. The owner's retirement declaration
# ═════════════════════════════════════════════════════════════════════════════
def _retired(monkeypatch):
    """Amman stopped being read; the owner declares its last spend day
    2026-06-30. The 30-day window starts after it: the figure rests on it."""
    meta_store(monkeypatch, [
        amman(chunks=[_chunk("2026-04-20", "2026-06-30", "2026-07-01T06:02:00+00:00")],
              last_spend="2026-06-30"),
        los_angeles_read_through_21st()],
        configured=[LA], retired=f"{AMMAN}:2026-06-30")


def test_04_the_retirement_disclosure_is_on_the_line_whenever_its_marker_is(monkeypatch):
    _retired(monkeypatch)
    line = _outcomes(monkeypatch, window="30d")["meta_ads_line"]
    assert line["spend"]["status"] == "published" and line["spend"]["value"] == 0.0
    assert line["owner_declared_retired"] == [
        {"account_id": AMMAN, "last_spend_day": "2026-06-30", "relied_from": "2026-07-01"}]
    assert line["owner_declared_retired_disclosure"] == (
        f"This total assumes ad account {AMMAN} spent nothing from 2026-07-01 on "
        "(declared last spend day 2026-06-30) — the owner's declaration "
        "(META_AD_ACCOUNT_RETIRED), not a reading from Meta.")


def test_04b_a_marker_without_its_disclosure_withholds_the_figure():
    from services.meta_ads_spend_line_service import line_from_truth
    truth = {"state": "verified", "usd_spend": 10.0, "native_spend": 10.0,
             "native_currency": "USD", "accounts": [],
             "owner_declared_retired": [{"account_id": AMMAN, "last_spend_day": "2026-06-30",
                                         "relied_from": "2026-07-01"}],
             "owner_declared_retired_disclosure": None}
    line = line_from_truth(truth)
    assert line["spend"]["value"] is None
    assert line["spend"]["reason"] == "meta_retired_disclosure_missing"
    assert line["native_spend"]["value"] is None
    # With the disclosure the same truth publishes.
    ok = line_from_truth({**truth, "owner_declared_retired_disclosure": "This total assumes …"})
    assert ok["spend"]["value"] == 10.0


# ═════════════════════════════════════════════════════════════════════════════
# 4. Paid Social by platform
# ═════════════════════════════════════════════════════════════════════════════
def _contact_row(i, source, drill, *, lifecycle="lead"):
    return {"funnel_row_id": i, "contact_id": str(i),
            "created_at": "2026-11-01T10:00:00+00:00", "lifecycle_stage": lifecycle,
            "hs_analytics_source": source, "hs_analytics_source_data_1": drill}


PAID_SOCIAL_ROWS = [
    _contact_row(1, "PAID_SOCIAL", "facebook"),
    _contact_row(2, "PAID_SOCIAL", "facebook", lifecycle="opportunity"),
    _contact_row(3, "PAID_SOCIAL", "instagram"),
    _contact_row(4, "PAID_SOCIAL", "linkedin", lifecycle="salesqualifiedlead"),
    _contact_row(5, "PAID_SOCIAL", "LinkedIn "),            # trimmed, lower case
    _contact_row(6, "PAID_SOCIAL", None),
    _contact_row(7, "PAID_SOCIAL", "  "),
    _contact_row(8, "PAID_SOCIAL", "facebook ads"),         # not a stored value we know
    _contact_row(9, "ORGANIC_SEARCH", "google"),
]


def _split(rows=PAID_SOCIAL_ROWS):
    from services.channel_funnel_service import build_funnel
    funnel = build_funnel(rows)
    ch = next(c for c in funnel["channels"] if c["source"] == "PAID_SOCIAL")
    return funnel, ch, {p["platform"]: p for p in ch["platform_split"]}


def test_05_paid_social_is_split_by_drill_down_one_exactly_as_stored():
    from services.channel_funnel_service import reconcile
    funnel, ch, split = _split()
    assert [p["platform"] for p in ch["platform_split"]] == ["meta", "linkedin", "other", "unknown"]
    assert split["meta"]["contacts"] == 3
    assert split["meta"]["drilldown_values"] == ["facebook", "instagram"]
    assert split["linkedin"]["contacts"] == 2
    assert split["other"]["contacts"] == 1
    assert split["other"]["drilldown_values"] == ["facebook ads"]
    assert split["unknown"]["contacts"] == 2
    # The same funnel columns, and they add back up to the channel.
    assert split["meta"]["reached_opportunity"] == 1
    assert split["linkedin"]["reached_sql"] == 1
    for key in ("contacts", "reached_mql", "reached_sql", "reached_opportunity",
                "reached_customer_stage"):
        assert sum(p[key] for p in ch["platform_split"]) == ch[key]
    assert reconcile(funnel) == []
    # Only Paid Social is split.
    assert "platform_split" not in next(c for c in funnel["channels"]
                                        if c["source"] == "ORGANIC_SEARCH")


def test_05b_linkedin_is_never_counted_under_meta_and_a_missing_drilldown_is_unknown():
    from services.channel_funnel_service import platform_key
    assert platform_key({"hs_analytics_source_data_1": "linkedin"}) == "linkedin"
    assert platform_key({"hs_analytics_source_data_1": None}) == "unknown"
    assert platform_key({"hs_analytics_source_data_1": ""}) == "unknown"
    assert platform_key({}) == "unknown"
    # A substring is not a match: the classifier's "meta"/"facebook" substring
    # rule would call these Meta.
    for v in ("facebook ads", "metadata", "linkedin-facebook", "fb"):
        assert platform_key({"hs_analytics_source_data_1": v}) == "other", v


def test_05c_mutation_a_split_that_guesses_meta_for_a_missing_drilldown_is_caught(monkeypatch):
    import services.channel_funnel_service as cfs
    real = cfs.platform_key
    monkeypatch.setattr(cfs, "platform_key", lambda row: (
        "meta" if real(row) == "unknown" else real(row)))
    _f, _ch, split = _split()
    with pytest.raises(AssertionError):
        assert split["unknown"]["contacts"] == 2 and split["meta"]["contacts"] == 3


def test_05d_a_split_that_does_not_add_up_is_a_reconciliation_problem():
    from services.channel_funnel_service import reconcile
    funnel, ch, _split_rows = _split()
    ch["platform_split"][0]["contacts"] += 1
    assert "platforms do not sum to PAID_SOCIAL for contacts" in reconcile(funnel)


# ═════════════════════════════════════════════════════════════════════════════
# 5. No ratio between Meta spend and HubSpot counts
# ═════════════════════════════════════════════════════════════════════════════
def test_06_the_meta_line_carries_no_hubspot_count_and_no_ratio(monkeypatch):
    from scripts.audit_customer_revenue_reader_cutover import (
        meta_line_violations, ratio_name_violations)
    published(monkeypatch)
    out = _outcomes(monkeypatch)
    line = out["meta_ads_line"]
    assert meta_line_violations(line) == []
    assert line["compared_with_outcomes"] is False
    assert line["comparison"] == {"value": None, "status": "unavailable",
                                  "reason": "channel_population_is_not_meta_spend_population"}
    for k in cwt.PERIOD_RATIO_KEYS + ("closed_won_deals", "customers", "won_revenue",
                                      "contacts", "reached_sql", "metrics"):
        assert k not in line
    # Check 18 over the whole outcomes payload still holds.
    assert ratio_name_violations(out) == []
    # The Paid Social row: its spend points at the Meta line; its ratios are a
    # rate across two populations.
    ps = _row(out, "PAID_SOCIAL")
    assert ps["spend"] == {"value": None, "status": "unavailable",
                           "reason": "spend_shown_on_meta_ads_line"}
    for k in cwt.PERIOD_RATIO_KEYS:
        assert ps[k] is None
        assert ps["metrics"][k]["reason"] == "channel_population_is_not_meta_spend_population"


def test_06b_mutation_a_cost_per_contact_on_the_line_is_caught():
    from scripts.audit_customer_revenue_reader_cutover import meta_line_violations
    from services.meta_ads_spend_line_service import line_from_truth
    line = line_from_truth({"state": "verified", "usd_spend": 100.0, "accounts": []})
    assert meta_line_violations(line) == []
    for bad in ({"cost_per_contact": 2.0}, {"paid_social_contacts": 50},
                {"spend_per_sql": 9.0}, {"closed_won_deals": 1}):
        assert meta_line_violations({**line, **bad}), bad
    assert meta_line_violations({**line, "comparison": {"value": 2.0, "status": "published"}})


def test_06c_a_withheld_line_with_a_figure_is_caught():
    from scripts.audit_customer_revenue_reader_cutover import meta_line_violations
    from services.meta_ads_spend_line_service import line_from_truth
    line = line_from_truth({"state": "coverage_incomplete", "accounts": [
        {"account_id": AMMAN, "native_spend": AMMAN_USD, "usd_spend": AMMAN_USD}]})
    assert meta_line_violations(line) == []
    leaked = json.loads(json.dumps(line))
    leaked["accounts"][0]["usd_spend"]["value"] = AMMAN_USD
    assert meta_line_violations(leaked)


# ═════════════════════════════════════════════════════════════════════════════
# 6. The page, evaluated in node
# ═════════════════════════════════════════════════════════════════════════════
def _render_channels(payload, mutate=None):
    from tests.test_pr_ads_168a_channel_outcomes import _render
    return _render(payload, mutate=mutate)


def _page(monkeypatch, out, funnel_channels=None):
    from tests.test_pr_ads_168a_channel_outcomes import _funnel
    funnel = _funnel()
    if funnel_channels is not None:
        funnel["channels"] = funnel_channels
    return {**funnel, "outcomes": out}


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _tr(html, label):
    table = html[html.index("<tbody>"):html.index("</tbody>")]
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", table, re.S):
        if f'<span class="channels-name">{label}<' in tr:
            return tr
    raise AssertionError(label)


def _meta_card(html):
    """PR-ADS-181A: the Meta Ads card of the paid channels block (it replaces
    the strip cell and the "Meta Ads (spend)" table line)."""
    i = html.index("channels-paid__card--meta")
    return html[i:html.index("</section>", i)]


def _drawer(payload, key):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app("channelDrawerHtml(%s, %s)" % (json.dumps(payload), json.dumps(key)))


def test_07_published_the_meta_row_shows_the_spend_and_each_account(monkeypatch):
    published(monkeypatch)
    html = _render_channels(_page(monkeypatch, _outcomes(monkeypatch)))
    card = _text(_meta_card(html))
    assert "Spend $5.9k" in card
    assert (f"Ad account {AMMAN} (Logistaas) · USD · Asia/Amman days · verified "
            "through 2026-11-21 · 5,927.77 USD") in card
    assert (f"Ad account {LA} (Logistaas Limited) · GBP · America/Los_Angeles days · "
            "verified through 2026-11-21 · 0.00 GBP") in card
    assert "The same date is a different 24 hours in each account" in card
    assert "Not yet matched to HubSpot deals, so there is no revenue ratio" in card
    assert "Not matched to HubSpot's Paid Social contacts or deals — no rate is calculated" in card
    # The Google Ads card keeps its own spend beside it.
    assert "channels-paid__card--google" in html


def test_07b_withheld_the_page_shows_the_word_and_why_and_no_number(monkeypatch):
    _state_store(monkeypatch, "coverage_incomplete")
    html = _render_channels(_page(monkeypatch, _outcomes(monkeypatch)))
    card = _text(_meta_card(html))
    assert ("Spend Withheld some ad account's spend is not verified and settled for every day"
            in card)
    assert f"account {LA} is not verified for every day through 2026-11-21" in card
    assert _no_meta_figure(html) == []
    assert "$5" not in card


def test_07c_the_retired_disclosure_is_beside_the_figure_on_the_page(monkeypatch):
    _retired(monkeypatch)
    out = _outcomes(monkeypatch, window="30d")
    disclosure = out["meta_ads_line"]["owner_declared_retired_disclosure"]
    html = _render_channels(_page(monkeypatch, out))
    card = _meta_card(html)
    assert disclosure in _text(card)
    # Beside the figure, in view — never behind "Account details".
    shown = card[:card.index('data-channels-panel="meta"')] if 'data-channels-panel="meta"' in card else card
    assert disclosure in _text(shown)
    # A payload with the marker and no disclosure shows no figure.
    stripped = json.loads(json.dumps(out))
    stripped["meta_ads_line"]["owner_declared_retired_disclosure"] = None
    bare = _render_channels(_page(monkeypatch, stripped))
    assert ("Withheld the figure rests on the owner's retirement declaration and its "
            "disclosure is missing") in _text(_meta_card(bare))
    # Counterfactual: a gate that ignores the marker shows the bare figure.
    leaky = _render_channels(_page(monkeypatch, stripped), mutate={
        "if (metaLineRetired(l) && !l.owner_declared_retired_disclosure) {\n    return { value: null":
        "if (false) {\n    return { value: null"})
    assert "$0" in _text(_meta_card(leaky))


def test_07d_paid_social_platforms_render_with_their_contacts_in_its_drawer(monkeypatch):
    from services.channel_funnel_service import build_funnel
    published(monkeypatch)
    out = _outcomes(monkeypatch)
    channels = json.loads(json.dumps(build_funnel(PAID_SOCIAL_ROWS)["channels"]))
    page = _page(monkeypatch, out, funnel_channels=channels)
    drawer = _text(_drawer(page, "PAID_SOCIAL"))
    block = drawer[drawer.index("By platform"):]
    assert "Meta (facebook + instagram) drill-down: facebook, instagram 3" in block
    assert "LinkedIn drill-down: linkedin 2" in block
    assert "Drill-down not recorded (unknown) no drill-down recorded — never counted as Meta 2" in block
    # Nothing on a platform line puts Meta spend beside its contacts.
    assert "$" not in block and "%" not in block


def test_08_no_column_or_cell_reads_as_a_cost_per(monkeypatch):
    published(monkeypatch)
    html = _render_channels(_page(monkeypatch, _outcomes(monkeypatch)))
    head = html[html.index("<thead>"):html.index("</thead>")]
    for bad in ("per lead", "per contact", "per SQL", "Cost per", "CPL", "/ contacts"):
        assert bad.lower() not in _text(head).lower()
    assert not re.search(r"(?i)meta[^\n]{0,80}(?:per (?:lead|contact|sql)|/\s*(?:contacts|leads|sqls))",
                         APP_JS)
    card = _meta_card(html)
    assert "%" not in card
    for bad in ("per lead", "per contact", "per SQL", "per customer", "/ ad spend"):
        assert bad.lower() not in _text(card).lower(), bad


# ═════════════════════════════════════════════════════════════════════════════
# 7. The Dashboard's Channels tab — retired (PR-ADS-181A)
# ═════════════════════════════════════════════════════════════════════════════
# test_09, 09b, 09c and 09d stood here. They built /api/dashboard/channels
# (build_dashboard_channels) over business windows and rendered its Meta panel
# (renderChanMetaPanel). The owner retired the tab; its route, builder and
# renderer are deleted, and the reader-cutover audit fails if one returns. The
# rules they held on the tab hold on the Channels page, the line's one page:
# the line's settled days and no figure under a withheld total (01, 02, 03,
# 07b), the retirement disclosure (04, 07c), no rate over Meta spend (06, 08).


# ═════════════════════════════════════════════════════════════════════════════
# 8. The audit (checks 19 and 20) — passing now, failing on each mutation
# ═════════════════════════════════════════════════════════════════════════════
def _structural(js=APP_JS):
    from scripts.audit_customer_revenue_reader_cutover import Audit, meta_structural_checks
    a = Audit()
    meta_structural_checks(a, js)
    return a


def test_10_checks_19_and_20_hold_on_this_tree():
    a = _structural()
    assert a.violations == []
    assert a.checks == {"meta_spend_disclosure_carried": True,
                        "no_rate_over_meta_spend": True}


@pytest.mark.parametrize("old,new", [
    # The gate stops looking at the declaration.
    ("if (metaLineRetired(l) && !l.owner_declared_retired_disclosure) {\n    return { value: null, status: \"withheld\", reason: \"meta_retired_disclosure_missing\" };\n  }\n",
     ""),
    # A renderer reads the figure around the gate.
    ("const usd = canonicalMetricHtml(metaLineSpendMetric(line), channelMoney, opts);",
     "const usd = canonicalMetricHtml(line.spend, channelMoney, opts); const _m = metaLine.spend;"),
    # PR-ADS-181A: the Meta card stops going through the gate (it replaces
    # the retired Dashboard panel's case).
    ("    : canonicalMetricHtml(metaLineSpendMetric(null), channelMoney, opts);",
     "    : canonicalMetricHtml((o || {}).spend, channelMoney, opts);"),
    # A cost per contact label appears.
    ('function channelPaidFigure(label, html) {',
     'function channelPaidFigure(label, html) { const metaCost = "Meta spend per contact";'),
])
def test_10b_each_guard_fails_on_its_mutation(old, new):
    assert old in APP_JS, old
    a = _structural(APP_JS.replace(old, new))
    assert a.violations, (old, new)


def test_10c_a_new_module_reading_the_meta_truth_must_be_registered(monkeypatch):
    """The rogue module exists only in the audit's view of the tree (its file
    list and its reader), never on disk: a killed run leaves nothing behind."""
    import scripts.audit_customer_revenue_reader_cutover as audit
    rel = "services/_pr_ads_171b_rogue_probe.py"
    source = ("from services.meta_ads_spend_truth_service import "
              "build_meta_ads_spend_truth_for_days\n"
              "def total():\n    return build_meta_ads_spend_truth_for_days(None, None)['usd_spend']\n")
    real_files, real_text = audit._production_py, audit._text
    monkeypatch.setattr(audit, "_production_py", lambda: real_files() + [rel])
    monkeypatch.setattr(audit, "_text", lambda r: source if r == rel else real_text(r))
    a = _structural()
    assert any("_pr_ads_171b_rogue_probe.py reads the Meta spend truth" in v
               for v in a.violations)
    assert not (ROOT / rel).exists()


def test_10d_a_registered_reader_that_drops_the_disclosure_fails(monkeypatch):
    import scripts.audit_customer_revenue_reader_cutover as audit
    real = audit._string_constants
    monkeypatch.setattr(audit, "_string_constants", lambda rel: (
        real(rel) - {"owner_declared_retired_disclosure"}
        if rel == "services/meta_ads_spend_line_service.py" else real(rel)))
    a = _structural()
    assert any("meta_ads_spend_line_service.py reads the Meta spend truth without "
               "carrying owner_declared_retired_disclosure" in v for v in a.violations)


# ═════════════════════════════════════════════════════════════════════════════
# 9. Registration
# ═════════════════════════════════════════════════════════════════════════════
def test_11_every_new_reason_has_a_label_in_the_page():
    from services import meta_ads_spend_line_service as line_service
    labels = APP_JS[APP_JS.index("const CANONICAL_REASON_LABELS = {"):]
    labels = labels[:labels.index("};")]
    for reason in line_service.REASONS + (cwt.R_SPEND_ON_META_ADS_LINE,
                                           cwt.R_CHANNEL_POPULATION_NOT_META_SPEND,
                                           "no_settled_day_in_window"):
        assert re.search(rf"\b{reason}:", labels), reason


def test_12_new_api_strings_name_no_roas_or_cac():
    from scripts.audit_customer_revenue_reader_cutover import _names_ratio_in_text
    from services import channel_funnel_service as cfs
    from services import meta_ads_spend_line_service as line_service
    from services import source_attribution_service as sas
    for text in (line_service.NOTE, cfs.PLATFORM_NOTE, cfs.PLATFORM_RULE,
                 sas.CHANNEL_OUTCOME_NOTES["meta_spend"],
                 *cfs.PLATFORM_LABELS.values()):
        assert not _names_ratio_in_text(text), text


def test_13_the_line_is_read_only():
    src = (ROOT / "services" / "meta_ads_spend_line_service.py").read_text(encoding="utf-8")
    for forbidden in ("connectors.meta_ads", "db.writers", "requests", "INSERT",
                      "UPDATE ", "DELETE"):
        assert forbidden not in src


# ═════════════════════════════════════════════════════════════════════════════
# 10. Review round 1
# ═════════════════════════════════════════════════════════════════════════════
def test_14_the_meta_card_says_each_account_is_read_on_its_own_days(monkeypatch):
    published(monkeypatch)
    card = _text(_meta_card(_render_channels(_page(monkeypatch, _outcomes(monkeypatch)))))
    assert "Each ad account is read on its own calendar days" in card
    assert "Asia/Amman" in card and "America/Los_Angeles" in card
    assert "not the Europe/London days" in card


def test_15_could_not_tell_is_never_worded_as_not_connected(monkeypatch):
    """PR-ADS-181A: ported from the retired Dashboard tab to the Channels page.
    A Meta store that could not be read says so — never "not connected", never
    "not set up". (The tab's first-day-of-quarter case was a BUSINESS window
    with no settled day; the Channels page's evidence windows always hold
    settled days, and test_03b holds an empty settled window on the line.)"""
    meta_store(monkeypatch, [amman(), los_angeles()], available=False)
    out = _outcomes(monkeypatch)
    assert out["meta_ads_line"]["connected"] is None
    html = _render_channels(_page(monkeypatch, out))
    # Everything the page says about Meta: its card (the Google Ads notes'
    # "LinkedIn and ChatGPT spend are not connected" is about other sources).
    card = _text(_meta_card(html)).lower()
    assert "not connected" not in card and "not set up" not in card
    assert "spend unavailable stored meta spend could not be read" in card
    assert not re.search(r"meta[^.]{0,60}not (?:connected|set up)", _text(html).lower())
    assert _no_meta_figure(html) == []


def test_15b_the_footer_dot_tells_not_set_up_from_unavailable():
    dots = APP_JS[APP_JS.index("const DASH_STATUS_DOT = {"):]
    dots = dots[:dots.index("};")]
    not_connected = re.search(r'not_connected:\s*"([^"]+)"', dots).group(1)
    unavailable = re.search(r'unavailable:\s*"([^"]+)"', dots).group(1)
    assert not_connected != unavailable
    css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
    assert f".{not_connected} " in css


@pytest.mark.parametrize("label", [
    "Meta spend / lead", "Meta spend / contact", "Meta spend / SQL",
    "Meta spend per lead", "Meta CPL", "Meta cost per lead", "meta COST-PER contact"])
def test_16_check_20_catches_singular_and_abbreviated_rate_labels(label):
    old = 'function channelPaidFigure(label, html) {'
    assert old in APP_JS
    a = _structural(APP_JS.replace(
        old, f'function channelPaidFigure(label, html) {{ const x = "{label}";'))
    assert any(v.startswith("no_rate_over_meta_spend") for v in a.violations), label
