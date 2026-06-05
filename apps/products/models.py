from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel


class Product(TimeStampedModel):
	class ProductType(models.TextChoices):
		TOKEN = 'token', 'Token'
		STOCK = 'stock', 'Stock'
		BOND = 'bond', 'Bond'
		CRYPTO = 'crypto', 'Crypto'
		CFD = 'cfd', 'CFD'
		DEPOSIT = 'deposit', 'Deposit'
		ETF = 'etf', 'ETF'
		PENSION = 'pension', 'Pension'
		LIFE_INSURANCE = 'life_insurance', 'Life insurance'
		OTHER = 'other', 'Other'

	class IncomeSchedule(models.TextChoices):
		MONTHLY = 'monthly', 'Monthly'
		QUARTERLY = 'quarterly', 'Quarterly'
		SEMI_ANNUAL = 'semi_annual', 'Semi-annual'
		ANNUAL = 'annual', 'Annual'
		AT_MATURITY = 'at_maturity', 'At maturity'
		OTHER = 'other', 'Other'

	institution = models.ForeignKey('institutions.FinancialInstitution', on_delete=models.CASCADE, related_name='products')
	income_account = models.ForeignKey(
		'accounts.Account',
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='income_products',
	)
	name = models.CharField(max_length=255)
	symbol = models.CharField(max_length=32, blank=True)
	isin = models.CharField(max_length=32, blank=True)
	product_type = models.CharField(max_length=32, choices=ProductType.choices, default=ProductType.OTHER)
	currency = models.ForeignKey('common.Currency', on_delete=models.PROTECT, related_name='products')
	units = models.DecimalField(max_digits=20, decimal_places=6, default=0)
	current_price = models.DecimalField(max_digits=20, decimal_places=8, default=0)
	current_value_usd = models.DecimalField(max_digits=20, decimal_places=2, default=0)
	external_id = models.CharField(max_length=128, blank=True)
	metadata = models.JSONField(default=dict, blank=True)
	is_active = models.BooleanField(default=True)
	annual_rate_pct = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
	maturity_date = models.DateField(null=True, blank=True)
	income_schedule = models.CharField(
		max_length=16,
		choices=IncomeSchedule.choices,
		blank=True,
		default='',
	)
	next_income_date = models.DateField(null=True, blank=True)
	terms_updated_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		ordering = ['name']
		constraints = [
			models.UniqueConstraint(
				fields=['institution', 'external_id'],
				condition=~Q(external_id=''),
				name='unique_product_external_id_per_institution',
			),
		]

	def __str__(self) -> str:
		return self.name

	@property
	def market_value(self):
		return self.units * self.current_price

	@property
	def bond_kind_display(self) -> str:
		if self.product_type != self.ProductType.BOND:
			return ''
		labels = {
			'indexed': 'Indexed bond',
		}
		kind = ''
		if isinstance(self.metadata, dict):
			kind = str(self.metadata.get('bond_kind', '')).strip()
		return labels.get(kind, '')

	@property
	def is_pension_product(self) -> bool:
		return self.product_type == self.ProductType.PENSION

	@property
	def is_life_insurance_product(self) -> bool:
		return self.product_type == self.ProductType.LIFE_INSURANCE

	@property
	def is_unit_valued_insurance_product(self) -> bool:
		return self.is_pension_product or self.is_life_insurance_product

	@property
	def pension_program_display(self) -> str:
		if not self.is_pension_product:
			return ''
		if isinstance(self.metadata, dict):
			program = str(self.metadata.get('program', '')).strip()
			if program == 'dnps_state':
				return 'ДНПС с участием государства'
		return 'Pension'

	@property
	def life_insurance_program_display(self) -> str:
		if not self.is_life_insurance_product:
			return ''
		if isinstance(self.metadata, dict):
			program = str(self.metadata.get('program', '')).strip()
			labels = {
				'zabota_o_buduschem': 'Забота о будущем',
				'zabota_kompleks': 'Забота о будущем — комплекс',
				'pro100': 'Pro100',
				'pro75': 'Pro75',
				'pension_capital': 'Пенсионный капитал',
			}
			if program in labels:
				return labels[program]
			insurance_type = str(self.metadata.get('insurance_type', '')).strip()
			if insurance_type:
				return insurance_type
		return 'Life insurance'

	@property
	def finstore_token_id(self) -> str:
		if isinstance(self.metadata, dict) and self.metadata.get('token_id'):
			return str(self.metadata['token_id'])
		if self.external_id and '_' in self.external_id:
			suffix = self.external_id.rsplit('_', 1)[-1]
			if suffix.endswith(')'):
				return suffix[:-1]
		return ''
