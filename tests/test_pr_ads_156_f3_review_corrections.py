"""PR-ADS-156-F3 review corrections — regression coverage.

The F3 review found one arithmetic defect and three gaps in how far the cutover
had actually been carried. This module covers all four.

§1 — the false-green residual. The audit subtracts two explained populations
from `interval_canonical_provenance.rows_missing_identity`, so that one broken
row yields one code rather than three. That subtraction is only sound while both
subtrahends are SUBSETS of the minuend. As reviewed, the orphan query counted
every null-account row in the window regardless of provenance — so a single
unmatched Windsor row could cancel a genuinely malformed Google Ads row and the
audit would report a clean cutover over a database that still had one. The tests
below fix the population boundaries in place.

§2 — the operational scripts read `search_terms` by date alone, which is the
exact defect F3 removed from the production readers. An operator running a
verification command is asking the same question the dashboard asks and must get
the same population back.

§3 — the endpoint tests were AST assertions. An AST test proves a call was
written; it cannot prove the composed SQL binds its parameters in the right
order, and parameter order is precisely what breaks when a scope predicate is
spliced into a hand-built WHERE clause.

§4 — the suite-wide `GOOGLE_ADS_CUSTOMER_ID` default. A default that is always
present makes fail-closed behaviour untestable by construction: every test
passes because the configuration is never absent.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import search_term_scope as scope_mod  # noqa: E402
from scripts import audit_keyword_search_term_freshness as audit  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)
from tests.test_pr_ads_156_f3_account_identity_cutover import (  # noqa: E402
    ACCOUNT, DAY, OTHER_ACCOUNT, _TWIN_COLUMNS, _exec, _init_db,
    _run_audit, _scalar, _seed_batch, _seed_pre_cutover_twin,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")


def _insert(**over):
    """One `search_terms` row, spelled out. Every column that participates in
    identity is named, because the point of these tests is which population a
    row falls into and that is decided entirely by those columns."""
    values = {
        "source_date": DAY, "customer_id": None, "campaign_name": "brand - uk",
        "campaign_id": "10", "ad_group": "Core", "keyword": "freight",
        "match_type": "BROAD", "search_term": "freight forwarding software",
        "cost_micros": 5_000_000, "currency_code": "GBP",
        "source_system": "google_ads_api", "spend_usd": 5.0, "clicks": 3,
        "impressions": 40,
    }
    values.update(over)
    _exec(f"INSERT INTO search_terms ({_TWIN_COLUMNS}) VALUES "
          "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", tuple(values.values()))


def _search_dataset(pg):  # noqa: F811
    res = _run_audit("--json", env_extra={"DATABASE_URL": pg.url})
    payload = json.loads(res.stdout)
    dataset = next(d for d in payload["datasets"]
                   if d["dataset"] == "search_terms")
    return res, payload, dataset


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the residual may only be reduced by rows drawn from the same population
# ═════════════════════════════════════════════════════════════════════════════
@_needs_pg
def test_1_a_windsor_orphan_cannot_cancel_a_malformed_google_ads_row(
        pg, monkeypatch):  # noqa: F811
    """The reviewed false green, reproduced exactly.

    Two rows, one of each kind: this account's own Google Ads row written
    without a campaign id (a real, current, fixable defect), and an unrelated
    Windsor row with no account (history nobody can repair). Before the fix the
    orphan count was provenance-blind, so it returned 1, the residual computed
    1 - 0 - 1 = 0, and `missing_identity` never fired. The malformed row was
    still there; only the arithmetic had absorbed it.
    """
    _init_db(pg, monkeypatch)
    _insert(customer_id=ACCOUNT, campaign_id=None,
            search_term="malformed canonical row")
    _insert(customer_id=None, source_system="windsor",
            search_term="unrelated windsor history")
    _seed_batch(DAY)

    res, payload, search = _search_dataset(pg)

    # The Windsor row is outside the canonical population, so it neither
    # explains anything nor cancels anything.
    assert search["unmatched_null_customer_rows"] == 0
    assert search["noncanonical_null_customer_rows"] == 1
    assert search["null_customer_twins"] == 0

    # And the defect it used to hide is reported, and blocks.
    assert audit.V_MISSING_IDENTITY in payload["violation_codes"]
    assert res.returncode == 1, res.stdout + res.stderr
    detail = next(v["detail"] for v in payload["violations"]
                  if v["code"] == audit.V_MISSING_IDENTITY)
    assert detail.startswith("1 canonical-provenance row(s)")


@_needs_pg
def test_2_a_true_canonical_twin_is_named_a_pre_cutover_twin(pg, monkeypatch):  # noqa: F811
    """The one case that IS a twin: canonical provenance on both sides, the
    configured account on the replacement, and every other key component equal.
    It is reported as a supersession failure and nothing else, because calling
    it a generic identity fault would send an operator looking for a bug in
    ingestion that is working correctly."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin()                       # null account, canonical
    _insert(customer_id=ACCOUNT)                   # its exact replacement
    _seed_batch(DAY)

    res, payload, search = _search_dataset(pg)

    assert search["null_customer_twins"] == 1
    assert search["unmatched_null_customer_rows"] == 0
    assert audit.V_NULL_CUSTOMER_TWIN in payload["violation_codes"]
    assert audit.V_MISSING_IDENTITY not in payload["violation_codes"]
    assert res.returncode == 1, res.stdout + res.stderr


