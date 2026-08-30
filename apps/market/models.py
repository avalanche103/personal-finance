from django.db import models
from django.utils.text import slugify

from apps.common.models import TimeStampedModel


class DepositBank(TimeStampedModel):
	class EntityType(models.TextChoices):
		BANK = 'bank', 'Bank'
		NKFO = 'nkfo', 'Non-bank credit organization'
		OTHER = 'other', 'Other'

	name = models.CharField(max_length=512)
	short_name = models.CharField(max_length=255, blank=True)
	slug = models.SlugField(max_length=255, unique=True, blank=True)
	reg_number = models.CharField(max_length=64, blank=True, db_index=True)
	swift = models.CharField(max_length=32, blank=True)
	address = models.CharField(max_length=512, blank=True)
	phone = models.CharField(max_length=255, blank=True)
	website = models.URLField(blank=True)
	nbrb_url = models.URLField(blank=True)
	external_key = models.CharField(max_length=128, unique=True)
	entity_type = models.CharField(
		max_length=16,
		choices=EntityType.choices,
		default=EntityType.BANK,
	)
	parser_code = models.CharField(
		max_length=64,
		blank=True,
		help_text='Deposit offers adapter code (empty = no rate sync).',
	)
	institution = models.ForeignKey(
		'institutions.FinancialInstitution',
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='deposit_market_banks',
	)
	is_active = models.BooleanField(default=True)
	last_synced_at = models.DateTimeField(null=True, blank=True)
	metadata = models.JSONField(default=dict, blank=True)

	class Meta:
		ordering = ['name']
		verbose_name = 'Deposit bank'
		verbose_name_plural = 'Deposit banks'

	def __str__(self) -> str:
		return self.short_name or self.name

	def save(self, *args, **kwargs):
		if not self.slug:
			base = self.short_name or self.name
			self.slug = slugify(base) or slugify(self.external_key) or 'bank'
		original = self.slug
		suffix = 2
		while DepositBank.objects.filter(slug=self.slug).exclude(pk=self.pk).exists():
			self.slug = f'{original}-{suffix}'
			suffix += 1
		super().save(*args, **kwargs)


class DepositOffer(TimeStampedModel):
	bank = models.ForeignKey(DepositBank, on_delete=models.CASCADE, related_name='offers')
	name = models.CharField(max_length=512)
	currency = models.CharField(max_length=8, db_index=True)
	rate_pct = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
	rate_pct_max = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
	term_text = models.CharField(max_length=255, blank=True)
	term_days_min = models.PositiveIntegerField(null=True, blank=True)
	term_days_max = models.PositiveIntegerField(null=True, blank=True)
	min_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
	is_irrevocable = models.BooleanField(null=True, blank=True)
	source_url = models.URLField(blank=True)
	external_id = models.CharField(max_length=255)
	scraped_at = models.DateTimeField()
	raw = models.JSONField(default=dict, blank=True)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ['-rate_pct_max', '-rate_pct', 'bank__name', 'name']
		verbose_name = 'Deposit offer'
		verbose_name_plural = 'Deposit offers'
		constraints = [
			models.UniqueConstraint(
				fields=('bank', 'external_id'),
				name='market_depositoffer_bank_external_uniq',
			),
		]
		indexes = [
			models.Index(fields=('is_active', 'currency')),
			models.Index(fields=('bank', 'is_active')),
		]

	def __str__(self) -> str:
		return f'{self.bank}: {self.name} ({self.currency})'

	@property
	def display_rate(self) -> str:
		if self.rate_pct is None and self.rate_pct_max is None:
			return '—'

		def fmt(value) -> str:
			text = format(value.normalize(), 'f')
			if '.' in text:
				text = text.rstrip('0').rstrip('.')
			return text

		if self.rate_pct is None and self.rate_pct_max is not None:
			return f'до {fmt(self.rate_pct_max)}%'
		if self.rate_pct_max is not None and self.rate_pct != self.rate_pct_max:
			return f'{fmt(self.rate_pct)}–{fmt(self.rate_pct_max)}%'
		return f'{fmt(self.rate_pct)}%'
