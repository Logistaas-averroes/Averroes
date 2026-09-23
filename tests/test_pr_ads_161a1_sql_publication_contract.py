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
import re
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
_BOUNDARY = datetime(2026, 9, 21, 4, 34, 37, tzinfo=timezone.utc)


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
                "contact_created_at": datetime(2026, 9, 23, tzinfo=timezone.utc),
                "detected_at": datetime(2026, 9, 23, 6, tzinfo=timezone.utc)}
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

    # …and the same value carried one step further, so this spans repository
    # shape → service → coverage → verdict rather than stopping at an echoed
    # dict. Reverting the key to `incidents` must fail HERE too, not only on
    # the assertion above.
    boundary = inputs["boundary_observed_at"]
    cov = coverage.window_coverage(
        window="7d", window_start=boundary + timedelta(days=1),
        window_end=boundary + timedelta(days=8),
        confirmed_sqls=42, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=boundary,
        open_post_boundary_incidents=inputs["open_incidents"],
        freshness={"fresh": True, "reason": "source_fresh"})
    v = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source", coverage=cov, inputs=inputs)

    assert v["publishable"] is False
    assert v["withheld_reason"] == coverage.CERT_POST_BOUNDARY_GAPS, (
        "the real open gap did not reach the verdict; with the wrong key this "
        "reports an unreadable store, and with `or []` it certifies a zero")


def test_20_a_real_open_gap_withholds_rather_than_certifying_a_zero(monkeypatch):
    """The consequence of test_19's defect, asserted on the REASON.

    Reading the wrong key gave `None`, which `window_coverage` reports as
    `CERT_UNAVAILABLE` — "the incident store could not be read" — about a
    store that read perfectly. And the natural defensive `or []` would have
    made a real open gap into a certified zero. The reason must name the gap.
    """
    boundary = datetime(2026, 9, 21, 4, 34, tzinfo=timezone.utc)
    incident = {"contact_id": "77001",
                "contact_created_at": boundary + timedelta(days=3),
                "detected_at": boundary + timedelta(days=3, hours=6)}

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


def test_22_the_writer_refuses_a_naive_observed_at(monkeypatch):
    """A naive instant is interpreted in the SESSION zone on the way in.

    The first version asserted only the return value, with no database — so
    `get_conn()` yielded `None` and the function returned False whether or not
    the guard existed. This intercepts the cursor: the refusal must happen
    before any statement is executed.
    """
    from db import writers

    cm, cursor = _fake_conn([])
    monkeypatch.setattr(writers, "get_conn", cm)

    assert writers.record_reader_reconciliation(
        observed_at=datetime(2026, 9, 21, 12, 0),      # no tzinfo
        reconciliation_complete=True) is False
    assert cursor.executed == [], (
        "a naive instant reached the database; normalising it on read is too "
        "late, the session zone has already chosen the instant")

    assert writers.record_reader_reconciliation(
        observed_at=None, reconciliation_complete=True) is False
    assert cursor.executed == []

    # An unproven outcome is refused too — the `bool()` coercion in the
    # recorder used to defeat this.
    assert writers.record_reader_reconciliation(
        observed_at=_NOW, reconciliation_complete=None) is False
    assert cursor.executed == []

    # The positive control: a tz-aware instant with a proven outcome IS
    # written, so the three refusals above are not passing on a writer that
    # refuses everything.
    assert writers.record_reader_reconciliation(
        observed_at=_NOW, reconciliation_complete=True) is True
    assert len(cursor.executed) == 1
    assert "INSERT INTO sql_reader_reconciliation" in cursor.executed[0][0]


class _FakeCursor:
    """Enough of a psycopg2 cursor for the reconciliation read, and a record
    of every statement it was asked to run."""

    def __init__(self, rows):
        self._rows = rows
        self.executed: list = []
        self.description = [
            ("observed_at",), ("reconciliation_complete",),
            ("combinations_expected",), ("combinations_compared",),
            ("combinations_mismatched",), ("all_combinations_compared",),
            ("effective_date_basis",), ("run_id",)]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_conn(rows):
    """A `get_conn`-shaped context manager yielding a connection over `rows`."""
    import contextlib

    cursor = _FakeCursor(rows)

    class _Conn:
        def cursor(self):
            return cursor

        def commit(self):
            pass

        def rollback(self):
            pass

    @contextlib.contextmanager
    def _cm():
        yield _Conn()

    return _cm, cursor


