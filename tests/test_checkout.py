"""Checkout: re-pricing, idempotency, refusals, events, payment modes."""
import re
import uuid
from decimal import Decimal

import pytest

from stapel_orders.models import Cart, CartLine, CheckoutAttempt, Order

LINES = "/orders/api/v1/cart/lines/"
CHECKOUT = "/orders/api/v1/checkout/"

pytestmark = pytest.mark.django_db


def add(client, key, quantity=1):
    resp = client.post(
        LINES, {"purchasable_type": "item", "purchasable_key": key, "quantity": quantity},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    return resp


def body(key=None, **extra):
    data = {
        "idempotency_key": str(key or uuid.uuid4()),
        "contact": {"name": "Ann Buyer", "phone": "+1 555 0100", "email": "ann@example.com"},
        "shipping_address": {"line1": "1 Main St", "city": "Springfield", "country": "us"},
        "shipping_method": "courier",
        "comment": "Leave at the door",
    }
    data.update(extra)
    return data


class TestPlace:
    def test_checkout_places_an_order_and_empties_the_cart(self, auth_client, user, catalog):
        catalog.put("a", price="10.00")
        catalog.put("b", price="2.25")
        add(auth_client, "a", 2)
        add(auth_client, "b", 3)
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 201, resp.content
        order = resp.json()
        assert order["status"] == "new"
        assert order["payment_status"] == "unpaid"
        assert order["total"] == "26.75"
        assert order["subtotal"] == "26.75"
        assert order["currency"] == "USD"
        assert re.fullmatch(r"\d{8}-[A-Z2-7]{6}", order["number"])
        assert order["shipping_address"]["country"] == "US"
        assert order["contact"]["name"] == "Ann Buyer"
        assert order["cancellable"] is True
        assert [line["quantity"] for line in order["lines"]] == [2, 3]
        assert not CartLine.objects.exists()
        assert Cart.objects.get(user_id=str(user.pk)).currency == ""

    def test_number_is_not_the_pk(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        order = auth_client.post(CHECKOUT, body(), format="json").json()
        assert order["number"] != str(order["id"])

    def test_empty_cart_is_400(self, auth_client, catalog):
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.cart_empty"

    def test_address_lengths_are_enforced(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        data = body()
        data["shipping_address"]["city"] = "x" * 101
        assert auth_client.post(CHECKOUT, data, format="json").status_code == 400
        data = body()
        data["comment"] = "x" * 501
        assert auth_client.post(CHECKOUT, data, format="json").status_code == 400

    def test_total_base_is_null_without_a_converter_answer(self, auth_client, catalog):
        catalog.put("eur", currency="EUR")
        add(auth_client, "eur")
        order_id = auth_client.post(CHECKOUT, body(), format="json").json()["id"]
        order = Order.objects.get(pk=order_id)
        assert order.total_base is None
        assert order.base_currency == "USD"

    def test_total_base_equals_total_in_base_currency(self, auth_client, catalog):
        catalog.put("a", price="5.00")
        add(auth_client, "a")
        order_id = auth_client.post(CHECKOUT, body(), format="json").json()["id"]
        assert Order.objects.get(pk=order_id).total_base == Decimal("5.00")


class TestIdempotency:
    def test_replay_returns_the_same_order_with_200(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        key = uuid.uuid4()
        first = auth_client.post(CHECKOUT, body(key), format="json")
        assert first.status_code == 201
        # The cart is empty now; a replay must not answer cart_empty.
        again = auth_client.post(CHECKOUT, body(key), format="json")
        assert again.status_code == 200
        assert again.json()["id"] == first.json()["id"]
        assert Order.objects.count() == 1
        assert CheckoutAttempt.objects.count() == 1

    def test_a_new_key_is_a_new_checkout(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        auth_client.post(CHECKOUT, body(), format="json")
        add(auth_client, "a")
        assert auth_client.post(CHECKOUT, body(), format="json").status_code == 201
        assert Order.objects.count() == 2

    def test_keys_are_per_user(self, client_for, user, other_user, catalog):
        catalog.put("a")
        key = uuid.uuid4()
        for u in (user, other_user):
            c = client_for(u)
            add(c, "a")
            assert c.post(CHECKOUT, body(key), format="json").status_code == 201
        assert Order.objects.count() == 2


class TestRepricing:
    def test_price_drift_is_409_with_the_refreshed_cart(self, auth_client, catalog):
        catalog.put("a", price="10.00")
        add(auth_client, "a", 2)
        catalog.put("a", price="12.00")
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 409
        err = resp.json()
        assert err["localizable_error"] == "error.409.cart_price_changed"
        cart = err["params"]["cart"]
        assert cart["lines"][0]["unit_price"] == "12.00"
        assert cart["total"] == "24.00"
        assert not Order.objects.exists()
        # The snapshot was committed: the next submit succeeds at the new price.
        again = auth_client.post(CHECKOUT, body(), format="json")
        assert again.status_code == 201
        assert again.json()["total"] == "24.00"

    def test_vanished_purchasable_is_409(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        del catalog.items["a"]
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.purchasable_unavailable"

    def test_unavailable_is_409(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        catalog.put("a", available=False)
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.purchasable_unavailable"

    def test_short_stock_clamps_and_asks_again(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a", 5)
        catalog.put("a", max_quantity=2)
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 409
        assert resp.json()["params"]["cart"]["lines"][0]["quantity"] == 2
        assert auth_client.post(CHECKOUT, body(), format="json").status_code == 201

    def test_currency_change_is_409(self, auth_client, catalog):
        catalog.put("a", currency="USD")
        add(auth_client, "a")
        catalog.put("a", currency="EUR")
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.cart_currency_mismatch"

    def test_resolver_unreachable_is_503_never_200(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        catalog.fail = True
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == "error.503.purchasable_unavailable"
        assert not Order.objects.exists()
        assert CartLine.objects.count() == 1

    def test_deregistered_type_is_unavailable(self, auth_client, catalog, settings):
        catalog.put("a")
        add(auth_client, "a")
        settings.STAPEL_ORDERS = {"PURCHASABLE_TYPES": {"item": None, "other": {"resolve": "x.y"}}}
        resp = auth_client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 409


class TestGuests:
    def test_guest_checkout_is_refused_by_default(self, client_for, guest, catalog):
        catalog.put("a")
        client = client_for(guest)
        add(client, "a")
        resp = client.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 403
        err = resp.json()
        assert err["localizable_error"] == "error.403.checkout_requires_account"
        assert not Order.objects.exists()

    def test_guest_refusal_comes_before_validation(self, client_for, guest, catalog):
        assert client_for(guest).post(CHECKOUT, {}, format="json").status_code == 403

    def test_guest_checkout_when_the_switch_is_off(self, client_for, guest, catalog, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "CHECKOUT_REQUIRES_ACCOUNT": False}
        catalog.put("a")
        client = client_for(guest)
        add(client, "a")
        assert client.post(CHECKOUT, body(), format="json").status_code == 201

    def test_anonymous_without_a_session_is_refused(self, api_client, catalog):
        assert api_client.post(CHECKOUT, body(), format="json").status_code in (401, 403)


class TestEvents:
    def test_placed_event_carries_lines_and_no_pii(self, auth_client, user, catalog, captured_events):
        catalog.put("a", price="3.00", seller_id="s-9")
        add(auth_client, "a", 2)
        order = auth_client.post(CHECKOUT, body(), format="json").json()
        placed = [e for e in captured_events if e.event_type == "orders.order.placed"]
        assert len(placed) == 1
        payload = placed[0].payload
        assert payload["order_id"] == str(order["id"])
        assert payload["user_id"] == str(user.pk)
        assert payload["total"] == "6.00"
        assert payload["lines"] == [{
            "purchasable_type": "item", "purchasable_key": "a", "quantity": 2,
            "unit_price": "3.00", "seller_id": "s-9",
        }]
        flat = str(payload)
        assert "Ann Buyer" not in flat and "Main St" not in flat

    def test_replay_does_not_emit_twice(self, auth_client, catalog, captured_events):
        catalog.put("a")
        add(auth_client, "a")
        key = uuid.uuid4()
        auth_client.post(CHECKOUT, body(key), format="json")
        auth_client.post(CHECKOUT, body(key), format="json")
        assert len([e for e in captured_events if e.event_type == "orders.order.placed"]) == 1

    def test_buyer_is_notified(self, auth_client, catalog, monkeypatch):
        sent = []
        import stapel_core.notifications.publish as publish

        monkeypatch.setattr(publish, "request_notification",
                            lambda t, **kw: sent.append((t, kw)) or True)
        catalog.put("a")
        add(auth_client, "a")
        auth_client.post(CHECKOUT, body(), format="json")
        # pytest-django's transaction never commits; on_commit is run by hand.
        from django.db import connection

        for _, callback, _ in list(connection.run_on_commit):
            callback()
        assert [t for t, _ in sent] == ["orders.placed"]


class TestPaymentModes:
    def test_none_leaves_the_order_unpaid(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        assert auth_client.post(CHECKOUT, body(), format="json").json()["payment_status"] == "unpaid"

    def test_mock_pays_through_the_consumer(self, auth_client, catalog, settings, captured_events):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "mock"}
        catalog.put("a", price="7.00")
        add(auth_client, "a")
        order = auth_client.post(CHECKOUT, body(), format="json").json()
        assert order["payment_status"] == "paid"
        assert Order.objects.get(pk=order["id"]).provider_ref == f"mock-{order['number']}"
        names = [e.event_type for e in captured_events]
        assert names == ["orders.order.placed", "orders.order.paid"]


class TestContactEmail:
    def test_bad_email_is_400(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        data = body()
        data["contact"]["email"] = "not-an-email"
        assert auth_client.post(CHECKOUT, data, format="json").status_code == 400

    def test_no_email_is_fine(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        data = body()
        del data["contact"]["email"]
        resp = auth_client.post(CHECKOUT, data, format="json")
        assert resp.status_code == 201
        assert resp.json()["contact"]["email"] == ""
