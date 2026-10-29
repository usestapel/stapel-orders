"""v1 URL set for stapel-orders (plain paths, no router)."""
from typing import NamedTuple

from django.urls import path

from .views import (
    CartLineDetailView,
    CartLinesView,
    CartView,
    CheckoutView,
    OrderCancelView,
    OrderDetailView,
    OrderListView,
    OrderTransitionView,
)

urlpatterns = [
    path("cart/", CartView.as_view(), name="orders-cart"),
    path("cart/lines/", CartLinesView.as_view(), name="orders-cart-lines"),
    path("cart/lines/<int:line_id>/", CartLineDetailView.as_view(), name="orders-cart-line"),
    path("checkout/", CheckoutView.as_view(), name="orders-checkout"),
    path("orders/", OrderListView.as_view(), name="orders-list"),
    path("orders/<int:order_id>/", OrderDetailView.as_view(), name="orders-detail"),
    path("orders/<int:order_id>/cancel/", OrderCancelView.as_view(), name="orders-cancel"),
    path(
        "orders/<int:order_id>/transition/",
        OrderTransitionView.as_view(),
        name="orders-transition",
    ),
]


class GateEntry(NamedTuple):
    """One gated URL block: which flags gate which url patterns."""

    name: str
    flags: tuple
    patterns: tuple


#: No axis unmounts an endpoint (CHECKOUT_REQUIRES_ACCOUNT and PAYMENT are
#: behavioural), so the surface is one always-on block.
GATE_REGISTRY: dict = {
    "orders.api": GateEntry("orders.api", (), tuple(urlpatterns)),
}
