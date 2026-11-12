# Contract deltas — SPEC-BRICKS-WAVE-2026-11-12 §2 as built in stapel-orders 0.2.0

Where the spec text was silent or ambiguous, stapel-orders implements the
reading below. Companions (promotions, delivery, inventory) code to this
file together with the spec; the JSON schemas in `schemas/functions/` are
the machine form.

## Adjusters

1. **Where `order` lives.** Inside the entry:
   `{"<name>": {"quote": "<function>", "order": 100}}`. Ties sort by name.
2. **`lines[].attrs`** carries the resolver item's fields beyond the
   purchasables contract's known keys (`title, price, currency, available,
   max_quantity, seller_id, image, url`), scalars only (string, number,
   bool, null), at most 32 keys, key length ≤ 64. Snapshotted on the cart
   line at add and refreshed at checkout, so quote and checkout send the
   same attrs. This is how delivery gets a weight: the purchasable resolver
   answers an extra `"weight_grams": 1250` (or similar) and it arrives as
   `attrs.weight_grams`. Orders does not interpret attrs.
3. **`user_id` is `null` for a guest** (a stapel-auth anonymous account);
   otherwise `str(user.pk)`.
4. **`adjustments_so_far`** items are earlier adjusters' adjustments,
   normalized (amount as a 2-dp string, `line_index` int or null, `meta`
   object), plus `"adjuster": "<name>"`. The reserve payload's
   `adjustments_so_far` is the complete final list.
5. **Unanswered or malformed adjuster fails closed.** Quote: an error entry
   `{"code": "error.503.adjuster_unavailable", "field": null, "params":
   {"adjuster": "<name>"}}`. Checkout: refused 503 with that key.
   Malformed means: not an object, an unknown `kind`, an `amount` that is
   not a decimal string with ≤ 2 dp and |amount| < 10^14, a `line_index`
   outside the lines, `code` longer than 64, more than `MAX_ADJUSTMENTS`
   (default 20) adjustments in one quote across all adjusters.
6. **Error code → HTTP status at checkout.** `error.<4xx>.<slug>` refuses
   with that status; any other code refuses with 409. The envelope's
   `params` are the error's `params` plus `"field"`. The first error (in
   adjuster order) is the one returned; all adjusters run.
7. **Empty cart:** quote calls no adjuster.
8. `meta` is snapshotted on `OrderAdjustment`, not shown on the public
   wire, and survives GDPR erasure: **never put personal data in `meta`**.
9. Totals: `total = max(0, subtotal + Σ amount)`; adjustments are always
   snapshotted as answered (the floor is not distributed back onto them).

## Checkout hooks

10. **Entry:** `{"reserve", "commit", "release"}` all required, optional
    `"order"` (default 100, ties by name). Reserve runs in that order;
    release runs in reverse.
11. **Reserve answer:** refused when `ok` is not `true` or `errors` is
    non-empty (first error's code, status rule of item 6; `ok: false` with
    no errors refuses with `error.409.checkout_hook_refused`). `token` is a
    string or null. Unanswered or malformed: `error.503.checkout_hook_unavailable`.
12. **Commit payload** (not in the spec): `{"attempt_key", "token" | null,
    "order_id", "user_id" | null}` → `{"ok": bool}`.
13. **Release payload** (not in the spec): `{"attempt_key", "token" | null,
    "order_id" | null, "reason": "checkout_failed" | "cancelled" |
    "orphaned"}` → `{"ok": bool}`.
14. **`token` may be null on commit/release** (after a crash the token lived
    in a rolled-back transaction). Companions commit and release by
    `(attempt_key, name)` alone; the token is a convenience. A release may
    also arrive for a pair that was never reserved (the checkout died
    between writing its state rows and calling reserve): answer `ok`.
15. **Reserve after release reserves anew.** A refused checkout is retried
    with the same idempotency key; a released `(attempt_key, name)` must
    accept a fresh reserve. Reserve on a live (not released) pair answers
    the same reservation.
16. **Commit runs after the order transaction commits** (on_commit), not
    inside it; until it answers `ok` the beat sweep retries it. Release on
    cancel likewise runs after the cancel commits, retried until `ok`.
    Cancel = any transition to `cancelled` (owner, staff or system).
17. **Orphans:** a reserve whose checkout never finished (crash) is
    released by the beat task `stapel_orders.beat.sweep_checkout_hooks`
    (and `manage.py sweep_checkout_hooks`) after
    `CHECKOUT_HOOK_ORPHAN_SECONDS` (default 900) with `reason: "orphaned"`.
    Companions should still keep their own hold TTL longer than that.
18. `attempt_key` is the checkout `idempotency_key` as a lowercase
    hyphenated UUID string. A key already used by another user is refused
    409 `error.409.checkout_attempt_conflict`.

## Accepted follow-ups (foreman, 2026-11-12)

20. `orders.order.placed` carries `attempt_key` (lowercase hyphenated UUID,
    the same string the hooks saw); optional in the schema for consumers of
    older emitters.
21. Commit/release answers may carry keys beyond `ok`; orders reads `ok`
    only. `ok: true` is final and never re-sent, so a companion that does
    not know an attempt key may answer `ok: true` to stop the retries.
22. A cancel before an unanswered commit wins: the commit is not retried,
    release (`cancelled`) is sent; a late commit is the companion's no-op.

23. Optional `expected_total` on checkout (orders-side only, companions
    unaffected): compared after the adjusters, before any reserve; a
    mismatch is `error.409.total_changed` with the fresh `total` and
    `quote` in params, and reserves nothing.

## Payment facts (defect O2, 2026-11-12)

24. **Orders never emits `payment.completed`.** That fact is stapel-billing's
    (billing ships `schemas/emits/payment.completed.json` and emits it with
    `user_id`, `amount_cents`, `transaction_id`, `created_at`, …). The
    `PAYMENT` providers registered in orders, the mock included, emit
    **`orders.payment.completed`** `{reference_type: "order", reference_key,
    amount, currency, provider_ref, user_id | null, simulated}`; `user_id` is
    null for a guest account (as in item 3), `simulated` is true for the mock.
    Orders' consumer reads both names; on `payment.completed` it acts only
    on `reference_type == "order"`. The old name is not emitted alongside
    for a deprecation minor: an emit of it under billing's schema is the
    defect itself, and its only 0.1.0 consumer is orders' own handler,
    which still reads it. Companions that watched the mock's
    `payment.completed` read `orders.payment.completed` or
    `orders.order.paid`.
25. **Consumer-side schemas are not registered.** `on_action(schema=)` for
    `payment.completed` / `user.merged` is gone: core keeps one schema per
    action name and validates the emitter against it, so orders' copy
    replaced billing's whenever core's autoload ran first and refused
    billing's own payments. The same class sits in core
    (`on_action(schema=)` writes the emit registry) and in
    stapel-reservations 0.1.0 (same mock, same consumer schemas).

## Release reasons (decided, foreman 2026-11-12)

26. A `release` whose `reason` is `checkout_failed` or `orphaned` must be a
    no-op answering `ok: true` at the companion for a pair it has already
    committed; only `cancelled` (sent on cancel and on refund) undoes a
    commit. Orders' side: it never sends `checkout_failed` or `orphaned` for
    a pair it recorded as placed or committed (those reasons are claimed
    only from `pending`), and a checkout refused before the reserve step
    sends no release at all (no reserve was sent).

## Wiring

19. `SETTINGS_DEFAULTS` is merged by stapel-tools' scaffold only for
    composites today (`_wiring.render_preset_defaults`); companion bricks
    need the registry/wiring to merge their `preset.SETTINGS_DEFAULTS` too
    (stapel-tools follow-up). Hand-wired hosts paste the fragment.