@_needs_pg
def test_2b_a_replacement_from_another_account_is_not_a_replacement(
        pg, monkeypatch):  # noqa: F811
    """Same shape as the twin above, except the complete row belongs to a
    DIFFERENT account. Another account's observation of the same term on the
    same day is a different fact, not a supersession of this one — so the
    null-account row is unmatched history, not a twin awaiting cleanup."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin()
    _insert(customer_id=OTHER_ACCOUNT)
    _seed_batch(DAY)

    _res, payload, search = _search_dataset(pg)

    assert search["null_customer_twins"] == 0
    assert search["unmatched_null_customer_rows"] == 1
    assert audit.V_NULL_CUSTOMER_TWIN not in payload["violation_codes"]


@_needs_pg
def test_2c_a_windsor_replacement_is_not_a_replacement_either(pg, monkeypatch):  # noqa: F811
    """And the mirror: the complete row carries the configured account but was
    not produced by the Google Ads API. Supersession is a claim that the SAME
    observation now exists in canonical form; a row from another system is not
    that observation."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin()
    _insert(customer_id=ACCOUNT, source_system="windsor")
    _seed_batch(DAY)

    _res, payload, search = _search_dataset(pg)

    assert search["null_customer_twins"] == 0
    assert search["unmatched_null_customer_rows"] == 1


@_needs_pg
def test_3_an_unmatched_canonical_null_account_row_is_disclosed_separately(
        pg, monkeypatch):  # noqa: F811
    """A canonical row with no account and nothing to replace it is history:
    disclosed under its own code, excluded from every metric, never repaired
    here and never given an invented account.

    It is deliberately NOT a violation. A check that can only ever be red over
    rows nobody is permitted to touch is a check nobody reads.
    """
    _init_db(pg, monkeypatch)
    _insert(customer_id=ACCOUNT)
    _seed_pre_cutover_twin(search_term="orphaned historical query")
    _seed_batch(DAY)

    res, payload, search = _search_dataset(pg)

    assert search["unmatched_null_customer_rows"] == 1
    assert search["null_customer_twins"] == 0
    assert search["noncanonical_null_customer_rows"] == 0

    codes = {d["code"] for d in payload["disclosures"]}
    assert audit.D_UNMATCHED_NULL_CUSTOMER in codes
    assert audit.D_NONCANONICAL_NULL_CUSTOMER not in codes
    # No IDENTITY verdict of any kind. The fixture day is deliberately old, so
    # the run is stale and exits non-zero for that reason — which is the point
    # of naming codes rather than reading an exit status: an exit code is a
    # summary of every check, and this test is about one of them.
    assert audit.V_MISSING_IDENTITY not in payload["violation_codes"]
    assert audit.V_NULL_CUSTOMER_TWIN not in payload["violation_codes"]
    assert res.returncode == 1, res.stdout + res.stderr

    # Disclosure means disclosure: the row is still there, still account-less.
    assert _scalar("SELECT customer_id FROM search_terms "
                   "WHERE search_term = 'orphaned historical query'") is None


@_needs_pg
def test_4_non_google_history_never_alters_a_canonical_defect(pg, monkeypatch):  # noqa: F811
    """Scale the mixed case up and hold the canonical verdict fixed.

    Three malformed canonical rows and a pile of non-Google history. However
    much of the second there is, the first is reported in full: the residual is
    computed over one population and reduced only by subsets of it.
    """
    _init_db(pg, monkeypatch)
    for i in range(3):
        _insert(customer_id=ACCOUNT, campaign_id=None,
                search_term=f"malformed canonical {i}")
    for i in range(9):
        _insert(customer_id=None, source_system="windsor",
                search_term=f"windsor history {i}")
    _insert(customer_id=None, source_system=None,
            search_term="unlabelled history")
    _seed_batch(DAY)

    res, payload, search = _search_dataset(pg)

    assert search["unmatched_null_customer_rows"] == 0
    # An unlabelled row counts as non-canonical too. COALESCE, not a bare
    # comparison — otherwise `NULL <> 'google_ads_api'` is NULL and the row
    # falls out of every bucket, which is the quietest way to lose evidence.
    assert search["noncanonical_null_customer_rows"] == 10

    detail = next(v["detail"] for v in payload["violations"]
                  if v["code"] == audit.V_MISSING_IDENTITY)
    assert detail.startswith("3 canonical-provenance row(s)")
    assert res.returncode == 1, res.stdout + res.stderr

    # The disclosure reports them, and says in words that it is inert.
    disclosure = next(d for d in payload["disclosures"]
                      if d["code"] == audit.D_NONCANONICAL_NULL_CUSTOMER)
    assert "10 row(s)" in disclosure["detail"]


