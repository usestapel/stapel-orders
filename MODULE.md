# stapel-orders — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same change as any edit
> to a seam. See also README.md and CHANGELOG.md.

## What this module provides

- **Cart** per user (`Cart`, `CartLine`): snapshot lines of opaque
  purchasables `(purchasable_type, purchasable_key)`; one currency per cart.
- **Checkout** turning the cart into an **Order** (`Order`, `OrderLine`),
  idempotent on a client key (`CheckoutAttempt`).
- An order lifecycle FSM (`fsm.py`) with buyer cancel, staff transitions
  and a staff **refund** of a paid order (in full).
- API under `/orders/api/v1/` (`cart/`, `cart/lines/`, `cart/quote/`,
  `checkout/`, `orders/`, `orders/<id>/refund/`), facts `orders.order.*`,
  Functions `orders.*`.
- **Checkout seams** for commerce companions: `ADJUSTERS` (price
  adjustments snapshotted as `OrderAdjustment`) and `CHECKOUT_HOOKS`
  (reserve / commit / release, state in `CheckoutHookState`).

## Extension points (fork-free)

### Settings — `STAPEL_ORDERS` namespace (`conf.py`, registry in CONFIG.MD)

| Key | Default | What it customizes |
|---|---|---|
| `PURCHASABLE_TYPES` | `{}` | MERGE registry `{type: {"resolve": "<function>"}}`; `None` removes. Empty = E001. |
| `CHECKOUT_REQUIRES_ACCOUNT` | `True` | Axis. Guests get `error.403.checkout_requires_account`. A switch, not posture. |
| `MAX_LINE_QUANTITY` / `MAX_LINES_PER_CART` | `999` / `100` | Caps; both feed the money proof (E005). |
| `CART_TTL_DAYS` | `30` | Purge age (beat / `manage.py purge_carts`). |
| `BASE_CURRENCY` / `TOTAL_BASE_CONVERTER` | `"USD"` / `money.currencies_converter` | REPLACE dotted path for `Order.total_base` (reporting; NULL when unanswered). |
| `ADJUSTERS` | `{}` | MERGE registry `{name: {"quote": "<function>", "order": 100}}`; `None` removes. See *Checkout seams*. |
| `CHECKOUT_HOOKS` | `{}` | MERGE registry `{name: {"reserve", "commit", "release": "<function>", "order": 100}}`; `None` removes. |
| `MAX_ADJUSTMENTS` | `20` | Adjustments per quote across all adjusters; feeds E005. |
| `CHECKOUT_HOOK_ORPHAN_SECONDS` | `900` | Age after which the hook sweep releases an unfinished checkout's reservations. |
| `PAYMENT` | `"none"` | Axis. Short name (`none`, `mock`, registered) or dotted path; env may set short names only. |
| `OWNER_CANCELLABLE_STATUSES` | `("new",)` | Where the buyer may cancel. |
| `NOTIFY` | `True` | Best-effort `orders.placed` / `orders.status_changed` / `orders.refunded` notifications. |
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

### Checkout seams (`seams.py`, `services/hooks.py`) — SPEC-BRICKS-WAVE §2

Companions never import orders and orders never imports them: each seam is
a comm Function named in settings. A companion ships the fragment in its
`preset.SETTINGS_DEFAULTS` (merged two levels deep under the host's own
`STAPEL_ORDERS`), e.g.:

```python
SETTINGS_DEFAULTS = {"STAPEL_ORDERS": {
    "ADJUSTERS": {"promotions": {"quote": "promotions.quote", "order": 100}},
    "CHECKOUT_HOOKS": {"promotions": {"reserve": "promotions.reserve",
                                      "commit": "promotions.commit",
                                      "release": "promotions.release"}},
}}
```

Runtime registration (`register_adjuster`, `register_checkout_hook`) is for
tests and hosts; `reset_seams()` drops it.

**Adjusters** (`schemas/functions/adjusters.json`), ordered by `order`
(discounts 100, shipping 200, tax 300), ties by name. Payloads, verbatim
from the spec:

