from django.contrib import admin

from apps.common.models import Currency, ExchangeRateHistory


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
	list_display = ('code', 'name', 'symbol', 'usd_rate', 'is_base')
	list_filter = ('is_base',)
	search_fields = ('code', 'name')


@admin.register(ExchangeRateHistory)
class ExchangeRateHistoryAdmin(admin.ModelAdmin):
	list_display = ('rate_date', 'currency', 'rate_byn', 'usd_cross_rate', 'scale', 'source')
	list_filter = ('source', 'currency__code')
	search_fields = ('currency__code', 'currency__name')
	date_hierarchy = 'rate_date'
