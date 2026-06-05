from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from apps.accounts.models import Account, Transaction
from apps.common.models import Currency
from apps.common.services.aigenis_bonds import apply_aigenis_indexed_bond_defaults
from apps.common.services.aigenis_reconciliation import canonical_aigenis_security_name, reconcile_aigenis_products
from apps.products.models import Product
from apps.imports.services.parsers.base import BaseImportParser, ParseResult
from django.utils import timezone


class XLSImportParser(BaseImportParser):
    parser_name = 'xls-parser'
    supported_extensions = ('xls', 'xlsx')
    finstore_token_pattern = re.compile(r'^(?P<symbol>.+?)_\((?P<currency>[A-Z]{3})_(?P<token_id>\d+)\)$')
    finstore_amount_pattern = re.compile(r'(?P<amount>-?[0-9]+(?:[\.,][0-9]+)?)\s+(?P<currency>[A-Z]{3})(?:\.sc)?')
    iso_date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')

    def parse(self, raw_import_file):
        try:
            import pandas as pd
        except ImportError:
            return ParseResult(warnings=['pandas is not installed yet. XLS/XLSX parsing is disabled.'])

        file_path = Path(raw_import_file.stored_path)
        dataframe = pd.read_excel(file_path, header=None)
        if self._looks_like_finstore_history(raw_import_file, dataframe):
            return self._parse_finstore_history(dataframe)
        if self._looks_like_aigenis_report(raw_import_file, dataframe):
            return self._parse_aigenis_report(dataframe)

        dataframe = pd.read_excel(file_path)
        preview = dataframe.head(5).fillna('').to_dict(orient='records')
        return ParseResult(
            records=preview,
            warnings=['XLS/XLSX import is in skeleton mode. Validate the preview before saving transactions.'],
            metadata={'columns': list(dataframe.columns), 'rows': int(len(dataframe))},
        )

    def persist(self, raw_import_file, result: ParseResult) -> int:
        parser_variant = result.metadata.get('parser_variant')
        if parser_variant == 'finstore-history':
            return self._persist_finstore_history(raw_import_file, result)
        if parser_variant == 'aigenis-report':
            return self._persist_aigenis_report(raw_import_file, result)
        return 0

    def _persist_finstore_history(self, raw_import_file, result: ParseResult) -> int:
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

        from apps.accounts.services.balance import sync_account_balance

        accounts_synced = 0
        for account in finstore_accounts.values():
            if sync_account_balance(account):
                accounts_synced += 1
            else:
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

    def _persist_aigenis_report(self, raw_import_file, result: ParseResult) -> int:
        institution = None
        if raw_import_file.source and raw_import_file.source.institution_id:
            institution = raw_import_file.source.institution
        elif raw_import_file.job and raw_import_file.job.institution_id:
            institution = raw_import_file.job.institution

        if institution is None:
            return 0

        aigenis_accounts = {
            account.currency.code: account
            for account in Account.objects.select_related('currency').filter(institution=institution)
        }
        security_summaries: dict[str, dict] = {}
        pending_transactions: list[dict] = []
        for row in result.artifacts.get('normalized_records', []):
            operation_type = row.get('operation_type', '')
            if operation_type == 'Входящий':
                continue

            amount_decimal = self._to_decimal(row.get('amount')) if row.get('amount') not in ('', None) else Decimal('0')
            quantity_decimal = self._to_decimal(row.get('quantity')) if row.get('quantity') not in ('', None) else Decimal('0')
            occurred_at = row.get('occurred_at', '')
            amount_currency_code = self._normalize_currency_code(row.get('currency', 'BYN'))
            isin = row.get('isin', '')
            security_name = row.get('security_name', '')

            account = aigenis_accounts.get(amount_currency_code)
            transaction_amount, transaction_type = self._build_aigenis_transaction_payload(operation_type, amount_decimal)
            position_quantity = self._build_aigenis_position_quantity(operation_type, quantity_decimal)
            unit_price = self._to_decimal(row.get('unit_price')) if row.get('unit_price') not in ('', None) else Decimal('0')

            if isin:
                summary = security_summaries.setdefault(
                    isin,
                    {
                        'security_name': security_name,
                        'issuer': row.get('issuer', ''),
                        'security_type': row.get('security_type', ''),
                        'currency_code': amount_currency_code or 'BYN',
                        'units': Decimal('0'),
                        'operations': set(),
                        'history_rows': 0,
                        'first_operation_at': occurred_at,
                        'last_operation_at': occurred_at,
                        'latest_unit_price': Decimal('0'),
                        'latest_price_at': '',
                    },
                )
                summary['history_rows'] += 1
                summary['operations'].add(operation_type)
                if security_name:
                    summary['security_name'] = canonical_aigenis_security_name(summary.get('security_name', ''), security_name)
                if row.get('issuer'):
                    summary['issuer'] = row.get('issuer', '')
                summary['units'] += position_quantity
                if position_quantity > 0 and unit_price > 0 and (not summary['latest_price_at'] or occurred_at >= summary['latest_price_at']):
                    summary['latest_unit_price'] = unit_price
                    summary['latest_price_at'] = occurred_at
                if occurred_at:
                    if not summary['first_operation_at'] or occurred_at < summary['first_operation_at']:
                        summary['first_operation_at'] = occurred_at
                    if not summary['last_operation_at'] or occurred_at > summary['last_operation_at']:
                        summary['last_operation_at'] = occurred_at

            if account is not None and occurred_at and operation_type != 'Входящий':
                pending_transactions.append(
                    {
                        'row_number': row.get('row_number'),
                        'fingerprint_suffix': '',
                        'isin': isin,
                        'security_name': security_name,
                        'account': account,
                        'transaction_type': transaction_type,
                        'amount': transaction_amount,
                        'quantity': position_quantity,
                        'unit_price': unit_price,
                        'occurred_at': occurred_at,
                        'amount_currency_code': amount_currency_code,
                        'raw_amount': row.get('amount', ''),
                        'description': self._build_aigenis_transaction_description(row),
                        'operation_type': operation_type,
                        'issuer': row.get('issuer', ''),
                        'security_type': row.get('security_type', ''),
                        'fee_metadata': {},
                    }
                )

                fee_total = self._aigenis_fee_total(row)
                if fee_total > 0:
                    pending_transactions.append(
                        {
                            'row_number': row.get('row_number'),
                            'fingerprint_suffix': ':fee',
                            'isin': isin,
                            'security_name': security_name,
                            'account': account,
                            'transaction_type': Transaction.TransactionType.FEE,
                            'amount': -fee_total,
                            'quantity': Decimal('0'),
                            'unit_price': Decimal('0'),
                            'occurred_at': occurred_at,
                            'amount_currency_code': amount_currency_code,
                            'raw_amount': str(fee_total),
                            'description': self._build_aigenis_fee_description(row),
                            'operation_type': operation_type,
                            'issuer': row.get('issuer', ''),
                            'security_type': row.get('security_type', ''),
                            'fee_metadata': {
                                'broker_fee': row.get('broker_fee', ''),
                                'exchange_fee': row.get('exchange_fee', ''),
                                'clearing_fee': row.get('clearing_fee', ''),
                                'other_expenses': row.get('other_expenses', ''),
                            },
                        }
                    )

        products_created = 0
        product_map: dict[str, Product] = {}
        for isin, summary in security_summaries.items():
            currency = self._resolve_currency(summary['currency_code'])
            product, created = Product.objects.update_or_create(
                institution=institution,
                external_id=isin,
                defaults={
                    'name': summary['security_name'] or isin,
                    'symbol': isin[:32],
                    'isin': isin,
                    'product_type': Product.ProductType.BOND,
                    'currency': currency,
                    'units': summary['units'],
                    'current_price': summary['latest_unit_price'],
                    'current_value_usd': Decimal('0'),
                    'is_active': summary['units'] > 0,
                    'metadata': {
                        'imported_from': 'aigenis-report',
                        'issuer': summary['issuer'],
                        'security_type': summary['security_type'],
                        'history_rows': summary['history_rows'],
                        'operation_types': sorted(value for value in summary['operations'] if value),
                        'first_operation_at': summary['first_operation_at'],
                        'last_operation_at': summary['last_operation_at'],
                        'latest_price_at': summary['latest_price_at'],
                    },
                },
            )
            apply_aigenis_indexed_bond_defaults(product)
            product_map[isin] = product
            if created:
                products_created += 1

        transactions_created = 0
        for transaction_row in pending_transactions:
            metadata = {
                'imported_from': 'aigenis-report',
                'operation_type': transaction_row['operation_type'],
                'security_name': transaction_row['security_name'],
                'isin': transaction_row['isin'],
                'issuer': transaction_row['issuer'],
                'security_type': transaction_row['security_type'],
                'amount_currency': transaction_row['amount_currency_code'],
                'raw_amount': transaction_row['raw_amount'],
            }
            metadata.update(transaction_row.get('fee_metadata') or {})
            _, was_created = Transaction.objects.update_or_create(
                import_fingerprint=(
                    f"aigenis:{raw_import_file.checksum}:{transaction_row['row_number']}"
                    f"{transaction_row.get('fingerprint_suffix', '')}"
                ),
                defaults={
                    'account': transaction_row['account'],
                    'product': product_map.get(transaction_row['isin']),
                    'import_job': raw_import_file.job,
                    'transaction_type': transaction_row['transaction_type'],
                    'currency': transaction_row['account'].currency,
                    'amount': transaction_row['amount'],
                    'amount_usd': Decimal('0'),
                    'quantity': transaction_row['quantity'],
                    'unit_price': transaction_row['unit_price'],
                    'occurred_at': transaction_row['occurred_at'],
                    'description': transaction_row['description'],
                    'metadata': metadata,
                },
            )
            if was_created:
                transactions_created += 1

        from apps.accounts.services.balance import sync_account_balance

        accounts_synced = 0
        for account in aigenis_accounts.values():
            sync_account_balance(account)
            accounts_synced += 1

        reconcile_aigenis_products(
            institution_id=institution.id,
            isins=sorted(security_summaries.keys()),
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

    def _looks_like_aigenis_report(self, raw_import_file, dataframe) -> bool:
        filename = (raw_import_file.original_filename or '').lower()
        source_code = (raw_import_file.source.code if raw_import_file.source else '').lower()
        if 'aigenis' in filename or 'aigenis' in source_code:
            return True

        normalized_rows = dataframe.fillna('').astype(str).values.tolist()[:10]
        for row in normalized_rows:
            if len(row) >= 2 and row[0].strip() == 'Дата совершения операции' and row[1].strip() == 'Тип операции':
                return True
        return False

    def _parse_aigenis_report(self, dataframe):
        records = []
        header_seen = False
        report_meta = self._extract_aigenis_report_meta(dataframe)

        for index, raw_row in dataframe.fillna('').iterrows():
            row = [self._normalize_cell(value) for value in raw_row.tolist()]
            if len(row) < 12:
                continue

            if row[0] == 'Дата совершения операции' and row[1] == 'Тип операции':
                header_seen = True
                continue

            if not header_seen:
                continue

            if row[0] in ('1', '2', '3'):
                continue

            if not row[0] or not row[1]:
                continue

            if row[0].startswith('*') or row[0].startswith('**'):
                continue

            occurred_at = self._parse_aigenis_date(raw_row.iloc[0])
            fee_fields = self._parse_aigenis_fee_fields(row)
            records.append(
                {
                    'row_number': int(index) + 1,
                    'operation_type': row[1],
                    'occurred_at': occurred_at,
                    'security_type': row[3],
                    'trading_mode': row[4],
                    'issuer': row[5],
                    'security_name': row[6],
                    'isin': row[7],
                    'currency': self._normalize_currency_code(row[8] or 'BYN'),
                    'unit_price': self._normalize_quantity(row[9]) if row[9] else '',
                    'quantity': self._normalize_quantity(row[10]) if row[10] else '',
                    'amount': self._normalize_quantity(row[11]) if row[11] else '',
                    'deposit_source': row[3] if row[1] == 'Пополнение д.с.' else '',
                    **fee_fields,
                }
            )

        return ParseResult(
            records=records[:25],
            warnings=['Aigenis broker report detected. Bond products will be created for each unique ISIN found in the operation history.'],
            metadata={
                'parser_variant': 'aigenis-report',
                'rows': len(records),
                'security_count': len({record['isin'] for record in records if record['isin']}),
                'operation_types': sorted({record['operation_type'] for record in records}),
                **report_meta,
            },
            artifacts={'normalized_records': records},
        )

    def _extract_aigenis_report_meta(self, dataframe) -> dict:
        meta = {}
        for _, raw_row in dataframe.fillna('').head(6).iterrows():
            row = [self._normalize_cell(value) for value in raw_row.tolist()]
            if len(row) < 4:
                continue
            if row[1] == 'Клиент' and row[2]:
                meta['client_name'] = row[2]
            if row[1] == 'Договор №' and row[2]:
                meta['contract_number'] = row[2]
                meta['contract_date'] = row[3]
            if row[1] == 'Период с' and row[2]:
                meta['period_from'] = row[2]
                meta['period_to'] = row[3].removeprefix('по ').strip()
        return meta

    def _build_aigenis_transaction_payload(self, operation_type: str, amount_decimal: Decimal) -> tuple[Decimal, str]:
        if operation_type == 'Покупка':
            return -abs(amount_decimal), Transaction.TransactionType.TRADE
        if operation_type == 'Продажа':
            return abs(amount_decimal), Transaction.TransactionType.TRADE
        if operation_type in {'Пополнение д.с.', 'Зачисление д.с.'}:
            return abs(amount_decimal), Transaction.TransactionType.DEPOSIT
        if operation_type in {'Выплата дохода', 'Выплата купона', 'Вознаграждение'}:
            return abs(amount_decimal), Transaction.TransactionType.INCOME
        return amount_decimal, Transaction.TransactionType.OTHER

    def _build_aigenis_position_quantity(self, operation_type: str, quantity_decimal: Decimal) -> Decimal:
        if operation_type == 'Продажа':
            return -abs(quantity_decimal)
        return quantity_decimal

    def _parse_aigenis_fee_fields(self, row: list[str]) -> dict:
        return {
            'broker_fee': self._normalize_quantity(row[12]) if len(row) > 12 and row[12] else '',
            'exchange_fee': self._normalize_quantity(row[15]) if len(row) > 15 and row[15] else '',
            'clearing_fee': self._normalize_quantity(row[16]) if len(row) > 16 and row[16] else '',
            'other_expenses': self._normalize_quantity(row[17]) if len(row) > 17 and row[17] else '',
        }

    def _aigenis_fee_total(self, row: dict) -> Decimal:
        total = Decimal('0')
        for field_name in ('broker_fee', 'exchange_fee', 'clearing_fee', 'other_expenses'):
            value = row.get(field_name, '')
            if value not in ('', None):
                total += self._to_decimal(value)
        return total

    def _build_aigenis_fee_description(self, row: dict) -> str:
        parts = []
        labels = {
            'broker_fee': 'брокер',
            'exchange_fee': 'биржа',
            'clearing_fee': 'клиринг',
            'other_expenses': 'прочие',
        }
        for field_name, label in labels.items():
            value = row.get(field_name, '')
            if value not in ('', None):
                parts.append(f'{label} {value}')
        operation_type = row.get('operation_type', '')
        security_name = row.get('security_name', '')
        fee_summary = ', '.join(parts)
        if security_name:
            return f'Комиссии ({operation_type}: {security_name}): {fee_summary}'
        return f'Комиссии ({operation_type}): {fee_summary}'

    def _parse_aigenis_date(self, value) -> str:
        if value in ('', None):
            return ''

        text = self._normalize_cell(value)
        if not self.iso_date_pattern.match(text):
            return self._parse_excel_datetime(value)

        try:
            import pandas as pd

            parsed = pd.to_datetime(text, format='%Y-%m-%d', errors='coerce')
        except Exception:
            return ''

        if getattr(parsed, 'isoformat', None) is None or pd.isna(parsed):
            return ''
        if getattr(parsed, 'tzinfo', None) is None:
            parsed = parsed.floor('us')
            parsed = timezone.make_aware(parsed.to_pydatetime(), timezone.get_current_timezone())
        return parsed.isoformat()

    def _build_aigenis_transaction_description(self, row: dict) -> str:
        operation_type = row.get('operation_type', '')
        security_name = row.get('security_name', '')
        isin = row.get('isin', '')
        if security_name:
            return f'{operation_type}: {security_name} ({isin})' if isin else f'{operation_type}: {security_name}'
        if row.get('deposit_source'):
            return f'{operation_type}: {row["deposit_source"]}'
        return operation_type

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