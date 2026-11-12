## What this is

Cart, checkout and orders for a Stapel shop — over **opaque purchasables**.
The module ships knowing nothing about what is sold: a host registers each
purchasable type with a comm Function that resolves a keyed batch of keys to
title, price, currency and availability. Orders never imports a product
model, so a listing, a spare part from a custom app, or anything else is
bought through one cart.

## Quick start

```python
INSTALLED_APPS = [
    # ...
    "stapel_orders",
]

STAPEL_ORDERS = {
    "PURCHASABLE_TYPES": {
        "listing": {"resolve": "listings.purchasables"},  # stapel-shop preset default
    },
}

# urls.py
path("orders/api/", include("stapel_orders.urls"))   # -> /orders/api/v1/...

# settings.py — the abandoned-cart purge and the checkout-hook sweep (Celery optional)
from stapel_orders.beat import get_orders_beat_schedule
CELERY_BEAT_SCHEDULE = {**get_orders_beat_schedule()}
```

A custom product type is one resolver and one registration:

```python
from stapel_core.comm import function

@function("parts.purchasables")
def purchasables(payload):
    parts = Part.objects.filter(pk__in=payload["keys"])
    return {str(p.pk): {"title": p.name, "price": str(p.price), "currency": "USD",
                        "available": p.stock > 0, "max_quantity": p.stock,
                        "seller_id": None, "image": None, "url": f"/parts/{p.pk}"}
            for p in parts}

# AppConfig.ready()
from stapel_orders.registry import register_purchasable_type
register_purchasable_type("part", {"resolve": "parts.purchasables"})
```

## Concepts

- **Purchasable** — an opaque pair `(purchasable_type, purchasable_key)`.
  The resolver answers on add and at checkout only; a cart line is a
  snapshot, so `GET cart/` makes zero comm calls.
- **Cart** — one per user. A stapel-auth guest is a user too, and
  `user.merged` folds the guest's cart into the account on sign-in (same
  item summed, capped at `MAX_LINE_QUANTITY`). One currency per cart; a line
  in another currency is refused, never converted.
- **Checkout** — one idempotent call (`idempotency_key`). Every line is
  re-priced; a changed price, a vanished item or short stock refreshes the
  cart and refuses with it in `params.cart`, so the shopper is never charged
  a number they did not see. Guests are asked to sign in unless
  `CHECKOUT_REQUIRES_ACCOUNT` is off.
- **Checkout seams** — commerce companions plug in by settings, never by
  import. `ADJUSTERS` (discounts, shipping, tax) price the cart; `POST
  cart/quote/` shows their lines and errors with the client's opaque
  `context` (promo codes, delivery choice), and checkout re-quotes, refuses
  on any error and snapshots the adjustments: `total = max(0, subtotal +
  adjustments)`. `CHECKOUT_HOOKS` (stock, redemptions) reserve before the
  order is written, commit once it is placed, release on failure or cancel;
  a crashed checkout is released by the hook sweep. Both registries ship
  empty, and a silent companion refuses the checkout instead of being
  skipped.
- **Order** — `new → confirmed → shipped → completed`, cancel from `new` by
  the buyer (`OWNER_CANCELLABLE_STATUSES`), staff for the rest, `needs_review`
  when a payment does not match. History is anchor-paginated.
- **Money** — `Decimal` inside, strings on the wire. Line and cart limits
  are a proof that a total cannot overflow its column
  (`tests/test_money_bounds.py`, system check `E005`).
- **Payment** — a provider seam: `none` (staff confirm by hand) or `mock`
  (prototype posture only). The outcome always arrives as
  `payment.completed`, the one path that marks an order paid.
- **GDPR** — erasure deletes carts and pseudonymizes the person on orders;
  amounts stay.
