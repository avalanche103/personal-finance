from pathlib import Path

from apps.imports.services.parsers.base import ParseResult
from apps.imports.services.parsers.xls import XLSImportParser


class TextImportParser(XLSImportParser):
    parser_name = 'text-parser'
    supported_extensions = ('txt', 'tsv')

    def parse(self, raw_import_file):
        raw_bytes = Path(raw_import_file.stored_path).read_bytes()
        text = self._decode_text(raw_bytes)
        if self._looks_like_finstore_clipboard(raw_import_file, text):
            return self._parse_finstore_clipboard_text(text)
        return ParseResult(warnings=['No parser registered for this text content.'])

    def _decode_text(self, raw_bytes: bytes) -> str:
        for encoding in ('utf-8-sig', 'utf-8', 'cp1251'):
            try:
                return raw_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw_bytes.decode('latin-1')

    def _looks_like_finstore_clipboard(self, raw_import_file, text: str) -> bool:
        filename = (raw_import_file.original_filename or '').lower()
        source_code = (raw_import_file.source.code if raw_import_file.source else '').lower()
        lowered = text.lower()
        return (
            'finstore' in filename
            or 'finstore' in source_code
            or ('вид операции' in lowered and 'название токена' in lowered and 'сумма валюты' in lowered)
        )

    def _parse_finstore_clipboard_text(self, text: str):
        records = []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or '\t' not in line:
                continue

            columns = [value.strip() for value in raw_line.split('\t')]
            while len(columns) < 5:
                columns.append('')

            if columns[0] == 'Вид операции':
                continue
            if not columns[0]:
                continue
            if not columns[1] and columns[0] != 'Пополнение кошелька':
                continue

            token_meta = self._parse_token_name(columns[1])
            amount, amount_currency = self._parse_amount_cell(columns[3])
            occurred_at = self._parse_excel_datetime(columns[4])
            quantity = self._normalize_quantity(columns[2])

            records.append(
                {
                    'row_number': line_number,
                    'operation_type': columns[0],
                    'token_name': columns[1],
                    'token_symbol': token_meta['symbol'],
                    'token_currency': self._normalize_currency_code(token_meta['currency']),
                    'token_id': token_meta['token_id'],
                    'quantity': quantity,
                    'amount': str(amount) if amount is not None else '',
                    'amount_currency': self._normalize_currency_code(amount_currency),
                    'occurred_at': occurred_at,
                }
            )

        return ParseResult(
            records=records[:25],
            warnings=['Finstore clipboard history detected. Rows will be saved the same way as file imports.'],
            metadata={
                'parser_variant': 'finstore-history',
                'rows': len(records),
                'token_count': len({record['token_name'] for record in records if record['token_name']}),
                'operation_types': sorted({record['operation_type'] for record in records}),
                'import_channel': 'clipboard',
            },
            artifacts={'normalized_records': records},
        )