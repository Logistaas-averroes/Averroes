"""PR-ADS-161D — canonical closed-won deals, customers and revenue.

What this suite will not accept
-------------------------------
* a won deal counted as a customer, or one deal counted twice;
* a number where the status says withheld or unavailable — ``None`` only;
* a finite-window count that silently drops an undated won deal;
* revenue published while any deal in the window lacks proven USD;
* a deal placed on a campaign by anything but the canonical resolver;
* a won deal disappearing because it has no contact, campaign or source.

Pure cases inject the universe; PostgreSQL cases seed the REAL ledger writer
(``db.deal_ledger_repository.upsert_deal``), the real sync-state recorder and
the real contact funnel, then read everything back through the service. No
production total is hardcoded.
"""

from __future__ import annotations

import ast
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import tests.conftest as conftest  # noqa: E402,F401  (import-order guard)
import analysis.closed_won_truth as cwt  # noqa: E402
import services.canonical_customer_revenue_service as svc  # noqa: E402
from tests.canonical_ledger_fixtures import (  # noqa: E402
    READY_SYNC_STATE, ledger_row,
)
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

#: A fixed reference instant: inside Q4 2026, so current_quarter starts Oct 1.
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
Q4 = "2026-10-02T09:00:00+00:00"          # inside current_quarter and 7d
Q3 = "2026-08-15T09:00:00+00:00"          # last_quarter
BUSINESS = svc.WINDOW_BUSINESS
EVIDENCE = svc.WINDOW_EVIDENCE


def _definition_rows(rows):
    return [{"deal_id": r["deal_id"], "deal_stage_id": r["deal_stage_id"],
             "hs_is_closed_won": r["hs_is_closed_won"],
             "deal_close_date": r["deal_close_date"]} for r in rows]


def _universe(rows, *, findings=None, contacts=None, definition_rows=None,
              sync_state=READY_SYNC_STATE):
    return {"available": True, "won_rows": rows,
            "won_definition_rows": (definition_rows if definition_rows
                                    is not None else _definition_rows(rows)),
            "acquisition_contacts": contacts or [],
            "sync_state": sync_state,
            "coverage_findings": [] if findings is None else findings}


def _truth(rows, windows=((BUSINESS, "current_quarter"),), *, resolver=None,
           **kw):
    return svc.get_closed_won_truth(
        list(windows), now=NOW, universe=_universe(rows, **kw),
        resolver=resolver, resolver_detail="test")


def _one(rows, window=(BUSINESS, "current_quarter"), **kw):
    return _truth(rows, (window,), **kw)["windows"][0]


def _status(w, metric):
    return w["publication"][metric]["status"]


def _resolver(mapping):
    """A stand-in for Campaign Evidence's resolver: label → (kind, key)."""
    return lambda label: mapping.get(label, ("unmatched", label))


# ═════════════════════════════════════════════════════════════════════════════
# §1 — deals, deduplication and the won definition
# ═════════════════════════════════════════════════════════════════════════════

def test_01_a_won_deal_is_counted_once_and_is_never_a_customer():
    w = _one([ledger_row("1", deal_close_date=Q4)])
    assert w["outcomes"]["closed_won_deals"] == 1
    assert _status(w, "closed_won_deals") == cwt.PUBLISHED
    # No company identity exists in the repository: customers are withheld,
    # and the lower bound is unknown — not "zero customers".
    assert w["outcomes"]["customers"] is None
    assert w["outcomes"]["confirmed_customer_lower_bound"] is None
    assert w["publication"]["customers"] == {
        "status": cwt.WITHHELD, "reason": cwt.R_CUSTOMER_IDENTITY_NOT_INGESTED}


