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


def _recon(complete=True, *, available=True, stale=False) -> dict:
    return {"available": available, "stale": stale,
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
    ({"available": True, "reconciliation_complete": None}, False),
    ({"available": True, "reconciliation_complete": False}, False),
    ({"available": True, "reconciliation_complete": True, "stale": True}, False),
    ({"available": True, "reconciliation_complete": True, "stale": False}, True),
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

    payload = svc.withheld_payload(v)
    assert payload["value"] is None, "a withheld total serialised as a number"
    assert payload["available"] is False
    assert payload["confirmed_sql_subset"] == 42, (
        "the proven subset must survive the withholding")
    assert payload["reason"], "a withheld value must say why"


def test_13_a_recorded_proof_older_than_its_max_age_is_not_proof():
    """Staleness is measured from when the comparison RAN."""
    fresh = {"available": True, "reconciliation_complete": True, "stale": False}
    stale = {"available": True, "reconciliation_complete": True, "stale": True}

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


def _product_modules():
    for d in _PRODUCT_DIRS:
        for path in sorted((_ROOT / d).rglob("*.py")):
            rel = path.relative_to(_ROOT).as_posix()
            if "__pycache__" in rel:
                continue
            yield rel, path


def test_14_no_product_module_imports_the_pre_certification_coverage_layer():
    """An AST guard, not a substring search.

    A product surface importing `lifecycle_sql_coverage` can read
    `window_coverage()["cpql_publishable"]` — the value that is TRUE for
    windows the gate refuses. The legal route is
    `services.canonical_sql_publication_service`.
    """
    offenders = []
    for rel, path in _product_modules():
        if rel in _COVERAGE_IMPORT_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.endswith("lifecycle_sql_coverage"):
                        offenders.append(f"{rel}:{node.lineno} import {a.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.endswith("lifecycle_sql_coverage"):
                    offenders.append(f"{rel}:{node.lineno} from {mod}")
                if mod.endswith("analysis") or mod == "analysis":
                    for a in node.names:
                        if a.name == "lifecycle_sql_coverage":
                            offenders.append(f"{rel}:{node.lineno} from {mod} "
                                             f"import {a.name}")
    assert offenders == [], (
        "these modules can reach the pre-certification publication flag; "
        "use services.canonical_sql_publication_service instead: " + str(offenders))


def test_15_the_guard_fails_when_the_violation_is_reintroduced(tmp_path):
    """The negative control for test_14.

    A guard whose absence changes nothing is not a guard, and a guard that
    cannot see a deliberate violation is the same thing. This re-runs the
    detector over a module that does exactly what test_14 forbids.
    """
    offending = tmp_path / "fake_service.py"
    offending.write_text(
        "from analysis import lifecycle_sql_coverage as coverage\n"
        "def build():\n"
        "    return coverage.window_coverage\n", encoding="utf-8")

    tree = ast.parse(offending.read_text(encoding="utf-8"))
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            and any(a.name == "lifecycle_sql_coverage" for a in n.names)]

    assert hits, "the detector cannot see a deliberate violation"


def test_16_no_product_module_reads_cpql_publishable_off_raw_coverage():
    """The attribute access itself, wherever the dict came from.

    `window_coverage()["cpql_publishable"]` and `.get("cpql_publishable")` are
    the two shapes that leak the intermediate. `analysis.sql_publication` is
    where the key is legitimately recomputed.
    """
    allowed = _COVERAGE_IMPORT_ALLOWED | {
        "services/canonical_sql_publication_service.py"}
    offenders = []
    for rel, path in _product_modules():
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "cpql_publishable":
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "these modules name the pre-certification publication key directly: "
        + str(offenders))


def test_17_the_audit_and_production_share_one_certification_implementation():
    """Two callers, one decision — or the audit stops describing production."""
    src = (_ROOT / "scripts" / "audit_lifecycle_sql_coverage.py").read_text(
        encoding="utf-8")
    assert "from analysis import sql_publication as pub" in src
    assert "pub.publication_verdict(" in src, (
        "the audit no longer delegates to the shared gate; a second "
        "certification implementation has appeared")
