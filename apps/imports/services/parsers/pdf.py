from pathlib import Path

from apps.imports.services.parsers.base import BaseImportParser, ParseResult


class PDFImportParser(BaseImportParser):
    parser_name = 'pdf-parser'
    supported_extensions = ('pdf',)

    def parse(self, raw_import_file):
        try:
            from pypdf import PdfReader
        except ImportError:
            return ParseResult(warnings=['pypdf is not installed yet. PDF parsing is disabled.'])

        file_path = Path(raw_import_file.stored_path)
        reader = PdfReader(file_path)
        preview_text = []
        for page in reader.pages[:3]:
            preview_text.append((page.extract_text() or '').strip())

        excerpt = '\n'.join(chunk for chunk in preview_text if chunk)[:1000]
        return ParseResult(
            records=[],
            warnings=['PDF imports require manual validation before any database save step.'],
            metadata={'pages': len(reader.pages), 'excerpt': excerpt},
        )