@_needs_pg
def test_4b_the_residual_never_goes_negative(pg, monkeypatch):  # noqa: F811
    """Twins and orphans are disjoint by construction (EXISTS vs NOT EXISTS)
    and both are subsets of the canonical missing-identity population, so the
    residual is non-negative on the arithmetic alone. The clamp stays anyway:
    a future scope change that broke the subset property would otherwise turn
    a negative residual into a silent pass, and this asserts the mixed
    population that would exercise it."""
    _init_db(pg, monkeypatch)
    _seed_pre_cutover_twin()                              # twin
    _insert(customer_id=ACCOUNT)                          # its replacement
    _seed_pre_cutover_twin(search_term="orphan a")        # unmatched history
    _seed_pre_cutover_twin(search_term="orphan b")
    _insert(customer_id=None, source_system="windsor",
            search_term="windsor")                        # inert
    _seed_batch(DAY)

    _res, payload, search = _search_dataset(pg)

    provenance = search["interval_canonical_provenance"]["rows_missing_identity"]
    assert provenance == 3                                # twin + two orphans
    assert search["null_customer_twins"] == 1
    assert search["unmatched_null_customer_rows"] == 2
    assert search["noncanonical_null_customer_rows"] == 1
    # 3 - 1 - 2 = 0. Every account-less canonical row is accounted for by
    # exactly one explanation, and none of them is `missing_identity`.
    assert audit.V_MISSING_IDENTITY not in payload["violation_codes"]


def test_4c_both_cutover_queries_bound_provenance_on_both_sides():
    """A source-level guard on the property the tests above depend on.

    The subtraction is only valid while the twin and orphan populations are
    subsets of the canonical-provenance population. That is enforced in SQL, so
    it is asserted in SQL: both the outer row and the candidate replacement must
    be bound by `source_system`, and the replacement must be bound by account.
    """
    for name in ("_SEARCH_TERM_TWINS", "_SEARCH_TERM_ORPHANS"):
        sql = getattr(audit, name)
        assert sql.count("source_system") == 2, name
        assert "twin.customer_id IS NULL" in sql, name
        assert "canonical.customer_id = ANY(%s)" in sql, name
        assert "canonical.id <> twin.id" in sql, name

    # And the inert one is never bound to an account, because it is never
    # compared against the canonical population at all.
    assert "ANY(" not in audit._SEARCH_TERM_NONCANONICAL_HISTORY


def test_4d_the_residual_subtracts_only_canonical_populations():
    """The arithmetic itself, read from source.

    `noncanonical_null_customer_rows` is fetched and published; the assertion is
    that it never reaches the subtraction. A reviewer adding it there later —
    reasonably, since it looks like the other two — would reintroduce exactly
    the defect this section fixed.
    """
    src = Path(audit.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assign = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "residual"
                for t in node.targets))
    names = {n.id for n in ast.walk(assign.value) if isinstance(n, ast.Name)}
    assert {"provenance", "twins", "unmatched"} <= names
    assert "noncanonical" not in names
    # Clamped at zero rather than allowed to wrap into a pass.
    assert isinstance(assign.value, ast.Call)
    assert getattr(assign.value.func, "id", None) == "max"


# ═════════════════════════════════════════════════════════════════════════════
# §2 — operational commands read the same population the product reads
# ═════════════════════════════════════════════════════════════════════════════
def test_5_no_unscoped_search_term_reader_exists():
    """The contract guard, run over the real tree.

    An empty result here is a genuine all-clear: unlike a database read this
    scan cannot be refused — the files are either parsed or the repository is
    not there.
    """
    from analysis.legacy_source_guard import scan_unscoped_search_term_readers

    findings = scan_unscoped_search_term_readers()
    assert findings == [], "\n".join(f["detail"] for f in findings)


