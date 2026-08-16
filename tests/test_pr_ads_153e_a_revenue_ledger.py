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

from decimal import Decimal  # noqa: E402

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
        close_date_iso="2026-07-01",
        fx_rates_by_currency={"GBP": {"2026-07-01": 1.26}})
    assert converted["revenue_usd"] == 126.0
    assert converted["currency_status"] == currency.CURRENCY_CONVERTED
    assert converted["fx_rate_date"] == "2026-07-01"

    # A missing daily rate withholds the value — same fail-closed posture spend
    # already uses. It is never converted at a neighbouring day's rate.
    missing = currency.resolve_deal_currency(
        amount_raw=100, deal_currency_code="GBP",
        close_date_iso="2026-07-02",
        fx_rates_by_currency={"GBP": {"2026-07-01": 1.26}})
    assert missing["revenue_usd"] is None
    assert missing["currency_reason"] == currency.REASON_NO_FX_RATE

    no_date = currency.resolve_deal_currency(amount_raw=100,
                                             deal_currency_code="GBP")
    assert no_date["currency_reason"] == currency.REASON_NO_CLOSE_DATE


def test_10b_an_amount_is_never_converted_at_another_currencys_rate():
    """The GBP rate is missing and a EUR home-currency rate exists. Borrowing it
    produces a number that looks like revenue and is simply wrong."""
    out = currency.resolve_deal_currency(
        amount_raw=100, deal_currency_code="GBP",
        amount_in_home_currency=115, home_currency_code="EUR",
        home_currency_verified=True,
        close_date_iso="2026-07-01",
        fx_rates_by_currency={"EUR": {"2026-07-01": 1.09}})

    assert out["revenue_usd"] is None, "a GBP amount was converted at the EUR rate"
    assert out["currency_status"] == currency.CURRENCY_UNAVAILABLE
    assert out["currency_reason"] == currency.REASON_NO_FX_RATE
    assert out["resolved_currency"] == "GBP"

    # With the GBP rate present it converts — at GBP, never at 1.09.
    ok = currency.resolve_deal_currency(
        amount_raw=100, deal_currency_code="GBP",
        amount_in_home_currency=115, home_currency_code="EUR",
        home_currency_verified=True, close_date_iso="2026-07-01",
        fx_rates_by_currency={"EUR": {"2026-07-01": 1.09},
                              "GBP": {"2026-07-01": 1.26}})
    assert ok["revenue_usd"] == 126.0
    assert ok["fx_rate_used"] == 1.26


def test_10c_a_home_currency_amount_uses_only_the_home_currency_rate():
    """No deal currency at all: the home amount converts at the HOME rate."""
    out = currency.resolve_deal_currency(
        amount_in_home_currency=200, home_currency_code="EUR",
        home_currency_verified=True, close_date_iso="2026-07-01",
        fx_rates_by_currency={"EUR": {"2026-07-01": 1.09},
                              "GBP": {"2026-07-01": 1.26}})
    assert out["resolved_currency"] == "EUR"
    assert out["fx_rate_used"] == 1.09
    assert out["revenue_usd"] == 218.0


def test_10d_the_service_hands_the_whole_rate_table_to_the_resolver():
    """Pre-selecting a rate map in the service is what allowed the substitution
    in the first place."""
    fn = _code_only(
        _SYNC_SERVICE_PY.split("def sync_deals(")[1].split("\ndef ")[0])
    assert "fx_rates_by_currency=fx_by_currency" in fn
    # No per-deal rate map is chosen before the call.
    assert "rates = fx_by_currency.get" not in fn


# =============================================================================
# PR-ADS-153E-A3 — ONE monetary parser, shared with persistence
# =============================================================================
# Production evidence: the historical bootstrap stopped with
#     invalid input syntax for type numeric: ""
# HubSpot returns "" for an unset numeric property. Currency resolution already
# read that as "no amount" and failed closed CORRECTLY — but the ledger's own
# lineage columns were written from the RAW property, so the same deal produced
# a valid `unavailable` verdict and then an unpersistable NUMERIC(18,2) insert.
@pytest.mark.parametrize("blank", [None, "", " ", "   ", "\t", "\n", "\t \n"])
def test_a3_blank_monetary_values_normalize_to_none(blank):
    assert currency.parse_monetary_value(blank) is None


@pytest.mark.parametrize("junk", ["abc", "1,000.00", "$500", "12.3.4", "--5",
                                  "1e", object()])
def test_a3_malformed_monetary_values_normalize_to_none(junk):
    assert currency.parse_monetary_value(junk) is None


@pytest.mark.parametrize("nonfinite", ["NaN", "nan", "-NaN", "Infinity",
                                       "-Infinity", "inf", "-inf",
                                       float("nan"), float("inf"),
                                       float("-inf")])
def test_a3_non_finite_values_normalize_to_none(nonfinite):
    """`Decimal("NaN")` and `Decimal("Infinity")` PARSE. Neither is money, and
    both would reach the NUMERIC column."""
    assert currency.parse_monetary_value(nonfinite) is None


@pytest.mark.parametrize("value,expected", [
    ("1000.50", "1000.50"), (1000.5, "1000.5"), ("  42  ", "42"),
    ("0", "0"), (0, "0"), ("-3.25", "-3.25"), ("1e3", "1E+3"),
    (Decimal("7.77"), "7.77"),
])
def test_a3_valid_amounts_survive_numerically(value, expected):
    parsed = currency.parse_monetary_value(value)
    assert parsed is not None
    assert parsed == Decimal(expected)
    assert parsed.is_finite()


