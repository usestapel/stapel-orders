"""Order services: reads, the lifecycle FSM, and the payment fact.

Owner reads are scoped by ``user_id``: a foreign id and an absent id answer
the same 404. Every status move goes through :func:`transition`, which
checks the table in ``fsm.py`` and emits ``orders.order.status_changed`` in
the same transaction. A cancel releases the order's checkout-hook
reservations once it commits.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.db.models import QuerySet
from stapel_core.comm import mutate_and_emit

from .. import fsm
from ..errors import ERR_404_ORDER_NOT_FOUND, ERR_409_ORDER_TRANSITION_INVALID
from ..models import Order, OrderStatus, PaymentStatus
from . import OrdersError, events, hooks

logger = logging.getLogger(__name__)


def orders_for(user_id: str, statuses=None) -> QuerySet:
    qs = Order.objects.filter(user_id=str(user_id))
    statuses = [s for s in (statuses or []) if s]
    if statuses:
        qs = qs.filter(status__in=statuses)
    return qs.prefetch_related("lines", "adjustments")


def get_owned(user_id: str, order_id) -> Order:
    order = orders_for(user_id).filter(pk=order_id).first()
    if order is None:
        raise OrdersError(404, ERR_404_ORDER_NOT_FOUND)
    return order


def get_any(order_id) -> Order:
    order = Order.objects.prefetch_related("lines", "adjustments").filter(pk=order_id).first()
    if order is None:
        raise OrdersError(404, ERR_404_ORDER_NOT_FOUND)
    return order


def _invalid(order: Order, to_status: str) -> OrdersError:
    return OrdersError(
        409, ERR_409_ORDER_TRANSITION_INVALID, {"from": order.status, "to": to_status}
    )


def transition(order_id, to_status: str, *, actor: str, reason: str = "", emit=None) -> Order:
    """Move an order along the FSM as *actor*; emits the status fact.

    Pass *emit* to join a caller's ``mutate_and_emit`` block.
    """
    if emit is None:
        with mutate_and_emit() as scoped:
            return transition(order_id, to_status, actor=actor, reason=reason, emit=scoped)
    order = Order.objects.select_for_update().get(pk=order_id)
    try:
        fsm.check(order.status, to_status, actor)
    except fsm.TransitionInvalid:
        raise _invalid(order, to_status) from None
    from_status = order.status
    order.status = to_status
    order.save(update_fields=["status", "updated_at"])
    if to_status == OrderStatus.CANCELLED:
        hooks.release_for_order(order.pk)
    emit(  # emit-check: ok — emit is always a caller's mutate_and_emit emitter (see top)
        events.EVENT_STATUS_CHANGED,
        events.status_changed_payload(order, from_status, actor, reason),
        key=str(order.pk),
    )
    events.notify_after_commit(
        events.NOTIFY_STATUS_CHANGED, order, from_status=from_status, reason=reason
    )
    return order


def cancel_owned(user_id: str, order_id) -> Order:
    order = get_owned(user_id, order_id)
    if not fsm.allowed(order.status, OrderStatus.CANCELLED, fsm.OWNER):
        raise _invalid(order, OrderStatus.CANCELLED)
    transition(order.pk, OrderStatus.CANCELLED, actor=fsm.OWNER)
    return get_owned(user_id, order_id)


def staff_transition(order_id, to_status: str, reason: str = "") -> Order:
    get_any(order_id)
    transition(order_id, to_status, actor=fsm.STAFF, reason=reason)
    return get_any(order_id)


def _amount(raw) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def apply_payment(payload: dict) -> str:
    """Apply one payment fact (``orders.payment.completed`` or the payment
    module's ``payment.completed``). Returns what happened.

    Idempotent by state: a fact for an already-paid order with the same
    ``provider_ref`` is a no-op. An amount or currency that differs from the
    order's moves it to ``needs_review`` and leaves ``payment_status``
    alone — a short payment is not a paid order, and nobody guesses.
    """
    if payload.get("reference_type") != "order":
        return "ignored"
    key = str(payload.get("reference_key") or "")
    if not key.isdigit():
        logger.warning("orders: payment.completed for a non-order key %r", key)
        return "ignored"
    provider_ref = str(payload.get("provider_ref") or "")[:128]
    with mutate_and_emit() as emit:
        order = Order.objects.select_for_update().filter(pk=int(key)).first()
        if order is None:
            logger.warning("orders: payment.completed for unknown order %s", key)
            return "unknown"
        if order.payment_status == PaymentStatus.PAID and order.provider_ref == provider_ref:
            return "duplicate"
        amount = _amount(payload.get("amount"))
        currency = str(payload.get("currency") or "")
        if amount is None or amount != order.total or currency != order.currency:
            logger.error(
                "orders: payment.completed for order %s does not match: paid %s %s, "
                "order total %s %s; moved to needs_review",
                order.pk, payload.get("amount"), currency, order.total, order.currency,
            )
            order.provider_ref = provider_ref
            order.save(update_fields=["provider_ref", "updated_at"])
            if order.status != OrderStatus.NEEDS_REVIEW:
                transition(
                    order.pk, OrderStatus.NEEDS_REVIEW,
                    actor=fsm.SYSTEM, reason="payment_mismatch", emit=emit,
                )
            return "mismatch"
        if (order.payment_status, PaymentStatus.PAID) not in fsm.PAYMENT_TRANSITIONS:
            logger.warning(
                "orders: payment.completed for order %s in payment state %s ignored",
                order.pk, order.payment_status,
            )
            return "ignored"
        order.payment_status = PaymentStatus.PAID
        order.provider_ref = provider_ref
        order.save(update_fields=["payment_status", "provider_ref", "updated_at"])
        emit(
            events.EVENT_PAID,
            {"order_id": str(order.pk), "provider_ref": provider_ref},
            key=str(order.pk),
        )
    return "paid"
