"""Admin for stapel-orders.

Money and snapshots are read-only; status moves only through the service
(transition actions), never a raw field edit, so every move is checked by
the FSM and announced on the bus.
"""
from django.contrib import admin, messages

from .models import Cart, CartLine, Order, OrderAdjustment, OrderLine, OrderStatus


class OrderLineInline(admin.TabularInline):
    model = OrderLine
    extra = 0
    can_delete = False
    fields = ("purchasable_type", "purchasable_key", "title", "quantity",
              "unit_price", "line_total", "seller_id")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class OrderAdjustmentInline(admin.TabularInline):
    model = OrderAdjustment
    extra = 0
    can_delete = False
    fields = ("position", "adjuster", "kind", "code", "label", "amount", "line_index")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


def _transition_action(to_status: str):
    def action(modeladmin, request, queryset):
        from .services import OrdersError
        from .services.orders import staff_transition

        moved = refused = 0
        for order in queryset:
            try:
                staff_transition(order.pk, to_status, reason="admin")
                moved += 1
            except OrdersError:
                refused += 1
        if moved:
            modeladmin.message_user(request, f"{moved} order(s) moved to {to_status}.")
        if refused:
            modeladmin.message_user(
                request, f"{refused} order(s) cannot move to {to_status}.", messages.WARNING
            )

    action.__name__ = f"mark_{to_status}"
    action.short_description = f"Mark selected orders {to_status}"
    return action


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "payment_status", "total", "currency", "created_at")
    list_filter = ("status", "payment_status", "currency")
    search_fields = ("number", "user_id")
    inlines = [OrderLineInline, OrderAdjustmentInline]
    readonly_fields = (
        "number", "user_id", "status", "payment_status", "currency", "subtotal",
        "total", "total_base", "base_currency", "provider_ref", "contact_name",
        "contact_phone", "contact_email", "address_line1", "address_line2",
        "address_city", "address_region", "address_postal_code", "address_country",
        "shipping_method", "comment", "created_at", "updated_at",
    )
    actions = [
        _transition_action(OrderStatus.CONFIRMED),
        _transition_action(OrderStatus.SHIPPED),
        _transition_action(OrderStatus.COMPLETED),
        _transition_action(OrderStatus.CANCELLED),
    ]

    def has_add_permission(self, request):
        return False


class CartLineInline(admin.TabularInline):
    model = CartLine
    extra = 0
    fields = ("purchasable_type", "purchasable_key", "title", "quantity", "unit_price", "line_total")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("user_id", "currency", "updated_at")
    search_fields = ("user_id",)
    readonly_fields = ("user_id", "currency", "created_at", "updated_at")
    inlines = [CartLineInline]
