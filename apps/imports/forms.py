from django import forms

from apps.imports.models import ImportSource


class ImportUploadForm(forms.Form):
    source = forms.ModelChoiceField(queryset=ImportSource.objects.filter(is_active=True).order_by('name'))
    file = forms.FileField(required=False)
    clipboard_text = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 10, 'placeholder': 'Paste Finstore rows from clipboard here'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')

    def clean(self):
        cleaned_data = super().clean()
        uploaded_file = cleaned_data.get('file')
        clipboard_text = (cleaned_data.get('clipboard_text') or '').strip()
        source = cleaned_data.get('source')

        if not uploaded_file and not clipboard_text:
            raise forms.ValidationError('Attach a file or paste clipboard data.')

        if clipboard_text and uploaded_file:
            raise forms.ValidationError('Use either a file upload or clipboard import, not both at once.')

        if clipboard_text and source and 'finstore' not in source.code.lower():
            raise forms.ValidationError('Clipboard import is currently supported only for Finstore sources.')

        cleaned_data['clipboard_text'] = clipboard_text
        return cleaned_data


class PriorlifeManualUpdateForm(forms.Form):
    def __init__(self, products, *args, **kwargs):
        self.products = list(products)
        super().__init__(*args, **kwargs)
        for product in self.products:
            account_number = product.external_id
            premium_default = (product.metadata or {}).get('premium_amount') or '25'
            self.fields[f'{account_number}_payment_date'] = forms.DateField(
                label=f'{product.name} — дата взноса',
                required=False,
                widget=forms.DateInput(attrs={'type': 'date'}),
            )
            self.fields[f'{account_number}_accumulated_amount'] = forms.DecimalField(
                label=f'{product.name} — сумма продукта, USD',
                required=False,
                min_value=0,
                decimal_places=2,
                max_digits=12,
            )
            self.fields[f'{account_number}_premium_amount'] = forms.DecimalField(
                label=f'{product.name} — взнос, USD',
                min_value=0,
                decimal_places=2,
                max_digits=12,
                required=False,
                initial=premium_default,
            )
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')

    def clean(self):
        cleaned_data = super().clean()
        updates = []
        for product in self.products:
            account_number = product.external_id
            payment_date = cleaned_data.get(f'{account_number}_payment_date')
            accumulated_amount = cleaned_data.get(f'{account_number}_accumulated_amount')
            premium_amount = cleaned_data.get(f'{account_number}_premium_amount')
            if payment_date is None and accumulated_amount in (None, ''):
                continue
            if payment_date is None or accumulated_amount in (None, ''):
                raise forms.ValidationError(
                    f'Укажите дату взноса и сумму продукта для договора {account_number}.'
                )
            if premium_amount in (None, ''):
                premium_amount = (product.metadata or {}).get('premium_amount') or '25'
            updates.append(
                {
                    'account_number': account_number,
                    'payment_date': payment_date,
                    'premium_amount': premium_amount,
                    'accumulated_amount': accumulated_amount,
                }
            )
        if not updates:
            raise forms.ValidationError('Заполните данные хотя бы по одному договору.')
        self.contract_updates = updates
        return cleaned_data

    def cleaned_contract_updates(self):
        return getattr(self, 'contract_updates', [])

    def field_groups(self):
        groups = []
        for product in self.products:
            account_number = product.external_id
            groups.append(
                {
                    'product': product,
                    'payment_date': self[f'{account_number}_payment_date'],
                    'accumulated_amount': self[f'{account_number}_accumulated_amount'],
                    'premium_amount': self[f'{account_number}_premium_amount'],
                }
            )
        return groups