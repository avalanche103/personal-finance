from pathlib import Path

from apps.common.services.stravita_pension import (
	is_stravita_contributions_text,
	is_stravita_extract_text,
	parse_stravita_contributions,
	parse_stravita_extract,
	persist_stravita_contributions,
	persist_stravita_extract,
)
from apps.imports.services.parsers.base import BaseImportParser, ParseResult


class PDFImportParser(BaseImportParser):
	parser_name = 'pdf-parser'
	supported_extensions = ('pdf',)

	def _read_preview_text(self, file_path: Path, *, max_pages: int = 3) -> str:
		try:
			import pdfplumber
		except ImportError:
			from pypdf import PdfReader

			reader = PdfReader(file_path)
			chunks = []
			for page in reader.pages[:max_pages]:
				chunks.append((page.extract_text() or '').strip())
			return '\n'.join(chunk for chunk in chunks if chunk)

		chunks = []
		with pdfplumber.open(file_path) as pdf:
			for page in pdf.pages[:max_pages]:
				chunks.append(page.extract_text() or '')
		return '\n'.join(chunks)

	def parse(self, raw_import_file):
		file_path = Path(raw_import_file.stored_path)
		preview_text = self._read_preview_text(file_path)

		parser_hint = ''
		if raw_import_file.source and isinstance(raw_import_file.source.config, dict):
			parser_hint = str(raw_import_file.source.config.get('parser', '')).strip()

		if parser_hint == 'stravita-extract' or is_stravita_extract_text(preview_text):
			return parse_stravita_extract(file_path)
		if parser_hint == 'stravita-contributions' or is_stravita_contributions_text(preview_text):
			return parse_stravita_contributions(file_path)

		try:
			from pypdf import PdfReader
		except ImportError:
			return ParseResult(warnings=['pypdf is not installed yet. PDF parsing is disabled.'])

		reader = PdfReader(file_path)
		excerpt = preview_text[:1000]
		if not excerpt:
			for page in reader.pages[:3]:
				excerpt += (page.extract_text() or '').strip()
			excerpt = excerpt[:1000]

		return ParseResult(
			records=[],
			warnings=['PDF imports require manual validation before any database save step.'],
			metadata={'pages': len(reader.pages), 'excerpt': excerpt},
		)

	def persist(self, raw_import_file, result: ParseResult) -> int:
		parser_variant = result.metadata.get('parser_variant')
		management_expense_pct = None
		if raw_import_file.source and isinstance(raw_import_file.source.config, dict):
			raw_pct = raw_import_file.source.config.get('management_expense_pct')
			if raw_pct not in (None, ''):
				from decimal import Decimal

				management_expense_pct = Decimal(str(raw_pct))

		if parser_variant == 'stravita-extract':
			return persist_stravita_extract(
				raw_import_file,
				result,
				management_expense_pct=management_expense_pct,
			)
		if parser_variant == 'stravita-contributions':
			return persist_stravita_contributions(raw_import_file, result)
		return 0
