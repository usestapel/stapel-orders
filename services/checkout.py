"""Checkout: the one call that turns a cart into an order.

1. Replay: the same ``(user, idempotency_key)`` returns the same order.
2. Every line is re-resolved (outside the transaction, so no lock waits on
   the network). Price drift, a vanished/unavailable purchasable or a
   currency change refreshes the snapshot, COMMITS it, and refuses with the
   refreshed cart in ``params.cart`` — the shopper is never charged a number
   they did not see, and the next submit succeeds.
3. Otherwise, under the cart lock: order + lines + idempotency row, the cart
   emptied, ``orders.order.placed`` emitted, the payment provider started —
   all one transaction.
"""
from __future__ import annotations

import datetime
import logging
import secrets
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone
from stapel_core.comm import mutate_and_emit

from .. import money, registry
from ..errors import (
    ERR_400_CART_EMPTY,
    ERR_403_CHECKOUT_REQUIRES_ACCOUNT,
    ERR_409_CART_CURRENCY_MISMATCH,
    ERR_409_CART_PRICE_CHANGED,
    ERR_409_PURCHASABLE_UNAVAILABLE,
    ERR_503_PURCHASABLE_UNAVAILABLE,
)
from ..models import CartLine, CheckoutAttempt, Order, OrderLine, PaymentStatus
from . import OrdersError, events
from .cart import get_cart, locked_cart

logger = logging.getLogger(__name__)

_NUMBER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_NUMBER_ATTEMPTS = 8


@dataclass
class Contact:
    """Validated contact snapshot."""

    name: str
    phone: str
    email: str = ""


@dataclass
class Address:
    """Validated shipping address snapshot."""

    line1: str
    city: str
    country: str
    line2: str = ""
    region: str = ""
    postal_code: str = ""


def requires_account_refusal(user) -> OrdersError | None:
    """The guest wall: core's predicate, this module's switch and error key."""
    from types import SimpleNamespace

    from stapel_core.django.api.permissions import IsNotAnonymousUser

    from ..conf import orders_settings

    if not orders_settings.CHECKOUT_REQUIRES_ACCOUNT:
        return None
    if IsNotAnonymousUser().has_permission(SimpleNamespace(user=user), None):
        return None
    return OrdersError(403, ERR_403_CHECKOUT_REQUIRES_ACCOUNT)


def new_order_number(today: datetime.date | None = None) -> str:
    day = (today or timezone.now().date()).strftime("%Y%m%d")
    suffix = "".join(secrets.choice(_NUMBER_ALPHABET) for _ in range(6))
    return f"{day}-{suffix}"


def _replayed(user_id: str, idempotency_key) -> Order | None:
    attempt = (
        CheckoutAttempt.objects.select_related("order")
        .filter(user_id=user_id, idempotency_key=idempotency_key)
        .first()
    )
    return attempt.order if attempt else None


def _resolve_lines(pairs) -> dict:
    """Fresh answers per pair; an unregistered type resolves to nothing."""
    by_type: dict[str, list[str]] = {}
    for purchasable_type, key in pairs:
        by_type.setdefault(purchasable_type, []).append(key)
    resolved = {}
    for purchasable_type, keys in by_type.items():
        try:
            answers = registry.resolve(purchasable_type, keys)
        except registry.UnknownPurchasableType:
            continue
        except registry.ResolverUnavailable:
            logger.warning("orders: resolver for %r unavailable at checkout",
                           purchasable_type, exc_info=True)
            raise OrdersError(503, ERR_503_PURCHASABLE_UNAVAILABLE) from None
        for key, item in answers.items():
            resolved[(purchasable_type, key)] = item
    return resolved


def _reconcile(cart, lines: list[CartLine], resolved: dict) -> str | None:
    """Refresh snapshots from *resolved*; return the refusal key, if any.

    Precedence: unavailable > currency > price. Snapshots are saved whatever
    the verdict, so the refreshed cart is what the caller shows next.
    """
    unavailable = currency_changed = price_changed = False
    for line in lines:
        item = resolved.get((line.purchasable_type, line.purchasable_key))
        if item is None or not item.available:
            unavailable = True
            continue
        if item.currency != cart.currency:
            currency_changed = True
            continue
        if item.max_quantity is not None and line.quantity > item.max_quantity:
            unavailable = True
            if item.max_quantity <= 0:
                continue
            line.quantity = item.max_quantity
        if item.price != line.unit_price:
            price_changed = True
        line.title = item.title
        line.unit_price = item.price
        line.image = item.image
        line.url = item.url
        line.seller_id = item.seller_id
        line.line_total = money.line_total(item.price, line.quantity)
        line.save()
    if unavailable:
        return ERR_409_PURCHASABLE_UNAVAILABLE
    if currency_changed:
        return ERR_409_CART_CURRENCY_MISMATCH
    if price_changed:
        return ERR_409_CART_PRICE_CHANGED
    return None


