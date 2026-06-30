"""
Country name -> ISO 3166-1 alpha-2 lookup (PR-ADS-107A)

Small, dependency-free helper used by the revenue attribution service to attach
a best-effort ``country_code`` to country rows. Lookups are case-insensitive and
tolerant of a handful of common aliases. Unknown names resolve to None — the
country row still appears, just without a code.

Read-only. No external calls.
"""

from __future__ import annotations

# Lowercased country name -> ISO alpha-2. Focused on Logistaas markets
# (MENA, Gulf, Europe, LATAM) plus major economies, with common aliases.
_COUNTRY_CODES = {
    # Gulf / MENA
    "saudi arabia": "SA",
    "ksa": "SA",
    "kingdom of saudi arabia": "SA",
    "united arab emirates": "AE",
    "uae": "AE",
    "u.a.e.": "AE",
    "emirates": "AE",
    "qatar": "QA",
    "kuwait": "KW",
    "bahrain": "BH",
    "oman": "OM",
    "jordan": "JO",
    "lebanon": "LB",
    "egypt": "EG",
    "iraq": "IQ",
    "yemen": "YE",
    "syria": "SY",
    "palestine": "PS",
    "israel": "IL",
    "turkey": "TR",
    "turkiye": "TR",
    "iran": "IR",
    "morocco": "MA",
    "algeria": "DZ",
    "tunisia": "TN",
    "libya": "LY",
    "sudan": "SD",
    # Europe
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "ireland": "IE",
    "france": "FR",
    "germany": "DE",
    "spain": "ES",
    "portugal": "PT",
    "italy": "IT",
    "netherlands": "NL",
    "the netherlands": "NL",
    "belgium": "BE",
    "luxembourg": "LU",
    "switzerland": "CH",
    "austria": "AT",
    "poland": "PL",
    "czech republic": "CZ",
    "czechia": "CZ",
    "slovakia": "SK",
    "hungary": "HU",
    "romania": "RO",
    "bulgaria": "BG",
    "greece": "GR",
    "cyprus": "CY",
    "malta": "MT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "iceland": "IS",
    "estonia": "EE",
    "latvia": "LV",
    "lithuania": "LT",
    "ukraine": "UA",
    "russia": "RU",
    "russian federation": "RU",
    "croatia": "HR",
    "slovenia": "SI",
    "serbia": "RS",
    # Americas
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "america": "US",
    "canada": "CA",
    "mexico": "MX",
    "brazil": "BR",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
    "peru": "PE",
    "ecuador": "EC",
    "venezuela": "VE",
    "uruguay": "UY",
    "paraguay": "PY",
    "bolivia": "BO",
    "panama": "PA",
    "costa rica": "CR",
    "guatemala": "GT",
    "dominican republic": "DO",
    # Asia / Pacific
    "china": "CN",
    "hong kong": "HK",
    "taiwan": "TW",
    "japan": "JP",
    "south korea": "KR",
    "korea": "KR",
    "republic of korea": "KR",
    "india": "IN",
    "pakistan": "PK",
    "bangladesh": "BD",
    "sri lanka": "LK",
    "singapore": "SG",
    "malaysia": "MY",
    "indonesia": "ID",
    "thailand": "TH",
    "vietnam": "VN",
    "viet nam": "VN",
    "philippines": "PH",
    "australia": "AU",
    "new zealand": "NZ",
    # Africa (sub-Saharan)
    "south africa": "ZA",
    "nigeria": "NG",
    "kenya": "KE",
    "ghana": "GH",
    "ethiopia": "ET",
    "tanzania": "TZ",
    "uganda": "UG",
}


def get_country_code(name: str | None) -> str | None:
    """Return the ISO alpha-2 code for a country name, or None if unknown.

    Lookup is case-insensitive and trims surrounding whitespace. A 2-letter
    input that is already a plausible code is uppercased and returned as-is.
    """
    if not name:
        return None
    key = str(name).strip().lower()
    if not key:
        return None
    if key in _COUNTRY_CODES:
        return _COUNTRY_CODES[key]
    # Treat a bare 2-letter token as an already-supplied code.
    if len(key) == 2 and key.isalpha():
        return key.upper()
    return None


# PR-ADS-124: reverse map (ISO alpha-2 -> canonical display name) so the
# canonical Google Ads geo table (which resolves a criterion id to a country
# code) can present a human-readable country name that also joins HubSpot deal
# country names. The first display name listed for each code wins.
_CODE_TO_NAME = {
    "SA": "Saudi Arabia", "AE": "United Arab Emirates", "QA": "Qatar",
    "KW": "Kuwait", "BH": "Bahrain", "OM": "Oman", "JO": "Jordan",
    "LB": "Lebanon", "EG": "Egypt", "IQ": "Iraq", "YE": "Yemen", "SY": "Syria",
    "PS": "Palestine", "IL": "Israel", "TR": "Turkey", "IR": "Iran",
    "MA": "Morocco", "DZ": "Algeria", "TN": "Tunisia", "LY": "Libya", "SD": "Sudan",
    "GB": "United Kingdom", "IE": "Ireland", "FR": "France", "DE": "Germany",
    "ES": "Spain", "PT": "Portugal", "IT": "Italy", "NL": "Netherlands",
    "BE": "Belgium", "LU": "Luxembourg", "CH": "Switzerland", "AT": "Austria",
    "PL": "Poland", "CZ": "Czech Republic", "SK": "Slovakia", "HU": "Hungary",
    "RO": "Romania", "BG": "Bulgaria", "GR": "Greece", "CY": "Cyprus",
    "MT": "Malta", "SE": "Sweden", "NO": "Norway", "DK": "Denmark",
    "FI": "Finland", "IS": "Iceland", "EE": "Estonia", "LV": "Latvia",
    "LT": "Lithuania", "UA": "Ukraine", "RU": "Russia", "HR": "Croatia",
    "SI": "Slovenia", "RS": "Serbia", "US": "United States", "CA": "Canada",
    "MX": "Mexico", "BR": "Brazil", "AR": "Argentina", "CL": "Chile",
    "CO": "Colombia", "PE": "Peru", "EC": "Ecuador", "VE": "Venezuela",
    "UY": "Uruguay", "PY": "Paraguay", "BO": "Bolivia", "PA": "Panama",
    "CR": "Costa Rica", "GT": "Guatemala", "DO": "Dominican Republic",
    "CN": "China", "HK": "Hong Kong", "TW": "Taiwan", "JP": "Japan",
    "KR": "South Korea", "IN": "India", "PK": "Pakistan", "BD": "Bangladesh",
    "KE": "Kenya", "GH": "Ghana", "ET": "Ethiopia", "TZ": "Tanzania", "UG": "Uganda",
}


def country_name_for_code(code: str | None) -> str | None:
    """Return a canonical display country name for an ISO alpha-2 code, or None.

    Used by the canonical Google Ads geo path: a criterion id resolves to a code,
    and this maps the code to a human-readable name for the ROAS by Country row.
    Unknown codes return None so the caller can fall back to the raw code.
    """
    if not code:
        return None
    return _CODE_TO_NAME.get(str(code).strip().upper())
