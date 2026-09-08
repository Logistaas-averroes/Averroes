"""PR-ADS-157-F1 — Campaign certification population reconciliation.

The defect this suite exists for
--------------------------------
Production validation of the merged PR-ADS-157 gate failed on real data::

    180d      summary.confirmed_sqls_total = 71    sum over all rows = 72
    all_time  summary.confirmed_sqls_total = 382   sum over all rows = 614

Neither number was wrong. The comparison was.

``confirmed_sqls_total`` deliberately counts one population — SQLs attributed to
a canonical Google Ads campaign identity (``mapping_status="mapped"``). The
campaign table additionally renders **Mapping Review** rows
(``mapping_status="unmatched"``): real paid-search SQLs whose campaign identity
is not yet proven. They are displayed so nobody loses sight of them, and they
are excluded from the published total so an unattributed SQL can never lower
canonical CPQL.

So ``71 mapped + 1 unmatched = 72`` and ``382 mapped + 232 unmatched = 614``.
The audit was summing a mapped + unmatched population and comparing it to a
mapped-only field, which fails on any account carrying a single Mapping Review
SQL. That is a population mismatch inside the gate, not a defect in the product.

The correction is to reconcile each population against the field it actually
feeds — never to widen ``confirmed_sqls_total`` or the CPQL denominator, and
never to drop Mapping Review rows to make the arithmetic line up. Both of those
"fixes" would trade a broken audit for a false number.

What these tests will not accept
--------------------------------
The last test in this file reintroduces the merged-population sum on disk and
requires the audit to fail. A gate that cannot be shown to fail is not a gate,
and this particular gate has already passed once while comparing the wrong two
numbers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import tests.conftest as conftest  # noqa: E402,F401  (import-order guard)
import services.campaign_evidence_service as ces  # noqa: E402
from scripts import audit_campaign_evidence_certification as audit  # noqa: E402

_AUDIT_SRC = "scripts/audit_campaign_evidence_certification.py"


# ─────────────────────────────────────────────────────────────────────────────
# Production-shaped payload builders
# ─────────────────────────────────────────────────────────────────────────────

def _spread(total: int, buckets: int) -> list[int]:
    """Split a SQL total across rows the way a real account does.

    A single row carrying the whole total would let a wrong-population sum look
    right by coincidence, because one row is indistinguishable from all rows.
    """
    base, rem = divmod(total, buckets)
    return [base + (1 if i < rem else 0) for i in range(buckets)]


def _row(key: str, status: str, sqls, *, spend_usd=None) -> dict:
    return {
        "campaign_key": key,
        "campaign_name": key.replace("_", " ").title(),
        "mapping_status": status,
        "confirmed_sqls": sqls,
        "spend_usd": spend_usd,
    }


def _shaped(mapped_counts, unmatched_counts, excluded_sqls):
    """A campaign payload with all three paid-search populations present.

    Returns the rows and the summary the production service builds from them —
    mapped-only headline total, full mapping coverage beside it.
    """
    campaigns = [_row(f"g{i}", "mapped", n, spend_usd=100.0 * (i + 1))
                 for i, n in enumerate(mapped_counts)]
    campaigns += [_row(f"unmatched:review{i}", "unmatched", n)
                  for i, n in enumerate(unmatched_counts)]
    mapped = sum(mapped_counts)
    unmatched = sum(unmatched_counts)
    summary = {
        "campaigns": len(campaigns),
        "confirmed_sqls_total": mapped,
        "mapping_coverage": {
            "mapped_sqls": mapped,
            "unmatched_sqls": unmatched,
            "excluded_not_google_sqls": excluded_sqls,
            "total_paid_search_sqls": mapped + unmatched + excluded_sqls,
            "status": "complete" if (unmatched == 0 and excluded_sqls == 0) else "partial",
        },
    }
    return campaigns, summary


def _check(window, campaigns, summary):
    """Run the reconciliation check and return (violations, scopes)."""
    f = audit.Findings()
    scopes = audit.check_summary_population_reconciliation(window, campaigns, summary, f)
    return f.violations, scopes


def _names(violations) -> set[str]:
    return {v.split(":")[0].split("[")[0] for v in violations}


# ═════════════════════════════════════════════════════════════════════════════
# 1 · The production shapes that failed the merged gate
# ═════════════════════════════════════════════════════════════════════════════

def test_01_full_production_shape_reconciles_across_all_three_populations():
    """Mapped SQLs, Mapping Review SQLs and excluded not-Google SQLs together.

    This is the shape every real account has. Under the merged gate it was an
    automatic failure; under a population-aligned gate it must be clean.
    """
    campaigns, summary = _shaped(_spread(48, 9), [3, 2], 5)
    violations, scopes = _check("30d", campaigns, summary)

    assert violations == [], violations
    assert scopes["campaign_attributable_sqls"]["sqls"] == 48
    assert scopes["unmatched_sqls"]["sqls"] == 5
    assert scopes["excluded_not_google_sqls"]["sqls"] == 5
    assert scopes["total_paid_search_sqls"]["sqls"] == 58


def test_02_the_180d_production_case_71_mapped_plus_1_unmatched():
    """The exact 180d failure: 71 mapped + 1 unmatched, 72 across all rows."""
    campaigns, summary = _shaped(_spread(71, 18), [1], 4)

    assert sum(c["confirmed_sqls"] for c in campaigns) == 72, (
        "the fixture must reproduce the failing shape: the naive all-rows sum "
        "is 72 while the published total is 71")
    assert summary["confirmed_sqls_total"] == 71

    violations, scopes = _check("180d", campaigns, summary)
    assert violations == [], violations
    assert scopes["campaign_attributable_sqls"]["sqls"] == 71
    assert scopes["unmatched_sqls"]["sqls"] == 1


def test_03_the_all_time_production_case_382_mapped_plus_232_unmatched():
    """The exact all_time failure: 382 mapped + 232 unmatched, 614 across rows."""
    campaigns, summary = _shaped(_spread(382, 60), _spread(232, 24), 11)

    assert sum(c["confirmed_sqls"] for c in campaigns) == 614
    assert summary["confirmed_sqls_total"] == 382

    violations, scopes = _check("all_time", campaigns, summary)
    assert violations == [], violations
    assert scopes["campaign_attributable_sqls"]["sqls"] == 382
    assert scopes["unmatched_sqls"]["sqls"] == 232
    assert scopes["total_paid_search_sqls"]["sqls"] == 625


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Each population is reconciled against its own field
# ═════════════════════════════════════════════════════════════════════════════

def test_04_a_wrong_mapped_total_is_still_caught():
    """The gate must not have been loosened into uselessness.

    Aligning the populations is only correct if a genuine disagreement inside
    the mapped population still fails.
    """
    campaigns, summary = _shaped([10, 20, 30], [4], 0)
    summary["confirmed_sqls_total"] = 61          # rows say 60
    summary["mapping_coverage"]["mapped_sqls"] = 61
    summary["mapping_coverage"]["total_paid_search_sqls"] = 65

    violations, _ = _check("30d", campaigns, summary)
    assert "mapped_rows_reconcile" in _names(violations)
    assert "mapped_coverage_reconcile" in _names(violations)


def test_05_confirmed_sqls_total_and_mapped_coverage_must_agree_with_each_other():
    """Two published fields naming the same population cannot disagree."""
    campaigns, summary = _shaped([7, 8], [1], 0)
    summary["mapping_coverage"]["mapped_sqls"] = 14   # headline says 15

    violations, _ = _check("30d", campaigns, summary)
    assert "mapped_coverage_reconcile" in _names(violations)
    assert "mapped_rows_reconcile" not in _names(violations)


def test_06_mapping_review_rows_reconcile_to_unmatched_sqls():
    """A Mapping Review row that vanishes from mapping_coverage is a violation.

    Dropping unmatched rows is the other way to make the old comparison pass,
    and it hides exactly the SQLs an operator needs to go and map.
    """
    campaigns, summary = _shaped([12], [3, 2], 0)
    summary["mapping_coverage"]["unmatched_sqls"] = 3   # one review row dropped

    violations, _ = _check("30d", campaigns, summary)
    assert "unmatched_rows_reconcile" in _names(violations)


def test_07_the_three_populations_must_add_up_to_the_declared_total():
    campaigns, summary = _shaped([20], [5], 3)
    summary["mapping_coverage"]["total_paid_search_sqls"] = 25   # 20 + 5 + 3 = 28

    violations, _ = _check("30d", campaigns, summary)
    assert "paid_search_partition" in _names(violations)


def test_08_a_row_in_neither_population_is_a_violation():
    """A row must belong to exactly one reconciled population.

    A row with an unrecognised mapping_status reconciles against nothing, so it
    would disappear from every total without a single check going red.
    """
    campaigns, summary = _shaped([10], [2], 0)
    campaigns.append(_row("mystery", "provisional", 9))

    violations, _ = _check("30d", campaigns, summary)
    assert "population_partition" in _names(violations)

    campaigns[-1]["mapping_status"] = None
    violations, _ = _check("30d", campaigns, summary)
    assert "population_partition" in _names(violations)


def test_09_scope_definitions_are_published_for_every_population():
    """The audit output names each population, so no reader has to infer it."""
    campaigns, summary = _shaped([10], [2], 1)
    _, scopes = _check("30d", campaigns, summary)

    assert set(scopes) == {
        "campaign_attributable_sqls", "unmatched_sqls",
        "excluded_not_google_sqls", "total_paid_search_sqls",
    }
    for name, block in scopes.items():
        assert block["definition"].strip(), f"{name} has no stated definition"
    assert scopes["excluded_not_google_sqls"]["rows"] == 0, (
        "excluded SQLs are proven not to be Google Ads, so no campaign row "
        "exists for them")


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Unavailable stays unavailable
# ═════════════════════════════════════════════════════════════════════════════

def test_10_a_withheld_population_is_not_reconciled_as_zero():
    """When lead evidence is unavailable, rows and summary both withhold."""
    campaigns = [_row("g0", "mapped", None), _row("unmatched:r0", "unmatched", None)]
    summary = {
        "confirmed_sqls_total": None,
        "mapping_coverage": {
            "mapped_sqls": None, "unmatched_sqls": None,
            "excluded_not_google_sqls": None, "total_paid_search_sqls": None,
            "status": "unavailable",
        },
    }
    violations, scopes = _check("30d", campaigns, summary)

    assert violations == [], violations
    assert scopes["campaign_attributable_sqls"]["sqls"] is None
    assert scopes["total_paid_search_sqls"]["sqls"] is None


def test_11_withheld_on_one_side_only_is_a_violation():
    """Half the evidence missing while the other half is published as certain."""
    campaigns, summary = _shaped([10, 5], [1], 0)
    campaigns[0]["confirmed_sqls"] = None

    violations, _ = _check("30d", campaigns, summary)
    assert "mapped_rows_reconcile" in _names(violations)

    campaigns, summary = _shaped([10, 5], [1], 0)
    summary["confirmed_sqls_total"] = None
    violations, _ = _check("30d", campaigns, summary)
    assert "mapped_rows_reconcile" in _names(violations)


def test_12_a_missing_coverage_block_never_reads_as_zero():
    """An absent mapping_coverage is unavailable evidence, not four zeroes.

    Rows publishing 10 mapped and 2 review SQLs beside a coverage block that
    states nothing is exactly the half-certain shape the gate must reject.
    """
    campaigns, summary = _shaped([10], [2], 0)
    summary.pop("mapping_coverage")

    violations, scopes = _check("30d", campaigns, summary)
    assert {"mapped_coverage_reconcile", "unmatched_rows_reconcile"} <= _names(violations)
    assert scopes["total_paid_search_sqls"]["sqls"] is None, (
        "a missing coverage total must stay unavailable, never become 0")


def test_12b_an_empty_population_does_not_contradict_a_withheld_field():
    """No rows is not evidence of zero.

    An account with no campaign rows and no lead evidence withholds every SQL
    field. Reading the empty row set as a certain `0` would make that outage
    look like a disagreement and send an operator hunting a bug that is not
    there.
    """
    summary = {
        "confirmed_sqls_total": None,
        "mapping_coverage": {
            "mapped_sqls": None, "unmatched_sqls": None,
            "excluded_not_google_sqls": None, "total_paid_search_sqls": None,
            "status": "unavailable",
        },
    }
    violations, _ = _check("30d", [], summary)
    assert violations == [], violations

    # But an empty population still has to match a field that publishes a number.
    summary["mapping_coverage"]["unmatched_sqls"] = 1
    violations, _ = _check("30d", [], summary)
    assert "unmatched_rows_reconcile" in _names(violations)


# ═════════════════════════════════════════════════════════════════════════════
# 4 · The product itself must not widen the published scope
# ═════════════════════════════════════════════════════════════════════════════

def _outcomes(display_name, qualified):
    agg = ces._new_outcomes(display_name)
    agg[ces._QUALIFIED] = qualified
    agg["total_leads"] = qualified
    return agg


def test_13_unmatched_sqls_enter_neither_the_headline_total_nor_the_cpql_denominator():
    """Proven against the real summary builder, on the 71 + 1 production shape.

    This is the assertion the audit must never be "fixed" into contradicting:
    an unattributed SQL in the denominator would silently lower canonical CPQL.
    """
    mapped_counts = _spread(71, 18)
    campaigns = [
        ces._row({"spend_currency": "GBP"}, f"g{i}", f"Campaign {i}",
                 {"native": 100.0, "usd": 125.0, "fx_complete": True},
                 _outcomes(f"Campaign {i}", n),
                 True, True, {"junk_heavy_pct": 25.0, "small_sample": 5},
                 aliases=[], mapping_status="mapped", is_mapping_review=False)
        for i, n in enumerate(mapped_counts)
    ]
    review = _outcomes("Unmapped Label", 1)
    campaigns.append(
        ces._row({"spend_currency": "GBP"}, "unmatched:unmapped label",
                 "Unmapped Label", None, review, True, True,
                 {"junk_heavy_pct": 25.0, "small_sample": 5},
                 aliases=[], mapping_status="unmatched", is_mapping_review=True))

    spend_result = {"total_spend": 1800.0, "total_spend_usd": 2250.0,
                    "currency_code": "GBP", "fx_complete": True}
    summary, _sums = ces._build_summary(
        campaigns, spend_result, {}, True, True, True,
        unmatched={"unmapped label": review},
        excluded=_outcomes(None, 4))

    assert sum(c["confirmed_sqls"] for c in campaigns) == 72
    assert summary["confirmed_sqls_total"] == 71, (
        "the headline total is the mapped population only")
    assert summary["mapping_coverage"]["unmatched_sqls"] == 1
    assert summary["mapping_coverage"]["excluded_not_google_sqls"] == 4
    assert summary["mapping_coverage"]["total_paid_search_sqls"] == 76
    assert summary["overall_cpql_scope"] == "mapped_only"
    assert summary["overall_cpql_usd"] == round(2250.0 / 71, 2), (
        "CPQL divides by the mapped SQLs only — dividing by 72 would let an "
        "unattributed lead lower the published cost per qualified lead")

    # …and the corrected audit certifies exactly that payload.
    violations, _ = _check("180d", campaigns, summary)
    assert violations == [], violations


def test_14_the_service_output_is_what_the_audit_reconciles():
    """No translation layer between the product and the gate.

    The check runs against the same row dictionaries and the same summary the
    API returns, so the gate cannot pass on a shape production never produces.
    """
    import inspect
    src = inspect.getsource(audit._audit_window)
    assert "check_summary_population_reconciliation(" in src
    assert 'payload.get("summary")' in inspect.getsource(audit._audit_window)


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Negative control — the gate must fail if the populations are merged again
# ═════════════════════════════════════════════════════════════════════════════

#: Reconcile a payload handed in on stdin and print the violation names.
#: Executed in a SUBPROCESS so a patched source tree can never leak into this
#: interpreter's already-imported modules.
_RECON_PROBE = """
import json, sys
sys.path.insert(0, %r)
from scripts import audit_campaign_evidence_certification as audit
data = json.loads(sys.stdin.read())
f = audit.Findings()
audit.check_summary_population_reconciliation(
    data["window"], data["campaigns"], data["summary"], f)
