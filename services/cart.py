"""Cart services — keyed on ``str(user.pk)``; a guest row is a user too.

Only ``add_line`` asks the resolver (and checkout). Every write locks the
cart row (``select_for_update``) so two concurrent adds of one purchasable
end as one line with the summed quantity; the unique constraint on
``(cart, type, key)`` is the backstop when the cart row itself is new.
"""
from __future__ import annotations

import datetime
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .. import money, registry
from ..errors import (
    ERR_400_PURCHASABLE_TYPE_UNKNOWN,
    ERR_400_PURCHASABLE_UNKNOWN,
    ERR_404_CART_LINE_NOT_FOUND,
    ERR_409_CART_CURRENCY_MISMATCH,
    ERR_409_CART_TOO_MANY_LINES,
    ERR_409_PURCHASABLE_UNAVAILABLE,
    ERR_503_PURCHASABLE_UNAVAILABLE,
)
from ..models import Cart, CartLine
from . import OrdersError, events

logger = logging.getLogger(__name__)


def limits() -> tuple[int, int]:
    """``(MAX_LINE_QUANTITY, MAX_LINES_PER_CART)`` as effective here."""
    from ..conf import orders_settings

    return int(orders_settings.MAX_LINE_QUANTITY), int(orders_settings.MAX_LINES_PER_CART)


def get_cart(user_id: str) -> Cart | None:
    """The user's cart or ``None`` — a read, never a write, never a comm call."""
    return Cart.objects.filter(user_id=str(user_id)).first()


def cart_lines(cart: Cart | None) -> list[CartLine]:
    return [] if cart is None else list(cart.lines.all())


def locked_cart(user_id: str) -> Cart:
    """The user's cart row, locked for this transaction, created if absent."""
    user_id = str(user_id)
    cart = Cart.objects.select_for_update().filter(user_id=user_id).first()
    if cart is not None:
        return cart
    try:
        with transaction.atomic():
            return Cart.objects.create(user_id=user_id)
    except IntegrityError:
        # A concurrent first add created it between our read and insert.
        return Cart.objects.select_for_update().get(user_id=user_id)


def _snapshot(line: CartLine, item: registry.Resolved) -> None:
    line.title = item.title
    line.unit_price = item.price
    line.currency = item.currency
    line.image = item.image
    line.url = item.url
    line.seller_id = item.seller_id
    line.line_total = money.line_total(item.price, line.quantity)


def _resolve_one(purchasable_type: str, purchasable_key: str) -> registry.Resolved:
    try:
        found = registry.resolve(purchasable_type, [purchasable_key])
    except registry.UnknownPurchasableType:
        raise OrdersError(400, ERR_400_PURCHASABLE_TYPE_UNKNOWN) from None
    except registry.ResolverUnavailable:
        logger.warning("orders: resolver for %r unavailable", purchasable_type, exc_info=True)
        raise OrdersError(503, ERR_503_PURCHASABLE_UNAVAILABLE) from None
    item = found.get(str(purchasable_key))
    if item is None:
        raise OrdersError(400, ERR_400_PURCHASABLE_UNKNOWN)
    if not item.available:
        raise OrdersError(409, ERR_409_PURCHASABLE_UNAVAILABLE)
    return item


def _line_cap(item: registry.Resolved) -> int:
    cap, _ = limits()
    if item.max_quantity is not None:
        cap = min(cap, item.max_quantity)
    return cap


def add_line(user_id: str, purchasable_type: str, purchasable_key: str, quantity: int = 1) -> Cart:
    """Add *quantity* of a purchasable; an existing line of the pair grows, capped.

    The resolver is asked BEFORE the transaction opens, so no lock is held
    across a network call.
    """
    purchasable_key = str(purchasable_key)
    item = _resolve_one(purchasable_type, purchasable_key)
    cap = _line_cap(item)
    if cap <= 0:
        raise OrdersError(409, ERR_409_PURCHASABLE_UNAVAILABLE)
    _, max_lines = limits()

    with transaction.atomic():
        cart = locked_cart(user_id)
        lines = cart.lines.all()
        existing = lines.filter(
            purchasable_type=purchasable_type, purchasable_key=purchasable_key
        ).first()
        if cart.currency and cart.currency != item.currency and lines.exists():
            raise OrdersError(409, ERR_409_CART_CURRENCY_MISMATCH)
        if existing is None:
            if lines.count() >= max_lines:
                raise OrdersError(409, ERR_409_CART_TOO_MANY_LINES, {"max": max_lines})
            line = CartLine(
                cart=cart,
                purchasable_type=purchasable_type,
                purchasable_key=purchasable_key,
                quantity=min(quantity, cap),
            )
            _snapshot(line, item)
            try:
                with transaction.atomic():
                    line.save()
            except IntegrityError:
                # The pair landed concurrently: grow that line instead.
                existing = CartLine.objects.select_for_update().get(
                    cart=cart,
                    purchasable_type=purchasable_type,
                    purchasable_key=purchasable_key,
                )
        if existing is not None:
            existing.quantity = min(existing.quantity + quantity, cap)
            _snapshot(existing, item)
            existing.save()
        cart.currency = item.currency
        cart.save(update_fields=["currency", "updated_at"])
        events.cart_changed(cart.user_id, cart.lines.count())
    return cart


