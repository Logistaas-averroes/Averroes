"""PR-ADS-179A — build a shared input once per certification window, inside
one database snapshot.

The certification audits build every page a window publishes (the Revenue
Decision Mart's views, the Dashboard tabs, Pipeline, the Meta table …), and
most of those pages rebuild the SAME inputs: the revenue-attribution core
(``revenue_attribution_service._build_from_db``) and the window's won-deal
population (``canonical_revenue_service.load_won_deals``). On 25 Nov 2026
that made ``reader_cutover`` 477 s and ``marketing_outcome_cohorts`` 430 s
of a 900 s per-audit limit.

Caching those inputs across pages is only sound if every page reads the
same database: a core built from one snapshot joined to a page's own reads
from another is a combination of reads nothing ever produced (``docs/59``
§11.5). So the cache lives INSIDE one snapshot and dies with it. What the
audits then certify is a snapshot-consistent build of each page — not the
statement-by-statement READ COMMITTED reads of a web request, whose torn
reads (a sync committing mid-request) certification cannot see
(``docs/62`` §3.3):

* :func:`certification_window` opens ONE ``REPEATABLE READ, READ ONLY``
  transaction that every read in the block shares
  (``db.connection.pinned_read_snapshot``), one closed-won universe for the
  block (``canonical_revenue_service.one_snapshot``, which the page builders'
  ``@single_snapshot`` joins), and a memo scope bound to that transaction;
* :func:`memoised` caches a function's result in the active scope, keyed by
  its arguments, and returns a deep copy to every caller — the value a fresh
  call would have returned, since within one snapshot a read is a pure
  function of its arguments;
* outside a certification window (every web request, every sync) nothing is
  cached and nothing changes;
* a call whose arguments carry no explicit ``now`` is never cached: its
  result would depend on the wall clock, not only on the snapshot;
* a scope whose snapshot is not the active one (or no snapshot at all) is
  never consulted — the memo cannot outlive or cross a snapshot;
* PR-ADS-179B: a window that could not pin a snapshot runs unpinned (every
  read on its own, as before 179A) and SAYS so: ``certification_window``
  fills the caller's ``disclosure``, :func:`window_snapshot` turns it into the
  per-window ``snapshot`` entry of an audit's report, and
  :func:`pinning_summary` into the report's ``snapshot_pinning`` note. It
  changes no exit code.

Read-only. It adds no gate and decides nothing; it changes how often an
input is built, never what any check sees.
"""
from __future__ import annotations

import contextlib
import copy
import functools
import logging
from collections import Counter
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from decimal import Decimal

log = logging.getLogger(__name__)

_SCOPE: ContextVar = ContextVar("certification_memo_scope", default=None)

_ATOMS = (str, int, float, bool, bytes, type(None), date, datetime, time,
          timedelta, Decimal)


class _Unkeyable(Exception):
    pass


def _freeze(value):
    """A hashable, order-independent key for ``value``; raises ``_Unkeyable``
    for anything it cannot key exactly (the call is then not cached)."""
    if isinstance(value, _ATOMS):
        return (type(value).__name__, value)
    if isinstance(value, dict):
        return ("dict", tuple(sorted(((_freeze(k), _freeze(v)) for k, v in value.items()),
                                     key=repr)))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(_freeze(v) for v in value))
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted((_freeze(v) for v in value), key=repr)))
    raise _Unkeyable(type(value).__name__)


class MemoScope:
    """The memo of ONE pinned read snapshot. ``stats`` counts, per function,
    ``built`` (the function ran) and ``reused`` (a copy was returned)."""

    def __init__(self, pin):
        self.pin = pin
        self.values: dict = {}
        self.stats: Counter = Counter()

    def valid(self) -> bool:
        from db.connection import current_read_snapshot  # noqa: PLC0415
        return self.pin is not None and current_read_snapshot() is self.pin


def current_scope() -> MemoScope | None:
    return _SCOPE.get()


def memoised(fn):
    """Cache ``fn`` inside the active certification window (see the module
    docstring). Elsewhere the wrapper is a plain call. ``fn`` must take a
    ``now`` argument: a call without an explicit one is never cached."""
    import inspect  # noqa: PLC0415

    name = f"{fn.__module__}.{fn.__qualname__}"
    signature = inspect.signature(fn)
    if "now" not in signature.parameters:
        raise TypeError(f"{name} takes no `now`; its result is not a function of "
                        "the snapshot and its arguments alone")

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        scope = _SCOPE.get()
        if scope is None or not scope.valid():
            return fn(*args, **kwargs)
        try:
            # The call as the function sees it — positional or keyword,
            # defaults filled in — so two spellings of one call share a key.
            bound = signature.bind(*args, **kwargs)
        except TypeError:
            return fn(*args, **kwargs)      # let the function raise its own error
        bound.apply_defaults()
        if bound.arguments.get("now") is None:
            scope.stats[name + ":wall_clock"] += 1
            return fn(*args, **kwargs)
        try:
            key = (name, _freeze(dict(bound.arguments)))
        except _Unkeyable:
            scope.stats[name + ":unkeyed"] += 1
            return fn(*args, **kwargs)
        if key in scope.values:
            scope.stats[name + ":reused"] += 1
            return copy.deepcopy(scope.values[key])
        value = fn(*args, **kwargs)
        scope.values[key] = copy.deepcopy(value)
        scope.stats[name + ":built"] += 1
        return value

    wrapper.__memoised__ = fn
    return wrapper