def test_a3_a_boolean_is_not_an_amount():
    """True/False are ints in Python. `Decimal(True)` would be 1.00 of money."""
    assert currency.parse_monetary_value(True) is None
    assert currency.parse_monetary_value(False) is None


@pytest.mark.parametrize("blank", [None, "", "   ", "abc", float("nan")])
def test_a3_missing_money_is_never_coerced_to_zero(blank):
    """Unknown and zero are different facts, in both directions."""
    assert currency.parse_monetary_value(blank) is None
    # A real zero is still a real zero — the parser does not flatten it to
    # "missing" either.
    assert currency.parse_monetary_value("0") == Decimal("0")
    assert currency.parse_monetary_value("0") is not None


def _normalize_deal_assignments() -> dict:
    """`{ledger column: ast expression}` for the dict `_normalize_deal` returns.

    Parsed rather than string-matched: the guarantee is that both monetary
    columns are built by calling the shared parser on the raw property, and a
    reformat (Black wrapping, say) must not be able to fail that.
    """
    import ast

    tree = ast.parse(_SYNC_SERVICE_PY)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_normalize_deal")
    returned = next(n.value for n in ast.walk(fn) if isinstance(n, ast.Return))
    assert isinstance(returned, ast.Dict), "_normalize_deal no longer returns a dict literal"
    return {k.value: v for k, v in zip(returned.keys, returned.values)
            if isinstance(k, ast.Constant)}


@pytest.mark.parametrize("column,hubspot_property", [
    ("amount_raw", "amount"),
    ("amount_in_home_currency", "amount_in_home_currency"),
])
def test_a3_the_normalizer_uses_the_shared_parser_for_both_columns(
        column, hubspot_property):
    import ast

    expr = _normalize_deal_assignments()[column]

    # The value is a CALL to the shared parser...
    assert isinstance(expr, ast.Call), (
        f"{column} is not built by a call — the raw property may be reaching "
        "the NUMERIC column verbatim")
    assert isinstance(expr.func, ast.Name)
    assert expr.func.id == "parse_monetary_value", (
        f"{column} is normalized by {ast.unparse(expr.func)}, not the shared "
        "monetary parser")

    # ...applied to the right HubSpot property.
    arg, = expr.args
    assert ast.unparse(arg) == f'props.get(\'{hubspot_property}\')', (
        f"{column} is parsed from {ast.unparse(arg)}")


def test_a3_the_parser_is_imported_from_the_currency_module():
    """One parser, not a local re-implementation that could drift."""
    import ast

    tree = ast.parse(_SYNC_SERVICE_PY)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "analysis.deal_currency"
        for alias in node.names
    }
    assert "parse_monetary_value" in imported


def test_a3_blank_hubspot_amounts_produce_a_persistable_row():
    """The exact production deal shape, through the real normalizer."""
    import services.hubspot_deal_sync_service as svc

    row = svc._normalize_deal(
        {"id": "D_BLANK", "properties": {
            "dealstage": "326093516", "hs_is_closed_won": "true",
            "closedate": "2026-07-10T00:00:00Z",
            "amount": "", "amount_in_home_currency": "",
            "deal_currency_code": ""}},
        {"326093516": "Deal Won / Payment Received"})

    # NULL, not "" and not 0 — both NUMERIC columns are now writable.
    assert row["amount_raw"] is None
    assert row["amount_in_home_currency"] is None
    assert row["deal_id"] == "D_BLANK"
    # The deal is NOT skipped; it stays in the ledger with its won state.
    assert row["hs_is_closed_won"] is True


def test_a3_the_currency_verdict_is_unchanged_for_a_blank_amount():
    """Doctrine preserved: no amount → unavailable/no_amount, never 0."""
    out = currency.resolve_deal_currency(
        amount_raw=None, amount_in_home_currency=None,
        deal_currency_code="USD")
    assert out["revenue_usd"] is None
    assert out["currency_status"] == currency.CURRENCY_UNAVAILABLE
    assert out["currency_reason"] == currency.REASON_NO_AMOUNT
    # Identical verdict from the raw "" the connector actually returns.
    from_blank = currency.resolve_deal_currency(
        amount_raw="", amount_in_home_currency="", deal_currency_code="USD")
    assert from_blank["currency_status"] == out["currency_status"]
    assert from_blank["currency_reason"] == out["currency_reason"]


def test_a3_a_valid_usd_amount_still_produces_the_same_revenue():
    """The regression guard: normalization must not move a real number."""
    for amount in ("1234.56", 1234.56, Decimal("1234.56")):
        out = currency.resolve_deal_currency(amount_raw=amount,
                                             deal_currency_code="USD")
        assert out["revenue_usd"] == 1234.56
        assert out["currency_status"] == currency.CURRENCY_VERIFIED_USD


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
    repo_fn = _code_only(
        _LEDGER_REPO_PY.split("def upsert_deal(")[1].split("\ndef ")[0])
    # The bridge is replaced only on an observed lookup that was also APPLIED.
    assert "if applied and associations_observed:" in repo_fn
    # And the ledger row's own evidence columns drop out of the update.
    assert "if not associations_observed:" in repo_fn
    assert "_ASSOCIATION_DERIVED_COLUMNS" in repo_fn


