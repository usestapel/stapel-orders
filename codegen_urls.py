"""Canonical-prefix URLconf for contract emission: the documented host mount."""
from django.urls import include, path

urlpatterns = [
    path("orders/api/", include("stapel_orders.urls")),
]
