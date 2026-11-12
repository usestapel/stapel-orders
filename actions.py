"""Action subscriptions of stapel-orders.

Handlers are idempotent: delivery is at-least-once.

- ``user.merged`` (stapel-auth) — a guest was folded into an account on
  sign-in: its cart lines join the survivor's cart (same pair summed and
  capped, another currency dropped) and its orders follow it.
- ``orders.payment.completed`` (this module's fact, emitted by the
  ``PAYMENT`` providers registered here) and ``payment.completed`` (the
  payment module's fact; billing ask B1) — one consumer, and only
  ``reference_type == "order"`` is ours.

- ``user.deleted`` / ``gdpr.erasure.requested`` are answered by
  ``register_gdpr_owner`` (``apps.py``), not here.

A subscriber here never passes ``schema=`` for a fact another module owns:
``on_action`` writes it into the one per-name registry the EMITTER is
validated against, so a consumer-side copy would refuse the owner's own
payloads (``tests/test_fact_schemas.py``). The files in
``schemas/consumes/`` document what is read; the handlers read tolerantly.
"""
import logging

from django.db import transaction
from stapel_core.comm import on_action

from .services.events import EVENT_FOREIGN_PAYMENT_COMPLETED, EVENT_PAYMENT_COMPLETED

logger = logging.getLogger(__name__)


@on_action("user.merged")
def handle_user_merged(event):
    """Re-parent the guest's cart lines and orders to the surviving account."""
    from .models import CheckoutAttempt, CheckoutHookState, Order
    from .services.cart import merge_carts

    payload = event.payload or {}
    from_user_id = str(payload.get("from_user_id") or "")
    into_user_id = str(payload.get("into_user_id") or "")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if from_user_id == into_user_id:
        return
    with transaction.atomic():
        cart = merge_carts(from_user_id, into_user_id)
        orders = Order.objects.filter(user_id=from_user_id).update(user_id=into_user_id)
        survivor_keys = set(
            CheckoutAttempt.objects.filter(user_id=into_user_id).values_list(
                "idempotency_key", flat=True
            )
        )
        CheckoutAttempt.objects.filter(
            user_id=from_user_id, idempotency_key__in=survivor_keys
        ).delete()
        CheckoutAttempt.objects.filter(user_id=from_user_id).update(user_id=into_user_id)
        CheckoutHookState.objects.filter(user_id=from_user_id).update(user_id=into_user_id)
    if cart["moved"] or cart["summed"] or cart["dropped"] or orders:
        logger.info(
            "user.merged %s -> %s: cart moved %s, summed %s, dropped %s; %s orders",
            from_user_id, into_user_id, cart["moved"], cart["summed"], cart["dropped"], orders,
        )


@on_action(EVENT_PAYMENT_COMPLETED)
@on_action(EVENT_FOREIGN_PAYMENT_COMPLETED)
def handle_payment_completed(event):
    """Mark an order paid, or send it to review when the amount disagrees."""
    from .services.orders import apply_payment

    outcome = apply_payment(event.payload or {})
    if outcome not in ("ignored", "duplicate"):
        logger.info("payment.completed %s: %s", event.event_id, outcome)
