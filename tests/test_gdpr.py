"""Erasure keeps the economics and pseudonymizes the person."""
from decimal import Decimal

import pytest
from stapel_core.gdpr import gdpr_registry, registered_gdpr_owners

from stapel_orders.gdpr import OrdersGDPRProvider, erase_subject
from stapel_orders.models import Cart, CheckoutAttempt, Order, OrderLine
from stapel_orders.services import cart as cart_service

from .factories import make_order

pytestmark = pytest.mark.django_db


def test_owner_and_provider_are_registered():
    assert registered_gdpr_owners()["orders"] == ("account",)
    assert any(p.section == "orders" for p in gdpr_registry.providers)


def test_erase_keeps_totals_and_pseudonymizes_contact(user, catalog):
    catalog.put("a")
    cart_service.add_line(str(user.pk), "item", "a", 1)
    order = make_order(user, total="42.00")
    CheckoutAttempt.objects.create(
        user_id=str(user.pk), idempotency_key="00000000-0000-0000-0000-000000000009", order=order
    )
    counts = erase_subject("account", str(user.pk))
    assert counts == {"carts": 1, "orders": 1, "checkout_attempts": 1}
    order.refresh_from_db()
    assert order.total == Decimal("42.00")
    assert order.lines.count() == 1
    assert order.user_id.startswith("erased:")
    assert order.user_id != str(user.pk)
    for field in ("contact_name", "contact_phone", "contact_email", "address_line1",
                  "address_city", "address_country", "comment"):
        assert getattr(order, field) in ("", "erased"), field
    assert not Cart.objects.exists()
    assert OrderLine.objects.get().unit_price == Decimal("42.00")


def test_erase_is_idempotent(user):
    make_order(user)
    erase_subject("account", str(user.pk))
    pseudonym = Order.objects.get().user_id
    assert erase_subject("account", str(user.pk)) == {"carts": 0, "orders": 0, "checkout_attempts": 0}
    assert Order.objects.get().user_id == pseudonym


def test_other_subject_types_are_not_claimed(user):
    assert erase_subject("workspace", "w1") is None


def test_provider_delete_and_export(user, catalog):
    catalog.put("a")
    cart_service.add_line(str(user.pk), "item", "a", 2)
    make_order(user)
    exported = OrdersGDPRProvider().export(str(user.pk))
    assert exported["carts"][0]["lines"][0]["quantity"] == 2
    assert exported["orders"][0]["contact"]["name"] == "Ann Buyer"
    OrdersGDPRProvider().delete(str(user.pk))
    assert OrdersGDPRProvider().export(str(user.pk)) == {"carts": [], "orders": []}