def test_14c_failed_lookup_preserves_every_evidence_column_on_the_row():
    """The bridge alone is not enough: the ledger ROW carries the evidence the
    dashboards read, and a timed-out API call must not blank it."""
    from db import deal_ledger_repository as repo

    for column in ("primary_contact_id", "association_count",
                   "association_status", "association_reason",
                   "gclid", "campaign_name_raw", "keyword_raw", "country_raw",
                   "source_primary_raw", "source_detail_raw",
                   "acquisition_group",
                   "attribution_status", "attribution_reason"):
        assert column in repo._ASSOCIATION_DERIVED_COLUMNS, column
    # Deal facts read successfully in the same run still update.
    for column in ("deal_stage_id", "hs_is_closed_won", "amount_raw",
                   "revenue_usd", "currency_status",
                   "hubspot_lastmodified_at"):
        assert column not in repo._ASSOCIATION_DERIVED_COLUMNS, column


def test_connector_distinguishes_failed_lookup_from_empty_result():
    assert "class DealAssociationLookupError" in _CONNECTOR_PY
    fn = _CONNECTOR_PY.split("def fetch_deal_associations(")[1].split("\ndef ")[0]
    assert "raise DealAssociationLookupError" in fn
    assert '"complete": True' in fn


# =============================================================================
# Connector boundary — the contact reader the ledger actually needs
# =============================================================================
class _FakeContactObj:
    def __init__(self, cid, props):
        self._d = {"id": str(cid), "properties": props}

    def to_dict(self):
        return dict(self._d)


class _FakeBatchResponse:
    def __init__(self, results):
        self.results = results


def _install_fake_hubspot(monkeypatch, *, contacts_by_id, associations):
    """Point the connector at an in-memory HubSpot. No network, no credentials."""
    import connectors.hubspot_pull as hubspot

    class _BatchApi:
        def read(self, batch_read_input_simple_public_object_id=None, **_):
            payload = batch_read_input_simple_public_object_id or {}
            wanted = [i["id"] for i in payload.get("inputs") or []]
            props = payload.get("properties") or []
            return _FakeBatchResponse([
                _FakeContactObj(cid, {p: contacts_by_id[cid].get(p)
                                      for p in props})
                for cid in wanted if cid in contacts_by_id
            ])

    class _Client:
        class crm:  # noqa: N801
            class contacts:  # noqa: N801
                batch_api = _BatchApi()

    monkeypatch.setattr(hubspot, "get_client", lambda: _Client())
    monkeypatch.setattr(hubspot, "fetch_deal_associations",
                        lambda deal_id: {"deal_id": str(deal_id),
                                         "contacts": associations,
                                         "complete": True})
    return hubspot


def test_contact_attribution_reader_returns_the_documented_shape(monkeypatch):
    """`pull_contacts_by_ids` returns {id: createdate} — a STRING. Treating that
    as a contact dict is what made every deal with a contact blow up."""
    import connectors.hubspot_pull as hubspot

    _install_fake_hubspot(
        monkeypatch,
        contacts_by_id={"C1": {"hs_google_click_id": "Cj0KEQ",
                               "hs_analytics_source": "PAID_SEARCH",
                               "hs_analytics_source_data_1": "Brand - UK",
                               "ip_country": "AE"}},
        associations=[{"contact_id": "C1", "association_type_id": "4",
                       "association_label": "Primary"}])

    out = hubspot.pull_contact_attribution_properties(["C1"])
    assert set(out) == {"C1"}
    assert out["C1"]["id"] == "C1"
    assert out["C1"]["properties"]["hs_google_click_id"] == "Cj0KEQ"
    # It is a genuinely different contract from the PR-ADS-115 lead-date reader.
    assert "pull_contact_attribution_properties" != "pull_contacts_by_ids"
    lead_reader = _CONNECTOR_PY.split("def pull_contacts_by_ids(")[1].split("\ndef ")[0]
    assert '"properties": ["createdate"]' in lead_reader


def test_ledger_contact_reader_never_reads_names_or_emails():
    import connectors.hubspot_pull as hubspot

    for banned in ("email", "firstname", "lastname", "phone"):
        assert banned not in hubspot.DEAL_ATTRIBUTION_CONTACT_PROPERTIES
    # Every property `_contact_evidence` consumes is actually requested.
    for needed in ("hs_google_click_id", "hs_analytics_first_url",
                   "hs_analytics_source", "hs_analytics_source_data_1",
                   "hs_analytics_source_data_2", "ip_country", "country"):
        assert needed in hubspot.DEAL_ATTRIBUTION_CONTACT_PROPERTIES, needed


