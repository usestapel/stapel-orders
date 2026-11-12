# Changelog

All notable changes to stapel-orders are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased] — proposed 0.2.0

Checkout seams for commerce companions (promotions, delivery, inventory).
With nothing registered, behaviour and API output are those of 0.1.0 plus
additive fields.

### Added
- **`ADJUSTERS`** merge registry (`{name: {"quote", "order"}}`, empty by
  default) and the contract `schemas/functions/adjusters.json`: adjusters
  run in order, each seeing the adjustments before it and the client's
  opaque `context`; `total = max(0, subtotal + adjustments)`. An error
  refuses checkout with its key (`error.<4xx>.*` keeps its status, else
  409); an unanswered or malformed adjuster is
  `error.503.adjuster_unavailable` (fail closed).
- **`POST cart/quote/`** (`CartQuoteRequest` → `CartQuoteResponse`: the
  cart plus `adjustments` and `errors`, never failing on an adjuster's
  refusal); swap key `STAPEL_ORDERS_QUOTE_PRESENTER`.
- **`context`** on `CheckoutRequest` (opaque object, ≤ 8 KB) passed to every
  adjuster and hook; checkout re-quotes inside its transaction.
- Optional **`expected_total`** on `CheckoutRequest`: a re-quoted total that
  differs refuses with `error.409.total_changed` (`params.total`,
  `params.quote` = the fresh quote) before any checkout hook reserves.
- **`OrderAdjustment`** snapshot rows; `adjustments` on `OrderResponse`
  (kind, code, label, signed amount, line_index); in the GDPR export and the
  admin.
- **`CHECKOUT_HOOKS`** merge registry and the contracts
  `schemas/functions/checkout_hooks.{reserve,commit,release}.json`: reserve
  before the order row is written (refusal or silence refuses: fail
  closed), commit after the order commits, release on a failed checkout and
  on cancel; idempotent on `(attempt_key, name)`. `CheckoutHookState` rows
  are written durably before reserve, so the hook sweep
  (`stapel_orders.beat.sweep_checkout_hooks`, every 5 min, and `manage.py
  sweep_checkout_hooks`) retries unanswered commits/releases and releases
  orphans after `CHECKOUT_HOOK_ORPHAN_SECONDS` (900).
- `orders.order.placed` carries `attempt_key` (the checkout idempotency key
  as a lowercase hyphenated UUID; optional in the schema), so a companion
  finds its checkout-hook intent from the fact.
- `CartLine.attrs`: resolver fields beyond the purchasables contract
  (scalars, ≤ 32), handed to adjusters and hooks as `lines[].attrs`.
- Settings `MAX_ADJUSTMENTS` (20, part of the E005 money proof) and
  `CHECKOUT_HOOK_ORPHAN_SECONDS`; checks E006 / E007 (malformed entries),
  W003 (an adjuster or hook Function unreachable here).
- Error keys `error.409.checkout_hook_refused`,
  `error.409.checkout_attempt_conflict`, `error.503.adjuster_unavailable`,
  `error.503.checkout_hook_unavailable`.
- Migration `0002_checkout_seams`. Tests run on PostgreSQL when
  `STAPEL_ORDERS_TEST_PG` is set (real-lock concurrency proofs in
  `tests/test_checkout_hooks_pg.py`).
- `docs/CONTRACT-DELTAS.md`: where the brick-wave spec was silent, the
  reading built here.

## [0.1.0]

First release: cart, checkout and orders over opaque purchasables.

### Added
- **Purchasable registry** (`PURCHASABLE_TYPES`, merge-over-builtins, empty
  by default; `register_purchasable_type()`), and the keyed-batch resolver
  contract `schemas/functions/purchasables.json`, validated on every reply.
  An unreachable or malformed resolver answers 503, never a guess.
- **Cart** keyed on `request.user` (a guest account is a user): snapshot
  lines, one currency per cart, `MAX_LINE_QUANTITY` (999) and
  `MAX_LINES_PER_CART` (100) caps, `select_for_update` on every write with a
  unique `(cart, type, key)` backstop. `GET cart/` makes zero comm calls.
- **`user.merged`**: the guest cart joins the account's (same pair summed and
  capped, another currency dropped), guest orders follow.
- **Checkout** (`POST checkout/`): re-prices every line, refuses
  price/availability/currency drift with the refreshed cart in
  `params.cart` after committing the new snapshot, idempotent on
  `idempotency_key` (replay = 200, same order), `CHECKOUT_REQUIRES_ACCOUNT`
  (default on).
- **Orders**: FSM table (`fsm.TRANSITIONS`), buyer cancel
  (`OWNER_CANCELLABLE_STATUSES`), staff transitions, anchor-paginated
  history, human order numbers.
- **Events**: `orders.order.placed` (with lines, no PII),
  `orders.order.status_changed`, `orders.order.paid`, all inside
  `mutate_and_emit`; `orders.cart.changed` realtime signal.
- **Payment seam** `PAYMENT` = `none` | `mock` | registered provider; the
  `payment.completed` consumer is the only path to `paid`, and a mismatching
  amount or currency sends the order to `needs_review`.
- **Functions**: `orders.order.status`, `orders.orders_export`,
  `orders.cart.count`.
- **GDPR owner** `orders` (`account`): carts deleted, orders pseudonymized,
  amounts kept.
- **Checks**: E001 (nothing to sell), E003 (malformed type), W001
  (unreachable resolver), E002/W002 (`PAYMENT="mock"` outside/inside the
  prototype stage), E004 (unknown provider), E005 (money bounds vs columns).
- Admin (transition actions through the service), `purge_carts` beat entry
  and management command, contract triad, capabilities, llms.txt, README.
