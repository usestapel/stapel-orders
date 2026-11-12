"""The schedules shipped by the library: the cart purge and the hook sweep.

The hook sweep retries unanswered checkout-hook commits and releases and
releases reservations of checkouts that never finished (``services/hooks.py``).
With no ``CHECKOUT_HOOKS`` registered it finds nothing.

Wire it in the settings module itself::

    from stapel_orders.beat import get_orders_beat_schedule

    CELERY_BEAT_SCHEDULE = {**get_orders_beat_schedule(), ...}

Imports settings and nothing else, so it is safe from a settings module.
Celery is optional: ``manage.py purge_carts`` and ``manage.py
sweep_checkout_hooks`` run the same passes from any scheduler.
"""
from __future__ import annotations

PURGE_TASK_NAME = "stapel_orders.beat.purge_carts"
PURGE_BEAT_KEY = "orders-purge-carts"
#: Daily. CART_TTL_DAYS is measured in days, so a finer cadence buys nothing.
PURGE_INTERVAL_SECONDS = 24 * 60 * 60

SWEEP_TASK_NAME = "stapel_orders.beat.sweep_checkout_hooks"
SWEEP_BEAT_KEY = "orders-sweep-checkout-hooks"
#: Every five minutes: well inside the default orphan age (900 s).
SWEEP_INTERVAL_SECONDS = 5 * 60


def purge_carts() -> int:
    """One purge pass. A plain callable for hosts without Celery."""
    from .services.cart import purge_carts as _purge

    return _purge()


def sweep_checkout_hooks() -> dict:
    """One hook sweep pass. A plain callable for hosts without Celery."""
    from .services.hooks import sweep

    return sweep()


def get_orders_beat_schedule(*, seconds: int = PURGE_INTERVAL_SECONDS,
                             sweep_seconds: int = SWEEP_INTERVAL_SECONDS) -> dict:
    """Beat entries for the cart purge and the hook sweep. Add to ``CELERY_BEAT_SCHEDULE``."""
    return {
        PURGE_BEAT_KEY: {"task": PURGE_TASK_NAME, "schedule": float(seconds)},
        SWEEP_BEAT_KEY: {"task": SWEEP_TASK_NAME, "schedule": float(sweep_seconds)},
    }


try:  # pragma: no cover - exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    purge_carts = shared_task(name=PURGE_TASK_NAME)(purge_carts)
    sweep_checkout_hooks = shared_task(name=SWEEP_TASK_NAME)(sweep_checkout_hooks)


__all__ = [
    "PURGE_BEAT_KEY",
    "PURGE_INTERVAL_SECONDS",
    "PURGE_TASK_NAME",
    "SWEEP_BEAT_KEY",
    "SWEEP_INTERVAL_SECONDS",
    "SWEEP_TASK_NAME",
    "get_orders_beat_schedule",
    "purge_carts",
    "sweep_checkout_hooks",
]
