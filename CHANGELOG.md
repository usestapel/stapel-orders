# Changelog

All notable changes to stapel-orders are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

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
