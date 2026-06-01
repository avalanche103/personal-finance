from django.contrib import admin

from apps.institutions.models import FinancialInstitution


@admin.register(FinancialInstitution)
class FinancialInstitutionAdmin(admin.ModelAdmin):
	list_display = ('name', 'institution_type', 'country', 'base_currency', 'is_active')
	list_filter = ('institution_type', 'country', 'is_active')
	search_fields = ('name', 'slug', 'website')
	prepopulated_fields = {'slug': ('name',)}