def _total_base(total, currency: str):
    from ..conf import orders_settings

    base = orders_settings.BASE_CURRENCY
    try:
        value = orders_settings.TOTAL_BASE_CONVERTER(total, currency, base)
    except Exception:
        logger.warning("orders: TOTAL_BASE_CONVERTER failed; total_base stays NULL", exc_info=True)
        return None, base
    if value is None:
        return None, base
    limit = money.Decimal(10) ** money.TOTAL_INTEGER_DIGITS
    if abs(value) >= limit:
        return None, base
    return value.quantize(money.QUANTUM), base


def _create_order(cart, lines, user_id, contact: Contact, address: Address,
                  shipping_method: str, comment: str) -> Order:
    totals = [line.line_total for line in lines]
    total = money.sum_lines(totals)
    total_base, base = _total_base(total, cart.currency)
    fields = dict(
        user_id=user_id,
        currency=cart.currency,
        subtotal=total,
        total=total,
        total_base=total_base,
        base_currency=base,
        contact_name=contact.name,
        contact_phone=contact.phone,
        contact_email=contact.email or "",
        address_line1=address.line1,
        address_line2=address.line2 or "",
        address_city=address.city,
        address_region=address.region or "",
        address_postal_code=address.postal_code or "",
        address_country=address.country.upper(),
        shipping_method=shipping_method or "",
        comment=comment or "",
    )
    for _ in range(_NUMBER_ATTEMPTS):
        try:
            with transaction.atomic():
                order = Order.objects.create(number=new_order_number(), **fields)
            break
        except IntegrityError:
            continue
    else:  # pragma: no cover - 32**6 per day makes this unreachable in practice
        raise RuntimeError("could not mint a unique order number")
    OrderLine.objects.bulk_create(
        [
            OrderLine(
                order=order,
                purchasable_type=line.purchasable_type,
                purchasable_key=line.purchasable_key,
                title=line.title,
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_total=line.line_total,
                image=line.image,
                url=line.url,
                seller_id=line.seller_id,
            )
            for line in lines
        ]
    )
    return order


def checkout(user, *, idempotency_key, contact: Contact, address: Address,
             shipping_method: str = "", comment: str = "") -> tuple[Order, bool]:
    """Place the caller's cart as an order. Returns ``(order, created)``."""
    refusal = requires_account_refusal(user)
    if refusal is not None:
        raise refusal
    user_id = str(user.pk)

    replay = _replayed(user_id, idempotency_key)
    if replay is not None:
        return replay, False

    cart = get_cart(user_id)
    pairs = [] if cart is None else list(
        cart.lines.values_list("purchasable_type", "purchasable_key")
    )
    if not pairs:
        raise OrdersError(400, ERR_400_CART_EMPTY)
    resolved = _resolve_lines(pairs)

    refusal_key = None
    with mutate_and_emit() as emit:
        cart = locked_cart(user_id)
        replay = _replayed(user_id, idempotency_key)
        if replay is not None:
            return replay, False
        lines = list(cart.lines.all())
        if not lines:
            raise OrdersError(400, ERR_400_CART_EMPTY)
        late = [
            (line.purchasable_type, line.purchasable_key)
            for line in lines
            if (line.purchasable_type, line.purchasable_key) not in resolved
        ]
        if late:
            # Lines added between the read and the lock: rare, resolved here.
            resolved.update(_resolve_lines(late))
        refusal_key = _reconcile(cart, lines, resolved)
        if refusal_key is None:
            order = _create_order(cart, lines, user_id, contact, address,
                                  shipping_method, comment)
            CheckoutAttempt.objects.create(
                user_id=user_id, idempotency_key=idempotency_key, order=order
            )
            emit(events.EVENT_PLACED, events.placed_payload(order, order.lines.all()),
                 key=str(order.pk))
            cart.lines.all().delete()
            cart.currency = ""
            cart.save(update_fields=["currency", "updated_at"])
            events.cart_changed(user_id, 0)

            from ..payment import active_provider

            status = active_provider().start(order, emit)
            if status and status != PaymentStatus.UNPAID:
                Order.objects.filter(pk=order.pk).update(payment_status=status)
            events.notify_after_commit(events.NOTIFY_PLACED, order)
        else:
            cart.save(update_fields=["updated_at"])

    if refusal_key is not None:
        from ..presenters import get_cart_presenter
        from ..serializers import CartResponseSerializer

        fresh = get_cart(user_id)
        cart_data = CartResponseSerializer(get_cart_presenter()().present(fresh)).data
        raise OrdersError(409, refusal_key, {"cart": cart_data})
    order.refresh_from_db()
    return order, True