def test_5b_the_guard_catches_a_new_unscoped_reader(tmp_path):
    """A guard nobody has seen fail is a guard nobody knows works.

    The synthetic module below is the exact shape the review found in the
    operational scripts: a literal query bounded on `source_date` alone.
    """
    from analysis.legacy_source_guard import (
        REASON_UNSCOPED_SEARCH_TERM_READ, scan_unscoped_search_term_readers,
    )

    pkg = tmp_path / "services"
    pkg.mkdir()
    (pkg / "new_reader.py").write_text(
        "def total(cur, start, end):\n"
        "    cur.execute('SELECT SUM(spend_usd) FROM search_terms '\n"
        "                'WHERE source_date >= %s AND source_date <= %s',\n"
        "                (start, end))\n"
        "    return cur.fetchone()[0]\n", encoding="utf-8")

    findings = scan_unscoped_search_term_readers(
        root=tmp_path, directories=("services",))
    assert len(findings) == 1
    assert findings[0]["reason"] == REASON_UNSCOPED_SEARCH_TERM_READ
    assert findings[0]["function"] == "total"
    assert "never mentions `customer_id`" in findings[0]["detail"]


def test_5c_the_guard_accepts_the_scoped_form_and_ignores_writes(tmp_path):
    """Both halves matter. A guard that flagged correct code would be turned
    off within a week, and one that flagged the writer's supersession DELETE
    would be flagging the very mechanism that fixes the problem."""
    from analysis.legacy_source_guard import scan_unscoped_search_term_readers

    pkg = tmp_path / "services"
    pkg.mkdir()
    (pkg / "scoped_reader.py").write_text(
        "from analysis.search_term_scope import canonical_scope\n"
        "def total(cur, start, end):\n"
        "    scope = canonical_scope(start, end)\n"
        "    if not scope.available:\n"
        "        return None\n"
        "    cur.execute('SELECT SUM(spend_usd) FROM search_terms WHERE '\n"
        "                + scope.sql, scope.params)\n"
        "    return cur.fetchone()[0]\n", encoding="utf-8")
    (pkg / "writer.py").write_text(
        "def supersede(cur):\n"
        "    cur.execute('DELETE FROM search_terms WHERE customer_id IS NULL')\n",
        encoding="utf-8")

    assert scan_unscoped_search_term_readers(
        root=tmp_path, directories=("services",)) == []


def test_5d_every_scope_exemption_names_why_it_is_historical():
    """An allowlist nobody maintains is a hole with documentation attached."""
    from analysis.legacy_source_guard import SEARCH_TERM_SCOPE_ALLOWLIST

    assert SEARCH_TERM_SCOPE_ALLOWLIST, "an empty allowlist would be suspicious"
    for entry, reason in SEARCH_TERM_SCOPE_ALLOWLIST.items():
        assert len(reason) > 40, f"{entry} is exempt without a real reason"
        # The two legitimate categories, named in the reason itself.
        assert any(w in reason.lower() for w in ("historical", "guard itself")), \
            f"{entry} is exempt for a reason that is not a historical diagnostic"


def test_6_the_verification_command_fails_closed_without_an_account(monkeypatch):
    """Zero rows because nothing was configured must not read as zero rows
    because the pipeline is broken. Those call for opposite responses, and only
    one of them is fixed by an environment variable."""
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    import importlib

    verify = importlib.import_module("scripts.verify_search_terms_pipeline")
    db = verify._check_db(30)
    assert db["scope_available"] is False
    assert db["scope_reason"] == scope_mod.REASON_CUSTOMER_NOT_CONFIGURED
    assert db["rows_requested_window"] == 0
    assert db["available"] is False

    verdict, reason = verify.compute_search_terms_verdict(
        db_available=False, scope_available=False)
    assert verdict == verify.Verdict.ACCOUNT_NOT_CONFIGURED
    assert "GOOGLE_ADS_CUSTOMER_ID" in reason


def test_6b_the_waste_truth_audit_fails_closed_without_an_account(monkeypatch):
    """Same rule, second command. `_canonical_facts` is NAMED canonical, so
    returning an unscoped total from it would be the most direct untruth in the
    codebase."""
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    import importlib

    waste = importlib.import_module("scripts.audit_search_term_waste_truth")
    facts = waste._canonical_facts(DAY - timedelta(days=30), DAY)
    assert facts["available"] is False
    assert facts["reason"] == scope_mod.REASON_CUSTOMER_NOT_CONFIGURED


