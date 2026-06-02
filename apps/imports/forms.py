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