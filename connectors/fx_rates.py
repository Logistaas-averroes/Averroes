"""
Daily FX rate connector (PR-ADS-119) — read-only.

Fetches historical daily exchange rates so Google Ads native spend (GBP) can be
converted to USD reporting spend using each spend row's OWN date. Read-only: it
only reads published reference rates; it never writes to any external platform.

Default provider is the European Central Bank reference rates exposed by
frankfurter.app (no API key). The rate for a non-trading day (weekend/holiday)
is the most recent published rate, which the provider returns automatically.
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

FX_PROVIDER = "frankfurter.ecb"
FX_SOURCE_VERSION = "fx_daily_v1"
_BASE_URL = "https://api.frankfurter.app"
_TIMEOUT_SECONDS = 20


def fetch_fx_rate(rate_date: str, base_currency: str, quote_currency: str) -> dict:
    """Fetch the daily FX rate for ``base->quote`` on ``rate_date``.

    Read-only. Returns
    {rate_date, base_currency, quote_currency, rate, provider, source_version,
     effective_date}. ``effective_date`` is the date the provider actually
    priced (the most recent trading day on/before rate_date). Raises on a
    network/parse error or when the quote currency is absent.
    """
    url = f"{_BASE_URL}/{rate_date}?from={base_currency}&to={quote_currency}"
    req = urllib.request.Request(url, headers={"User-Agent": "averroes-fx/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8"))

    rates = payload.get("rates") or {}
    if quote_currency not in rates:
        raise ValueError(
            f"FX provider returned no {base_currency}->{quote_currency} rate for {rate_date}")
    return {
        "rate_date": rate_date,
        "base_currency": base_currency,
        "quote_currency": quote_currency,
        "rate": float(rates[quote_currency]),
        "provider": FX_PROVIDER,
        "source_version": FX_SOURCE_VERSION,
        "effective_date": payload.get("date") or rate_date,
    }
