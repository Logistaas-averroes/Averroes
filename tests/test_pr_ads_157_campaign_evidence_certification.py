"""PR-ADS-157 — Campaign Evidence Certification and Canonical Drawer Completion.

What this suite is about
------------------------
The Campaign drawer was the last consumer in ``api/server.py`` reading the
retired ``keywords`` and ``waste_terms`` snapshot tables directly, and the
Campaign page published a "Confirmed SQLs" KPI and an "Overall CPQL" derived
from it while silently discarding the ``sql_reconciliation`` contract the API
already returned beside them.

Those are two different certification failures with the same shape: a number
rendered as proven when nothing in the code path proved it.

  * The drawer previews matched campaigns on ``lower(btrim(campaign_name))``, so
    two campaigns sharing a display name shared each other's rows; they took the
    latest scheduler snapshot regardless of the Evidence Window the operator had
    selected; and the waste preview presented ``waste_terms.spend_usd`` as the
    term's spend, which PR-ADS-153D established it is not.
  * The KPI strip rendered a ``mismatch``, ``partial`` or ``unavailable``
    reconciliation exactly like a reconciled one.

The tests below are grouped by the claim they defend rather than by the file
they touch, because the same claim is usually enforced in two places at once —
a service contract and the consumer that must honour it.

A note on what these tests will NOT accept
------------------------------------------
Several of them are deliberately written so they cannot be satisfied by
documentation, a variable name, or a comment. ``campaign=`` being "the campaign
key" is proven by resolving a real identity through the shared resolver and
matching it against the filter the evidence service actually applies — not by
observing that both are spelled ``campaign_key``.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import tests.conftest as conftest  # noqa: E402
import services.keyword_evidence_service as kw_svc  # noqa: E402
import services.search_term_evidence_service as st_svc  # noqa: E402

_API_SERVER = _ROOT / "api" / "server.py"
_APP_JS = _ROOT / "static" / "app.js"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _function_source(path: Path, name: str) -> str:
    """The exact source of one top-level function.

    Scoped deliberately: a whole-file substring scan for ``FROM keywords`` would
    also match the five other consumers in ``api/server.py`` that this PR does
    not touch, and would therefore fail for a reason unrelated to the claim.
    """
    src = path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg, f"could not extract source for {name}"
            return seg
    raise AssertionError(f"{name} not found in {path}")


def _function_code(path: Path, name: str) -> str:
    """The function's EXECUTABLE code, with comments and docstrings removed.

    A raw-source scan for `waste_terms.spend_usd` matches the comment that
    explains why that read was deleted, so the test would fail precisely
    because the removal was documented. Comments are not behaviour; `ast.unparse`
    drops them, and the docstring is stripped explicitly.
    """
    src = path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            fn = ast.parse(ast.get_source_segment(src, node) or "").body[0]
            body = list(getattr(fn, "body", []))
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            fn.body = body or [ast.Pass()]
            return ast.unparse(fn)
    raise AssertionError(f"{name} not found in {path}")


def _called_names(path: Path, func: str) -> set[str]:
    """Every function name called (directly or as an attribute) inside `func`."""
    tree = ast.parse(_function_source(path, func))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


#: The §6 section contract. Every key is required on EVERY return path — an
#: unavailable section that omits its window or its account is not a contract,
#: it is a shape that happens to be returned on the happy path.
_SECTION_KEYS = {
    "available", "reason", "source", "source_dataset", "source_table", "scope",
    "grain", "window", "window_start", "window_end", "all_time", "customer_id",
    "campaign_id", "identity_status", "coverage_status", "rows",
}


def _assert_section_contract(section: dict, *, where: str) -> None:
    missing = _SECTION_KEYS - set(section)
    assert not missing, f"{where}: section contract missing {sorted(missing)}"
    assert isinstance(section["rows"], list), f"{where}: rows must be a list"
    if section["available"]:
        assert section["reason"] is None, f"{where}: available section carries a reason"
    else:
        assert section["reason"], f"{where}: unavailable section carries no reason"
        assert section["rows"] == [], f"{where}: unavailable section published rows"


# ═════════════════════════════════════════════════════════════════════════════
# §3/§5 — the legacy snapshot readers are gone from the campaign detail builder
# ═════════════════════════════════════════════════════════════════════════════

def test_01_campaign_detail_builder_has_no_legacy_keyword_query():
    """The `FROM keywords` snapshot query is gone from the drawer builder.

    Scoped to `_build_campaign_detail` on purpose. Five other readers of the
    legacy tables remain elsewhere in `api/server.py`; they are other consumers
    that PR-ADS-157 explicitly defers, and failing on them would make this test
    a proxy for work it is not measuring.
    """
    src = _function_code(_API_SERVER, "_build_campaign_detail")
    assert "FROM keywords" not in src
    assert "DISTINCT ON (keyword" not in src


def test_02_campaign_detail_builder_takes_no_metric_from_waste_terms():
    """No drawer metric may originate in the `waste_terms` snapshot table.

    `waste_terms.spend_usd` was read as the term's spend. PR-ADS-153D
    established that column is not a canonical metric, so its presence anywhere
    in this builder is the defect, not merely the summing of it.
    """
    src = _function_code(_API_SERVER, "_build_campaign_detail")
    assert "FROM waste_terms" not in src
    assert "waste_terms.spend_usd" not in src
    assert "crm_junk_confirmed" not in src, (
        "a waste_terms-only column has no business in the builder")
    assert "matched_pattern" not in src

    # The builder DOES still carry `spend_usd` — and must. That field is the
    # canonical Google Ads window spend lifted from the campaign card built by
    # `build_campaign_drawer_evidence`, which is the drawer's headline evidence.
    # The defect was never "a spend field exists"; it was "a spend field read
    # from the waste snapshot". So assert the provenance, not the absence.
    assert "row.get('spend_usd')" in src, (
        "canonical campaign spend must survive the legacy-reader removal")


def test_03_campaign_detail_builder_opens_no_database_connection():
    """Aggregation moved out entirely, not merely the SQL text.

    A builder that still opened a connection could grow a replacement query
    later without any test noticing. Removing the connection removes the place
    such a query could live.
    """
    src = _function_code(_API_SERVER, "_build_campaign_detail")
    assert "get_conn" not in src
    assert "cursor()" not in src
    assert "cur.execute" not in src


def test_04_campaign_detail_actually_calls_both_canonical_adapters():
    """The adapters are CALLED, not merely defined.

    PR #174's first push added `build_campaign_keyword_preview` without wiring
    it in — a canonical adapter nothing invokes certifies nothing.
    """
    called = _called_names(_API_SERVER, "_build_campaign_detail")
    assert "_campaign_keyword_preview" in called
    assert "_campaign_flagged_preview" in called

    assert "build_campaign_keyword_preview" in _called_names(
        _API_SERVER, "_campaign_keyword_preview")
    assert "build_campaign_flagged_preview" in _called_names(
        _API_SERVER, "_campaign_flagged_preview")


def test_05_helpers_compose_rather_than_reimplement():
    """Neither server-side helper may hold aggregation logic of its own."""
    for name in ("_campaign_keyword_preview", "_campaign_flagged_preview"):
        src = _function_source(_API_SERVER, name)
        assert "FROM " not in src, f"{name} contains SQL"
        assert "SELECT" not in src, f"{name} contains SQL"
        assert "get_conn" not in src, f"{name} opens a connection"


# ═════════════════════════════════════════════════════════════════════════════
# §3 — `campaign=` is EXACTLY the canonical filter identity
# ═════════════════════════════════════════════════════════════════════════════

def test_06_campaign_key_is_the_same_identity_the_keyword_filter_matches():
    """Proven by execution, not by both sides being spelled `campaign_key`.

    `_resolve_campaign_identity` is the single shared resolver: the Campaign
    Evidence card gets its `campaign_key` from it, and the keyword population
    stamps the same field on every unit. `_filter_rows(campaign=...)` compares
    against that field. This test runs the resolver on a realistic input and
    then feeds its output through the real filter, so a future change to either
    side breaks it.
    """
    spend_by_id = {"23094767513": {"campaign_name": "global - competitors"}}
    norm_to_ids: dict = {}
    identity_by_label: dict = {}

    status, campaign_key, display = st_svc._resolve_campaign_identity(
        "23094767513", "global - competitors",
        spend_by_id, norm_to_ids, identity_by_label)

    assert status == "mapped"
    assert campaign_key == "23094767513"
    assert display == "global - competitors"

    # The exact structure `build_keyword_evidence` filters over.
    rows = [
        {"campaign_key": campaign_key, "keyword": "winfleet", "campaign": display},
        {"campaign_key": "99999999999", "keyword": "other", "campaign": "other"},
    ]
    kept = kw_svc._filter_rows(
        rows, q=None, campaign=campaign_key, match_type=None,
        criterion_status=None, quality_band=None, signal=None, min_spend=None)
    assert [r["keyword"] for r in kept] == ["winfleet"], (
        "the value the drawer passes as campaign= must select exactly the rows "
        "carrying that canonical campaign identity")


def test_07_keyword_filter_is_keyed_on_identity_not_display_name():
    """A row whose DISPLAY NAME matches but whose identity differs is excluded.

    This is the defect the old drawer had, expressed as an assertion: two
    campaigns sharing the name `global - competitors` must not share rows.
    """
    rows = [
        {"campaign_key": "111", "campaign": "global - competitors", "keyword": "a"},
        {"campaign_key": "222", "campaign": "global - competitors", "keyword": "b"},
    ]
    kept = kw_svc._filter_rows(
        rows, q=None, campaign="111", match_type=None, criterion_status=None,
        quality_band=None, signal=None, min_spend=None)
    assert [r["keyword"] for r in kept] == ["a"]


def test_08_same_name_campaigns_resolve_to_different_keys():
    """Two campaigns sharing a display name keep separate canonical identities."""
    spend_by_id = {
        "111": {"campaign_name": "global - competitors"},
        "222": {"campaign_name": "global - competitors"},
    }
    a = st_svc._resolve_campaign_identity("111", "global - competitors",
                                          spend_by_id, {}, {})
    b = st_svc._resolve_campaign_identity("222", "global - competitors",
                                          spend_by_id, {}, {})
    assert a[1] != b[1]
    assert (a[1], b[1]) == ("111", "222")


def test_09_name_derived_identity_is_disclosed_not_presented_as_campaign_id():
    """A campaign the resolver could NOT pin to an id yields a name-derived key.

    Such a key is normalized display name, which is exactly the identity this PR
    retires — so the section must SAY so rather than presenting the preview as
    campaign_id evidence. Disclosure, not silent downgrade.
    """
    status, key, _ = st_svc._resolve_campaign_identity(
        None, "global - competitors", {}, {}, {})
    assert status == "unmatched"
    assert key.startswith("unmatched:")

    assert kw_svc._identity_status(key) == "name_derived"
    assert st_svc._flagged_identity_status(key) == "name_derived"
    assert kw_svc._identity_status("23094767513") == "resolved"
    assert kw_svc._identity_status(None) == "unresolved"


# ═════════════════════════════════════════════════════════════════════════════
# §6 — the preview adapters fail closed, on every path
# ═════════════════════════════════════════════════════════════════════════════

def test_10_keyword_preview_fails_closed_when_account_is_not_configured():
    """No configured account ⇒ `available: False` with an exact reason code.

    This matters more for keywords than it looks: `fetch_keyword_aggregates`
    filters on `source_date` ALONE — there is no `customer_id` predicate in that
    SQL. An unresolved account therefore has nothing to scope within, and a
    preview that proceeded anyway would publish every account's keywords under
    one campaign's drawer.
    """
    saved = os.environ.pop("GOOGLE_ADS_CUSTOMER_ID", None)
    try:
        section = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
    finally:
        if saved is not None:
            os.environ["GOOGLE_ADS_CUSTOMER_ID"] = saved

    _assert_section_contract(section, where="keyword preview / no account")
    assert section["available"] is False
    assert section["reason"] == kw_svc.PREVIEW_UNAVAILABLE_ACCOUNT
    assert section["reason"] == "google_ads_customer_not_configured"
    assert section["customer_id"] is None, "an absent account must not be invented"
    assert section["total_count"] is None, "no count may be presented as proven"


def test_11_flagged_preview_fails_closed_when_account_is_not_configured():
    saved = os.environ.pop("GOOGLE_ADS_CUSTOMER_ID", None)
    try:
        section = st_svc.build_campaign_flagged_preview("30d", "23094767513")
    finally:
        if saved is not None:
            os.environ["GOOGLE_ADS_CUSTOMER_ID"] = saved

    _assert_section_contract(section, where="flagged preview / no account")
    assert section["available"] is False
    assert section["reason"] == st_svc.FLAGGED_PREVIEW_UNAVAILABLE_ACCOUNT
    assert section["customer_id"] is None


def test_12_account_resolution_failure_does_not_raise():
    """A raising account resolver must become a reason code, not an exception.

    The original adapter called `configured_customer_id()` while building the
    section shell, OUTSIDE its own exception boundary. A raise there would have
    propagated out of the adapter and taken down a Campaign detail payload whose
    spend, lead, junk and wrong-fit evidence is entirely independent of keywords.
    """
    import analysis.search_term_scope as scope

    def _boom():
        raise RuntimeError("account backend unreachable")

    original = scope.configured_customer_id
    scope.configured_customer_id = _boom
    try:
        kw_section = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
        st_section = st_svc.build_campaign_flagged_preview("30d", "23094767513")
    finally:
        scope.configured_customer_id = original

    for section, where in ((kw_section, "keyword"), (st_section, "flagged")):
        _assert_section_contract(section, where=f"{where} / resolver raised")
        assert section["available"] is False
        assert section["reason"] == "google_ads_customer_not_configured"
        assert section["customer_id"] is None


def test_13_unresolved_campaign_identity_is_unavailable_not_empty():
    """No campaign identity ⇒ unavailable. The old query fell back to name
    matching at exactly this point, which is how one campaign's rows appeared
    under another campaign of the same name."""
    for section, reason in (
        (kw_svc.build_campaign_keyword_preview("30d", None),
         kw_svc.PREVIEW_UNAVAILABLE_IDENTITY),
        (st_svc.build_campaign_flagged_preview("30d", None),
         st_svc.FLAGGED_PREVIEW_UNAVAILABLE_IDENTITY),
    ):
        _assert_section_contract(section, where="unresolved identity")
        assert section["available"] is False
        assert section["reason"] == reason == "campaign_identity_unresolved"
        assert section["identity_status"] == "unresolved"


def test_14_evidence_service_failure_is_unavailable_not_empty():
    """A failing evidence service must be distinguishable from a clean window.

    `available: True, rows: []` means "measured, and there was nothing".
    `available: False` means "we could not tell". Collapsing the second into the
    first is how a broken pipeline renders as a clean campaign.
    """
    original = kw_svc.build_keyword_evidence
    kw_svc.build_keyword_evidence = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("evidence build failed"))
    try:
        section = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_keyword_evidence = original

    _assert_section_contract(section, where="keyword preview / service raised")
    assert section["available"] is False
    assert section["reason"] == kw_svc.PREVIEW_UNAVAILABLE_ERROR
    assert section["total_count"] is None


def test_15_certified_empty_is_available_with_zero_rows():
    """The other half of test 14: an empty measured window stays AVAILABLE."""
    original = kw_svc.build_keyword_evidence
    kw_svc.build_keyword_evidence = lambda *a, **k: {
        "rows": [], "pagination": {"total_count": 0},
        "kpis": {"coverage": {"status": "complete", "scope": "window"}},
    }
    try:
        section = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_keyword_evidence = original

    _assert_section_contract(section, where="keyword preview / certified empty")
    assert section["available"] is True
    assert section["reason"] is None
    assert section["rows"] == []
    assert section["coverage_status"] == "complete"
    assert section["total_count"] == 0, "a measured zero is a number, not None"


def test_16_db_unavailable_is_its_own_state():
    original = kw_svc.build_keyword_evidence
    kw_svc.build_keyword_evidence = lambda *a, **k: {"db_unavailable": True}
    try:
        section = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_keyword_evidence = original

    _assert_section_contract(section, where="keyword preview / db down")
    assert section["available"] is False
    assert section["reason"] == kw_svc.PREVIEW_UNAVAILABLE_SOURCE
    assert section["coverage_status"] == "unavailable"


def test_17_foreign_account_rows_are_withheld_not_shown_with_a_caveat():
    """The account is PROVEN from the returned rows, because the SQL cannot.

    `fetch_keyword_aggregates` has no `customer_id` predicate, so the adapter's
    account claim would otherwise be an assertion about a filter that does not
    exist. A row belonging to another account inside a campaign drawer is a
    scope violation: the rows are withheld entirely rather than rendered with a
    warning beside them.
    """
    configured = os.environ.get("GOOGLE_ADS_CUSTOMER_ID") or "555"
    original = kw_svc.build_keyword_evidence
    kw_svc.build_keyword_evidence = lambda *a, **k: {
        "rows": [
            {"keyword": "ours", "customer_id": configured, "campaign_key": "23094767513"},
            {"keyword": "theirs", "customer_id": "999999", "campaign_key": "23094767513"},
        ],
        "pagination": {"total_count": 2},
        "kpis": {"coverage": {"status": "complete"}},
    }
    try:
        section = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_keyword_evidence = original

    _assert_section_contract(section, where="keyword preview / foreign account")
    assert section["available"] is False
    assert section["reason"] == kw_svc.PREVIEW_UNAVAILABLE_SCOPE
    assert section["rows"] == [], "foreign-account rows must not reach the drawer"


def test_18_rows_without_a_recorded_account_are_equally_unprovable():
    """A row with no `customer_id` cannot be proven to belong to this account.

    Treating "no account recorded" as "our account" is the assumption that
    produced 16,100 account-less twins in PR-ADS-156-F3. It is not made here.
    """
    original = kw_svc.build_keyword_evidence
    kw_svc.build_keyword_evidence = lambda *a, **k: {
        "rows": [{"keyword": "orphan", "customer_id": None}],
        "pagination": {"total_count": 1},
        "kpis": {"coverage": {"status": "complete"}},
    }
    try:
        section = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_keyword_evidence = original

    assert section["available"] is False
    assert section["reason"] == kw_svc.PREVIEW_UNAVAILABLE_SCOPE


def test_19_keyword_rows_carry_the_account_that_makes_the_check_possible():
    """`_unit_row` must emit `customer_id`, or test 17 is checking nothing.

    A guard whose input is always absent passes forever. This asserts the field
    the guard reads is genuinely produced by the row builder.
    """
    row = kw_svc._unit_row(
        {"customer_id": "555", "campaign_id": "111", "criterion_id": "c1",
         "clicks": 0, "impressions": 0},
        {}, None)
    assert "customer_id" in row
    assert row["customer_id"] == "555"


def test_20_flagged_preview_declares_waste_terms_as_annotation_only():
    """`waste_terms` may annotate. It may never be a metric."""
    section = st_svc.build_campaign_flagged_preview("30d", None)
    assert section["source_table"] == "search_terms"
    assert section["annotation_table"] == "waste_terms"
    role = section["annotation_role"].lower()
    assert "annotation only" in role
    assert "never a metric" in role
    assert "never summed" in role
    assert "display name" in role


def test_21_declared_scope_matches_what_the_query_actually_does():
    """A scope claim must describe the predicate, not the intention.

    This test has always enforced the same rule; what changed is the predicate.
    The first revision declared account scope while `fetch_keyword_aggregates`
    filtered on `source_date` alone, so the test required the keyword section
    NOT to claim an account filter. PR-ADS-157 §1 added the predicate, so the
    claim is now true — and the test verifies it against the SQL rather than
    accepting the new string on faith.
    """
    kw_section = kw_svc.build_campaign_keyword_preview("30d", None)
    st_section = st_svc.build_campaign_flagged_preview("30d", None)

    # Both sections claim account scope…
    assert "account" in kw_section["scope"].lower()
    assert "account" in st_section["scope"].lower()

    # …and each names the mechanism that actually enforces it.
    assert "enforced in SQL" in kw_section["account_scope"]
    assert "customer_id = ANY" in kw_section["account_scope"]
    assert "before aggregation, sorting and pagination" in kw_section["account_scope"]
    assert "canonical_scope" in st_section["account_scope"]

    # The claims are checked against the source, so they cannot drift into
    # comments that used to be true.
    st_repo = (_ROOT / "db" / "search_term_repository.py").read_text()
    assert "canonical_scope(start, end)" in st_repo

    kw_repo_src = (_ROOT / "db" / "keyword_repository.py").read_text()
    assert '_ACCOUNT = "customer_id = ANY(%s)"' in kw_repo_src, (
        "the keyword section claims an account predicate; it must exist in SQL")
    # It is applied to BOTH keyword reads — the aggregates and the per-date
    # costs must describe the same population or the FX conversion would be
    # computed over rows the aggregates excluded.
    for fn in ("fetch_keyword_aggregates", "fetch_keyword_daily_costs"):
        body = _function_code(_ROOT / "db" / "keyword_repository.py", fn)
        assert "_scope(customer_ids)" in body, f"{fn} does not apply the scope"
        assert "scope_params" in body, f"{fn} does not bind the scope parameters"


def test_21b_an_empty_account_candidate_list_selects_nothing():
    """Fail closed, in the predicate itself.

    `customer_ids=None` means "account-wide", which is what the Keyword Evidence
    page has always had. `customer_ids=[]` means "no account resolved", and must
    NOT collapse to the same thing — a widening fallback is how an unscoped
    total gets published under an account-scoped label.
    """
    import db.keyword_repository as kw_repo

    wide_sql, wide_params = kw_repo._scope(None)
    assert "customer_id" not in wide_sql
    assert wide_params == ()

    empty_sql, empty_params = kw_repo._scope([])
    assert "customer_id = ANY" in empty_sql, (
        "an empty candidate list must still apply the predicate, so it selects "
        "nothing rather than silently widening to every account")
    assert empty_params == ([],)

    scoped_sql, scoped_params = kw_repo._scope(["555", "5-5-5"])
    assert "customer_id = ANY" in scoped_sql
    assert scoped_params == (["555", "5-5-5"],)


def test_22_all_supported_windows_produce_a_complete_contract():
    """Every Evidence Window returns the full §6 shape, including `all_time`."""
    for window in ("7d", "14d", "30d", "60d", "180d", "all_time"):
        kw_section = kw_svc.build_campaign_keyword_preview(window, None)
        st_section = st_svc.build_campaign_flagged_preview(window, None)
        _assert_section_contract(kw_section, where=f"keyword/{window}")
        _assert_section_contract(st_section, where=f"flagged/{window}")
        assert kw_section["window"] == window
        assert st_section["window"] == window


def test_23_unknown_window_still_raises_for_the_api_to_answer_400():
    """An invalid window is a CALLER error, not a drawer section.

    Absorbing it into `available: False` would turn a malformed request into a
    200 with an apologetic panel, and `/api/campaign-detail` would stop
    answering HTTP 400 for a window it does not support.
    """
    from analysis.evidence_windows import EvidenceWindowError
    with pytest.raises(EvidenceWindowError):
        kw_svc.build_campaign_keyword_preview("nonsense_window", "23094767513")


# ═════════════════════════════════════════════════════════════════════════════
# §5/§6 — one broken section must not take down the payload
# ═════════════════════════════════════════════════════════════════════════════

def test_24_server_helpers_convert_an_adapter_raise_into_a_section():
    """Belt and braces: the adapters fail closed, and the helpers do too.

    The adapters are the contract. The server helpers are the guarantee that a
    future adapter regression still cannot cost the operator their spend, lead,
    junk and wrong-fit evidence.
    """
    import api.server as server

    original = kw_svc.build_campaign_keyword_preview
    kw_svc.build_campaign_keyword_preview = lambda *a, **k: (
        _ for _ in ()).throw(RuntimeError("adapter regression"))
    try:
        section = server._campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_campaign_keyword_preview = original

    assert section["available"] is False
    assert section["reason"] == "keyword_preview_failed"
    assert section["rows"] == []


def test_25_no_window_is_its_own_reason_code():
    import api.server as server
    for section in (server._campaign_keyword_preview(None, "111"),
                    server._campaign_flagged_preview(None, "111")):
        assert section["available"] is False
        assert section["reason"] == "window_not_resolved"
        assert section["rows"] == []


# ═════════════════════════════════════════════════════════════════════════════
# Read-only doctrine
# ═════════════════════════════════════════════════════════════════════════════

def test_26_no_external_writes_anywhere_in_the_changed_paths():
    """Read-only. No Google Ads mutate, no HubSpot write, in any touched path."""
    forbidden = ("MutateOperation", "mutate(", "GoogleAdsService.mutate",
                 "hubspot.*create", "crm/v3/objects")
    targets = [
        _function_code(_API_SERVER, "_build_campaign_detail"),
        _function_code(_API_SERVER, "_campaign_keyword_preview"),
        _function_code(_API_SERVER, "_campaign_flagged_preview"),
    ]
    for src in targets:
        low = src.lower()
        for token in forbidden:
            assert token.lower() not in low, f"external write token {token!r} present"
        for verb in ("insert into", "update ", "delete from"):
            assert verb not in low, f"write verb {verb!r} present in a read path"


# ═════════════════════════════════════════════════════════════════════════════
# §2 — SQL scope and reconciliation publication rules
# ═════════════════════════════════════════════════════════════════════════════

def _js_region(marker: str, *, source: str | None = None) -> str:
    """One top-level JS function body, bounded at the next `\\nfunction `."""
    js = source if source is not None else _APP_JS.read_text()
    i = js.find(f"function {marker}")
    assert i != -1, f"function {marker} not found in static/app.js"
    j = js.find("\nfunction ", i + len(marker) + 9)
    return js[i:j if j != -1 else len(js)]


def test_27_api_returns_the_campaign_attributable_scope_not_a_bare_sql_count():
    """The reconciliation must NAME its population.

    "SQLs" is four different numbers: all-source, Google Ads-source,
    campaign-attributable, and keyword-attributable — plus Google Ads platform
    conversions, which is a fifth number from a different system entirely. A
    page that publishes one of them under a bare label is not reporting a
    metric, it is inviting a reader to pick whichever definition makes the
    number make sense.
    """
    import services.canonical_contact_outcome_service as canon
    assert canon.SCOPE_CAMPAIGN_ATTRIBUTABLE == "campaign_attributable_sqls"

    # `reconciled` is stricter than "the page agrees with itself": every
    # Google Ads-source SQL must also be campaign-attributable, and no qualified
    # contact may be excluded. A Google Ads-source SQL with no campaign identity
    # is a real coverage gap and downgrades the status honestly.
    reconciled = canon.reconciliation_metadata(
        {"counts": {"total_all_source_sqls": 100, "google_ads_source_sqls": 40,
                    "campaign_attributable_sqls": 40}},
        canon.SCOPE_CAMPAIGN_ATTRIBUTABLE, available=True, consumer_count=40)
    assert reconciled["sql_scope"] == "campaign_attributable_sqls"
    assert reconciled["reconciliation_status"] == canon.STATUS_RECONCILED
    assert reconciled["unmatched_sql_contacts"] == 0

    # The nested scopes are all published, so "40" is checkable rather than
    # merely asserted — and 20 unmatched Google Ads-source SQLs make this a
    # PARTIAL scope, not a reconciled one.
    block = canon.reconciliation_metadata(
        {"counts": {"total_all_source_sqls": 100, "google_ads_source_sqls": 60,
                    "campaign_attributable_sqls": 40, "excluded_sql_contacts": 5}},
        canon.SCOPE_CAMPAIGN_ATTRIBUTABLE, available=True, consumer_count=40)
    assert block["reconciliation_status"] == canon.STATUS_PARTIAL
    assert block["total_all_source_sqls"] == 100
    assert block["google_ads_source_sqls"] == 60
    assert block["campaign_attributable_sqls"] == 40
    assert block["unmatched_sql_contacts"] == 20


def test_28_consumer_disagreement_is_a_mismatch_not_a_rounding_note():
    import services.canonical_contact_outcome_service as canon
    block = canon.reconciliation_metadata(
        {"counts": {"total_all_source_sqls": 100, "google_ads_source_sqls": 60,
                    "campaign_attributable_sqls": 40}},
        canon.SCOPE_CAMPAIGN_ATTRIBUTABLE, available=True,
        consumer_count=37)   # the page rendered a different number
    assert block["reconciliation_status"] == canon.STATUS_MISMATCH


def test_29_unavailable_reconciliation_publishes_no_counts_at_all():
    """Unavailable is not zero — the counts must be None, not 0."""
    import services.canonical_contact_outcome_service as canon
    block = canon.reconciliation_metadata({"counts": {}},
                                          canon.SCOPE_CAMPAIGN_ATTRIBUTABLE,
                                          available=False)
    assert block["reconciliation_status"] == canon.STATUS_UNAVAILABLE
    for key in ("total_all_source_sqls", "google_ads_source_sqls",
                "campaign_attributable_sqls", "excluded_sql_contacts",
                "unmatched_sql_contacts"):
        assert block[key] is None, f"{key} was published as {block[key]!r}"


def test_30_campaigns_endpoint_still_carries_the_reconciliation_block():
    """The contract the frontend now depends on must exist at the source."""
    svc = (_ROOT / "services" / "campaign_evidence_service.py").read_text()
    assert '"sql_reconciliation"' in svc
    assert "SCOPE_CAMPAIGN_ATTRIBUTABLE" in svc


def test_31_frontend_stores_the_reconciliation_in_campaign_state():
    """PR #174's root cause, as an assertion.

    `/api/campaigns` returned `sql_reconciliation` all along. The Campaign page
    read `campaigns`, `summary`, `audit`, `window`, `spend_currency` and
    `reporting_currency` — and never this. A contract nothing reads is not a
    contract; it is a field.
    """
    js = _APP_JS.read_text()
    assert "let _campaignSqlReconciliation" in js
    assert "_campaignSqlReconciliation = data.sql_reconciliation" in js


def test_32_there_is_exactly_one_publication_gate():
    """One decision function, so five surfaces cannot drift into three.

    A gate applied in the KPI strip but not in the sort order is not a gate; it
    is a place where an operator can still rank campaigns by a number the page
    just told them it could not certify.
    """
    js = _APP_JS.read_text()
    assert js.count("function campaignSqlPublication()") == 1
    for fn in ("renderCampaignEvidenceKPIs", "renderCampaignEvidenceFilters",
               "filterCampaignEvidence", "sortCampaignEvidence",
               "renderCampaignEvidenceRow", "renderCampaignDrawer",
               "_appendDrawerEvidenceSections"):
        assert "campaignSqlPublication" in _js_region(fn, source=js), (
            f"{fn} publishes SQL-dependent output without consulting the gate")


def test_33_gate_publishes_only_on_a_reconciled_scope():
    """Read the gate's own branches, so the rule is checked, not assumed."""
    region = _js_region("campaignSqlPublication")
    # Exactly one branch may set publish: true, and it is the reconciled one.
    assert region.count("publish: true") == 1
    reconciled_at = region.index('status === "reconciled"')
    publish_at = region.index("publish: true")
    assert reconciled_at < publish_at
    for state in ("mismatch", "partial"):
        assert f'status === "{state}"' in region
    # A missing block is unproven, not permission.
    assert "if (!r || !r.reconciliation_status)" in region


