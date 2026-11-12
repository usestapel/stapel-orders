"""Seam with the released stapel-billing: its ``payment.completed`` is the contract.

Billing owns ``payment.completed`` (it ships ``schemas/emits/payment.completed.json``
and emits it). In a host with both modules core's schema autoload registers
billing's file for that name, and every emit of the name is validated
against it. These tests load billing's real file from the installed package
the way the autoload does and walk orders' paths under it.
"""
import json
import uuid
from importlib.util import find_spec
from pathlib import Path

import pytest

from .siblings import requires

pytestmark = pytest.mark.django_db

LINES = "/orders/api/v1/cart/lines/"
CHECKOUT = "/orders/api/v1/checkout/"


def _billing_schema() -> dict:
    root = Path(find_spec("stapel_billing").origin).parent
    return json.loads((root / "schemas" / "emits" / "payment.completed.json").read_text())


@pytest.fixture
def billing_contract(monkeypatch):
    """billing's payment.completed schema registered as a host's autoload does."""
    from stapel_core.comm import action_registry

    schema = _billing_schema()
    monkeypatch.setitem(action_registry._schemas, "payment.completed", schema)
    return schema


def _checkout(client, catalog):
    catalog.put("a", price="9.90")
    client.post(LINES, {"purchasable_type": "item", "purchasable_key": "a", "quantity": 1},
                format="json")
    return client.post(CHECKOUT, {
        "idempotency_key": str(uuid.uuid4()),
        "contact": {"name": "Ann Buyer", "phone": "+1 555 0100"},
        "shipping_address": {"line1": "1 Main St", "city": "Springfield", "country": "us"},
    }, format="json")


@requires("stapel_billing")
def test_mock_checkout_succeeds_next_to_billing(billing_contract, auth_client, catalog, settings):
    settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "mock"}
    resp = _checkout(auth_client, catalog)
    assert resp.status_code == 201, resp.content
    assert resp.json()["payment_status"] == "paid"


@requires("stapel_billing")
def test_guest_mock_checkout_succeeds_next_to_billing(billing_contract, client_for, guest,
                                                      catalog, settings):
    settings.STAPEL_ORDERS = {
        **settings.STAPEL_ORDERS, "PAYMENT": "mock", "CHECKOUT_REQUIRES_ACCOUNT": False,
    }
    resp = _checkout(client_for(guest), catalog)
    assert resp.status_code == 201, resp.content
    assert resp.json()["payment_status"] == "paid"


@requires("stapel_billing")
def test_billings_own_payment_passes_and_is_ignored_by_orders(billing_contract, user):
    """Billing's credit purchase validates (orders registered no rival schema)
    and orders' consumer ignores it: it references no order."""
    from stapel_core.comm import emit, mutate_and_emit

    from stapel_orders.services.orders import apply_payment

    payload = {
        "user_id": str(uuid.uuid4()), "amount_cents": 990, "currency": "USD",
        "transaction_id": str(uuid.uuid4()), "created_at": "2026-11-12T10:00:00+00:00",
        "package": "starter",
    }
    with mutate_and_emit():
        emit("payment.completed", payload, key="t-1")
    assert apply_payment(payload) == "ignored"


@requires("stapel_billing")
def test_the_0_1_0_mock_shape_is_what_billing_refuses(billing_contract):
    """The defect, pinned: the shape the 0.1.0 mock emitted under billing's name."""
    import jsonschema

    old_mock = {"reference_type": "order", "reference_key": "1", "amount": "9.90",
                "currency": "USD", "provider_ref": "mock-1"}
    with pytest.raises(jsonschema.ValidationError, match="'user_id' is a required property"):
        jsonschema.validate(old_mock, billing_contract)