def test_02_the_won_flag_disagreeing_with_the_won_stage_withholds_the_count():
    """The flag is the predicate (docs/35 §3); the stage cross-checks it."""
    other_stage = ledger_row("1", deal_close_date=Q4, deal_stage_id="999")
    w = _one([other_stage])
    assert _status(w, "closed_won_deals") == cwt.WITHHELD
    assert w["publication"]["closed_won_deals"]["reason"] == \
        cwt.R_WON_DEFINITION_CONFLICT
    assert w["outcomes"]["closed_won_deals"] is None

    # The won STAGE without the won flag is a conflict too — never revenue.
    stage_only = {"deal_id": "2", "deal_stage_id": cwt.CONFIRMED_WON_STAGE_ID,
                  "hs_is_closed_won": None, "deal_close_date": Q4}
    w = _one([ledger_row("1", deal_close_date=Q4)],
             definition_rows=_definition_rows(
                 [ledger_row("1", deal_close_date=Q4)]) + [stage_only])
    assert _status(w, "closed_won_deals") == cwt.WITHHELD
    assert w["membership"]["deal_ids"] == ["1"], "stage alone adds no deal"


def test_03_a_conflict_outside_the_window_does_not_touch_it():
    conflict = {"deal_id": "9", "deal_stage_id": cwt.CONFIRMED_WON_STAGE_ID,
                "hs_is_closed_won": False, "deal_close_date": Q3}
    rows = [ledger_row("1", deal_close_date=Q4)]
    w = _one(rows, definition_rows=_definition_rows(rows) + [conflict])
    assert _status(w, "closed_won_deals") == cwt.PUBLISHED


def test_04_the_confirmed_won_stage_is_the_connectors_won_stage():
    from connectors.hubspot_pull import DEAL_STAGE_MAP, WON_DEAL_STAGES
    assert WON_DEAL_STAGES == [cwt.CONFIRMED_WON_STAGE_ID]
    assert cwt.CONFIRMED_WON_STAGE_LABEL in str(
        DEAL_STAGE_MAP.get(cwt.CONFIRMED_WON_STAGE_ID))


# ═════════════════════════════════════════════════════════════════════════════
# §2 — customers are proven identities, never deals
# ═════════════════════════════════════════════════════════════════════════════

def _with_companies(rows, companies):
    return cwt.evaluate_window(
        won_rows=rows, definition_rows=_definition_rows(rows),
        contacts_by_deal={}, start=None, end=NOW + timedelta(days=1),
        is_all_time=True, now=NOW, coverage_findings=[],
        company_ids_by_deal=companies)


def test_10_one_company_with_two_won_deals_is_two_deals_and_one_customer():
    rows = [ledger_row("1"), ledger_row("2")]
    out = _with_companies(rows, {"1": ["C1"], "2": ["C1"]})
    assert out["outcomes"]["closed_won_deals"] == 2
    assert out["outcomes"]["customers"] == 1
    assert out["publication"]["customers"]["status"] == cwt.PUBLISHED


def test_11_two_paths_to_one_company_is_one_resolved_customer():
    assert cwt.customer_identity_state(["C1", "C1"], source_available=True) \
        == cwt.CUSTOMER_RESOLVED_MULTI_PATH
    out = _with_companies([ledger_row("1")], {"1": ["C1", "C1"]})
    assert out["outcomes"]["customers"] == 1


def test_12_a_deal_without_a_company_withholds_the_customer_count():
    rows = [ledger_row("1"), ledger_row("2")]
    out = _with_companies(rows, {"1": ["C1"], "2": []})
    assert out["outcomes"]["customers"] is None
    assert out["outcomes"]["confirmed_customer_lower_bound"] == 1
    assert out["outcomes"]["unresolved_customer_identity"] == 1
    assert out["coverage"]["customer_identity"] == {
        cwt.CUSTOMER_RESOLVED_SINGLE: 1, cwt.CUSTOMER_NO_COMPANY: 1}


def test_13_a_deal_with_two_companies_is_ambiguous_not_two_customers():
    out = _with_companies([ledger_row("1")], {"1": ["C1", "C2"]})
    assert out["outcomes"]["customers"] is None
    assert out["coverage"]["customer_identity"] == {
        cwt.CUSTOMER_AMBIGUOUS: 1}


