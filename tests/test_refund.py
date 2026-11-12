"""Staff refund: a paid order, in full, once; the checkout hooks release it."""
import uuid

import pytest

from stapel_orders import seams
from stapel_orders.models import Order

from .fake_seams import FakeHook

pytestmark = pytest.mark.django_db

V1 = "/orders/api/v1"
LINES = V1 + "/cart/lines/"
CHECKOUT = V1 + "/checkout/"


@pytest.fixture(autouse=True)
def _reset_seams():
    seams.reset_seams()
    yield
    seams.reset_seams()


@pytest.fixture
def stock():
    hook = FakeHook("stock").install()
    yield hook
    hook.uninstall()


@pytest.fixture
def notified(monkeypatch):
    import stapel_core.notifications.publish as publish

    sent = []
    monkeypatch.setattr(publish, "request_notification",
                        lambda t, **kw: sent.append(t) or True)
    return sent


@pytest.fixture
def refund_events():
    from stapel_core.comm import action_registry, subscribe_action

    seen = []

    def _handler(event):
        seen.append(event)

    names = ["orders.order.refunded", "orders.order.status_changed"]
    for name in names:
        subscribe_action(name, _handler)
    yield seen
    for name in names:
        handlers = action_registry._subscribers.get(name, [])
        if _handler in handlers:
            handlers.remove(_handler)


@pytest.fixture
def paid_order(auth_client, catalog, stock, settings, django_capture_on_commit_callbacks):
    """A mock-paid order placed through checkout with a checkout hook."""
    settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "mock"}
    catalog.put("a", price="25.00")
    auth_client.post(LINES, {"purchasable_type": "item", "purchasable_key": "a",
                             "quantity": 2}, format="json")
    key = uuid.uuid4()
    with django_capture_on_commit_callbacks(execute=True):
        resp = auth_client.post(CHECKOUT, {
            "idempotency_key": str(key),
            "contact": {"name": "Ann Buyer", "phone": "+1 555 0100"},
            "shipping_address": {"line1": "1 Main St", "city": "Springfield", "country": "US"},
        }, format="json")
    assert resp.status_code == 201 and resp.json()["payment_status"] == "paid"
    return resp.json(), key


def _refund(client, order_id, captured, reason=""):
    with captured(execute=True):
        return client.post(f"{V1}/orders/{order_id}/refund/", {"reason": reason}, format="json")


def test_refund_releases_once_and_announces(paid_order, client_for, staff, stock,
                                            refund_events, notified,
                                            django_capture_on_commit_callbacks):
    order, key = paid_order
    resp = _refund(client_for(staff), order["id"], django_capture_on_commit_callbacks,
                   "returned")
    assert resp.status_code == 200, resp.content
    assert (resp.json()["status"], resp.json()["payment_status"]) == ("refunded", "refunded")
    releases = stock.ops("release")
    assert [(p["reason"], p["order_id"]) for p in releases] == [("cancelled", str(order["id"]))]
    assert stock.holds[str(key)]["state"] == "released"
    refunded = [e.payload for e in refund_events if e.event_type == "orders.order.refunded"]
    assert refunded == [{"order_id": str(order["id"]), "attempt_key": str(key),
                         "user_id": refunded[0]["user_id"], "total": "50.00",
                         "currency": "USD"}]
    assert refunded[0]["user_id"] is not None
    moves = [(e.payload["from"], e.payload["to"], e.payload["actor"], e.payload.get("reason"))
             for e in refund_events if e.event_type == "orders.order.status_changed"]
    assert moves == [("new", "refunded", "staff", "returned")]
    assert notified == ["orders.refunded"]


def test_a_second_refund_is_a_no_op(paid_order, client_for, staff, stock, refund_events,
                                    django_capture_on_commit_callbacks):
    order, _key = paid_order
    staff_client = client_for(staff)
    first = _refund(staff_client, order["id"], django_capture_on_commit_callbacks)
    second = _refund(staff_client, order["id"], django_capture_on_commit_callbacks)
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "refunded"
    assert len(stock.ops("release")) == 1
    assert len([e for e in refund_events if e.event_type == "orders.order.refunded"]) == 1


