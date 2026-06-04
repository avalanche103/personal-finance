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
			'maturity_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
			'next_income_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
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
