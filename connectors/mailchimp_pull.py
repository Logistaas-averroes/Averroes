"""
Mailchimp Marketing API Connector — READ ONLY (PR-ADS-151).

Pull-only email-marketing evidence from the Logistaas Mailchimp account.

GOVERNANCE (non-negotiable):
  - Mailchimp access is PULL ONLY. The only HTTP method this module ever issues
    is GET. There is no create/edit/send/schedule/cancel path, no audience or
    member mutation, no tag/segment change, and no delete. ``_request`` hard-fails
    on any non-GET method so a future accidental write cannot slip through.
  - Credentials come from server-side environment variables and are NEVER
    returned to callers, logged, or exposed through the API/frontend.

Credentials (env):
  MAILCHIMP_API_KEY        — Marketing API key. This is the ONLY required
                             variable: a present key with a valid data-centre
                             suffix (…-usXX) both enables the connector and lets
                             it derive the server prefix. Key absent → the
                             connector reports "not configured" and never touches
                             the network.
  MAILCHIMP_SERVER_PREFIX  — OPTIONAL explicit data-centre prefix (e.g. "us21").
                             Only needed to override the key-derived prefix.

Read-only sources exposed here (all GET):
  - ping / account root (connection test);
  - campaigns (list);
  - campaign reports (per-campaign summary);
  - campaign open summary (from the report — no per-member PII pull);
  - click / link details;
  - unsubscribe count (from the report — no per-member PII pull);
  - audiences / lists + audience summary / member-status counts;
  - sent-to member identities (audit only — email_id / status; used by the
    attribution feasibility audit, never persisted with raw addresses).

Personal-data minimisation: routine syncs pull only aggregate report + list
metrics. Per-recipient endpoints (sent-to) are used ONLY by the on-demand
attribution audit and never store raw email addresses.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

MAILCHIMP_API_VERSION = "3.0"
SOURCE_SYSTEM = "mailchimp"

# Retry / rate-limit handling — mirrors the HubSpot/Windsor connector doctrine.
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 2
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_PAGE_SIZE = 200        # Mailchimp caps count at 1000; 200 is safe + polite
MAX_PAGES = 10_000             # hard runaway guard for pagination loops


class MailchimpError(RuntimeError):
    """Base class for Mailchimp connector failures."""


class MailchimpNotConfigured(MailchimpError):
    """The API key is missing, or its data-centre prefix could not be resolved."""


class MailchimpAuthError(MailchimpError):
    """Authentication/authorisation failure (401/403) — permission denied."""


class MailchimpRateLimited(MailchimpError):
    """Rate limited (429) and retries were exhausted."""


class MailchimpRetryableError(MailchimpError):
    """Transient failure (5xx / network) that exhausted retries — never treated
    as an empty result so a sync failure is honestly reported."""


# ── Configuration / credential handling ──────────────────────────────────────

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def is_enabled() -> bool:
    """True when Mailchimp is usable — i.e. an API key is set AND a data-centre
    prefix is resolvable from it (or from an explicit override).

    The API key is the single required variable; MAILCHIMP_ENABLED is no longer
    used (a present, valid key is what enables the connector).
    """
    return bool(_api_key() and derive_server_prefix())


def _api_key() -> str:
    return _env("MAILCHIMP_API_KEY")


def derive_server_prefix(api_key: Optional[str] = None,
                         explicit_prefix: Optional[str] = None) -> Optional[str]:
    """Resolve the Mailchimp data-centre prefix (e.g. "us21").

    Precedence:
      1. an explicit prefix argument / MAILCHIMP_SERVER_PREFIX env var;
      2. the suffix of the API key after the final "-" (Mailchimp keys look like
         ``<hex>-us21``).
    Returns None when neither yields a syntactically valid ``[a-z]+[0-9]+`` prefix
    (fail closed — never guess a data centre).
    """
    prefix = (explicit_prefix if explicit_prefix is not None
              else _env("MAILCHIMP_SERVER_PREFIX"))
    prefix = (prefix or "").strip().lower()
    if not prefix and (api_key is not None or _api_key()):
        key = (api_key if api_key is not None else _api_key()).strip()
        if "-" in key:
            prefix = key.rsplit("-", 1)[-1].strip().lower()
    if not prefix:
        return None
    # Validate shape: letters followed by digits (e.g. us21, us6). Reject anything
    # else so a malformed key can never produce a bogus hostname.
    import re  # noqa: PLC0415
    return prefix if re.fullmatch(r"[a-z]+[0-9]+", prefix) else None


def base_url(prefix: Optional[str] = None) -> Optional[str]:
    """Return the API base URL for the resolved data centre, or None."""
    p = prefix or derive_server_prefix()
    if not p:
        return None
    return f"https://{p}.api.mailchimp.com/{MAILCHIMP_API_VERSION}"


def config_status() -> dict:
    """Non-secret snapshot of connector configuration.

    NEVER includes the API key. ``server_prefix`` is safe to expose (it is only a
    data-centre identifier, not a credential).
    """
    prefix = derive_server_prefix()
    has_key = bool(_api_key())
    configured = bool(has_key and prefix)
    return {
        # A valid key alone (with a resolvable prefix) makes the connector both
        # enabled and configured — these are equal by design now.
        "enabled": configured,
        "has_api_key": has_key,
        "server_prefix": prefix,
        "server_prefix_source": (
            "explicit" if _env("MAILCHIMP_SERVER_PREFIX") else
            ("derived_from_key" if prefix else "unresolved")
        ),
        "configured": configured,
    }


def _require_config() -> tuple[str, str]:
    """Return (base_url, api_key) or raise MailchimpNotConfigured. Never logs the key."""
    key = _api_key()
    if not key:
        raise MailchimpNotConfigured("MAILCHIMP_API_KEY is not set")
    burl = base_url()
    if not burl:
        raise MailchimpNotConfigured(
            "Mailchimp server prefix could not be resolved — use an API key with a "
            "-usXX suffix or set the optional MAILCHIMP_SERVER_PREFIX override")
    return burl, key


# ── Core request primitive (GET only) ────────────────────────────────────────

def _request(path: str, params: Optional[dict] = None, *, method: str = "GET",
             timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Issue a single read-only GET against the Mailchimp API and return JSON.

    Hard governance guard: ``method`` MUST be "GET". Any other verb raises
    immediately (defence in depth against an accidental future write).

    Retries transient 5xx / network errors and honours 429 ``Retry-After`` with
    exponential backoff. Raises a typed MailchimpError on exhaustion so callers
    never mistake a failure for an empty pull. The API key is sent via HTTP basic
    auth (username is arbitrary) and is never placed in the URL or logged.
    """
    if method.upper() != "GET":
        raise MailchimpError(
            f"Mailchimp connector is read-only: refusing HTTP {method} to {path}")

    burl, key = _require_config()
    url = f"{burl}/{path.lstrip('/')}"

    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                auth=("anystring", key),   # Mailchimp basic-auth: any username + key
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("Mailchimp network error on %s — retry %d/%d in %ds: %s",
                               path, attempt, MAX_RETRIES, wait, exc)
                time.sleep(wait)
                continue
            raise MailchimpRetryableError(f"network error calling {path}: {exc}") from exc

        status = resp.status_code
        if status == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise MailchimpRetryableError(
                    f"invalid JSON from {path}: {exc}") from exc

        if status in (401, 403):
            # Permission denied — never retry, never leak the key.
            raise MailchimpAuthError(
                f"Mailchimp authentication/permission failure ({status}) on {path}")

        if status == 429:
            if attempt < MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = int(retry_after) if retry_after else (
                        INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                except (TypeError, ValueError):
                    wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("Mailchimp rate limited (429) on %s — retry %d/%d in %ds",
                               path, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            raise MailchimpRateLimited(f"rate limited (429) on {path} after {MAX_RETRIES} attempts")

        if 500 <= status < 600:
            last_exc = MailchimpRetryableError(f"server error {status} on {path}")
            if attempt < MAX_RETRIES:
                wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("Mailchimp %d on %s — retry %d/%d in %ds",
                               status, path, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            raise MailchimpRetryableError(f"server error {status} on {path} after {MAX_RETRIES} attempts")

        # Any other 4xx — a real, non-retryable client error.
        raise MailchimpError(f"Mailchimp request to {path} failed with HTTP {status}")

    # Unreachable, but keep the type-checker + runtime honest.
    raise MailchimpRetryableError(
        f"exhausted retries calling {path}: {last_exc}")


def _paginate(path: str, root_key: str, *, params: Optional[dict] = None,
              page_size: int = DEFAULT_PAGE_SIZE) -> list:
    """Yield-collect all items across offset pagination for a list endpoint.

    Mailchimp list endpoints return ``{root_key: [...], total_items: N}`` and
    accept ``count`` + ``offset``. Loops until fewer than ``count`` items come
    back or ``total_items`` is reached. Bounded by MAX_PAGES.
    """
    out: list = []
    offset = 0
    base_params = dict(params or {})
    for _page in range(MAX_PAGES):
        page_params = dict(base_params)
        page_params["count"] = page_size
        page_params["offset"] = offset
        payload = _request(path, params=page_params)
        items = payload.get(root_key) or []
        out.extend(items)
        total = payload.get("total_items")
        got = len(items)
        offset += got
        if got < page_size:
            break
        if isinstance(total, int) and offset >= total:
            break
    return out


# ── Connection test ──────────────────────────────────────────────────────────

def ping() -> dict:
    """Connection test via the Mailchimp API root/ping resource.

    Returns ``{ok, health_status}`` on success. Raises a typed MailchimpError on
    failure (not-configured / permission-denied / rate-limited / transient) so
    the caller can map it to the right status state. Never returns the key.
    """
    payload = _request("ping")
    return {"ok": True, "health_status": payload.get("health_status")}


def get_account() -> dict:
    """Account-root metadata (GET /). Read-only; contains no credentials."""
    payload = _request("")
    return {
        "account_id": payload.get("account_id"),
        "account_name": payload.get("account_name"),
        "email": None,  # deliberately not surfaced — avoid exposing personal data
        "total_subscribers": payload.get("total_subscribers"),
        "pro_enabled": payload.get("pro_enabled"),
        "last_login": payload.get("last_login"),
    }


# ── Campaigns ────────────────────────────────────────────────────────────────

_CAMPAIGN_FIELDS = ",".join([
    "campaigns.id", "campaigns.web_id", "campaigns.type", "campaigns.status",
    "campaigns.create_time", "campaigns.send_time", "campaigns.emails_sent",
    "campaigns.settings.subject_line", "campaigns.settings.title",
    "campaigns.recipients.list_id", "campaigns.recipients.recipient_count",
    "campaigns.report_summary", "total_items",
])


def list_campaigns(*, since_send_time: Optional[str] = None,
                   before_send_time: Optional[str] = None,
                   page_size: int = DEFAULT_PAGE_SIZE) -> list:
    """List campaigns (GET /campaigns), newest first, with report summaries.

    ``since_send_time`` / ``before_send_time`` are ISO-8601 strings used for
    incremental pulls. Returns raw normalised campaign dicts.
    """
    params: dict[str, Any] = {
        "sort_field": "send_time",
        "sort_dir": "DESC",
        "fields": _CAMPAIGN_FIELDS,
    }
    if since_send_time:
        params["since_send_time"] = since_send_time
    if before_send_time:
        params["before_send_time"] = before_send_time
    raw = _paginate("campaigns", "campaigns", params=params, page_size=page_size)
    return [normalize_campaign(c) for c in raw]


def normalize_campaign(c: dict) -> dict:
    """Flatten a raw Mailchimp campaign into durable-fact fields (no PII)."""
    settings = c.get("settings") or {}
    recipients = c.get("recipients") or {}
    summary = c.get("report_summary") or {}
    return {
        "campaign_id": c.get("id"),
        "web_id": c.get("web_id"),
        "list_id": recipients.get("list_id"),
        "campaign_type": c.get("type"),
        "status": c.get("status"),
        "subject_line": settings.get("subject_line"),
        "title": settings.get("title"),
        "create_time": c.get("create_time"),
        "send_time": c.get("send_time"),
        "emails_sent": c.get("emails_sent"),
        "recipient_count": recipients.get("recipient_count"),
        "report_summary": summary or None,
        "source_system": SOURCE_SYSTEM,
    }


def get_campaign_report(campaign_id: str) -> dict:
    """Per-campaign report summary (GET /reports/{id}) → durable metric fields.

    Includes opens, unique opens, clicks, unique clicks, hard/soft bounces,
    unsubscribes, abuse reports, and a delivered estimate (emails_sent − bounces).
    No per-member data is pulled here.
    """
    payload = _request(f"reports/{campaign_id}")
    return normalize_report(payload)


def normalize_report(r: dict) -> dict:
    """Flatten a raw Mailchimp report into durable metric fields (no PII)."""
    opens = r.get("opens") or {}
    clicks = r.get("clicks") or {}
    bounces = r.get("bounces") or {}
    fwd = r.get("forwards") or {}
    emails_sent = _int(r.get("emails_sent"))
    hard = _int(bounces.get("hard_bounces"))
    soft = _int(bounces.get("soft_bounces"))
    syntax = _int(bounces.get("syntax_errors"))
    delivered_estimate = None
    if emails_sent is not None:
        delivered_estimate = emails_sent - (hard or 0) - (soft or 0) - (syntax or 0)
    return {
        "campaign_id": r.get("id"),
        "list_id": r.get("list_id"),
        "campaign_type": r.get("type"),
        "campaign_title": r.get("campaign_title"),
        "subject_line": r.get("subject_line"),
        "send_time": r.get("send_time"),
        "emails_sent": emails_sent,
        "delivered_estimate": delivered_estimate,
        "opens_total": _int(opens.get("opens_total")),
        "unique_opens": _int(opens.get("unique_opens")),
        "open_rate": _float(opens.get("open_rate")),
        "last_open": opens.get("last_open"),
        "clicks_total": _int(clicks.get("clicks_total")),
        "unique_clicks": _int(clicks.get("unique_clicks")),
        "unique_subscriber_clicks": _int(clicks.get("unique_subscriber_clicks")),
        "click_rate": _float(clicks.get("click_rate")),
        "last_click": clicks.get("last_click"),
        "hard_bounces": hard,
        "soft_bounces": soft,
        "syntax_errors": syntax,
        "unsubscribes": _int(r.get("unsubscribed")),
        "abuse_reports": _int(r.get("abuse_reports")),
        "forwards_count": _int(fwd.get("forwards_count")),
        "source_system": SOURCE_SYSTEM,
    }


def get_campaign_link_details(campaign_id: str,
                              page_size: int = DEFAULT_PAGE_SIZE) -> list:
    """Click / link details (GET /reports/{id}/click-details) → per-URL rows.

    Aggregate click metrics per link — no per-member data.
    """
    raw = _paginate(f"reports/{campaign_id}/click-details", "urls_clicked",
                    page_size=page_size)
    return [normalize_link(campaign_id, u) for u in raw]


def normalize_link(campaign_id: str, u: dict) -> dict:
    return {
        "campaign_id": campaign_id,
        "link_id": u.get("id"),
        "url": u.get("url"),
        "total_clicks": _int(u.get("total_clicks")),
        "click_percentage": _float(u.get("click_percentage")),
        "unique_clicks": _int(u.get("unique_clicks")),
        "unique_click_percentage": _float(u.get("unique_click_percentage")),
        "source_system": SOURCE_SYSTEM,
    }


# ── Audiences / lists ─────────────────────────────────────────────────────────

def list_audiences(page_size: int = DEFAULT_PAGE_SIZE) -> list:
    """List audiences/lists (GET /lists) with member-status counts."""
    raw = _paginate("lists", "lists", page_size=page_size)
    return [normalize_audience(a) for a in raw]


def normalize_audience(a: dict) -> dict:
    """Flatten a raw list into durable audience-summary fields (no member PII)."""
    stats = a.get("stats") or {}
    return {
        "list_id": a.get("id"),
        "list_name": a.get("name"),
        "date_created": a.get("date_created"),
        "member_count": _int(stats.get("member_count")),
        "unsubscribe_count": _int(stats.get("unsubscribe_count")),
        "cleaned_count": _int(stats.get("cleaned_count")),
        "member_count_since_send": _int(stats.get("member_count_since_send")),
        "pending_count": _int(stats.get("pending_count")),
        "total_contacts": _int(a.get("stats", {}).get("total_contacts")
                               if isinstance(a.get("stats"), dict) else None),
        "open_rate": _float(stats.get("open_rate")),
        "click_rate": _float(stats.get("click_rate")),
        "campaign_count": _int(stats.get("campaign_count")),
        "source_system": SOURCE_SYSTEM,
    }


# ── Sent-to member identities (AUDIT ONLY) ────────────────────────────────────

def get_campaign_sent_to(campaign_id: str,
                         page_size: int = DEFAULT_PAGE_SIZE) -> list:
    """Recipient member identities for a campaign (GET /reports/{id}/sent-to).

    AUDIT USE ONLY. Returns each recipient's durable Mailchimp member identity
    (``email_id`` = MD5 of the lowercased email) and status. The MD5 is a stable,
    privacy-preserving join key: it matches HubSpot contacts without ever
    transporting or persisting the raw address. Raw ``email_address`` is dropped
    here on purpose so it never leaves the connector boundary.
    """
    raw = _paginate(f"reports/{campaign_id}/sent-to", "sent_to", page_size=page_size)
    out = []
    for m in raw:
        out.append({
            "campaign_id": campaign_id,
            "member_id": m.get("email_id"),   # MD5(lower(email)) — durable identity
            "list_id": m.get("list_id"),
            "status": m.get("status"),
            "is_unique_open": m.get("open_count", 0) and int(m.get("open_count") or 0) > 0,
            # NOTE: raw email_address intentionally NOT included.
        })
    return out


# ── Small coercion helpers ────────────────────────────────────────────────────

def _int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger.info("Mailchimp config: %s", config_status())
    try:
        logger.info("Ping: %s", ping())
        camps = list_campaigns(page_size=50)
        logger.info("Pulled %d campaigns", len(camps))
    except MailchimpError as exc:
        logger.error("Mailchimp connectivity check failed: %s", exc)