def test_14_no_identity_is_ever_invented_from_name_email_or_campaign():
    """Only a company id counts — the resolver takes nothing else."""
    src = (_ROOT / "analysis/closed_won_truth.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef)
              and n.name == "customer_identity_state")
    body = ast.get_source_segment(src, fn)
    for forbidden in ("deal_name", "company_name", "email", "campaign",
                      "contact_id"):
        assert f'"{forbidden}"' not in body and f"'{forbidden}'" not in body


# ═════════════════════════════════════════════════════════════════════════════
# §3 — dates, amounts and currency
# ═════════════════════════════════════════════════════════════════════════════

def test_20_an_undated_won_deal_withholds_finite_windows_and_counts_in_all_time():
    rows = [ledger_row("1", deal_close_date=Q4),
            ledger_row("2", deal_close_date=None)]
    t = _truth(rows, ((BUSINESS, "current_quarter"), (BUSINESS, "all_time"),
                      (EVIDENCE, "all_time")))
    finite, all_b, all_e = t["windows"]
    assert _status(finite, "closed_won_deals") == cwt.WITHHELD
    assert finite["publication"]["closed_won_deals"]["reason"] == \
        cwt.R_UNDATED_WON_DEALS
    assert finite["outcomes"]["closed_won_deals"] is None
    assert finite["outcomes"]["closed_won_deals_confirmed_in_window"] == 1
    assert finite["membership"]["undated_won_deals"] == 1
    for w in (all_b, all_e):
        assert w["outcomes"]["closed_won_deals"] == 2
        assert sorted(w["membership"]["deal_ids"]) == ["1", "2"]


def test_21_no_ingestion_or_sync_time_ever_dates_a_deal():
    """An undated deal stays undated, whatever other timestamps it carries."""
    row = ledger_row("2", deal_close_date=None, deal_created_at=Q4)
    w = _one([row])
    assert w["membership"]["deal_ids"] == []
    assert cwt.close_date_state(row, NOW) == cwt.CLOSE_MISSING


def test_22_windows_are_half_open():
    b = svc.window_bounds(BUSINESS, "current_quarter", NOW)
    at_start = ledger_row("s", deal_close_date=b["start"].isoformat())
    at_end = ledger_row("e", deal_close_date=b["end"].isoformat())
    just_before_end = ledger_row(
        "b", deal_close_date=(b["end"] - timedelta(seconds=1)).isoformat())
    w = _one([at_start, at_end, just_before_end])
    assert w["membership"]["deal_ids"] == ["b", "s"]


def test_23_a_future_close_date_is_disclosed_and_in_no_window():
    future = ledger_row("f", deal_close_date="2026-12-01T00:00:00+00:00")
    w = _one([future], (BUSINESS, "all_time"))
    assert w["membership"]["deal_ids"] == []
    assert w["coverage"]["close_date"][cwt.CLOSE_INVALID] == 1


@pytest.mark.parametrize("override,state", [
    ({"amount_raw": 500.0}, cwt.AMOUNT_POSITIVE),
    ({"amount_raw": 0.0}, cwt.AMOUNT_ZERO),
    ({"amount_raw": None}, cwt.AMOUNT_MISSING),
    ({"amount_raw": "abc"}, cwt.AMOUNT_INVALID),
    ({"amount_raw": -10.0}, cwt.AMOUNT_NEGATIVE),
])
def test_24_amount_states_are_told_apart(override, state):
    assert cwt.amount_state(ledger_row("1", **override)) == state


def test_25_a_missing_amount_withholds_revenue_but_not_the_deal_count():
    rows = [ledger_row("1", deal_close_date=Q4),
            ledger_row("2", deal_close_date=Q4, amount_raw=None,
                       revenue_usd=None, currency_status="unavailable",
                       currency_reason="no_amount")]
    w = _one(rows)
    assert w["outcomes"]["closed_won_deals"] == 2
    assert w["outcomes"]["revenue_usd"] is None
    assert w["outcomes"]["revenue_usd_confirmed_subset"] == 1000.0
    assert w["publication"]["revenue_usd"] == {
        "status": cwt.WITHHELD, "reason": cwt.R_REVENUE_UNPROVEN}
    assert w["coverage"]["currency"][cwt.CURRENCY_NOT_APPLICABLE] == 1


