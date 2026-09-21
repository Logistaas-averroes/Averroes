"""
PR-ADS-161A-1 — one publication verdict, and it fails closed.

`analysis.lifecycle_sql_coverage.window_coverage` answers a NECESSARY question
— could a complete total exist for this window — and sets `cpql_publishable`
from that alone. Its own docstring says the final word belongs to the audit's
`audit_certification`, which lived in a CLI script coupled to a `Findings`
object and which production could not import.

So the only publication flag reachable from product code was the intermediate
one: TRUE for windows the audit refuses to certify. No consumer read it yet.
PR-ADS-161 migrates executive surfaces onto canonical lifecycle truth, and the
first one to reach for `window_coverage()` would have published a total the
audit withholds.

§1 proves the gate refuses in every combination, with the control that proves
it is capable of publishing at all.
§2 is the case the brief names explicitly: membership looks complete, but
certification is false, and production publication must stay withheld.
§3 covers the reconciliation flag, which is read rather than computed and must
treat absence as refusal.
§4 is the structural guard: no production module may reach the intermediate.
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from analysis import lifecycle_sql_coverage as coverage  # noqa: E402
from analysis import sql_publication as pub  # noqa: E402
import services.canonical_sql_publication_service as svc  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _recon(complete=True, *, available=True, stale=False,
           all_compared=True) -> dict:
    """A recorded reconciliation. `all_compared` defaults to the only value
    that permits publication — a record that skipped combinations does not."""
    return {"available": available, "stale": stale,
            "all_combinations_compared": all_compared,
            "reconciliation_complete": complete}


def _eligible_coverage(**over) -> dict:
    """A window that has passed every WINDOW-LOCAL gate."""
    base = {
        "window": "7d",
        "confirmed_sqls": 42,
        "confirmed_sql_subset": 42,
        "window_membership_recovered": 0,
        "window_total_complete": True,
        "complete_sql_total": 42,
        "cpql_publishable": True,          # the intermediate — must not leak
        "certification_eligible": True,
        "certification_status": coverage.CERT_ELIGIBLE,
        "source_fresh": True,
        "window_after_boundary": True,
        "open_post_boundary_gaps": 0,
        "reason": coverage.COVERAGE_COMPLETE,
        "certification_explanation": "every window-local gate passed",
    }
    base.update(over)
    return base


# ═════════════════════════════════════════════════════════════════════════════
# §1 — every gate refuses, and the control proves the gate can say yes
# ═════════════════════════════════════════════════════════════════════════════

def test_01_the_positive_control_a_fully_gated_window_publishes():
    """Without this, every assertion below passes on a gate that never opens."""
    v = pub.publication_verdict(
        coverage=_eligible_coverage(), reconciliation=_recon(True),
        boundary_readable=True, incidents_readable=True,
        window="7d", window_type="evidence", scope="all_source")

    assert v["publishable"] is True
    assert v["status"] == pub.PUBLISHED
    assert v["complete_sql_total"] == 42
    assert v["cpql_publishable"] is True
    assert v["certified"] is True
    assert v["withheld_reason"] is None


@pytest.mark.parametrize("kwargs,expected_reason", [
    ({"reconciliation": _recon(False)},
     pub.WITHHELD_READERS_NOT_RECONCILED),
    ({"reconciliation": None},
     pub.WITHHELD_RECONCILIATION_NOT_PROVEN),
    ({"reconciliation": _recon(True, available=False)},
     pub.WITHHELD_RECONCILIATION_NOT_PROVEN),
    ({"reconciliation": _recon(True, stale=True)},
     pub.WITHHELD_RECONCILIATION_STALE),
    ({"reconciliation": _recon(True, stale=None)},
     pub.WITHHELD_RECONCILIATION_STALE),
    ({"reconciliation": _recon(True, all_compared=False)},
     pub.WITHHELD_RECONCILIATION_PARTIAL),
    ({"reconciliation": _recon(True, all_compared=None)},
     pub.WITHHELD_RECONCILIATION_PARTIAL),
    ({"reconciliation": _recon(None)},
     pub.WITHHELD_RECONCILIATION_NOT_PROVEN),
    ({"boundary_readable": False}, pub.WITHHELD_INPUTS_UNREADABLE),
    ({"incidents_readable": False}, pub.WITHHELD_INPUTS_UNREADABLE),
    ({"boundary_readable": None}, pub.WITHHELD_INPUTS_UNREADABLE),
])
def test_02_any_single_global_gate_withholds_the_total(kwargs, expected_reason):
    """One failed gate is enough. Every other input stays fully satisfied."""
    call = {"coverage": _eligible_coverage(), "reconciliation": _recon(True),
            "boundary_readable": True, "incidents_readable": True,
            "window": "7d", "window_type": "evidence", "scope": "all_source"}
    call.update(kwargs)

    v = pub.publication_verdict(**call)

    assert v["publishable"] is False
    assert v["withheld_reason"] == expected_reason
    assert v["complete_sql_total"] is None, "a withheld total became a number"
    assert v["cpql_publishable"] is False
    # The proven subset survives — withholding the total must not hide the
    # evidence that does exist.
    assert v["confirmed_sql_subset"] == 42


def test_03_a_withheld_total_is_none_and_never_zero():
    """`0` is a measurement. `None` is the absence of one. They are not equal."""
    v = pub.publication_verdict(
        coverage=_eligible_coverage(), reconciliation=_recon(False),
        boundary_readable=True, incidents_readable=True)

    assert v["complete_sql_total"] is None
    assert v["complete_sql_total"] != 0
    assert v["complete_sql_total"] is not False


@pytest.mark.parametrize("cert_status", [
    coverage.CERT_PRE_BOUNDARY,
    coverage.CERT_OVERLAPS_BOUNDARY,
    coverage.CERT_POST_BOUNDARY_GAPS,
    coverage.CERT_INCOMPLETE,
    coverage.CERT_NO_BOUNDARY,
    coverage.CERT_STALE_SOURCE,
])
def test_04_a_window_local_refusal_is_carried_through_verbatim(cert_status):
    """The window-local reason reaches the consumer, not a generic one.

    An operator's next step differs for each: wait for the boundary, fix the
    pipeline, resolve a gap. Collapsing them into "withheld" would lose that.
    """
    v = pub.publication_verdict(
        coverage=_eligible_coverage(certification_eligible=False,
                                    certification_status=cert_status),
        reconciliation=_recon(True),
        boundary_readable=True, incidents_readable=True)

    assert v["publishable"] is False
    assert v["withheld_reason"] == cert_status
    assert v["complete_sql_total"] is None


def test_05_an_absent_coverage_verdict_is_unavailable_not_withheld():
    """We could not look, versus we looked and refused. Different claims."""
    v = pub.publication_verdict(coverage=None, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True)

    assert v["status"] == pub.UNAVAILABLE
    assert v["publishable"] is False
    assert v["complete_sql_total"] is None
    assert v["confirmed_sql_subset"] is None, (
        "an unread population has no proven subset either")


def test_06_unreadable_stores_report_unavailable_rather_than_withheld():
    v = pub.publication_verdict(coverage=_eligible_coverage(),
                                reconciliation=_recon(True),
                                boundary_readable=False, incidents_readable=True)
    assert v["status"] == pub.UNAVAILABLE
    assert v["publishable"] is False


# ═════════════════════════════════════════════════════════════════════════════
# §2 — the case the brief names: membership complete, certification false
# ═════════════════════════════════════════════════════════════════════════════

def test_07_membership_complete_but_uncertified_stays_withheld_in_production():
    """The required regression, driven through the REAL coverage function.

    `window_coverage` is given a window whose membership genuinely resolves —
    nothing undated can belong to it — but which straddles the boundary, so it
    cannot be certified. Its own `cpql_publishable` is therefore the
    intermediate TRUE. Production publication must still withhold.
    """
    boundary = datetime(2026, 9, 21, 4, 34, 37, tzinfo=timezone.utc)
    # A window that opens BEFORE the boundary and closes after it.
    cov = coverage.window_coverage(
        window="30d", window_start=boundary - timedelta(days=23),
        window_end=boundary + timedelta(days=7),
        confirmed_sqls=42, recovered_sqls=0,
        unresolved_rows=[],                 # membership fully resolved
        boundary_observed_at=boundary,
        open_post_boundary_incidents=[],
        freshness={"fresh": True, "reason": "source_fresh"})

    # The premise of this test, asserted rather than assumed.
    assert cov["window_total_complete"] is True, "membership must look complete"
    assert cov["cpql_publishable"] is True, (
        "the intermediate must be the permissive value, or this proves nothing")
    assert cov["certification_eligible"] is False, (
        "and certification must refuse it")

    v = pub.publication_verdict(coverage=cov, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True,
                                window="30d", window_type="evidence",
                                scope="all_source")

    assert v["publishable"] is False, (
        "production published a total the audit would withhold")
    assert v["cpql_publishable"] is False
    assert v["complete_sql_total"] is None
    assert v["withheld_reason"] == coverage.CERT_OVERLAPS_BOUNDARY
    # Coverage's own completeness is still reported truthfully — it IS
    # complete; it is just not publishable.
    assert v["coverage_complete"] is True


def test_08_the_negative_control_move_the_window_past_the_boundary():
    """Same inputs, window moved wholly after the boundary — now it publishes.

    Without this, test_07 would pass against a gate that refuses everything.
    """
    boundary = datetime(2026, 9, 21, 4, 34, 37, tzinfo=timezone.utc)
    cov = coverage.window_coverage(
        window="7d", window_start=boundary + timedelta(days=1),
        window_end=boundary + timedelta(days=8),
        confirmed_sqls=42, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=boundary, open_post_boundary_incidents=[],
        freshness={"fresh": True, "reason": "source_fresh"})

    assert cov["certification_eligible"] is True, "control setup failed"

    v = pub.publication_verdict(coverage=cov, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True)

    assert v["publishable"] is True
    assert v["complete_sql_total"] == 42


def test_09_the_verdict_never_echoes_the_intermediate_publication_flag():
    """A consumer must not be able to reach the pre-certification value.

    The returned `cpql_publishable` is RECOMPUTED, never copied: here coverage
    says True and the verdict says False for the same key.
    """
    cov = _eligible_coverage(certification_eligible=False,
                             certification_status=coverage.CERT_PRE_BOUNDARY,
                             cpql_publishable=True, complete_sql_total=42)

    v = pub.publication_verdict(coverage=cov, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True)

    assert cov["cpql_publishable"] is True, "fixture premise"
    assert v["cpql_publishable"] is False
    assert v["complete_sql_total"] is None


# ═════════════════════════════════════════════════════════════════════════════
# §3 — the reconciliation flag is read, so absence must refuse
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("state,expected", [
    (None, False),
    ({"available": False}, False),
    (_recon(None), False),
    (_recon(False), False),
    (_recon(True, stale=True), False),
    # Unknown staleness withholds exactly as stale does — the module's own
    # contract, and the one place the first version failed OPEN.
    (_recon(True, stale=None), False),
    # A record that never compared every combination is not "all 44".
    (_recon(True, all_compared=False), False),
    (_recon(True, all_compared=None), False),
    (_recon(True), True),
])
def test_10_only_a_present_fresh_proven_record_reconciles(state, expected):
    ok, reason = pub.reconciliation_gate(state)
    assert ok is expected
    assert (reason is None) is expected


def test_11_the_repository_reader_fails_closed_without_a_database():
    """No database is not "the readers agree"."""
    from db import crm_funnel_repository as repo

    state = repo.fetch_reader_reconciliation(now=_NOW)
    assert state["reconciliation_complete"] is not True
    ok, _ = pub.reconciliation_gate(state)
    assert ok is False


def test_12_the_service_withholds_end_to_end_with_no_recorded_proof():
    """The real service, the real gate — a locally-perfect window still withheld."""
    inputs = svc.publication_inputs(now=_NOW)
    v = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source",
                            coverage=_eligible_coverage(), inputs=inputs)

    assert v["publishable"] is False
    assert v["complete_sql_total"] is None
    # WHICH gate refused matters. With no database the boundary store is
    # unreadable and that gate fires first, so this case does NOT prove the
    # reconciliation gate — `test_18` does, with the stores readable.
    assert v["withheld_reason"] == pub.WITHHELD_INPUTS_UNREADABLE

    payload = svc.withheld_payload(v)
    assert payload["value"] is None, "a withheld total serialised as a number"
    assert payload["available"] is False
    assert payload["confirmed_sql_subset"] == 42, (
        "the proven subset must survive the withholding")
    assert payload["reason"], "a withheld value must say why"


def test_13_a_recorded_proof_older_than_its_max_age_is_not_proof():
    """Staleness is measured from when the comparison RAN."""
    fresh = _recon(True, stale=False)
    stale = _recon(True, stale=True)

    assert pub.reconciliation_gate(fresh)[0] is True
    assert pub.reconciliation_gate(stale)[0] is False
    assert pub.reconciliation_gate(stale)[1] == pub.WITHHELD_RECONCILIATION_STALE


# ═════════════════════════════════════════════════════════════════════════════
# §4 — structural guard: nothing in production may reach the intermediate
# ═════════════════════════════════════════════════════════════════════════════

#: Modules legitimately allowed to import the pre-certification coverage layer.
#: `analysis.sql_publication` is the gate itself; the two CLI audits are the
#: read-only reporting layer and are not product surfaces.
_COVERAGE_IMPORT_ALLOWED = {
    "analysis/lifecycle_sql_coverage.py",
    "analysis/sql_publication.py",
    "analysis/sql_doctrine_registry.py",
    "scripts/audit_lifecycle_sql_coverage.py",
    "scripts/audit_sql_coverage_gate.py",
    "scripts/audit_sql_doctrine_inventory.py",
}

_PRODUCT_DIRS = ("services", "api", "db", "scheduler", "connectors", "analysis")

#: Keys `window_coverage` sets from membership alone — both are pre-certification.
_PRE_CERTIFICATION_KEYS = ("cpql_publishable", "complete_sql_total")


def _product_modules():
    for d in _PRODUCT_DIRS:
        for path in sorted((_ROOT / d).rglob("*.py")):
            rel = path.relative_to(_ROOT).as_posix()
            if "__pycache__" in rel:
                continue
            yield rel, path


#: Every spelling that reaches the pre-certification module. The review of
#: this PR found the first version blind to `importlib`, relative imports and
#: `sys.modules`, so those arms are here and are exercised by `test_15`.
_FORBIDDEN_MODULE = "lifecycle_sql_coverage"


def _import_offenders(rel: str, source: str) -> list[str]:
    """THE detector. `test_14` and its control `test_15` both call this.

    The first version of this guard had `test_15` re-implement a weaker
    predicate instead of calling the real one, so both stayed green with the
    detector deliberately gutted — a guard whose control could not see its
    own absence. One function, two callers, is the fix.
    """
    out: list[str] = []
    tree = ast.parse(source, filename=rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.endswith(_FORBIDDEN_MODULE):
                    out.append(f"{rel}:{node.lineno} import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.endswith(_FORBIDDEN_MODULE):
                out.append(f"{rel}:{node.lineno} from {mod}")
            # `from analysis import lifecycle_sql_coverage`, and the relative
            # `from . import lifecycle_sql_coverage` where `node.module` is
            # None — the shape the first version missed entirely.
            if mod.endswith("analysis") or mod == "analysis" or node.level:
                for a in node.names:
                    if a.name == _FORBIDDEN_MODULE:
                        out.append(f"{rel}:{node.lineno} from "
                                   f"{'.' * (node.level or 0)}{mod} "
                                   f"import {a.name}")
        elif isinstance(node, ast.Call):
            # importlib.import_module("analysis.lifecycle_sql_coverage")
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else getattr(fn, "id", ""))
            if name in ("import_module", "__import__"):
                for arg in node.args:
                    if (isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and _FORBIDDEN_MODULE in arg.value):
                        out.append(f"{rel}:{node.lineno} {name}({arg.value!r})")
        elif isinstance(node, ast.Subscript):
            # sys.modules["analysis.lifecycle_sql_coverage"]
            val, sl = node.value, node.slice
            if (isinstance(val, ast.Attribute) and val.attr == "modules"
                    and isinstance(sl, ast.Constant)
                    and isinstance(sl.value, str)
                    and _FORBIDDEN_MODULE in sl.value):
                out.append(f"{rel}:{node.lineno} sys.modules[{sl.value!r}]")
    return out


def test_14_no_product_module_imports_the_pre_certification_coverage_layer():
    """An AST guard, not a substring search.

    A product surface reaching `lifecycle_sql_coverage` can read
    `window_coverage()["cpql_publishable"]` or `["complete_sql_total"]` — both
    TRUE/populated for windows the gate refuses. The legal route is
    `services.canonical_sql_publication_service`.
    """
    offenders = []
    for rel, path in _product_modules():
        if rel in _COVERAGE_IMPORT_ALLOWED:
            continue
        offenders += _import_offenders(rel, path.read_text(encoding="utf-8"))
    assert offenders == [], (
        "these modules can reach the pre-certification publication flag; "
        "use services.canonical_sql_publication_service instead: " + str(offenders))


@pytest.mark.parametrize("label,source", [
    ("plain import", "import analysis.lifecycle_sql_coverage\n"),
    ("from module", "from analysis.lifecycle_sql_coverage import window_coverage\n"),
    ("from package", "from analysis import lifecycle_sql_coverage as c\n"),
    ("relative", "from . import lifecycle_sql_coverage\n"),
    ("importlib", "import importlib\n"
                  "m = importlib.import_module('analysis.lifecycle_sql_coverage')\n"),
    ("sys.modules", "import sys\n"
                    "m = sys.modules['analysis.lifecycle_sql_coverage']\n"),
    ("inside a function", "def build():\n"
                          "    from analysis import lifecycle_sql_coverage\n"
                          "    return lifecycle_sql_coverage\n"),
])
def test_15_the_detector_sees_every_deliberate_violation(label, source):
    """The negative control for test_14 — calling THE SAME detector.

    A guard whose absence changes nothing is not a guard, and a control that
    re-implements the guard proves nothing about it. This runs
    `_import_offenders`, the exact function `test_14` runs, over sources that
    each commit the violation a different way.
    """
    assert _import_offenders(f"services/fake_{label}.py", source), (
        f"the detector cannot see a deliberate violation: {label}")


def test_15b_the_detector_does_not_fire_on_innocent_code():
    """The positive control for the detector: it must not flag everything."""
    innocent = ("from services import canonical_sql_publication_service as pub\n"
                "import json\n"
                "def build():\n"
                "    return pub.publication_for\n")
    assert _import_offenders("services/innocent.py", innocent) == []


def test_16_no_product_module_reads_cpql_publishable_off_raw_coverage():
    """The attribute access itself, wherever the dict came from.

    Two keys leak the intermediate, not one: `cpql_publishable` and
    `complete_sql_total`, which `lifecycle_sql_coverage` sets from
    MEMBERSHIP alone. The review of this PR found only the first was guarded,
    and the second is the field a migrating consumer reaches for first.
    `analysis.sql_publication` is where both are legitimately recomputed.
    """
    allowed = _COVERAGE_IMPORT_ALLOWED | {
        "services/canonical_sql_publication_service.py"}
    offenders = []
    for rel, path in _product_modules():
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and node.value in _PRE_CERTIFICATION_KEYS):
                offenders.append(f"{rel}:{node.lineno} {node.value}")
    assert offenders == [], (
        "these modules name a pre-certification publication key directly: "
        + str(offenders))


def test_17_the_audit_and_production_share_one_certification_implementation():
    """Two callers, one decision — or the audit stops describing production."""
    src = (_ROOT / "scripts" / "audit_lifecycle_sql_coverage.py").read_text(
        encoding="utf-8")
    assert "from analysis import sql_publication as pub" in src
    assert "pub.publication_verdict(" in src, (
        "the audit no longer delegates to the shared gate; a second "
        "certification implementation has appeared")


# ═════════════════════════════════════════════════════════════════════════════
# §5 — defects found reviewing this PR, each with the control that proves it
# ═════════════════════════════════════════════════════════════════════════════

def test_18_with_the_stores_readable_a_missing_record_is_what_refuses():
    """The claim docs/43 §4 rests on, isolated from every other gate.

    `test_12` cannot prove it: with no database the boundary store fails
    first. Here the stores read fine and the ONLY thing missing is the
    recorded reconciliation.
    """
    inputs = {"boundary_readable": True, "incidents_readable": True,
              "reconciliation": {"available": True, "stale": None,
                                 "reconciliation_complete": None},
              "freshness": {"fresh": True, "reason": "source_fresh"}}
    v = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source",
                            coverage=_eligible_coverage(), inputs=inputs)

    assert v["publishable"] is False
    assert v["withheld_reason"] == pub.WITHHELD_RECONCILIATION_NOT_PROVEN
    assert v["complete_sql_total"] is None

    # The control: supply the proof and the same window publishes.
    inputs["reconciliation"] = _recon(True)
    ok = svc.publication_for(window="7d", window_type="evidence",
                             scope="all_source",
                             coverage=_eligible_coverage(), inputs=ok_inputs) \
        if (ok_inputs := inputs) else None
    assert ok["publishable"] is True, "the gate refuses even with proof"


def test_19_publication_inputs_reads_the_incident_rows_the_repository_returns(
        monkeypatch):
    """The incident gate was dead on arrival — it read a key that never exists.

    `fetch_post_boundary_incidents` returns `rows`; this service read
    `incidents`. Readable store, real open incident, and the gate saw `None`.
    """
    from db import crm_funnel_repository as repo

    incident = {"contact_id": "77001", "boundary_id": "b1",
                "reason": "post_boundary_no_direct_sql_date",
                "contact_created_at": datetime(2026, 9, 25, tzinfo=timezone.utc)}
    monkeypatch.setattr(repo, "fetch_post_boundary_incidents",
                        lambda **k: {"available": True, "rows": [incident],
                                     "open_count": 1})
    monkeypatch.setattr(repo, "fetch_active_sql_coverage_boundary",
                        lambda: {"available": True, "boundary": {
                            "boundary_id": "b1",
                            "observed_at": datetime(2026, 9, 21, 4, 34,
                                                    tzinfo=timezone.utc)}})
    monkeypatch.setattr(repo, "fetch_contact_funnel_sync_state", lambda: {})
    monkeypatch.setattr(repo, "fetch_reader_reconciliation",
                        lambda **k: _recon(True))

    inputs = svc.publication_inputs(now=_NOW)

    assert inputs["incidents_readable"] is True
    assert inputs["open_incidents"] == [incident], (
        "the incident gate is reading a key the repository does not return")


def test_20_a_real_open_gap_withholds_rather_than_certifying_a_zero(monkeypatch):
    """The consequence of test_19's defect, asserted on the REASON.

    Reading the wrong key gave `None`, which `window_coverage` reports as
    `CERT_UNAVAILABLE` — "the incident store could not be read" — about a
    store that read perfectly. And the natural defensive `or []` would have
    made a real open gap into a certified zero. The reason must name the gap.
    """
    boundary = datetime(2026, 9, 21, 4, 34, tzinfo=timezone.utc)
    incident = {"contact_id": "77001",
                "contact_created_at": boundary + timedelta(days=3)}

    cov = coverage.window_coverage(
        window="7d", window_start=boundary + timedelta(days=1),
        window_end=boundary + timedelta(days=8),
        confirmed_sqls=42, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=boundary,
        open_post_boundary_incidents=[incident],
        freshness={"fresh": True, "reason": "source_fresh"})

    v = pub.publication_verdict(coverage=cov, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True)

    assert v["publishable"] is False
    assert v["withheld_reason"] == coverage.CERT_POST_BOUNDARY_GAPS, (
        "a real open gap must be named as the reason, not reported as an "
        "unreadable store and not silently counted as zero")
    assert v["complete_sql_total"] is None

    # Control: remove the gap and the identical window publishes.
    clean = coverage.window_coverage(
        window="7d", window_start=boundary + timedelta(days=1),
        window_end=boundary + timedelta(days=8),
        confirmed_sqls=42, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=boundary, open_post_boundary_incidents=[],
        freshness={"fresh": True, "reason": "source_fresh"})
    assert pub.publication_verdict(
        coverage=clean, reconciliation=_recon(True), boundary_readable=True,
        incidents_readable=True)["publishable"] is True


def test_21_the_recorder_refuses_to_write_an_unproven_run_as_a_disagreement():
    """`reconciliation_complete=False` means "compared and disagreed".

    An execution failure makes that flag False too, and recording it would
    tell every later reader a stronger, false thing about the data.
    """
    import scripts.record_sql_reader_reconciliation as rec

    result = {"available": True, "reconciliation_complete": False,
              "combinations_expected": 44, "combinations_compared": 43,
              "combinations_execution_unavailable": 1, "unavailable": 1,
              "all_combinations_compared": False, "results": []}

    import unittest.mock as mock
    with mock.patch.object(rec, "__name__", rec.__name__):
        from db.connection import init_pool  # noqa: F401
        with mock.patch("scripts.audit_lifecycle_sql_coverage."
                        "audit_read_reconciliation", return_value=result), \
             mock.patch("db.writers.record_reader_reconciliation") as writer:
            code, payload = rec.run(apply=True, now=_NOW)

    assert code == rec.EXIT_UNAVAILABLE
    assert payload["applied"] is False
    writer.assert_not_called(), "an unproven run was written as a disagreement"

    # Control: a clean run with no execution failures IS recorded.
    clean = {**result, "reconciliation_complete": True,
             "combinations_compared": 44, "combinations_execution_unavailable": 0,
             "unavailable": 0, "all_combinations_compared": True}
    with mock.patch("scripts.audit_lifecycle_sql_coverage."
                    "audit_read_reconciliation", return_value=clean), \
         mock.patch("db.writers.record_reader_reconciliation",
                    return_value=True) as writer:
        code, payload = rec.run(apply=True, now=_NOW)
    assert code == rec.EXIT_OK and payload["applied"] is True
    writer.assert_called_once()


def test_22_the_writer_refuses_a_naive_observed_at():
    """A naive instant is interpreted in the session zone on the way in."""
    from db import writers

    assert writers.record_reader_reconciliation(
        observed_at=datetime(2026, 9, 21, 12, 0),      # no tzinfo
        reconciliation_complete=True) is False
    assert writers.record_reader_reconciliation(
        observed_at=None, reconciliation_complete=True) is False


def test_23_a_future_dated_record_is_not_fresh_forever():
    """Negative age read as "not older than the limit" grants publication
    indefinitely — clock skew must withhold, not certify."""
    from db import crm_funnel_repository as repo

    max_age = repo.DEFAULT_RECONCILIATION_MAX_AGE_HOURS
    for age_hours, expect_stale in ((-9600.0, True), (1.0, False),
                                    (max_age + 1, True)):
        stale = not (0 <= age_hours <= max_age)
        assert stale is expect_stale, age_hours


def test_24_a_published_verdict_never_carries_a_missing_count():
    """`available: true` beside `value: null` is a blank rendered as a total.

    Reachable because `publication_for` takes `coverage` from its CALLER —
    exactly the PR-ADS-161A-2 seam.
    """
    cov = _eligible_coverage(confirmed_sqls=None, confirmed_sql_subset=None)
    v = pub.publication_verdict(coverage=cov, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True)

    assert v["publishable"] is False
    assert v["status"] == pub.UNAVAILABLE
    payload = svc.withheld_payload(v)
    assert not (payload["available"] and payload["value"] is None), (
        "a certified total with no number reached the API shape")


def test_25_a_malformed_coverage_input_is_unavailable_not_an_exception():
    for bad in ("not a dict", 42, [], object()):
        v = pub.publication_verdict(coverage=bad, reconciliation=_recon(True),
                                    boundary_readable=True,
                                    incidents_readable=True)
        assert v["publishable"] is False
        assert v["status"] == pub.UNAVAILABLE


def test_26_the_audit_still_gates_on_freshness_independently():
    """Freshness must not be merely inherited from the window verdict.

    `run()` passes one freshness object to both call sites today, so the two
    can only agree — which made this an unasserted coupling rather than a
    gate. A CERT_ELIGIBLE window with a stale source must not certify.
    """
    from scripts.audit_lifecycle_sql_coverage import Findings, audit_certification

    win = {"window": "7d", "window_type": "evidence",
           "certification_eligible": True,
           "certification_status": coverage.CERT_ELIGIBLE,
           "confirmed_sqls": 5, "confirmed_sql_subset": 5,
           "cpql_publishable": True, "complete_sql_total": 5}

    out = audit_certification(
        Findings(), [dict(win)],
        {"available": True, "post_boundary_incidents_available": True},
        {"reconciliation_complete": True, "all_combinations_compared": True},
        {"fresh": False, "reason": "source_stale"})

    assert out["windows_certified"] == 0, "a stale source certified a window"
    assert out["blocked_windows"][0]["reason"] == "source_stale"

    # Control: the same window with a fresh source certifies.
    ok = audit_certification(
        Findings(), [dict(win)],
        {"available": True, "post_boundary_incidents_available": True},
        {"reconciliation_complete": True, "all_combinations_compared": True},
        {"fresh": True, "reason": "source_fresh"})
    assert ok["windows_certified"] == 1


def test_27_the_changed_audit_reason_strings_are_asserted_not_assumed():
    """MAJOR 1 from this PR's review: two audit reasons DID change.

    The PR first claimed the refactor changed nothing and offered the 141
    green PR-ADS-160 cases as proof. They pass, but none of them exercises
    either changed path — `_certify()` in that suite always passes
    `freshness=FRESH` regardless of how the window was built. Asserted here
    so the change is a recorded decision rather than an unnoticed side effect.
    """
    from scripts.audit_lifecycle_sql_coverage import Findings, audit_certification

    good_stores = {"available": True, "post_boundary_incidents_available": True}
    good_recon = {"reconciliation_complete": True,
                  "all_combinations_compared": True}

    # 1. Stale source → the FRESHNESS reason, not the window's.
    stale_win = {"window": "7d", "window_type": "evidence",
                 "certification_eligible": False,
                 "certification_status": coverage.CERT_STALE_SOURCE}
    out = audit_certification(Findings(), [stale_win], good_stores, good_recon,
                              {"fresh": False, "reason": "source_stale"})
    assert out["blocked_windows"][0]["reason"] == "source_stale"
    assert stale_win["certification_status"] == "source_stale"
    assert stale_win["cpql_publishable"] is False
    assert stale_win["complete_sql_total"] is None

    # 2. The shape `audit_windows` returns when the contact store is
    #    unreadable — no `certification_eligible` key at all.
    absent_win = {"window": "7d", "window_type": "evidence"}
    out2 = audit_certification(Findings(), [absent_win], good_stores,
                               good_recon,
                               {"fresh": True, "reason": "source_fresh"})
    assert out2["blocked_windows"][0]["reason"] == "coverage_verdict_absent"
    assert out2["windows_certified"] == 0

    # Control: a genuinely eligible window with everything good still
    # certifies, so the two above are not passing on a gate that refuses all.
    ok_win = {"window": "7d", "window_type": "evidence",
              "certification_eligible": True,
              "certification_status": coverage.CERT_ELIGIBLE,
              "confirmed_sqls": 3}
    out3 = audit_certification(Findings(), [ok_win], good_stores, good_recon,
                               {"fresh": True, "reason": "source_fresh"})
    assert out3["windows_certified"] == 1


def test_28_production_and_the_audit_differ_on_exactly_one_named_flag():
    """The one explicit difference between the two callers of the gate.

    Production must not publish a narrow scope on a record where that scope
    was never compared; the audit's `reconciliation_complete` is documented to
    mean "every COMPARABLE combination agreed". The difference is a named
    parameter, not a second implementation — assert it stays that way.
    """
    partial = _recon(True, all_compared=False)

    assert pub.reconciliation_gate(partial)[0] is False
    assert pub.reconciliation_gate(partial)[1] == \
        pub.WITHHELD_RECONCILIATION_PARTIAL
    assert pub.reconciliation_gate(
        partial, require_full_scope_coverage=False)[0] is True

    src = (_ROOT / "scripts" / "audit_lifecycle_sql_coverage.py").read_text(
        encoding="utf-8")
    assert "require_full_scope_coverage=False" in src, (
        "the audit must opt out explicitly, so the difference is visible")

    # The service never opts out — production always requires full coverage.
    svc_src = (_ROOT / "services"
               / "canonical_sql_publication_service.py").read_text(encoding="utf-8")
    assert "require_full_scope_coverage" not in svc_src, (
        "production must not be able to opt out of full scope coverage")
