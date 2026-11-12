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
- :func:`sweep` retries unanswered commits and releases and releases orphans.

Every call is idempotent on ``(attempt_key, name)`` at the hook's side; a
release followed by a new reserve of the same pair reserves anew.
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
from ..models import CheckoutHookState, HookState
from . import OrdersError

logger = logging.getLogger(__name__)

REASON_CHECKOUT_FAILED = "checkout_failed"
REASON_CANCELLED = "cancelled"
REASON_ORPHANED = "orphaned"

_REUSABLE = (HookState.PENDING, HookState.RELEASED)


def _entries() -> dict[str, dict]:
    return dict(seams.get_checkout_hooks())


def prepare(attempt_key, user_id: str) -> list[str]:
    """Durable ``pending`` rows for every registered hook; names to reserve.

    A row this attempt key already placed (a concurrent twin got there first)
    is left alone and not reserved again; a released one is reused.
    """
    names = []
    for name, _entry in seams.get_checkout_hooks():
        with transaction.atomic():
            row, created = CheckoutHookState.objects.get_or_create(
                attempt_key=attempt_key, name=name,
                defaults={"user_id": user_id, "state": HookState.PENDING},
            )
            if row.user_id != user_id:
                raise OrdersError(409, ERR_409_CHECKOUT_ATTEMPT_CONFLICT)
            if row.state not in _REUSABLE:
                continue
            if not created:
                row.state = HookState.PENDING
                row.order = None
                row.token = ""
                row.release_reason = ""
                row.save(update_fields=["state", "order", "token", "release_reason", "updated_at"])
        names.append(name)
    return names


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
    """Inside the checkout transaction: the reservations now belong to *order*."""
    for name in names:
        CheckoutHookState.objects.filter(attempt_key=attempt_key, name=name).update(
            state=HookState.PLACED, order=order, token=tokens.get(name, ""),
            updated_at=timezone.now(),
        )
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
    payload = {
        "attempt_key": str(row.attempt_key),
        "token": _token(row),
        "order_id": str(row.order_id),
        "user_id": row.order.user_id if row.order_id else None,
    }
    if not _call(row, "commit", payload):
        return False
    CheckoutHookState.objects.filter(pk=row.pk, state=HookState.PLACED).update(
        state=HookState.COMMITTED, last_error="", updated_at=timezone.now(),
    )
    return True


def _release(row: CheckoutHookState, reason: str, tokens: dict | None = None,
             expect=HookState.RELEASING) -> bool:
    payload = {
        "attempt_key": str(row.attempt_key),
        "token": _token(row, tokens),
        "order_id": str(row.order_id) if row.order_id else None,
        "reason": reason,
    }
    if not _call(row, "release", payload):
        return False
    CheckoutHookState.objects.filter(pk=row.pk, state=expect).update(
        state=HookState.RELEASED, release_reason=reason, last_error="",
        updated_at=timezone.now(),
    )
    return True


def commit_for_order(order_id) -> int:
    """Commit every placed reservation of an order. Returns how many answered."""
    rows = CheckoutHookState.objects.select_related("order").filter(
        order_id=order_id, state=HookState.PLACED
    )
    return sum(1 for row in _in_hook_order(rows) if _commit(row))


def release_now(attempt_key, names: list[str], tokens: dict[str, str]) -> None:
    """After a failed checkout: release what this attempt asked to reserve.

    Runs outside the (rolled back) checkout transaction. Rows stay
    ``pending`` when a release is unanswered, so the sweep retries it.
    """
    rows = CheckoutHookState.objects.filter(
        attempt_key=attempt_key, name__in=names, state=HookState.PENDING
    )
    for row in reversed(_in_hook_order(rows)):
        try:
            _release(row, REASON_CHECKOUT_FAILED, tokens, expect=HookState.PENDING)
        except Exception:  # a failed release must never mask the refusal
            logger.exception("orders: release of checkout hook %s failed", row.name)


def release_for_order(order_id) -> None:
    """Inside the cancel transaction: mark the order's reservations for release."""
    updated = CheckoutHookState.objects.filter(
        order_id=order_id, state__in=(HookState.PLACED, HookState.COMMITTED)
    ).update(state=HookState.RELEASING, release_reason=REASON_CANCELLED,
             updated_at=timezone.now())
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
    counts = {"committed": 0, "released": 0, "orphans_released": 0, "unanswered": 0}

    for row in CheckoutHookState.objects.select_related("order").filter(state=HookState.PLACED):
        counts["committed" if _commit(row) else "unanswered"] += 1
    for row in CheckoutHookState.objects.filter(state=HookState.RELEASING):
        ok = _release(row, row.release_reason or REASON_CANCELLED)
        counts["released" if ok else "unanswered"] += 1
    orphans = CheckoutHookState.objects.filter(state=HookState.PENDING, updated_at__lt=cutoff)
    for row in orphans:
        ok = _release(row, REASON_ORPHANED, expect=HookState.PENDING)
        counts["orphans_released" if ok else "unanswered"] += 1
    return counts


__all__ = [
    "REASON_CANCELLED",
    "REASON_CHECKOUT_FAILED",
    "REASON_ORPHANED",
    "commit_for_order",
    "mark_placed",
    "prepare",
    "release_for_order",
    "release_now",
    "reserve_all",
    "sweep",
]
