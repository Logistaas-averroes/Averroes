"""
analysis/search_term_scope.py

PR-ADS-156-F3 §1 — THE definition of the canonical search-term population.

What went wrong
---------------
PR-ADS-156-F2 added ``customer_id`` to the search-term natural key, and the
ingestion side did its job: the first production sync fetched and wrote 16,267
rows with zero rejections and zero missing identities.

But the older rows in the same window carry ``customer_id IS NULL``. Under the
NEW key those are different rows, so the complete rows did not conflict with
them, did not supersede them, and both populations now sit in the table. In the
certified window production held 32,367 rows, of which 16,100 had no account —
almost exactly one stale twin per new row.

That is not a display problem. Every reader that queried ``search_terms`` with
only a date window counted both copies: raw Search Terms, the summary, N-grams,
the evidence repositories, the Dashboard Campaign signals. A metric built that
way is not wrong by a rounding error; it is close to doubled.

The rule
--------
A row belongs to the canonical population only when it PROVES all four:

1. ``source_system = 'google_ads_api'`` — the canonical provenance;
2. a non-empty account identity;
3. that identity equals the effective configured Google Ads customer;
4. complete campaign / ad-group / search-term identity.

Everything else — Windsor-era rows, unknown provenance, rows from another
account, rows predating the account column — is HISTORY. It may be disclosed
diagnostically and must never contribute spend, clicks, impressions,
conversions, row counts, n-grams, waste counts or recommendations.

Fail closed
-----------
When the effective Google Ads customer cannot be resolved this module returns
UNAVAILABLE. It does not fall back to querying every account, and it does not
quietly include null-account rows. There is exactly one configured account
today, which makes "just take everything" look harmless — and that is the trap:
the moment a second account exists, every historical total silently changes
meaning, and nothing in the code would mark the day it happened.

For the same reason no account id is ever INVENTED for a historical row. A row
that does not say which account it describes does not become this account's row
because this account is the only one configured.

One definition
--------------
Every repository, endpoint, service and audit composes its predicate from here.
Letting each reproduce "roughly the same" SQL is how they end up disagreeing —
and a disagreement between two totals on two pages is discovered by a person
noticing, which is the slowest detector available.

Pure and read-only: this module builds SQL text and parameters. It opens no
connection and executes nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

#: The one provenance value a canonical search-term row may carry. Identical to
#: the value the writer stamps and the evidence service quarantines against.
CANONICAL_PROVENANCE = "google_ads_api"

#: Why a scope could not be built. Returned rather than raised so a reader can
#: answer "unavailable" with a reason instead of failing or, worse, widening.
REASON_CUSTOMER_NOT_CONFIGURED = "google_ads_customer_not_configured"

#: The documented durable natural key, spelled once (PR-ADS-156-F3 §4). Every
#: contract string and provenance payload quotes THIS, so a reader is never told
#: the account-less key is canonical.
SEARCH_TERMS_NATURAL_KEY = (
    "source_date + COALESCE(customer_id,'') + COALESCE(campaign_name,'') + "
    "COALESCE(campaign_id,'') + COALESCE(ad_group,'') + COALESCE(keyword,'') + "
    "COALESCE(match_type,'') + search_term "
    "(UNIQUE index idx_search_terms_unique_fact; writer upserts ON CONFLICT)"
)

#: The natural-key components other than the account, in the order the index
#: declares them. Shared with the writer's twin supersession so the two cannot
#: describe different keys.
NATURAL_KEY_NON_ACCOUNT_COLUMNS = (
    "source_date", "campaign_name", "campaign_id", "ad_group", "keyword",
    "match_type", "search_term",
)


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def configured_customer_id() -> str | None:
    """The configured Google Ads customer account, or None.

    Read from the environment directly, with no SDK import and no API call, so
    this resolves identically in a web process, a scheduler run and a standalone
    ``python -m`` command. Tests patch this symbol.
    """
    return (os.getenv("GOOGLE_ADS_CUSTOMER_ID") or "").strip() or None


def customer_id_candidates(configured: str | None = None) -> list[str]:
    """Every EXACT spelling of the configured account a stored row may carry.

    Google Ads writes customer ids both as ``1234567890`` and ``123-456-7890``,
    and which form reaches the column depends on how the environment variable
    was typed on the day the row was written. Matching only the current spelling
    would silently exclude the account's own history the first time someone
    reformats the variable — a total that quietly halves, with nothing to say
    why.

    These are still EXACT identities: a fixed, tiny candidate set, no wildcard,
    no pattern, and never NULL. A row belonging to a different account cannot
    match any of them.
    """
    cid = (configured if configured is not None else configured_customer_id())
    cid = (cid or "").strip()
    if not cid:
        return []
    digits = _digits(cid)
    forms = {cid}
    if digits:
        forms.add(digits)
        if len(digits) == 10:
            forms.add(f"{digits[:3]}-{digits[3:6]}-{digits[6:]}")
    return sorted(forms)


@dataclass(frozen=True)
class SearchTermScope:
    """A composable SQL predicate over ``search_terms``.

    ``available`` False means no canonical population can be identified at all;
    a caller must report unavailable rather than run a wider query.
    """

    available: bool
    sql: str = "FALSE"
    params: tuple = ()
    customer_id: str | None = None
    customer_id_candidates: tuple = ()
    reason: str | None = None
    #: Column alias the predicate is written against ("" for an unaliased table).
    alias: str = ""
    natural_key: str = field(default=SEARCH_TERMS_NATURAL_KEY, repr=False)

    def and_(self, *conditions: str) -> str:
        """This scope AND the caller's own conditions, as one WHERE body.

        The caller supplies its own parameters separately and in order —
        ``scope.params + tuple(extra_params)`` — because the scope always comes
        first in the composed predicate.
        """
        parts = [self.sql, *[c for c in conditions if c]]
        return " AND ".join(f"({p})" for p in parts)


def _col(alias: str, name: str) -> str:
    return f"{alias}.{name}" if alias else name


def canonical_scope(start: date | None = None, end: date | None = None, *,
                    alias: str = "", configured: str | None = None) -> SearchTermScope:
    """Build the canonical search-term predicate for ``[start, end]``.

    ``start=None`` means no lower bound (all-time); ``end=None`` means no upper
    bound. Both bounds are on ``source_date`` — the Google Ads reporting date —
    never ``run_date``, because scheduler timing must not move business totals.

    Returns an UNAVAILABLE scope when the configured account cannot be resolved.
    """
    candidates = customer_id_candidates(configured)
    if not candidates:
        return SearchTermScope(
            available=False, sql="FALSE", params=(),
            reason=REASON_CUSTOMER_NOT_CONFIGURED, alias=alias)

    conditions = [
        f"{_col(alias, 'source_system')} = %s",
        # `= ANY(...)` over a fixed candidate list. NULL never matches, so a row
        # with no account is excluded by identity rather than by an extra clause
        # someone could later drop.
        f"{_col(alias, 'customer_id')} = ANY(%s)",
        f"{_col(alias, 'campaign_id')} IS NOT NULL",
        f"TRIM({_col(alias, 'campaign_id')}) <> ''",
        f"{_col(alias, 'ad_group')} IS NOT NULL",
        f"TRIM({_col(alias, 'ad_group')}) <> ''",
        f"{_col(alias, 'search_term')} IS NOT NULL",
        f"TRIM({_col(alias, 'search_term')}) <> ''",
    ]
    params: list = [CANONICAL_PROVENANCE, candidates]

    if start is not None:
        conditions.append(f"{_col(alias, 'source_date')} >= %s")
        params.append(start)
    if end is not None:
        conditions.append(f"{_col(alias, 'source_date')} <= %s")
        params.append(end)

    return SearchTermScope(
        available=True,
        sql=" AND ".join(conditions),
        params=tuple(params),
        customer_id=candidates[0] if len(candidates) == 1 else (
            configured if configured is not None else configured_customer_id()),
        customer_id_candidates=tuple(candidates),
        alias=alias,
    )


def unscoped_history_scope(start: date | None = None, end: date | None = None, *,
                           alias: str = "",
                           configured: str | None = None) -> SearchTermScope:
    """The complement: rows in the window that are NOT canonical.

    Reported as disclosure — counted and labelled, never added to a total, never
    relabelled canonical, and never repaired or deleted by a reader.
    """
    scope = canonical_scope(start, end, alias=alias, configured=configured)
    if not scope.available:
        return scope

    window = []
    window_params: list = []
    if start is not None:
        window.append(f"{_col(alias, 'source_date')} >= %s")
        window_params.append(start)
    if end is not None:
        window.append(f"{_col(alias, 'source_date')} <= %s")
        window_params.append(end)

    # NOT(scope) alone would be enough, but the window has to be re-stated: the
    # complement of a windowed predicate includes every row outside the window,
    # and a disclosure about "this interval" must stay inside it.
    sql = " AND ".join([*window, f"NOT ({scope.sql})"]) if window else f"NOT ({scope.sql})"
    return SearchTermScope(
        available=True, sql=sql,
        params=(*window_params, *scope.params),
        customer_id=scope.customer_id,
        customer_id_candidates=scope.customer_id_candidates,
        alias=alias,
    )


__all__ = [
    "CANONICAL_PROVENANCE", "REASON_CUSTOMER_NOT_CONFIGURED",
    "SEARCH_TERMS_NATURAL_KEY", "NATURAL_KEY_NON_ACCOUNT_COLUMNS",
    "SearchTermScope", "configured_customer_id", "customer_id_candidates",
    "canonical_scope", "unscoped_history_scope",
]
