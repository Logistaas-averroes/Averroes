"""PR-ADS-165A — complete ISO country resolution, beside the market registry.

``analysis/country_identity.py`` resolves against ``SUPPORTED_COUNTRIES``, a
supported-MARKET registry of 96 codes that the revenue pages group on. The
Google Ads → Countries page was resolved through it too, and production does
not fit: the last 30 days of ``google_ads_geo_daily_spend`` hold spend in 162
distinct ISO codes, and the top lead countries in ``leads`` are stored
lower-case, some under names ISO retired ("libyan arab jamahiriya"). Through
the market registry most of the page read "(unresolved location)" or "not
matched".

So the module gained a SEPARATE resolver over the complete ISO 3166-1 table
(``analysis/iso_countries.py``, data in the repo — no new dependency). These
cases hold both halves: the complete resolver is right for what production
holds and never joins on a guess, and the market registry is exactly what it
was.

The two production lists below are verbatim from a read-only query on
2026-11-19.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis import country_identity as ci  # noqa: E402
from analysis import iso_countries  # noqa: E402

#: The 40 most common `leads.country` values over 90 days, as stored, and the
#: country each one is.
PRODUCTION_LEAD_COUNTRIES = {
    "nigeria": "NG", "egypt": "EG", "saudi arabia": "SA",
    "united arab emirates": "AE", "mexico": "MX", "colombia": "CO",
    "kenya": "KE", "iraq": "IQ", "united states": "US", "algeria": "DZ",
    "turkey": "TR", "south africa": "ZA", "morocco": "MA", "uzbekistan": "UZ",
    "jordan": "JO", "romania": "RO", "chile": "CL", "somalia": "SO",
    "tunisia": "TN", "canada": "CA", "bangladesh": "BD", "spain": "ES",
    "france": "FR", "oman": "OM", "ethiopia": "ET", "benin": "BJ",
    "sri lanka": "LK", "mozambique": "MZ", "malaysia": "MY", "cameroon": "CM",
    "tanzania": "TZ", "nepal": "NP", "pakistan": "PK", "philippines": "PH",
    "libyan arab jamahiriya": "LY", "denmark": "DK", "bahrain": "BH",
    "netherlands": "NL", "united kingdom": "GB", "kazakhstan": "KZ",
}

#: The 162 ISO codes with Google Ads country spend in the last 30 days.
PRODUCTION_SPEND_CODES = (
    "AE,AG,AL,AM,AQ,AR,AT,AU,AZ,BA,BB,BD,BE,BF,BG,BH,BI,BJ,BO,BR,BS,BW,BY,CA,"
    "CD,CF,CG,CH,CL,CM,CN,CO,CR,CY,CZ,DE,DJ,DK,DO,DZ,EC,EE,EG,ES,ET,FI,FJ,FR,"
    "GB,GE,GM,GN,GR,GT,GY,HK,HN,HR,HT,HU,ID,IE,IL,IN,IQ,IT,JE,JM,JO,JP,KE,KG,"
    "KH,KI,KM,KR,KW,KZ,LA,LB,LC,LK,LR,LS,LT,LV,LY,MA,MD,ME,MG,MK,MM,MN,MR,MT,"
    "MU,MV,MW,MX,MY,MZ,NA,NC,NE,NG,NI,NL,NO,NP,NZ,OM,PA,PE,PG,PH,PL,PR,PS,PT,"
    "PW,PY,QA,RO,RS,RW,SA,SE,SG,SI,SK,SL,SN,SO,SR,SS,SV,SY,SZ,TC,TG,TH,TJ,TL,"
    "TM,TN,TR,TT,TW,TZ,UA,UG,US,UY,UZ,VC,VE,VN,WS,YE,ZA,ZW").split(",")


def test_01_the_table_is_the_complete_iso_3166_1_alpha_2_set():
    names = iso_countries.ISO_COUNTRY_NAMES
    assert len(names) == 249
    for code, name in names.items():
        assert len(code) == 2 and code.isalpha() and code.isupper(), code
        assert name and name.strip() == name and name.isascii(), (code, name)
    assert len(set(names.values())) == 249, "two codes share a display name"
    # Spot checks across the alphabet, including the ones a market list omits.
    for code, name in (("AD", "Andorra"), ("KZ", "Kazakhstan"), ("GE", "Georgia"),
                       ("NG", "Nigeria"), ("SO", "Somalia"), ("UZ", "Uzbekistan"),
                       ("GB", "United Kingdom"), ("KR", "South Korea"),
                       ("KP", "North Korea"), ("CD", "Democratic Republic of the Congo"),
                       ("CG", "Republic of the Congo"), ("CI", "Cote d'Ivoire"),
                       ("ZW", "Zimbabwe")):
        assert names[code] == name
        assert ci.iso_country_label(code) == name
    assert ci.iso_country_label("xx") is None and ci.iso_country_label(None) is None
    # Every alias points at a real code, and no alias contradicts a name.
    for alias, code in iso_countries.ISO_COUNTRY_NAME_ALIASES.items():
        assert code in names, (alias, code)
    # It is repo data: nothing is imported to build it.
    src = (ROOT / "analysis/iso_countries.py").read_text(encoding="utf-8")
    assert "pycountry" not in (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "\nimport " not in src.split('"""', 2)[2].replace(
        "from __future__ import annotations", "")


