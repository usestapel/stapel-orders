"""Models of stapel-orders.

A purchasable is an opaque pair ``(purchasable_type, purchasable_key)``;
nothing here has a foreign key into a sibling module. Users are referenced
by ``user_id`` (``str(user.pk)``), not a foreign key: an erased order keeps
its economics under a pseudonymized id, which a foreign key cannot hold.

Cart lines and order lines are snapshots taken from the resolver (title,
price, currency, image, seller) — reading a cart never calls a sibling.
"""
from __future__ import annotations

from django.db import models
from stapel_core.access import access

from . import money

USER_ID_MAX_LENGTH = 64
PURCHASABLE_TYPE_MAX_LENGTH = 32
PURCHASABLE_KEY_MAX_LENGTH = 255
TITLE_MAX_LENGTH = 255
URL_MAX_LENGTH = 500
CURRENCY_MAX_LENGTH = 3
SELLER_ID_MAX_LENGTH = 64
ORDER_NUMBER_MAX_LENGTH = 16

# Snapshot field lengths (spec §5).
CONTACT_NAME_MAX_LENGTH = 120
CONTACT_PHONE_MAX_LENGTH = 32
CONTACT_EMAIL_MAX_LENGTH = 254
ADDRESS_LINE_MAX_LENGTH = 200
CITY_MAX_LENGTH = 100
REGION_MAX_LENGTH = 100
POSTAL_CODE_MAX_LENGTH = 20
COUNTRY_MAX_LENGTH = 2
SHIPPING_METHOD_MAX_LENGTH = 32
COMMENT_MAX_LENGTH = 500
PROVIDER_REF_MAX_LENGTH = 128
SEAM_NAME_MAX_LENGTH = 64
ADJUSTMENT_CODE_MAX_LENGTH = 64
HOOK_TOKEN_MAX_LENGTH = 255
HOOK_ERROR_MAX_LENGTH = 255


def _price_field(**kwargs):
    return models.DecimalField(
        max_digits=money.UNIT_PRICE_MAX_DIGITS,
        decimal_places=money.DECIMAL_PLACES,
        **kwargs,
    )


def _line_total_field(**kwargs):
    return models.DecimalField(
        max_digits=money.LINE_TOTAL_MAX_DIGITS,
        decimal_places=money.DECIMAL_PLACES,
        **kwargs,
    )


def _total_field(**kwargs):
    return models.DecimalField(
        max_digits=money.ORDER_TOTAL_MAX_DIGITS,
        decimal_places=money.DECIMAL_PLACES,
        **kwargs,
    )


class OrderStatus(models.TextChoices):
    NEW = "new", "New"
    CONFIRMED = "confirmed", "Confirmed"
    SHIPPED = "shipped", "Shipped"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    NEEDS_REVIEW = "needs_review", "Needs review"
    #: Staff refunded a paid order in full (payment_status refunded too).
    REFUNDED = "refunded", "Refunded"


class AdjustmentKind(models.TextChoices):
    DISCOUNT = "discount", "Discount"
    SHIPPING = "shipping", "Shipping"
    TAX = "tax", "Tax"
    FEE = "fee", "Fee"


class HookState(models.TextChoices):
    #: Durable before reserve is called; no order yet. Old ones are orphans.
    PENDING = "pending", "Pending"
    #: Written with the order in the checkout transaction; commit not yet answered.
    PLACED = "placed", "Placed"
    COMMITTED = "committed", "Committed"
    #: Written with the cancel; release not yet answered.
    RELEASING = "releasing", "Releasing"
    RELEASED = "released", "Released"


class PaymentStatus(models.TextChoices):
    UNPAID = "unpaid", "Unpaid"
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    REFUNDED = "refunded", "Refunded"


