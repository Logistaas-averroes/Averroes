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


def _seed_keyword_fact(cid, day, criterion, keyword, account=ACCOUNT):
    """One canonical `keyword_daily_facts` row. Module level so a test can add
    one after the fixture has run."""
    _exec("INSERT INTO keyword_daily_facts "
          "(source_date, customer_id, campaign_id, campaign_name, "
          " ad_group_id, ad_group_name, criterion_id, keyword_text, "
          " match_type, criterion_status, cost_micros, currency_code, "
          " source_system, impressions, clicks, conversions) "
          "VALUES (%s,%s,%s,%s,'ag1','Ad Group',%s,%s,'EXACT','ENABLED',"
          " 5000000,'GBP','google_ads_api',100,10,1.0)",
          (day, account, cid, SHARED_NAME, criterion, keyword))


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
def test_46_pg_foreign_account_rows_are_withheld(seeded):
    """A criterion belonging to another account, on the same date, under the
    same campaign id.

    `fetch_keyword_aggregates` filters on `source_date` ALONE — there is no
    `customer_id` predicate in that SQL — so the foreign row genuinely comes
    back from the database. The preview must refuse the whole result rather
    than publish another account's spend inside this campaign's drawer.

    Before the row is added, the same call succeeds. That contrast is the point:
    it shows the withholding is caused by the foreign row and not by some other
    unavailability.
    """
    before = kw_svc.build_campaign_keyword_preview("7d", CAMP_A)
    assert before["available"] is True, before.get("reason")

    _seed_keyword_fact(CAMP_A, DAY, "c-foreign", "winfleet foreign",
                       account=OTHER_ACCOUNT)

    after = kw_svc.build_campaign_keyword_preview("7d", CAMP_A)
    assert after["available"] is False
    assert after["reason"] == kw_svc.PREVIEW_UNAVAILABLE_SCOPE
    assert after["rows"] == [], "foreign-account rows must not reach the drawer"
    assert after["total_count"] is None


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


