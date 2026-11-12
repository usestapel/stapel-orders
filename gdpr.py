"""GDPR for stapel-orders — the ledger rule: the person goes, the economics stay.

Erasure of an ``account``:

- carts (and their lines) are deleted — nothing in a cart is a record;
- orders keep their lines, amounts, statuses and timestamps, while the ids
  and text that name the person are replaced: ``user_id`` by the fleet
  pseudonym (``stapel_core.gdpr.pseudonymize``), the contact and address
  snapshot and the free-text comment by blanks;
- idempotency rows are deleted (they name the user and carry no economics);
- checkout-hook state rows keep working for the sweep under the pseudonym;
- order adjustments are economics and stay (their ``meta`` never holds
  personal data — the adjuster contract says so).

The export lists ``checkout_hooks`` only when the user has such rows.

One operation, :func:`erase_subject`, reached by the in-process provider and
by ``register_gdpr_owner``'s ``gdpr.erasure.requested`` / ``user.deleted``
subscribers alike.
"""
from __future__ import annotations

from stapel_core.gdpr import GDPRProvider

#: Marker written over erased snapshot text (country keeps its 2-char width).
ERASED = "erased"


class OrdersGDPRProvider(GDPRProvider):
    section = "orders"

    def export(self, user_id) -> dict:
        from .models import Cart, CheckoutHookState, Order

        user_id = str(user_id)
        carts = [
            {
                "currency": cart.currency,
                "updated_at": cart.updated_at.isoformat(),
                "lines": [
                    {
                        "purchasable_type": line.purchasable_type,
                        "purchasable_key": line.purchasable_key,
                        "title": line.title,
                        "quantity": line.quantity,
                        "unit_price": str(line.unit_price),
                        "currency": line.currency,
                    }
                    for line in cart.lines.all()
                ],
            }
            for cart in Cart.objects.filter(user_id=user_id)
        ]
        orders = [
            {
                "number": order.number,
                "status": order.status,
                "payment_status": order.payment_status,
                "currency": order.currency,
                "total": str(order.total),
                "created_at": order.created_at.isoformat(),
                "contact": {
                    "name": order.contact_name,
                    "phone": order.contact_phone,
                    "email": order.contact_email,
                },
                "shipping_address": {
                    "line1": order.address_line1,
                    "line2": order.address_line2,
                    "city": order.address_city,
                    "region": order.address_region,
                    "postal_code": order.address_postal_code,
                    "country": order.address_country,
                },
                "comment": order.comment,
                "lines": [
                    {
                        "purchasable_type": line.purchasable_type,
                        "purchasable_key": line.purchasable_key,
                        "title": line.title,
                        "quantity": line.quantity,
                        "unit_price": str(line.unit_price),
                        "line_total": str(line.line_total),
                    }
                    for line in order.lines.all()
                ],
                "adjustments": [
                    {
                        "kind": a.kind,
                        "code": a.code,
                        "label": a.label,
                        "amount": str(a.amount),
                        "line_index": a.line_index,
                    }
                    for a in order.adjustments.all()
                ],
            }
            for order in Order.objects.filter(user_id=user_id).prefetch_related(
                "lines", "adjustments"
            )
        ]
        data = {"carts": carts, "orders": orders}
        hook_rows = [
            {
                "attempt_key": str(row.attempt_key),
                "hook": row.name,
                "state": row.state,
                "created_at": row.created_at.isoformat(),
            }
            for row in CheckoutHookState.objects.filter(user_id=user_id)
        ]
        if hook_rows:
            data["checkout_hooks"] = hook_rows
        return data

    def delete(self, user_id) -> None:
        erase_subject("account", user_id)

    #: After erasure the rows hold amounts and a pseudonym: that IS the
    #: anonymisation, so one operation answers both names.
    anonymize = delete


OWNER = OrdersGDPRProvider.section
SUBJECT_TYPES = ("account",)


def erase_subject(subject_type, subject_key, workspace_id=None):
    """Erase one account's personal data. Idempotent; counts what it touched."""
    from django.db import transaction
    from stapel_core.gdpr.owners import pseudonymize

    from .models import Cart, CheckoutAttempt, CheckoutHookState, Order

    if subject_type != "account" or not subject_key:
        return None
    user_id = str(subject_key)
    with transaction.atomic():
        carts = Cart.objects.filter(user_id=user_id)
        cart_count = carts.count()
        carts.delete()
        attempts, _ = CheckoutAttempt.objects.filter(user_id=user_id).delete()
        CheckoutHookState.objects.filter(user_id=user_id).update(user_id=pseudonymize(user_id))
        orders = Order.objects.filter(user_id=user_id).update(
            user_id=pseudonymize(user_id),
            contact_name=ERASED,
            contact_phone=ERASED,
            contact_email="",
            address_line1=ERASED,
            address_line2="",
            address_city=ERASED,
            address_region="",
            address_postal_code="",
            address_country="",
            comment="",
        )
    return {"carts": cart_count, "orders": orders, "checkout_attempts": attempts}