@access.ops
class Cart(models.Model):
    """One open cart per user (a stapel-auth guest row is a user too)."""

    user_id = models.CharField(
        max_length=USER_ID_MAX_LENGTH, unique=True,
        help_text="str(user.pk) of the owner; a guest account is a user too.",
    )
    # Set by the first line; "" while empty. One currency per cart.
    currency = models.CharField(max_length=CURRENCY_MAX_LENGTH, blank=True, default="")
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the cart was first written.",
    )
    updated_at = models.DateTimeField(
        auto_now=True, db_index=True,
        help_text="Last write; drives the CART_TTL_DAYS purge.",
    )

    class Meta:
        app_label = "orders"

    def __str__(self) -> str:
        return f"Cart({self.user_id})"


@access.ops
class CartLine(models.Model):
    cart = models.ForeignKey(
        Cart, on_delete=models.CASCADE, related_name="lines",
        help_text="The cart this line belongs to.",
    )
    purchasable_type = models.CharField(
        max_length=PURCHASABLE_TYPE_MAX_LENGTH,
        help_text="Registered purchasable type (PURCHASABLE_TYPES key).",
    )
    purchasable_key = models.CharField(
        max_length=PURCHASABLE_KEY_MAX_LENGTH,
        help_text="Opaque key the type resolver understands.",
    )
    quantity = models.PositiveIntegerField(help_text="Units, capped at MAX_LINE_QUANTITY and the resolver max_quantity.")
    title = models.CharField(
        max_length=TITLE_MAX_LENGTH, blank=True, default="",
        help_text="Snapshot of the resolver title at add time.",
    )
    unit_price = _price_field()
    currency = models.CharField(
        max_length=CURRENCY_MAX_LENGTH,
        help_text="ISO 4217 code of the snapshot price.",
    )
    line_total = _line_total_field()
    image = models.CharField(
        max_length=URL_MAX_LENGTH, blank=True, default="",
        help_text="Snapshot image URL, empty when none.",
    )
    url = models.CharField(
        max_length=URL_MAX_LENGTH, blank=True, default="",
        help_text="Snapshot product URL, empty when none.",
    )
    seller_id = models.CharField(
        max_length=SELLER_ID_MAX_LENGTH, blank=True, default="",
        help_text="Snapshot seller id from the resolver, empty when none.",
    )
    attrs = models.JSONField(
        default=dict, blank=True,
        help_text="Resolver fields beyond the contract (scalars), handed to adjusters and hooks.",
    )
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the line was added.")
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Last change of quantity or snapshot.",
    )

    class Meta:
        app_label = "orders"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "purchasable_type", "purchasable_key"],
                name="orders_cartline_one_per_purchasable",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.purchasable_type}:{self.purchasable_key} x{self.quantity}"


