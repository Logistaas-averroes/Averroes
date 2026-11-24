"""
tests/pr_ads_173a_scenarios.py — the adversarial dataset and the request matrix
PR-ADS-173A's equality proof is taken over.

This is a helper, not a test module. It does two jobs with ONE definition:

  * ``tests/test_pr_ads_173a_search_terms_pages_memory.py`` runs it in a child
    process against the working tree and compares every payload with the
    committed golden file;
  * the golden file was produced by running THIS FILE against the PRE-CHANGE
    tree (the parent commit of PR-ADS-173A)::

        cd <pre-change checkout>
        DATABASE_URL=<throwaway cluster> python <this file> --sorted-reader --out golden.json

    so the expected values are what the old implementation published, not what
    the new one was observed to produce.

It therefore uses only API that exists on both sides of the change: the public
builders, the two route functions that have no builder of their own (the CSV
export and the ``/api/waste`` adapter), and the repository's row-list reader.

``--sorted-reader`` (pre-change tree only). The grouped rows of the old read
arrive in whatever order PostgreSQL's aggregate emits them — there was no
``ORDER BY`` — and a handful of published details follow that order (which of
two same-named campaigns is listed first in a facet; which of two flagged rows
with an equal sort key comes first; the order of a term drawer's campaign
list). The new read is ordered (``search_term, campaign_name, campaign_id``,
byte order, NULLs last). To compare like with like, the flag hands the OLD
code its own rows in that same order. Nothing else about the old code is
touched, and no value is altered — only the order of an unordered result set
is fixed.

The golden file is the record of what the pages published BEFORE PR-ADS-173A.
A later change that alters one of these payloads on purpose regenerates the
file with this script on its own tree and says, in that PR, which fields moved
and why — it does not edit the comparison.

Read-only against every external platform; the only writes are local seeds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

ACCOUNT = "5551234567"

#: A FIXED clock. Every builder is called with it and the two route functions
#: that take no clock are pinned to it, so the golden payloads do not depend on
#: the day the suite runs.
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
D0 = date(2026, 11, 22)
IN_7D = D0 - timedelta(days=2)
NO_FX = D0 - timedelta(days=10)          # the one day with no GBP→USD rate
IN_30D = D0 - timedelta(days=20)
IN_180D = D0 - timedelta(days=100)
OLD = D0 - timedelta(days=300)

CAMP_A, CAMP_B = "23094767501", "23094767502"
#: Two campaign ids sharing ONE display name.
CAMP_C, CAMP_D = "23094767503", "23094767504"
#: An id the canonical spend table does not know, seen under two labels.
CAMP_E = "23094767505"
#: A campaign with approved alias labels.
CAMP_F = "23094767506"
NAMES = {CAMP_A: "uk - brand", CAMP_B: "uae - competitors",
         CAMP_C: "global - shared name", CAMP_D: "global - shared name",
         CAMP_F: "ksa - freight"}

WINDOWS = ("7d", "30d", "180d", "all_time")


# ═════════════════════════════════════════════════════════════════════════════
# The dataset
# ═════════════════════════════════════════════════════════════════════════════

def seed(execute) -> None:
    """Insert the adversarial dataset. ``execute(sql, params)`` runs one
    statement."""

    def term(day, campaign_id, text, *, name=None, flagged=None, category=None,
             micros=2_000_000, currency="GBP", clicks=3, impressions=40,
             conversions=0, ad_group="ag 1", keyword="kw", match_type="BROAD"):
        execute(
            "INSERT INTO search_terms "
            "(source_date, campaign_name, campaign_id, ad_group, keyword, match_type,"
            " search_term, customer_id, spend_usd, clicks, impressions, conversions,"
            " cost_micros, currency_code, source_system, is_flagged_waste,"
            " junk_category, matched_pattern) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'google_ads_api',"
            " %s,%s,%s)",
            (day, name if name is not None else NAMES[campaign_id], campaign_id,
             ad_group, keyword, match_type, text, ACCOUNT,
             (micros or 0) / 1_000_000, clicks, impressions, conversions, micros,
             currency, flagged, category, "pattern" if category else None))

    def annotation(text, campaign_name, category="job_seeker", days_ago=5):
        execute(
            "INSERT INTO waste_terms (run_id, run_date, search_term, campaign_name,"
            " spend_usd, junk_category, matched_pattern, crm_junk_confirmed) "
            "VALUES ((SELECT min(id) FROM runs), %s, %s, %s, 1.0, %s, 'jobs', 1)",
            (D0 - timedelta(days=days_ago), text, campaign_name, category))

    execute("INSERT INTO runs (run_type, started_at, status) "
            "VALUES ('weekly', %s, 'success')", (NOW,))
    for offset in range(0, 401):
        if offset == 10:
            continue                      # NO_FX: a rate genuinely missing
        execute("INSERT INTO fx_rates (rate_date, base_currency, quote_currency,"
                " rate, provider) VALUES (%s,'GBP','USD',%s,'synthetic')",
                (D0 - timedelta(days=offset), 1.2 + (offset % 7) * 0.01))
    for cid, name in NAMES.items():
        for day in (IN_7D, IN_30D, IN_180D, OLD):
            execute("INSERT INTO google_ads_campaign_daily_spend (customer_id,"
                    " currency_code, campaign_id, campaign_name, spend_date,"
                    " cost_micros, spend_account_currency) "
                    "VALUES (%s,'GBP',%s,%s,%s,50000000,50.0)",
                    (ACCOUNT, cid, name, day))
    # PR-ADS-176C-F2 (m3): production's campaign spend sits inside a verified
    # coverage ledger; an open-start (all_time) read counts each account's
    # rows only from its earliest verified chunk, and an account with none
    # contributes nothing. The ledger certifies every day this matrix stores,
    # so every payload is unchanged.
    execute("INSERT INTO google_ads_spend_coverage (customer_id, chunk_start,"
            " chunk_end, status) VALUES (%s,%s,%s,'verified')", (ACCOUNT, OLD, D0))
    for label in ("KSA Freight (legacy)", "ksa freight old"):
        execute("INSERT INTO google_ads_campaign_identity (customer_id, campaign_id,"
                " canonical_campaign_name, external_campaign_label, match_method,"
                " approved_at, approved_by) VALUES (%s,%s,%s,%s,'manual',%s,'test')",
                (ACCOUNT, CAMP_F, NAMES[CAMP_F], label, NOW))

    # A · one unit made of several raw spellings, only ONE of them flagged; the
    #     same term, unflagged, is a separate unit in another campaign.
    term(IN_7D, CAMP_A, "Freight JOBS", flagged=True, category="job_seeker")
    term(IN_7D, CAMP_A, "freight jobs", micros=3_000_000)
    term(IN_30D, CAMP_A, "freight  jobs", flagged=False, micros=4_000_000)
    term(IN_30D, CAMP_A, "ｆreight jobs", micros=1_500_000)   # NFKC: fullwidth f
    term(IN_7D, CAMP_B, "freight jobs")
    # B · flagged only by an annotation that names exactly one campaign.
    term(IN_7D, CAMP_A, "tms course")
    annotation("tms course", NAMES[CAMP_A], "education")
    # C · an annotation whose campaign name belongs to TWO ids: unplaceable.
    term(IN_7D, CAMP_C, "erp login")
    term(IN_7D, CAMP_D, "erp login", micros=2_500_000)
    annotation("erp login", NAMES[CAMP_C], "irrelevant")
    # D · an annotation with no campaign at all.
    term(IN_30D, CAMP_B, "cargo salary")
    annotation("cargo salary", None)
    # E · annotated against a spelling the fact rows also carry in another case.
    term(IN_7D, CAMP_B, "Free TMS")
    term(IN_30D, CAMP_B, "free tms")
    annotation("free tms", NAMES[CAMP_B], "free_intent")
    # F · flagged only OUTSIDE the short windows; unflagged inside them.
    term(OLD, CAMP_A, "nvocc salary", flagged=True, category="job_seeker")
    term(IN_7D, CAMP_A, "nvocc salary", flagged=False)
    # G · annotations for a term the window lacks / an old-only term.
    annotation("never searched here", NAMES[CAMP_A])
    term(OLD, CAMP_B, "old only term")
    annotation("old only term", NAMES[CAMP_B])
    # H · enough flagged terms in one campaign to fill several pages.
    for i in range(14):
        term(IN_7D if i % 2 else IN_30D, CAMP_A, f"logistics jobs {i}",
             flagged=True, category="job_seeker", micros=(i + 1) * 1_000_000,
             clicks=i)
    # I · the bulk nobody flagged.
    for i in range(60):
        term((IN_7D, IN_30D, IN_180D, OLD)[i % 4],
             (CAMP_A, CAMP_B, CAMP_C, CAMP_D)[i % 4], f"freight software {i}",
             flagged=False if i % 2 else None, micros=(1 + i % 9) * 1_000_000,
             clicks=i % 7, conversions=1 if i % 10 == 0 else 0)
    # J · a day with no FX rate: a unit only on that day, and one that also has
    #     a convertible day (one missing rate withholds the whole unit).
    term(NO_FX, CAMP_A, "customs broker", micros=7_000_000)
    term(NO_FX, CAMP_A, "customs software", micros=5_000_000)
    term(IN_7D, CAMP_A, "customs software", micros=6_000_000)
    # K · native USD — verified without any rate.
    term(IN_7D, CAMP_B, "usd term", currency="USD", micros=9_000_000)
    # L · two currencies inside one unit.
    term(IN_7D, CAMP_B, "mixed currency term", currency="GBP")
    term(IN_30D, CAMP_B, "mixed currency term", currency="EUR")
    # M · no currency at all; a currency with no cost.
    term(IN_7D, CAMP_A, "legacy currency term", currency=None, micros=3_000_000)
    term(IN_7D, CAMP_A, "no cost term", micros=None)
    # N · a currency no rate exists for.
    term(IN_7D, CAMP_B, "euro only term", currency="EUR", micros=4_000_000)
    # P · one campaign id seen under two labels, unknown to canonical spend.
    term(IN_30D, CAMP_E, "label drift term", name="old label e")
    term(IN_7D, CAMP_E, "label drift term", name="new label e", micros=3_000_000)
    term(IN_7D, CAMP_E, "label e only term", name="new label e")
    # Q · a campaign with approved aliases.
    term(IN_7D, CAMP_F, "ksa freight forwarder", micros=8_000_000, clicks=4)
    term(IN_30D, CAMP_F, "ksa customs clearance", micros=2_000_000)
    # R · ties: equal spend and clicks across units; equal flagged sort keys
    #     for one term in four campaigns.
    for i in range(1, 7):
        term(IN_7D, CAMP_A, f"tie term {i}", micros=5_000_000, clicks=5)
    for cid in (CAMP_A, CAMP_B, CAMP_C, CAMP_D):
        term(IN_7D, cid, "tie jobs", flagged=True, category="job_seeker",
             micros=5_000_000, clicks=5)
    # S · no clicks (CPC unavailable, never 0).
    term(IN_7D, CAMP_B, "zero click term", clicks=0, micros=1_000_000)
    # T · one unit across several ad groups / keywords / match types.
    term(IN_7D, CAMP_A, "freight forwarding crm", ad_group="ag 1", keyword="crm",
         match_type="EXACT", conversions=2, micros=6_000_000, clicks=9)
    term(IN_7D, CAMP_A, "freight forwarding crm", ad_group="ag 2", keyword="freight crm",
         match_type="PHRASE", conversions=1, micros=2_000_000, clicks=2)
    term(IN_30D, CAMP_A, "freight forwarding crm", ad_group="ag 2", keyword="crm",
         match_type="BROAD", micros=1_000_000)
    # V · several raw categories on one flagged unit, one of them unmapped.
    term(IN_7D, CAMP_B, "free logistics course", flagged=True, category="free_intent")
    term(IN_30D, CAMP_B, "free logistics course", flagged=True, category="weird_reason")

    # U · durable review decisions.
    from analysis.search_term_identity import (  # noqa: PLC0415
        IDENTITY_RULE_VERSION, term_identity_key,
    )
    for campaign_key, text, state in ((CAMP_A, "logistics jobs 3", "keep"),
                                      (CAMP_A, "logistics jobs 5", "resolved"),
                                      (CAMP_B, "tie jobs", "monitor")):
        execute("INSERT INTO search_term_review (term_identity, campaign_key,"
                " search_term_normalized, identity_rule_version, review_state,"
                " review_note, reviewed_by, reviewed_at, first_flagged_at,"
                " latest_flagged_at, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,'note','reviewer',%s,%s,%s,%s,%s)",
                (term_identity_key(campaign_key, text), campaign_key, text,
                 IDENTITY_RULE_VERSION, state, NOW, NOW, NOW, NOW, NOW))


#: Applied in order AFTER the base matrix has run; each is followed by its own
#: (smaller) matrix.
VARIANTS = (
    # A flagged row with no reason evidence: the flagged view quarantines.
    ("quarantine",
     "UPDATE search_terms SET junk_category = NULL, matched_pattern = NULL "
     "WHERE search_term = 'logistics jobs 7'"),
    # Production's state on 2026-11-22: nothing flagged, no annotation.
    ("nothing_flagged",
     "UPDATE search_terms SET is_flagged_waste = FALSE, junk_category = NULL,"
     " matched_pattern = NULL; DELETE FROM waste_terms"),
)


# ═════════════════════════════════════════════════════════════════════════════
# The request matrix
# ═════════════════════════════════════════════════════════════════════════════

def _json(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _response_text(response) -> str:
    """The body of a route's response, whether it was built whole or streamed."""
    if hasattr(response, "body_iterator"):
        async def _drain():
            parts = []
            async for chunk in response.body_iterator:
                parts.append(chunk if isinstance(chunk, str) else chunk.decode())
            return "".join(parts)
        return asyncio.run(_drain())
    return response.body.decode()