@pytest.mark.parametrize("age_hours,expect_stale", [
    (-9600.0, True),    # stamped 400 days in the future — clock skew
    (-0.5, True),       # slightly future
    (1.0, False),       # fresh
    (35.0, False),      # inside the 36h limit
    (40.0, True),       # beyond it
])
def test_23_the_reader_decides_staleness_including_future_dated_rows(
        monkeypatch, age_hours, expect_stale):
    """Driven through `fetch_reader_reconciliation`, not a copy of its maths.

    The first version of this test computed `not (0 <= age <= max_age)` in
    the test body and asserted it against itself. It never called the
    production reader, so reverting the fix left every test in the repository
    green — round 1's `test_15` defect, committed a second time. This calls
    the real function over a real row shape.
    """
    from db import crm_funnel_repository as repo

    observed = _NOW - timedelta(hours=age_hours)
    row = (observed, True, 44, 44, 0, True, "date_entered_sql", "run1")
    cm, _cursor = _fake_conn([row])
    monkeypatch.setattr(repo, "get_conn", cm)

    state = repo.fetch_reader_reconciliation(now=_NOW)

    assert state["available"] is True
    assert state["stale"] is expect_stale, (age_hours, state["age_hours"])

    # And the verdict the gate reaches, so this is a refusal rather than a
    # boolean: a future-dated row must not publish.
    ok, reason = pub.reconciliation_gate(state)
    assert ok is (not expect_stale)
    if expect_stale:
        assert reason == pub.WITHHELD_RECONCILIATION_STALE


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

    # `window_total_complete: True` because `audit_windows` builds every row
    # through `window_coverage`, which always emits the key and always emits
    # it True on an eligible window. Omitting it made this fixture a shape
    # production cannot produce, and round 4 added a gate that (correctly)
    # refuses an "eligible" window whose membership is unresolved.
    win = {"window": "7d", "window_type": "evidence",
           "certification_eligible": True,
           "certification_status": coverage.CERT_ELIGIBLE,
           "source_fresh": True, "window_total_complete": True,
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
              "source_fresh": True, "window_total_complete": True,
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


def test_29_production_refuses_a_stale_source_exactly_as_the_audit_does():
    """Round 2: the audit gained an independent freshness gate and production
    did not, so the audit stopped describing what production publishes — on
    the one axis the previous round added defence to.

    Measured before the fix: this coverage dict published a total of 42 here
    while `audit_certification` refused the identical dict.
    """
    stale_cov = _eligible_coverage(
        source_fresh=False,
        certification_status=coverage.CERT_STALE_SOURCE)

    v = pub.publication_verdict(coverage=stale_cov, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True)
    assert v["publishable"] is False
    assert v["complete_sql_total"] is None
    assert v["withheld_reason"] == coverage.CERT_STALE_SOURCE

    # Parity with the audit over the SAME dict — the property that makes the
    # shared implementation worth having.
    from scripts.audit_lifecycle_sql_coverage import Findings, audit_certification
    out = audit_certification(
        Findings(), [{**stale_cov, "window": "7d", "window_type": "evidence"}],
        {"available": True, "post_boundary_incidents_available": True},
        {"reconciliation_complete": True, "all_combinations_compared": True},
        {"fresh": False, "reason": "source_stale"})
    assert out["windows_certified"] == 0
    assert (out["windows_certified"] == 1) is v["publishable"], (
        "the audit and production disagree about the same coverage dict")

    # Control: fresh source, both publish.
    fresh_cov = _eligible_coverage(source_fresh=True)
    assert pub.publication_verdict(
        coverage=fresh_cov, reconciliation=_recon(True),
        boundary_readable=True, incidents_readable=True)["publishable"] is True


def test_30_a_countless_window_and_an_absent_verdict_have_different_reasons():
    """Refusals are kept apart because the remedy differs — including these
    two, which shared one constant until round 2."""
    absent = pub.publication_verdict(
        coverage=None, reconciliation=_recon(True),
        boundary_readable=True, incidents_readable=True)
    countless = pub.publication_verdict(
        coverage=_eligible_coverage(confirmed_sqls=None, confirmed_sql_subset=None),
        reconciliation=_recon(True),
        boundary_readable=True, incidents_readable=True)

    assert absent["withheld_reason"] == pub.WITHHELD_COVERAGE_ABSENT
    assert countless["withheld_reason"] == pub.WITHHELD_COUNT_ABSENT
    assert absent["withheld_reason"] != countless["withheld_reason"]
    assert all(v["complete_sql_total"] is None for v in (absent, countless))


# ═════════════════════════════════════════════════════════════════════════════
# §6 — round 3: the freshness the SERVICE read, and refusals that keep apart
# ═════════════════════════════════════════════════════════════════════════════

def _service_inputs(**over) -> dict:
    """Everything `publication_inputs` returns, all gates satisfied.

    Delegates to §7's `_inputs` so the couplings the real function enforces
    hold here too. It used to build its own dict, which let
    `_service_inputs(boundary_readable=False)` hand back a boundary instant —
    a tuple production cannot produce, and round 4's whole finding. Every §6
    assertion below holds unchanged on the corrected shape; they were true,
    they were simply not being proven on anything production emits.
    """
    return _inputs(**over)


def _real_eligible_coverage():
    """A window `window_coverage` can actually emit as eligible."""
    return coverage.window_coverage(
        window="7d", window_start=_BOUNDARY + timedelta(days=1),
        window_end=_BOUNDARY + timedelta(days=8),
        confirmed_sqls=42, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=_BOUNDARY, open_post_boundary_incidents=[],
        freshness={"fresh": True, "reason": "source_fresh"})


def test_31_the_service_gates_on_the_freshness_it_read_not_the_callers_copy():
    """Round 3's blocker: the freshness read was a label, not a gate.

    `publication_inputs` performs a real database read for contact-funnel
    freshness. `publication_for` stamped the result on the verdict and
    delegated to a gate that reads `coverage["source_fresh"]` — a value the
    CALLER copied in. A caller whose coverage says fresh, over a service that
    has just read stale, published:

        value: 42, available: True, certified: True,
        explanation: "...and the contact-funnel source is fresh"
        source_freshness_reason: "source_stale"      <- the same object

    while `audit_certification` refused the identical inputs.
    """
    cov = _real_eligible_coverage()
    assert cov["certification_eligible"] is True, "fixture premise"
    assert cov["source_fresh"] is True, (
        "the caller's copy must say fresh, or this proves nothing")

    v = svc.publication_for(
        window="7d", window_type="evidence", scope="all_source", coverage=cov,
        inputs=_service_inputs(freshness={"fresh": False,
                                          "reason": "source_stale"}))

    assert v["publishable"] is False
    assert v["complete_sql_total"] is None
    assert "fresh" in (v["withheld_reason"] or "")

    payload = svc.withheld_payload(v)
    assert payload["value"] is None, "a stale-source total reached the API"
    assert payload["available"] is False
    assert payload["source_freshness_reason"] == "source_stale", (
        "the payload must carry the fact it was judged on")

    # The control: the identical call with the service reading fresh.
    ok = svc.publication_for(window="7d", window_type="evidence",
                             scope="all_source", coverage=cov,
                             inputs=_service_inputs())
    assert ok["publishable"] is True
    assert svc.withheld_payload(ok)["value"] == 42


@pytest.mark.parametrize("fresh_value", [False, None, "yes", 0])
def test_32_an_unproven_service_freshness_withholds_whatever_its_shape(
        fresh_value):
    """Only an explicit `True` is fresh — every other value refuses."""
    v = svc.publication_for(
        window="7d", window_type="evidence", scope="all_source",
        coverage=_real_eligible_coverage(),
        inputs=_service_inputs(freshness={"fresh": fresh_value}))
    assert v["publishable"] is False
    assert v["complete_sql_total"] is None


def test_33_the_freshness_refusal_is_not_labelled_eligible():
    """Round 3: the gate reported its own refusal under the window's status.

    On the only shape `window_coverage` emits with `certification_eligible:
    True`, that status is literally `"eligible"` — so a withheld total was
    served to a consumer under a reason meaning "every prerequisite is met".
    """
    cov = {**_real_eligible_coverage(), "source_fresh": False}
    assert cov["certification_status"] == coverage.CERT_ELIGIBLE, (
        "fixture premise: this is the near-production shape")

    v = pub.publication_verdict(coverage=cov, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True)

    assert v["publishable"] is False
    assert v["withheld_reason"] == pub.WITHHELD_SOURCE_NOT_FRESH
    assert v["withheld_reason"] != coverage.CERT_ELIGIBLE
    assert svc.withheld_payload(v)["reason"] == pub.WITHHELD_SOURCE_NOT_FRESH

    # A window whose status IS about freshness keeps its own, more specific one.
    stale = {**cov, "certification_status": coverage.CERT_STALE_SOURCE}
    assert pub.publication_verdict(
        coverage=stale, reconciliation=_recon(True), boundary_readable=True,
        incidents_readable=True)["withheld_reason"] == coverage.CERT_STALE_SOURCE


def test_34_could_not_look_outranks_not_fresh():
    """`unavailable` and `withheld` are different claims; ordering decides.

    The first version placed the freshness gate ahead of the readability and
    reconciliation gates, so "we could not look" was reported as "we looked
    and the source is stale".
    """
    cov = {**_real_eligible_coverage(), "source_fresh": False}

    unreadable = pub.publication_verdict(
        coverage=cov, reconciliation=_recon(True),
        boundary_readable=False, incidents_readable=True)
    assert unreadable["status"] == pub.UNAVAILABLE
    assert unreadable["withheld_reason"] == pub.WITHHELD_INPUTS_UNREADABLE

    unproven = pub.publication_verdict(
        coverage=cov, reconciliation=None,
        boundary_readable=True, incidents_readable=True)
    assert unproven["withheld_reason"] == pub.WITHHELD_RECONCILIATION_NOT_PROVEN

    # With everything readable and reconciled, freshness is the blocker.
    assert pub.publication_verdict(
        coverage=cov, reconciliation=_recon(True), boundary_readable=True,
        incidents_readable=True)["withheld_reason"] == pub.WITHHELD_SOURCE_NOT_FRESH


@pytest.mark.parametrize("status,expected", [
    ("not_certifiable_window_precedes_boundary",
     "not_certifiable_window_precedes_boundary"),
    ("not_certifiable_open_post_boundary_gaps",
     "not_certifiable_open_post_boundary_gaps"),
    ("certification_unavailable", "certification_unavailable"),
    ("not_certifiable_source_not_fresh", "source_stale"),
])
def test_35_a_stale_source_does_not_relabel_every_other_audit_refusal(
        status, expected):
    """Round 3: keying the relabel on `source_fresh` alone rewrote everything.

    A pre-boundary window, an unreadable store and a reader disagreement all
    reported `source_stale` whenever the source also happened to be stale —
    reachable from `run()` on ordinary windows, and it sends an operator to
    fix the pipeline when the real blocker is something else. Only a refusal
    that IS about freshness may be relabelled.
    """
    from scripts.audit_lifecycle_sql_coverage import Findings, audit_certification

    win = {"window": "7d", "window_type": "evidence",
           "certification_eligible": False, "certification_status": status}
    out = audit_certification(
        Findings(), [win],
        {"available": True, "post_boundary_incidents_available": True},
        {"reconciliation_complete": True, "all_combinations_compared": True},
        {"fresh": False, "reason": "source_stale"})

    assert out["blocked_windows"][0]["reason"] == expected
    assert out["windows_certified"] == 0


def test_36_the_recorder_refuses_an_unproven_outcome_without_coercing_it():
    """The `bool()` that turned an unproven None into a recorded False.

    Unreachable from today's only producer, which always returns a bool — so
    this drives the shape directly rather than claiming the hole is live.
    """
    import unittest.mock as mock

    import scripts.record_sql_reader_reconciliation as rec

    unproven = {"available": True, "reconciliation_complete": None,
                "combinations_expected": 44, "combinations_compared": 44,
                "combinations_execution_unavailable": 0, "unavailable": 0,
                "all_combinations_compared": True, "results": []}

    with mock.patch("scripts.audit_lifecycle_sql_coverage."
                    "audit_read_reconciliation", return_value=unproven), \
         mock.patch("db.writers.record_reader_reconciliation",
                    return_value=False) as writer:
        rec.run(apply=True, now=_NOW)

    assert writer.call_args is not None, "the writer was never reached"
    assert writer.call_args.kwargs["reconciliation_complete"] is None, (
        "an unproven outcome was coerced to False on the way to the writer, "
        "which records a disagreement nobody observed")


def test_37_the_services_freshness_fold_does_not_relabel_other_refusals():
    """The service repeated test_35's defect one layer up, and nothing caught it.

    The first round-3 fix short-circuited ahead of the gate and overrode the
    window's `certification_status` with the freshness reason. So a window
    that precedes the boundary — a permanent, structural refusal an operator
    resolves by waiting, not by fixing a pipeline — was reported as a stale
    source whenever the source also happened to be stale. Same defect as
    test_35 pins in the audit; the audit had a test and the service did not.

    Folding `source_fresh` into the coverage the gate reads, instead of
    short-circuiting, leaves the ORDER of refusals with the pure layer.
    """
    stale = _service_inputs(freshness={"fresh": False, "reason": "source_stale"})

    # A window-local refusal outranks freshness and keeps its own reason.
    pre_boundary = coverage.window_coverage(
        window="30d", window_start=_BOUNDARY - timedelta(days=30),
        window_end=_BOUNDARY - timedelta(days=1),
        confirmed_sqls=42, recovered_sqls=0, unresolved_rows=[],
        boundary_observed_at=_BOUNDARY, open_post_boundary_incidents=[],
        freshness={"fresh": True, "reason": "source_fresh"})
    assert pre_boundary["certification_status"] == coverage.CERT_PRE_BOUNDARY, (
        "fixture premise")

    v = svc.publication_for(window="30d", window_type="evidence",
                            scope="all_source", coverage=pre_boundary,
                            inputs=stale)
    assert v["publishable"] is False
    assert v["withheld_reason"] == coverage.CERT_PRE_BOUNDARY, (
        "a structural refusal was relabelled as a pipeline failure")
    assert "not proven fresh" not in (v["explanation"] or ""), (
        "the freshness explanation was stamped over another refusal's reason")

    # "Could not look" still outranks "we looked and it is stale".
    unreadable = svc.publication_for(
        window="7d", window_type="evidence", scope="all_source",
        coverage=_real_eligible_coverage(),
        inputs=_service_inputs(freshness={"fresh": False, "reason": "source_stale"},
                               boundary_readable=False))
    assert unreadable["status"] == pub.UNAVAILABLE
    assert unreadable["withheld_reason"] == pub.WITHHELD_INPUTS_UNREADABLE

    # …and an unproven reconciliation likewise.
    unproven = svc.publication_for(
        window="7d", window_type="evidence", scope="all_source",
        coverage=_real_eligible_coverage(),
        inputs=_service_inputs(freshness={"fresh": False, "reason": "source_stale"},
                               reconciliation=None))
    assert unproven["withheld_reason"] == pub.WITHHELD_RECONCILIATION_NOT_PROVEN

    # The control: with freshness the ONLY blocker, the service does refuse
    # on it and does name what it measured.
    only_stale = svc.publication_for(
        window="7d", window_type="evidence", scope="all_source",
        coverage=_real_eligible_coverage(), inputs=stale)
    assert only_stale["withheld_reason"] == pub.WITHHELD_SOURCE_NOT_FRESH
    assert "source_stale" in (only_stale["explanation"] or "")


def test_38_the_freshness_refusal_table_still_covers_the_coverage_constant():
    """One table, and it must not drift away from the layer it describes.

    `sql_publication.FRESHNESS_REFUSALS` spells the window-local member as a
    literal rather than importing it, so that the pure layer stays free of the
    coverage layer. That choice is only safe while the literal still matches.
    If `CERT_STALE_SOURCE` is ever renamed, the relabel in the audit and the
    gate's own reason lookup both silently stop recognising it — a genuinely
    stale window would then be refused under a reason meaning something else.
    """
    assert coverage.CERT_STALE_SOURCE in pub.FRESHNESS_REFUSALS, (
        "the coverage layer's stale-source status is no longer in the shared "
        "freshness table; the relabel and the gate's reason lookup are now "
        "blind to it")
    assert pub.WITHHELD_SOURCE_NOT_FRESH in pub.FRESHNESS_REFUSALS

    # And the audit reads THAT table rather than keeping its own copy.
    src = (_ROOT / "scripts" / "audit_lifecycle_sql_coverage.py").read_text(
        encoding="utf-8")
    assert "pub.FRESHNESS_REFUSALS" in src, (
        "the audit has grown a second freshness table; they will diverge")


# ═════════════════════════════════════════════════════════════════════════════
# §7 — round 4: the gates that were unreachable on production-shaped inputs
#
# Round 4's finding was not a fabricated control. Every §6 guard goes red under
# a targeted mutation. The defect was subtler: several of them prove their
# property only on input tuples `publication_inputs()` and `window_coverage()`
# CANNOT JOINTLY PRODUCE, and on the tuples production does produce the
# property was false. Every test below therefore builds its coverage from the
# real `publication_inputs()` output, exactly as a consumer must.
# ═════════════════════════════════════════════════════════════════════════════

def _coverage_from_inputs(inputs, *, window_start=None, window_end=None,
                          confirmed=42, unresolved=None):
    """Build coverage the ONLY way a consumer can: out of `publication_inputs`.

    A fixture that sets `boundary_observed_at` while `boundary_readable` is
    False, or `source_fresh: True` while the service read stale, is describing
    a state the service cannot hand a caller. Round 4 found three guards
    resting on exactly that.
    """
    boundary = inputs["boundary_observed_at"]
    anchor = boundary or _BOUNDARY
    return coverage.window_coverage(
        window="7d",
        window_start=window_start or anchor + timedelta(days=1),
        window_end=window_end or anchor + timedelta(days=8),
        confirmed_sqls=confirmed, recovered_sqls=0,
        unresolved_rows=unresolved or [],
        boundary_observed_at=boundary,
        open_post_boundary_incidents=inputs["open_incidents"],
        freshness=inputs["freshness"])


def _inputs(**over):
    """`publication_inputs()`'s real output shape, all gates satisfied.

    Unlike `_service_inputs`, this enforces both couplings the real function
    enforces: an unreadable boundary store CANNOT carry a boundary instant,
    and an unreadable incident store yields `None`, never `[]`.
    """
    base = {"boundary_readable": True, "incidents_readable": True,
            "boundary_observed_at": _BOUNDARY, "boundary_id": "b1",
            "open_incidents": [], "reconciliation": _recon(True),
            "freshness": {"fresh": True, "reason": "source_fresh"}}
    base.update(over)
    if base["boundary_readable"] is not True:
        # What `publication_inputs` actually does:
        #   boundary = (state.get("boundary") or {}) if boundary_readable else {}
        base["boundary_observed_at"] = None
        base["boundary_id"] = None
    if base["incidents_readable"] is not True:
        # And for the incident store: an unreadable read yields None — the
        # unknown — never `[]`, which is the affirmative claim "no open gaps".
        base["open_incidents"] = None
    return base


def test_39_the_copied_coverage_status_literals_still_match_their_constants():
    """Three `CERT_*` values are spelled as literals in the pure layer.

    They are copies, kept so that `analysis.sql_publication` need not import
    `analysis.lifecycle_sql_coverage` (the AST guard in §4 forbids it). A
    rename on either side silently un-matches them, and each one decides a
    gate: `NO_BOUNDARY` decides whether an unread store is reported as
    `unavailable`, `UNAVAILABLE` decides a status, `STALE_SOURCE` decides the
    freshness deferral. This is the only thing that makes copying safe.
    """
    assert pub.COVERAGE_STATUS_NO_BOUNDARY == coverage.CERT_NO_BOUNDARY
    assert pub.COVERAGE_STATUS_UNAVAILABLE == coverage.CERT_UNAVAILABLE
    assert pub.COVERAGE_STATUS_STALE_SOURCE == coverage.CERT_STALE_SOURCE
    assert coverage.CERT_STALE_SOURCE in pub.FRESHNESS_REFUSALS

    # And no coverage-layer status VALUE appears in this module that the
    # table above does not cover. What this catches is a NEW copied status —
    # a gate added tomorrow that compares against, say, the pre-boundary
    # literal — entering without a drift guard. What it does NOT catch is an
    # existing tabled value being written inline instead of via its constant;
    # that is a style point, and a rename is caught by the assertions above
    # either way. Stated so the guard is not credited with more than it does.
    #
    # Read with AST rather than a substring scan, so that a literal inside a
    # comment or a docstring cannot satisfy it — that weakness is exactly what
    # round 4 found in `test_38`'s structural arm.
    src = (_ROOT / "analysis" / "sql_publication.py").read_text(encoding="utf-8")
    # Names of DICT KEYS the module legitimately reads off a coverage dict.
    # They are keys, not status values, so they carry no drift risk.
    keys = {"certification_status", "certification_eligible",
            "certification_explanation"}
    found = {n.value for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and re.fullmatch(r"not_certifiable_\w+|certification_\w+", n.value)
             and n.value not in keys}
    tabled = {pub.COVERAGE_STATUS_NO_BOUNDARY, pub.COVERAGE_STATUS_UNAVAILABLE,
              pub.COVERAGE_STATUS_STALE_SOURCE, pub.WITHHELD_INPUTS_UNREADABLE}
    assert found <= tabled, (
        f"un-tabled coverage-layer literal(s) in sql_publication.py: "
        f"{sorted(found - tabled)} — add them to the table test_39 guards")


def test_40_an_unread_boundary_store_is_not_the_claim_that_none_exists():
    """ROUND 4 BLOCKER: two different claims, byte-identical at every surface.

    `publication_inputs` sets `boundary_observed_at = None` when the store is
    unreadable, so a caller building coverage from it gets `CERT_NO_BOUNDARY`
    — which fired before the readability gate. During an outage every SQL
    surface stated a permanent, benign "no boundary has been established yet"
    and an operator would wait it out.

    `false` and `null` are different claims. This is the repository's own
    named landmine.
    """
    unreadable = _inputs(boundary_readable=False)
    # The coupling, asserted rather than assumed — this is what made the
    # earlier fixtures unreal.
    assert unreadable["boundary_observed_at"] is None, (
        "an unreadable store cannot also hand back a boundary instant")

    v = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source",
                            coverage=_coverage_from_inputs(unreadable),
                            inputs=unreadable)

    assert v["status"] == pub.UNAVAILABLE, (
        "we could not look, reported as a thing we looked at")
    assert v["withheld_reason"] == pub.WITHHELD_INPUTS_UNREADABLE
    assert v["complete_sql_total"] is None

    # The other claim: the store read fine and there is genuinely no boundary.
    none_yet = _inputs(boundary_observed_at=None, boundary_id=None)
    w = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source",
                            coverage=_coverage_from_inputs(none_yet),
                            inputs=none_yet)
    assert w["status"] == pub.WITHHELD
    assert w["withheld_reason"] == coverage.CERT_NO_BOUNDARY

    # They must be distinguishable at the API boundary, not only internally —
    # `withheld_payload` drops `boundary_id`, so this is where it was lost.
    assert svc.withheld_payload(v) != svc.withheld_payload(w)
    assert (svc.withheld_payload(v)["status"]
            != svc.withheld_payload(w)["status"])

    # The control: a readable store WITH a boundary still publishes.
    ok = _inputs()
    good = svc.publication_for(window="7d", window_type="evidence",
                               scope="all_source",
                               coverage=_coverage_from_inputs(ok), inputs=ok)
    assert good["publishable"] is True and good["complete_sql_total"] == 42


