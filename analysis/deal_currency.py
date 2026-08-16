"""
analysis/deal_currency.py

PR-ADS-153E-A — fail-closed currency doctrine for canonical deal revenue.

The defect this replaces
------------------------
PR-ADS-153A §9.3 found that revenue had NO currency doctrine at all: HubSpot's
raw ``amount`` was written straight into a column named ``deal_amount_usd`` with
no conversion and no currency check. The one field that could have verified the
USD assumption — ``deal_currency_code`` — was fetched only by the dead JSON
chain and discarded. Every revenue figure in the product was therefore an
unverified USD *claim*.

The rule
--------
``revenue_usd`` is populated ONLY when the currency is proven. There are exactly
two ways to prove it:

1. **verified_usd** — the amount is already in a currency we have verified to be
   USD (the portal home currency, confirmed as USD, or a deal whose own currency
   is USD).
2. **converted** — a non-USD amount converted at the LOCAL FX rate for the deal's
   close date, through the same ``fx_rates`` contract that governs spend.

Anything else yields ``revenue_usd = None`` with an explicit status and reason.

Never do any of these:
  * treat ``amount_in_home_currency`` as USD without verifying the home currency;
  * coerce an unknown currency to zero (zero is a *claim* that the deal was
    worth nothing);
  * mix currencies inside one total;
  * fetch FX from an external service (spend already proved the local contract
    works — services must not reach the network for rates).

Purity
------
No DB, no network. The caller supplies the FX rate map and the verified home
currency; this module only decides.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

CURRENCY_RULE_VERSION = "v1"

REPORTING_CURRENCY = "USD"

# ── Currency resolution statuses ─────────────────────────────────────────────
# A proven USD amount, taken directly (no conversion applied).
CURRENCY_VERIFIED_USD = "verified_usd"
# A non-USD amount converted at the local close-date rate.
CURRENCY_CONVERTED = "converted"
# Everything below leaves revenue_usd NULL. They are DISTINCT on purpose: an
# operator fixing "no FX rate for that day" does something very different from
# an operator fixing "HubSpot never told us the currency".
CURRENCY_UNAVAILABLE = "unavailable"

# Reasons (paired with CURRENCY_UNAVAILABLE)
REASON_NO_AMOUNT = "no_amount"
REASON_UNKNOWN_CURRENCY = "unknown_currency"
REASON_HOME_CURRENCY_UNVERIFIED = "home_currency_unverified"
REASON_NO_CLOSE_DATE = "no_close_date_for_fx"
REASON_NO_FX_RATE = "no_fx_rate_for_close_date"

ALL_CURRENCY_STATUSES = (
    CURRENCY_VERIFIED_USD,
    CURRENCY_CONVERTED,
    CURRENCY_UNAVAILABLE,
)

# Statuses whose revenue_usd may be summed into a canonical total.
SUMMABLE_CURRENCY_STATUSES = frozenset({
    CURRENCY_VERIFIED_USD,
    CURRENCY_CONVERTED,
})


def _decimal(value):
    """Parse a monetary value, or None. An unparseable amount is NOT zero."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _norm_currency(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _round2(value):
    if value is None:
        return None
    return float(Decimal(value).quantize(Decimal("0.01")))