def test_34_aggregate_sql_and_cpql_are_both_withheld_together():
    """CPQL's denominator IS the SQL count.

    Withholding the SQL total but publishing the CPQL derived from it would
    leave the number that matters most standing on the evidence just declared
    uncertifiable.
    """
    region = _js_region("renderCampaignEvidenceKPIs")
    assert "pub.publish\n    ? fmtCount(s.confirmed_sqls_total)" in region
    assert "campaignSqlWithheld(pub)" in region
    # The CPQL branch tests the gate BEFORE it ever reads overall_cpql_usd.
    cpql_branch = region[region.index("const cpql ="):]
    gate_at = cpql_branch.index("!pub.publish")
    value_at = cpql_branch.index("s.overall_cpql_usd")
    assert gate_at < value_at, "CPQL reads its value before checking the gate"


def test_35_withheld_evidence_never_renders_as_zero():
    js = _APP_JS.read_text()
    region = _js_region("campaignSqlWithheld", source=js)
    assert "Reconciliation required" in region
    assert "Unreconciled" in region
    assert ">0<" not in region and "return 0" not in region


def test_36_sql_filters_and_sorts_cannot_classify_an_unproven_count():
    """Both the control and the classification refuse.

    A disabled <option> is an affordance. `filterCampaignEvidence` is where a
    campaign actually gets sorted into "has SQL" or "no SQL", so the refusal has
    to live there as well — otherwise stale state, a restored session, or a
    direct call reintroduces the classification the UI just hid.
    """
    filt = _js_region("filterCampaignEvidence")
    assert "campaignSqlPublication()" in filt
    assert 'f.outcome === "has_sql" || f.outcome === "no_sql"' in filt

    controls = _js_region("renderCampaignEvidenceFilters")
    assert "sqlDisabled" in controls
    assert 'f.outcome = "all"' in controls    # stale selection is cleared
    assert 'f.sort = "spend"' in controls

    sort = _js_region("sortCampaignEvidence")
    assert 'by === "sqls" || by === "cpql"' in sort
    assert "campaignSqlPublication().publish" in sort


