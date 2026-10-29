"""i18n error keys of stapel-orders.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_PURCHASABLE_TYPE_UNKNOWN = "error.400.purchasable_type_unknown"
ERR_400_PURCHASABLE_UNKNOWN = "error.400.purchasable_unknown"
ERR_400_CART_EMPTY = "error.400.cart_empty"
ERR_403_CHECKOUT_REQUIRES_ACCOUNT = "error.403.checkout_requires_account"
ERR_404_CART_LINE_NOT_FOUND = "error.404.cart_line_not_found"
ERR_404_ORDER_NOT_FOUND = "error.404.order_not_found"
ERR_409_PURCHASABLE_UNAVAILABLE = "error.409.purchasable_unavailable"
ERR_409_CART_CURRENCY_MISMATCH = "error.409.cart_currency_mismatch"
ERR_409_CART_PRICE_CHANGED = "error.409.cart_price_changed"
ERR_409_CART_TOO_MANY_LINES = "error.409.cart_too_many_lines"
ERR_409_ORDER_TRANSITION_INVALID = "error.409.order_transition_invalid"
ERR_503_PURCHASABLE_UNAVAILABLE = "error.503.purchasable_unavailable"

STAPEL_ORDERS_ERRORS = {
    ERR_400_PURCHASABLE_TYPE_UNKNOWN: "Unknown purchasable type",
    ERR_400_PURCHASABLE_UNKNOWN: "This item is not available for purchase",
    ERR_400_CART_EMPTY: "The cart is empty",
    ERR_403_CHECKOUT_REQUIRES_ACCOUNT: "Sign in to place an order",
    ERR_404_CART_LINE_NOT_FOUND: "Cart line not found",
    ERR_404_ORDER_NOT_FOUND: "Order not found",
    ERR_409_PURCHASABLE_UNAVAILABLE: "This item is no longer available in the requested quantity",
    ERR_409_CART_CURRENCY_MISMATCH: "This item is priced in another currency than your cart",
    ERR_409_CART_PRICE_CHANGED: "Prices in your cart have changed; review the cart and submit again",
    ERR_409_CART_TOO_MANY_LINES: "The cart holds at most {max} different items",
    ERR_409_ORDER_TRANSITION_INVALID: "The order cannot move from {from} to {to}",
    ERR_503_PURCHASABLE_UNAVAILABLE: "Item details are temporarily unavailable; try again shortly",
}

#: Explicit remediation where the status heuristic would mislead.
STAPEL_ORDERS_REMEDIATION = {
    ERR_403_CHECKOUT_REQUIRES_ACCOUNT: "reauthenticate",
    ERR_409_CART_PRICE_CHANGED: "retry",
    ERR_503_PURCHASABLE_UNAVAILABLE: "wait_and_retry",
}

register_service_errors(STAPEL_ORDERS_ERRORS, remediation=STAPEL_ORDERS_REMEDIATION)

__all__ = [
    "STAPEL_ORDERS_ERRORS",
    "ERR_400_PURCHASABLE_TYPE_UNKNOWN",
    "ERR_400_PURCHASABLE_UNKNOWN",
    "ERR_400_CART_EMPTY",
    "ERR_403_CHECKOUT_REQUIRES_ACCOUNT",
    "ERR_404_CART_LINE_NOT_FOUND",
    "ERR_404_ORDER_NOT_FOUND",
    "ERR_409_PURCHASABLE_UNAVAILABLE",
    "ERR_409_CART_CURRENCY_MISMATCH",
    "ERR_409_CART_PRICE_CHANGED",
    "ERR_409_CART_TOO_MANY_LINES",
    "ERR_409_ORDER_TRANSITION_INVALID",
    "ERR_503_PURCHASABLE_UNAVAILABLE",
]
