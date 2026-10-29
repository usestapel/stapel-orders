from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete carts untouched for STAPEL_ORDERS['CART_TTL_DAYS'] days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None)

    def handle(self, *args, **options):
        from stapel_orders.services.cart import purge_carts

        removed = purge_carts(options["days"])
        self.stdout.write(f"purged {removed} cart(s)")
