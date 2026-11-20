"""Every response body the committed schema declares is the body the views send.

The operation list is DERIVED from ``docs/schema.json``: an operation with a
declared JSON 2xx body and no recipe here fails loudly. Every body is
validated against its declared schema (OAS ``nullable`` honoured), and each
read is driven in a populated and an empty state.
"""
import copy
import json
import uuid
from pathlib import Path

import jsonschema
import pytest

from .factories import make_order

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())
V1 = "/orders/api/v1"

pytestmark = pytest.mark.django_db


def _json_schema(node):
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(body_schema):
    root = copy.deepcopy(body_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            for code, response in op.get("responses", {}).items():
                body = response.get("content", {}).get("application/json", {}).get("schema")
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


def _url(path, **params):
    for name, value in params.items():
        path = path.replace("{%s}" % name, str(value))
    assert "{" not in path, f"unfilled path parameter in {path}"
    return path


def _checkout_body(key=None):
    return {
        "idempotency_key": str(key or uuid.uuid4()),
        "contact": {"name": "Ann", "phone": "+1 555 0100"},
        "shipping_address": {"line1": "1 Main St", "city": "Springfield", "country": "US"},
    }


def _filled_cart(client, catalog):
    catalog.put("a", price="4.50")
    catalog.put("b", price="1.00", seller_id=None)
    client.post(f"{V1}/cart/lines/", {"purchasable_type": "item", "purchasable_key": "a"}, format="json")
    client.post(f"{V1}/cart/lines/", {"purchasable_type": "item", "purchasable_key": "b"}, format="json")


# (method, path, code) -> recipe(env) returning (populated responses, empty responses)
def r_cart_get(env):
    empty = env.client.get(_url(V1 + "/cart/"))
    _filled_cart(env.client, env.catalog)
    return [env.client.get(V1 + "/cart/")], [empty]


def r_cart_delete(env):
    """Clearing answers the emptied cart: both states are 'empty' bodies."""
    never_had_one = env.client.delete(V1 + "/cart/")
    _filled_cart(env.client, env.catalog)
    return [], [never_had_one, env.client.delete(V1 + "/cart/")]


def r_lines_post(env):
    env.catalog.put("a")
    resp = env.client.post(f"{V1}/cart/lines/", {"purchasable_type": "item", "purchasable_key": "a"}, format="json")
    return [resp], []


def r_line_patch(env):
    _filled_cart(env.client, env.catalog)
    line_id = env.client.get(V1 + "/cart/").json()["lines"][0]["id"]
    keep = env.client.patch(_url(V1 + "/cart/lines/{line_id}/", line_id=line_id), {"quantity": 3}, format="json")
    gone = env.client.patch(_url(V1 + "/cart/lines/{line_id}/", line_id=line_id), {"quantity": 0}, format="json")
    return [keep], [gone]


def r_line_delete(env):
    _filled_cart(env.client, env.catalog)
    line_id = env.client.get(V1 + "/cart/").json()["lines"][0]["id"]
    return [env.client.delete(_url(V1 + "/cart/lines/{line_id}/", line_id=line_id))], []


def r_cart_quote(env):
    """Populated: a discount, a shipping line and an error, from the fakes."""
    from .fake_seams import FakeAdjuster, discount_by_code, flat_shipping

    empty = env.client.post(V1 + "/cart/quote/", {}, format="json")
    _filled_cart(env.client, env.catalog)
    fakes = [
        FakeAdjuster("promo", discount_by_code("SAVE10", 10), order=100).install(),
        FakeAdjuster("ship", flat_shipping("5.00"), order=200).install(),
    ]
    try:
        priced = env.client.post(V1 + "/cart/quote/", {"context": {
            "codes": ["save10"], "delivery": {"method": "courier"},
        }}, format="json")
        refused = env.client.post(V1 + "/cart/quote/", {"context": {"codes": ["nope"]}},
                                  format="json")
    finally:
        for fake in fakes:
            fake.uninstall()
    assert priced.json()["adjustments"] and refused.json()["errors"]
    return [priced, refused], [empty]


def r_checkout(code):
    def recipe(env):
        _filled_cart(env.client, env.catalog)
        key = uuid.uuid4()
        first = env.client.post(V1 + "/checkout/", _checkout_body(key), format="json")
        if code == 201:
            return [first], []
        return [env.client.post(V1 + "/checkout/", _checkout_body(key), format="json")], []
    return recipe


def r_orders_list(env):
    empty = env.client.get(V1 + "/orders/")
    for _ in range(3):
        make_order(env.user, lines=2)
    return [env.client.get(V1 + "/orders/?limit=2")], [empty]


def r_order_get(env):
    order = make_order(env.user)
    return [env.client.get(_url(V1 + "/orders/{order_id}/", order_id=order.pk))], []


def r_order_cancel(env):
    order = make_order(env.user)
    return [env.client.post(_url(V1 + "/orders/{order_id}/cancel/", order_id=order.pk))], []


def r_order_transition(env):
    order = make_order(env.user)
    staff = env.client_for(env.staff)
    return [staff.post(_url(V1 + "/orders/{order_id}/transition/", order_id=order.pk),
                       {"to": "confirmed"}, format="json")], []


def r_order_refund(env):
    order = make_order(env.user, payment_status="paid")
    staff = env.client_for(env.staff)
    return [staff.post(_url(V1 + "/orders/{order_id}/refund/", order_id=order.pk),
                       {"reason": "returned"}, format="json")], []


RECIPES = {
    ("GET", V1 + "/cart/", 200): r_cart_get,
    ("DELETE", V1 + "/cart/", 200): r_cart_delete,
    ("POST", V1 + "/cart/lines/", 200): r_lines_post,
    ("PATCH", V1 + "/cart/lines/{line_id}/", 200): r_line_patch,
    ("DELETE", V1 + "/cart/lines/{line_id}/", 200): r_line_delete,
    ("POST", V1 + "/cart/quote/", 200): r_cart_quote,
    ("POST", V1 + "/checkout/", 201): r_checkout(201),
    ("POST", V1 + "/checkout/", 200): r_checkout(200),
    ("GET", V1 + "/orders/", 200): r_orders_list,
    ("GET", V1 + "/orders/{order_id}/", 200): r_order_get,
    ("POST", V1 + "/orders/{order_id}/cancel/", 200): r_order_cancel,
    ("POST", V1 + "/orders/{order_id}/transition/", 200): r_order_transition,
    ("POST", V1 + "/orders/{order_id}/refund/", 200): r_order_refund,
}


class Env:
    def __init__(self, client, client_for, user, staff, catalog):
        self.client, self.client_for = client, client_for
        self.user, self.staff, self.catalog = user, staff, catalog


def test_every_declared_operation_has_a_recipe():
    declared = {(m, p, c) for m, p, c, _ in OPERATIONS}
    assert declared == set(RECIPES), {
        "missing": sorted(declared - set(RECIPES)),
        "stale": sorted(set(RECIPES) - declared),
    }


@pytest.mark.parametrize("method,path,code,body_schema", OPERATIONS,
                         ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS])
