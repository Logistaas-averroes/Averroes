"""PR-ADS-177C-F1 — truth audit round 1 on PR-ADS-177C (no database).

MAJOR M-1. Production has a shared name: "low cpc - europe" is two Google Ads
campaign ids (22536989505, 23054708812). 177C flagged such a row in the API
(``identity_status = campaign_name_shared_by_google_ads_campaigns``) but no
page printed the flag: the Campaigns table showed the combined row as one
campaign, its badge said "Spend mapping required" (the wrong reason), and
Dashboard → Campaigns summed both ids under one name with no word about it.
Owner decision (label, don't withhold): the figures are the name's true
combined figures, so they stay — labelled, beside the name and the period
ratio, with the ids in the drawer; the badge says why the row is not judged.

Also here: the certification's S4 now requires those labels (a renderer that
drops one goes red); the stored run row records how many drawers L6 probed;
the Windsor backfill no longer swallows the deleted ``write_keywords``; the
CLAUDE.md / API-contract references to the retired gate and routes.

Every case fails on 2cfd095 (``docs/60`` §9.11).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
SHARED = "campaign_name_shared_by_google_ads_campaigns"
IDS = ["22536989505", "23054708812"]


def _run_app(script: str, js: str | None = None):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app as run
    return run(script, js=js)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _shared(row: dict) -> dict:
    return {**row, "google_ads_campaign_ids": IDS, "identity_status": SHARED,
            "identity_reason": "2 Google Ads campaigns share this name; …",
            "verdict": "mapping_required", "verdict_reason": SHARED}


def _single(row: dict) -> dict:
    return {**row, "google_ads_campaign_ids": ["111"], "identity_status": "single_campaign"}


def _page_rows():
    from tests.test_pr_ads_177b_campaigns_one_page import _COHORT, _WINDOW, _row
    shared = _shared(_row("low cpc - europe", 7985.0, revenue=24000.0, deal_ids=("d-1", "d-2"),
                          deals=2, verdict="winner"))
    single = _single(_row("brand - uk", 1200.0, revenue=3000.0))
    data = {"cohort": _COHORT, "window": _WINDOW, "rows": [shared, single],
            "readiness": {"spend_decision_ready": True}}
    return shared, single, data


# ── M-1: the Campaigns page ─────────────────────────────────────────────────
def test_01_the_campaigns_table_labels_a_shared_name_and_nothing_else():
    shared, single, data = _page_rows()
    html = _run_app("renderCampaignsPageTable(%s, %s)"
                    % (json.dumps([shared, single]), json.dumps(data)))
    rows = re.findall(r'<tr class="camp-row".*?</tr>', html, flags=re.S)
    assert len(rows) == 2
    shared_txt, single_txt = _text(rows[0]), _text(rows[1])
    assert "2 Google Ads campaigns share this name — figures combined" in shared_txt
    # the period ratio carries the same combined label
    assert "combined across the campaigns sharing this name" in shared_txt
    # its own badge, never "Spend mapping required"
    assert "Not judged — shared name" in shared_txt
    assert "Spend mapping required" not in rows[0]
    badge = re.search(r'<span class="roas-verdict-badge[^"]*" title="([^"]*)"', rows[0])
    assert badge and "share this name" in badge.group(1)                 # tooltip
    # the figures stay (label, don't withhold)
    assert "$24.0k" in shared_txt
    assert "share this name" not in single_txt and "combined" not in single_txt


def test_02_the_campaigns_drawer_lists_the_campaign_ids():
    shared, single, data = _page_rows()
    html = _run_app("renderCampaignsPageDrawer(%s, %s)" % (json.dumps(shared), json.dumps(data)))
    txt = _text(html)
    assert "2 Google Ads campaigns share this name — figures combined" in txt
    for cid in IDS:
        assert cid in txt
    assert "cannot be split by campaign" in txt
    plain = _run_app("renderCampaignsPageDrawer(%s, %s)" % (json.dumps(single), json.dumps(data)))
    assert "share this name" not in plain


# ── M-1: Dashboard → Campaigns ──────────────────────────────────────────────
def _dash_row(name, *, shared):
    row = {"campaign_id": name, "campaign_name": name, "spend_native": 6500.0,
           "native_currency": "GBP", "spend_usd": 7985.0, "fx_complete": True,
           "leads": 9, "sqls": 3, "sqls_status": "published", "closed_won_deals": 2,
           "customers": 2, "won_revenue_usd": 24000.0,
           "metrics": {"closed_won_deals": {"value": 2, "status": "published"},
                       "customers": {"value": 2, "status": "published"},
                       "revenue_usd": {"value": 24000.0, "status": "published"}},
           "period_revenue_per_ad_spend": 3.01, "period_ratio_available": True,
           "status": "Mapping needs review" if shared else "Revenue proven",
           "deal_ids": ["d-1", "d-2"], "deals": [], "target_page": "campaigns"}
    if shared:
        row.update(google_ads_campaign_ids=IDS, identity_status=SHARED)
    else:
        row.update(google_ads_campaign_ids=["111"], identity_status="single_campaign")
    return row


def test_03_the_dashboard_leaderboard_labels_the_shared_row():
    from tests.test_pr_ads_177b_campaigns_one_page import _COHORT
    d = {"campaigns": [_dash_row("low cpc - europe", shared=True),
                       _dash_row("brand - uk", shared=False)],
         "cohort": _COHORT, "kpis": {}, "residual": None}
    html = _run_app("renderCampLeaderboard(%s)" % json.dumps(d))
    rows = re.findall(r'<tr class="camp-row".*?</tr>', html, flags=re.S)
    shared_txt, single_txt = _text(rows[0]), _text(rows[1])
    assert "2 Google Ads campaigns share this name — figures combined" in shared_txt
    assert "combined across the campaigns sharing this name" in shared_txt
    assert "Not judged — several campaigns share this name" in shared_txt
    assert "Map campaign spend" not in shared_txt          # not a spend-mapping gap
    assert "share this name" not in single_txt


def test_04_the_certification_requires_the_labels_on_every_surface():
    """S4's twin: drop any label from any surface and the gate goes red."""
    from scripts import audit_campaigns_page_certification as cert
    js = APP_JS.read_text(encoding="utf-8")
    f = cert.Findings()
    cert.check_frontend_gates(f, js=js)
    assert not f.violations, f.violations
    for fn, helpers in cert.JS_SHARED_NAME_SURFACES.items():
        body = cert._js_function(js, fn)
        for helper in helpers:
            mutated = js.replace(body, body.replace(f"{helper}(", "String("))
            g = cert.Findings()
            cert.check_frontend_gates(g, js=mutated)
            assert any(f"{fn} prints a campaign row without {helper}" in v
                       for v in g.violations), (fn, helper)
    badge = cert._js_function(js, "getBusinessVerdictBadge")
    g = cert.Findings()
    cert.check_frontend_gates(g, js=js.replace(badge, badge.replace(
        "CAMPAIGN_SHARED_NAME", "NOT_THE_FLAG")))
    assert any("getBusinessVerdictBadge" in v for v in g.violations)


