"""Tiny in-process companions behind the checkout seams.

They sit BEHIND the seam: each is a comm Function registered under its own
name with the published contract schema (``schemas/functions/``) attached,
so every payload orders sends them is validated against the contract the
real companions code to, and every reply goes through orders' own parsing.
"""
from __future__ import annotations

import itertools
import json
from decimal import Decimal
from pathlib import Path

from stapel_orders import seams

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas" / "functions"
_tokens = itertools.count(1)


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))


def _register(function_name: str, handler, contract: str) -> None:
    from stapel_core.comm.registry import function_registry

    function_registry.register(function_name, handler)
    function_registry.register_schema(function_name, _schema(contract))


def _unregister(function_name: str) -> None:
    from stapel_core.comm.registry import function_registry

    with function_registry._lock:
        function_registry._providers.pop(function_name, None)
        function_registry._schemas.pop(function_name, None)


class FakeAdjuster:
    """An adjuster whose answer is ``answer(payload) -> reply``; logs payloads."""

    def __init__(self, name: str, answer, order: int = 100):
        self.name = name
        self.function = f"ox_fake.{name}.quote"
        self.answer = answer
        self.order = order
        self.calls: list[dict] = []
        self.fail = False

    def __call__(self, payload):
        self.calls.append(payload)
        if self.fail:
            raise RuntimeError(f"{self.name} is down")
        return self.answer(payload)

    def install(self):
        _register(self.function, self, "adjusters")
        seams.register_adjuster(self.name, {"quote": self.function, "order": self.order})
        return self

    def uninstall(self):
        _unregister(self.function)
        seams.register_adjuster(self.name, None)


def discount_by_code(code: str, percent: int):
    """A promotions-like answer: ``percent`` off the subtotal for ``code``."""

    def answer(payload):
        codes = [str(c).upper() for c in (payload["context"].get("codes") or [])]
        unknown = [c for c in codes if c != code]
        if unknown:
            return {"adjustments": [], "errors": [
                {"code": "error.409.promo_code_unknown", "field": "codes",
                 "params": {"code": unknown[0]}},
            ]}
        if code not in codes:
            return {"adjustments": [], "errors": []}
        amount = (Decimal(payload["subtotal"]) * percent / 100).quantize(Decimal("0.01"))
        return {"adjustments": [{
            "kind": "discount", "code": code, "label": f"{percent}% off",
            "amount": f"-{amount}", "line_index": None, "meta": {"promotion_id": 7},
        }], "errors": []}

    return answer


def flat_shipping(price: str, free_from: str | None = None):
    """A delivery-like answer: a flat rate for ``context.delivery.method``."""

    def answer(payload):
        delivery = payload["context"].get("delivery") or {}
        if not delivery.get("method"):
            return {"adjustments": [], "errors": [
                {"code": "error.400.delivery_method_required", "field": "delivery", "params": {}},
            ]}
        amount = price
        if free_from is not None and Decimal(payload["subtotal"]) >= Decimal(free_from):
            amount = "0.00"
        return {"adjustments": [{
            "kind": "shipping", "code": delivery["method"], "label": "Courier",
            "amount": amount, "line_index": None, "meta": {},
        }], "errors": []}

    return answer


class FakeHook:
    """An inventory-like hook keeping holds per attempt key; logs every call."""

    def __init__(self, name: str, order: int = 100, silent=None):
        self.name = name
        self.order = order
        self.functions = {op: f"ox_fake.{name}.{op}" for op in seams.HOOK_OPERATIONS}
        self.calls: list[tuple[str, dict]] = []
        #: attempt_key -> {"state": reserved|committed|released, "token": str}
        self.holds: dict[str, dict] = {}
        self.refuse_with: dict | None = None
        #: Operations that raise (an unanswered call): e.g. {"release"} keeps
        #: a failed checkout's rows ``releasing`` until the sweep gets an answer.
        self.silent: set[str] = set(silent or ())

    def ops(self, op: str) -> list[dict]:
        return [payload for name, payload in self.calls if name == op]

    def _guard(self, op: str, payload: dict) -> None:
        self.calls.append((op, payload))
        if op in self.silent:
            raise RuntimeError(f"{self.name} {op} is down")

    def reserve(self, payload):
        self._guard("reserve", payload)
        if self.refuse_with is not None:
            return {"ok": False, "token": None, "errors": [self.refuse_with]}
        key = payload["attempt_key"]
        hold = self.holds.get(key)
        if hold is None or hold["state"] == "released":
            hold = {"state": "reserved", "token": f"{self.name}-{next(_tokens)}"}
            self.holds[key] = hold
        return {"ok": True, "token": hold["token"], "errors": []}

    def commit(self, payload):
        self._guard("commit", payload)
        hold = self.holds.get(payload["attempt_key"])
        if hold is not None and hold["state"] == "reserved":
            hold["state"] = "committed"
        return {"ok": True}

    def release(self, payload):
        self._guard("release", payload)
        hold = self.holds.get(payload["attempt_key"])
        if hold is not None:
            hold["state"] = "released"
        return {"ok": True}

    def install(self):
        for op, function in self.functions.items():
            _register(function, getattr(self, op), f"checkout_hooks.{op}")
        seams.register_checkout_hook(self.name, {**self.functions, "order": self.order})
        return self

    def uninstall(self):
        for function in self.functions.values():
            _unregister(function)
        seams.register_checkout_hook(self.name, None)

    def missing_release(self):
        """The hook stays registered but its release Function is gone."""
        _unregister(self.functions["release"])
        return self
