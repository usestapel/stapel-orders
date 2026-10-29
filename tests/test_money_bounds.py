"""The bound proof: totals cannot overflow their columns.

Derived from the three constants and the field declarations — never from
literals here — so widening a limit past a column fails this file, and the
lint (which cannot see this class) does not have to.
"""
from decimal import Decimal

import pytest

from stapel_orders import money
from stapel_orders.conf import DEFAULTS
from stapel_orders.models import CartLine, Order, OrderLine


def _integer_digits(model, name):
    field = model._meta.get_field(name)
    return field.max_digits - field.decimal_places


def test_columns_are_the_ones_money_declares():
    assert _integer_digits(CartLine, "unit_price") == money.UNIT_PRICE_INTEGER_DIGITS
    assert _integer_digits(OrderLine, "unit_price") == money.UNIT_PRICE_INTEGER_DIGITS
    assert _integer_digits(CartLine, "line_total") == money.LINE_TOTAL_INTEGER_DIGITS
    assert _integer_digits(OrderLine, "line_total") == money.LINE_TOTAL_INTEGER_DIGITS
    for name in ("subtotal", "total", "total_base"):
        assert _integer_digits(Order, name) == money.TOTAL_INTEGER_DIGITS


def test_default_limits_bound_the_total_below_the_column():
    max_q = DEFAULTS["MAX_LINE_QUANTITY"]
    max_lines = DEFAULTS["MAX_LINES_PER_CART"]
    unit_limit = Decimal(10) ** _integer_digits(CartLine, "unit_price")
    line_limit = Decimal(10) ** _integer_digits(CartLine, "line_total")
    total_limit = Decimal(10) ** _integer_digits(Order, "total")

    worst_line = unit_limit * max_q
    worst_total = worst_line * max_lines
    assert worst_line <= line_limit
    assert worst_total <= total_limit
    # The spec's statement, by name: total < 10**15 < 10**16.
    assert worst_total <= Decimal(10) ** 15 < total_limit
    assert money.columns_hold(max_q, max_lines)


def test_the_default_quantity_cap_is_not_the_integer_column():
    assert DEFAULTS["MAX_LINE_QUANTITY"] == 999
    assert DEFAULTS["MAX_LINES_PER_CART"] == 100


@pytest.mark.parametrize("max_q,max_lines", [(10**6, 100), (999, 10**4)])
def test_a_widened_limit_breaks_the_proof(max_q, max_lines):
    """Inversion: the predicate is not constant-true."""
    assert not money.columns_hold(max_q, max_lines)


def test_largest_price_is_accepted_and_one_more_is_not():
    largest = Decimal(10) ** money.UNIT_PRICE_INTEGER_DIGITS - Decimal("0.01")
    assert money.parse_price(str(largest)) == largest
    with pytest.raises(money.MalformedPrice):
        money.parse_price(str(largest + Decimal("0.01")))


@pytest.mark.django_db
def test_worst_case_order_fits_the_database(user):
    """The largest legal cart, written for real."""
    largest = Decimal(10) ** money.UNIT_PRICE_INTEGER_DIGITS - Decimal("0.01")
    max_q, max_lines = DEFAULTS["MAX_LINE_QUANTITY"], DEFAULTS["MAX_LINES_PER_CART"]
    line_total = money.line_total(largest, max_q)
    total = money.sum_lines([line_total] * max_lines)
    order = Order.objects.create(
        number="20260101-WORST1", user_id=str(user.pk), currency="USD",
        subtotal=total, total=total, contact_name="n", contact_phone="p",
        address_line1="l", address_city="c", address_country="US",
    )
    order.refresh_from_db()
    assert order.total == total
    assert total < Decimal(10) ** 15


def test_rounding_is_half_up_per_line_and_never_re_rounded():
    assert money.line_total(Decimal("0.125"), 1) == Decimal("0.13")
    lines = [money.line_total(Decimal("0.33"), 1)] * 3
    assert money.sum_lines(lines) == Decimal("0.99")
