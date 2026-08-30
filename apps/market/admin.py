from django.contrib import admin

from apps.market.models import DepositBank, DepositOffer


@admin.register(DepositBank)
class DepositBankAdmin(admin.ModelAdmin):
	list_display = (
		'name',
		'short_name',
		'entity_type',
		'parser_code',
		'swift',
		'is_active',
		'last_synced_at',
	)
	list_filter = ('entity_type', 'is_active', 'parser_code')
	search_fields = ('name', 'short_name', 'reg_number', 'swift', 'external_key')
	raw_id_fields = ('institution',)
	prepopulated_fields = {'slug': ('short_name',)}


@admin.register(DepositOffer)
class DepositOfferAdmin(admin.ModelAdmin):
	list_display = (
		'name',
		'bank',
		'currency',
		'rate_pct',
		'rate_pct_max',
		'term_text',
		'is_irrevocable',
		'is_active',
		'scraped_at',
	)
	list_filter = ('currency', 'is_active', 'is_irrevocable', 'bank')
	search_fields = ('name', 'external_id', 'bank__name')
	raw_id_fields = ('bank',)
