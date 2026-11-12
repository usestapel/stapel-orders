"""Checkout hooks: reserve -> (order written) -> commit; release on failure and cancel.

One ``CheckoutHookState`` row per ``(attempt_key, hook name)``:

- :func:`prepare` writes the rows ``pending`` and commits them BEFORE any
  ``reserve`` is called, so a checkout that dies mid-way leaves a durable
  trace the sweep releases (``reason="orphaned"``).
- :func:`reserve_all` calls ``reserve`` inside the checkout transaction; a
  refusal or an unanswered hook raises :class:`OrdersError` (fail closed).
- :func:`mark_placed` moves the rows to ``placed`` in the same transaction
  as the order, and schedules ``commit`` after it commits.
- :func:`release_now` releases what this attempt reserved when the checkout
  fails; :func:`release_for_order` does it on cancel.
- :func:`drop_unsent` closes rows this attempt wrote but never sent a
  reserve for (a checkout refused before the reserve step), without calling
  the companion.
- :func:`sweep` retries unanswered commits and releases and releases orphans.

Every call is idempotent on ``(attempt_key, name)`` at the hook's side; a
release followed by a new reserve of the same pair reserves anew.

Every state change of a row is a conditional UPDATE through :func:`move`
over the table :data:`ROW_TRANSITIONS`; nothing else writes ``state``. A
row never moves backwards: ``placed``/``committed`` → ``pending`` is not an
edge, so a replay twin cannot hand a live order's reservation to the orphan
sweep. A release is claimed (``→ releasing``) before the companion is
called, and ``checkout_failed``/``orphaned`` are claimed only from
``pending`` — never for a pair recorded as placed or committed.
"""
from __future__ import annotations

import datetime
import logging
import uuid

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .. import seams
from ..errors import (
    ERR_409_CHECKOUT_ATTEMPT_CONFLICT,
    ERR_409_CHECKOUT_HOOK_REFUSED,
    ERR_503_CHECKOUT_HOOK_UNAVAILABLE,
)
from ..models import CheckoutAttempt, CheckoutHookState, HookState
from . import OrdersError

logger = logging.getLogger(__name__)

REASON_CHECKOUT_FAILED = "checkout_failed"
REASON_CANCELLED = "cancelled"
REASON_ORPHANED = "orphaned"

EV_REUSE = "reuse"
EV_PLACE = "place"
EV_COMMIT = "commit"
EV_CANCEL = "cancel"
EV_FAIL = "fail"
EV_ORPHAN = "orphan"
EV_RELEASED = "released"
EV_UNSENT = "unsent"

#: event -> (states it may leave, state it enters). Absent pairs are illegal.
ROW_TRANSITIONS: dict[str, tuple[frozenset, str]] = {
    # a released pair is reserved anew by a retry of the same attempt key
    EV_REUSE: (frozenset({HookState.RELEASED}), HookState.PENDING),
    # the order transaction took the reservation (also: the sweep adopts a
    # pending row whose attempt key has an order)
    EV_PLACE: (frozenset({HookState.PENDING}), HookState.PLACED),
    EV_COMMIT: (frozenset({HookState.PLACED}), HookState.COMMITTED),
    # cancel / refund: the only event that undoes a placed or committed pair
    EV_CANCEL: (frozenset({HookState.PLACED, HookState.COMMITTED}), HookState.RELEASING),
    EV_FAIL: (frozenset({HookState.PENDING}), HookState.RELEASING),
    EV_ORPHAN: (frozenset({HookState.PENDING}), HookState.RELEASING),
    EV_RELEASED: (frozenset({HookState.RELEASING}), HookState.RELEASED),
    # written by this attempt, reserve never sent: closed without a call
    EV_UNSENT: (frozenset({HookState.PENDING}), HookState.RELEASED),
}


def move(rows, event: str, **fields) -> int:
    """Apply *event* to *rows* (a queryset) as one conditional UPDATE.

    Only rows in a source state of the event move; the count says how many
    did. The UPDATE takes the row lock and re-reads the state under it, so a
    concurrent mover either wins or finds the row gone from the source set.
    """
    sources, target = ROW_TRANSITIONS[event]
    return rows.filter(state__in=sources).update(
        state=target, updated_at=timezone.now(), **fields
    )


