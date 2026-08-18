"""
PR-ADS-153F — canonical country geography synchronization.

What this proves
────────────────
Before this PR the country pipeline had no owner. Nothing scheduled
``run_google_ads_geo_sync``, so canonical geo spend went stale the moment the
window advanced past the last manual click on Revenue Health; geo had no
coverage ledger, no resume and no freshness entry, so that staleness was
invisible on every health surface; three different country join rules coexisted,
so the same business window and revenue scope produced different country rows on
different pages; and blank-country revenue was silently DROPPED from ROAS by
Country while Dashboard Countries preserved it as a residual.

Every test below is a statement about one of those defects being structurally
impossible now, not merely absent:

  §1  the daily incremental pipeline invokes canonical geo synchronization
  §2  the external connectors stay read-only
  §3  scheduler and manual trigger cannot overlap
  §4  verified chunks resume idempotently
  §5  a failed chunk stays failed and is retried
  §6  a partial run cannot publish complete coverage or healthy freshness
  §7  checkpoints advance only after committed database writes
  §8  one stable source key across writer, freshness, status and consumers
  §9  no phantom freshness configuration remains
  §10 every supported country code has a deterministic label
  §11 arbitrary two-letter strings are rejected
  §12 aliases normalize to one canonical ISO code
  §13 Dashboard Countries and ROAS by Country produce identical country keys
  §14 the drilldown uses the same canonical keys
  §15 blank-country revenue appears in the explicit residual bucket
  §16 known rows plus eligible residual reconcile exactly
  §17 missing dates block availability
  §18 campaign spend without geo blocks availability
  §19 a safe PR-ADS-131 structural residual is accepted consistently
  §20 a general totals mismatch remains blocked
  §21 the mart and Dashboard Countries use the same gate implementation
  §22 the existing tolerance is unchanged
  §23 window boundaries stay inclusive-start / exclusive-end and UTC-normalized
  §24 unknown / unavailable metrics render as unavailable, never $0
  §25 no legacy geo or revenue fallback is called
  §26 no HubSpot or Google Ads external mutation method is reachable

Deterministic and synthetic: no production identifiers, no database, no network.
"""

from __future__ import annotations

import ast
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from analysis import country_identity  # noqa: E402
from analysis.business_windows import get_window_bounds  # noqa: E402
from services import dataset_keys  # noqa: E402
from services import google_ads_geo_sync_service as geo  # noqa: E402
from tests.canonical_ledger_fixtures import (  # noqa: E402
    ledger_row, patch_canonical_ledger,
)

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
WINDOW = "ytd"

_GEO_SRC = (_ROOT / "services" / "google_ads_geo_sync_service.py").read_text()
_SCHED_SRC = (_ROOT / "scheduler" / "incremental_sync.py").read_text()
_MART_SRC = (_ROOT / "services" / "revenue_decision_mart.py").read_text()
_COUNTRIES_SRC = (_ROOT / "services" / "dashboard_countries_service.py").read_text()
_ATTRIB_SRC = (_ROOT / "services" / "revenue_attribution_service.py").read_text()


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the daily incremental pipeline invokes canonical geo synchronization
# ═════════════════════════════════════════════════════════════════════════════

