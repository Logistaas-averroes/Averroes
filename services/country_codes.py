"""
services/country_codes.py — DEPRECATED SHIM (PR-ADS-153F)

The country identity contract now lives in ``analysis.country_identity``. This
module remains only so pre-153F call sites keep importing successfully; it holds
no table and no logic of its own.

Why it was emptied rather than kept:

  * ``_COUNTRY_CODES`` (name -> code) and ``_CODE_TO_NAME`` (code -> name) were
    two hand-maintained tables that had already drifted — eleven codes (SG, MY,
    ID, TH, VN, PH, AU, NZ, LK, ZA, NG) resolved forward but had no name, so a
    country could appear on one page and be nameless on another.
  * ``get_country_code`` uppercased ANY two-letter token and returned it as a
    country code, so ``"XX"`` was as valid as ``"AE"``.

Both directions are now derived from one registry
(``analysis.country_identity.SUPPORTED_COUNTRIES``), which makes that class of
drift unrepresentable.

New code must import ``analysis.country_identity`` directly and group on
``country_key`` — not on a name, and not on a code it validated itself.

Removal of this shim (and of the legacy country readers that still import it)
belongs to PR-ADS-153G, not here.
"""

from __future__ import annotations

from analysis.country_identity import (  # noqa: F401
    SUPPORTED_COUNTRIES,
    country_name_for_code,
    get_country_code,
)

__all__ = ["get_country_code", "country_name_for_code", "SUPPORTED_COUNTRIES"]