def test_37_independent_evidence_survives_a_sql_reconciliation_failure():
    """Spend, junk, wrong-fit and lead counts never depended on the SQL scope.

    Withholding them because SQL reconciliation failed would punish the operator
    for a defect in an unrelated population — and would remove exactly the
    evidence they need in order to investigate it.
    """
    kpis = _js_region("renderCampaignEvidenceKPIs")
    # Spend and junk KPIs are rendered unconditionally.
    assert "${spendPrimary}" in kpis
    assert "fmtCount(s.confirmed_junk_total)" in kpis
    assert "campaignSqlPublication" not in kpis.split("const spendPrimary")[0].split(
        "const s = _campaignSummary")[-1], "spend is gated on the SQL scope"

    row = _js_region("renderCampaignEvidenceRow")
    assert "campaignSpendCell(c)" in row
    assert "c.confirmed_junk" in row

    sections = _js_region("_appendDrawerEvidenceSections")
    assert "independent canonical lead evidence" in sections


def test_38_sql_dependent_statuses_stop_concluding_when_unproven():
    """"Spend without SQL proof" is an accusation, and an unreconciled scope is
    not proof of absence."""
    js = _APP_JS.read_text()
    assert 'CAMPAIGN_SQL_DEPENDENT_STATUSES = new Set([' in js
    assert '"SQL producer", "Spend without SQL proof",' in js
    for fn in ("renderCampaignEvidenceRow", "renderCampaignDrawer"):
        region = _js_region(fn, source=js)
        assert "CAMPAIGN_SQL_DEPENDENT_STATUSES.has" in region, (
            f"{fn} publishes an SQL-dependent conclusion ungated")
        assert "Reconciliation required" in region