def test_real_connector_shape_flows_through_the_service_without_error(monkeypatch):
    """The end-to-end boundary regression: connector → service → repository, on
    the REAL return shape, for a deal that actually has an associated contact."""
    import services.hubspot_deal_sync_service as svc

    hubspot = _install_fake_hubspot(
        monkeypatch,
        contacts_by_id={"C1": {"hs_google_click_id": "Cj0KEQ",
                               "hs_analytics_source": "PAID_SEARCH",
                               "hs_analytics_source_data_1": "Brand - UK",
                               "hs_analytics_source_data_2": "logistics",
                               "ip_country": "AE"}},
        associations=[{"contact_id": "C1", "association_type_id": "4",
                       "association_label": "Primary"}])

    monkeypatch.setattr(hubspot, "pull_deals_for_ledger", lambda **_: {
        "available": True, "complete": True, "pages": 1, "error": None,
        "deals": [{"id": "D1", "properties": {
            "dealname": "Acme", "dealstage": "326093516",
            "hs_is_closed": "true", "hs_is_closed_won": "true",
            "closedate": "2026-07-10T00:00:00Z",
            "hs_lastmodifieddate": "2026-07-11T00:00:00Z",
            "amount": "1000", "deal_currency_code": "USD"}}]})
    monkeypatch.setattr(hubspot, "fetch_portal_home_currency",
                        lambda: {"available": True, "home_currency_code": "USD",
                                 "verified": True})

    written = []
    monkeypatch.setattr(svc, "_fx_rates_for", lambda *a, **k: {})

    import db.deal_ledger_repository as repo

    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "row": None})
    monkeypatch.setattr(repo, "upsert_deal",
                        lambda row, **kw: (written.append((row, kw))
                                           or {"available": True, "written": 1,
                                               "skipped_stale": 0, "error": None}))
    monkeypatch.setattr(repo, "record_sync_state",
                        lambda **kw: {"available": True, "error": None})

    result = svc.sync_deals(full_refresh=True)

    assert result["status"] == "success", result
    assert result["association_failures"] == 0, "the contact read was misused"
    row, kw = written[0]
    assert kw["associations_observed"] is True
    # The contact's evidence actually landed on the ledger row.
    assert row["gclid"] == "Cj0KEQ"
    assert row["campaign_name_raw"] == "Brand - UK"
    assert row["country_raw"] == "AE"
    assert row["primary_contact_id"] == "C1"
    assert row["attribution_status"] == truth.ATTR_ATTRIBUTED
    assert row["revenue_usd"] == 1000.0


def test_incomplete_contact_batch_is_a_lookup_failure_not_empty_attribution(
        monkeypatch):
    """HubSpot returning 1 of 2 contacts is missing evidence, not proof that a
    contact has no source."""
    import services.hubspot_deal_sync_service as svc

    hubspot = _install_fake_hubspot(
        monkeypatch,
        contacts_by_id={"C1": {"hs_analytics_source": "PAID_SEARCH"}},
        associations=[{"contact_id": "C1", "association_type_id": "4",
                       "association_label": "Primary"},
                      {"contact_id": "C2", "association_type_id": "4",
                       "association_label": "Primary"}])

    monkeypatch.setattr(hubspot, "pull_deals_for_ledger", lambda **_: {
        "available": True, "complete": True, "pages": 1, "error": None,
        "deals": [{"id": "D1", "properties": {
            "dealstage": "326093516", "hs_is_closed_won": "true",
            "hs_lastmodifieddate": "2026-07-11T00:00:00Z",
            "amount": "1000", "deal_currency_code": "USD"}}]})
    monkeypatch.setattr(hubspot, "fetch_portal_home_currency",
                        lambda: {"available": True, "home_currency_code": "USD",
                                 "verified": True})

    import db.deal_ledger_repository as repo

    seen = []
    monkeypatch.setattr(svc, "_fx_rates_for", lambda *a, **k: {})
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "row": None})
    monkeypatch.setattr(repo, "upsert_deal",
                        lambda row, **kw: (seen.append(kw)
                                           or {"available": True, "written": 1,
                                               "skipped_stale": 0, "error": None}))
    monkeypatch.setattr(repo, "record_sync_state",
                        lambda **kw: {"available": True, "error": None})

    result = svc.sync_deals(full_refresh=True)
    assert result["association_failures"] == 1
    assert result["status"] == "partial"
    assert seen[0]["associations_observed"] is False, (
        "a partial batch was written as observed attribution")


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


def test_16c_ingestion_is_not_gated_on_the_hardcoded_stage_map(monkeypatch):
    """The stage map is a DISPLAY vocabulary. Filtering the population on it
    would silently drop every deal in a stage added after this code shipped."""
    import connectors.hubspot_pull as hubspot

    captured = {}

    class _SearchApi:
        def do_search(self, public_object_search_request=None, **_):
            captured["request"] = public_object_search_request

            class _R:
                results = []
                paging = None
            return _R()

    class _Client:
        class crm:  # noqa: N801
            class deals:  # noqa: N801
                search_api = _SearchApi()

    monkeypatch.setattr(hubspot, "get_client", lambda: _Client())
    hubspot.pull_deals_for_ledger()

    filters = captured["request"]["filterGroups"][0]["filters"]
    assert not any(f["propertyName"] == "dealstage" for f in filters), (
        "the default read is filtered on the stage map and is therefore "
        "structurally incomplete")


def test_16d_unknown_stage_is_ingested_and_labelled_unknown():
    import services.hubspot_deal_sync_service as svc

    row = svc._normalize_deal(
        {"id": "D9", "properties": {"dealstage": "999_new_stage",
                                    "hs_is_closed_won": "false"}},
        {"326093516": "Deal Won / Payment Received"})
    # Ingested, not dropped...
    assert row["deal_id"] == "D9"
    # ...and never defaulted to a won label.
    assert row["deal_stage_label"] == "Unknown stage (999_new_stage)"
    assert row["hs_is_closed_won"] is False


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


# =============================================================================
# Reconciliation difference classes and their gate outcome
# =============================================================================
def _diff(canonical_won, canonical_states, legacy_deals, *,
          label="gclid_attribution", expect_gclid_only=True, duplicates=None,
          legacy=None):
    from services import revenue_reconciliation_service as recon

    return recon._diff_against_legacy(
        canonical_won, canonical_states,
        legacy if legacy is not None else {
            "available": True, "deals": legacy_deals,
            "duplicates": duplicates or []},
        label, expect_gclid_only=expect_gclid_only)


