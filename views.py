"""DRF views for stapel-orders — thin over ``services``.

Every view declares its guest stance. Cart and order views admit a guest
session (a stapel-auth guest row is authenticated) and scope rows to
``request.user``; checkout admits the guest statically and enforces
``CHECKOUT_REQUIRES_ACCOUNT`` in the body; the staff transition refuses guests.
"""
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from stapel_core.django.api.errors import StapelErrorResponse
from stapel_core.django.api.pagination import AnchorPagination
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    ANONYMOUS_DENIED,
    IsStaffUser,
)
from stapel_core.django.api.views import StapelAPIView
from stapel_core.flows import flow_step

from .flows import CART, HISTORY, PURCHASE, STAFF_FULFILMENT
from .models import OrderStatus
from .presenters import get_cart_presenter, get_order_presenter, get_quote_presenter
from .serializers import (
    CartLineAddRequestSerializer,
    CartLineUpdateRequestSerializer,
    CartQuoteRequestSerializer,
    CartQuoteResponseSerializer,
    CartResponseSerializer,
    CheckoutRequestSerializer,
    OrderPageSerializer,
    OrderRefundRequestSerializer,
    OrderResponseSerializer,
    OrderTransitionRequestSerializer,
)
from .services import OrdersError
from .services import cart as cart_service
from .services import checkout as checkout_service
from .services import orders as order_service

TAGS = ["Orders"]


class OrderAnchorPagination(AnchorPagination):
    """Order history, newest first, anchored on the id."""

    page_size = 20
    max_page_size = 100
    anchor_field = "id"
    anchor_type = "integer"
    ordering = "-id"


class _OrdersView(StapelAPIView):
    """Renders a service refusal as the fleet error envelope."""

    def handle_exception(self, exc):
        if isinstance(exc, OrdersError):
            return StapelErrorResponse(exc.status, exc.key, exc.params)
        return super().handle_exception(exc)

    def _user_id(self, request) -> str:
        return str(request.user.pk)

    def _cart_response(self, cart, *, status=200):
        return self.serialized_response(get_cart_presenter()().present(cart), status=status)

    def _order_response(self, order, *, status=200):
        return self.serialized_response(get_order_presenter()().present(order), status=status)


@extend_schema(tags=TAGS)
class CartView(_OrdersView):
    """The caller's cart. Reading it makes no comm call."""

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    response_serializer_class = CartResponseSerializer

    @extend_schema(operation_id="orders_cart_retrieve", responses={200: CartResponseSerializer})
    @flow_step(CART, order=1, note="Read the cart (zero calls to the product owner)")
    def get(self, request):
        return self._cart_response(cart_service.get_cart(self._user_id(request)))

    @extend_schema(operation_id="orders_cart_clear", responses={200: CartResponseSerializer})
    @flow_step(CART, order=5, note="Empty the cart")
    def delete(self, request):
        return self._cart_response(cart_service.clear_cart(self._user_id(request)))


@extend_schema(tags=TAGS)
class CartLinesView(_OrdersView):
    """Add a purchasable to the cart (re-priced by its resolver)."""

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    request_serializer_class = CartLineAddRequestSerializer
    response_serializer_class = CartResponseSerializer

    @extend_schema(
        operation_id="orders_cart_lines_create",
        request=CartLineAddRequestSerializer,
        responses={200: CartResponseSerializer},
    )
    @flow_step(CART, order=2, note="Add a purchasable; the same pair grows its line")
    def post(self, request):
        data = self.validated_request_data(request)
        cart = cart_service.add_line(
            self._user_id(request),
            data["purchasable_type"],
            data["purchasable_key"],
            data["quantity"],
        )
        return self._cart_response(cart)


