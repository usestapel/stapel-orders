"""Money rules of stapel-orders: Decimal inside, strings on the wire.

The bound proof lives here as data, so the test, the system check and the
models read one source:

    lines <= MAX_LINES_PER_CART, quantity <= MAX_LINE_QUANTITY,
    unit_price < 10**UNIT_PRICE_INTEGER_DIGITS
    =>  total < 10**TOTAL_INTEGER_DIGITS

``max_total_exclusive()`` computes the left side from the effective settings;
``columns_hold()`` compares it with the column widths.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

logger = logging.getLogger(__name__)

#: Decimal places of every money column (currencies with exponent 2 only).
DECIMAL_PLACES = 2
QUANTUM = Decimal("0.01")

#: Column widths (max_digits) — models.py reads these, never literals.
UNIT_PRICE_MAX_DIGITS = 12
LINE_TOTAL_MAX_DIGITS = 16
ORDER_TOTAL_MAX_DIGITS = 18

UNIT_PRICE_INTEGER_DIGITS = UNIT_PRICE_MAX_DIGITS - DECIMAL_PLACES
LINE_TOTAL_INTEGER_DIGITS = LINE_TOTAL_MAX_DIGITS - DECIMAL_PLACES
TOTAL_INTEGER_DIGITS = ORDER_TOTAL_MAX_DIGITS - DECIMAL_PLACES

#: Strict upper bound of a unit price the resolver may answer.
UNIT_PRICE_LIMIT = Decimal(10) ** UNIT_PRICE_INTEGER_DIGITS


class MalformedPrice(ValueError):
    """A price that is not a non-negative 2-dp decimal below the column."""


def parse_price(raw) -> Decimal:
    """A resolver's price string as a Decimal, or raise :class:`MalformedPrice`.

    Floats are refused: a float price is already a rounding error.
    """
    if isinstance(raw, float) or isinstance(raw, bool):
        raise MalformedPrice(f"price must be a decimal string, got {raw!r}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise MalformedPrice(f"price is not a decimal: {raw!r}") from None
    if not value.is_finite() or value < 0:
        raise MalformedPrice(f"price must be finite and non-negative: {raw!r}")
    if value != value.quantize(QUANTUM):
        raise MalformedPrice(f"price has more than {DECIMAL_PLACES} decimal places: {raw!r}")
    if value >= UNIT_PRICE_LIMIT:
        raise MalformedPrice(f"price {raw!r} does not fit the unit price column")
    return value.quantize(QUANTUM)


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    """``unit_price * quantity`` quantized ROUND_HALF_UP to 2 dp."""
    return (unit_price * quantity).quantize(QUANTUM, rounding=ROUND_HALF_UP)


def sum_lines(totals) -> Decimal:
    """Sum of already-quantized line totals; never re-rounded."""
    result = Decimal("0.00")
    for value in totals:
        result += value
    return result


def to_wire(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(QUANTUM))


def max_line_total_exclusive(max_line_quantity: int) -> Decimal:
    return UNIT_PRICE_LIMIT * max_line_quantity


def max_total_exclusive(max_line_quantity: int, max_lines: int) -> Decimal:
    return max_line_total_exclusive(max_line_quantity) * max_lines


def columns_hold(max_line_quantity: int, max_lines: int) -> bool:
    """Do the line and order columns hold the largest possible values?"""
    return (
        max_line_total_exclusive(max_line_quantity)
        <= Decimal(10) ** LINE_TOTAL_INTEGER_DIGITS
        and max_total_exclusive(max_line_quantity, max_lines)
        <= Decimal(10) ** TOTAL_INTEGER_DIGITS
    )


def currencies_converter(amount: Decimal, currency: str, base: str) -> Decimal | None:
    """Default ``TOTAL_BASE_CONVERTER``: ask ``currencies.convert`` by name.

    Same currency is the identity. No provider, or a failing one, answers
    ``None`` — an unknown base total beats a wrong one.
    """
    if not currency or currency == base:
        return amount
    from stapel_core.comm import CommError, call, function_unreachable_reason

    if function_unreachable_reason("currencies.convert"):
        return None
    try:
        reply = call(
            "currencies.convert",
            {"amount": str(amount), "from_currency": currency, "to_currency": base},
        )
        return Decimal(str(reply["amount"])).quantize(QUANTUM, rounding=ROUND_HALF_UP)
    except (CommError, KeyError, TypeError, InvalidOperation):
        logger.warning(
            "orders: base total conversion %s %s -> %s failed; storing NULL",
            amount, currency, base, exc_info=True,
        )
        return None
