"""
tests/test_pr_ads_155_f2_lifecycle_activity_truth.py

PR-ADS-155-F2 — make the Dashboard lifecycle section cohort-truthful.

The production data was never wrong. For `current_quarter` the canonical funnel
returned stage-ENTRY counts of 352 / 553 / 42 / 46 / 17 — MQL above Lead,
Opportunity above SQL — and separately returned cohort conversions of 82.95%,
6.69%, 11.90% and 2.17%. Both are true, because they answer different questions:
a stage-entry count asks "who entered this stage during the window", and a
contact can enter MQL this quarter having entered Lead two years ago.

What was wrong was the presentation, in two specific places:

  §2  `kpis.sql_rate` divided two INDEPENDENT window totals, and
      `kpis.customer_rate` divided closed-won DEALS by lead CONTACTS — two
      populations and two grains — and the Dashboard rendered the second as
      "n% of leads" on the Customers card, drawing in words the arrow from
      lifecycle progression into commercial outcome that PR-ADS-155 §2 forbids
      drawing at all.
  §1  the five stage-entry totals had no home that said what they were, so the
      only lifecycle numbers on the page were the Lead-anchored cohort's, and
      the window's actual activity was invisible.

This suite fixes both in place: activity is published as activity, and every
percentage between two stages is read whole from the canonical funnel's own
`conversions` collection.

Run with:
    python -m pytest tests/test_pr_ads_155_f2_lifecycle_activity_truth.py -v
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from services import canonical_contract  # noqa: E402
from services import canonical_crm_funnel_service as funnel  # noqa: E402
from services import cross_page_parity_service as parity  # noqa: E402
from services import dashboard_overview_service as overview  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

# Two cases audit real PostgreSQL. The rest are pure and always run; CI fails
# loudly if the database ones skip, because a skipped database case is not
# merge evidence.
_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

_APP = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
_CSS = (_ROOT / "static" / "styles.css").read_text(encoding="utf-8")
_OVERVIEW_SRC = (_ROOT / "services" / "dashboard_overview_service.py").read_text(
    encoding="utf-8")

_START = date(2026, 1, 1)
_END = date(2026, 3, 31)
_EVENTS = ("lead", "mql", "sql", "opportunity", "customer")


# ── fixture builders ─────────────────────────────────────────────────────────
def _contact(cid, *, lead=None, mql=None, sql=None, opp=None, customer=None,
             stage=None, source="ORGANIC_SEARCH"):
    return {
        "contact_id": cid,
        "lifecycle_stage": stage,
        "hs_analytics_source": source,
        "date_entered_lead": lead,
        "date_entered_mql": mql,
        "date_entered_sql": sql,
        "date_entered_opportunity": opp,
        "date_entered_customer": customer,
    }


def _production_shaped_rows():
    """The exact production `current_quarter` shape, reconstructed.

    Stage-entry totals  352 / 553 / 42 / 46 / 17  (MQL > Lead, Opportunity > SQL)
    Cohort conversions  292/352, 37/553, 5/42, 1/46

    Built from four overlapping groups rather than five independent ones,
    because that is what the real data is: one contact contributes to several
    stage-entry populations, and to the cohort of whichever stage it entered
    first.
    """
    inside = date(2026, 2, 10)
    later = date(2026, 3, 20)
    before = date(2025, 6, 1)
    rows = []

    # A: entered Lead AND MQL inside the window — the 292 that converted.
    for i in range(292):
        sql_date = later if i < 37 else None
        opp_date = later if i < 5 else None
        cust_date = later if i < 1 else None
        rows.append(_contact(f"A{i}", lead=inside, mql=inside, sql=sql_date,
                             opp=opp_date, customer=cust_date))
    # B: entered Lead inside the window and got no further. 292 + 60 = 352.
    for i in range(60):
        rows.append(_contact(f"B{i}", lead=inside))
    # C: entered MQL inside the window having entered Lead LAST YEAR. This is
    #    the group that makes MQL exceed Lead, and it is not an anomaly.
    for i in range(261):
        rows.append(_contact(f"C{i}", lead=before, mql=inside))
    # D: entered SQL inside the window only. 37 + 5 = 42.
    for i in range(5):
        rows.append(_contact(f"D{i}", sql=inside))
    # E: entered Opportunity inside the window only. 5 + 41 = 46.
    for i in range(41):
        rows.append(_contact(f"E{i}", opp=inside))
    # F: entered Customer inside the window only. 1 + 16 = 17.
    for i in range(16):
        rows.append(_contact(f"F{i}", customer=inside))
    return rows


def _pops(rows, start=_START, end=_END, **kwargs):
    return funnel.build_populations(rows, start, end, **kwargs)


def _funnel_block(populations, *, available=True):
    """The `lifecycle_funnel` shape `_lifecycle_funnel_block` publishes.

    Assembled from the REAL canonical service — counts, conversions, coverage
    and reconciliation all come from `canonical_crm_funnel_service`, so a test
    that passes here is a statement about the contract and not about a mock.
    """
    status = funnel.reconciliation_status(populations, available=available)
    counts = populations["counts"]
    scope = funnel.SCOPE_ALL_SOURCE
    mismatch = status["status"] == funnel.STATUS_MISMATCH
    return {
        **{e: (None if (not available or mismatch) else counts[e][scope])
           for e in _EVENTS},
        "available": available,
        "status": status["status"],
        "reconciliation": status,
        "scope": scope,
        "scope_label": funnel.SCOPE_LABELS[scope],
        "window": {"window_type": "business", "window_key": "current_quarter",
                   "start_date": _START.isoformat(), "end_date": _END.isoformat()},
        "conversions": (funnel.build_conversions(populations, scope)
                        if available else None),
        "coverage": populations["coverage"] if available else None,
        "definitions": {e: {
            "label": funnel.EVENT_LABELS[e],
            "event_date_property": funnel.EVENT_HUBSPOT_PROPERTY[e],
        } for e in _EVENTS},
        "sync": {"status": "ok"},
        "cohort": None,
    }


def _activity(rows=None, *, populations=None, available=True, monkeypatch=None,
              previous=None):
    """Build the published `lifecycle_activity` block for a fixture."""
    pops = populations if populations is not None else _pops(rows or [])
    block = _funnel_block(pops, available=available)
    if monkeypatch is not None:
        monkeypatch.setattr(
            overview, "_lifecycle_previous_period",
            lambda _w, _n: previous or {"available": False, "label": None,
                                        "window": None, "counts": {},
                                        "reason": "Previous period unavailable."})
    return overview._lifecycle_activity_block(block, "current_quarter", None)


def _by_pair(activity):
    return {f"{c['from_event']}>{c['to_event']}": c
            for c in activity["conversions"]}


def _entered(activity):
    return [s["entered"] for s in activity["stages"]]


# ── frontend source slices ───────────────────────────────────────────────────
def _activity_renderer():
    """Everything from the activity renderer to the start of the cohort one."""
    start = _APP.index("function renderDashLifecycleActivity(d)")
    end = _APP.index("function renderDashLifecycleCohort(d)")
    return _APP[start:end]


def _kpi_row_source():
    start = _APP.index("function renderDashKpiRow(d)")
    return _APP[start:start + 6000]


def _strip_comments(src):
    return re.sub(r"//.*|/\*[\s\S]*?\*/", "", src)


# ═════════════════════════════════════════════════════════════════════════════
# §1 — activity counts are activity counts, and may rise
# ═════════════════════════════════════════════════════════════════════════════
def test_1_stage_entry_counts_are_published_without_forcing_a_descent(monkeypatch):
    """Order preserved, values untouched, and the shape stated on the block.

    Nothing sorts, caps or clamps these five numbers. `monotonic_expected` is
    published as False so the renderer reads the expectation from the contract
    rather than deciding for itself that a rise is a defect.
    """
    activity = _activity(_production_shaped_rows(), monkeypatch=monkeypatch)

    assert [s["event"] for s in activity["stages"]] == list(_EVENTS)
    assert _entered(activity) == [352, 553, 42, 46, 17]
    assert activity["monotonic_expected"] is False
    assert activity["basis"] == "stage_entry_events"
    # Every card names the question its number answers, using the canonical
    # event label rather than a second display vocabulary.
    assert [s["activity_label"] for s in activity["stages"]] == [
        "Leads entered during this period",
        "MQLs entered during this period",
        "SQLs entered during this period",
        "Opportunities entered during this period",
        "Lifecycle Customers entered during this period",
    ]
    assert [s["label"] for s in activity["stages"]] == [
        funnel.EVENT_LABELS[e] for e in _EVENTS]


def test_2_mql_may_exceed_lead_without_being_an_error(monkeypatch):
    """The production case. 553 MQL entries against 352 Lead entries is data."""
    activity = _activity(_production_shaped_rows(), monkeypatch=monkeypatch)
    stages = {s["event"]: s for s in activity["stages"]}

    assert stages["mql"]["entered"] > stages["lead"]["entered"]
    assert activity["available"] is True
    assert activity["reason"] is None
    assert activity["reconciliation"]["status"] != funnel.STATUS_MISMATCH
    # And no stage is withheld because of the rise.
    assert all(s["available"] for s in activity["stages"])


def test_3_opportunity_may_exceed_sql_without_being_an_error(monkeypatch):
    activity = _activity(_production_shaped_rows(), monkeypatch=monkeypatch)
    stages = {s["event"]: s for s in activity["stages"]}

    assert stages["opportunity"]["entered"] > stages["sql"]["entered"]
    assert activity["available"] is True
    assert activity["reconciliation"]["status"] != funnel.STATUS_MISMATCH


# ═════════════════════════════════════════════════════════════════════════════
# §2 — every connector is a canonical cohort conversion
# ═════════════════════════════════════════════════════════════════════════════
def test_4_connectors_come_from_canonical_conversions_not_a_quotient_of_cards(
        monkeypatch):
    """The rates are the canonical ones, and are demonstrably NOT card ÷ card.

    On this fixture the two answers differ enormously — 82.95% against 157.10%
    for Lead→MQL — so a renderer that had quietly divided one card by the next
    could not pass by coincidence.
    """
    activity = _activity(_production_shaped_rows(), monkeypatch=monkeypatch)
    pairs = _by_pair(activity)
    counts = {s["event"]: s["entered"] for s in activity["stages"]}

    for key, (from_event, to_event) in {
        "lead>mql": ("lead", "mql"), "mql>sql": ("mql", "sql"),
        "sql>opportunity": ("sql", "opportunity"),
        "opportunity>customer": ("opportunity", "customer"),
    }.items():
        conversion = pairs[key]
        assert conversion["basis"] == funnel.BASIS_COHORT
        assert conversion["cohort_size"] == counts[from_event]
        card_quotient = round(counts[to_event] * 100.0 / counts[from_event], 2)
        assert conversion["rate_pct"] != card_quotient, (
            f"{key} rate equals the card-to-card quotient — the one arithmetic "
            "this section exists to stop")

    # And the DISPLAYED rate is the canonical field itself, not something
    # derived beside it. Checking only that `rate_pct` appears somewhere in the
    # renderer would pass on a file that guards on it and then prints its own
    # quotient, so the assertion is on the span that the reader actually sees.
    src = _activity_renderer()
    rate_span = src[src.index('class="dash-activity__conv-rate">${'):]
    rate_span = rate_span[:rate_span.index("</span>")]
    assert "conversion.rate_pct" in rate_span, rate_span
    for field in ("cohort_size", "converted", "cohort_label"):
        assert field in src, field


def test_5_the_production_shape_renders_lead_to_mql_as_82_95_percent(monkeypatch):
    """292 of a 352-contact Lead cohort, exactly as production reported."""
    activity = _activity(_production_shaped_rows(), monkeypatch=monkeypatch)
    lead_to_mql = _by_pair(activity)["lead>mql"]

    assert lead_to_mql["cohort_size"] == 352
    assert lead_to_mql["converted"] == 292
    assert lead_to_mql["rate_pct"] == 82.95
    assert lead_to_mql["available"] is True
    assert lead_to_mql["basis"] == funnel.BASIS_COHORT
    # The denominator travels with the rate, so a bare percentage never has to
    # borrow the nearest number on the page.
    assert lead_to_mql["cohort_label"] == "in the Leads cohort"
    assert lead_to_mql["from_label"] == "Leads"
    assert lead_to_mql["to_label"] == "MQLs"

    # The other three production rates arrive from the same contract.
    pairs = _by_pair(activity)
    assert pairs["mql>sql"]["rate_pct"] == 6.69
    assert pairs["mql>sql"]["converted"] == 37
    assert pairs["mql>sql"]["cohort_size"] == 553
    assert pairs["sql>opportunity"]["rate_pct"] == 11.9
    assert pairs["opportunity>customer"]["rate_pct"] == 2.17


def test_6_no_lifecycle_conversion_is_ever_negative(monkeypatch):
    """A cohort numerator is a subset of its own denominator, so it cannot be.

    Checked over the production shape AND over a shape where every later stage
    predates its anchor, which is the closest a cohort can come to "going
    backwards": those contacts are excluded, not counted as negative movement.
    """
    backwards = [_contact(f"X{i}", lead=date(2026, 2, 10), mql=date(2025, 1, 1))
                 for i in range(20)]
    for rows in (_production_shaped_rows(), backwards):
        activity = _activity(rows, monkeypatch=monkeypatch)
        for conversion in activity["conversions"]:
            rate = conversion["rate_pct"]
            if rate is not None:
                assert rate >= 0, conversion
                assert conversion["converted"] <= conversion["cohort_size"]

    # Nothing in the renderer can produce a signed conversion: no subtraction,
    # no negation, no explicit sign prefix. (The trend chip does carry a sign —
    # that is the point of §5, and it lives in a different function.)
    src = _strip_comments(_activity_renderer())
    conv = src[src.index("function dashActivityConversion("):
               src.index("function dashActivityConversionReason(")]
    for operator in (" - ", "-=", '"-"', "'-'", "Math.abs", "> 0 ?"):
        assert operator not in conv, operator


def test_7_a_canonical_available_zero_conversion_renders_as_zero(monkeypatch):
    """A proven zero is a measurement, and withholding it would be its own lie."""
    rows = [_contact(f"Z{i}", lead=date(2026, 2, 10)) for i in range(9)]
    activity = _activity(rows, monkeypatch=monkeypatch)
    lead_to_mql = _by_pair(activity)["lead>mql"]

    assert lead_to_mql["available"] is True
    assert lead_to_mql["cohort_size"] == 9
    assert lead_to_mql["converted"] == 0
    assert lead_to_mql["rate_pct"] == 0.0

    # The renderer's unavailable branch is guarded on availability and on the
    # three values it needs — a real 0.0 passes every one of those guards.
    src = _activity_renderer()
    assert "!conversion.available" in src
    assert "conversion.rate_pct === null" in src


def test_8_an_unavailable_conversion_renders_unavailable_not_zero(monkeypatch):
    """No cohort means no rate. The card says so instead of showing 0%."""
    rows = [_contact("only-mql", mql=date(2026, 2, 10))]
    activity = _activity(rows, monkeypatch=monkeypatch)
    lead_to_mql = _by_pair(activity)["lead>mql"]

    assert lead_to_mql["available"] is False
    assert lead_to_mql["rate_pct"] is None
    assert lead_to_mql["converted"] is None
    assert lead_to_mql["basis"] == funnel.BASIS_UNAVAILABLE
    assert lead_to_mql["reason"] == "empty_cohort"

    src = _activity_renderer()
    assert "Unavailable" in src
    assert "dash-activity__conv--muted" in src
    assert "empty_cohort" in src, "the reason is given a human label, not shown raw"


def test_9_a_missing_cohort_denominator_fails_closed(monkeypatch):
    """An unreadable scope is not an empty one, and never becomes a rate."""
    activity = _activity(_production_shaped_rows(), available=False,
                         monkeypatch=monkeypatch)

    assert activity["available"] is False
    assert activity["reason"] == "canonical_contact_store_unavailable"
    assert activity["conversions"] == []
    assert _entered(activity) == [None] * 5
    assert all(s["available"] is False for s in activity["stages"])
    # Not a single zero anywhere in the withheld block.
    assert 0 not in _entered(activity)

    # The renderer refuses to draw the panel body at all in this state.
    src = _activity_renderer()
    assert "!activity.available" in src


# ═════════════════════════════════════════════════════════════════════════════
# §5 — a trend is a trend
# ═════════════════════════════════════════════════════════════════════════════
def test_10_previous_period_trend_is_distinct_from_cohort_conversion(monkeypatch):
    """Different data, different keys, different markup, different place.

    The two are the classic confusion: a stage that fell 12% against last
    quarter is not a stage that converts at 12%, and on the old strip the only
    percentage a reader ever saw between two cards was a conversion, so any
    period movement had nowhere honest to appear.
    """
    previous = {"available": True, "label": "vs Last Quarter", "window": None,
                "counts": {"lead": 300, "mql": 553, "sql": 42,
                           "opportunity": 46, "customer": 17},
                "reason": None}
    activity = _activity(_production_shaped_rows(), monkeypatch=monkeypatch,
                         previous=previous)
    lead = activity["stages"][0]

    # The trend lives on the STAGE, the conversion lives between stages.
    assert lead["previous_period"]["status"] == "ok"
    assert lead["previous_period"]["previous"] == 300
    assert "previous_period" not in _by_pair(activity)["lead>mql"]
    assert activity["previous_period"]["label"] == "vs Last Quarter"
    # The comparison block never carries the counts themselves into the payload
    # beside the conversions, so the two can never be read as one series.
    assert "counts" not in activity["previous_period"]

    # A zero baseline fails closed rather than becoming an infinite rise.
    zeroed = dict(previous, counts=dict(previous["counts"], lead=0))
    zero_activity = _activity(_production_shaped_rows(), monkeypatch=monkeypatch,
                              previous=zeroed)
    assert zero_activity["stages"][0]["previous_period"]["status"] == "no_comparison"
    assert zero_activity["stages"][0]["previous_period"]["delta_pct"] is None

    # In the markup they are different elements with different classes, and the
    # trend is rendered INSIDE the stage cell, never between two cells.
    src = _activity_renderer()
    assert "dash-activity__trend" in src and "dash-activity__conv" in src
    stage_cell = src[src.index('<div class="dash-activity__stage"'):]
    stage_cell = stage_cell[:stage_cell.index("</div>`;")]
    assert "dashActivityTrend(" in stage_cell
    assert "dashActivityConversion(" not in stage_cell
    assert "vs previous period" in src


# ═════════════════════════════════════════════════════════════════════════════
# §4 — lifecycle stages are not commercial outcomes
# ═════════════════════════════════════════════════════════════════════════════
def test_11_lifecycle_customer_stays_distinct_from_closed_won_customers():
    """Contact-stage fact and deal fact, kept apart in payload and in markup."""
    src = _activity_renderer()
    for commercial in ("commercial_outcomes", "closed_won_revenue_usd",
                       "k.customers", "known_revenue_usd", "total_revenue_usd"):
        assert commercial not in src, (
            f"{commercial} is a deal fact and must not appear in the lifecycle "
            "activity panel")
    assert '"connected_to_commercial_outcomes": False' in _OVERVIEW_SRC

    # The retired ratio was the one place the two were joined by arithmetic:
    # closed-won DEALS ÷ lead CONTACTS, rendered as "n% of leads". Comments are
    # stripped first — the explanation of what was removed necessarily quotes
    # the string that was removed.
    kpi = _strip_comments(_kpi_row_source())
    assert "% of leads" not in kpi
    assert "k.customer_rate" not in kpi
    assert "k.sql_rate" not in kpi


def test_12_closed_won_revenue_is_not_a_lifecycle_stage(monkeypatch):
    """The activity block holds exactly the five lifecycle stages, and no money."""
    activity = _activity(_production_shaped_rows(), monkeypatch=monkeypatch)

    assert [s["event"] for s in activity["stages"]] == list(_EVENTS)
    serialised = repr(activity)
    for money in ("revenue", "usd", "amount"):
        assert money not in serialised.lower(), money
    assert activity["connected_to_commercial_outcomes"] is False
    # The commercial panel is still rendered separately, after the lifecycle
    # ones, and still declares that it is not joined to them.
    assert "renderDashLifecycleActivity(d)" in _APP
    assert "renderDashCommercialOutcomes(d)" in _APP
    assert '"connected_to_lifecycle_funnel": False' in _OVERVIEW_SRC


# ═════════════════════════════════════════════════════════════════════════════
# §6 — partial coverage, disclosed rather than smoothed
# ═════════════════════════════════════════════════════════════════════════════
def test_13_partial_reconciliation_publishes_a_visible_coverage_disclosure(
        monkeypatch):
    """A stage reached with no entry timestamp turns the verdict partial."""
    rows = _production_shaped_rows()
    # Twelve contacts whose CURRENT stage proves they reached SQL, with no SQL
    # entry date anywhere in HubSpot.
    rows += [_contact(f"G{i}", stage=funnel.EVENT_STAGE["sql"]) for i in range(12)]
    activity = _activity(rows, monkeypatch=monkeypatch)

    assert activity["reconciliation"]["status"] == funnel.STATUS_PARTIAL
    assert funnel.REASON_MISSING_STAGE_DATE in activity["reconciliation"]["reasons"]
    by_event = {s["event"]: s for s in activity["stages"]}
    # A contact now at SQL must have passed through Lead and MQL, so the gap is
    # counted at each of the three stages it proves and at none of the two it
    # does not. Reporting it only at SQL would understate the missing evidence.
    assert by_event["sql"]["missing_entry_date_contacts"] == 12
    assert by_event["lead"]["missing_entry_date_contacts"] == 12
    assert by_event["mql"]["missing_entry_date_contacts"] == 12
    assert by_event["opportunity"]["missing_entry_date_contacts"] == 0
    assert by_event["customer"]["missing_entry_date_contacts"] == 0
    # The activity counts themselves are unchanged by the gap: no date, no
    # window membership, and nothing invented to give them one.
    assert _entered(activity) == [352, 553, 42, 46, 17]

    # Rendered, not hidden: a badge on the face of the panel and a disclosure
    # that names the per-stage counts.
    src = _activity_renderer()
    assert "Partial historical coverage" in src
    assert "function dashActivityCoverageBadge" in src
    assert "function dashActivityCoverageDisclosure" in src
    assert "missing_entry_date_contacts" in src
    assert "reconciliation" in src


def test_14_a_missing_stage_entry_date_is_never_inferred(monkeypatch):
    """The transition is real, the date is unknown, and it stays unknown."""
    rows = [_contact(f"H{i}", stage=funnel.EVENT_STAGE["customer"])
            for i in range(7)]
    activity = _activity(rows, monkeypatch=monkeypatch)

    # No proxy date was invented, so these contacts enter no window's totals.
    assert _entered(activity) == [0, 0, 0, 0, 0]
    coverage = activity["coverage"]["stage_reached_without_entry_date"]
    assert coverage["customer"] == 7
    assert coverage["lead"] == 7, "reaching Customer implies reaching Lead"

    src = _activity_renderer()
    assert "never replaced" in src
    assert "contact creation date" in src
    # And the canonical service still refuses to substitute one, anywhere: an
    # event date is admitted only from the contact's own stage-entry column.
    service_src = (_ROOT / "services" / "canonical_crm_funnel_service.py").read_text(
        encoding="utf-8")
    assert "never substituted" in service_src
    assert "date_entered_" not in _activity_renderer(), (
        "the renderer must not reach for a raw date column of its own")


def test_15_a_missing_history_payload_is_not_an_empty_but_complete_history():
    """`history_payload_missing` and `history_payload_empty` stay different.

    Production's dry run returned the first for all 50 contacts: HubSpot sent
    back no history object at all. That proves nothing about whether the
    transitions happened — unlike an empty history, which is evidence they were
    never recorded. Collapsing the two would turn "we did not receive it" into
    "it does not exist".
    """
    from services import lifecycle_history_recovery_service as recovery

    assert recovery.HISTORY_PAYLOAD_MISSING != recovery.HISTORY_PAYLOAD_EMPTY
    assert recovery.NO_HISTORY_VERSION == "history_present_no_matching_stage_version"

    from connectors import hubspot_pull

    states = {hubspot_pull.HISTORY_CONTACT_ABSENT,
              hubspot_pull.HISTORY_PROPERTY_ABSENT,
              hubspot_pull.HISTORY_EMPTY,
              hubspot_pull.HISTORY_PRESENT}
    assert len(states) == 4, "four evidence states, four names"


# ═════════════════════════════════════════════════════════════════════════════
# §7 — revenue stays fail-closed
# ═════════════════════════════════════════════════════════════════════════════
def test_16_all_time_revenue_stays_unavailable_while_any_amount_is_missing():
    from services import canonical_revenue_service as canonical_revenue

    verdict = canonical_revenue.total_verdict_for_population(
        won_deals=181, unpriced_deals=14, scope="all_source")
    assert verdict["publishable"] is False
    assert verdict["reason"] == canonical_revenue.REASON_REVENUE_INCOMPLETE
    assert verdict["reason"] == "closed_won_deals_missing_amount"
    assert "currency_unproven_deals_in_population" in verdict["violation_codes"]
    assert verdict["currency_unavailable_deals"] == 14
    # The detail names the numerator AND the denominator, so the sentence is
    # about deals rather than about whatever rows happened to be summed.
    assert "14 of 181 closed-won deal(s)" in verdict["detail"]

    priced = canonical_revenue.total_verdict_for_population(
        won_deals=181, unpriced_deals=0, scope="all_source")
    assert priced["publishable"] is True
    assert priced["reason"] is None


def test_17_known_partial_revenue_is_never_relabelled_as_total():
    outcomes_start = _APP.index("function renderDashCommercialOutcomes(d)")
    outcomes = _APP[outcomes_start:outcomes_start + 4000]
    total_cell = outcomes[outcomes.index("Total Closed-Won Revenue"):]
    total_cell = total_cell[:total_cell.index("</div>\n        </div>")]

    assert "total_revenue_usd" in total_cell
    assert "known_revenue_usd" not in total_cell
    assert "o.known_revenue_label" in outcomes
    # The label the partial sum appears under is the backend's, and names its
    # own denominator.
    assert "known_revenue_label" in _OVERVIEW_SRC


# ═════════════════════════════════════════════════════════════════════════════
# Contracts, parity, layout
# ═════════════════════════════════════════════════════════════════════════════
def test_18_the_existing_metric_contracts_survive_and_the_new_one_is_valid(
        monkeypatch):
    """Additive, not a replacement. Every PR-ADS-155 contract is still filed."""
    from services import canonical_contract

    for existing in ("lifecycle_cohort", "known_revenue_usd",
                     "revenue_unavailable_deals", "closed_won_revenue_usd",
                     "customers", "google_ads_spend_usd"):
        assert f'"metric": "{existing}"' in _OVERVIEW_SRC, existing
    assert '"metric": "lifecycle_activity"' in _OVERVIEW_SRC

    resolved = {"key": "current_quarter", "start_date": "2026-01-01",
                "end_date": "2026-03-31", "timezone": "Europe/London"}
    contract = canonical_contract.metric_contract(
        metric="lifecycle_activity",
        data_source=canonical_contract.SOURCE_CANONICAL_FUNNEL,
        scope="stage_entry_activity", resolved=resolved)
    for field in parity.REQUIRED_CONTRACT_FIELDS:
        assert field in contract, field
    assert contract["truth_status"] == canonical_contract.TRUTH_READY
    assert contract["fallback_used"] is False


def test_19_the_bounded_windows_gain_no_new_parity_violation():
    """This patch introduces no blocker of its own in the four bounded windows.

    Stated precisely rather than as "parity is green": in a bare fixture the
    audit legitimately reports metrics it cannot read, and asserting green here
    would be evidence of the harness rather than of the code. What IS provable
    is that `lifecycle_activity` is not a parity metric, contributes no
    violation, and that no revenue-total blocker appears in a bounded window.
    The production run across all five windows is the acceptance step.
    """
    audited_metrics = set(parity.METRIC_IDENTITIES)
    assert "lifecycle_activity" not in audited_metrics
    assert "sql_rate" not in audited_metrics
    assert "customer_rate" not in audited_metrics
    # The identities the audit DOES compare are untouched by this patch, so a
    # bounded window cannot acquire a violation from it.
    for untouched in ("closed_won_revenue_usd", "customers",
                      "google_ads_spend_usd", "campaign_attributable_sqls"):
        assert untouched in audited_metrics, untouched


@_needs_pg
def test_19b_the_four_bounded_windows_audit_against_a_live_database(pg):  # noqa: F811
    """The same claim, end to end against real PostgreSQL.

    Every closed-won deal in the fixture is priced, so no bounded window may
    report a revenue-total blocker, and none may report the ledger as unreadable
    — the audit reached a real database and produced a structured result for
    each window.
    """
    from tests.test_pr_ads_154c_parity_pg import _DEAL_SQL, _READY_SYNC_SQL, _exec

    _exec(_READY_SYNC_SQL, ("complete",))
    for i in range(12):
        _exec(_DEAL_SQL, (f"F2P{i}", f"Priced {i}", 4000.0, "USD", 4000.0,
                          "verified_usd", "deal_currency_is_usd"))

    for window in ("current_quarter", "last_quarter", "last_6_months", "ytd"):
        outcome = parity.audit_window(window)
        codes = outcome["violation_codes"]
        assert parity.V_TOTAL_UNPUBLISHABLE not in codes, (window, outcome)
        assert parity.V_DB_UNREADABLE not in codes, (window, outcome)
        # And the new block contributes no violation of its own.
        assert not [v for v in outcome["violations"]
                    if "lifecycle_activity" in str(v.get("metric") or "")], outcome

        # The block is published and readable against the same live database.
        from services.dashboard_overview_service import build_dashboard_overview

        payload = build_dashboard_overview(window=window)
        activity = payload["lifecycle_activity"]
        assert isinstance(activity, dict), window
        assert activity["basis"] == "stage_entry_events"
        assert activity["monotonic_expected"] is False
        assert payload[canonical_contract.METRIC_TRUTH_KEY]["lifecycle_activity"]
        # An empty contact store yields NULL counts, never zeros.
        assert all(s["entered"] is None or isinstance(s["entered"], int)
                   for s in activity["stages"]), window


@_needs_pg
def test_20b_all_time_is_blocked_only_by_the_missing_amounts(pg):  # noqa: F811
    """181 closed-won deals, 14 unpriced: the production shape, audited."""
    from tests.test_pr_ads_154c_parity_pg import _DEAL_SQL, _READY_SYNC_SQL, _exec

    _exec(_READY_SYNC_SQL, ("complete",))
    for i in range(167):
        _exec(_DEAL_SQL, (f"F2A{i}", f"Priced {i}", 5259.43, "USD", 5259.43,
                          "verified_usd", "deal_currency_is_usd"))
    for i in range(14):
        _exec(_DEAL_SQL, (f"F2U{i}", f"Unpriced {i}", None, None, None,
                          "unavailable", "no_amount"))

    outcome = parity.audit_window("all_time")
    assert outcome["ok"] is False
    assert parity.V_TOTAL_UNPUBLISHABLE in outcome["violation_codes"], outcome
    by_metric = {m["metric"]: m for m in outcome["metrics"]}
    assert by_metric["closed_won_revenue_usd"]["status"] == "total_unpublishable"
    assert by_metric["closed_won_revenue_usd"]["value"] is None
    # No revenue identity falls back to the generic catch-all.
    assert [] == [v for v in outcome["violations"]
                  if v.get("code") == parity.V_SOURCE_UNAVAILABLE
                  and "revenue" in (v.get("metric") or "")]
    # The count survives the unknown total, on the page as well as in the audit.
    from services.dashboard_overview_service import build_dashboard_overview

    payload = build_dashboard_overview(window="all_time")
    outcomes = payload.get("commercial_outcomes") or {}
    assert outcomes["total_revenue_publishable"] is False
    assert outcomes["total_revenue_usd"] is None
    assert outcomes["connected_to_lifecycle_funnel"] is False
    # The reason is always one of the governed codes — never absent, and never a
    # total published anyway. Which of the two it is depends on whether the
    # HubSpot integration is wired in the environment under test; the
    # missing-amount path itself is asserted by `test_16` and, over the real
    # ledger, by the audit above.
    assert outcomes["unavailable_reason"] in (
        "closed_won_deals_missing_amount", "revenue_integration_not_connected")


def test_20_the_only_all_time_revenue_blocker_is_the_missing_amount_code():
    status, code, _detail = parity._classify_unavailable(
        [{"consumer": "dashboard/overview",
          "unavailable_reason": "closed_won_deals_missing_amount",
          "truth_status": "not_ready",
          "violation_codes": ["currency_unproven_deals_in_population"]}],
        "closed_won_revenue_usd")
    assert code == parity.V_TOTAL_UNPUBLISHABLE
    assert code == "revenue_total_unpublishable_missing_amount"
    assert code != parity.V_SOURCE_UNAVAILABLE
    assert status == "total_unpublishable"


def test_21_the_activity_panel_is_responsive_on_desktop_and_mobile():
    """One grid row on desktop, one column under 900px, no sideways scroll.

    The row is a grid rather than a wrapping flex line for a reason worth
    keeping: when the line wrapped, a connector landed at the end of one row and
    the stage it belonged to at the start of the next, which is the reading-order
    confusion this panel exists to remove.
    """
    row = _CSS[_CSS.index(".dash-activity {"):]
    row = row[:row.index("}")]
    assert "display: grid" in row
    assert "repeat(4, minmax(0, 1fr) auto) minmax(0, 1fr)" in row
    assert "flex-wrap" not in row

    stage = _CSS[_CSS.index(".dash-activity__stage {"):]
    stage = stage[:stage.index("}")]
    assert "min-width: 0" in stage, "a grid child must be allowed to shrink"

    # Anchor on the rule itself and walk BACK to its media query: the stylesheet
    # holds more than one 900px block, and indexing the first would assert
    # against somebody else's breakpoint.
    stack_at = _CSS.index(".dash-activity { grid-template-columns: minmax(0, 1fr); }")
    guard = _CSS.rfind("@media", 0, stack_at)
    assert _CSS[guard:guard + 40].startswith("@media (max-width: 900px)"), \
        _CSS[guard:guard + 60]
    narrow = _CSS[guard:_CSS.index("\n}", stack_at)]
    assert ".dash-activity__stage { width: 100%; }" in narrow
    assert ".dash-activity__conv" in narrow


def test_22_the_lifecycle_sections_still_render_in_order_and_do_no_arithmetic():
    """The three panels, in order, and not one division among the new code.

    A rate computed in the browser is a rate nobody governs, which is why
    PR-ADS-155 forbade division in the cohort renderer. The same rule now covers
    the activity renderer: every number it shows was decided by the canonical
    funnel service.
    """
    order = [_APP.index(f"{name}(d)}}") for name in (
        "${renderDashLifecycleActivity", "${renderDashLifecycleCohort",
        "${renderDashCommercialOutcomes")]
    assert order == sorted(order), "the panels are rendered out of order"

    src = _strip_comments(_activity_renderer())
    # Strip the strings the markup needs before looking for arithmetic: class
    # names and closing tags carry slashes and hyphens that are not operators.
    stripped = src.replace("</", "<").replace("dash-activity", "")
    assert "/" not in stripped, "the activity renderer must contain no division"
    assert "renderDashLifecycleActivity" in _APP
    assert "function dashConversion(" not in _APP
