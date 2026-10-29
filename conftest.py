def pytest_configure(config):
    from django.conf import settings

    if not settings.configured:
        from stapel_orders._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
        import django

        django.setup()

        from stapel_core.comm.schemas import autoload_schemas

        autoload_schemas()


import pytest  # noqa: E402
from stapel_core.testing import emit_outside_atomic_gate  # noqa: E402,F401


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


def _make_user(username, **extra):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="x", **extra
    )


@pytest.fixture
def user(db):
    return _make_user("alice")


@pytest.fixture
def other_user(db):
    return _make_user("bob")


@pytest.fixture
def guest(db):
    return _make_user("guest-1", is_anonymous=True)


@pytest.fixture
def staff(db):
    return _make_user("staff", is_staff=True)


@pytest.fixture
def client_for(api_client):
    def _client(u):
        from rest_framework.test import APIClient

        c = APIClient()
        c.force_authenticate(user=u)
        return c

    return _client


@pytest.fixture
def auth_client(client_for, user):
    return client_for(user)


@pytest.fixture
def catalog():
    """The fake product owner behind the ``item`` purchasable type."""
    from stapel_orders.tests.catalog import FakeCatalog

    fake = FakeCatalog()
    fake.install()
    try:
        yield fake
    finally:
        fake.uninstall()


@pytest.fixture(autouse=True)
def _reset_registries():
    from stapel_orders.payment import reset_payment_providers
    from stapel_orders.registry import reset_purchasable_types

    reset_purchasable_types()
    reset_payment_providers()
    yield
    reset_purchasable_types()
    reset_payment_providers()


@pytest.fixture
def captured_events():
    """Collect orders emits delivered in-process."""
    from stapel_core.comm import action_registry, subscribe_action

    collected = []

    def _handler(event):
        collected.append(event)

    names = ["orders.order.placed", "orders.order.status_changed", "orders.order.paid"]
    for name in names:
        subscribe_action(name, _handler)
    try:
        yield collected
    finally:
        for name in names:
            handlers = action_registry._subscribers.get(name, [])
            if _handler in handlers:
                handlers.remove(_handler)
