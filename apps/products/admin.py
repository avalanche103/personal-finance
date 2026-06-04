from django.contrib import admin

from apps.products.models import Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
	list_display = (
		'name',
		'institution',
		'product_type',
		'currency',
		'annual_rate_pct',
		'maturity_date',
		'next_income_date',
		'formatted_units',
		'formatted_current_value_usd',
		'is_active',
	)
	list_filter = ('product_type', 'currency', 'is_active', 'income_schedule', 'institution')
	search_fields = ('name', 'symbol', 'isin', 'institution__name', 'external_id')
	fieldsets = (
		(None, {
			'fields': (
				'institution',
				'name',
				'symbol',
				'isin',
				'external_id',
				'product_type',
				'currency',
				'is_active',
			),
		}),
		('Position', {
			'fields': ('units', 'current_price', 'current_value_usd'),
		}),
		('Token terms', {
			'fields': (
				'annual_rate_pct',
				'maturity_date',
				'income_schedule',
				'next_income_date',
				'terms_updated_at',
			),
		}),
		('Metadata', {
			'fields': ('metadata',),
		}),
	)
	readonly_fields = ('terms_updated_at',)

	@admin.display(description='Units')
	def formatted_units(self, obj):
		if obj.product_type == Product.ProductType.TOKEN:
			return f'{obj.units:.0f}'
		return f'{obj.units:.2f}'

	@admin.display(description='Current price')
	def formatted_current_price(self, obj):
		return f'{obj.current_price:.2f}'

	@admin.display(description='Value USD')
	def formatted_current_value_usd(self, obj):
		return f'{obj.current_value_usd:.2f}'