def resolve_deal_currency(
    *,
    amount_raw=None,
    deal_currency_code=None,
    amount_in_home_currency=None,
    home_currency_code=None,
    home_currency_verified: bool = False,
    close_date_iso: str | None = None,
    fx_rates: dict | None = None,
) -> dict:
    """Resolve one deal's canonical USD revenue, or explain why it cannot be.

    Args:
        amount_raw: the deal amount in ``deal_currency_code``.
        deal_currency_code: the deal's own currency, if HubSpot reported one.
        amount_in_home_currency: the amount in the PORTAL home currency.
        home_currency_code: the portal home currency, if known.
        home_currency_verified: True only when the home currency has been
            positively confirmed. Without this, ``amount_in_home_currency`` is
            an amount in an UNKNOWN currency and must not be read as USD.
        close_date_iso: ``YYYY-MM-DD`` — the FX date for a conversion.
        fx_rates: ``{iso_date: rate}`` mapping the deal currency to USD, from
            the LOCAL fx_rates table. Never fetched here.

    Returns ``{revenue_usd, currency_status, currency_reason,
    currency_rule_version, fx_rate_used, fx_rate_date, resolved_currency}``.
    """
    rates = fx_rates or {}
    deal_ccy = _norm_currency(deal_currency_code)
    home_ccy = _norm_currency(home_currency_code)

    raw = _decimal(amount_raw)
    home_amount = _decimal(amount_in_home_currency)

    def _unavailable(reason, resolved=None):
        return {
            "revenue_usd": None,
            "currency_status": CURRENCY_UNAVAILABLE,
            "currency_reason": reason,
            "currency_rule_version": CURRENCY_RULE_VERSION,
            "fx_rate_used": None,
            "fx_rate_date": None,
            "resolved_currency": resolved,
        }

    # ── 1. No money at all ──────────────────────────────────────────────────
    # A deal with no amount is not a zero-value deal; it is a deal whose value
    # we do not know. It stays NULL so it can never be summed as 0.00.
    if raw is None and home_amount is None:
        return _unavailable(REASON_NO_AMOUNT)

    # ── 2. Already USD, provably ────────────────────────────────────────────
    if deal_ccy == REPORTING_CURRENCY and raw is not None:
        return {
            "revenue_usd": _round2(raw),
            "currency_status": CURRENCY_VERIFIED_USD,
            "currency_reason": "deal_currency_is_usd",
            "currency_rule_version": CURRENCY_RULE_VERSION,
            "fx_rate_used": None,
            "fx_rate_date": None,
            "resolved_currency": REPORTING_CURRENCY,
        }

    if home_currency_verified and home_ccy == REPORTING_CURRENCY and home_amount is not None:
        return {
            "revenue_usd": _round2(home_amount),
            "currency_status": CURRENCY_VERIFIED_USD,
            "currency_reason": "verified_home_currency_is_usd",
            "currency_rule_version": CURRENCY_RULE_VERSION,
            "fx_rate_used": None,
            "fx_rate_date": None,
            "resolved_currency": REPORTING_CURRENCY,
        }

    # A home amount exists but the home currency is unproven. This is exactly
    # the assumption PR-ADS-153A flagged: it LOOKS like USD and may not be.
    if home_amount is not None and raw is None and not (
            home_currency_verified and home_ccy):
        return _unavailable(REASON_HOME_CURRENCY_UNVERIFIED, resolved=home_ccy)

    # ── 3. Known non-USD currency → convert at the close-date rate ──────────
    convert_amount, convert_ccy = (raw, deal_ccy)
    if convert_amount is None and home_currency_verified and home_ccy:
        convert_amount, convert_ccy = (home_amount, home_ccy)

    if convert_amount is None:
        return _unavailable(REASON_NO_AMOUNT, resolved=convert_ccy)
    if not convert_ccy:
        # HubSpot gave us a number and no currency. Summing it would mix
        # currencies inside one total.
        return _unavailable(REASON_UNKNOWN_CURRENCY)
    if not close_date_iso:
        return _unavailable(REASON_NO_CLOSE_DATE, resolved=convert_ccy)

    rate = rates.get(str(close_date_iso))
    if rate is None:
        # Same fail-closed posture as spend: a missing daily rate withholds the
        # value rather than converting at a neighbouring day's rate.
        return _unavailable(REASON_NO_FX_RATE, resolved=convert_ccy)

    try:
        converted = Decimal(str(convert_amount)) * Decimal(str(rate))
    except (InvalidOperation, ValueError, TypeError):
        return _unavailable(REASON_NO_FX_RATE, resolved=convert_ccy)

    return {
        "revenue_usd": _round2(converted),
        "currency_status": CURRENCY_CONVERTED,
        "currency_reason": f"converted_from_{convert_ccy.lower()}_at_close_date",
        "currency_rule_version": CURRENCY_RULE_VERSION,
        "fx_rate_used": float(rate),
        "fx_rate_date": str(close_date_iso),
        "resolved_currency": convert_ccy,
    }


def is_summable(currency_status) -> bool:
    """May this row's ``revenue_usd`` enter a canonical USD total?"""
    return currency_status in SUMMABLE_CURRENCY_STATUSES


__all__ = [
    "CURRENCY_RULE_VERSION", "REPORTING_CURRENCY",
    "CURRENCY_VERIFIED_USD", "CURRENCY_CONVERTED", "CURRENCY_UNAVAILABLE",
    "ALL_CURRENCY_STATUSES", "SUMMABLE_CURRENCY_STATUSES",
    "REASON_NO_AMOUNT", "REASON_UNKNOWN_CURRENCY",
    "REASON_HOME_CURRENCY_UNVERIFIED", "REASON_NO_CLOSE_DATE",
    "REASON_NO_FX_RATE",
    "resolve_deal_currency", "is_summable",
]
