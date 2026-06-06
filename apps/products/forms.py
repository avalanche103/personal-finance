import json

from django import forms
from django.core.exceptions import ValidationError

from apps.common.services.ledger import create_product
from apps.products.models import Product


DEPOSIT_INTEREST_MODE_CHOICES = [
	('', '— Not set —'),
	('payout', 'Pay interest to account'),
	('capitalized', 'Capitalize interest in deposit'),
]


def income_schedule_choices() -> list[tuple[str, str]]:
	return [('', '— Not set —')] + list(Product.IncomeSchedule.choices)


class DepositMetadataMixin:
	def _deposit_metadata(self) -> dict:
		metadata = {}
		if getattr(self.instance, 'pk', None) and isinstance(self.instance.metadata, dict):
			metadata = dict(self.instance.metadata)
		raw_metadata = self.cleaned_data.get('metadata')
		if isinstance(raw_metadata, dict):
			metadata.update(raw_metadata)
		elif isinstance(raw_metadata, str) and raw_metadata.strip():
			try:
				metadata.update(json.loads(raw_metadata))
			except json.JSONDecodeError as exc:
				raise ValidationError({'metadata': 'Metadata must be valid JSON.'}) from exc
		interest_mode = self.cleaned_data.get('interest_mode', '')
		if interest_mode:
			metadata['interest_mode'] = interest_mode
		else:
			metadata.pop('interest_mode', None)
		return metadata

	def _init_interest_mode_field(self):
		if 'interest_mode' not in self.fields:
			return
		self.fields['interest_mode'].widget.attrs.setdefault('class', 'form-control')
		if getattr(self.instance, 'pk', None):
			metadata = self.instance.metadata if isinstance(self.instance.metadata, dict) else {}
			self.fields['interest_mode'].initial = metadata.get('interest_mode', '')

	def clean(self):
		cleaned_data = super().clean()
		product_type = cleaned_data.get('product_type')
		if product_type is None and getattr(self.instance, 'pk', None):
			product_type = self.instance.product_type
		if product_type == Product.ProductType.DEPOSIT:
			if not cleaned_data.get('interest_mode'):
				self.add_error('interest_mode', 'Select how interest is handled for a deposit.')
			elif cleaned_data.get('interest_mode') == 'payout' and not cleaned_data.get('income_account'):
				self.add_error('income_account', 'Select the account that receives interest payments.')
		return cleaned_data


class ProductForm(DepositMetadataMixin, forms.ModelForm):
	interest_mode = forms.ChoiceField(
		required=False,
		choices=DEPOSIT_INTEREST_MODE_CHOICES,
		label='Interest mode',
		help_text='How deposit interest is handled: paid to a bank account or added to principal.',
	)
	class Meta:
		model = Product
		fields = (
			'institution',
			'income_account',
			'name',
			'symbol',
			'isin',
			'product_type',
			'currency',
			'units',
			'current_price',
			'external_id',
			'is_active',
			'annual_rate_pct',
			'maturity_date',
			'income_schedule',
			'next_income_date',
		)
		widgets = {
			'maturity_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
			'next_income_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
		}

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._init_interest_mode_field()
		for field_name in (
			'income_account',
			'symbol',
			'isin',
			'external_id',
			'annual_rate_pct',
			'maturity_date',
			'income_schedule',
			'next_income_date',
			'interest_mode',
		):
			self.fields[field_name].required = False
		self.fields['income_schedule'].choices = income_schedule_choices()
		for field in self.fields.values():
			field.widget.attrs.setdefault('class', 'form-control')
		self.order_fields(
			[
				'institution',
				'name',
				'product_type',
				'currency',
				'income_account',
				'interest_mode',
				'units',
				'current_price',
				'symbol',
				'isin',
				'external_id',
				'annual_rate_pct',
				'maturity_date',
				'income_schedule',
				'next_income_date',
				'is_active',
			]
		)

	def save(self, commit=True):
		if not commit:
			return super().save(commit=False)
		cleaned_data = dict(self.cleaned_data)
		metadata = self._deposit_metadata() if cleaned_data.get('product_type') == Product.ProductType.DEPOSIT else {}
		cleaned_data.pop('interest_mode', None)
		cleaned_data['metadata'] = metadata
		return create_product(**cleaned_data)