@_needs_pg
def test_7_the_verification_command_counts_canonical_rows_only(pg, monkeypatch):  # noqa: F811
    """Executed, not inspected.

    A twin, a foreign account and a Windsor row alongside one canonical row.
    Before the fix this command reported 4 and called the pipeline healthy —
    the operator's own verification would have confirmed the doubled table.
    """
    _init_db(pg, monkeypatch)
    today = date.today()
    _insert(customer_id=ACCOUNT, source_date=today)
    _insert(customer_id=None, source_date=today)                    # twin
    _insert(customer_id=OTHER_ACCOUNT, source_date=today,
            search_term="another account")
    _insert(customer_id=None, source_system="windsor", source_date=today,
            search_term="windsor row")
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 4

    import importlib

    verify = importlib.reload(
        importlib.import_module("scripts.verify_search_terms_pipeline"))
    db = verify._check_db(30)

    assert db["scope_available"] is True
    assert db["rows_requested_window"] == 1
    assert db["rows_30d"] == 1
    # The excluded rows are disclosed rather than silently dropped, so an
    # operator comparing this against a raw count is not left guessing.
    assert db["null_customer_rows_in_window"] == 2


@_needs_pg
def test_7b_the_waste_truth_audit_counts_canonical_rows_only(pg, monkeypatch):  # noqa: F811
    """The same fixture through the second command, including the money."""
    _init_db(pg, monkeypatch)
    _insert(customer_id=ACCOUNT, spend_usd=5.0)
    _insert(customer_id=None, spend_usd=5.0)                        # twin
    _insert(customer_id=OTHER_ACCOUNT, spend_usd=5.0,
            search_term="another account")
    _insert(customer_id=None, source_system="windsor", spend_usd=5.0,
            search_term="windsor row")

    import importlib

    waste = importlib.reload(
        importlib.import_module("scripts.audit_search_term_waste_truth"))
    facts = waste._canonical_facts(DAY - timedelta(days=7), DAY)

    assert facts["available"] is True
    assert facts["fact_rows"] == 1
    # 5.0, not 20.0. This is the number the retired page published.
    assert facts["spend_usd"] == pytest.approx(5.0)
    assert facts["unique_identities"] == 1


@_needs_pg
def test_7c_quality_counts_are_taken_over_the_claimed_population(pg, monkeypatch):  # noqa: F811
    """A count of malformed rows cannot be taken inside a filter that requires
    them to be well-formed: it would read zero forever, over any table.

    `claimed_scope` is provenance and account WITHOUT the completeness clauses,
    which is the only population over which "how many are broken" is a question
    with an answer.
    """
    _init_db(pg, monkeypatch)
    _insert(customer_id=ACCOUNT)                                    # healthy
    _insert(customer_id=ACCOUNT, campaign_name=None,
            search_term="no campaign name")                         # malformed
    _insert(customer_id=None, campaign_name=None,
            search_term="historical, not ours")                     # excluded

    import importlib

    verify = importlib.reload(
        importlib.import_module("scripts.verify_search_terms_pipeline"))
    db = verify._check_db(30)

    # The account's own malformed row is visible…
    assert db["null_campaign_count"] == 1
    # …and does not silently join the canonical totals.
    assert db["rows_with_spend"] == 2   # both of this account's rows have spend
    assert scope_mod.claimed_scope().available is True


# ═════════════════════════════════════════════════════════════════════════════
# §3 — the endpoints, executed against a real database
# ═════════════════════════════════════════════════════════════════════════════
#
# The F3 endpoint tests asserted over the AST: that `_canonical_search_term_scope`
# appeared in the handler and that a fail-closed branch existed. That proves the
# call was written. It cannot prove the composed SQL binds its parameters in the
# right order — and parameter order is exactly what breaks when a scope predicate
# is spliced into a hand-built WHERE clause, because `psycopg2` fills `%s` by
# position and a predicate inserted at the front shifts every argument after it.
# A misordered query does not raise; it filters on the wrong values and returns a
# plausible number.
#
# So these run the handlers over PostgreSQL, seeded with the four rows that
# matter, and assert on results.

def _api_client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:  # pragma: no cover - environment guard
        pytest.skip("fastapi[testclient] not available")
    import os

    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    try:
        from api.server import app
    except Exception as exc:  # noqa: BLE001 - environment guard
        pytest.skip(f"api.server import failed: {exc}")
    return TestClient(app, raise_server_exceptions=False)


def _viewer_cookies():
    import os

    from starlette.responses import Response as StarletteResponse

    from api.auth import set_session

    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    r = StarletteResponse()
    set_session(r, "testviewer", "viewer")
    for part in r.headers.get("set-cookie", "").split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return {"ads_session": part.split("=", 1)[1]}
    return {}


def _seed_mixed_population():
    """The four rows the cutover has to tell apart, on today's date so every
    endpoint's default window contains them.

    Only the first belongs in any total. The second is its own pre-cutover twin
    — same observation, no account — and is the row that doubled every metric.
    """
    today = date.today()
    _insert(customer_id=ACCOUNT, source_date=today, spend_usd=5.0, clicks=3,
            impressions=40)
    _insert(customer_id=None, source_date=today, spend_usd=5.0, clicks=3,
            impressions=40)
    _insert(customer_id=OTHER_ACCOUNT, source_date=today, spend_usd=11.0,
            clicks=7, impressions=90, search_term="another account term")
    _insert(customer_id=None, source_system="windsor", source_date=today,
            spend_usd=13.0, clicks=9, impressions=110,
            search_term="windsor era term")
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 4