def test_39_the_five_sql_populations_are_never_labelled_the_same():
    """Campaign-attributable SQLs ≠ Google Ads platform conversions."""
    js = _APP_JS.read_text()
    assert 'CAMPAIGN_SQL_SCOPE_LABEL = "Campaign-attributable SQLs"' in js
    assert 'CAMPAIGN_SQL_SCOPE_SHORT = "Attributed SQLs"' in js
    assert "Google Ads platform conversions" in js
    # And the old undefined label is gone from every rendered string.
    rendered = "\n".join(ln for ln in js.splitlines()
                         if not ln.strip().startswith("//"))
    assert ">Confirmed SQLs<" not in rendered


# ═════════════════════════════════════════════════════════════════════════════
# §7 — the drawer renders all six section states
# ═════════════════════════════════════════════════════════════════════════════

def test_40_drawer_distinguishes_all_six_section_states():
    js = _APP_JS.read_text()
    assert "function drawerSectionUnavailable" in js
    assert "function drawerSectionProvenance" in js

    unavailable = _js_region("drawerSectionUnavailable", source=js)
    assert "Unavailable is not zero" in unavailable
    assert "Reason code" in unavailable
    assert "identity_status" in unavailable

    sections = _js_region("_appendDrawerEvidenceSections", source=js)
    # certified empty — explicitly a measurement, not an absence of data
    assert "This is a measured result, not a missing one." in sections
    # absence from the flagged population is never "clean"
    assert "That is not a statement that its traffic is clean" in sections
    # unavailable is its own branch, checked BEFORE the empty branch, so an
    # unavailable section can never fall through to "no rows".
    kw_branch = sections[sections.index("const kwSection"):]
    assert kw_branch.index("available === false") < kw_branch.index("keywords.length === 0")


def test_41_provenance_never_claims_window_evidence_for_an_unbounded_result():
    """The phrase is DERIVED from the window bounds, never hardcoded."""
    region = _js_region("drawerSectionProvenance")
    assert "const bounded = !!(s.all_time || (s.window_start && s.window_end))" in region
    assert 'bounded ? "selected-window evidence" : "window bounds unavailable"' in region
    # And every §6 field the operator needs is rendered.
    for field in ("source", "source_dataset", "source_table", "grain",
                  "customer_id", "campaign_id", "coverage_status",
                  "identity_status", "annotation_table"):
        assert f"s.{field}" in region, f"provenance omits {field}"


def test_42_name_derived_identity_is_disclosed_in_the_drawer():
    """The cross-campaign hazard is stated where the rows are shown."""
    region = _js_region("drawerSectionProvenance")
    assert 'identity_status === "name_derived"' in region
    assert "could be shared with another campaign of the same name" in region


def test_43_drawer_reason_codes_are_all_explained():
    """Every reason code an adapter can return has a sentence in the UI.

    A reason code with no mapping renders as a bare identifier, which is better
    than nothing but is not the disclosure the contract promises. This test
    fails when a new reason code is added without one.
    """
    js = _APP_JS.read_text()
    mapped = set()
    block = js[js.index("const DRAWER_SECTION_REASONS = {"):]
    block = block[:block.index("\n};")]
    for line in block.splitlines():
        line = line.strip()
        if line and ":" in line and not line.startswith("//"):
            mapped.add(line.split(":", 1)[0].strip())

    emitted = set()
    for module, prefix in ((kw_svc, "PREVIEW_UNAVAILABLE_"),
                           (st_svc, "FLAGGED_PREVIEW_UNAVAILABLE_")):
        for name in dir(module):
            if name.startswith(prefix):
                emitted.add(getattr(module, name))
    emitted.add(st_svc.FLAGGED_PREVIEW_QUARANTINED)
    emitted.add("window_not_resolved")   # emitted by the api/server.py helpers

    missing = emitted - mapped
    assert not missing, f"reason codes with no operator-facing explanation: {sorted(missing)}"


# ═════════════════════════════════════════════════════════════════════════════
# PostgreSQL-backed behaviour
#
# Everything above reasons about contracts and source. These execute the real
# composed SQL against a real server, because a preview that returns the right
# shape while reading the wrong rows would satisfy every test so far.
# ═════════════════════════════════════════════════════════════════════════════

from datetime import date, timedelta  # noqa: E402

from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

#: The account every row below is stamped with — the same value tests/conftest.py
#: configures, so a seeded row is visible to the scoped readers.
ACCOUNT = "555"
OTHER_ACCOUNT = "777"

#: Inside the 7d window, so every window from 7d up contains it.
DAY = date.today() - timedelta(days=2)
#: Outside 7d but inside 30d — the row that proves the window is real.
OLD_DAY = date.today() - timedelta(days=20)

#: Two campaigns that SHARE a display name and differ only by id. The exact
#: shape the legacy `lower(btrim(campaign_name)) = ANY(...)` query merged.
CAMP_A = "23094767513"
CAMP_B = "23094767514"
SHARED_NAME = "global - competitors"