def test_26_a_zero_amount_is_a_real_zero_not_a_gap():
    rows = [ledger_row("1", deal_close_date=Q4),
            ledger_row("2", deal_close_date=Q4, amount_raw=0.0,
                       revenue_usd=0.0)]
    w = _one(rows)
    assert w["outcomes"]["revenue_usd"] == 1000.0
    assert _status(w, "revenue_usd") == cwt.PUBLISHED
    assert w["coverage"]["amount"][cwt.AMOUNT_ZERO] == 1


def test_27_non_usd_with_proven_fx_is_published_and_without_it_is_withheld():
    converted = ledger_row("1", deal_close_date=Q4, deal_currency_code="GBP",
                           amount_raw=800.0, revenue_usd=1012.5,
                           currency_status="converted",
                           currency_reason="converted_at_close_date_fx")
    w = _one([converted])
    assert w["outcomes"]["revenue_usd"] == 1012.5
    assert w["coverage"]["currency"] == {cwt.CURRENCY_PROVEN_FX: 1}

    no_fx = ledger_row("2", deal_close_date=Q4, deal_currency_code="GBP",
                       amount_raw=800.0, revenue_usd=None,
                       currency_status="unavailable",
                       currency_reason="no_fx_rate_for_close_date")
    w = _one([converted, no_fx])
    assert w["outcomes"]["revenue_usd"] is None
    assert w["coverage"]["currency"][cwt.CURRENCY_MISSING_FX] == 1


def test_28_an_unrecognised_currency_status_is_never_usd():
    row = ledger_row("1", currency_status="mystery", currency_reason=None)
    assert cwt.currency_state(row) == cwt.CURRENCY_UNSUPPORTED
    assert cwt.revenue_is_proven(row) is False


# ═════════════════════════════════════════════════════════════════════════════
# §4 — attribution: a partition that always adds up
# ═════════════════════════════════════════════════════════════════════════════

def _attr_rows():
    return [
        # Google Ads, label resolves to a campaign.
        ledger_row("c", deal_close_date=Q4, gclid="g1",
                   campaign_name_raw="Brand - UK", acquisition_group="google_ads",
                   attribution_status="attributed"),
        # Google Ads, label approved as not Google Ads.
        ledger_row("x", deal_close_date=Q4, gclid="g2",
                   campaign_name_raw="newsletter", acquisition_group="google_ads",
                   attribution_status="attributed"),
        # Google Ads, label not mapped.
        ledger_row("u", deal_close_date=Q4, gclid="g3",
                   campaign_name_raw="Mystery Label",
                   acquisition_group="google_ads",
                   attribution_status="attributed"),
        # Conflicting contacts.
        ledger_row("a", deal_close_date=Q4, attribution_status="ambiguous",
                   association_status="ambiguous", association_count=2),
        # No contact at all.
        ledger_row("n", deal_close_date=Q4, association_status="none",
                   association_count=0),
        # Another proven source.
        ledger_row("o", deal_close_date=Q4, acquisition_group="organic",
                   attribution_status="attributed"),
    ]


RESOLVER = _resolver({"Brand - UK": ("google_ads", "111"),
                      "newsletter": ("not_google_ads", None)})


def test_30_every_won_deal_lands_in_exactly_one_bucket():
    w = _one(_attr_rows(), resolver=RESOLVER)
    cov = w["coverage"]["campaign_attribution"]
    assert cov == {cwt.BUCKET_CAMPAIGN: 1, cwt.BUCKET_GOOGLE_ADS_UNPLACED: 1,
                   cwt.BUCKET_EXCLUDED: 1, cwt.BUCKET_OTHER_SOURCE: 1,
                   cwt.BUCKET_UNATTRIBUTED: 1, cwt.BUCKET_AMBIGUOUS: 1}
    assert sum(cov.values()) == w["outcomes"]["closed_won_deals"] == 6
    assert w["attribution"]["all_source"]["deals"] == 6