def test_02_every_production_spend_code_is_a_known_country():
    assert len(PRODUCTION_SPEND_CODES) == len(set(PRODUCTION_SPEND_CODES)) == 162
    for code in PRODUCTION_SPEND_CODES:
        assert ci.is_iso_country_code(code), code
        assert ci.iso_country_label(code), code
    # 81 of them are outside the market registry: resolved through it, half of
    # production's spend countries were "(unresolved location)".
    outside = [c for c in PRODUCTION_SPEND_CODES if c not in ci.SUPPORTED_CODES]
    assert len(outside) > 60, len(outside)
    assert {"KZ", "GE", "SO", "UZ", "CM", "MZ"} <= set(outside)


@pytest.mark.parametrize("text, code", sorted(PRODUCTION_LEAD_COUNTRIES.items()))
def test_03_every_production_lead_spelling_resolves_to_its_country(text, code):
    assert ci.resolve_iso_country_text(text) == code
    # Case, padding and punctuation do not matter.
    assert ci.resolve_iso_country_text(f"  {text.upper()}  ") == code
    assert ci.resolve_iso_country_text(text.title()) == code
    assert ci.resolve_iso_country_text(text.replace(" ", "-")) == code
    assert not ci.is_no_country_placeholder(text)
    # And the canonical name of that country resolves back to it.
    assert ci.resolve_iso_country_text(ci.iso_country_label(code)) == code


def test_03b_the_whole_table_round_trips():
    for code, name in iso_countries.ISO_COUNTRY_NAMES.items():
        assert ci.resolve_iso_country_text(name) == code, (code, name)
        assert ci.resolve_iso_country_text(name.upper()) == code, (code, name)