def _entries() -> dict[str, dict]:
    return dict(seams.get_checkout_hooks())


def prepare(attempt_key, user_id: str, written: list[str] | None = None) -> list[str]:
    """Durable ``pending`` rows for every registered hook; names to reserve.

    A row of this attempt key that is placed or committed belongs to an
    order (a twin got there first): it is neither touched nor reserved
    again. A pending one is reserved without a write; a released one is
    reused. A failed attempt's release that is still unanswered is finished
    first, or the checkout is refused (fail closed). *written* collects the
    names whose row this call created or reused (see :func:`drop_unsent`).
    """
    names = []
    for name, _entry in seams.get_checkout_hooks():
        for _round in range(3):
            with transaction.atomic():
                row, created = CheckoutHookState.objects.get_or_create(
                    attempt_key=attempt_key, name=name,
                    defaults={"user_id": user_id, "state": HookState.PENDING},
                )
                if row.user_id != user_id:
                    raise OrdersError(409, ERR_409_CHECKOUT_ATTEMPT_CONFLICT)
                state = row.state
                if not created and state == HookState.RELEASED:
                    created = bool(move(
                        CheckoutHookState.objects.filter(pk=row.pk), EV_REUSE,
                        order=None, token="", release_reason="",
                    ))
                    if not created:
                        continue  # moved under us: read it again
            if created:
                names.append(name)
                if written is not None:
                    written.append(name)
            elif state == HookState.PENDING:
                names.append(name)
            elif state == HookState.RELEASING and row.order_id is None:
                if not _release(row, row.release_reason or REASON_CHECKOUT_FAILED):
                    raise OrdersError(503, ERR_503_CHECKOUT_HOOK_UNAVAILABLE, {"hook": name})
                continue  # released now: reused on the next round
            break
        else:
            raise OrdersError(503, ERR_503_CHECKOUT_HOOK_UNAVAILABLE, {"hook": name})
    return names


def drop_unsent(attempt_key, written: list[str], reserved: list[str]) -> int:
    """Close rows this attempt wrote but sent no reserve for; no companion call.

    A checkout refused before the reserve step (empty cart, a price change,
    an adjuster's error, a changed total) or answered as a replay would
    otherwise leave ``pending`` rows for the orphan sweep to release pairs
    that were never reserved.
    """
    unsent = [name for name in written if name not in reserved]
    if not unsent:
        return 0
    return move(
        CheckoutHookState.objects.filter(
            attempt_key=attempt_key, name__in=unsent, order__isnull=True
        ),
        EV_UNSENT,
    )


def _refusal(name: str, reply: dict) -> OrdersError | None:
    errors = seams.parse_errors(reply.get("errors"))
    if errors:
        first = errors[0]
        return OrdersError(first.http_status(), first.code, {**first.params, "field": first.field})
    if reply.get("ok") is not True:
        return OrdersError(409, ERR_409_CHECKOUT_HOOK_REFUSED, {"hook": name})
    return None


def reserve_all(names: list[str], request: dict, attempt_key, reserved: list[str],
                tokens: dict[str, str]) -> None:
    """Reserve every hook in *names*; *reserved*/*tokens* record what was asked.

    A name is appended to *reserved* BEFORE its call, so an unanswered call
    is released too (the hook may have reserved before the reply was lost).
    """
    entries = _entries()
    payload = {**request, "attempt_key": str(uuid.UUID(str(attempt_key)))}
    for name in names:
        entry = entries.get(name)
        if entry is None:
            continue
        reserved.append(name)
        try:
            reply = seams.call_seam(str(entry["reserve"]), payload, "checkout_hooks.reserve")
            refusal = _refusal(name, reply)
        except seams.SeamUnavailable as exc:
            logger.warning("orders: checkout hook %s reserve unanswered: %s", name, exc)
            raise OrdersError(503, ERR_503_CHECKOUT_HOOK_UNAVAILABLE, {"hook": name}) from None
        if refusal is not None:
            raise refusal
        token = reply.get("token")
        tokens[name] = str(token)[: seams.TOKEN_MAX_LENGTH] if token is not None else ""


