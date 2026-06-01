from django.db import models
from django.utils.text import slugify

from apps.common.models import TimeStampedModel


class FinancialInstitution(TimeStampedModel):
	class InstitutionType(models.TextChoices):
		BANK = 'bank', 'Bank'
		BROKER = 'broker', 'Broker'
		CRYPTO_EXCHANGE = 'crypto_exchange', 'Crypto exchange'
		INSURANCE = 'insurance', 'Insurance'
		OTHER = 'other', 'Other'

	name = models.CharField(max_length=255, unique=True)
	slug = models.SlugField(max_length=255, unique=True, blank=True)
	institution_type = models.CharField(max_length=32, choices=InstitutionType.choices, default=InstitutionType.BANK)
	country = models.CharField(max_length=2, default='BY')
	website = models.URLField(blank=True)
	base_currency = models.ForeignKey('common.Currency', null=True, blank=True, on_delete=models.SET_NULL, related_name='institutions')
	notes = models.TextField(blank=True)
	metadata = models.JSONField(default=dict, blank=True)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ['name']
		verbose_name = 'Financial institution'
		verbose_name_plural = 'Financial institutions'

	def __str__(self) -> str:
		return self.name

	def save(self, *args, **kwargs):
		if not self.slug:
			self.slug = slugify(self.name)
		super().save(*args, **kwargs)
