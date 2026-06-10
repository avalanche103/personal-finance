from __future__ import annotations

from django import forms
from django.utils import timezone

from apps.accounts.models import Account, Transaction
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
