"""
tests/test_pr_ads_158_sql_doctrine_audit.py

PR-ADS-158 — focused tests for the system-wide SQL doctrine audit.

Numbered to the brief (§12). The audit is an investigation: finding legacy
code is the expected result, and these tests would fail if the audit ever
claimed unification. What they enforce is that the audit cannot be silently
incomplete (an unclassified production occurrence fails it), cannot fabricate
(unavailable is never a zero), cannot leak (no email address in any output),
and cannot write.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import tests.conftest as conftest  # noqa: E402,F401  (import-order guard)

from analysis import sql_doctrine_audit as audit  # noqa: E402
from analysis import sql_doctrine_registry as registry  # noqa: E402
from scripts import audit_sql_doctrine_inventory as cli  # noqa: E402

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
IN_WINDOW = date(2026, 8, 20)
BEFORE_WINDOW = date(2026, 7, 1)
CAMPAIGN = "Brand - US"


# ── helpers ──────────────────────────────────────────────────────────────────
def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _lead(contact_id, status, created=IN_WINDOW, *, src="PAID_SEARCH", campaign=CAMPAIGN,
          run_date="2026-09-01", row_id=1):
    return {"contact_key": contact_id, "contact_id": contact_id, "row_id": row_id,
            "run_date": run_date, "contact_created_at": created, "status_category": status,
            "source_type": "paid_search", "hs_analytics_source": src,
            "campaign_name": campaign, "keyword": "tms", "country": "US",
            "company": f"Co {contact_id}", "has_gclid": True}


def _classification(contact_id, status, group="google_ads"):
    return {"contact_key": contact_id, "contact_id": contact_id, "acquisition_group": group,
            "source_primary_raw": "PAID_SEARCH", "source_detail_raw": None,
            "status_category": status, "contact_created_at": IN_WINDOW}


def _funnel(contact_id, *, stage="salesqualifiedlead", entered_sql=IN_WINDOW,
            src="PAID_SEARCH", campaign=CAMPAIGN, keyword="tms"):
    return {"contact_id": contact_id, "lifecycle_stage": stage, "created_at": BEFORE_WINDOW,
            "date_entered_lead": BEFORE_WINDOW, "date_entered_mql": None,
            "date_entered_sql": entered_sql, "date_entered_opportunity": None,
            "date_entered_customer": None, "hs_analytics_source": src,
            "hs_analytics_source_data_1": campaign, "hs_analytics_source_data_2": keyword,
            "company": f"Co {contact_id}"}


def _production_shaped():
    """Legacy 6 (all campaign-attributable) vs lifecycle 33 / 8 / 8 / 8 in 30d,
    40 SQL-stage contacts without an entry timestamp, one stale and one missing
    classification on NON-SQL contacts."""
    leads = [_lead(f"hs-{i}", "qualified") for i in range(1, 7)]
    leads += [_lead("hs-unknown", "unknown"), _lead("hs-progress", "in_progress")]
    classification = [_classification(f"hs-{i}", "qualified") for i in range(1, 7)]
    classification.append(_classification("hs-progress", "wrong_fit"))   # stale
    funnel = [_funnel(f"hs-{i}") for i in (1, 2, 3, 4, 7, 8, 9, 10)]
    funnel.append(_funnel("hs-5", stage="customer", entered_sql=BEFORE_WINDOW))
    funnel.append(_funnel("hs-6", stage="marketingqualifiedlead", entered_sql=None))
    funnel += [_funnel(f"org-{i}", src="ORGANIC_SEARCH", campaign=None, keyword=None)
               for i in range(1, 26)]
    funnel += [_funnel(f"gap-{i}", entered_sql=None, src="ORGANIC_SEARCH",
                       campaign=None, keyword=None) for i in range(1, 41)]
    return {"available": True, "lead_rows": leads, "exclusions": set(),
            "classification": classification, "table": "leads",
            "date_field": "contact_created_at"}, {"available": True, "rows": funnel,
                                                   "table": "hubspot_contact_funnel"}


def _injected_resolver(_start, _end):
    from services import canonical_contact_outcome_service as canon
    return canon.default_campaign_resolver, True


def _runtime(legacy_inputs=None, funnel_fetch=None):
    from services import canonical_contact_outcome_service as canon
    if legacy_inputs is None:
        legacy_inputs, funnel_fetch = _production_shaped()
    windows = cli.resolve_all_windows(canon, NOW)
    return cli.build_runtime_comparison(
        legacy_inputs=legacy_inputs, funnel_fetch=funnel_fetch, windows=windows,
        resolver_factory=_injected_resolver)


def _window(runtime, window_type, key):
    for w in runtime["windows"]:
        if w["window_type"] == window_type and w["window"] == key:
            return w
    raise AssertionError(f"{window_type}:{key} missing")


def _report(runtime=None, static_only=False):
    return cli.run_audit(root=_ROOT, now=NOW, runtime=runtime, static_only=static_only)


# ── 1-4: discovery patterns ──────────────────────────────────────────────────
def test_01_discovery_finds_direct_sql_qualified_expression(tmp_path):
    root = _tree(tmp_path, {"db/repo.py":
                            "SQL = \"SELECT SUM(CASE WHEN status_category = 'qualified' THEN 1 END)\"\n"})
    found = audit.discover_occurrences(root)
    ids = {o.pattern_id for o in found}
    assert "legacy_sql_literal" in ids
    assert "sql_case_expression" in ids
    assert all(o.location_kind == "production" for o in found)


def test_02_discovery_finds_python_comparison(tmp_path):
    root = _tree(tmp_path, {"services/svc.py":
                            "def f(row):\n    return row.get(\"status_category\") == \"qualified\"\n"})
    found = audit.discover_occurrences(root)
    assert [o.pattern_id for o in found] == ["legacy_python_comparison"]
    assert found[0].symbol == "f"


def test_03_discovery_finds_legacy_service_import(tmp_path):
    root = _tree(tmp_path, {"services/page.py":
                            "from services import canonical_contact_outcome_service as canon\n"})
    found = audit.discover_occurrences(root)
    assert {o.pattern_id for o in found} == {"legacy_outcome_service_ref"}


def test_04_discovery_finds_lifecycle_consumers(tmp_path):
    root = _tree(tmp_path, {"services/funnel.py":
                            "COL = 'date_entered_sql'\nPROP = 'hs_v2_date_entered_salesqualifiedlead'\n"
                            "STAGE = 'salesqualifiedlead'\n"})
    ids = {o.pattern_id for o in audit.discover_occurrences(root)}
    assert {"lifecycle_sql_column_ref", "lifecycle_sql_property_ref",
            "lifecycle_sql_stage_ref"} <= ids


# ── 5: tests / docs / fixtures are not production consumers ──────────────────
def test_05_tests_docs_fixtures_separated_from_production(tmp_path):
    root = _tree(tmp_path, {
        "tests/test_x.py": "status_category = 'qualified'\n",
        "docs/DOC.md": "status_category = 'qualified'\n",
        "scripts/campaign_evidence_fixtures.json": '{"k": "confirmed_sqls"}\n',
        "services/live.py": "status_category = 'qualified'\n",
    })
    occ = audit.classify_occurrences(audit.discover_occurrences(root), [])
    by_path = {o.path: o for o in occ}
    assert by_path["tests/test_x.py"].classification == audit.CLS_TEST_FIXTURE
    assert by_path["docs/DOC.md"].classification == audit.CLS_DOCUMENTATION
    assert by_path["scripts/campaign_evidence_fixtures.json"].classification == audit.CLS_TEST_FIXTURE
    assert by_path["services/live.py"].classification == audit.CLS_UNKNOWN
    static = audit.build_static_inventory(occ, [], [], [])
    assert static["production_occurrences"] == 1
    assert static["non_production_occurrences"] == 3


# ── 6: Google Ads conversions are not SQLs ───────────────────────────────────
def test_06_google_ads_conversions_never_classified_as_sql(tmp_path):
    root = _tree(tmp_path, {"services/kw.py":
                            "row['platform_conversions'] = conversions\n"})
    assert audit.discover_occurrences(root) == []      # no SQL marker at all
    conv = [c for c in registry.CONSUMERS
            if c["classification"] == audit.CLS_GOOGLE_ADS_CONVERSION]
    assert len(conv) == 1
    assert "NOT an SQL" in conv[0]["sql_definition"]
    for c in registry.CONSUMERS:
        if c["classification"] in (audit.CLS_CANONICAL, audit.CLS_LEGACY):
            assert "conversion" not in c["sql_definition"].lower()


# ── 7-8: every production occurrence needs a classification ──────────────────
def test_07_every_production_occurrence_in_repo_is_classified():
    static = cli.build_static(_ROOT)
    assert static["unclassified_occurrences"] == []
    assert static["registry_problems"] == []
    assert static["production_occurrences"] > 500
    hit = static["production_patterns_hit"]
    for required in ("legacy_sql_literal", "legacy_python_comparison",
                     "legacy_outcome_service_ref", "platform_sql_attribution_ref",
                     "confirmed_sqls_ref", "sql_reconciliation_ref",
                     "lifecycle_sql_column_ref", "lifecycle_sql_property_ref",
                     "contact_created_at_ref", "sql_case_expression",
                     "frontend_sqls_label", "cpql_ref", "sql_verdict_ref", "sql_count_ref"):
        assert hit[required] > 0, required


def test_08_unknown_production_occurrence_makes_audit_incomplete(tmp_path):
    root = _tree(tmp_path, {"services/new_page.py": "status_category = 'qualified'\n"})
    occ = audit.classify_occurrences(audit.discover_occurrences(root), registry.RULES)
    static = audit.build_static_inventory(occ, registry.CONSUMERS, [], [])
    assert len(static["unclassified_occurrences"]) == 1
    report = audit.assemble_report(
        static=static, consumers=registry.CONSUMERS, cpql_consumers=[],
        decision_surfaces=[], known_conflicts=[], window_comparisons=[],
        runtime_available=True, runtime_reason=None, generated_at="t",
        audited_commit=None, write_safety={"ok": True, "problems": []})
    assert report["audit_complete"] is False
    assert report["migration_complete"] is False
    assert report["verdict"] == "AUDIT_INCOMPLETE"
    assert report["exit_code"] == 1
    assert report["unclassified_occurrences"][0]["path"] == "services/new_page.py"


# ── 9: equal totals, different contacts ──────────────────────────────────────
def test_09_equal_totals_with_different_contact_sets_is_a_population_difference():
    from services import canonical_contact_outcome_service as canon
    from services import canonical_crm_funnel_service as funnel
    win = canon.resolve_window_contract(canon.WINDOW_EVIDENCE, "30d", now=NOW)
    legacy = canon.build_populations([_lead("a", "qualified"), _lead("b", "qualified")],
                                     set(), [], win["start"], win["end"])
    life = funnel.build_populations([_funnel("b"), _funnel("c")], win["start"], win["end"])
    cmp = audit.compare_window(
        {k: v for k, v in win.items() if k not in ("start", "end")},
        legacy={"available": True, "counts": legacy["counts"], "contacts": legacy["contacts"],
                "identity_available": True},
        lifecycle={"available": True, "populations": life},
        legacy_all_time_keys={"a", "b"}, lifecycle_all_time_keys={"b", "c"},
        legacy_keyword_keys=None, legacy_keyword_note=None,
        reconcile_fn=canon.reconciliation_metadata,
        lifecycle_status_fn=funnel.reconciliation_status)
    assert cmp["legacy_counts"]["all_source"] == cmp["lifecycle_counts"]["all_source"] == 2
    assert cmp["totals_equal"] is True
    assert cmp["populations_equal"] is False
    assert cmp["population_difference"] is True
    assert cmp["overlap_count"] == 1 and cmp["legacy_only_count"] == 1 and cmp["lifecycle_only_count"] == 1


# ── 10-12: production-shaped fixture ─────────────────────────────────────────
def test_10_fixture_reports_legacy_6_versus_lifecycle_8():
    w = _window(_runtime(), "evidence", "30d")
    assert w["legacy_counts"]["campaign_attributable"] == 6
    assert w["legacy_counts"]["google_ads_source"] == 6
    assert w["legacy_counts"]["all_source"] == 6
    assert w["lifecycle_counts"]["campaign_attributable"] == 8
    assert w["overlap_count"] == 4
    assert w["legacy_only_count"] == 2
    assert w["date_shifted_count"] == 1
    assert w["legacy_only_never_lifecycle_sql_count"] == 1
    assert "event_date_moved_from_creation_to_stage_entry" in w["difference_reason_codes"]


def test_11_fixture_reports_lifecycle_scopes_33_8_8_8():
    w = _window(_runtime(), "evidence", "30d")
    assert w["lifecycle_counts"] == {"all_source": 33, "google_ads_source": 8,
                                     "campaign_attributable": 8, "keyword_attributable": 8}
    assert w["lifecycle_reconciliation"]["status"] == "partial"
    assert "missing_stage_entry_date" in w["lifecycle_reconciliation"]["reasons"]


def test_12_missing_sql_entry_dates_remain_explicit_coverage_gaps():
    runtime = _runtime()
    w = _window(runtime, "evidence", "30d")
    assert w["missing_sql_entry_date_count"] == 40
    assert w["lifecycle_complete_total_publishable"] is False
    assert w["lifecycle_complete_total_reasons"] == ["missing_stage_entry_date:sql"]
    assert w["lifecycle_cpql_denominator_complete"] is False
    # The 40 are never counted anywhere — no creation date is substituted.
    assert w["lifecycle_counts"]["all_source"] == 33
    all_time = _window(runtime, "evidence", "all_time")
    assert all_time["lifecycle_counts"]["all_source"] == 34
    gaps = audit.coverage_gaps(runtime["windows"])
    assert gaps["lifecycle_sql_reached_without_entry_timestamp"]["max_contacts"] == 40
    assert gaps["lifecycle_sql_reached_without_entry_timestamp"]["blocks_complete_lifecycle_total"] is True
    assert "evidence:30d" in gaps["windows_with_incomplete_lifecycle_timestamp_coverage"]


# ── 13-15: classification gaps ───────────────────────────────────────────────
def _gap_case(sql_stale=False, non_sql_missing=False):
    from services import canonical_contact_outcome_service as canon
    win = canon.resolve_window_contract(canon.WINDOW_EVIDENCE, "30d", now=NOW)
    leads = [_lead("s1", "qualified"), _lead("n1", "unknown")]
    cls = [_classification("s1", "in_progress" if sql_stale else "qualified")]
    if not non_sql_missing:
        cls.append(_classification("n1", "unknown"))
    pops = canon.build_populations(leads, set(), cls, win["start"], win["end"])
    return canon, pops


def test_13_stale_qualified_contact_is_an_sql_classification_gap():
    canon, pops = _gap_case(sql_stale=True)
    hidden = audit.hidden_reconciliation_causes(
        pops["counts"], pops["contacts"], scope="campaign_attributable_sqls",
        reconcile_fn=canon.reconciliation_metadata)
    assert hidden["sql_contacts_stale_classification"] == 1
    assert hidden["non_sql_contacts_stale_classification"] == 0
    reasons = audit.legacy_reconciliation_reasons(
        pops["counts"], audit.classification_gap_breakdown(pops["contacts"]))
    assert "stale_sql_classification" in reasons
    assert hidden["production_status"] == "partial"


def test_14_missing_classification_on_non_sql_contact_reported_separately():
    canon, pops = _gap_case(non_sql_missing=True)
    hidden = audit.hidden_reconciliation_causes(
        pops["counts"], pops["contacts"], scope="campaign_attributable_sqls",
        reconcile_fn=canon.reconciliation_metadata)
    assert hidden["sql_contacts_missing_classification"] == 0
    assert hidden["non_sql_contacts_missing_classification"] == 1
    reasons = audit.legacy_reconciliation_reasons(
        pops["counts"], audit.classification_gap_breakdown(pops["contacts"]))
    assert "missing_non_sql_classification" in reasons
    assert "missing_sql_classification" not in reasons


def test_15_audit_detects_non_sql_gap_influencing_sql_reconciliation():
    canon, pops = _gap_case(non_sql_missing=True)
    hidden = audit.hidden_reconciliation_causes(
        pops["counts"], pops["contacts"], scope="campaign_attributable_sqls",
        reconcile_fn=canon.reconciliation_metadata)
    assert hidden["production_status"] == "partial"
    assert hidden["status_if_only_sql_gaps_counted"] == "reconciled"
    assert hidden["irrelevant_non_sql_gap_affects_sql_status"] is True
    assert hidden["status_function"].endswith("_reconciliation_status")
    # When an SQL contact is ALSO stale the status is partial for a legitimate
    # reason, and the audit must not blame the non-SQL gap.
    canon, pops = _gap_case(sql_stale=True, non_sql_missing=True)
    hidden = audit.hidden_reconciliation_causes(
        pops["counts"], pops["contacts"], scope="campaign_attributable_sqls",
        reconcile_fn=canon.reconciliation_metadata)
    assert hidden["non_sql_gap_present"] is True
    assert hidden["irrelevant_non_sql_gap_affects_sql_status"] is False
    # And on the production-shaped fixture the flag is set per window.
    w = _window(_runtime(), "evidence", "30d")
    assert w["classification_gaps"]["irrelevant_non_sql_gap_affects_sql_status"] is True
    assert set(w["legacy_reconciliation"]["reasons"]) == {
        "stale_non_sql_classification", "missing_non_sql_classification"}


# ── 16: CPQL consumers ───────────────────────────────────────────────────────
def test_16_cpql_consumers_disclose_denominator_definition_and_scope():
    assert len(registry.CPQL_CONSUMERS) >= 5
    for c in registry.CPQL_CONSUMERS:
        for key in ("spend_numerator_source", "currency_fx_contract",
                    "sql_denominator_definition", "sql_denominator_scope",
                    "sql_denominator_date_field", "numerator_denominator_same_window",
                    "unmatched_sqls_excluded", "denominator_incomplete_when_stage_dates_missing",
                    "publication", "code_location"):
            assert c.get(key) not in (None, ""), f"{c['consumer']}: {key}"
        path = c["code_location"].split(":")[0]
        assert (_ROOT / path).exists(), path
    names = {c["consumer"] for c in registry.CPQL_CONSUMERS}
    assert any("Campaign Evidence row" in n for n in names)
    assert any("overall_cpql" in n for n in names)


# ── 17: database unavailable → exit 2, no fabricated zeros ───────────────────
def test_17_database_unavailable_returns_exit_2_without_zeros(monkeypatch):
    import db.connection as connection
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(connection, "_pool", None)
    report = _report()
    assert report["exit_code"] == 2
    assert report["verdict"] == "SOURCE_UNAVAILABLE"
    assert report["runtime_comparison_available"] is False
    assert report["window_comparisons"] == []
    assert report["coverage_gaps"]["lifecycle_sql_reached_without_entry_timestamp"]["max_contacts"] is None
    assert report["summary"]["windows_with_population_differences"] == 0
    # An unavailable side inside the comparison yields None counts, never 0.
    legacy_inputs, funnel_fetch = _production_shaped()
    runtime = _runtime({"available": False, "lead_rows": [], "exclusions": set(),
                        "classification": []}, funnel_fetch)
    assert runtime["available"] is False
    assert runtime["reason"] == "legacy_leads_source_unavailable"
    assert runtime["sources"]["legacy"]["latest_snapshot_rows"] is None


# ── 18: complete audit exits 0 while migration is incomplete ─────────────────
def test_18_default_completion_exits_0_with_migration_incomplete():
    report = _report(runtime=_runtime())
    assert report["audit_complete"] is True
    assert report["migration_complete"] is False
    assert report["verdict"] == "READY_FOR_ROADMAP"
    assert report["exit_code"] == 0
    assert report["active_legacy_consumers"]
    assert report["mixed_consumers"]
    assert report["unclassified_occurrences"] == []
    assert "ok" not in report            # no ambiguous single ok field
    static = _report(static_only=True)
    assert static["exit_code"] == 0 and static["migration_complete"] is False


# ── 19: no write path reachable ──────────────────────────────────────────────
def test_19_no_external_or_database_write_path_is_reachable():
    safety = cli.write_safety()
    assert safety["ok"] is True, safety["problems"]
    assert set(safety["checked_modules"]) == {
        "analysis/sql_doctrine_audit.py", "analysis/sql_doctrine_registry.py",
        "scripts/audit_sql_doctrine_inventory.py"}
    # The proof rejects a module that would write.
    bad = audit.write_safety_proof({"x": "cur.execute('INSERT INTO t VALUES (1)')\n"})
    assert bad["ok"] is False
    bad = audit.write_safety_proof({"x": "from db import writers\n"})
    assert bad["ok"] is False
    bad = audit.write_safety_proof({"x": "requests.post(url)\n"})
    assert bad["ok"] is False
    # Runtime guard: every pooled connection is forced read-only.
    class _Conn:
        def __init__(self):
            self.executed = []
            self.committed = False

        def cursor(self):
            conn = self

            class _Cur:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def execute(self, sql):
                    conn.executed.append(sql)
            return _Cur()

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

    class _Pool:
        def getconn(self):
            return _Conn()

        def putconn(self, c):
            pass

    pool = cli.ReadOnlyPool(_Pool())
    conn = pool.getconn()
    assert conn.executed == ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"]
    assert conn.committed is True and pool.guarded_connections == 1


# ── 20: human and JSON reconcile ─────────────────────────────────────────────
def test_20_human_and_json_reports_reconcile():
    report = _report(runtime=_runtime())
    human = audit.render_human(report)
    s = report["summary"]
    assert human.rstrip().endswith(f"Verdict: {s['verdict']}")
    assert f"Active legacy consumers: {s['active_legacy_consumers']}" in human
    assert f"Mixed/adapter consumers: {s['mixed_adapter_consumers']}" in human
    assert f"Active canonical-lifecycle consumers: {s['active_canonical_lifecycle_consumers']}" in human
    assert f"Windows with population differences: {s['windows_with_population_differences']}" in human
    assert "External writes performed: no" in human and "Database writes performed: no" in human
    assert s["active_legacy_consumers"] == len(report["active_legacy_consumers"]) == len(
        [c for c in report["inventory"] if c["classification"] == audit.CLS_LEGACY])
    for c in report["inventory"]:
        assert c["consumer"] in human
    json.dumps(report, default=str)        # serialisable


# ── 21: every window audited ─────────────────────────────────────────────────
def test_21_all_evidence_and_business_windows_are_audited():
    runtime = _runtime()
    keys = [(w["window_type"], w["window"]) for w in runtime["windows"]]
    assert keys == [("evidence", k) for k in audit.EVIDENCE_WINDOWS] + [
        ("business", k) for k in audit.BUSINESS_WINDOWS]
    for w in runtime["windows"]:
        assert w["end_date"]
        assert w["legacy_date_field"] == "contact_created_at"
        assert w["lifecycle_event_date_field"] == "date_entered_sql"
        assert w["comparison_available"] is True
    assert _window(runtime, "evidence", "all_time")["start_date"] is None
    assert _window(runtime, "business", "all_time")["start_date"] is None
    report = _report(runtime=runtime)
    assert report["summary"]["windows_audited"] == 11


# ── 22: durable identities, not totals ───────────────────────────────────────
def test_22_contact_level_comparison_uses_durable_identities():
    hub, none = audit.split_legacy_keys({"hs-1", "id:42", "hs-2"})
    assert hub == {"hs-1", "hs-2"} and none == {"id:42"}
    legacy_inputs, funnel_fetch = _production_shaped()
    legacy_inputs["lead_rows"].append(_lead("id:99", "qualified"))
    legacy_inputs["lead_rows"][-1]["contact_id"] = None
    w = _window(_runtime(legacy_inputs, funnel_fetch), "evidence", "30d")
    assert w["legacy_counts"]["all_source"] == 7
    assert w["legacy_sqls_without_hubspot_identity"] == 1
    assert w["overlap_count"] == 4                       # computed on HubSpot ids
    assert "legacy_rows_without_hubspot_identity" in w["difference_reason_codes"]
    assert w["scope_set_comparison"]["campaign_attributable"]["overlap"] == 4


# ── 23: no email addresses in output ─────────────────────────────────────────
def test_23_no_email_addresses_in_output(tmp_path):
    assert audit.scrub("x = 'a.b+c@example.com' # +44 20 7946 0958") == \
        "x = '[redacted-email]' # [redacted-number]"
    root = _tree(tmp_path, {"services/s.py": "status_category = 'qualified'  # owner@example.com\n"})
    occ = audit.discover_occurrences(root)
    assert "@" not in occ[0].snippet
    legacy_inputs, funnel_fetch = _production_shaped()
    for r in legacy_inputs["lead_rows"]:
        r["company"] = "leak@example.com"
    report = _report(runtime=_runtime(legacy_inputs, funnel_fetch))
    text = json.dumps(report, default=str) + audit.render_human(report)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)


# ── 24: production untouched ─────────────────────────────────────────────────
def test_24_production_services_and_ui_do_not_import_the_audit():
    for top in ("api", "services", "analysis", "db", "scheduler", "connectors", "static"):
        for p in (_ROOT / top).rglob("*"):
            if p.suffix not in (".py", ".js", ".html") or p.name in (
                    "sql_doctrine_audit.py", "sql_doctrine_registry.py"):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            assert "sql_doctrine" not in text, p
    # Importing the audit leaves the production builders untouched.
    from services import canonical_contact_outcome_service as canon
    from services import canonical_crm_funnel_service as funnel
    before = (canon.build_populations, canon.reconciliation_metadata,
              funnel.build_populations, funnel.reconciliation_status)
    import importlib
    importlib.reload(audit)
    importlib.reload(registry)
    assert (canon.build_populations, canon.reconciliation_metadata,
            funnel.build_populations, funnel.reconciliation_status) == before
    assert canon.SQL_DEFINITION == "latest status_category = qualified"


# ── registry integrity ───────────────────────────────────────────────────────
def test_registry_records_carry_every_required_field_and_point_at_real_code():
    assert audit.validate_consumers(registry.CONSUMERS, _ROOT) == []
    assert audit.validate_rules(registry.RULES, _ROOT) == []
    for c in registry.CONSUMERS:
        assert c["classification"] in audit.CLASSIFICATIONS
        assert c["confidence"] in ("confirmed", "inferred", "unknown")
    legacy_engine = next(c for c in registry.CONSUMERS
                         if "canonical_contact_outcome_service" in c["consumer"])
    assert legacy_engine["classification"] == audit.CLS_LEGACY   # name ≠ doctrine
    for d in registry.DECISION_SURFACES:
        assert (_ROOT / d["code_location"].split(":")[0]).exists()
    for k in registry.KNOWN_CONTRACT_CONFLICTS:
        assert (_ROOT / k["code_location"].split(":")[0]).exists()


def test_cli_json_and_exit_code(capsys, monkeypatch):
    real_run_audit = cli.run_audit
    monkeypatch.setattr(cli, "run_audit",
                        lambda **kw: real_run_audit(root=_ROOT, now=NOW, runtime=_runtime(),
                                                    static_only=kw.get("static_only", False)))
    rc = cli.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    for key in ("audit_complete", "migration_complete", "verdict", "canonical_standard",
                "inventory", "active_legacy_consumers", "mixed_consumers",
                "unclassified_occurrences", "window_comparisons", "coverage_gaps",
                "cpql_consumers", "decision_surfaces", "known_contract_conflicts",
                "external_writes_performed", "database_writes_performed"):
        assert key in out, key
    assert out["external_writes_performed"] is False
    assert out["database_writes_performed"] is False
    rc = cli.main([])
    text = capsys.readouterr().out
    assert rc == 0 and "Verdict: READY_FOR_ROADMAP" in text