- Request: `{"currency", "subtotal", "user_id" | null, "actor_id", "lines": [{"index", "purchasable_type", "purchasable_key", "quantity", "unit_price", "line_total", "attrs": {}}], "adjustments_so_far": [...], "context": {...}}`
  — `context` is the opaque dict the client sends on quote and checkout
  (`codes: [..]`, `delivery: {method, address | pickup_point}`), validated
  by the adjuster that owns the key, never by orders.
- Response: `{"adjustments": [{"kind": "discount" | "shipping" | "tax" | "fee", "code", "label", "amount" (signed decimal string, cart currency), "line_index" | null, "meta": {}}], "errors": [{"code", "field" | null, "params": {}}]}`.

A `delivery` key in `context` hands the address to the delivery companion:
checkout then neither requires nor validates `shipping_address` (a sent one
is ignored, never compared), and the order's `shipping_address` is null; the
companion's address is authoritative. `Order.shipping_method` is the first
`shipping` adjustment's `code` when there is one.

Any error refuses the checkout with that code; `POST cart/quote/` returns
them without failing. Optional `expected_total` on checkout (decimal string, cart
currency — the total the shopper was shown): when present and the re-quote
inside the checkout transaction differs, checkout refuses with
`error.409.total_changed`, `params.total` = the fresh total and
`params.quote` = the fresh `CartQuoteResponse`. The comparison runs after
the adjusters and BEFORE any hook reserves, so a refusal reserves nothing;
the same attempt key resubmitted reserves anew. Absent: no comparison. Orders re-quotes inside the checkout transaction and
snapshots `OrderAdjustment` rows; `total = max(0, subtotal + Σ amount)`.
The readings where the spec was silent (`attrs` = the resolver's extra
scalar fields; `user_id` null for a guest; `adjustments_so_far` items carry
`adjuster`; unanswered = `error.503.adjuster_unavailable`; status from the
error key; `meta` not on the public wire and never personal data) are in
`docs/CONTRACT-DELTAS.md`.

**Checkout hooks** (`schemas/functions/checkout_hooks.{reserve,commit,release}.json`):

- `reserve` runs before the order row is written, payload = the adjuster
  request (`adjustments_so_far` = the final list) plus `"attempt_key"` (the
  checkout idempotency key) → `{"ok", "token", "errors"}`.
- `commit` `{attempt_key, token | null, order_id, user_id | null, actor_id}` → `{ok}`,
  after the order transaction commits (`orders.order.placed`).
- `release` `{attempt_key, token | null, order_id | null, reason:
  checkout_failed | cancelled | orphaned, user_id | null, actor_id}` → `{ok}`
  — on a failed checkout (reverse order) and on any transition to
  `cancelled`.