@access.standard
class Order(models.Model):
    number = models.CharField(
        max_length=ORDER_NUMBER_MAX_LENGTH, unique=True,
        help_text="Human order number YYYYMMDD-XXXXXX; never the pk.",
    )
    user_id = models.CharField(
        max_length=USER_ID_MAX_LENGTH, db_index=True,
        help_text="str(user.pk) of the buyer; a pseudonym after erasure.",
    )
    status = models.CharField(
        max_length=16, choices=OrderStatus.choices, default=OrderStatus.NEW, db_index=True,
        help_text="Lifecycle status; moved only through the FSM (fsm.py).",
    )
    payment_status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID,
        help_text="Payment axis; set by the payment.completed consumer.",
    )
    currency = models.CharField(
        max_length=CURRENCY_MAX_LENGTH,
        help_text="ISO 4217 code of every amount on the order.",
    )
    subtotal = _total_field()
    total = _total_field()
    # Reporting only: total in BASE_CURRENCY, NULL when no converter answered.
    total_base = _total_field(null=True, blank=True)
    base_currency = models.CharField(
        max_length=CURRENCY_MAX_LENGTH, blank=True, default="",
        help_text="BASE_CURRENCY total_base is expressed in.",
    )

    contact_name = models.CharField(
        max_length=CONTACT_NAME_MAX_LENGTH,
        help_text="Contact snapshot: name.",
    )
    contact_phone = models.CharField(
        max_length=CONTACT_PHONE_MAX_LENGTH,
        help_text="Contact snapshot: phone.",
    )
    contact_email = models.CharField(
        max_length=CONTACT_EMAIL_MAX_LENGTH, blank=True, default="",
        help_text="Contact snapshot: e-mail, optional.",
    )
    address_line1 = models.CharField(
        max_length=ADDRESS_LINE_MAX_LENGTH,
        help_text="Shipping address snapshot: first line.",
    )
    address_line2 = models.CharField(
        max_length=ADDRESS_LINE_MAX_LENGTH, blank=True, default="",
        help_text="Shipping address snapshot: second line.",
    )
    address_city = models.CharField(
        max_length=CITY_MAX_LENGTH,
        help_text="Shipping address snapshot: city.",
    )
    address_region = models.CharField(
        max_length=REGION_MAX_LENGTH, blank=True, default="",
        help_text="Shipping address snapshot: region or state.",
    )
    address_postal_code = models.CharField(
        max_length=POSTAL_CODE_MAX_LENGTH, blank=True, default="",
        help_text="Shipping address snapshot: postal code.",
    )
    address_country = models.CharField(
        max_length=COUNTRY_MAX_LENGTH,
        help_text="Shipping address snapshot: ISO 3166-1 alpha-2 country.",
    )
    shipping_method = models.CharField(
        max_length=SHIPPING_METHOD_MAX_LENGTH, blank=True, default="",
        help_text="Free shipping method code chosen at checkout.",
    )
    comment = models.CharField(
        max_length=COMMENT_MAX_LENGTH, blank=True, default="",
        help_text="Buyer comment at checkout.",
    )

    provider_ref = models.CharField(
        max_length=PROVIDER_REF_MAX_LENGTH, blank=True, default="",
        help_text="Payment provider reference from payment.completed.",
    )
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the order was placed.")
    updated_at = models.DateTimeField(auto_now=True, help_text="Last status or payment change.")

    class Meta:
        app_label = "orders"
        ordering = ["-id"]
        indexes = [models.Index(fields=["user_id", "-id"], name="orders_order_user_id_desc")]

    def __str__(self) -> str:
        return f"Order {self.number} ({self.status})"


@access.standard
class OrderLine(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="lines",
        help_text="The order this line belongs to.",
    )
    purchasable_type = models.CharField(
        max_length=PURCHASABLE_TYPE_MAX_LENGTH,
        help_text="Purchasable type at checkout.",
    )
    purchasable_key = models.CharField(
        max_length=PURCHASABLE_KEY_MAX_LENGTH,
        help_text="Purchasable key at checkout.",
    )
    title = models.CharField(
        max_length=TITLE_MAX_LENGTH, blank=True, default="",
        help_text="Title frozen at checkout.",
    )
    quantity = models.PositiveIntegerField(help_text="Units ordered.")
    unit_price = _price_field()
    line_total = _line_total_field()
    image = models.CharField(
        max_length=URL_MAX_LENGTH, blank=True, default="",
        help_text="Image URL frozen at checkout.",
    )
    url = models.CharField(
        max_length=URL_MAX_LENGTH, blank=True, default="",
        help_text="Product URL frozen at checkout.",
    )
    seller_id = models.CharField(
        max_length=SELLER_ID_MAX_LENGTH, blank=True, default="",
        help_text="Seller id frozen at checkout, empty when none.",
    )

    class Meta:
        app_label = "orders"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.purchasable_type}:{self.purchasable_key} x{self.quantity}"


