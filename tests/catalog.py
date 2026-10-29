"""A fake product owner answering the purchasables contract for type ``item``."""
from __future__ import annotations

from stapel_orders._codegen_settings import TEST_RESOLVER


class FakeCatalog:
    """In-process resolver with a call log; tests edit ``items`` freely."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.fail = False

    def put(self, key, price="10.00", currency="USD", available=True,
            max_quantity=None, title=None, seller_id="seller-1"):
        self.items[str(key)] = {
            "title": title or f"Item {key}",
            "price": price,
            "currency": currency,
            "available": available,
            "max_quantity": max_quantity,
            "seller_id": seller_id,
            "image": f"https://cdn.example/{key}.jpg",
            "url": f"/items/{key}",
        }
        return self.items[str(key)]

    def __call__(self, payload):
        keys = list(payload["keys"])
        self.calls.append(keys)
        if self.fail:
            raise RuntimeError("catalog is down")
        return {k: dict(self.items[k]) for k in keys if k in self.items}

    def install(self):
        from stapel_core.comm.registry import function_registry

        function_registry.register(TEST_RESOLVER, self)

    def uninstall(self):
        from stapel_core.comm.registry import function_registry

        with function_registry._lock:
            function_registry._providers.pop(TEST_RESOLVER, None)
            function_registry._schemas.pop(TEST_RESOLVER, None)
