"""Meta (Facebook/Instagram) Marketing API connector — READ ONLY (PR-ADS-171A).

Reads each configured ad account's description, its daily spend and (for the
smoke test) how many of its campaigns are active. Nothing else.

Governance
----------
* The only HTTP method this module issues is GET. ``_request`` refuses any
  other verb before it resolves a credential. Asynchronous report runs are a
  POST and are therefore not used.
* The access token is sent ONLY in the ``Authorization: Bearer`` header. It is
  never placed in a URL, a query parameter, a log line, an exception message
  or a return value.
* Meta's ``paging.next`` is a complete URL that embeds the access token. This
  module never requests, stores or logs it: its PRESENCE means "another page
  exists", and the next page is requested with our own parameters plus
  ``after=<paging.cursors.after>``.
* An exception raised by ``requests`` carries the request URL in its text, so
  no error built here includes ``str(exc)`` or the request URL: transport
  failures are named by exception class only, and API refusals by HTTP status
  and the fields of Meta's ``error`` object. Meta's own ``message`` is
  included; it is Meta's text, so it is scrubbed of every configured secret
  and of anything shaped like a token parameter, and truncated, first.

Credentials (environment)
-------------------------
``META_ADS_ACCESS_TOKEN``  required. A system-user token with ``ads_read``.
``META_AD_ACCOUNT_ID``     required. One ad account id, or several separated
                           by commas. Digits; a leading ``act_`` and spaces
                           are accepted; a repeated id is used once. The token
                           above reads them all.
``META_ADS_ACCESS_TOKEN_2`` + ``META_AD_ACCOUNT_ID_2`` (… ``_5``)
                           optional further CREDENTIAL SETS: another token and
                           the accounts IT reads — for an account in another
                           Business portfolio. An account is only ever called
                           with its own set's token. An account listed in two
                           sets makes the configuration invalid.
``META_AD_ACCOUNT_RETIRED`` optional, not a secret. ``<account id>:<last spend
                           day>`` pairs, comma-separated: the owner's
                           declaration that an account Meta no longer lets us
                           read spent nothing after that day (docs/52 §6).
``META_API_VERSION``       optional override of ``meta_ads.api_version``
                           (config/thresholds.yaml). No version is written in
                           this file.
``META_APP_SECRET``        optional (``META_APP_SECRET_2`` … for the other
                           sets). When set, that set's calls carry
                           ``appsecret_proof`` (an HMAC of the token, not the
                           token).

Nothing set: not configured, and no network call is made. A set with one of
its two variables is misconfigured and is not called; complete sets still
sync. No complete set, or no resolvable API version: no call is made.

Some facts about the live API were confirmed from production on 2026-11-22
and the rest could not be checked from the build environment. Both lists are
in docs/52_META_ADS_SPEND_CONNECTOR.md §9.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Optional

import requests

logger = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
SOURCE_QUERY_VERSION = "meta_insights_campaign_daily_v1"

#: Credential sets: the unsuffixed pair, then ``_2`` ... up to this many.
MAX_CREDENTIAL_SETS = 5

MAX_ATTEMPTS = 4
INITIAL_BACKOFF_SECONDS = 2
#: The longest one wait may be, whatever Meta says it needs. The daily sync
#: must not sit for an hour inside one step; a longer throttle fails the run.
MAX_WAIT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_PAGE_LIMIT = 500
#: A response that never stops paging is a failure, not a long result.
MAX_PAGES = 200

#: Meta reports rate limiting as HTTP 400 with one of these ``error.code``s.
THROTTLE_CODES = frozenset({4, 17, 32, 613, 80000, 80004})
#: Invalid, expired or revoked access token.
AUTH_ERROR_CODE = 190
#: "You are calling a deprecated version of the Ads API" (observed in
#: production 2026-11-22, HTTP 400). Retrying cannot help.
DEPRECATED_VERSION_CODE = 2635
#: Response headers that may carry ``estimated_time_to_regain_access`` (minutes).
_USAGE_HEADERS = ("x-business-use-case-usage", "x-ad-account-usage", "x-app-usage")

ACCOUNT_FIELDS = ("account_id,name,currency,timezone_name,"
                  "timezone_offset_hours_utc,account_status")
CAMPAIGN_FIELDS = ("campaign_id,campaign_name,spend,impressions,clicks,"
                   "account_currency,date_start,date_stop")
ACCOUNT_DAY_FIELDS = "spend,account_currency,date_start,date_stop"

_VERSION_RE = re.compile(r"^v\d{1,3}\.\d{1,2}$")
_ACCOUNT_RE = re.compile(r"^\d{1,32}$")
_MICROS = Decimal(1_000_000)


class MetaAdsError(RuntimeError):
    """Base class. ``reason`` is the machine code a run records."""
    reason = "meta_api_error"

    def __init__(self, message: str, *, http_status: Optional[int] = None,
                 code=None, subcode=None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.subcode = subcode


class MetaAdsNotConfigured(MetaAdsError):
    """Credentials are absent or incomplete, or no API version resolves."""
    reason = "meta_ads_misconfigured"


class MetaAdsAuthError(MetaAdsError):
    """The access token is expired, revoked or invalid (code 190). Never retried."""
    reason = "meta_token_rejected"


class MetaAdsRefused(MetaAdsError):
    """Meta refused the request (a 4xx that is not throttling). Never retried."""
    reason = "meta_api_refused"


class MetaAdsVersionDeprecated(MetaAdsRefused):
    """The configured API version is no longer served (code 2635). Never
    retried. ``upgrade_to`` is the version Meta names, when it names one."""
    reason = "meta_api_version_deprecated"
    upgrade_to: Optional[str] = None


class MetaAdsRetriesExhausted(MetaAdsError):
    """Throttling, 5xx or transport failures outlasted the bounded retries."""
    reason = "meta_retries_exhausted"


class MetaAdsMalformedResponse(MetaAdsError):
    """A response that cannot be read as what was asked for."""
    reason = "meta_malformed_response"


class MetaAdsPageOverrun(MetaAdsError):
    """Paging did not end within :data:`MAX_PAGES`."""
    reason = "meta_page_overrun"


# ── configuration ─────────────────────────────────────────────────────────

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _config_file() -> dict:
    try:
        import yaml  # noqa: PLC0415
        path = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
        with path.open() as handle:
            return (yaml.safe_load(handle) or {}).get("meta_ads") or {}
    except Exception:  # noqa: BLE001 — unreadable config resolves nothing
        return {}


def resolve_api_version() -> tuple[Optional[str], str]:
    """``(version, source)``. The env var wins; an invalid value resolves None.

    An invalid override does NOT fall back to the configured version: the
    operator asked for something specific and did not get it.
    """
    override = _env("META_API_VERSION")
    if override:
        return (override if _VERSION_RE.match(override) else None), "env"
    configured = str(_config_file().get("api_version") or "").strip()
    if configured:
        return (configured if _VERSION_RE.match(configured) else None), "config"
    return None, "unresolved"


def parse_account_ids(raw: str) -> tuple[list, list]:
    """``(ids, invalid)`` from a comma-separated list.

    Spaces and a leading ``act_`` are removed, empty entries ignored, a
    repeated id kept once, order preserved. ``invalid`` holds every entry that
    is not all digits; one invalid entry makes its credential set invalid.
    """
    ids, invalid = [], []
    for part in str(raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        if entry.lower().startswith("act_"):
            entry = entry[4:].strip()
        if not _ACCOUNT_RE.match(entry):
            invalid.append(part.strip())
        elif entry not in ids:
            ids.append(entry)
    return ids, invalid


def _suffix(number: int) -> str:
    return "" if number == 1 else f"_{number}"


def _credential_sets() -> list:
    """Every credential set that has ANY of its variables set, in order.

    Set 1 is the unsuffixed pair; sets 2..:data:`MAX_CREDENTIAL_SETS` carry
    the suffix ``_2`` ... A set is one token and the accounts it reads.
    INTERNAL: each entry holds the token. Never return this to a caller.
    """
    sets = []
    for number in range(1, MAX_CREDENTIAL_SETS + 1):
        suffix = _suffix(number)
        token = _env(f"META_ADS_ACCESS_TOKEN{suffix}")
        raw_accounts = _env(f"META_AD_ACCOUNT_ID{suffix}")
        secret = _env(f"META_APP_SECRET{suffix}")
        if not token and not raw_accounts:
            continue
        ids, invalid = parse_account_ids(raw_accounts)
        if not token:
            reason = "access_token_missing"
        elif not raw_accounts:
            reason = "ad_account_id_missing"
        elif invalid or not ids:
            reason = "ad_account_id_invalid"
        else:
            reason = None
        sets.append({"set": number, "token": token, "app_secret": secret,
                     "account_ids": ids if reason is None else [],
                     "listed_account_ids": ids, "invalid_entries": invalid,
                     "reason": reason})
    return sets


def parse_retired(raw: str) -> tuple[list, list]:
    """``(declarations, invalid)`` from ``META_AD_ACCOUNT_RETIRED``.

    A comma-separated list of ``<account id>:<YYYY-MM-DD>`` — the owner's
    declaration that the account spent nothing after that day (a day of the
    account's own time zone). An entry that is not exactly that, or an id
    declared twice with different days, is ``invalid`` and is never guessed.
    """
    from datetime import date as _date  # noqa: PLC0415
    seen, invalid = {}, []
    for part in str(raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        account, sep, day = entry.partition(":")
        account = account.strip()
        if account.lower().startswith("act_"):
            account = account[4:].strip()
        day = day.strip()
        try:
            ok = bool(sep) and bool(_ACCOUNT_RE.match(account)) and (
                _date.fromisoformat(day).isoformat() == day)
        except ValueError:
            ok = False
        if not ok or seen.get(account, day) != day:
            invalid.append(entry)
            continue
        seen[account] = day
    for entry in invalid:                  # a contradicted id is not honoured
        seen.pop(entry.partition(":")[0].strip().removeprefix("act_"), None)
    return ([{"account_id": a, "last_spend_day": d} for a, d in seen.items()],
            invalid)


def page_limit() -> int:
    try:
        value = int(_config_file().get("page_limit") or DEFAULT_PAGE_LIMIT)
    except (TypeError, ValueError):
        value = DEFAULT_PAGE_LIMIT
    return value if 1 <= value <= 5000 else DEFAULT_PAGE_LIMIT


def config_status() -> dict:
    """A non-secret description of the configuration. Never a token.

    ``sets`` — one entry per credential set that has any variable set:
    ``{set, has_access_token, has_app_secret, account_ids, configured,
    reason}``. ``account_ids`` — every account of every complete set, in
    order; these are the accounts that sync.

    ``listed_account_ids`` — every well-formed account id listed in ANY set,
    complete or not. An account in a broken set is still an account the owner
    configured: it does not sync, and it is never read as removed.
    ``unparsed_account_entries`` — True when some set's account list holds an
    entry that is not an id, so the configured accounts are not fully known.

    ``retired`` — the owner's declarations (``META_AD_ACCOUNT_RETIRED``):
    ``[{account_id, last_spend_day}]``. ``retired_invalid`` — the entries that
    could not be read; they are ignored, reported, and make the run partial.

    ``configured`` — at least one complete set, an API version resolved, and
    no account listed in two sets. ``misconfigured`` — something is set and
    nothing can sync; ``reason`` says why. ``misconfigured_sets`` — the
    incomplete sets when OTHER sets are complete: those still sync, and the
    run is partial. Nothing set at all: both False.
    """
    sets = _credential_sets()
    version, version_source = resolve_api_version()
    public = [{"set": s["set"], "has_access_token": bool(s["token"]),
               "has_app_secret": bool(s["app_secret"]),
               "account_ids": list(s["listed_account_ids"]),
               "configured": s["reason"] is None, "reason": s["reason"]}
              for s in sets]
    seen, duplicates = {}, []
    for s in sets:
        for account_id in s["listed_account_ids"]:
            if account_id in seen and seen[account_id] != s["set"] \
                    and account_id not in duplicates:
                duplicates.append(account_id)
            seen.setdefault(account_id, s["set"])
    complete = [s for s in sets if s["reason"] is None]
    broken = [e for e in public if not e["configured"]]
    listed = []
    for s in sets:
        listed += [a for a in s["listed_account_ids"] if a not in listed]
    retired, retired_invalid = parse_retired(_env("META_AD_ACCOUNT_RETIRED"))
    out = {"configured": False, "misconfigured": False, "reason": None,
           "sets": public, "account_ids": [], "misconfigured_sets": [],
           "listed_account_ids": listed,
           "unparsed_account_entries": any(s["invalid_entries"] for s in sets),
           "retired": retired, "retired_invalid": retired_invalid,
           "duplicate_account_ids": duplicates,
           "has_access_token": any(e["has_access_token"] for e in public),
           "api_version": version, "api_version_source": version_source}
    if not sets:
        return out
    if duplicates:
        reason = "ad_account_id_in_two_sets"
    elif not complete:
        reason = broken[0]["reason"]
    elif version is None:
        reason = "api_version_unresolved"
    else:
        ids = [a for s in complete for a in s["account_ids"]]
        return {**out, "configured": True, "account_ids": ids,
                "misconfigured_sets": broken}
    return {**out, "misconfigured": True, "reason": reason,
            "misconfigured_sets": broken}


def _require_config() -> dict:
    status = config_status()
    if not status["configured"]:
        raise MetaAdsNotConfigured(
            "Meta Ads is not configured"
            + (f" ({status['reason']})" if status["reason"] else ""))
    return status


def _require_account(account_id) -> str:
    """``account_id`` if it is one of the configured accounts; else raise.
    The connector reads nothing it was not configured to read."""
    account_id = str(account_id or "").strip()
    if account_id not in _require_config()["account_ids"]:
        raise MetaAdsNotConfigured(
            f"ad account {account_id or '(none)'} is not a configured account")
    return account_id


def _credentials_for(account_id: str) -> tuple[str, str]:
    """``(token, app_secret)`` of the ONE set that lists ``account_id``.
    An account is only ever called with its own set's token."""
    account_id = _require_account(account_id)
    for entry in _credential_sets():
        if entry["reason"] is None and account_id in entry["account_ids"]:
            return entry["token"], entry["app_secret"]
    raise MetaAdsNotConfigured(
        f"ad account {account_id} has no credential set")


# ── the one request primitive ─────────────────────────────────────────────

def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _scrub(text) -> str:
    """Response text with every configured secret removed, bounded."""
    out = str(text if text is not None else "")
    for entry in _credential_sets():           # every set's, not only this one's
        for secret in (entry["token"], entry["app_secret"]):
            if secret:
                out = out.replace(secret, "[redacted]")
    # Belt and braces: anything shaped like a token parameter.
    out = re.sub(r"(access_token|appsecret_proof)=[^&\s\"']+", r"\1=[redacted]", out)
    return out[:300]


def _regain_seconds(headers) -> Optional[float]:
    """The largest ``estimated_time_to_regain_access`` (minutes) in the usage
    headers, as seconds; None when no header carries one."""
    best = None
    for name in _USAGE_HEADERS:
        try:
            raw = (headers or {}).get(name)
            payload = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            continue
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                minutes = node.get("estimated_time_to_regain_access")
                if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
                    best = max(best or 0.0, float(minutes) * 60.0)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return best


def _wait_seconds(attempt: int, headers=None) -> float:
    wait = float(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    regain = _regain_seconds(headers)
    if regain is not None:
        wait = max(wait, regain)
    return min(wait, float(MAX_WAIT_SECONDS))


def _int_or_none(value):
    """An integer error code, or None. Meta's codes are integers; anything
    else is not interpolated into a message or stored."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _describe(status: int, error: dict) -> str:
    """``HTTP 400, code 100, subcode 33, type OAuthException: message``."""
    parts = [f"HTTP {status}", f"code {_int_or_none(error.get('code'))}"]
    subcode = _int_or_none(error.get("error_subcode"))
    if subcode is not None:
        parts.append(f"subcode {subcode}")
    if error.get("type"):
        parts.append(f"type {_scrub(error.get('type'))}")
    text = ", ".join(parts)
    if error.get("message"):
        text += f": {_scrub(error.get('message'))}"
    return text


def _request(account_id: str, edge: str = "", params: Optional[dict] = None, *,
             method: str = "GET", timeout: int = DEFAULT_TIMEOUT_SECONDS,
             stats: Optional[dict] = None) -> dict:
    """One read-only GET of ``act_<account_id>[/edge]``. Returns the decoded
    JSON object. The request carries the token of the credential set that
    lists ``account_id``, and no other.

    Retried, at most :data:`MAX_ATTEMPTS` attempts with backoff: transport
    errors, HTTP 5xx, HTTP 429, the throttle codes, and errors Meta marks
    ``is_transient``. Refused at once: code 190 (:class:`MetaAdsAuthError`)
    and every other 4xx (:class:`MetaAdsRefused`).
    """
    if str(method).upper() != "GET":
        raise MetaAdsError(
            f"the Meta Ads connector is read-only: refusing HTTP {method}")
    config = _require_config()
    token, secret = _credentials_for(account_id)
    path = f"act_{account_id}" + (f"/{str(edge).strip('/')}" if edge else "")
    url = f"{GRAPH_HOST}/{config['api_version']}/{path}"
    query = dict(params or {})
    if secret:
        query["appsecret_proof"] = hmac.new(
            secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    last = "no attempt was made"
    last_status = last_code = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, params=query, headers=headers,
                                    timeout=timeout, allow_redirects=False)
        except requests.exceptions.RequestException as exc:
            # The exception's text contains the URL: name its class only.
            last, last_status, last_code = (
                f"transport error ({type(exc).__name__})", None, None)
            if attempt < MAX_ATTEMPTS:
                wait = _wait_seconds(attempt)
                logger.warning("[meta_ads] %s — retry %d/%d in %.0fs",
                               last, attempt, MAX_ATTEMPTS, wait)
                _sleep(wait)
            continue

        status = int(response.status_code)
        if stats is not None:
            stats["http_status"] = status
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else None

        if status == 200 and error is None:
            if not isinstance(body, dict):
                raise MetaAdsMalformedResponse(
                    "Meta returned HTTP 200 with a body that is not a JSON object",
                    http_status=status)
            return body

        code = _int_or_none(error.get("code")) if error else None
        subcode = _int_or_none(error.get("error_subcode")) if error else None
        described = _describe(status, error) if error else f"HTTP {status}, code None"
        if code == AUTH_ERROR_CODE:
            raise MetaAdsAuthError(
                f"Meta access token expired or invalid (HTTP {status}, code "
                f"{AUTH_ERROR_CODE}, subcode {subcode})",
                http_status=status, code=code, subcode=subcode)
        if code == DEPRECATED_VERSION_CODE:
            named = re.search(r"\bv\d{1,3}\.\d{1,2}\b",
                              str(error.get("message") or ""))
            failure = MetaAdsVersionDeprecated(
                f"Meta API version {config['api_version']} is deprecated; "
                + (f"Meta says upgrade to {named.group(0)}" if named
                   else "Meta did not name a version to upgrade to")
                + f" (HTTP {status}, code {code})",
                http_status=status, code=code, subcode=subcode)
            failure.upgrade_to = named.group(0) if named else None
            raise failure
        retryable = (status >= 500 or status == 429 or code in THROTTLE_CODES
                     or bool(error and error.get("is_transient") is True))
        if not retryable:
            raise MetaAdsRefused(f"Meta API refused the request: {described}",
                                 http_status=status, code=code, subcode=subcode)
        last, last_status, last_code = described, status, code
        if attempt < MAX_ATTEMPTS:
            wait = _wait_seconds(attempt, response.headers)
            logger.warning("[meta_ads] %s — retry %d/%d in %.0fs",
                           f"HTTP {status}, code {code}", attempt, MAX_ATTEMPTS, wait)
            _sleep(wait)

    raise MetaAdsRetriesExhausted(
        f"Meta API still failing after {MAX_ATTEMPTS} attempts: {last}",
        http_status=last_status, code=last_code)


# ── reads ─────────────────────────────────────────────────────────────────

def fetch_account(account_id: str) -> dict:
    """One configured ad account: id, name, currency, time zone, status."""
    wanted = _require_account(account_id)
    body = _request(wanted, "", {"fields": ACCOUNT_FIELDS})
    account_id = str(body.get("account_id") or "").strip()
    if not account_id and str(body.get("id") or "").startswith("act_"):
        account_id = str(body["id"])[4:]
    if account_id != wanted:
        raise MetaAdsMalformedResponse(
            "the account Meta returned is not the account asked for")
    currency = str(body.get("currency") or "").strip().upper()
    zone = str(body.get("timezone_name") or "").strip()
    if not currency or not zone:
        raise MetaAdsMalformedResponse(
            "the account response carries no currency or no time zone")
    offset = body.get("timezone_offset_hours_utc")
    status = body.get("account_status")
    return {
        "account_id": account_id,
        "account_name": (str(body.get("name")).strip() or None)
        if body.get("name") is not None else None,
        "currency_code": currency,
        "time_zone_name": zone,
        "timezone_offset_hours_utc": (float(offset)
                                      if isinstance(offset, (int, float))
                                      and not isinstance(offset, bool) else None),
        "account_status": (int(status) if isinstance(status, int)
                           and not isinstance(status, bool) else None),
    }


def spend_to_micros(spend) -> int:
    """``Decimal(spend) * 1_000_000`` as an exact integer. Never a float.

    Raises :class:`MetaAdsMalformedResponse` for a missing, non-numeric,
    negative or sub-micro value: an unreadable amount is not a zero.
    """
    if spend is None or isinstance(spend, (bool, float)):
        raise MetaAdsMalformedResponse(
            f"spend is not a decimal string: {type(spend).__name__}")
    try:
        micros = Decimal(str(spend).strip()) * _MICROS
    except (InvalidOperation, ValueError):
        raise MetaAdsMalformedResponse("spend is not a decimal string") from None
    if not micros.is_finite() or micros < 0 or micros != micros.to_integral_value():
        raise MetaAdsMalformedResponse(
            "spend is negative, not finite or finer than one micro")
    return int(micros)


def _count(value) -> Optional[int]:
    try:
        return int(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _day(row: dict) -> str:
    start, stop = str(row.get("date_start") or ""), str(row.get("date_stop") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or start != stop:
        raise MetaAdsMalformedResponse(
            "an Insights row does not describe exactly one day")
    return start


def _pages(account_id: str, edge: str, params: dict,
           stats: Optional[dict] = None) -> Iterator[list]:
    """Every page of one list query, as lists of raw rows.

    Ends only on a page with no ``paging.next``. The next page is requested
    with the SAME parameters plus ``after``; ``paging.next`` itself is never
    used. Raises on a 200 without a ``data`` list, a page that promises more
    without a usable cursor, and more than :data:`MAX_PAGES` pages.
    """
    stats = stats if stats is not None else {}
    stats.setdefault("pages_read", 0)
    after = None
    while True:
        if stats["pages_read"] >= MAX_PAGES:
            raise MetaAdsPageOverrun(
                f"Meta paging did not end within {MAX_PAGES} pages")
        query = dict(params)
        if after:
            query["after"] = after
        body = _request(account_id, edge, query, stats=stats)
        data = body.get("data")
        if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
            raise MetaAdsMalformedResponse(
                "Meta returned HTTP 200 without a list of rows",
                http_status=stats.get("http_status"))
        stats["pages_read"] += 1
        yield data
        paging = body.get("paging") if isinstance(body.get("paging"), dict) else {}
        if not paging.get("next"):
            return
        cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
        following = cursors.get("after")
        if not following or following == after:
            raise MetaAdsMalformedResponse(
                "Meta reports another page but gives no cursor that advances")
        after = str(following)


def _insight_pages(account_id: str, level: str, fields: str, start: str,
                   end: str, stats: Optional[dict] = None) -> Iterator[list]:
    """Every page of one Insights query for one configured account."""
    account_id = _require_account(account_id)
    params = {
        "level": level, "time_increment": 1, "fields": fields,
        "time_range": json.dumps({"since": str(start), "until": str(end)},
                                 separators=(",", ":")),
        "limit": page_limit(),
    }
    return _pages(account_id, "insights", params, stats)


def count_active_campaigns(account_id: str) -> int:
    """How many of the account's campaigns are ACTIVE right now (smoke test).

    Counts the rows of a paged ``campaigns`` read; nothing is stored.
    """
    account_id = _require_account(account_id)
    params = {"fields": "id", "limit": page_limit(),
              "effective_status": json.dumps(["ACTIVE"])}
    return sum(len(page) for page in _pages(account_id, "campaigns", params))


def iter_campaign_daily_spend(account_id: str, start: str, end: str,
                              stats: Optional[dict] = None) -> Iterator[list]:
    """Campaign-day spend of one account for ``[start, end]`` (that account's
    local days), a page at a time. Each row: ``{account_id, campaign_id, campaign_name, spend_date,
    cost_micros, spend_raw, currency_code, impressions, clicks}``.

    Raises on a campaign-day that appears twice across the pages, on a day
    outside the range, and on any row that cannot be read.
    """
    account_id = _require_account(account_id)
    seen: set = set()
    for data in _insight_pages(account_id, "campaign", CAMPAIGN_FIELDS, start,
                               end, stats):
        page = []
        for raw in data:
            campaign_id = str(raw.get("campaign_id") or "").strip()
            if not campaign_id:
                raise MetaAdsMalformedResponse("an Insights row has no campaign_id")
            day = _day(raw)
            if not (str(start) <= day <= str(end)):
                raise MetaAdsMalformedResponse(
                    f"an Insights row is dated {day}, outside {start}..{end}")
            if (campaign_id, day) in seen:
                raise MetaAdsMalformedResponse(
                    f"campaign {campaign_id} on {day} was returned twice")
            seen.add((campaign_id, day))
            name = raw.get("campaign_name")
            page.append({
                "account_id": account_id, "campaign_id": campaign_id,
                "campaign_name": (str(name).strip() or None) if name is not None else None,
                "spend_date": day,
                "cost_micros": spend_to_micros(raw.get("spend")),
                "spend_raw": str(raw.get("spend")),
                "currency_code": (str(raw.get("account_currency") or "").strip().upper()
                                  or None),
                "impressions": _count(raw.get("impressions")),
                "clicks": _count(raw.get("clicks")),
            })
        yield page


def fetch_account_daily_spend(account_id: str, start: str, end: str,
                              stats: Optional[dict] = None) -> list:
    """The account-level total per day: ``[{spend_date, cost_micros,
    currency_code}]``. The reconciliation total for the campaign rows."""
    out, seen = [], set()
    for data in _insight_pages(account_id, "account", ACCOUNT_DAY_FIELDS, start,
                               end, stats):
        for raw in data:
            day = _day(raw)
            if not (str(start) <= day <= str(end)) or day in seen:
                raise MetaAdsMalformedResponse(
                    f"account day {day} is outside {start}..{end} or repeated")
            seen.add(day)
            out.append({
                "spend_date": day,
                "cost_micros": spend_to_micros(raw.get("spend")),
                "currency_code": (str(raw.get("account_currency") or "").strip().upper()
                                  or None),
            })
    return out
