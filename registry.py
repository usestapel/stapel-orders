"""Purchasable-type registry and the keyed-batch resolver client.

The module ships knowing **no** purchasable types. A host (or a composite
such as stapel-shop) registers each type with the comm Function that
resolves it::

    STAPEL_ORDERS = {"PURCHASABLE_TYPES": {
        "listing": {"resolve": "listings.purchasables"},
        "part":    {"resolve": "fitment.purchasables"},
    }}

or at runtime from an ``AppConfig.ready()`` with :func:`register_purchasable_type`.
Built-ins <- settings <- runtime, ``None`` removes a type.

The resolver contract (``schemas/functions/purchasables.json``) is one
keyed batch for every type::

    {"keys": [str, ...]}  ->  {key: {"title", "price", "currency",
        "available", "max_quantity", "seller_id", "image", "url"}}

An absent key is unknown. Fields beyond these (a weight, a tax class) are
kept as the line's ``attrs`` — scalars only, bounded — and handed to the
checkout adjusters and hooks untouched. Orders asks the resolver only on add and at
checkout; reading a cart never calls it. The call rides
``FUNCTION_TRANSPORT`` — monolith or fleet is a transport question only.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from . import money

logger = logging.getLogger(__name__)

#: Built-ins are EMPTY — the module sells nothing until a host says what.
BUILTIN_PURCHASABLE_TYPES: dict[str, Optional[dict]] = {}

_runtime_types: dict[str, Optional[dict]] = {}

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "functions" / "purchasables.json"
_schema_cache: dict | None = None

#: Item fields the purchasables contract names; everything else is ``attrs``.
KNOWN_ITEM_FIELDS = frozenset(
    ("title", "price", "currency", "available", "max_quantity", "seller_id", "image", "url")
)
ATTRS_MAX_KEYS = 32
ATTRS_KEY_MAX_LENGTH = 64
ATTRS_STRING_MAX_LENGTH = 255


class UnknownPurchasableType(Exception):
    """The type is not registered here."""


class ResolverUnavailable(Exception):
    """The resolver cannot be reached or answered something unusable.

    Never turned into a guess: the caller answers 503.
    """


@dataclass(frozen=True)
class Resolved:
    """One resolver answer, parsed and validated."""

    title: str
    price: Decimal
    currency: str
    available: bool
    max_quantity: Optional[int]
    seller_id: str
    image: str
    url: str
    attrs: dict = field(default_factory=dict)


def _attr_value_ok(value) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= ATTRS_STRING_MAX_LENGTH


def _extract_attrs(raw: dict) -> dict:
    """The resolver's extra scalar fields, sorted, at most ``ATTRS_MAX_KEYS``.

    Non-scalar values, over-long keys or strings are dropped, never guessed.
    """
    extras = {
        key: raw[key]
        for key in sorted(raw)
        if key not in KNOWN_ITEM_FIELDS
        and isinstance(key, str)
        and 0 < len(key) <= ATTRS_KEY_MAX_LENGTH
        and _attr_value_ok(raw[key])
    }
    return dict(list(extras.items())[:ATTRS_MAX_KEYS])


def register_purchasable_type(name: str, entry: Optional[dict]) -> None:
    """Register/override a purchasable type at runtime (``None`` removes it)."""
    _runtime_types[name] = entry


def reset_purchasable_types() -> None:
    """Tests only: drop runtime registrations."""
    _runtime_types.clear()


def get_purchasable_types() -> dict[str, dict]:
    """Effective registry: built-ins <- settings <- runtime, live entries only."""
    from .conf import orders_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_PURCHASABLE_TYPES)
    configured = orders_settings.PURCHASABLE_TYPES or {}
    if isinstance(configured, dict):
        merged.update(configured)
    merged.update(_runtime_types)
    return {name: entry for name, entry in merged.items() if entry is not None}


def resolver_name(purchasable_type: str) -> str:
    entry = get_purchasable_types().get(purchasable_type)
    if not isinstance(entry, dict) or not entry.get("resolve"):
        raise UnknownPurchasableType(purchasable_type)
    return str(entry["resolve"])


def _result_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        full = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        _schema_cache = {"$defs": full["$defs"], "$ref": "#/$defs/result"}
    return _schema_cache


def _validate_reply(name: str, reply) -> None:
    from stapel_core.comm.config import validation_enabled

    if not validation_enabled():
        return
    import jsonschema

    try:
        jsonschema.validate(reply, _result_schema())
    except jsonschema.ValidationError as exc:
        raise ResolverUnavailable(
            f"resolver {name} answered outside the purchasables contract: {exc.message}"
        ) from exc


def _parse(name: str, key: str, raw) -> Resolved:
    if not isinstance(raw, dict):
        raise ResolverUnavailable(f"resolver {name} answered a non-object for {key!r}")
    try:
        price = money.parse_price(raw.get("price"))
    except money.MalformedPrice as exc:
        raise ResolverUnavailable(f"resolver {name}, key {key!r}: {exc}") from exc
    currency = raw.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        raise ResolverUnavailable(f"resolver {name}, key {key!r}: bad currency {currency!r}")
    max_quantity = raw.get("max_quantity")
    if max_quantity is not None and (
        isinstance(max_quantity, bool) or not isinstance(max_quantity, int) or max_quantity < 0
    ):
        raise ResolverUnavailable(
            f"resolver {name}, key {key!r}: bad max_quantity {max_quantity!r}"
        )
    return Resolved(
        title=str(raw.get("title") or "")[:255],
        price=price,
        currency=currency.upper(),
        available=bool(raw.get("available")),
        max_quantity=max_quantity,
        seller_id=str(raw.get("seller_id") or "")[:64],
        image=str(raw.get("image") or "")[:500],
        url=str(raw.get("url") or "")[:500],
        attrs=_extract_attrs(raw),
    )


def resolve(purchasable_type: str, keys) -> dict[str, Resolved]:
    """Resolve *keys* of one type in one call. Absent keys are unknown.

    Raises :class:`UnknownPurchasableType` or :class:`ResolverUnavailable`.
    """
    from stapel_core.comm import CommError, call, function_unreachable_reason

    name = resolver_name(purchasable_type)
    unique_keys = list(dict.fromkeys(str(k) for k in keys))
    if not unique_keys:
        return {}
    reason = function_unreachable_reason(name)
    if reason:
        raise ResolverUnavailable(f"resolver {name} for {purchasable_type!r}: {reason}")
    try:
        reply = call(name, {"keys": unique_keys})
    except CommError as exc:
        raise ResolverUnavailable(f"resolver {name} failed: {exc}") from exc
    if not isinstance(reply, dict):
        raise ResolverUnavailable(f"resolver {name} answered {type(reply).__name__}, not an object")
    _validate_reply(name, reply)
    wanted = set(unique_keys)
    return {
        key: _parse(name, key, raw)
        for key, raw in reply.items()
        if key in wanted
    }


def resolve_many(pairs) -> dict[tuple[str, str], Resolved]:
    """Resolve ``[(type, key), ...]`` — one call per type."""
    by_type: dict[str, list[str]] = {}
    for purchasable_type, key in pairs:
        by_type.setdefault(purchasable_type, []).append(key)
    resolved: dict[tuple[str, str], Resolved] = {}
    for purchasable_type, keys in by_type.items():
        for key, item in resolve(purchasable_type, keys).items():
            resolved[(purchasable_type, key)] = item
    return resolved