def _exec(sql, params=()):
    from db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def _seed_keyword_fact(cid, day, criterion, keyword, account=ACCOUNT,
                       cost_micros=5_000_000):
    """One canonical `keyword_daily_facts` row. Module level so a test can add
    one after the fixture has run."""
    _exec("INSERT INTO keyword_daily_facts "
          "(source_date, customer_id, campaign_id, campaign_name, "
          " ad_group_id, ad_group_name, criterion_id, keyword_text, "
          " match_type, criterion_status, cost_micros, currency_code, "
          " source_system, impressions, clicks, conversions) "
          "VALUES (%s,%s,%s,%s,'ag1','Ad Group',%s,%s,'EXACT','ENABLED',"
          " %s,'GBP','google_ads_api',100,10,1.0)",
          (day, account, cid, SHARED_NAME, criterion, keyword, cost_micros))


@pytest.fixture()
def seeded(pg, monkeypatch):  # noqa: F811
    """A live database with two same-named campaigns and their canonical facts."""
    import db.connection as connection
    monkeypatch.setenv("DATABASE_URL", pg.url)
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", ACCOUNT)
    connection._pool = None
    connection.init_pool()
    from db.schema import init_db
    init_db()

    for cid in (CAMP_A, CAMP_B):
        for day in (DAY, OLD_DAY):
            _exec("INSERT INTO google_ads_campaign_daily_spend "
                  "(customer_id, currency_code, campaign_id, campaign_name, "
                  " spend_date, cost_micros, spend_account_currency) "
                  "VALUES (%s,'GBP',%s,%s,%s,%s,%s)",
                  (ACCOUNT, cid, SHARED_NAME, day, 10_000_000, 10.0))

    # Keyword facts: one criterion per campaign inside the 7d window, plus one
    # for campaign A outside it.
    #
    # NOTE: no foreign-account row here. Test 46 seeds one itself, because the
    # scope guard withholds the WHOLE preview when it sees one — correctly —
    # and leaving it in the shared fixture would mean no other test could ever
    # observe a row. Discovered by writing these tests: the guard is not
    # decorative, and a fixture that trips it certifies nothing else.
    _seed_keyword_fact(CAMP_A, DAY, "c-a-in", "winfleet a")
    _seed_keyword_fact(CAMP_B, DAY, "c-b-in", "winfleet b")
    _seed_keyword_fact(CAMP_A, OLD_DAY, "c-a-old", "winfleet old")

    # Search-term facts for the same two campaigns.
    for cid, term in ((CAMP_A, "winfleet a"), (CAMP_B, "winfleet b")):
        _exec("INSERT INTO search_terms "
              "(source_date, campaign_name, campaign_id, ad_group, keyword, "
              " match_type, search_term, customer_id, spend_usd, clicks, "
              " impressions, conversions, source_system) "
              "VALUES (%s,%s,%s,'Ad Group','','',%s,%s,5.0,10,100,1.0,"
              " 'google_ads_api')",
              (DAY, SHARED_NAME, cid, term, ACCOUNT))
    yield pg


@_needs_pg
def test_44_pg_keyword_preview_returns_only_this_campaigns_rows(seeded):
    """The defect, executed. Two campaigns share a display name; the preview
    for one must not contain the other's keyword."""
    section = kw_svc.build_campaign_keyword_preview("7d", CAMP_A)
    assert section["available"] is True, section.get("reason")
    keywords = {r["keyword"] for r in section["rows"]}
    assert "winfleet a" in keywords
    assert "winfleet b" not in keywords, (
        "a campaign sharing a display name leaked into this campaign's preview")


@_needs_pg
def test_45_pg_keyword_preview_respects_the_selected_window(seeded):
    """The old query took the latest snapshot regardless of the window."""
    seven = kw_svc.build_campaign_keyword_preview("7d", CAMP_A)
    thirty = kw_svc.build_campaign_keyword_preview("30d", CAMP_A)
    assert seven["available"] and thirty["available"]

    seven_kw = {r["keyword"] for r in seven["rows"]}
    thirty_kw = {r["keyword"] for r in thirty["rows"]}
    assert "winfleet old" not in seven_kw, "a row outside the 7d window was returned"
    assert "winfleet old" in thirty_kw, "a row inside the 30d window was missing"
    assert seven["window_start"] != thirty["window_start"]


@_needs_pg
def test_46b_pg_foreign_account_row_beyond_the_preview_limit_cannot_leak(seeded):
    """The case a page scan structurally cannot catch.

    One allowed row sits on page 1. A foreign-account row carrying the SAME
    `campaign_id` is given far more spend, so the account-wide ordering would put
    it first — and enough filler rows are added that it would fall beyond the
    preview limit either way.

    If the account were enforced by inspecting the returned page, the foreign row
    would be invisible to the check while still inflating `total_count`, the
    monetary KPIs and the coverage block. Scoping the population in SQL is what
    makes the numbers beside the rows describe the same account as the rows.
    """
    limit = 3

    # Baseline over the account-scoped population, before the foreign rows exist.
    before = kw_svc.build_campaign_keyword_preview("7d", CAMP_A, limit=limit)
    assert before["available"] is True, before.get("reason")
    base_total = before["total_count"]
    base_spend = ((before.get("rows") or [{}])[0] or {}).get("spend_usd")

    # A foreign-account row under the same campaign_id, with the largest spend in
    # the table, plus filler so it cannot land on page 1 of the preview.
    _seed_keyword_fact(CAMP_A, DAY, "c-foreign-big", "zzz foreign whale",
                       account=OTHER_ACCOUNT, cost_micros=999_000_000)
    for i in range(limit + 2):
        _seed_keyword_fact(CAMP_A, DAY, f"c-foreign-{i}", f"foreign filler {i}",
                           account=OTHER_ACCOUNT, cost_micros=500_000_000)

    after = kw_svc.build_campaign_keyword_preview("7d", CAMP_A, limit=limit)
    assert after["available"] is True, after.get("reason")

    # No foreign row in the page…
    accounts = {str(r.get("customer_id")) for r in after["rows"]}
    assert OTHER_ACCOUNT not in accounts
    assert all(a in set(kw_svc._preview_account()[1]) for a in accounts)

    # …and no foreign row in the AGGREGATES either. This is the half a page scan
    # could never defend: seven foreign rows were added and the scoped total is
    # unchanged.
    assert after["total_count"] == base_total, (
        f"foreign-account rows leaked into total_count: {base_total} -> "
        f"{after['total_count']}")

    # Nor into the money.
    top_spend = ((after.get("rows") or [{}])[0] or {}).get("spend_usd")
    assert top_spend == base_spend, (
        "a foreign-account row outranked this account's own spend")

    # And the account-wide population really did grow — otherwise this test
    # would pass over a fixture that never created the hazard.
    import db.keyword_repository as kw_repo
    from datetime import date as _date
    wide = kw_repo.fetch_keyword_aggregates(DAY, _date.today())
    scoped = kw_repo.fetch_keyword_aggregates(
        DAY, _date.today(), customer_ids=kw_svc._preview_account()[1])
    assert len(wide["rows"]) > len(scoped["rows"]), (
        "the fixture did not actually create a cross-account population")
    assert {str(r["customer_id"]) for r in scoped["rows"]} == {ACCOUNT}


@_needs_pg
def test_46_pg_foreign_account_rows_are_excluded_not_merely_withheld(seeded):
    """A foreign-account row on the same campaign_id is EXCLUDED by the query.

    This assertion was strengthened, not relaxed. The first implementation had
    no account predicate in SQL, so it detected foreign rows by scanning the
    returned page and withheld the WHOLE section when it saw one — the operator
    lost their own campaign's evidence because a different account had a row.

    With the predicate applied in SQL the foreign row never enters the
    population: the section stays available, this account's rows are all
    present, and no foreign row appears. Withholding is still the outcome if the
    predicate is ever bypassed — `PREVIEW_UNAVAILABLE_SCOPE` remains reachable
    as a post-condition — but exclusion is the correct primary behaviour, and it
    preserves evidence that was never in doubt.
    """
    before = kw_svc.build_campaign_keyword_preview("7d", CAMP_A)
    assert before["available"] is True, before.get("reason")
    before_keywords = {r["keyword"] for r in before["rows"]}

    _seed_keyword_fact(CAMP_A, DAY, "c-foreign", "winfleet foreign",
                       account=OTHER_ACCOUNT)

    after = kw_svc.build_campaign_keyword_preview("7d", CAMP_A)

    assert after["available"] is True, (
        "this account's own evidence must survive another account having a row")
    assert {r["keyword"] for r in after["rows"]} == before_keywords, (
        "the account-scoped population changed when a foreign row was added")
    assert "winfleet foreign" not in {r["keyword"] for r in after["rows"]}
    assert OTHER_ACCOUNT not in {str(r.get("customer_id")) for r in after["rows"]}
    assert after["total_count"] == before["total_count"]
    assert after["account_scope"].startswith("enforced in SQL")


@_needs_pg
def test_47_pg_flagged_preview_is_campaign_and_window_scoped(seeded):
    section = st_svc.build_campaign_flagged_preview("7d", CAMP_A)
    _assert_section_contract(section, where="pg flagged preview")
    if section["available"]:
        terms = {r["search_term"] for r in section["rows"]}
        assert "winfleet b" not in terms, "another campaign's term leaked in"
        assert section["window_start"] is not None
        assert section["window_end"] is not None


@_needs_pg
def test_48_pg_empty_campaign_is_available_and_empty_not_unavailable(seeded):
    """A campaign identity with no keyword facts is a MEASUREMENT.

    This is the distinction the whole section contract exists for, and it can
    only be checked against a real database: an in-memory double would return
    whatever the test told it to.
    """
    section = kw_svc.build_campaign_keyword_preview("7d", "99999999999")
    assert section["available"] is True, section.get("reason")
    assert section["rows"] == []
    assert section["reason"] is None
    assert section["total_count"] == 0


