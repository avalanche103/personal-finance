from django.contrib import admin

from apps.imports.models import ImportJob, ImportSource, RawImportFile


@admin.register(ImportSource)
class ImportSourceAdmin(admin.ModelAdmin):
	list_display = ('name', 'code', 'source_type', 'institution', 'is_active')
	list_filter = ('source_type', 'is_active')
	search_fields = ('name', 'code', 'institution__name')


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
	list_display = ('created_at', 'source', 'status', 'file_type', 'rows_detected', 'records_created')
	list_filter = ('status', 'file_type', 'source__source_type')
	search_fields = ('original_filename', 'idempotency_key', 'source__name')
	autocomplete_fields = ('source', 'institution')


@admin.register(RawImportFile)
class RawImportFileAdmin(admin.ModelAdmin):
	list_display = ('original_filename', 'source', 'file_type', 'checksum', 'size_bytes', 'created_at')
	list_filter = ('file_type',)
	search_fields = ('original_filename', 'checksum', 'stored_path')
	autocomplete_fields = ('job', 'source')