@pytest.mark.parametrize("blocker,expected_reason,expected_status", [
    ({"reconciliation": None},
     pub.WITHHELD_RECONCILIATION_NOT_PROVEN, pub.UNAVAILABLE),
    ({"reconciliation": _recon(True, available=False)},
     pub.WITHHELD_RECONCILIATION_NOT_PROVEN, pub.UNAVAILABLE),
    ({"reconciliation": _recon(False)},
     pub.WITHHELD_READERS_NOT_RECONCILED, pub.WITHHELD),
    ({"reconciliation": _recon(True, stale=True)},
     pub.WITHHELD_RECONCILIATION_STALE, pub.WITHHELD),
    ({"boundary_readable": False},
     pub.WITHHELD_INPUTS_UNREADABLE, pub.UNAVAILABLE),
    ({"incidents_readable": False},
     pub.COVERAGE_STATUS_UNAVAILABLE, pub.UNAVAILABLE),
])
def test_41_a_stale_source_no_longer_displaces_the_global_refusals(
        blocker, expected_reason, expected_status):
    """ROUND 4 BLOCKER: the round-3 reordering was inert on real inputs.

    Freshness is gated in TWO places. `_certification` refuses an
    otherwise-perfect window with `CERT_STALE_SOURCE`, and that is the
    window-local gate — step 1, ahead of everything the reordering put in
    front of the step-4 gate. So on every coherent production input a stale
    source still won. Measured before the fix, these three reporting
    `not_certifiable_source_not_fresh`:

        stale + reconciliation record absent
        stale + readers disagreed
        stale + boundary store unreadable

    The fourth arm, `stale + incident store unreadable`, is NOT one of them
    and never was: `publication_inputs` emits `open_incidents = None` for an
    unreadable incident store, which `_certification` turns into
    `CERT_UNAVAILABLE` several branches before it ever reaches the
    stale-source branch. Measured under the pre-fix gate it reported
    `unavailable / certification_unavailable`, identically to today. It is
    carried here as the negative control: a refusal the reordering must
    leave exactly where it was.

    Today NO reconciliation record exists — the recorder has no scheduled
    home — so a stale sync would have sent an operator to the pipeline while
    the refusal actually blocking every window went unreported.

    Every case here is built through the real `window_coverage`, with the
    caller's freshness copy taken from `inputs` as a consumer must.
    """
    ins = _inputs(freshness={"fresh": False, "reason": "source_stale"},
                  **blocker)
    cov = _coverage_from_inputs(ins)
    # The premise: this is genuinely the window-local stale refusal, not a
    # hand-built dict. (Either unreadable store outranks it even
    # window-locally — the boundary as CERT_NO_BOUNDARY, the incident store
    # as CERT_UNAVAILABLE — so those two arms assert that instead.)
    if ins["boundary_readable"] is not True:
        assert cov["certification_status"] == coverage.CERT_NO_BOUNDARY, (
            "fixture premise: a null boundary instant outranks staleness")
    elif ins["incidents_readable"] is not True:
        assert cov["certification_status"] == coverage.CERT_UNAVAILABLE, (
            "fixture premise: unknown open gaps outrank staleness")
    else:
        assert cov["certification_status"] == coverage.CERT_STALE_SOURCE, (
            "fixture premise: production emits the window-local stale refusal")

    v = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source", coverage=cov, inputs=ins)

    assert v["withheld_reason"] == expected_reason, (
        "a stale source displaced the refusal an operator must act on")
    assert v["status"] == expected_status
    assert v["complete_sql_total"] is None