@_needs_pg
def test_49_pg_campaign_detail_endpoint_composes_both_previews(seeded):
    """End to end: the real endpoint builder, against a real database."""
    import api.server as server
    detail = server._build_campaign_detail(SHARED_NAME, 7, window_key="7d",
                                           campaign_key=CAMP_A)
    assert "keyword_evidence" in detail
    assert "flagged_evidence" in detail
    _assert_section_contract(detail["keyword_evidence"], where="endpoint keyword")
    _assert_section_contract(detail["flagged_evidence"], where="endpoint flagged")
    # The legacy keys still exist and carry the canonical rows.
    assert detail["keywords"] == (detail["keyword_evidence"].get("rows") or [])
    assert detail["waste_terms"] == (detail["flagged_evidence"].get("rows") or [])
    assert "Canonical keyword_daily_facts" in detail["keywords_note"]


@_needs_pg
def test_50_pg_one_broken_section_does_not_take_down_the_payload(seeded):
    """A failing preview must cost the operator the preview, not the drawer."""
    import api.server as server
    original = st_svc.build_campaign_flagged_preview
    st_svc.build_campaign_flagged_preview = lambda *a, **k: (
        _ for _ in ()).throw(RuntimeError("search-term backend down"))
    try:
        detail = server._build_campaign_detail(SHARED_NAME, 7, window_key="7d",
                                               campaign_key=CAMP_A)
    finally:
        st_svc.build_campaign_flagged_preview = original

    assert detail["flagged_evidence"]["available"] is False
    assert detail["flagged_evidence"]["reason"] == "flagged_preview_failed"
    # …and the independent evidence is untouched.
    assert detail["keyword_evidence"]["available"] is True
    assert detail["campaign"] is not None
    assert detail.get("db_unavailable") is not True


@_needs_pg
def test_51_pg_unconfigured_account_fails_closed_over_a_populated_table(seeded,
                                                                       monkeypatch):
    """Fail-closed matters ONLY over a populated table.

    With no rows, every implementation returns nothing and looks correct. These
    rows exist and would be returned by an unscoped read, so this is the case
    that distinguishes a real gate from an absent one.
    """
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    for section in (kw_svc.build_campaign_keyword_preview("7d", CAMP_A),
                    st_svc.build_campaign_flagged_preview("7d", CAMP_A)):
        assert section["available"] is False
        assert section["reason"] == "google_ads_customer_not_configured"
        assert section["rows"] == []
        assert section["customer_id"] is None


@_needs_pg
def test_52_pg_all_windows_execute_against_a_real_server(seeded):
    """Every supported window runs its real composed SQL, including all_time."""
    for window in ("7d", "14d", "30d", "60d", "180d", "all_time"):
        kw = kw_svc.build_campaign_keyword_preview(window, CAMP_A)
        st = st_svc.build_campaign_flagged_preview(window, CAMP_A)
        _assert_section_contract(kw, where=f"pg keyword/{window}")
        _assert_section_contract(st, where=f"pg flagged/{window}")
        if window == "all_time":
            assert kw["all_time"] is True, "all_time must disclose itself"
        elif kw["available"]:
            assert kw["all_time"] is not True
            assert kw["window_start"] and kw["window_end"]


# ═════════════════════════════════════════════════════════════════════════════
# §8 — the audit command's exit codes
# ═════════════════════════════════════════════════════════════════════════════

def _run_audit(env_extra=None, args=("--window", "30d", "--json")):
    """Run the audit as a SUBPROCESS.

    In-process would let the test's own imports, pool and patched modules decide
    the outcome. The command's contract is that it initialises its own pool and
    exits with a code an operator or a CI job can act on, and a subprocess is
    the only way to check that claim.
    """
    import os
    import subprocess
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "scripts.audit_campaign_evidence_certification",
         *args],
        capture_output=True, text=True, cwd=str(_ROOT), env=env)


def test_53_audit_exits_2_when_the_database_is_unavailable():
    """Unavailable is exit 2, never exit 1 and never exit 0.

    "The database is down" and "the code publishes an uncertified number" lead
    an operator to opposite actions. Collapsing them would make an outage look
    like a defect, and — far worse — would let a real defect hide behind one.
    """
    result = _run_audit({"DATABASE_URL": ""})
    assert result.returncode == 2, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["certified"] is False
    assert payload["exit_code"] == 2
    assert payload["violations"] == [], (
        "a database outage must not be reported as a truth-contract violation")
    assert payload["unavailable"], "an outage must be reported, not swallowed"


def test_54_audit_exits_2_for_an_unknown_window():
    result = _run_audit(args=("--window", "not_a_window", "--json"))
    assert result.returncode == 2


def test_55_audit_reports_no_external_writes():
    result = _run_audit({"DATABASE_URL": ""})
    payload = json.loads(result.stdout)
    assert payload["external_writes_performed"] is False


def test_56_audit_is_read_only_by_construction():
    """No write verb and no external client anywhere in the audit source."""
    path = _ROOT / "scripts" / "audit_campaign_evidence_certification.py"
    src = path.read_text()
    # Strip comments AND docstrings. The module docstring states that Google
    # Ads, HubSpot and Mailchimp are never contacted — a raw scan would fail
    # precisely because the guarantee was written down. `ast.unparse` drops
    # comments; docstrings are removed explicitly.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    lowered = ast.unparse(tree).lower()
    for verb in ("insert into", "update ", "delete from", "drop ", "truncate",
                 "commit()"):
        assert verb not in lowered, f"write verb {verb!r} in a read-only audit"
    for client in ("googleads", "google.ads", "hubspot", "requests.post",
                   "requests.put", "requests.patch"):
        assert client not in lowered, f"external client {client!r} in the audit"


def test_57_audit_reports_a_concise_default_and_full_json():
    """Both output modes exist and agree on the verdict."""
    human = _run_audit({"DATABASE_URL": ""}, args=("--window", "30d"))
    assert "PR-ADS-157" in human.stdout
    assert "External writes performed: no" in human.stdout
    assert "VERDICT: UNAVAILABLE" in human.stdout
    assert human.returncode == 2

    machine = _run_audit({"DATABASE_URL": ""})
    assert json.loads(machine.stdout)["exit_code"] == human.returncode


def test_58_audit_static_checks_pass_on_this_branch():
    """The legacy-reader, waste-metric and frontend-gate checks certify HEAD.

    These run without a database, so a failure here is a genuine regression in
    the code this PR changed rather than an environment problem.
    """
    from scripts import audit_campaign_evidence_certification as audit
    findings = audit.Findings()
    audit.check_legacy_readers(findings)
    audit.check_no_waste_terms_metric(findings)
    audit.check_frontend_gates(findings)
    assert findings.violations == [], findings.violations
    assert findings.unavailable == [], findings.unavailable
    assert all(c["ok"] for c in findings.checks)


@_needs_pg
def test_59_audit_runs_all_windows_against_a_real_database(seeded):
    """The live half of the audit, executed rather than described."""
    from scripts import audit_campaign_evidence_certification as audit
    findings, per_window = audit.run(audit.WINDOWS)
    assert set(per_window) == set(audit.WINDOWS)
    assert findings.violations == [], findings.violations
    # Every window either reported availability or said why it could not.
    for window, data in per_window.items():
        assert "available" in data, f"{window} produced no verdict"



# ═════════════════════════════════════════════════════════════════════════════
# API surface — auth, window validation, and the fail-closed default
# ═════════════════════════════════════════════════════════════════════════════

def test_60_unknown_window_is_http_400_at_the_endpoint():
    """§5: unknown windows still answer 400.

    The preview adapters let `EvidenceWindowError` propagate precisely so this
    keeps working. If they had absorbed it into `available: False`, a malformed
    request would become a 200 with an apologetic panel and the endpoint would
    silently stop validating its own input.
    """
    import api.server as server
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        server._resolve_evidence_window("not_a_window", 30)
    assert excinfo.value.status_code == 400

    # And every supported window resolves.
    for window in ("7d", "14d", "30d", "60d", "180d", "all_time"):
        days, key = server._resolve_evidence_window(window, 30)
        assert key == window
        if window == "all_time":
            assert days is None, "all_time must carry no lower bound"


def test_61_campaign_detail_endpoint_requires_authentication():
    """Unchanged by this PR — asserted so it stays that way."""
    import api.server as server
    route = next(r for r in server.app.routes
                 if getattr(r, "path", None) == "/api/campaign-detail")
    deps = repr(getattr(route, "dependant", None)) + repr(route.__dict__)
    assert "require_auth" in deps, "the drawer endpoint lost its auth dependency"


def test_62_gate_fails_closed_on_a_status_it_has_never_seen():
    """A future reconciliation status must withhold, not publish.

    The gate's LAST return is the withholding branch, so any status that does
    not match a known case falls through to it. A gate whose default was
    permissive would silently publish the first time the vocabulary grew.
    """
    region = _js_region("campaignSqlPublication")
    tail = region[region.rindex("return {"):]
    assert "publish: false" in tail
    assert 'state: "unavailable"' in tail
    assert "Unavailable is not zero" in tail


def test_63_withheld_states_are_announced_to_assistive_technology():
    """A visual-only "Reconciliation required" is not a disclosure for everyone.

    The unavailable section carries role="status" so a screen reader announces
    it, and every withheld value carries the reason as a title rather than
    relying on colour or position alone.
    """
    js = _APP_JS.read_text()
    assert 'role="status"' in _js_region("drawerSectionUnavailable", source=js)
    assert 'role="note"' in _js_region("renderCampaignSqlReconciliation", source=js)
    withheld = _js_region("campaignSqlWithheld", source=js)
    assert 'title="${escapeHtml(pub.reason || "")}"' in withheld


def test_64_all_time_coverage_is_disclosed_rather_than_implied():
    """`all_time` has no lower bound, and the section says so.

    Rendering an all-time result with a blank start date would look like a
    window whose start was merely unknown — a different and much worse claim.
    """
    for section in (kw_svc.build_campaign_keyword_preview("all_time", None),
                    st_svc.build_campaign_flagged_preview("all_time", None)):
        assert section["window"] == "all_time"
        assert "all_time" in section

    provenance = _js_region("drawerSectionProvenance")
    assert 's.all_time ? "all time"' in provenance
    assert "s.all_time ||" in provenance, (
        "all_time must count as bounded, or an all-time section would be told "
        "its window bounds are unavailable")