# A coverage state that satisfies the PR-ADS-153E-A2 interlock, so these tests
# isolate the DIFF violations rather than re-testing the bootstrap gate (which
# has its own suite in tests/test_pr_ads_153e_a2_cutover_gate.py).
_HEALTHY_SYNC_STATE = {
    "available": True,
    "row": {
        "bootstrap_status": "complete",
        "bootstrap_started_at": "2026-08-01T00:00:00+00:00",
        "bootstrap_completed_at": "2026-08-01T01:00:00+00:00",
        "last_incremental_at": "2026-08-02T00:00:00+00:00",
        "last_sync_mode": "incremental",
        "last_status": "success",
        "last_error": None,
    },
}


def _gate(diff):
    """Violation MESSAGES the gate raises for one diff, ignoring the
    ledger-health checks that need a full summary."""
    from services import revenue_reconciliation_service as recon

    return [f["message"] for f in recon._check_invariants(
        {}, {}, [diff], _HEALTHY_SYNC_STATE, {"available": True, "rows": []})]


def _won(deal_id, **kw):
    row = {"deal_id": deal_id, "hs_is_closed_won": True, "revenue_usd": 100.0,
           "currency_status": "verified_usd", "currency_reason":
           "deal_currency_is_usd", "gclid": None}
    row.update(kw)
    return row


def _legacy(deal_id, **kw):
    row = {"deal_id": deal_id, "deal_amount_usd": 100.0,
           "deal_close_date": "2026-07-10", "deal_stage": "326093516",
           "deal_stage_label": "Deal Won / Payment Received", "gclid": None}
    row.update(kw)
    return row


def test_diff_non_gclid_deal_is_an_EXPECTED_canonical_only_difference():
    from services import revenue_reconciliation_service as recon

    diff = _diff({"D1": _won("D1", gclid=None)}, {}, {})
    item, = diff["canonical_only"]
    assert item["reason"] == recon.REASON_NON_GCLID_EXCLUDED
    assert item["expected"] is True
    assert _gate(diff) == []


def test_diff_gclid_won_deal_missing_from_the_gclid_ledger_FAILS_the_gate():
    from services import revenue_reconciliation_service as recon

    diff = _diff({"D1": _won("D1", gclid="Cj0KEQ")}, {}, {})
    item, = diff["canonical_only"]
    assert item["reason"] == recon.REASON_GCLID_DEAL_MISSING_FROM_LEGACY
    assert item["expected"] is False
    violations = _gate(diff)
    assert any("unexplained canonical_only" in v for v in violations), violations


def test_diff_won_deal_missing_from_the_source_ledger_FAILS_the_gate():
    from services import revenue_reconciliation_service as recon

    diff = _diff({"D1": _won("D1")}, {}, {}, label="deal_source_attribution",
                 expect_gclid_only=False)
    item, = diff["canonical_only"]
    assert item["reason"] == recon.REASON_WON_DEAL_MISSING_FROM_LEGACY
    assert item["expected"] is False
    assert _gate(diff)


def test_diff_legacy_only_means_truly_absent_from_canonical():
    from services import revenue_reconciliation_service as recon

    diff = _diff({}, {}, {"D9": _legacy("D9")})
    item, = diff["legacy_only"]
    assert item["reason"] == recon.REASON_MISSING_FROM_CANONICAL
    assert diff["won_disagreement"] == []
    assert any("missing from the canonical ledger" in v for v in _gate(diff))


def test_diff_won_disagreement_is_separated_from_legacy_only():
    """The canonical ledger HOLDS the deal — the two ledgers merely classify it
    differently. Reporting that as "the sync missed it" sends an operator
    looking for a bug that is not there."""
    from services import revenue_reconciliation_service as recon

    states = {"D9": {"deal_id": "D9", "hs_is_closed_won": False,
                     "deal_close_date": "2026-07-10",
                     "deal_stage_label": "Lost Deal"}}
    diff = _diff({}, states, {"D9": _legacy("D9", deal_stage_label="Won-ish")})

    assert diff["legacy_only"] == []
    item, = diff["won_disagreement"]
    assert item["reason"] == recon.REASON_LEGACY_PREDICATE_FALSE_POSITIVE
    assert item["canonical_is_closed_won"] is False
    # The legacy ILIKE predicate counting a non-won deal IS the known defect.
    assert item["expected"] is True
    assert _gate(diff) == []


def test_diff_unknown_canonical_won_state_FAILS_the_gate():
    from services import revenue_reconciliation_service as recon

    states = {"D9": {"deal_id": "D9", "hs_is_closed_won": None,
                     "deal_close_date": "2026-07-10"}}
    diff = _diff({}, states, {"D9": _legacy("D9")})
    item, = diff["won_disagreement"]
    assert item["reason"] == recon.REASON_CANONICAL_WON_UNKNOWN
    assert item["expected"] is False
    assert _gate(diff)


def test_diff_close_date_outside_the_window_FAILS_the_gate():
    from services import revenue_reconciliation_service as recon

    states = {"D9": {"deal_id": "D9", "hs_is_closed_won": True,
                     "deal_close_date": "2025-01-01"}}
    diff = _diff({}, states, {"D9": _legacy("D9")})
    item, = diff["won_disagreement"]
    assert item["reason"] == recon.REASON_CLOSE_DATE_OUTSIDE_WINDOW
    assert item["expected"] is False
    assert _gate(diff)


