"""PR-ADS-177A-F3 — truth audit round 3 on the one SQL count.

Each case fails on ``4fc3f8a`` (PR-ADS-177A-F2) or under the mutation its
docstring names; ``docs/59`` §12 records the evidence.

* MINOR 1 — pseudo / email rows in the LEGACY `leads` table still made the
  revenue verdict UNSAFE ("Revenue is not ready"), though no figure reads it.
* MINOR 2 — the first-day exemption trusted the consumers' word, and its
  unanimity rule was guarded only through one-consumer identities.
* MINOR 3 — leftovers: the mapping workbench's legacy labels, the Revenue
  Health cohort chip's as-of note, and its last day on every window.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_177a_f1_truth_audit import _cohort  # noqa: E402
from tests.test_pr_ads_177a_f2_truth_audit import (  # noqa: E402
    _BARE_SQL, _FIRST_DAYS, _fn, _healthy_parity_at, _literals, _render,
)


# ══ MINOR 1 — legacy-table pollution is reported, never a revenue blocker ═══
def test_a_polluted_legacy_rows_with_a_published_ledger_are_safe_and_reported():
    import tests.test_revenue_attribution_audit as t
    pollution = {**t._pollution(clean=False), "email_campaign_rows": ["newsletter"]}
    a = json.loads(json.dumps(t._audit(pollution=pollution), default=str))
    assert a["canonical_revenue_status"] == "canonical_available"
    assert a["verdict"] == "SAFE" and a["blockers"] == []
    assert a["pseudo_campaign_rows"] == ["(direct)"]
    assert a["email_campaign_rows"] == ["newsletter"]
    assert len(a["legacy_table_findings"]) == 2
    assert all("read by no figure" in f for f in a["legacy_table_findings"])
    html = _render("renderRevenueHealth(%s)" % json.dumps(a))
    assert "Revenue is not ready" not in html and "Revenue Ready" in html
    assert "Clean pseudo/email" not in html
    chip = html[html.index("Campaign pollution"):]
    assert "2 legacy leads-table row(s)" in chip[:chip.index("</div>")]


# ══ MINOR 2 — the first-day exemption ═══════════════════════════════════════
def test_b_the_exemption_needs_every_consumer_and_an_empty_settled_range():
    """Identity level, two consumers. Fails under `all(` → `any(` in
    `_no_settled_day_exempt`, and under dropping its `settled_empty` term."""
    from services.canonical_contract import NO_SETTLED_DAY_IN_WINDOW as NSD
    from services.cross_page_parity_service import _no_settled_day_exempt as exempt
    both = [{"consumer": "a", "unavailable_reason": NSD},
            {"consumer": "b", "unavailable_reason": NSD}]
    assert exempt(both, True) is True
    assert exempt([both[0], {"consumer": "b", "unavailable_reason": "other"}], True) is False
    assert exempt([both[0], {"consumer": "b", "unavailable_reason": None}], True) is False
    assert exempt([both[0], {"consumer": "b"}], True) is False
    assert exempt(both, False) is False                 # a settled day exists
    assert exempt([], True) is False


def _declare_no_settled_day(built):
    """Every dashboard/countries identity: no value, `no_settled_day_in_window`."""
    from services import cross_page_parity_service as parity
    payload = built["dashboard/countries"]["payload"]
    for key, spec in parity.METRIC_IDENTITIES.items():
        for consumer, path in spec["consumers"]:
            if consumer != "dashboard/countries":
                continue
            node = payload
            *parents, leaf = path.split(".")
            for p in parents:
                node = node[p]
            node[leaf] = None
            truth = payload["metric_truth"].setdefault(key, {})
            payload["metric_truth"][key] = {**truth, "truth_status": "not_ready",
                                            "unavailable_reason": "no_settled_day_in_window"}
    return built


def test_c_a_consumer_claiming_no_settled_day_on_a_settled_day_is_a_violation(monkeypatch):
    """Mid-quarter (a settled range exists): the consumers' word is not enough.
    On `4fc3f8a` every one of these identities was exempted."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    parity = _healthy_parity_at(monkeypatch, now, mutate=_declare_no_settled_day)
    r = parity.audit_window("current_quarter", now=now)
    by = {m["metric"]: m for m in r["metrics"]}
    for key in ("country_attributed_spend_usd", "country_attributed_sqls",
                "country_unattributed_residual_sqls"):
        assert by[key]["status"] != "not_evaluable_no_settled_day", key
    assert "canonical_source_unavailable" in r["violation_codes"]
    assert "consumer_certified_nothing" in r["violation_codes"]
    cert = {c["consumer"]: c for c in r["consumer_certification"]}
    assert "not_evaluable_reason" not in cert["dashboard/countries"]


@pytest.mark.parametrize("window,now", _FIRST_DAYS)
def test_d_the_real_first_day_is_still_exempt(monkeypatch, window, now):
    """The positive control for test_c: the same declaration on a real first
    day (the audit's own settled range is empty) is not a violation."""
    parity = _healthy_parity_at(monkeypatch, now, mutate=_declare_no_settled_day)
    r = parity.audit_window(window, now=now)
    assert r["ok"] is True, r["violation_codes"]


# ══ MINOR 3 — leftovers ═════════════════════════════════════════════════════
def test_e_the_mapping_workbench_labels_its_legacy_counts():
    src = _fn("renderCampaignMappingCard")
    assert "${LEAD_STATUS_QUALIFIED_LABEL}" in src
    assert "Leads · legacy leads table" in src
    for text in _literals(src):
        assert not _BARE_SQL.search(text), text[:120]


def _health(cohort, key="last_6_months"):
    a = {"verdict": "SAFE", "canonical_revenue_status": "canonical_available",
         "revenue_window_status": "has_revenue", "window": {"key": key},
         "lead_cohort": {"published": cohort["sql_status"] == "published",
                         "status": cohort["sql_status"], "reason": cohort["sql_reason"],
                         "cohort": cohort},
         "window_comparison": {}, "window_cohorts": {}}
    html = _render("renderRevenueHealth(%s)" % json.dumps(a))
    chip = html[html.index("Leads / SQLs (acquisition cohort)"):]
    return chip[:chip.index("</div>")]


@pytest.mark.parametrize("key", ["current_quarter", "last_quarter", "last_6_months",
                                 "ytd", "all_time"])
def test_f_the_cohort_chip_states_its_last_day_on_every_window_and_as_of_when_stale(key):
    fresh = _health(_cohort(end_date="2026-11-24"), key)
    assert "Published" in fresh and "through 24 Nov 2026" in fresh
    assert "measured as of" not in fresh
    stale = _health(_cohort(stale=True), key)
    assert "measured as of" in stale


def test_g_the_audit_carries_the_selected_windows_own_cohort():
    import tests.test_revenue_attribution_audit as t
    for window in ("last_quarter", "last_6_months"):
        a = json.loads(json.dumps(t._audit(window=window), default=str))
        c = a["lead_cohort"]["cohort"]
        assert c["sql_status"] == "published", window
        assert c["window"]["end_date"] == a["window"]["end_date"], window