def test_42_with_every_global_gate_satisfied_freshness_is_still_the_blocker():
    """The control for test_41: the deferral must not swallow the refusal.

    Deferring the window-local freshness reason past the global gates would
    be a fail-open if nothing caught it afterwards. With every global gate
    satisfied it must still refuse, and under its own specific reason rather
    than the generic one.
    """
    ins = _inputs(freshness={"fresh": False, "reason": "source_stale"})
    cov = _coverage_from_inputs(ins)
    assert cov["certification_status"] == coverage.CERT_STALE_SOURCE

    v = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source", coverage=cov, inputs=ins)

    assert v["publishable"] is False
    assert v["complete_sql_total"] is None
    assert v["withheld_reason"] == coverage.CERT_STALE_SOURCE, (
        "the window's own, more specific freshness reason was lost in the "
        "deferral")
    assert "source_stale" in (v["explanation"] or "")

    # A window that is stale AND structurally refused keeps the structural
    # reason: the deferral must not promote freshness over a permanent
    # refusal either.
    pre = _coverage_from_inputs(
        ins, window_start=_BOUNDARY - timedelta(days=30),
        window_end=_BOUNDARY - timedelta(days=1))
    assert svc.publication_for(
        window="30d", window_type="evidence", scope="all_source",
        coverage=pre, inputs=ins)["withheld_reason"] == coverage.CERT_PRE_BOUNDARY

    # The fail-open the deferral's second condition exists to prevent, named
    # rather than left to be caught incidentally. Deferring on the REASON
    # alone would let an incoherent dict — a stale status beside a fresh
    # source — fall past the step-1 refusal and out through the publishing
    # branch, because the step-4 gate that is supposed to catch it reads
    # `source_fresh`. Such a dict keeps its immediate refusal.
    incoherent = {**_eligible_coverage(),
                  "certification_eligible": False,
                  "certification_status": coverage.CERT_STALE_SOURCE,
                  "source_fresh": True}
    bad = pub.publication_verdict(
        coverage=incoherent, reconciliation=_recon(True),
        boundary_readable=True, incidents_readable=True)
    assert bad["publishable"] is False, (
        "a coverage dict claiming both a stale status and a fresh source fell "
        "through the deferral and published")
    assert bad["withheld_reason"] == coverage.CERT_STALE_SOURCE


