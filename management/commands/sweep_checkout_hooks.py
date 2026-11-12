from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Retry unanswered checkout-hook commits/releases and release reservations "
        "older than STAPEL_ORDERS['CHECKOUT_HOOK_ORPHAN_SECONDS'] whose checkout "
        "never finished."
    )

    def add_arguments(self, parser):
        parser.add_argument("--orphan-seconds", type=int, default=None)

    def handle(self, *args, **options):
        from stapel_orders.services.hooks import sweep

        counts = sweep(orphan_seconds=options["orphan_seconds"])
        self.stdout.write(
            "committed {committed}, released {released}, orphans released "
            "{orphans_released}, unanswered {unanswered}".format(**counts)
        )
