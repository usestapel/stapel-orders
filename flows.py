"""Business flows of stapel-orders (stapel_core.flows).

The Flow objects live here; HTTP steps are attached in ``views.py`` with
``@flow_step``. Literals are the canonical English source texts; i18n keys
are derived (``flow.<id>.title`` / ``.step.<order>.note``).
"""
from stapel_core.flows import Flow

CART = Flow(
    "orders.cart",
    title="Fill a cart",
    description=(
        "A shopper, guest or signed in, adds purchasables to a cart, changes "
        "quantities and removes lines. Each line is a snapshot taken from the "
        "purchasable's resolver when it was added; reading the cart never asks "
        "the product owner."
    ),
    actors=["Shopper (guest session or account)"],
)
CART.human(order=0, note="The shopper presses 'Add to cart' on a product page")

PURCHASE = Flow(
    "orders.purchase",
    title="Check out",
    description=(
        "The shopper submits contact and shipping details. Every line is "
        "re-priced; a changed price refreshes the cart and asks again. The "
        "order is placed, the cart emptied, and the payment provider started "
        "in one transaction."
    ),
    actors=["Shopper with an account (guests when CHECKOUT_REQUIRES_ACCOUNT is off)"],
)
PURCHASE.human(order=1, note="The shopper opens checkout from the cart")
PURCHASE.action("orders.order.placed", order=3, note="The placed order is announced with its lines")
PURCHASE.action("payment.completed", order=4, note="A payment fact marks the order paid")

HISTORY = Flow(
    "orders.history",
    title="Follow my orders",
    description=(
        "The buyer pages through their orders, opens one, and may cancel it "
        "while its status allows."
    ),
    actors=["Buyer"],
)

STAFF_FULFILMENT = Flow(
    "orders.fulfilment",
    title="Fulfil an order",
    description=(
        "Staff confirm, ship, complete or cancel orders; a payment that does "
        "not match the order sends it to review."
    ),
    actors=["Staff"],
)
STAFF_FULFILMENT.action(
    "orders.order.status_changed", order=2, note="Every status move is announced"
)
