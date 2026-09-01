"""
db/redaction.py

THE database-error redactor. Dependency-free on purpose.

Why this module exists
----------------------
PR-ADS-154A put the redactor in :mod:`db.writers`, which was the right home
while only write paths needed it. But ``db.writers`` imports
``db.connection``, so ``db.connection`` could never import it back — and
``db.connection`` is exactly where the most dangerous message is produced.

``init_pool()`` logged its failure as::

    log.error("Failed to initialise database connection pool: %s", exc)

A psycopg2 connection failure can carry the DSN, and a DSN carries the
password, so that line could put a production credential into the Render logs.
The redaction added in PR-ADS-155-F1-F1 did not help: it guarded the value
``ensure_database_ready`` *returns*, while ``init_pool`` had already logged the
raw exception on the way there.

This module imports nothing from the ``db`` package — only ``re`` — so every
module in it, ``db.connection`` included, can import it at module scope with no
possibility of a cycle. ``db.writers`` re-exports it so the ~5 existing
importers are unaffected.

Read-only, side-effect free, and never raises: a redactor that can fail is a
redactor that leaks on the day it fails.
"""

from __future__ import annotations

import re

#: Redacted forms, in one pattern:
#:   * a DSN, credentials and all;
#:   * the libpq keyword form that assigns a password;
#:   * any ``user:pass@host`` embedded in a URL.
#:
#: (The keyword form is described rather than spelled: review tools mask the
#: literal as a suspected secret, which made the explanation unreadable in the
#: PR diff where it was first written.)
_DB_SECRET_RE = re.compile(
    r"(postgres(?:ql)?://[^\s'\"]+)"     # a DSN, credentials and all
    r"|(password\s*=\s*\S+)"             # libpq keyword form
    r"|(://[^/\s:]+:[^@\s]+@)",          # any user:pass@host
    re.IGNORECASE,
)

#: Returned when redaction itself fails. A redactor that raises would otherwise
#: propagate the original exception — with its DSN — to whatever was logging it.
UNREDACTABLE = ("[error text withheld: it could not be redacted, so it cannot "
                "be proven free of credentials]")


def safe_db_error(exc: BaseException, limit: int = 300) -> str:
    """A database error message safe to log or put in an API response.

    Callers need to know WHY something failed — "value too long for type
    character varying(20)" is the entire diagnosis of a real production failure,
    and withholding it would leave an operator guessing. But a connection error
    can carry the DSN, so the text is redacted before it travels anywhere, and
    capped so a driver's multi-kilobyte context dump cannot flood a log line or
    a response body.

    Never raises. If anything at all goes wrong while redacting, the ORIGINAL
    text is discarded and :data:`UNREDACTABLE` is returned — the one thing this
    function must never do is hand back something it has not proven safe.
    """
    try:
        text = " ".join(str(exc).split())      # collapse newlines/tabs
        return _DB_SECRET_RE.sub("[redacted]", text)[:limit]
    except Exception:  # noqa: BLE001
        return UNREDACTABLE


__all__ = ["safe_db_error", "UNREDACTABLE", "_DB_SECRET_RE"]