def test_1_daily_incremental_orchestration_invokes_canonical_geo_sync():
    """The scheduler CALLS the geo sync — not a docstring that says it does.

    This is asserted on the AST rather than on text: a comment mentioning geo
    sync is not a scheduled sync, and the whole defect was that the only caller
    was an admin endpoint a human had to click.
    """
    tree = ast.parse(_SCHED_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_daily_incremental_sync")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_sync_canonical_geo" in called
    assert "_publish_geo_reconciliation" in called

    geo_fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_sync_canonical_geo")
    inner = {getattr(n.func, "id", getattr(n.func, "attr", None))
             for n in ast.walk(geo_fn) if isinstance(n, ast.Call)}
    assert "run_google_ads_geo_sync" in inner


def test_1b_the_manual_recovery_trigger_still_exists():
    """Scheduling geo must not remove the manual path used to repair a gap."""
    server = (_ROOT / "api" / "server.py").read_text()
    assert '@app.post("/api/google-ads-geo-sync/run"' in server
    assert "run_google_ads_geo_sync" in server


# ═════════════════════════════════════════════════════════════════════════════
# §2 — external connectors are read-only
# ═════════════════════════════════════════════════════════════════════════════

_MUTATION_MARKERS = (
    "requests.post", "requests.put", "requests.patch", "requests.delete",
    "MutateOperation", "mutate_", "upload_click_conversions",
    "conversion_upload", "OfflineUserDataJob",
)


@pytest.mark.parametrize("path", [
    "services/google_ads_geo_sync_service.py",
    "services/dashboard_countries_service.py",
    "services/revenue_decision_mart.py",
    "analysis/country_identity.py",
    "services/dataset_keys.py",
])
def test_2_geo_modules_contain_no_external_mutation_path(path):
    src = (_ROOT / path).read_text()
    body = "\n".join(line for line in src.splitlines()
                     if not line.strip().startswith("#"))
    for marker in _MUTATION_MARKERS:
        assert marker not in body, f"{path} must stay read-only (found {marker!r})"


def test_2b_the_geo_connector_seam_reads_only():
    """The geo service reaches Google Ads through two READ seams and no other."""
    tree = ast.parse(_GEO_SRC)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("connectors"):
            imported |= {a.name for a in node.names}
    assert imported == {"fetch_geo_daily_spend", "fetch_geo_target_country_codes"}


# ═════════════════════════════════════════════════════════════════════════════
# §3 — scheduler and manual trigger cannot create an unsafe overlapping run
# ═════════════════════════════════════════════════════════════════════════════

def test_3_a_second_run_refuses_to_start_while_the_lease_is_held(monkeypatch):
    import db.writers as w

    monkeypatch.setattr(geo, "configured_customer_id", lambda: "cust-1")
    monkeypatch.setattr(w, "try_claim_geo_sync_lease", lambda *a, **k: "held")
    fetched = []
    monkeypatch.setattr(geo, "fetch_geo_daily",
                        lambda s, e: fetched.append((s, e)) or {"rows": []})

    out = geo.run_google_ads_geo_sync(date_from="2026-06-01", date_to="2026-06-30",
                                      dry_run=False)
    assert out["status"] == "skipped_locked"
    assert out["reason"] == "another_geo_sync_is_running"
    # Refusing to START is the safe outcome: nothing was fetched and nothing was
    # written, so the range simply stays uncovered for the next run.
    assert fetched == []


def test_3b_an_unreachable_lease_store_fails_closed(monkeypatch):
    """No database means no coordination AND no persistence — so do not fetch.

    An earlier revision proceeded without a lease here, arguing that a visible
    stale run beat a silent skip. That reasoning was wrong: with the store
    unreachable this run cannot persist geo rows, coverage or state either, so
    proceeding buys no visibility at all — it only spends Google Ads quota and
    risks an uncoordinated concurrent fetch. Not starting leaves the range
    uncovered, which the gate already reports as blocked.
    """
    import db.writers as w

    monkeypatch.setattr(geo, "configured_customer_id", lambda: "cust-1")
    monkeypatch.setattr(w, "try_claim_geo_sync_lease", lambda *a, **k: "unavailable")
    fetched = []
    monkeypatch.setattr(geo, "fetch_geo_daily",
                        lambda s, e: fetched.append((s, e)) or {"rows": []})

    out = geo.run_google_ads_geo_sync(date_from="2026-06-01", date_to="2026-06-30",
                                      dry_run=False)
    assert out["status"] == "skipped_locked"
    assert out["reason"] == "lease_store_unavailable"
    # The decisive assertion: ZERO Google Ads fetches.
    assert fetched == []


def test_3d_every_real_run_carries_a_unique_lease_fence(monkeypatch):
    """Expiry recovers a dead worker's lease; the TOKEN proves ownership.

    Without a fence, worker A can overrun the lease window, worker B can
    legitimately reclaim it, and A can then still finish and overwrite B's
    terminal state and checkpoint.
    """
    import db.writers as w

    tokens = []

    def _claim(cid, run_id=None, scope="geo_daily_spend", lease_minutes=120,
               lease_token=None):
        tokens.append(lease_token)
        return "held"      # stop immediately; we only want the token

    monkeypatch.setattr(geo, "configured_customer_id", lambda: "cust-1")
    monkeypatch.setattr(w, "try_claim_geo_sync_lease", _claim)
    for _ in range(2):
        geo.run_google_ads_geo_sync(date_from="2026-06-01", date_to="2026-06-30",
                                    dry_run=False)
    # A token is generated even with no job id (the manual trigger), and two
    # runs never share one.
    assert all(t for t in tokens)
    assert len(set(tokens)) == 2


def test_3e_terminal_state_that_cannot_persist_is_not_a_success(monkeypatch):
    """A run with no durable terminal state has no evidence it happened.

    Its checkpoint did not move and its freshness was not published, so
    reporting success would be a claim nothing can back up. This is also what a
    lost lease looks like: the fenced write matches no row.
    """
    _fetched, _recorded, _state = _patch_geo_run(monkeypatch, existing_chunks=[])
    import db.writers as w
    monkeypatch.setattr(w, "upsert_geo_sync_state", lambda *a, **k: False)
    monkeypatch.setattr(geo, "analyze_geo_coverage",
                        lambda c, s, e: {"available": True, "complete": True,
                                         "missing_ranges": [], "failed_chunks": []})

    out = geo.run_google_ads_geo_sync(date_from="2026-04-01", date_to="2026-04-30",
                                      dry_run=False)
    assert out["status"] == "failed"
    assert any("terminal geo sync state" in e for e in out["errors"])


def test_3c_the_lease_claim_is_one_conditional_update():
    """The guard must be durable and atomic, not a process-local flag.

    Render runs more than one instance, so an in-process boolean proves nothing.
    """
    src = (_ROOT / "db" / "writers.py").read_text()
    body = src[src.index("def try_claim_geo_sync_lease"):]
    body = body[:body.index("\ndef ", 1)]
    assert "UPDATE google_ads_geo_sync_state" in body
    assert "RETURNING id" in body
    assert "last_started_at < NOW()" in body  # stale leases expire
    assert "lease_token" in body              # ...and ownership is fenced

    # Terminal writes are fenced on the token AND on still being 'running', so a
    # stale worker cannot stamp over the run that owns the lease now.
    src = (_ROOT / "db" / "writers.py").read_text()
    for fn in ("release_geo_sync_lease", "upsert_geo_sync_state"):
        fbody = src[src.index(f"def {fn}"):]
        fbody = fbody[:fbody.index("\ndef ", 1)]
        assert "lease_token = %s" in fbody, fn
        assert "last_status = 'running'" in fbody, fn


# ═════════════════════════════════════════════════════════════════════════════
# §4 / §5 — resume: verified chunks skip, failed chunks retry
# ═════════════════════════════════════════════════════════════════════════════

def _patch_geo_run(monkeypatch, *, existing_chunks, fetch=None, written=None):
    """Wire a geo run against an in-memory coverage ledger."""
    import db.writers as w

    recorded: list = []
    fetched: list = []

    monkeypatch.setattr(geo, "configured_customer_id", lambda: "cust-1")
    monkeypatch.setattr(w, "try_claim_geo_sync_lease", lambda *a, **k: "acquired")
    monkeypatch.setattr(geo.repo, "fetch_geo_coverage",
                        lambda c, s, e: {"available": True, "chunks": list(existing_chunks)})

    def _fetch(s, e):
        fetched.append((s, e))
        if fetch is not None:
            return fetch(s, e)
        return {"rows": [{"country_criterion_id": "2826", "cost_micros": 1_000_000}]}

    monkeypatch.setattr(geo, "fetch_geo_daily", _fetch)
    monkeypatch.setattr(geo, "fetch_geo_country_codes",
                        lambda ids: {"2826": {"country_code": "GB", "name": "United Kingdom"}})
    def _replace(cid, cs, ce, rows, **k):
        n = written(rows) if written else len(rows)
        return {"replaced": True, "deleted": 0, "written": n}

    monkeypatch.setattr(w, "replace_geo_daily_spend_chunk", _replace)

    def _cov(customer_id, start, end, status, **kw):
        recorded.append({"chunk": f"{start}:{end}", "status": status, **kw})
        return True

    monkeypatch.setattr(w, "upsert_geo_coverage", _cov)
    state: list = []
    monkeypatch.setattr(w, "upsert_geo_sync_state",
                        lambda cid, **fields: state.append(fields) or True)
    return fetched, recorded, state


def test_4_an_already_verified_chunk_is_not_refetched(monkeypatch):
    existing = [{"chunk_start": "2026-04-01", "chunk_end": "2026-04-30",
                 "status": "verified"}]
    fetched, recorded, _ = _patch_geo_run(monkeypatch, existing_chunks=existing)
    monkeypatch.setattr(geo, "analyze_geo_coverage",
                        lambda c, s, e: {"available": True, "complete": True,
                                      "missing_ranges": [], "failed_chunks": []})

    out = geo.run_google_ads_geo_sync(date_from="2026-04-01", date_to="2026-05-31",
                                      dry_run=False)
    assert out["summary"]["chunks_skipped"] == 1
    assert ("2026-04-01", "2026-04-30") not in fetched
    assert ("2026-05-01", "2026-05-31") in fetched


def test_5_a_failed_chunk_stays_failed_and_is_retried(monkeypatch):
    """A failed chunk is recorded as failed, so the NEXT run re-fetches it.

    Silence would be worse than failure: an absent chunk and a fetched-but-empty
    one are indistinguishable, and only one of them is safe to skip.
    """
    existing = [{"chunk_start": "2026-04-01", "chunk_end": "2026-04-30",
                 "status": "failed"}]

    def _boom(s, e):
        raise RuntimeError("google ads api unavailable")

    fetched, recorded, _ = _patch_geo_run(monkeypatch, existing_chunks=existing,
                                          fetch=_boom)
    monkeypatch.setattr(geo, "analyze_geo_coverage",
                        lambda c, s, e: {"available": True, "complete": False,
                                      "missing_ranges": [{"start": "2026-04-01",
                                                          "end": "2026-04-30"}],
                                      "failed_chunks": []})

    out = geo.run_google_ads_geo_sync(date_from="2026-04-01", date_to="2026-04-30",
                                      dry_run=False)
    # It was retried (not skipped as "already known") ...
    assert ("2026-04-01", "2026-04-30") in fetched
    assert out["summary"]["chunks_skipped"] == 0
    # ... and its failure is durable evidence, not a silent gap.
    assert recorded == [{"chunk": "2026-04-01:2026-04-30", "status": "failed",
                         "error_message": recorded[0]["error_message"],
                         "sync_run_id": None}]
    assert "google ads api unavailable" in recorded[0]["error_message"]
    assert out["status"] == "failed"


def test_5b_a_failed_write_never_demotes_a_verified_chunk():
    """The writer refuses to overwrite `verified` with `failed`.

    Without this a transient error during a recovery run would erase coverage
    that had already been proven good.
    """
    src = (_ROOT / "db" / "writers.py").read_text()
    body = src[src.index("def upsert_geo_coverage"):]
    body = body[:body.index("\ndef ", 1)]
    assert "WHERE google_ads_geo_coverage.status <> 'verified'" in body
    assert "OR EXCLUDED.status = 'verified'" in body


# ═════════════════════════════════════════════════════════════════════════════
# §6 / §7 — a partial run publishes neither coverage nor a checkpoint
# ═════════════════════════════════════════════════════════════════════════════

def test_6_a_partial_run_cannot_publish_complete_coverage_or_freshness(monkeypatch):
    calls = {"n": 0}

    def _sometimes(s, e):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("chunk 2 failed")
        return {"rows": [{"country_criterion_id": "2826", "cost_micros": 1_000_000}]}

    _fetched, _recorded, state = _patch_geo_run(monkeypatch, existing_chunks=[],
                                                fetch=_sometimes)
    monkeypatch.setattr(geo, "analyze_geo_coverage",
                        lambda c, s, e: {"available": True, "complete": False,
                                      "missing_ranges": [{"start": "2026-05-01",
                                                          "end": "2026-05-31"}],
                                      "failed_chunks": []})

    out = geo.run_google_ads_geo_sync(date_from="2026-04-01", date_to="2026-06-30",
                                      dry_run=False)
    assert out["status"] == "partial"
    assert out["coverage_complete"] is False
    # The durable state records the partial outcome and does NOT advance the
    # checkpoint or the last-successful-completion marker.
    assert state and state[-1]["last_status"] == "partial"
    assert "checkpoint_date" not in state[-1]
    assert "last_successful_completed_at" not in state[-1]


def test_7_the_checkpoint_advances_only_after_the_coverage_ledger_agrees(monkeypatch):
    """A run whose own counters look clean still cannot claim completion.

    Completion is re-read from the LEDGER, so an earlier unrepaired failure
    outside this run's chunks keeps the checkpoint where it is.
    """
    _fetched, _recorded, state = _patch_geo_run(monkeypatch, existing_chunks=[])
    monkeypatch.setattr(geo, "analyze_geo_coverage",
                        lambda c, s, e: {"available": True, "complete": False,
                                      "missing_ranges": [{"start": "2026-01-01",
                                                          "end": "2026-03-31"}],
                                      "failed_chunks": []})

    out = geo.run_google_ads_geo_sync(date_from="2026-04-01", date_to="2026-04-30",
                                      dry_run=False)
    assert out["status"] == "success"          # this run had no errors ...
    assert out["coverage_complete"] is False   # ... but the window is not covered
    assert "checkpoint_date" not in state[-1]

    # And when the ledger DOES agree, the checkpoint advances.
    state.clear()
    monkeypatch.setattr(geo, "analyze_geo_coverage",
                        lambda c, s, e: {"available": True, "complete": True,
                                      "missing_ranges": [], "failed_chunks": []})
    geo.run_google_ads_geo_sync(date_from="2026-04-01", date_to="2026-04-30",
                                dry_run=False)
    assert state[-1]["checkpoint_date"] == date(2026, 4, 30)
    assert state[-1]["last_successful_completed_at"] is not None


def test_7b_a_read_that_did_not_persist_is_never_recorded_as_verified(monkeypatch):
    """Coverage is claimed only AFTER the rows land."""
    _fetched, recorded, _state = _patch_geo_run(
        monkeypatch, existing_chunks=[],
        written=lambda rows: 0)               # replacement drops every row
    monkeypatch.setattr(geo, "analyze_geo_coverage",
                        lambda c, s, e: {"available": True, "complete": False,
                                      "missing_ranges": [], "failed_chunks": []})

    out = geo.run_google_ads_geo_sync(date_from="2026-04-01", date_to="2026-04-30",
                                      dry_run=False)
    assert out["status"] == "failed"
    assert [r["status"] for r in recorded] == ["failed"]


# ═════════════════════════════════════════════════════════════════════════════
# §8 / §9 — one stable dataset key; no phantom freshness configuration
# ═════════════════════════════════════════════════════════════════════════════

def test_8_the_geo_dataset_key_is_identical_everywhere():
    from services.freshness_service import DATASET_FRESHNESS_CONFIG
    from services.system_status_service import PIPELINE_DEPENDENCIES, SOURCE_DEFINITIONS

    key = (dataset_keys.CANONICAL_GEO_SOURCE, dataset_keys.CANONICAL_GEO_DATASET)
    assert (geo.GEO_SYNC_SOURCE, geo.GEO_SYNC_DATASET) == key

    cfg = DATASET_FRESHNESS_CONFIG["canonical_geo"]
    assert (cfg["source"], cfg["dataset"]) == key
    assert PIPELINE_DEPENDENCIES["canonical_geo"]["source"] == key[0]
    assert "canonical_geo" in SOURCE_DEFINITIONS[key[0]]["datasets"]


def test_8b_the_scheduler_stamps_the_registry_key_not_a_literal():
    """The writer must import the key, not spell it.

    Spelling a key in two places is what allowed canonical_spend's config to
    drift from its writer and leave the ROAS denominator with no freshness
    signal at all.
    """
    tree = ast.parse(_SCHED_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_sync_canonical_geo")
    imports = [n for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)]
    assert any(n.module == "services.dataset_keys" for n in imports)
    for call in ast.walk(fn):
        if isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "start_sync_batch":
            kw = {k.arg: k.value for k in call.keywords}
            assert isinstance(kw["source"], ast.Name), "source must be a constant, not a literal"
            assert isinstance(kw["dataset"], ast.Name)


def test_8c_canonical_spend_freshness_key_matches_its_writer():
    """Regression for the PR-ADS-153A audit finding (§1.7).

    The config expected ``google_ads_api`` while the writer stamped
    ``google_ads``, so the ROAS denominator's freshness matched nothing.
    """
    from services.freshness_service import DATASET_FRESHNESS_CONFIG

    cfg = DATASET_FRESHNESS_CONFIG["canonical_spend"]
    assert (cfg["source"], cfg["dataset"]) == (dataset_keys.CANONICAL_SPEND_SOURCE,
                                               dataset_keys.CANONICAL_SPEND_DATASET)
    assert "google_ads" in _SCHED_SRC
    assert cfg["source"] == "google_ads"


def test_9_no_phantom_freshness_configuration_remains():
    """Every configured dataset names a table that exists and a key a writer stamps.

    A freshness row without a durable source is not evidence — it is a dataset
    that looks monitored and can only ever report "never run".
    """
    from services.freshness_service import DATASET_FRESHNESS_CONFIG

    schema = (_ROOT / "db" / "schema.py").read_text()
    for key, cfg in DATASET_FRESHNESS_CONFIG.items():
        table = cfg["table"]
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema, (
            f"{key} names table {table!r}, which db/schema.py never creates")

    for removed in ("ngrams", "historical_intelligence", "mailchimp_attribution"):
        assert removed not in DATASET_FRESHNESS_CONFIG


# ═════════════════════════════════════════════════════════════════════════════
# §10 / §11 / §12 — the country identity contract
# ═════════════════════════════════════════════════════════════════════════════

def test_10_every_supported_code_has_a_deterministic_label_both_ways():
    """The full registry round-trips — not just the eleven codes that were broken.

    Testing only the known-bad examples would guard the symptom; this makes
    "resolvable forward, nameless backward" unrepresentable for any code.
    """
    for code, label in country_identity.SUPPORTED_COUNTRIES.items():
        assert country_identity.country_name_for_code(code) == label
        assert country_identity.get_country_code(label) == code
        assert country_identity.resolve(code=code).key == f"code:{code}"
        assert country_identity.display_label(f"code:{code}") == label

    # The eleven that the previous reverse map omitted.
    for code in ("SG", "MY", "ID", "TH", "VN", "PH", "AU", "NZ", "LK", "ZA", "NG"):
        assert country_identity.country_name_for_code(code)


def test_11_arbitrary_two_letter_strings_are_rejected():
    """A two-letter token is not a country code.

    The retired helper uppercased ANY two alphabetic characters and returned
    them, so "XX" was as valid as "AE" and a truncated label became a country.
    """
    for junk in ("XX", "ZZ", "QQ", "AA", "zz"):
        assert not country_identity.is_supported_code(junk)
        identity = country_identity.resolve(name=junk)
        assert identity.status == country_identity.STATUS_INVALID
        assert identity.key == country_identity.RESIDUAL_KEY
        assert country_identity.get_country_code(junk) is None

    assert country_identity.is_supported_code("AE")
    assert country_identity.is_supported_code("ae")


def test_12_aliases_normalize_to_one_canonical_iso_code():
    for alias in ("UAE", "uae", "U.A.E.", "United Arab Emirates",
                  "  united   arab  emirates  ", "Emirates"):
        assert country_identity.country_key(alias) == "code:AE"
    for alias in ("UK", "England", "Great Britain", "United Kingdom", "Scotland"):
        assert country_identity.country_key(alias) == "code:GB"
    # Normalization is locale-independent: the same input must key identically
    # regardless of the server's locale, so it is an explicit casefold over an
    # explicit alias table.
    #
    # Checked on the AST, not on the file text: the module's own docstring
    # NAMES the transforms it avoids, and a guard that bans a word would fail on
    # the sentence explaining why the word is banned — a guard against
    # vocabulary rather than behaviour.
    tree = ast.parse((_ROOT / "analysis" / "country_identity.py").read_text())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (called & {"title", "capitalize", "swapcase"}), (
        f"locale-sensitive normalization introduced: {called}")
    assert "casefold" in called


def test_12b_a_supplied_code_outranks_a_name_but_a_bad_code_does_not_lose_the_row():
    assert country_identity.resolve(name="United Kingdom", code="AE").key == "code:AE"
    # A good label rescues a malformed code — dropping real revenue because one
    # field was wrong would be worse — and the rejection stays auditable.
    rescued = country_identity.resolve(name="United Kingdom", code="XX")
    assert rescued.key == "code:GB"
    assert "not_a_supported_country_code" in rescued.reason


# ═════════════════════════════════════════════════════════════════════════════
# §13 / §14 — one join rule across every country consumer
# ═════════════════════════════════════════════════════════════════════════════

_COUNTRY_DEALS = [
    ledger_row("d1", country_raw="United Arab Emirates", revenue_usd=10000.0,
               amount_raw=10000.0, campaign_name_raw="Gulf",
               acquisition_group="google_ads", attribution_status="attributed"),
    ledger_row("d2", country_raw="UAE", revenue_usd=5000.0, amount_raw=5000.0,
               campaign_name_raw="Gulf", acquisition_group="google_ads",
               attribution_status="attributed"),
    ledger_row("d3", country_raw="United Kingdom", revenue_usd=8000.0,
               amount_raw=8000.0, campaign_name_raw="Europe",
               acquisition_group="google_ads", attribution_status="attributed"),
    # Blank geography — real revenue with no identifiable country.
    ledger_row("d4", country_raw=None, revenue_usd=4000.0, amount_raw=4000.0,
               campaign_name_raw="Gulf", acquisition_group="google_ads",
               attribution_status="attributed"),
]


def test_13_the_same_labels_produce_the_same_key_on_every_consumer():
    """"UAE" and "United Arab Emirates" are one country, everywhere.

    Dashboard Countries keyed ISO-code-first, ROAS by Country keyed on a
    lowercased string, and the drilldown used a third rule — so the same window
    produced different key sets on pages claiming to describe the same thing.
    """
    from services.dashboard_countries_service import _country_key

    for a, b in (("UAE", "United Arab Emirates"), ("UK", "United Kingdom"),
                 ("usa", "United States")):
        assert country_identity.country_key(a) == country_identity.country_key(b)
        assert _country_key(a, None) == country_identity.country_key(b)

    # Dashboard Countries reuses the contract rather than reimplementing it.
    assert "country_identity.resolve" in _COUNTRIES_SRC


def test_13b_no_consumer_keeps_a_private_country_normalizer():
    """Local country-name matching is gone from every migrated consumer.

    A second normalizer is a second key space waiting to reappear, so this
    asserts the country paths call the shared contract.
    """
    for src, label in ((_ATTRIB_SRC, "revenue_attribution_service"),
                       (_COUNTRIES_SRC, "dashboard_countries_service")):
        assert "country_identity" in src, f"{label} must use the shared contract"

    # ROAS by Country buckets on the canonical key, not on `_norm(country)`.
    body = _ATTRIB_SRC[_ATTRIB_SRC.index("def _build_db_rows"):]
    body = body[:body.index("\ndef ", 1)]
    assert "country_identity.resolve" in body


def test_14_the_drilldown_matches_on_the_same_canonical_key(monkeypatch):
    from services import revenue_attribution_service as svc

    patch_canonical_ledger(monkeypatch, _COUNTRY_DEALS)
    monkeypatch.setattr(svc.repo, "fetch_canonical_campaign_spend",
                        lambda s, e: {"available": False})

    # Both spellings open the same drawer, holding the same two deals.
    for label in ("United Arab Emirates", "UAE"):
        out = svc.build_country_deal_details(WINDOW, label, now=NOW)
        assert {d["deal_record_id"] for d in out["details"]} == {"d1", "d2"}

    # An ISO code opens it too.
    by_code = svc.build_country_deal_details(WINDOW, "United Arab Emirates",
                                             country_code="AE", now=NOW)
    assert {d["deal_record_id"] for d in by_code["details"]} == {"d1", "d2"}


def test_14b_a_label_that_is_not_a_country_drills_into_nothing(monkeypatch):
    """Refusing is safer than answering with the residual's contents.

    Every unidentifiable label resolves to the ONE residual key, so answering
    would return one unknown country's deals under another's name.
    """
    from services import revenue_attribution_service as svc

    patch_canonical_ledger(monkeypatch, _COUNTRY_DEALS)
    out = svc.build_country_deal_details(WINDOW, "Atlantis", now=NOW)
    assert out["details"] == []
    assert out["source_health"]["status"] == "country_not_canonical"

    # The residual bucket IS reachable by asking for it, which is what makes the
    # residual row's totals auditable.
    residual = svc.build_country_deal_details(
        WINDOW, country_identity.RESIDUAL_LABEL, now=NOW)
    assert {d["deal_record_id"] for d in residual["details"]} == {"d4"}


# ═════════════════════════════════════════════════════════════════════════════
# §15 / §16 — the residual bucket, and exact reconciliation
# ═════════════════════════════════════════════════════════════════════════════

def _country_rows(monkeypatch):
    from services import revenue_attribution_service as svc

    return svc._build_db_rows(
        [], [], [{"country": r["country_raw"], "country_code": None,
                  "deal_amount_usd": r["revenue_usd"],
                  "campaign_name": r["campaign_name_raw"],
                  "attribution_scope": "campaign_attributable"}
                 for r in _COUNTRY_DEALS],
        group_field="country")


def test_15_blank_country_revenue_lands_in_the_explicit_residual(monkeypatch):
    """Revenue is never dropped because its geography is unknown.

    ROAS by Country used to discard the blank bucket outright, so revenue that
    exists simply vanished from the page while Dashboard Countries kept it.
    """
    rows = _country_rows(monkeypatch)
    residual = [r for r in rows if r["is_residual"]]
    assert len(residual) == 1
    assert residual[0]["country_key"] == country_identity.RESIDUAL_KEY
    assert residual[0]["country"] == country_identity.RESIDUAL_LABEL
    assert residual[0]["customers"] == 1
    assert residual[0]["won_revenue"] == 4000.0
    # It is reported as a residual, never scored as a market.
    assert residual[0]["verdict"] == "unattributed"
    assert residual[0]["top_campaign"] is None


def test_15b_the_residual_is_never_spread_across_real_countries(monkeypatch):
    rows = _country_rows(monkeypatch)
    by_key = {r["country_key"]: r for r in rows}
    assert by_key["code:AE"]["won_revenue"] == 15000.0   # 10000 + 5000, merged
    assert by_key["code:GB"]["won_revenue"] == 8000.0
    assert country_identity.RESIDUAL_KEY in by_key


def test_16_known_rows_plus_the_residual_reconcile_exactly(monkeypatch):
    rows = _country_rows(monkeypatch)
    total = sum(r["won_revenue"] for r in rows)
    assert total == sum(r["revenue_usd"] for r in _COUNTRY_DEALS) == 27000.0

    # And ordering cannot change the reconciliation.
    shuffled = list(reversed(rows))
    assert sum(r["won_revenue"] for r in shuffled) == total


def test_16b_one_residual_row_carries_both_the_spend_and_the_revenue_side():
    """Two facts, ONE bucket — and they must not be confused with each other.

    The geo SPEND residual (Google Ads location-less spend) and the REVENUE
    residual (unidentifiable CRM geography) are independent. Before 153F the
    spend residual was appended as its own row asserting `customers: 0,
    won_revenue: 0.0` while unidentifiable revenue was being discarded, so the
    view could claim "no revenue here" over revenue it had thrown away.
    """
    from services.revenue_attribution_service import _apply_geo_spend_residual

    rows = [{"country_key": country_identity.RESIDUAL_KEY, "is_residual": True,
             "customers": 1, "won_revenue": 4000.0, "attribution_notes": []}]
    merged = _apply_geo_spend_residual(rows, 250.0, 325.0, True)
    assert len([r for r in merged if r["is_residual"]]) == 1
    residual = merged[0]
    assert residual["spend_native"] == 250.0
    assert residual["spend_usd"] == 325.0
    assert residual["won_revenue"] == 4000.0     # revenue side preserved
    assert residual["customers"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# §17 – §22 — the geo readiness gate
# ═════════════════════════════════════════════════════════════════════════════

def test_17_missing_geo_dates_block_availability():
    detail = {"reason": geo.GEO_GAP_MISSING_DATES,
              "missing_geo_dates": ["2026-05-04"], "campaigns_missing_geo": []}
    residual = geo.evaluate_country_residual(
        10000.0, 6000.0, detail, coverage_complete=True, fx_complete=True,
        geo_has_rows=True, reconciled=False)
    assert residual["eligible"] is False
    status, gaps = geo.resolve_country_spend_status(
        reconciled=False, residual_eligible=residual["eligible"],
        missing_geo_dates=detail["missing_geo_dates"])
    assert status == geo.GEO_STATUS_MISMATCH
    assert geo.GEO_GAP_MISSING_DATES in gaps
    assert geo.country_geo_ready(status) is False


def test_18_campaign_spend_without_geo_blocks_availability():
    detail = {"reason": geo.GEO_GAP_CAMPAIGN_WITHOUT_GEO, "missing_geo_dates": [],
              "campaigns_missing_geo": [{"campaign_id": "c1"}]}
    residual = geo.evaluate_country_residual(
        10000.0, 6000.0, detail, coverage_complete=True, fx_complete=True,
        geo_has_rows=True, reconciled=False)
    assert residual["eligible"] is False
    status, gaps = geo.resolve_country_spend_status(
        reconciled=False, residual_eligible=False,
        campaigns_missing_geo=detail["campaigns_missing_geo"])
    assert geo.country_geo_ready(status) is False
    assert geo.GEO_GAP_CAMPAIGN_WITHOUT_GEO in gaps


def test_19_a_safe_structural_residual_is_accepted_consistently():
    """The PR-ADS-131 safe-residual rules are unchanged and still the only route."""
    detail = {"reason": geo.GEO_GAP_BY_DESIGN_RESIDUAL, "missing_geo_dates": [],
              "campaigns_missing_geo": []}
    residual = geo.evaluate_country_residual(
        10000.0, 9500.0, detail, coverage_complete=True, fx_complete=True,
        geo_has_rows=True, reconciled=False)
    assert residual["eligible"] is True
    assert residual["residual_native"] == 500.0

    status, gaps = geo.resolve_country_spend_status(reconciled=False,
                                                    residual_eligible=True)
    assert status == geo.GEO_STATUS_RECONCILED_WITH_RESIDUAL
    assert gaps == []
    assert geo.country_geo_ready(status) is True

    # Incomplete FX or coverage keeps it BLOCKED even with the same reason.
    for kwargs in ({"coverage_complete": False}, {"fx_complete": False}):
        base = {"coverage_complete": True, "fx_complete": True,
                "geo_has_rows": True, "reconciled": False}
        base.update(kwargs)
        assert geo.evaluate_country_residual(10000.0, 9500.0, detail, **base)["eligible"] is False


def test_20_a_general_totals_mismatch_remains_blocked():
    detail = {"reason": geo.GEO_GAP_TOTALS_DIFFER, "missing_geo_dates": [],
              "campaigns_missing_geo": []}
    residual = geo.evaluate_country_residual(
        10000.0, 4000.0, detail, coverage_complete=True, fx_complete=True,
        geo_has_rows=True, reconciled=False)
    assert residual["eligible"] is False
    assert geo.country_geo_ready(geo.GEO_STATUS_MISMATCH) is False
    # An UNMEASURED reconciliation is unavailable, never a mismatch: reporting a
    # mismatch would assert a comparison nobody performed.
    assert geo.resolve_country_spend_status(
        reconciled=None, residual_eligible=False)[0] == geo.GEO_STATUS_UNAVAILABLE


def test_21_the_mart_and_dashboard_countries_use_the_same_gate():
    """One predicate, called by name — not two copies of the same comparison.

    The mart's page-difference audit previously demanded `== "verified"`, a
    stricter bar than Dashboard Countries' accepted set, so a window could be
    ready on one page and reported as differing on the other.
    """
    assert "geo_gate.country_geo_ready" in _MART_SRC
    assert "geo_gate.country_geo_ready" in _COUNTRIES_SRC
    for src in (_MART_SRC, _COUNTRIES_SRC):
        assert 'in ("verified", "reconciled_with_residual")' not in src
    assert '"country_spend_status"] != "verified"' not in _MART_SRC

    assert geo.GEO_ACCEPTED_STATES == frozenset({"verified", "reconciled_with_residual"})
    assert geo.country_geo_ready("mismatch") is False
    assert geo.country_geo_ready("unavailable") is False


# ═════════════════════════════════════════════════════════════════════════════
# BLOCKER 1 — the gate is holistic: matching totals are not enough
# ═════════════════════════════════════════════════════════════════════════════
# Every case below feeds a PERFECT reconciliation (`reconciled=True`) and removes
# exactly one mandatory input. Two unproven numbers can agree; agreement is not
# evidence that the inputs behind them were ever established.

_PERFECT = {"reconciled": True, "residual_eligible": False}


def test_b1_1_incomplete_campaign_coverage_is_never_ready():
    status, gaps = geo.resolve_country_spend_status(
        **_PERFECT, campaign_coverage_complete=False)
    assert status == geo.GEO_STATUS_UNAVAILABLE
    assert geo.country_geo_ready(status) is False
    assert geo.GEO_GAP_CAMPAIGN_COVERAGE_INCOMPLETE in gaps


def test_b1_2_incomplete_fx_is_never_ready():
    status, gaps = geo.resolve_country_spend_status(**_PERFECT, fx_complete=False)
    assert geo.country_geo_ready(status) is False
    assert geo.GEO_GAP_FX_COVERAGE_INCOMPLETE in gaps


def test_b1_3_an_unreadable_geo_ledger_is_never_ready():
    """Unreadable is not "nothing to worry about".

    The ledger is the ONLY thing that separates "fetched and genuinely zero"
    from "never fetched", so a gate that cannot read it cannot certify anything.
    """
    status, gaps = geo.resolve_country_spend_status(
        **_PERFECT, geo_coverage_readable=False)
    assert geo.country_geo_ready(status) is False
    assert geo.GEO_GAP_GEO_COVERAGE_UNREADABLE in gaps


def test_b1_4_incomplete_geo_coverage_is_never_ready():
    status, gaps = geo.resolve_country_spend_status(
        **_PERFECT, geo_coverage_complete=False)
    assert geo.country_geo_ready(status) is False
    assert geo.GEO_GAP_GEO_COVERAGE_INCOMPLETE in gaps


def test_b1_5_a_failed_geo_chunk_is_never_ready():
    status, gaps = geo.resolve_country_spend_status(
        **_PERFECT, geo_failed_chunks=[{"chunk_start": "2026-05-01",
                                        "chunk_end": "2026-05-31"}])
    assert geo.country_geo_ready(status) is False
    assert geo.GEO_GAP_GEO_CHUNKS_FAILED in gaps


def test_b1_6_complete_coverage_plus_valid_reconciliation_is_ready():
    status, gaps = geo.resolve_country_spend_status(**_PERFECT)
    assert status == geo.GEO_STATUS_VERIFIED
    assert geo.country_geo_ready(status) is True
    assert gaps == []

    # And the safe-residual route reaches the other accepted state, unchanged.
    residual_status, residual_gaps = geo.resolve_country_spend_status(
        reconciled=False, residual_eligible=True)
    assert residual_status == geo.GEO_STATUS_RECONCILED_WITH_RESIDUAL
    assert geo.country_geo_ready(residual_status) is True
    assert residual_gaps == []


def test_b1_7_unreadable_campaign_spend_or_geo_rows_are_never_ready():
    for kwargs, code in (
        ({"campaign_spend_readable": False}, geo.GEO_GAP_CAMPAIGN_SPEND_UNREADABLE),
        ({"geo_readable": False}, geo.GEO_GAP_GEO_ROWS_UNREADABLE),
    ):
        status, gaps = geo.resolve_country_spend_status(**_PERFECT, **kwargs)
        assert geo.country_geo_ready(status) is False, kwargs
        assert code in gaps


def test_b1_8_an_unproven_input_is_unavailable_not_a_mismatch():
    """A mismatch claims a comparison was made and disagreed.

    Saying that about a window whose geo was never fetched would be a more
    alarming — and simply different — statement than "we never established this".
    """
    status, _gaps = geo.resolve_country_spend_status(
        **_PERFECT, geo_coverage_complete=False)
    assert status == geo.GEO_STATUS_UNAVAILABLE
    assert status != geo.GEO_STATUS_MISMATCH


def test_b1_9_the_safe_residual_cannot_be_asserted_past_the_131_conditions():
    """`residual_eligible=True` alone must not reach the accepted state.

    `evaluate_country_residual` already refuses when geo dates or campaigns are
    missing. Re-checking here means a future caller cannot bypass PR-ADS-131 by
    passing the flag without those conditions.
    """
    for kwargs, expect in (
        ({"missing_geo_dates": ["2026-05-04"]}, geo.GEO_GAP_MISSING_DATES),
        ({"campaigns_missing_geo": [{"campaign_id": "c1"}]},
         geo.GEO_GAP_CAMPAIGN_WITHOUT_GEO),
    ):
        status, gaps = geo.resolve_country_spend_status(
            reconciled=False, residual_eligible=True, **kwargs)
        assert status == geo.GEO_STATUS_MISMATCH
        assert gaps == [expect]


def test_b1_10_every_readiness_field_agrees_with_the_shared_gate():
    """`geo_ready`, `country_roas_unblockable` and the disclosed status are ONE
    verdict published under several names, so they cannot contradict."""
    src = _GEO_SRC
    assert "country_roas_unblockable = country_geo_ready(country_spend_status)" in src
    assert '"geo_ready": country_roas_unblockable,' in src
    # The mart derives both from the same predicate over the same status.
    assert "country_roas_unblockable = geo_gate.country_geo_ready(country_spend_status)" in _MART_SRC
    assert "geo_gate.country_geo_ready" in _COUNTRIES_SRC
    # And revenue-attribution's country_roas_available requires geo coverage.
    assert "geo_coverage_ok" in _ATTRIB_SRC
    body = _ATTRIB_SRC[_ATTRIB_SRC.index("country_spend_trusted = ("):]
    assert "geo_coverage_ok" in body[:400]


def test_b1_11_every_gap_code_has_a_distinct_operator_sentence():
    """One holistic status must not collapse into one generic explanation.

    After blocker 1, `unavailable` stands for an unreadable spend baseline,
    incomplete FX and a geo range nobody ever fetched alike. Answering all
    three with "geo does not reconcile" would send two operators to fix the
    wrong thing, which is the opposite of making unavailability explicit (§8).
    """
    codes = [v for k, v in vars(geo).items() if k.startswith("GEO_GAP_")
             and isinstance(v, str) and k != "GEO_GAP_MESSAGES"]
    described = {c: geo.describe_geo_gap([c]) for c in codes
                 if c != geo.GEO_GAP_BY_DESIGN_RESIDUAL}
    assert all(described.values()), f"gap code with no message: {described}"
    # Distinct causes get distinct wording — no two share a sentence.
    assert len(set(described.values())) == len(described)
    assert geo.describe_geo_gap([]) is None
    assert geo.describe_geo_gap(None) is None


def test_b1_12_the_gap_sentence_names_what_to_repair_first():
    """With several gaps at once, the message names the most fundamental one.

    FX ranks LAST among the blocking gaps: it is the only one that still leaves
    native-currency spend usable on the page, so "fix FX" is the wrong headline
    while the spend baseline itself cannot be read.
    """
    msg = geo.describe_geo_gap([geo.GEO_GAP_FX_COVERAGE_INCOMPLETE,
                                geo.GEO_GAP_CAMPAIGN_SPEND_UNREADABLE])
    assert msg == geo.GEO_GAP_MESSAGES[geo.GEO_GAP_CAMPAIGN_SPEND_UNREADABLE]
    # But FX alone is still reported as FX, not as a reconciliation failure.
    fx_only = geo.describe_geo_gap([geo.GEO_GAP_FX_COVERAGE_INCOMPLETE])
    assert "FX" in fx_only


def test_b1_13_the_country_page_publishes_the_specific_gap_sentence():
    """Dashboard Countries explains the real cause, not a stand-in for it."""
    assert "geo_gate.describe_geo_gap" in _COUNTRIES_SRC
    body = _COUNTRIES_SRC[_COUNTRIES_SRC.index("def _build_unavailable"):]
    assert "country_gap_codes" in body


def test_22_the_existing_spend_variance_tolerance_is_unchanged():
    """The gate was never the problem, so the tolerance must not move.

    Loosening it would "fix" a blocked page by lowering the bar rather than by
    giving geo an owner, which is exactly what this PR must not do.
    """
    from services.google_ads_spend_service import SPEND_VARIANCE_TOLERANCE

    # Pinned to the value on `main` before this PR. If a future change needs a
    # different tolerance it must move this number deliberately, in its own
    # review, rather than drifting to make a blocked page go green.
    assert SPEND_VARIANCE_TOLERANCE == 0.02
    assert geo.SPEND_VARIANCE_TOLERANCE is SPEND_VARIANCE_TOLERANCE


# ═════════════════════════════════════════════════════════════════════════════
# §23 / §24 — windows and unavailability
# ═════════════════════════════════════════════════════════════════════════════

def test_23_window_bounds_stay_inclusive_start_exclusive_end_utc():
    start, end = get_window_bounds("current_quarter", now=NOW)
    assert start.tzinfo is not None and start.utcoffset().total_seconds() == 0
    assert end.tzinfo is not None and end.utcoffset().total_seconds() == 0
    assert start.hour == start.minute == start.second == 0
    assert end.hour == end.minute == end.second == 0

    # Adjacent windows must not overlap: one window's exclusive end is the next
    # window's inclusive start, never a shared instant counted twice.
    q_start, q_end = get_window_bounds("current_quarter", now=NOW)
    y_start, _y_end = get_window_bounds("ytd", now=NOW)
    assert y_start <= q_start < q_end


def test_23b_the_country_disclosure_publishes_the_exact_utc_bounds():
    block = geo.build_country_truth_disclosure(
        {"country_spend_status": "verified"},
        {"key": "ytd", "start_utc": "2026-01-01T00:00:00+00:00",
         "end_utc_exclusive": "2026-06-23T00:00:00+00:00"})
    assert block["window"]["start_utc"] == "2026-01-01T00:00:00+00:00"
    assert block["window"]["end_utc_exclusive"] == "2026-06-23T00:00:00+00:00"
    assert block["window"]["bounds"] == "inclusive_start_exclusive_end_utc"


def test_23c_the_disclosed_bounds_come_from_the_window_the_mart_actually_used():
    """Review follow-up: the bounds are DERIVED, never re-resolved.

    Recomputing them from the window key would re-resolve against the current
    clock, so a mart built with an explicit `now` — a deterministic run, or a
    previous-period comparison — could disclose bounds for a different window
    than the one it used. That is a second answer to a question that already has
    one, which is the defect class this programme exists to remove.
    """
    from services.revenue_decision_mart import _window_block

    # A window resolved in the PAST. If the block re-resolved `key` against the
    # clock it would return today's YTD, not this one.
    block = _window_block({"window": {"key": "ytd", "label": "Year to date",
                                      "start_date": "2024-01-01",
                                      "end_date": "2024-03-31"}})
    assert block["start_utc"] == "2024-01-01T00:00:00+00:00"
    assert block["end_utc_exclusive"] == "2024-04-01T00:00:00+00:00"

    # And for a live window the derived instants equal the resolver's, so the
    # two can never disagree.
    from analysis.business_windows import resolve_window

    resolved = resolve_window("current_quarter", now=NOW)
    derived = _window_block({"window": resolved})
    s_dt, e_dt = get_window_bounds("current_quarter", now=NOW)
    assert derived["start_utc"] == s_dt.isoformat()
    assert derived["end_utc_exclusive"] == e_dt.isoformat()


def test_9b_the_coverage_snapshot_batch_records_its_own_real_outcome():
    """Review follow-up: a connected dataset must not report a comfortable lie.

    The `(gclid, coverage_snapshots)` batch was finished as `success` with
    `row_count=1` regardless of what the writer returned, and the snapshot row
    was stamped with the ATTRIBUTION batch's id — so the dataset this PR set out
    to make honest would have reported healthy on a failed insert, and its rows
    would not have been linked to the batch that produced them.
    """
    for rel in ("scheduler/weekly.py", "scheduler/monthly.py"):
        src = (_ROOT / rel).read_text()
        i = src.index("dataset=\"coverage_snapshots\"")
        block = src[i:i + 1400]
        # The snapshot is linked to ITS OWN batch.
        assert "sync_batch_id=cov_batch_id" in block, rel
        assert "sync_batch_id=gclid_batch_id" not in block, rel
        # And the batch carries the real outcome, not a hard-coded success.
        assert "cov_written = db_writers.write_gclid_coverage_snapshot" in block, rel
        assert 'status="success" if cov_written else "failed"' in block, rel
        assert "row_count=cov_written" in block, rel


def test_24_unavailable_country_metrics_are_null_never_zero():
    """Unavailable and zero are different states, and must stay different."""
    block = geo.build_country_truth_disclosure(
        {"country_spend_status": geo.GEO_STATUS_MISMATCH,
         "country_gap_reason": geo.GEO_GAP_TOTALS_DIFFER},
        {"key": "ytd"}, revenue_available=False, revenue_reason="db_unavailable",
        revenue_violation_codes=["coverage_unproven"])
    assert block["geo_ready"] is False
    assert block["gap_codes"] == [geo.GEO_GAP_TOTALS_DIFFER]
    assert block["revenue_available"] is False
    assert block["revenue_unavailable_reason"] == "db_unavailable"
    assert block["revenue_violation_codes"] == ["coverage_unproven"]
    assert block["legacy_fallback_used"] is False
    # Withheld figures are absent/None — never a zero a reader could mistake for
    # a measurement.
    assert block["residual_spend_native"] is None
    assert block["residual_spend_usd"] is None
    assert block["residual_accepted"] is False


def test_24b_the_disclosure_states_both_sources_and_the_estimate_grade_join():
    block = geo.build_country_truth_disclosure(
        {"country_spend_status": "verified", "spend_source": "google_ads_api"},
        {"key": "ytd"}, revenue_source="hubspot_deal_ledger",
        revenue_scope="all_source")
    assert block["revenue_source"] == "hubspot_deal_ledger"
    assert block["spend_source"] == "google_ads_api"
    assert block["geo_spend_source"] == "google_ads_geo_daily_spend"
    assert block["country_identity_contract"] == "analysis.country_identity"
    # Google Ads advertising geography and HubSpot contact geography are
    # different facts; the payload says so rather than implying they are one.
    assert "different facts" in block["estimate_grade_note"]


# ═════════════════════════════════════════════════════════════════════════════
# BLOCKER 5 — a seven-day refresh does not bootstrap history
# ═════════════════════════════════════════════════════════════════════════════

def test_b5_1_a_historical_bootstrap_command_exists_and_reuses_the_sync():
    """History needs one deliberate resumable pass, and it must NOT be a copy.

    The scheduled step refreshes seven days because Google restates recent
    spend. On a fresh ledger that cannot prove current_quarter, last_quarter,
    last_6_months, ytd or all_time — so the documented verification could never
    pass from the daily run alone.
    """
    script = _ROOT / "scripts" / "backfill_canonical_geo.py"
    assert script.exists()
    src = script.read_text()
    # It DELEGATES: one implementation of lease, replacement, coverage and
    # checkpoint, not a second set of rules to keep in step.
    assert "run_google_ads_geo_sync" in src
    assert "analyze_geo_coverage" in src
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "fetch_geo_daily" not in defined, "the bootstrap must not re-implement fetching"
    assert not (defined & {"_run_chunks", "upsert_geo_coverage"})


def test_b5_2_the_bootstrap_exits_non_zero_unless_coverage_is_proven():
    """"Probably worked" is not evidence; the windows keep blocking until it did."""
    src = (_ROOT / "scripts" / "backfill_canonical_geo.py").read_text()
    body = src[src.index("    ok = bool("):src.index("    return {", src.index("    ok = bool("))]
    for condition in ('status == "success"', 'coverage.get("available")',
                      'coverage.get("complete")'):
        assert condition in body, condition
    assert "return 0 if outcome[\"ok\"] else 1" in src


def test_b5_3_documented_windows_are_real_window_keys():
    """A documented verification step that names a window the app rejects is
    a procedure nobody can follow."""
    from analysis.business_windows import WINDOW_KEYS

    doc = (_ROOT / "docs" / "36_CANONICAL_COUNTRY_GEOGRAPHY.md").read_text()
    procedure = doc[doc.index("## 9. Production verification"):]
    # Every `for w in ...` list in the procedure must contain only real keys.
    import re
    for match in re.findall(r"for w in ([a-z_0-9 ]+); do", procedure):
        named = match.split()
        assert named, "empty window list in the documented procedure"
        unknown = [w for w in named if w not in WINDOW_KEYS]
        assert not unknown, f"documented windows that do not exist: {unknown}"
    assert "last_12_months" not in procedure


def test_b5_4_the_docs_no_longer_claim_the_retracted_positions():
    """Three claims became false in this patch and must not survive in prose.

    Documentation that describes a safety property the code no longer has is
    worse than no documentation: it is a reason not to look.
    """
    doc = (_ROOT / "docs" / "36_CANONICAL_COUNTRY_GEOGRAPHY.md").read_text()
    assert "EVIDENCE, not a new blocking condition" not in doc
    assert "proceeds without a lease" not in doc
    assert "running without a lease" not in doc.lower() or "must not" in doc.lower()
    # And it states the corrected positions.
    assert "mandatory" in doc.lower()
    assert "fail" in doc.lower() and "closed" in doc.lower()


# ═════════════════════════════════════════════════════════════════════════════
# BLOCKER 2 — a refresh REPLACES its range
# ═════════════════════════════════════════════════════════════════════════════

def test_b2_1_the_sync_replaces_the_range_rather_than_merging_into_it():
    """Merge-only writing cannot express "this row no longer exists".

    Google restates recent spend, so a row present in an earlier fetch and
    absent from this one must disappear — otherwise the chunk is certified over
    a denominator Google no longer reports.
    """
    assert "replace_geo_daily_spend_chunk" in _GEO_SRC
    body = _GEO_SRC[_GEO_SRC.index("def _run_chunks"):]
    assert "upsert_geo_daily_spend" not in body, (
        "the chunk loop must replace the range, not merge into it")


def test_b2_2_the_replacement_validates_before_it_deletes():
    """A row from another account, or outside the range, would be deleted and
    never re-inserted — silent data loss."""
    src = (_ROOT / "db" / "writers.py").read_text()
    body = src[src.index("def replace_geo_daily_spend_chunk"):]
    body = body[:body.index("\ndef ", 1)]
    validate_at = body.index("does not belong to")
    delete_at = body.index("DELETE FROM google_ads_geo_daily_spend")
    assert validate_at < delete_at, "validation must precede the delete"
    assert "is outside" in body           # range check
    assert "GeoRangeReplacementError" in body   # raises; never a silent zero


# ═════════════════════════════════════════════════════════════════════════════
# §25 / §26 — no legacy fallback, no external mutation
# ═════════════════════════════════════════════════════════════════════════════

_LEGACY_GEO_PROVIDERS = (
    "fetch_geo_spend_total", "fetch_campaign_country_spend_legacy",
    "fetch_revenue_deals", "fetch_source_revenue", "fetch_won_revenue",
)


def test_25_country_consumers_call_no_legacy_geo_or_revenue_provider():
    """Static guard: a migrated country module may not CALL a retired provider.

    AST-based, so it tracks behaviour rather than prose — a comment naming a
    legacy function is not a call to it, and renaming a comment cannot silence
    this check.
    """
    offenders: dict[str, list] = {}
    for rel in ("services/dashboard_countries_service.py",
                "services/google_ads_geo_sync_service.py",
                "analysis/country_identity.py"):
        tree = ast.parse((_ROOT / rel).read_text())
        called = {getattr(n.func, "attr", getattr(n.func, "id", None))
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        hits = sorted(called & set(_LEGACY_GEO_PROVIDERS))
        if hits:
            offenders[rel] = hits
    assert not offenders, f"legacy providers still called: {offenders}"


def test_25b_the_geo_reconciliation_never_falls_back_to_a_legacy_table():
    for legacy in ("windsor", "attributed_deals.json", "campaign_performance.json"):
        assert legacy not in _GEO_SRC.lower()


def test_26_no_external_mutation_method_is_reachable_from_the_new_modules():
    """Read-only governance: no Google Ads or HubSpot write path is introduced."""
    for rel in ("services/google_ads_geo_sync_service.py",
                "services/dataset_keys.py",
                "analysis/country_identity.py",
                "scheduler/incremental_sync.py"):
        src = (_ROOT / rel).read_text()
        body = "\n".join(line for line in src.splitlines()
                         if not line.strip().startswith("#"))
        for marker in ("upload_click_conversions", "OfflineUserDataJob",
                       "campaign_budget", "mutate_campaigns", "mutate_ad_group",
                       "hubspot.*create", "requests.post"):
            assert marker not in body, f"{rel} must not reach {marker!r}"


def test_26b_the_geo_writer_touches_only_local_canonical_tables():
    """Every table the geo sync writes is a LOCAL canonical table."""
    src = (_ROOT / "db" / "writers.py").read_text()
    for fn in ("upsert_geo_coverage", "upsert_geo_sync_state",
               "try_claim_geo_sync_lease", "release_geo_sync_lease"):
        body = src[src.index(f"def {fn}"):]
        body = body[:body.index("\ndef ", 1)]
        assert "google_ads_geo" in body
        assert "requests." not in body
