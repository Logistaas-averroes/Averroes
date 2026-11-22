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

PR-ADS-170A — the owner label. Google reports some criteria with no country at
all (production: ``2192``, ``2364``). The owner may label such a criterion in
``config/geo_owner_labels.yaml``; ``criterion_identity`` is the ONE place the
label is applied, for every READER:

* stored codes are judged first, exactly as above. A stored Google code ALWAYS
  wins — one code resolves to it, two that disagree resolve to nothing;
* only a criterion with NO stored code takes its owner label, and says so
  (``provenance == "owner_labelled"``);
* a labelled criterion whose stored code(s) differ from the label is a
  MISMATCH: the stored code is used, the mismatch is logged and returned, and
  nothing is merged;
* an id that is not listed is never labelled. Nothing is derived from the id.

The label is never written to ``google_ads_geo_daily_spend``: the writer and
the sync keep using ``resolved_code`` (stored codes only), so the table stays
what Google sent and a code Google supplies later is stored and wins.
"""

import logging

log = logging.getLogger(__name__)

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
    """The criterion's STORED code when exactly one is on record, else None.

    Stored evidence only — the owner label is not consulted. The writer and the
    sync use this, so a label is never written into the table. A READER uses
    ``criterion_identity``.
    """
    clean = sorted({str(c).strip().upper() for c in (codes or []) if str(c or "").strip()})
    return clean[0] if len(clean) == 1 else None


_MISMATCH_LOGGED: set = set()


def criterion_identity(criterion_id, codes, name=None, labels=None) -> dict:
    """The country one Google criterion resolves to, and on whose word.

    ``codes`` / ``name`` are what ``criterion_identity_sql`` read for it;
    ``labels`` is the owner-label table (default: the configured one).

    Returns ``{criterion_id, code, name, provenance, stored_codes, owner_label,
    owner_label_mismatch}``:

    * ``code`` — the stored code when exactly one is on record; else the owner
      label's code when NO code is stored and the criterion is listed; else
      None;
    * ``provenance`` — ``"google_ads"``, ``"owner_labelled"`` or None;
    * ``owner_label`` — the criterion's label ``{code, name, approved, by}``
      whether or not it was applied, else None;
    * ``owner_label_mismatch`` — None, or ``{criterion_id, owner_code,
      stored_codes}`` when stored codes exist and are not exactly the label's.
    """
    from analysis import geo_owner_labels  # noqa: PLC0415

    criterion = str(criterion_id if criterion_id is not None else "").strip()
    stored = sorted({str(c).strip().upper() for c in (codes or [])
                     if str(c or "").strip()})
    if labels is None:
        labels = geo_owner_labels.owner_labels()
    label = (labels or {}).get(criterion) if criterion else None
    out = {"criterion_id": criterion, "code": None, "name": None,
           "provenance": None, "stored_codes": stored,
           "owner_label": dict(label) if label else None,
           "owner_label_mismatch": None}
    if stored:
        if len(stored) == 1:
            out.update(code=stored[0], name=name,
                       provenance=geo_owner_labels.PROVENANCE_GOOGLE_ADS)
        if label and stored != [label["code"]]:
            out["owner_label_mismatch"] = {
                "criterion_id": criterion, "owner_code": label["code"],
                "stored_codes": stored}
            seen = (criterion, tuple(stored), label["code"])
            if seen not in _MISMATCH_LOGGED:
                _MISMATCH_LOGGED.add(seen)
                log.warning(
                    "[geo-identity] criterion %s is owner-labelled %s but Google "
                    "Ads rows on record carry %s; the stored code is used and "
                    "the owner label is NOT applied", criterion, label["code"],
                    ", ".join(stored))
        return out
    if label:
        out.update(code=label["code"], name=label["name"],
                   provenance=geo_owner_labels.PROVENANCE_OWNER_LABELLED)
    return out


def owner_label_summary(identities) -> dict:
    """The owner-label disclosure for a set of ``criterion_identity`` results.

    ``{criteria: [{criterion_id, country_code, country, approved, by}],
    mismatches: [{criterion_id, owner_code, stored_codes}]}`` — ``criteria``
    are the ones whose country IS the owner's label; ``mismatches`` the ones
    where Google's stored code overrode a different label.
    """
    criteria, mismatches = {}, {}
    for ident in identities or []:
        cid = ident.get("criterion_id")
        if ident.get("provenance") == "owner_labelled" and ident.get("owner_label"):
            label = ident["owner_label"]
            criteria[cid] = {"criterion_id": cid, "country_code": label["code"],
                             "country": label["name"],
                             "approved": label["approved"], "by": label["by"]}
        if ident.get("owner_label_mismatch"):
            mismatches[cid] = dict(ident["owner_label_mismatch"])
    return {"criteria": [criteria[k] for k in sorted(criteria)],
            "mismatches": [mismatches[k] for k in sorted(mismatches)]}


__all__ = ["criterion_identity", "criterion_identity_sql",
           "owner_label_summary", "resolved_code"]
