"""Dataclass DTOs — the API models of stapel-orders (never ORM instances).

Built only by the presenters (``presenters.py``); views never instantiate
them. Money is ``Decimal`` here and a string on the wire.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class CartLineDTO:
    """One cart line: a snapshot of the purchasable taken when it was added."""

    id: int
    purchasable_type: str
    purchasable_key: str
    title: str
    quantity: int
    unit_price: Decimal
    currency: str
    line_total: Decimal
    image: Optional[str]
    url: Optional[str]
    seller_id: Optional[str]


@dataclass
class CartDTO:
    """The caller's cart. ``id``/``updated_at`` are null before the first add."""

    id: Optional[int]
    currency: Optional[str]
    lines: list[CartLineDTO] = field(default_factory=list)
    line_count: int = 0
    subtotal: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    updated_at: Optional[datetime.datetime] = None


@dataclass
class ContactDTO:
    """Contact snapshot taken at checkout."""

    name: str
    phone: str
    email: str


@dataclass
class AddressDTO:
    """Shipping address snapshot taken at checkout."""

    line1: str
    line2: str
    city: str
    region: str
    postal_code: str
    country: str


@dataclass
class OrderLineDTO:
    """One order line, frozen at checkout."""

    id: int
    purchasable_type: str
    purchasable_key: str
    title: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    image: Optional[str]
    url: Optional[str]
    seller_id: Optional[str]


@dataclass
class OrderDTO:
    """An order as its owner (or staff) sees it."""

    id: int
    number: str
    status: str
    payment_status: str
    currency: str
    subtotal: Decimal
    total: Decimal
    lines: list[OrderLineDTO]
    contact: ContactDTO
    shipping_address: AddressDTO
    shipping_method: str
    comment: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    cancellable: bool