# ── M-1: Overview's signals choose among distinct campaigns ─────────────────
def test_05_overview_never_ranks_a_shared_row_as_one_campaign():
    """PR-ADS-177C-F2: rewritten. F1's version held trivially — its shared row
    had 2 won deals, so it could never be a waste row. Here the shared row is
    BOTH the top revenue producer's rival and the biggest spender without
    proof: it is passed over when one row is NAMED, and kept in every total."""
    from services.dashboard_overview_service import _build_signals
    rows = [
        {"campaign_name": "low cpc - europe", "identity_status": SHARED,
         "google_ads_campaign_ids": IDS, "won_revenue": 0.0, "closed_won_deals": 0,
         "spend": 7985.0, "sqls": 0},
        {"campaign_name": "brand - uk", "identity_status": "single_campaign",
         "won_revenue": 3000.0, "closed_won_deals": 1, "spend": 1200.0, "sqls": 2},
        {"campaign_name": "gulf - uae", "identity_status": "single_campaign",
         "won_revenue": 0.0, "closed_won_deals": 0, "spend": 400.0, "sqls": 0},
    ]
    s = _build_signals(rows, [], [], {"sqls": 2}, {"fx_status": "verified"}, {})
    assert s["best_campaign"]["label"] == "brand - uk"
    w = s["biggest_waste"]
    assert w["top_campaign"] == "gulf - uae"            # named: the distinct one
    assert w["shared_name_rows_excluded"] == 1
    assert w["campaign_count"] == 2                     # counted: both
    assert w["value"] == 8385.0                         # 7985 + 400, never 400
    assert w["shared_name_rows_included"] == 1
    line = _run_app("dashSignalBody('biggest_waste', %s).line" % json.dumps(w))
    assert "includes 1 name several Google Ads campaigns share (figures combined)" in line
    assert "1 shared-name row (several campaigns combined) not ranked" in line


