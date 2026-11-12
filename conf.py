"""Settings namespace for stapel-orders.

All configuration is read through ``orders_settings`` (lazily, at call
time). Resolution order per key: ``settings.STAPEL_ORDERS`` dict -> flat
Django setting of the same name -> environment variable -> default below.

The module is purchasable-generic: it ships knowing no product types. A host
registers what can be bought in ``PURCHASABLE_TYPES`` (merge-over-builtins,
``None`` removes a type); each entry names a comm Function that resolves a
keyed batch of purchasable keys to title/price/availability. Orders never
imports a sibling model.

``ADJUSTERS`` and ``CHECKOUT_HOOKS`` are the checkout seams commerce
companions plug into (promotions, delivery, inventory): merge registries of
comm Function names, empty by default (``adjusters.py``, ``hooks.py``).

``PAYMENT`` is a short name from the payment registry (``none``, ``mock``,
plus whatever a deployment registers) or a dotted path to a provider. The
environment may choose among the short names only (``env_enum``).
"""
from stapel_core.conf import AppSettings

#: AppSettings-shaped literal dict: a top-level DEFAULTS lets the
#: capabilities.json emitter introspect axis keys/kinds.
DEFAULTS = {
    # {type_name: {"resolve": "comm.function.name"} | None}, merged over the
    # (empty) built-ins. See registry.py for the resolver contract.
    "PURCHASABLE_TYPES": {},
    # Axis: may a guest (anonymous account) place an order? A product
    # decision, so a switch; not derived from the posture.
    "CHECKOUT_REQUIRES_ACCOUNT": True,
    # Money bounds. Both feed the total < column proof (money.py,
    # tests/test_money_bounds.py, check E005).
    "MAX_LINE_QUANTITY": 999,
    "MAX_LINES_PER_CART": 100,
    # Checkout seams, merge registries ({name: entry | None}, None removes).
    # {name: {"quote": "<function>", "order": 100}} — price adjustments
    # (discount 100, shipping 200, tax 300), schemas/functions/adjusters.json.
    "ADJUSTERS": {},
    # {name: {"reserve", "commit", "release": "<function>", "order": 100}} —
    # reservations taken before the order is written (schemas/functions/
    # checkout_hooks.*.json).
    "CHECKOUT_HOOKS": {},
    # Adjustments one quote may carry across all adjusters; feeds E005.
    "MAX_ADJUSTMENTS": 20,
    # A reserve whose checkout never finished is released by the hook sweep
    # (beat / manage.py sweep_checkout_hooks) after this many seconds.
    "CHECKOUT_HOOK_ORPHAN_SECONDS": 900,
    # Carts untouched for this many days are purged by the beat entry.
    "CART_TTL_DAYS": 30,
    # Reporting currency of Order.total_base and the converter computing it
    # ((amount: Decimal, currency: str, base: str) -> Decimal | None).
    "BASE_CURRENCY": "USD",
    "TOTAL_BASE_CONVERTER": "stapel_orders.money.currencies_converter",
    # Axis: how an order gets paid. "none": the order stays unpaid and staff
    # confirm it by hand. "mock": a fake payment.completed is emitted in the
    # checkout transaction (prototype stage only, check E002/W002).
    "PAYMENT": "none",
    # Statuses from which the buyer may cancel their own order.
    "OWNER_CANCELLABLE_STATUSES": ("new",),
    # Best-effort buyer notifications (orders.placed, orders.status_changed).
    "NOTIFY": True,
    # "{order_id}"/"{number}" template for notification links; empty = no link.
    "ORDER_URL_TEMPLATE": "",
}

orders_settings = AppSettings(
    "STAPEL_ORDERS",
    defaults=DEFAULTS,
    import_strings=("TOTAL_BASE_CONVERTER",),
    resolvers={"PAYMENT": "stapel_orders.payment.resolve_payment"},
    env_enum={"PAYMENT": "stapel_orders.payment.payment_names"},
)

__all__ = ["orders_settings", "DEFAULTS"]
