"""Checkout seams: ADJUSTERS (quote, snapshot, refusal) and CHECKOUT_HOOKS.

The companions are the in-process fakes of ``tests/fake_seams.py``, behind
the seam: every payload orders sends them is validated against the
published contract schema, every reply goes through orders' parsing.
"""
import datetime
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from stapel_orders import checks as orders_checks
from stapel_orders import money, seams
from stapel_orders.models import (
    CheckoutHookState,
    HookState,
    Order,
    OrderAdjustment,
)
from stapel_orders.services import hooks as hook_service

from .fake_seams import FakeAdjuster, FakeHook, discount_by_code, flat_shipping

V1 = "/orders/api/v1"
LINES = V1 + "/cart/lines/"
QUOTE = V1 + "/cart/quote/"
CHECKOUT = V1 + "/checkout/"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_seams():
    seams.reset_seams()
    yield
    seams.reset_seams()


@pytest.fixture
def installed():
    """Install fakes for one test; uninstalled afterwards."""
    fakes = []

    def _install(fake):
        fakes.append(fake.install())
        return fake

    yield _install
    for fake in fakes:
        fake.uninstall()


@pytest.fixture
def filled(auth_client, catalog):
    catalog.put("a", price="40.00")
    catalog.put("b", price="10.00")
    for key, qty in (("a", 1), ("b", 2)):
        resp = auth_client.post(
            LINES, {"purchasable_type": "item", "purchasable_key": key, "quantity": qty},
            format="json",
        )
        assert resp.status_code == 200, resp.content
    return auth_client


def body(key=None, **context):
    return {
        "idempotency_key": str(key or uuid.uuid4()),
        "contact": {"name": "Ann Buyer", "phone": "+1 555 0100"},
        "shipping_address": {"line1": "1 Main St", "city": "Springfield", "country": "US"},
        "context": context,
    }


def promo_and_shipping(installed):
    promo = installed(FakeAdjuster("promo", discount_by_code("SAVE10", 10), order=100))
    ship = installed(FakeAdjuster("ship", flat_shipping("5.00"), order=200))
    return promo, ship


# ── Defaults: nothing registered ─────────────────────────────────────────