def scenarios(st, srv, variant: str | None = None) -> list:
    """``[(name, thunk)]`` — every request of the matrix for one variant."""
    out: list = []

    def add(name, fn, *args, **kwargs):
        out.append((f"{variant}:{name}" if variant else name,
                    lambda: fn(*args, **kwargs)))

    def terms(window, **kw):
        return st.build_search_term_evidence(window, now=NOW, **kw)

    def flagged(window, **kw):
        return st.build_flagged_search_terms(window, now=NOW, **kw)

    def patterns(window, **kw):
        return st.build_search_pattern_evidence(window, now=NOW, **kw)

    def pattern_detail(window, pattern, n, **kw):
        return st.build_search_pattern_drawer(window, pattern, n, now=NOW, **kw)

    def drawer(window, text, campaign_key=None):
        return st.build_search_term_drawer(window, text, campaign_key, now=NOW)

    def preview(window, campaign_key):
        return st.build_campaign_flagged_preview(window, campaign_key, now=NOW)

    def export(window, **kw):
        params = {"q": None, "campaign": None, "state": None, "junk_category": None,
                  "min_spend": None, "sort": "spend", "sql_state": None}
        params.update(kw)
        response = srv.api_search_term_evidence_export(user={}, window=window, **params)
        return {"media_type": response.media_type,
                "content_disposition": response.headers.get("content-disposition"),
                "csv": _response_text(response)}

    def waste(window):
        return srv.api_waste(user={}, days=30, window=window)

    if variant == "quarantine":
        for window in ("30d", "all_time"):
            add(f"flagged/{window}", flagged, window, page_size=5)
            add(f"flagged/{window}/filtered", flagged, window, page=2, q="jobs")
            add(f"waste/{window}", waste, window)
            add(f"preview/{window}/A", preview, window, CAMP_A)
            add(f"terms/{window}", terms, window, page_size=7)
        return out

    if variant == "nothing_flagged":
        for window in ("30d", "all_time"):
            add(f"terms/{window}", terms, window, page_size=7)
            add(f"terms/{window}/state=flagged", terms, window, state="flagged")
            add(f"flagged/{window}", flagged, window, page_size=5)
            add(f"waste/{window}", waste, window)
            add(f"preview/{window}/A", preview, window, CAMP_A)
            add(f"patterns/{window}/n=2", patterns, window, n=2, limit=8)
            add(f"pattern_detail/{window}/jobs", pattern_detail, window, "jobs", 1)
            add(f"term/{window}/freight jobs", drawer, window, "freight jobs")
            add(f"export/{window}", export, window)
        return out

    # ── Terms ───────────────────────────────────────────────────────────────
    for window in WINDOWS:
        for sort in st.TERM_SORTS:
            add(f"terms/{window}/sort={sort}", terms, window, page_size=7, sort=sort)
    for page in (2, 3, 5, 15, 16, 17, 40):          # middle, last, past the end
        add(f"terms/all_time/page={page}", terms, "all_time", page=page, page_size=7)
    add("terms/all_time/page_size=200", terms, "all_time", page_size=200)
    for sort in ("clicks", "cpc", "last_seen", "term"):
        add(f"terms/180d/sort={sort}/page=3", terms, "180d", page=3, page_size=7,
            sort=sort)
    for name, kw in (
            ("q=freight", {"q": "freight"}), ("q=JOBS", {"q": "JOBS"}),
            ("q=Freight JOBS", {"q": "Freight JOBS"}), ("q=none", {"q": "zzz-none"}),
            ("campaign=A", {"campaign": CAMP_A}), ("campaign=C", {"campaign": CAMP_C}),
            ("campaign=D", {"campaign": CAMP_D}), ("campaign=E", {"campaign": CAMP_E}),
            ("campaign=F", {"campaign": CAMP_F}),
            ("campaign=unknown", {"campaign": "does-not-exist"}),
            ("state=flagged", {"state": "flagged"}), ("state=clean", {"state": "clean"}),
            ("state=needs_review", {"state": "needs_review"}),
            ("junk=job_seeker", {"junk_category": "job_seeker"}),
            ("junk=weird_reason", {"junk_category": "weird_reason"}),
            ("min_spend=5", {"min_spend": 5.0}), ("min_spend=huge", {"min_spend": 1e9}),
            ("sql_state=all", {"sql_state": "all"}),
            ("sql_state=has_sql", {"sql_state": "has_sql"}),
            ("sql_state=known_zero", {"sql_state": "known_zero"}),
            ("sql_state=unavailable", {"sql_state": "unavailable"}),
            ("combo", {"q": "jobs", "campaign": CAMP_A, "state": "flagged",
                       "min_spend": 2.0, "sort": "clicks"}),
            ("combo/page=2", {"q": "freight", "state": "clean", "sort": "term",
                              "page": 2, "page_size": 5})):
        for window in ("30d", "all_time"):
            add(f"terms/{window}/{name}", terms, window, **{"page_size": 7, **kw})
    add("terms/error/sort", terms, "30d", sort="nope")
    add("terms/error/state", terms, "30d", state="nope")
    add("terms/error/window", terms, "9d")

    # ── Flagged (the page, complete population) ─────────────────────────────
    for window in WINDOWS:
        for sort in st.FLAGGED_SORTS:
            add(f"flagged/{window}/sort={sort}", flagged, window, page_size=5, sort=sort)
    for page in (2, 3, 5, 6, 30):
        add(f"flagged/all_time/page={page}", flagged, "all_time", page=page, page_size=5)
    for name, kw in (
            ("q=jobs", {"q": "jobs"}), ("q=shared name", {"q": "shared name"}),
            ("q=none", {"q": "zzz-none"}),
            ("campaign=A", {"campaign": CAMP_A}), ("campaign=C", {"campaign": CAMP_C}),
            ("campaign=name", {"campaign": NAMES[CAMP_C]}),
            ("review=unreviewed", {"review_state": "unreviewed"}),
            ("review=keep", {"review_state": "keep"}),
            ("review=resolved", {"review_state": "resolved"}),
            ("reason=job_seeker", {"flag_reason": "job_seeker"}),
            ("min_spend=5", {"min_spend": 5.0}),
            ("sql_state=unavailable", {"sql_state": "unavailable"}),
            ("sql_state=known_zero", {"sql_state": "known_zero"}),
            ("high_spend=3", {"high_spend_usd": 3.0}),
            ("combo", {"q": "logistics", "campaign": CAMP_A, "sort": "clicks",
                       "page": 2, "page_size": 4})):
        for window in ("30d", "all_time"):
            add(f"flagged/{window}/{name}", flagged, window, **{"page_size": 5, **kw})
    add("flagged/error/sort", flagged, "30d", sort="nope")
    for window in WINDOWS:
        add(f"waste/{window}", waste, window)
        for key in (CAMP_A, CAMP_B, CAMP_C, "does-not-exist"):
            add(f"preview/{window}/{key}", preview, window, key)

    # ── Patterns ────────────────────────────────────────────────────────────
    for window in WINDOWS:
        for n in (1, 2, 3):
            add(f"patterns/{window}/n={n}", patterns, window, n=n, limit=8)
    for sort in st.PATTERN_SORTS:
        add(f"patterns/all_time/sort={sort}", patterns, "all_time", n=1, sort=sort,
            limit=6)
    for name, kw in (
            ("min_terms=2", {"min_terms": 2}), ("min_terms=5", {"min_terms": 5}),
            ("min_terms=999", {"min_terms": 999}),
            ("limit=1", {"limit": 1}), ("limit=500", {"limit": 500}),
            ("q=jobs", {"q": "jobs"}), ("campaign=B", {"campaign": CAMP_B}),
            ("state=flagged", {"state": "flagged"}),
            ("state=clean", {"state": "clean"}), ("min_spend=5", {"min_spend": 5.0}),
            ("q=none", {"q": "zzz-none"})):
        for n in (1, 2):
            add(f"patterns/all_time/n={n}/{name}", patterns, "all_time",
                **{"n": n, "limit": 8, **kw})
            add(f"patterns/30d/n={n}/{name}", patterns, "30d",
                **{"n": n, "limit": 8, **kw})
    add("patterns/error/n", patterns, "30d", n=4)

    for window in ("30d", "all_time"):
        for pattern, n in (("jobs", 1), ("freight", 1), ("freight jobs", 2),
                           ("logistics jobs", 2), ("freight software", 2),
                           ("freight forwarding crm", 3), ("tie", 1),
                           ("not a pattern", 3)):
            add(f"pattern_detail/{window}/{pattern}", pattern_detail, window, pattern, n)
        add(f"pattern_detail/{window}/jobs/campaign=A", pattern_detail, window,
            "jobs", 1, campaign=CAMP_A)
        add(f"pattern_detail/{window}/jobs/state=flagged", pattern_detail, window,
            "jobs", 1, state="flagged")
        add(f"pattern_detail/{window}/freight/q", pattern_detail, window,
            "freight", 1, q="software 1", min_spend=2.0)

    # ── Term drawer ─────────────────────────────────────────────────────────
    for window in ("7d", "30d", "all_time"):
        for text, key in (
                ("Freight JOBS", None), ("freight jobs", CAMP_A),
                ("FREIGHT   jobs", CAMP_B), ("freight jobs", "does-not-exist"),
                ("erp login", None), ("erp login", CAMP_C), ("erp login", CAMP_D),
                ("tms course", CAMP_A), ("free tms", CAMP_B), ("Free TMS", None),
                ("tie jobs", None), ("tie jobs", CAMP_C),
                ("nvocc salary", CAMP_A), ("customs software", CAMP_A),
                ("mixed currency term", CAMP_B), ("legacy currency term", CAMP_A),
                ("no cost term", CAMP_A), ("euro only term", None),
                ("usd term", CAMP_B), ("label drift term", CAMP_E),
                ("ksa freight forwarder", CAMP_F),
                ("freight forwarding crm", CAMP_A),
                ("free logistics course", CAMP_B), ("never searched here", None)):
            add(f"term/{window}/{text}/{key}", drawer, window, text, key)

    # ── CSV export ──────────────────────────────────────────────────────────
    for window in WINDOWS:
        add(f"export/{window}", export, window)
    for name, kw in (("sort=term", {"sort": "term"}), ("sort=clicks", {"sort": "clicks"}),
                     ("q=jobs", {"q": "jobs"}), ("state=flagged", {"state": "flagged"}),
                     ("campaign=C", {"campaign": CAMP_C}), ("q=none", {"q": "zzz-none"}),
                     ("min_spend=5", {"min_spend": 5.0}),
                     ("sql_state=unavailable", {"sql_state": "unavailable"})):
        add(f"export/all_time/{name}", export, "all_time", **kw)
    return out