for v in f.violations:
    print(v.split(":")[0])
"""


def _recon_violations(window, campaigns, summary, patches=()) -> set[str]:
    """Violation names from a subprocess run, optionally over a patched audit.

    The audit reads its own source only indirectly here — what is patched is the
    behaviour under test — but the subprocess is still the right boundary: an
    in-process reload would leave this interpreter holding two copies of the
    module, which is how a previous version of this pattern broke fifty
    unrelated tests.
    """
    path = _ROOT / _AUDIT_SRC
    original = path.read_text()
    try:
        if patches:
            src = original
            for old, new in patches:
                assert old in src, f"patch anchor not found: {old[:60]!r}"
                src = src.replace(old, new, 1)
            path.write_text(src)

        payload = json.dumps({"window": window, "campaigns": campaigns,
                              "summary": summary})
        result = subprocess.run(
            [sys.executable, "-c", _RECON_PROBE % str(_ROOT)],
            input=payload, capture_output=True, text=True, cwd=str(_ROOT))
        assert result.returncode == 0, (
            f"the reconciliation probe crashed:\n{result.stdout}\n{result.stderr}")
        return {line.strip().split("[")[0]
                for line in result.stdout.splitlines() if line.strip()}
    finally:
        path.write_text(original)


def test_15_baseline_the_production_shape_is_clean_in_a_fresh_interpreter():
    """Without this baseline the negative control below proves nothing."""
    campaigns, summary = _shaped(_spread(71, 18), [1], 4)
    assert _recon_violations("180d", campaigns, summary) == set()


def test_16_negative_control_merging_the_populations_fails_the_audit():
    """Reintroduce the merged sum on disk; the gate must go red.

    This is the exact regression that shipped: summing SQLs across every
    campaign row and comparing the result to the mapped-only headline. On the
    real 180d shape it produces 72 against a published 71.
    """
    campaigns, summary = _shaped(_spread(71, 18), [1], 4)
    violations = _recon_violations("180d", campaigns, summary, patches=[(
        '    mapped_rows = [c for c in campaigns if c.get("mapping_status") == "mapped"]',
        "    mapped_rows = list(campaigns)")])

    assert "mapped_rows_reconcile" in violations, (
        "merging the mapped and unmatched populations must fail the audit — "
        "72 rows-sum against a 71 mapped-only total")
    assert "mapped_coverage_reconcile" in violations


def test_17_negative_control_dropping_review_rows_also_fails_the_audit():
    """The other tempting "fix": hide Mapping Review rows so the sums agree.

    It makes the arithmetic work and loses the SQLs an operator has to act on,
    so the gate must reject it too.
    """
    campaigns, summary = _shaped(_spread(71, 18), [1], 4)
    campaigns = [c for c in campaigns if c["mapping_status"] != "unmatched"]

    violations, _ = _check("180d", campaigns, summary)
    assert "unmatched_rows_reconcile" in _names(violations)


@pytest.mark.parametrize("window", audit.WINDOWS)
def test_18_the_check_is_wired_into_every_audited_window(window):
    """A 180d-only correction would leave all_time shipping the same defect."""
    campaigns, summary = _shaped(_spread(12, 4), [2], 1)
    violations, scopes = _check(window, campaigns, summary)
    assert violations == [], violations
    assert scopes["campaign_attributable_sqls"]["sqls"] == 12


# ═════════════════════════════════════════════════════════════════════════════
# 6 · The command itself, over a real production-shaped database
# ═════════════════════════════════════════════════════════════════════════════
#
# Everything above reasons about payload shapes. This section builds the shape
# out of rows in a live PostgreSQL server and runs the packaged command against
# it, because the mismatch that shipped was not visible in any unit fixture —
# it appeared the first time the gate met an account that had both a mapped
# campaign and a Mapping Review row.

from datetime import date, datetime, timedelta, timezone  # noqa: E402

from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

F1_ACCOUNT = "555"
F1_DAY = date.today() - timedelta(days=3)

#: Three distinct canonical campaigns. Distinct names matter: two campaigns
#: sharing a normalized display name are deliberately NOT auto-mapped, so a
#: shared name here would quietly turn mapped rows into Mapping Review rows and
#: the fixture would stop testing what it claims to.
F1_CAMPAIGNS = (
    ("30001", "winfleet uk - brand", 4),
    ("30002", "winfleet uk - generic", 3),
    ("30003", "winfleet gulf - competitors", 2),
)
#: A HubSpot label with no canonical identity → one Mapping Review row.
F1_REVIEW_LABEL = "legacy paid label 2019"
F1_REVIEW_SQLS = 2
#: A label approved as NOT Google Ads → excluded, and it gets no campaign row.
F1_EXCLUDED_LABEL = "linkedin sponsored content"
F1_EXCLUDED_SQLS = 1

F1_MAPPED_SQLS = sum(n for _, _, n in F1_CAMPAIGNS)          # 9
F1_TOTAL_PAID = F1_MAPPED_SQLS + F1_REVIEW_SQLS + F1_EXCLUDED_SQLS   # 12


@pytest.fixture()
def f1_seeded(pg, monkeypatch):  # noqa: F811
    """An account with all three paid-search SQL populations present at once.

    This is the configuration the merged gate could not certify: the sum over
    every campaign row (11) is not the published mapped-only total (9).
    """
    import db.connection as connection
    monkeypatch.setenv("DATABASE_URL", pg.url)
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", F1_ACCOUNT)
    connection._pool = None
    connection.init_pool()
    from db.schema import init_db
    init_db()

    def _exec(sql, params=()):
        with connection.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone() if cur.description else None

    for cid, name, _sqls in F1_CAMPAIGNS:
        _exec("INSERT INTO google_ads_campaign_daily_spend "
              "(customer_id, currency_code, campaign_id, campaign_name, "
              " spend_date, cost_micros, spend_account_currency) "
              "VALUES (%s,'GBP',%s,%s,%s,%s,%s)",
              (F1_ACCOUNT, cid, name, F1_DAY, 120_000_000, 120.0))

    # The not-Google-Ads label is approved as such; without the approved row it
    # would fall through to Mapping Review and the excluded population would be
    # empty — a fixture that silently tests two populations instead of three.
    _exec("INSERT INTO google_ads_campaign_identity "
          "(customer_id, campaign_id, external_campaign_label, match_method, "
          " approved_at, approved_by) "
          "VALUES (%s, NULL, %s, 'not_google_ads', %s, 'pr-ads-157-f1')",
          (F1_ACCOUNT, F1_EXCLUDED_LABEL, datetime.now(tz=timezone.utc)))

    run_id = _exec("INSERT INTO runs (run_type, started_at, status) "
                   "VALUES ('daily', %s, 'success') RETURNING id",
                   (datetime.now(tz=timezone.utc),))[0]

    def _lead(contact_id, label, status):
        _exec("INSERT INTO leads (run_id, run_date, contact_id, campaign_name, "
              " keyword, country, mql_status, status_category, gclid, "
              " source_type, company, contact_created_at) "
              "VALUES (%s,%s,%s,%s,'kw','UK','x',%s,'g','paid_search','Acme',%s)",
              (run_id, F1_DAY, contact_id, label, status,
               datetime.combine(F1_DAY, datetime.min.time(), tzinfo=timezone.utc)))

    n = 0
    for _cid, name, sqls in F1_CAMPAIGNS:
        for _ in range(sqls):
            n += 1
            _lead(f"c{n}", name, "qualified")
        n += 1
        _lead(f"c{n}", name, "junk")          # a non-SQL lead on the same campaign
    for _ in range(F1_REVIEW_SQLS):
        n += 1
        _lead(f"c{n}", F1_REVIEW_LABEL, "qualified")
    for _ in range(F1_EXCLUDED_SQLS):
        n += 1
        _lead(f"c{n}", F1_EXCLUDED_LABEL, "qualified")

    yield pg


@_needs_pg
def test_19_pg_the_production_shape_is_built_from_real_rows(f1_seeded):
    """The service really does produce the mismatching pair the gate saw.

    Asserted before the audit runs: if the fixture failed to create a Mapping
    Review row, the audit test below would pass for the wrong reason.
    """
    from services.campaign_evidence_service import build_campaign_evidence
    payload = build_campaign_evidence("30d")
    campaigns = payload["campaigns"]
    summary = payload["summary"]
    cov = summary["mapping_coverage"]

    review = [c for c in campaigns if c["mapping_status"] == "unmatched"]
    assert len(review) == 1, [c["campaign_name"] for c in campaigns]
    assert summary["confirmed_sqls_total"] == F1_MAPPED_SQLS
    assert cov["unmatched_sqls"] == F1_REVIEW_SQLS
    assert cov["excluded_not_google_sqls"] == F1_EXCLUDED_SQLS
    assert cov["total_paid_search_sqls"] == F1_TOTAL_PAID
    assert sum(c["confirmed_sqls"] for c in campaigns) == (
        F1_MAPPED_SQLS + F1_REVIEW_SQLS), (
        "the naive all-rows sum must differ from the published total — that "
        "difference is the entire defect")

    violations, scopes = _check("30d", campaigns, summary)
    assert violations == [], violations
    assert scopes["campaign_attributable_sqls"]["sqls"] == F1_MAPPED_SQLS
    assert scopes["unmatched_sqls"]["sqls"] == F1_REVIEW_SQLS


@_needs_pg
def test_20_pg_the_audit_command_certifies_the_production_shape(f1_seeded):
    """`python -m scripts.audit_campaign_evidence_certification` on real rows.

    Run as a subprocess so the command's own pool initialisation and exit code
    are what is being tested, not this process's already-open connection.
    """
    import os
    env = {**os.environ, "DATABASE_URL": f1_seeded.url,
           "GOOGLE_ADS_CUSTOMER_ID": F1_ACCOUNT}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.audit_campaign_evidence_certification",
         "--window", "30d", "--json"],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)

    report = json.loads(result.stdout)
    assert report["violations"] == [], report["violations"]
    assert result.returncode != 1, (
        "a production-shaped account with Mapping Review rows must not be "
        "reported as a truth-contract violation")
    assert report["external_writes_performed"] is False

    pops = report["per_window"]["30d"]["sql_populations"]
    assert pops["campaign_attributable_sqls"]["sqls"] == F1_MAPPED_SQLS
    assert pops["unmatched_sqls"]["sqls"] == F1_REVIEW_SQLS
    assert pops["excluded_not_google_sqls"]["sqls"] == F1_EXCLUDED_SQLS
    assert pops["total_paid_search_sqls"]["sqls"] == F1_TOTAL_PAID