def test_65_every_reason_code_is_distinct():
    """Two states sharing a reason code cannot be told apart downstream."""
    codes = {}
    for module, prefix in ((kw_svc, "PREVIEW_UNAVAILABLE_"),
                           (st_svc, "FLAGGED_PREVIEW_UNAVAILABLE_")):
        for name in dir(module):
            if name.startswith(prefix):
                codes.setdefault(getattr(module, name), []).append(
                    f"{module.__name__}.{name}")
    # The account code is deliberately SHARED between the two services: it is
    # the same condition with the same remedy, and giving it two spellings
    # would make a single misconfiguration look like two unrelated faults.
    shared = {code for code, names in codes.items() if len(names) > 1}
    assert shared <= {"google_ads_customer_not_configured",
                      "campaign_identity_unresolved"}, (
        f"reason codes collide without justification: {shared}")

# ═════════════════════════════════════════════════════════════════════════════
# Test isolation — the hazard this PR tripped over
# ═════════════════════════════════════════════════════════════════════════════

def test_66_every_by_value_get_conn_importer_is_eagerly_imported():
    """`tests/conftest.py`'s eager-import list must stay EXHAUSTIVE.

    The hazard, concretely: a module that does `from db.connection import
    get_conn` at module scope binds that function BY VALUE. A test that
    monkeypatches `db.connection.get_conn` therefore leaks its fake permanently
    into any module whose FIRST import happens while the fake is installed —
    monkeypatch restores the attribute, not the copy.

    This PR hit it. Composing the canonical keyword evidence service in the
    campaign drawer moved the first import of `db.writers` into a PR-ADS-141
    test that patches `get_conn`. Every real write afterwards went through a
    fake cursor; `db.writers` caught the AttributeError, logged it, and returned
    normally. Nine PostgreSQL tests in suites this PR never touched failed with
    empty tables, and nothing in their output named the cause.

    The conftest fixture imports these modules before any test runs. That fixes
    it only for as long as the list matches the code — a list nobody maintains
    is the same class of defect as the bug it was written for, so this test
    derives the truth from the source tree instead of trusting the list.
    """
    import re

    listed = set(conftest._EAGER_DB_MODULES)
    pattern = re.compile(r"^from db\.connection import .*\bget_conn\b", re.M)

    actual = set()
    for path in sorted((_ROOT / "db").glob("*.py")):
        if path.name == "__init__.py":
            continue
        if pattern.search(path.read_text()):
            actual.add(f"db.{path.stem}")

    missing = actual - listed
    assert not missing, (
        "these modules bind get_conn by value but are not eagerly imported by "
        f"tests/conftest.py, so a monkeypatched connection can leak into them: "
        f"{sorted(missing)}")


def test_67_the_eager_import_fixture_actually_binds_the_real_get_conn():
    """The list being right is not the same as the fixture having worked.

    Asserts identity, not equality: every listed module's `get_conn` must be the
    very object `db.connection` exposes. A captured fake would compare unequal
    here, which is precisely the state that produced silent write failures.
    """
    import importlib
    import db.connection as conn_mod

    for name in conftest._EAGER_DB_MODULES:
        if name == "db.connection":
            continue
        module = importlib.import_module(name)
        bound = getattr(module, "get_conn", None)
        if bound is None:
            continue          # resolves through the module at call time — safe
        assert bound is conn_mod.get_conn, (
            f"{name}.get_conn is not db.connection.get_conn — a patched "
            "connection factory has been captured as its permanent binding")

# ═════════════════════════════════════════════════════════════════════════════
# §2 correction — SQL-DEPENDENT STATUS filters are gated too
# ═════════════════════════════════════════════════════════════════════════════

def test_68_sql_dependent_status_options_are_disabled_when_unproven():
    """"SQL producer" and "Spend without SQL proof" are conclusions.

    Both are read off the SQL count. Offering them as filters over an
    unreconciled count invites the operator to slice the table by a finding the
    evidence does not support — and "Spend without SQL proof" in particular
    reads as an accusation.
    """
    region = _js_region("renderCampaignEvidenceFilters")
    assert "CAMPAIGN_SQL_DEPENDENT_STATUSES.has(v)" in region, (
        "the status <option> builder does not consult the SQL-dependent set")
    assert "disabled" in region
    # And a stale selection is neutralized, not silently left applying.
    assert 'if (CAMPAIGN_SQL_DEPENDENT_STATUSES.has(f.status)) f.status = "all";' in region


def test_69_filter_refuses_sql_dependent_statuses_internally():
    """Defence where the classification actually happens.

    A disabled `<option>` is an affordance. `filterCampaignEvidence` is where a
    campaign is included or excluded, so stale state or a direct call must not
    be able to classify by an unreconciled count.
    """
    region = _js_region("filterCampaignEvidence")
    assert "const sqlPub = campaignSqlPublication();" in region
    assert "CAMPAIGN_SQL_DEPENDENT_STATUSES.has(f.status)" in region
    assert "!sqlPub.publish) return true;" in region

    # The refusal is evaluated BEFORE the equality check that would otherwise
    # exclude every row whose status differs.
    gate_at = region.index("CAMPAIGN_SQL_DEPENDENT_STATUSES.has(f.status)")
    equality_at = region.index('(c.outcome_status || "") !== f.status')
    assert gate_at < equality_at, (
        "the status equality check runs before the gate, so an unproven "
        "SQL-dependent filter would still exclude rows")


def test_70_sql_independent_statuses_stay_usable_in_every_state():
    """Only the two SQL-dependent statuses are gated.

    Junk-heavy, Mapping review, No outcome evidence and Data unavailable never
    depended on the SQL count. Disabling them because SQL reconciliation failed
    would remove evidence the operator needs precisely then.
    """
    js = _APP_JS.read_text()
    block = js[js.index("const CAMPAIGN_SQL_DEPENDENT_STATUSES"):]
    block = block[:block.index("]);") + 3]
    for gated in ("SQL producer", "Spend without SQL proof"):
        assert gated in block
    for independent in ("Junk-heavy", "Mapping review", "No outcome evidence",
                        "Data unavailable"):
        assert independent not in block, (
            f"{independent!r} does not depend on the SQL count and must not be gated")

    # The predicate gates by membership, so an independent status falls through
    # to the ordinary equality check in every reconciliation state.
    region = _js_region("filterCampaignEvidence", source=js)
    assert 'f.status !== "all" && CAMPAIGN_SQL_DEPENDENT_STATUSES.has(f.status)' in region


def test_71_every_unproven_reconciliation_state_gates_the_status_filter():
    """mismatch · partial · unavailable · missing block — all withhold.

    The gate is one function with a single `publish: true` branch, so this is
    checked at the source of truth rather than by re-listing the states in the
    filter.
    """
    gate = _js_region("campaignSqlPublication")
    assert gate.count("publish: true") == 1
    for state in ("mismatch", "partial"):
        assert f'status === "{state}"' in gate
    assert "if (!r || !r.reconciliation_status)" in gate      # missing block
    tail = gate[gate.rindex("return {"):]
    assert "publish: false" in tail                            # anything else


# ═════════════════════════════════════════════════════════════════════════════
# §3 correction — every fallback carries the COMPLETE section contract
# ═════════════════════════════════════════════════════════════════════════════

def test_72_shared_unavailable_builders_produce_the_complete_contract():
    """One builder per section, used by the service AND by `api/server.py`.

    A fallback that omits half the contract is not a smaller answer — it is one
    a renderer cannot distinguish from a real one. `window_start` absent and
    `window_start` genuinely unbounded both arrive as `None`, and the renderer
    decides whether it may say "selected-window evidence" from exactly that.
    """
    kw_keys = set(kw_svc.KEYWORD_SECTION_KEYS)
    st_keys = set(st_svc.FLAGGED_SECTION_KEYS)

    cases = [
        ("kw/no window", kw_svc.keyword_preview_unavailable("r"), kw_keys),
        ("kw/window", kw_svc.keyword_preview_unavailable("r", window="30d",
                                                         campaign_key="1"), kw_keys),
        ("kw/bad window", kw_svc.keyword_preview_unavailable("r", window="nope"), kw_keys),
        ("fl/no window", st_svc.flagged_preview_unavailable("r"), st_keys),
        ("fl/window", st_svc.flagged_preview_unavailable("r", window="30d",
                                                         campaign_key="1"), st_keys),
    ]
    for label, section, keys in cases:
        missing = keys - set(section)
        assert not missing, f"{label}: incomplete contract, missing {sorted(missing)}"
        assert section["available"] is False
        assert section["reason"] == "r"
        assert section["rows"] == []
        assert section["total_count"] is None
        # The invariant descriptors are always populated — never blanked.
        for field in ("source", "source_dataset", "source_table", "scope", "grain"):
            assert section[field], f"{label}: {field} is empty on a fallback"


def test_73_api_helpers_delegate_to_the_shared_builders():
    """`api/server.py` must not maintain section dictionaries of its own."""
    import api.server as server

    for helper, keys, reason in (
        (server._campaign_keyword_preview, kw_svc.KEYWORD_SECTION_KEYS,
         "window_not_resolved"),
        (server._campaign_flagged_preview, st_svc.FLAGGED_SECTION_KEYS,
         "window_not_resolved"),
    ):
        section = helper(None, "23094767513")
        assert not (set(keys) - set(section)), "incomplete unresolved-window section"
        assert section["reason"] == reason

    # Structural: the helpers call the builders and hold no literal contract.
    for name in ("_campaign_keyword_preview", "_campaign_flagged_preview"):
        called = _called_names(_API_SERVER, name)
        assert ("keyword_preview_unavailable" in called
                or "flagged_preview_unavailable" in called), (
            f"{name} does not use a shared unavailable builder")
        code = _function_code(_API_SERVER, name)
        assert '"source_table"' not in code, (
            f"{name} still hand-builds a section dictionary")


def test_74_service_exception_path_returns_a_complete_contract():
    """A raising evidence service still yields the full §6 shape."""
    import api.server as server

    original = kw_svc.build_campaign_keyword_preview
    kw_svc.build_campaign_keyword_preview = lambda *a, **k: (
        _ for _ in ()).throw(RuntimeError("boom"))
    try:
        section = server._campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_campaign_keyword_preview = original

    assert not (set(kw_svc.KEYWORD_SECTION_KEYS) - set(section))
    assert section["reason"] == "keyword_preview_failed"
    assert section["window"] == "30d"
    assert section["scope"] and section["grain"]