def test_diff_withheld_currency_is_an_EXPECTED_amount_difference():
    diff = _diff({"D1": _won("D1", gclid="g", revenue_usd=None,
                             currency_status="unavailable",
                             currency_reason="unknown_currency")},
                 {}, {"D1": _legacy("D1")})
    item, = diff["amount_disagreement"]
    assert item["reason"] == "canonical_currency_unknown_currency"
    assert item["canonical_usd"] is None, "an unknown currency became a number"
    assert item["expected"] is True
    assert _gate(diff) == []


def test_diff_converted_amount_is_an_EXPECTED_amount_difference():
    diff = _diff({"D1": _won("D1", gclid="g", revenue_usd=126.0,
                             currency_status="converted")},
                 {}, {"D1": _legacy("D1", deal_amount_usd=100.0)})
    item, = diff["amount_disagreement"]
    assert item["reason"] == "currency_resolution_differs:converted"
    assert item["expected"] is True
    assert _gate(diff) == []


def test_diff_two_proven_usd_amounts_that_differ_FAIL_the_gate():
    from services import revenue_reconciliation_service as recon

    diff = _diff({"D1": _won("D1", gclid="g", revenue_usd=100.0,
                             currency_status="verified_usd")},
                 {}, {"D1": _legacy("D1", deal_amount_usd=250.0)})
    item, = diff["amount_disagreement"]
    assert item["reason"] == recon.REASON_AMOUNT_PROVEN_BOTH_SIDES
    assert item["expected"] is False
    assert _gate(diff)


def test_diff_duplicate_legacy_rows_are_reported_and_windowed():
    diff = _diff({}, {}, {}, duplicates=[{"deal_id": "D1", "rows_held": 3}])
    assert diff["duplicate_legacy_rows"] == [{"deal_id": "D1", "rows_held": 3}]
    dupe_query = _RECON_SERVICE_PY.split("rows_held")[1].split('"""')[0]
    assert "deal_close_date >= %s" in dupe_query
    assert "deal_close_date < %s" in dupe_query


def test_diff_legacy_amount_unavailable_is_itemized_and_FAILS_the_gate():
    """Legacy holds the deal with no money for it while canonical has proven an
    amount. Skipping it silently would let the cutover move a figure nobody
    could see move."""
    from services import revenue_reconciliation_service as recon

    diff = _diff({"D1": _won("D1", gclid="g", revenue_usd=100.0,
                             currency_status="verified_usd")},
                 {}, {"D1": _legacy("D1", deal_amount_usd=None)})
    item, = diff["amount_disagreement"]
    assert item["reason"] == recon.REASON_LEGACY_AMOUNT_UNAVAILABLE
    assert item["canonical_usd"] == 100.0
    assert item["legacy_usd"] is None
    assert item["expected"] is False
    assert _gate(diff)


def test_diff_canonical_null_vs_legacy_amount_keeps_its_currency_reason():
    """The mirror case stays where it was: canonical withholding is explained by
    the currency doctrine, not by this new category."""
    from services import revenue_reconciliation_service as recon

    diff = _diff({"D1": _won("D1", gclid="g", revenue_usd=None,
                             currency_status="unavailable",
                             currency_reason="no_fx_rate_for_close_date")},
                 {}, {"D1": _legacy("D1", deal_amount_usd=100.0)})
    item, = diff["amount_disagreement"]
    assert item["reason"] == "canonical_currency_no_fx_rate_for_close_date"
    assert item["reason"] != recon.REASON_LEGACY_AMOUNT_UNAVAILABLE
    assert item["expected"] is True
    assert _gate(diff) == []


def test_diff_both_amounts_null_is_not_a_difference():
    diff = _diff({"D1": _won("D1", gclid="g", revenue_usd=None,
                             currency_status="unavailable",
                             currency_reason="no_amount")},
                 {}, {"D1": _legacy("D1", deal_amount_usd=None)})
    assert diff["amount_disagreement"] == []
    assert _gate(diff) == []


# ── An unreadable legacy lineage is never an empty one ──────────────────────
def test_an_unavailable_legacy_ledger_FAILS_the_gate():
    from services import revenue_reconciliation_service as recon

    diff = _diff({}, {}, {}, legacy={"available": False,
                                     "reason": "database_unavailable"})
    assert diff["available"] is False
    assert diff["reason"] == recon.REASON_LEGACY_LEDGER_UNAVAILABLE
    violations = _gate(diff)
    assert any("gclid_attribution unavailable" in v for v in violations), violations


def test_an_unavailable_ledger_fails_even_with_no_canonical_won_deals():
    """The dangerous case. Zero canonical won deals against an unreadable legacy
    ledger produces zero differences — indistinguishable from a clean
    reconciliation unless availability is checked on its own."""
    for label in ("gclid_attribution", "deal_source_attribution"):
        diff = _diff({}, {}, {}, label=label,
                     expect_gclid_only=(label == "gclid_attribution"),
                     legacy={"available": False, "reason": "connection refused"})
        assert diff["canonical_only"] == []
        assert diff["legacy_only"] == []
        assert diff["won_disagreement"] == []
        assert diff["amount_disagreement"] == []
        violations = _gate(diff)
        assert violations, f"{label}: an unreadable ledger reconciled cleanly"
        assert f"{label} unavailable" in violations[0]
        assert "connection refused" in violations[0]


