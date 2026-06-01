from django.contrib import admin

from apps.products.models import Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
	list_display = ('name', 'institution', 'product_type', 'currency', 'formatted_units', 'formatted_current_price', 'formatted_current_value_usd', 'is_active')
	list_filter = ('product_type', 'currency', 'is_active')
	search_fields = ('name', 'symbol', 'isin', 'institution__name', 'external_id')

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
