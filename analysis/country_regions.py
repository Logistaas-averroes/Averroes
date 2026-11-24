"""
analysis/country_regions.py

PR-ADS-176B — the ONE country-code → region table (presentation grouping).

It lived in ``services/dashboard_countries_service.py`` (PR-ADS-138) and is
moved here, unchanged, so the Countries page (the mart country view) groups
markets by region with the same table as Dashboard → Countries. It adds no
business math: a region is a presentation lane, and an unresolved or unknown
code lands in a clearly named "Unknown / Needs Review" lane, never guessed.
Mexico is grouped with LATAM (commercial grouping); North-African markets are
grouped with Africa.
"""
from __future__ import annotations

REGION_NORTH_AMERICA = "North America"
REGION_EUROPE = "Europe"
REGION_MIDDLE_EAST = "Middle East"
REGION_LATAM = "LATAM"
REGION_AFRICA = "Africa"
REGION_APAC = "APAC"
REGION_UNKNOWN = "Unknown / Needs Review"

REGION_ORDER = [
    REGION_NORTH_AMERICA, REGION_EUROPE, REGION_MIDDLE_EAST,
    REGION_LATAM, REGION_AFRICA, REGION_APAC, REGION_UNKNOWN,
]

COUNTRY_REGION = {
    # North America
    "US": REGION_NORTH_AMERICA, "CA": REGION_NORTH_AMERICA,
    # LATAM (Mexico grouped commercially with Latin America)
    "MX": REGION_LATAM, "BR": REGION_LATAM, "AR": REGION_LATAM, "CL": REGION_LATAM,
    "CO": REGION_LATAM, "PE": REGION_LATAM, "EC": REGION_LATAM, "VE": REGION_LATAM,
    "UY": REGION_LATAM, "PY": REGION_LATAM, "BO": REGION_LATAM, "PA": REGION_LATAM,
    "CR": REGION_LATAM, "GT": REGION_LATAM, "DO": REGION_LATAM,
    # Europe
    "GB": REGION_EUROPE, "IE": REGION_EUROPE, "FR": REGION_EUROPE, "DE": REGION_EUROPE,
    "ES": REGION_EUROPE, "PT": REGION_EUROPE, "IT": REGION_EUROPE, "NL": REGION_EUROPE,
    "BE": REGION_EUROPE, "LU": REGION_EUROPE, "CH": REGION_EUROPE, "AT": REGION_EUROPE,
    "PL": REGION_EUROPE, "CZ": REGION_EUROPE, "SK": REGION_EUROPE, "HU": REGION_EUROPE,
    "RO": REGION_EUROPE, "BG": REGION_EUROPE, "GR": REGION_EUROPE, "CY": REGION_EUROPE,
    "MT": REGION_EUROPE, "SE": REGION_EUROPE, "NO": REGION_EUROPE, "DK": REGION_EUROPE,
    "FI": REGION_EUROPE, "IS": REGION_EUROPE, "EE": REGION_EUROPE, "LV": REGION_EUROPE,
    "LT": REGION_EUROPE, "UA": REGION_EUROPE, "RU": REGION_EUROPE, "HR": REGION_EUROPE,
    "SI": REGION_EUROPE, "RS": REGION_EUROPE,
    # Middle East (Levant + Gulf + Turkey / Iran / Israel)
    "SA": REGION_MIDDLE_EAST, "AE": REGION_MIDDLE_EAST, "QA": REGION_MIDDLE_EAST,
    "KW": REGION_MIDDLE_EAST, "BH": REGION_MIDDLE_EAST, "OM": REGION_MIDDLE_EAST,
    "JO": REGION_MIDDLE_EAST, "LB": REGION_MIDDLE_EAST, "IQ": REGION_MIDDLE_EAST,
    "YE": REGION_MIDDLE_EAST, "SY": REGION_MIDDLE_EAST, "PS": REGION_MIDDLE_EAST,
    "IL": REGION_MIDDLE_EAST, "IR": REGION_MIDDLE_EAST, "TR": REGION_MIDDLE_EAST,
    # Africa (incl. North Africa)
    "EG": REGION_AFRICA, "MA": REGION_AFRICA, "DZ": REGION_AFRICA, "TN": REGION_AFRICA,
    "LY": REGION_AFRICA, "SD": REGION_AFRICA, "ZA": REGION_AFRICA, "NG": REGION_AFRICA,
    "KE": REGION_AFRICA, "GH": REGION_AFRICA, "ET": REGION_AFRICA, "TZ": REGION_AFRICA,
    "UG": REGION_AFRICA,
    # APAC
    "CN": REGION_APAC, "HK": REGION_APAC, "TW": REGION_APAC, "JP": REGION_APAC,
    "KR": REGION_APAC, "IN": REGION_APAC, "PK": REGION_APAC, "BD": REGION_APAC,
    "LK": REGION_APAC, "SG": REGION_APAC, "MY": REGION_APAC, "ID": REGION_APAC,
    "TH": REGION_APAC, "VN": REGION_APAC, "PH": REGION_APAC, "AU": REGION_APAC,
    "NZ": REGION_APAC,
}


def region_for_code(code) -> str:
    if not code:
        return REGION_UNKNOWN
    return COUNTRY_REGION.get(str(code).strip().upper(), REGION_UNKNOWN)