def test_31_conflicting_campaign_contacts_are_ambiguous_never_split():
    w = _one(_attr_rows(), resolver=RESOLVER)
    assert w["attribution"]["partition"][cwt.BUCKET_AMBIGUOUS]["deals"] == 1
    assert w["attribution"]["by_campaign"] == {
        "111": {"deals": 1, "revenue_usd": 1000.0,
                "revenue_usd_confirmed_subset": 1000.0,
                "deals_without_proven_usd": 0}}


def test_32_unattributed_and_contactless_deals_stay_in_all_source():
    w = _one(_attr_rows(), resolver=RESOLVER)
    un = w["attribution"]["partition"][cwt.BUCKET_UNATTRIBUTED]
    assert un["deals"] == 1 and un["reasons"] == {"no_associated_contact": 1}
    assert w["outcomes"]["revenue_usd"] == 6000.0


def test_33_without_the_resolver_no_deal_is_placed_on_a_campaign():
    """A raw label is not linkage — campaign revenue is withheld instead."""
    w = _one(_attr_rows(), resolver=None)
    assert w["coverage"]["campaign_attribution"][cwt.BUCKET_CAMPAIGN] == 0
    assert w["attribution"]["by_campaign"] is None
    assert w["publication"]["campaign_revenue"] == {
        "status": cwt.WITHHELD, "reason": cwt.R_CAMPAIGN_IDENTITY_UNAVAILABLE}


def test_34_production_binds_campaign_evidences_own_resolver():
    src = (_ROOT / "services/canonical_customer_revenue_service.py") \
        .read_text(encoding="utf-8")
    assert "_assign_lead" in src and "fuzzy" in src
    assert "fuzzy_match_score" not in src


def test_35_roas_and_cac_are_never_published():
    w = _one(_attr_rows(), resolver=RESOLVER)
    for metric in ("roas", "cac"):
        assert _status(w, metric) == cwt.NOT_PUBLISHED


# ═════════════════════════════════════════════════════════════════════════════
# §5 — the acquisition cohort is a different question
# ═════════════════════════════════════════════════════════════════════════════

def _contact(deal, cid, created):
    return {"deal_id": deal, "contact_id": cid,
            "contact_created_at": created, "funnel_row_present":
            created is not None}


def test_40_a_deal_is_in_an_acquisition_window_only_if_every_contact_is():
    rows = [ledger_row("m", deal_close_date=Q4),
            ledger_row("s", deal_close_date=Q4)]
    contacts = [_contact("m", "1", "2026-10-03T00:00:00+00:00"),
                _contact("s", "2", "2026-10-03T00:00:00+00:00"),
                _contact("s", "3", Q3)]
    w = _one(rows, contacts=contacts)
    acq = w["acquisition_cohort"]
    assert acq["closed_won_deals_confirmed"] == 1
    assert acq["ambiguous_membership"] == 1
    assert acq["status"] == cwt.WITHHELD and acq["closed_won_deals"] is None


def test_41_a_contact_without_creation_time_leaves_membership_unresolved():
    rows = [ledger_row("m", deal_close_date=Q4)]
    w = _one(rows, contacts=[_contact("m", "1", None)])
    assert w["acquisition_cohort"]["unresolved_membership"] == 1
    assert w["acquisition_cohort"]["status"] == cwt.WITHHELD


def test_42_a_failed_association_lookup_is_unresolved_not_contactless():
    row = ledger_row("m", deal_close_date=Q4,
                     association_status="lookup_failed")
    assert cwt.acquisition_state([], None, NOW,
                                 association_status="lookup_failed") == \
        cwt.ACQ_UNRESOLVED
    w = _one([row])
    assert w["acquisition_cohort"]["unresolved_membership"] == 1


