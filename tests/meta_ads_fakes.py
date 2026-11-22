"""PR-ADS-171A — a fake Meta Graph API at the ``requests.get`` seam.

It stands where ``requests.get`` stands and behaves as the Graph API is
documented to:

* it serves ``/{version}/act_{id}``, ``/{version}/act_{id}/insights`` and
  ``/{version}/act_{id}/campaigns`` for every ad account it holds;
* it honours ``level``, ``time_range``, ``limit`` and ``after``;
* every page but the last carries ``paging.next`` — a full URL with the
  ACCESS TOKEN in it, as Meta's does — and ``paging.cursors.after``;
* the account-level rows are the per-day sum of every campaign row an account
  holds, including rows it has been told not to return at campaign level
  (``hidden``), which is how a campaign missing from ``level=campaign`` would
  look. A day with no delivery has no row at either level.

Each account belongs to ONE access token (its credential set's). The fake
FAILS the request (AssertionError) if any token it knows is anywhere but the
``Authorization`` header, if an account is called with another account's
token, or if the URL requested is one it handed out as ``paging.next``.

Identities are production-shaped: all-digit account ids, 18-digit campaign
ids, decimal-string spend. The default account is the production one as Meta
described it on 2026-11-22: currency GBP, time zone America/Los_Angeles, API
version v27.0.
"""
from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlparse

import requests

SENTINEL_TOKEN = "EAAGm0PX4ZCpsBOSENTINELTOKEN9f3c2a71ZDZD"
SENTINEL_TOKEN_2 = "EAAJx7Qw2ZBkYBOSECONDSENTINEL51d8e0b4ZDZD"
ACCOUNT_ID = "4730305973961806"            # the new account (set 1)
OLD_ACCOUNT_ID = "944370805110952"         # the old account (its own token)
VERSION = "v27.0"

CAMPAIGN_A = "120211234567890101"
CAMPAIGN_B = "120211234567890102"
CAMPAIGN_C = "120211234567890103"


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def error_response(status, code, *, subcode=None, message="(#%s) error",
                   type_="OAuthException", transient=None, headers=None):
    error = {"message": message % code if "%s" in message else message,
             "type": type_, "code": code, "fbtrace_id": "AbCdEfGhIjK"}
    if subcode is not None:
        error["error_subcode"] = subcode
    if transient is not None:
        error["is_transient"] = transient
    return FakeResponse(status, {"error": error}, headers=headers)


class FakeAccount:
    """One ad account: what Meta holds for it, and the token that reads it."""

    def __init__(self, account_id, *, currency, zone, name, token, offset=-8):
        self.account_id = account_id
        self.token = token
        self.account = {"account_id": account_id, "name": name,
                        "currency": currency, "timezone_name": zone,
                        "timezone_offset_hours_utc": offset, "account_status": 1,
                        "id": f"act_{account_id}"}
        # {(campaign_id, "YYYY-MM-DD"): {"name", "spend", "impressions", "clicks"}}
        self.rows: dict = {}
        self.hidden: set = set()      # held, but not returned at level=campaign
        self.active_campaigns: list = []
        # Served for EVERY request on this account while set (a suspended or
        # closed account, a removed permission).
        self.refusal = None

    def spend(self, campaign_id, day, spend, name="Lead Gen — MENA",
              impressions=1000, clicks=25):
        self.rows[(campaign_id, str(day))] = {
            "name": name, "spend": str(spend),
            "impressions": str(impressions), "clicks": str(clicks)}
        if campaign_id not in self.active_campaigns:
            self.active_campaigns.append(campaign_id)
        return self

    def remove(self, campaign_id, day):
        self.rows.pop((campaign_id, str(day)), None)
        return self


