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
    # PR-ADS-176C-F2 (m5): two more production spellings (7 and 32 leads).
    "iran (islamic republic of)": "IR",
    "democratic republic of the congo": "CD",
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


def test_07_the_market_registry_is_unchanged_and_resolve_adds_only_exact_iso_names():
    """The registry the revenue pages group on is unchanged: 96 markets, the
    same aliases, a country outside it is still the residual.

    PR-ADS-176C-F1 (F1): `resolve` — and so `country_key` — now falls back to
    the exact complete-ISO lookup for a NAME the alias table does not spell,
    and accepts it only for a listed market. That moves exactly such text
    ("libyan arab jamahiriya", "Iran, Islamic Republic of", "Türkiye",
    "Jordan.") from the residual onto its market, as the retired /api/geo did;
    nothing else moves. (This case pinned that `resolve` did NOT reach the ISO
    table — the defect.)"""
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
    # F1: exact ISO / legacy names of LISTED markets resolve to them…
    for text, code in (("libyan arab jamahiriya", "LY"), ("Iran, Islamic Republic of", "IR"),
                       ("Türkiye", "TR"), ("Jordan.", "JO"), ("  jordan .", "JO")):
        ident = ci.resolve(name=text)
        assert (ident.key, ident.status, ident.reason) == (
            f"code:{code}", ci.STATUS_VALID, ci.REASON_ISO_NAME_MATCH), text
    # …and nothing looser: no fuzzy or prefix match, no two-letter token, no
    # ambiguous name, no country outside the 96, no placeholder.
    for text in ("Jordann", "Jor dan", "Libya Arab", "Congo", "Dubai", "California",
                 "CA", "IN", "ZZ", "North Korea", "Kosovo", "Other", "N/A", "Narnia"):
        assert ci.resolve(name=text).key == ci.RESIDUAL_KEY, text
    # PR-ADS-176C-F2 (m5): two production spellings. Iran is a listed market;
    # the Democratic Republic of the Congo is not (CD), so it is the residual —
    # and never the Republic of the Congo (CG).
    assert ci.resolve(name="iran (islamic republic of)").key == "code:IR"
    drc = ci.resolve(name="democratic republic of the congo")
    assert (drc.key, drc.code) == (ci.RESIDUAL_KEY, None)
    assert ci.resolve_iso_country_text("democratic republic of the congo") == "CD"
    assert "CD" not in ci.SUPPORTED_CODES and "CG" not in ci.SUPPORTED_CODES
    assert ci.resolve(name="Congo").key == ci.RESIDUAL_KEY
    # A supplied code still wins over any name.
    assert ci.resolve(name="libyan arab jamahiriya", code="JO").key == "code:JO"
    # The one production caller of the complete resolver is `resolve` itself.
    src = (ROOT / "analysis/country_identity.py").read_text(encoding="utf-8")
    old, new = src.split("# Complete ISO resolution (PR-ADS-165A)")
    resolve_src = old[old.index("def resolve("):old.index("\ndef country_key(")]
    assert "resolve_iso_country_text(name)" in resolve_src
    assert "iso in SUPPORTED_CODES" in resolve_src
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
