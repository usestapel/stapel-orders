# stapel-orders — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same change as any edit
> to a seam. See also README.md and CHANGELOG.md.

## What this module provides

- **Cart** per user (`Cart`, `CartLine`): snapshot lines of opaque
  purchasables `(purchasable_type, purchasable_key)`; one currency per cart.
- **Checkout** turning the cart into an **Order** (`Order`, `OrderLine`),
  idempotent on a client key (`CheckoutAttempt`).
- An order lifecycle FSM (`fsm.py`) with buyer cancel and staff transitions.
- API under `/orders/api/v1/` (`cart/`, `cart/lines/`, `checkout/`,
  `orders/`), facts `orders.order.*`, Functions `orders.*`.

## Extension points (fork-free)

### Settings — `STAPEL_ORDERS` namespace (`conf.py`, registry in CONFIG.MD)

| Key | Default | What it customizes |
|---|---|---|
| `PURCHASABLE_TYPES` | `{}` | MERGE registry `{type: {"resolve": "<function>"}}`; `None` removes. Empty = E001. |
| `CHECKOUT_REQUIRES_ACCOUNT` | `True` | Axis. Guests get `error.403.checkout_requires_account`. A switch, not posture. |
| `MAX_LINE_QUANTITY` / `MAX_LINES_PER_CART` | `999` / `100` | Caps; both feed the money proof (E005). |
| `CART_TTL_DAYS` | `30` | Purge age (beat / `manage.py purge_carts`). |
| `BASE_CURRENCY` / `TOTAL_BASE_CONVERTER` | `"USD"` / `money.currencies_converter` | REPLACE dotted path for `Order.total_base` (reporting; NULL when unanswered). |
| `PAYMENT` | `"none"` | Axis. Short name (`none`, `mock`, registered) or dotted path; env may set short names only. |
| `OWNER_CANCELLABLE_STATUSES` | `("new",)` | Where the buyer may cancel. |
| `NOTIFY` | `True` | Best-effort `orders.placed` / `orders.status_changed` notifications. |
| `ORDER_URL_TEMPLATE` | `""` | `{order_id}`/`{number}` link in notifications; empty = no link. |

### The purchasable seam (`registry.py`)

Register a type in settings or from `AppConfig.ready()`:

```python
from stapel_orders.registry import register_purchasable_type
register_purchasable_type("part", {"resolve": "parts.purchasables"})
```

The resolver is a comm Function, keyed batch, one contract for every type
(`schemas/functions/purchasables.json`): `{"keys": [...]}` →
`{key: {title, price, currency, available, max_quantity, seller_id, image, url}}`.
Absent key = unknown (400 on add, 409 at checkout); `available=false` = 409;
unreachable or malformed = 503. Prices are 2-dp strings below 10^10.

### Payment providers (`payment.py`)

`register_payment_provider(name, factory)`; a provider's
`start(order, emit)` runs inside the checkout transaction. Whatever it does,
`paid` is set only by the `payment.completed` consumer (`actions.py`).

### Serializer seams and presenters (`views.py`, `presenters.py`)

Views inherit `StapelAPIView`; set `request_serializer_class` /
`response_serializer_class` on a subclass. Presentation swaps via
`STAPEL_SWAP["STAPEL_ORDERS_CART_PRESENTER"]` /
`["STAPEL_ORDERS_ORDER_PRESENTER"]`.

| View | Request | Response |
|---|---|---|
| `CartView` (GET, DELETE) | — | `CartResponseSerializer` |
| `CartLinesView` (POST) | `CartLineAddRequestSerializer` | `CartResponseSerializer` |
| `CartLineDetailView` (PATCH, DELETE) | `CartLineUpdateRequestSerializer` | `CartResponseSerializer` |
| `CheckoutView` (POST) | `CheckoutRequestSerializer` | `OrderResponseSerializer` |
| `OrderListView` (GET) | — | `OrderPage` envelope of `OrderResponseSerializer` |
| `OrderDetailView` / `OrderCancelView` | — | `OrderResponseSerializer` |
| `OrderTransitionView` (staff) | `OrderTransitionRequestSerializer` | `OrderResponseSerializer` |

Guest stance: every view declares `stapel_anonymous_access`. Cart, checkout
and owner order views are `ANONYMOUS_ALLOWED` (rows scoped to
`request.user`; checkout enforces `CHECKOUT_REQUIRES_ACCOUNT` in the body —
the declaration is static, the policy is a setting); the staff transition is
`IsStaffUser` + `ANONYMOUS_DENIED`. Staff get no bypass on owner views.

### Events & functions (comm surface)

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Action (emits) | `orders.order.placed` | `{order_id, user_id, currency, total, placed_at, lines[]}` | `schemas/emits/` |
| Action (emits) | `orders.order.status_changed` | `{order_id, user_id, from, to, actor, reason?}` | `schemas/emits/` |
| Action (emits) | `orders.order.paid` | `{order_id, provider_ref}` | `schemas/emits/` |
| Action (consumes) | `user.merged` | `{from_user_id, into_user_id}` | `schemas/consumes/` |
| Action (consumes) | `payment.completed` | `{reference_type, reference_key, amount, currency, provider_ref}` | `schemas/consumes/` |
| Action (consumes) | `user.deleted`, `gdpr.erasure.requested` | via `register_gdpr_owner("orders", ["account"], …)` | core |
| Function (provides) | `orders.order.status` | `{order_id}` → `{status, payment_status, user_id, total, currency}` | `schemas/functions/` |
| Function (provides) | `orders.orders_export` | `{cursor, limit}` → `{rows, cursor, total}` | `schemas/functions/` |
| Function (provides) | `orders.cart.count` | `{user_id}` → `{line_count}` | `schemas/functions/` |
| Function (calls) | `<type resolver>` | purchasables contract | `schemas/functions/purchasables.json` |
| Function (calls) | `currencies.convert` | default `TOTAL_BASE_CONVERTER` | stapel-currencies |
| Signal | `orders:user:<id>` / `orders.cart.changed` | `{line_count}` | no-op without a transport |

### Reserved, not built

`STOCK_RESERVATION` (decrement is the owner's reaction to
`orders.order.placed`), taxes/discounts/multi-seller split (`seller_id` is
captured per line), currencies with exponent ≠ 2.

## Contract emission

`make contract` emits `docs/{schema,flows,errors,capabilities}.json`,
`docs/llms.txt` and README.md (from `docs/readme.md`); `SPECTACULAR_SETTINGS`
stays unset (`info.version` is not the contract version). Enum names are
pinned in `_codegen_settings.enum_name_overrides()`.

## Anti-patterns

- **Never import a sibling module** (`stapel_listings`, `stapel_billing`, …);
  `tests/test_architecture.py` greps the package.
- **Never read a purchasable on the cart read path** — snapshots exist so a
  sibling's outage cannot break `GET cart/`.
- **Never convert currency in a cart** or re-round a total.
- **Never set `status`/`payment_status` directly**; go through
  `services.orders.transition` / the payment consumer.
- **Don't bypass the settings namespace** with `os.getenv` at import time.