def _only_shared(**over):
    return [{"campaign_name": "low cpc - europe", "identity_status": SHARED,
             "google_ads_campaign_ids": IDS, "spend": 550.0, **over}]


def _cards(rows, sqls=0):
    from services.dashboard_overview_service import _build_decision_cards, _build_signals
    summary = {"sqls": sqls}
    signals = _build_signals(rows, [], [], summary, {"fx_status": "verified"}, {})
    cards = _build_decision_cards(rows, summary, {"canonical_revenue_available": True},
                                  {"fx_status": "verified",
                                   "campaign_spend_status": "verified"}, signals, [])
    return signals, {c["type"]: c for c in cards}


def test_05b_f2_the_only_producer_is_a_shared_row():
    """(a) The shared row is the window's only producer. F1 filtered it out
    before counting: `producing` was non-empty, `best` None, and the Scale
    card said "No verified won-deal producers yet"."""
    signals, cards = _cards(_only_shared(won_revenue=24000.0, closed_won_deals=2, sqls=3),
                            sqls=3)
    best = signals["best_campaign"]
    assert best and best["label"] == "low cpc - europe"
    assert best["shared_name_label"] == ("2 Google Ads campaigns share this name — "
                                         "figures combined")
    scale = cards["scale"]
    assert "No verified" not in scale["headline"]
    # Round 3 (carried minor 2): the headline counts NAMES when one is shared.
    assert scale["headline"] == ("1 campaign name producing won deals "
                                 "(1 shared by several Google Ads campaigns)")
    assert "low cpc - europe (2 Google Ads campaigns share this name — figures combined)" \
        in scale["body"]
    html = _run_app("dashSignalBody('best_campaign', %s)" % json.dumps(best))
    assert "share this name — figures combined" in html["line"]


def test_05c_f2_the_only_spend_without_proof_is_a_shared_row():
    """(b) The shared row spent with 0 SQLs and 0 won deals. F1 dropped it:
    the waste signal was None and the page said "No signal"."""
    signals, cards = _cards(_only_shared(won_revenue=0.0, closed_won_deals=0, sqls=0),
                            sqls=0)
    w = signals["biggest_waste"]
    assert w is not None and w["value"] == 550.0 and w["campaign_count"] == 1
    assert w["top_campaign"] == "low cpc - europe"
    assert "share this name" in w["top_campaign_shared_name_label"]
    line = _run_app("dashSignalBody('biggest_waste', %s).line" % json.dumps(w))
    assert "low cpc - europe (2 Google Ads campaigns share this name — figures combined)" in line
    inv = cards["investigate"]
    assert inv["headline"] == "Paid spend without SQL proof"
    assert "several Google Ads campaigns share — figures combined" in inv["body"]


def test_05d_f2_the_watch_card_labels_a_shared_row():
    signals, cards = _cards(_only_shared(won_revenue=0.0, closed_won_deals=0, sqls=4),
                            sqls=4)
    body = cards["watch"]["body"]
    assert body.startswith("low cpc - europe (2 Google Ads campaigns share this name — "
                           "figures combined) has 4 SQLs")


def test_05e_f2_the_certification_checks_overview_cards_and_totals():
    """L5's Overview check, against the rows Overview is computed from: a
    total that drops a shared row, a "no producers" Scale card over a shared
    producer, or a card naming a shared row unlabelled is a violation."""
    from scripts.audit_campaigns_page_certification import overview_shared_name_problems
    rows = _only_shared(won_revenue=0.0, closed_won_deals=0, sqls=0)
    view = {"rows": rows, "summary": {"sqls": 0}}
    signals, cards = _cards(rows)
    good = {"top_signals": signals, "decision_cards": list(cards.values()),
            "readiness": {"canonical_revenue_available": True}}
    assert overview_shared_name_problems(good, view) == []
    dropped = {**good, "top_signals": {**signals, "biggest_waste": None}}
    assert any("biggest_waste is absent" in p
               for p in overview_shared_name_problems(dropped, view))
    understated = {**good, "top_signals": {**signals, "biggest_waste": {
        **signals["biggest_waste"], "value": 50.0}}}
    assert any("biggest_waste 50.0" in p
               for p in overview_shared_name_problems(understated, view))
    prod = _only_shared(won_revenue=24000.0, closed_won_deals=2, sqls=3)
    psig, pcards = _cards(prod, sqls=3)
    no_prod = {"top_signals": psig, "readiness": {"canonical_revenue_available": True},
               "decision_cards": [{"type": "scale", "headline":
                                   "No verified won-deal producers yet", "body": ""}]}
    assert any("says no producers" in p for p in overview_shared_name_problems(
        no_prod, {"rows": prod, "summary": {"sqls": 3}}))
    unlabelled = {"top_signals": psig, "readiness": {"canonical_revenue_available": True},
                  "decision_cards": [{"type": "watch", "headline": "1 campaign",
                                      "body": "low cpc - europe has 3 SQLs"}]}
    assert any("watch card names a shared name" in p for p in overview_shared_name_problems(
        unlabelled, {"rows": prod, "summary": {"sqls": 3}}))