On every adjuster and hook payload `user_id` is null for a guest account and
`actor_id` is the acting account id (guest or not, always sent);
`orders.order.placed` carries `actor_id` too (its `user_id` stays the stored
account id, a guest's included).

Commit and release answers may carry keys beyond `ok` (`token`, `errors`,
…); orders reads `ok` only. `ok: true` is final — orders never re-sends that
call, so a companion that does not know the attempt key may answer `ok: true`
(logging on its side) to stop the retries. A cancel that arrives before an
unanswered commit wins: the commit is not retried and `release` (reason
`cancelled`) is sent; a late commit at the companion is its no-op.
`orders.order.placed` carries `attempt_key` (since 0.2.0), so a companion can
find its checkout intent from the fact.

A replay of an attempt key always answers the order it placed: any refusal
before the cart lock re-checks the key behind the lock first. All three are idempotent on `(attempt_key, name)`; a release followed by a
reserve of the same pair reserves anew (a refused checkout is retried with
the same key). A hook that is registered but unanswered refuses the
checkout (fail closed, `error.503.checkout_hook_unavailable`).
`CheckoutHookState` (`pending → placed → committed`, `releasing →
released`) is written durably BEFORE reserve. Its states move only through
`services.hooks.move()` over `ROW_TRANSITIONS` (conditional UPDATEs; a row
never moves backwards, `checkout_failed`/`orphaned` are claimed from
`pending` only; only cancel/refund undoes a placed or committed pair). Rows
an attempt wrote but sent no reserve for (refused before the reserve step,
or a replay) are closed without a companion call. The hook sweep
(`beat.sweep_checkout_hooks`, `manage.py sweep_checkout_hooks`) retries
unanswered commits/releases and releases `pending` rows older than
`CHECKOUT_HOOK_ORPHAN_SECONDS` (re-checked under `FOR UPDATE`; a pending row
whose attempt key has an order is adopted, not released). Proven under real row locks on PostgreSQL
by `tests/test_checkout_hooks_pg.py` (`STAPEL_ORDERS_TEST_PG`).

### Payment providers (`payment.py`)

`register_payment_provider(name, factory)`; a provider's
`start(order, emit)` runs inside the checkout transaction and
`refund(order, emit)` inside the refund transaction (default: refuses with
`RefundUnsupported`). Whatever it does,
`paid` is set only by the payment consumer (`actions.py`), which reads two
facts with one handler:

- `orders.payment.completed` — orders' own fact (`schemas/emits/`). A
  provider registered here emits it, built by
  `payment.payment_completed_payload(order, provider_ref=…, simulated=…)`;
  `user_id` is null for a guest account, `simulated` is true when no money
  moved (the mock). Amount and currency must equal the order's, otherwise
  the order goes to `needs_review`.
- `payment.completed` — **owned by the payment module (stapel-billing)**,
  only read here, and only when it carries `reference_type == "order"`
  (billing ask B1). Orders never emits it. A host provider that emits
  `payment.completed` for an order is deprecated since 0.2.0: next to
  billing that name is validated against billing's schema and refused.

### Refund (`services.orders.refund_order`, `POST orders/<id>/refund/`)

Staff only. A paid order (`payment_status == paid`) in any status but
`cancelled` is refunded **in full**; partial refunds are out of scope in
this version. The `PAYMENT` provider's `refund(order, emit)` runs first in
the refund transaction (`none` and `mock` succeed trivially; a provider that
does not override it refuses with `error.409.refund_unsupported`, nothing
changes). Then `payment_status` and `status` become `refunded`,
`orders.order.status_changed` (`to: refunded`) and `orders.order.refunded`
are emitted, the buyer gets the `orders.refunded` notification, and every
checkout hook gets `release` with reason `cancelled` once — the same path as
a cancel, so companions return stock, a code use, a shipment. A second
refund answers the refunded order and does nothing; a cancelled order is
`error.409.order_transition_invalid` (its reservations are already
released: refund its money out of band); an unpaid one is
`error.409.order_not_refundable`. `POST transition/ {"to": "refunded"}` takes
the same path. Cancelling a paid order does not move money: refund it.

### Serializer seams and presenters (`views.py`, `presenters.py`)

Views inherit `StapelAPIView`; set `request_serializer_class` /
`response_serializer_class` on a subclass. Presentation swaps via
`STAPEL_SWAP["STAPEL_ORDERS_CART_PRESENTER"]` /
`["STAPEL_ORDERS_ORDER_PRESENTER"]` / `["STAPEL_ORDERS_QUOTE_PRESENTER"]`.

| View | Request | Response |
|---|---|---|
| `CartView` (GET, DELETE) | — | `CartResponseSerializer` |
| `CartLinesView` (POST) | `CartLineAddRequestSerializer` | `CartResponseSerializer` |
| `CartLineDetailView` (PATCH, DELETE) | `CartLineUpdateRequestSerializer` | `CartResponseSerializer` |
| `CartQuoteView` (POST) | `CartQuoteRequestSerializer` | `CartQuoteResponseSerializer` |
| `CheckoutView` (POST) | `CheckoutRequestSerializer` | `OrderResponseSerializer` |
| `OrderListView` (GET) | — | `OrderPage` envelope of `OrderResponseSerializer` |
| `OrderDetailView` / `OrderCancelView` | — | `OrderResponseSerializer` |
| `OrderTransitionView` (staff) | `OrderTransitionRequestSerializer` | `OrderResponseSerializer` |
| `OrderRefundView` (staff) | `OrderRefundRequestSerializer` | `OrderResponseSerializer` |

Guest stance: every view declares `stapel_anonymous_access`. Cart, checkout
and owner order views are `ANONYMOUS_ALLOWED` (rows scoped to
`request.user`; checkout enforces `CHECKOUT_REQUIRES_ACCOUNT` in the body —
the declaration is static, the policy is a setting); the staff transition is
`IsStaffUser` + `ANONYMOUS_DENIED`. Staff get no bypass on owner views.

### Events & functions (comm surface)

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Action (emits) | `orders.order.placed` | `{order_id, user_id, actor_id, currency, total, placed_at, attempt_key, lines[]}` | `schemas/emits/` |
| Action (emits) | `orders.order.status_changed` | `{order_id, user_id, from, to, actor, reason?}` | `schemas/emits/` |
| Action (emits) | `orders.order.paid` | `{order_id, provider_ref}` | `schemas/emits/` |
| Action (emits) | `orders.order.refunded` | `{order_id, attempt_key?, user_id \| null, total, currency}` | `schemas/emits/` |
| Action (emits) | `orders.payment.completed` | `{reference_type: "order", reference_key, amount, currency, provider_ref, user_id \| null, simulated}` | `schemas/emits/` |
| Action (consumes) | `user.merged` | `{from_user_id, into_user_id}` | `schemas/consumes/` |
| Action (consumes) | `orders.payment.completed` | own fact (above) | `schemas/emits/` |
| Action (consumes) | `payment.completed` (billing's) | reads `{reference_type: "order", reference_key, amount, currency, provider_ref}`, ignores anything else | `schemas/consumes/` (documentation; never registered) |
| Action (consumes) | `user.deleted`, `gdpr.erasure.requested` | via `register_gdpr_owner("orders", ["account"], …)` | core |
| Function (provides) | `orders.order.status` | `{order_id}` → `{status, payment_status, user_id, total, currency}` | `schemas/functions/` |
| Function (provides) | `orders.orders_export` | `{cursor, limit}` → `{rows, cursor, total}` | `schemas/functions/` |
| Function (provides) | `orders.cart.count` | `{user_id}` → `{line_count}` | `schemas/functions/` |
| Function (calls) | `<type resolver>` | purchasables contract | `schemas/functions/purchasables.json` |
| Function (calls) | `<adjuster quote>` | adjusters contract | `schemas/functions/adjusters.json` |
| Function (calls) | `<hook reserve/commit/release>` | checkout-hooks contracts | `schemas/functions/checkout_hooks.*.json` |
| Function (calls) | `currencies.convert` | default `TOTAL_BASE_CONVERTER` | stapel-currencies |
| Signal | `orders:user:<id>` / `orders.cart.changed` | `{line_count}` | no-op without a transport |

### Reserved, not built

Multi-seller split (`seller_id` is captured per line), currencies with
exponent ≠ 2. Stock, discounts, shipping and tax are companions behind the
checkout seams, not code here.

## Contract emission

`make contract` emits `docs/{schema,flows,errors,capabilities}.json`,
`docs/llms.txt` and README.md (from `docs/readme.md`); `SPECTACULAR_SETTINGS`
stays unset (`info.version` is not the contract version). Enum names are
pinned in `_codegen_settings.enum_name_overrides()`.

## Anti-patterns

- **Never import a sibling module** (`stapel_listings`, `stapel_billing`, …);
  `tests/test_architecture.py` greps the package.
- **Never import a companion** (`stapel_promotions`, `stapel_delivery`,
  `stapel_inventory`); they are Functions named in `ADJUSTERS` /
  `CHECKOUT_HOOKS`. Never validate a `context` key in orders.
- **Never call a hook or adjuster and continue without its answer**: an
  unanswered seam refuses (fail closed).
- **Never read a purchasable on the cart read path** — snapshots exist so a
  sibling's outage cannot break `GET cart/`.
- **Never convert currency in a cart** or re-round a total.
- **Never emit a fact another module owns, and never pass `schema=` to
  `on_action` for one**: the schema registry is one per name and validates
  the emitter, so a consumer-side copy refuses the owner's payloads
  (`tests/test_fact_schemas.py`).
- **Never set `status`/`payment_status` directly**; go through
  `services.orders.transition` / the payment consumer.
- **Don't bypass the settings namespace** with `os.getenv` at import time.
