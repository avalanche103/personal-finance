from django.contrib import admin

from apps.accounts.models import Account, BalanceSnapshot, Transaction


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
	list_display = ('name', 'institution', 'account_type', 'currency', 'formatted_current_balance', 'formatted_current_balance_usd', 'is_active')
	list_filter = ('account_type', 'currency', 'is_active')
	search_fields = ('name', 'institution__name', 'external_id')

	@admin.display(description='Current balance')
	def formatted_current_balance(self, obj):
		return f'{obj.current_balance:.2f}'

	@admin.display(description='Current balance USD')
	def formatted_current_balance_usd(self, obj):
		return f'{obj.current_balance_usd:.2f}'


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
	list_display = ('occurred_at', 'account', 'transaction_type', 'formatted_amount', 'currency', 'formatted_amount_usd', 'import_job')
	list_filter = ('transaction_type', 'currency')
	search_fields = ('description', 'external_id', 'account__name', 'product__name')
	autocomplete_fields = ('account', 'related_account', 'product', 'import_job', 'currency')

	@admin.display(description='Amount')
	def formatted_amount(self, obj):
		return f'{obj.amount:.2f}'

	@admin.display(description='Amount USD')
	def formatted_amount_usd(self, obj):
		return f'{obj.amount_usd:.2f}'


@admin.register(BalanceSnapshot)
class BalanceSnapshotAdmin(admin.ModelAdmin):
	list_display = ('captured_at', 'institution', 'account', 'product', 'formatted_balance', 'formatted_balance_usd', 'currency')
	list_filter = ('currency',)
	search_fields = ('institution__name', 'account__name', 'product__name')
	autocomplete_fields = ('institution', 'account', 'product', 'currency')

	@admin.display(description='Balance')
	def formatted_balance(self, obj):
		if obj.product and obj.product.product_type == 'token':
			return f'{obj.balance:.0f}'
		return f'{obj.balance:.2f}'

	@admin.display(description='Balance USD')
	def formatted_balance_usd(self, obj):
		return f'{obj.balance_usd:.2f}'
