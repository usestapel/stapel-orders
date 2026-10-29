"""System checks, each with the case that must fire and the one that must not."""
import pytest
from django.core import checks

from stapel_orders import checks as orders_checks


def ids(findings):
    return [f.id for f in findings]


class TestPurchasableTypes:
    def test_empty_registry_is_e001(self, settings):
        settings.STAPEL_ORDERS = {"PURCHASABLE_TYPES": {}}
        assert ids(orders_checks.check_purchasable_types(None)) == ["stapel_orders.E001"]

    def test_runtime_registration_counts(self, settings, catalog):
        from stapel_orders.registry import register_purchasable_type

        settings.STAPEL_ORDERS = {"PURCHASABLE_TYPES": {}}
        register_purchasable_type("item", {"resolve": "orders_test.purchasables"})
        assert orders_checks.check_purchasable_types(None) == []

    def test_malformed_entry_is_e003(self, settings):
        settings.STAPEL_ORDERS = {"PURCHASABLE_TYPES": {"item": {"resolver": "typo"}}}
        assert ids(orders_checks.check_purchasable_types(None)) == ["stapel_orders.E003"]

    def test_not_a_dict_is_e003(self, settings):
        settings.STAPEL_ORDERS = {"PURCHASABLE_TYPES": ["item"]}
        assert ids(orders_checks.check_purchasable_types(None)) == ["stapel_orders.E003"]

    def test_unreachable_resolver_is_w001(self):
        # The harness settings register "item" but no catalog is installed.
        assert ids(orders_checks.check_purchasable_types(None)) == ["stapel_orders.W001"]

    def test_reachable_resolver_is_silent(self, catalog):
        assert orders_checks.check_purchasable_types(None) == []


class TestPaymentPosture:
    def _mock(self, settings, posture=None):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "mock"}
        if posture is not None:
            settings.STAPEL_POSTURE = posture

    def test_none_is_silent(self):
        assert orders_checks.check_payment(None) == []

    def test_mock_without_a_posture_is_e002(self, settings):
        self._mock(settings)
        findings = orders_checks.check_payment(None)
        assert ids(findings) == ["stapel_orders.E002"]
        assert isinstance(findings[0], checks.Error)

    def test_mock_in_live_stage_is_e002(self, settings):
        self._mock(settings, {"PRESET": "public-space", "OPTIONS": {"stage": "live"}})
        assert ids(orders_checks.check_payment(None)) == ["stapel_orders.E002"]

    def test_mock_in_prototype_stage_is_w002(self, settings):
        self._mock(settings, {"PRESET": "public-space", "OPTIONS": {"stage": "prototype"}})
        findings = orders_checks.check_payment(None)
        assert ids(findings) == ["stapel_orders.W002"]
        assert isinstance(findings[0], checks.Warning)

    def test_unknown_provider_is_e004(self, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "paypalish"}
        assert ids(orders_checks.check_payment(None)) == ["stapel_orders.E004"]

    def test_registered_provider_is_accepted(self, settings):
        from stapel_orders.payment import NoPayment, register_payment_provider

        class Custom(NoPayment):
            name = "custom"

        register_payment_provider("custom", Custom)
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "PAYMENT": "custom"}
        assert orders_checks.check_payment(None) == []

    def test_env_may_pick_a_short_name_only(self, settings, monkeypatch):
        from django.core.exceptions import ImproperlyConfigured

        from stapel_orders.conf import orders_settings

        settings.STAPEL_ORDERS = {"PURCHASABLE_TYPES": {}}
        monkeypatch.setenv("PAYMENT", "mock")
        assert orders_settings.PAYMENT.name == "mock"
        monkeypatch.setenv("PAYMENT", "evil.module.Provider")
        orders_settings._cache.clear()
        with pytest.raises(ImproperlyConfigured):
            orders_settings.PAYMENT


class TestMoneyBoundsCheck:
    def test_defaults_are_silent(self):
        assert orders_checks.check_money_bounds(None) == []

    def test_widened_limit_is_e005(self, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "MAX_LINE_QUANTITY": 10**7}
        assert ids(orders_checks.check_money_bounds(None)) == ["stapel_orders.E005"]

    def test_non_positive_limit_is_e005(self, settings):
        settings.STAPEL_ORDERS = {**settings.STAPEL_ORDERS, "MAX_LINES_PER_CART": 0}
        assert ids(orders_checks.check_money_bounds(None)) == ["stapel_orders.E005"]


@pytest.mark.django_db
def test_manage_py_check_is_clean_with_a_resolver(catalog):
    """Every registered check over the harness — the adoption/lifecycle/posture
    families included — reports nothing at Error level for this app."""
    from django.core.management import call_command

    call_command("check", "--fail-level", "ERROR")
