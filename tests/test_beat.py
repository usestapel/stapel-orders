"""The purge ships with its schedule; Celery stays optional."""
import datetime

import pytest
from django.core.management import call_command
from django.utils import timezone

from stapel_orders.beat import PURGE_BEAT_KEY, PURGE_TASK_NAME, get_orders_beat_schedule
from stapel_orders.models import Cart
from stapel_orders.services import cart as cart_service


def test_schedule_names_the_task():
    entry = get_orders_beat_schedule()[PURGE_BEAT_KEY]
    assert entry["task"] == PURGE_TASK_NAME
    assert entry["schedule"] == 86400.0


@pytest.mark.django_db
def test_purge_removes_only_stale_carts(user, other_user):
    old = Cart.objects.create(user_id=str(user.pk))
    Cart.objects.filter(pk=old.pk).update(updated_at=timezone.now() - datetime.timedelta(days=31))
    Cart.objects.create(user_id=str(other_user.pk))
    assert cart_service.purge_carts() == 1
    assert list(Cart.objects.values_list("user_id", flat=True)) == [str(other_user.pk)]


@pytest.mark.django_db
def test_management_command(user, capsys):
    old = Cart.objects.create(user_id=str(user.pk))
    Cart.objects.filter(pk=old.pk).update(updated_at=timezone.now() - datetime.timedelta(days=2))
    call_command("purge_carts", "--days", "1")
    assert "purged 1" in capsys.readouterr().out
