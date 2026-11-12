"""Payment providers — how an order gets paid (``STAPEL_ORDERS["PAYMENT"]``).

Orders does not take payments. It asks a provider to start one inside the
checkout transaction and learns the outcome from a payment fact — the
consumer in ``actions.py`` is the only place ``payment_status`` becomes
``paid``:

- ``orders.payment.completed`` — this module's own fact (schema in
  ``schemas/emits/``). Providers registered here emit it.
- ``payment.completed`` with ``reference_type == "order"`` — the fact a
  payment module owns (stapel-billing). Orders only reads it and never emits
  it: the owner's schema is the contract, and a second shape under the same
  name refuses one emitter or the other.

Built-ins:

- ``none`` — nothing is started; the order stays ``unpaid`` and staff
  confirm it by hand.
- ``mock`` — emits ``orders.payment.completed`` (``simulated: true``) for the
  full amount in the checkout transaction. Allowed in the ``prototype``
  posture stage only (check E002/W002); never a ``USE_MOCK_*`` switch.

A real provider is registered by name (:func:`register_payment_provider`)
or named by dotted path in settings; the environment may pick among the
registered short names only.
"""
from __future__ import annotations

from typing import Callable

from django.core.exceptions import ImproperlyConfigured


class PaymentProvider:
    """Interface: ``start(order, emit)`` runs inside the checkout transaction.

    ``emit`` is the ``mutate_and_emit`` emitter of that transaction. Return
    the payment status the order should carry after checkout (``None`` keeps
    ``unpaid``).
    """

    name = ""

    def start(self, order, emit) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError


class NoPayment(PaymentProvider):
    name = "none"

    def start(self, order, emit) -> str | None:
        return None


def payment_completed_payload(order, *, provider_ref: str, simulated: bool = False) -> dict:
    """The ``orders.payment.completed`` payload for *order* paid in full.

    ``user_id`` is null for a guest account, as on every seam payload.
    """
    from .money import to_wire
    from .services.events import fact_user_id

    return {
        "reference_type": "order",
        "reference_key": str(order.pk),
        "amount": to_wire(order.total),
        "currency": order.currency,
        "provider_ref": provider_ref,
        "user_id": fact_user_id(order.user_id),
        "simulated": simulated,
    }


class MockPayment(PaymentProvider):
    """Pays every order in full, at once, through the real consumer path."""

    name = "mock"

    def start(self, order, emit) -> str | None:
        from .services.events import EVENT_PAYMENT_COMPLETED

        emit(
            EVENT_PAYMENT_COMPLETED,
            payment_completed_payload(
                order, provider_ref=f"mock-{order.number}", simulated=True
            ),
            key=str(order.pk),
        )
        return None


_BUILTINS: dict[str, Callable[[], PaymentProvider]] = {
    "none": NoPayment,
    "mock": MockPayment,
}
_registered: dict[str, Callable[[], PaymentProvider]] = {}


def register_payment_provider(name: str, factory: Callable[[], PaymentProvider]) -> None:
    """Register a provider under a short name the settings/env may choose."""
    _registered[name] = factory


def reset_payment_providers() -> None:
    """Tests only."""
    _registered.clear()


def payment_names() -> tuple[str, ...]:
    """The short names ``PAYMENT`` accepts (also the env vocabulary)."""
    return tuple(sorted({**_BUILTINS, **_registered}))


def resolve_payment(value) -> PaymentProvider:
    """Resolver for ``orders_settings.PAYMENT``: short name or dotted path."""
    if isinstance(value, PaymentProvider):
        return value
    name = str(value or "none")
    factory = _registered.get(name) or _BUILTINS.get(name)
    if factory is None:
        if "." not in name:
            raise ImproperlyConfigured(
                f'STAPEL_ORDERS["PAYMENT"] = {name!r} is not a known payment '
                f"provider. Known: {', '.join(payment_names())}."
            )
        from django.utils.module_loading import import_string

        factory = import_string(name)
    provider = factory() if callable(factory) else factory
    if not isinstance(provider, PaymentProvider):
        raise ImproperlyConfigured(
            f'STAPEL_ORDERS["PAYMENT"] = {name!r} does not produce a PaymentProvider'
        )
    if not provider.name:
        provider.name = name
    return provider


def active_provider() -> PaymentProvider:
    from .conf import orders_settings

    return orders_settings.PAYMENT
