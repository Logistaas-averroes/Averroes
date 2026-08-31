"""
tests/test_pr_ads_155_f1_cli_init_and_scoped_provenance.py

PR-ADS-155-F1 — four bounded production defects, and the guards against them.

  A  Both new standalone CLIs initialize AND probe the database themselves.
     They shipped without it, so `python -m scripts.report_missing_deal_amounts`
     reported `canonical_coverage_not_proven` over a perfectly healthy ledger:
     nothing had called `init_pool` in that process, so every `get_conn` yielded
     None and the sync-state read came back empty. The command turned "we never
     opened the database" into an assertion about the DATA.

  B  Scoped revenue contracts derive readiness from their OWN scoped population.
     `country_attributed_won_revenue_usd` declared `truth_status: ready` beside a
     null value — a contract contradicting the number it describes.

  C  The parity audit no longer falls back to the generic
     `canonical_source_unavailable` when every consumer names the real blocker.

  D  Lifecycle recovery tells four HubSpot evidence states apart, instead of
     reporting all of them as "no history version for stage".

The CLI tests run the commands as SUBPROCESSES, exactly as documented. Calling
`main()` in-process while the suite has already initialized a pool is precisely
how this defect escaped: the test passed because the harness had done the thing
the command forgot to do.

Run with:
    python -m pytest tests/test_pr_ads_155_f1_cli_init_and_scoped_provenance.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

from services import canonical_revenue_service as canonical_revenue  # noqa: E402
from services import cross_page_parity_service as parity  # noqa: E402
from services import lifecycle_history_recovery_service as recovery  # noqa: E402
from connectors import hubspot_pull as hubspot  # noqa: E402

_REPORT_CMD = "scripts.report_missing_deal_amounts"
_LIFECYCLE_CMD = "scripts.backfill_lifecycle_stage_history"

_SECRET = "hunter2"
_POISONED_DSN = f"postgresql://appuser:{_SECRET}@127.0.0.1:1/nope"


def _run_module(module: str, *args, env_extra=None, timeout=180):
    """Run `python -m <module>` as a real subprocess.

    A fresh interpreter, so nothing has initialized the connection pool on the
    command's behalf. That is the whole point: the production defect was
    invisible to any test that shared a process with the pool.
    """
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(_ROOT)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, "-m", module, *args],
                          cwd=str(_ROOT), env=env, capture_output=True,
                          text=True, timeout=timeout)


# ═════════════════════════════════════════════════════════════════════════════
# A — standalone CLI database initialization
# ═════════════════════════════════════════════════════════════════════════════
def test_1_missing_amount_report_initializes_the_database_itself(pg):
    """The production scenario exactly: a HEALTHY ledger, read with no wrapper.

    The seed matters. Against an empty database `canonical_coverage_not_proven`
    is the correct answer — no sync has ever run — so an unseeded run would pass
    this test even with the defect still present. Production's ledger had a
    completed bootstrap and 181 deals, and the command still claimed unproven
    coverage, because it was reporting on a database it had never opened.
    """
    from tests.test_pr_ads_154c_parity_pg import _DEAL_SQL, _READY_SYNC_SQL, _exec

    _exec(_READY_SYNC_SQL, ("complete",))
    _exec(_DEAL_SQL, ("A", "Priced", 1000.0, "USD", 1000.0,
                      "verified_usd", "deal_currency_is_usd"))
    _exec(_DEAL_SQL, ("B", "Unpriced", None, None, None,
                      "unavailable", "no_amount"))

    res = _run_module(_REPORT_CMD, "--window", "all_time",
                      env_extra={"DATABASE_URL": pg.url})
    combined = res.stdout + res.stderr
    # The defect signature: a healthy ledger misreported as unproven coverage.
    assert "canonical_coverage_not_proven" not in combined, combined
    assert "database unavailable" not in combined, combined
    # It read the real population and reported the real blocker.
    assert "closed-won deals            2" in res.stdout, combined
    assert "with NO proven amount       1" in res.stdout, combined
    assert "closed_won_deals_missing_amount" in res.stdout, combined
    assert res.returncode == 1, combined


def test_2_lifecycle_dry_run_initializes_the_database_itself(pg):
    res = _run_module(_LIFECYCLE_CMD, "--limit", "50",
                      env_extra={"DATABASE_URL": pg.url})
    combined = res.stdout + res.stderr
    assert "database unavailable" not in combined, combined
    assert "LIFECYCLE STAGE-ENTRY RECOVERY" in res.stdout, combined
    assert "recovery_checkpoint_unreadable" not in combined, combined
    assert res.returncode == 0, combined


def test_3_neither_command_imports_the_web_application():
    """No Flask/FastAPI app startup, and no reliance on it having run."""
    for name in ("report_missing_deal_amounts", "backfill_lifecycle_stage_history"):
        src = (_ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
        for forbidden in ("from api", "import api", "api.server", "lifespan"):
            assert forbidden not in src, f"{name} must not depend on the web app"
        # It initializes explicitly, inside main() — never at import time, so
        # importing the module for a test cannot open a connection as a side effect.
        assert "_database_ready()" in src
        assert "def _database_ready" in src
        top_level = [ln for ln in src.splitlines()
                     if ln.startswith("ensure_database_ready")
                     or ln.startswith("init_pool")]
        assert not top_level, f"{name} must not initialize at import time"


@pytest.mark.parametrize("module,expected_rc", [
    (_REPORT_CMD, 2),        # EXIT_UNAVAILABLE
    (_LIFECYCLE_CMD, 1),     # EXIT_FAILED
])
def test_4_initialization_failure_exits_nonzero_without_exposing_the_dsn(
        module, expected_rc):
    """A controlled non-zero result, and never a raw exception carrying the DSN."""
    res = _run_module(module, env_extra={"DATABASE_URL": _POISONED_DSN})
    combined = res.stdout + res.stderr
    assert res.returncode == expected_rc, combined
    assert _SECRET not in combined, "the password must never reach operator output"
    assert _POISONED_DSN not in combined
    assert "Traceback" not in combined, "an uncaught exception is not a controlled exit"
    assert "database unavailable" in combined


@pytest.mark.parametrize("module", [_REPORT_CMD, _LIFECYCLE_CMD])
def test_5_database_unavailable_never_reports_zero_affected_records(module):
    """A refused read carries no population, so every count is UNKNOWN.

    Reporting "0 deals missing an amount" over a database that was never opened
    would be a fabricated all-clear — the exact class of defect this programme
    exists to remove.
    """
    res = _run_module(module, env_extra={"DATABASE_URL": _POISONED_DSN})
    combined = res.stdout + res.stderr
    assert res.returncode != 0
    assert "UNKNOWN, not zero" in combined, combined
    for fabricated in ("0 DEAL(S)", "deal_count\": 0", "contacts examined:         0"):
        assert fabricated not in combined, combined


def test_6_the_report_returns_the_production_shaped_missing_records(pg):
    """14 unpriced deals among 181 closed-won, read through the real schema."""
    from tests.test_pr_ads_154c_parity_pg import _DEAL_SQL, _READY_SYNC_SQL, _exec

    _exec(_READY_SYNC_SQL, ("complete",))
    for i in range(167):
        _exec(_DEAL_SQL, (f"P{i}", f"Priced {i}", 5259.43, "USD", 5259.43,
                          "verified_usd", "deal_currency_is_usd"))
    for i in range(14):
        _exec(_DEAL_SQL, (f"U{i}", f"Unpriced {i}", None, None, None,
                          "unavailable", "no_amount"))

    base = canonical_revenue.load_won_deals("all_time")
    report = canonical_revenue.missing_amount_deals(base)
    disclosure = canonical_revenue.revenue_disclosure(base)

    assert report["deal_count"] == 14
    assert disclosure["closed_won_deals"] == 181
    assert disclosure["revenue_proven_deals"] == 167
    assert disclosure["revenue_unavailable_deals"] == 14
    assert disclosure["total_revenue_usd"] is None
    assert disclosure["unavailable_reason"] == "closed_won_deals_missing_amount"
    # Every listed deal reports its amount as MISSING — never inferred.
    assert all(d["amount_status"] == "missing" for d in report["deals"])
    assert all(d["fallback_used"] is False for d in report["deals"])


def test_7_the_report_exits_1_when_records_are_missing(pg):
    from tests.test_pr_ads_154c_parity_pg import _DEAL_SQL, _READY_SYNC_SQL, _exec

    _exec(_READY_SYNC_SQL, ("complete",))
    _exec(_DEAL_SQL, ("OK1", "Priced", 100.0, "USD", 100.0,
                      "verified_usd", "deal_currency_is_usd"))
    _exec(_DEAL_SQL, ("BAD1", "Unpriced", None, None, None,
                      "unavailable", "no_amount"))

    res = _run_module(_REPORT_CMD, "--window", "all_time",
                      env_extra={"DATABASE_URL": pg.url})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "1 DEAL(S) TO PRICE IN HUBSPOT" in res.stdout

    js = _run_module(_REPORT_CMD, "--window", "all_time", "--json",
                     env_extra={"DATABASE_URL": pg.url})
    assert js.returncode == 1
    payload = json.loads(js.stdout)
    assert payload["report"]["deal_count"] == 1
    assert payload["revenue_disclosure"]["total_revenue_publishable"] is False


def test_8_the_lifecycle_command_is_dry_run_by_default(pg):
    res = _run_module(_LIFECYCLE_CMD, "--limit", "50", "--json",
                      env_extra={"DATABASE_URL": pg.url})
    payload = json.loads(res.stdout)
    assert payload["mode"] == "dry_run"
    assert payload["apply"] is False
    assert payload["hubspot_writes_performed"] is False
    assert payload["events_persisted"] == 0


def test_9_no_hubspot_mutation_is_reachable_from_lifecycle_recovery():
    svc = (_ROOT / "services" / "lifecycle_history_recovery_service.py").read_text()
    cli = (_ROOT / "scripts" / "backfill_lifecycle_stage_history.py").read_text()
    for src in (svc, cli):
        for verb in ("basic_api.create", "basic_api.update", "basic_api.archive",
                     "batch_api.create", "batch_api.update", "batch_api.archive",
                     "requests.post", "requests.patch", "requests.put",
                     "requests.delete"):
            assert verb not in src, f"{verb} must never appear in the recovery path"
    # The one HubSpot call it makes is a READ.
    assert "fetch_lifecycle_stage_history" in svc
    history_fn = hubspot.fetch_lifecycle_stage_history.__doc__ or ""
    assert "READ-ONLY" in history_fn


# ═════════════════════════════════════════════════════════════════════════════
# B — scoped revenue readiness proved from the scoped population
# ═════════════════════════════════════════════════════════════════════════════
def _deal(deal_id, *, amount=100.0, status="verified_usd", reason=None,
          campaign="Brand", country="United States"):
    return {
        "deal_id": deal_id, "deal_name": f"Co {deal_id}",
        "deal_close_date": "2026-02-01", "deal_stage_label": "Closed Won",
        "amount_raw": amount, "deal_currency_code": "USD" if amount else None,
        "revenue_usd": amount if status in ("verified_usd", "converted") else None,
        "currency_status": status, "currency_reason": reason,
        "campaign_name_raw": campaign, "country_raw": country,
        "acquisition_group": "google_ads", "attribution_status": "attributed",
        "association_status": "resolved",
    }


def test_10_campaign_revenue_null_is_not_ready_with_the_precise_reason():
    """A withheld scoped total names the blocker it already knows."""
    from analysis import revenue_scope

    base = {"available": True, "window": "all_time", "window_start": None,
            "window_end": None, "as_of": None,
            "deals": [_deal("p1"), _deal("u1", amount=None, status="unavailable",
                                         reason="no_amount")]}
    ladder = canonical_revenue.get_scope_ladder(base=base)
    disclosure = canonical_revenue.disclosure_from_ladder(
        ladder, revenue_scope.SCOPE_ALL_SOURCE)

    assert disclosure["total_revenue_usd"] is None
    assert disclosure["total_revenue_publishable"] is False
    assert disclosure["unavailable_reason"] == "closed_won_deals_missing_amount"
    assert disclosure["violation_codes"] == [
        canonical_revenue.V_CURRENCY_UNPROVEN_DEALS]


def test_11_country_revenue_null_cannot_publish_truth_status_ready():
    """The contradiction production showed: ready beside a null value."""
    from services import dashboard_countries_service as countries

    verdict = countries._country_revenue_total_verdict(
        [{"won_revenue_usd": 100.0}, {"won_revenue_usd": None}],
        deal_proof_available=True, revenue_connected=True, canonical={})
    assert verdict["publishable"] is False
    assert verdict["reason"] == canonical_revenue.REASON_REVENUE_INCOMPLETE
    assert verdict["violation_codes"] == [canonical_revenue.V_CURRENCY_UNPROVEN_DEALS]

    # And the source itself: the revenue metric may not take the geo-derived
    # status that produced `ready` over a withheld value.
    src = (_ROOT / "services" / "dashboard_countries_service.py").read_text()
    block = src[src.index('"metric": "country_attributed_won_revenue_usd"'):]
    block = block[:block.index('"metric": "country_attributed_customers"')]
    assert "_country_total_ok" in block
    assert "unavailable_reason" in block and "violation_codes" in block


def test_12_a_publishable_scoped_population_stays_ready():
    """A narrower scope with no unpriced deal keeps its total.

    The blocker is NOT inherited blindly from the all-source population: it is
    proved, or not, from the scope's own deals.
    """
    from services import dashboard_countries_service as countries

    verdict = countries._country_revenue_total_verdict(
        [{"won_revenue_usd": 100.0}, {"won_revenue_usd": 250.0}],
        deal_proof_available=True, revenue_connected=True, canonical={})
    assert verdict["publishable"] is True
    assert verdict["reason"] is None
    assert verdict["violation_codes"] == []


def test_13_a_scoped_population_containing_an_unpriced_deal_is_withheld():
    v = canonical_revenue.total_verdict_for_population(
        won_deals=40, unpriced_deals=1, scope="country_attributed_revenue")
    assert v["publishable"] is False
    assert v["reason"] == "closed_won_deals_missing_amount"
    assert "country_attributed_revenue" in v["detail"]

    # An unreadable population is a different verdict, with NULL counts.
    v2 = canonical_revenue.total_verdict_for_population(
        won_deals=None, unpriced_deals=None, scope="country_attributed_revenue",
        population_available=False,
        unavailable_reason=canonical_revenue.REASON_LEDGER_UNREADABLE)
    assert v2["publishable"] is False
    assert v2["reason"] == canonical_revenue.REASON_LEDGER_UNREADABLE
    assert v2["currency_unavailable_deals"] is None


# ═════════════════════════════════════════════════════════════════════════════
# C — the generic parity violation gives way to the precise one
# ═════════════════════════════════════════════════════════════════════════════
def _reading(consumer, reason):
    return {"consumer": consumer, "unavailable_reason": reason,
            "truth_status": "not_ready", "violation_codes": []}


def test_14_declared_blockers_emit_the_precise_code_not_the_generic_one():
    status, code, _detail = parity._classify_unavailable(
        [_reading("dashboard/campaigns", "closed_won_deals_missing_amount"),
         _reading("revenue_decision_mart", "closed_won_deals_missing_amount")],
        "campaign_attributed_won_revenue_usd")
    assert code == parity.V_TOTAL_UNPUBLISHABLE
    assert code == "revenue_total_unpublishable_missing_amount"
    assert code != parity.V_SOURCE_UNAVAILABLE
    assert status == "total_unpublishable"

    # Several metrics blocked by the SAME condition deduplicate to one code at
    # the window level, because codes are collected as a set.
    codes = sorted({
        parity._classify_unavailable(
            [_reading("c", "closed_won_deals_missing_amount")], m)[1]
        for m in ("closed_won_revenue_usd", "campaign_attributed_won_revenue_usd",
                  "country_attributed_won_revenue_usd")})
    assert codes == [parity.V_TOTAL_UNPUBLISHABLE]
    assert parity.V_SOURCE_UNAVAILABLE not in codes


def test_14b_all_time_revenue_carries_the_precise_code_against_a_real_database(pg):
    """The production shape, audited end to end: 181 won deals, 14 unpriced.

    Asserted over the REVENUE identities specifically. Every revenue consumer
    must now declare its blocker, so none of them falls back to the generic code
    — which is what made All-Time emit both codes at once in production.
    """
    from tests.test_pr_ads_154c_parity_pg import _DEAL_SQL, _READY_SYNC_SQL, _exec

    _exec(_READY_SYNC_SQL, ("complete",))
    for i in range(167):
        _exec(_DEAL_SQL, (f"P{i}", f"Priced {i}", 5259.43, "USD", 5259.43,
                          "verified_usd", "deal_currency_is_usd"))
    for i in range(14):
        _exec(_DEAL_SQL, (f"U{i}", f"Unpriced {i}", None, None, None,
                          "unavailable", "no_amount"))

    outcome = parity.audit_window("all_time")

    assert parity.V_TOTAL_UNPUBLISHABLE in outcome["violation_codes"], outcome
    # The all-source total names the unpriced deals, not a generic outage.
    by_metric = {m["metric"]: m for m in outcome["metrics"]}
    assert by_metric["closed_won_revenue_usd"]["status"] == "total_unpublishable"
    assert by_metric["closed_won_revenue_usd"]["value"] is None

    # No REVENUE metric falls back to the generic catch-all any more.
    revenue_generics = [
        v for v in outcome["violations"]
        if v.get("code") == parity.V_SOURCE_UNAVAILABLE
        and "revenue" in (v.get("metric") or "")]
    assert revenue_generics == [], revenue_generics

    # And the audit is NOT green while the 14 remain unpriced.
    assert outcome["ok"] is False


def test_15_a_genuinely_unexplained_absence_still_emits_the_generic_code():
    """The fail-closed fallback is kept, not deleted."""
    status, code, detail = parity._classify_unavailable(
        [{"consumer": "x", "unavailable_reason": None, "truth_status": None,
          "violation_codes": []}], "some_metric")
    assert code == parity.V_SOURCE_UNAVAILABLE
    assert status == "unavailable"
    assert "none declared a reason" in detail


# ═════════════════════════════════════════════════════════════════════════════
# D — four HubSpot evidence states, told apart
# ═════════════════════════════════════════════════════════════════════════════
class _FakeVersion:
    def __init__(self, value, ts, source_type="FORM"):
        self._d = {"value": value, "timestamp": ts, "source_type": source_type,
                   "source_id": "77", "source_label": "Signup form",
                   "updated_by_user_id": 12}

    def to_dict(self):
        return dict(self._d)


class _FakeResult:
    def __init__(self, cid, history=None, include_key=True):
        self._d = {"id": cid}
        if history is not None or include_key:
            payload = {} if history is None else {"lifecyclestage": history}
            self._d["properties_with_history"] = payload

    def to_dict(self):
        return dict(self._d)


class _FakeResponse:
    def __init__(self, results):
        self.results = results


def _client_returning(results):
    class _Batch:
        def read(self, **kwargs):
            _client_returning.last_kwargs = kwargs
            return _FakeResponse(results)

    class _Contacts:
        batch_api = _Batch()

    class _Crm:
        contacts = _Contacts()

    class _Client:
        crm = _Crm()

    return _Client()


def test_16_the_connector_distinguishes_every_payload_state():
    from datetime import datetime, timezone as tz

    ts = datetime(2026, 2, 1, tzinfo=tz.utc)
    client = _client_returning([
        _FakeResult("present", history=[_FakeVersion("marketingqualifiedlead", ts)]),
        _FakeResult("empty", history=[]),
        _FakeResult("no_property", include_key=False),
        # "absent" is deliberately NOT returned by the fake response.
    ])
    out = hubspot.fetch_lifecycle_stage_history(
        ["present", "empty", "no_property", "absent"], client=client)

    assert out["present"]["state"] == hubspot.HISTORY_PRESENT
    assert out["empty"]["state"] == hubspot.HISTORY_EMPTY
    assert out["no_property"]["state"] == hubspot.HISTORY_PROPERTY_ABSENT
    assert out["absent"]["state"] == hubspot.HISTORY_CONTACT_ABSENT
    # Four distinct states — the production run collapsed all of them into one.
    assert len({out[k]["state"] for k in out}) == 4

    # The request genuinely asks for history, under the field the SDK serializes
    # to `propertiesWithHistory`.
    sent = _client_returning.last_kwargs["batch_read_input_simple_public_object_id"]
    assert sent["properties_with_history"] == ["lifecyclestage"]
    from hubspot.crm.contacts.models import BatchReadInputSimplePublicObjectId as _B
    assert _B.attribute_map["properties_with_history"] == "propertiesWithHistory"
    from hubspot.crm.contacts.models import SimplePublicObject as _S
    assert "properties_with_history" in _S.openapi_types, (
        "the batch response model must carry history, or the SDK drops it")


def test_17_a_matching_version_preserves_the_authoritative_timestamp():
    from datetime import datetime, timezone as tz

    ts = datetime(2026, 2, 14, 9, 30, tzinfo=tz.utc)
    row = {"contact_id": "c1", "lifecycle_stage": "salesqualifiedlead",
           "date_entered_lead": None, "date_entered_mql": None,
           "date_entered_sql": None, "date_entered_opportunity": None,
           "date_entered_customer": None}
    found, _ = recovery.select_recovered_events(row, [{
        "value": "marketingqualifiedlead", "timestamp": ts,
        "source_type": "AUTOMATION", "source_id": "wf-9",
        "source_label": "Lifecycle workflow", "updated_by_user_id": 5,
    }])
    mql = next(f for f in found if f["funnel_event"] == "mql")
    assert mql["entered_at"] == ts, "HubSpot's own timestamp, unchanged"
    assert mql["hubspot_source_type"] == "AUTOMATION"
    assert mql["hubspot_source_id"] == "wf-9"
    assert mql["hubspot_source_label"] == "Lifecycle workflow"
    assert mql["hubspot_value"] == "marketingqualifiedlead"
    assert mql["evidence_state"] == recovery.MATCHING_VERSION_RECOVERED


def test_18_no_proxy_timestamp_is_ever_invented():
    """No history → no row, and the stage date stays NULL."""
    row = {"contact_id": "c1", "lifecycle_stage": "customer",
           "created_at": "2020-01-01T00:00:00+00:00",
           "date_entered_lead": None, "date_entered_mql": None,
           "date_entered_sql": None, "date_entered_opportunity": None,
           "date_entered_customer": None}
    found, unresolved = recovery.select_recovered_events(row, [])
    assert found == [], "nothing may be recovered from an empty history"
    assert unresolved, "and the gap must be reported, not silently dropped"

    src = (_ROOT / "services" / "lifecycle_history_recovery_service.py").read_text()
    for proxy in ("created_at", "createdate", "ingested_at", "datetime.now"):
        assert f'"{proxy}"' not in src or proxy == "created_at", proxy
    # The writer rejects an undated row rather than storing a NULL timestamp.
    writers = (_ROOT / "db" / "writers.py").read_text()
    block = writers[writers.index("def upsert_lifecycle_stage_history"):]
    block = block[:block.index("\n_FUNNEL_SYNC_STATE_FIELDS")]
    assert "entered_at is None" in block


def test_19_the_recovery_report_separates_the_four_states_with_counts():
    summary = recovery._evidence_summary(
        {hubspot.HISTORY_CONTACT_ABSENT: 3, hubspot.HISTORY_EMPTY: 5,
         hubspot.HISTORY_PRESENT: 42},
        [{"reason": recovery.NO_HISTORY_VERSION},
         {"reason": recovery.HISTORY_PAYLOAD_EMPTY}],
        [{"contact_id": "c1"}])
    per_contact = summary["per_contact_payload_state"]
    per_stage = summary["per_stage_gap_reason"]
    assert per_contact[hubspot.HISTORY_EMPTY] == 5
    assert per_stage[recovery.NO_HISTORY_VERSION] == 1
    assert per_stage[recovery.MATCHING_VERSION_RECOVERED] == 1
    # Two denominators, each named — never merged into one misleading total.
    assert set(summary) == {"per_contact_payload_state", "per_stage_gap_reason"}

    # An empty history is NOT reported as a connector failure, and a request
    # failure is NOT reported as absent history.
    assert recovery.HISTORY_PAYLOAD_EMPTY != recovery.HISTORY_REQUEST_UNAVAILABLE
    assert recovery.HISTORY_PAYLOAD_MISSING != recovery.NO_HISTORY_VERSION


# ═════════════════════════════════════════════════════════════════════════════
# Regression cover for the work this patch sits on
# ═════════════════════════════════════════════════════════════════════════════
def test_20_the_lifecycle_cohort_stays_monotonic():
    from datetime import date

    from services import canonical_crm_funnel_service as cfunnel

    rows = [
        {"contact_id": "a", "lifecycle_stage": "customer",
         "hs_analytics_source": "ORGANIC_SEARCH",
         "date_entered_lead": date(2026, 1, 2), "date_entered_mql": date(2026, 1, 9),
         "date_entered_sql": date(2026, 2, 1),
         "date_entered_opportunity": date(2026, 2, 20),
         "date_entered_customer": date(2026, 3, 1)},
        {"contact_id": "b", "lifecycle_stage": "lead",
         "hs_analytics_source": "ORGANIC_SEARCH",
         "date_entered_lead": date(2026, 1, 3), "date_entered_mql": None,
         "date_entered_sql": None, "date_entered_opportunity": None,
         "date_entered_customer": None},
    ]
    pops = cfunnel.build_populations(rows, date(2026, 1, 1), date(2026, 3, 31))
    reached = [s["reached"]
               for s in cfunnel.lead_cohort_progression(pops)["stages"]]
    assert reached == [2, 1, 1, 1, 1]
    assert all(reached[i] >= reached[i + 1] for i in range(len(reached) - 1))


def test_21_bounded_windows_still_audit_against_a_live_database(pg):
    """The PG-backed CLI path executes for real — it must not skip.

    A clean, empty-but-readable database is not a parity violation of the kind
    this patch is about: what matters here is that the audit RAN against real
    PostgreSQL and produced a structured result per window.
    """
    from analysis.business_windows import WINDOW_KEYS

    res = _run_module("scripts.audit_cross_page_canonical_parity", "--json",
                      env_extra={"DATABASE_URL": pg.url})
    assert res.returncode in (0, 1), res.stdout + res.stderr
    outcome = json.loads(res.stdout)
    audited = {r["window"] for r in outcome["results"]}
    assert audited == set(WINDOW_KEYS)
    for result in outcome["results"]:
        assert "violation_codes" in result
        assert "coverage_disclosures" in result
    # It reached a real database: no window reports the ledger as unreadable.
    assert parity.V_DB_UNREADABLE not in outcome["violation_codes"], outcome