def mark_placed(attempt_key, names: list[str], order, tokens: dict[str, str]) -> None:
    """Inside the checkout transaction: the reservations now belong to *order*.

    A row that is no longer ``pending`` (the sweep or a failed twin claimed
    its release meanwhile) refuses the checkout: the companion may already
    have let the reservation go.
    """
    for name in names:
        placed = move(
            CheckoutHookState.objects.filter(attempt_key=attempt_key, name=name),
            EV_PLACE, order=order, token=tokens.get(name, ""),
        )
        if not placed:
            raise OrdersError(503, ERR_503_CHECKOUT_HOOK_UNAVAILABLE, {"hook": name})
    if names:
        order_id = order.pk
        transaction.on_commit(lambda: commit_for_order(order_id))


def _call(row: CheckoutHookState, operation: str, payload: dict) -> bool:
    entry = _entries().get(row.name)
    if entry is None:
        row.last_error = f"hook {row.name} is no longer registered"
    else:
        try:
            reply = seams.call_seam(str(entry[operation]), payload,
                                    f"checkout_hooks.{operation}")
            if reply.get("ok") is True:
                return True
            row.last_error = f"{operation} answered ok={reply.get('ok')!r}"
        except seams.SeamUnavailable as exc:
            row.last_error = str(exc)
    row.last_error = row.last_error[:255]
    row.tries += 1
    # A queryset update leaves updated_at alone: it is the orphan clock.
    CheckoutHookState.objects.filter(pk=row.pk).update(
        last_error=row.last_error, tries=F("tries") + 1
    )
    logger.warning("orders: checkout hook %s %s unanswered (%s), retried by the sweep",
                   row.name, operation, row.last_error)
    return False


def _token(row: CheckoutHookState, tokens: dict | None = None):
    value = (tokens or {}).get(row.name) or row.token
    return value or None


def _commit(row: CheckoutHookState) -> bool:
    from .events import fact_user_id

    actor_id = row.order.user_id if row.order_id else row.user_id
    payload = {
        "attempt_key": str(row.attempt_key),
        "token": _token(row),
        "order_id": str(row.order_id),
        "user_id": fact_user_id(actor_id),
        "actor_id": str(actor_id),
    }
    if not _call(row, "commit", payload):
        return False
    move(CheckoutHookState.objects.filter(pk=row.pk), EV_COMMIT, last_error="")
    return True


def _release(row: CheckoutHookState, reason: str, tokens: dict | None = None) -> bool:
    """Call ``release`` for a row already claimed ``releasing``."""
    from .events import fact_user_id

    payload = {
        "attempt_key": str(row.attempt_key),
        "token": _token(row, tokens),
        "order_id": str(row.order_id) if row.order_id else None,
        "reason": reason,
        "user_id": fact_user_id(row.user_id),
        "actor_id": str(row.user_id),
    }
    if not _call(row, "release", payload):
        return False
    move(CheckoutHookState.objects.filter(pk=row.pk), EV_RELEASED,
         release_reason=reason, last_error="")
    return True


def commit_for_order(order_id) -> int:
    """Commit every placed reservation of an order. Returns how many answered."""
    rows = CheckoutHookState.objects.select_related("order").filter(
        order_id=order_id, state=HookState.PLACED
    )
    return sum(1 for row in _in_hook_order(rows) if _commit(row))


def release_now(attempt_key, names: list[str], tokens: dict[str, str]) -> None:
    """After a failed checkout: release what this attempt asked to reserve.

    Runs outside the (rolled back) checkout transaction. Only ``pending``
    rows without an order are claimed: a pair a twin placed or committed is
    never released as ``checkout_failed``. An unanswered release stays
    ``releasing`` and the sweep retries it.
    """
    rows = CheckoutHookState.objects.filter(
        attempt_key=attempt_key, name__in=names, state=HookState.PENDING,
        order__isnull=True,
    )
    for row in reversed(_in_hook_order(rows)):
        claimed = move(
            CheckoutHookState.objects.filter(pk=row.pk, order__isnull=True),
            EV_FAIL, release_reason=REASON_CHECKOUT_FAILED,
        )
        if not claimed:
            continue
        try:
            _release(row, REASON_CHECKOUT_FAILED, tokens)
        except Exception:  # a failed release must never mask the refusal
            logger.exception("orders: release of checkout hook %s failed", row.name)