#: PR-ADS-179B — why a certification window ran unpinned.
UNPINNED_SCHEMA_LOCK = "schema_lock_wait_exceeded"
UNPINNED_OPEN_FAILED = "snapshot_open_failed"
UNPINNED_NO_DATABASE = "no_database"
#: The window recorded nothing (it was not opened by ``certification_window``):
#: never read as pinned.
UNPINNED_NOT_RECORDED = "not_recorded"


def _unpinned_because(exc: BaseException) -> tuple[str, str]:
    from db.connection import WINDOW_SCHEMA_LOCK_WAIT_SECONDS  # noqa: PLC0415
    try:
        from psycopg2.errors import LockNotAvailable  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        LockNotAvailable = ()  # noqa: N806
    if LockNotAvailable and isinstance(exc, LockNotAvailable):
        return UNPINNED_SCHEMA_LOCK, (
            f"the shared schema lock was not granted within "
            f"{WINDOW_SCHEMA_LOCK_WAIT_SECONDS}s (a migration held it): this "
            f"window read every statement on its own, nothing cached")
    return UNPINNED_OPEN_FAILED, (
        f"the pinned snapshot could not be opened ({type(exc).__name__}): this "
        f"window read every statement on its own, nothing cached")


@contextlib.contextmanager
def certification_window(disclosure: dict | None = None):
    """ONE snapshot, ONE closed-won universe and ONE memo for the block.

    Yields the :class:`MemoScope`, or None when no database is configured or
    the snapshot could not be opened — the block then runs exactly as it
    would without this (every read on its own, nothing cached), and each
    builder reports its own unavailability as before.

    PR-ADS-179B: ``disclosure``, when given, is filled with whether the window
    is pinned and, if not, why (``pinned`` / ``reason`` / ``detail``) — an
    audit reports it per window (:func:`window_snapshot`).
    """
    from db.connection import pinned_read_snapshot  # noqa: PLC0415
    from services.canonical_revenue_service import one_snapshot  # noqa: PLC0415

    record = disclosure if disclosure is not None else {}
    record.clear()
    record.update(pinned=False, reason=UNPINNED_NO_DATABASE,
                  detail="no database is configured: no snapshot was opened")
    stack = contextlib.ExitStack()
    with stack:
        try:
            pin = stack.enter_context(pinned_read_snapshot())
        except Exception as exc:  # noqa: BLE001 — unpinned is the pre-179A behaviour
            # E.g. a migration held the schema lock past the window's bound
            # (``db.connection.WINDOW_SCHEMA_LOCK_WAIT_SECONDS``).
            log.warning("certification window runs unpinned (every read on its own, "
                        "nothing cached): the snapshot could not be opened: %s",
                        type(exc).__name__)
            reason, detail = _unpinned_because(exc)
            record.update(reason=reason, detail=detail)
            pin = None
        if pin is None:
            # No shared universe either: without one database snapshot, a
            # universe shared across pages would join reads of two instants.
            yield None
            return
        stack.enter_context(one_snapshot())
        scope = MemoScope(pin)
        record.update(pinned=True, reason=None, detail=None)
        token = _SCOPE.set(scope)
        try:
            yield scope
        finally:
            _SCOPE.reset(token)


def refused_reads(scope: MemoScope | None) -> list:
    """Every ``PinnedSnapshotError`` raised inside the window ``scope`` ran in
    — recorded on its snapshot as it was raised, so a reader that caught and
    swallowed it cannot hide it (PR-ADS-179A-F1). An audit reports a window
    with any as ``cannot_check``: what it read is not proven to be the
    snapshot. Empty for an unpinned window (None)."""
    pin = getattr(scope, "pin", None)
    return list(getattr(pin, "violations", None) or [])


def window_snapshot(scope: MemoScope | None, disclosure: dict | None) -> dict:
    """PR-ADS-179B — the ``snapshot`` entry an audit reports for one window:
    ``{"pinned", "reason", "detail"}``. Pinned only when the window's scope
    really carries a pin — the disclosure supplies the reason, never the
    verdict — so a window opened some other way reads unpinned
    (``not_recorded``), never pinned by default."""
    if scope is not None and getattr(scope, "pin", None) is not None:
        return {"pinned": True, "reason": None, "detail": None}
    d = disclosure or {}
    if d.get("pinned") is False and d.get("reason"):
        return {"pinned": False, "reason": d["reason"], "detail": d.get("detail")}
    return {"pinned": False, "reason": UNPINNED_NOT_RECORDED,
            "detail": "the window recorded no snapshot: it is not proven pinned"}


def pinning_summary(windows) -> dict:
    """PR-ADS-179B — the report-level note over ``windows`` (an iterable of
    ``(name, snapshot entry)``). An unpinned window does not change an exit
    code: it is the pre-179A behaviour, not a failed check. But what it
    certified is NOT a snapshot-consistent build, and the note says so."""
    entries = list(windows)
    unpinned = [{"window": name, "reason": snap.get("reason"),
                 "detail": snap.get("detail")}
                for name, snap in entries if snap.get("pinned") is not True]
    pinned = len(entries) - len(unpinned)
    if not entries:
        note = "no window was read"
    elif not unpinned:
        note = (f"every window ({pinned}) was read inside one pinned REPEATABLE "
                f"READ, READ ONLY snapshot: a snapshot-consistent build")
    else:
        note = (f"{len(unpinned)} of {len(entries)} window(s) ran UNPINNED "
                f"({', '.join(u['window'] for u in unpinned)}): each read there ran "
                f"in its own READ COMMITTED transaction, as before PR-ADS-179A, so "
                f"what was certified there is NOT a snapshot-consistent build. The "
                f"exit code is unchanged by this.")
    return {"all_pinned": bool(entries) and not unpinned, "windows": len(entries),
            "pinned": pinned, "unpinned": unpinned, "note": note}