class TestDefaultsAreInert:
    def test_quote_is_the_cart_with_no_adjustments(self, filled):
        resp = filled.post(QUOTE, {}, format="json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["subtotal"] == data["total"] == "60.00"
        assert data["adjustments"] == [] and data["errors"] == []

    def test_checkout_total_is_the_subtotal_and_adds_only_adjustments(self, filled):
        resp = filled.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 201, resp.content
        order = resp.json()
        assert order["subtotal"] == order["total"] == "60.00"
        assert order["adjustments"] == []
        assert not OrderAdjustment.objects.exists()
        assert not CheckoutHookState.objects.exists()

    def test_registries_ship_empty(self):
        assert seams.get_adjusters() == []
        assert seams.get_checkout_hooks() == []


# ── Adjusters ────────────────────────────────────────────────────────────


class TestAdjusters:
    def test_discount_and_shipping_lines_on_quote(self, filled, installed):
        promo, ship = promo_and_shipping(installed)
        resp = filled.post(QUOTE, {"context": {
            "codes": ["save10"], "delivery": {"method": "courier"},
        }}, format="json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["subtotal"] == "60.00"
        assert [(a["kind"], a["amount"]) for a in data["adjustments"]] == [
            ("discount", "-6.00"), ("shipping", "5.00"),
        ]
        assert data["total"] == "59.00"
        assert data["errors"] == []
        assert "meta" not in data["adjustments"][0]
        # The shipping adjuster ran second and saw the discount.
        seen = ship.calls[-1]["adjustments_so_far"]
        assert [(a["adjuster"], a["amount"]) for a in seen] == [("promo", "-6.00")]
        assert promo.calls[-1]["adjustments_so_far"] == []

    def test_request_carries_lines_attrs_user_and_context(self, filled, installed, user, catalog):
        catalog.items["a"]["weight_grams"] = 1250
        catalog.items["a"]["nested"] = {"dropped": True}
        filled.post(LINES, {"purchasable_type": "item", "purchasable_key": "a", "quantity": 1},
                    format="json")
        promo, _ = promo_and_shipping(installed)
        filled.post(QUOTE, {"context": {"codes": ["SAVE10"]}}, format="json")
        request = promo.calls[-1]
        assert request["user_id"] == str(user.pk)
        assert request["currency"] == "USD"
        assert request["context"] == {"codes": ["SAVE10"]}
        first = request["lines"][0]
        assert first["index"] == 0 and first["quantity"] == 2
        assert first["attrs"] == {"weight_grams": 1250}

    def test_cart_lines_show_the_attrs_the_adjusters_see(self, filled, catalog):
        """The browser quotes delivery options with the same attrs the server
        quote sends (weight_grams, shippable)."""
        catalog.items["a"]["weight_grams"] = 1250
        catalog.items["a"]["shippable"] = True
        resp = filled.post(LINES, {"purchasable_type": "item", "purchasable_key": "a",
                                   "quantity": 1}, format="json")
        lines = {line["purchasable_key"]: line for line in resp.json()["lines"]}
        assert lines["a"]["attrs"] == {"weight_grams": 1250, "shippable": True}
        assert lines["b"]["attrs"] == {}
        cart = filled.get(V1 + "/cart/").json()
        assert [line["attrs"] for line in cart["lines"]] == [
            lines[line["purchasable_key"]]["attrs"] for line in cart["lines"]
        ]

    def test_checkout_snapshots_adjustments_and_total(self, filled, installed, captured_events):
        promo_and_shipping(installed)
        resp = filled.post(CHECKOUT, body(codes=["SAVE10"], delivery={"method": "courier"}),
                           format="json")
        assert resp.status_code == 201, resp.content
        order = resp.json()
        assert (order["subtotal"], order["total"]) == ("60.00", "59.00")
        assert [(a["kind"], a["code"], a["amount"]) for a in order["adjustments"]] == [
            ("discount", "SAVE10", "-6.00"), ("shipping", "courier", "5.00"),
        ]
        rows = list(OrderAdjustment.objects.order_by("position"))
        assert [(r.adjuster, r.amount, r.meta) for r in rows] == [
            ("promo", Decimal("-6.00"), {"promotion_id": 7}),
            ("ship", Decimal("5.00"), {}),
        ]
        placed = [e for e in captured_events if e.event_type == "orders.order.placed"]
        assert placed[-1].payload["total"] == "59.00"
        # The money proof on the stored row: total = max(0, subtotal + adjustments).
        stored = Order.objects.get(pk=order["id"])
        assert stored.total == money.adjusted_total(stored.subtotal, [r.amount for r in rows])

    def test_total_is_floored_at_zero(self, filled, installed):
        installed(FakeAdjuster("giveaway", lambda p: {"adjustments": [{
            "kind": "discount", "code": "ALL", "label": "", "amount": "-100.00",
            "line_index": 0, "meta": {},
        }], "errors": []}))
        resp = filled.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 201, resp.content
        assert resp.json()["total"] == "0.00"
        assert resp.json()["adjustments"][0]["line_index"] == 0

    def test_error_is_shown_by_quote_and_refuses_checkout(self, filled, installed):
        promo_and_shipping(installed)
        quote = filled.post(QUOTE, {"context": {"codes": ["BOGUS"], "delivery": {"method": "c"}}},
                            format="json")
        assert quote.status_code == 200
        assert quote.json()["errors"] == [
            {"code": "error.409.promo_code_unknown", "field": "codes", "params": {"code": "BOGUS"}},
        ]
        resp = filled.post(CHECKOUT, body(codes=["BOGUS"], delivery={"method": "c"}), format="json")
        assert resp.status_code == 409
        envelope = resp.json()
        assert envelope["localizable_error"] == "error.409.promo_code_unknown"
        assert envelope["params"] == {"code": "BOGUS", "field": "codes"}
        assert not Order.objects.exists()

    def test_status_comes_from_the_error_key(self, filled, installed):
        installed(FakeAdjuster("ship", flat_shipping("5.00")))
        resp = filled.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.delivery_method_required"

    def test_unanswered_adjuster_fails_closed(self, filled, installed):
        promo = installed(FakeAdjuster("promo", discount_by_code("SAVE10", 10)))
        promo.fail = True
        quote = filled.post(QUOTE, {}, format="json").json()
        assert quote["errors"] == [{"code": "error.503.adjuster_unavailable", "field": None,
                                    "params": {"adjuster": "promo"}}]
        assert quote["total"] == "60.00"
        resp = filled.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == "error.503.adjuster_unavailable"

    @pytest.mark.parametrize("bad", [
        {"kind": "discount", "amount": -6.0},
        {"kind": "discount", "amount": "-6.001"},
        {"kind": "rebate", "amount": "-6.00"},
        {"kind": "fee", "amount": "1.00", "line_index": 2},
        {"kind": "fee", "amount": "100000000000000.00"},
    ])
    def test_malformed_adjustment_is_unavailable(self, filled, installed, bad):
        installed(FakeAdjuster("odd", lambda p: {"adjustments": [bad], "errors": []}))
        quote = filled.post(QUOTE, {}, format="json").json()
        assert [e["code"] for e in quote["errors"]] == ["error.503.adjuster_unavailable"]
        assert quote["adjustments"] == []

    def test_more_than_max_adjustments_is_unavailable(self, filled, installed, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "MAX_ADJUSTMENTS": 1}
        promo_and_shipping(installed)
        quote = filled.post(QUOTE, {"context": {"codes": ["SAVE10"],
                                                "delivery": {"method": "c"}}}, format="json").json()
        assert [a["kind"] for a in quote["adjustments"]] == ["discount"]
        assert quote["errors"][0]["params"] == {"adjuster": "ship"}

    def test_empty_cart_asks_nobody(self, auth_client, installed, catalog):
        promo, _ = promo_and_shipping(installed)
        data = auth_client.post(QUOTE, {"context": {"codes": ["BOGUS"]}}, format="json").json()
        assert data["errors"] == [] and data["total"] == "0.00"
        assert promo.calls == []

    def test_guest_is_a_null_user(self, client_for, guest, catalog, installed, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "CHECKOUT_REQUIRES_ACCOUNT": False}
        client = client_for(guest)
        catalog.put("a")
        client.post(LINES, {"purchasable_type": "item", "purchasable_key": "a"}, format="json")
        promo, _ = promo_and_shipping(installed)
        client.post(QUOTE, {}, format="json")
        assert promo.calls[-1]["user_id"] is None

    def test_oversized_context_is_400(self, filled):
        resp = filled.post(QUOTE, {"context": {"blob": "x" * 9000}}, format="json")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.checkout_context_too_large"

    def test_adjusters_run_by_order_then_name(self, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "ADJUSTERS": {
            "tax": {"quote": "t.quote", "order": 300},
            "b": {"quote": "b.quote"},
            "a": {"quote": "a.quote"},
            "gone": None,
        }}
        assert [name for name, _ in seams.get_adjusters()] == ["a", "b", "tax"]


# ── Checkout hooks ───────────────────────────────────────────────────────


def _run_commits(django_capture_on_commit_callbacks, call):
    with django_capture_on_commit_callbacks(execute=True):
        return call()


class TestHooks:
    def test_reserve_then_commit_on_placed(self, filled, installed,
                                           django_capture_on_commit_callbacks):
        installed(FakeAdjuster("promo", discount_by_code("SAVE10", 10)))
        stock = installed(FakeHook("stock"))
        key = uuid.uuid4()
        resp = _run_commits(django_capture_on_commit_callbacks,
                            lambda: filled.post(CHECKOUT, body(key, codes=["SAVE10"]), format="json"))
        assert resp.status_code == 201, resp.content
        reserve = stock.ops("reserve")[0]
        assert reserve["attempt_key"] == str(key)
        assert [a["code"] for a in reserve["adjustments_so_far"]] == ["SAVE10"]
        commit = stock.ops("commit")[0]
        assert commit["order_id"] == str(resp.json()["id"])
        assert commit["token"] == stock.holds[str(key)]["token"]
        assert stock.holds[str(key)]["state"] == "committed"
        row = CheckoutHookState.objects.get()
        assert (row.state, row.order_id, row.token) == (
            HookState.COMMITTED, resp.json()["id"], stock.holds[str(key)]["token"]
        )
        assert stock.ops("release") == []

    def test_a_refusing_hook_releases_the_ones_before_it(self, filled, installed):
        stock = installed(FakeHook("stock", order=10))
        promo = installed(FakeHook("redemptions", order=20))
        promo.refuse_with = {"code": "error.409.promo_exhausted", "field": "codes", "params": {}}
        key = uuid.uuid4()
        resp = filled.post(CHECKOUT, body(key), format="json")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.promo_exhausted"
        assert not Order.objects.exists()
        assert stock.holds[str(key)]["state"] == "released"
        assert [p["reason"] for p in stock.ops("release")] == ["checkout_failed"]
        assert stock.ops("release")[0]["order_id"] is None
        assert stock.ops("commit") == []
        assert set(CheckoutHookState.objects.values_list("state", flat=True)) == {HookState.RELEASED}

    def test_an_unanswered_reserve_fails_closed_and_is_released(self, filled, installed):
        stock = installed(FakeHook("stock"))
        stock.silent = {"reserve"}
        resp = filled.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == "error.503.checkout_hook_unavailable"
        assert not Order.objects.exists()
        assert len(stock.ops("release")) == 1

    def test_ok_false_without_errors_refuses(self, filled, installed):
        stock = installed(FakeHook("stock"))
        stock.reserve = lambda payload: {"ok": False}
        stock.uninstall()
        stock.install()
        resp = filled.post(CHECKOUT, body(), format="json")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.checkout_hook_refused"

    def test_adjuster_errors_refuse_before_any_reserve(self, filled, installed):
        installed(FakeAdjuster("promo", discount_by_code("SAVE10", 10)))
        stock = installed(FakeHook("stock"))
        resp = filled.post(CHECKOUT, body(codes=["NOPE"]), format="json")
        assert resp.status_code == 409
        assert stock.ops("reserve") == []

    def test_retry_with_the_same_key_reserves_anew(self, filled, installed,
                                                   django_capture_on_commit_callbacks):
        installed(FakeAdjuster("promo", discount_by_code("SAVE10", 10)))
        stock = installed(FakeHook("stock"))
        key = uuid.uuid4()
        stock.refuse_with = {"code": "error.409.out_of_stock", "field": None, "params": {}}
        assert filled.post(CHECKOUT, body(key), format="json").status_code == 409
        stock.refuse_with = None
        resp = _run_commits(django_capture_on_commit_callbacks,
                            lambda: filled.post(CHECKOUT, body(key), format="json"))
        assert resp.status_code == 201, resp.content
        assert stock.holds[str(key)]["state"] == "committed"
        assert CheckoutHookState.objects.get().state == HookState.COMMITTED

    def test_cancel_releases_after_commit(self, filled, installed,
                                          django_capture_on_commit_callbacks):
        stock = installed(FakeHook("stock"))
        key = uuid.uuid4()
        order = _run_commits(django_capture_on_commit_callbacks,
                             lambda: filled.post(CHECKOUT, body(key), format="json")).json()
        resp = _run_commits(django_capture_on_commit_callbacks,
                            lambda: filled.post(f"{V1}/orders/{order['id']}/cancel/"))
        assert resp.status_code == 200, resp.content
        release = stock.ops("release")
        assert [(p["reason"], p["order_id"]) for p in release] == [("cancelled", str(order["id"]))]
        assert stock.holds[str(key)]["state"] == "released"
        assert CheckoutHookState.objects.get().state == HookState.RELEASED

    def test_staff_cancel_releases_too(self, filled, installed, client_for, staff,
                                       django_capture_on_commit_callbacks):
        stock = installed(FakeHook("stock"))
        order = _run_commits(django_capture_on_commit_callbacks,
                             lambda: filled.post(CHECKOUT, body(), format="json")).json()
        _run_commits(django_capture_on_commit_callbacks, lambda: client_for(staff).post(
            f"{V1}/orders/{order['id']}/transition/", {"to": "cancelled"}, format="json"))
        assert [p["reason"] for p in stock.ops("release")] == ["cancelled"]

    def test_crash_mid_checkout_is_released_by_the_sweep(self, filled, installed, monkeypatch,
                                                         settings):
        from stapel_orders.payment import PaymentProvider, register_payment_provider

        class Crashing(PaymentProvider):
            def start(self, order, emit):
                raise RuntimeError("worker killed")

        register_payment_provider("crash", Crashing)
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "crash"}
        stock = installed(FakeHook("stock"))
        # The process dies: the in-request release never runs.
        monkeypatch.setattr(hook_service, "release_now", lambda *a, **k: None)
        key = uuid.uuid4()
        with pytest.raises(RuntimeError):
            filled.post(CHECKOUT, body(key), format="json")
        assert not Order.objects.exists()
        assert stock.holds[str(key)]["state"] == "reserved"
        row = CheckoutHookState.objects.get()
        assert row.state == HookState.PENDING and row.order_id is None

        assert hook_service.sweep()["orphans_released"] == 0  # too young
        later = timezone.now() + datetime.timedelta(seconds=901)
        assert hook_service.sweep(now=later)["orphans_released"] == 1
        assert [p["reason"] for p in stock.ops("release")] == ["orphaned"]
        assert stock.ops("release")[0]["token"] is None
        assert stock.holds[str(key)]["state"] == "released"
        assert CheckoutHookState.objects.get().state == HookState.RELEASED

    def test_unanswered_commit_is_retried_by_the_sweep(self, filled, installed,
                                                       django_capture_on_commit_callbacks):
        stock = installed(FakeHook("stock"))
        stock.silent = {"commit"}
        key = uuid.uuid4()
        resp = _run_commits(django_capture_on_commit_callbacks,
                            lambda: filled.post(CHECKOUT, body(key), format="json"))
        assert resp.status_code == 201
        row = CheckoutHookState.objects.get()
        assert (row.state, row.tries) == (HookState.PLACED, 1)
        stock.silent = set()
        assert hook_service.sweep()["committed"] == 1
        assert CheckoutHookState.objects.get().state == HookState.COMMITTED
        assert stock.holds[str(key)]["state"] == "committed"

    def test_management_command_runs_the_sweep(self, capsys):
        from django.core.management import call_command

        call_command("sweep_checkout_hooks")
        assert "orphans released 0" in capsys.readouterr().out

    def test_a_key_of_another_user_is_refused(self, filled, installed, other_user, client_for,
                                              catalog):
        installed(FakeHook("stock"))
        key = uuid.uuid4()
        CheckoutHookState.objects.create(attempt_key=key, name="stock",
                                         user_id=str(other_user.pk), state=HookState.RELEASED)
        resp = filled.post(CHECKOUT, body(key), format="json")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == "error.409.checkout_attempt_conflict"


class TestConcurrentAttempts:
    """Two submits of one attempt key: one order, one reservation, no release.

    SQLite drops FOR UPDATE, so the interleaving is forced deterministically:
    the twin runs completely between the first request's durable state rows
    and its locked transaction (where it would wait on the cart lock).
    """

    def test_the_late_twin_replays_without_reserving_or_releasing(
        self, filled, installed, user, monkeypatch, django_capture_on_commit_callbacks
    ):
        from stapel_orders.services import checkout as checkout_service

        stock = installed(FakeHook("stock"))
        key = uuid.uuid4()
        real_prepare = hook_service.prepare
        state = {"twin": None}

        def prepare_then_twin(attempt_key, user_id, written=None):
            names = real_prepare(attempt_key, user_id, written)
            if state["twin"] is None:
                state["twin"] = "running"
                state["twin"] = filled.post(CHECKOUT, body(key), format="json")
            return names

        monkeypatch.setattr(hook_service, "prepare", prepare_then_twin)
        with django_capture_on_commit_callbacks(execute=True):
            order, created = checkout_service.checkout(
                user, idempotency_key=key,
                contact=checkout_service.Contact(name="Ann", phone="1"),
                address=checkout_service.Address(line1="l", city="c", country="US"),
            )
        assert state["twin"].status_code == 201
        assert created is False
        assert order.pk == state["twin"].json()["id"]
        assert Order.objects.count() == 1
        assert len(stock.ops("reserve")) == 1
        assert stock.ops("release") == []
        assert CheckoutHookState.objects.get().state == HookState.COMMITTED

    def test_a_twin_after_commit_leaves_the_rows_alone(self, filled, installed, user,
                                                       monkeypatch,
                                                       django_capture_on_commit_callbacks):
        from stapel_orders.services import checkout as checkout_service

        stock = installed(FakeHook("stock"))
        key = uuid.uuid4()
        first = _run_commits(django_capture_on_commit_callbacks,
                             lambda: filled.post(CHECKOUT, body(key), format="json"))
        # The twin's first replay read was stale; the lock-side read is not.
        real = checkout_service._replayed
        calls = {"n": 0}

        def stale_once(user_id, k):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(user_id, k)

        monkeypatch.setattr(checkout_service, "_replayed", stale_once)
        # The shopper filled a new cart meanwhile, so the twin gets past the
        # empty-cart check and reaches the hook rows.
        filled.post(LINES, {"purchasable_type": "item", "purchasable_key": "a"}, format="json")
        catalog_twin = filled.post(CHECKOUT, body(key), format="json")
        assert catalog_twin.status_code == 200
        assert catalog_twin.json()["id"] == first.json()["id"]
        assert len(stock.ops("reserve")) == 1 and stock.ops("release") == []
        assert CheckoutHookState.objects.get().state == HookState.COMMITTED

    def test_unique_pair_is_the_backstop(self, user):
        from django.db import IntegrityError, transaction

        key = uuid.uuid4()
        CheckoutHookState.objects.create(attempt_key=key, name="stock", user_id=str(user.pk))
        with pytest.raises(IntegrityError), transaction.atomic():
            CheckoutHookState.objects.create(attempt_key=key, name="stock", user_id=str(user.pk))


# ── Checks and the money proof ───────────────────────────────────────────


def ids(findings):
    return [f.id for f in findings]


class TestChecks:
    def test_empty_seams_are_silent(self):
        assert orders_checks.check_checkout_seams(None) == []

    def test_malformed_entries(self, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS,
                                  "ADJUSTERS": {"p": {"quote": ""}},
                                  "CHECKOUT_HOOKS": {"s": {"reserve": "x"}}}
        assert ids(orders_checks.check_checkout_seams(None)) == [
            "stapel_orders.E006", "stapel_orders.E007",
        ]

    def test_not_a_dict(self, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "ADJUSTERS": ["p"]}
        assert ids(orders_checks.check_checkout_seams(None)) == ["stapel_orders.E006"]

    def test_unreachable_is_a_warning_and_reachable_is_silent(self, settings, installed):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS,
                                  "ADJUSTERS": {"p": {"quote": "nobody.quote"}}}
        assert ids(orders_checks.check_checkout_seams(None)) == ["stapel_orders.W003"]
        settings.STAPEL_ORDERS = {k: v for k, v in settings.STAPEL_ORDERS.items()
                                  if k != "ADJUSTERS"}
        installed(FakeAdjuster("promo", discount_by_code("X", 1)))
        installed(FakeHook("stock"))
        assert orders_checks.check_checkout_seams(None) == []

    def test_e005_counts_adjustments(self, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "MAX_ADJUSTMENTS": 100}
        assert ids(orders_checks.check_money_bounds(None)) == ["stapel_orders.E005"]


