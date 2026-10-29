"""Facts and courtesies stapel-orders sends: bus emits, notifications, signals.

Emits go through the caller's ``mutate_and_emit`` emitter (key = order id),
so the outbox row commits with the mutation. Notifications and realtime
signals are best-effort and leave only after commit.
"""
from __future__ import annotations

import logging

from django.db import transaction

from ..money import to_wire

logger = logging.getLogger(__name__)

EVENT_PLACED = "orders.order.placed"
EVENT_STATUS_CHANGED = "orders.order.status_changed"
EVENT_PAID = "orders.order.paid"
SIGNAL_CART_CHANGED = "orders.cart.changed"

NOTIFY_PLACED = "orders.placed"
NOTIFY_STATUS_CHANGED = "orders.status_changed"


def placed_payload(order, lines) -> dict:
    return {
        "order_id": str(order.pk),
        "user_id": order.user_id,
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
            for line in lines
        ],
    }


def status_changed_payload(order, from_status: str, actor: str, reason: str = "") -> dict:
    payload = {
        "order_id": str(order.pk),
        "user_id": order.user_id,
        "from": from_status,
        "to": order.status,
        "actor": actor,
    }
    if reason:
        payload["reason"] = reason
    return payload


def order_url(order) -> str:
    from ..conf import orders_settings

    template = orders_settings.ORDER_URL_TEMPLATE or ""
    if not template:
        return ""
    try:
        return template.format(order_id=order.pk, number=order.number)
    except (KeyError, IndexError, ValueError):
        logger.warning("ORDER_URL_TEMPLATE %r cannot be formatted", template)
        return ""


def notify_after_commit(notification_type: str, order, **variables) -> None:
    """Best-effort buyer notification, sent once the transaction commits."""
    from ..conf import orders_settings

    if not orders_settings.NOTIFY:
        return
    user_id = order.user_id
    data = {
        "order_id": str(order.pk),
        "order_number": order.number,
        "status": order.status,
        "total": to_wire(order.total),
        "currency": order.currency,
        "order_url": order_url(order),
        **variables,
    }

    def _send():
        try:
            from stapel_core.notifications.publish import request_notification

            request_notification(
                notification_type,
                user_id=user_id,
                variables=data,
                source_service="orders",
            )
        except Exception:
            logger.warning(
                "orders: %s notification for order %s not sent",
                notification_type, data["order_id"], exc_info=True,
            )

    transaction.on_commit(_send)


def cart_changed(user_id: str, line_count: int) -> None:
    """Realtime hint for a cart badge (a no-op without a signal transport)."""
    from stapel_core.comm import SignalError, signal, stream_key

    try:
        signal(
            stream_key("orders", "user", user_id),
            SIGNAL_CART_CHANGED,
            {"line_count": line_count},
        )
    except SignalError:
        logger.warning("orders: cart signal for user %s not addressable", user_id)