@extend_schema(tags=TAGS)
class CartLineDetailView(_OrdersView):
    """Change or remove one line of the caller's cart."""

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    request_serializer_class = CartLineUpdateRequestSerializer
    response_serializer_class = CartResponseSerializer

    @extend_schema(
        operation_id="orders_cart_lines_update",
        request=CartLineUpdateRequestSerializer,
        responses={200: CartResponseSerializer},
    )
    @flow_step(CART, order=3, note="Change a line's quantity (0 removes it)")
    def patch(self, request, line_id):
        data = self.validated_request_data(request)
        cart = cart_service.update_line(self._user_id(request), line_id, data["quantity"])
        return self._cart_response(cart)

    @extend_schema(operation_id="orders_cart_lines_destroy", responses={200: CartResponseSerializer})
    @flow_step(CART, order=4, note="Remove a line")
    def delete(self, request, line_id):
        return self._cart_response(cart_service.remove_line(self._user_id(request), line_id))


@extend_schema(tags=TAGS)
class CartQuoteView(_OrdersView):
    """Price the cart through the ADJUSTERS with the client's ``context``.

    Never fails on an adjuster's refusal: errors come back in ``errors`` (the
    same ones checkout would refuse with). Asks no purchasable resolver.
    """

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    request_serializer_class = CartQuoteRequestSerializer
    response_serializer_class = CartQuoteResponseSerializer

    @extend_schema(
        operation_id="orders_cart_quote",
        request=CartQuoteRequestSerializer,
        responses={200: CartQuoteResponseSerializer},
    )
    @flow_step(PURCHASE, order=5, note="Re-quote the cart with codes and a delivery choice before submitting")
    def post(self, request):
        data = self.validated_request_data(request)
        cart, quote = checkout_service.quote_cart(request.user, data.get("context") or {})
        return self.serialized_response(get_quote_presenter()().present(cart, quote))


@extend_schema(tags=TAGS)
class CheckoutView(_OrdersView):
    """Turn the cart into an order. Idempotent on ``idempotency_key``.

    Guests are admitted by the class gate on purpose: whether a guest may
    check out is ``CHECKOUT_REQUIRES_ACCOUNT``, enforced in the body.
    """

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    request_serializer_class = CheckoutRequestSerializer
    response_serializer_class = OrderResponseSerializer

    @extend_schema(
        operation_id="orders_checkout_create",
        request=CheckoutRequestSerializer,
        responses={201: OrderResponseSerializer, 200: OrderResponseSerializer},
    )
    @flow_step(PURCHASE, order=2, note="Submit contact and address; the order is placed")
    def post(self, request):
        refusal = checkout_service.requires_account_refusal(request.user)
        if refusal is not None:
            raise refusal
        data = self.validated_request_data(request)
        contact = data["contact"]
        address = data.get("shipping_address")
        order, created = checkout_service.checkout(
            request.user,
            idempotency_key=data["idempotency_key"],
            contact=checkout_service.Contact(
                name=contact["name"], phone=contact["phone"], email=contact.get("email", "")
            ),
            address=None if address is None else checkout_service.Address(
                line1=address["line1"],
                line2=address.get("line2", ""),
                city=address["city"],
                region=address.get("region", ""),
                postal_code=address.get("postal_code", ""),
                country=address["country"],
            ),
            shipping_method=data.get("shipping_method", ""),
            comment=data.get("comment", ""),
            context=data.get("context") or {},
            expected_total=data.get("expected_total"),
        )
        return self._order_response(order, status=201 if created else 200)


STATUS_QUERY_PARAMETER = OpenApiParameter(
    "status",
    str,
    OpenApiParameter.QUERY,
    required=False,
    many=True,
    enum=[value for value, _ in OrderStatus.choices],
    description="Filter by status (repeatable).",
)

