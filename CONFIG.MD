# CONFIG.MD — stapel-orders

Config registry for **stapel-orders**. One row per configuration key the
module reads, its **source** (`env` = the process environment / the
`STAPEL_ORDERS` settings dict), what it is for, whether it is required, and
its default.

Every key is read through `orders_settings` (`stapel_orders.conf`, namespace
`STAPEL_ORDERS`). Resolution order per key: `settings.STAPEL_ORDERS` dict →
a flat Django setting of the same name → environment variable → the default
below. `CHECKOUT_REQUIRES_ACCOUNT` and `PAYMENT` are CTO-facing config axes.

## stapel-orders

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| PURCHASABLE_TYPES | settings | what can be bought: {type_name: {"resolve": "<comm function>"}} merged over the empty built-ins; None removes a type. Empty is system check E001. | yes | {} |
| CHECKOUT_REQUIRES_ACCOUNT | env | axis: a guest (anonymous account) is refused at checkout with `error.403.checkout_requires_account`. Not derived from the posture. | no | true |
| MAX_LINE_QUANTITY | env | cap on one line's quantity; feeds the money-bound proof (E005). | no | 999 |
| MAX_LINES_PER_CART | env | cap on distinct lines per cart; feeds the money-bound proof (E005). | no | 100 |
| ADJUSTERS | settings | checkout price adjusters: {name: {"quote": "<comm function>", "order": int}} merged over the empty built-ins, None removes; applied by order (discount 100, shipping 200, tax 300). Contract `schemas/functions/adjusters.json`. A companion's settings fragment fills it. | no | {} |
| CHECKOUT_HOOKS | settings | checkout reservations: {name: {"reserve", "commit", "release": "<comm function>", "order": int}} merged over the empty built-ins, None removes. Contracts `schemas/functions/checkout_hooks.*.json`. | no | {} |
| MAX_ADJUSTMENTS | env | cap on adjustments one quote may carry across all adjusters; feeds the money-bound proof (E005). | no | 20 |
| CHECKOUT_HOOK_ORPHAN_SECONDS | env | a checkout-hook reservation whose checkout never finished is released by the hook sweep (beat / `manage.py sweep_checkout_hooks`) after this age. | no | 900 |
| CART_TTL_DAYS | env | carts untouched this long are purged (beat entry / `manage.py purge_carts`). | no | 30 |
| BASE_CURRENCY | env | reporting currency of `Order.total_base`. | no | USD |
| TOTAL_BASE_CONVERTER | settings | dotted path `(amount, currency, base) -> Decimal or None` computing `total_base`; default asks `currencies.convert`, NULL when unanswered. | no | stapel_orders.money.currencies_converter |
| PAYMENT | env | axis: `none` (orders stay unpaid, staff confirm) or `mock` (paid at once; prototype stage only, E002/W002), or a registered provider name; a dotted path only from the settings module. | no | none |
| OWNER_CANCELLABLE_STATUSES | settings | statuses from which the buyer may cancel their own order. | no | ("new",) |
| NOTIFY | env | best-effort buyer notifications `orders.placed` / `orders.status_changed`. | no | true |
| ORDER_URL_TEMPLATE | env | `{order_id}` / `{number}` template for notification links; empty = no link. | no | "" |