@pytest.mark.parametrize("text, code", [
    # legacy and inverted names HubSpot emits
    ("libyan arab jamahiriya", "LY"), ("Korea, Republic of", "KR"),
    ("korea, democratic people's republic of", "KP"),
    ("Russian Federation", "RU"), ("Viet Nam", "VN"),
    ("Iran, Islamic Republic of", "IR"), ("Syrian Arab Republic", "SY"),
    ("Tanzania, United Republic of", "TZ"), ("Czech Republic", "CZ"),
    ("Czechia", "CZ"), ("Côte d'Ivoire", "CI"), ("Cote d’Ivoire", "CI"),
    ("Ivory Coast", "CI"), ("Türkiye", "TR"), ("Turkiye", "TR"), ("Turkey", "TR"),
    ("United States of America", "US"), ("USA", "US"), ("U.S.A.", "US"),
    ("Macedonia", "MK"), ("North Macedonia", "MK"),
    ("Swaziland", "SZ"), ("Eswatini", "SZ"), ("Burma", "MM"), ("Myanmar", "MM"),
    ("Moldova, Republic of", "MD"), ("Venezuela, Bolivarian Republic of", "VE"),
    ("Bolivia, Plurinational State of", "BO"), ("Lao People's Democratic Republic", "LA"),
    ("Brunei Darussalam", "BN"), ("Palestine, State of", "PS"),
    ("Taiwan, Province of China", "TW"), ("Macao", "MO"), ("Cabo Verde", "CV"),
    ("The Netherlands", "NL"), ("the gambia", "GM"), ("Bosnia & Herzegovina", "BA"),
    ("Trinidad & Tobago", "TT"), ("UK", "GB"), ("U.K.", "GB"), ("England", "GB"),
    ("Northern Ireland", "GB"), ("UAE", "AE"), ("KSA", "SA"),
    ("Saudi-Arabia", "SA"), ("Jordan.", "JO"),
    # the two Congos are DISTINCT
    ("Congo, The Democratic Republic of the", "CD"),
    ("Democratic Republic of the Congo", "CD"), ("DR Congo", "CD"),
    ("Republic of the Congo", "CG"), ("Congo-Brazzaville", "CG"),
    # neighbours whose names contain one another are exact matches only
    ("Niger", "NE"), ("Nigeria", "NG"), ("Guinea", "GN"), ("Guinea-Bissau", "GW"),
    ("Equatorial Guinea", "GQ"), ("Papua New Guinea", "PG"),
    ("Dominica", "DM"), ("Dominican Republic", "DO"),
    ("Sudan", "SD"), ("South Sudan", "SS"), ("Samoa", "WS"), ("American Samoa", "AS"),
    ("Ireland", "IE"), ("India", "IN"), ("Indonesia", "ID"),
    # judgement calls, stated in analysis/iso_countries.py
    ("Georgia", "GE"), ("Korea", "KR"),
])
def test_04_legacy_inverted_and_lookalike_names_resolve_exactly(text, code):
    assert ci.resolve_iso_country_text(text) == code


@pytest.mark.parametrize("text", [
    # ambiguous: the text does not say which country
    "Congo", "congo", "Virgin Islands",
    # not countries
    "California", "Dubai", "Abu Dhabi", "Texas", "London", "Europe", "Africa",
    "Middle East", "Narnia", "Untied Kingdom", "Kazakstan", "Nigerian",
    # two-letter text is NOT a code: CA is Canada or California, IN India or Indiana
    "IN", "CA", "in", "ca", "GE", "DE", "NG", "KZ", "AE",
    # a name with something added is not that name
    "United Kingdom and Ireland", "France / Germany", "Kenya (Nairobi)",
])
def test_05_a_name_that_does_not_name_one_country_is_never_matched(text):
    assert ci.resolve_iso_country_text(text) is None


@pytest.mark.parametrize("text", [
    None, "", "   ", "(unknown)", "unknown", "Unknown", "Other", "others", "N/A",
    "n/a", "NA", "none", "null", "-", "--", "?", "Not set", "not specified",
    "(unresolved location)", "ZZ", "XX", "Unknown / Unattributed country",
])
def test_06_placeholders_are_no_country_not_an_unmatched_country(text):
    assert ci.is_no_country_placeholder(text) is True
    assert ci.resolve_iso_country_text(text) is None


