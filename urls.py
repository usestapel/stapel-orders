"""Root URLconf for stapel-orders — v1 canon mount.

The host mounts the module under ``orders/api/``; this module contributes
the ``v1/`` segment:

    path("orders/api/", include("stapel_orders.urls"))   # -> /orders/api/v1/...
"""
from django.urls import include, path

from stapel_orders.urls_v1 import GATE_REGISTRY  # noqa: F401  (re-export)

urlpatterns = [
    path("v1/", include("stapel_orders.urls_v1")),
]