def test_43_acquisition_membership_never_uses_the_close_date():
    """Closed in Q4, acquired in Q3: in Q3's acquisition cohort, not Q4's."""
    rows = [ledger_row("m", deal_close_date=Q4)]
    contacts = [_contact("m", "1", Q3)]
    q4, q3 = _truth(rows, ((BUSINESS, "current_quarter"),
                           (BUSINESS, "last_quarter")),
                    contacts=contacts)["windows"]
    assert q4["outcomes"]["closed_won_deals"] == 1
    assert q4["acquisition_cohort"]["closed_won_deals"] == 0
    assert q3["acquisition_cohort"]["closed_won_deals"] == 1
    assert q3["outcomes"]["closed_won_deals"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# §6 — freshness and unavailability
# ═════════════════════════════════════════════════════════════════════════════

def test_50_unproven_ledger_coverage_makes_every_metric_unavailable():
    finding = [{"code": "post_bootstrap_incremental_missing",
                "message": "no incremental"}]
    w = _one([ledger_row("1", deal_close_date=Q4)], findings=finding)
    for metric in ("closed_won_deals", "revenue_usd", "customers",
                   "campaign_revenue"):
        assert _status(w, metric) == cwt.UNAVAILABLE
    assert w["outcomes"]["closed_won_deals"] is None
    assert w["outcomes"]["revenue_usd"] is None
    assert w["coverage"]["source_freshness"]["coverage_proven"] is False
    assert w["coverage"]["source_freshness"][
        "latest_successful_incremental_at"] is None


def test_51_freshness_is_sync_coverage_never_the_newest_deal():
    rows = [ledger_row("1", deal_close_date="2026-10-07T11:00:00+00:00")]
    fresh = _one(rows)["coverage"]["source_freshness"]
    assert fresh["latest_successful_incremental_at"] == \
        READY_SYNC_STATE["last_incremental_at"]
    assert fresh["staleness_assessed"] is False
    assert fresh["staleness_threshold_hours"] is None
    assert fresh["age_hours"] > 24 * 100   # June → October, reported not judged


def test_52_an_unreadable_ledger_is_unavailable_everywhere_never_zero():
    t = svc.get_closed_won_truth(
        now=NOW, universe={"available": False}, resolver=None,
        resolver_detail="test")
    assert t["available"] is False
    for w in t["windows"]:
        assert all(v is None for v in w["outcomes"].values())
        assert _status(w, "closed_won_deals") == cwt.UNAVAILABLE


def test_53_every_supported_window_is_evaluated():
    t = _truth([ledger_row("1", deal_close_date=Q4)], svc.all_windows())
    keys = [(w["window"]["window_type"], w["window"]["window_key"])
            for w in t["windows"]]
    assert keys == [(EVIDENCE, k) for k in
                    ("7d", "14d", "30d", "60d", "180d", "all_time")] + \
        [(BUSINESS, k) for k in ("current_quarter", "last_quarter",
                                 "last_6_months", "ytd", "all_time")]


# ═════════════════════════════════════════════════════════════════════════════
# §7 — the audit catches a broken contract (counterfactuals)
# ═════════════════════════════════════════════════════════════════════════════

def _audit_window(w, rows):
    from scripts import audit_customer_closed_won_truth as audit
    a = audit.Audit()
    audit.window_checks(a, w, _universe(rows), now=NOW)
    return a


def test_60_the_audit_passes_a_correct_window():
    rows = _attr_rows()
    w = _one(rows, resolver=RESOLVER)
    assert _audit_window(w, rows).violations == []


@pytest.mark.parametrize("mutate,check", [
    # publish revenue over an unpriced deal
    (lambda w: (w["publication"]["revenue_usd"].update(status="published"),
                w["outcomes"].update(revenue_usd=1.0)),
     "revenue_only_published_when_every_deal_proven"),
    # a withheld metric carrying a number
    (lambda w: w["outcomes"].update(revenue_usd=999.0),
     "withheld_is_never_a_number"),
    # a deal counted twice
    (lambda w: w["membership"]["deal_ids"].append(
        w["membership"]["deal_ids"][0]), "deal_ids_unique"),
    # a bucket dropped from the partition
    (lambda w: w["coverage"]["campaign_attribution"].update(unattributed=0),
     "attribution_partition_reconciles"),
    # ROAS published
    (lambda w: w["publication"]["roas"].update(status="published"),
     "roas_and_cac_not_published"),
])
def test_61_each_contract_breach_is_caught(mutate, check):
    rows = _attr_rows() + [ledger_row("p", deal_close_date=Q4,
                                      amount_raw=None, revenue_usd=None,
                                      currency_status="unavailable",
                                      currency_reason="no_amount")]
    w = _one(rows, resolver=RESOLVER)
    assert _audit_window(w, rows).violations == []
    mutate(w)
    a = _audit_window(w, rows)
    assert a.checks.get(check) is False, (check, a.violations)


def test_62_a_finite_window_published_over_an_undated_deal_is_caught():
    rows = [ledger_row("1", deal_close_date=Q4),
            ledger_row("2", deal_close_date=None)]
    w = _one(rows)
    w["publication"]["closed_won_deals"]["status"] = cwt.PUBLISHED
    w["outcomes"]["closed_won_deals"] = 1
    assert _audit_window(w, rows).checks["missing_close_dates_disclosed"] \
        is False


def test_63_the_audit_has_no_external_or_write_path_and_no_consumer():
    from scripts import audit_customer_closed_won_truth as audit
    a = audit.Audit()
    out = audit.structural_checks(a)
    assert a.violations == [], a.violations
    assert out["production_consumers"] == []


def test_64_the_structural_check_catches_a_production_consumer(monkeypatch):
    """Counterfactual: a page importing the service must fail check 17."""
    from scripts import audit_customer_closed_won_truth as audit
    real = Path.read_text

    def fake(self, *a, **k):
        text = real(self, *a, **k)
        if self.name == "server.py":
            text += "\nimport services.canonical_customer_revenue_service\n"
        return text

    monkeypatch.setattr(Path, "read_text", fake)
    a = audit.Audit()
    audit.structural_checks(a)
    assert a.checks["no_production_page_changed"] is False


# ═════════════════════════════════════════════════════════════════════════════
# §8 — against a real PostgreSQL schema
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def ledger(pg, monkeypatch):  # noqa: F811
    """The real schema, a proven sync state, and the real ledger writer."""
    from db import deal_ledger_repository as repo

    assert repo.record_sync_state(status="success", sync_mode="bootstrap",
                                  proved_complete=True)["available"]
    assert repo.record_sync_state(status="success",
                                  sync_mode="incremental")["available"]
    return repo


def _write(repo, row, associations=()):
    res = repo.upsert_deal(row, associations=list(associations))
    assert res.get("available") is not False, res


def _q(sql, params=()):
    from db.connection import get_conn
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        try:
            return cur.fetchall()
        except Exception:  # noqa: BLE001
            return None


_TABLES = ("hubspot_deal_ledger", "hubspot_deal_contact_association",
           "hubspot_deal_sync_state", "hubspot_contact_funnel")


def _snapshot():
    return {t: _q(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' "
                  f"ORDER BY x::text), '')) FROM {t} x")[0] for t in _TABLES}


@_needs_pg
def test_70_pg_open_lost_and_unknown_deals_are_never_closed_won(ledger):
    _write(ledger, ledger_row("won", deal_close_date=Q4))
    _write(ledger, ledger_row("lost", deal_close_date=Q4,
                              hs_is_closed_won=False, deal_stage_id="x"))
    _write(ledger, ledger_row("open", deal_close_date=Q4,
                              hs_is_closed_won=None, deal_stage_id="y"))
    w = svc.get_window_outcome(BUSINESS, "current_quarter", now=NOW)
    assert w["membership"]["deal_ids"] == ["won"]
    assert w["outcomes"]["closed_won_deals"] == 1


@_needs_pg
def test_71_pg_a_rewritten_deal_and_a_multi_contact_deal_count_once(ledger):
    row = ledger_row("d1", deal_close_date=Q4, association_count=2)
    _write(ledger, row, [{"contact_id": "c1"}, {"contact_id": "c2"}])
    _write(ledger, row, [{"contact_id": "c1"}, {"contact_id": "c2"}])
    w = svc.get_window_outcome(BUSINESS, "current_quarter", now=NOW)
    assert w["membership"]["deal_ids"] == ["d1"]
    assert w["attribution"]["all_source"]["deals"] == 1


@_needs_pg
def test_72_pg_a_lifecycle_customer_without_a_won_deal_creates_nothing(ledger):
    from db import writers
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "lc", "lifecycle_stage": "customer",
        "created_at": Q4, "last_modified_at": Q4}])
    w = svc.get_window_outcome(BUSINESS, "current_quarter", now=NOW)
    assert w["outcomes"]["closed_won_deals"] == 0
    assert w["outcomes"]["revenue_usd"] == 0.0
    assert w["membership"]["deal_ids"] == []


