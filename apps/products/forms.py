from django import forms



from apps.products.models import Product





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

		schedule_choices = [('', '— Not set —')] + list(Product.IncomeSchedule.choices)

		self.fields['income_schedule'].choices = schedule_choices

		self.fields['annual_rate_pct'].required = False

		self.fields['maturity_date'].required = False

		self.fields['income_schedule'].required = False

		self.fields['next_income_date'].required = False

		for field in self.fields.values():

			field.widget.attrs.setdefault('class', 'form-control')





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

		self.fields['coupon_day'].initial = config.get('coupon_day') or (product.next_income_date.day if product.next_income_date else None)

		start_date = resolve_schedule_start_date(product)

		if start_date is not None:

			self.fields['schedule_start_date'].initial = start_date

		for field in self.fields.values():

			field.widget.attrs.setdefault('class', 'form-control')


