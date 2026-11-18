"""PR-ADS-163B — one statement whose failure costs only itself.

PostgreSQL aborts a transaction on its first error: every later statement on
that connection then fails with "current transaction is aborted, commands
ignored until end of transaction block", whatever it asked. Code that runs
several independent diagnostic reads on one cursor and catches each one's
exception therefore does NOT isolate them — the first failure silently fails
all the rest.

That is what production did. One freshness registry entry named a column its
table does not have; its row count failed, and with it the row count of every
dataset registered after it and the latest-batch read for all of them, on
every request to the status endpoints.

This module is deliberately not part of ``db.connection``: tests replace that
module wholesale, and a replaced helper would silently do nothing.
"""
from __future__ import annotations

SAVEPOINT = "isolated_read"


def execute_isolated(cur, query, params=None) -> None:
    """Execute ``query`` inside a savepoint; on error roll back to it, re-raise.

    The transaction stays usable for the caller's next read. The error is
    re-raised: the caller still decides what an unreadable value means, and it
    must mean unknown — never zero.

    The savepoint is not released on success: a RELEASE would discard the
    result set the caller is about to fetch. Re-using the name stacks
    savepoints, which all end with the transaction.
    """
    cur.execute(f"SAVEPOINT {SAVEPOINT}")
    try:
        cur.execute(query, params)
    except Exception:
        cur.execute(f"ROLLBACK TO SAVEPOINT {SAVEPOINT}")
        raise
