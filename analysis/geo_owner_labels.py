"""
analysis/geo_owner_labels.py

PR-ADS-170A. The owner's explicit country labels for Google Ads geo criteria
that Google itself reports with NO country.

Production holds spend against criteria ``2192`` and ``2364``; Google's
geo_target_constant lookup returns no country for either. Nothing in this
system infers one — not from the id, not from Google's "2000 + ISO numeric"
convention. On 2026-11-22 the owner decided: ``2192`` is Cuba, ``2364`` is
Iran. That decision is DATA (``config/geo_owner_labels.yaml``), one entry per
criterion, each carrying who approved it and when. An id that is not listed is
not labelled.

This module only LOADS and VALIDATES the table. The rule that applies it —
a stored Google code always wins — is ``db.geo_identity_sql.criterion_identity``.

Validation. An entry is kept only when:

* its key is a Google criterion id (digits);
* ``code`` is an officially assigned ISO 3166-1 alpha-2 code
  (``analysis.iso_countries.ISO_COUNTRY_NAMES``);
* ``approved`` is a calendar date and ``by`` is non-empty — a label nobody is
  on record as approving is not an owner label.

Anything else is ignored with a logged warning. A missing or unreadable file
means "no labels". Loading never raises: a config typo must not take a page
down, and the fail-closed result of dropping a label is the pre-170A
behaviour ("(unresolved location)").

Pure: no database, no network.
"""

from __future__ import annotations

import functools
import logging
from datetime import date, datetime
from pathlib import Path

from analysis.iso_countries import ISO_COUNTRY_NAMES

log = logging.getLogger(__name__)

#: The config file. A module attribute so a test can point it elsewhere.
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "geo_owner_labels.yaml"
CONFIG_KEY = "owner_labelled_geo_criteria"

#: Provenance of a criterion's country.
PROVENANCE_GOOGLE_ADS = "google_ads"
PROVENANCE_OWNER_LABELLED = "owner_labelled"

#: What the pages say beside an owner-labelled country.
OWNER_LABEL_NOTE = ("Country set by owner — Google Ads reports this location "
                    "without a country")


def _approved_date(value) -> str | None:
    """ISO date string for a YAML date or a ``YYYY-MM-DD`` string, else None."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (TypeError, ValueError):
        return None


def validate_owner_labels(raw) -> tuple:
    """``(labels, rejected)`` from the raw config mapping.

    ``labels`` is ``{criterion_id: {code, name, approved, by}}`` for the valid
    entries; ``rejected`` is ``[(key, why)]`` for the rest. Never raises.
    """
    labels: dict = {}
    rejected: list = []
    seen: set = set()
    if raw is None:
        return labels, rejected
    if not isinstance(raw, dict):
        return labels, [(CONFIG_KEY, "not a mapping of criterion id to label")]
    for key, entry in raw.items():
        criterion = str(key if key is not None else "").strip()
        if not criterion.isdigit():
            rejected.append((repr(key), "criterion id is not a number"))
            continue
        if criterion in seen:
            # "2192" and 2192 are one criterion; two entries are a contradiction
            # nothing here settles. Neither is applied.
            rejected.append((criterion, "listed twice"))
            labels.pop(criterion, None)
            continue
        seen.add(criterion)
        if not isinstance(entry, dict):
            rejected.append((criterion, "entry is not a mapping with code / approved / by"))
            continue
        code = entry.get("code")
        # YAML reads a bare `NO` as False: only a string is a code.
        code = code.strip().upper() if isinstance(code, str) else None
        if not code or code not in ISO_COUNTRY_NAMES:
            rejected.append((criterion, f"code {entry.get('code')!r} is not an "
                                        "ISO 3166-1 alpha-2 country code"))
            continue
        approved = _approved_date(entry.get("approved"))
        if not approved:
            rejected.append((criterion, f"approved {entry.get('approved')!r} is not a date"))
            continue
        by = entry.get("by")
        by = by.strip() if isinstance(by, str) else ""
        if not by:
            rejected.append((criterion, "no approver ('by')"))
            continue
        labels[criterion] = {"code": code, "name": ISO_COUNTRY_NAMES[code],
                             "approved": approved, "by": by}
    return labels, rejected


@functools.lru_cache(maxsize=8)
def _load(path: str) -> dict:
    try:
        import yaml  # noqa: PLC0415
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        raw = doc.get(CONFIG_KEY) if isinstance(doc, dict) else None
        if not isinstance(doc, dict):
            log.warning("[geo-owner-labels] %s is not a mapping; no criterion is "
                        "owner-labelled", path)
    except Exception as exc:  # noqa: BLE001 — missing / unreadable: no labels
        log.warning("[geo-owner-labels] %s could not be read (%s); no criterion "
                    "is owner-labelled", path, exc)
        return {}
    labels, rejected = validate_owner_labels(raw)
    for key, why in rejected:
        log.warning("[geo-owner-labels] entry %s ignored: %s", key, why)
    return labels


def owner_labels() -> dict:
    """``{criterion_id: {code, name, approved, by}}`` — the valid owner labels.

    Read once per process per path. Never raises; ``{}`` when there are none.
    """
    return dict(_load(str(CONFIG_PATH)))


__all__ = [
    "CONFIG_KEY", "CONFIG_PATH", "OWNER_LABEL_NOTE", "PROVENANCE_GOOGLE_ADS",
    "PROVENANCE_OWNER_LABELLED", "owner_labels", "validate_owner_labels",
]
