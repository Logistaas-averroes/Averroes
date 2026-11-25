"""PR-ADS-179A — build a shared input once per certification window, inside
one database snapshot.

The certification audits build every page a window publishes (the Revenue
Decision Mart's views, the Dashboard tabs, Pipeline, the Meta table …), and
most of those pages rebuild the SAME inputs: the revenue-attribution core
(``revenue_attribution_service._build_from_db``) and the window's won-deal
population (``canonical_revenue_service.load_won_deals``). On 25 Nov 2026
that made ``reader_cutover`` 477 s and ``marketing_outcome_cohorts`` 430 s
of a 900 s per-audit limit.

Caching those inputs across pages is only sound if every page would have
read the same database: a core built from one snapshot joined to a page's
own reads from another is a payload no request ever served (``docs/59``
§11.5). So the cache lives INSIDE one snapshot and dies with it:

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
  never consulted — the memo cannot outlive or cross a snapshot.

Read-only. It adds no gate and decides nothing; it changes how often an
input is built, never what any check sees.
"""
from __future__ import annotations

import contextlib
import copy
import functools
from collections import Counter
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from decimal import Decimal

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


@contextlib.contextmanager
def certification_window():
    """ONE snapshot, ONE closed-won universe and ONE memo for the block.

    Yields the :class:`MemoScope`, or None when no database is configured or
    the snapshot could not be opened — the block then runs exactly as it
    would without this (every read on its own, nothing cached), and each
    builder reports its own unavailability as before.
    """
    from db.connection import pinned_read_snapshot  # noqa: PLC0415
    from services.canonical_revenue_service import one_snapshot  # noqa: PLC0415

    stack = contextlib.ExitStack()
    with stack:
        try:
            pin = stack.enter_context(pinned_read_snapshot())
        except Exception:  # noqa: BLE001 — unpinned is the pre-179A behaviour
            pin = None
        if pin is None:
            # No shared universe either: without one database snapshot, a
            # universe shared across pages would join reads of two instants.
            yield None
            return
        stack.enter_context(one_snapshot())
        scope = MemoScope(pin)
        token = _SCOPE.set(scope)
        try:
            yield scope
        finally:
            _SCOPE.reset(token)