class TestMoneyProof:
    def test_default_limits_hold_with_adjustments(self):
        from stapel_orders.conf import DEFAULTS

        assert money.columns_hold(DEFAULTS["MAX_LINE_QUANTITY"], DEFAULTS["MAX_LINES_PER_CART"],
                                  DEFAULTS["MAX_ADJUSTMENTS"])

    def test_too_many_adjustments_break_the_proof(self):
        """Inversion: 100 adjustments of < 10^14 overflow the 10^16 column."""
        assert not money.columns_hold(999, 100, 100)

    def test_adjustment_column_is_the_one_money_declares(self):
        field = OrderAdjustment._meta.get_field("amount")
        assert field.max_digits - field.decimal_places == money.ADJUSTMENT_INTEGER_DIGITS

    def test_worst_case_total_passes_the_column_validators(self):
        """What PostgreSQL's numeric(18,2) enforces; SQLite stores REAL and cannot."""
        from stapel_orders.conf import DEFAULTS

        largest = Decimal(10) ** money.UNIT_PRICE_INTEGER_DIGITS - Decimal("0.01")
        line = money.line_total(largest, DEFAULTS["MAX_LINE_QUANTITY"])
        subtotal = money.sum_lines([line] * DEFAULTS["MAX_LINES_PER_CART"])
        top = money.ADJUSTMENT_LIMIT - Decimal("0.01")
        total = money.adjusted_total(subtotal, [top] * DEFAULTS["MAX_ADJUSTMENTS"])
        assert Order._meta.get_field("total").clean(total, None) == total
        assert OrderAdjustment._meta.get_field("amount").clean(top, None) == top
        with pytest.raises(Exception):
            Order._meta.get_field("total").clean(Decimal(10) ** money.TOTAL_INTEGER_DIGITS, None)

    def test_parse_adjustment_bounds(self):
        assert money.parse_adjustment("-0.5") == Decimal("-0.50")
        for bad in (1.5, "1.001", "NaN", "1e20", str(money.ADJUSTMENT_LIMIT)):
            with pytest.raises(money.MalformedPrice):
                money.parse_adjustment(bad)