@_needs_pg
def test_13_raw_search_terms_returns_only_the_canonical_row(pg, monkeypatch):  # noqa: F811
    """One row out, not four — and specifically the one carrying the account."""
    _init_db(pg, monkeypatch)
    _seed_mixed_population()

    client = _api_client()
    r = client.get("/api/search-terms?days=30&limit=500",
                   cookies=_viewer_cookies())
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body["rows"]

    assert len(rows) == 1, [row["search_term"] for row in rows]
    assert rows[0]["search_term"] == "freight forwarding software"
    assert rows[0]["spend_usd"] == pytest.approx(5.0)
    # The other three are not in the payload under any label.
    terms = {row["search_term"] for row in rows}
    assert "another account term" not in terms
    assert "windsor era term" not in terms


@_needs_pg
def test_13b_a_filter_argument_still_binds_to_the_right_column(pg, monkeypatch):  # noqa: F811
    """Parameter order, tested by making it matter.

    The scope contributes its own placeholders at the FRONT of the WHERE clause.
    If the handler appended the caller's filter values in the wrong order, the
    query would silently compare the search text against a date or an account
    id — and return zero rows, which looks exactly like "no matches".
    """
    _init_db(pg, monkeypatch)
    _seed_mixed_population()
    _insert(customer_id=ACCOUNT, source_date=date.today(), spend_usd=2.0,
            search_term="second canonical term", campaign_name="brand - de",
            match_type="EXACT")

    client, cookies = _api_client(), _viewer_cookies()

    hit = client.get("/api/search-terms?days=30&limit=500&q=forwarding",
                     cookies=cookies).json()
    assert [r["search_term"] for r in hit["rows"]] == \
        ["freight forwarding software"]

    by_campaign = client.get(
        "/api/search-terms?days=30&limit=500&campaign=brand - de",
        cookies=cookies).json()
    assert [r["search_term"] for r in by_campaign["rows"]] == \
        ["second canonical term"]

    by_match = client.get(
        "/api/search-terms?days=30&limit=500&match_type=EXACT",
        cookies=cookies).json()
    assert [r["search_term"] for r in by_match["rows"]] == \
        ["second canonical term"]

    # A filter matching nothing returns nothing — proving the empty results
    # above are real filtering, not a misbound parameter matching nothing.
    assert client.get("/api/search-terms?days=30&q=zzz-no-such-term",
                      cookies=cookies).json()["rows"] == []


@_needs_pg
def test_14_the_summary_totals_are_not_doubled(pg, monkeypatch):  # noqa: F811
    """The number a person reads and acts on.

    Unscoped this reported 34.0 spend over 4 terms. The twin alone accounted
    for 5.0 of that — the same clicks and the same impressions counted twice,
    from SQL that looked entirely reasonable.
    """
    _init_db(pg, monkeypatch)
    _seed_mixed_population()

    r = _api_client().get("/api/search-terms/summary?days=30",
                          cookies=_viewer_cookies())
    assert r.status_code == 200, r.text
    summary = r.json()["summary"]

    assert summary["total_terms"] == 1
    assert summary["unique_search_terms"] == 1
    assert summary["total_spend_usd"] == pytest.approx(5.0)
    assert summary["total_clicks"] == 3
    assert summary["total_impressions"] == 40

    # The denominator behind the derived rates is the same single row, so the
    # averages describe one observation rather than an average of a fact and
    # its own duplicate.
    assert r.json()["data_quality"]["rows_in_window"] == 1


@_needs_pg
def test_15_ngrams_are_built_from_canonical_rows_only(pg, monkeypatch):  # noqa: F811
    """N-grams aggregate text, so an excluded row does not merely add to a
    total — it invents a phrase that no canonical row contains, and puts it in
    front of someone deciding what to add as a negative keyword."""
    _init_db(pg, monkeypatch)
    _seed_mixed_population()

    r = _api_client().get("/api/search-terms/ngrams?days=30",
                          cookies=_viewer_cookies())
    assert r.status_code == 200, r.text
    body = r.json()

    grams = {g["ngram"] for g in body["rows"]}
    assert grams, body
    assert any("freight" in g for g in grams)
    # Words that exist ONLY in the excluded rows must not appear at all.
    assert not any("windsor" in g for g in grams)
    assert not any("another" in g for g in grams)
    assert not any("account" in g for g in grams)

    for gram in body["rows"]:
        # Spend and row counts are summed per n-gram; the twin would have
        # doubled every one of them.
        assert gram["total_spend_usd"] == pytest.approx(5.0), gram["ngram"]
        assert gram["row_count"] == 1, gram["ngram"]