def test_05f_f2_no_deals_campaign_table_is_left_unlabelled():
    """PR-ADS-177C-F2 labelled Dashboard → Deals' Campaign → Pipeline table.
    Rebased onto PR-ADS-178B, that table no longer exists: Dashboard → Deals
    republishes the Pipeline page (``build_pipeline``), which has no
    per-campaign rows. The guard is that it stays gone — a campaign table that
    comes back must be labelled (and registered in the certification's S4)."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "function renderDealCampaignPipeline(" not in js
    deals = (ROOT / "services" / "dashboard_deals_service.py").read_text(encoding="utf-8")
    assert "_build_campaign_breakdown" not in deals and "campaign_breakdown" not in deals
    from scripts import audit_campaigns_page_certification as cert
    for fn in cert.JS_SHARED_NAME_SURFACES:
        assert f"function {fn}(" in js, fn


def test_05g_r3_the_scale_card_says_a_bigger_shared_row_was_not_named():
    """Carried minor 1 (round 3): the named best campaign is the best SINGLE
    campaign. When a shared row won more, the Scale card says so."""
    rows = _only_shared(won_revenue=24000.0, closed_won_deals=2, sqls=3) + [
        {"campaign_name": "brand - uk", "identity_status": "single_campaign",
         "google_ads_campaign_ids": ["111"], "won_revenue": 3000.0,
         "closed_won_deals": 1, "spend": 1200.0, "sqls": 2}]
    signals, cards = _cards(rows, sqls=5)
    assert signals["best_campaign"]["label"] == "brand - uk"
    body = cards["scale"]["body"]
    assert body.startswith("brand - uk won 1 deal")
    assert ("low cpc - europe (2 Google Ads campaigns share this name — figures combined) "
            "won more ($24.0k), but it is not one campaign, so it is not named the best."
            ) in body
    # A smaller shared row is not mentioned.
    small = _only_shared(won_revenue=500.0, closed_won_deals=1, sqls=3) + rows[1:]
    _, c2 = _cards(small, sqls=5)
    assert "low cpc - europe" not in c2["scale"]["body"]


def test_05h_r3_headlines_count_names_when_one_is_shared():
    """Carried minor 2 (round 3): a shared row is one NAME, not one campaign."""
    rows = _only_shared(won_revenue=24000.0, closed_won_deals=2, sqls=3)
    _, cards = _cards(rows, sqls=3)
    assert cards["scale"]["headline"] == (
        "1 campaign name producing won deals (1 shared by several Google Ads campaigns)")
    watch = _only_shared(won_revenue=0.0, closed_won_deals=0, sqls=4) + [
        {"campaign_name": "brand - uk", "identity_status": "single_campaign",
         "google_ads_campaign_ids": ["111"], "won_revenue": 0.0, "closed_won_deals": 0,
         "spend": 10.0, "sqls": 1}]
    _, wc = _cards(watch, sqls=5)
    assert wc["watch"]["headline"] == ("2 campaign names with SQLs but no won deals yet "
                                       "(1 shared by several Google Ads campaigns)")
    plain = [{"campaign_name": "brand - uk", "identity_status": "single_campaign",
              "google_ads_campaign_ids": ["111"], "won_revenue": 10.0,
              "closed_won_deals": 1, "spend": 10.0, "sqls": 1}]
    _, pc = _cards(plain, sqls=1)
    assert pc["scale"]["headline"] == "1 campaign producing won deals"


def test_05i_r3_the_certification_matches_the_exact_label():
    """Carried minor 3 (round 3): L5 matched any "share" substring, so a label
    with the wrong count — or the word in another sentence — passed."""
    from scripts.audit_campaigns_page_certification import overview_shared_name_problems
    rows = _only_shared(won_revenue=0.0, closed_won_deals=0, sqls=4)
    view = {"rows": rows, "summary": {"sqls": 4}}
    signals, cards = _cards(rows, sqls=4)
    ok = {"top_signals": signals, "decision_cards": list(cards.values()),
          "readiness": {"canonical_revenue_available": True}}
    assert overview_shared_name_problems(ok, view) == []
    for body in ("low cpc - europe (3 Google Ads campaigns share this name — figures "
                 "combined) has 4 SQLs",
                 "low cpc - europe has 4 SQLs; budgets are shared across markets"):
        bad = {**ok, "decision_cards": [{"type": "watch", "headline": "x", "body": body}]}
        assert any("watch card names a shared name" in p
                   for p in overview_shared_name_problems(bad, view)), body
    wrong_sig = {**ok, "top_signals": {**signals, "biggest_waste": None, "best_campaign": {
        "label": "low cpc - europe",
        "shared_name_label": "Several campaigns share this name"}}}
    assert any("best_campaign names" in p
               for p in overview_shared_name_problems(wrong_sig, {**view, "summary": {}}))


# ── minor 2: the stored run row says how many drawers L6 probed ─────────────
def test_06_the_run_row_records_the_drawers_probed():
    from services.platform_certification_service import summarize
    report = {"verdict": "CERTIFIED", "exit_code": 0, "audits": [
        {"key": "campaigns", "outcome": "holds", "report": {"per_window": {
            "ytd": {"window": "ytd", "drawers_probed": 7, "rows_with_deals": 3,
                    "canonical_revenue": "available"},
            "all_time": {"window": "all_time", "available": False}}}},
        {"key": "reader_cutover", "outcome": "holds", "report": {"windows": []}}]}
    out = summarize(report)
    camp = next(a for a in out["audits"] if a["key"] == "campaigns")
    assert camp["drawer_populations"] == {
        "ytd": {"drawers_probed": 7, "rows_with_deals": 3, "canonical_revenue": "available"},
        "all_time": {"drawers_probed": None, "rows_with_deals": None,
                     "canonical_revenue": None}}
    other = next(a for a in out["audits"] if a["key"] == "reader_cutover")
    assert "drawer_populations" not in other
    src = (ROOT / "services" / "platform_certification_service.py").read_text(encoding="utf-8")
    assert "def _drawer_sections" not in src           # the retired drawer's record
    cert = (ROOT / "scripts" / "audit_campaigns_page_certification.py").read_text(
        encoding="utf-8")
    assert "revenue_unavailable" not in cert           # the dead field


# ── minor 3: leftovers ──────────────────────────────────────────────────────
def test_07_the_windsor_backfill_no_longer_has_a_keywords_step():
    from scripts import backfill, backfill_windsor as bw
    assert "keywords" not in bw.SUPPORTED_DATASETS
    assert "keywords" not in bw._PULL_FN and "keywords" not in bw._WRITE_FN
    assert "keywords" not in backfill.GOOGLE_ADS_DATASETS
    for p in (ROOT / "scripts").rglob("*.py"):
        assert "import write_keywords" not in p.read_text(encoding="utf-8"), p


def test_08_no_document_names_the_retired_gate_or_lists_a_retired_route_as_live():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "audit_campaign_evidence_certification" not in claude
    contract = (ROOT / "docs" / "API_CONTRACT.md").read_text(encoding="utf-8")
    for route in ("/api/campaigns", "/api/campaign-detail", "/api/keywords",
                  "/api/search-terms", "/api/search-terms/summary",
                  "/api/search-terms/ngrams"):
        row = re.search(r"^\| GET \| `%s` \|.*$" % re.escape(route), contract, flags=re.M)
        assert row and "Retired (PR-ADS-177C)" in row.group(0), route
    fresh = (ROOT / "scripts" / "audit_keyword_search_term_freshness.py").read_text(
        encoding="utf-8")
    assert "still has a LIVE non-evidence consumer" not in fresh
    thresholds = (ROOT / "config" / "thresholds.yaml").read_text(encoding="utf-8")
    assert "campaign_evidence child imports api.server" not in thresholds