class FakeGraph:
    """``monkeypatch.setattr(requests, "get", graph.get)``.

    Built with one account (the default). ``add_account`` adds another, by
    default under a SECOND token. ``graph.rows`` / ``hidden`` / ``account`` /
    ``spend`` / ``remove`` are the default account's.
    """

    def __init__(self, *, currency="GBP", zone="America/Los_Angeles",
                 account_id=ACCOUNT_ID, name="Logistaas Limited",
                 token=SENTINEL_TOKEN):
        self.accounts: dict = {}
        self.default = self.add_account(account_id, currency=currency, zone=zone,
                                        name=name, token=token, offset=-8)
        self.account_id = account_id
        self.calls: list = []
        self.queue: list = []             # responses/exceptions served first
        self.max_limit = 500
        self.endless = False              # every page promises another
        self.repeat_first_row = False     # page 2 repeats a row of page 1
        self.no_data_key = False          # 200 with no `data`
        self.next_urls: list = []

    def add_account(self, account_id, *, currency="USD", zone="Asia/Amman",
                    name="Logistaas", token=SENTINEL_TOKEN_2,
                    offset=3) -> FakeAccount:
        account = FakeAccount(account_id, currency=currency, zone=zone, name=name,
                              token=token, offset=offset)
        self.accounts[account_id] = account
        return account

    # ── the default account ────────────────────────────────────────────
    @property
    def rows(self):
        return self.default.rows

    @property
    def hidden(self):
        return self.default.hidden

    @property
    def account(self):
        return self.default.account

    @property
    def token(self):
        return self.default.token

    @property
    def tokens(self):
        return sorted({a.token for a in self.accounts.values()})

    def spend(self, *args, **kwargs):
        self.default.spend(*args, **kwargs)
        return self

    def remove(self, *args, **kwargs):
        self.default.remove(*args, **kwargs)
        return self

    # ── the seam ───────────────────────────────────────────────────────
    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        params = dict(params or {})
        headers = dict(headers or {})
        path = urlparse(url).path
        parts = path.strip("/").split("/")
        account_id = (parts[1][4:] if len(parts) > 1
                      and parts[1].startswith("act_") else None)
        self.calls.append({"url": url, "params": params, "headers": headers,
                           "kwargs": kwargs, "account_id": account_id})
        for token in self.tokens:
            assert token not in url, "an access token is in the URL"
            assert all(token not in str(v) and token not in str(k)
                       for k, v in params.items()), \
                "an access token is in the query"
        assert "access_token" not in params, "an access_token parameter was sent"
        assert url not in self.next_urls and "access_token" not in url, \
            "paging.next was requested"
        assert timeout, "a request without a timeout can hang the sync"
        account = self.accounts.get(account_id)
        assert account is not None, f"an account Meta was not asked about: {path}"
        assert headers.get("Authorization") == f"Bearer {account.token}", \
            f"account {account_id} was not called with its own token"

        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if account.refusal is not None:
            return account.refusal

        if parts[0] != VERSION:
            return error_response(
                400, 2635, message="(#2635) You are calling a deprecated version "
                                   "of the Ads API. Please upgrade to the latest "
                                   f"version: {VERSION}.")
        edge = parts[2] if len(parts) > 2 else ""
        if edge == "":
            wanted = str(params.get("fields") or "").split(",")
            return FakeResponse(200, {k: v for k, v in account.account.items()
                                      if k in wanted or k == "id"})
        if edge == "insights":
            return self._insights(account, params)
        if edge == "campaigns":
            assert json.loads(params["effective_status"]) == ["ACTIVE"]
            return self._page(account, "campaigns", params,
                              [{"id": c} for c in account.active_campaigns])
        return error_response(400, 100, subcode=33,
                              message="Unsupported get request.",
                              type_="GraphMethodException")

    def _insights(self, account, params):
        assert str(params.get("time_increment")) == "1"
        window = json.loads(params["time_range"])
        since, until = window["since"], window["until"]
        level = params.get("level")
        fields = str(params.get("fields") or "").split(",")
        currency = account.account["currency"]
        if self.no_data_key:
            return FakeResponse(200, {"paging": {}})
        if level == "account":
            by_day = defaultdict(Decimal)
            for (_, day), row in account.rows.items():
                if since <= day <= until:
                    by_day[day] += Decimal(row["spend"])
            data = [{"spend": str(total), "date_start": day, "date_stop": day,
                     "account_currency": currency}
                    for day, total in sorted(by_day.items())]
        else:
            assert level == "campaign", level
            keys = sorted(((day, cid) for (cid, day) in account.rows
                           if since <= day <= until
                           and (cid, day) not in account.hidden))
            data = []
            for day, cid in keys:
                row = account.rows[(cid, day)]
                full = {"campaign_id": cid, "campaign_name": row["name"],
                        "spend": row["spend"], "impressions": row["impressions"],
                        "clicks": row["clicks"], "account_currency": currency,
                        "date_start": day, "date_stop": day}
                data.append({k: v for k, v in full.items()
                             if k in fields and v is not None})
        return self._page(account, "insights", params, data)

    def _page(self, account, edge, params, data):
        limit = max(1, min(int(params.get("limit") or 25), self.max_limit))
        offset = int(params.get("after") or 0)
        page = data[offset:offset + limit]
        if self.repeat_first_row and offset and data:
            page = [data[0]] + page
        body = {"data": page}
        if offset + limit < len(data) or self.endless:
            after = str(offset + limit)
            next_url = (f"https://graph.facebook.com/{VERSION}/act_"
                        f"{account.account_id}/{edge}?access_token={account.token}"
                        f"&limit={limit}&after={after}")
            self.next_urls.append(next_url)
            body["paging"] = {"cursors": {"before": str(offset), "after": after},
                              "next": next_url}
        else:
            body["paging"] = {"cursors": {"before": str(offset),
                                          "after": str(offset + len(page))}}
        return FakeResponse(200, body)

    # ── conveniences for assertions ────────────────────────────────────
    def insight_calls(self, level=None, account_id=None):
        return [c for c in self.calls if c["url"].endswith("/insights")
                and (level is None or c["params"].get("level") == level)
                and (account_id is None or c["account_id"] == account_id)]


