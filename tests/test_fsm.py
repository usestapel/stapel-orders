"""The transition table, with its inversion: every illegal edge is a 409."""
import itertools

import pytest

from stapel_orders import fsm
from stapel_orders.models import Order, OrderStatus

from .factories import make_order

STATUSES = list(OrderStatus.values)


def test_table_covers_the_spec_edges():
    expected = {
        ("new", "confirmed"), ("confirmed", "shipped"), ("shipped", "completed"),
        ("new", "cancelled"), ("confirmed", "cancelled"),
        ("needs_review", "confirmed"), ("needs_review", "cancelled"),
    }
    expected |= {(s, "refunded") for s in
                 ("new", "confirmed", "shipped", "completed", "needs_review")}
    expected |= {(s, "needs_review") for s in STATUSES if s != "needs_review"}
    assert set(fsm.TRANSITIONS) == expected


def test_needs_review_is_system_only():
    for (frm, to), actors in fsm.TRANSITIONS.items():
        if to == "needs_review":
            assert actors == {fsm.SYSTEM}


def test_owner_only_cancels():
    for (frm, to), actors in fsm.TRANSITIONS.items():
        if fsm.OWNER in actors:
            assert to == "cancelled"


@pytest.mark.django_db
@pytest.mark.parametrize("frm,to", [
    (f, t) for f, t in itertools.product(STATUSES, STATUSES)
    if fsm.STAFF not in fsm.TRANSITIONS.get((f, t), frozenset())
])
def test_every_edge_staff_may_not_take_is_409(frm, to, client_for, staff, user):
    order = make_order(user, status=frm)
    resp = client_for(staff).post(
        f"/orders/api/v1/orders/{order.pk}/transition/", {"to": to}, format="json"
    )
    assert resp.status_code == 409, (frm, to, resp.content)
    assert resp.json()["localizable_error"] == "error.409.order_transition_invalid"
    assert Order.objects.get(pk=order.pk).status == frm


@pytest.mark.django_db
@pytest.mark.parametrize("frm,to", [
    edge for edge, actors in fsm.TRANSITIONS.items() if fsm.STAFF in actors
])
def test_every_edge_staff_may_take_is_200(frm, to, client_for, staff, user):
    """Inversion of the above: the 409s are not a blanket refusal (a refund
    needs a paid order; services.orders.refund_order)."""
    paid = {"payment_status": "paid"} if to == "refunded" else {}
    order = make_order(user, status=frm, **paid)
    resp = client_for(staff).post(
        f"/orders/api/v1/orders/{order.pk}/transition/", {"to": to}, format="json"
    )
    assert resp.status_code == 200, (frm, to, resp.content)
