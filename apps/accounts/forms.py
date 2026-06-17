from __future__ import annotations

from django import forms
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.products.models import Product
from apps.common.services.ledger import create_account, create_transaction, update_transaction


class AccountForm(forms.ModelForm):
	class Meta:
		model = Account
		fields = (
			'institution',
			'name',
			'account_type',
			'currency',
			'external_id',
			'current_balance',
			'metadata',
			'is_active',
		)
		widgets = {
			'metadata': forms.Textarea(attrs={'rows': 4, 'placeholder': '{"source": "manual"}'}),
		}

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields['external_id'].required = False
		self.fields['metadata'].required = False
		for field in self.fields.values():
			field.widget.attrs.setdefault('class', 'form-control')

	def save(self, commit=True):
		if not commit:
			return super().save(commit=False)
		return create_account(**self.cleaned_data)


class TransactionForm(forms.ModelForm):
	class Meta:
		model = Transaction
		fields = (
			'account',
			'related_account',
			'product',
			'transaction_type',
			'currency',
			'external_id',
			'amount',
			'quantity',
			'unit_price',
			'occurred_at',
			'description',
			'metadata',
		)
		widgets = {
			'occurred_at': forms.DateTimeInput(
				format='%Y-%m-%dT%H:%M',
				attrs={'type': 'datetime-local'},
			),
			'description': forms.Textarea(attrs={'rows': 3}),
			'metadata': forms.Textarea(attrs={'rows': 4, 'placeholder': '{"source": "manual"}'}),
		}

	def clean(self):
		cleaned_data = super().clean()
		transaction_type = cleaned_data.get('transaction_type')
		account = cleaned_data.get('account')
		related_account = cleaned_data.get('related_account')
		product = cleaned_data.get('product')
		currency = cleaned_data.get('currency')
		amount = cleaned_data.get('amount')

		if (
			transaction_type == Transaction.TransactionType.DEPOSIT
			and product is not None
			and product.product_type == Product.ProductType.DEPOSIT
		):
			if product.income_account_id is None:
				self.add_error('product', 'This deposit product has no linked income account.')
			elif account is not None and account.pk != product.income_account_id:
				self.add_error('account', 'Record the deposit on the linked income account for this product.')
			elif amount in (None, '') or amount == 0:
				self.add_error('amount', 'Deposit amount must be non-zero.')

		if transaction_type == Transaction.TransactionType.TRANSFER:
			if related_account is None:
				self.add_error('related_account', 'Select the destination account for a transfer.')
			elif account is not None and related_account.pk == account.pk:
				self.add_error('related_account', 'Transfer destination must be different from the source account.')
			elif account is not None and related_account.currency_id != account.currency_id:
				self.add_error('related_account', 'Transfer accounts must use the same currency.')
			elif currency is not None and account is not None and currency.pk != account.currency_id:
				self.add_error('currency', 'Transfer currency must match the source account currency.')
			elif amount in (None, '') or amount == 0:
				self.add_error('amount', 'Transfer amount must be non-zero.')
		elif related_account is not None:
			self.add_error('related_account', 'Related account is only used for transfers.')

		return cleaned_data

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields['related_account'].required = False
		self.fields['product'].required = False
		self.fields['external_id'].required = False
		self.fields['quantity'].required = False
		self.fields['unit_price'].required = False
		self.fields['description'].required = False
		self.fields['metadata'].required = False
		self.fields['occurred_at'].input_formats = ['%Y-%m-%dT%H:%M']
		for field in self.fields.values():
			field.widget.attrs.setdefault('class', 'form-control')

	def save(self, commit=True):
		if not commit:
			return super().save(commit=False)
		if self.instance and self.instance.pk:
			metadata = self.cleaned_data.get('metadata') or {}
			if self.instance.import_job_id or not self.instance.import_fingerprint.startswith('manual:'):
				metadata = {
					**metadata,
					'manual_override': True,
					'manual_override_at': timezone.now().isoformat(),
				}
			return update_transaction(self.instance, **{**self.cleaned_data, 'metadata': metadata})
		return create_transaction(**self.cleaned_data)
