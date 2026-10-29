"""The schedule that purges abandoned carts — shipped by the library.

Wire it in the settings module itself::

    from stapel_orders.beat import get_orders_beat_schedule

    CELERY_BEAT_SCHEDULE = {**get_orders_beat_schedule(), ...}

Imports settings and nothing else, so it is safe from a settings module.
Celery is optional: ``manage.py purge_carts`` runs the same pass from any
scheduler.
"""
from __future__ import annotations

PURGE_TASK_NAME = "stapel_orders.beat.purge_carts"
PURGE_BEAT_KEY = "orders-purge-carts"
#: Daily. CART_TTL_DAYS is measured in days, so a finer cadence buys nothing.
PURGE_INTERVAL_SECONDS = 24 * 60 * 60


def purge_carts() -> int:
    """One purge pass. A plain callable for hosts without Celery."""
    from .services.cart import purge_carts as _purge

    return _purge()


def get_orders_beat_schedule(*, seconds: int = PURGE_INTERVAL_SECONDS) -> dict:
    """Beat entry for the cart purge. Add to ``CELERY_BEAT_SCHEDULE``."""
    return {PURGE_BEAT_KEY: {"task": PURGE_TASK_NAME, "schedule": float(seconds)}}


try:  # pragma: no cover - exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    purge_carts = shared_task(name=PURGE_TASK_NAME)(purge_carts)


__all__ = [
    "PURGE_BEAT_KEY",
    "PURGE_INTERVAL_SECONDS",
    "PURGE_TASK_NAME",
    "get_orders_beat_schedule",
    "purge_carts",
]
