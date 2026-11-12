"""Checkout hooks under real row locks and real commits (PostgreSQL only).

Armed by ``STAPEL_ORDERS_TEST_PG`` (see ``_codegen_settings``); without it
the module is skipped with a ``NOT ARMED`` reason, because SQLite drops
``FOR UPDATE`` and cannot show what these tests show:

- two threads submit one attempt key at once: one order, one reservation,
  no release, the twin answers the replay;
- a checkout that dies after ``reserve`` leaves a committed ``pending`` row
  (the order transaction really rolled back) that the sweep releases;
- a STAGGERED twin (it reads the row while the first order transaction is
  open, then waits on the cart lock) leaves the live order's row placed or
  committed, and the sweep releases nothing.
"""
import datetime
import threading
import time
import uuid

import pytest
from django.db import connection
from django.utils import timezone

from stapel_orders import seams
from stapel_orders._codegen_settings import TEST_PG_ENV
from stapel_orders.models import CheckoutHookState, HookState, Order
from stapel_orders.services import cart as cart_service
from stapel_orders.services import checkout as checkout_service
from stapel_orders.services import hooks as hook_service

from .fake_seams import FakeHook

pytestmark = [pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _armed():
    if connection.vendor != "postgresql":
        pytest.skip(f"NOT ARMED: set {TEST_PG_ENV}=postgresql://... to run on PostgreSQL")
    seams.reset_seams()
    yield
    seams.reset_seams()


def _contact():
    return checkout_service.Contact(name="Ann", phone="1")


def _address():
    return checkout_service.Address(line1="l", city="c", country="US")


class SlowHook(FakeHook):
    """Holds the cart lock long enough for the twin to queue behind it."""

    def reserve(self, payload):
        time.sleep(0.5)
        return super().reserve(payload)


def test_two_threads_one_attempt_key(user, catalog):
    catalog.put("a", price="10.00")
    cart_service.add_line(str(user.pk), "item", "a", 2)
    hook = SlowHook("stock").install()
    key = uuid.uuid4()
    barrier = threading.Barrier(2)
    results, failures = [], []

    def submit():
        try:
            barrier.wait()
            results.append(checkout_service.checkout(
                user, idempotency_key=key, contact=_contact(), address=_address(),
            ))
        except Exception as exc:  # surfaced below
            failures.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=submit) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        hook.uninstall()
    assert failures == []
    assert sorted(created for _, created in results) == [False, True]
    assert len({order.pk for order, _ in results}) == 1
    assert Order.objects.count() == 1
    assert len(hook.ops("reserve")) == 1
    assert hook.ops("release") == []
    row = CheckoutHookState.objects.get()
    assert row.state == HookState.COMMITTED  # commit ran on the real on_commit
    assert hook.holds[str(key)]["state"] == "committed"


def test_crash_after_reserve_is_released_by_the_sweep(user, catalog, monkeypatch, settings):
    from stapel_orders.payment import PaymentProvider, register_payment_provider

    class Crashing(PaymentProvider):
        def start(self, order, emit):
            raise RuntimeError("worker killed")

    register_payment_provider("crash", Crashing)
    settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "crash"}
    catalog.put("a", price="10.00")
    cart_service.add_line(str(user.pk), "item", "a", 1)
    hook = FakeHook("stock").install()
    monkeypatch.setattr(hook_service, "release_now", lambda *a, **k: None)
    key = uuid.uuid4()
    try:
        with pytest.raises(RuntimeError):
            checkout_service.checkout(user, idempotency_key=key, contact=_contact(),
                                      address=_address())
        assert Order.objects.count() == 0
        assert cart_service.line_count(str(user.pk)) == 1  # the cart write rolled back
        row = CheckoutHookState.objects.get()
        assert (row.state, row.order_id) == (HookState.PENDING, None)
        assert hook.holds[str(key)]["state"] == "reserved"

        assert hook_service.sweep()["orphans_released"] == 0
        later = timezone.now() + datetime.timedelta(seconds=901)
        assert hook_service.sweep(now=later)["orphans_released"] == 1
    finally:
        hook.uninstall()
    assert [p["reason"] for p in hook.ops("release")] == ["orphaned"]
    assert hook.holds[str(key)]["state"] == "released"
    assert CheckoutHookState.objects.get().state == HookState.RELEASED


def test_a_staggered_twin_does_not_orphan_the_live_order(user, catalog, settings):
    from stapel_orders.payment import PaymentProvider, register_payment_provider

    class SlowPayment(PaymentProvider):
        """Keeps the order transaction open after the rows were placed."""

        def start(self, order, emit):
            time.sleep(1.0)
            return None

    register_payment_provider("slow", SlowPayment)
    settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "slow"}
    catalog.put("a", price="10.00")
    cart_service.add_line(str(user.pk), "item", "a", 1)
    hook = FakeHook("stock").install()
    key = uuid.uuid4()
    results, failures = [], []

    def submit(delay):
        try:
            time.sleep(delay)
            results.append(checkout_service.checkout(
                user, idempotency_key=key, contact=_contact(), address=_address(),
            ))
        except Exception as exc:  # surfaced below
            failures.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=submit, args=(delay,)) for delay in (0, 0.4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert failures == []
        assert sorted(created for _, created in results) == [False, True]
        assert Order.objects.count() == 1
        row = CheckoutHookState.objects.get()
        assert row.order_id == Order.objects.get().pk
        assert row.state == HookState.COMMITTED
        later = timezone.now() + datetime.timedelta(seconds=901)
        counts = hook_service.sweep(now=later)
        assert counts["orphans_released"] == 0 and counts["adopted"] == 0
    finally:
        hook.uninstall()
    assert len(hook.ops("reserve")) == 1
    assert hook.ops("release") == []
    assert hook.holds[str(key)]["state"] == "committed"


def test_every_staggered_twin_gets_the_order_even_after_the_cart_emptied(
    user, catalog, settings, monkeypatch
):
    """Twins whose replay check runs before the first submit commits and whose
    cart read runs after it: each one gets the order, none ``cart_empty``."""
    from stapel_orders.payment import PaymentProvider, register_payment_provider

    first_done = threading.Event()

    class SlowPayment(PaymentProvider):
        def start(self, order, emit):
            time.sleep(1.0)
            return None

    register_payment_provider("slow", SlowPayment)
    settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "slow"}
    real_get_cart = checkout_service.get_cart

    def get_cart_after_the_first_commit(user_id):
        if threading.current_thread().name.startswith("twin"):
            first_done.wait(30)
        return real_get_cart(user_id)

    monkeypatch.setattr(checkout_service, "get_cart", get_cart_after_the_first_commit)
    catalog.put("a", price="10.00")
    cart_service.add_line(str(user.pk), "item", "a", 1)
    hook = FakeHook("stock").install()
    key = uuid.uuid4()
    results, failures = [], []

    def submit(delay):
        try:
            time.sleep(delay)
            results.append(checkout_service.checkout(
                user, idempotency_key=key, contact=_contact(), address=_address(),
            ))
        except Exception as exc:  # surfaced below
            failures.append(exc)
        finally:
            if threading.current_thread().name == "first":
                first_done.set()
            connection.close()

    threads = [threading.Thread(target=submit, args=(0,), name="first")] + [
        threading.Thread(target=submit, args=(0.3,), name=f"twin-{i}") for i in range(3)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        hook.uninstall()
    assert failures == []
    assert sorted(created for _, created in results) == [False, False, False, True]
    assert len({order.pk for order, _ in results}) == 1
    assert Order.objects.count() == 1
    assert len(hook.ops("reserve")) == 1 and hook.ops("release") == []
