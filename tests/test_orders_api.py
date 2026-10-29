"""Order history, detail, cancel and the staff transition."""
import pytest

from stapel_orders.models import Order

from .factories import make_order

ORDERS = "/orders/api/v1/orders/"

pytestmark = pytest.mark.django_db


class TestHistory:
    def test_newest_first_anchor_pages(self, auth_client, user):
        ids = [make_order(user).pk for _ in range(5)]
        page = auth_client.get(ORDERS + "?limit=2").json()
        assert [o["id"] for o in page["items"]] == ids[::-1][:2]
        assert page["has_next"] is True
        assert page["count"] == 2
        nxt = auth_client.get(f"{ORDERS}?limit=2&anchor={page['next_anchor']}").json()
        assert [o["id"] for o in nxt["items"]] == ids[::-1][2:4]

    def test_page_size_is_bounded(self, auth_client, user):
        for _ in range(3):
            make_order(user)
        page = auth_client.get(ORDERS + "?limit=100000").json()
        assert page["count"] == 3  # max_page_size caps the request, rows cap the page

    def test_status_filter_is_repeatable(self, auth_client, user):
        make_order(user, status="new")
        make_order(user, status="shipped")
        make_order(user, status="cancelled")
        page = auth_client.get(ORDERS + "?status=new&status=shipped").json()
        assert sorted(o["status"] for o in page["items"]) == ["new", "shipped"]

    def test_only_my_orders(self, auth_client, user, other_user):
        make_order(other_user)
        mine = make_order(user)
        assert [o["id"] for o in auth_client.get(ORDERS).json()["items"]] == [mine.pk]

    def test_empty_history(self, auth_client):
        page = auth_client.get(ORDERS).json()
        assert page["items"] == [] and page["has_next"] is False


class TestDetail:
    def test_detail(self, auth_client, user):
        order = make_order(user)
        body = auth_client.get(f"{ORDERS}{order.pk}/").json()
        assert body["number"] == order.number
        assert body["lines"][0]["purchasable_key"] == "k0"

    def test_foreign_id_is_404_exactly_like_an_absent_one(self, auth_client, other_user):
        foreign = make_order(other_user)
        a = auth_client.get(f"{ORDERS}{foreign.pk}/")
        b = auth_client.get(f"{ORDERS}999999/")
        assert a.status_code == b.status_code == 404
        assert a.json() == b.json()

    def test_staff_get_no_bypass_on_owner_views(self, client_for, staff, user):
        order = make_order(user)
        assert client_for(staff).get(f"{ORDERS}{order.pk}/").status_code == 404


class TestCancel:
    def test_owner_cancels_a_new_order(self, auth_client, user, captured_events):
        order = make_order(user)
        resp = auth_client.post(f"{ORDERS}{order.pk}/cancel/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
        assert resp.json()["cancellable"] is False
        changed = [e.payload for e in captured_events if e.event_type == "orders.order.status_changed"]
        assert changed == [{
            "order_id": str(order.pk), "user_id": str(user.pk),
            "from": "new", "to": "cancelled", "actor": "owner",
        }]

    def test_owner_cannot_cancel_a_confirmed_order(self, auth_client, user):
        order = make_order(user, status="confirmed")
        resp = auth_client.post(f"{ORDERS}{order.pk}/cancel/")
        assert resp.status_code == 409
        err = resp.json()
        assert err["localizable_error"] == "error.409.order_transition_invalid"
        assert err["params"] == {"from": "confirmed", "to": "cancelled"}

    def test_cancellable_statuses_are_a_setting(self, auth_client, user, settings):
        settings.STAPEL_ORDERS = {
            **settings.STAPEL_ORDERS, "OWNER_CANCELLABLE_STATUSES": ("new", "confirmed"),
        }
        order = make_order(user, status="confirmed")
        assert auth_client.get(f"{ORDERS}{order.pk}/").json()["cancellable"] is True
        assert auth_client.post(f"{ORDERS}{order.pk}/cancel/").status_code == 200

    def test_cannot_cancel_a_foreign_order(self, auth_client, other_user):
        order = make_order(other_user)
        assert auth_client.post(f"{ORDERS}{order.pk}/cancel/").status_code == 404
        assert Order.objects.get(pk=order.pk).status == "new"


class TestStaffTransition:
    def test_staff_walk_the_happy_path(self, client_for, staff, user):
        order = make_order(user)
        client = client_for(staff)
        for to in ("confirmed", "shipped", "completed"):
            resp = client.post(f"{ORDERS}{order.pk}/transition/", {"to": to}, format="json")
            assert resp.status_code == 200, resp.content
            assert resp.json()["status"] == to

    def test_non_staff_are_refused(self, auth_client, user):
        order = make_order(user)
        resp = auth_client.post(f"{ORDERS}{order.pk}/transition/", {"to": "confirmed"}, format="json")
        assert resp.status_code == 403

    def test_guest_is_refused(self, client_for, guest, user):
        order = make_order(user)
        resp = client_for(guest).post(
            f"{ORDERS}{order.pk}/transition/", {"to": "confirmed"}, format="json"
        )
        assert resp.status_code == 403

    def test_unknown_order_is_404(self, client_for, staff):
        resp = client_for(staff).post(f"{ORDERS}424242/transition/", {"to": "confirmed"}, format="json")
        assert resp.status_code == 404