def test_no_companion_import_anywhere():
    from .test_architecture import _imports, _sources

    companions = ("stapel_promotions", "stapel_delivery", "stapel_inventory")
    offenders = [
        (path.name, name) for path in _sources() for name in _imports(path)
        if name.split(".")[0] in companions
    ]
    assert offenders == []
    assert "seams.py" in {p.name for p in _sources()}


def test_placed_fact_carries_the_attempt_key(filled, captured_events, installed,
                                              django_capture_on_commit_callbacks):
    """The key the hooks reserved under, on the fact: a companion finds its intent."""
    stock = installed(FakeHook("stock"))
    key = uuid.uuid4()
    upper = str(key).upper()
    from stapel_orders.services import checkout as checkout_service
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.get(username="alice")
    with django_capture_on_commit_callbacks(execute=True):
        checkout_service.checkout(
            user, idempotency_key=upper,
            contact=checkout_service.Contact(name="Ann", phone="1"),
            address=checkout_service.Address(line1="l", city="c", country="US"),
        )
    placed = [e for e in captured_events if e.event_type == "orders.order.placed"]
    assert placed[-1].payload["attempt_key"] == str(key)
    assert stock.ops("reserve")[0]["attempt_key"] == str(key)


def test_commit_and_release_answers_may_carry_extra_keys_and_ok_is_final(
    filled, installed, django_capture_on_commit_callbacks
):
    """(a) unknown keys beyond {ok} are ignored; (b) ok: true is never re-sent."""
    stock = installed(FakeHook("stock"))
    stock.commit = lambda p: (stock.calls.append(("commit", p)),
                              {"ok": True, "token": None, "errors": [], "note": "unknown key"})[1]
    stock.release = lambda p: (stock.calls.append(("release", p)),
                               {"ok": True, "errors": []})[1]
    stock.uninstall()
    stock.install()
    with django_capture_on_commit_callbacks(execute=True):
        order = filled.post(CHECKOUT, body(), format="json").json()
    assert CheckoutHookState.objects.get().state == HookState.COMMITTED
    hook_service.sweep(now=timezone.now() + datetime.timedelta(days=1))
    assert len(stock.ops("commit")) == 1
    with django_capture_on_commit_callbacks(execute=True):
        filled.post(f"{V1}/orders/{order['id']}/cancel/")
    hook_service.sweep(now=timezone.now() + datetime.timedelta(days=1))
    assert len(stock.ops("release")) == 1
    assert CheckoutHookState.objects.get().state == HookState.RELEASED