def test_an_unavailable_ledger_reports_no_deal_count_rather_than_zero():
    """0 is a claim the legacy ledger is empty. We do not know that."""
    diff = _diff({}, {}, {}, legacy={"available": False, "reason": "boom"})
    assert diff["legacy_deal_count"] is None


def test_an_unavailable_ledger_fabricates_no_canonical_only_findings():
    """Comparing against a ledger we could not read would itemize every
    canonical deal as absent from it — findings that are pure artefact."""
    diff = _diff({"D1": _won("D1"), "D2": _won("D2")}, {}, {},
                 legacy={"available": False, "reason": "boom"})
    assert diff["canonical_only"] == []


def test_audit_renderer_does_not_print_zeros_for_an_unreadable_ledger():
    render = _AUDIT_PY.split("def _render(")[1].split("\ndef ")[0]
    assert 'if not diff.get("available"):' in render
    assert "no comparison performed" in render


def test_reconciliation_reads_canonical_identity_across_all_states():
    fn = _code_only(
        _RECON_SERVICE_PY.split("def build_revenue_reconciliation(")[1]
        .split("\ndef ")[0])
    assert "fetch_deal_states" in fn
    # Raw source here: the SQL lives in a triple-quoted literal that
    # `_code_only` would strip along with the docstrings.
    states_fn = _LEDGER_REPO_PY.split("def fetch_deal_states(")[1].split("\ndef ")[0]
    # Deliberately unfiltered by won-state and by window.
    where = states_fn.split("WHERE")[1]
    assert "hs_is_closed_won" not in where
    assert "deal_close_date" not in where
    assert "deal_id = ANY(%s)" in where


def test_18b_audit_exits_non_zero_on_failure_including_json():
    assert "EXIT_VALIDATION_FAILED = 1" in _AUDIT_PY
    main = _AUDIT_PY.split("def main(")[1]
    # The exit code is computed once from the aggregate and returned on BOTH
    # the JSON and human paths — --json cannot report a failure as a success.
    assert ("exit_code = EXIT_OK if overall_ok else EXIT_VALIDATION_FAILED"
            in main)
    assert main.count("return exit_code") >= 2
    # And a window that could not run at all is a failure, not an absence.
    runner = _AUDIT_PY.split("def _audit_window(")[1].split("\ndef ")[0]
    assert '"ok": False' in runner
    assert "audit could not run" in runner


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


def test_watermark_advances_only_on_success_or_a_clean_checkpoint():
    fn = _code_only(
        _LEDGER_REPO_PY.split("def record_sync_state(")[1].split("\ndef ")[0])
    assert 'status == "success"' in fn
    # A partial run may advance ONLY to an explicitly-flagged clean prefix.
    assert 'status == "partial" and watermark_is_checkpoint' in fn
    # A failed run never advances.
    assert 'status == "failed"' not in fn


# =============================================================================
# Fail-closed persistence — a write that fails is a FAILED sync
# =============================================================================
def _stub_hubspot_for_sync(monkeypatch, deals, *, complete=True):
    import connectors.hubspot_pull as hubspot
    import services.hubspot_deal_sync_service as svc

    monkeypatch.setattr(hubspot, "pull_deals_for_ledger", lambda **_: {
        "available": True, "complete": complete, "pages": 1, "error": None,
        "deals": deals})
    monkeypatch.setattr(hubspot, "fetch_portal_home_currency",
                        lambda: {"available": True, "home_currency_code": "USD",
                                 "verified": True})
    monkeypatch.setattr(hubspot, "fetch_deal_associations",
                        lambda deal_id: {"deal_id": str(deal_id),
                                         "contacts": [], "complete": True})
    monkeypatch.setattr(svc, "_fx_rates_for", lambda *a, **k: {})
    return hubspot


def _deal(deal_id, modified, *, amount="1000", currency_code="USD"):
    return {"id": deal_id, "properties": {
        "dealstage": "326093516", "hs_is_closed": "true",
        "hs_is_closed_won": "true", "closedate": "2026-07-10T00:00:00Z",
        "hs_lastmodifieddate": modified, "amount": amount,
        "deal_currency_code": currency_code}}


def test_a_failed_ledger_write_cannot_report_success(monkeypatch):
    """A database failure counted as "zero rows written" is how a sync reports
    success while the ledger silently loses a deal."""
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc

    _stub_hubspot_for_sync(monkeypatch, [_deal("D1", "2026-07-11T00:00:00Z")])
    recorded = {}
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "row": None})
    monkeypatch.setattr(repo, "upsert_deal", lambda row, **kw: {
        "available": False, "reason": "database_unavailable",
        "written": 0, "skipped_stale": 0, "error": "connection refused"})
    monkeypatch.setattr(repo, "record_sync_state",
                        lambda **kw: (recorded.update(kw)
                                      or {"available": True, "error": None}))

    result = svc.sync_deals(full_refresh=True)

    assert result["status"] == "failed", result
    assert result["write_failures"] == 1
    assert "ledger_write_failed" in (result["error"] or "")
    # The watermark must NOT move past a deal that was never persisted.
    assert result["watermark"] is None
    assert recorded["watermark"] is None
    assert recorded["status"] == "failed"


