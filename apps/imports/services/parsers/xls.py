from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from django.db.models import Sum

from apps.accounts.models import Account, Transaction
from apps.common.models import Currency
from apps.products.models import Product
from apps.imports.services.parsers.base import BaseImportParser, ParseResult
from django.utils import timezone


class XLSImportParser(BaseImportParser):
    parser_name = 'xls-parser'
    supported_extensions = ('xls', 'xlsx')
    finstore_token_pattern = re.compile(r'^(?P<symbol>.+?)_\((?P<currency>[A-Z]{3})_(?P<token_id>\d+)\)$')
    finstore_amount_pattern = re.compile(r'(?P<amount>-?[0-9]+(?:[\.,][0-9]+)?)\s+(?P<currency>[A-Z]{3})(?:\.sc)?')

    def parse(self, raw_import_file):
        try:
            import pandas as pd
        except ImportError:
            return ParseResult(warnings=['pandas is not installed yet. XLS/XLSX parsing is disabled.'])

        file_path = Path(raw_import_file.stored_path)
        finstore_dataframe = pd.read_excel(file_path, header=None)
        if self._looks_like_finstore_history(raw_import_file, finstore_dataframe):
            return self._parse_finstore_history(finstore_dataframe)

        dataframe = pd.read_excel(file_path)
        preview = dataframe.head(5).fillna('').to_dict(orient='records')
        return ParseResult(
            records=preview,
            warnings=['XLS/XLSX import is in skeleton mode. Validate the preview before saving transactions.'],
            metadata={'columns': list(dataframe.columns), 'rows': int(len(dataframe))},
        )

    def persist(self, raw_import_file, result: ParseResult) -> int:
        if result.metadata.get('parser_variant') != 'finstore-history':
            return 0

        institution = None
        if raw_import_file.source and raw_import_file.source.institution_id:
            institution = raw_import_file.source.institution
        elif raw_import_file.job and raw_import_file.job.institution_id:
            institution = raw_import_file.job.institution

        if institution is None:
            return 0

        finstore_accounts = {
            account.currency.code: account
            for account in Account.objects.select_related('currency').filter(institution=institution)
        }
        token_summaries: dict[str, dict] = {}
        pending_transactions: list[dict] = []
        for row in result.artifacts.get('normalized_records', []):
            token_name = row.get('token_name')

            amount_decimal = self._to_decimal(row.get('amount')) if row.get('amount') not in ('', None) else Decimal('0')
            quantity = row.get('quantity')
            quantity_decimal = self._to_decimal(quantity) if quantity not in ('', None) else Decimal('0')
            position_quantity = self._build_finstore_position_quantity(row, quantity_decimal)
            occurred_at = row.get('occurred_at', '')
            amount_currency_code = self._normalize_currency_code(row.get('amount_currency', ''))

            account = finstore_accounts.get(amount_currency_code)
            transaction_amount, transaction_type = self._build_finstore_transaction_payload(row, amount_decimal)

            if not token_name:
                if account is not None and occurred_at:
                    pending_transactions.append(
                        {
                            'row_number': row.get('row_number'),
                            'token_name': '',
                            'account': account,
                            'transaction_type': transaction_type,
                            'amount': transaction_amount,
                            'quantity': position_quantity,
                            'unit_price': Decimal('0'),
                            'occurred_at': occurred_at,
                            'amount_currency_code': amount_currency_code,
                            'raw_amount': row.get('amount', ''),
                            'token_id': row.get('token_id', ''),
                            'description': self._build_transaction_description(row),
                            'operation_type': row.get('operation_type', ''),
                        }
                    )
                continue

            summary = token_summaries.setdefault(
                token_name,
                {
                    'symbol': row.get('token_symbol', ''),
                    'currency_code': row.get('token_currency') or row.get('amount_currency') or 'USD',
                    'token_id': row.get('token_id', ''),
                    'units': Decimal('0'),
                    'operations': set(),
                    'history_rows': 0,
                    'first_operation_at': row.get('occurred_at', ''),
                    'last_operation_at': row.get('occurred_at', ''),
                    'latest_unit_price': Decimal('0'),
                    'latest_price_at': '',
                },
            )
            summary['history_rows'] += 1
            summary['operations'].add(row.get('operation_type', ''))

            if quantity not in ('', None):
                summary['units'] += position_quantity

            if position_quantity > 0 and amount_decimal > 0 and (not summary['latest_price_at'] or occurred_at >= summary['latest_price_at']):
                summary['latest_unit_price'] = amount_decimal / quantity_decimal
                summary['latest_price_at'] = occurred_at

            if occurred_at:
                if not summary['first_operation_at'] or occurred_at < summary['first_operation_at']:
                    summary['first_operation_at'] = occurred_at
                if not summary['last_operation_at'] or occurred_at > summary['last_operation_at']:
                    summary['last_operation_at'] = occurred_at

            if account is not None and occurred_at:
                pending_transactions.append(
                    {
                        'row_number': row.get('row_number'),
                        'token_name': token_name,
                        'account': account,
                        'transaction_type': transaction_type,
                        'amount': transaction_amount,
                        'quantity': position_quantity,
                        'unit_price': summary['latest_unit_price'],
                        'occurred_at': occurred_at,
                        'amount_currency_code': amount_currency_code,
                        'raw_amount': row.get('amount', ''),
                        'token_id': row.get('token_id', ''),
                        'description': self._build_transaction_description(row),
                        'operation_type': row.get('operation_type', ''),
                    }
                )

        products_created = 0
        product_map: dict[str, Product] = {}
        for token_name, summary in token_summaries.items():
            currency = self._resolve_currency(summary['currency_code'])
            product, created = Product.objects.update_or_create(
                institution=institution,
                external_id=token_name,
                defaults={
                    'name': token_name,
                    'symbol': summary['symbol'][:32],
                    'product_type': Product.ProductType.TOKEN,
                    'currency': currency,
                    'units': summary['units'],
                    'current_price': summary['latest_unit_price'],
                    'current_value_usd': Decimal('0'),
                    'is_active': summary['units'] > 0,
                    'metadata': {
                        'imported_from': 'finstore-history',
                        'token_id': summary['token_id'],
                        'history_rows': summary['history_rows'],
                        'operation_types': sorted(value for value in summary['operations'] if value),
                        'first_operation_at': summary['first_operation_at'],
                        'last_operation_at': summary['last_operation_at'],
                        'latest_price_at': summary['latest_price_at'],
                    },
                },
            )
            product_map[token_name] = product
            if created:
                products_created += 1

        transactions_created = 0
        for transaction_row in pending_transactions:
            _, was_created = Transaction.objects.update_or_create(
                import_fingerprint=f"finstore:{raw_import_file.checksum}:{transaction_row['row_number']}",
                defaults={
                    'account': transaction_row['account'],
                    'product': product_map.get(transaction_row['token_name']),
                    'import_job': raw_import_file.job,
                    'transaction_type': transaction_row['transaction_type'],
                    'currency': transaction_row['account'].currency,
                    'amount': transaction_row['amount'],
                    'amount_usd': Decimal('0'),
                    'quantity': transaction_row['quantity'],
                    'unit_price': transaction_row['unit_price'],
                    'occurred_at': transaction_row['occurred_at'],
                    'description': transaction_row['description'],
                    'metadata': {
                        'imported_from': 'finstore-history',
                        'operation_type': transaction_row['operation_type'],
                        'token_name': transaction_row['token_name'],
                        'token_id': transaction_row['token_id'],
                        'amount_currency': transaction_row['amount_currency_code'],
                        'raw_amount': transaction_row['raw_amount'],
                    },
                },
            )
            if was_created:
                transactions_created += 1

        from apps.common.services.finstore_reconciliation import reconcile_finstore_products

        transaction_totals = {
            row['account_id']: row['total'] or Decimal('0')
            for row in Transaction.objects.filter(account__in=finstore_accounts.values())
            .values('account_id')
            .annotate(total=Sum('amount'))
        }
        accounts_synced = 0
        for account in finstore_accounts.values():
            current_balance = transaction_totals.get(account.id, Decimal('0'))
            if account.current_balance != current_balance:
                account.current_balance = current_balance
                account.save(update_fields=['current_balance', 'updated_at'])
            accounts_synced += 1

        reconcile_finstore_products(
            institution_id=institution.id,
            token_names=sorted(token_summaries.keys()),
        )

        from apps.products.services.token_terms import recompute_next_income_dates

        result.metadata['next_income_dates_updated'] = recompute_next_income_dates(
            institution,
            overwrite=True,
        )

        result.metadata['products_created'] = products_created
        result.metadata['transactions_created'] = transactions_created
        result.metadata['accounts_synced'] = accounts_synced

        return products_created + transactions_created

    def _looks_like_finstore_history(self, raw_import_file, dataframe) -> bool:
        filename = (raw_import_file.original_filename or '').lower()
        source_code = (raw_import_file.source.code if raw_import_file.source else '').lower()
        if 'finstore' in filename or 'finstore' in source_code:
            return True

        normalized_rows = dataframe.fillna('').astype(str).values.tolist()[:5]
        for row in normalized_rows:
            if len(row) >= 4 and row[0].strip() == 'Вид операции' and row[1].strip() == 'Название токена' and row[2].strip() == 'Количество токенов' and row[3].strip() == 'Сумма валюты':
                return True
        return False

    def _parse_finstore_history(self, dataframe):
        records = []
        header_seen = False

        for index, raw_row in dataframe.fillna('').iterrows():
            row = [self._normalize_cell(value) for value in raw_row.tolist()]
            if len(row) < 4:
                continue

            if row[0] == 'Вид операции' and row[1] == 'Название токена' and row[2] == 'Количество токенов' and row[3] == 'Сумма валюты':
                header_seen = True
                continue

            if not header_seen or not any(row[:4]):
                continue

            if not row[0]:
                continue

            if not row[1] and row[0] != 'Пополнение кошелька':
                continue

            token_meta = self._parse_token_name(row[1])
            amount, amount_currency = self._parse_amount_cell(row[3])
            occurred_at = self._parse_excel_datetime(raw_row.iloc[4] if len(raw_row) > 4 else '')
            quantity = self._normalize_quantity(row[2])

            records.append(
                {
                    'row_number': int(index) + 1,
                    'operation_type': row[0],
                    'token_name': row[1],
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
            warnings=['Finstore history detected. Products will be created for each unique token found in the operation history.'],
            metadata={
                'parser_variant': 'finstore-history',
                'rows': len(records),
                'token_count': len({record['token_name'] for record in records if record['token_name']}),
                'operation_types': sorted({record['operation_type'] for record in records}),
            },
            artifacts={'normalized_records': records},
        )

    def _parse_token_name(self, token_name: str) -> dict:
        match = self.finstore_token_pattern.match(token_name)
        if not match:
            return {'symbol': token_name[:32], 'currency': '', 'token_id': ''}
        return match.groupdict()

    def _parse_amount_cell(self, raw_amount: str) -> tuple[Decimal | None, str]:
        match = self.finstore_amount_pattern.search(raw_amount)
        if not match:
            return None, ''
        amount = self._to_decimal(match.group('amount').replace(',', '.'))
        return amount, self._normalize_currency_code(match.group('currency'))

    def _normalize_currency_code(self, currency_code: str) -> str:
        if not currency_code:
            return ''
        return currency_code.replace('.sc', '').strip().upper()

    def _build_finstore_transaction_payload(self, row: dict, amount_decimal: Decimal) -> tuple[Decimal, str]:
        operation_type = row.get('operation_type', '')
        trade_operations = {'Покупка токенов', 'Покупка ICO токенов на Вторичном рынке'}
        income_operations = {'Получение дохода'}
        redemption_operations = {'Возврат инвестиций'}
        deposit_operations = {'Пополнение кошелька'}

        if operation_type in trade_operations:
            return -abs(amount_decimal), Transaction.TransactionType.TRADE
        if operation_type in redemption_operations:
            return abs(amount_decimal), Transaction.TransactionType.INCOME
        if operation_type in income_operations:
            return abs(amount_decimal), Transaction.TransactionType.INCOME
        if operation_type in deposit_operations:
            return abs(amount_decimal), Transaction.TransactionType.DEPOSIT
        return amount_decimal, Transaction.TransactionType.OTHER

    def _build_finstore_position_quantity(self, row: dict, quantity_decimal: Decimal) -> Decimal:
        operation_type = row.get('operation_type', '')
        if operation_type == 'Возврат инвестиций':
            return -abs(quantity_decimal)
        return quantity_decimal

    def _build_transaction_description(self, row: dict) -> str:
        token_name = row.get('token_name', '')
        operation_type = row.get('operation_type', '')
        if token_name:
            return f'{operation_type}: {token_name}'
        return operation_type

    def _parse_excel_datetime(self, value) -> str:
        if value in ('', None):
            return ''

        try:
            import pandas as pd

            if isinstance(value, str):
                normalized_value = value.strip().replace(',', '.')
                try:
                    numeric_value = float(normalized_value)
                except ValueError:
                    numeric_value = None

                if numeric_value is not None:
                    parsed = pd.to_datetime(numeric_value, unit='D', origin='1899-12-30', errors='coerce')
                else:
                    parsed = pd.to_datetime(value, errors='coerce', dayfirst=True)
            elif isinstance(value, (int, float, Decimal)):
                parsed = pd.to_datetime(value, unit='D', origin='1899-12-30', errors='coerce')
            else:
                parsed = pd.to_datetime(value, errors='coerce')
        except Exception:
            return ''

        if getattr(parsed, 'isoformat', None) is None or pd.isna(parsed):
            return ''
        if getattr(parsed, 'tzinfo', None) is None:
            parsed = parsed.floor('us')
            parsed = timezone.make_aware(parsed.to_pydatetime(), timezone.get_current_timezone())
        return parsed.isoformat()

    def _normalize_quantity(self, value: str) -> str:
        if value in ('', None):
            return ''
        return str(self._to_decimal(value.replace(',', '.')))

    def _normalize_cell(self, value) -> str:
        if value in (None, ''):
            return ''
        text = str(value).strip()
        if text.endswith('.0'):
            try:
                decimal_value = Decimal(text)
                if decimal_value == decimal_value.to_integral_value():
                    return str(decimal_value.quantize(Decimal('1')))
            except InvalidOperation:
                return text
        return text

    def _to_decimal(self, value) -> Decimal:
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return Decimal('0')

    def _resolve_currency(self, currency_code: str) -> Currency:
        currency_names = {
            'BYN': ('Belarusian Ruble', 'Br', Decimal('0.31')),
            'USD': ('US Dollar', '$', Decimal('1')),
            'EUR': ('Euro', 'EUR', Decimal('1.08')),
            'RUB': ('Russian Ruble', 'RUB', Decimal('0.011')),
        }
        name, symbol, usd_rate = currency_names.get(currency_code, (currency_code, currency_code, Decimal('1')))
        currency, _ = Currency.objects.get_or_create(
            code=currency_code,
            defaults={
                'name': name,
                'symbol': symbol,
                'usd_rate': usd_rate,
                'metadata': {'imported_from': 'finstore-history'},
            },
        )
        return currency