@_needs_pg
@pytest.mark.parametrize("path", ["/api/search-terms",
                                  "/api/search-terms/summary",
                                  "/api/search-terms/ngrams"])
def test_15b_every_endpoint_fails_closed_on_an_unresolved_account(
        pg, monkeypatch, path):  # noqa: F811
    """Executed, not inspected. With no account configured each endpoint
    returns an empty, explicitly-unavailable payload — not the whole table, and
    not a 500."""
    _init_db(pg, monkeypatch)
    _seed_mixed_population()
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)

    r = _api_client().get(f"{path}?days=30", cookies=_viewer_cookies())
    assert r.status_code == 200, r.text
    body = r.json()

    # `rows` carries the fact rows for two of the three endpoints and the
    # n-gram aggregates for the third, so one assertion covers all of them.
    assert not body.get("rows")
    summary = body.get("summary") or {}
    assert not summary.get("total_terms")
    assert not summary.get("total_spend_usd")
    # And it SAYS so, rather than presenting an empty table as an answer.
    blob = json.dumps(body)
    assert scope_mod.REASON_CUSTOMER_NOT_CONFIGURED in blob, blob[:600]


# ═════════════════════════════════════════════════════════════════════════════
# §4 — the suite-wide account default must not be able to hide fail-closed
# ═════════════════════════════════════════════════════════════════════════════
#
# `tests/conftest.py` gives the session a configured Google Ads account, which is
# right: production always has one, and without it every scoped read would take
# the unavailable branch and hundreds of assertions about page content would pass
# or fail for a reason unrelated to what they test.
#
# The cost is that a default which is always present makes fail-closed behaviour
# untestable by default — no test would notice a consumer that quietly stopped
# handling an unresolved account, because no test would ever see one. Narrowing
# the fixture is not the answer (most suites genuinely need it), so the answer is
# an explicit guard: prove the default yields to any test that removes it, prove
# every scoped consumer fails closed when it does, and prove that list cannot
# fall behind the code.

#: Every function that resolves a search-term scope, and therefore every place
#: an unresolved account must produce "unavailable" rather than a wider query.
#: `test_19` asserts this is EXHAUSTIVE against the source tree, so a new
#: consumer cannot be added without either handling the absent-account case or
#: failing this suite.
SCOPED_CONSUMERS = {
    "db/revenue_repository.py::fetch_search_term_signals",
    "db/search_term_repository.py::fetch_search_term_aggregates",
    "db/search_term_repository.py::fetch_search_term_daily_costs",
    "db/search_term_repository.py::fetch_search_term_daily_for_campaign",
    "api/server.py::_canonical_search_term_scope",
    "api/server.py::_build_search_terms_verdict",
    "scripts/audit_search_term_waste_truth.py::_canonical_facts",
    "scripts/verify_search_terms_pipeline.py::_check_db",
    # The scope module's own complement helper, which delegates to
    # `canonical_scope` and therefore inherits its unavailable result.
    "analysis/search_term_scope.py::unscoped_history_scope",
}


def test_18_the_session_default_yields_to_any_test_that_speaks(monkeypatch):
    """The fixture sets the account only when it is ABSENT, so a test that
    overrides or deletes it gets exactly the environment it asked for.

    If this ever stopped being true, every fail-closed test in the repository
    would silently start exercising the configured path instead — passing, and
    proving nothing.
    """
    from tests.conftest import TEST_GOOGLE_ADS_CUSTOMER_ID

    # The default is in force by default.
    assert scope_mod.configured_customer_id() == TEST_GOOGLE_ADS_CUSTOMER_ID

    # An override wins.
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "999")
    assert scope_mod.configured_customer_id() == "999"
    assert scope_mod.canonical_scope(DAY, DAY).customer_id_candidates == ("999",)

    # And a deletion is a real deletion — the fixture does not put it back.
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    assert scope_mod.configured_customer_id() is None
    assert scope_mod.canonical_scope(DAY, DAY).available is False


def test_18b_an_empty_or_blank_account_is_absent_not_configured(monkeypatch):
    """A variable set to "" or whitespace is the shape a misconfigured
    deployment actually takes — an unset variable is the tidy case, and the
    untidy one must not be treated as a configured account."""
    for value in ("", "   ", "\t"):
        monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", value)
        assert scope_mod.configured_customer_id() is None
        scope = scope_mod.canonical_scope(DAY, DAY)
        assert scope.available is False
        assert scope.reason == scope_mod.REASON_CUSTOMER_NOT_CONFIGURED
        assert scope.sql == "FALSE"
        assert scope.params == ()
        assert scope_mod.claimed_scope(DAY, DAY).available is False


