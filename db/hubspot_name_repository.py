"""
db/hubspot_name_repository.py
PR-ADS-166A — read-side queries for the name loads.

The queue of contacts whose names are due, the load's progress, the queue of
companies whose name is due, and the stored company names. Read-only; every
queue is bounded by a SQL ``LIMIT`` so no whole table is loaded into memory.

Three states per record, never merged:

  never asked      no read of this id has been answered
  not returned     a read was ANSWERED and HubSpot did not return this id
  read             HubSpot returned it (with a name, or with none)
"""
from __future__ import annotations

import logging

from db.connection import get_conn

log = logging.getLogger(__name__)

FUNNEL_TABLE = "hubspot_contact_funnel"


def fetch_contacts_due_for_names(limit: int, *, reask_before) -> dict:
    """Ids of stored contacts whose names are due, in this order:

    1. never asked — newest contact first;
    2. asked and NOT returned by HubSpot, last asked before ``reask_before``
       — longest-waiting first.

    The queue is ``names_read_at IS NULL``, NOT ``first_name IS NULL``: a
    contact HubSpot holds no name for has been read and must not be read again
    for ever. A not-returned contact is asked again only after
    ``reask_before``, so a batch of stuck ids cannot hold the front of the
    queue. ``LIMIT`` is applied in SQL.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "contact_ids": []}
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT contact_id FROM {FUNNEL_TABLE}
                         WHERE names_read_at IS NULL
                           AND (names_last_asked_at IS NULL
                                OR names_last_asked_at < %s)
                         ORDER BY (names_last_asked_at IS NOT NULL),
                                  names_last_asked_at ASC,
                                  created_at DESC NULLS LAST, contact_id DESC
                         LIMIT %s""", (reask_before, max(0, int(limit))))
                ids = [str(r[0]) for r in cur.fetchall()]
        return {"available": True, "contact_ids": ids}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_contacts_due_for_names failed: %s", exc)
        return {"available": False, "contact_ids": []}


def fetch_contact_name_progress() -> dict:
    """``read`` / ``not_returned`` / ``never_asked`` / ``total`` stored contacts.

    ``available: False`` carries ``None`` counts — unknown is not zero.
    """
    empty = {"available": False, "read": None, "not_returned": None,
             "never_asked": None, "total": None}
    try:
        with get_conn() as conn:
            if conn is None:
                return empty
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT COUNT(*) FILTER (WHERE names_read_at IS NOT NULL),
                               COUNT(*) FILTER (WHERE names_read_at IS NULL
                                                AND names_last_asked_at IS NOT NULL),
                               COUNT(*) FILTER (WHERE names_read_at IS NULL
                                                AND names_last_asked_at IS NULL),
                               COUNT(*) FROM {FUNNEL_TABLE}""")
                read, not_returned, never_asked, total = cur.fetchone()
        return {"available": True, "read": int(read),
                "not_returned": int(not_returned),
                "never_asked": int(never_asked), "total": int(total)}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_contact_name_progress failed: %s", exc)
        return empty


def fetch_company_ids_needing_names(limit: int, *, reread_before) -> dict:
    """Company ids on a deal→company association whose name is due:

    1. never asked (no row);
    2. then rows whose last answer — a read, or an ask HubSpot did not return
       the id for — is older than ``reread_before``, oldest first.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "company_ids": []}
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT a.company_id
                         FROM (SELECT DISTINCT company_id
                                 FROM hubspot_deal_company_association) a
                         LEFT JOIN hubspot_company_name n
                                ON n.company_id = a.company_id
                        WHERE n.company_id IS NULL
                           OR GREATEST(n.read_at, n.last_asked_at) < %s
                        ORDER BY (n.company_id IS NOT NULL),
                                 GREATEST(n.read_at, n.last_asked_at) ASC,
                                 a.company_id ASC
                        LIMIT %s""", (reread_before, max(0, int(limit))))
                ids = [str(r[0]) for r in cur.fetchall()]
        return {"available": True, "company_ids": ids}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_company_ids_needing_names failed: %s", exc)
        return {"available": False, "company_ids": []}


def fetch_company_names(company_ids) -> dict:
    """``{company_id: {"name", "read"}}`` for the ids a read was ANSWERED for.

    ``read`` False means HubSpot was asked and did not return the company.
    An id absent from ``names`` was never asked. ``available: False`` means
    the store could not be read — the caller must not say "not loaded" then.
    """
    ids = sorted({str(c) for c in company_ids or [] if str(c or "").strip()})
    if not ids:
        return {"available": True, "names": {}}
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "names": {}}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT company_id, name, read_at FROM hubspot_company_name "
                    "WHERE company_id = ANY(%s)", (ids,))
                names = {str(r[0]): {"name": r[1], "read": r[2] is not None}
                         for r in cur.fetchall()}
        return {"available": True, "names": names}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_company_names failed: %s", exc)
        return {"available": False, "names": {}}
