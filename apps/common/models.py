from django.db import models


class TimeStampedModel(models.Model):
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		abstract = True


class Currency(TimeStampedModel):
	code = models.CharField(max_length=3, unique=True)
	name = models.CharField(max_length=64)
	symbol = models.CharField(max_length=8, blank=True)
	usd_rate = models.DecimalField(max_digits=18, decimal_places=6, default=1)
	is_base = models.BooleanField(default=False)
	metadata = models.JSONField(default=dict, blank=True)

	class Meta:
		ordering = ['code']
		verbose_name = 'Currency'
		verbose_name_plural = 'Currencies'

	def __str__(self) -> str:
		return f'{self.code} - {self.name}'


class ExchangeRateHistory(TimeStampedModel):
	class Source(models.TextChoices):
		NBRB = 'nbrb', 'NBRB API'

	currency = models.ForeignKey(Currency, on_delete=models.CASCADE, related_name='rate_history')
	rate_date = models.DateField()
	rate_byn = models.DecimalField(max_digits=18, decimal_places=6)
	usd_cross_rate = models.DecimalField(max_digits=18, decimal_places=10)
	scale = models.PositiveIntegerField(default=1)
	source = models.CharField(max_length=16, choices=Source.choices, default=Source.NBRB)
	source_currency_id = models.PositiveIntegerField()
	payload = models.JSONField(default=dict, blank=True)

	class Meta:
		ordering = ['-rate_date', 'currency__code']
		constraints = [
			models.UniqueConstraint(fields=['currency', 'rate_date', 'source'], name='unique_exchange_rate_per_day_source'),
		]
		verbose_name = 'Exchange rate history'
		verbose_name_plural = 'Exchange rate history'

	def __str__(self) -> str:
		from apps.common.dates import format_display_date

		return f'{self.currency.code} @ {format_display_date(self.rate_date)}'
