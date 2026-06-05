from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.common.services.stravita_pension import import_stravita_pension_files


class Command(BaseCommand):
	help = 'Import Stravita DNPS pension data from statement and contributions PDF files.'

	def add_arguments(self, parser):
		parser.add_argument(
			'--extract',
			type=str,
			help='Path to Stravita account statement PDF (policy_pension_extract.pdf).',
		)
		parser.add_argument(
			'--contributions',
			type=str,
			help='Path to Stravita contributions PDF (policy_pension_contributions.pdf).',
		)
		parser.add_argument(
			'--management-expense-pct',
			type=str,
			default='5.7',
			help='Management expense percentage stored on the product metadata.',
		)

	def handle(self, *args, **options):
		extract_path = Path(options['extract']).resolve() if options.get('extract') else None
		contributions_path = Path(options['contributions']).resolve() if options.get('contributions') else None

		if extract_path is None and contributions_path is None:
			raise CommandError('Provide at least one of --extract or --contributions.')

		for label, path in (('extract', extract_path), ('contributions', contributions_path)):
			if path is not None and not path.exists():
				raise CommandError(f'{label} file not found: {path}')

		summary = import_stravita_pension_files(
			extract_path=extract_path,
			contributions_path=contributions_path,
			management_expense_pct=Decimal(str(options['management_expense_pct'])),
		)

		self.stdout.write(
			self.style.SUCCESS(
				'Stravita pension import completed: '
				f'account={summary.get("account_number") or "-"}, '
				f'extract_records={summary["extract_records"]}, '
				f'contribution_records={summary["contribution_records"]}.'
			)
		)