@_needs_pg
def test_73_pg_the_acquisition_cohort_reads_real_contact_creation(ledger):
    from db import writers
    writers.upsert_hubspot_contact_funnel([{
        "contact_id": "c1", "lifecycle_stage": "customer",
        "created_at": Q3, "last_modified_at": Q3}])
    _write(ledger, ledger_row("d1", deal_close_date=Q4),
           [{"contact_id": "c1"}])
    q3 = svc.get_window_outcome(BUSINESS, "last_quarter", now=NOW)
    assert q3["acquisition_cohort"]["closed_won_deals"] == 1
    assert q3["outcomes"]["closed_won_deals"] == 0


@_needs_pg
def test_74_pg_an_unready_ledger_is_unavailable(pg):  # noqa: F811
    from db import deal_ledger_repository as repo
    repo.record_sync_state(status="success", sync_mode="bootstrap",
                           proved_complete=True)
    _write(repo, ledger_row("d1", deal_close_date=Q4))
    w = svc.get_window_outcome(BUSINESS, "current_quarter", now=NOW)
    assert _status(w, "closed_won_deals") == cwt.UNAVAILABLE
    assert w["outcomes"]["closed_won_deals"] is None


@_needs_pg
def test_75_pg_the_universe_read_is_read_only_by_postgresql(ledger):
    import psycopg2
    from db.connection import get_conn
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        with get_conn() as c, c.cursor() as cur:
            cur.execute(ledger.CLOSED_WON_UNIVERSE_TRANSACTION)
            cur.execute("DELETE FROM hubspot_deal_ledger")


