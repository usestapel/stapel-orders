"""payment.completed: paid on a match, needs_review on a mismatch, idempotent."""
import pytest
from stapel_core.comm import emit, mutate_and_emit

from stapel_orders.models import Order

from .factories import make_order

pytestmark = pytest.mark.django_db


def paid(order, amount=None, currency=None, reference_type="order", ref="pay-1", key=None):
    with mutate_and_emit():
        emit("payment.completed", {
            "reference_type": reference_type,
            "reference_key": key or str(order.pk),
            "amount": amount or str(order.total),
            "currency": currency or order.currency,
            "provider_ref": ref,
        })
    order.refresh_from_db()
    return order


def test_matching_payment_marks_the_order_paid(user, captured_events):
    order = paid(make_order(user, total="19.99"))
    assert order.payment_status == "paid"
    assert order.provider_ref == "pay-1"
    assert [e.payload for e in captured_events if e.event_type == "orders.order.paid"] == [
        {"order_id": str(order.pk), "provider_ref": "pay-1"}
    ]


def test_short_payment_moves_to_needs_review(user, captured_events):
    order = paid(make_order(user, total="19.99"), amount="10.00")
    assert order.status == "needs_review"
    assert order.payment_status == "unpaid"
    changed = [e.payload for e in captured_events if e.event_type == "orders.order.status_changed"]
    assert changed[0]["actor"] == "system" and changed[0]["reason"] == "payment_mismatch"


def test_wrong_currency_moves_to_needs_review(user):
    order = paid(make_order(user, total="5.00"), currency="EUR")
    assert order.status == "needs_review"
    assert order.payment_status == "unpaid"


def test_redelivery_is_a_no_op(user, captured_events):
    order = make_order(user)
    paid(order)
    paid(order)
    assert len([e for e in captured_events if e.event_type == "orders.order.paid"]) == 1


def test_other_reference_types_are_ignored(user):
    order = paid(make_order(user), reference_type="subscription")
    assert order.payment_status == "unpaid"


def test_unknown_order_is_dropped_quietly(user):
    order = make_order(user)
    paid(order, key="987654")
    order.refresh_from_db()
    assert order.payment_status == "unpaid"


def test_staff_resolve_a_reviewed_order(client_for, staff, user):
    order = paid(make_order(user, total="5.00"), amount="1.00")
    resp = client_for(staff).post(
        f"/orders/api/v1/orders/{order.pk}/transition/", {"to": "confirmed"}, format="json"
    )
    assert resp.status_code == 200
    assert Order.objects.get(pk=order.pk).status == "confirmed"
