"""How every backend writes an amount (owner, 2026-10-21), as the apps' ``@minty/shared/money`` does.

* The ISO code, never a symbol: ``HKD 1,234.50`` - ``currency_info.symbol`` is not read for
  display. Code and number are joined by ONE NON-BREAKING space (U+00A0, written only as the
  ``\\u00a0`` escape here), so a line never splits them.
* The number is grouped and carries the currency's own decimal places (JPY 0, HKD 2).
* A PRICE drops a whole ``.00`` (``HKD 400``, but ``HKD 400.50``) - :func:`format_price`. A
  charge, an invoice line or a report figure keeps its places - :func:`format_major`.
* No currency is never a silent default: :func:`format_amount` says so,
  ``12.00 (No currency selected)``.

Pure ``Decimal`` arithmetic: the places come in as a number. Each service resolves them from
its own ``currency_info`` read (cached per request in one, threaded through in another); that
lookup is not shared, the rule is.
"""

from __future__ import annotations

from decimal import Decimal

NBSP = "\u00a0"
#: The places for a currency whose row cannot be read - a guess callers should log.
FALLBACK_DECIMAL_PLACES = 2
NO_CURRENCY = "(No currency selected)"


def label(currency_code: str | None) -> str:
    """The label in front of an amount: the upper-cased ISO code, "" with no currency."""
    return str(currency_code).strip().upper() if currency_code else ""


def join(code_label: str, text: str) -> str:
    """``HKD`` + NBSP + an already-formatted number; the number alone with no code."""
    return f"{code_label}{NBSP}{text}" if code_label else text


def to_major(amount_minor: int | str | None, places: int) -> Decimal:
    """Minor units to a major-unit Decimal: 28000 at 2 places is 280.00, 280 at 0 is 280."""
    if amount_minor is None:
        return Decimal("0")
    return Decimal(int(amount_minor)) / (Decimal(10) ** places)


def _number(amount_major: Decimal | int | str | float, places: int) -> str:
    if places <= 0:
        return f"{int(Decimal(str(amount_major)).to_integral_value()):,}"
    quantized = Decimal(str(amount_major)).quantize(Decimal(1).scaleb(-places))
    return f"{quantized:,.{places}f}"


def format_minor(amount_minor: int | str | None, places: int) -> str:
    """Minor units as a plain grouped number - no code (callers state the currency)."""
    value = abs(Decimal(int(amount_minor or 0))) / (Decimal(10) ** places)
    return f"{value:,.{places}f}"


def format_major(
    code_label: str,
    amount_major: Decimal | int | str | float,
    places: int = FALLBACK_DECIMAL_PLACES,
) -> str:
    """MAJOR units with the currency's places: ``HKD 400.00``, ``HKD 400.50``, ``JPY 1,200``."""
    return join(code_label, _number(amount_major, places))


def format_price(
    code_label: str,
    amount_major: Decimal | int | str | float,
    places: int = FALLBACK_DECIMAL_PLACES,
) -> str:
    """A PRICE: ``HKD 400`` when whole, ``HKD 400.50`` when not - cents only when they mean
    something. Invoices and charges keep their places (:func:`format_major`)."""
    if places > 0:
        quantized = Decimal(str(amount_major)).quantize(Decimal(1).scaleb(-places))
        if quantized == quantized.to_integral_value():
            return join(code_label, f"{int(quantized):,}")
    return format_major(code_label, amount_major, places)


def format_amount(
    currency_code: str | None,
    amount_major: Decimal | int | str | float,
    places: int = FALLBACK_DECIMAL_PLACES,
) -> str:
    """An amount on a screen or a report: ``HKD 1,234.50``, or with no currency
    ``1,234.50 (No currency selected)`` - never a silent symbol."""
    code = label(currency_code)
    number = _number(amount_major, places)
    return join(code, number) if code else f"{number} {NO_CURRENCY}"