def test_43_an_eligible_window_with_unresolved_membership_publishes_nothing():
    """ROUND 4 MAJOR: the subset was published as a certified COMPLETE total.

        value: 42, available: True, certified: True, coverage_complete: False

    PR-ADS-160 §2's defect verbatim, in one object. `window_coverage` cannot
    emit that pair — `_certification` requires `complete` before returning
    `CERT_ELIGIBLE` — so it arises only from a caller-built dict, which is
    exactly the seam `publication_for` exposes and the reason `test_24`
    exists for the sibling missing-count case. The PR guarded "eligible with
    no count" (a blank) and left "eligible with unresolved membership" (a
    WRONG NUMBER) open.
    """
    contradictory = {"confirmed_sqls": 42, "confirmed_sql_subset": 42,
                     "window_total_complete": False, "complete_sql_total": None,
                     "certification_eligible": True,
                     "certification_status": coverage.CERT_ELIGIBLE,
                     "source_fresh": True}

    v = pub.publication_verdict(coverage=contradictory, reconciliation=_recon(True),
                                boundary_readable=True, incidents_readable=True)

    assert v["publishable"] is False
    assert v["complete_sql_total"] is None
    assert v["withheld_reason"] == pub.WITHHELD_COVERAGE_SELF_CONTRADICTORY
    assert v["status"] == pub.UNAVAILABLE
    payload = svc.withheld_payload(v)
    assert payload["value"] is None and payload["available"] is False

    # An absent key is not a resolved membership either — unknown is not yes.
    missing = {k: val for k, val in contradictory.items()
               if k != "window_total_complete"}
    assert pub.publication_verdict(
        coverage=missing, reconciliation=_recon(True), boundary_readable=True,
        incidents_readable=True)["publishable"] is False

    # The control: resolve the membership and the same window publishes.
    resolved = {**contradictory, "window_total_complete": True}
    ok = pub.publication_verdict(coverage=resolved, reconciliation=_recon(True),
                                 boundary_readable=True, incidents_readable=True)
    assert ok["publishable"] is True and ok["complete_sql_total"] == 42


