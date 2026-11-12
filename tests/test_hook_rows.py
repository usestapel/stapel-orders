"""CheckoutHookState rows: the state table, and the staggered replay twin.

A replay of one attempt key that arrives while the first request's order
transaction is open reads the row as ``pending`` (the placing is not
committed yet). It must not write that stale state back: a live order's
row moved to ``pending`` is released by the orphan sweep, and the
companions undo a placed order (stock returned, shipment cancelled, a
promotion use given back).
"""
import datetime
import uuid

import pytest
from django.utils import timezone

from stapel_orders import seams
from stapel_orders.models import CheckoutAttempt, CheckoutHookState, HookState, Order
from stapel_orders.services import hooks as hook_service

from .fake_seams import FakeHook

pytestmark = pytest.mark.django_db

V1 = "/orders/api/v1"
LINES = V1 + "/cart/lines/"
CHECKOUT = V1 + "/checkout/"
LATER = datetime.timedelta(seconds=901)
OUT_OF_STOCK = {"code": "error.409.out_of_stock", "field": None, "params": {}}


@pytest.fixture(autouse=True)
def _reset_seams():
    seams.reset_seams()
    yield
    seams.reset_seams()


@pytest.fixture
def stock():
    hook = FakeHook("stock").install()
    yield hook
    hook.uninstall()


def _fill(client, catalog):
    catalog.put("a", price="40.00")
    resp = client.post(LINES, {"purchasable_type": "item", "purchasable_key": "a",
                               "quantity": 1}, format="json")
    assert resp.status_code == 200, resp.content


def _body(key):
    return {
        "idempotency_key": str(key),
        "contact": {"name": "Ann Buyer", "phone": "+1 555 0100"},
        "shipping_address": {"line1": "1 Main St", "city": "Springfield", "country": "US"},
    }


# -- the state table ----------------------------------------------------------

STATES = list(HookState.values)
EVENTS = sorted(hook_service.ROW_TRANSITIONS)


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("state", STATES)
def test_every_state_event_pair(user, state, event):
    """Each (state, event) pair moves exactly as the table says, or not at all."""
    row = CheckoutHookState.objects.create(
        attempt_key=uuid.uuid4(), name="stock", user_id=str(user.pk), state=state
    )
    sources, target = hook_service.ROW_TRANSITIONS[event]
    moved = hook_service.move(CheckoutHookState.objects.filter(pk=row.pk), event)
    row.refresh_from_db()
    if state in sources:
        assert (moved, row.state) == (1, target)
    else:
        assert (moved, row.state) == (0, state)


def test_the_table_never_moves_a_row_backwards():
    for event, (sources, target) in hook_service.ROW_TRANSITIONS.items():
        if target == HookState.PENDING:
            assert sources == {HookState.RELEASED}, event
        if event in (hook_service.EV_FAIL, hook_service.EV_ORPHAN):
            # checkout_failed / orphaned are never sent for a placed or committed pair
            assert not sources & {HookState.PLACED, HookState.COMMITTED}, event
    undo = [e for e, (sources, _) in hook_service.ROW_TRANSITIONS.items()
            if HookState.COMMITTED in sources]
    assert undo == [hook_service.EV_CANCEL]


# -- the staggered replay twin ------------------------------------------------