def test_19_the_fail_closed_registry_is_exhaustive():
    """The guard that keeps the guard honest.

    Every function resolving a scope is enumerated from source and compared
    against SCOPED_CONSUMERS. A new consumer added without a fail-closed test
    fails here — which is the whole point, because the session default means it
    would otherwise never meet an unresolved account in any test run.
    """
    found = set()
    for directory in ("db", "api", "services", "scripts", "analysis"):
        for path in sorted((_ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            rel = str(path.relative_to(_ROOT))
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for call in ast.walk(fn):
                    if not isinstance(call, ast.Call):
                        continue
                    name = (getattr(call.func, "id", None)
                            or getattr(call.func, "attr", None))
                    if name in ("canonical_scope", "claimed_scope",
                                "unscoped_history_scope"):
                        found.add(f"{rel}::{fn.name}")
                        break

    missing = found - SCOPED_CONSUMERS
    assert not missing, (
        "New scoped consumer(s) with no fail-closed coverage: "
        + ", ".join(sorted(missing))
        + ". Add a case asserting each returns unavailable with "
          "GOOGLE_ADS_CUSTOMER_ID removed, then list it in SCOPED_CONSUMERS.")
    stale = SCOPED_CONSUMERS - found
    assert not stale, f"SCOPED_CONSUMERS lists functions that no longer exist: {stale}"


def test_20_every_repository_reader_fails_closed_with_the_default_removed(
        monkeypatch):
    """The repositories, with the session default explicitly taken away.

    No database is needed: an unresolved account must be answered before a
    connection is opened, and a reader that got as far as querying would be
    doing so with no account predicate at all.
    """
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)

    import db.revenue_repository as revenue_repo
    import db.search_term_repository as repo

    results = {
        "fetch_search_term_aggregates":
            repo.fetch_search_term_aggregates(DAY - timedelta(days=7), DAY),
        "fetch_search_term_daily_costs":
            repo.fetch_search_term_daily_costs(DAY - timedelta(days=7), DAY),
        "fetch_search_term_daily_for_campaign":
            repo.fetch_search_term_daily_for_campaign(
                DAY - timedelta(days=7), DAY, "brand - uk"),
        "fetch_search_term_signals":
            revenue_repo.fetch_search_term_signals(DAY - timedelta(days=7), DAY),
    }
    for name, result in results.items():
        assert result["available"] is False, name
        assert result["reason"] == scope_mod.REASON_CUSTOMER_NOT_CONFIGURED, name
        assert result["rows"] == [], name


@_needs_pg
def test_20b_a_populated_table_does_not_change_that(pg, monkeypatch):  # noqa: F811
    """The dangerous version of the same case.

    With rows in the table, an unscoped reader would return them and look
    perfectly healthy. Fail-closed has to mean unavailable even when there is
    plenty to return — that is the only case where it matters.
    """
    _init_db(pg, monkeypatch)
    _seed_mixed_population()
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)

    import db.search_term_repository as repo

    result = repo.fetch_search_term_aggregates(date.today() - timedelta(days=7),
                                               date.today())
    assert result["available"] is False
    assert result["rows"] == []
    # The rows are still there — they were withheld, not missing.
    assert _scalar("SELECT COUNT(*) FROM search_terms") == 4


@_needs_pg
def test_21_the_operational_audits_fail_closed_over_a_populated_table(
        pg, monkeypatch):  # noqa: F811
    """The last category: the commands an operator runs by hand.

    All three refuse to answer rather than answering about every account, and
    each says which of the two it is doing.
    """
    _init_db(pg, monkeypatch)
    _seed_mixed_population()
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    import importlib

    verify = importlib.reload(
        importlib.import_module("scripts.verify_search_terms_pipeline"))
    db = verify._check_db(30)
    assert db["scope_available"] is False
    assert db["rows_requested_window"] == 0

    waste = importlib.reload(
        importlib.import_module("scripts.audit_search_term_waste_truth"))
    assert waste._canonical_facts(
        date.today() - timedelta(days=7), date.today())["available"] is False

    # The freshness audit runs in a subprocess, so its environment is set
    # explicitly rather than inherited from this process.
    res = _run_audit("--json", env_extra={"DATABASE_URL": pg.url,
                                          "GOOGLE_ADS_CUSTOMER_ID": ""})
    assert res.returncode == 2, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert payload["violation_codes"] == [audit.V_ACCOUNT_NOT_CONFIGURED]
    assert payload["datasets"] == []