def test_refund_after_cancel_is_refused(paid_order, client_for, staff, stock,
                                        refund_events, django_capture_on_commit_callbacks):
    order, _key = paid_order
    staff_client = client_for(staff)
    with django_capture_on_commit_callbacks(execute=True):
        staff_client.post(f"{V1}/orders/{order['id']}/transition/", {"to": "cancelled"},
                          format="json")
    resp = _refund(staff_client, order["id"], django_capture_on_commit_callbacks)
    assert resp.status_code == 409
    assert resp.json()["localizable_error"] == "error.409.order_transition_invalid"
    row = Order.objects.get(pk=order["id"])
    assert (row.status, row.payment_status) == ("cancelled", "paid")
    assert [p["reason"] for p in stock.ops("release")] == ["cancelled"]
    assert not [e for e in refund_events if e.event_type == "orders.order.refunded"]


def test_cancel_after_refund_is_refused(paid_order, client_for, staff, stock,
                                        django_capture_on_commit_callbacks):
    order, _key = paid_order
    staff_client = client_for(staff)
    _refund(staff_client, order["id"], django_capture_on_commit_callbacks)
    resp = staff_client.post(f"{V1}/orders/{order['id']}/transition/", {"to": "cancelled"},
                             format="json")
    assert resp.status_code == 409
    assert len(stock.ops("release")) == 1


def test_the_transition_endpoint_refunds_through_the_same_path(
    paid_order, client_for, staff, stock, refund_events, django_capture_on_commit_callbacks
):
    order, _key = paid_order
    with django_capture_on_commit_callbacks(execute=True):
        resp = client_for(staff).post(f"{V1}/orders/{order['id']}/transition/",
                                      {"to": "refunded"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["payment_status"] == "refunded"
    assert len(stock.ops("release")) == 1
    assert len([e for e in refund_events if e.event_type == "orders.order.refunded"]) == 1


def test_an_unpaid_order_is_not_refundable(auth_client, catalog, client_for, staff,
                                           django_capture_on_commit_callbacks):
    catalog.put("a")
    auth_client.post(LINES, {"purchasable_type": "item", "purchasable_key": "a"}, format="json")
    order = auth_client.post(CHECKOUT, {
        "idempotency_key": str(uuid.uuid4()),
        "contact": {"name": "Ann Buyer", "phone": "+1 555 0100"},
        "shipping_address": {"line1": "1 Main St", "city": "Springfield", "country": "US"},
    }, format="json").json()
    resp = _refund(client_for(staff), order["id"], django_capture_on_commit_callbacks)
    assert resp.status_code == 409
    assert resp.json()["localizable_error"] == "error.409.order_not_refundable"
    assert resp.json()["params"]["payment_status"] == "unpaid"


def test_a_provider_without_refund_refuses_and_changes_nothing(
    paid_order, client_for, staff, stock, settings, django_capture_on_commit_callbacks
):
    from stapel_orders.payment import PaymentProvider, register_payment_provider

    class StartOnly(PaymentProvider):
        def start(self, order, emit):
            return None

    register_payment_provider("start-only", StartOnly)
    settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "start-only"}
    order, _key = paid_order
    resp = _refund(client_for(staff), order["id"], django_capture_on_commit_callbacks)
    assert resp.status_code == 409
    assert resp.json()["localizable_error"] == "error.409.refund_unsupported"
    assert resp.json()["params"]["provider"] == "start-only"
    row = Order.objects.get(pk=order["id"])
    assert (row.status, row.payment_status) == ("new", "paid")
    assert stock.ops("release") == []


def test_the_buyer_cannot_refund(paid_order, auth_client):
    order, _key = paid_order
    resp = auth_client.post(f"{V1}/orders/{order['id']}/refund/", {}, format="json")
    assert resp.status_code == 403
    assert Order.objects.get(pk=order["id"]).payment_status == "paid"