ANCHOR_QUERY_PARAMETERS = [
    OpenApiParameter("anchor", int, OpenApiParameter.QUERY, required=False,
                     description="Order id to continue after (exclusive)."),
    OpenApiParameter("limit", int, OpenApiParameter.QUERY, required=False,
                     description=f"Page size, default {OrderAnchorPagination.page_size}, "
                                 f"max {OrderAnchorPagination.max_page_size}."),
    OpenApiParameter("direction", str, OpenApiParameter.QUERY, required=False,
                     enum=["next", "prev", "center"],
                     description="Walk direction from the anchor."),
]


@extend_schema(tags=TAGS)
class OrderListView(_OrdersView):
    """The caller's orders, newest first, anchor-paginated."""

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    pagination_class = OrderAnchorPagination
    response_serializer_class = OrderResponseSerializer

    @extend_schema(
        operation_id="orders_orders_list",
        parameters=[STATUS_QUERY_PARAMETER, *ANCHOR_QUERY_PARAMETERS],
        responses={200: OrderPageSerializer},
    )
    @flow_step(HISTORY, order=1, note="Page through the order history")
    def get(self, request):
        qs = order_service.orders_for(
            self._user_id(request), request.query_params.getlist("status")
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request, view=self)
        presenter = get_order_presenter()()
        serializer = self.get_response_serializer_class()
        data = serializer([presenter.present(order) for order in page], many=True).data
        return paginator.get_paginated_response(data)


@extend_schema(tags=TAGS)
class OrderDetailView(_OrdersView):
    """One of the caller's orders; a foreign id is a 404 like an absent one."""

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    response_serializer_class = OrderResponseSerializer

    @extend_schema(operation_id="orders_orders_retrieve", responses={200: OrderResponseSerializer})
    @flow_step(HISTORY, order=2, note="Open one order")
    def get(self, request, order_id):
        return self._order_response(order_service.get_owned(self._user_id(request), order_id))


@extend_schema(tags=TAGS)
class OrderCancelView(_OrdersView):
    """The buyer cancels while the status allows it (OWNER_CANCELLABLE_STATUSES)."""

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    response_serializer_class = OrderResponseSerializer

    @extend_schema(
        operation_id="orders_orders_cancel",
        request=None,
        responses={200: OrderResponseSerializer},
    )
    @flow_step(HISTORY, order=3, note="Cancel an order that is still new")
    def post(self, request, order_id):
        return self._order_response(order_service.cancel_owned(self._user_id(request), order_id))


@extend_schema(tags=TAGS)
class OrderTransitionView(_OrdersView):
    """Staff move an order along the lifecycle."""

    permission_classes = [IsStaffUser]
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = OrderTransitionRequestSerializer
    response_serializer_class = OrderResponseSerializer

    @extend_schema(
        operation_id="orders_orders_transition",
        request=OrderTransitionRequestSerializer,
        responses={200: OrderResponseSerializer},
    )
    @flow_step(STAFF_FULFILMENT, order=1, note="Confirm, ship, complete or cancel")
    def post(self, request, order_id):
        data = self.validated_request_data(request)
        order = order_service.staff_transition(order_id, data["to"], data.get("reason", ""))
        return self._order_response(order)


@extend_schema(tags=TAGS)
class OrderRefundView(_OrdersView):
    """Staff refund a paid order in full (partial refunds are not built).

    The PAYMENT provider refunds first; then status and payment_status become
    ``refunded``, the checkout hooks release (reason ``cancelled``) and
    ``orders.order.refunded`` is emitted. Idempotent: a refunded order is
    answered as it is. A cancelled order is not refundable here.
    """

    permission_classes = [IsStaffUser]
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = OrderRefundRequestSerializer
    response_serializer_class = OrderResponseSerializer

    @extend_schema(
        operation_id="orders_orders_refund",
        request=OrderRefundRequestSerializer,
        responses={200: OrderResponseSerializer},
    )
    @flow_step(STAFF_FULFILMENT, order=2, note="Refund a paid order in full")
    def post(self, request, order_id):
        data = self.validated_request_data(request)
        return self._order_response(order_service.refund_order(order_id, data.get("reason", "")))
