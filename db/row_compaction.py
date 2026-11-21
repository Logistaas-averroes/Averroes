"""PR-ADS-169A — read rows as dicts without one string object per cell.

psycopg2 builds a NEW ``str`` for every text cell, so a 21,847-row read of
``leads`` holds 21,847 copies of ``"qualified"``, ``"paid_search"``,
``"PAID_SEARCH"`` and every campaign name. Those copies were most of the
memory the in-app daily certification spent on contact reads, and it is what
put the 512Mi web instance over its limit (``docs/09`` PR-ADS-169A).

:func:`rows_as_dicts` returns exactly what ``dict(zip(cols, row))`` returns —
the same keys, the same values, equal by ``==`` — except that equal ``str``
cells share one object. Only exact ``str`` values are shared: a ``str`` is
immutable and equal strings are interchangeable, so nothing a reader can do
with a value changes. Datetimes are NOT shared (two equal instants may carry
different offsets and print differently), nor is any other type.

No I/O of its own. A real psycopg2 cursor is read with ``fetchmany`` in
chunks of :data:`CHUNK_ROWS` (the same rows, in the same order); any other
cursor object with ``fetchall`` as before.
"""
from __future__ import annotations


def share_equal_strings(rows, seen: dict | None = None):
    """Yield each row (a sequence) as a list whose equal ``str`` cells are one
    object. ``seen`` may be passed to share strings across several reads."""
    seen = {} if seen is None else seen
    for row in rows:
        out = []
        for v in row:
            if type(v) is str:  # noqa: E721 — exact type: never a subclass
                v = seen.setdefault(v, v)
            out.append(v)
        yield out


#: Rows converted per ``fetchmany`` call: the un-shared strings psycopg2 makes
#: exist for one chunk at a time instead of for the whole result.
CHUNK_ROWS = 2000


def _raw_rows(cur):
    """The cursor's remaining rows. A real psycopg2 cursor is read in chunks;
    anything else (a test double) through ``fetchall`` exactly as before."""
    try:
        import psycopg2.extensions  # noqa: PLC0415
        real = isinstance(cur, psycopg2.extensions.cursor)
    except Exception:  # noqa: BLE001
        real = False
    if not real:
        yield from cur.fetchall()
        return
    while True:
        chunk = cur.fetchmany(CHUNK_ROWS)
        if not chunk:
            return
        yield from chunk


def rows_as_dicts(cur, seen: dict | None = None) -> list[dict]:
    """``[dict(zip(cols, row)) for row in cur.fetchall()]``, strings shared."""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row))
            for row in share_equal_strings(_raw_rows(cur), seen)]
