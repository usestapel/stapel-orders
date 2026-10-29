"""Two adds of one purchasable end as one line (lock + unique backstop)."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from stapel_orders.models import Cart, CartLine
from stapel_orders.services import cart as cart_service


@pytest.mark.django_db(transaction=True)
class TestConcurrentAdds:
    def test_a_line_inserted_concurrently_is_grown_not_duplicated(
        self, user, catalog, monkeypatch
    ):
        catalog.put("a")
        uid = str(user.pk)
        cart_service.add_line(uid, "item", "a", 1)
        cart = Cart.objects.get(user_id=uid)
        CartLine.objects.all().delete()

        # The concurrent request: its line lands after our read said "none".
        real_snapshot = cart_service._snapshot
        state = {"raced": False}

        def snapshot_then_race(line, item):
            real_snapshot(line, item)
            if not state["raced"] and line.pk is None:
                state["raced"] = True
                CartLine.objects.create(
                    cart=cart, purchasable_type="item", purchasable_key="a",
                    quantity=2, unit_price=item.price, currency="USD",
                    line_total=item.price * 2,
                )

        monkeypatch.setattr(cart_service, "_snapshot", snapshot_then_race)
        cart_service.add_line(uid, "item", "a", 3)
        assert state["raced"]
        lines = CartLine.objects.filter(cart=cart)
        assert lines.count() == 1
        assert lines.get().quantity == 5

    def test_a_cart_created_concurrently_is_reused(self, user, catalog, monkeypatch):
        catalog.put("a")
        uid = str(user.pk)
        manager = Cart.objects
        real_sfu = type(manager).select_for_update
        state = {"raced": False}

        def stale_first_read(self, *args, **kwargs):
            if not state["raced"]:
                # Our read finds nothing; the rival's insert lands right after.
                state["raced"] = True
                Cart.objects.create(user_id=uid)
                return Cart.objects.none()
            return real_sfu(self, *args, **kwargs)

        monkeypatch.setattr(type(manager), "select_for_update", stale_first_read)
        cart_service.add_line(uid, "item", "a", 1)
        assert state["raced"]
        assert Cart.objects.filter(user_id=uid).count() == 1
        assert CartLine.objects.get().quantity == 1

    def test_writes_lock_the_cart_row(self, user, catalog, monkeypatch):
        """The lock is asked for (SQLite drops FOR UPDATE, so ask the ORM)."""
        catalog.put("a")
        seen = []
        from django.db.models import QuerySet

        real = QuerySet.select_for_update

        def spy(self, *args, **kwargs):
            seen.append(self.model.__name__)
            return real(self, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", spy)
        with CaptureQueriesContext(connection):
            cart_service.add_line(str(user.pk), "item", "a", 1)
        assert "Cart" in seen
