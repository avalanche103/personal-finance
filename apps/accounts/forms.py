from __future__ import annotations

from django import forms
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.accounts.querysets import portfolio_account_queryset
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
		labels = {
			'account': 'Счёт-отправитель',
			'related_account': 'Счёт-получатель',
		}
		help_texts = {
			'account': 'Для перевода — откуда уходят деньги. Для остальных операций — счёт операции.',
			'related_account': 'Только для перевода: куда зачисляются деньги.',
		}
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

		if (
			transaction_type == Transaction.TransactionType.WITHDRAWAL
			and product is not None
			and product.product_type == Product.ProductType.DEPOSIT
		):
			if product.income_account_id is None:
				self.add_error('product', 'This deposit product has no linked income account.')
			elif account is not None and account.pk != product.income_account_id:
				self.add_error('account', 'Record the deposit redemption on the linked income account for this product.')
			elif amount in (None, '') or amount == 0:
				self.add_error('amount', 'Deposit redemption amount must be non-zero.')

		if transaction_type == Transaction.TransactionType.TRANSFER:
			if related_account is None:
				self.add_error('related_account', 'Укажите счёт-получатель для перевода.')
			elif account is not None and related_account.pk == account.pk:
				self.add_error('related_account', 'Счёт-получатель должен отличаться от счёта-отправителя.')
			elif account is not None and related_account.currency_id != account.currency_id:
				self.add_error('related_account', 'Счёт-отправитель и счёт-получатель должны быть в одной валюте.')
			elif currency is not None and account is not None and currency.pk != account.currency_id:
				self.add_error('currency', 'Валюта перевода должна совпадать со счётом-отправителем.')
			elif amount in (None, '') or amount == 0:
				self.add_error('amount', 'Сумма перевода должна быть ненулевой.')
		elif related_account is not None:
			self.add_error('related_account', 'Счёт-получатель используется только для переводов.')

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
		holding_accounts = portfolio_account_queryset().order_by('institution__name', 'name')
		account_ids = set(holding_accounts.values_list('pk', flat=True))
		if self.instance and self.instance.pk:
			if self.instance.account_id:
				account_ids.add(self.instance.account_id)
			if self.instance.related_account_id:
				account_ids.add(self.instance.related_account_id)
		account_queryset = Account.objects.filter(pk__in=account_ids).select_related('institution', 'currency').order_by(
			'institution__name',
			'name',
		)
		self.fields['account'].queryset = account_queryset
		self.fields['related_account'].queryset = account_queryset
		self.fields['account'].empty_label = 'Выберите счёт-отправитель'
		self.fields['related_account'].empty_label = 'Выберите счёт-получатель'
		self.fields['amount'].help_text = (
			'Для Withdrawal можно указать 100 — сумма сохранится как −100.'
		)
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