def _owned_line(user_id: str, line_id: int) -> tuple[Cart, CartLine]:
    cart = Cart.objects.select_for_update().filter(user_id=str(user_id)).first()
    line = None
    if cart is not None:
        line = CartLine.objects.filter(cart=cart, pk=line_id).first()
    if line is None:
        raise OrdersError(404, ERR_404_CART_LINE_NOT_FOUND)
    return cart, line


def _after_removal(cart: Cart) -> None:
    remaining = cart.lines.count()
    if remaining == 0:
        cart.currency = ""
    cart.save(update_fields=["currency", "updated_at"])
    events.cart_changed(cart.user_id, remaining)


def update_line(user_id: str, line_id: int, quantity: int) -> Cart:
    """Set a line's quantity (``0`` deletes it). No resolver call."""
    cap, _ = limits()
    with transaction.atomic():
        cart, line = _owned_line(user_id, line_id)
        if quantity <= 0:
            line.delete()
            _after_removal(cart)
            return cart
        line.quantity = min(quantity, cap)
        line.line_total = money.line_total(line.unit_price, line.quantity)
        line.save(update_fields=["quantity", "line_total", "updated_at"])
        cart.save(update_fields=["updated_at"])
        events.cart_changed(cart.user_id, cart.lines.count())
    return cart


def remove_line(user_id: str, line_id: int) -> Cart:
    with transaction.atomic():
        cart, line = _owned_line(user_id, line_id)
        line.delete()
        _after_removal(cart)
    return cart


def clear_cart(user_id: str) -> Cart | None:
    with transaction.atomic():
        cart = Cart.objects.select_for_update().filter(user_id=str(user_id)).first()
        if cart is None:
            return None
        cart.lines.all().delete()
        _after_removal(cart)
    return cart


def line_count(user_id: str) -> int:
    return CartLine.objects.filter(cart__user_id=str(user_id)).count()


def purge_carts(ttl_days: int | None = None, *, now: datetime.datetime | None = None) -> int:
    """Delete carts untouched for ``CART_TTL_DAYS``. Returns carts removed."""
    from ..conf import orders_settings

    days = int(orders_settings.CART_TTL_DAYS if ttl_days is None else ttl_days)
    cutoff = (now or timezone.now()) - datetime.timedelta(days=days)
    stale = Cart.objects.filter(updated_at__lt=cutoff)
    count = stale.count()
    stale.delete()
    return count


def merge_carts(from_user_id: str, into_user_id: str) -> dict:
    """Fold a guest's cart into the survivor's (the ``user.merged`` rule).

    Same pair -> quantities summed, capped at ``MAX_LINE_QUANTITY``; a line in
    another currency than the survivor's cart is dropped (logged); lines past
    ``MAX_LINES_PER_CART`` are dropped (logged). Idempotent: a second run finds
    no guest cart. Runs inside the caller's transaction.
    """
    cap, max_lines = limits()
    guest = Cart.objects.select_for_update().filter(user_id=str(from_user_id)).first()
    if guest is None:
        return {"moved": 0, "summed": 0, "dropped": 0}
    guest_lines = list(guest.lines.all())
    survivor = Cart.objects.select_for_update().filter(user_id=str(into_user_id)).first()
    if survivor is None:
        guest.user_id = str(into_user_id)
        guest.save(update_fields=["user_id", "updated_at"])
        return {"moved": len(guest_lines), "summed": 0, "dropped": 0}

    moved = summed = dropped = 0
    survivor_lines = {
        (line.purchasable_type, line.purchasable_key): line for line in survivor.lines.all()
    }
    count = len(survivor_lines)
    for line in guest_lines:
        pair = (line.purchasable_type, line.purchasable_key)
        target = survivor_lines.get(pair)
        if target is not None:
            target.quantity = min(target.quantity + line.quantity, cap)
            target.line_total = money.line_total(target.unit_price, target.quantity)
            target.save(update_fields=["quantity", "line_total", "updated_at"])
            line.delete()
            summed += 1
            continue
        if survivor.currency and count and line.currency != survivor.currency:
            logger.info(
                "orders: user.merged %s -> %s dropped %s:%s (currency %s, cart in %s)",
                from_user_id, into_user_id, *pair, line.currency, survivor.currency,
            )
            line.delete()
            dropped += 1
            continue
        if count >= max_lines:
            logger.info(
                "orders: user.merged %s -> %s dropped %s:%s (cart holds %s lines)",
                from_user_id, into_user_id, *pair, max_lines,
            )
            line.delete()
            dropped += 1
            continue
        line.cart = survivor
        line.save(update_fields=["cart", "updated_at"])
        survivor_lines[pair] = line
        if count == 0:
            survivor.currency = line.currency
        count += 1
        moved += 1
    guest.delete()
    survivor.save(update_fields=["currency", "updated_at"])
    return {"moved": moved, "summed": summed, "dropped": dropped}
