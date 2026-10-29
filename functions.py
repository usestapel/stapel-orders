"""comm Functions provided by stapel-orders (schemas in ``schemas/functions/``).

- ``orders.order.status`` — an order's state for another service.
- ``orders.orders_export`` — cursor snapshot (``source_of_truth`` shape) a
  composite can project "orders per seller/listing" from.
- ``orders.cart.count`` — a user's cart line count.

The purchasable resolvers are NOT provided here: each type's owner provides
its own (``listings.purchasables``, ``<app>.purchasables``) against the
contract in ``schemas/functions/purchasables.json``.
"""
from stapel_core.comm import function

EXPORT_DEFAULT_LIMIT = 100
EXPORT_MAX_LIMIT = 500


@function("orders.order.status")
def order_status(payload):
    from .models import Order
    from .money import to_wire

    order_id = str(payload["order_id"])
    order = Order.objects.filter(pk=int(order_id)).first() if order_id.isdigit() else None
    if order is None:
        raise LookupError(f"order {order_id} not found")
    return {
        "status": order.status,
        "payment_status": order.payment_status,
        "user_id": order.user_id,
        "total": to_wire(order.total),
        "currency": order.currency,
    }


@function("orders.orders_export")
def orders_export(payload):
    from .models import Order
    from .money import to_wire

    limit = min(int(payload.get("limit") or EXPORT_DEFAULT_LIMIT), EXPORT_MAX_LIMIT)
    cursor = payload.get("cursor")
    qs = Order.objects.order_by("id").prefetch_related("lines")
    if cursor and str(cursor).isdigit():
        qs = qs.filter(id__gt=int(cursor))
    page = list(qs[: limit + 1])
    more = len(page) > limit
    page = page[:limit]
    rows = [
        {
            "order_id": str(order.pk),
            "number": order.number,
            "user_id": order.user_id,
            "status": order.status,
            "payment_status": order.payment_status,
            "currency": order.currency,
            "total": to_wire(order.total),
            "placed_at": order.created_at.isoformat(),
            "lines": [
                {
                    "purchasable_type": line.purchasable_type,
                    "purchasable_key": line.purchasable_key,
                    "quantity": line.quantity,
                    "unit_price": to_wire(line.unit_price),
                    "seller_id": line.seller_id or None,
                }
                for line in order.lines.all()
            ],
            "seq": order.pk,
        }
        for order in page
    ]
    return {
        "rows": rows,
        "cursor": str(page[-1].pk) if more and page else None,
        "total": Order.objects.count(),
    }


@function("orders.cart.count")
def cart_count(payload):
    from .services.cart import line_count

    return {"line_count": line_count(str(payload["user_id"]))}
