"""comm Functions under schema validation."""
import pytest
from stapel_core.comm import FunctionLookupError, call

from stapel_orders.services import cart as cart_service

from .factories import make_order

pytestmark = pytest.mark.django_db


def test_order_status(user):
    order = make_order(user, total="3.50")
    assert call("orders.order.status", {"order_id": str(order.pk)}) == {
        "status": "new", "payment_status": "unpaid", "user_id": str(user.pk),
        "total": "3.50", "currency": "USD",
    }


def test_order_status_unknown():
    with pytest.raises(FunctionLookupError):
        call("orders.order.status", {"order_id": "123456"})


def test_export_pages_with_a_cursor(user):
    ids = [make_order(user, lines=2).pk for _ in range(3)]
    first = call("orders.orders_export", {"limit": 2})
    assert [r["order_id"] for r in first["rows"]] == [str(i) for i in ids[:2]]
    assert first["total"] == 3
    assert len(first["rows"][0]["lines"]) == 2
    assert "contact_name" not in first["rows"][0]
    rest = call("orders.orders_export", {"cursor": first["cursor"], "limit": 2})
    assert [r["order_id"] for r in rest["rows"]] == [str(ids[2])]
    assert rest["cursor"] is None


def test_export_schema_refuses_huge_limits():
    from stapel_core.comm import CommError

    with pytest.raises(CommError):
        call("orders.orders_export", {"limit": 100000})


def test_cart_count(user, catalog):
    catalog.put("a")
    catalog.put("b")
    cart_service.add_line(str(user.pk), "item", "a", 3)
    cart_service.add_line(str(user.pk), "item", "b", 1)
    assert call("orders.cart.count", {"user_id": str(user.pk)}) == {"line_count": 2}