def test_07_the_market_registry_and_its_callers_are_unchanged():
    """The complete resolver is a separate set of names. `resolve`,
    `country_key` and the registry the revenue pages group on behave exactly
    as before — widening them would move rows between "valid" and "unknown" on
    Country Revenue & Spend and Dashboard Countries."""
    assert len(ci.SUPPORTED_COUNTRIES) == 96
    assert len(ci.COUNTRY_ALIASES) == 124
    assert ci.SUPPORTED_CODES < set(iso_countries.ISO_COUNTRY_NAMES)
    # A country outside the market registry is STILL not a valid identity
    # there, by name or by code…
    for name, code in (("Kazakhstan", "KZ"), ("Georgia", "GE"), ("Somalia", "SO")):
        assert code not in ci.SUPPORTED_CODES
        assert ci.resolve(name=name).status == ci.STATUS_INVALID
        assert ci.resolve(code=code).status == ci.STATUS_INVALID
        assert ci.country_key(name=name) == ci.RESIDUAL_KEY
        assert ci.get_country_code(name) is None
        # …while the complete resolver knows it.
        assert ci.resolve_iso_country_text(name) == code
    # Where both know a name they agree on the code.
    for norm, code in ci.COUNTRY_ALIASES.items():
        got = ci.resolve_iso_country_text(norm)
        assert got in (code, None), (norm, code, got)
        if len(norm) > 2:
            assert got == code, (norm, code, got)
    # Unchanged behaviours, pinned.
    assert ci.resolve(name="UAE").key == "code:AE"
    assert ci.resolve(name="UK").key == "code:GB"
    assert ci.resolve(name="XX").status == ci.STATUS_INVALID
    assert ci.resolve(name="").status == ci.STATUS_UNKNOWN
    assert ci.display_label("code:GB") == "United Kingdom"
    # The functions above the new block do not reach into it.
    src = (ROOT / "analysis/country_identity.py").read_text(encoding="utf-8")
    old, new = src.split("# Complete ISO resolution (PR-ADS-165A)")
    for name in ("resolve_iso_country_text", "ISO_COUNTRY_NAMES", "iso_countries",
                 "ISO_NAME_TO_CODE", "is_no_country_placeholder"):
        assert name not in old, name
    # And no live page calls the complete resolver. PR-ADS-165A: only the
    # Countries page service did; PR-ADS-176C retired that service with
    # /api/geo, so no production module calls it now. The live country pages
    # resolve lead text through the market registry — which is why a listed
    # market's ISO-only spelling lands in their residual (FINDING 176C-F1,
    # docs/58 §8.4). Adopting this resolver there is a publication change.
    callers = sorted(
        str(p.relative_to(ROOT)) for pkg in ("analysis", "api", "db", "scheduler",
                                             "services", "scripts", "connectors")
        for p in (ROOT / pkg).rglob("*.py")
        if "__pycache__" not in p.parts
        and "resolve_iso_country_text" in p.read_text(encoding="utf-8"))
    assert callers == ["analysis/country_identity.py",
                       "analysis/iso_countries.py"]       # names it in its docstring


def test_08_kosovo_resolves_to_the_code_google_uses_and_iso_stays_iso():
    """`XK` is a user-assigned code, not an ISO assignment — and it is the code
    Google Ads reports Kosovo's spend under. It is held apart, so the ISO
    table is still exactly ISO's 249."""
    assert iso_countries.USER_ASSIGNED_COUNTRY_CODES == {"XK": "Kosovo"}
    assert "XK" not in iso_countries.ISO_COUNTRY_NAMES
    assert len(iso_countries.ISO_COUNTRY_NAMES) == 249
    for text in ("Kosovo", "kosovo", " KOSOVO "):
        assert ci.resolve_iso_country_text(text) == "XK", text
    assert ci.iso_country_label("XK") == "Kosovo"
    assert ci.is_iso_country_code("XK") is False
    # The two-letter token is still not read as a code.
    assert ci.resolve_iso_country_text("XK") is None
    # And the market registry does not know it, as before.
    assert ci.resolve(name="Kosovo").status == ci.STATUS_INVALID
