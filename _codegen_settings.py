"""Single-module Django settings shared by the test suite and contract emission.

One copy, so the harness that emits ``docs/schema.json`` and the tests that
prove it can never drift in INSTALLED_APPS or comm config. The production
``REST_FRAMEWORK`` (exception handler, schema class) is used by both: the
error envelope the tests assert is the one a host serves.
"""
from __future__ import annotations

import os

#: The resolver Function the test catalog provides (tests/catalog.py).
TEST_RESOLVER = "orders_test.purchasables"

#: ``postgresql://user:password@host:port/db`` runs the suite on PostgreSQL
#: (real row locks; tests/test_checkout_hooks_pg.py needs it). Unset: SQLite.
TEST_PG_ENV = "STAPEL_ORDERS_TEST_PG"


def _databases() -> dict:
    url = os.environ.get(TEST_PG_ENV, "")
    if not url:
        return {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parts.path.lstrip("/"),
        "USER": parts.username or "",
        "PASSWORD": parts.password or "",
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
    }}


def _migration_modules() -> dict:
    """``users`` has no migrations here. On PostgreSQL its synced tables
    reference ``auth`` at creation, so the framework apps sync with it;
    stapel_orders always runs its own migrations from zero."""
    if not os.environ.get(TEST_PG_ENV, ""):
        return {"users": None}
    return {app: None for app in ("users", "auth", "contenttypes", "admin", "sessions")}


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_orders.tests.urls",
    contract: bool = False,
) -> dict:
    rest_framework = {
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
        "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
    }
    if contract:
        rest_framework.update({
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
        })
    return dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            "stapel_orders",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES=_databases(),
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
        },
        STAPEL_ORDERS={
            "PURCHASABLE_TYPES": {"item": {"resolve": TEST_RESOLVER}},
        },
        TEMPLATES=[{
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {"context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]},
        }],
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        MIGRATION_MODULES=_migration_modules(),
        REST_FRAMEWORK=rest_framework,
    )


#: Forced on drf-spectacular's singleton so a single-module instance derives
#: the same operation ids as a multi-module aggregate.
CODEGEN_SCHEMA_PATH_PREFIX = "/"


def enum_name_overrides() -> dict:
    """Stable enum component names (stapel-schema-lint SCH004).

    ``OrderStatus`` is carried by ``OrderResponse.status`` and
    ``OrderTransitionRequest.to``; naming both sets keeps the react pair's
    generated types from being renamed by an unrelated edit. Called after
    ``django.setup()``.
    """
    from stapel_orders.models import AdjustmentKind, OrderStatus, PaymentStatus

    return {
        "AdjustmentKindEnum": AdjustmentKind,
        "OrderStatusEnum": OrderStatus,
        "PaymentStatusEnum": PaymentStatus,
    }
