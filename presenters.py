"""Presenters for stapel-orders — the DTO-building layer (§55).

Views never instantiate a ``dto.py`` dataclass (SWAP002) nor import a
concrete presenter (SWAP001); they resolve one through ``get_presenter`` so
a host can swap the presentation via ``STAPEL_SWAP`` without forking.
"""
from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

from stapel_core.django.swappable import declare_swap, get_presenter

from . import money
from .dto import (
    AddressDTO,
    AdjustmentDTO,
    CartDTO,
    CartLineDTO,
    CartQuoteDTO,
    ContactDTO,
    OrderDTO,
    OrderLineDTO,
    QuoteErrorDTO,
)

CART_PRESENTER_KEY = "STAPEL_ORDERS_CART_PRESENTER"
ORDER_PRESENTER_KEY = "STAPEL_ORDERS_ORDER_PRESENTER"
QUOTE_PRESENTER_KEY = "STAPEL_ORDERS_QUOTE_PRESENTER"
DEFAULT_CART_PRESENTER = "stapel_orders.presenters.CartPresenter"
DEFAULT_ORDER_PRESENTER = "stapel_orders.presenters.OrderPresenter"
DEFAULT_QUOTE_PRESENTER = "stapel_orders.presenters.QuotePresenter"

declare_swap(CART_PRESENTER_KEY, DEFAULT_CART_PRESENTER)
declare_swap(ORDER_PRESENTER_KEY, DEFAULT_ORDER_PRESENTER)
declare_swap(QUOTE_PRESENTER_KEY, DEFAULT_QUOTE_PRESENTER)


def _or_none(value: str) -> str | None:
    return value or None


class CartPresenter:
    """Cart (or no cart yet) -> CartDTO. Reads rows only; never a sibling."""

    def present(self, cart, lines=None) -> CartDTO:
        if cart is None:
            return CartDTO(id=None, currency=None)
        rows = list(lines if lines is not None else cart.lines.all())
        line_dtos = [
            CartLineDTO(
                id=line.pk,
                purchasable_type=line.purchasable_type,
                purchasable_key=line.purchasable_key,
                title=line.title,
                quantity=line.quantity,
                unit_price=line.unit_price,
                currency=line.currency,
                line_total=line.line_total,
                image=_or_none(line.image),
                url=_or_none(line.url),
                seller_id=_or_none(line.seller_id),
                attrs=dict(line.attrs or {}),
            )
            for line in rows
        ]
        subtotal = money.sum_lines(Decimal(line.line_total) for line in rows)
        return CartDTO(
            id=cart.pk,
            currency=cart.currency or None,
            lines=line_dtos,
            line_count=len(line_dtos),
            subtotal=subtotal,
            total=subtotal,
            updated_at=cart.updated_at,
        )


class QuotePresenter:
    """(cart, seams.Quote) -> CartQuoteDTO: the cart DTO plus adjustments."""

    def present(self, cart, quote) -> CartQuoteDTO:
        base = get_cart_presenter()().present(cart)
        data = {f.name: getattr(base, f.name) for f in fields(CartDTO)}
        data["total"] = quote.total if base.lines else base.total
        return CartQuoteDTO(
            **data,
            adjustments=[
                AdjustmentDTO(
                    kind=a.kind, code=a.code, label=a.label,
                    amount=a.amount, line_index=a.line_index,
                )
                for a in quote.adjustments
            ],
            errors=[
                QuoteErrorDTO(code=e.code, field=e.field, params=dict(e.params))
                for e in quote.errors
            ],
        )


class OrderPresenter:
    """Order -> OrderDTO, ``cancellable`` from the FSM and the settings."""

    def present(self, order) -> OrderDTO:
        from .fsm import OWNER, allowed
        from .models import OrderStatus

        return OrderDTO(
            id=order.pk,
            number=order.number,
            status=order.status,
            payment_status=order.payment_status,
            currency=order.currency,
            subtotal=order.subtotal,
            total=order.total,
            lines=[
                OrderLineDTO(
                    id=line.pk,
                    purchasable_type=line.purchasable_type,
                    purchasable_key=line.purchasable_key,
                    title=line.title,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    line_total=line.line_total,
                    image=_or_none(line.image),
                    url=_or_none(line.url),
                    seller_id=_or_none(line.seller_id),
                )
                for line in order.lines.all()
            ],
            contact=ContactDTO(
                name=order.contact_name,
                phone=order.contact_phone,
                email=order.contact_email,
            ),
            shipping_address=AddressDTO(
                line1=order.address_line1,
                line2=order.address_line2,
                city=order.address_city,
                region=order.address_region,
                postal_code=order.address_postal_code,
                country=order.address_country,
            ),
            shipping_method=order.shipping_method,
            comment=order.comment,
            created_at=order.created_at,
            updated_at=order.updated_at,
            cancellable=allowed(order.status, OrderStatus.CANCELLED, OWNER),
            adjustments=[
                AdjustmentDTO(
                    kind=a.kind, code=a.code, label=a.label,
                    amount=a.amount, line_index=a.line_index,
                )
                for a in order.adjustments.all()
            ],
        )


def get_cart_presenter() -> type:
    return get_presenter(CART_PRESENTER_KEY, default=DEFAULT_CART_PRESENTER)


def get_order_presenter() -> type:
    return get_presenter(ORDER_PRESENTER_KEY, default=DEFAULT_ORDER_PRESENTER)


def get_quote_presenter() -> type:
    return get_presenter(QUOTE_PRESENTER_KEY, default=DEFAULT_QUOTE_PRESENTER)


__all__ = [
    "CART_PRESENTER_KEY",
    "ORDER_PRESENTER_KEY",
    "QUOTE_PRESENTER_KEY",
    "CartPresenter",
    "OrderPresenter",
    "QuotePresenter",
    "get_cart_presenter",
    "get_order_presenter",
    "get_quote_presenter",
]
