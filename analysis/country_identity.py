"""
analysis/country_identity.py

PR-ADS-153F — THE canonical country identity contract.

Before this module the product had three different country join rules:

  * ROAS by Country grouped on a raw lowercased country string,
  * Dashboard Countries grouped ISO-code-first and fell back to a normalized
    name, and
  * the country drilldown matched by code and then by normalized name.

Three rules produce three key sets, so the same business window and the same
revenue scope could return different country rows — and different totals — on
different pages. That is precisely the failure the source-of-truth program
exists to remove:

    same metric + same business window + same scope = the same result
    on every page.

Every country consumer now resolves identity HERE and groups on
:func:`country_key`. The rules, once, in one place:

  * A validated ISO 3166-1 alpha-2 code is the identity. Names are only ever
    an input to resolution and a display label — never a join key.
  * A two-letter token is NOT a country code. ``get_country_code`` used to
    uppercase any 2-letter string and hand it back, so ``"XX"``, ``"ZZ"`` and
    a truncated label all became "valid ISO codes". Only codes in
    :data:`SUPPORTED_COUNTRIES` resolve.
  * Geography that cannot be identified is never dropped and never guessed. It
    resolves to the explicit residual key ``unknown`` with a reason, so the
    revenue behind it stays visible and reconcilable.
  * Normalization is locale-independent: ASCII casefold on an explicit alias
    table, never ``str.title()``/``str.capitalize()`` or a locale-sensitive
    transform whose output depends on the server's environment.

Source ownership is unchanged and must stay that way. Google Ads
``geographic_view`` answers *where advertising spend occurred*; HubSpot contact
country answers *the CRM geography of the associated contact*. They are
different facts about different entities. This module gives them ONE key so
they can be joined and reconciled at reporting grain — it does NOT claim they
measure the same thing. See :data:`ESTIMATE_GRADE_NOTE`.

Pure and read-only: no database, no network, no I/O.
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Resolution status
# ─────────────────────────────────────────────────────────────────────────────

#: The country resolved to a supported ISO 3166-1 alpha-2 code.
STATUS_VALID = "valid"
#: No country evidence at all (blank / null / whitespace).
STATUS_UNKNOWN = "unknown"
#: Country evidence exists but does not resolve to a supported country.
STATUS_INVALID = "invalid"
#: The row IS the explicit unattributed bucket (e.g. Google Ads location-less
#: spend, which ``geographic_view`` omits by design).
STATUS_RESIDUAL = "residual"

RESOLVED_STATUSES = (STATUS_VALID, STATUS_UNKNOWN, STATUS_INVALID, STATUS_RESIDUAL)

#: Statuses that are NOT a real country and therefore share the residual key.
#: Kept as a named set so no consumer re-invents "which ones are residual".
NON_COUNTRY_STATUSES = frozenset({STATUS_UNKNOWN, STATUS_INVALID, STATUS_RESIDUAL})

# Reasons — narrower than status, so a page can say WHY without re-deriving it.
REASON_SUPPORTED_CODE = "supported_iso_code"
REASON_ALIAS_MATCH = "alias_resolved_to_iso_code"
REASON_BLANK = "no_country_evidence"
REASON_UNSUPPORTED_CODE = "two_letter_token_is_not_a_supported_country_code"
REASON_UNRECOGNIZED_NAME = "country_name_not_in_supported_registry"
REASON_RESIDUAL_LABEL = "explicit_unattributed_residual"

# ─────────────────────────────────────────────────────────────────────────────
# The residual bucket
# ─────────────────────────────────────────────────────────────────────────────

#: The one canonical key for everything that is not an identified country.
RESIDUAL_KEY = "unknown"
#: The one display label for that key. Presentation only — never a join key.
RESIDUAL_LABEL = "Unknown / Unattributed country"

#: Labels a producer may already have written to mean "no country". Matched on
#: the normalized form so a page's own residual row round-trips back to the
#: residual key instead of being mistaken for a country called "Unattributed".
_RESIDUAL_LABELS = frozenset({
    "unknown", "unknown / unattributed country", "unattributed",
    "unattributed / no country", "no country", "none", "null", "n/a", "na",
    "not set", "unspecified", "other", "unattributed / unknown",
})

ESTIMATE_GRADE_NOTE = (
    "Google Ads advertising geography (geographic_view) and HubSpot "
    "contact/IP geography are different facts about different entities. They "
    "are joined at reporting grain on a shared country key to compare markets; "
    "the join is estimate-grade and must be disclosed as such. Neither source "
    "defines the other: Google Ads never defines the won-deal population, and "
    "HubSpot country never replaces Google Ads spend geography."
)


# ─────────────────────────────────────────────────────────────────────────────
# The supported country registry
# ─────────────────────────────────────────────────────────────────────────────
# ISO 3166-1 alpha-2 code -> canonical English display label.
#
# This is the AUTHORITATIVE list. ``SUPPORTED_CODES``, the alias table and the
# reverse (code -> name) lookup are all derived from it, so a code can never
# again be resolvable in one direction and missing in the other — the defect
# that left SG, MY, ID, TH, VN, PH, AU, NZ, LK, ZA and NG nameless while
# ``_COUNTRY_CODES`` happily produced them.
#
# It is deliberately a SUPPORTED-MARKET registry rather than all 249 ISO
# entries: an unrecognised code must be reported as unsupported, not silently
# accepted, and adding a market is a reviewed one-line change here.
SUPPORTED_COUNTRIES: dict[str, str] = {
    # Gulf / MENA
    "SA": "Saudi Arabia",
    "AE": "United Arab Emirates",
    "QA": "Qatar",
    "KW": "Kuwait",
    "BH": "Bahrain",
    "OM": "Oman",
    "JO": "Jordan",
    "LB": "Lebanon",
    "EG": "Egypt",
    "IQ": "Iraq",
    "YE": "Yemen",
    "SY": "Syria",
    "PS": "Palestine",
    "IL": "Israel",
    "TR": "Turkey",
    "IR": "Iran",
    "MA": "Morocco",
    "DZ": "Algeria",
    "TN": "Tunisia",
    "LY": "Libya",
    "SD": "Sudan",
    # Europe
    "GB": "United Kingdom",
    "IE": "Ireland",
    "FR": "France",
    "DE": "Germany",
    "ES": "Spain",
    "PT": "Portugal",
    "IT": "Italy",
    "NL": "Netherlands",
    "BE": "Belgium",
    "LU": "Luxembourg",
    "CH": "Switzerland",
    "AT": "Austria",
    "PL": "Poland",
    "CZ": "Czech Republic",
    "SK": "Slovakia",
    "HU": "Hungary",
    "RO": "Romania",
    "BG": "Bulgaria",
    "GR": "Greece",
    "CY": "Cyprus",
    "MT": "Malta",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "IS": "Iceland",
    "EE": "Estonia",
    "LV": "Latvia",
    "LT": "Lithuania",
    "UA": "Ukraine",
    "RU": "Russia",
    "HR": "Croatia",
    "SI": "Slovenia",
    "RS": "Serbia",
    # Americas
    "US": "United States",
    "CA": "Canada",
    "MX": "Mexico",
    "BR": "Brazil",
    "AR": "Argentina",
    "CL": "Chile",
    "CO": "Colombia",
    "PE": "Peru",
    "EC": "Ecuador",
    "VE": "Venezuela",
    "UY": "Uruguay",
    "PY": "Paraguay",
    "BO": "Bolivia",
    "PA": "Panama",
    "CR": "Costa Rica",
    "GT": "Guatemala",
    "DO": "Dominican Republic",
    # Asia / Pacific
    "CN": "China",
    "HK": "Hong Kong",
    "TW": "Taiwan",
    "JP": "Japan",
    "KR": "South Korea",
    "IN": "India",
    "PK": "Pakistan",
    "BD": "Bangladesh",
    "LK": "Sri Lanka",
    "SG": "Singapore",
    "MY": "Malaysia",
    "ID": "Indonesia",
    "TH": "Thailand",
    "VN": "Vietnam",
    "PH": "Philippines",
    "AU": "Australia",
    "NZ": "New Zealand",
    # Africa (sub-Saharan)
    "ZA": "South Africa",
    "NG": "Nigeria",
    "KE": "Kenya",
    "GH": "Ghana",
    "ET": "Ethiopia",
    "TZ": "Tanzania",
    "UG": "Uganda",
}

SUPPORTED_CODES = frozenset(SUPPORTED_COUNTRIES)

# Extra spellings a source may emit. The canonical display label from
# SUPPORTED_COUNTRIES is registered automatically, so this table only carries
# the ALIASES — a name here can never disagree with the registry.
_EXTRA_ALIASES: dict[str, str] = {
    # Gulf / MENA
    "ksa": "SA",
    "kingdom of saudi arabia": "SA",
    "uae": "AE",
    "u.a.e.": "AE",
    "emirates": "AE",
    "turkiye": "TR",
    # Europe
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "the netherlands": "NL",
    "holland": "NL",
    "czechia": "CZ",
    "russian federation": "RU",
    # Americas
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "america": "US",
    # Asia / Pacific
    "korea": "KR",
    "republic of korea": "KR",
    "south korea": "KR",
    "viet nam": "VN",
    "hong kong sar": "HK",
    "hong kong sar china": "HK",
    # Africa
    "republic of south africa": "ZA",
}


def normalize_label(value) -> str:
    """Locale-independent normalization of a country label.

    ASCII casefold, underscores and punctuation-free separators collapsed to
    single spaces. Deliberately NOT ``str.lower()`` on arbitrary Unicode with a
    locale-sensitive path, and deliberately not ``title()``: the normalized form
    is a lookup key, so it must be identical on every machine, in every locale,
    for the same input.
    """
    if value is None:
        return ""
    text = str(value).replace("_", " ").replace("-", " ").strip()
    if not text:
        return ""
    return " ".join(text.casefold().split())


def _build_alias_table() -> dict[str, str]:
    table: dict[str, str] = {}
    for code, label in SUPPORTED_COUNTRIES.items():
        table[normalize_label(label)] = code
    for alias, code in _EXTRA_ALIASES.items():
        if code not in SUPPORTED_COUNTRIES:  # pragma: no cover - guarded by test
            raise ValueError(f"alias {alias!r} points at unsupported code {code!r}")
        table[normalize_label(alias)] = code
    return table


#: Normalized name/alias -> ISO alpha-2. Built from the registry, so every
#: supported country is reachable by its own canonical label.
COUNTRY_ALIASES: dict[str, str] = _build_alias_table()


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────

class CountryIdentity:
    """The resolved identity of one country-bearing row.

    ``key`` is the ONLY thing consumers may group or join on. ``label`` is for
    display and may change without changing identity.
    """

    __slots__ = ("code", "key", "label", "status", "reason", "raw")

    def __init__(self, *, code, key, label, status, reason, raw):
        self.code = code
        self.key = key
        self.label = label
        self.status = status
        self.reason = reason
        self.raw = raw

    @property
    def is_country(self) -> bool:
        """True only for an identified, supported country."""
        return self.status == STATUS_VALID

    @property
    def is_residual(self) -> bool:
        """True when this row belongs in the explicit residual bucket."""
        return self.status in NON_COUNTRY_STATUSES

    def as_dict(self) -> dict:
        return {
            "country_key": self.key,
            "country_code": self.code,
            "country": self.label,
            "country_status": self.status,
            "country_reason": self.reason,
            "is_residual": self.is_residual,
        }

    def __eq__(self, other) -> bool:
        return (isinstance(other, CountryIdentity)
                and (self.key, self.code, self.status) == (other.key, other.code, other.status))

    def __hash__(self) -> int:
        return hash((self.key, self.code, self.status))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CountryIdentity {self.key} {self.status} {self.label!r}>"


def _residual(status: str, reason: str, raw) -> CountryIdentity:
    return CountryIdentity(code=None, key=RESIDUAL_KEY, label=RESIDUAL_LABEL,
                           status=status, reason=reason, raw=raw)


def is_supported_code(value) -> bool:
    """True only for a supported ISO 3166-1 alpha-2 code.

    ``"XX"``, ``"ZZ"``, ``"1A"`` and a truncated label are all False. This is
    the check that replaces "any two alphabetic characters is a country code".
    """
    if value is None:
        return False
    text = str(value).strip()
    return len(text) == 2 and text.upper() in SUPPORTED_CODES


def resolve(name=None, code=None) -> CountryIdentity:
    """Resolve country evidence to ONE canonical identity.

    ``code`` (an ISO alpha-2 from a source that supplies one, e.g. the resolved
    Google Ads geo target) wins over ``name`` — a code is an identity, a name is
    a spelling. Anything that cannot be identified becomes the explicit residual
    rather than being dropped or guessed.
    """
    raw = code if code not in (None, "") else name

    if code not in (None, ""):
        text = str(code).strip()
        if is_supported_code(text):
            upper = text.upper()
            return CountryIdentity(code=upper, key=f"code:{upper}",
                                   label=SUPPORTED_COUNTRIES[upper],
                                   status=STATUS_VALID, reason=REASON_SUPPORTED_CODE,
                                   raw=raw)
        # A supplied code that is not supported is evidence we cannot trust. Fall
        # through to the name (a source may send a bad code and a good label),
        # but remember that the code itself was rejected.
        code_rejected = REASON_UNSUPPORTED_CODE if len(text) == 2 else REASON_UNRECOGNIZED_NAME
    else:
        code_rejected = None

    norm = normalize_label(name)
    if not norm:
        # A rejected code with no label is bad evidence, not absent evidence.
        return _residual(STATUS_INVALID if code_rejected else STATUS_UNKNOWN,
                         code_rejected or REASON_BLANK, raw)
    if norm in _RESIDUAL_LABELS:
        return _residual(STATUS_RESIDUAL, REASON_RESIDUAL_LABEL, raw)

    resolved = COUNTRY_ALIASES.get(norm)
    if resolved:
        # A good label rescues a bad code — dropping real revenue because one
        # field was malformed would be worse. The rejection stays in `reason` so
        # the bad code is auditable rather than silently forgotten.
        return CountryIdentity(code=resolved, key=f"code:{resolved}",
                               label=SUPPORTED_COUNTRIES[resolved],
                               status=STATUS_VALID,
                               reason=(f"{REASON_ALIAS_MATCH}_after_{code_rejected}"
                                       if code_rejected else REASON_ALIAS_MATCH),
                               raw=raw)

    # A bare two-letter token that is not a supported code is NOT a country.
    reason = (REASON_UNSUPPORTED_CODE if len(norm) == 2 and norm.isalpha()
              else REASON_UNRECOGNIZED_NAME)
    return _residual(STATUS_INVALID, code_rejected or reason, raw)


def country_key(name=None, code=None) -> str:
    """The stable join key for a country-bearing row.

    ``code:XX`` for an identified country, :data:`RESIDUAL_KEY` otherwise. This
    is the ONE function every consumer groups on.
    """
    return resolve(name=name, code=code).key


def display_label(key) -> str:
    """The display label for a canonical country key.

    Presentation only. API and UI labels may differ in wording; identity is the
    key, so a label change can never move a row between buckets.
    """
    if key in (None, "", RESIDUAL_KEY):
        return RESIDUAL_LABEL
    text = str(key)
    if text.startswith("code:"):
        return SUPPORTED_COUNTRIES.get(text[5:].upper(), RESIDUAL_LABEL)
    return SUPPORTED_COUNTRIES.get(text.upper(), RESIDUAL_LABEL)


def code_for_key(key) -> str | None:
    """The ISO alpha-2 behind a canonical key, or None for the residual."""
    if not key or key == RESIDUAL_KEY:
        return None
    text = str(key)
    code = text[5:].upper() if text.startswith("code:") else text.upper()
    return code if code in SUPPORTED_CODES else None


def country_name_for_code(code) -> str | None:
    """Canonical display name for a supported ISO alpha-2 code, else None.

    The reverse direction of :data:`SUPPORTED_COUNTRIES`, derived from the same
    dict — so "resolvable forward but nameless backward" is now unrepresentable.
    """
    if not code:
        return None
    return SUPPORTED_COUNTRIES.get(str(code).strip().upper())


def get_country_code(name) -> str | None:
    """ISO alpha-2 for a country name/alias, or None.

    Unlike the retired ``services.country_codes.get_country_code``, a bare
    two-letter token is only returned when it is a SUPPORTED code.
    """
    identity = resolve(name=name)
    return identity.code


__all__ = [
    "STATUS_VALID", "STATUS_UNKNOWN", "STATUS_INVALID", "STATUS_RESIDUAL",
    "RESOLVED_STATUSES", "NON_COUNTRY_STATUSES",
    "REASON_SUPPORTED_CODE", "REASON_ALIAS_MATCH", "REASON_BLANK",
    "REASON_UNSUPPORTED_CODE", "REASON_UNRECOGNIZED_NAME", "REASON_RESIDUAL_LABEL",
    "RESIDUAL_KEY", "RESIDUAL_LABEL", "ESTIMATE_GRADE_NOTE",
    "SUPPORTED_COUNTRIES", "SUPPORTED_CODES", "COUNTRY_ALIASES",
    "CountryIdentity", "normalize_label", "is_supported_code", "resolve",
    "country_key", "display_label", "code_for_key", "country_name_for_code",
    "get_country_code",
]
