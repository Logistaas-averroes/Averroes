"""
tests/test_pr_ads_153e_a_revenue_ledger.py

PR-ADS-153E-A — Canonical Deal Ledger Foundation & Shadow Revenue Reconciliation.

Covers the 20 required cases from §11 plus the governance and consumer-boundary
contracts. Pure/unit level; the PostgreSQL suite
(`test_pr_ads_153e_a_pg_integration.py`) proves the durable behaviour against a
real cluster.

Run with:
    python -m pytest tests/test_pr_ads_153e_a_revenue_ledger.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import deal_currency as currency  # noqa: E402
from analysis import deal_truth as truth  # noqa: E402

_SCHEMA_PY = (_ROOT / "db" / "schema.py").read_text()
_LEDGER_REPO_PY = (_ROOT / "db" / "deal_ledger_repository.py").read_text()
_SYNC_SERVICE_PY = (_ROOT / "services" / "hubspot_deal_sync_service.py").read_text()
_RECON_SERVICE_PY = (
    _ROOT / "services" / "revenue_reconciliation_service.py").read_text()
_CONNECTOR_PY = (_ROOT / "connectors" / "hubspot_pull.py").read_text()
_AUDIT_PY = (_ROOT / "scripts" / "audit_canonical_revenue_truth.py").read_text()
_SERVER_PY = (_ROOT / "api" / "server.py").read_text()
_INCREMENTAL_PY = (_ROOT / "scheduler" / "incremental_sync.py").read_text()


def _code_only(source: str) -> str:
    """Executable code with docstrings and comments stripped.

    Governance guards must inspect what the code DOES, not what its prose says.
    A module whose docstring states "never writes to Mailchimp" would otherwise
    fail a naive substring check for "mailchimp".
    """
    without_docstrings = re.sub(r'(?s)"""[^"]*(?:"(?!"")[^"]*)*"""', "", source)
    return re.sub(r"^\s*#.*$", "", without_docstrings, flags=re.M)


def _contact(cid, **evidence):
    base = {"contact_id": str(cid), "gclid": None, "campaign_name_raw": None,
            "country_raw": None, "acquisition_group": None,
            "keyword_raw": None, "source_primary_raw": None,
            "source_detail_raw": None}
    base.update(evidence)
    return base


# =============================================================================
# §11.1-2 — Attribution is EVIDENCE; it never gates ledger membership
# =============================================================================
def test_1_non_gclid_won_deal_stays_in_all_source_revenue():
    """The defect this whole PR exists for: `gclid_attribution` structurally
    drops deals with no click evidence, so non-GCLID revenue was invisible."""
    resolution = truth.resolve_deal_associations([_contact("1", gclid=None)])
    evidence = truth.primary_contact_evidence(
        [_contact("1", gclid=None)], resolution)

    # No GCLID, yet the deal resolves normally and is fully attributable.
    assert evidence["gclid"] is None
    assert resolution["association_status"] == truth.ASSOC_RESOLVED
    assert resolution["attribution_status"] == truth.ATTR_ATTRIBUTED
    # And won-ness is decided by the boolean alone, never by attribution.
    assert truth.is_won(True) is True


def test_2_gclid_changes_attribution_not_population_membership():
    with_gclid = truth.resolve_deal_associations([_contact("1", gclid="abc")])
    without = truth.resolve_deal_associations([_contact("1", gclid=None)])
    # Same association verdict either way — a GCLID is evidence, not a gate.
    assert with_gclid["association_status"] == without["association_status"]
    assert with_gclid["attribution_status"] == without["attribution_status"]


def test_ledger_membership_never_depends_on_attribution():
    """The ledger's primary key is the deal. No attribution column may appear in
    a NOT NULL / filtering position in the table definition."""
    ddl = _SCHEMA_PY.split("CREATE TABLE IF NOT EXISTS hubspot_deal_ledger (")[1]
    ddl = ddl.split(");")[0]
    assert "deal_id                  TEXT PRIMARY KEY" in ddl
    for attribution_col in ("gclid", "campaign_name_raw", "country_raw",
                            "acquisition_group", "primary_contact_id"):
        line = next(ln for ln in ddl.splitlines()
                    if ln.strip().startswith(attribution_col))
        assert "NOT NULL" not in line, attribution_col


# =============================================================================
# §11.3-4 — Idempotency by deal_id
# =============================================================================
def test_3_and_4_ledger_upsert_is_keyed_on_deal_id_only():
    """Reprocessing a deal, or relabelling its campaign/source, updates ONE row.
    `gclid_attribution` keyed on a SHA1 attribution hash, so a relabel minted a
    new row and the deal's history migrated between campaigns."""
    fn = _LEDGER_REPO_PY.split("def upsert_deal(")[1].split("\ndef ")[0]
    assert "ON CONFLICT (deal_id) DO UPDATE SET" in fn
    # The SET clause is built from every non-key column, so attribution fields
    # are updated IN PLACE rather than participating in the key.
    assert 'f"{c} = EXCLUDED.{c}" for c in updatable' in fn
    assert 'updatable = [c for c in insert_cols if c != "deal_id"]' in fn
    for col in ("campaign_name_raw", "acquisition_group", "gclid"):
        assert col in _LEDGER_REPO_PY.split("_LEDGER_COLUMNS = (")[1].split(")")[0]
    # No hash-based identity anywhere — that is the legacy ledger's defect.
    code = _code_only(_LEDGER_REPO_PY).lower()
    assert "sha1" not in code
    assert "attribution_key" not in code


# =============================================================================
# §11.5-7 — The won predicate
# =============================================================================
def test_5_stage_label_containing_won_never_makes_a_deal_won():
    assert truth.is_won("Deal Won / Payment Received") is False
    assert truth.is_won("won") is False
    assert truth.is_won(False) is False


def test_6_unknown_stage_never_defaults_to_won():
    """The old connector labelled an unknown stage id "Deal Won / Payment
    Received", which combined with a downstream ILIKE '%won%' predicate turned
    an unrecognised stage into revenue."""
    assert 'DEAL_STAGE_MAP.get(stage, "Deal Won / Payment Received")' not in _CONNECTOR_PY
    assert "Unknown stage" in _CONNECTOR_PY
    fn = _SYNC_SERVICE_PY.split("def _normalize_deal(")[1].split("\ndef ")[0]
    assert "Unknown stage" in fn
    assert "hs_is_closed_won" in fn


def test_7_missing_won_boolean_fails_closed():
    for missing in (None, "", "unknown", {}, []):
        assert truth.is_won(missing) is False
    # And it is STORED as unknown rather than as False, so the audit can count
    # how many deals lack the property.
    assert truth.parse_hubspot_bool(None) is None
    assert truth.parse_hubspot_bool("") is None
    assert truth.parse_hubspot_bool("false") is False
    assert truth.parse_hubspot_bool("true") is True


@pytest.mark.parametrize("value,expected", [
    (True, True), ("true", True), ("TRUE", True), ("1", True), (1, True),
    (False, False), ("false", False), (0, False), (None, False),
])
def test_won_predicate_parses_hubspot_boolean_forms(value, expected):
    assert truth.is_won(value) is expected


def test_won_predicate_is_the_only_one_used_by_the_ledger():
    """No stage-id or ILIKE won rule may leak into the canonical layer."""
    for text, label in ((_LEDGER_REPO_PY, "ledger repo"),
                        (_SYNC_SERVICE_PY, "sync service")):
        code = _code_only(text)
        assert "ILIKE '%won%'" not in code, label
        assert "326093516" not in code, label
    # The repository's won filters are the boolean, nothing else.
    assert "hs_is_closed_won IS TRUE" in _LEDGER_REPO_PY


# =============================================================================
# §11.8-11 — Currency doctrine, fail-closed
# =============================================================================
def test_8_missing_currency_never_becomes_usd_or_zero():
    out = currency.resolve_deal_currency(amount_raw=500)
    assert out["revenue_usd"] is None
    assert out["currency_status"] == currency.CURRENCY_UNAVAILABLE
    assert out["currency_reason"] == currency.REASON_UNKNOWN_CURRENCY
    # Explicitly NOT zero: zero is a claim that the deal was worth nothing.
    assert out["revenue_usd"] != 0


def test_8b_unverified_home_currency_is_not_read_as_usd():
    """`amount_in_home_currency` was previously written straight into a column
    named deal_amount_usd with no verification (PR-ADS-153A §9.3)."""
    out = currency.resolve_deal_currency(amount_in_home_currency=500)
    assert out["revenue_usd"] is None
    assert out["currency_reason"] == currency.REASON_HOME_CURRENCY_UNVERIFIED

    verified = currency.resolve_deal_currency(
        amount_in_home_currency=500, home_currency_code="USD",
        home_currency_verified=True)
    assert verified["revenue_usd"] == 500.0
    assert verified["currency_status"] == currency.CURRENCY_VERIFIED_USD


def test_9_verified_usd_home_amount_produces_canonical_revenue():
    out = currency.resolve_deal_currency(amount_raw=1234.56,
                                         deal_currency_code="USD")
    assert out["revenue_usd"] == 1234.56
    assert out["currency_status"] == currency.CURRENCY_VERIFIED_USD
    assert currency.is_summable(out["currency_status"]) is True


def test_10_foreign_currency_uses_the_close_date_fx_or_stays_unavailable():
    converted = currency.resolve_deal_currency(
        amount_raw=100, deal_currency_code="GBP",
        close_date_iso="2026-07-01", fx_rates={"2026-07-01": 1.26})
    assert converted["revenue_usd"] == 126.0
    assert converted["currency_status"] == currency.CURRENCY_CONVERTED
    assert converted["fx_rate_date"] == "2026-07-01"

    # A missing daily rate withholds the value — same fail-closed posture spend
    # already uses. It is never converted at a neighbouring day's rate.
    missing = currency.resolve_deal_currency(
        amount_raw=100, deal_currency_code="GBP",
        close_date_iso="2026-07-02", fx_rates={"2026-07-01": 1.26})
    assert missing["revenue_usd"] is None
    assert missing["currency_reason"] == currency.REASON_NO_FX_RATE

    no_date = currency.resolve_deal_currency(amount_raw=100,
                                             deal_currency_code="GBP")
    assert no_date["currency_reason"] == currency.REASON_NO_CLOSE_DATE


def test_11_missing_amount_remains_null():
    out = currency.resolve_deal_currency(deal_currency_code="USD")
    assert out["revenue_usd"] is None
    assert out["currency_reason"] == currency.REASON_NO_AMOUNT


def test_unproven_currency_is_never_summable():
    for status in (currency.CURRENCY_UNAVAILABLE, None, "guess"):
        assert currency.is_summable(status) is False


def test_currency_service_never_fetches_external_rates():
    """Rates come from the local fx_rates contract; a service reaching the
    network for money would be unauditable."""
    fn = _SYNC_SERVICE_PY.split("def _fx_rates_for(")[1].split("\ndef ")[0]
    assert "revenue_repo.fetch_fx_rates" in fn
    for banned in ("requests.", "httpx.", "urlopen", "ecb", "openexchange"):
        assert banned not in fn.lower(), banned


# =============================================================================
# §11.12-14 — Deterministic association resolution
# =============================================================================
def test_12_multiple_consistent_associations_are_deterministic():
    contacts = [_contact("77", gclid="a", campaign_name_raw="Brand"),
                _contact("12", gclid="a", campaign_name_raw="Brand"),
                _contact("45", gclid="a", campaign_name_raw="Brand")]
    first = truth.resolve_deal_associations(contacts)
    # Reversed input must give the identical answer — API ordering must not
    # decide which campaign a deal belongs to.
    second = truth.resolve_deal_associations(list(reversed(contacts)))
    assert first == second
    assert first["primary_contact_id"] == "12"       # lowest stable id
    assert first["association_status"] == truth.ASSOC_RESOLVED
    assert first["attribution_status"] == truth.ATTR_ATTRIBUTED
    assert "display identity only" in first["association_reason"]


@pytest.mark.parametrize("dimension", list(truth.CONFLICT_DIMENSIONS))
def test_13_conflicting_associations_become_ambiguous(dimension):
    contacts = [_contact("1", **{dimension: "alpha"}),
                _contact("2", **{dimension: "beta"})]
    out = truth.resolve_deal_associations(contacts)
    assert out["association_status"] == truth.ASSOC_AMBIGUOUS
    assert out["attribution_status"] == truth.ATTR_AMBIGUOUS
    # No arbitrary winner — that is how one deal bucketed differently on two
    # pages.
    assert out["primary_contact_id"] is None
    assert dimension in out["association_reason"]
    # Every association is still retained for the bridge.
    assert out["association_count"] == 2


def test_13b_ambiguous_deal_contributes_no_attribution_evidence():
    contacts = [_contact("1", campaign_name_raw="Brand", gclid="a"),
                _contact("2", campaign_name_raw="Gulf", gclid="b")]
    resolution = truth.resolve_deal_associations(contacts)
    evidence = truth.primary_contact_evidence(contacts, resolution)
    assert all(v is None for v in evidence.values())


def test_14_failed_lookup_is_not_a_classification():
    out = truth.resolve_deal_associations([], lookup_failed=True)
    assert out["association_status"] == truth.ASSOC_LOOKUP_FAILED
    # NOT "unclassified" — unclassified is a conclusion we did not reach.
    assert out["attribution_status"] == truth.ATTR_UNAVAILABLE
    assert out["association_count"] is None

    # A SUCCESSFUL lookup that found nothing is a different, real conclusion.
    empty = truth.resolve_deal_associations([])
    assert empty["association_status"] == truth.ASSOC_NONE
    assert empty["attribution_status"] == truth.ATTR_UNCLASSIFIED
    assert empty["association_count"] == 0


def test_14b_failed_lookup_writes_no_associations():
    """Preserving prior evidence is a WRITE-path guarantee, not just a label."""
    fn = _SYNC_SERVICE_PY.split("def sync_deals(")[1].split("\ndef ")[0]
    assert "associations_observed=not lookup_failed" in fn
    repo_fn = _LEDGER_REPO_PY.split("def upsert_deal(")[1].split("\ndef ")[0]
    assert "if associations_observed:" in repo_fn


def test_connector_distinguishes_failed_lookup_from_empty_result():
    assert "class DealAssociationLookupError" in _CONNECTOR_PY
    fn = _CONNECTOR_PY.split("def fetch_deal_associations(")[1].split("\ndef ")[0]
    assert "raise DealAssociationLookupError" in fn
    assert '"complete": True' in fn


# =============================================================================
# §11.15 — Monotonic replay
# =============================================================================
def test_15_older_replay_cannot_overwrite_newer_state():
    fn = _LEDGER_REPO_PY.split("def upsert_deal(")[1].split("\ndef ")[0]
    assert "hubspot_lastmodified_at" in fn
    assert ">=" in fn and "WHERE" in fn
    assert "MONOTONIC GUARD" in fn


def test_incremental_sync_uses_modification_time_not_creation_recency():
    fn = _SYNC_SERVICE_PY.split("def sync_deals(")[1].split("\ndef ")[0]
    assert "last_modified_watermark" in fn
    assert "WATERMARK_OVERLAP_MINUTES" in fn
    pull = _CONNECTOR_PY.split("def pull_deals_for_ledger(")[1].split("\ndef ")[0]
    assert "hs_lastmodifieddate" in pull
    assert "createdate" not in pull.split('"sorts"')[1].split("]")[0]


# =============================================================================
# §11.16 — All stages stored; only the boolean decides won
# =============================================================================
def test_16_all_pipeline_stages_are_tracked():
    import connectors.hubspot_pull as hubspot

    tracked = set(hubspot.ALL_TRACKED_DEAL_STAGES)
    # Open, won, lost, downgrade AND churn — previously only won was synced, so
    # open pipeline was invisible and churn could never reverse a customer.
    for stage in ("qualifiedtobuy", "334269159", "326093513", "326093515",
                  "379260140", "326093516", "379124201", "379124202",
                  "379124203"):
        assert stage in tracked, stage
    assert tracked == set(hubspot.DEAL_STAGE_MAP)


def test_16b_non_won_stages_are_excluded_from_won_totals_by_the_boolean():
    summary_fn = _LEDGER_REPO_PY.split("def fetch_ledger_summary(")[1].split("\ndef ")[0]
    assert "FILTER (WHERE hs_is_closed_won IS TRUE)" in summary_fn
    # Unknown won-state is counted separately rather than folded into either side.
    assert "hs_is_closed_won IS NULL" in summary_fn


def test_16c_no_negative_revenue_or_acv_subtraction_is_invented():
    for text, label in ((_LEDGER_REPO_PY, "repo"), (_SYNC_SERVICE_PY, "sync"),
                        (_RECON_SERVICE_PY, "recon")):
        lowered = _code_only(text).lower()
        for banned in ("negative_revenue", "churn_revenue", "- acv", "refund_amount"):
            assert banned not in lowered, f"{label}: {banned}"


# =============================================================================
# §11.17-18 — Summary/row agreement and the audit gate
# =============================================================================
def test_17_summary_and_rows_are_reconciled_by_an_invariant():
    fn = _RECON_SERVICE_PY.split("def _check_invariants(")[1].split("\ndef ")[0]
    assert "disagree with ledger summary" in fn
    assert "currency completeness misreported" in fn


@pytest.mark.parametrize("violation", [
    "canonical deal_id duplicated",
    "counted as won without hs_is_closed_won",
    "unproven currency",
    "reported as",              # failed lookup reported as unclassified
    "disagree with ledger summary",
    "missing from the canonical ledger",
    "deal sync state unavailable",
])
def test_18_audit_flags_every_required_invariant(violation):
    fn = _RECON_SERVICE_PY.split("def _check_invariants(")[1].split("\ndef ")[0]
    assert violation in fn, violation


def test_18b_audit_exits_non_zero_on_failure_including_json():
    assert "EXIT_VALIDATION_FAILED = 1" in _AUDIT_PY
    main = _AUDIT_PY.split("def main(")[1]
    # Every failure path returns the failure code, --json included.
    assert main.count("return EXIT_VALIDATION_FAILED") >= 2
    assert 'if args.json:\n        print(json.dumps(report, indent=2, default=str))\n        return exit_code' in main


def test_18c_audit_that_cannot_run_is_a_failure_not_a_pass():
    main = _AUDIT_PY.split("def main(")[1]
    assert "audit could not run" in main


def test_audit_itemizes_differences_by_deal_id():
    """"Totals differ" without a deal-level explanation is not acceptable."""
    for key in ("canonical_only", "legacy_only", "amount_disagreement",
                "duplicate_legacy_rows"):
        assert key in _RECON_SERVICE_PY, key
    assert '"deal_id": deal_id' in _RECON_SERVICE_PY
    assert '"reason"' in _RECON_SERVICE_PY


# =============================================================================
# §11.19-20 — Governance and the consumer boundary
# =============================================================================
def test_19_no_canonical_service_reads_local_revenue_json():
    for text, label in ((_LEDGER_REPO_PY, "repo"), (_SYNC_SERVICE_PY, "sync"),
                        (_RECON_SERVICE_PY, "recon")):
        lowered = _code_only(text).lower()
        for banned in ("hubspot_won_deals.json", "data/", "load_json",
                       "windsor"):
            assert banned not in lowered, f"{label}: {banned}"


def test_20_no_external_write_method_is_imported_or_called():
    banned = ("basic_api.create", "basic_api.update", "basic_api.archive",
              ".mutate(", "upload_conversions", "upload_offline",
              "add_negative", "batch_api.create", "batch_api.update",
              "requests.post", "requests.put", "requests.patch",
              "requests.delete")
    for text, label in ((_LEDGER_REPO_PY, "repo"), (_SYNC_SERVICE_PY, "sync"),
                        (_RECON_SERVICE_PY, "recon"), (_AUDIT_PY, "audit")):
        lowered = _code_only(text).lower()
        for token in banned:
            assert token.lower() not in lowered, f"{label}: {token}"


def test_new_connector_contract_is_read_only():
    for fn_name in ("fetch_deal_associations", "pull_deals_for_ledger",
                    "fetch_portal_home_currency"):
        fn = _CONNECTOR_PY.split(f"def {fn_name}(")[1].split("\ndef ")[0]
        code = _code_only(fn).lower()
        for banned in ("requests.post", "requests.put", "requests.patch",
                       "requests.delete", "_api.create", "_api.update",
                       "_api.archive"):
            assert banned not in code, f"{fn_name}: {banned}"
        # Reads only: GET, or the search API (a POST-shaped read HubSpot
        # requires for filtered queries and which mutates nothing).
        assert ("requests.get" in code or "do_search" in code
                or "account-info" in code)


def test_schedulers_hold_no_revenue_business_logic():
    fn = _INCREMENTAL_PY.split("def _sync_deal_ledger(")[1].split("\ndef ")[0]
    assert "sync_deals(" in fn
    code = _code_only(fn).lower()
    for banned in ("hs_is_closed_won", "revenue_usd", "currency", "gclid",
                   "acquisition_group", "resolve_deal"):
        assert banned not in code, banned


def test_failed_sync_is_visible_as_failed_not_as_zero_rows():
    fn = _INCREMENTAL_PY.split("def _sync_deal_ledger(")[1].split("\ndef ")[0]
    assert 'status="failed"' in fn
    assert "errors.append" in fn
    # A partial run must not be recorded as a success.
    assert '"success" if status == "success" else "failed"' in fn


def test_watermark_only_advances_on_a_fully_successful_sync():
    fn = _LEDGER_REPO_PY.split("def record_sync_state(")[1].split("\ndef ")[0]
    assert 'advance = status == "success"' in fn


# ── Consumer boundary (§9) ──────────────────────────────────────────────────
CONSUMER_MODULES = [
    "services/dashboard_overview_service.py",
    "services/dashboard_revenue_service.py",
    "services/dashboard_campaigns_service.py",
    "services/dashboard_countries_service.py",
    "services/dashboard_deals_service.py",
    "services/dashboard_channels_service.py",
    "services/revenue_attribution_service.py",
    "services/source_attribution_service.py",
    "services/revenue_decision_mart.py",
]


@pytest.mark.parametrize("module", CONSUMER_MODULES)
def test_no_production_consumer_is_switched_in_this_shadow_pr(module):
    """153E-A builds and reconciles the ledger. Consumers migrate in 153E-B."""
    path = _ROOT / module
    if not path.exists():
        pytest.skip(f"{module} not present")
    text = path.read_text()
    assert "hubspot_deal_ledger" not in text, module
    assert "deal_ledger_repository" not in text, module


def test_ledger_is_ready_for_the_153e_b_cutover():
    """The next PR must be able to migrate consumers without a schema change:
    every field a revenue page needs is already on the ledger."""
    ddl = _SCHEMA_PY.split("CREATE TABLE IF NOT EXISTS hubspot_deal_ledger (")[1]
    ddl = ddl.split(");")[0]
    for required in ("deal_id", "hs_is_closed_won", "deal_close_date",
                     "revenue_usd", "currency_status", "gclid",
                     "campaign_name_raw", "country_raw", "acquisition_group",
                     "attribution_status", "primary_contact_id"):
        assert required in ddl, required


def test_legacy_tables_are_not_dropped_or_rewritten():
    for table in ("gclid_attribution", "deal_source_attribution"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in _SCHEMA_PY, table
    for banned in ("DROP TABLE gclid_attribution",
                   "DROP TABLE deal_source_attribution",
                   "DROP TABLE deals"):
        assert banned not in _SCHEMA_PY, banned


def test_admin_audit_endpoint_is_read_only_and_admin_gated():
    fn = _SERVER_PY.split('@app.get("/api/audit/revenue-truth")')[1].split("@app.")[0]
    assert "check_admin_or_token(request)" in fn
    assert "build_revenue_reconciliation" in fn
    for banned in ("insert", "update", "delete", "mutate", "requests.post"):
        assert banned not in fn.lower(), banned


def test_no_frontend_change_in_this_backend_pr():
    """§9: static/ belongs to a separate frontend PR."""
    import subprocess

    diff = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=str(_ROOT), capture_output=True, text=True)
    if diff.returncode != 0:
        pytest.skip("git diff unavailable")
    changed = [f for f in diff.stdout.split() if f.startswith("static/")]
    assert changed == [], changed


# ── Privacy (§8) ────────────────────────────────────────────────────────────
def test_reconciliation_output_carries_no_contact_pii():
    """Reconciliation output goes into CI logs."""
    code = _code_only(_RECON_SERVICE_PY).lower()
    for banned in ("email", "firstname", "lastname", "phone"):
        assert banned not in code, banned
    # A GCLID is reported only as present/absent, never printed in full.
    assert '"has_gclid": bool(row.get("gclid"))' in _RECON_SERVICE_PY
    rows_fn = _LEDGER_REPO_PY.split("def fetch_ledger_rows(")[1].split("\ndef ")[0]
    assert "no contact PII" in rows_fn


def test_ledger_rows_expose_no_contact_names_or_emails():
    ddl = _SCHEMA_PY.split("CREATE TABLE IF NOT EXISTS hubspot_deal_ledger (")[1]
    ddl = ddl.split(");")[0].lower()
    for banned in ("email", "firstname", "lastname", "phone", "contact_name"):
        assert banned not in ddl, banned


def test_no_mailchimp_or_google_ads_work_in_this_pr():
    for text, label in ((_LEDGER_REPO_PY, "repo"), (_SYNC_SERVICE_PY, "sync"),
                        (_RECON_SERVICE_PY, "recon"), (_AUDIT_PY, "audit")):
        assert "mailchimp" not in _code_only(text).lower(), label
    # The sync service touches Google Ads nowhere at all.
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", _SYNC_SERVICE_PY, re.M)
    for module in imports:
        assert "google" not in module.lower(), module