def release_for_order(order_id) -> None:
    """Inside the cancel transaction: mark the order's reservations for release."""
    updated = move(CheckoutHookState.objects.filter(order_id=order_id), EV_CANCEL,
                   release_reason=REASON_CANCELLED)
    if updated:
        transaction.on_commit(lambda: _release_marked(order_id))


def _release_marked(order_id) -> int:
    rows = CheckoutHookState.objects.filter(order_id=order_id, state=HookState.RELEASING)
    return sum(
        1 for row in reversed(_in_hook_order(rows))
        if _release(row, row.release_reason or REASON_CANCELLED)
    )


def _in_hook_order(rows) -> list[CheckoutHookState]:
    rank = {name: i for i, (name, _e) in enumerate(seams.get_checkout_hooks())}
    return sorted(rows, key=lambda r: (rank.get(r.name, len(rank)), r.name, r.pk))


def sweep(*, now: datetime.datetime | None = None, orphan_seconds: int | None = None) -> dict:
    """One pass: retry commits and releases, release orphaned reservations."""
    from ..conf import orders_settings

    seconds = int(
        orders_settings.CHECKOUT_HOOK_ORPHAN_SECONDS if orphan_seconds is None else orphan_seconds
    )
    cutoff = (now or timezone.now()) - datetime.timedelta(seconds=seconds)
    counts = {"committed": 0, "released": 0, "orphans_released": 0, "adopted": 0,
              "unanswered": 0}

    for row in CheckoutHookState.objects.select_related("order").filter(state=HookState.PLACED):
        counts["committed" if _commit(row) else "unanswered"] += 1
    for row in CheckoutHookState.objects.filter(state=HookState.RELEASING):
        ok = _release(row, row.release_reason or REASON_CANCELLED)
        counts["released" if ok else "unanswered"] += 1
    orphans = CheckoutHookState.objects.filter(state=HookState.PENDING, updated_at__lt=cutoff)
    for row in list(orphans):
        verdict = _claim_orphan(row.pk, cutoff)
        if verdict == "adopted":
            counts["adopted"] += 1
        elif verdict == "claimed":
            ok = _release(row, REASON_ORPHANED)
            counts["orphans_released" if ok else "unanswered"] += 1
    return counts


def _claim_orphan(pk, cutoff) -> str | None:
    """Under the row lock: is the row still an orphan? Claim it, or adopt it.

    Re-read with ``FOR UPDATE``: still ``pending``, still older than the
    cutoff, and no order exists for its attempt key. A pending row whose
    attempt key HAS an order belongs to that order (it is placed, and
    committed by the next pass) — releasing it would undo a live order.
    """
    with transaction.atomic():
        row = CheckoutHookState.objects.select_for_update().filter(
            pk=pk, state=HookState.PENDING, updated_at__lt=cutoff
        ).first()
        if row is None:
            return None
        attempt = CheckoutAttempt.objects.filter(
            idempotency_key=row.attempt_key, user_id=row.user_id
        ).first()
        rows = CheckoutHookState.objects.filter(pk=pk)
        if attempt is not None and attempt.order_id:
            move(rows, EV_PLACE, order_id=attempt.order_id)
            logger.warning("orders: pending hook row %s of order %s adopted, not released",
                           pk, attempt.order_id)
            return "adopted"
        move(rows, EV_ORPHAN, release_reason=REASON_ORPHANED)
        return "claimed"


__all__ = [
    "REASON_CANCELLED",
    "REASON_CHECKOUT_FAILED",
    "REASON_ORPHANED",
    "ROW_TRANSITIONS",
    "commit_for_order",
    "drop_unsent",
    "mark_placed",
    "move",
    "prepare",
    "release_for_order",
    "release_now",
    "reserve_all",
    "sweep",
]