def test_44_an_unreadable_sync_state_is_not_the_claim_that_it_is_stale():
    """ROUND 4 MAJOR: the F3 fold wrote `False` for `None`.

    `sql_coverage_freshness.assess` is explicit: `fresh` is `None`, never
    `False`, when the state could not be read — False is a claim about the
    pipeline, None is a statement about us, and the caller must be able to
    tell them apart. The service's own fold was the first thing in the chain
    to erase that, recording `source_fresh: False` for a read that failed.
    """
    unreadable = _inputs(freshness={"fresh": None,
                                    "reason": "source_freshness_unreadable"})
    stale = _inputs(freshness={"fresh": False, "reason": "source_stale"})

    a = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source",
                            coverage=_coverage_from_inputs(unreadable),
                            inputs=unreadable)
    b = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source",
                            coverage=_coverage_from_inputs(stale), inputs=stale)

    assert a["source_fresh"] is None, (
        "'we could not read the sync state' was recorded as 'the pipeline is "
        "stale' — a claim nobody made")
    assert b["source_fresh"] is False

    # Both still block, and both still say which one they are.
    assert a["publishable"] is False and b["publishable"] is False
    assert svc.withheld_payload(a)["source_freshness_reason"] == (
        "source_freshness_unreadable")
    assert svc.withheld_payload(b)["source_freshness_reason"] == "source_stale"


@pytest.mark.parametrize("malformed", [[1, 2], "not a dict", 42, 0, "", [], None])
def test_45_a_malformed_coverage_survives_the_service_not_only_the_gate(
        malformed):
    """ROUND 4 MINOR: an F3 regression `test_25` structurally could not see.

    `test_25` proves the PURE gate returns `unavailable` on a malformed
    coverage. F3 added `{**(coverage or {}), ...}` to the SERVICE, which
    raises `TypeError` on a truthy non-mapping — measured against the F2
    service, which returned `unavailable / coverage_verdict_absent`. Because
    `test_25` routes nothing through `publication_for`, the regression was
    invisible to it. Same inputs, both layers, from now on.
    """
    ins = _inputs(freshness={"fresh": False, "reason": "source_stale"})

    v = svc.publication_for(window="7d", window_type="evidence",
                            scope="all_source", coverage=malformed, inputs=ins)

    assert v["status"] == pub.UNAVAILABLE
    assert v["publishable"] is False
    assert v["complete_sql_total"] is None
    assert svc.withheld_payload(v)["value"] is None
