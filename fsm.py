"""The order lifecycle as a table — the only place an edge is legal.

``TRANSITIONS[(from, to)]`` is the set of actors that may take the edge.
Anything not in the table is ``error.409.order_transition_invalid``. The
owner's cancel is further narrowed by ``OWNER_CANCELLABLE_STATUSES``.

``payment_status`` is a second, independent axis (``PAYMENT_TRANSITIONS``),
moved only by the payment consumer.
"""
from __future__ import annotations

from .models import OrderStatus as S
from .models import PaymentStatus as P

OWNER = "owner"
STAFF = "staff"
SYSTEM = "system"
ACTORS = (OWNER, STAFF, SYSTEM)

TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    (S.NEW, S.CONFIRMED): frozenset({STAFF}),
    (S.NEW, S.CANCELLED): frozenset({OWNER, STAFF}),
    (S.CONFIRMED, S.SHIPPED): frozenset({STAFF}),
    (S.CONFIRMED, S.CANCELLED): frozenset({OWNER, STAFF}),
    (S.SHIPPED, S.COMPLETED): frozenset({STAFF}),
    (S.NEEDS_REVIEW, S.CONFIRMED): frozenset({STAFF}),
    (S.NEEDS_REVIEW, S.CANCELLED): frozenset({STAFF}),
}
# Any status may be flagged for review by the system (a payment mismatch).
for _status in S.values:
    if _status != S.NEEDS_REVIEW:
        TRANSITIONS[(_status, S.NEEDS_REVIEW)] = frozenset({SYSTEM})

PAYMENT_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    (P.UNPAID, P.PENDING): frozenset({SYSTEM}),
    (P.UNPAID, P.PAID): frozenset({SYSTEM}),
    (P.PENDING, P.PAID): frozenset({SYSTEM}),
    (P.PENDING, P.UNPAID): frozenset({SYSTEM}),
    (P.PAID, P.REFUNDED): frozenset({SYSTEM}),
}


class TransitionInvalid(Exception):
    def __init__(self, from_status: str, to_status: str):
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"{from_status} -> {to_status}")


def owner_may_cancel(status: str) -> bool:
    from .conf import orders_settings

    return status in tuple(orders_settings.OWNER_CANCELLABLE_STATUSES or ())


def allowed(from_status: str, to_status: str, actor: str) -> bool:
    actors = TRANSITIONS.get((from_status, to_status))
    if not actors or actor not in actors:
        return False
    if actor == OWNER:
        return to_status == S.CANCELLED and owner_may_cancel(from_status)
    return True


def check(from_status: str, to_status: str, actor: str) -> None:
    if not allowed(from_status, to_status, actor):
        raise TransitionInvalid(from_status, to_status)
