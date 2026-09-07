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
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

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

    `search_terms` reads apply `canonical_scope()`, an account + provenance
    predicate in SQL, so the flagged section may claim account scope.
    `keyword_daily_facts` reads do not, so the keyword section may not — it
    claims verification from the returned rows instead, which is what test 17
    enforces. Declaring the stronger claim on the weaker query is the exact
    false certification this PR removes.
    """
    kw_section = kw_svc.build_campaign_keyword_preview("30d", None)
    st_section = st_svc.build_campaign_flagged_preview("30d", None)

    assert "account" not in kw_section["scope"].lower(), (
        "keyword reads are date-scoped in SQL; the scope string must not imply "
        "an account predicate the query does not apply")
    assert "date-scoped" in kw_section["account_scope"]

    assert "account" in st_section["scope"].lower()
    assert "canonical_scope" in st_section["account_scope"]

    # And the claim about the search-term SQL is itself checked against source,
    # so it cannot drift into a comment that used to be true.
    repo = (_ROOT / "db" / "search_term_repository.py").read_text()
    assert "canonical_scope(start, end)" in repo
    kw_repo = (_ROOT / "db" / "keyword_repository.py").read_text()
    assert "customer_id" not in kw_repo.split("_WINDOW = ")[1].split("\n")[0], (
        "if keyword reads become account-scoped in SQL, the keyword section may "
        "and should upgrade its claim — and this test should be updated with it")


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