def transport_error(token=SENTINEL_TOKEN):
    """What ``requests`` raises when the connection drops: its text carries
    the full URL — here, as it would if the token had been put in the URL."""
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='graph.facebook.com', port=443): Max retries "
        f"exceeded with url: /{VERSION}/act_{ACCOUNT_ID}/insights?access_token="
        f"{token}&level=campaign (Caused by NewConnectionError)")


ENV_NAMES = tuple(f"{name}{suffix}"
                  for suffix in ("", "_2", "_3", "_4", "_5")
                  for name in ("META_ADS_ACCESS_TOKEN", "META_AD_ACCOUNT_ID",
                               "META_APP_SECRET")) + ("META_API_VERSION",
                                                       "META_AD_ACCOUNT_RETIRED")


def unconfigure(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, graph=None, *, version=None, app_secret=None):
    """Set the Meta environment for a test and install the fake.

    Each distinct token among the fake's accounts becomes one credential set,
    in the order the accounts were added: set 1 unsuffixed, then ``_2`` …
    """
    unconfigure(monkeypatch)
    monkeypatch.setenv("META_API_VERSION", version or VERSION)
    sets: dict = {}
    for account in (graph.accounts.values() if graph else ()):
        sets.setdefault(account.token, []).append(account.account_id)
    if graph is None:
        sets = {SENTINEL_TOKEN: [ACCOUNT_ID]}
    for number, (token, ids) in enumerate(sets.items(), start=1):
        suffix = "" if number == 1 else f"_{number}"
        monkeypatch.setenv(f"META_ADS_ACCESS_TOKEN{suffix}", token)
        monkeypatch.setenv(f"META_AD_ACCOUNT_ID{suffix}", ",".join(ids))
    if app_secret:
        monkeypatch.setenv("META_APP_SECRET", app_secret)
    if graph is not None:
        monkeypatch.setattr(requests, "get", graph.get)
    import connectors.meta_ads as connector
    sleeps: list = []
    monkeypatch.setattr(connector, "_sleep", sleeps.append)
    return sleeps
