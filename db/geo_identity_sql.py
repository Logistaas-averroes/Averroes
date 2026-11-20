"""
db/geo_identity_sql.py

PR-ADS-167A. ONE statement of "which country does a Google Ads geo criterion
carry on record", shared by every reader and by the writer that must not lose
it.

A geo row's ``country_code`` is filled at sync time from a lookup that can
fail. The criterion id is Google's own identifier for the location, so a code
ANY stored row of the same criterion carries is stored evidence about that same
location. The rule, everywhere:

* every DISTINCT non-blank code on record for the criterion, over the WHOLE
  table (every account, every date) — upper-cased and trimmed;
* exactly one code resolves; none, or two that disagree, do not. Nothing is
  picked from a disagreement and nothing is inferred (not even Google's
  2000 + ISO-numeric id convention);
* the name is the one on the newest row that carries a code.

``criterion_identity_sql(criteria)`` returns the statement for the criteria
named by the SQL array expression ``criteria`` (``"%s"`` for a bound array, or a
subquery so the identity is read in the SAME statement — one snapshot — as the
rows it names). It yields ``(country_criterion_id, codes, name)`` for each
criterion that has ANY row (``codes`` may be NULL). Read-only.
"""

_CRITERION_IDENTITY_TEMPLATE = """
    SELECT country_criterion_id,
           ARRAY_AGG(DISTINCT UPPER(BTRIM(country_code)))
               FILTER (WHERE NULLIF(BTRIM(country_code), '') IS NOT NULL)
               AS codes,
           (ARRAY_AGG(country_name ORDER BY spend_date DESC)
               FILTER (WHERE NULLIF(BTRIM(country_code), '') IS NOT NULL
                         AND NULLIF(BTRIM(country_name), '') IS NOT NULL))[1]
               AS name
    FROM google_ads_geo_daily_spend
    WHERE country_criterion_id = ANY({criteria})
    GROUP BY country_criterion_id
"""


def criterion_identity_sql(criteria: str = "%s") -> str:
    """The identity statement over the criteria the array expression names."""
    return _CRITERION_IDENTITY_TEMPLATE.format(criteria=criteria)


def resolved_code(codes) -> str | None:
    """The criterion's code when exactly one is on record, else None."""
    clean = sorted({str(c).strip().upper() for c in (codes or []) if str(c or "").strip()})
    return clean[0] if len(clean) == 1 else None


__all__ = ["criterion_identity_sql", "resolved_code"]