def test_a_cancel_before_an_unanswered_commit_sends_release_not_commit(
    filled, installed, django_capture_on_commit_callbacks
):
    stock = installed(FakeHook("stock"))
    stock.silent = {"commit"}
    with django_capture_on_commit_callbacks(execute=True):
        order = filled.post(CHECKOUT, body(), format="json").json()
    assert CheckoutHookState.objects.get().state == HookState.PLACED
    stock.silent = set()
    with django_capture_on_commit_callbacks(execute=True):
        filled.post(f"{V1}/orders/{order['id']}/cancel/")
    hook_service.sweep()
    assert len(stock.ops("commit")) == 1  # the one unanswered try, never retried
    assert [p["reason"] for p in stock.ops("release")] == ["cancelled"]
    assert CheckoutHookState.objects.get().state == HookState.RELEASED


class TestExpectedTotal:
    """Optional expected_total: the shopper is never charged a total they did not see."""

    def test_a_matching_total_places_the_order(self, filled, installed):
        promo_and_shipping(installed)
        ctx = {"codes": ["SAVE10"], "delivery": {"method": "courier"}}
        shown = filled.post(QUOTE, {"context": ctx}, format="json").json()["total"]
        resp = filled.post(CHECKOUT, {**body(**ctx), "expected_total": shown}, format="json")
        assert resp.status_code == 201, resp.content
        assert resp.json()["total"] == shown == "59.00"

    def test_absent_means_no_comparison(self, filled, installed):
        promo_and_shipping(installed)
        resp = filled.post(CHECKOUT, body(delivery={"method": "courier"}), format="json")
        assert resp.status_code == 201

    def test_a_changed_adjuster_answer_refuses_with_the_fresh_quote_before_any_reserve(
        self, filled, installed, django_capture_on_commit_callbacks
    ):
        _, ship = promo_and_shipping(installed)
        stock = installed(FakeHook("stock"))
        ctx = {"delivery": {"method": "courier"}}
        shown = filled.post(QUOTE, {"context": ctx}, format="json").json()["total"]
        assert shown == "65.00"
        ship.answer = flat_shipping("7.50")  # the rate changed between quote and submit
        key = uuid.uuid4()
        resp = filled.post(CHECKOUT, {**body(key, **ctx), "expected_total": shown},
                           format="json")
        assert resp.status_code == 409
        envelope = resp.json()
        assert envelope["localizable_error"] == "error.409.total_changed"
        assert envelope["params"]["total"] == "67.50"
        fresh = envelope["params"]["quote"]
        assert fresh["total"] == "67.50"
        assert [(a["kind"], a["amount"]) for a in fresh["adjustments"]] == [("shipping", "7.50")]
        assert len(fresh["lines"]) == 2 and fresh["errors"] == []
        assert not Order.objects.exists()
        assert stock.ops("reserve") == [] and stock.ops("release") == []

        # The same attempt key, resubmitted with the fresh total, reserves anew.
        with django_capture_on_commit_callbacks(execute=True):
            again = filled.post(CHECKOUT, {**body(key, **ctx), "expected_total": "67.50"},
                                format="json")
        assert again.status_code == 201, again.content
        assert again.json()["total"] == "67.50"
        assert len(stock.ops("reserve")) == 1
        assert stock.holds[str(key)]["state"] == "committed"
        assert CheckoutHookState.objects.get().state == HookState.COMMITTED

    def test_the_fresh_quote_matches_the_declared_schema(self, filled, installed):
        import json
        from pathlib import Path

        from .test_contract_wire import _validator

        promo_and_shipping(installed)
        resp = filled.post(CHECKOUT, {**body(delivery={"method": "c"}), "expected_total": "1.00"},
                           format="json")
        schema = json.loads((Path(__file__).resolve().parent.parent / "docs" / "schema.json")
                            .read_text())
        quote_schema = {"$ref": "#/components/schemas/CartQuoteResponse"}
        assert _validator({**quote_schema, "components": schema["components"]}) \
            .is_valid(resp.json()["params"]["quote"])

    def test_a_malformed_expected_total_is_400(self, filled):
        resp = filled.post(CHECKOUT, {**body(), "expected_total": "1.234"}, format="json")
        assert resp.status_code == 400
