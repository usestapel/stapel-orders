"""Checkout seams: price adjusters and checkout hooks (comm Functions by name).

Commerce companions (promotions, delivery, inventory) plug in here without
orders importing them. Both registries ship EMPTY and merge built-ins <-
settings <- runtime, ``None`` removing an entry::

    STAPEL_ORDERS = {
        "ADJUSTERS": {"promotions": {"quote": "promotions.quote", "order": 100}},
        "CHECKOUT_HOOKS": {"inventory": {"reserve": "inventory.reserve",
                                         "commit": "inventory.commit",
                                         "release": "inventory.release"}},
    }

**Adjusters** answer ``schemas/functions/adjusters.json``: a request with
the cart (currency, subtotal, lines with ``attrs``), the adjustments earlier
adjusters answered and the client's opaque ``context`` → ``{adjustments,
errors}``. ``total = max(0, subtotal + sum(amount))``. Any error refuses a
checkout; the quote endpoint shows errors without failing. An adjuster that
does not answer, or answers outside the contract, is an error too
(``error.503.adjuster_unavailable``): a price nobody could compute is never
silently dropped.

**Checkout hooks** answer ``schemas/functions/checkout_hooks.*.json``:
``reserve`` before the order row is written, ``commit`` once the order is
placed, ``release`` on a failed checkout, on cancel and for orphans. The
state machine is ``services/hooks.py``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from . import money

logger = logging.getLogger(__name__)

DEFAULT_ORDER = 100
ADJUSTMENT_KINDS = ("discount", "shipping", "tax", "fee")
CODE_MAX_LENGTH = 64
LABEL_MAX_LENGTH = 255
TOKEN_MAX_LENGTH = 255
META_MAX_BYTES = 4096
HOOK_OPERATIONS = ("reserve", "commit", "release")

BUILTIN_ADJUSTERS: dict[str, Optional[dict]] = {}
BUILTIN_CHECKOUT_HOOKS: dict[str, Optional[dict]] = {}

_runtime_adjusters: dict[str, Optional[dict]] = {}
_runtime_hooks: dict[str, Optional[dict]] = {}

_SCHEMAS = Path(__file__).resolve().parent / "schemas" / "functions"
_result_schemas: dict[str, dict] = {}

_STATUS_IN_KEY = re.compile(r"^error\.(\d{3})\.")


class SeamUnavailable(Exception):
    """A seam Function was unreachable or answered outside its contract."""


# ── Registries ────────────────────────────────────────────────────────────


def register_adjuster(name: str, entry: Optional[dict]) -> None:
    """Register/override an adjuster at runtime (``None`` removes it)."""
    _runtime_adjusters[name] = entry


def register_checkout_hook(name: str, entry: Optional[dict]) -> None:
    """Register/override a checkout hook at runtime (``None`` removes it)."""
    _runtime_hooks[name] = entry


def reset_seams() -> None:
    """Tests only: drop runtime adjusters and hooks."""
    _runtime_adjusters.clear()
    _runtime_hooks.clear()


def _merged(builtins: dict, configured, runtime: dict) -> dict:
    merged: dict[str, Optional[dict]] = dict(builtins)
    if isinstance(configured, dict):
        merged.update(configured)
    merged.update(runtime)
    return {name: entry for name, entry in merged.items() if entry is not None}


def _sort_key(item):
    name, entry = item
    order = entry.get("order", DEFAULT_ORDER) if isinstance(entry, dict) else DEFAULT_ORDER
    if isinstance(order, bool) or not isinstance(order, int):
        order = DEFAULT_ORDER
    return (order, name)


def get_adjusters() -> list[tuple[str, dict]]:
    """Effective adjusters ``[(name, entry)]`` in application order."""
    from .conf import orders_settings

    merged = _merged(BUILTIN_ADJUSTERS, orders_settings.ADJUSTERS, _runtime_adjusters)
    return sorted(merged.items(), key=_sort_key)


def get_checkout_hooks() -> list[tuple[str, dict]]:
    """Effective checkout hooks ``[(name, entry)]`` in reserve order."""
    from .conf import orders_settings

    merged = _merged(BUILTIN_CHECKOUT_HOOKS, orders_settings.CHECKOUT_HOOKS, _runtime_hooks)
    return sorted(merged.items(), key=_sort_key)


def adjuster_entry_problem(entry) -> str | None:
    """Why an ADJUSTERS entry is malformed, or None."""
    if not isinstance(entry, dict) or not isinstance(entry.get("quote"), str) or not entry["quote"]:
        return "must be {'quote': '<comm function>', 'order': <int>}"
    order = entry.get("order", DEFAULT_ORDER)
    if isinstance(order, bool) or not isinstance(order, int):
        return "'order' must be an int"
    return None


def hook_entry_problem(entry) -> str | None:
    """Why a CHECKOUT_HOOKS entry is malformed, or None."""
    if not isinstance(entry, dict) or not all(
        isinstance(entry.get(op), str) and entry[op] for op in HOOK_OPERATIONS
    ):
        return "must be {'reserve', 'commit', 'release': '<comm function>'}"
    order = entry.get("order", DEFAULT_ORDER)
    if isinstance(order, bool) or not isinstance(order, int):
        return "'order' must be an int"
    return None


# ── Calls ─────────────────────────────────────────────────────────────────


def _result_schema(contract: str) -> dict:
    if contract not in _result_schemas:
        full = json.loads((_SCHEMAS / f"{contract}.json").read_text(encoding="utf-8"))
        _result_schemas[contract] = {"$defs": full["$defs"], "$ref": "#/$defs/result"}
    return _result_schemas[contract]


def call_seam(function: str, payload: dict, contract: str) -> dict:
    """Call a seam Function; its reply validated against *contract*'s result.

    Raises :class:`SeamUnavailable` — never a guess — when the Function is
    unreachable, fails, or answers outside the contract.
    """
    from stapel_core.comm import CommError, call, function_unreachable_reason
    from stapel_core.comm.config import validation_enabled

    reason = function_unreachable_reason(function)
    if reason:
        raise SeamUnavailable(f"{function}: {reason}")
    try:
        reply = call(function, payload)
    except CommError as exc:
        raise SeamUnavailable(f"{function} failed: {exc}") from exc
    if not isinstance(reply, dict):
        raise SeamUnavailable(f"{function} answered {type(reply).__name__}, not an object")
    if validation_enabled():
        import jsonschema

        try:
            jsonschema.validate(reply, _result_schema(contract))
        except jsonschema.ValidationError as exc:
            raise SeamUnavailable(
                f"{function} answered outside the {contract} contract: {exc.message}"
            ) from exc
    return reply


# ── Adjusters: data ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Adjustment:
    """One adjustment as orders keeps it (amount signed, cart currency)."""

    adjuster: str
    kind: str
    code: str
    label: str
    amount: Decimal
    line_index: Optional[int]
    meta: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {
            "kind": self.kind,
            "code": self.code,
            "label": self.label,
            "amount": money.to_wire(self.amount),
            "line_index": self.line_index,
            "meta": dict(self.meta),
            "adjuster": self.adjuster,
        }


@dataclass(frozen=True)
class QuoteError:
    """One refusal an adjuster answered (or orders recorded for it)."""

    code: str
    field: Optional[str]
    params: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {"code": self.code, "field": self.field, "params": dict(self.params)}

    def http_status(self) -> int:
        """``error.<4xx|503>.<slug>`` keeps its status; anything else is 409."""
        match = _STATUS_IN_KEY.match(self.code)
        if match:
            status = int(match.group(1))
            if 400 <= status < 500 or status == 503:
                return status
        return 409


@dataclass
class Quote:
    """The adjusted price of a cart: what checkout snapshots and quote shows."""

    currency: str
    subtotal: Decimal
    adjustments: list[Adjustment] = field(default_factory=list)
    errors: list[QuoteError] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return money.adjusted_total(self.subtotal, [a.amount for a in self.adjustments])


def is_guest(user) -> bool:
    """A stapel-auth anonymous account (core's predicate)."""
    from types import SimpleNamespace

    from stapel_core.django.api.permissions import IsNotAnonymousUser

    return not IsNotAnonymousUser().has_permission(SimpleNamespace(user=user), None)


def build_request(*, currency: str, user_id: Optional[str], lines, context: dict,
                  actor_id: Optional[str] = None) -> dict:
    """The adjuster request (without ``adjustments_so_far``) for cart *lines*.

    ``user_id`` is null for a guest; ``actor_id`` is the acting account id,
    guest or not (omitted only when a caller has none). ``line_index``
    everywhere is the position in this ``lines`` list, which is the order of
    the order lines checkout writes.
    """
    subtotal = money.sum_lines(line.line_total for line in lines)
    request = {
        "currency": currency,
        "subtotal": money.to_wire(subtotal),
        "user_id": user_id,
        "lines": [
            {
                "index": index,
                "purchasable_type": line.purchasable_type,
                "purchasable_key": line.purchasable_key,
                "quantity": line.quantity,
                "unit_price": money.to_wire(line.unit_price),
                "line_total": money.to_wire(line.line_total),
                "attrs": dict(line.attrs or {}),
            }
            for index, line in enumerate(lines)
        ],
        "context": context,
    }
    if actor_id is not None:
        request["actor_id"] = actor_id
    return request


def _parse_adjustment(name: str, raw, line_count: int) -> Adjustment:
    if not isinstance(raw, dict):
        raise SeamUnavailable(f"adjuster {name}: adjustment is not an object")
    kind = raw.get("kind")
    if kind not in ADJUSTMENT_KINDS:
        raise SeamUnavailable(f"adjuster {name}: unknown kind {kind!r}")
    try:
        amount = money.parse_adjustment(raw.get("amount"))
    except money.MalformedPrice as exc:
        raise SeamUnavailable(f"adjuster {name}: {exc}") from exc
    code = raw.get("code") or ""
    if not isinstance(code, str) or len(code) > CODE_MAX_LENGTH:
        raise SeamUnavailable(f"adjuster {name}: bad code {code!r}")
    label = raw.get("label") or ""
    if not isinstance(label, str):
        raise SeamUnavailable(f"adjuster {name}: bad label {label!r}")
    line_index = raw.get("line_index")
    if line_index is not None and (
        isinstance(line_index, bool)
        or not isinstance(line_index, int)
        or not 0 <= line_index < line_count
    ):
        raise SeamUnavailable(f"adjuster {name}: line_index {line_index!r} outside the lines")
    meta = raw.get("meta") or {}
    if not isinstance(meta, dict) or len(json.dumps(meta, default=str)) > META_MAX_BYTES:
        raise SeamUnavailable(f"adjuster {name}: meta is not an object under {META_MAX_BYTES} bytes")
    return Adjustment(
        adjuster=name, kind=kind, code=code, label=label[:LABEL_MAX_LENGTH],
        amount=amount, line_index=line_index, meta=meta,
    )


def _parse_error(raw) -> QuoteError:
    if not isinstance(raw, dict) or not isinstance(raw.get("code"), str) or not raw["code"]:
        raise SeamUnavailable("error entry without a code")
    fld = raw.get("field")
    params = raw.get("params") or {}
    if fld is not None and not isinstance(fld, str):
        raise SeamUnavailable(f"error {raw['code']}: bad field {fld!r}")
    if not isinstance(params, dict):
        raise SeamUnavailable(f"error {raw['code']}: params is not an object")
    return QuoteError(code=raw["code"], field=fld, params=params)


def parse_errors(raw_errors) -> list[QuoteError]:
    if raw_errors is None:
        return []
    if not isinstance(raw_errors, list):
        raise SeamUnavailable("errors is not a list")
    return [_parse_error(e) for e in raw_errors]


def quote(request: dict) -> Quote:
    """Run every adjuster in order over *request*; never raises.

    Each adjuster sees the adjustments answered before it. One that is
    unreachable or malformed contributes an ``error.503.adjuster_unavailable``
    error and no adjustment.
    """
    from .conf import orders_settings
    from .errors import ERR_503_ADJUSTER_UNAVAILABLE

    max_adjustments = int(orders_settings.MAX_ADJUSTMENTS)
    result = Quote(currency=request["currency"], subtotal=Decimal(request["subtotal"]))
    line_count = len(request["lines"])
    for name, entry in get_adjusters():
        payload = {
            **request,
            "adjustments_so_far": [a.to_wire() for a in result.adjustments],
        }
        try:
            reply = call_seam(str(entry["quote"]), payload, "adjusters")
            raw_adjustments = reply.get("adjustments") or []
            if not isinstance(raw_adjustments, list):
                raise SeamUnavailable(f"adjuster {name}: adjustments is not a list")
            adjustments = [_parse_adjustment(name, raw, line_count) for raw in raw_adjustments]
            errors = parse_errors(reply.get("errors"))
            if len(result.adjustments) + len(adjustments) > max_adjustments:
                raise SeamUnavailable(
                    f"adjuster {name}: more than MAX_ADJUSTMENTS={max_adjustments} adjustments"
                )
        except (SeamUnavailable, KeyError, TypeError) as exc:
            logger.warning("orders: adjuster %s unavailable: %s", name, exc)
            result.errors.append(
                QuoteError(code=ERR_503_ADJUSTER_UNAVAILABLE, field=None, params={"adjuster": name})
            )
            continue
        result.adjustments.extend(adjustments)
        result.errors.extend(errors)
    return result


__all__ = [
    "Adjustment",
    "Quote",
    "QuoteError",
    "SeamUnavailable",
    "build_request",
    "call_seam",
    "get_adjusters",
    "get_checkout_hooks",
    "quote",
    "register_adjuster",
    "register_checkout_hook",
    "reset_seams",
]