def test_75_db_unavailable_identity_and_certified_empty_all_complete():
    """Database down · identity unresolved · certified empty — all complete."""
    kw_keys = set(kw_svc.KEYWORD_SECTION_KEYS)

    original = kw_svc.build_keyword_evidence
    kw_svc.build_keyword_evidence = lambda *a, **k: {"db_unavailable": True}
    try:
        db_down = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_keyword_evidence = original
    assert not (kw_keys - set(db_down))
    assert db_down["reason"] == kw_svc.PREVIEW_UNAVAILABLE_SOURCE
    assert db_down["coverage_status"] == "unavailable"

    unresolved = kw_svc.build_campaign_keyword_preview("30d", None)
    assert not (kw_keys - set(unresolved))
    assert unresolved["identity_status"] == "unresolved"

    kw_svc.build_keyword_evidence = lambda *a, **k: {
        "rows": [], "pagination": {"total_count": 0},
        "kpis": {"coverage": {"status": "complete"}}}
    try:
        empty = kw_svc.build_campaign_keyword_preview("30d", "23094767513")
    finally:
        kw_svc.build_keyword_evidence = original
    assert not (kw_keys - set(empty))
    assert empty["available"] is True and empty["reason"] is None
    assert empty["total_count"] == 0, "a measured zero is a number, not None"


def test_76_context_failure_path_no_longer_hand_builds_a_partial_dict():
    """The one path that cannot use the shell must still be complete."""
    code = _function_code(_ROOT / "services" / "keyword_evidence_service.py",
                          "build_campaign_keyword_preview")
    assert "keyword_preview_unavailable(" in code
    assert '"source_dataset": \'keyword_facts\'' not in code
    assert "'scope': None" not in code and '"scope": None' not in code


# ═════════════════════════════════════════════════════════════════════════════
# §4 correction — database-outage propagation
# ═════════════════════════════════════════════════════════════════════════════

def test_77_outage_flag_is_read_from_the_envelope_not_the_row():
    """The bug, as an assertion.

    `build_campaign_drawer_evidence` returns
    `{"campaign": None, …, "db_unavailable": True}` on an outage — the flag is
    on the ENVELOPE and `row` is None. Reading `(row or {}).get("db_unavailable")`
    therefore evaluated `False` in the exact case it existed to detect, and a
    dead database rendered as "no campaign detail available for this window": a
    factual claim about the campaign instead of an admission that nothing could
    be read.
    """
    code = _function_code(_API_SERVER, "_build_campaign_detail")
    assert 'drawer_db_unavailable = bool(ev.get(\'db_unavailable\'))' in code \
        or 'bool(ev.get("db_unavailable"))' in code
    assert '(row or {}).get(\'db_unavailable\')' not in code
    assert '(row or {}).get("db_unavailable")' not in code

    # The service really does put it on the envelope — checked, not assumed.
    svc = (_ROOT / "services" / "campaign_evidence_service.py").read_text()
    assert '"recent_leads": [], "label_set": [], "db_unavailable": True' in svc


def _detail_with(monkeypatch, *, ev, keyword, flagged):
    """Run `_build_campaign_detail` with all three sources stubbed."""
    import api.server as server
    import services.campaign_evidence_service as ce

    monkeypatch.setattr(ce, "build_campaign_drawer_evidence",
                        lambda *a, **k: ev)
    monkeypatch.setattr(server, "_campaign_keyword_preview", lambda *a, **k: keyword)
    monkeypatch.setattr(server, "_campaign_flagged_preview", lambda *a, **k: flagged)
    return server._build_campaign_detail("Brand - UK", 30, window_key="30d",
                                         campaign_key="111")


_OUTAGE_EV = {"campaign": None, "lead_quality": None, "countries": [],
              "recent_leads": [], "label_set": [], "db_unavailable": True}
_EMPTY_EV = {"campaign": None, "lead_quality": None, "countries": [],
             "recent_leads": [], "label_set": [], "db_unavailable": False}


def test_78_complete_outage_sets_the_whole_drawer_flag(monkeypatch):
    detail = _detail_with(
        monkeypatch, ev=_OUTAGE_EV,
        keyword=kw_svc.keyword_preview_unavailable("keyword_evidence_unavailable",
                                                   window="30d"),
        flagged=st_svc.flagged_preview_unavailable("search_term_evidence_unavailable",
                                                   window="30d"))
    assert detail.get("db_unavailable") is True, (
        "a total outage must say so, not render as an empty campaign")


def test_79_one_surviving_preview_suppresses_the_outage_banner(monkeypatch):
    """A global "database offline" banner would hide evidence that loaded.

    If any of the three reads came back available the database is demonstrably
    up for that path, so the whole-drawer claim is false — and it would replace
    a section the operator can actually use with an apology.
    """
    surviving = {**kw_svc.keyword_preview_unavailable("x", window="30d"),
                 "available": True, "reason": None,
                 "rows": [{"keyword": "winfleet"}], "total_count": 1}

    detail = _detail_with(
        monkeypatch, ev=_OUTAGE_EV, keyword=surviving,
        flagged=st_svc.flagged_preview_unavailable("search_term_evidence_unavailable",
                                                   window="30d"))
    assert detail.get("db_unavailable") is not True
    assert detail["keyword_evidence"]["available"] is True
    assert detail["keywords"] == [{"keyword": "winfleet"}]

    # Symmetric: the flagged section surviving is equally sufficient.
    surviving_flagged = {**st_svc.flagged_preview_unavailable("x", window="30d"),
                         "available": True, "reason": None,
                         "rows": [{"search_term": "winfleet"}], "total_count": 1}
    detail2 = _detail_with(
        monkeypatch, ev=_OUTAGE_EV,
        keyword=kw_svc.keyword_preview_unavailable("keyword_evidence_unavailable",
                                                   window="30d"),
        flagged=surviving_flagged)
    assert detail2.get("db_unavailable") is not True


def test_80_campaign_not_found_is_not_an_outage(monkeypatch):
    """No campaign row + a healthy database is "no evidence", not "cannot tell".

    Both states render the drawer without a headline, and conflating them tells
    the operator to go and check the database when the honest answer is that
    this campaign had nothing in this window.
    """
    detail = _detail_with(
        monkeypatch, ev=_EMPTY_EV,
        keyword=kw_svc.keyword_preview_unavailable("keyword_evidence_unavailable",
                                                   window="30d"),
        flagged=st_svc.flagged_preview_unavailable("search_term_evidence_unavailable",
                                                   window="30d"))
    assert detail.get("db_unavailable") is not True, (
        "a campaign with no rows must not be reported as a database outage")
    assert detail["campaign"] is None


# ═════════════════════════════════════════════════════════════════════════════
# §5 correction — the audit must FAIL on each of these regressions
# ═════════════════════════════════════════════════════════════════════════════

def _audit_violations(**patched_sources) -> list[str]:
    """Run the audit's static checks over a temporarily patched source tree.

    Each check reads the real files, so the only honest way to prove it can fail
    is to reintroduce the defect on disk and watch it fail. Every file is
    restored in `finally`, including on assertion error.
    """
    import importlib
    originals = {path: Path(path).read_text() for path in patched_sources}
    try:
        for path, replace in patched_sources.items():
            src = originals[path]
            for old, new in replace:
                assert old in src, f"{path}: patch anchor not found: {old[:60]!r}"
                src = src.replace(old, new, 1)
            Path(path).write_text(src)

        for name in list(sys.modules):
            if name.startswith(("services.", "api.", "db.", "scripts.")):
                del sys.modules[name]
        audit = importlib.import_module(
            "scripts.audit_campaign_evidence_certification")
        f = audit.Findings()
        audit.check_account_scope_before_aggregation(f)
        audit.check_section_contracts_are_complete(f)
        audit.check_outage_propagation(f)
        audit.check_frontend_gates(f)
        return [v.split(":")[0] for v in f.violations]
    finally:
        for path, text in originals.items():
            Path(path).write_text(text)
        for name in list(sys.modules):
            if name.startswith(("services.", "api.", "db.", "scripts.")):
                del sys.modules[name]


def test_81_audit_is_clean_on_this_branch():
    """The baseline. Without it, the four failure tests below prove nothing —
    a check that fails on everything is not a gate either."""
    assert _audit_violations() == []


def test_82_audit_fails_when_account_scope_leaves_the_query():
    """Dropping the predicate from the aggregate query must be caught.

    This is the regression that a page scan could not detect, so the audit is
    the only place it can be caught statically.
    """
    violations = _audit_violations(**{
        "db/keyword_repository.py": [
            ("    where, scope_params = _scope(customer_ids)\n    agg_sql",
             "    where, scope_params = _WINDOW, ()\n    agg_sql"),
        ]})
    assert "account_scope" in violations


def test_83_audit_fails_on_an_incomplete_section_contract():
    violations = _audit_violations(**{
        "api/server.py": [
            ('''        return keyword_preview_unavailable(
            WINDOW_NOT_RESOLVED, campaign_key=campaign_key,
            identity_status="unknown")''',
             '''        return {"available": False, "reason": WINDOW_NOT_RESOLVED,
                "source_table": "keyword_daily_facts", "rows": []}'''),
        ]})
    assert "section_contracts" in violations


def test_84_audit_fails_when_the_outage_flag_is_read_off_the_row():
    violations = _audit_violations(**{
        "api/server.py": [
            ('drawer_db_unavailable = bool(ev.get("db_unavailable"))',
             'drawer_db_unavailable = bool((row or {}).get("db_unavailable"))'),
        ]})
    assert "outage_propagation" in violations


def test_85_audit_fails_when_the_sql_status_filter_is_ungated():
    violations = _audit_violations(**{
        "static/app.js": [
            ('''    if (f.status !== "all" && CAMPAIGN_SQL_DEPENDENT_STATUSES.has(f.status)
        && !sqlPub.publish) return true;
''', ""),
        ]})
    assert "sql_status_filter_gate" in violations