@access.ops
class CheckoutAttempt(models.Model):
    """Idempotency row: one order per (user, client idempotency key)."""

    user_id = models.CharField(
        max_length=USER_ID_MAX_LENGTH,
        help_text="str(user.pk) that submitted the key.",
    )
    idempotency_key = models.UUIDField(help_text="Client key; a replay returns the same order.")
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="attempts",
        help_text="The order this key produced.",
    )
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the key was first used.")

    class Meta:
        app_label = "orders"
        constraints = [
            models.UniqueConstraint(
                fields=["user_id", "idempotency_key"],
                name="orders_checkout_one_order_per_key",
            ),
        ]


@access.standard
class OrderAdjustment(models.Model):
    """One adjuster answer frozen at checkout (discount, shipping, tax, fee)."""

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="adjustments",
        help_text="The order this adjustment belongs to.",
    )
    position = models.PositiveIntegerField(help_text="Order of application (adjuster order).")
    adjuster = models.CharField(
        max_length=SEAM_NAME_MAX_LENGTH,
        help_text="ADJUSTERS name that answered it.",
    )
    kind = models.CharField(
        max_length=16, choices=AdjustmentKind.choices,
        help_text="discount, shipping, tax or fee.",
    )
    code = models.CharField(
        max_length=ADJUSTMENT_CODE_MAX_LENGTH, blank=True, default="",
        help_text="The adjuster's code (promo code, delivery method), empty when none.",
    )
    label = models.CharField(
        max_length=TITLE_MAX_LENGTH, blank=True, default="",
        help_text="Human label as the adjuster answered it.",
    )
    amount = models.DecimalField(
        max_digits=money.ADJUSTMENT_MAX_DIGITS,
        decimal_places=money.DECIMAL_PLACES,
        help_text="Signed amount in the order currency (negative lowers the total).",
    )
    line_index = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Position of the order line it applies to; NULL for the whole order.",
    )
    meta = models.JSONField(
        default=dict, blank=True,
        help_text="Adjuster's opaque details; never personal data (kept after erasure).",
    )

    class Meta:
        app_label = "orders"
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.kind} {self.amount} ({self.adjuster})"


@access.ops
class CheckoutHookState(models.Model):
    """One CHECKOUT_HOOKS reservation per (attempt key, hook name).

    Written durably before ``reserve`` is called, so a checkout that dies
    mid-way leaves a row the hook sweep can release.
    """

    attempt_key = models.UUIDField(help_text="The checkout idempotency key.")
    name = models.CharField(
        max_length=SEAM_NAME_MAX_LENGTH,
        help_text="CHECKOUT_HOOKS name.",
    )
    user_id = models.CharField(
        max_length=USER_ID_MAX_LENGTH, db_index=True,
        help_text="str(user.pk) that submitted the key.",
    )
    state = models.CharField(
        max_length=16, choices=HookState.choices, default=HookState.PENDING,
        help_text="pending -> placed -> committed; releasing -> released.",
    )
    token = models.CharField(
        max_length=HOOK_TOKEN_MAX_LENGTH, blank=True, default="",
        help_text="Token reserve answered; empty when none.",
    )
    order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="hook_states",
        help_text="The order the reservation was committed for.",
    )
    release_reason = models.CharField(
        max_length=32, blank=True, default="",
        help_text="Why release was asked: checkout_failed, cancelled, orphaned.",
    )
    tries = models.PositiveIntegerField(
        default=0, help_text="Unanswered commit/release calls so far.",
    )
    last_error = models.CharField(
        max_length=HOOK_ERROR_MAX_LENGTH, blank=True, default="",
        help_text="Last failure of a commit/release call.",
    )
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the row was written.")
    updated_at = models.DateTimeField(
        auto_now=True, db_index=True,
        help_text="Last change; a pending row older than the orphan age is released.",
    )

    class Meta:
        app_label = "orders"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["attempt_key", "name"],
                name="orders_hookstate_one_per_attempt_hook",
            ),
        ]
        indexes = [models.Index(fields=["state", "updated_at"], name="orders_hookstate_sweep")]

    def __str__(self) -> str:
        return f"{self.name}@{self.attempt_key} ({self.state})"
