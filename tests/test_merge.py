"""user.merged: the guest's cart joins the survivor's; orders follow."""
import pytest
from stapel_core.comm import emit, mutate_and_emit

from stapel_orders.models import Cart, CartLine, CheckoutAttempt, Order
from stapel_orders.services import cart as cart_service

pytestmark = pytest.mark.django_db


def merged(guest, survivor):
    with mutate_and_emit():
        emit("user.merged", {"from_user_id": str(guest.pk), "into_user_id": str(survivor.pk)})


def quantities(user):
    return {
        line.purchasable_key: line.quantity
        for line in CartLine.objects.filter(cart__user_id=str(user.pk))
    }


def test_lines_are_moved_and_same_pairs_summed(guest, user, catalog):
    for key in "abc":
        catalog.put(key)
    cart_service.add_line(str(guest.pk), "item", "a", 2)
    cart_service.add_line(str(guest.pk), "item", "b", 1)
    cart_service.add_line(str(user.pk), "item", "a", 3)
    cart_service.add_line(str(user.pk), "item", "c", 1)
    merged(guest, user)
    assert quantities(user) == {"a": 5, "b": 1, "c": 1}
    assert not Cart.objects.filter(user_id=str(guest.pk)).exists()


def test_sum_is_capped(guest, user, catalog):
    catalog.put("a")
    cart_service.add_line(str(guest.pk), "item", "a", 900)
    cart_service.add_line(str(user.pk), "item", "a", 900)
    merged(guest, user)
    assert quantities(user) == {"a": 999}


def test_survivor_without_a_cart_takes_the_guest_cart(guest, user, catalog):
    catalog.put("a")
    cart_service.add_line(str(guest.pk), "item", "a", 2)
    merged(guest, user)
    assert quantities(user) == {"a": 2}


def test_other_currency_lines_are_dropped(guest, user, catalog):
    catalog.put("usd", currency="USD")
    catalog.put("eur", currency="EUR")
    cart_service.add_line(str(guest.pk), "item", "eur", 1)
    cart_service.add_line(str(user.pk), "item", "usd", 1)
    merged(guest, user)
    assert quantities(user) == {"usd": 1}
    assert Cart.objects.get(user_id=str(user.pk)).currency == "USD"


def test_merge_is_idempotent(guest, user, catalog):
    catalog.put("a")
    cart_service.add_line(str(guest.pk), "item", "a", 2)
    cart_service.add_line(str(user.pk), "item", "a", 1)
    merged(guest, user)
    merged(guest, user)
    assert quantities(user) == {"a": 3}


def test_orders_follow_the_survivor(guest, user):
    order = Order.objects.create(
        number="20260101-AAAAAA", user_id=str(guest.pk), currency="USD",
        subtotal=1, total=1, contact_name="n", contact_phone="p",
        address_line1="l", address_city="c", address_country="US",
    )
    CheckoutAttempt.objects.create(
        user_id=str(guest.pk), idempotency_key="00000000-0000-0000-0000-000000000001",
        order=order,
    )
    merged(guest, user)
    order.refresh_from_db()
    assert order.user_id == str(user.pk)
    assert CheckoutAttempt.objects.get().user_id == str(user.pk)


def test_the_lifecycle_pair_check_is_green():
    from django.core import checks

    found = [m for m in checks.run_checks(tags=["stapel_lifecycle"]) if "orders" in m.msg]
    assert found == []
