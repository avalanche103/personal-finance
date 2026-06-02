from django.core.management.base import BaseCommand

from apps.common.services.finstore_reconciliation import reconcile_finstore_products


class Command(BaseCommand):
    help = 'Backfill Finstore product links and recalculate token positions from imported transactions.'

    def handle(self, *args, **options):
        result = reconcile_finstore_products()
        self.stdout.write(
            self.style.SUCCESS(
                'Finstore reconciliation completed. '
                f"products_updated={result['products_updated']} "
                f"transactions_linked={result['transactions_linked']} "
                f"normalized_transactions={result['normalized_transactions']}"
            )
        )