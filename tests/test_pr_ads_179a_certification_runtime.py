"""PR-ADS-179A — the certification audits build each window once, in one
snapshot.

``reader_cutover``, ``marketing_outcome_cohorts`` and ``campaigns`` build every
page a window publishes, and those pages rebuild the same inputs: the mart
core (``revenue_attribution_service._build_from_db``), the won-deal
population (``canonical_revenue_service.load_won_deals``) and the contact
funnel (``canonical_crm_funnel_service.build`` /
``canonical_contact_outcome_service.build``). Each window now runs inside
``services.snapshot_memo.certification_window``: ONE ``REPEATABLE READ, READ
ONLY`` transaction for every read (``db.connection.pinned_read_snapshot``) and
a memo of those inputs that lives and dies with it (``docs/62``).

What these cases prove, on PostgreSQL through the real builders:

1. inside a window, a shared input is built once and every later caller gets
   an independent copy of the same value; outside one, nothing is cached;
2. the memo never crosses windows: data committed between two windows is
   seen by the second;
3. a window IS one snapshot: data committed by another session while a window
   is open is not seen inside it, by any reader;
4. the pinned view keeps each reader's own transaction contract (its
   REPEATABLE READ / READ ONLY request is satisfied, any other mode raises, a
   failed statement rolls back to the reader's savepoint, writes are refused);
5. every audit still builds its whole payload inventory for every window,
   each window inside its own snapshot — nothing dropped to save time;
6. every audit's full report, and every page payload it builds, are
   identical with and without the windows;
7. with no snapshot (no database, an exhausted pool) a window changes nothing.

Every case fails on ``ea6a7ae``, where ``services.snapshot_memo`` and the
pinned snapshot do not exist. Each case also fails under the mutation that
``docs/62`` §6 names for it.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_pr_ads_165a_countries_pg_integration import (  # noqa: E402,F401
    CUSTOMER, live_db,
)
from tests.test_pr_ads_176a_country_builder_pg_integration import WINDOW  # noqa: E402
from tests.test_pr_ads_177a_one_sql_count_pg_integration import (  # noqa: E402
    _ready, _yesterday_at,
)

CORE = "services.revenue_attribution_service._build_from_db"
WON = "services.canonical_revenue_service.load_won_deals"
FUNNEL = "services.canonical_crm_funnel_service.build"
TIMING_KEYS = {"generated_at"}


def _json(payload):
    return json.loads(json.dumps(payload, default=str))


def _strip(node):
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in TIMING_KEYS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _mart(view="campaign", window=WINDOW, now=None, **kw):
    from services.revenue_decision_mart import build_revenue_decision_mart
    return _json(build_revenue_decision_mart(view=view, window=window, now=now, **kw))


def _add_sql_contact(contact_id: str) -> None:
    """One more Google Ads SQL on ``brand - uk``, created yesterday (inside
    every window the fixture's cohort is counted in)."""
    from db import writers
    y = _yesterday_at(11)
    out = writers.upsert_hubspot_contact_funnel([{
        "contact_id": contact_id, "created_at": y, "last_modified_at": y,
        "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "brand - uk",
        "lifecycle_stage": "salesqualifiedlead", "date_entered_sql": y,
        "ip_country": "United Kingdom"}])
    assert out["ok"] and out["persisted"] == 1, out


def _in_another_session(fn, *args) -> None:
    """Run ``fn`` on another thread: a new context, so its ``get_conn()`` is
    an ordinary pooled connection — another session — even while this thread
    holds a pinned snapshot."""
    import threading
    errors = []

    def run():
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    t = threading.Thread(target=run)
    t.start()
    t.join(60)
    assert not t.is_alive() and not errors, errors


def _count_contacts_via_reader() -> int:
    from db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM hubspot_contact_funnel")
        return cur.fetchone()[0]


@pytest.fixture()
def ready(live_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    _ready(live_db)
    return live_db


# ═════════════════════════════════════════════════════════════════════════════
# 1 · inside a window: built once, copies out; outside: nothing cached
# ═════════════════════════════════════════════════════════════════════════════
def test_01_a_window_builds_a_shared_input_once_and_serves_the_same_payload(ready):
    """Four pages over one window: the through-today campaign view twice, the
    settled campaign view (the Campaigns page) and Dashboard → Campaigns, which
    rebuilds the settled core. Inside a window each core is built ONCE per
    distinct input; every payload equals the one built with no window."""
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    from services.snapshot_memo import certification_window
    now = datetime.now(tz=timezone.utc)
    plain = [_mart(now=now), _mart(now=now), _mart(now=now, settled=True),
             _json(build_dashboard_campaigns(window=WINDOW, now=now))]
    with certification_window() as scope:
        assert scope is not None, "the fixture database must pin a snapshot"
        memo = [_mart(now=now), _mart(now=now), _mart(now=now, settled=True),
                _json(build_dashboard_campaigns(window=WINDOW, now=now))]
        stats = dict(scope.stats)
        distinct = sum(1 for key in scope.values if key[0] == CORE)
    assert [_strip(p) for p in memo] == [_strip(p) for p in plain]
    # One build per DISTINCT core input (the through-today view, the settled
    # views and their previous periods) — never one per page.
    assert stats[CORE + ":built"] == distinct, stats
    assert stats[CORE + ":reused"] >= 2, stats
    assert stats[WON + ":reused"] >= 1, stats


def test_01_b_a_reused_value_is_an_independent_copy(ready):
    from services import canonical_revenue_service as crs
    from services.snapshot_memo import certification_window
    now = datetime.now(tz=timezone.utc)
    with certification_window() as scope:
        first = crs.load_won_deals(WINDOW, now=now)
        pristine = json.dumps(first, default=str, sort_keys=True)
        first["deals"].clear()
        first["publication"] = "tampered"
        second = crs.load_won_deals(WINDOW, now=now)
        second["deals"].append({"deal_id": "fabricated"})
        third = crs.load_won_deals(WINDOW, now=now)
        assert scope.stats[WON + ":reused"] == 2
    assert json.dumps(second, default=str, sort_keys=True) != pristine
    assert json.dumps(third, default=str, sort_keys=True) == pristine


def test_01_c_outside_a_window_and_without_now_nothing_is_cached(ready, monkeypatch):
    """Every web request: no window, so every call builds. Inside a window, a
    call with no explicit ``now`` reads the wall clock and is not cached."""
    from db import revenue_repository as repo
    from services.snapshot_memo import certification_window
    reads = []
    real = repo.fetch_campaign_country_spend
    monkeypatch.setattr(repo, "fetch_campaign_country_spend",
                        lambda *a, **k: reads.append(a) or real(*a, **k))
    now = datetime.now(tz=timezone.utc)
    _mart(now=now)
    _mart(now=now)
    assert len(reads) == 2, "outside a certification window every call builds"
    reads.clear()
    with certification_window() as scope:
        _mart(now=None)
        _mart(now=None)
        assert len(reads) == 2, "a wall-clock call is never served from the memo"
        assert scope.stats[CORE + ":wall_clock"] == 2
        assert scope.stats[CORE + ":built"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# 2 · the memo never crosses windows
# ═════════════════════════════════════════════════════════════════════════════
def test_02_data_committed_between_two_windows_is_seen_by_the_second(ready):
    from services.snapshot_memo import certification_window
    now = datetime.now(tz=timezone.utc)
    with certification_window():
        before = _mart(now=now)["summary"]["sqls"]
        assert _mart(now=now)["summary"]["sqls"] == before          # memo hit
    _add_sql_contact("c-between-windows")
    with certification_window() as scope:
        after = _mart(now=now)
        assert scope.stats[CORE + ":reused"] == 0
        assert scope.stats[CORE + ":built"] >= 1
    assert after["summary"]["sqls"] == before + 1
    assert _mart(now=now)["summary"]["sqls"] == before + 1           # no window


# ═════════════════════════════════════════════════════════════════════════════
# 3 · a window is ONE snapshot
# ═════════════════════════════════════════════════════════════════════════════
def test_03_a_commit_by_another_session_during_a_window_is_not_seen_inside_it(ready):
    """Another session commits a new SQL contact while the window is open. No
    reader inside the window sees it — not a direct read, not the audit's
    own independent SQL, not a core built AFTER the commit for an input the
    memo has never seen — so every payload of the window describes one
    instant. The next window sees it."""
    from scripts.audit_marketing_outcome_cohorts import independent_counts
    from services.marketing_outcome_cohort_service import window_instants
    from services.snapshot_memo import certification_window
    now = datetime.now(tz=timezone.utc)
    y = _yesterday_at().date()
    instants = window_instants(y, y)
    rows_before = _count_contacts_via_reader()
    with certification_window():
        campaign = _mart(now=now)
        independent = independent_counts(*instants)
        _in_another_session(_add_sql_contact, "c-during-window")
        assert _count_contacts_via_reader() == rows_before
        assert independent_counts(*instants) == independent
        country = _mart(view="country", now=now)  # a core first built now
        settled = _mart(now=now, settled=True)
    assert country["cohort"]["sql_status"] == "published"
    assert country["summary"]["sqls"] == campaign["summary"]["sqls"]
    assert settled["summary"]["sqls"] == campaign["summary"]["sqls"]
    assert _count_contacts_via_reader() == rows_before + 1
    with certification_window():
        assert independent_counts(*instants)["sqls"] == independent["sqls"] + 1
        assert _mart(view="country", now=now)["summary"]["sqls"] == \
            country["summary"]["sqls"] + 1


# ═════════════════════════════════════════════════════════════════════════════
# 4 · the pinned view keeps each reader's own transaction contract
# ═════════════════════════════════════════════════════════════════════════════
def test_04_the_pinned_view_satisfies_refuses_and_isolates_like_a_transaction(ready):
    import psycopg2
    from db.connection import (
        PinnedSnapshotError, current_read_snapshot, get_conn, pinned_read_snapshot,
    )
    with pinned_read_snapshot() as pin:
        assert current_read_snapshot() is pin is not None
        with get_conn() as conn, conn.cursor() as cur:
            # A reader's own snapshot request is already satisfied.
            for stmt in ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
                         "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                         "set transaction read only"):
                cur.execute(stmt)
            cur.execute("SELECT current_setting('transaction_isolation'), "
                        "current_setting('transaction_read_only')")
            assert cur.fetchone() == ("repeatable read", "on")
        # Anything weaker or writable is refused, never silently granted.
        for stmt in ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
                     "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
                     "SET TRANSACTION READ WRITE", "BEGIN", "COMMIT", "ROLLBACK"):
            with pytest.raises(PinnedSnapshotError):
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(stmt)
        # A write fails in the database (READ ONLY) and rolls back to the
        # reader's savepoint: the next reader in the window still reads.
        with pytest.raises(psycopg2.Error):
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM hubspot_contact_funnel")
        n = _count_contacts_via_reader()
        assert n > 0
        # A reader's own commit / rollback keeps the window's snapshot.
        with get_conn() as conn:
            conn.commit()
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM hubspot_contact_funnel")
                assert cur.fetchone()[0] == n
        assert current_read_snapshot() is pin
    assert current_read_snapshot() is None


# ═════════════════════════════════════════════════════════════════════════════
# 5 · nothing dropped: every payload, every window, each in its own snapshot
# ═════════════════════════════════════════════════════════════════════════════
#: Every page builder each audit runs for every business window, pinned here
#: as a SECOND copy of each audit's own list. A payload removed from an audit
#: to save time fails this inventory.
MARKETING_PAYLOADS = {"mart/campaign", "mart/country", "mart/campaign_settled",
                      "dashboard/overview", "dashboard/campaigns",
                      "dashboard/countries", "dashboard/revenue", "dashboard/deals",
                      "pipeline"}
READER_PAGES = {"revenue_decision_mart", "revenue_decision_mart_country",
                "revenue_attribution", "revenue_by_source", "dashboard_overview",
                "dashboard_revenue", "dashboard_channels", "dashboard_campaigns",
                "dashboard_countries", "dashboard_deals", "pipeline",
                "revenue_decision_mart_campaign_settled", "meta_ads_adsets"}
READER_EVIDENCE_PAGES = {"channel_outcomes"}
#: Campaigns: the builder calls ``audit_window`` makes, by (builder, view).
CAMPAIGN_BUILDS = {("build_revenue_decision_mart", "campaign", True),
                   ("build_dashboard_campaigns", None, None),
                   ("build_dashboard_overview", None, None),
                   ("build_revenue_decision_mart", "campaign", False),
                   ("campaign_deal_details_by_campaign", None, True)}


def _pin_id():
    """The serial of the snapshot this call runs in (unique per process)."""
    from db.connection import current_read_snapshot
    pin = current_read_snapshot()
    return pin.serial if pin is not None else None


def test_05_a_the_cohort_audit_builds_every_payload_of_every_window_in_its_own_snapshot(
        ready, monkeypatch):
    from analysis.business_windows import WINDOW_KEYS
    from scripts import audit_marketing_outcome_cohorts as audit
    calls = []
    real = audit._build_mart_payloads

    def spy(window, now):
        out = real(window, now)
        calls.append((window, frozenset(k for k, v in out.items() if v is not None),
                      _pin_id()))
        return out
    monkeypatch.setattr(audit, "_build_mart_payloads", spy)
    a, report = audit.run(datetime.now(tz=timezone.utc))
    assert [c[0] for c in calls] == list(WINDOW_KEYS)
    for window, built, pin in calls:
        assert built == MARKETING_PAYLOADS, (window, MARKETING_PAYLOADS - built)
        assert pin is not None, f"{window} was not built inside a pinned snapshot"
    assert len({c[2] for c in calls}) == len(WINDOW_KEYS), "one snapshot per window"
    assert [set(w["surfaces"]) for w in report["business_windows"]] == \
        [MARKETING_PAYLOADS] * len(WINDOW_KEYS)
    assert [w["window"] for w in report["windows"]] == list(WINDOW_KEYS)
    assert a.unavailable == [] and a.violations == [], (a.unavailable, a.violations)


def test_05_b_the_reader_cutover_audit_builds_every_page_of_every_window(ready, monkeypatch):
    from analysis.business_windows import WINDOW_KEYS
    from scripts import audit_customer_revenue_reader_cutover as audit
    seen = []
    real_b, real_e = audit._page_builders, audit._evidence_page_builders

    def wrap(builders, kind):
        def wrapped():
            return tuple((name, (lambda w, n, _b=b, _name=name: (
                seen.append((_name, f"{kind}/{w}", _pin_id())), _b(w, n))[1]))
                for name, b in builders())
        return wrapped
    monkeypatch.setattr(audit, "_page_builders", wrap(real_b, "business"))
    monkeypatch.setattr(audit, "_evidence_page_builders", wrap(real_e, "evidence"))
    _, report = audit.run(datetime.now(tz=timezone.utc))
    assert report["pages_not_run"] == [], report["pages_not_run"]
    for key in WINDOW_KEYS:
        assert {n for n, w, _ in seen if w == f"business/{key}"} == READER_PAGES, key
    for key in audit.EVIDENCE_PASS_WINDOWS:
        assert {n for n, w, _ in seen if w == f"evidence/{key}"} == \
            READER_EVIDENCE_PAGES, key
    assert all(pin is not None for _, _, pin in seen), "a page built outside a snapshot"
    by_window = {}
    for name, w, pin in seen:
        by_window.setdefault(w, set()).add(pin)
    assert all(len(p) == 1 for p in by_window.values()), "one snapshot per window"
    assert len({next(iter(p)) for p in by_window.values()}) == len(by_window), \
        "a snapshot shared by two windows"
    assert [w["window"] for w in report["windows"]] == list(WINDOW_KEYS)
    assert report["exit_code"] == 0, report.get("violations")


def test_05_c_the_campaigns_audit_builds_every_surface_of_every_window(ready, monkeypatch):
    from analysis.business_windows import WINDOW_KEYS
    from scripts import audit_campaigns_page_certification as audit
    import services.dashboard_campaigns_service as dcs
    import services.dashboard_overview_service as dos
    import services.revenue_attribution_service as ras
    import services.revenue_decision_mart as rdm
    seen = []

    def spy(mod, attr, describe):
        real = getattr(mod, attr)

        def wrapped(*a, **k):
            seen.append((describe(a, k), k.get("window", a[0] if a else None), _pin_id()))
            return real(*a, **k)
        monkeypatch.setattr(mod, attr, wrapped)
    spy(rdm, "build_revenue_decision_mart",
        lambda a, k: ("build_revenue_decision_mart", k.get("view"), bool(k.get("settled"))))
    spy(dcs, "build_dashboard_campaigns", lambda a, k: ("build_dashboard_campaigns", None, None))
    spy(dos, "build_dashboard_overview", lambda a, k: ("build_dashboard_overview", None, None))
    spy(ras, "campaign_deal_details_by_campaign",
        lambda a, k: ("campaign_deal_details_by_campaign", None, k.get("settled_days")))
    f, per_window = audit.run(tuple(WINDOW_KEYS))
    top = [(d, w, p) for d, w, p in seen]
    for key in WINDOW_KEYS:
        assert {d for d, w, _ in top if w == key} >= CAMPAIGN_BUILDS, key
        assert per_window[key]["available"] is True, per_window[key]
    pins = {}
    for _, w, p in top:
        assert p is not None, f"{w}: a surface built outside a snapshot"
        pins.setdefault(w, set()).add(p)
    assert all(len(v) == 1 for v in pins.values())
    assert len({next(iter(pins[k])) for k in WINDOW_KEYS}) == len(WINDOW_KEYS)
    assert f.exit_code == 0, (f.violations, f.unavailable)


# ═════════════════════════════════════════════════════════════════════════════
# 6 · equivalence: the whole report, with and without the windows
# ═════════════════════════════════════════════════════════════════════════════
@contextmanager
def _no_windows():
    """The pre-179A behaviour: every read on its own, nothing cached."""
    import contextlib
    import services.snapshot_memo as sm
    real = sm.certification_window
    sm.certification_window = lambda: contextlib.nullcontext(None)
    try:
        yield
    finally:
        sm.certification_window = real


def _run_all(now):
    from analysis.business_windows import WINDOW_KEYS
    from scripts import audit_campaigns_page_certification as camp
    from scripts import audit_customer_revenue_reader_cutover as reader
    from scripts import audit_marketing_outcome_cohorts as cohorts
    a, rep = cohorts.run(now)
    out = {"cohorts": {**rep, "violations": a.violations,
                       "unavailable": a.unavailable, "checks": a.checks}}
    _, out["reader_cutover"] = reader.run(now)
    f, per_window = camp.run(tuple(WINDOW_KEYS), now=now)
    out["campaigns"] = {"violations": f.violations, "unavailable": f.unavailable,
                        "checks": f.checks, "per_window": per_window}
    return _strip(_json(out))


#: Every page builder the three audits call (each audit imports it from its
#: module at call time, so patching the module attribute records it).
PAGE_BUILDERS = (
    ("services.revenue_decision_mart", "build_revenue_decision_mart"),
    ("services.revenue_attribution_service", "build_revenue_attribution"),
    ("services.revenue_attribution_service", "campaign_deal_details_by_campaign"),
    ("services.dashboard_overview_service", "build_dashboard_overview"),
    ("services.dashboard_campaigns_service", "build_dashboard_campaigns"),
    ("services.dashboard_countries_service", "build_dashboard_countries"),
    ("services.dashboard_revenue_service", "build_dashboard_revenue"),
    ("services.dashboard_deals_service", "build_dashboard_deals"),
    ("services.dashboard_channels_service", "build_dashboard_channels"),
    ("services.pipeline_service", "build_pipeline"),
    ("services.source_attribution_service", "build_revenue_by_source"),
    ("services.source_attribution_service", "build_channel_outcomes"),
    ("services.meta_ads_adset_table_service", "build_meta_adset_table"),
)


def test_06_every_audit_reports_and_builds_exactly_the_same_with_and_without_the_windows(
        ready, monkeypatch):
    """The whole report of every audit AND every page payload each audit
    builds — by builder and arguments, in call order — are identical with the
    windows and without them (the pre-179A behaviour). A report alone would
    not see a memo key that served one window's population as its previous
    period's: the audits check conservation and withholding, which hold for
    either."""
    import importlib
    # A deal in the PREVIOUS quarter, so a current / previous period mix-up
    # changes a payload.
    from tests.test_pr_ads_153e_b_pg_integration import _insert
    from analysis.business_windows import get_window_bounds
    lq_start, _ = get_window_bounds("last_quarter")
    _insert(ready, "d-last-quarter", close_date=(lq_start + timedelta(days=20)).isoformat(),
            amount=700.0, revenue_usd=700.0, country="United Kingdom",
            gclid="g-lq", campaign="brand - uk", group="google_ads",
            attribution_status="google_ads")
    log: list = []
    for module, attr in PAGE_BUILDERS:
        mod = importlib.import_module(module)
        real = getattr(mod, attr)

        def recorded(*a, _real=real, _attr=attr, **k):
            out = _real(*a, **k)
            log.append((_attr, repr(a), repr(sorted(k.items())),
                        json.dumps(_strip(_json(out)), sort_keys=True)))
            return out
        monkeypatch.setattr(mod, attr, recorded)
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    with _no_windows():
        plain = _run_all(now)
    plain_pages, log[:] = list(log), []
    windowed = _run_all(now)
    windowed_pages = list(log)
    for name in plain:
        assert windowed[name] == plain[name], name
    assert len(windowed_pages) == len(plain_pages)
    for got, want in zip(windowed_pages, plain_pages):
        assert got == want, got[:3]
    # Not a vacuous comparison: real, published figures, every builder.
    assert {p[0] for p in plain_pages} == {attr for _, attr in PAGE_BUILDERS}
    assert plain["cohorts"]["violations"] == [] and len(plain["cohorts"]["checks"]) > 50
    assert any(w.get("published") for w in plain["cohorts"]["windows"])
    assert plain["reader_cutover"]["exit_code"] == 0
    assert len(plain["campaigns"]["checks"]) > 30


# ═════════════════════════════════════════════════════════════════════════════
# 7 · no snapshot, no memo, no shared universe (no database needed)
# ═════════════════════════════════════════════════════════════════════════════
def test_07_without_a_pinned_snapshot_a_window_changes_nothing(monkeypatch):
    """No database, or a snapshot that could not be opened (an exhausted
    pool): the window yields None and the block runs exactly as before —
    every call builds, and no closed-won universe is shared across pages
    (without one snapshot it would join reads of two instants)."""
    import db.connection as connection
    import services.snapshot_memo as sm
    from services import canonical_revenue_service as crs

    calls = []

    @sm.memoised
    def reader(window, *, now=None):
        calls.append(window)
        return {"window": window}

    with pytest.raises(TypeError):
        sm.memoised(lambda window: window)            # no `now`: never cacheable

    now = datetime.now(tz=timezone.utc)
    monkeypatch.setattr(connection, "_pool", None)
    with sm.certification_window() as scope:
        assert scope is None and crs._SNAPSHOT.get() is None
        reader("ytd", now=now)
        reader("ytd", now=now)
    assert calls == ["ytd", "ytd"]

    @contextmanager
    def exhausted():
        raise RuntimeError("connection pool exhausted")
        yield  # pragma: no cover
    monkeypatch.setattr(connection, "pinned_read_snapshot", exhausted)
    with sm.certification_window() as scope:
        assert scope is None and crs._SNAPSHOT.get() is None
        reader("ytd", now=now)
    assert calls == ["ytd", "ytd", "ytd"]
