from django import forms

from apps.imports.models import ImportSource


class ImportUploadForm(forms.Form):
    source = forms.ModelChoiceField(queryset=ImportSource.objects.filter(is_active=True).order_by('name'))
    file = forms.FileField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')