def run_matrix(st, srv, variant: str | None = None) -> dict:
    """Run one variant's matrix: ``{name: payload}``. A request that raises is
    recorded as the exception it raised — an error is part of the contract."""
    results: dict = {}
    for name, thunk in scenarios(st, srv, variant):
        try:
            results[name] = _json(thunk())
        except Exception as exc:  # noqa: BLE001
            results[name] = {"__raised__": type(exc).__name__,
                             "status_code": getattr(exc, "status_code", None),
                             "detail": str(getattr(exc, "detail", exc))}
    return results


def pin_clock(st) -> None:
    """Give the builders the fixed clock when a caller passes none (the CSV
    export route and the ``/api/waste`` adapter do not)."""
    real = st._base
    st._base = lambda window, now: real(window, now or NOW)


def digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _use_sorted_reader(st_repo) -> None:
    """PRE-CHANGE TREE ONLY — hand the old code its grouped rows in the order
    the new statement specifies (see the module docstring)."""
    real = st_repo.fetch_search_term_aggregates

    def _b(value):
        return (value is None, (value or "").encode("utf-8"))

    def _ordered(start, end, **kw):
        result = real(start, end, **kw)
        if result.get("rows"):
            result["rows"].sort(key=lambda r: (
                r["search_term"].encode("utf-8"), _b(r.get("campaign_name")),
                _b(r.get("campaign_id"))))
        return result

    st_repo.fetch_search_term_aggregates = _ordered


def main(argv: list) -> int:
    sys.path.insert(0, os.getcwd())
    os.environ.setdefault("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)

    import db.connection as connection
    connection.init_pool()
    from db.connection import get_conn
    from db.schema import init_db

    def execute(sql, params=()):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)

    if "--no-seed" not in argv:
        init_db()
        seed(execute)

    import api.server as srv
    import db.search_term_repository as st_repo
    import services.search_term_evidence_service as st

    pin_clock(st)
    if "--sorted-reader" in argv:
        _use_sorted_reader(st_repo)

    results = run_matrix(st, srv)
    for variant, statements in VARIANTS:
        for statement in statements.split(";"):
            if statement.strip():
                execute(statement)
        results.update(run_matrix(st, srv, variant))

    text = json.dumps(results, sort_keys=True)
    if "--out" in argv:
        with open(argv[argv.index("--out") + 1], "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
