"""Cart API: snapshots, caps, one currency, refusals, guest stance."""
from decimal import Decimal

import pytest

from stapel_orders.models import Cart, CartLine

CART = "/orders/api/v1/cart/"
LINES = "/orders/api/v1/cart/lines/"

pytestmark = pytest.mark.django_db


def add(client, key, quantity=1, ptype="item"):
    return client.post(
        LINES,
        {"purchasable_type": ptype, "purchasable_key": key, "quantity": quantity},
        format="json",
    )


class TestRead:
    def test_empty_cart_is_200_with_no_lines(self, auth_client, catalog):
        resp = auth_client.get(CART)
        assert resp.status_code == 200
        body = resp.json()
        assert body["lines"] == []
        assert body["line_count"] == 0
        assert body["total"] == "0.00"
        assert body["id"] is None
        assert not Cart.objects.exists()  # a read never writes

    def test_visitor_with_no_session_reads_the_empty_cart(self, api_client, catalog):
        resp = api_client.get(CART)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] is None
        assert body["lines"] == []
        assert body["line_count"] == 0
        assert body["total"] == "0.00"
        assert not Cart.objects.exists()  # a read never writes

    def test_cold_read_matches_a_guest_before_the_first_add(
        self, api_client, client_for, guest, catalog
    ):
        cold = api_client.get(CART).json()
        assert client_for(guest).get(CART).json() == cold
        assert not Cart.objects.exists()

    def test_visitor_with_no_session_cannot_write(self, api_client, catalog):
        catalog.put("a")
        assert api_client.delete(CART).status_code in (401, 403)
        assert add(api_client, "a").status_code in (401, 403)
        assert api_client.post(f"{CART}quote/", {}, format="json").status_code in (401, 403)
        assert not Cart.objects.exists()

    def test_guest_session_has_a_cart(self, client_for, guest, catalog):
        catalog.put("a")
        client = client_for(guest)
        assert add(client, "a").status_code == 200
        assert client.get(CART).json()["line_count"] == 1


