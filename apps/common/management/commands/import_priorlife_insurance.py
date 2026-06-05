from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.common.services.priorlife_insurance import import_priorlife_files


class Command(BaseCommand):
	help = 'Import Priorlife life insurance data from contributions PDF and optional contract details.'

	def add_arguments(self, parser):
		parser.add_argument(
			'--contributions',
			type=str,
			required=True,
			help='Path to Priorlife contributions PDF (e.g. Priorlife_1.pdf).',
		)
		parser.add_argument(
			'--contract-date',
			type=str,
			default='',
			help='Contract signing date (DD.MM.YYYY).',
		)
		parser.add_argument(
			'--contract-load-pct',
			type=str,
			default='',
			help='Contract load percentage (e.g. 8).',
		)
		parser.add_argument(
			'--guaranteed-yield-pct',
			type=str,
			default='',
			help='Guaranteed yield percentage (e.g. 6).',
		)
		parser.add_argument(
			'--accrued-yield',
			type=str,
			default='',
			help='Accrued yield from Priorlife cabinet (informational; not added to gross premiums).',
		)
		parser.add_argument(
			'--accumulated-amount',
			type=str,
			default='',
			help='Current accumulated balance from Priorlife cabinet (source of truth for NAV).',
		)
		parser.add_argument(
			'--additional-accrued-yield',
			type=str,
			default='',
			help='Accrued additional yield / insurance bonus amount.',
		)
		parser.add_argument(
			'--premium-amount',
			type=str,
			default='',
			help='Scheduled premium amount (e.g. 25).',
		)
		parser.add_argument(
			'--premium-schedule',
			type=str,
			default='monthly',
			help='Premium schedule: monthly, quarterly, annual.',
		)
		parser.add_argument(
			'--insurance-type',
			type=str,
			default='life',
			help='Insurance type label from the contract (e.g. life).',
		)
		parser.add_argument(
			'--program',
			type=str,
			default='',
			help='Program code: zabota_o_buduschem, pro100, etc.',
		)

	def handle(self, *args, **options):
		contributions_path = Path(options['contributions']).resolve()
		if not contributions_path.exists():
			raise CommandError(f'Contributions file not found: {contributions_path}')

		contract_details = {
			key: value
			for key, value in {
				'contract_date': options.get('contract_date') or '',
				'contract_load_pct': options.get('contract_load_pct') or '',
				'guaranteed_yield_pct': options.get('guaranteed_yield_pct') or '',
				'accrued_yield': options.get('accrued_yield') or '',
				'accumulated_amount': options.get('accumulated_amount') or '',
				'additional_accrued_yield': options.get('additional_accrued_yield') or '',
				'premium_amount': options.get('premium_amount') or '',
				'premium_schedule': options.get('premium_schedule') or '',
				'insurance_type': options.get('insurance_type') or '',
				'program': options.get('program') or '',
				'contract_status': 'active',
			}.items()
			if value not in (None, '')
		}

		summary = import_priorlife_files(
			contributions_path=contributions_path,
			contract_details=contract_details or None,
		)

		self.stdout.write(
			self.style.SUCCESS(
				'Priorlife import completed: '
				f'account={summary.get("account_number") or "-"}, '
				f'records={summary["contribution_records"]}.'
			)
		)