def test_wire_matches_the_declared_body(method, path, code, body_schema,
                                         auth_client, client_for, user, staff, catalog):
    populated, empty = RECIPES[(method, path, code)](Env(auth_client, client_for, user, staff, catalog))
    validator = _validator(body_schema)
    for resp in populated + empty:
        assert resp.status_code == code, (method, path, resp.status_code, resp.content)
        errors = sorted(validator.iter_errors(resp.json()), key=str)
        assert not errors, [e.message for e in errors[:5]]
    for resp in populated:
        data = resp.json()
        for key in ("lines", "items"):
            if key in data:
                assert data[key], f"{method} {path}: populated '{key}' came back empty"


def test_the_gate_is_not_blind(auth_client, catalog):
    """Inversion: a body validated against a wrong schema must fail."""
    catalog.put("a")
    auth_client.post(f"{V1}/cart/lines/", {"purchasable_type": "item", "purchasable_key": "a"}, format="json")
    body = auth_client.get(V1 + "/cart/").json()
    assert list(jsonschema.Draft202012Validator({"type": "string"}).iter_errors(body))
    broken = dict(body, total=12.5)
    cart_schema = SCHEMA["paths"][V1 + "/cart/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert list(_validator(cart_schema).iter_errors(broken))


# ─────────────────────────────────────────────────────────────────────────────
# Every caller state
# ─────────────────────────────────────────────────────────────────────────────

#: The document a native client generates from (contract profile 2).
SCHEMA_V2 = json.loads((Path(__file__).resolve().parent.parent / "docs" / "schema.v2.json").read_text())


def _state_client(user=None):
    from rest_framework.test import APIClient

    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def _registered_stranger():
    from django.contrib.auth import get_user_model

    tag = uuid.uuid4().hex[:10]
    return _state_client(
        get_user_model().objects.create(username=f"wire_state_{tag}", email=f"state-{tag}@example.com")
    )


def _guest_session():
    from stapel_core.django.users.models import User

    return _state_client(User.create_anonymous_user())


def _production_error_envelope():
    """Refusals leave through core's exception handler in every deployment
    (stapel_core.django.settings.REST_FRAMEWORK); a harness without it would
    judge DRF's bare ``{"detail": ...}`` instead of the declared envelope."""
    from django.conf import settings
    from django.test import override_settings

    current = dict(getattr(settings, "REST_FRAMEWORK", None) or {})
    current["EXCEPTION_HANDLER"] = "stapel_core.django.api.errors.stapel_exception_handler"
    return override_settings(REST_FRAMEWORK=current)


#: The caller states every recipe request is replayed as. The recipe's own
#: caller is the fourth (the owner or the operator the operation needs).
CALLER_STATES = {"anonymous": _state_client, "guest": _guest_session, "member": _registered_stranger}
_STATE_OPERATIONS = sorted({(m, p, c) for m, p, c, _s in OPERATIONS})


@pytest.mark.parametrize("method,path,code", _STATE_OPERATIONS, ids=[f"{m} {p} {c}" for m, p, c in _STATE_OPERATIONS])
def test_every_caller_state_is_answered_as_declared(method, path, code, auth_client, client_for, user, staff, catalog):
    """The recipe's requests, sent again with no session, as a guest and as a stranger.

    A 2xx must be declared and its body must validate against schema.json and
    schema.v2.json; a refusal is a 4xx; an operation open to callers without
    a session does not refuse them 401.
    """
    from stapel_tools.contract_gates import AuthStateReplay

    # The first document's `security` is the claim: profile 2 derives it from the
    # running permission stack, profile 1 does not.
    replay = AuthStateReplay({"schema.v2.json": SCHEMA_V2, "schema.json": SCHEMA}, CALLER_STATES)
    with _production_error_envelope(), replay.watching(method, path):
        RECIPES[(method, path, code)](Env(auth_client, client_for, user, staff, catalog))
    assert replay.replayed >= len(CALLER_STATES), (
        f"{method} {path}: the recipe never reached the operation, nothing was replayed"
    )
    assert not replay.findings, "\n".join(str(f) for f in replay.findings)