def test_a_failed_sync_state_write_downgrades_the_run(monkeypatch):
    """Coverage we could not record is coverage we cannot claim."""
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc

    _stub_hubspot_for_sync(monkeypatch, [_deal("D1", "2026-07-11T00:00:00Z")])
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "row": None})
    monkeypatch.setattr(repo, "upsert_deal", lambda row, **kw: {
        "available": True, "written": 1, "skipped_stale": 0, "error": None})
    monkeypatch.setattr(repo, "record_sync_state", lambda **kw: {
        "available": False, "reason": "database_unavailable",
        "error": "connection refused"})

    result = svc.sync_deals(full_refresh=True)
    assert result["status"] == "failed"
    assert "sync_state_write_failed" in result["error"]


def test_partial_write_failure_still_blocks_the_watermark(monkeypatch):
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc

    _stub_hubspot_for_sync(monkeypatch, [
        _deal("D1", "2026-07-11T00:00:00Z"),
        _deal("D2", "2026-07-12T00:00:00Z"),
    ])
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "row": None})

    def _write(row, **kw):
        if row["deal_id"] == "D2":
            return {"available": False, "reason": "database_unavailable",
                    "written": 0, "skipped_stale": 0, "error": "boom"}
        return {"available": True, "written": 1, "skipped_stale": 0,
                "error": None}

    monkeypatch.setattr(repo, "upsert_deal", _write)
    monkeypatch.setattr(repo, "record_sync_state",
                        lambda **kw: {"available": True, "error": None})

    result = svc.sync_deals(full_refresh=True)
    assert result["status"] == "partial"
    assert result["write_failures"] == 1
    # Only the clean prefix (D1) may be checkpointed — never past D2.
    assert result["watermark"] == "2026-07-11T00:00:00Z"
    assert result["watermark_is_checkpoint"] is True


# =============================================================================
# Resumable capped backfill
# =============================================================================
def test_capped_run_checkpoints_and_the_next_run_resumes_after_it(monkeypatch):
    """The 5,000-lookup cap previously restarted from the first page on every
    attempt, so a large portal could never finish a backfill."""
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc

    all_deals = [_deal(f"D{i}", f"2026-07-{10 + i:02d}T00:00:00Z")
                 for i in range(1, 5)]
    state = {"watermark": None}
    processed: list = []

    import connectors.hubspot_pull as hubspot

    def _pull(*, modified_since_ms=None, **_):
        # Emulate the connector's "modified at or after the watermark" filter.
        if modified_since_ms is None:
            visible = all_deals
        else:
            from datetime import datetime, timezone
            cutoff = datetime.fromtimestamp(modified_since_ms / 1000,
                                            tz=timezone.utc)
            visible = [d for d in all_deals
                       if datetime.fromisoformat(
                           d["properties"]["hs_lastmodifieddate"].replace(
                               "Z", "+00:00")) >= cutoff]
        return {"available": True, "complete": True, "pages": 1,
                "error": None, "deals": visible}

    _stub_hubspot_for_sync(monkeypatch, [])
    monkeypatch.setattr(hubspot, "pull_deals_for_ledger", _pull)
    monkeypatch.setattr(repo, "fetch_sync_state", lambda: {
        "available": True,
        "row": {"last_modified_watermark": state["watermark"]}})
    monkeypatch.setattr(repo, "upsert_deal", lambda row, **kw: (
        processed.append(row["deal_id"])
        or {"available": True, "written": 1, "skipped_stale": 0, "error": None}))

    def _record(**kw):
        if kw.get("watermark") is not None:
            state["watermark"] = kw["watermark"]
        return {"available": True, "error": None}

    monkeypatch.setattr(repo, "record_sync_state", _record)

    first = svc.backfill_deals(max_association_lookups=2)
    assert first["status"] == "partial"
    assert "association_lookup_cap_reached" in first["error"]
    assert processed == ["D1", "D2"], processed
    assert first["watermark_is_checkpoint"] is True
    # The checkpoint is the LAST deal actually committed, not the first.
    assert state["watermark"] == "2026-07-12T00:00:00Z"

    processed.clear()
    second = svc.backfill_deals(max_association_lookups=10)
    assert second["status"] == "success"
    # It RESUMED. D3 and D4 are new work; the 15-minute overlap legitimately
    # re-reads D2, which the idempotent upsert absorbs. D1 is never seen again.
    assert "D3" in processed and "D4" in processed
    assert "D1" not in processed, "the capped run restarted from the first page"


def test_a_capped_run_does_not_invent_failed_lookups(monkeypatch):
    """Deals the run never reached must not be written as `lookup_failed` —
    that manufactures evidence of a failure that never happened."""
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc

    _stub_hubspot_for_sync(monkeypatch, [
        _deal("D1", "2026-07-11T00:00:00Z"),
        _deal("D2", "2026-07-12T00:00:00Z"),
        _deal("D3", "2026-07-13T00:00:00Z"),
    ])
    seen: list = []
    monkeypatch.setattr(repo, "fetch_sync_state",
                        lambda: {"available": True, "row": None})
    monkeypatch.setattr(repo, "upsert_deal", lambda row, **kw: (
        seen.append((row["deal_id"], kw["associations_observed"]))
        or {"available": True, "written": 1, "skipped_stale": 0, "error": None}))
    monkeypatch.setattr(repo, "record_sync_state",
                        lambda **kw: {"available": True, "error": None})

    result = svc.sync_deals(full_refresh=True, max_association_lookups=1)
    assert [d for d, _ in seen] == ["D1"]
    assert result["association_failures"] == 0
    assert result["status"] == "partial"


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