class TestAdd:
    def test_add_snapshots_the_resolver_answer(self, auth_client, catalog):
        catalog.put("a", price="12.50", title="Brake pad")
        body = add(auth_client, "a", 2).json()
        line = body["lines"][0]
        assert line["title"] == "Brake pad"
        assert line["unit_price"] == "12.50"
        assert line["line_total"] == "25.00"
        assert line["seller_id"] == "seller-1"
        assert body["currency"] == "USD"
        assert body["total"] == "25.00"
        assert body["subtotal"] == body["total"]

    def test_same_pair_grows_one_line(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a", 2)
        body = add(auth_client, "a", 3).json()
        assert body["line_count"] == 1
        assert body["lines"][0]["quantity"] == 5

    def test_quantity_is_capped_at_max_line_quantity(self, auth_client, catalog, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "MAX_LINE_QUANTITY": 5}
        catalog.put("a")
        add(auth_client, "a", 4)
        body = add(auth_client, "a", 4).json()
        assert body["lines"][0]["quantity"] == 5

    def test_default_cap_is_999(self, auth_client, catalog):
        catalog.put("a")
        body = add(auth_client, "a", 5000).json()
        assert body["lines"][0]["quantity"] == 999

    def test_quantity_is_capped_by_resolver_max_quantity(self, auth_client, catalog):
        catalog.put("a", max_quantity=3)
        body = add(auth_client, "a", 10).json()
        assert body["lines"][0]["quantity"] == 3

    def test_zero_stock_is_unavailable(self, auth_client, catalog):
        catalog.put("a", max_quantity=0)
        resp = add(auth_client, "a")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.purchasable_unavailable"

    def test_unavailable_is_409(self, auth_client, catalog):
        catalog.put("a", available=False)
        resp = add(auth_client, "a")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.purchasable_unavailable"

    def test_unknown_key_is_400(self, auth_client, catalog):
        resp = add(auth_client, "nope")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.purchasable_unknown"

    def test_unknown_type_is_400(self, auth_client, catalog):
        resp = add(auth_client, "a", ptype="spaceship")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.purchasable_type_unknown"

    def test_quantity_zero_is_a_field_error(self, auth_client, catalog):
        catalog.put("a")
        assert add(auth_client, "a", 0).status_code == 400

    def test_currency_mismatch_is_409_and_never_converted(self, auth_client, catalog):
        catalog.put("usd", currency="USD")
        catalog.put("eur", currency="EUR")
        add(auth_client, "usd")
        resp = add(auth_client, "eur")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.cart_currency_mismatch"
        assert CartLine.objects.count() == 1

    def test_line_limit(self, auth_client, catalog, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "MAX_LINES_PER_CART": 2}
        for key in "abc":
            catalog.put(key)
        add(auth_client, "a")
        add(auth_client, "b")
        resp = add(auth_client, "c")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.cart_too_many_lines"
        assert resp.json()["params"] == {"max": 2}

    def test_add_refreshes_the_snapshot_of_an_existing_line(self, auth_client, catalog):
        catalog.put("a", price="10.00")
        add(auth_client, "a")
        catalog.put("a", price="11.00")
        line = add(auth_client, "a").json()["lines"][0]
        assert line["unit_price"] == "11.00"
        assert line["line_total"] == "22.00"


class TestResolverDown:
    def test_failing_resolver_is_503_never_200(self, auth_client, catalog):
        catalog.put("a")
        catalog.fail = True
        resp = add(auth_client, "a")
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == "error.503.purchasable_unavailable"
        assert not CartLine.objects.exists()

    def test_unregistered_resolver_is_503(self, auth_client):
        # No catalog fixture: the type is registered, its provider is not.
        resp = add(auth_client, "a")
        assert resp.status_code == 503

    def test_reply_outside_the_contract_is_503(self, auth_client, catalog):
        catalog.put("a", price=12.5)  # a float price
        assert add(auth_client, "a").status_code == 503

    def test_price_with_three_decimals_is_503(self, auth_client, catalog):
        catalog.put("a", price="1.005")
        assert add(auth_client, "a").status_code == 503


class TestChangeAndRemove:
    def test_patch_sets_quantity_without_asking_the_resolver(self, auth_client, catalog):
        catalog.put("a", price="2.50")
        line_id = add(auth_client, "a").json()["lines"][0]["id"]
        calls = len(catalog.calls)
        resp = auth_client.patch(f"{LINES}{line_id}/", {"quantity": 4}, format="json")
        assert resp.status_code == 200
        assert resp.json()["lines"][0]["line_total"] == "10.00"
        assert len(catalog.calls) == calls

    def test_patch_zero_deletes(self, auth_client, catalog):
        catalog.put("a")
        line_id = add(auth_client, "a").json()["lines"][0]["id"]
        body = auth_client.patch(f"{LINES}{line_id}/", {"quantity": 0}, format="json").json()
        assert body["lines"] == []
        assert body["currency"] is None

    def test_delete_line(self, auth_client, catalog):
        catalog.put("a")
        catalog.put("b")
        line_id = add(auth_client, "a").json()["lines"][0]["id"]
        add(auth_client, "b")
        body = auth_client.delete(f"{LINES}{line_id}/").json()
        assert [line["purchasable_key"] for line in body["lines"]] == ["b"]

    def test_clear_cart(self, auth_client, catalog):
        catalog.put("a")
        add(auth_client, "a")
        resp = auth_client.delete(CART)
        assert resp.status_code == 200
        assert resp.json()["lines"] == []

    def test_clear_without_a_cart_is_200(self, auth_client, catalog):
        assert auth_client.delete(CART).status_code == 200

    def test_foreign_line_is_404(self, client_for, user, other_user, catalog):
        catalog.put("a")
        line_id = add(client_for(user), "a").json()["lines"][0]["id"]
        other = client_for(other_user)
        resp = other.patch(f"{LINES}{line_id}/", {"quantity": 2}, format="json")
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == "error.404.cart_line_not_found"
        assert other.delete(f"{LINES}{line_id}/").status_code == 404
        assert CartLine.objects.get(pk=line_id).quantity == 1


class TestZeroCommRead:
    """``GET cart/`` makes zero comm calls — the cart never blocks on a sibling."""

    def test_reading_a_full_cart_calls_nothing(self, auth_client, catalog, monkeypatch):
        catalog.put("a")
        catalog.put("b")
        add(auth_client, "a")
        add(auth_client, "b")

        import stapel_core.comm.functions as functions

        calls = []
        real_call = functions.call

        def spy(name, payload=None, **kwargs):
            calls.append(name)
            return real_call(name, payload, **kwargs)

        monkeypatch.setattr(functions, "call", spy)
        import stapel_core.comm as comm

        monkeypatch.setattr(comm, "call", spy)
        catalog.calls.clear()
        catalog.fail = True  # a sibling that is down must not matter

        resp = auth_client.get(CART)
        assert resp.status_code == 200
        assert resp.json()["line_count"] == 2
        assert calls == []
        assert catalog.calls == []

    def test_the_spy_sees_a_call_when_there_is_one(self, auth_client, catalog, monkeypatch):
        """Inversion: the same spy does see the resolver on an add."""
        catalog.put("a")
        import stapel_core.comm as comm

        calls = []
        real_call = comm.call

        def spy(name, payload=None, **kwargs):
            calls.append(name)
            return real_call(name, payload, **kwargs)

        monkeypatch.setattr(comm, "call", spy)
        add(auth_client, "a")
        assert calls == ["orders_test.purchasables"]


class TestMoneyOnTheWire:
    def test_money_is_a_string(self, auth_client, catalog):
        catalog.put("a", price="0.10")
        body = add(auth_client, "a", 3).json()
        assert body["total"] == "0.30"
        assert isinstance(body["lines"][0]["unit_price"], str)

    def test_stored_line_total_is_quantized(self, auth_client, catalog):
        catalog.put("a", price="9999999999.99")
        add(auth_client, "a", 999)
        line = CartLine.objects.get()
        assert line.line_total == Decimal("9999999999.99") * 999
