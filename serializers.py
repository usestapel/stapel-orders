"""Serializers for the stapel-orders API.

The class names are the codegen contract: drf-spectacular names each
component after its serializer minus the ``Serializer`` suffix
(``CartResponse``, ``OrderPage``, …), and the frontend pair generates its
client from those names. Every CharField declares a ``max_length``; money
is a ``DecimalField`` rendered as a string.
"""
from django.core.validators import EmailValidator
from rest_framework import serializers

from . import money
from . import models as m


def _money(max_digits: int, **kwargs):
    return serializers.DecimalField(
        max_digits=max_digits,
        decimal_places=money.DECIMAL_PLACES,
        coerce_to_string=True,
        **kwargs,
    )


# ── Cart ──────────────────────────────────────────────────────────────────


class CartLineSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    purchasable_type = serializers.CharField(max_length=m.PURCHASABLE_TYPE_MAX_LENGTH)
    purchasable_key = serializers.CharField(max_length=m.PURCHASABLE_KEY_MAX_LENGTH)
    title = serializers.CharField(max_length=m.TITLE_MAX_LENGTH, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1)
    unit_price = _money(money.UNIT_PRICE_MAX_DIGITS)
    currency = serializers.CharField(max_length=m.CURRENCY_MAX_LENGTH)
    line_total = _money(money.LINE_TOTAL_MAX_DIGITS)
    image = serializers.CharField(max_length=m.URL_MAX_LENGTH, allow_null=True)
    url = serializers.CharField(max_length=m.URL_MAX_LENGTH, allow_null=True)
    seller_id = serializers.CharField(max_length=m.SELLER_ID_MAX_LENGTH, allow_null=True)


class CartResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField(allow_null=True)
    currency = serializers.CharField(max_length=m.CURRENCY_MAX_LENGTH, allow_null=True)
    lines = CartLineSerializer(many=True)
    line_count = serializers.IntegerField(min_value=0)
    subtotal = _money(money.ORDER_TOTAL_MAX_DIGITS)
    total = _money(money.ORDER_TOTAL_MAX_DIGITS)
    updated_at = serializers.DateTimeField(allow_null=True)


class CartLineAddRequestSerializer(serializers.Serializer):
    purchasable_type = serializers.CharField(max_length=m.PURCHASABLE_TYPE_MAX_LENGTH)
    purchasable_key = serializers.CharField(max_length=m.PURCHASABLE_KEY_MAX_LENGTH)
    # The service caps at MAX_LINE_QUANTITY; the wire bound is the column's.
    quantity = serializers.IntegerField(min_value=1, max_value=2_147_483_647, default=1)


class CartLineUpdateRequestSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=0, max_value=2_147_483_647)


# ── Checkout ──────────────────────────────────────────────────────────────


class ContactSnapshotSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=m.CONTACT_NAME_MAX_LENGTH)
    phone = serializers.CharField(max_length=m.CONTACT_PHONE_MAX_LENGTH)
    # A plain CharField checked in validate_email: an email-formatted field
    # with allow_blank emits a oneOf the empty string satisfies twice, so the
    # schema would reject the very body the API sends for "no email".
    email = serializers.CharField(
        max_length=m.CONTACT_EMAIL_MAX_LENGTH, required=False, allow_blank=True, default=""
    )

    def validate_email(self, value):
        if value:
            EmailValidator()(value)
        return value


class AddressSnapshotSerializer(serializers.Serializer):
    line1 = serializers.CharField(max_length=m.ADDRESS_LINE_MAX_LENGTH)
    line2 = serializers.CharField(
        max_length=m.ADDRESS_LINE_MAX_LENGTH, required=False, allow_blank=True, default=""
    )
    city = serializers.CharField(max_length=m.CITY_MAX_LENGTH)
    region = serializers.CharField(
        max_length=m.REGION_MAX_LENGTH, required=False, allow_blank=True, default=""
    )
    postal_code = serializers.CharField(
        max_length=m.POSTAL_CODE_MAX_LENGTH, required=False, allow_blank=True, default=""
    )
    country = serializers.RegexField(
        r"^[A-Za-z]{2}$", max_length=m.COUNTRY_MAX_LENGTH, min_length=m.COUNTRY_MAX_LENGTH
    )


class CheckoutRequestSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField()
    contact = ContactSnapshotSerializer()
    shipping_address = AddressSnapshotSerializer()
    shipping_method = serializers.CharField(
        max_length=m.SHIPPING_METHOD_MAX_LENGTH, required=False, allow_blank=True, default=""
    )
    comment = serializers.CharField(
        max_length=m.COMMENT_MAX_LENGTH, required=False, allow_blank=True, default=""
    )


# ── Orders ────────────────────────────────────────────────────────────────


class OrderLineSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    purchasable_type = serializers.CharField(max_length=m.PURCHASABLE_TYPE_MAX_LENGTH)
    purchasable_key = serializers.CharField(max_length=m.PURCHASABLE_KEY_MAX_LENGTH)
    title = serializers.CharField(max_length=m.TITLE_MAX_LENGTH, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1)
    unit_price = _money(money.UNIT_PRICE_MAX_DIGITS)
    line_total = _money(money.LINE_TOTAL_MAX_DIGITS)
    image = serializers.CharField(max_length=m.URL_MAX_LENGTH, allow_null=True)
    url = serializers.CharField(max_length=m.URL_MAX_LENGTH, allow_null=True)
    seller_id = serializers.CharField(max_length=m.SELLER_ID_MAX_LENGTH, allow_null=True)


class OrderResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField(max_length=m.ORDER_NUMBER_MAX_LENGTH)
    status = serializers.ChoiceField(choices=m.OrderStatus.choices)
    payment_status = serializers.ChoiceField(choices=m.PaymentStatus.choices)
    currency = serializers.CharField(max_length=m.CURRENCY_MAX_LENGTH)
    subtotal = _money(money.ORDER_TOTAL_MAX_DIGITS)
    total = _money(money.ORDER_TOTAL_MAX_DIGITS)
    lines = OrderLineSerializer(many=True)
    contact = ContactSnapshotSerializer()
    shipping_address = AddressSnapshotSerializer()
    shipping_method = serializers.CharField(max_length=m.SHIPPING_METHOD_MAX_LENGTH, allow_blank=True)
    comment = serializers.CharField(max_length=m.COMMENT_MAX_LENGTH, allow_blank=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    cancellable = serializers.BooleanField()


class OrderPageSerializer(serializers.Serializer):
    """The AnchorPagination envelope ``GET orders/`` returns (schema only —
    the view's paginator builds the body)."""

    items = OrderResponseSerializer(many=True)
    next_anchor = serializers.IntegerField(allow_null=True)
    prev_anchor = serializers.IntegerField(allow_null=True)
    has_next = serializers.BooleanField()
    has_prev = serializers.BooleanField()
    count = serializers.IntegerField()


class OrderTransitionRequestSerializer(serializers.Serializer):
    to = serializers.ChoiceField(choices=m.OrderStatus.choices)
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