def _stale_reads(monkeypatch, state=HookState.PENDING):
    """Force the interleaving: the twin's early reads (the replay check before
    the lock, the hook row) are the ones it makes while the first request's
    order transaction is still open."""
    from stapel_orders.services import checkout as checkout_service

    real_replayed = checkout_service._replayed
    calls = {"n": 0}

    def replayed(user_id, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_replayed(user_id, key)

    monkeypatch.setattr(checkout_service, "_replayed", replayed)
    manager = CheckoutHookState.objects
    real = manager.get_or_create

    def stale(*args, **kwargs):
        row, created = real(*args, **kwargs)
        if not created:
            row.state = state
            row.order = None
        return row, created

    monkeypatch.setattr(manager, "get_or_create", stale)


def test_a_staggered_twin_leaves_the_live_orders_row_alone(
    auth_client, catalog, stock, monkeypatch, django_capture_on_commit_callbacks
):
    _fill(auth_client, catalog)
    key = uuid.uuid4()
    with django_capture_on_commit_callbacks(execute=True):
        first = auth_client.post(CHECKOUT, _body(key), format="json")
    assert first.status_code == 201
    assert CheckoutHookState.objects.get().state == HookState.COMMITTED

    _fill(auth_client, catalog)  # the twin read the cart before the first one emptied it
    _stale_reads(monkeypatch)
    with django_capture_on_commit_callbacks(execute=True):
        twin = auth_client.post(CHECKOUT, _body(key), format="json")
    assert twin.status_code == 200 and twin.json()["id"] == first.json()["id"]
    row = CheckoutHookState.objects.get()
    assert (row.state, row.order_id) == (HookState.COMMITTED, first.json()["id"])

    counts = hook_service.sweep(now=timezone.now() + LATER)
    assert counts["orphans_released"] == 0
    assert stock.ops("release") == []
    assert stock.holds[str(key)]["state"] == "committed"


def test_the_sweep_adopts_a_pending_row_whose_attempt_has_an_order(
    auth_client, catalog, stock, django_capture_on_commit_callbacks
):
    """Defence in depth: a pending row of an attempt key that placed an order
    is that order's, so the sweep places it instead of releasing it."""
    _fill(auth_client, catalog)
    key = uuid.uuid4()
    with django_capture_on_commit_callbacks(execute=True):
        order = auth_client.post(CHECKOUT, _body(key), format="json").json()
    CheckoutHookState.objects.update(state=HookState.PENDING, order=None)
    assert CheckoutAttempt.objects.filter(idempotency_key=key).exists()

    counts = hook_service.sweep(now=timezone.now() + LATER)
    assert counts["adopted"] == 1 and counts["orphans_released"] == 0
    assert stock.ops("release") == []
    row = CheckoutHookState.objects.get()
    assert (row.state, row.order_id) == (HookState.PLACED, order["id"])
    assert hook_service.sweep()["committed"] == 1
    assert CheckoutHookState.objects.get().state == HookState.COMMITTED


def test_a_row_claimed_mid_checkout_refuses_the_order(auth_client, catalog, stock,
                                                      monkeypatch):
    """The sweep (or a failed twin) claimed the release while this checkout
    reserved: placing it anyway would hand over a reservation already let go."""
    _fill(auth_client, catalog)
    real = hook_service.reserve_all

    def reserve_then_claimed(names, request, attempt_key, reserved, tokens):
        real(names, request, attempt_key, reserved, tokens)
        hook_service.move(CheckoutHookState.objects.filter(attempt_key=attempt_key),
                          hook_service.EV_ORPHAN, release_reason="orphaned")

    monkeypatch.setattr(hook_service, "reserve_all", reserve_then_claimed)
    resp = auth_client.post(CHECKOUT, _body(uuid.uuid4()), format="json")
    assert resp.status_code == 503
    assert resp.json()["localizable_error"] == "error.503.checkout_hook_unavailable"
    assert not Order.objects.exists()
    # Here the claim rode the rolled-back checkout transaction, so the
    # in-request release then claimed the row itself: released exactly once.
    assert [p["reason"] for p in stock.ops("release")] == ["checkout_failed"]
    assert CheckoutHookState.objects.get().state == HookState.RELEASED


def test_a_failed_twin_never_releases_a_placed_pair(user, stock):
    """release_now claims only pending rows: checkout_failed is never sent for
    a pair a twin placed."""
    key = uuid.uuid4()
    CheckoutHookState.objects.create(attempt_key=key, name="stock", user_id=str(user.pk),
                                     state=HookState.PLACED)
    hook_service.release_now(key, ["stock"], {})
    assert stock.ops("release") == []
    assert CheckoutHookState.objects.get().state == HookState.PLACED


# -- refused before the reserve step (D3) -------------------------------------

def test_a_checkout_refused_before_reserve_leaves_no_orphan(auth_client, catalog, stock):
    """No reserve was sent, so no row is left for the sweep to release."""
    _fill(auth_client, catalog)
    resp = auth_client.post(CHECKOUT, {**_body(uuid.uuid4()), "expected_total": "1.00"},
                            format="json")
    assert resp.status_code == 409
    assert resp.json()["localizable_error"] == "error.409.total_changed"
    assert stock.ops("reserve") == []
    assert list(CheckoutHookState.objects.values_list("state", "release_reason")) == [
        (HookState.RELEASED, "unsent")
    ]
    counts = hook_service.sweep(now=timezone.now() + LATER)
    assert counts["orphans_released"] == 0
    assert stock.calls == []


def test_the_same_key_reserves_after_a_refusal_before_reserve(
    auth_client, catalog, stock, django_capture_on_commit_callbacks
):
    _fill(auth_client, catalog)
    key = uuid.uuid4()
    refused = auth_client.post(CHECKOUT, {**_body(key), "expected_total": "1.00"},
                               format="json")
    assert refused.status_code == 409
    with django_capture_on_commit_callbacks(execute=True):
        placed = auth_client.post(CHECKOUT, {**_body(key), "expected_total": "40.00"},
                                  format="json")
    assert placed.status_code == 201, placed.content
    assert len(stock.ops("reserve")) == 1 and stock.ops("release") == []
    assert CheckoutHookState.objects.get().state == HookState.COMMITTED


# -- a twin that reads the cart after the first submit committed (D8) --------

def test_a_twin_that_reads_an_emptied_cart_gets_the_order(
    auth_client, catalog, stock, monkeypatch, django_capture_on_commit_callbacks
):
    """The twin's replay check ran before the first submit committed, its cart
    read after: refusing ``cart_empty`` there would lose the order it placed."""
    from stapel_orders.services import checkout as checkout_service

    _fill(auth_client, catalog)
    key = uuid.uuid4()
    with django_capture_on_commit_callbacks(execute=True):
        first = auth_client.post(CHECKOUT, _body(key), format="json")
    assert first.status_code == 201
    real_replayed = checkout_service._replayed
    calls = {"n": 0}

    def stale_first(user_id, attempt_key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_replayed(user_id, attempt_key)

    monkeypatch.setattr(checkout_service, "_replayed", stale_first)
    twin = auth_client.post(CHECKOUT, _body(key), format="json")
    assert twin.status_code == 200, twin.content
    assert twin.json()["id"] == first.json()["id"]
    assert Order.objects.count() == 1
    assert len(stock.ops("reserve")) == 1 and stock.ops("release") == []


def test_an_empty_cart_without_a_twin_is_still_refused(auth_client, stock):
    resp = auth_client.post(CHECKOUT, _body(uuid.uuid4()), format="json")
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.cart_empty"
    assert not CheckoutHookState.objects.filter(state=HookState.PENDING).exists()


# -- the releasing path (an unanswered release after a failed checkout) -------

def test_an_unanswered_release_stays_releasing_and_blocks_a_retry_until_answered(
    auth_client, catalog, monkeypatch, django_capture_on_commit_callbacks
):
    """Harness lever: ``FakeHook(..., silent={"release"})`` (or
    ``missing_release()``) leaves the companion's release unanswered."""
    hook = FakeHook("stock", silent={"release"}).install()
    try:
        _fill(auth_client, catalog)
        key = uuid.uuid4()
        hook.refuse_with = OUT_OF_STOCK
        refused = auth_client.post(CHECKOUT, _body(key), format="json")
        assert refused.status_code == 409
        row = CheckoutHookState.objects.get()
        assert (row.state, row.release_reason) == (HookState.RELEASING, "checkout_failed")

        retry = auth_client.post(CHECKOUT, _body(key), format="json")
        assert retry.status_code == 503
        assert retry.json()["localizable_error"] == "error.503.checkout_hook_unavailable"
        assert len(hook.ops("reserve")) == 1

        hook.silent = set()
        assert hook_service.sweep()["released"] == 1
        assert CheckoutHookState.objects.get().state == HookState.RELEASED
        hook.refuse_with = None
        with django_capture_on_commit_callbacks(execute=True):
            placed = auth_client.post(CHECKOUT, _body(key), format="json")
        assert placed.status_code == 201, placed.content
        assert [p["reason"] for p in hook.ops("release")] == ["checkout_failed"] * 3
        assert CheckoutHookState.objects.get().state == HookState.COMMITTED
    finally:
        hook.uninstall()


def test_a_missing_release_function_is_unanswered_too(auth_client, catalog):
    hook = FakeHook("stock").install().missing_release()
    try:
        _fill(auth_client, catalog)
        hook.refuse_with = OUT_OF_STOCK
        assert auth_client.post(CHECKOUT, _body(uuid.uuid4()), format="json").status_code == 409
        assert CheckoutHookState.objects.get().state == HookState.RELEASING
        assert hook_service.sweep()["unanswered"] == 1
    finally:
        hook.uninstall()
