"""Orders written directly, for tests that start after checkout."""
import itertools
from decimal import Decimal

from stapel_orders.models import Order, OrderLine

_seq = itertools.count(1)


def make_order(user, *, status="new", total="10.00", currency="USD", lines=1, **extra):
    n = next(_seq)
    total = Decimal(total)
    order = Order.objects.create(
        number=f"20260101-T{n:05d}",
        user_id=str(user.pk) if hasattr(user, "pk") else str(user),
        status=status,
        currency=currency,
        subtotal=total,
        total=total,
        contact_name="Ann Buyer",
        contact_phone="+1 555 0100",
        contact_email="ann@example.com",
        address_line1="1 Main St",
        address_city="Springfield",
        address_country="US",
        comment="ring twice",
        **extra,
    )
    for i in range(lines):
        OrderLine.objects.create(
            order=order, purchasable_type="item", purchasable_key=f"k{i}",
            title=f"Item {i}", quantity=1, unit_price=total, line_total=total,
            seller_id="seller-1",
        )
    return order