class ProductTokenTermsForm(forms.ModelForm):
	class Meta:
		model = Product
		fields = (
			'annual_rate_pct',
			'maturity_date',
			'income_schedule',
			'next_income_date',
		)
		labels = {
			'annual_rate_pct': 'Annual rate (%)',
			'maturity_date': 'Maturity date',
			'income_schedule': 'Income schedule',
			'next_income_date': 'Next income date',
		}
		widgets = {
			'annual_rate_pct': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'placeholder': 'e.g. 8.5'}),
			'maturity_date': forms.DateInput(
				format='%d.%m.%Y',
				attrs={'placeholder': 'дд.мм.гггг', 'inputmode': 'numeric'},
			),
			'next_income_date': forms.DateInput(
				format='%d.%m.%Y',
				attrs={'placeholder': 'дд.мм.гггг', 'inputmode': 'numeric'},
			),
		}

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields['income_schedule'].choices = income_schedule_choices()
		self.fields['annual_rate_pct'].required = False
		self.fields['maturity_date'].required = False
		self.fields['income_schedule'].required = False
		self.fields['next_income_date'].required = False
		for field in self.fields.values():
			field.widget.attrs.setdefault('class', 'form-control')


class ProductDepositTermsForm(DepositMetadataMixin, ProductTokenTermsForm):
	interest_mode = forms.ChoiceField(
		required=False,
		choices=DEPOSIT_INTEREST_MODE_CHOICES,
		label='Interest mode',
		help_text='How deposit interest is handled: paid to a bank account or added to principal.',
	)
	income_account = forms.ModelChoiceField(
		queryset=None,
		required=False,
		label='Interest account',
		help_text='Bank account that receives interest when payout mode is selected.',
	)

	class Meta(ProductTokenTermsForm.Meta):
		fields = ProductTokenTermsForm.Meta.fields + ('income_account',)

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._init_interest_mode_field()
		from apps.accounts.models import Account

		self.fields['income_account'].queryset = Account.objects.select_related('institution', 'currency').order_by(
			'institution__name',
			'name',
		)
		self.fields['income_account'].widget.attrs.setdefault('class', 'form-control')
		if getattr(self.instance, 'pk', None):
			self.fields['income_account'].initial = self.instance.income_account_id

	def clean(self):
		cleaned_data = super().clean()
		if cleaned_data.get('interest_mode') == 'payout' and not cleaned_data.get('income_account'):
			self.add_error('income_account', 'Select the account that receives interest payments.')
		return cleaned_data

	def save(self, commit=True):
		product = super(ProductTokenTermsForm, self).save(commit=False)
		product.income_account = self.cleaned_data.get('income_account')
		product.metadata = self._deposit_metadata()
		if commit:
			product.save()
		return product


class ProductIncomeCalendarForm(forms.Form):
	enabled = forms.BooleanField(required=False, label='Enable payment calendar')
	coupon_day = forms.IntegerField(
		required=False,
		min_value=1,
		max_value=31,
		label='Coupon day of month',
		widget=forms.NumberInput(attrs={'min': '1', 'max': '31', 'placeholder': '8'}),
	)
	schedule_start_date = forms.DateField(
		required=False,
		label='First coupon date',
		widget=forms.DateInput(
			format='%d.%m.%Y',
			attrs={'placeholder': 'дд.мм.гггг', 'inputmode': 'numeric'},
		),
	)

	def __init__(self, *args, product: Product | None = None, **kwargs):
		super().__init__(*args, **kwargs)
		if product is None:
			return
		from apps.common.services.indexed_bonds import get_income_calendar_config, resolve_schedule_start_date

		config = get_income_calendar_config(product)
		self.fields['enabled'].initial = bool(config.get('enabled'))
		self.fields['coupon_day'].initial = config.get('coupon_day') or (
			product.next_income_date.day if product.next_income_date else None
		)
		start_date = resolve_schedule_start_date(product)
		if start_date is not None:
			self.fields['schedule_start_date'].initial = start_date
		for field in self.fields.values():
			field.widget.attrs.setdefault('class', 'form-control')
