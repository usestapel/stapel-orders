"""Every fact orders emits is its own and matches the schema file it ships.

Two gates over one defect class (a fact named like another module's, with
another shape):

- every Action this package emits on its real paths is one it declares in
  ``schemas/emits/`` and validates against that file — so orders never emits
  a foreign fact (``payment.completed`` is stapel-billing's), and a new emit
  without a scenario here fails the coverage assertion;
- no consumer-side schema reaches the action registry: ``on_action(schema=)``
  writes into the one per-name registry the EMITTER is validated against.
"""
import json
import uuid
from pathlib import Path

import jsonschema
import pytest

from stapel_orders.models import Order

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
EMITS = ROOT / "schemas" / "emits"
CONSUMES = ROOT / "schemas" / "consumes"
LINES = "/orders/api/v1/cart/lines/"
CHECKOUT = "/orders/api/v1/checkout/"


def _declared() -> dict[str, dict]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(EMITS.glob("*.json"))
    }


@pytest.fixture
def emitted(monkeypatch):
    """Every (name, payload) handed to the action registry's emit validation."""
    from stapel_core.comm import action_registry

    seen: list[tuple[str, dict]] = []
    original = action_registry.validate

    def _record(name, payload):
        seen.append((name, json.loads(json.dumps(payload))))
        return original(name, payload)

    monkeypatch.setattr(action_registry, "validate", _record)
    return seen


def _checkout(client, catalog, key):
    catalog.put(key, price="12.50")
    resp = client.post(
        LINES, {"purchasable_type": "item", "purchasable_key": key, "quantity": 2}, format="json"
    )
    assert resp.status_code == 200, resp.content
    resp = client.post(CHECKOUT, {
        "idempotency_key": str(uuid.uuid4()),
        "contact": {"name": "Ann Buyer", "phone": "+1 555 0100"},
        "shipping_address": {"line1": "1 Main St", "city": "Springfield", "country": "us"},
    }, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def _walk_every_emit_path(client_for, user, guest, staff, catalog, settings):
    """The real paths that emit: checkout + mock payment (account and guest),
    staff moves, a staff refund, a payment mismatch, a buyer cancel."""
    from stapel_core.comm import emit, mutate_and_emit

    settings.STAPEL_ORDERS = {
        **settings.STAPEL_ORDERS, "PAYMENT": "mock", "CHECKOUT_REQUIRES_ACCOUNT": False,
    }
    buyer, staff_client = client_for(user), client_for(staff)
    paid = _checkout(buyer, catalog, "a")
    guest_paid = _checkout(client_for(guest), catalog, "g")
    for to in ("confirmed", "shipped", "completed"):
        resp = staff_client.post(
            f"/orders/api/v1/orders/{paid['id']}/transition/", {"to": to}, format="json"
        )
        assert resp.status_code == 200, resp.content
    refund = staff_client.post(f"/orders/api/v1/orders/{guest_paid['id']}/refund/", {},
                               format="json")
    assert refund.status_code == 200, refund.content

    settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "none"}
    short = _checkout(buyer, catalog, "b")
    with mutate_and_emit():
        emit("payment.completed", {
            "reference_type": "order", "reference_key": str(short["id"]),
            "amount": "1.00", "currency": "USD", "provider_ref": "short-1",
        })
    cancelled = _checkout(buyer, catalog, "c")
    assert buyer.post(f"/orders/api/v1/orders/{cancelled['id']}/cancel/").status_code == 200
    return paid, guest_paid


def test_every_emitted_fact_is_declared_and_matches_its_file(
    emitted, client_for, user, guest, staff, catalog, settings
):
    _walk_every_emit_path(client_for, user, guest, staff, catalog, settings)
    declared = _declared()
    ours = [(name, payload) for name, payload in emitted if name != "payment.completed"]
    foreign = sorted({name for name, _ in ours if name not in declared})
    assert foreign == [], f"orders emitted facts it does not declare: {foreign}"
    for name, payload in ours:
        jsonschema.Draft202012Validator(declared[name]).validate(payload)
    assert sorted({name for name, _ in ours}) == sorted(declared), (
        "a declared fact no emit path here produces: add its path to the walk"
    )


def test_orders_never_emits_the_payment_modules_fact(
    emitted, client_for, user, guest, staff, catalog, settings
):
    _walk_every_emit_path(client_for, user, guest, staff, catalog, settings)
    # The one payment.completed in the walk is the test's own stand-in for a
    # payment module; orders' paths never produce one.
    assert [p["provider_ref"] for n, p in emitted if n == "payment.completed"] == ["short-1"]


def test_the_mock_payment_fact_is_simulated_and_null_for_a_guest(
    emitted, client_for, user, guest, staff, catalog, settings
):
    paid, guest_paid = _walk_every_emit_path(client_for, user, guest, staff, catalog, settings)
    facts = {p["reference_key"]: p for n, p in emitted if n == "orders.payment.completed"}
    assert facts[str(paid["id"])]["user_id"] == str(user.pk)
    assert facts[str(paid["id"])]["simulated"] is True
    assert facts[str(guest_paid["id"])]["user_id"] is None
    assert Order.objects.get(pk=guest_paid["id"]).payment_status == "refunded"
    refunded = [p for n, p in emitted if n == "orders.order.refunded"]
    assert [p["user_id"] for p in refunded] == [None]


@pytest.mark.parametrize("path", sorted(CONSUMES.glob("*.json")), ids=lambda p: p.stem)
def test_no_consumer_side_schema_reaches_the_action_registry(path):
    """A consumed fact's schema is its owner's; orders registers none."""
    from stapel_core.comm import action_registry

    assert path.stem not in action_registry._schemas, (
        f"a schema for {path.stem!r} is registered in an orders-only process: "
        "a consumer passed schema= to on_action, which makes the copy the "
        "contract the owner's emits are validated against"
    )
