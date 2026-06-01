from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel


class ImportSource(TimeStampedModel):
	class SourceType(models.TextChoices):
		API = 'api', 'API'
		PDF = 'pdf', 'PDF'
		XLS = 'xls', 'XLS/XLSX'
		MANUAL = 'manual', 'Manual'

	institution = models.ForeignKey('institutions.FinancialInstitution', null=True, blank=True, on_delete=models.CASCADE, related_name='import_sources')
	name = models.CharField(max_length=255)
	code = models.CharField(max_length=64, unique=True)
	source_type = models.CharField(max_length=16, choices=SourceType.choices, default=SourceType.MANUAL)
	config = models.JSONField(default=dict, blank=True)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ['name']

	def __str__(self) -> str:
		return self.name


class ImportJob(TimeStampedModel):
	class Status(models.TextChoices):
		PENDING = 'pending', 'Pending'
		PARSING = 'parsing', 'Parsing'
		VALIDATED = 'validated', 'Validated'
		SAVED = 'saved', 'Saved'
		FAILED = 'failed', 'Failed'

	source = models.ForeignKey(ImportSource, on_delete=models.CASCADE, related_name='jobs')
	institution = models.ForeignKey('institutions.FinancialInstitution', null=True, blank=True, on_delete=models.SET_NULL, related_name='import_jobs')
	status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
	file_type = models.CharField(max_length=16, blank=True)
	parser_name = models.CharField(max_length=128, blank=True)
	original_filename = models.CharField(max_length=255, blank=True)
	idempotency_key = models.CharField(max_length=128)
	rows_detected = models.PositiveIntegerField(default=0)
	records_created = models.PositiveIntegerField(default=0)
	details = models.JSONField(default=dict, blank=True)
	error_message = models.TextField(blank=True)
	started_at = models.DateTimeField(null=True, blank=True)
	finished_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		ordering = ['-created_at']
		constraints = [
			models.UniqueConstraint(fields=['source', 'idempotency_key'], name='unique_import_job_per_source_key'),
		]

	def __str__(self) -> str:
		return f'{self.source} - {self.status}'


class RawImportFile(TimeStampedModel):
	job = models.ForeignKey(ImportJob, on_delete=models.CASCADE, related_name='raw_files')
	source = models.ForeignKey(ImportSource, null=True, blank=True, on_delete=models.SET_NULL, related_name='raw_files')
	original_filename = models.CharField(max_length=255)
	stored_path = models.CharField(max_length=512)
	file_type = models.CharField(max_length=16)
	mime_type = models.CharField(max_length=128, blank=True)
	checksum = models.CharField(max_length=128)
	size_bytes = models.PositiveBigIntegerField(default=0)
	metadata = models.JSONField(default=dict, blank=True)

	class Meta:
		ordering = ['-created_at']
		constraints = [
			models.UniqueConstraint(
				fields=['source', 'checksum'],
				condition=Q(source__isnull=False),
				name='unique_raw_import_checksum_per_source',
			),
		]

	def __str__(self) -> str:
		return self.original_filename