@_needs_pg
def test_76_pg_the_audit_cli_holds_writes_nothing_and_cross_checks_sql(
        ledger, monkeypatch):
    from scripts import audit_customer_closed_won_truth as audit
    _write(ledger, ledger_row("d1", deal_close_date=Q4))
    _write(ledger, ledger_row("d2", deal_close_date=Q3, amount_raw=None,
                              revenue_usd=None, currency_status="unavailable",
                              currency_reason="no_amount"))
    _write(ledger, ledger_row("d3", deal_close_date=None),
           [{"contact_id": "c9"}])
    before = _snapshot()
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = audit.main(["--json"])
    out = json.loads(buf.getvalue())
    assert _snapshot() == before, "the audit changed the database"
    assert code == audit.EXIT_OK, out["violations"]
    assert out["verdict"] == "contracts_hold"
    assert out["external_writes_performed"] is False
    assert out["database_writes_performed"] is False
    assert out["hubspot_calls_performed"] is False
    assert {c["window"] for c in out["sql_cross_checks"]} == {
        "current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"}
    for c in out["sql_cross_checks"]:
        assert c["production_read_deals"] == c["service_confirmed_in_window"]


@_needs_pg
def test_77_pg_the_human_readable_audit_renders(ledger):
    from scripts import audit_customer_closed_won_truth as audit
    _write(ledger, ledger_row("d1", deal_close_date=Q4))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = audit.main([])
    text = buf.getvalue()
    assert code == audit.EXIT_OK
    assert "won deals" in text and "missing close date" in text
    assert "company association" in text or "customer identity" in text


def test_78_ci_runs_this_suite_in_the_postgresql_step_and_asserts_it_ran():
    wf = (_ROOT / ".github/workflows/pr-ads-153d-checks.yml").read_text(
        encoding="utf-8")
    assert "tests/test_pr_ads_161d_customer_closed_won_truth.py" in wf
    assert '"tests.test_pr_ads_161d_customer_closed_won_truth"' in wf
